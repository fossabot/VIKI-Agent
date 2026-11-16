"""Backup restore: a verified `notes` backup copied into a new folder, then registered read-only.

This is not the journal's `restore` kind, which puts a deleted note back out of the recovery store:
a backup restore never writes a vault. Hard rule 1 governs what it writes. The only
folder it writes is one it creates itself, create-only, under an existing parent outside every
registered root and the deny set, through `vault_storage`'s bootstrap pattern; the copy then
becomes a registration through `WorkspaceService.register_existing`, the ordinary open-existing
path, and so opens read-only.

The manifest is untrusted input. It is read bounded and refused whole (`invalid_backup`) unless
every field has the shape `vault_backup` writes, and it can only narrow the binding: the copy is
bound protected when its identity matches a protected registration (`register_existing`'s own
criterion), when the manifest's source workspace is protected on this device, or when the manifest
says its source was protected, in the transaction that registers it. Without a manifest the same
folder would open `existing_readonly`, so a tampered one widens nothing. Every file is re-hashed
against the manifest as it is copied: a mismatch is skipped and the restore is reported unverified.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from anyio.to_thread import run_sync

from viki_agent import osfs
from viki_agent.app.exports import denied_destination, is_device_or_unc
from viki_agent.domain import Workspace
from viki_agent.storage import StateError
from viki_agent.vault_storage import _make_dir, folder_name
from viki_agent.vaults.vault import (
    MAX_NOTE_BYTES,
    MAX_VISITED_ITEMS,
    WRITE_ONLY_NAMES,
    VaultError,
    _is_reparse,
)
from viki_agent.vaults.vault_backup import (
    BACKUP_FORMAT,
    BACKUP_SCOPE,
    BACKUP_VERSION,
    MANIFEST_NAME,
    MAX_BACKUP_BYTES,
    MAX_BACKUP_FILES,
    MAX_MANIFEST_BYTES,
    MAX_RECEIPTS,
    PARTIAL_SUFFIX,
    _fold,
    manifest_parts,
)
from viki_agent.workspaces import WorkspaceError, WorkspaceService, chain_has_reparse

MAX_SOURCE_CHARS = 256
POLICIES = frozenset({"ordinary", "existing_readonly", "protected"})
_SHA256 = re.compile(r"[0-9a-f]{64}")


class BackupRestoreError(RuntimeError):
    """A typed, path-free backup-restore failure safe to map at an API boundary."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class BackupRestoreProblem:
    ref: str
    reason: str  # missing | unreadable | reparse_point | not_regular | mismatch


@dataclass(frozen=True)
class BackupRestorePreview:
    source_name: str
    source_policy: str
    protected: bool  # bound protected by the manifest; an identity match can still add it
    files: int
    bytes: int
    instruction_files: tuple[str, ...]
    problems: tuple[BackupRestoreProblem, ...]
    backup_skipped: int  # rows the backup itself skipped; they were never copied


@dataclass(frozen=True)
class BackupRestoreResult:
    workspace: Workspace
    files: int
    bytes: int
    verified: bool  # every file the manifest lists was copied and matched its hash
    problems: tuple[BackupRestoreProblem, ...]


@dataclass(frozen=True)
class _Entry:
    ref: str
    parts: tuple[str, ...]
    size: int
    sha256: str


@dataclass(frozen=True)
class _Manifest:
    source_name: str
    source_policy: str
    source_workspace_id: str
    files: tuple[_Entry, ...]
    skipped: int


def _invalid() -> BackupRestoreError:
    return BackupRestoreError("invalid_backup")


def _is_int(value: object) -> bool:
    return type(value) is int  # `True == 1`, so a bool must not pass as a count


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """A repeated key would let two readers of one manifest see two different values."""
    keys = [key for key, _ in pairs]
    if len(set(keys)) != len(keys):
        raise ValueError("duplicate key")
    return dict(pairs)


def _manifest_parts(path: object) -> tuple[str, ...]:
    """`vault_backup.manifest_parts`, the rule the backup listed its refs by, applied to a ref
    the manifest claims."""
    if not isinstance(path, str):
        raise _invalid()
    try:
        return manifest_parts(path)
    except VaultError:
        raise _invalid() from None


