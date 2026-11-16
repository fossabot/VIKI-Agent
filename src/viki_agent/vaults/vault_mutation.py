"""Filesystem mutation leases and handle-relative publication primitives.

The coordinator serializes VIKI actors by filesystem resource identity.  It is not an
external-editor lock.  Publication therefore also uses no-replace, handle-relative namespace
changes: on Windows, sharing modes keep other programs off a held file; POSIX has none, so each
rename checks the file's identity before and its identity and bytes after, and moves back on a
mismatch, except another program's file at a note's name, which stays.  Durable replacement
orchestration lives above this module so it can checkpoint between the individually verified
steps exposed here.
"""

from __future__ import annotations

import asyncio
import ctypes
import errno
import hashlib
import logging
import ntpath
import os
import stat
import sys
import uuid
from collections.abc import AsyncIterator, Callable, Iterable, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from viki_agent import osfs
from viki_agent.code.code_files import CodeError, is_private, split_ref
from viki_agent.domain import Workspace
from viki_agent.vaults.vault import BACKUP_PREFIX, STAGE_PREFIX, excluded_part
from viki_agent.workspaces import chain_has_reparse, root_key

logger = logging.getLogger(__name__)

# The grammar a root's primitives apply: a vault's notes and attachments, a code folder's files,
# or "instructions", a vault root whose only refs are `INSTRUCTION_REFS`, replaced in place
# (hard rule 1's one exception, ADR-082).
MutationRootKind = Literal["vault", "code", "instructions"]
INSTRUCTION_REFS = frozenset({"AGENTS.md", "CLAUDE.md"})

MAX_PUBLICATION_BYTES = 2 * 1024 * 1024
# The most a caller may ever ask to stage: the one non-note file kind, a kept chat attachment
# (hard rule 1). A note keeps `MAX_PUBLICATION_BYTES`.
MAX_STAGE_BYTES = 20 << 20
ATTACHMENT_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp"})
_NOTE_SUFFIXES = frozenset({".md"})
STAGE_SUFFIX = ".tmp"

# A name Win32 reserves (`ntpath.isreserved`: `"*:<>?|`, control characters, a trailing dot or
# space, a device stem such as `COM1` or `nul`) or one longer than 255 UTF-16 units (510 bytes).
# The lease refuses them before any reservation, so no save stages a file it can never publish.
# The Win32 rules apply on every OS, so a note stays portable.
_MAX_NAME_BYTES = 510


class MutationError(RuntimeError):
    """A typed, path-free mutation failure safe to map at a service boundary."""

    def __init__(self, code: str, *, stage_retained: bool = False) -> None:
        self.code = code
        self.stage_retained = stage_retained
        super().__init__(code)


@dataclass(frozen=True)
class OrdinaryMutationRoot:
    """A caller-supplied snapshot of an admitted ordinary workspace.

    The policy service remains responsible for rechecking the current policy revision before it
    creates this context.  This type prevents a lower layer from accepting an arbitrary path.
    """

    workspace_id: str
    path: Path
    canonical_key: str
    physical_id: str
    policy_revision: int
    git_common_dir: str | None
    # Selects the ref grammar every primitive applies: `code_ref` in a code folder, else a
    # vault's note and attachment refs, `instruction_ref` on an instructions root. Last, with a
    # default, so positional callers keep working.
    kind: MutationRootKind = "vault"
    # The (name, SHA-256) pairs of the root instruction files every other protected workspace's
    # stored identity records, as the resolver read them: `save_instruction` refuses to give this
    # root one of them. Evidence about other workspaces, not this root's identity, so equality and
    # hashing ignore it.
    protected_instructions: frozenset[tuple[str, str]] = field(default=frozenset(), compare=False)

    @classmethod
    def from_admitted_workspace(cls, workspace: Workspace) -> OrdinaryMutationRoot:
        if (
            workspace.kind != "vault"
            or workspace.policy != "ordinary"
            or not workspace.persistent
            or workspace.bootstrap != "complete"
        ):
            raise MutationError("ordinary_workspace_required")
        path = Path(workspace.root.display)
        if root_key(path) != workspace.root.key:
            raise MutationError("stale_root")
        return cls(
            workspace_id=workspace.workspace_id,
            path=path,
            canonical_key=workspace.root.key,
            physical_id=workspace.root.physical_id,
            policy_revision=workspace.policy_revision,
            git_common_dir=workspace.identity.git_common_dir,
        )

    @classmethod
    def from_trusted_code_folder(cls, workspace: Workspace) -> OrdinaryMutationRoot:
        """A trusted code folder; it has no bootstrap, so none is read (a code row's is `none`)."""
        if workspace.kind != "code" or workspace.policy != "ordinary" or not workspace.persistent:
            raise MutationError("ordinary_workspace_required")
        path = Path(workspace.root.display)
        if root_key(path) != workspace.root.key:
            raise MutationError("stale_root")
        return cls(
            workspace_id=workspace.workspace_id,
            path=path,
            canonical_key=workspace.root.key,
            physical_id=workspace.root.physical_id,
            policy_revision=workspace.policy_revision,
            git_common_dir=workspace.identity.git_common_dir,
            kind="code",
        )


@dataclass(frozen=True)
class _Resource:
    kind: Literal["path", "git"]
    physical_root: str
    relative: tuple[str, ...]
    canonical_key: str

    @property
    def order_key(self) -> tuple[str, str, tuple[str, ...], str]:
        return self.kind, self.physical_root, self.relative, self.canonical_key


class MutationLease:
    """Capability yielded by ``MutationCoordinator.lease``; invalid after its context exits."""

    def __init__(self, root: OrdinaryMutationRoot, resources: tuple[_Resource, ...]) -> None:
        self.root = root
        self._resources = resources
        self._active = True

    def _close(self) -> None:
        self._active = False

    def require_target(self, root: OrdinaryMutationRoot, ref: str) -> tuple[str, ...]:
        """A held note ref, a code ref on a code root, or an instruction ref on an instructions
        root: every primitive but a create's own steps takes only these."""
        if root.kind == "instructions":
            return self._require(root, ref, instruction_ref)
        return self._require(root, ref, code_ref if root.kind == "code" else _safe_ref)

    def _require(
        self, root: OrdinaryMutationRoot, ref: str, grammar: Callable[[str], tuple[str, ...]]
    ) -> tuple[str, ...]:
        if not self._active or root != self.root:
            raise MutationError("lease_required")
        parts = grammar(ref)
        wanted = _path_resource(root, parts)
        if not any(_covers(resource, wanted) for resource in self._resources):
            raise MutationError("lease_required")
        return parts

    def require_chain(self, root: OrdinaryMutationRoot, parts: tuple[str, ...]) -> None:
        """Admit a folder chain a held target lies in, above, or under.

        ``require_target`` takes a note ref, and a lease over `a/b/n.md` holds no resource for the
        folder `a/b` it needs created.  A chain on the held line of one tree is admitted instead:
        folder creation is create-only and verified, so two notes in one folder need no order.
        """

        if not self._active or root != self.root:
            raise MutationError("lease_required")
        wanted = _path_resource(root, parts)
        if not any(_resources_conflict(resource, wanted) for resource in self._resources):
            raise MutationError("lease_required")


class MutationCoordinator:
    """One-process lease coordinator for roots, paths, aliases, overlap, and Git metadata."""

    def __init__(self) -> None:
        self._condition = asyncio.Condition()
        self._held: dict[int, tuple[_Resource, ...]] = {}
        self._next_token = 0

    @asynccontextmanager
    async def lease(
        self,
        root: OrdinaryMutationRoot,
        refs: Iterable[str],
        *,
        include_git_common: bool = False,
        wait_s: float | None = None,
    ) -> AsyncIterator[MutationLease]:
        """`wait_s` bounds the wait for a conflicting lease; past it the request is refused."""

        _revalidate_root(root)
        grammar = _create_grammar(root)
        resources = {_path_resource(root, grammar(ref)) for ref in refs}
        if not resources:
            resources.add(_path_resource(root, ()))
        if include_git_common:
            resources.add(_git_resource(root))
        ordered = tuple(sorted(resources, key=lambda resource: resource.order_key))
        async with self._condition:
            try:
                async with asyncio.timeout(wait_s):
                    while any(_sets_conflict(ordered, held) for held in self._held.values()):
                        await self._condition.wait()
            except TimeoutError as exc:
                # The holder may be another actor's save; the caller retries or reports.
                raise MutationError("vault_locked") from exc
            self._next_token += 1
            token = self._next_token
            self._held[token] = ordered
        lease = MutationLease(root, ordered)
        try:
            yield lease
        finally:
            lease._close()
            async with self._condition:
                self._held.pop(token, None)
                self._condition.notify_all()


@dataclass(frozen=True)
class PublicationReservation:
    operation_id: str
    stage_name: str
    backup_name: str


@dataclass(frozen=True)
class StagedPublication:
    root: OrdinaryMutationRoot
    ref: str
    operation_id: str
    stage_name: str
    backup_name: str
    physical_id: str
    sha256: str
    byte_size: int

    @property
    def fingerprint(self) -> FileFingerprint:
        return FileFingerprint(self.physical_id, self.sha256, self.byte_size)


@dataclass(frozen=True)
class FileFingerprint:
    physical_id: str
    sha256: str
    byte_size: int


@dataclass(frozen=True)
class PublishedPublication:
    ref: str
    fingerprint: FileFingerprint


