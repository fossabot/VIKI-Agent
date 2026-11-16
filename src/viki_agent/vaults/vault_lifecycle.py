"""Rename, move, archive, delete-to-recovery and restore of one note (hard rule 1, `AGENTS.md`).

Each operation is one `vault_operations` row whose steps are some `mkdir`s and exactly one file
step. Apply observes the source under the mutation lease, checks it through the native layer,
plans the row, keeps a delete's bytes in the recovery store, and only then runs the step. A
refusal before the step raises its typed code and leaves the vault as it was. A failed step is
classified from the filesystem: nothing of ours changed is `failed` (`conflict` when another
writer caused it), anything else is `recovery_required`, which blocks the workspace. Folders are
never removed again. Finished rows are deleted, since every row freezes registration, so a
bounded in-memory receipt answers a retried command instead.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import stat
from dataclasses import dataclass, replace
from typing import Literal, cast, get_args

from anyio.to_thread import run_sync

from viki_agent import osfs, storage
from viki_agent.storage import StateError
from viki_agent.vaults.vault import is_bot_memory_ref
from viki_agent.vaults.vault_mutation import (
    BACKUP_PREFIX,
    MAX_PUBLICATION_BYTES,
    STAGE_PREFIX,
    STAGE_SUFFIX,
    FileFingerprint,
    MutationCoordinator,
    MutationError,
    MutationLease,
    OrdinaryMutationRoot,
    PublicationReservation,
    StagedPublication,
    VaultPublication,
    _revalidate_root,
    _safe_ref,
)
from viki_agent.vaults.vault_operations import (
    RECOVERY_DAYS,
    OperationJournal,
    OperationKind,
    OperationRow,
    OperationStep,
    RecoveryStore,
)
from viki_agent.vaults.vault_publication import (
    PublicationError,
    PublicationJournal,
    ResolveRoot,
    _canonical_uuid,
    _valid_hash,
)

logger = logging.getLogger(__name__)

# Every status `apply` returns. A refusal before the file step raises instead.
OperationStatus = Literal["done", "failed", "conflict", "recovery_required"]
MAX_RECEIPTS = 64
# Refusals a retry of the same command may clear, so their receipt is not kept: a lease or a
# file someone else holds, an unreadable store, or a block the user can lift.
_RETRYABLE = frozenset(
    {
        "command_conflict",
        "vault_locked",
        "target_busy",
        "parent_busy",
        "stage_busy",
        "stale_root",
        "workspace_changed",
        "unavailable",
        "journal_unavailable",
        "recovery_unavailable",
        "operation_busy",
        "publication_busy",
        "vault_recovery_required",
    }
)

_KINDS = frozenset(get_args(OperationKind))
_StepState = Literal["applied", "pending", "diverged"]
_RECONCILED: dict[_StepState, OperationStatus] = {
    "applied": "done",
    "pending": "failed",
    "diverged": "recovery_required",
}


@dataclass(frozen=True)
class PreviewStep:
    """A planned step as the caller may see it: no hash of content the caller did not send."""

    n: int
    action: str
    ref: str
    to: str | None


@dataclass(frozen=True)
class OperationPlan:
    steps: tuple[PreviewStep, ...]
    creates_folders: tuple[str, ...]
    recovery_days: int
    refusal: str | None = None


@dataclass(frozen=True)
class OperationOutcome:
    operation_id: str
    status: OperationStatus
    replayed: bool
    steps_applied: int
    # The failed step's typed code; never a path.
    reason: str | None = None
    # Folders the operation planned to create that now exist: a folder is never removed again.
    created_folders: tuple[str, ...] = ()


@dataclass(frozen=True)
class StuckOperation:
    """A row no apply in this launch holds: `recovery_required`, or one reconcile left blocked.
    Its note paths only: no hash and no absolute path."""

    operation_id: str
    kind: OperationKind
    state: str
    paths: tuple[str, ...]


@dataclass(frozen=True)
class ForgottenOperation:
    """What `forget` left. The row is gone and no vault file changed; the recovery entries it
    named stay in the private store until their sweep, and a restore's stage may stay in the
    vault under its hidden name."""

    operation_id: str
    recovery_ids: tuple[str, ...]
    stage_retained: bool


@dataclass(frozen=True)
class _Request:
    kind: OperationKind
    ref: str | None
    recovery_id: str | None
    to: str | None
    # The source's revision as the user loaded it; a restore's source is a recovery entry.
    expected_sha256: str | None

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(path for path in (self.ref, self.to) if path is not None)


@dataclass(frozen=True)
class _Draft:
    steps: tuple[OperationStep, ...]
    source: FileFingerprint | None
    data: bytes | None


def _parse(
    kind: str,
    ref: str | None,
    recovery_id: str | None,
    to: str | None,
    expected_sha256: str | None,
) -> _Request:
    restore = kind == "restore"
    if (
        kind not in _KINDS
        or (ref is None) != restore
        or (recovery_id is None) == restore
        or (to is None) != (kind == "delete")
        or (restore and not (isinstance(recovery_id, str) and _canonical_uuid(recovery_id)))
        or (expected_sha256 is not None) == restore
        or (not restore and not (isinstance(expected_sha256, str) and _valid_hash(expected_sha256)))
    ):
        raise PublicationError("invalid_operation")
    for path in (ref, to):
        if path is not None:
            try:
                _safe_ref(path)
            except MutationError as exc:
                raise PublicationError(exc.code) from None
            if is_bot_memory_ref(path):
                raise PublicationError("excluded_path")
    # An archive is a move under the top-level `archive/` folder, and lands nowhere else.
    if kind == "archive" and to is not None and to.split("/")[0].casefold() != "archive":
        raise PublicationError("invalid_ref")
    return _Request(cast(OperationKind, kind), ref, recovery_id, to, expected_sha256)


def _request_sha256(
    workspace_id: str, policy_revision: int, root_physical_id: str, request: _Request
) -> str:
    canonical = json.dumps(
        [
            workspace_id,
            policy_revision,
            root_physical_id,
            request.kind,
            request.ref,
            request.recovery_id,
            request.to,
            request.expected_sha256,
        ],
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _leaf(ref: str) -> str:
    return ref.rpartition("/")[2]


def _case_only(ref: str | None, to: str | None) -> bool:
    return (
        ref is not None
        and to is not None
        and ref.casefold() == to.casefold()
        and _leaf(ref) != _leaf(to)
    )


def _reservation(operation_id: str) -> PublicationReservation:
    # Named by the operation, so a stage a crash left behind is found again by its row.
    return PublicationReservation(
        operation_id,
        f"{STAGE_PREFIX}{operation_id}{STAGE_SUFFIX}",
        f"{BACKUP_PREFIX}{operation_id}{STAGE_SUFFIX}",
    )


def _code(exc: BaseException) -> str:
    # A `MutationError` becomes its bare code: its `stage_retained` flag describes a save's
    # stage, which a move, read or remove never has.
    if isinstance(exc, PublicationError | MutationError):
        return exc.code
    if isinstance(exc, OSError):
        return "unavailable"
    return "journal_unavailable"


class VaultLifecycleService:
    """Plan, apply and reconcile journalled note operations in an admitted ordinary vault."""

    def __init__(
        self,
        journal: OperationJournal,
        recovery: RecoveryStore,
        resolve_root: ResolveRoot,
        coordinator: MutationCoordinator,
        native: VaultPublication | None = None,
        *,
        lease_wait_s: float,
        publications: PublicationJournal,
    ) -> None:
        self._journal = journal
        self._recovery = recovery
        self._resolve_root = resolve_root
        self._coordinator = coordinator
        self._native = native or VaultPublication()
        self._lease_wait_s = lease_wait_s
        self._publications = publications
        # (request_sha256, outcome or refusal code) by command id, oldest first. Touched only on
        # the event loop.
        self._receipts: dict[str, tuple[str, OperationOutcome | str]] = {}
        self._running: set[str] = set()

    async def preview(
        self,
        workspace_id: str,
        policy_revision: int,
        root_physical_id: str,
        kind: str,
        *,
        ref: str | None = None,
        recovery_id: str | None = None,
        to: str | None = None,
        expected_sha256: str | None = None,
    ) -> OperationPlan:
        """The steps an apply would run now, or the typed code it would refuse with. Reads
        the vault and the recovery store; writes nothing."""
        try:
            request = _parse(kind, ref, recovery_id, to, expected_sha256)
            root = await run_sync(
                lambda: self._resolve_checked(workspace_id, policy_revision, root_physical_id)
            )
            draft = await run_sync(lambda: self._preview_held(root, request))
        except (MutationError, OSError, RuntimeError) as exc:
            code = _code(exc)
            if code == "invalid_operation":
                raise PublicationError(code) from exc
            return OperationPlan((), (), RECOVERY_DAYS, code)
        return OperationPlan(
            tuple(PreviewStep(step.n, step.action, step.ref, step.to) for step in draft.steps),
            tuple(step.ref for step in draft.steps if step.action == "mkdir"),
            RECOVERY_DAYS,
        )

    async def apply(
        self,
        command_id: str,
        workspace_id: str,
        policy_revision: int,
        root_physical_id: str,
        kind: str,
        *,
        ref: str | None = None,
        recovery_id: str | None = None,
        to: str | None = None,
        expected_sha256: str | None = None,
    ) -> OperationOutcome:
        """Run one operation. `expected_sha256` is the source's revision as the user loaded
        it, required for every kind but `restore`; a different note is `conflict`."""
        request = _parse(kind, ref, recovery_id, to, expected_sha256)
        if not (isinstance(command_id, str) and _canonical_uuid(command_id)):
            raise PublicationError("invalid_operation")
        digest = _request_sha256(workspace_id, policy_revision, root_physical_id, request)
        receipt = self._receipts.get(command_id)
        if receipt is not None:
            recorded_digest, answer = receipt
            if recorded_digest != digest:
                raise PublicationError("command_conflict", command_id)
            if isinstance(answer, str):
                raise PublicationError(answer)
            return replace(answer, replayed=True)
        if command_id in self._running:
            raise PublicationError("op_in_progress", command_id)
        self._running.add(command_id)
        try:
            outcome = await self._apply(
                command_id, digest, workspace_id, policy_revision, root_physical_id, request
            )
        except (MutationError, OSError, RuntimeError) as exc:
            refusal = exc if isinstance(exc, PublicationError) else PublicationError(_code(exc))
            if refusal.code not in _RETRYABLE:
                self._remember(command_id, digest, refusal.code)
            if refusal is exc:
                raise
            raise refusal from exc
        finally:
            self._running.discard(command_id)
        if not outcome.replayed:
            self._remember(command_id, digest, outcome)
        return outcome

    def require_writable(self, workspace_id: str) -> None:
        """Refuse every write to a workspace that holds a `recovery_required` operation."""
        if any(
            row.workspace_id == workspace_id and row.state == "recovery_required"
            for row in self._journal.list_pending()
        ):
            raise PublicationError("vault_recovery_required")

    async def stuck(self, workspace_id: str) -> list[StuckOperation]:
        """The workspace's rows no apply in this launch holds, oldest first."""
        try:
            rows = await run_sync(self._journal.list_pending)
        except PublicationError:
            raise
        except RuntimeError as exc:
            raise PublicationError("journal_unavailable") from exc
        return [
            StuckOperation(row.operation_id, row.kind, row.state, row.steps[-1].paths())
            for row in rows
            if row.workspace_id == workspace_id and row.operation_id not in self._running
        ]

    async def forget(self, operation_id: str) -> ForgottenOperation:
        """Delete one stuck row and touch no vault file, as `VaultPublicationService.resolve`'s
        `forget` does: the workspace takes writes again and its registration thaws."""
        if not (isinstance(operation_id, str) and _canonical_uuid(operation_id)):
            raise PublicationError("invalid_operation")
        if operation_id in self._running:
            raise PublicationError("op_in_progress", operation_id)
        try:
            row = await run_sync(self._journal.get, operation_id)
            if row is None:
                raise PublicationError("unknown_operation", operation_id)
            await run_sync(self._journal.delete, operation_id, row.state)
        except PublicationError:
            raise
        except RuntimeError as exc:
            raise PublicationError("journal_unavailable") from exc
        # The command plans afresh now; its old answer named a row that is gone.
        self._receipts.pop(operation_id, None)
        return ForgottenOperation(
            operation_id,
            tuple(step.preimage for step in row.steps if step.preimage is not None),
            row.steps[-1].action == "create",
        )

    async def reconcile(self) -> list[OperationOutcome]:
        """Decide every row a previous launch left, from the filesystem: all applied is `done`,
        none is `failed`, anything else `recovery_required`, left for the user. A row whose
        ref, admission or lease now refuses is blocked alone and stays for the next launch."""
        try:
            rows = await run_sync(self._journal.list_pending)
        except RuntimeError as exc:
            logger.warning("vault operations unreadable: %s", _code(exc))
            return []
        outcomes: list[OperationOutcome] = []
        for row in rows:
            if row.state == "recovery_required":
                continue
            try:
                outcome = await self._reconcile(row)
            except MutationError, OSError, RuntimeError:
                # IDs, kinds and states only: never a ref, a path or content.
                logger.warning(
                    "vault operation %s blocked: %s %s", row.operation_id, row.kind, row.state
                )
                continue
            if outcome is not None:
                self._remember(row.operation_id, row.request_sha256, outcome)
                outcomes.append(outcome)
        return outcomes

    async def _apply(
        self,
        operation_id: str,
        digest: str,
        workspace_id: str,
        policy_revision: int,
        root_physical_id: str,
        request: _Request,
    ) -> OperationOutcome:
        existing = await run_sync(self._journal.get, operation_id)
        if existing is not None:
            if (existing.workspace_id, existing.request_sha256) != (workspace_id, digest):
                raise PublicationError("command_conflict", operation_id)
            # A row a previous launch left and reconcile could not finish needs recovery.
            return OperationOutcome(operation_id, "recovery_required", True, 0)
        root = await run_sync(
            lambda: self._resolve_checked(workspace_id, policy_revision, root_physical_id)
        )
        async with self._coordinator.lease(root, request.paths, wait_s=self._lease_wait_s) as lease:
            return await run_sync(
                lambda: self._apply_held(operation_id, digest, request, root, lease)
            )

    def _preview_held(self, root: OrdinaryMutationRoot, request: _Request) -> _Draft:
        # The root checks apply's lease runs first, so a preview refuses as apply would, a
        # volume of an unsupported type included.
        _revalidate_root(root)
        self._require_clear(root, request.paths)
        return self._draft(root, request)

    def _apply_held(
        self,
        operation_id: str,
        digest: str,
        request: _Request,
        root: OrdinaryMutationRoot,
        lease: MutationLease,
    ) -> OperationOutcome:
        self._require_same_root(root)
        self._require_clear(root, request.paths)
        draft = self._draft(root, request)
        data = draft.data
        if draft.source is not None:
            assert request.ref is not None
            # Through one verified handle: alias, read-only, size, link count, identity and hash
            # all refuse here, before any row or vault change. A delete keeps these bytes.
            data = self._native.read_verified(lease, root, request.ref, draft.source)
        row = self._journal.plan(operation_id, root, request.kind, draft.steps, digest)
        kept: str | None = None
        try:
            steps = row.steps
            if request.kind == "delete":
                assert request.ref is not None and data is not None
                kept = self._recovery.put(
                    root.workspace_id, "deleted", request.ref, data
                ).recovery_id
                # Hard rule 1: the copy is read back and re-hashed against its row before the
                # name goes; a refusal abandons below with the note untouched. The remove step
                # needs no bytes, so the note's go first and one note's bytes is the peak.
                data = None
                self._recovery.read(kept)
                steps = (replace(steps[-1], preimage=kept),)
            row = self._journal.transition(operation_id, "planned", "preserved", steps)
            row = self._journal.transition(operation_id, "preserved", "applying")
        except BaseException:
            self._abandon(operation_id, kept)
            raise
        return self._run(row, root, lease, draft.source, data)

    def _run(
        self,
        row: OperationRow,
        root: OrdinaryMutationRoot,
        lease: MutationLease,
        source: FileFingerprint | None,
        data: bytes | None,
    ) -> OperationOutcome:
        step = row.steps[-1]
        target = step.to or step.ref
        try:
            if step.action != "remove" and "/" in target:
                self._native.ensure_folders(lease, root, tuple(target.split("/"))[:-1])
            if step.action == "move":
                assert step.to is not None and source is not None
                self._native.move_verified(lease, root, step.ref, step.to, source)
            elif step.action == "remove":
                assert source is not None
                self._native.remove_verified(lease, root, step.ref, source)
            else:
                assert data is not None
                staged = self._native.stage(
                    lease, root, step.ref, _reservation(row.operation_id), data
                )
                self._native.publish_create(lease, staged)
        except (MutationError, OSError) as exc:
            reason = _code(exc)
            try:
                state = self._step_state(root, step)
            except MutationError, OSError:
                state = "diverged"
            if state == "applied":
                return self._finish(row, root, lease, "done")
            if state == "pending":
                failed = "conflict" if reason == "conflict" else "failed"
                return self._finish(row, root, lease, failed, reason)
            return self._finish(row, root, lease, "recovery_required", reason)
        return self._finish(row, root, lease, "done")

    async def _reconcile(self, row: OperationRow) -> OperationOutcome | None:
        root = await run_sync(
            lambda: self._resolve_checked(
                row.workspace_id, row.policy_revision, row.root_physical_id
            )
        )
        async with self._coordinator.lease(
            root, row.steps[-1].paths(), wait_s=self._lease_wait_s
        ) as lease:
            return await run_sync(lambda: self._reconcile_held(row, root, lease))

    def _reconcile_held(
        self, row: OperationRow, root: OrdinaryMutationRoot, lease: MutationLease
    ) -> OperationOutcome | None:
        self._require_same_root(root)
        latest = self._journal.get(row.operation_id)
        if latest is None:
            return None
        state = self._step_state(root, latest.steps[-1])
        return self._finish(latest, root, lease, _RECONCILED[state])

    def _finish(
        self,
        row: OperationRow,
        root: OrdinaryMutationRoot,
        lease: MutationLease,
        status: OperationStatus,
        reason: str | None = None,
    ) -> OperationOutcome:
        self._tidy(row, root, lease, status)
        try:
            if status == "recovery_required":
                self._journal.transition(row.operation_id, row.state, "recovery_required")
            else:
                self._journal.delete(row.operation_id, row.state)
        except RuntimeError as exc:
            # The vault already holds this outcome; the next launch reconciles the row.
            logger.warning("vault operation %s kept its row: %s", row.operation_id, _code(exc))
        created = tuple(
            step.ref
            for step in row.steps
            if step.action == "mkdir" and os.path.isdir(root.path.joinpath(*step.ref.split("/")))
        )
        applied = len(created) + (1 if status == "done" else 0)
        return OperationOutcome(row.operation_id, status, False, applied, reason, created)

    def _tidy(
        self,
        row: OperationRow,
        root: OrdinaryMutationRoot,
        lease: MutationLease,
        status: OperationStatus,
    ) -> None:
        """Clear what a finished row no longer needs: a restore's stage and, once restored, its
        recovery entry; a delete's kept copy when the note was never removed."""
        if status == "recovery_required":
            return
        step = row.steps[-1]
        try:
            if step.action == "create" and not self._discard_stage(lease, root, step, row):
                logger.warning("vault operation %s left its stage", row.operation_id)
            if step.preimage is not None and (
                (step.action == "create" and status == "done")
                or (step.action == "remove" and status != "done")
            ):
                self._recovery.drop(step.preimage)
        except (MutationError, OSError, RuntimeError) as exc:
            logger.warning("vault operation %s cleanup: %s", row.operation_id, _code(exc))

    def _abandon(self, operation_id: str, kept: str | None) -> None:
        # Nothing in the vault changed yet: the row and any copy it kept go.
        try:
            if kept is not None:
                self._recovery.drop(kept)
        except RuntimeError as exc:
            logger.warning("vault operation %s kept its copy: %s", operation_id, _code(exc))
        try:
            self._journal.delete(operation_id, ("planned", "preserved", "applying"))
        except RuntimeError as exc:
            logger.warning("vault operation %s kept its row: %s", operation_id, _code(exc))

    def _discard_stage(
        self,
        lease: MutationLease,
        root: OrdinaryMutationRoot,
        step: OperationStep,
        row: OperationRow,
    ) -> bool:
        """Delete this restore's stage, only while it holds exactly the bytes being restored."""
        reservation = _reservation(row.operation_id)
        parent = step.ref.rpartition("/")[0]
        stage_ref = f"{parent}/{reservation.stage_name}" if parent else reservation.stage_name
        found = self._observe(root, stage_ref)
        if found is None:
            return True
        if found.sha256 != step.post:
            return False
        self._native.discard(
            lease,
            StagedPublication(
                root,
                step.ref,
                row.operation_id,
                reservation.stage_name,
                reservation.backup_name,
                found.physical_id,
                found.sha256,
                found.byte_size,
            ),
        )
        return True

    def _draft(self, root: OrdinaryMutationRoot, request: _Request) -> _Draft:
        steps: list[OperationStep] = []
        source: FileFingerprint | None = None
        data: bytes | None = None
        if request.kind == "restore":
            assert request.recovery_id is not None
            entry, data = self._recovery.read(request.recovery_id)
            if entry.workspace_id != root.workspace_id:
                raise PublicationError("recovery_missing")
            if request.to is not None and is_bot_memory_ref(request.to):
                raise PublicationError("excluded_path")
            post: str | None = entry.sha256
        else:
            assert request.ref is not None
            source = self._observe(root, request.ref)
            if source is None:
                raise MutationError("missing_target")
            # The user acts on the note they loaded; the native layer then re-verifies this
            # read through a held handle.
            if source.sha256 != request.expected_sha256:
                raise PublicationError("conflict")
            post = source.sha256
        if request.to is not None:
            for folder in self._missing_folders(root, tuple(request.to.split("/"))[:-1]):
                steps.append(OperationStep(len(steps), "mkdir", folder, None, None, None, None))
            # A taken name refuses, unless the volume folds case and finds a case-only rename's
            # own source under the new spelling: the rename only respells it.
            taken = self._observe(root, request.to)
            if taken is not None and not (
                source is not None
                and _case_only(request.ref, request.to)
                and taken.physical_id == source.physical_id
            ):
                raise PublicationError("conflict")
        n = len(steps)
        if request.kind == "restore":
            assert request.to is not None
            # The create's preimage names the recovery entry its bytes come from.
            steps.append(
                OperationStep(n, "create", request.to, None, None, post, request.recovery_id)
            )
        elif request.kind == "delete":
            assert request.ref is not None
            steps.append(OperationStep(n, "remove", request.ref, None, post, None, None))
        else:
            assert request.ref is not None
            steps.append(OperationStep(n, "move", request.ref, request.to, post, post, None))
        return _Draft(tuple(steps), source, data)

    def _step_state(self, root: OrdinaryMutationRoot, step: OperationStep) -> _StepState:
        """Read one file step's state from its paths. A rename and a create are each one
        no-replace rename, so a source still holding `pre`, or a create whose name holds other
        bytes, proves that ours did not happen."""
        if step.action == "move":
            assert step.to is not None
            source = self._observe(root, step.ref)
            if source is not None and _case_only(step.ref, step.to):
                spelled = self._observe(root, step.to)
                # One file under both spellings means the volume folds case: only the name its
                # folder holds tells whether the rename ran. Two names read as any move.
                if spelled is not None and spelled.physical_id == source.physical_id:
                    if source.sha256 != step.pre:
                        return "diverged"
                    name = self._leaf_name(root, step.ref)
                    if name == _leaf(step.to):
                        return "applied"
                    return "pending" if name == _leaf(step.ref) else "diverged"
            if source is not None:
                return "pending" if source.sha256 == step.pre else "diverged"
            dest = self._observe(root, step.to)
            return "applied" if dest is not None and dest.sha256 == step.post else "diverged"
        current = self._observe(root, step.ref)
        held = None if current is None else current.sha256
        if step.action == "create":
            return "applied" if held == step.post else "pending"
        if held is None:
            return "applied"
        return "pending" if held == step.pre else "diverged"

    @staticmethod
    def _observe(root: OrdinaryMutationRoot, ref: str) -> FileFingerprint | None:
        """Open-read-close a note's identity and hash, or None when it is absent.

        This only names what the native layer must then find: every write verifies it through
        a held handle, which also refuses an alias, a link in the chain or a read-only note."""
        path = root.path.joinpath(*ref.split("/"))
        try:
            if not storage.check_file(path):
                return None
            info = os.lstat(path)
            data = osfs.read_regular(path, MAX_PUBLICATION_BYTES + 1, follow_symlinks=False)
        except FileNotFoundError:
            return None
        except PermissionError:
            raise MutationError("target_busy") from None
        except StateError, OSError:
            # A link, a reparse point, a hard link or something other than a file.
            raise MutationError("unsafe_target") from None
        if len(data) > MAX_PUBLICATION_BYTES:
            raise MutationError("unsafe_target")
        if len(data) != info.st_size:
            raise MutationError("conflict")
        return FileFingerprint(
            f"{info.st_dev}:{info.st_ino}", hashlib.sha256(data).hexdigest(), len(data)
        )

    @staticmethod
    def _leaf_name(root: OrdinaryMutationRoot, ref: str) -> str | None:
        """The exact name the folder holds for `ref`'s leaf in any letter case."""
        parts = ref.split("/")
        with os.scandir(root.path.joinpath(*parts[:-1])) as entries:
            names = [
                entry.name for entry in entries if entry.name.casefold() == parts[-1].casefold()
            ]
        return names[0] if len(names) == 1 else None

    @staticmethod
    def _missing_folders(root: OrdinaryMutationRoot, parts: tuple[str, ...]) -> list[str]:
        for depth in range(1, len(parts) + 1):
            try:
                info = os.lstat(root.path.joinpath(*parts[:depth]))
            except FileNotFoundError:
                return ["/".join(parts[:end]) for end in range(depth, len(parts) + 1)]
            if not stat.S_ISDIR(info.st_mode) or (
                getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
            ):
                raise MutationError("unsafe_parent")
        return []

    def _require_clear(self, root: OrdinaryMutationRoot, paths: tuple[str, ...]) -> None:
        self.require_writable(root.workspace_id)
        # `OperationJournal.plan` does not read `vault_publications`: a save stuck on a path an
        # operation then moved would later recover against the wrong name.
        held = {path.casefold() for path in paths}
        if any(
            row.workspace_id == root.workspace_id and row.ref.casefold() in held
            for row in self._publications.list_pending()
        ):
            raise PublicationError("publication_busy")

    def _remember(self, command_id: str, digest: str, outcome: OperationOutcome | str) -> None:
        if len(self._receipts) >= MAX_RECEIPTS:
            del self._receipts[next(iter(self._receipts))]  # insertion order: the oldest
        self._receipts[command_id] = (digest, outcome)

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
        if self._resolve_checked(root.workspace_id, root.policy_revision, root.physical_id) != root:
            raise PublicationError("workspace_changed")