def _parse(document: object) -> _Manifest:
    """Every field `vault_backup` writes, with exactly its shape, or `invalid_backup`."""
    if not isinstance(document, dict):
        raise _invalid()
    doc: dict[str, Any] = document
    if (
        doc.get("format") != BACKUP_FORMAT
        or not _is_int(doc.get("version"))
        or doc["version"] != BACKUP_VERSION
        or doc.get("scope") != BACKUP_SCOPE
        or doc.get("encrypted") is not False
    ):
        raise _invalid()
    source, files, skipped, totals = (
        doc.get(key) for key in ("source", "files", "skipped", "totals")
    )
    if not isinstance(source, dict) or set(source) != {"name", "policy", "workspace_id"}:
        raise _invalid()
    if (
        not isinstance(source["policy"], str)
        or source["policy"] not in POLICIES
        or not all(
            isinstance(source[key], str) and 1 <= len(source[key]) <= MAX_SOURCE_CHARS
            for key in ("name", "workspace_id")
        )
    ):
        raise _invalid()
    if not isinstance(files, list) or not isinstance(skipped, list):
        raise _invalid()
    if len(files) > MAX_BACKUP_FILES or len(skipped) > MAX_VISITED_ITEMS:
        raise _invalid()
    entries: list[_Entry] = []
    names: set[str] = set()
    folders: set[str] = set()
    for row in files:
        if not isinstance(row, dict) or set(row) != {"path", "bytes", "sha256"}:
            raise _invalid()
        parts = _manifest_parts(row["path"])
        size, digest = row["bytes"], row["sha256"]
        if not _is_int(size) or not 0 <= size <= MAX_NOTE_BYTES:
            raise _invalid()
        if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
            raise _invalid()
        key = _fold(row["path"])
        if key in names:
            raise _invalid()  # a duplicate, exact or by case
        names.add(key)
        folders.update(_fold("/".join(parts[:depth])) for depth in range(1, len(parts)))
        entries.append(_Entry(row["path"], parts, size, digest))
    if names & folders:
        raise _invalid()  # one name listed both as a note and as a folder
    total = sum(entry.size for entry in entries)
    if total > MAX_BACKUP_BYTES:
        raise _invalid()
    for row in skipped:
        if not isinstance(row, dict) or set(row) != {"path", "reason"}:
            raise _invalid()
        if not all(isinstance(value, str) for value in row.values()):
            raise _invalid()
    if (
        not isinstance(totals, dict)
        or set(totals) != {"files", "bytes", "skipped"}
        or not all(_is_int(value) for value in totals.values())
        or (totals["files"], totals["bytes"], totals["skipped"])
        != (len(entries), total, len(skipped))
    ):
        raise _invalid()
    return _Manifest(
        source_name=source["name"],
        source_policy=source["policy"],
        source_workspace_id=source["workspace_id"],
        files=tuple(entries),
        skipped=len(skipped),
    )


def _read_manifest(backup: Path) -> _Manifest:
    path = backup / MANIFEST_NAME
    try:
        if _is_reparse(path.lstat()):
            raise _invalid()
        raw = osfs.read_regular(path, MAX_MANIFEST_BYTES + 1, follow_symlinks=False)
    except FileNotFoundError:
        raise _invalid() from None
    except OSError:
        raise BackupRestoreError("unavailable") from None
    if len(raw) > MAX_MANIFEST_BYTES:
        raise _invalid()
    try:
        document = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_keys)
    except ValueError, RecursionError:
        raise _invalid() from None
    return _parse(document)


def _read_entry(backup: Path, entry: _Entry) -> bytes | BackupRestoreProblem:
    """One listed file's bytes, only when they match the manifest. No reparse point on the way
    is followed: `read_regular` alone follows one on Windows, so each part is checked first."""
    path = backup
    info: os.stat_result | None = None
    for part in entry.parts:
        path = path / part
        try:
            info = path.lstat()
        except FileNotFoundError:
            return BackupRestoreProblem(entry.ref, "missing")
        except OSError:
            return BackupRestoreProblem(entry.ref, "unreadable")
        if _is_reparse(info):
            return BackupRestoreProblem(entry.ref, "reparse_point")
    if info is None or not stat.S_ISREG(info.st_mode):
        return BackupRestoreProblem(entry.ref, "not_regular")
    try:
        data = osfs.read_regular(path, entry.size + 1, follow_symlinks=False)
    except OSError:
        return BackupRestoreProblem(entry.ref, "unreadable")
    if len(data) != entry.size or hashlib.sha256(data).hexdigest() != entry.sha256:
        return BackupRestoreProblem(entry.ref, "mismatch")
    return data