class PreparedReplacement:
    """Held replacement handles; callers durably checkpoint between ordered methods."""

    def __init__(
        self,
        parent_handle: int,
        stage_handle: FileHandle,
        target_handle: FileHandle,
        target_name: str,
        backup_name: str,
        old: FileFingerprint,
        new: FileFingerprint,
    ) -> None:
        self.parent_handle = parent_handle
        self.stage_handle = stage_handle
        self.target_handle = target_handle
        self.target_name = target_name
        self.backup_name = backup_name
        self.old = old
        self.new = new
        self._state: Literal["prepared", "backed_up", "published", "verified"] = "prepared"

    def move_target_to_backup(self) -> None:
        if self._state != "prepared":
            raise MutationError("invalid_publication_order", stage_retained=True)
        _rename_handle_relative_no_replace(self.target_handle, self.parent_handle, self.backup_name)
        self._state = "backed_up"

    def publish_stage_no_replace(self) -> None:
        if self._state != "backed_up":
            raise MutationError("invalid_publication_order", stage_retained=True)
        _rename_handle_relative_no_replace(self.stage_handle, self.parent_handle, self.target_name)
        self._state = "published"

    def verify_published(self) -> PublishedPublication:
        if self._state not in {"published", "verified"}:
            raise MutationError("invalid_publication_order", stage_retained=True)
        handle = _open_verified_relative(
            self.parent_handle,
            self.target_name,
            self.new,
            missing_code="missing_target",
            delete=False,
        )
        _CloseHandle(handle)
        self._state = "verified"
        return PublishedPublication(self.target_name, self.new)


class VaultPublication:
    """Stage bytes and publish a new relative file under an active mutation lease."""

    def reserve(self) -> PublicationReservation:
        operation_id = str(uuid.uuid4())
        return PublicationReservation(
            operation_id,
            f"{STAGE_PREFIX}{operation_id}{STAGE_SUFFIX}",
            f"{BACKUP_PREFIX}{operation_id}{STAGE_SUFFIX}",
        )

    def stage(
        self,
        lease: MutationLease,
        root: OrdinaryMutationRoot,
        ref: str,
        reservation: PublicationReservation,
        content: bytes,
    ) -> StagedPublication:
        parts = lease._require(root, ref, _create_grammar(root))
        cap = _byte_cap(root, parts)
        _validate_reservation(reservation)
        if type(content) is not bytes or len(content) > cap:
            raise MutationError("invalid_content")
        with _guard_parent(root, parts[:-1]) as (_parent, parent_handle):
            try:
                handle = _create_relative_stage(parent_handle, reservation.stage_name)
            except OSError as exc:
                raise MutationError("stage_failed") from exc
            try:
                _write_handle(handle, content)
                _flush(handle)
                fingerprint = _fingerprint_handle(handle, unsafe_code="unsafe_stage", max_bytes=cap)
                if fingerprint.sha256 != hashlib.sha256(content).hexdigest():
                    raise MutationError("unsafe_stage", stage_retained=True)
                if sys.platform != "win32":
                    # The stage's new name is on the drive before a journal row records it.
                    os.fsync(parent_handle)
            except (MutationError, OSError) as exc:
                # No journal row records this stage yet, so the save deletes it through
                # the handle that created it. Only a delete that fails leaves it behind.
                code = exc.code if isinstance(exc, MutationError) else "stage_failed"
                try:
                    _delete_handle(handle)
                except OSError:
                    raise MutationError(code, stage_retained=True) from exc
                raise MutationError(code) from exc
            finally:
                _CloseHandle(handle)
            return StagedPublication(
                root=root,
                ref=PurePosixPath(*parts).as_posix(),
                operation_id=reservation.operation_id,
                stage_name=reservation.stage_name,
                backup_name=reservation.backup_name,
                physical_id=fingerprint.physical_id,
                sha256=fingerprint.sha256,
                byte_size=fingerprint.byte_size,
            )

    def publish_create(
        self,
        lease: MutationLease,
        staged: StagedPublication,
    ) -> PublishedPublication:
        _refuse_instructions(staged.root)
        parts = lease._require(staged.root, staged.ref, _create_grammar(staged.root))
        cap = _byte_cap(staged.root, parts)
        with _guard_parent(staged.root, parts[:-1]) as (_parent, parent_handle):
            handle = _open_verified_stage(parent_handle, staged, max_bytes=cap)
            try:
                if sys.platform != "win32":
                    # The stage was created 0600; the new file takes a plain create's mode
                    # before it takes its name, and the flush keeps the mode with the bytes.
                    os.fchmod(handle.fd, _CREATE_MODE)
                _flush(handle)
                _rename_handle_relative_no_replace(handle, parent_handle, parts[-1])
            finally:
                _CloseHandle(handle)
            published = _open_verified_relative(
                parent_handle,
                parts[-1],
                staged.fingerprint,
                missing_code="missing_target",
                delete=False,
                max_bytes=cap,
            )
            _CloseHandle(published)
            return PublishedPublication(staged.ref, staged.fingerprint)

    @contextmanager
    def prepare_replace(
        self,
        lease: MutationLease,
        staged: StagedPublication,
        expected_sha256: str,
        backup_name: str,
    ) -> Iterator[PreparedReplacement]:
        parts = lease.require_target(staged.root, staged.ref)
        if not _is_sha256(expected_sha256):
            raise MutationError("invalid_expected_hash", stage_retained=True)
        _validate_leaf(backup_name)
        if backup_name != staged.backup_name:
            raise MutationError("invalid_backup_name", stage_retained=True)
        with _guard_parent(staged.root, parts[:-1]) as (parent, parent_handle):
            stage_handle = _open_verified_stage(parent_handle, staged)
            try:
                _flush(stage_handle)
                target_handle, old = _open_target(parent_handle, parent, parts[-1])
                try:
                    # A case-insensitive volume opens `Agents.md` for `AGENTS.md`, and the
                    # replace would respell it: only the exact name on disk is replaced. Off
                    # Windows the folder's own listing is asked too, since the kernel's path
                    # for a descriptor may carry the lookup's spelling.
                    if staged.root.kind == "instructions" and (
                        _final_leaf(target_handle) != parts[-1]
                        or (
                            sys.platform != "win32"
                            and not _listed_as(parent_handle, target_handle, parts[-1])
                        )
                    ):
                        raise MutationError("excluded_path", stage_retained=True)
                    if old.sha256 != expected_sha256:
                        raise MutationError("conflict", stage_retained=True)
                    _carry_mode(stage_handle, target_handle)
                    yield PreparedReplacement(
                        parent_handle,
                        stage_handle,
                        target_handle,
                        parts[-1],
                        backup_name,
                        old,
                        staged.fingerprint,
                    )
                finally:
                    _CloseHandle(target_handle)
            finally:
                _CloseHandle(stage_handle)

    def move_verified_no_replace(
        self,
        lease: MutationLease,
        root: OrdinaryMutationRoot,
        ref: str,
        source_name: str,
        expected: FileFingerprint,
    ) -> PublishedPublication:
        """Recovery primitive: move an exact sibling to an absent canonical leaf."""

        parts = lease.require_target(root, ref)
        _validate_leaf(source_name)
        with _guard_parent(root, parts[:-1]) as (_parent, parent_handle):
            handle = _open_verified_relative(
                parent_handle, source_name, expected, missing_code="missing_recovery_source"
            )
            try:
                _rename_handle_relative_no_replace(handle, parent_handle, parts[-1])
            finally:
                _CloseHandle(handle)
            verified = _open_verified_relative(
                parent_handle,
                parts[-1],
                expected,
                missing_code="missing_target",
                delete=False,
            )
            _CloseHandle(verified)
            return PublishedPublication(ref, expected)

    def move_verified(
        self,
        lease: MutationLease,
        root: OrdinaryMutationRoot,
        source_ref: str,
        dest_ref: str,
        expected: FileFingerprint,
    ) -> PublishedPublication:
        """Move a verified note to an absent ref, inside its parent or into another one."""

        _refuse_instructions(root)
        source_parts = lease.require_target(root, source_ref)
        dest_parts = lease.require_target(root, dest_ref)
        with _guard_parents(root, source_parts[:-1], dest_parts[:-1]) as (
            (source_parent, source_parent_handle),
            (dest_parent, dest_parent_handle),
        ):
            handle, actual = _open_target(source_parent_handle, source_parent, source_parts[-1])
            try:
                if actual != expected:
                    raise MutationError("conflict")
                _rename_handle_relative_no_replace(handle, dest_parent_handle, dest_parts[-1])
                # The handle followed the file, so only it knows the entry the rename made.
                if not same_final_path(handle, str(dest_parent / dest_parts[-1])):
                    raise MutationError("excluded_path")
            finally:
                _CloseHandle(handle)
            verified = _open_verified_relative(
                dest_parent_handle,
                dest_parts[-1],
                expected,
                missing_code="missing_target",
                delete=False,
            )
            _CloseHandle(verified)
            return PublishedPublication(PurePosixPath(*dest_parts).as_posix(), expected)

    def read_verified(
        self,
        lease: MutationLease,
        root: OrdinaryMutationRoot,
        ref: str,
        expected: FileFingerprint,
    ) -> bytes:
        """Read a bounded note through one verified handle, for the copy kept before a remove."""

        parts = lease.require_target(root, ref)
        with _guard_parent(root, parts[:-1]) as (parent, parent_handle):
            handle, actual = _open_target(parent_handle, parent, parts[-1])
            try:
                if actual != expected:
                    raise MutationError("conflict")
                content = _read_handle(handle, expected.byte_size)
            finally:
                _CloseHandle(handle)
        if hashlib.sha256(content).hexdigest() != expected.sha256:
            raise MutationError("conflict")
        return content

    def remove_verified(
        self,
        lease: MutationLease,
        root: OrdinaryMutationRoot,
        ref: str,
        expected: FileFingerprint,
    ) -> None:
        """Remove the note that still holds the identity and hash a read of it returned."""

        _refuse_instructions(root)
        parts = lease.require_target(root, ref)
        with _guard_parent(root, parts[:-1]) as (parent, parent_handle):
            handle, actual = _open_target(parent_handle, parent, parts[-1])
            try:
                if actual != expected:
                    raise MutationError("conflict")
                _delete_handle(handle)
            finally:
                _CloseHandle(handle)

    def ensure_folders(
        self,
        lease: MutationLease,
        root: OrdinaryMutationRoot,
        parent_parts: tuple[str, ...],
    ) -> tuple[str, ...]:
        """Create the missing folders of one note's parent chain and return what it created.

        A component is created only where it is absent, and the whole chain is then reopened
        handle-relative, so a file, link or reparse point at a component refuses `unsafe_parent`
        and an alias refuses `excluded_path`.  A created folder is not removed again: an empty
        folder is not data loss, and the caller reports it in the operation's outcome.
        """

        _refuse_instructions(root)
        lease.require_chain(root, parent_parts)
        # `_safe_ref`'s per-part rules, for a chain that reached here as parts and not as a ref.
        for part in parent_parts:
            _validate_leaf(part)
            if excluded_part(part, write=True):
                raise MutationError("excluded_path")
            if (
                ntpath.isreserved(part)
                or len(part.encode("utf-16-le", "surrogatepass")) > _MAX_NAME_BYTES
                or _posix_cannot_hold(part)
            ):
                raise MutationError("invalid_ref")
        created: list[str] = []
        for depth in range(len(parent_parts)):
            chain = parent_parts[: depth + 1]
            with _guard_parent(root, chain[:-1]) as (parent, parent_handle):
                if _folder_exists(parent_handle, chain[-1]):
                    continue
                try:
                    _mkdir_relative(parent_handle, parent, chain[-1])
                except FileExistsError:
                    # Another note's save needed the same folder; it is not this call's to report.
                    continue
                except OSError as exc:
                    raise MutationError("folder_create_failed") from exc
                created.append(PurePosixPath(*chain).as_posix())
        # Verify the chain a write will travel, end to end, through handles.
        with _guard_parent(root, parent_parts):
            pass
        return tuple(created)

    def probe_verified(
        self,
        lease: MutationLease,
        root: OrdinaryMutationRoot,
        ref: str,
        name: str | None,
        expected: FileFingerprint,
    ) -> Literal["absent", "match", "unexpected"]:
        """Classify one recorded sibling through a pinned handle without changing it."""

        parts = lease.require_target(root, ref)
        leaf = parts[-1] if name is None else name
        _validate_leaf(leaf)
        # A missing folder holds no entry either; a link, file or busy folder still refuses.
        try:
            with _guard_parent(root, parts[:-1], missing_code="missing_probe") as (
                _parent,
                parent_handle,
            ):
                try:
                    handle = _open_probe(parent_handle, leaf)
                    try:
                        actual = _fingerprint_handle(handle, unsafe_code="unsafe_probe")
                    finally:
                        _CloseHandle(handle)
                except MutationError as exc:
                    if exc.code == "unsafe_probe":
                        return "unexpected"
                    raise
                return "match" if actual == expected else "unexpected"
        except MutationError as exc:
            if exc.code == "missing_probe":
                return "absent"
            raise

    def attachment_sha256(
        self, lease: MutationLease, root: OrdinaryMutationRoot, ref: str
    ) -> str | None:
        """Hash a kept attachment through one verified handle, or answer None when it is absent.

        The keep path reuses an identical file instead of naming a new one.  A link, a folder or
        a file over `MAX_STAGE_BYTES` refuses `unsafe_target`, and a short-name alias
        `excluded_path`.
        """

        if root.kind != "vault":
            # Only a vault keeps chat attachments.
            raise MutationError("invalid_ref")
        parts = lease._require(root, ref, attachment_ref)
        try:
            with _guard_parent(root, parts[:-1], missing_code="missing_attachment") as (
                parent,
                parent_handle,
            ):
                handle = _open_attachment(parent_handle, parts[-1])
                try:
                    if not same_final_path(handle, str(parent / parts[-1])):
                        raise MutationError("excluded_path")
                    return _fingerprint_handle(
                        handle, unsafe_code="unsafe_target", max_bytes=MAX_STAGE_BYTES
                    ).sha256
                finally:
                    _CloseHandle(handle)
        except MutationError as exc:
            if exc.code == "missing_attachment":
                return None
            # There is no stage here, so no refusal claims one.
            raise MutationError(exc.code) from exc

    def discard_backup_after_publish(
        self,
        lease: MutationLease,
        root: OrdinaryMutationRoot,
        ref: str,
        backup_name: str,
        published: FileFingerprint,
        backup: FileFingerprint,
    ) -> None:
        """Reverify published and old identities together, then delete only the old backup."""

        parts = lease.require_target(root, ref)
        _validate_leaf(backup_name)
        with _guard_parent(root, parts[:-1]) as (_parent, parent_handle):
            published_handle = _open_verified_relative(
                parent_handle,
                parts[-1],
                published,
                missing_code="missing_target",
            )
            try:
                backup_handle = _open_verified_relative(
                    parent_handle,
                    backup_name,
                    backup,
                    missing_code="missing_recovery_source",
                )
                try:
                    _delete_handle(backup_handle)
                finally:
                    _CloseHandle(backup_handle)
            finally:
                _CloseHandle(published_handle)

    def discard(self, lease: MutationLease, staged: StagedPublication) -> bool:
        parts = lease._require(staged.root, staged.ref, _create_grammar(staged.root))
        # A missing folder holds no stage either; a link, file or busy folder refuses.
        try:
            with _guard_parent(staged.root, parts[:-1], missing_code="missing_stage") as (
                _parent,
                parent_handle,
            ):
                handle = _open_verified_stage(
                    parent_handle, staged, max_bytes=_byte_cap(staged.root, parts)
                )
                try:
                    _delete_handle(handle)
                except OSError as exc:
                    raise MutationError("discard_failed", stage_retained=True) from exc
                finally:
                    _CloseHandle(handle)
                return True
        except MutationError as exc:
            if exc.code == "missing_stage":
                return False
            raise


