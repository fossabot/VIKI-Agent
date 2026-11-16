"""The note lifecycle operation journal and the private recovery store (hard rule 1, `AGENTS.md`).

`vault_operations` has no ref column: a row's `steps` JSON is the only record of the paths it
touches (`persistence.feature_schemas.OPERATION_SCHEMAS`), so the one-live-row-per-path rule reads
every live row's steps in the workspace, which also covers both ends of a move. A row is live while
it exists, the
same rule `storage.update_workspace`'s registration freeze applies.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import os
import sqlite3
import threading
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Literal

from viki_agent import osfs, protection, storage
from viki_agent.storage import StateError
from viki_agent.vaults.vault import excluded_part
from viki_agent.vaults.vault_mutation import MAX_PUBLICATION_BYTES, OrdinaryMutationRoot
from viki_agent.vaults.vault_publication import (
    PublicationError,
    _canonical_uuid,
    _now,
    _valid_hash,
)

if TYPE_CHECKING:
    from viki_agent.storage import SessionStore

logger = logging.getLogger(__name__)

OperationKind = Literal["rename", "move", "archive", "delete", "restore"]
OperationState = Literal[
    "planned", "preserved", "applying", "done", "failed", "conflict", "recovery_required"
]
StepAction = Literal["create", "move", "remove", "mkdir"]
RecoveryKind = Literal["deleted", "preimage"]

MAX_LIVE_OPERATIONS = 64
MAX_STEPS_BYTES = 4096
MAX_RECOVERY_ENTRIES = 256
MAX_RECOVERY_BYTES = 64 << 20
RECOVERY_DAYS = 30

_KINDS = frozenset({"rename", "move", "archive", "delete", "restore"})
_STATES = frozenset(
    {"planned", "preserved", "applying", "done", "failed", "conflict", "recovery_required"}
)
_ACTIONS = frozenset({"create", "move", "remove", "mkdir"})
_COLUMNS = (
    "operation_id",
    "workspace_id",
    "root_physical_id",
    "policy_revision",
    "kind",
    "request_sha256",
    "steps",
    "state",
    "created_at",
    "updated_at",
)
_RECOVERY_COLUMNS = (
    "recovery_id",
    "workspace_id",
    "kind",
    "ref",
    "sha256",
    "byte_size",
    "created_at",
    "expires_at",
)


@dataclass(frozen=True)
class OperationStep:
    """One path's change. A null hash means the path is absent; `to` is a move's destination."""

    n: int
    action: StepAction
    ref: str
    to: str | None
    pre: str | None
    post: str | None
    preimage: str | None

    def paths(self) -> tuple[str, ...]:
        return (self.ref,) if self.to is None else (self.ref, self.to)


@dataclass(frozen=True)
class OperationRow:
    operation_id: str
    workspace_id: str
    root_physical_id: str
    policy_revision: int
    kind: OperationKind
    request_sha256: str
    steps: tuple[OperationStep, ...]
    state: OperationState
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class RecoveryEntry:
    recovery_id: str
    workspace_id: str
    kind: RecoveryKind
    ref: str
    sha256: str
    byte_size: int
    created_at: str
    expires_at: str


def _valid_path(ref: object, *, note: bool) -> bool:
    # Split by hand, as `vault.safe_ref` does: `PurePosixPath` would read `a//n.md` and
    # `a/./n.md` as `a/n.md`, and one file named two ways would slip past the busy check.
    if not isinstance(ref, str) or not 1 <= len(ref) <= 512 or any(c in ref for c in "\x00\\:"):
        return False
    parts = ref.split("/")
    if any(part in {"", ".", ".."} or excluded_part(part, write=True) for part in parts):
        return False
    return not note or PurePosixPath(parts[-1]).suffix.casefold() == ".md"


def _valid_step(step: OperationStep, n: int) -> bool:
    return (
        type(step.n) is int
        and step.n == n
        and step.action in _ACTIONS
        and _valid_path(step.ref, note=step.action != "mkdir")
        and (_valid_path(step.to, note=True) if step.action == "move" else step.to is None)
        and all(
            value is None or (isinstance(value, str) and _valid_hash(value))
            for value in (step.pre, step.post)
        )
        and (
            step.preimage is None
            or (isinstance(step.preimage, str) and _canonical_uuid(step.preimage))
        )
    )


def _valid_steps(steps: tuple[OperationStep, ...]) -> bool:
    if not steps or not all(_valid_step(step, n) for n, step in enumerate(steps)):
        return False
    # Steps touch pairwise-distinct paths, so each step's state reads from its own path alone. A
    # case-only rename names one file twice inside a single step, which is not a clash.
    seen: set[str] = set()
    for step in steps:
        own = {path.casefold() for path in step.paths()}
        if own & seen:
            return False
        seen |= own
    return True


def _held(steps: Iterable[OperationStep]) -> set[str]:
    # Windows names fold case, as the lease does.
    return {path.casefold() for step in steps for path in step.paths()}


def _shape(steps: Iterable[OperationStep]) -> tuple[tuple[str, str, str | None], ...]:
    return tuple((step.action, step.ref, step.to) for step in steps)


def _dump(steps: tuple[OperationStep, ...], operation_id: str | None = None) -> str:
    # ASCII-only JSON, so its length in characters (the column's CHECK) is its length in bytes.
    encoded = json.dumps([dataclasses.asdict(step) for step in steps], separators=(",", ":"))
    if len(encoded) > MAX_STEPS_BYTES:
        raise PublicationError("invalid_operation", operation_id)
    return encoded


def _load(raw: object, operation_id: str) -> tuple[OperationStep, ...]:
    try:
        steps = tuple(OperationStep(**item) for item in json.loads(raw))  # type: ignore[arg-type]
    except TypeError, ValueError:
        raise PublicationError("journal_corrupt", operation_id) from None
    if not _valid_steps(steps):
        raise PublicationError("journal_corrupt", operation_id)
    return steps


def _states(value: OperationState | Iterable[OperationState]) -> tuple[OperationState, ...]:
    return (value,) if isinstance(value, str) else tuple(value)


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class OperationJournal:
    """Bounded compare-and-set access to live lifecycle operation rows."""

    def __init__(self, store: SessionStore) -> None:
        self._store = store

    def plan(
        self,
        operation_id: str,
        root: OrdinaryMutationRoot,
        kind: OperationKind,
        steps: Sequence[OperationStep],
        request_sha256: str,
    ) -> OperationRow:
        steps = tuple(steps)
        if not (
            _canonical_uuid(operation_id)
            and _canonical_uuid(root.workspace_id)
            and 3 <= len(root.physical_id) <= 80
            and 1 <= root.policy_revision <= 9_007_199_254_740_991
            and kind in _KINDS
            and _valid_hash(request_sha256)
            and _valid_steps(steps)
        ):
            # No ID on a plan refusal: it names no row.
            raise PublicationError("invalid_operation")
        recorded = _now()
        values = (
            operation_id,
            root.workspace_id,
            root.physical_id,
            root.policy_revision,
            kind,
            request_sha256,
            _dump(steps),
            "planned",
            recorded,
            recorded,
        )
        held = _held(steps)
        with self._store._connection(write=True) as connection:
            existing = connection.execute(
                f"SELECT {','.join(_COLUMNS)} FROM vault_operations WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            if existing is not None:
                row = self._row(existing)
                if (row.workspace_id, row.request_sha256) == (root.workspace_id, request_sha256):
                    return row
                raise PublicationError("command_conflict", operation_id)
            for live_id, raw in connection.execute(
                "SELECT operation_id,steps FROM vault_operations WHERE workspace_id=?",
                (root.workspace_id,),
            ):
                if held & _held(_load(raw, live_id)):
                    raise PublicationError("operation_busy")
            count = connection.execute("SELECT count(*) FROM vault_operations").fetchone()[0]
            if count >= MAX_LIVE_OPERATIONS:
                raise PublicationError("operation_limit")
            try:
                connection.execute(
                    f"INSERT INTO vault_operations ({','.join(_COLUMNS)}) "
                    f"VALUES ({','.join('?' for _ in _COLUMNS)})",
                    values,
                )
            except sqlite3.IntegrityError:
                raise PublicationError("invalid_operation") from None
        # No read follows the commit, so a store `busy` here always means no row.
        return self._row(values)

    def transition(
        self,
        operation_id: str,
        expected_state: OperationState | Iterable[OperationState],
        new_state: OperationState,
        steps: Sequence[OperationStep] | None = None,
    ) -> OperationRow:
        """Move one row on from an expected state; new steps may only fill in hashes and
        preimages, so the paths the busy check admitted at `plan` never change."""
        allowed = _states(expected_state)
        if not allowed or new_state not in _STATES:
            raise PublicationError("invalid_operation", operation_id)
        replacement = None if steps is None else tuple(steps)
        if replacement is not None and not _valid_steps(replacement):
            raise PublicationError("invalid_operation", operation_id)
        marks = ",".join("?" for _ in allowed)
        with self._store._connection(write=True) as connection:
            current = connection.execute(
                f"SELECT steps FROM vault_operations WHERE operation_id=? AND state IN ({marks})",
                (operation_id, *allowed),
            ).fetchone()
            if current is None:
                raise PublicationError("recovery_required", operation_id)
            encoded = current[0]
            if replacement is not None:
                if _shape(_load(encoded, operation_id)) != _shape(replacement):
                    raise PublicationError("invalid_operation", operation_id)
                encoded = _dump(replacement, operation_id)
            connection.execute(
                "UPDATE vault_operations SET state=?,steps=?,updated_at=? WHERE operation_id=?",
                (new_state, encoded, _now(), operation_id),
            )
            # Read back inside the write transaction, so a store `busy` means no transition.
            values = connection.execute(
                f"SELECT {','.join(_COLUMNS)} FROM vault_operations WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
        return self._row(values)

    def delete(
        self,
        operation_id: str,
        expected_state: OperationState | Iterable[OperationState],
        now: datetime | None = None,
    ) -> None:
        """Delete one row and, in the same transaction, give each recovery entry its steps name
        that has expired by `now` another `RECOVERY_DAYS` from `now`: the row kept it past its
        expiry, and the next sweep would otherwise take it before the user could restore it.
        An entry still in date keeps its date; one already dropped re-arms nothing."""
        allowed = _states(expected_state)
        if not allowed:
            raise PublicationError("invalid_operation", operation_id)
        marks = ",".join("?" for _ in allowed)
        moment = now or datetime.now(UTC)
        cutoff, expires = _stamp(moment), _stamp(moment + timedelta(days=RECOVERY_DAYS))
        with self._store._connection(write=True) as connection:
            deleted = connection.execute(
                f"DELETE FROM vault_operations WHERE operation_id=? AND state IN ({marks}) "
                "RETURNING steps",
                (operation_id, *allowed),
            ).fetchall()
            if len(deleted) != 1:
                raise PublicationError("recovery_required", operation_id)
            # Stamps share one fixed-width format, so text order is time order; expired is the
            # sweep's own test, `expires_at` at or before the cutoff.
            connection.executemany(
                "UPDATE vault_recovery SET expires_at=? WHERE recovery_id=? AND expires_at<=?",
                [
                    (expires, step.preimage, cutoff)
                    for step in _load(deleted[0][0], operation_id)
                    if step.preimage is not None
                ],
            )

    def get(self, operation_id: str) -> OperationRow | None:
        with self._store._connection() as connection:
            row = connection.execute(
                f"SELECT {','.join(_COLUMNS)} FROM vault_operations WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
        return None if row is None else self._row(row)

    def list_pending(self) -> list[OperationRow]:
        with self._store._connection() as connection:
            rows = connection.execute(
                f"SELECT {','.join(_COLUMNS)} FROM vault_operations "
                f"ORDER BY created_at,operation_id LIMIT {MAX_LIVE_OPERATIONS + 1}"
            ).fetchall()
        if len(rows) > MAX_LIVE_OPERATIONS:
            raise PublicationError("journal_corrupt")
        return [self._row(row) for row in rows]

    @staticmethod
    def _row(values: tuple[Any, ...]) -> OperationRow:
        operation_id, workspace_id, physical_id, revision, kind, request, raw, state = values[:8]
        steps = _load(raw, operation_id)
        if not (
            _canonical_uuid(operation_id)
            and _canonical_uuid(workspace_id)
            and 3 <= len(physical_id) <= 80
            and 1 <= revision <= 9_007_199_254_740_991
            and kind in _KINDS
            and _valid_hash(request)
            and state in _STATES
        ):
            raise PublicationError("journal_corrupt", operation_id)
        return OperationRow(
            operation_id,
            workspace_id,
            physical_id,
            revision,
            kind,
            request,
            steps,
            state,
            *values[8:],
        )


class RecoveryStore:
    """Private app-data copies of the bytes an operation removes, one `<recovery_id>.bin` each,
    indexed by `vault_recovery` (a hash and a size, never bytes)."""

    def __init__(self, root: Path, journal: OperationJournal) -> None:
        self.root = Path(root)
        self._store = journal._store
        # One launch holds the store, so this orders a put's file-then-row against a sweep's
        # scan for files with no row.
        self._lock = threading.Lock()

    def put(self, workspace_id: str, kind: RecoveryKind, ref: str, data: bytes) -> RecoveryEntry:
        if not (
            _canonical_uuid(workspace_id)
            and kind in {"deleted", "preimage"}
            and _valid_path(ref, note=True)
            and len(data) <= MAX_PUBLICATION_BYTES
        ):
            raise PublicationError("invalid_recovery")
        created = datetime.now(UTC)
        entry = RecoveryEntry(
            str(uuid.uuid4()),
            workspace_id,
            kind,
            ref,
            hashlib.sha256(data).hexdigest(),
            len(data),
            _stamp(created),
            _stamp(created + timedelta(days=RECOVERY_DAYS)),
        )
        path = self._path(entry.recovery_id)
        with self._lock:
            self._ready()
            try:
                with self._store._connection() as connection:
                    self._refuse_full(connection, entry.byte_size)
                try:
                    with osfs.open_new_private(path) as target:
                        target.write(data)
                        target.flush()
                        os.fsync(target.fileno())
                    protection.require_private_file(path)
                    osfs.fsync_dir(self.root)
                    with self._store._connection(write=True) as connection:
                        # Again inside the write, which is what makes the bound hold.
                        self._refuse_full(connection, entry.byte_size)
                        connection.execute(
                            f"INSERT INTO vault_recovery ({','.join(_RECOVERY_COLUMNS)}) "
                            f"VALUES ({','.join('?' for _ in _RECOVERY_COLUMNS)})",
                            dataclasses.astuple(entry),
                        )
                except BaseException:
                    # No row committed, so the file must not outlive this call.
                    try:
                        path.unlink(missing_ok=True)
                    except OSError:
                        logger.warning("recovery put left an unindexed file for the next sweep")
                    raise
            except OSError, StateError, sqlite3.Error, protection.ProtectionError:
                raise PublicationError("recovery_unavailable") from None
        return entry

    def read(self, recovery_id: str) -> tuple[RecoveryEntry, bytes]:
        entry = self._entry(recovery_id)
        self._ready()
        path = self._path(recovery_id)
        try:
            if not storage.check_file(path):
                raise PublicationError("recovery_missing")
            protection.require_private_file(path)
            data = osfs.read_regular(path, MAX_PUBLICATION_BYTES + 1, follow_symlinks=False)
        except FileNotFoundError:
            raise PublicationError("recovery_missing") from None
        except StateError:
            # A link, a reparse point or a second hard link at the entry's name.
            raise PublicationError("recovery_corrupt") from None
        except OSError, protection.ProtectionError:
            raise PublicationError("recovery_unavailable") from None
        if len(data) != entry.byte_size or hashlib.sha256(data).hexdigest() != entry.sha256:
            raise PublicationError("recovery_corrupt")
        return entry, data

    def drop(self, recovery_id: str) -> None:
        """Remove one entry, file first and then its row; an unknown id is already dropped."""
        if not _canonical_uuid(recovery_id):
            raise PublicationError("invalid_recovery")
        with self._lock:
            self._ready()
            try:
                self._path(recovery_id).unlink(missing_ok=True)
                with self._store._connection(write=True) as connection:
                    connection.execute(
                        "DELETE FROM vault_recovery WHERE recovery_id=?", (recovery_id,)
                    )
            except OSError, StateError, sqlite3.Error:
                raise PublicationError("recovery_unavailable") from None

    def sweep(self, now: datetime) -> list[RecoveryEntry]:
        """Drop expired entries (file first, then row) that no operation row names, rows with
        no file and files with no row; answer the entries that remain, oldest first. One file
        that cannot go is logged by id and left for the next sweep."""
        cutoff = _stamp(now)
        kept: list[RecoveryEntry] = []
        with self._lock:
            self._ready()
            try:
                with self._store._connection() as connection:
                    entries = [
                        self._from_row(row)
                        for row in connection.execute(
                            f"SELECT {','.join(_RECOVERY_COLUMNS)} FROM vault_recovery "
                            "ORDER BY created_at,recovery_id"
                        )
                    ]
                indexed: set[str] = set()
                for entry in entries:
                    path = self._path(entry.recovery_id)
                    indexed.add(path.name)
                    present = os.path.lexists(path)
                    if entry.expires_at > cutoff and present:
                        kept.append(entry)
                        continue
                    with self._store._connection(write=True) as connection:
                        # A row delete may have re-armed the entry since the snapshot.
                        current = connection.execute(
                            "SELECT expires_at FROM vault_recovery WHERE recovery_id=?",
                            (entry.recovery_id,),
                        ).fetchone()
                        if current is None:
                            continue
                        if current[0] > cutoff and present:
                            kept.append(dataclasses.replace(entry, expires_at=current[0]))
                            continue
                        # Read under the write lock, so no row can come to name the entry
                        # between this check and its removal.
                        if present and entry.recovery_id in self._named(connection):
                            kept.append(entry)
                            continue
                        try:
                            path.unlink(missing_ok=True)
                        except OSError:
                            logger.warning(
                                "recovery sweep kept expired entry %s", entry.recovery_id
                            )
                            continue
                        connection.execute(
                            "DELETE FROM vault_recovery WHERE recovery_id=?", (entry.recovery_id,)
                        )
                with os.scandir(self.root) as found:
                    orphans = [
                        Path(item.path)
                        for item in found
                        if item.name not in indexed and not item.is_dir(follow_symlinks=False)
                    ]
                for orphan in orphans:
                    try:
                        orphan.unlink()
                    except OSError:
                        logger.warning("recovery sweep kept a file with no entry")
            except OSError, StateError, sqlite3.Error:
                raise PublicationError("recovery_unavailable") from None
        return kept

    def usage(self) -> tuple[int, int]:
        """The live entry count and their total bytes, the two bounds a put refuses past."""
        try:
            with self._store._connection() as connection:
                count, total = connection.execute(
                    "SELECT count(*),coalesce(sum(byte_size),0) FROM vault_recovery"
                ).fetchone()
        except StateError, sqlite3.Error:
            raise PublicationError("recovery_unavailable") from None
        return count, total

    def entries(self, workspace_id: str) -> list[RecoveryEntry]:
        """One vault's index rows, oldest first. Unlike `sweep` it drops nothing and touches no
        file, so an expired entry still shows until a sweep removes it."""
        try:
            with self._store._connection() as connection:
                rows = connection.execute(
                    f"SELECT {','.join(_RECOVERY_COLUMNS)} FROM vault_recovery "
                    "WHERE workspace_id=? ORDER BY created_at,recovery_id LIMIT ?",
                    (workspace_id, MAX_RECOVERY_ENTRIES + 1),
                ).fetchall()
        except StateError, sqlite3.Error:
            raise PublicationError("recovery_unavailable") from None
        if len(rows) > MAX_RECOVERY_ENTRIES:
            raise PublicationError("recovery_unavailable")
        return [self._from_row(row) for row in rows]

    def _ready(self) -> None:
        # Required private on every use, as `AssetService` requires its own folders.
        try:
            protection.require_private_directory(self.root.parent)
            protection.create_private_directory(self.root)
            protection.require_private_directory(self.root)
        except OSError, protection.ProtectionError:
            raise PublicationError("recovery_unavailable") from None

    def _path(self, recovery_id: str) -> Path:
        return self.root / f"{recovery_id}.bin"

    def _entry(self, recovery_id: str) -> RecoveryEntry:
        if not _canonical_uuid(recovery_id):
            raise PublicationError("invalid_recovery")
        try:
            with self._store._connection() as connection:
                row = connection.execute(
                    f"SELECT {','.join(_RECOVERY_COLUMNS)} FROM vault_recovery WHERE recovery_id=?",
                    (recovery_id,),
                ).fetchone()
        except StateError, sqlite3.Error:
            raise PublicationError("recovery_unavailable") from None
        if row is None:
            raise PublicationError("recovery_missing")
        return self._from_row(row)

    @staticmethod
    def _named(connection: sqlite3.Connection) -> set[str]:
        # A delete's remove step keeps its copy as `preimage`, and a restore's create step names
        # its source there; a row in any state holds its entry until it is reconciled or
        # forgotten. A corrupt row raises `journal_corrupt`, so nothing is swept unread.
        return {
            step.preimage
            for operation_id, raw in connection.execute(
                "SELECT operation_id,steps FROM vault_operations"
            )
            for step in _load(raw, operation_id)
            if step.preimage is not None
        }

    @staticmethod
    def _from_row(row: tuple[Any, ...]) -> RecoveryEntry:
        return RecoveryEntry(*row)

    @staticmethod
    def _refuse_full(connection: sqlite3.Connection, size: int) -> None:
        count, total = connection.execute(
            "SELECT count(*),coalesce(sum(byte_size),0) FROM vault_recovery"
        ).fetchone()
        if count >= MAX_RECOVERY_ENTRIES or total + size > MAX_RECOVERY_BYTES:
            raise PublicationError("recovery_full")
