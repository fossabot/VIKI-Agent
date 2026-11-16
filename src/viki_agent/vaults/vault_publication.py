"""Durable orchestration for recoverable ordinary-vault file publication."""

from __future__ import annotations

import hashlib
import logging
import sqlite3
import uuid
from collections.abc import Callable, Iterable
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Literal, overload

from anyio.to_thread import run_sync

from viki_agent.persistence.feature_schemas import JOURNAL_SCHEMA as JOURNAL_SCHEMA
from viki_agent.storage import StateError
from viki_agent.vaults.vault import excluded_part, is_bot_memory_ref
from viki_agent.vaults.vault_mutation import (
    BACKUP_PREFIX,
    INSTRUCTION_REFS,
    MAX_PUBLICATION_BYTES,
    MAX_STAGE_BYTES,
    STAGE_PREFIX,
    STAGE_SUFFIX,
    FileFingerprint,
    MutationCoordinator,
    MutationError,
    MutationLease,
    MutationRootKind,
    OrdinaryMutationRoot,
    PublicationReservation,
    StagedPublication,
    VaultPublication,
    attachment_ref,
    code_ref,
)

if TYPE_CHECKING:
    from viki_agent.storage import SessionStore
    from viki_agent.workspaces import RowKind

logger = logging.getLogger(__name__)

PublicationKind = Literal["create", "replace"]
PublicationState = Literal["reserved", "staged", "prepared", "backed_up", "published", "conflict"]

# A vault client gives up on a write request at 15 s, so a save it sends waits at most this long
# for a busy vault's lease (a backup holds the whole root while it copies) and then refuses
# `vault_locked` before any vault write, rather than land later as a save the client has already
# reported failed. The other vault writers take the same deadline from `server.VAULT_LEASE_WAIT_S`.
REQUEST_LEASE_WAIT_S = 10.0

_COLUMNS = (
    "operation_id",
    "workspace_id",
    "root_physical_id",
    "policy_revision",
    "ref",
    "kind",
    "stage_name",
    "backup_name",
    "stage_physical_id",
    "stage_sha256",
    "stage_bytes",
    "target_physical_id",
    "expected_sha256",
    "target_bytes",
    "state",
    "created_at",
    "updated_at",
)


# Refusals before `prepared` that recovery retries at the next launch, with raw `OSError`.
# The root codes too: recovery's lease has just passed the same root checks, so here they can
# only be a failed query, and a lasting failure blocks at the discard and the next lease anyway.
_TRANSIENT_REFUSALS = frozenset(
    {"stage_busy", "parent_busy", "target_busy", "stale_root", "unsupported_filesystem"}
)

# The backup rename refuses with these having moved nothing (an undone move included): a taken
# backup name answers `conflict`; off Windows, so does another program's change that the
# rename's checks see, and a rename denied permission answers `read_only_target`.
_MOVED_NOTHING = frozenset({"conflict", "read_only_target"})


class PublicationError(RuntimeError):
    """Path-free publication failure safe for an API boundary."""

    def __init__(self, code: str, operation_id: str | None = None) -> None:
        self.code = code
        self.operation_id = operation_id
        super().__init__(code)


@dataclass(frozen=True)
class PublicationRow:
    operation_id: str
    workspace_id: str
    root_physical_id: str
    policy_revision: int
    ref: str
    kind: PublicationKind
    stage_name: str
    backup_name: str
    stage_physical_id: str | None
    stage_sha256: str
    stage_bytes: int
    target_physical_id: str | None
    expected_sha256: str | None
    target_bytes: int | None
    state: PublicationState
    created_at: str
    updated_at: str

    @property
    def stage_fingerprint(self) -> FileFingerprint | None:
        if self.stage_physical_id is None:
            return None
        return FileFingerprint(self.stage_physical_id, self.stage_sha256, self.stage_bytes)

    @property
    def target_fingerprint(self) -> FileFingerprint | None:
        if self.target_physical_id is None or self.target_bytes is None:
            return None
        assert self.expected_sha256 is not None
        return FileFingerprint(self.target_physical_id, self.expected_sha256, self.target_bytes)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _valid_hash(value: str | None) -> bool:
    return value is not None and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _canonical_uuid(value: str) -> bool:
    try:
        return str(uuid.UUID(value)) == value
    except AttributeError, ValueError:
        return False


def _valid_ref(ref: str) -> bool:
    if (
        not isinstance(ref, str)
        or not 1 <= len(ref) <= 512
        or "\x00" in ref
        or "\\" in ref
        or ":" in ref
    ):
        return False
    path = PurePosixPath(ref)
    parts = path.parts
    if path.is_absolute() or not parts or any(part in {"", ".", ".."} for part in parts):
        return False
    # This gates new reservations and each row's recovery. NTFS names are the lease's `_safe_ref`
    # refusal, so a row holding one blocks at its lease.
    return PurePosixPath(parts[-1]).suffix.casefold() == ".md" and not any(
        excluded_part(part, write=True) for part in parts
    )


def _is_attachment(ref: str) -> bool:
    try:
        attachment_ref(ref)
    except MutationError:
        return False
    return True


def _ref_cap(ref: str, kind: PublicationKind, root_kind: MutationRootKind = "vault") -> int | None:
    """The most a publication of `ref` may stage, or None when it may not write `ref` at all.

    In a vault, a note, or a create of exactly `attachments/<leaf>`: an attachment is
    never replaced. In a code folder, any `code_ref`, at the note cap. On an instructions root,
    only a replace of a root `AGENTS.md` or `CLAUDE.md`, at the note cap.
    """
    if root_kind == "instructions":
        return MAX_PUBLICATION_BYTES if kind == "replace" and ref in INSTRUCTION_REFS else None
    if root_kind == "code":
        try:
            code_ref(ref)
        except MutationError:
            return None
        return MAX_PUBLICATION_BYTES
    if _valid_ref(ref):
        return MAX_PUBLICATION_BYTES
    if kind == "create" and _is_attachment(ref):
        return MAX_STAGE_BYTES
    return None


def _states(value: PublicationState | Iterable[PublicationState]) -> tuple[PublicationState, ...]:
    return (value,) if isinstance(value, str) else tuple(value)