def attachment_ref(ref: str) -> tuple[str, ...]:
    """Admit exactly `attachments/<leaf>` with an image suffix, the one non-note ref a write takes.

    Every note-ref refusal applies first, then the shape: one fixed parent, no nesting and no
    dot-prefixed leaf (hard rule 1).
    """

    parts = _safe_ref(ref, suffixes=ATTACHMENT_SUFFIXES)
    if len(parts) != 2 or parts[0] != "attachments" or parts[1].startswith("."):
        raise MutationError("invalid_ref")
    return parts


def instruction_ref(ref: str) -> tuple[str, ...]:
    """Admit exactly a root `AGENTS.md` or `CLAUDE.md`, the only refs an instructions root takes.

    An exact match: no other case, depth, suffix or spelling, so every name the vault grammar
    refuses but these two stays refused here too.
    """
    if not isinstance(ref, str) or ref not in INSTRUCTION_REFS:
        raise MutationError("invalid_ref")
    return (ref,)


def _refuse_instructions(root: OrdinaryMutationRoot) -> None:
    """An instructions root only replaces its file in place: no create, move, remove or folder."""
    if root.kind == "instructions":
        raise MutationError("invalid_ref")


def _write_parts(ref: str) -> tuple[str, ...]:
    """A note ref, or an attachment ref: the two things a create may write."""
    if isinstance(ref, str) and PurePosixPath(ref).suffix.casefold() in ATTACHMENT_SUFFIXES:
        return attachment_ref(ref)
    return _safe_ref(ref)


def code_ref(ref: str) -> tuple[str, ...]:
    """A code-folder ref: the code reader's grammar and private set, and the Win32 name rules.

    Any suffix or none is admitted, and so are the agent instruction files a vault save refuses;
    the writer's own stage and backup prefixes stay reserved at any depth. Off Windows, a part
    over NAME_MAX (255 UTF-8 bytes) is refused too.
    """
    if not isinstance(ref, str) or not 1 <= len(ref) <= 512:
        raise MutationError("invalid_ref")
    try:
        parts = split_ref(ref)
    except CodeError:
        raise MutationError("invalid_ref") from None
    if any(
        ntpath.isreserved(part)
        or len(part.encode("utf-16-le", "surrogatepass")) > _MAX_NAME_BYTES
        or _posix_cannot_hold(part)
        for part in parts
    ):
        raise MutationError("invalid_ref")
    if is_private(parts) or any(
        part.casefold().startswith((STAGE_PREFIX, BACKUP_PREFIX)) for part in parts
    ):
        raise MutationError("excluded_path")
    return parts


def _create_grammar(root: OrdinaryMutationRoot) -> Callable[[str], tuple[str, ...]]:
    """What a create's steps take: any code ref in a code folder, an instruction ref on an
    instructions root (whose stage and discard use it), else a note or attachment."""
    if root.kind == "instructions":
        return instruction_ref
    return code_ref if root.kind == "code" else _write_parts


def _byte_cap(root: OrdinaryMutationRoot, parts: tuple[str, ...]) -> int:
    """The ref decides the cap, so no caller can stage a note over `MAX_PUBLICATION_BYTES`.

    A code file keeps that cap whatever its suffix: only a vault keeps attachments.
    """
    if root.kind == "vault" and PurePosixPath(parts[-1]).suffix.casefold() in ATTACHMENT_SUFFIXES:
        return MAX_STAGE_BYTES
    return MAX_PUBLICATION_BYTES


def _safe_ref(ref: str, *, suffixes: frozenset[str] = _NOTE_SUFFIXES) -> tuple[str, ...]:
    if (
        not isinstance(ref, str)
        or not 1 <= len(ref) <= 512
        or "\x00" in ref
        or "\\" in ref
        or ":" in ref
    ):
        raise MutationError("invalid_ref")
    # Split by hand: PurePosixPath would silently drop the empty part of `a//b.md` and the `.`
    # of `a/./b.md`, and a leading `/` leaves an empty first part here.
    parts = tuple(ref.split("/"))
    if any(part in {"", ".", ".."} for part in parts):
        raise MutationError("invalid_ref")
    if any(excluded_part(part, write=True) for part in parts):
        raise MutationError("excluded_path")
    if (
        any(
            ntpath.isreserved(part)
            or len(part.encode("utf-16-le", "surrogatepass")) > _MAX_NAME_BYTES
            or _posix_cannot_hold(part)
            for part in parts
        )
        # The reader's own suffix rule, so a save never creates a note the reader hides.
        or PurePosixPath(parts[-1]).suffix.casefold() not in suffixes
    ):
        raise MutationError("invalid_ref")
    return parts


def _posix_cannot_hold(part: str) -> bool:
    """Off Windows, whether `part` is not strict UTF-8 (a lone surrogate) or is longer than
    NAME_MAX, 255 bytes. Windows counts UTF-16 units instead, against `_MAX_NAME_BYTES`."""
    if sys.platform == "win32":
        return False
    try:
        return len(part.encode("utf-8")) > 255
    except UnicodeEncodeError:
        return True


