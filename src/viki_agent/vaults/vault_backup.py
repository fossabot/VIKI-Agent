"""A verified copy of a vault's notes into a folder outside every registered root.

Hard rule 1's write path is not involved: a backup reads the vault open-read-close through
`osfs.read_regular` and writes only under the destination the user names, which is checked against
`exports.denied_destination` (the registered roots, the app data directory and every provider home
are in that deny set), so no vault byte is written here.

Scope is `notes` and nothing else: `all` would copy `.env*` and the reader's `PRIVATE_NAMES`,
against the project rule never to read secrets or credentials. There is no scope argument.
"""

from __future__ import annotations

import hashlib
import json
import ntpath
import os
import stat
import time
import unicodedata
from collections.abc import Callable, Iterable, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from anyio.to_thread import run_sync

from viki_agent import osfs
from viki_agent.app.exports import denied_destination, is_device_or_unc
from viki_agent.domain import Workspace
from viki_agent.vaults.vault import (
    BACKUP_PREFIX,
    MAX_NOTE_BYTES,
    MAX_VISITED_ITEMS,
    NOTE_SUFFIXES,
    STAGE_PREFIX,
    WRITE_ONLY_NAMES,
    VaultError,
    _is_reparse,
    _unsafe_name,
    excluded_part,
    safe_ref,
)
from viki_agent.vaults.vault_mutation import MutationError
from viki_agent.workspaces import WorkspaceError, WorkspaceService

MANIFEST_NAME = "viki-backup.json"
BACKUP_FORMAT = "viki-backup"
BACKUP_VERSION = 1
BACKUP_SCOPE = "notes"
PARTIAL_SUFFIX = ".partial"
# What keeps one backup a bounded request instead of a job row.
# The ceiling is a bounded request; a job row is the upgrade path if a real vault
# exceeds it.
MAX_BACKUP_FILES = 2_000
MAX_BACKUP_BYTES = 256 * 1024 * 1024
# The exact serialized manifest must fit the restore reader, including every skipped row.
MAX_MANIFEST_BYTES = 8 * 1024 * 1024
MAX_RECEIPTS = 16
# The workspace policies, least strict first.
_STRICTNESS = ("ordinary", "existing_readonly", "protected")

# The server passes the vault's mutation lease with its deadline for a vault a save can reach, so a
# copy never races the publication service; a vault no app write can reach needs no lease.
VaultLease = Callable[[Workspace], AbstractAsyncContextManager[object]]