def _resolvable(row: PublicationRow) -> tuple[ResolveChoice, ...]:
    # `resolve` asks this too, so a ref the writer no longer admits is offered only
    # what needs no lease on it. An instruction file's row is resolved as its recovery is.
    if (
        row.state == "published"
        and row.kind == "replace"
        and (_valid_ref(row.ref) or _ref_cap(row.ref, row.kind, "instructions") is not None)
    ):
        return ("forget", "drop_previous")
    return ("forget",)


def _grammar_root(root: OrdinaryMutationRoot, row: PublicationRow) -> OrdinaryMutationRoot:
    """The resolved root under the grammar the row's save used. A row does not record its root's
    kind, and the resolver answers a vault; a vault row of an instruction file's replace takes the
    instructions grammar, which shares no ref with the vault's."""
    if root.kind == "vault" and _ref_cap(row.ref, row.kind, "instructions") is not None:
        return replace(root, kind="instructions")
    return root


def _backup_may_remain(row: PublicationRow) -> bool:
    # A create has no backup, and a replace names one only once the note may have moved.
    return row.kind == "replace" and row.state in {"prepared", "backed_up", "published", "conflict"}


def _busy(exc: BaseException) -> bool:
    # The store's busy wait (`BUSY_TIMEOUT_S`) ran out before the write committed, and closing
    # the connection rolled it back. A checkpoint reads its row back inside the same
    # transaction, so its row certainly keeps its previous state.
    return isinstance(exc, StateError) and exc.code == "busy"