def _path_resource(root: OrdinaryMutationRoot, parts: tuple[str, ...]) -> _Resource:
    relative = tuple(part.casefold() for part in parts)
    absolute = root_key(root.path.joinpath(*parts))
    return _Resource("path", root.physical_id, relative, absolute)


def _git_resource(root: OrdinaryMutationRoot) -> _Resource:
    if root.git_common_dir is None:
        raise MutationError("git_common_unavailable")
    path = Path(root.git_common_dir)
    try:
        resolved = path.resolve(strict=True)
        info = resolved.lstat()
    except OSError as exc:
        raise MutationError("git_common_unavailable") from exc
    if not stat.S_ISDIR(info.st_mode) or _is_reparse(info) or chain_has_reparse(resolved):
        raise MutationError("git_common_unsafe")
    key = root_key(resolved)
    return _Resource("git", _physical_id(info), (), key)


def _covers(held: _Resource, wanted: _Resource) -> bool:
    if held.kind != wanted.kind:
        return False
    if held.kind == "git":
        return (
            held.physical_root == wanted.physical_root or held.canonical_key == wanted.canonical_key
        )
    return (
        held.physical_root == wanted.physical_root and _parts_under(wanted.relative, held.relative)
    ) or _key_under(wanted.canonical_key, held.canonical_key)


def _resources_conflict(left: _Resource, right: _Resource) -> bool:
    if left.kind != right.kind:
        return False
    if left.kind == "git":
        return (
            left.physical_root == right.physical_root or left.canonical_key == right.canonical_key
        )
    same_physical_tree = left.physical_root == right.physical_root and (
        _parts_under(left.relative, right.relative) or _parts_under(right.relative, left.relative)
    )
    same_named_tree = _key_under(left.canonical_key, right.canonical_key) or _key_under(
        right.canonical_key, left.canonical_key
    )
    return same_physical_tree or same_named_tree


def _sets_conflict(left: tuple[_Resource, ...], right: tuple[_Resource, ...]) -> bool:
    return any(_resources_conflict(a, b) for a in left for b in right)


def _parts_under(child: tuple[str, ...], parent: tuple[str, ...]) -> bool:
    return len(child) >= len(parent) and child[: len(parent)] == parent


def _key_under(child: str, parent: str) -> bool:
    return child == parent or child.startswith(parent.rstrip(os.sep) + os.sep)


def _physical_id(info: os.stat_result) -> str:
    return f"{info.st_dev}:{info.st_ino}"