def _read_back(path: Path, size: int) -> bytes:
    """A file just written, read again from the destination."""
    return osfs.read_regular(path, size + 1, follow_symlinks=False)


def _write_verified(folder: Path, entry: _Entry, data: bytes) -> None:
    """Create one file, never replacing one, and verify it from the destination. A copy that
    reads back as other bytes refuses the whole restore, so it is never registered."""
    target = folder.joinpath(*entry.parts)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with osfs.open_new_private(target) as handle:
            handle.write(data)
        copied = _read_back(target, len(data))
    except FileExistsError:
        raise BackupRestoreError("conflict") from None
    except OSError:
        raise BackupRestoreError("unavailable") from None
    if len(copied) != entry.size or hashlib.sha256(copied).hexdigest() != entry.sha256:
        raise BackupRestoreError("backup_restore_unverified")


class BackupRestoreService:
    """Preview a backup and restore it into a fresh folder registered read-only."""

    def __init__(
        self, workspaces: WorkspaceService, *, denied_roots: Callable[[], Sequence[Path]]
    ) -> None:
        self._workspaces = workspaces
        self._denied_roots = denied_roots
        # (result) by command_id, so a repeated request answers with the copy the first one made
        # instead of making a second one. Touched only on the event loop.
        self._receipts: dict[str, BackupRestoreResult] = {}

    async def preview(self, backup: str) -> BackupRestorePreview:
        """Read the manifest and verify every listed file against it. Writes nothing."""
        return await run_sync(lambda: self._preview(backup))

    async def apply(
        self, command_id: str, backup: str, parent: str, folder: str
    ) -> tuple[BackupRestoreResult, bool]:
        """Copy the backup into `<parent>/<folder>`, a folder this call creates, and register it."""
        if not isinstance(command_id, str) or not command_id:
            raise BackupRestoreError("invalid_request")
        replayed = self._receipts.get(command_id)
        if replayed is not None:
            return replayed, True
        result = await run_sync(lambda: self._apply(command_id, backup, parent, folder))
        if len(self._receipts) >= MAX_RECEIPTS:
            del self._receipts[next(iter(self._receipts))]  # insertion order: the oldest
        self._receipts[command_id] = result
        return result, False

    def _preview(self, backup: str) -> BackupRestorePreview:
        source = self._backup_folder(backup)
        manifest = _read_manifest(source)
        problems = tuple(
            read
            for read in (_read_entry(source, entry) for entry in manifest.files)
            if isinstance(read, BackupRestoreProblem)
        )
        return BackupRestorePreview(
            source_name=manifest.source_name,
            source_policy=manifest.source_policy,
            protected=self._protect(manifest),
            files=len(manifest.files),
            bytes=sum(entry.size for entry in manifest.files),
            instruction_files=tuple(
                entry.ref
                for entry in manifest.files
                if entry.parts[-1].casefold() in WRITE_ONLY_NAMES
            ),
            problems=problems,
            backup_skipped=manifest.skipped,
        )

    def _apply(self, command_id: str, backup: str, parent: str, folder: str) -> BackupRestoreResult:
        # Admission, copy and registration share one worker-held registry guard. Its RLock
        # allows the registration helpers to reenter it on this same worker thread.
        with self._workspaces.registration_lock:
            try:
                source = self._backup_folder(backup)
                manifest = _read_manifest(source)  # refused whole before anything is created
                protect = self._protect(manifest)
                target = self._new_folder(parent, folder)
                # A live journal row freezes registration, so the copy could never register: refuse
                # before it exists. A row planned after this check still refuses `_register`, typed
                # below, but leaves the renamed copy unregistered (a known race).
                self._workspaces.refuse_while_frozen()
                partial = target.with_name(target.name + PARTIAL_SUFFIX)
                copied, problems = self._copy(source, manifest, partial)
                try:
                    osfs.rename_no_replace(partial, target)
                except FileExistsError:
                    raise BackupRestoreError("conflict") from None
                except OSError:
                    raise BackupRestoreError("unavailable") from None
                workspace = self._register(command_id, target, protect)
            except (WorkspaceError, StateError) as exc:
                raise BackupRestoreError(exc.code) from None
            return BackupRestoreResult(
                workspace=workspace,
                files=len(copied),
                bytes=sum(entry.size for entry in copied),
                verified=not problems,
                problems=problems,
            )

    def _copy(
        self, source: Path, manifest: _Manifest, partial: Path
    ) -> tuple[list[_Entry], tuple[BackupRestoreProblem, ...]]:
        """Create the `.partial` folder and copy every matching file into it. Until the rename a
        stopped copy is never at the name the user chose, so it never looks like a restore."""
        try:
            _make_dir(partial)
        except FileExistsError:
            raise BackupRestoreError("conflict") from None
        except OSError:
            raise BackupRestoreError("unavailable") from None
        # `vault_storage.VaultBootstrap._refuse_link`'s last gate before any byte.
        if chain_has_reparse(partial):
            raise BackupRestoreError("unsafe_path")
        copied: list[_Entry] = []
        problems: list[BackupRestoreProblem] = []
        for entry in manifest.files:
            read = _read_entry(source, entry)
            if isinstance(read, BackupRestoreProblem):
                problems.append(read)
                continue
            _write_verified(partial, entry, read)
            copied.append(entry)
        return copied, tuple(problems)

    def _register(self, command_id: str, target: Path, protect: bool) -> Workspace:
        workspace, created = self._workspaces.register_existing(
            command_id, str(target), bind_protected=protect
        )
        if not created:
            raise BackupRestoreError("conflict")  # a row already held this folder: not ours
        return workspace

    def _protect(self, manifest: _Manifest) -> bool:
        """The manifest can only add protection: its word, or its source's binding here."""
        if manifest.source_policy == "protected":
            return True
        try:
            return self._workspaces.get(manifest.source_workspace_id).policy == "protected"
        except WorkspaceError:
            return False

    def _backup_folder(self, backup: str) -> Path:
        """`vault_backup`'s destination rule, so every backup the app wrote passes it, and no
        provider home, app data or registered root is read as one."""
        if not isinstance(backup, str) or not backup or is_device_or_unc(backup):
            raise BackupRestoreError("invalid_path")
        try:
            resolved = Path(backup).resolve()
            is_dir = resolved.is_dir()
        except OSError, ValueError:
            raise BackupRestoreError("invalid_path") from None
        if is_device_or_unc(str(resolved)) or not is_dir:
            raise BackupRestoreError("invalid_path")
        if denied_destination(resolved, self._denied_roots()):
            raise BackupRestoreError("denied_destination")
        return resolved

    def _new_folder(self, parent: str, folder: str) -> Path:
        """`<parent>/<folder>` and its `.partial` sibling, judged before either exists: the
        parent must exist, and neither may be a registered root, lie under one or the deny set."""
        leaf = folder_name(folder)
        if not isinstance(parent, str) or not parent or is_device_or_unc(parent):
            raise BackupRestoreError("invalid_path")
        try:
            base = Path(parent).resolve(strict=True)
            is_dir = base.is_dir()
        except OSError, ValueError:
            raise BackupRestoreError("invalid_path") from None
        if is_device_or_unc(str(base)) or not is_dir:
            raise BackupRestoreError("invalid_path")
        target = base / leaf
        roots = self._denied_roots()
        for path in (target, target.with_name(leaf + PARTIAL_SUFFIX)):
            if os.path.lexists(path):
                raise BackupRestoreError("conflict")  # never an existing folder, empty or not
            # `VaultBootstrap._destination`'s judgement of a folder the app is about to create.
            self._workspaces.refuse_inside_registration(self._workspaces.planned_key(path))
            if denied_destination(path, roots):
                raise BackupRestoreError("denied_destination")
        return target