class PublicationJournal:
    """Bounded compare-and-set access to live publication recovery rows."""

    def __init__(self, store: SessionStore) -> None:
        self._store = store

    def reserve(
        self,
        root: OrdinaryMutationRoot,
        ref: str,
        reservation: PublicationReservation,
        kind: PublicationKind,
        stage_sha256: str,
        stage_bytes: int,
        expected_sha256: str | None,
    ) -> PublicationRow:
        if not self._valid_reservation(reservation) or not self._valid_request(
            root, ref, kind, stage_sha256, stage_bytes, expected_sha256
        ):
            # No ID on a reserve refusal: it names no row the recovery status lists.
            raise PublicationError("invalid_publication")
        recorded = _now()
        folded = ref.casefold()
        values = (
            reservation.operation_id,
            root.workspace_id,
            root.physical_id,
            root.policy_revision,
            ref,
            kind,
            reservation.stage_name,
            reservation.backup_name,
            None,
            stage_sha256,
            stage_bytes,
            None,
            expected_sha256,
            None,
            "reserved",
            recorded,
            recorded,
        )
        with self._store._connection(write=True) as connection:
            # Windows names fold case, as the lease does; the column's binary UNIQUE does not.
            if any(
                live.casefold() == folded
                for (live,) in connection.execute(
                    "SELECT ref FROM vault_publications WHERE workspace_id=?",
                    (root.workspace_id,),
                )
            ):
                raise PublicationError("publication_busy")
            if connection.execute("SELECT count(*) FROM vault_publications").fetchone()[0] >= 128:
                raise PublicationError("publication_limit")
            try:
                connection.execute(
                    "INSERT INTO vault_publications "
                    f"({','.join(_COLUMNS)}) VALUES ({','.join('?' for _ in _COLUMNS)})",
                    values,
                )
            except sqlite3.IntegrityError:
                raise PublicationError("invalid_publication") from None
        # No read follows the commit, so a store `busy` here always means no row.
        return self._row(values)

    def record_stage(self, operation_id: str, physical_id: str) -> PublicationRow:
        if not 3 <= len(physical_id) <= 80:
            raise PublicationError("invalid_publication", operation_id)
        return self._update(
            operation_id,
            "reserved",
            "UPDATE vault_publications SET stage_physical_id=?,state='staged',updated_at=? "
            "WHERE operation_id=? AND state='reserved'",
            (physical_id, _now(), operation_id),
        )

    def record_prepared(self, operation_id: str, target: FileFingerprint) -> PublicationRow:
        if (
            not 3 <= len(target.physical_id) <= 80
            or not 0 <= target.byte_size <= MAX_PUBLICATION_BYTES
        ):
            raise PublicationError("invalid_publication", operation_id)
        return self._update(
            operation_id,
            "staged",
            "UPDATE vault_publications SET target_physical_id=?,target_bytes=?,"
            "state='prepared',updated_at=? WHERE operation_id=? AND kind='replace' "
            "AND state='staged' AND expected_sha256=?",
            (
                target.physical_id,
                target.byte_size,
                _now(),
                operation_id,
                target.sha256,
            ),
        )

    def transition(
        self,
        operation_id: str,
        expected: PublicationState | Iterable[PublicationState],
        state: PublicationState,
    ) -> PublicationRow:
        allowed = _states(expected)
        if not allowed:
            raise PublicationError("invalid_publication", operation_id)
        marks = ",".join("?" for _ in allowed)
        return self._update(
            operation_id,
            allowed,
            f"UPDATE vault_publications SET state=?,updated_at=? WHERE operation_id=? "
            f"AND state IN ({marks})",
            (state, _now(), operation_id, *allowed),
        )

    def delete(
        self,
        operation_id: str,
        expected: PublicationState | Iterable[PublicationState],
    ) -> None:
        allowed = _states(expected)
        if not allowed:
            raise PublicationError("invalid_publication", operation_id)
        marks = ",".join("?" for _ in allowed)
        with self._store._connection(write=True) as connection:
            deleted = connection.execute(
                f"DELETE FROM vault_publications WHERE operation_id=? AND state IN ({marks})",
                (operation_id, *allowed),
            ).rowcount
            if deleted != 1:
                raise PublicationError("recovery_required", operation_id)

    def get(self, operation_id: str) -> PublicationRow | None:
        with self._store._connection() as connection:
            row = connection.execute(
                f"SELECT {','.join(_COLUMNS)} FROM vault_publications WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
        return None if row is None else self._row(row)

    def list_pending(self) -> list[PublicationRow]:
        with self._store._connection() as connection:
            rows = connection.execute(
                f"SELECT {','.join(_COLUMNS)} FROM vault_publications "
                "ORDER BY created_at,operation_id LIMIT 129"
            ).fetchall()
        if len(rows) > 128:
            raise PublicationError("journal_corrupt")
        return [self._row(row) for row in rows]

    def _row(self, values: tuple[object, ...]) -> PublicationRow:
        row = PublicationRow(*values)  # type: ignore[arg-type]
        reservation = PublicationReservation(row.operation_id, row.stage_name, row.backup_name)
        stage_identity_valid = (row.state == "reserved") == (row.stage_physical_id is None)
        target_identity_valid = (
            row.kind == "create" and row.target_physical_id is None and row.target_bytes is None
        ) or (
            row.kind == "replace"
            and (
                (
                    row.state in {"reserved", "staged"}
                    and row.target_physical_id is None
                    and row.target_bytes is None
                )
                or (
                    row.state in {"prepared", "backed_up", "published", "conflict"}
                    and row.target_physical_id is not None
                    and row.target_bytes is not None
                )
            )
        )
        if not (
            self._valid_reservation(reservation)
            and _canonical_uuid(row.workspace_id)
            and 3 <= len(row.root_physical_id) <= 80
            and 1 <= row.policy_revision <= 9_007_199_254_740_991
            and _valid_hash(row.stage_sha256)
            # A row whose ref this journal would not admit keeps the note cap here and blocks
            # alone at its recovery.
            and 0 <= row.stage_bytes <= (_ref_cap(row.ref, row.kind) or MAX_PUBLICATION_BYTES)
            and row.kind in {"create", "replace"}
            and row.state
            in {"reserved", "staged", "prepared", "backed_up", "published", "conflict"}
            and (
                (row.kind == "create" and row.expected_sha256 is None)
                or (row.kind == "replace" and _valid_hash(row.expected_sha256))
            )
            and stage_identity_valid
            and target_identity_valid
        ):
            raise PublicationError("journal_corrupt", row.operation_id)
        return row

    def _update(
        self,
        operation_id: str,
        _expected: object,
        sql: str,
        params: tuple[object, ...],
    ) -> PublicationRow:
        # The row is read back inside the write transaction, not after it, so that a store
        # `busy` from a checkpoint always means the checkpoint did not commit (`_busy`).
        with self._store._connection(write=True) as connection:
            if connection.execute(sql, params).rowcount != 1:
                raise PublicationError("recovery_required", operation_id)
            values = connection.execute(
                f"SELECT {','.join(_COLUMNS)} FROM vault_publications WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
        return self._row(values)

    @staticmethod
    def _valid_reservation(reservation: PublicationReservation) -> bool:
        try:
            operation_id = str(uuid.UUID(reservation.operation_id))
        except AttributeError, ValueError:
            return False
        return (
            operation_id == reservation.operation_id
            and reservation.stage_name == f"{STAGE_PREFIX}{operation_id}{STAGE_SUFFIX}"
            and reservation.backup_name == f"{BACKUP_PREFIX}{operation_id}{STAGE_SUFFIX}"
        )

    @staticmethod
    def _valid_request(
        root: OrdinaryMutationRoot,
        ref: str,
        kind: PublicationKind,
        stage_sha256: str,
        stage_bytes: int,
        expected_sha256: str | None,
    ) -> bool:
        cap = _ref_cap(ref, kind, root.kind)
        return (
            len(root.workspace_id) == 36
            and 3 <= len(root.physical_id) <= 80
            and 1 <= root.policy_revision <= 9_007_199_254_740_991
            and cap is not None
            and _valid_hash(stage_sha256)
            and 0 <= stage_bytes <= cap
            and (
                (kind == "create" and expected_sha256 is None)
                or (kind == "replace" and _valid_hash(expected_sha256))
            )
        )


@dataclass(frozen=True)
class PublicationOutcome:
    operation_id: str
    status: Literal["saved", "no_change", "conflict", "recovered", "blocked"]
    cleanup_pending: bool = False


@dataclass(frozen=True)
class FolderOutcome(PublicationOutcome):
    """A create that opted into `create_folders`: also the folders it created, as posix chains
    (`("a", "a/b")`). A folder that already existed is not listed."""

    created_folders: tuple[str, ...] = ()


ResolveChoice = Literal["forget", "drop_previous"]


@dataclass(frozen=True)
class PublicationRecoveryStatus:
    operation_id: str
    workspace_id: str
    ref: str
    state: PublicationState
    reason: Literal["conflict", "recovery_required"]
    # What `resolve` may be asked for: a published replace also offers `drop_previous`, though
    # `resolve` still checks the files, which this listing does not probe.
    resolvable: tuple[ResolveChoice, ...] = ()


@dataclass(frozen=True)
class ResolveOutcome:
    """What `resolve` did. The `*_retained` flags come from the row's state, not from the disk:
    they say which private names the row's save may have left, which the user can now delete."""

    operation_id: str
    choice: ResolveChoice
    stage_retained: bool
    backup_retained: bool


ResolveRoot = Callable[[str, int, str], OrdinaryMutationRoot]
# Raises to refuse a write to the workspace it is given, as
# `VaultLifecycleService.require_writable` does while an operation is `recovery_required`.
WorkspaceGate = Callable[[str], None]


class VaultPublicationService:
    """Checkpoint native publication steps in the app journal and recover by identity."""

    def __init__(
        self,
        journal: PublicationJournal,
        resolve_root: ResolveRoot,
        coordinator: MutationCoordinator,
        native: VaultPublication | None = None,
        *,
        workspace_gate: WorkspaceGate | None = None,
    ) -> None:
        self._journal = journal
        self._resolve_root = resolve_root
        self._coordinator = coordinator
        self._native = native or VaultPublication()
        # Only `save` asks, and with no gate every workspace is writable: recovery, its listing
        # and `resolve` never ask, since they clear a blocked state.
        self._workspace_gate = workspace_gate

    def recovery_status(self) -> list[PublicationRecoveryStatus]:
        return [
            PublicationRecoveryStatus(
                row.operation_id,
                row.workspace_id,
                row.ref,
                row.state,
                "conflict" if row.state == "conflict" else "recovery_required",
                _resolvable(row),
            )
            for row in self._journal.list_pending()
        ]

    async def resolve(
        self, operation_id: str, choice: ResolveChoice, *, wait_s: float | None = None
    ) -> ResolveOutcome:
        """Let the user end a blocked row: `forget` it, or delete the backup it still guards.
        `wait_s` bounds `drop_previous`'s wait for the lease, as `save`'s does."""
        if choice not in ("forget", "drop_previous") or not _canonical_uuid(operation_id):
            raise PublicationError("invalid_publication")
        try:
            row = await run_sync(self._journal.get, operation_id)
        except PublicationError:
            raise
        except RuntimeError as exc:
            raise PublicationError("journal_unavailable") from exc
        if row is None:
            raise PublicationError("unknown_publication", operation_id)
        if choice == "forget":
            # No root, lease or file: a row whose workspace is gone must still be forgettable.
            await self._delete_row(row)
            return ResolveOutcome(
                operation_id,
                "forget",
                stage_retained=row.state != "published",
                backup_retained=_backup_may_remain(row),
            )
        if "drop_previous" not in _resolvable(row):
            raise PublicationError("not_resolvable", operation_id)
        resolved = await run_sync(
            lambda: self._resolve_checked(
                row.workspace_id, row.policy_revision, row.root_physical_id
            )
        )
        root = _grammar_root(resolved, row)
        async with self._coordinator.lease(root, (row.ref,), wait_s=wait_s) as lease:
            await run_sync(lambda: self._drop_previous_held(row, root, lease))
        return ResolveOutcome(operation_id, "drop_previous", False, False)

    async def _delete_row(self, row: PublicationRow) -> None:
        try:
            await run_sync(self._journal.delete, row.operation_id, row.state)
        except PublicationError:
            raise
        except RuntimeError as exc:
            raise PublicationError("recovery_required", row.operation_id) from exc

    def _drop_previous_held(
        self, row: PublicationRow, root: OrdinaryMutationRoot, lease: MutationLease
    ) -> None:
        self._require_same_root(root)
        # The row may have moved on while this waited for the lease.
        if self._journal.get(row.operation_id) != row:
            raise PublicationError("not_resolvable", row.operation_id)
        new, old = row.stage_fingerprint, row.target_fingerprint
        assert new is not None and old is not None  # a published replace row records both
        # `discard_backup_after_publish` re-verifies both files, but a refusal there reads as a
        # failed delete; probing first makes "the note or backup is no longer ours" its own answer.
        if (
            self._native.probe_verified(lease, root, row.ref, None, new) != "match"
            or self._native.probe_verified(lease, root, row.ref, row.backup_name, old) != "match"
        ):
            raise PublicationError("not_resolvable", row.operation_id)
        try:
            self._native.discard_backup_after_publish(
                lease, root, row.ref, row.backup_name, new, old
            )
            self._journal.delete(row.operation_id, "published")
        except PublicationError:
            raise
        except (MutationError, OSError, RuntimeError) as exc:
            # A backup already gone leaves a row that the next launch clears.
            raise PublicationError("recovery_required", row.operation_id) from exc

    @overload
    async def save(
        self,
        workspace_id: str,
        policy_revision: int,
        root_physical_id: str,
        ref: str,
        content: bytes,
        *,
        expected_sha256: None,
        create_folders: Literal[True],
        wait_s: float | None = None,
    ) -> FolderOutcome: ...

    @overload
    async def save(
        self,
        workspace_id: str,
        policy_revision: int,
        root_physical_id: str,
        ref: str,
        content: bytes,
        *,
        expected_sha256: str | None,
        create_folders: bool = False,
        wait_s: float | None = None,
    ) -> PublicationOutcome: ...

    async def save(
        self,
        workspace_id: str,
        policy_revision: int,
        root_physical_id: str,
        ref: str,
        content: bytes,
        *,
        expected_sha256: str | None,
        create_folders: bool = False,
        wait_s: float | None = None,
    ) -> PublicationOutcome:
        """`create_folders`, create-only: first create the target's missing parent folders
        under the same lease, and answer a `FolderOutcome` naming them. `wait_s` bounds
        the wait for the lease, past which the save refuses `MutationError("vault_locked")` before
        any vault write; `None` waits without limit."""
        await run_sync(self._require_writable, workspace_id)
        # The ref decides the cap: only `attachments/<leaf>` may stage past a note's.
        attachment = _is_attachment(ref)
        cap = MAX_STAGE_BYTES if attachment else MAX_PUBLICATION_BYTES
        if type(content) is not bytes or len(content) > cap:
            raise PublicationError("invalid_publication")
        if expected_sha256 is not None and (
            attachment or create_folders or not _valid_hash(expected_sha256)
        ):
            raise PublicationError("invalid_publication")
        root = await self._resolve_kind(workspace_id, policy_revision, root_physical_id, "vault")
        return await self._save_resolved(
            root, ref, content, expected_sha256, wait_s=wait_s, create_folders=create_folders
        )

    async def save_code(
        self,
        workspace_id: str,
        policy_revision: int,
        root_physical_id: str,
        ref: str,
        content: bytes,
        *,
        expected_sha256: str | None,
        wait_s: float | None = None,
    ) -> PublicationOutcome:
        """`save` for a trusted code folder: any `code_ref` up to the note cap, no attachment
        and no folder creation. `expected_sha256` None is create-only, as for `save`."""
        await run_sync(self._require_writable, workspace_id)
        if type(content) is not bytes or len(content) > MAX_PUBLICATION_BYTES:
            raise PublicationError("invalid_publication")
        if expected_sha256 is not None and not _valid_hash(expected_sha256):
            raise PublicationError("invalid_publication")
        root = await self._resolve_kind(workspace_id, policy_revision, root_physical_id, "code")
        return await self._save_resolved(
            root, ref, content, expected_sha256, wait_s=wait_s, create_folders=False
        )

    async def save_instruction(
        self,
        workspace_id: str,
        policy_revision: int,
        root_physical_id: str,
        name: str,
        content: bytes,
        *,
        expected_sha256: str,
        wait_s: float | None = REQUEST_LEASE_WAIT_S,
    ) -> PublicationOutcome:
        """Replace an admitted vault's existing root `AGENTS.md` or `CLAUDE.md` in place, under
        the note save's lease, journal and checks (hard rule 1's one exception, ADR-082). It
        never creates the file: a missing one refuses `missing_target`. `wait_s` bounds the wait
        for the lease as `save`'s does; its one caller is the editor's request, so by default it
        is `REQUEST_LEASE_WAIT_S`.

        Bytes a protected workspace records for a file of the same name refuse
        `protected_instructions` before any row: once the file held them, the resolver would
        refuse every later write to this vault, the save that reverts them included."""
        await run_sync(self._require_writable, workspace_id)
        if (
            not isinstance(name, str)
            or name not in INSTRUCTION_REFS
            or type(content) is not bytes
            or len(content) > MAX_PUBLICATION_BYTES
            or not _valid_hash(expected_sha256)
        ):
            raise PublicationError("invalid_publication")
        root = await self._resolve_kind(workspace_id, policy_revision, root_physical_id, "vault")
        if (name, hashlib.sha256(content).hexdigest()) in root.protected_instructions:
            raise PublicationError("protected_instructions")
        return await self._save_resolved(
            replace(root, kind="instructions"),
            name,
            content,
            expected_sha256,
            wait_s=wait_s,
            create_folders=False,
        )

    def _require_writable(self, workspace_id: str) -> None:
        if self._workspace_gate is None:
            return
        try:
            self._workspace_gate(workspace_id)
        except PublicationError:
            raise
        except RuntimeError as exc:
            raise PublicationError("journal_unavailable") from exc

    async def _resolve_kind(
        self, workspace_id: str, policy_revision: int, physical_id: str, kind: RowKind
    ) -> OrdinaryMutationRoot:
        # One resolver may answer either kind, so each save refuses the other kind's root
        # before any reservation, row or lease: a vault takes no code ref, a code folder no note.
        root = await run_sync(
            lambda: self._resolve_checked(workspace_id, policy_revision, physical_id)
        )
        if root.kind != kind:
            raise MutationError("ordinary_workspace_required")
        return root

    async def _save_resolved(
        self,
        root: OrdinaryMutationRoot,
        ref: str,
        content: bytes,
        expected_sha256: str | None,
        *,
        wait_s: float | None,
        create_folders: bool,
    ) -> PublicationOutcome:
        reservation = self._native.reserve()
        digest = hashlib.sha256(content).hexdigest()
        kind: PublicationKind = "create" if expected_sha256 is None else "replace"
        async with self._coordinator.lease(root, (ref,), wait_s=wait_s) as lease:
            return await run_sync(
                lambda: self._save_held(
                    root,
                    ref,
                    reservation,
                    kind,
                    digest,
                    content,
                    expected_sha256,
                    lease,
                    create_folders,
                )
            )

    def _save_held(
        self,
        root: OrdinaryMutationRoot,
        ref: str,
        reservation: PublicationReservation,
        kind: PublicationKind,
        digest: str,
        content: bytes,
        expected_sha256: str | None,
        lease: MutationLease,
        create_folders: bool,
    ) -> PublicationOutcome:
        self._require_same_root(root)
        # An operation may require recovery while this save waits for its lease.
        self._require_writable(root.workspace_id)
        if root.kind == "vault" and is_bot_memory_ref(ref):
            raise PublicationError("excluded_path")
        try:
            row = self._journal.reserve(
                root,
                ref,
                reservation,
                kind,
                digest,
                len(content),
                expected_sha256,
            )
        except PublicationError:
            raise
        except RuntimeError as exc:
            raise PublicationError("journal_unavailable") from exc
        # A refusal before any stage file exists releases the reservation, so the ref and
        # registration stay usable; once a stage exists, its row stays for recovery.
        try:
            self._require_same_root(root)
        except Exception:
            self._release(row.operation_id)
            raise
        created: tuple[str, ...] = ()
        try:
            if create_folders:
                # Before the stage, so a refusal leaves at most an empty folder, never a stage.
                created = self._native.ensure_folders(lease, root, tuple(ref.split("/"))[:-1])
            staged = self._native.stage(lease, root, ref, reservation, content)
        except (MutationError, OSError) as exc:
            if not (isinstance(exc, MutationError) and exc.stage_retained):
                self._release(row.operation_id)
            raise
        try:
            row = self._journal.record_stage(row.operation_id, staged.physical_id)
        except RuntimeError as exc:
            # The save deletes its own stage by its verified identity. A store `busy` did
            # not commit `staged`, so the reservation is released too. Any other failure may
            # have committed it, so the row stays, and the next launch deletes it.
            try:
                self._native.discard(lease, staged)
            except MutationError, OSError:
                raise PublicationError("recovery_required", row.operation_id) from exc
            if _busy(exc):
                self._release(row.operation_id)
                raise PublicationError("journal_unavailable") from exc
            if isinstance(exc, PublicationError):
                raise
            raise PublicationError("recovery_required", row.operation_id) from exc
        if kind == "create":
            outcome = self._publish_create(row, root, lease, staged)
            if not create_folders:
                return outcome
            return FolderOutcome(
                outcome.operation_id, outcome.status, outcome.cleanup_pending, created
            )
        assert expected_sha256 is not None
        return self._publish_replace(row, root, lease, staged, expected_sha256)

    def _release(self, operation_id: str) -> None:
        # A failed delete leaves the reserved row to startup recovery; the refusal still stands.
        with suppress(RuntimeError):
            self._journal.delete(operation_id, "reserved")

    def _discard(
        self,
        row: PublicationRow,
        lease: MutationLease,
        staged: StagedPublication,
        *,
        stage_required: bool = False,
        state: PublicationState = "staged",
    ) -> PublicationOutcome:
        # A save refused before `prepared` keeps neither its stage nor its row, so an
        # abandoned draft can never be published later. A failed discard keeps the row, and
        # the refusal becomes `recovery_required`, the label its recovery status shows.
        # `stage_required`: a gone stage may have been published, so its row stays.
        # `state`: the row's state; `prepared` once a backup rename has refused, moving nothing.
        try:
            if not self._native.discard(lease, staged) and stage_required:
                raise PublicationError("recovery_required", row.operation_id)
            self._journal.delete(row.operation_id, state)
        except PublicationError:
            raise
        except (MutationError, OSError, RuntimeError) as exc:
            raise PublicationError("recovery_required", row.operation_id) from exc
        return PublicationOutcome(row.operation_id, "conflict")

    async def recover_pending(self) -> list[PublicationOutcome]:
        outcomes: list[PublicationOutcome] = []
        for row in await run_sync(self._journal.list_pending):
            try:
                outcomes.append(await self._recover(row))
            except (MutationError, PublicationError, RuntimeError, OSError) as exc:
                # IDs and typed codes only: never a ref, a path or content.
                code = getattr(exc, "code", type(exc).__name__)
                logger.warning("vault publication %s blocked: %s", row.operation_id, code)
                outcomes.append(PublicationOutcome(row.operation_id, "blocked", True))
        return outcomes

    def _publish_create(
        self,
        row: PublicationRow,
        root: OrdinaryMutationRoot,
        lease: MutationLease,
        staged: StagedPublication,
    ) -> PublicationOutcome:
        try:
            self._native.publish_create(lease, staged)
        except (MutationError, OSError) as exc:
            # For a create, the refusal may follow the rename, so only our verified stage
            # still at its name proves that nothing was published. A `conflict` proves
            # it too: the rename answers one only while nothing of ours holds the name, which
            # off Windows includes another program's save there just after the rename (their
            # file stays, our stage went with the name it replaced), so a gone stage keeps no row.
            conflict = isinstance(exc, MutationError) and exc.code == "conflict"
            discarded = self._discard(row, lease, staged, stage_required=not conflict)
            if conflict:
                return discarded
            raise
        try:
            self._journal.transition(row.operation_id, "staged", "published")
        except PublicationError:
            raise
        except RuntimeError as exc:
            raise PublicationError("recovery_required", row.operation_id) from exc
        try:
            self._journal.delete(row.operation_id, "published")
        except RuntimeError:
            return PublicationOutcome(row.operation_id, "saved", True)
        return PublicationOutcome(row.operation_id, "saved")

    def _publish_replace(
        self,
        row: PublicationRow,
        root: OrdinaryMutationRoot,
        lease: MutationLease,
        staged: StagedPublication,
        expected_sha256: str,
        *,
        recovering: bool = False,
    ) -> PublicationOutcome:
        post_prepared = moved_nothing = False
        try:
            with self._native.prepare_replace(
                lease, staged, expected_sha256, staged.backup_name
            ) as prepared:
                if prepared.old.sha256 == staged.sha256:
                    unchanged = prepared.old
                else:
                    unchanged = None
                    self._journal.record_prepared(row.operation_id, prepared.old)
                    post_prepared = True
                    try:
                        prepared.move_target_to_backup()
                    except MutationError as exc:
                        moved_nothing = exc.code in _MOVED_NOTHING
                        raise
                    # The note's name stays empty until the stage takes it, so a failed
                    # `backed_up` commit does not stop the save; recovery treats `prepared`
                    # and `backed_up` alike once the note has moved.
                    with suppress(RuntimeError):
                        self._journal.transition(row.operation_id, "prepared", "backed_up")
                    try:
                        prepared.publish_stage_no_replace()
                    except MutationError as exc:
                        if exc.code != "conflict":
                            raise PublicationError("recovery_required", row.operation_id) from exc
                        self._journal.transition(
                            row.operation_id, ("prepared", "backed_up"), "conflict"
                        )
                        return PublicationOutcome(row.operation_id, "conflict", True)
                    prepared.verify_published()
                    self._journal.transition(
                        row.operation_id, ("prepared", "backed_up"), "published"
                    )
        except (MutationError, OSError, RuntimeError) as exc:
            if isinstance(exc, PublicationError):
                raise
            if moved_nothing:
                # Nothing moved, so the save is refused as one before `prepared` is, now
                # that the prepare has let go of its files: the stage and the `prepared` row go,
                # and only a conflict is an outcome.
                discarded = self._discard(row, lease, staged, state="prepared")
                if isinstance(exc, MutationError) and exc.code == "conflict":
                    return discarded
                raise
            # After `prepared`, or a journal failure whose commit is uncertain, the row stays.
            if post_prepared:
                raise PublicationError("recovery_required", row.operation_id) from exc
            if not isinstance(exc, (MutationError, OSError)):
                # A store `busy` did not commit `prepared`: at save time it is a refusal like
                # any other, so the stage and row go and nothing publishes later. Recovery
                # keeps the row, as for any transient refusal.
                if recovering or not _busy(exc):
                    raise PublicationError("recovery_required", row.operation_id) from exc
                self._discard(row, lease, staged)
                raise PublicationError("journal_unavailable") from exc
            # Recovery keeps a crash-interrupted save whose refusal may clear by the next
            # launch; at save time the draft is still on screen, so everything discards.
            if recovering and (isinstance(exc, OSError) or exc.code in _TRANSIENT_REFUSALS):
                raise PublicationError("recovery_required", row.operation_id) from exc
            # Every refusal before `prepared` discards the stage and row, then re-raises
            # the typed refusal; only a conflict is an outcome.
            discarded = self._discard(row, lease, staged)
            if isinstance(exc, MutationError) and exc.code == "conflict":
                return discarded
            raise
        if unchanged is not None:
            self._discard(row, lease, staged)  # its failure handling; its outcome does not apply
            return PublicationOutcome(row.operation_id, "no_change")
        try:
            self._native.discard_backup_after_publish(
                lease,
                root,
                row.ref,
                row.backup_name,
                staged.fingerprint,
                prepared.old,
            )
            self._journal.delete(row.operation_id, "published")
        except MutationError, PublicationError, OSError, RuntimeError:
            return PublicationOutcome(row.operation_id, "saved", True)
        return PublicationOutcome(row.operation_id, "saved")

    async def _recover(self, row: PublicationRow) -> PublicationOutcome:
        # The ref is checked per row, not in `_row`: a row written before its name joined the
        # excluded set blocks alone and never reaches the filesystem; the other rows recover.
        # A row does not record its root's kind, so either grammar admits it here, and its
        # resolved root's grammar decides next: a vault row holding a code-only ref, or a replace
        # of an attachment, which the lease's grammar alone would take, blocks before the lease.
        # A vault row of an instruction file's replace recovers under the instructions grammar.
        if all(
            _ref_cap(row.ref, row.kind, kind) is None for kind in ("vault", "code", "instructions")
        ):
            raise PublicationError("invalid_ref", row.operation_id)
        resolved = await run_sync(
            lambda: self._resolve_checked(
                row.workspace_id, row.policy_revision, row.root_physical_id
            )
        )
        root = _grammar_root(resolved, row)
        if _ref_cap(row.ref, row.kind, root.kind) is None:
            raise PublicationError("invalid_ref", row.operation_id)
        async with self._coordinator.lease(root, (row.ref,)) as lease:
            return await run_sync(lambda: self._recover_held(row, root, lease))

    def _recover_held(
        self, row: PublicationRow, root: OrdinaryMutationRoot, lease: MutationLease
    ) -> PublicationOutcome:
        self._require_same_root(root)
        latest = self._journal.get(row.operation_id)
        if latest is None:
            return PublicationOutcome(row.operation_id, "recovered")
        if latest != row:
            row = latest
        # A code folder keeps no attachments: its `attachments/x.png` is an ordinary file.
        if root.kind == "vault" and _is_attachment(row.ref):
            return self._recover_attachment(row, root, lease)
        unknown = FileFingerprint("unknown", row.stage_sha256, row.stage_bytes)
        stage = self._native.probe_verified(
            lease, root, row.ref, row.stage_name, row.stage_fingerprint or unknown
        )
        backup = self._native.probe_verified(
            lease, root, row.ref, row.backup_name, row.target_fingerprint or unknown
        )
        if stage == "absent" and backup == "absent":
            # No file of the save is left to finish or protect, so the row goes, in any
            # state and whatever the note now holds. Recovery touches no file here. A folder
            # moved inside the vault reads as absent names too, so the clearance is logged.
            self._journal.delete(row.operation_id, row.state)
            logger.warning("vault publication %s cleared: %s", row.operation_id, row.state)
            return PublicationOutcome(row.operation_id, "recovered")
        if row.state == "reserved":
            # The stage's identity was never recorded, so nothing here is provably ours.
            return PublicationOutcome(row.operation_id, "blocked", True)
        if row.kind == "create":
            return self._recover_create(row, root, lease, stage, backup)
        return self._recover_replace(row, root, lease, stage, backup)

    def _recover_create(
        self,
        row: PublicationRow,
        root: OrdinaryMutationRoot,
        lease: MutationLease,
        stage: str,
        backup: str,
    ) -> PublicationOutcome:
        new = row.stage_fingerprint
        if new is None:
            raise PublicationError("recovery_required", row.operation_id)
        target = self._native.probe_verified(lease, root, row.ref, None, new)
        if row.state == "staged" and backup == "absent":
            if target == "absent" and stage == "match":
                try:
                    self._native.move_verified_no_replace(lease, root, row.ref, row.stage_name, new)
                except MutationError as exc:
                    if exc.code != "conflict":
                        raise
                    return self._reprobe_create_collision(row, root, lease, new)
                self._journal.transition(row.operation_id, "staged", "published")
                row = self._journal.get(row.operation_id) or row
                target, stage = "match", "absent"
            else:
                # Only a crash-interrupted create completes; a stage that is not ours
                # makes the discard refuse, so the row blocks.
                return self._discard(row, lease, self._staged(row, root, new))
        if target == "match" and stage == "absent" and backup == "absent":
            self._journal.delete(row.operation_id, "published")
            return PublicationOutcome(row.operation_id, "recovered")
        return PublicationOutcome(row.operation_id, "blocked", True)

    def _recover_attachment(
        self, row: PublicationRow, root: OrdinaryMutationRoot, lease: MutationLease
    ) -> PublicationOutcome:
        """A create-only attachment row, recovered by identity as a note's create is.

        No probe takes an attachment ref: a discard against an identity no file has
        stands in for the stage probe, since its verified open refuses before any delete, and
        `attachment_sha256` stands in for the target probe.  A create never names a backup.
        """
        unknown = FileFingerprint("unknown", row.stage_sha256, row.stage_bytes)
        try:
            self._native.discard(lease, self._staged(row, root, unknown))
        except MutationError as exc:
            if exc.code not in {"stale_stage", "unsafe_stage"}:
                raise
        else:
            # As for a note: no file of the save is left, so the row goes.
            self._journal.delete(row.operation_id, row.state)
            logger.warning("vault publication %s cleared: %s", row.operation_id, row.state)
            return PublicationOutcome(row.operation_id, "recovered")
        new = row.stage_fingerprint
        if row.state != "staged" or new is None:
            # `reserved`: the stage's identity was never recorded. `published`: the stage
            # already took the name, and only a `staged` row publishes, as for a note.
            return PublicationOutcome(row.operation_id, "blocked", True)
        staged = self._staged(row, root, new)
        if self._native.attachment_sha256(lease, root, row.ref) is None:
            try:
                self._native.publish_create(lease, staged)
            except MutationError as exc:
                if exc.code != "conflict":
                    raise
            else:
                self._journal.transition(row.operation_id, "staged", "published")
                self._journal.delete(row.operation_id, "published")
                return PublicationOutcome(row.operation_id, "recovered")
        # Another file holds the name, even one with these bytes, so only the stage goes,
        # by its verified identity; a stage that is not ours makes the discard refuse.
        return self._discard(row, lease, staged)

    def _reprobe_create_collision(
        self,
        row: PublicationRow,
        root: OrdinaryMutationRoot,
        lease: MutationLease,
        new: FileFingerprint,
    ) -> PublicationOutcome:
        target = self._native.probe_verified(lease, root, row.ref, None, new)
        stage = self._native.probe_verified(lease, root, row.ref, row.stage_name, new)
        backup = self._native.probe_verified(lease, root, row.ref, row.backup_name, new)
        if target == "match" and stage == "absent" and backup == "absent":
            self._journal.transition(row.operation_id, "staged", "published")
            self._journal.delete(row.operation_id, "published")
            return PublicationOutcome(row.operation_id, "recovered")
        if target == "unexpected" and stage == "match" and backup == "absent":
            return self._discard(row, lease, self._staged(row, root, new))
        return PublicationOutcome(row.operation_id, "blocked", True)

    def _recover_replace(
        self,
        row: PublicationRow,
        root: OrdinaryMutationRoot,
        lease: MutationLease,
        stage: str,
        backup: str,
    ) -> PublicationOutcome:
        new = row.stage_fingerprint
        if new is None or row.expected_sha256 is None:
            raise PublicationError("recovery_required", row.operation_id)
        if row.state == "staged":
            staged = self._staged(row, root, new)
            try:
                return self._publish_replace(
                    row, root, lease, staged, row.expected_sha256, recovering=True
                )
            except MutationError, OSError:
                # `_publish_replace` lets a raw refusal escape only once it has discarded the
                # stage and row, a missing stage or folder included; nothing was
                # published. A transient refusal escapes as `recovery_required`.
                return PublicationOutcome(row.operation_id, "conflict")
        old = row.target_fingerprint
        if old is None:
            raise PublicationError("recovery_required", row.operation_id)
        target_new = self._native.probe_verified(lease, root, row.ref, None, new)
        target_old = self._native.probe_verified(lease, root, row.ref, None, old)
        if row.state == "published":
            return self._finish_published(row, root, lease, target_new, stage, backup, new, old)
        if row.state == "conflict":
            return PublicationOutcome(row.operation_id, "conflict", True)
        if (
            row.state == "prepared"
            and target_old == "match"
            and backup == "absent"
            and stage == "match"
        ):
            staged = self._staged(row, root, new)
            with self._native.prepare_replace(
                lease, staged, old.sha256, row.backup_name
            ) as prepared:
                if prepared.old != old:
                    return PublicationOutcome(row.operation_id, "blocked", True)
                prepared.move_target_to_backup()
                with suppress(RuntimeError):  # as at save time: the note has moved
                    self._journal.transition(row.operation_id, row.state, "backed_up")
                try:
                    prepared.publish_stage_no_replace()
                except MutationError as exc:
                    if exc.code != "conflict":
                        raise
                    self._journal.transition(
                        row.operation_id, ("prepared", "backed_up"), "conflict"
                    )
                    return PublicationOutcome(row.operation_id, "conflict", True)
                prepared.verify_published()
                self._journal.transition(row.operation_id, ("prepared", "backed_up"), "published")
            return self._cleanup_published(row, root, lease, new, old)
        if target_new == "absent" and backup == "match" and stage == "match":
            try:
                self._native.move_verified_no_replace(lease, root, row.ref, row.stage_name, new)
            except MutationError as exc:
                if exc.code != "conflict":
                    raise
                return self._reprobe_collision(row, root, lease, new, old)
            self._journal.transition(row.operation_id, row.state, "published")
            return self._cleanup_published(row, root, lease, new, old)
        if target_new == "absent" and backup == "match" and stage == "absent":
            try:
                self._native.move_verified_no_replace(lease, root, row.ref, row.backup_name, old)
            except MutationError as exc:
                if exc.code != "conflict":
                    raise
                return self._reprobe_collision(row, root, lease, new, old)
            return PublicationOutcome(row.operation_id, "blocked", True)
        if target_new == "match" and backup == "match" and stage == "absent":
            self._journal.transition(row.operation_id, row.state, "published")
            return self._cleanup_published(row, root, lease, new, old)
        if backup == "match" and stage == "match" and target_new == "unexpected":
            self._journal.transition(row.operation_id, row.state, "conflict")
            return PublicationOutcome(row.operation_id, "conflict", True)
        return PublicationOutcome(row.operation_id, "blocked", True)

    def _reprobe_collision(
        self,
        row: PublicationRow,
        root: OrdinaryMutationRoot,
        lease: MutationLease,
        new: FileFingerprint,
        old: FileFingerprint,
    ) -> PublicationOutcome:
        target_new = self._native.probe_verified(lease, root, row.ref, None, new)
        target_old = self._native.probe_verified(lease, root, row.ref, None, old)
        stage = self._native.probe_verified(lease, root, row.ref, row.stage_name, new)
        backup = self._native.probe_verified(lease, root, row.ref, row.backup_name, old)
        if target_new == "match" and backup == "match" and stage == "absent":
            self._journal.transition(row.operation_id, row.state, "published")
            return self._cleanup_published(row, root, lease, new, old)
        if target_old == "match" and backup == "absent" and stage == "absent":
            return PublicationOutcome(row.operation_id, "blocked", True)
        if target_new == "unexpected" and backup == "match" and stage in {"match", "absent"}:
            self._journal.transition(row.operation_id, row.state, "conflict")
            return PublicationOutcome(row.operation_id, "conflict", True)
        return PublicationOutcome(row.operation_id, "blocked", True)

    def _finish_published(
        self,
        row: PublicationRow,
        root: OrdinaryMutationRoot,
        lease: MutationLease,
        target: str,
        stage: str,
        backup: str,
        new: FileFingerprint,
        old: FileFingerprint,
    ) -> PublicationOutcome:
        # A backup still present beside a note changed elsewhere blocks until the user ends it.
        if target != "match" or stage != "absent":
            return PublicationOutcome(row.operation_id, "blocked", True)
        if backup == "match":
            return self._cleanup_published(row, root, lease, new, old)
        return PublicationOutcome(row.operation_id, "blocked", True)

    def _cleanup_published(
        self,
        row: PublicationRow,
        root: OrdinaryMutationRoot,
        lease: MutationLease,
        new: FileFingerprint,
        old: FileFingerprint,
    ) -> PublicationOutcome:
        try:
            self._native.discard_backup_after_publish(
                lease, root, row.ref, row.backup_name, new, old
            )
            self._journal.delete(row.operation_id, "published")
        except MutationError, PublicationError, OSError:
            return PublicationOutcome(row.operation_id, "saved", True)
        return PublicationOutcome(row.operation_id, "recovered")

    @staticmethod
    def _staged(
        row: PublicationRow, root: OrdinaryMutationRoot, fingerprint: FileFingerprint
    ) -> StagedPublication:
        return StagedPublication(
            root,
            row.ref,
            row.operation_id,
            row.stage_name,
            row.backup_name,
            fingerprint.physical_id,
            fingerprint.sha256,
            fingerprint.byte_size,
        )

    def _resolve_checked(
        self, workspace_id: str, policy_revision: int, physical_id: str
    ) -> OrdinaryMutationRoot:
        root = self._resolve_root(workspace_id, policy_revision, physical_id)
        if (
            root.workspace_id != workspace_id
            or root.policy_revision != policy_revision
            or root.physical_id != physical_id
        ):
            raise PublicationError("workspace_changed")
        return root

    def _require_same_root(self, root: OrdinaryMutationRoot) -> None:
        # The resolver answers a vault; an instructions root is that vault under another grammar.
        expected = replace(root, kind="vault") if root.kind == "instructions" else root
        if (
            self._resolve_checked(root.workspace_id, root.policy_revision, root.physical_id)
            != expected
        ):
            raise PublicationError("workspace_changed")