def _is_reparse(info: os.stat_result) -> bool:
    return bool(
        stat.S_ISLNK(info.st_mode)
        or getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _revalidate_root(root: OrdinaryMutationRoot) -> None:
    try:
        resolved = root.path.resolve(strict=True)
        info = resolved.lstat()
    except OSError as exc:
        raise MutationError("stale_root") from exc
    if (
        root_key(resolved) != root.canonical_key
        or _physical_id(info) != root.physical_id
        or not stat.S_ISDIR(info.st_mode)
        or _is_reparse(info)
        or chain_has_reparse(resolved)
    ):
        raise MutationError("stale_root")
    if not _supported_volume(resolved):
        raise MutationError("unsupported_filesystem")


def _is_ntfs(path: Path) -> bool:
    """Handle identity, sharing modes and handle-relative renames are measured on NTFS only."""
    if sys.platform != "win32":
        return False
    volume = ctypes.create_unicode_buffer(_MAX_PATH_UNITS)
    filesystem = ctypes.create_unicode_buffer(_MAX_PATH + 1)
    return bool(
        _GetVolumePathNameW(str(path), volume, len(volume))
        and _GetVolumeInformationW(
            volume.value, None, 0, None, None, None, filesystem, len(filesystem)
        )
        and filesystem.value == "NTFS"
    )


@contextmanager
def _guard_parents(
    root: OrdinaryMutationRoot,
    source: tuple[str, ...],
    dest: tuple[str, ...],
) -> Iterator[tuple[tuple[Path, int], tuple[Path, int]]]:
    """Guard a move's two parents in `_Resource.order_key` order, and yield them source first."""
    if _path_resource(root, source).order_key <= _path_resource(root, dest).order_key:
        with _guard_parent(root, source) as first, _guard_parent(root, dest) as second:
            yield first, second
    else:
        with _guard_parent(root, dest) as second, _guard_parent(root, source) as first:
            yield first, second


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _validate_leaf(name: str) -> None:
    if (
        not isinstance(name, str)
        or not name
        or name in {".", ".."}
        or "\x00" in name
        or "/" in name
        or "\\" in name
        or ":" in name
    ):
        raise MutationError("invalid_leaf", stage_retained=True)


def _validate_reservation(reservation: PublicationReservation) -> None:
    try:
        operation_id = str(uuid.UUID(reservation.operation_id))
    except AttributeError, ValueError:
        raise MutationError("invalid_operation_id") from None
    if (
        operation_id != reservation.operation_id
        or reservation.stage_name != f"{STAGE_PREFIX}{operation_id}{STAGE_SUFFIX}"
        or reservation.backup_name != f"{BACKUP_PREFIX}{operation_id}{STAGE_SUFFIX}"
    ):
        raise MutationError("invalid_operation_id")


# Win32 file operations.  FILE_SHARE_READ without WRITE/DELETE is the external exclusion guard.
if sys.platform == "win32":
    from ctypes import wintypes

    # One binding of the final-path query: the leaf check below and the alias check
    # (`same_final_path`) read the same path with the same flags.
    from viki_agent.vaults.vault import (
        _FILE_NAME_NORMALIZED,
        _MAX_FINAL_PATH,
        _GetFinalPathNameByHandleW,
        same_final_path,
    )

    FileHandle = int  # a Win32 HANDLE value

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    def _fn(name: str, restype: Any, *argtypes: Any) -> Any:
        function = getattr(_kernel32, name)
        function.restype = restype
        function.argtypes = argtypes
        return function

    _CreateFileW = _fn(
        "CreateFileW",
        wintypes.HANDLE,
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    _CloseHandle = _fn("CloseHandle", wintypes.BOOL, wintypes.HANDLE)
    _FlushFileBuffers = _fn("FlushFileBuffers", wintypes.BOOL, wintypes.HANDLE)
    _SetFileInformationByHandle = _fn(
        "SetFileInformationByHandle",
        wintypes.BOOL,
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    )
    _ReadFile = _fn(
        "ReadFile",
        wintypes.BOOL,
        wintypes.HANDLE,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.LPDWORD,
        wintypes.LPVOID,
    )
    _WriteFile = _fn(
        "WriteFile",
        wintypes.BOOL,
        wintypes.HANDLE,
        wintypes.LPCVOID,
        wintypes.DWORD,
        wintypes.LPDWORD,
        wintypes.LPVOID,
    )
    _SetFilePointerEx = _fn(
        "SetFilePointerEx",
        wintypes.BOOL,
        wintypes.HANDLE,
        ctypes.c_longlong,
        ctypes.POINTER(ctypes.c_longlong),
        wintypes.DWORD,
    )
    _GetFileInformationByHandle = _fn(
        "GetFileInformationByHandle",
        wintypes.BOOL,
        wintypes.HANDLE,
        wintypes.LPVOID,
    )
    _GetFileInformationByHandleEx = _fn(
        "GetFileInformationByHandleEx",
        wintypes.BOOL,
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    )
    _GetVolumePathNameW = _fn(
        "GetVolumePathNameW", wintypes.BOOL, wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD
    )
    _GetVolumeInformationW = _fn(
        "GetVolumeInformationW",
        wintypes.BOOL,
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.LPDWORD,
        wintypes.LPDWORD,
        wintypes.LPDWORD,
        wintypes.LPWSTR,
        wintypes.DWORD,
    )

    _ntdll = ctypes.WinDLL("ntdll")

    def _nt_fn(name: str, restype: Any, *argtypes: Any) -> Any:
        function = getattr(_ntdll, name)
        function.restype = restype
        function.argtypes = argtypes
        return function

    _GENERIC_READ = 0x80000000
    _DELETE = 0x00010000
    _SYNCHRONIZE = 0x00100000
    _FILE_READ_DATA = 0x00000001
    _FILE_WRITE_DATA = 0x00000002
    _FILE_TRAVERSE = 0x00000020
    _FILE_READ_ATTRIBUTES = 0x00000080
    _FILE_SHARE_READ = 0x00000001
    _FILE_SHARE_WRITE = 0x00000002
    _FILE_SHARE_DELETE = 0x00000004
    _OPEN_EXISTING = 3
    _FILE_ATTRIBUTE_NORMAL = 0x00000080
    _FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
    _FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
    _INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
    _ERROR_FILE_NOT_FOUND = 2
    _ERROR_PATH_NOT_FOUND = 3
    _ERROR_SHARING_VIOLATION = 32
    _ERROR_FILE_EXISTS = 80
    _ERROR_ALREADY_EXISTS = 183
    _FILE_BEGIN = 0
    _FILE_RENAME_INFO_EX = 22
    _FILE_RENAME_REPLACE_IF_EXISTS = 0x1
    _FILE_RENAME_POSIX_SEMANTICS = 0x2
    _FILE_DISPOSITION_INFO_EX = 21
    _FILE_DISPOSITION_DELETE = 0x1
    _FILE_DISPOSITION_POSIX_SEMANTICS = 0x2
    _FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
    _FILE_ATTRIBUTE_DIRECTORY = 0x00000010
    _FILE_ATTRIBUTE_READONLY = 0x00000001
    _OBJ_CASE_INSENSITIVE = 0x00000040
    _OBJ_DONT_REPARSE = 0x00001000
    _FILE_OPEN = 0x00000001
    _FILE_CREATE = 0x00000002
    _FILE_DIRECTORY_FILE = 0x00000001
    _FILE_NON_DIRECTORY_FILE = 0x00000040
    _FILE_SYNCHRONOUS_IO_NONALERT = 0x00000020
    _FILE_OPEN_REPARSE_POINT = 0x00200000
    _FILE_RENAME_INFORMATION = 10
    _FILE_ID_INFO_CLASS = 18
    _MAX_PATH = 260
    _MAX_PATH_UNITS = 32_768

    class _UnicodeString(ctypes.Structure):
        _fields_ = [
            ("Length", wintypes.USHORT),
            ("MaximumLength", wintypes.USHORT),
            ("Buffer", wintypes.LPWSTR),
        ]

    class _ObjectAttributes(ctypes.Structure):
        _fields_ = [
            ("Length", wintypes.ULONG),
            ("RootDirectory", wintypes.HANDLE),
            ("ObjectName", ctypes.POINTER(_UnicodeString)),
            ("Attributes", wintypes.ULONG),
            ("SecurityDescriptor", wintypes.LPVOID),
            ("SecurityQualityOfService", wintypes.LPVOID),
        ]

    class _IoStatusBlock(ctypes.Structure):
        _fields_ = [("Status", wintypes.LPVOID), ("Information", ctypes.c_size_t)]

    class _ByHandleFileInformation(ctypes.Structure):
        _fields_ = [
            ("FileAttributes", wintypes.DWORD),
            ("CreationTime", wintypes.FILETIME),
            ("LastAccessTime", wintypes.FILETIME),
            ("LastWriteTime", wintypes.FILETIME),
            ("VolumeSerialNumber", wintypes.DWORD),
            ("FileSizeHigh", wintypes.DWORD),
            ("FileSizeLow", wintypes.DWORD),
            ("NumberOfLinks", wintypes.DWORD),
            ("FileIndexHigh", wintypes.DWORD),
            ("FileIndexLow", wintypes.DWORD),
        ]

    class _FileIdInfo(ctypes.Structure):
        _fields_ = [
            ("VolumeSerialNumber", ctypes.c_ulonglong),
            ("FileId", ctypes.c_ubyte * 16),
        ]

    class _NtFileRenameInformation(ctypes.Structure):
        _fields_ = [
            ("ReplaceIfExists", wintypes.BOOLEAN),
            ("RootDirectory", wintypes.HANDLE),
            ("FileNameLength", wintypes.ULONG),
            ("FileName", wintypes.WCHAR * 1),
        ]

    _NtCreateFile = _nt_fn(
        "NtCreateFile",
        ctypes.c_long,
        ctypes.POINTER(wintypes.HANDLE),
        wintypes.ULONG,
        ctypes.POINTER(_ObjectAttributes),
        ctypes.POINTER(_IoStatusBlock),
        wintypes.LPVOID,
        wintypes.ULONG,
        wintypes.ULONG,
        wintypes.ULONG,
        wintypes.ULONG,
        wintypes.LPVOID,
        wintypes.ULONG,
    )
    _NtSetInformationFile = _nt_fn(
        "NtSetInformationFile",
        ctypes.c_long,
        wintypes.HANDLE,
        ctypes.POINTER(_IoStatusBlock),
        wintypes.LPVOID,
        wintypes.ULONG,
        wintypes.ULONG,
    )
    _RtlNtStatusToDosError = _nt_fn("RtlNtStatusToDosError", wintypes.ULONG, ctypes.c_long)

    class _FileRenameInfo(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("RootDirectory", wintypes.HANDLE),
            ("FileNameLength", wintypes.DWORD),
            ("FileName", wintypes.WCHAR * 1),
        ]

    def _open_file(
        path: Path,
        access: int,
        *,
        directory: bool = False,
        share: int = _FILE_SHARE_READ,
    ) -> int:
        flags = _FILE_FLAG_OPEN_REPARSE_POINT
        if directory:
            flags |= _FILE_FLAG_BACKUP_SEMANTICS
        else:
            flags |= _FILE_ATTRIBUTE_NORMAL
        handle = _CreateFileW(str(path), access, share, None, _OPEN_EXISTING, flags, None)
        if handle == _INVALID_HANDLE_VALUE:
            error = ctypes.get_last_error()
            if error in {_ERROR_FILE_NOT_FOUND, _ERROR_PATH_NOT_FOUND}:
                raise MutationError("missing_stage" if not directory else "unsafe_parent")
            if error == _ERROR_SHARING_VIOLATION:
                raise MutationError("target_busy", stage_retained=True)
            raise ctypes.WinError(error, "CreateFileW")
        return int(handle)

    def _open_directory(path: Path) -> int:
        try:
            return _open_file(
                path,
                _FILE_TRAVERSE | _FILE_READ_ATTRIBUTES | _SYNCHRONIZE,
                directory=True,
                share=_FILE_SHARE_READ | _FILE_SHARE_WRITE,
            )
        except MutationError as exc:
            if exc.code == "target_busy":
                raise MutationError("parent_busy") from exc
            raise
        except OSError as exc:
            raise MutationError("unsafe_parent") from exc

    def _nt_error(status: int) -> int:
        return int(_RtlNtStatusToDosError(status))

    def _open_relative(
        parent_handle: int,
        name: str,
        access: int,
        share: int,
        *,
        directory: bool,
        missing_code: str,
        disposition: int = _FILE_OPEN,
    ) -> int:
        _validate_leaf(name)
        name_buffer = ctypes.create_unicode_buffer(name)
        encoded_length = len(name.encode("utf-16-le"))
        unicode_name = _UnicodeString(
            encoded_length,
            encoded_length + ctypes.sizeof(wintypes.WCHAR),
            ctypes.cast(name_buffer, wintypes.LPWSTR),
        )
        attributes = _ObjectAttributes(
            ctypes.sizeof(_ObjectAttributes),
            wintypes.HANDLE(parent_handle),
            ctypes.pointer(unicode_name),
            _OBJ_CASE_INSENSITIVE | _OBJ_DONT_REPARSE,
            None,
            None,
        )
        io_status = _IoStatusBlock()
        handle = wintypes.HANDLE()
        options = _FILE_SYNCHRONOUS_IO_NONALERT | _FILE_OPEN_REPARSE_POINT
        options |= _FILE_DIRECTORY_FILE if directory else _FILE_NON_DIRECTORY_FILE
        status = int(
            _NtCreateFile(
                ctypes.byref(handle),
                access,
                ctypes.byref(attributes),
                ctypes.byref(io_status),
                None,
                _FILE_ATTRIBUTE_NORMAL,
                share,
                disposition,
                options,
                None,
                0,
            )
        )
        if status < 0:
            error = _nt_error(status)
            if disposition == _FILE_CREATE and error in {_ERROR_FILE_EXISTS, _ERROR_ALREADY_EXISTS}:
                raise MutationError("stage_collision")
            if error in {_ERROR_FILE_NOT_FOUND, _ERROR_PATH_NOT_FOUND}:
                raise MutationError(missing_code, stage_retained=not directory)
            if error == _ERROR_SHARING_VIOLATION:
                raise MutationError(
                    "parent_busy" if directory else "target_busy", stage_retained=not directory
                )
            if directory:
                raise MutationError("unsafe_parent") from ctypes.WinError(error, "NtCreateFile")
            raise ctypes.WinError(error, "NtCreateFile")
        if handle.value is None:
            raise MutationError("native_handle_missing", stage_retained=not directory)
        return int(handle.value)

    def _create_relative_stage(parent_handle: int, name: str) -> int:
        return _open_relative(
            parent_handle,
            name,
            _FILE_READ_DATA | _FILE_WRITE_DATA | _FILE_READ_ATTRIBUTES | _DELETE | _SYNCHRONIZE,
            _FILE_SHARE_READ,
            directory=False,
            missing_code="stage_failed",
            disposition=_FILE_CREATE,
        )

    def _open_directory_relative(parent_handle: int, name: str, missing_code: str) -> int:
        handle: int | None = None
        try:
            handle = _open_relative(
                parent_handle,
                name,
                _FILE_TRAVERSE | _FILE_READ_ATTRIBUTES | _SYNCHRONIZE,
                _FILE_SHARE_READ | _FILE_SHARE_WRITE,
                directory=True,
                missing_code=missing_code,
            )
            info = _handle_info(handle)
            if not info.directory or info.reparse:
                raise MutationError("unsafe_parent")
            return handle
        except BaseException:
            if handle is not None:
                _CloseHandle(handle)
            raise

    def _folder_exists(parent_handle: int, name: str) -> bool:
        """Whether `name` is a directory under the handle; a file, link or busy entry refuses."""
        try:
            handle = _open_directory_relative(parent_handle, name, "missing_folder")
        except MutationError as exc:
            if exc.code == "missing_folder":
                return False
            raise
        _CloseHandle(handle)
        return True

    @dataclass(frozen=True)
    class _HandleInfo:
        fingerprint: FileFingerprint
        links: int
        directory: bool
        reparse: bool
        read_only: bool

    def _handle_info(handle: int) -> _HandleInfo:
        raw = _ByHandleFileInformation()
        if not _GetFileInformationByHandle(handle, ctypes.byref(raw)):
            raise ctypes.WinError(ctypes.get_last_error(), "GetFileInformationByHandle")
        file_id = _FileIdInfo()
        if not _GetFileInformationByHandleEx(
            handle,
            _FILE_ID_INFO_CLASS,
            ctypes.byref(file_id),
            ctypes.sizeof(file_id),
        ):
            raise ctypes.WinError(ctypes.get_last_error(), "GetFileInformationByHandleEx")
        # Match the repository's Python 3.14 ``os.stat`` identity on supported NTFS volumes:
        # st_dev is FILE_ID_INFO.VolumeSerialNumber while st_ino is the legacy 64-bit file index.
        file_index = (int(raw.FileIndexHigh) << 32) | int(raw.FileIndexLow)
        size = (int(raw.FileSizeHigh) << 32) | int(raw.FileSizeLow)
        return _HandleInfo(
            FileFingerprint(f"{int(file_id.VolumeSerialNumber)}:{file_index}", "", size),
            int(raw.NumberOfLinks),
            bool(raw.FileAttributes & _FILE_ATTRIBUTE_DIRECTORY),
            bool(raw.FileAttributes & _FILE_ATTRIBUTE_REPARSE_POINT),
            bool(raw.FileAttributes & _FILE_ATTRIBUTE_READONLY),
        )

    def _fingerprint_handle(
        handle: int, *, unsafe_code: str, max_bytes: int = MAX_PUBLICATION_BYTES
    ) -> FileFingerprint:
        if max_bytes > MAX_STAGE_BYTES:
            raise MutationError("invalid_content")
        info = _handle_info(handle)
        if (
            info.directory
            or info.reparse
            or info.links != 1
            or info.fingerprint.byte_size > max_bytes
        ):
            raise MutationError(unsafe_code, stage_retained=True)
        return FileFingerprint(
            info.fingerprint.physical_id,
            _hash_handle(handle, info.fingerprint.byte_size, max_bytes),
            info.fingerprint.byte_size,
        )

    def _open_verified_relative(
        parent_handle: int,
        name: str,
        expected: FileFingerprint,
        *,
        missing_code: str,
        delete: bool = True,
        max_bytes: int = MAX_PUBLICATION_BYTES,
    ) -> int:
        access = _FILE_READ_DATA | _FILE_READ_ATTRIBUTES | _SYNCHRONIZE
        share = _FILE_SHARE_READ
        if delete:
            access |= _DELETE
        else:
            share |= _FILE_SHARE_WRITE | _FILE_SHARE_DELETE
        try:
            handle = _open_relative(
                parent_handle,
                name,
                access,
                share,
                directory=False,
                missing_code=missing_code,
            )
        except MutationError as exc:
            if exc.code == "target_busy":
                raise MutationError("stage_busy", stage_retained=True) from exc
            raise
        try:
            actual = _fingerprint_handle(handle, unsafe_code="unsafe_target", max_bytes=max_bytes)
            if actual != expected:
                raise MutationError("stale_stage", stage_retained=True)
            return handle
        except BaseException:
            _CloseHandle(handle)
            raise

    def _open_verified_stage(
        parent_handle: int, staged: StagedPublication, *, max_bytes: int = MAX_PUBLICATION_BYTES
    ) -> int:
        try:
            handle = _open_relative(
                parent_handle,
                staged.stage_name,
                _FILE_READ_DATA | _FILE_WRITE_DATA | _FILE_READ_ATTRIBUTES | _DELETE | _SYNCHRONIZE,
                _FILE_SHARE_READ,
                directory=False,
                missing_code="missing_stage",
            )
        except MutationError as exc:
            if exc.code == "target_busy":
                raise MutationError("stage_busy", stage_retained=True) from exc
            raise
        try:
            actual = _fingerprint_handle(handle, unsafe_code="unsafe_stage", max_bytes=max_bytes)
            if actual != staged.fingerprint:
                raise MutationError("stale_stage", stage_retained=True)
            return handle
        except BaseException:
            _CloseHandle(handle)
            raise

    def _open_target(parent_handle: int, parent: Path, name: str) -> tuple[int, FileFingerprint]:
        try:
            handle = _open_relative(
                parent_handle,
                name,
                _FILE_READ_DATA | _FILE_READ_ATTRIBUTES | _DELETE | _SYNCHRONIZE,
                _FILE_SHARE_READ,
                directory=False,
                missing_code="missing_target",
            )
        except MutationError:
            raise
        try:
            # A short-name leaf opens another file than the one the name check saw.
            if not same_final_path(handle, str(parent / name)):
                raise MutationError("excluded_path", stage_retained=True)
            # Windows renames a read-only file: the replace would drop the user's protection and
            # leave a backup that cannot be deleted.
            if _handle_info(handle).read_only:
                raise MutationError("read_only_target", stage_retained=True)
            return handle, _fingerprint_handle(handle, unsafe_code="unsafe_target")
        except BaseException:
            _CloseHandle(handle)
            raise

    def _final_leaf(handle: int) -> str | None:
        """The leaf name NTFS holds for the handle's file, in its letter case on disk (the
        normalized final path), or None when the query fails."""
        buffer = ctypes.create_unicode_buffer(_MAX_FINAL_PATH)
        length = _GetFinalPathNameByHandleW(handle, buffer, _MAX_FINAL_PATH, _FILE_NAME_NORMALIZED)
        if not 0 < length < _MAX_FINAL_PATH:
            return None
        return ntpath.basename(buffer.value)

    def _write_handle(handle: int, content: bytes) -> None:
        offset = 0
        while offset < len(content):
            chunk = content[offset : offset + 64 * 1024]
            buffer = ctypes.create_string_buffer(chunk)
            written = wintypes.DWORD()
            if not _WriteFile(handle, buffer, len(chunk), ctypes.byref(written), None):
                raise ctypes.WinError(ctypes.get_last_error(), "WriteFile")
            if written.value == 0:
                raise MutationError("short_write", stage_retained=True)
            offset += written.value

    def _delete_handle(handle: int) -> None:
        disposition = wintypes.DWORD(_FILE_DISPOSITION_DELETE | _FILE_DISPOSITION_POSIX_SEMANTICS)
        if not _SetFileInformationByHandle(
            handle,
            _FILE_DISPOSITION_INFO_EX,
            ctypes.byref(disposition),
            ctypes.sizeof(disposition),
        ):
            raise ctypes.WinError(ctypes.get_last_error(), "delete by handle")

    def _rename_handle_relative_no_replace(
        handle: int, parent_handle: int, target_name: str
    ) -> None:
        _validate_leaf(target_name)
        encoded = target_name.encode("utf-16-le")
        size = max(
            ctypes.sizeof(_NtFileRenameInformation),
            _NtFileRenameInformation.FileName.offset + len(encoded),
        )
        buffer = ctypes.create_string_buffer(size)
        info = _NtFileRenameInformation.from_buffer(buffer)
        info.ReplaceIfExists = False
        info.RootDirectory = wintypes.HANDLE(parent_handle)
        info.FileNameLength = len(encoded)
        ctypes.memmove(
            ctypes.addressof(buffer) + _NtFileRenameInformation.FileName.offset,
            encoded,
            len(encoded),
        )
        io_status = _IoStatusBlock()
        status = int(
            _NtSetInformationFile(
                handle,
                ctypes.byref(io_status),
                buffer,
                size,
                _FILE_RENAME_INFORMATION,
            )
        )
        if status < 0:
            error = _nt_error(status)
            if error in {_ERROR_FILE_EXISTS, _ERROR_ALREADY_EXISTS}:
                raise MutationError("conflict", stage_retained=True)
            if error == _ERROR_SHARING_VIOLATION:
                raise MutationError("target_busy", stage_retained=True)
            raise ctypes.WinError(error, "NtSetInformationFile rename")

    def _read_handle(handle: int, size: int) -> bytes:
        """Read exactly `size` bytes of a verified handle into one buffer."""
        if size > MAX_PUBLICATION_BYTES:
            raise MutationError("file_too_large")
        position = ctypes.c_longlong()
        if not _SetFilePointerEx(handle, 0, ctypes.byref(position), _FILE_BEGIN):
            raise ctypes.WinError(ctypes.get_last_error(), "SetFilePointerEx")
        buffer = ctypes.create_string_buffer(size + 1)
        offset = 0
        while offset < size:
            chunk = (ctypes.c_char * min(size - offset, 64 * 1024)).from_buffer(buffer, offset)
            read = wintypes.DWORD()
            if not _ReadFile(handle, chunk, len(chunk), ctypes.byref(read), None):
                raise ctypes.WinError(ctypes.get_last_error(), "ReadFile")
            if read.value == 0:
                raise MutationError("short_read")
            offset += read.value
        return buffer.raw[:size]

    def _hash_handle(handle: int, size: int, max_bytes: int = MAX_PUBLICATION_BYTES) -> str:
        if size > max_bytes:
            raise MutationError("file_too_large", stage_retained=True)
        position = ctypes.c_longlong()
        if not _SetFilePointerEx(handle, 0, ctypes.byref(position), _FILE_BEGIN):
            raise ctypes.WinError(ctypes.get_last_error(), "SetFilePointerEx")
        digest = hashlib.sha256()
        remaining = size
        while remaining:
            amount = min(remaining, 64 * 1024)
            buffer = ctypes.create_string_buffer(amount)
            read = wintypes.DWORD()
            if not _ReadFile(handle, buffer, amount, ctypes.byref(read), None):
                raise ctypes.WinError(ctypes.get_last_error(), "ReadFile")
            if read.value == 0:
                raise MutationError("short_read", stage_retained=True)
            digest.update(buffer.raw[: read.value])
            remaining -= read.value
        if not _SetFilePointerEx(handle, 0, ctypes.byref(position), _FILE_BEGIN):
            raise ctypes.WinError(ctypes.get_last_error(), "SetFilePointerEx")
        return digest.hexdigest()

    def _rename_handle(handle: int, target: Path, *, replace: bool) -> None:
        encoded = str(target).encode("utf-16-le")
        # Microsoft examples allocate sizeof(FILE_RENAME_INFO) plus the variable name.  The name
        # begins at its field offset; retaining the structure's tail padding is required on x64.
        size = ctypes.sizeof(_FileRenameInfo) + len(encoded)
        buffer = ctypes.create_string_buffer(size)
        info = _FileRenameInfo.from_buffer(buffer)
        info.Flags = _FILE_RENAME_REPLACE_IF_EXISTS | _FILE_RENAME_POSIX_SEMANTICS if replace else 0
        info.RootDirectory = None
        info.FileNameLength = len(encoded)
        ctypes.memmove(
            ctypes.addressof(buffer) + _FileRenameInfo.FileName.offset, encoded, len(encoded)
        )
        if not _SetFileInformationByHandle(
            handle, _FILE_RENAME_INFO_EX, buffer, ctypes.sizeof(buffer)
        ):
            raise ctypes.WinError(ctypes.get_last_error(), "SetFileInformationByHandle rename")

    @contextmanager
    def _guard_parent(
        root: OrdinaryMutationRoot,
        parent_parts: tuple[str, ...],
        *,
        missing_code: str = "unsafe_parent",
    ) -> Iterator[tuple[Path, int]]:
        handles: list[int] = []
        try:
            current = root.path
            handles.append(_open_directory(current))
            _revalidate_root(root)
            root_info = _handle_info(handles[0])
            if (
                root_info.fingerprint.physical_id != root.physical_id
                or not root_info.directory
                or root_info.reparse
            ):
                raise MutationError("stale_root")
            for part in parent_parts:
                _validate_leaf(part)
                current /= part
                handles.append(_open_directory_relative(handles[-1], part, missing_code))
            # A short name opens another directory than the one the name check saw.
            if not same_final_path(handles[-1], str(current)):
                raise MutationError("excluded_path")
            yield current, handles[-1]
        finally:
            for handle in reversed(handles):
                _CloseHandle(handle)

    def _open_probe(parent_handle: int, name: str) -> FileHandle:
        return _open_relative(
            parent_handle,
            name,
            _FILE_READ_DATA | _FILE_READ_ATTRIBUTES | _DELETE | _SYNCHRONIZE,
            _FILE_SHARE_READ,
            directory=False,
            missing_code="missing_probe",
        )

    def _open_attachment(parent_handle: int, name: str) -> FileHandle:
        try:
            return _open_relative(
                parent_handle,
                name,
                _FILE_READ_DATA | _FILE_READ_ATTRIBUTES | _SYNCHRONIZE,
                _FILE_SHARE_READ,
                directory=False,
                missing_code="missing_attachment",
            )
        except OSError as exc:
            # A folder at the name: the non-directory open refuses it natively.
            raise MutationError("unsafe_target") from exc

    def _flush(handle: FileHandle) -> None:
        if not _FlushFileBuffers(handle):
            raise ctypes.WinError(ctypes.get_last_error(), "FlushFileBuffers")

    def _carry_mode(stage: FileHandle, target: FileHandle) -> None:
        """No-op: NTFS has no mode bits to carry."""

    def _mkdir_relative(parent_handle: int, parent: Path, name: str) -> None:
        os.mkdir(parent / name)

    def _supported_volume(path: Path) -> bool:
        # `_is_ntfs` is looked up per call, so a test that patches it still decides.
        return _is_ntfs(path)

else:
    # Every open is relative to a held directory descriptor and follows no symlink, so a link
    # swapped in cannot redirect it; a FIFO or a device opens without blocking and is refused by
    # its fstat before a byte is read. Directory handles are plain descriptors. A rename or an
    # unlink takes a name, not a descriptor, so each first checks that the name still holds the
    # held file; every namespace change is flushed in its folders before its step returns.
    _DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    _FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_NOCTTY | os.O_CLOEXEC
    # With O_CREAT and O_EXCL an existing name, a symlink included, refuses: nothing is followed.
    _STAGE_FLAGS = os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    # A stage is created 0600. `prepare_replace` gives it the note's mode, and `publish_create`
    # the mode a plain create gives: 0666 less the umask. Reading the umask sets it, so it is
    # read once, at import, under a private value, and put back.
    _umask = os.umask(0o077)
    os.umask(_umask)
    _CREATE_MODE = 0o666 & ~_umask
    # What a file open answers for an entry that is no note: a symlink (ELOOP), a parent that
    # is no folder (ENOTDIR), a socket (ENXIO on Linux, EOPNOTSUPP on macOS) or a device with
    # nothing behind it (ENXIO).
    _UNSAFE_OPEN = frozenset({errno.ELOOP, errno.ENOTDIR, errno.ENXIO, errno.EOPNOTSUPP})
    # The local types whose kernels document the no-replace rename flag.
    _SUPPORTED_TYPES = {
        "linux": frozenset({"ext4", "xfs", "btrfs", "tmpfs"}),
        "darwin": frozenset({"apfs"}),
    }
    # chflags(2) locks a replace would drop: immutable or append-only, by the user or the system.
    _LOCKED_FLAGS = stat.UF_IMMUTABLE | stat.SF_IMMUTABLE | stat.UF_APPEND | stat.SF_APPEND

    @dataclass
    class FileHandle:
        """An open regular file and the entry it was opened at: `name` under `dir_fd`, the
        guarded parent's descriptor, which this handle does not own. `fingerprint` is the last
        one `_fingerprint_handle` took through it."""

        fd: int
        dir_fd: int
        name: str
        fingerprint: FileFingerprint | None = None

    def _CloseHandle(handle: FileHandle | int) -> None:
        os.close(handle.fd if isinstance(handle, FileHandle) else handle)

    def same_final_path(handle: FileHandle | int, expected: str) -> bool:
        """Whether the path the kernel holds for the descriptor is `expected`, compared by root
        key: macOS folds case and normalization, Linux compares exactly. A failed query (no
        `/proc` on Linux) answers False, so the caller refuses."""
        fd = handle.fd if isinstance(handle, FileHandle) else handle
        try:
            return root_key(osfs.final_path(fd)) == root_key(expected)
        except OSError:
            return False

    def _open_file_at(
        parent_fd: int, name: str, *, missing_code: str, unsafe_code: str
    ) -> FileHandle:
        _validate_leaf(name)
        try:
            fd = os.open(name, _FILE_FLAGS, dir_fd=parent_fd)
        except FileNotFoundError as exc:
            raise MutationError(missing_code, stage_retained=True) from exc
        except OSError as exc:
            if exc.errno in _UNSAFE_OPEN:
                raise MutationError(unsafe_code, stage_retained=True) from exc
            raise
        try:
            info = os.fstat(fd)
            # A folder, FIFO or device is no note, and another device than the parent's is a
            # mount crossing. A hard link refuses here, before any final-path check, as the
            # reader's open does: on macOS its final path may name another link.
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_dev != os.fstat(parent_fd).st_dev
            ):
                raise MutationError(unsafe_code, stage_retained=True)
        except BaseException:
            os.close(fd)
            raise
        return FileHandle(fd, parent_fd, name)

    def _open_directory_at(parent_fd: int, name: str, missing_code: str) -> int:
        _validate_leaf(name)
        try:
            fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
        except FileNotFoundError as exc:
            raise MutationError(missing_code) from exc
        except OSError as exc:
            # A link (ELOOP), a file (ENOTDIR) or a folder this account cannot open.
            raise MutationError("unsafe_parent") from exc
        try:
            if os.fstat(fd).st_dev != os.fstat(parent_fd).st_dev:
                raise MutationError("unsafe_parent")  # a mount point
        except BaseException:
            os.close(fd)
            raise
        return fd

    def _folder_exists(parent_handle: int, name: str) -> bool:
        """Whether `name` is a directory under the descriptor; a file or link refuses."""
        try:
            fd = _open_directory_at(parent_handle, name, "missing_folder")
        except MutationError as exc:
            if exc.code == "missing_folder":
                return False
            raise
        os.close(fd)
        return True

    def _fingerprint_handle(
        handle: FileHandle, *, unsafe_code: str, max_bytes: int = MAX_PUBLICATION_BYTES
    ) -> FileFingerprint:
        if max_bytes > MAX_STAGE_BYTES:
            raise MutationError("invalid_content")
        info = os.fstat(handle.fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > max_bytes:
            raise MutationError(unsafe_code, stage_retained=True)
        handle.fingerprint = FileFingerprint(
            _physical_id(info), _hash_handle(handle, info.st_size, max_bytes), info.st_size
        )
        return handle.fingerprint

    def _open_verified_relative(
        parent_handle: int,
        name: str,
        expected: FileFingerprint,
        *,
        missing_code: str,
        delete: bool = True,
        max_bytes: int = MAX_PUBLICATION_BYTES,
    ) -> FileHandle:
        # `delete` picks a Win32 sharing mode; a POSIX descriptor has none.
        handle = _open_file_at(
            parent_handle, name, missing_code=missing_code, unsafe_code="unsafe_target"
        )
        try:
            actual = _fingerprint_handle(handle, unsafe_code="unsafe_target", max_bytes=max_bytes)
            if actual != expected:
                raise MutationError("stale_stage", stage_retained=True)
            return handle
        except BaseException:
            _CloseHandle(handle)
            raise

    def _open_verified_stage(
        parent_handle: int, staged: StagedPublication, *, max_bytes: int = MAX_PUBLICATION_BYTES
    ) -> FileHandle:
        handle = _open_file_at(
            parent_handle,
            staged.stage_name,
            missing_code="missing_stage",
            unsafe_code="unsafe_stage",
        )
        try:
            actual = _fingerprint_handle(handle, unsafe_code="unsafe_stage", max_bytes=max_bytes)
            if actual != staged.fingerprint:
                raise MutationError("stale_stage", stage_retained=True)
            return handle
        except BaseException:
            _CloseHandle(handle)
            raise

    def _open_target(
        parent_handle: int, parent: Path, name: str
    ) -> tuple[FileHandle, FileFingerprint]:
        handle = _open_file_at(
            parent_handle, name, missing_code="missing_target", unsafe_code="unsafe_target"
        )
        try:
            if not same_final_path(handle, str(parent / name)):
                raise MutationError("excluded_path", stage_retained=True)
            # A replace swaps in a new file: another user's note, a mode without owner write or
            # a macOS lock would lose its protection, so they refuse.
            info = os.fstat(handle.fd)
            if (
                info.st_uid != os.geteuid()
                or not info.st_mode & stat.S_IWUSR
                or (sys.platform == "darwin" and info.st_flags & _LOCKED_FLAGS)
            ):
                raise MutationError("read_only_target", stage_retained=True)
            return handle, _fingerprint_handle(handle, unsafe_code="unsafe_target")
        except BaseException:
            _CloseHandle(handle)
            raise

    def _final_leaf(handle: FileHandle) -> str | None:
        """The leaf name the kernel holds for the handle's file (macOS: its spelling on disk),
        or None when the query fails (no `/proc` on Linux)."""
        try:
            return os.path.basename(osfs.final_path(handle.fd))
        except OSError:
            return None

    # The most entries `_listed_as` reads: a vault root is one folder that may hold many notes,
    # and the read runs under the save's lease, so a larger folder refuses instead of scanning
    # without bound. Reading the folder whole, not stopping at a match, keeps the answer
    # independent of the listing's order.
    _MAX_LISTED_ENTRIES = 100_000

    def _listed_as(parent_fd: int, handle: FileHandle, name: str) -> bool:
        """Whether an entry of the folder `parent_fd` named exactly `name` holds the handle's
        file. Each entry's name and inode come from the listing, with no lookup by name, so a
        case-insensitive folder that opened `Agents.md` for `AGENTS.md` answers False whatever
        `/proc/self/fd` reports. A failed read or a folder over `_MAX_LISTED_ENTRIES` entries
        answers False too."""
        try:
            info = os.fstat(handle.fd)
            if info.st_dev != os.fstat(parent_fd).st_dev:
                return False
            found = False
            # scandir reads through its own duplicate of the descriptor and closes only that.
            with os.scandir(parent_fd) as entries:
                for count, entry in enumerate(entries, 1):
                    if count > _MAX_LISTED_ENTRIES:
                        return False
                    if entry.name == name and entry.inode() == info.st_ino:
                        found = True
            return found
        except OSError, NotImplementedError, TypeError:
            # The last two: `os.scandir(fd)` where it is not in `os.supports_fd`.
            return False

    def _create_relative_stage(parent_handle: int, name: str) -> FileHandle:
        _validate_leaf(name)
        try:
            fd = os.open(name, _STAGE_FLAGS, 0o600, dir_fd=parent_handle)
        except FileExistsError as exc:
            raise MutationError("stage_collision") from exc
        return FileHandle(fd, parent_handle, name)

    def _write_handle(handle: FileHandle, content: bytes) -> None:
        offset = 0
        while offset < len(content):
            written = os.write(handle.fd, content[offset : offset + 64 * 1024])
            if written == 0:
                raise MutationError("short_write", stage_retained=True)
            offset += written

    def _entry_id(dir_fd: int, name: str) -> str | None:
        """The physical id of the entry `name` under `dir_fd`, a link's own; None when missing."""
        try:
            return _physical_id(os.stat(name, dir_fd=dir_fd, follow_symlinks=False))
        except FileNotFoundError:
            return None

    def _holds(dir_fd: int, name: str, fd: int) -> bool:
        """Whether the entry `name` under `dir_fd` is the file `fd` holds; a missing one is not."""
        return _entry_id(dir_fd, name) == _physical_id(os.fstat(fd))

    def _sync_folders(first: int, second: int) -> None:
        """Flush a namespace change in both folders it touched, once when they are one."""
        os.fsync(first)
        if _physical_id(os.fstat(first)) != _physical_id(os.fstat(second)):
            os.fsync(second)

    def _rename_entry(fd: int, source_dir: int, source: str, dest_dir: int, dest: str) -> None:
        """One no-replace rename of the entry `source`, whose file `fd` holds, refused typed."""
        try:
            osfs.rename_at_no_replace(source_dir, source, dest_dir, dest)
        except FileExistsError:
            # APFS may report a case-only rename's new spelling as taken: by the entry itself,
            # which is then the one name a plain rename may replace.
            if not (
                sys.platform == "darwin"
                and _physical_id(os.fstat(source_dir)) == _physical_id(os.fstat(dest_dir))
                and root_key(source) == root_key(dest)
                and _holds(dest_dir, dest, fd)
            ):
                raise MutationError("conflict", stage_retained=True) from None
            os.rename(source, dest, src_dir_fd=source_dir, dst_dir_fd=dest_dir)
        except PermissionError as exc:
            # A folder without write permission, or a Linux immutable or append-only flag.
            raise MutationError("read_only_target", stage_retained=True) from exc
        except OSError as exc:
            if exc.errno == errno.ENOTSUP:
                raise MutationError("unsupported_filesystem", stage_retained=True) from exc
            raise

    def _rename_handle_relative_no_replace(
        handle: FileHandle, parent_handle: int, target_name: str
    ) -> None:
        """Move the handle's entry to `target_name` under `parent_handle`, never replacing one.

        No sharing mode keeps another program away, so the entry must hold the handle's file
        before the rename, and the new name its identity and every byte of its fingerprint
        after; otherwise the step answers `conflict`. A mismatch is moved back, so nothing
        moved, except when the move left a private stage or backup name and another file now
        holds the new name: another program saved there just after the rename, replacing ours,
        and moving its file would hide that save under the private name, so it stays. An undo
        that fails answers `unsafe_target` and leaves each file where it is.
        """
        _validate_leaf(target_name)
        expected = handle.fingerprint
        assert expected is not None, "only a fingerprinted handle can prove its move"
        source_dir, source = handle.dir_fd, handle.name
        if not _holds(source_dir, source, handle.fd):
            raise MutationError("conflict", stage_retained=True)
        _rename_entry(handle.fd, source_dir, source, parent_handle, target_name)
        replaced = False  # the new name seen holding another file than ours
        try:
            found = _entry_id(parent_handle, target_name)
            if found == _physical_id(os.fstat(handle.fd)):
                moved = expected == _fingerprint_handle(
                    handle, unsafe_code="conflict", max_bytes=MAX_STAGE_BYTES
                )
            else:
                moved, replaced = False, found is not None
        except MutationError, OSError:
            # Unknown, so the move back below decides.
            moved = False
        if replaced and source.startswith((STAGE_PREFIX, BACKUP_PREFIX)):
            _sync_folders(parent_handle, source_dir)
            raise MutationError("conflict", stage_retained=True)
        if not moved:
            try:
                _rename_entry(handle.fd, parent_handle, target_name, source_dir, source)
            except MutationError, OSError:
                logger.warning("vault rename undo failed")  # path-free, as every refusal is
                code = "unsafe_target"
            else:
                code = "conflict"
            _sync_folders(parent_handle, source_dir)
            raise MutationError(code, stage_retained=True)
        handle.dir_fd, handle.name = parent_handle, target_name
        _sync_folders(parent_handle, source_dir)

    def _delete_handle(handle: FileHandle) -> None:
        """Unlink the handle's entry. A note first moves to a private tombstone name under the
        rename rule, so the unlink names a file no other program saves to."""
        if not handle.name.startswith((STAGE_PREFIX, BACKUP_PREFIX)):
            tombstone = f"{BACKUP_PREFIX}{uuid.uuid4()}{STAGE_SUFFIX}"
            _rename_handle_relative_no_replace(handle, handle.dir_fd, tombstone)
        if not _holds(handle.dir_fd, handle.name, handle.fd):
            raise MutationError("stale_stage", stage_retained=True)
        os.unlink(handle.name, dir_fd=handle.dir_fd)
        os.fsync(handle.dir_fd)

    def _read_handle(handle: FileHandle, size: int) -> bytes:
        """Read exactly `size` bytes of a verified handle into one buffer."""
        if size > MAX_PUBLICATION_BYTES:
            raise MutationError("file_too_large")
        data = bytearray()
        while len(data) < size:
            chunk = os.pread(handle.fd, min(size - len(data), 64 * 1024), len(data))
            if not chunk:
                raise MutationError("short_read")
            data += chunk
        return bytes(data)

    def _hash_handle(handle: FileHandle, size: int, max_bytes: int = MAX_PUBLICATION_BYTES) -> str:
        if size > max_bytes:
            raise MutationError("file_too_large", stage_retained=True)
        digest = hashlib.sha256()
        offset = 0
        while offset < size:
            chunk = os.pread(handle.fd, min(size - offset, 64 * 1024), offset)
            if not chunk:
                raise MutationError("short_read", stage_retained=True)
            digest.update(chunk)
            offset += len(chunk)
        return digest.hexdigest()

    @contextmanager
    def _guard_parent(
        root: OrdinaryMutationRoot,
        parent_parts: tuple[str, ...],
        *,
        missing_code: str = "unsafe_parent",
    ) -> Iterator[tuple[Path, int]]:
        handles: list[int] = []
        try:
            current = root.path
            try:
                handles.append(os.open(current, _DIRECTORY_FLAGS))
            except OSError as exc:
                raise MutationError("unsafe_parent") from exc
            _revalidate_root(root)
            root_info = os.fstat(handles[0])
            if _physical_id(root_info) != root.physical_id or not stat.S_ISDIR(root_info.st_mode):
                raise MutationError("stale_root")
            for part in parent_parts:
                _validate_leaf(part)
                current /= part
                handles.append(_open_directory_at(handles[-1], part, missing_code))
                # macOS opens `.CLAUDE` (or another case or normalization spelling) as `.claude`,
                # so the name on disk is judged too, as `vault._refuse_alias` does.
                if sys.platform == "darwin" and excluded_part(
                    os.path.basename(osfs.final_path(handles[-1])), write=True
                ):
                    raise MutationError("excluded_path")
            if not same_final_path(handles[-1], str(current)):
                raise MutationError("excluded_path")
            yield current, handles[-1]
        finally:
            for handle in reversed(handles):
                os.close(handle)

    def _open_probe(parent_handle: int, name: str) -> FileHandle:
        return _open_file_at(
            parent_handle, name, missing_code="missing_probe", unsafe_code="unsafe_probe"
        )

    def _open_attachment(parent_handle: int, name: str) -> FileHandle:
        # A folder at the name is no regular file, so the open refuses it `unsafe_target`.
        return _open_file_at(
            parent_handle, name, missing_code="missing_attachment", unsafe_code="unsafe_target"
        )

    def _flush(handle: FileHandle) -> None:
        os.fsync(handle.fd)

    def _carry_mode(stage: FileHandle, target: FileHandle) -> None:
        """Give the stage the note's permission bits and group, so the replace keeps them.
        The stage may be in another group (macOS gives a new file its folder's): when this
        account may not give it the note's, the stage keeps no group access instead, so a
        replace does not open a note to another group."""
        note = os.fstat(target.fd)
        mode = stat.S_IMODE(note.st_mode) & 0o777
        if os.fstat(stage.fd).st_gid != note.st_gid:
            try:
                os.fchown(stage.fd, -1, note.st_gid)
            except PermissionError:
                mode &= ~0o070
        os.fchmod(stage.fd, mode)
        os.fsync(stage.fd)

    def _mkdir_relative(parent_handle: int, parent: Path, name: str) -> None:
        os.mkdir(name, dir_fd=parent_handle)
        os.fsync(parent_handle)

    def _supported_volume(path: Path) -> bool:
        try:
            return osfs.filesystem_type(str(path)) in _SUPPORTED_TYPES.get(sys.platform, ())
        except OSError:
            return False