class BackupError(RuntimeError):
    """A typed, path-free backup failure safe to map at an API boundary."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class BackupSkip:
    ref: str
    reason: str  # reparse_point | unreadable | unsafe_name | not_regular | too_large


@dataclass(frozen=True)
class BackupPreview:
    files: int
    bytes: int
    instruction_files: tuple[str, ...]
    skipped: tuple[BackupSkip, ...]
    local_recovery: bool  # the destination is on the vault's own disk


@dataclass(frozen=True)
class BackupResult:
    folder: str  # the final folder name under the destination the caller named
    files: int
    bytes: int
    skipped: tuple[BackupSkip, ...]


@dataclass(frozen=True)
class _Plan:
    refs: tuple[str, ...]
    skipped: tuple[BackupSkip, ...]
    bytes: int


def _iso(epoch: float) -> str:
    return (
        datetime.fromtimestamp(epoch, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    )


def _stamp(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).strftime("%Y%m%d-%H%M%S")


def _read_digest(path: Path) -> tuple[int, str]:
    """The bytes and SHA-256 of a file just written, read back from the destination."""
    data = osfs.read_regular(path, MAX_NOTE_BYTES + 1, follow_symlinks=False)
    return len(data), hashlib.sha256(data).hexdigest()


class VaultBackupService:
    """Preview and write a verified `notes` copy of one vault. Writes no vault byte."""

    def __init__(
        self,
        workspaces: WorkspaceService,
        *,
        lease: VaultLease,
        denied_roots: Callable[[], Sequence[Path]],
        app_version: str,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._workspaces = workspaces
        self._lease = lease
        self._denied_roots = denied_roots
        self._app_version = app_version
        self._now = now
        # (result) by command_id, so a repeated request answers with the copy the first one made
        # instead of writing a second one. Touched only on the event loop.
        self._receipts: dict[str, BackupResult] = {}

    async def preview(self, workspace_id: str, destination: str) -> BackupPreview:
        """Count what a `notes` backup would copy. Reads the vault, writes nothing."""
        workspace = self._workspace(workspace_id)
        root = self._root(workspace)
        target = self._destination(destination)
        plan = await run_sync(lambda: self._plan(root))
        return BackupPreview(
            files=len(plan.refs),
            bytes=plan.bytes,
            instruction_files=tuple(
                ref for ref in plan.refs if PurePosixPath(ref).name.casefold() in WRITE_ONLY_NAMES
            ),
            skipped=plan.skipped,
            local_recovery=_same_device(root, target),
        )

    async def run(
        self, command_id: str, workspace_id: str, destination: str
    ) -> tuple[BackupResult, bool]:
        """Copy every note into a new folder under `destination`, verified from the destination."""
        if not isinstance(command_id, str) or not command_id:
            raise BackupError("invalid_request")
        replayed = self._receipts.get(command_id)
        if replayed is not None:
            return replayed, True
        workspace = self._workspace(workspace_id)
        root = self._root(workspace)
        target = self._destination(destination)
        try:
            async with self._lease(workspace):
                result = await run_sync(lambda: self._write(workspace, root, target))
        except MutationError as exc:
            raise BackupError(exc.code) from exc  # a deadline that passed: `vault_locked`
        if len(self._receipts) >= MAX_RECEIPTS:
            del self._receipts[next(iter(self._receipts))]  # insertion order: the oldest
        self._receipts[command_id] = result
        return result, False

    def _workspace(self, workspace_id: str) -> Workspace:
        try:
            return self._workspaces.get(workspace_id)
        except WorkspaceError as exc:
            raise BackupError("unknown_workspace") from exc

    def _root(self, workspace: Workspace) -> Path:
        root = self._workspaces.current_root(workspace)
        if root is None:
            raise BackupError("root_unavailable")
        return Path(root.display)

    def _destination(self, destination: str) -> Path:
        """`exports.py`'s own destination rule: a plain drive path outside the deny set."""
        if not isinstance(destination, str) or not destination:
            raise BackupError("invalid_path")
        if is_device_or_unc(destination):
            raise BackupError("invalid_path")
        folder = Path(destination)
        try:
            resolved = folder.resolve()
            is_dir = folder.is_dir()
        except OSError:
            raise BackupError("invalid_path") from None
        if is_device_or_unc(str(resolved)) or not is_dir:
            raise BackupError("invalid_path")
        if denied_destination(resolved, self._denied_roots()):
            raise BackupError("denied_destination")
        return resolved

    def _plan(self, root: Path) -> _Plan:
        """Walk the vault for note-suffix files outside dot-directories, following no reparse
        point and reading no file. Refuses over the bound before anything is written."""
        sizes: dict[str, int] = {}
        skipped: list[BackupSkip] = []
        total = 0
        visited = 0
        pending: list[tuple[Path, tuple[str, ...]]] = [(root, ())]
        while pending:
            folder, parts = pending.pop()
            try:
                listing = os.scandir(folder)
            except OSError as exc:
                if not parts:
                    raise BackupError("root_unavailable") from exc
                skipped.append(BackupSkip(_ref(parts), "unreadable"))
                continue
            with listing:
                for item in listing:
                    visited += 1
                    if visited > MAX_VISITED_ITEMS:
                        raise BackupError("backup_too_large")
                    child = (*parts, item.name)
                    if _unsafe_name(item.name):
                        # A control code point or a lone surrogate: the row names the folder,
                        # because the entry's own name can never reach a caller.
                        skipped.append(BackupSkip(_ref(parts), "unsafe_name"))
                        continue
                    if excluded_part(item.name, write=False):
                        continue  # the reader's private set and every dot-directory it names
                    try:
                        info = (folder / item.name).lstat()
                    except OSError:
                        skipped.append(BackupSkip(_ref(child), "unreadable"))
                        continue
                    if _is_reparse(info):
                        skipped.append(BackupSkip(_ref(child), "reparse_point"))
                        continue
                    if stat.S_ISDIR(info.st_mode):
                        if not item.name.startswith("."):
                            pending.append((folder / item.name, child))
                        continue
                    if not _in_scope(item.name):
                        continue
                    if not stat.S_ISREG(info.st_mode):
                        skipped.append(BackupSkip(_ref(child), "not_regular"))
                        continue
                    ref = _ref(child)
                    try:
                        manifest_parts(ref)
                    except VaultError:
                        skipped.append(BackupSkip(ref, "unsafe_name"))
                        continue
                    if info.st_size > MAX_NOTE_BYTES:
                        skipped.append(BackupSkip(ref, "too_large"))
                        continue
                    sizes[ref] = info.st_size
                    total += info.st_size
                    if len(sizes) > MAX_BACKUP_FILES or total > MAX_BACKUP_BYTES:
                        raise BackupError("backup_too_large")
        refs = _one_per_fold(sizes, skipped)
        return _Plan(tuple(refs), tuple(skipped), sum(sizes[ref] for ref in refs))

    def _write(self, workspace: Workspace, root: Path, destination: Path) -> BackupResult:
        # Recheck after the source lease wait and keep every registration write out until the
        # final rename. The guard is acquired in this worker, never on the event loop.
        with self._workspaces.registration_lock:
            destination = self._destination(str(destination))
            plan = self._plan(root)
            name = f"{root.name or 'vault'} backup {_stamp(self._now())}"
            partial = destination / (name + PARTIAL_SUFFIX)
            try:
                partial.mkdir()
            except FileExistsError:
                raise BackupError("conflict") from None
            except OSError:
                raise BackupError("unavailable") from None
            skipped = list(plan.skipped)
            files: list[dict[str, Any]] = []
            total = 0
            for ref in plan.refs:
                copied = self._copy(ref, root / PurePosixPath(ref), partial / PurePosixPath(ref))
                if isinstance(copied, BackupSkip):
                    skipped.append(copied)
                    continue
                size, digest = copied
                total += size
                if total > MAX_BACKUP_BYTES:
                    raise BackupError("backup_too_large")
                files.append({"path": ref, "bytes": size, "sha256": digest})
            # Binding can only narrow: a vault made stricter during the lease wait is
            # labelled with the stricter of this reading and the one taken at the start.
            policy = max(
                workspace.policy,
                self._workspace(workspace.workspace_id).policy,
                key=_STRICTNESS.index,
            )
            manifest = {
                "format": BACKUP_FORMAT,
                "version": BACKUP_VERSION,
                "created_at": _iso(self._now()),
                "app_version": self._app_version,
                "source": {
                    "name": root.name,
                    "policy": policy,
                    "workspace_id": workspace.workspace_id,
                },
                "scope": BACKUP_SCOPE,
                "encrypted": False,
                "files": files,
                "skipped": [{"path": row.ref, "reason": row.reason} for row in skipped],
                "totals": {"files": len(files), "bytes": total, "skipped": len(skipped)},
            }
            raw = json.dumps(manifest, ensure_ascii=True, indent=2).encode("utf-8")
            if len(raw) > MAX_MANIFEST_BYTES:
                raise BackupError("backup_too_large")
            # The manifest is written last, so a folder without one is an unfinished copy; the
            # rename is what makes the copy a backup.
            try:
                with open(partial / MANIFEST_NAME, "xb") as handle:
                    handle.write(raw)
                osfs.rename_no_replace(partial, destination / name)
            except FileExistsError:
                raise BackupError("conflict") from None
            except OSError:
                raise BackupError("unavailable") from None
            return BackupResult(folder=name, files=len(files), bytes=total, skipped=tuple(skipped))

    def _copy(self, ref: str, source: Path, target: Path) -> tuple[int, str] | BackupSkip:
        """Copy one note, hashing it, then verify the copy by re-reading the destination.

        An unreadable or grown source answers the skip row for it, with nothing created. A
        destination that reads back as other bytes refuses the whole backup: the `.partial` folder
        is never renamed, so no unverified copy is a backup.
        """
        try:
            data = osfs.read_regular(source, MAX_NOTE_BYTES + 1, follow_symlinks=False)
        except OSError:
            return BackupSkip(ref, "unreadable")
        if len(data) > MAX_NOTE_BYTES:
            return BackupSkip(ref, "too_large")  # grown past the note cap since the plan
        digest = hashlib.sha256(data).hexdigest()
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, "xb") as handle:
                handle.write(data)
            verified = _read_digest(target)
        except OSError:
            raise BackupError("unavailable") from None
        if verified != (len(data), digest):
            raise BackupError("backup_unverified")
        return len(data), digest


def _ref(parts: tuple[str, ...]) -> str:
    return PurePosixPath(*parts).as_posix() if parts else "."


def _in_scope(name: str) -> bool:
    """A note-suffix file that is not one of the writer's own temporary files."""
    if name.casefold().startswith((STAGE_PREFIX, BACKUP_PREFIX)):
        return False
    return PurePosixPath(name).suffix.casefold() in NOTE_SUFFIXES


def manifest_parts(ref: str) -> tuple[str, ...]:
    """The manifest path rule, one for the backup that lists a ref and the restore that reads it:
    a relative note ref outside every dot-directory and the reader's private set, with no part
    Windows would refuse or read as a device or alias by dropping a trailing dot or space.
    Raises `VaultError`."""
    parts = safe_ref(ref)  # absolute, `..`, `.`, empty, `\`, `:` and `.git` all refuse
    if (
        any(_unsafe_name(part) or ntpath.isreserved(part) for part in parts)
        or any(part.startswith(".") for part in parts[:-1])
        or not _in_scope(parts[-1])
    ):
        raise VaultError("invalid_ref")
    return parts


def _fold(ref: str) -> str:
    """Two refs one case- or normalisation-insensitive filesystem would store as one name."""
    return unicodedata.normalize("NFC", ref).casefold()


def _one_per_fold(refs: Iterable[str], skipped: list[BackupSkip]) -> list[str]:
    """The refs in sorted order, skipping each one an earlier ref would collide with on a case- or
    normalisation-insensitive filesystem, as a name or as a folder: the restore refuses a manifest
    that lists such a pair."""
    kept: list[str] = []
    names: set[str] = set()
    folders: set[str] = set()
    for ref in sorted(refs):
        parts = ref.split("/")
        key = _fold(ref)
        above = {_fold("/".join(parts[:depth])) for depth in range(1, len(parts))}
        if key in names or key in folders or not above.isdisjoint(names):
            skipped.append(BackupSkip(ref, "unsafe_name"))
            continue
        kept.append(ref)
        names.add(key)
        folders |= above
    return kept


def _same_device(root: Path, destination: Path) -> bool:
    try:
        return os.stat(root).st_dev == os.stat(destination).st_dev
    except OSError:
        return False
