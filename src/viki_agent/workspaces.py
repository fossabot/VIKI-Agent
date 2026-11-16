"""Canonical roots, workspace registrations and policy.

A registration grants nothing. This module decides which directories the app may
ever write: every candidate is canonicalised on the platform's rules (on Windows they refuse
device syntax, folder-mounted volumes and reparse fallbacks; on macOS and Linux, symlinks and
network filesystems), checked against a deny set derived only from the injected environment
and the OS's fixed system locations, and then evaluated against every existing row inside one
writer transaction so the same directory reached by any path is the same registration.

Nothing here writes inside a registered root; `vault_storage.py` owns the one bootstrap
that creates a new empty folder.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import logging
import ntpath
import os
import re
import stat
import sys
import threading
import unicodedata
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from viki_agent import osfs, protection, rails, storage, winproc
from viki_agent.domain import (
    SCRATCH_WORKSPACE_ID,
    ActiveWorkspace,
    Bootstrap,
    CanonicalRoot,
    Layout,
    PolicyKind,
    RootIdentity,
    Workspace,
    WorkspaceKind,
    WorkspaceSource,
)
from viki_agent.storage import SessionStore, StateError, WorkspaceRow

logger = logging.getLogger(__name__)

RowKind = Literal["vault", "code"]  # the kinds a registration row can hold

GIT_TIMEOUT_S = 5.0
MAX_INSTRUCTION_BYTES = 1 << 20
HEAD_BYTES = 4096
MAX_IDENTITY_OIDS = 8
MAX_IDENTITY_BYTES = 4096  # the `identity` column CHECK; never the thing that refuses a root
INSTRUCTION_FILES = ("CLAUDE.md", "AGENTS.md")
EXCLUDED_FROM_WRITES = ("CLAUDE.md", ".claude/", ".git/")
CODE_EDITOR_REFUSES = (
    *(f"{name}/" for name in (".claude", ".codex", ".git", ".viki")),
    ".credentials.json",
    ".env",
    ".env.local",
    "credentials",
    "credentials.json",
    "secrets.json",
    ".env.*",
    ".aws",
    ".git-credentials",
    ".gnupg",
    ".netrc",
    ".npmrc",
    ".pypirc",
    ".ssh",
    "_netrc",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "id_rsa",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.ppk",
    "*.kdbx",
    ".viki-stage-*",
    ".viki-backup-*",
)
SCRATCH_NAME = "Chat only"
# The scratch root is app-owned and app-created, so it carries the same policy as a vault the
# app created. Nothing here writes a note into it; the CLI owns everything under it.
SCRATCH_POLICY: PolicyKind = "ordinary"
SCRATCH_CHAT = ("scratch", "chat")  # <data_dir>/scratch/chat; the one shared chat cwd
_SCRATCH_SENTINEL = ".viki-scratch"  # never created; names the chain `check_file` walks

_DRIVE_LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_DRIVE_FIXED = 3
_DRIVE_REMOVABLE = 2
_DRIVE_SYNTAX = re.compile(r"^[A-Za-z]:\\")
RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{n}" for n in range(1, 10)}
    | {f"LPT{n}" for n in range(1, 10)}
)
# Filesystem types a POSIX root may not live on: a share can change under the app from another
# machine, so these are refused the way Windows refuses a remote drive.
NETWORK_FILESYSTEMS = frozenset(
    {
        "nfs",
        "nfs4",
        "cifs",
        "smb3",
        "smbfs",
        "9p",
        "afs",
        "ceph",
        "glusterfs",
        "lustre",
        "fuse.sshfs",
        "fuse.rclone",
        "davfs",
        "fuse.davfs2",
        "afpfs",
        "webdav",
        "ftp",
    }
)
# Prefix entries deny themselves and everything beneath; the environment names them all.
_PREFIX_VARIABLES = (
    ("TEMP", "temp"),
    ("TMP", "temp"),
    ("LOCALAPPDATA", "localappdata"),
    ("APPDATA", "appdata"),
    ("ProgramData", "programdata"),
    ("ProgramFiles", "program_files"),
    ("ProgramFiles(x86)", "program_files"),
    ("SystemRoot", "system_root"),
)
_CLOUD_VARIABLES = ("OneDrive", "OneDriveConsumer", "OneDriveCommercial")
# The fixed system locations of Linux and macOS as (path, category, prefix). An exact entry
# denies only itself, so the users' and mounted volumes' own folders below it stay registrable.
_LINUX_DENY: tuple[tuple[str, str, bool], ...] = (
    *(
        (path, "system", True)
        for path in (
            "/bin",
            "/boot",
            "/dev",
            "/etc",
            "/lib",
            "/lib32",
            "/lib64",
            "/opt",
            "/proc",
            "/root",
            "/run",
            "/sbin",
            "/snap",
            "/srv",
            "/sys",
            "/tmp",
            "/usr",
            "/var",
        )
    ),
    *((path, "system", False) for path in ("/home", "/media", "/mnt")),
)
# Removable drives auto-mount under /run/media, and Fedora Atomic's homes live in /var/home.
_LINUX_CARVE_OUTS = {"/run": ("/run/media",), "/var": ("/var/home",)}
_DARWIN_DENY: tuple[tuple[str, str, bool], ...] = (
    *(
        (path, "system", True)
        for path in (
            "/Applications",
            "/Library",
            "/System",
            "/bin",
            "/cores",
            "/dev",
            "/etc",
            "/opt",
            "/private",
            "/sbin",
            "/tmp",
            "/usr",
            "/var",
        )
    ),
    *((path, "system", False) for path in ("/Users", "/Volumes")),
)
# Prefixes under the injected home, as (relative path, category). The macOS cloud folders come
# before ~/Library, so they refuse as cloud sync, the way the Windows OneDrive roots do.
_LINUX_HOME_DENY = ((".cache", "system"), (".config", "system"), (".local", "system"))
_DARWIN_HOME_DENY = (
    ("Library/Mobile Documents", "onedrive"),
    ("Library/CloudStorage", "onedrive"),
    ("Library", "system"),
)
if sys.platform == "win32":
    SYSTEM_DENY: tuple[tuple[str, str, bool], ...] = ()
    _HOME_DENY: tuple[tuple[str, str], ...] = ()
    _CARVE_OUTS: dict[str, tuple[str, ...]] = {}
elif sys.platform == "darwin":
    SYSTEM_DENY, _HOME_DENY, _CARVE_OUTS = _DARWIN_DENY, _DARWIN_HOME_DENY, {}
else:
    SYSTEM_DENY, _HOME_DENY, _CARVE_OUTS = _LINUX_DENY, _LINUX_HOME_DENY, _LINUX_CARVE_OUTS

_MESSAGES = {
    "invalid_path": (
        "Check the folder's location. Use an ordinary path that starts with a drive letter."
    ),
    "not_found": "The folder does not exist.",
    "not_directory": "The path is not a folder.",
    "unsafe_path": ("The path contains a link or redirect. Use an ordinary folder instead."),
    "unsupported_location": (
        "This location isn't supported. Use an ordinary folder on a local disk."
    ),
    "denied_location": (
        "VIKI needs this location for itself, so it can't be used here. Choose another location."
    ),
    "case_collision": (
        "A folder differing only in letter case is already registered. "
        "Open that registered folder instead."
    ),
    "protected_overlap": "This folder lies inside, or contains, a protected vault.",
    "overlaps_registration": "This folder overlaps a registered vault.",
    "destination_not_empty": "The destination already has content.",
    "protected_root": "A protected vault's policy cannot be changed.",
    "invalid_transition": "That policy change is not allowed from the current policy.",
    "stale_revision": "Changed elsewhere; review the current values.",
    "acknowledgement_required": "Review the items before allowing this.",
    "root_unavailable": "The folder is not where it was registered.",
    "unknown_workspace": "No such registration.",
    "invalid_name": "Not a usable name.",
    "not_app_created": "The folder holds files this app did not create; nothing was removed.",
    "not_removable": "Only code folders can be removed from this list.",
    "workspace_in_use": (
        "Chats start in this folder, so it can't be removed. Choose another vault first."
    ),
    "worktrees_present": "This folder still has worktrees. Remove them in the Code view first.",
    "unavailable": "Couldn't write to the destination. Check the free space and access rights.",
    "kind_mismatch": ("This folder is already registered as the other kind (vault or code)."),
    "trust_required": "Trust this folder first.",
    "workspace_active": (
        "The active folder's trust can't change. Select another workspace and restart first."
    ),
}


class WorkspaceError(RuntimeError):
    """A safe service/API error; no raw path, native error or storage message escapes."""

    def __init__(self, code: str, **extra: Any) -> None:
        self.code = code
        self.message = _MESSAGES[code]
        self.extra: Mapping[str, Any] = extra
        super().__init__(self.message)


@dataclass(frozen=True)
class DenyEntry(CanonicalRoot):
    """A denied root and its tier. A plain `CanonicalRoot` in a deny set is a prefix entry."""

    category: str = "denied"
    prefix: bool = True  # False: the entry denies only itself (drive roots, the profile root)
    carve_outs: tuple[str, ...] = ()  # keys whose strict descendants the prefix does not deny


@dataclass(frozen=True)
class InstructionFile:
    path: str
    bytes: int
    sha256: str | None  # None when the file is larger than MAX_INSTRUCTION_BYTES
    head: str | None
    too_large: bool


@dataclass(frozen=True)
class PolicyInspection:
    workspace_id: str
    policy: PolicyKind
    policy_revision: int
    root_available: bool
    aliased: bool
    markers: Mapping[str, bool]
    instructions: list[InstructionFile]
    identity: Mapping[str, object]
    overlaps: list[str]
    admissible: Mapping[str, object]
    excluded_from_writes: list[str]
    acknowledgements_required: list[str]


if sys.platform == "win32":
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _GetVolumePathNameW = _kernel32.GetVolumePathNameW
    _GetVolumePathNameW.restype = wintypes.BOOL
    _GetVolumePathNameW.argtypes = (wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD)
    _GetDriveTypeW = _kernel32.GetDriveTypeW
    _GetDriveTypeW.restype = wintypes.UINT
    _GetDriveTypeW.argtypes = (wintypes.LPCWSTR,)

    def volume_path(path: str) -> str:
        """The mount point that owns `path`; a folder-mounted volume returns that folder."""
        buffer = ctypes.create_unicode_buffer(4096)
        if not _GetVolumePathNameW(path, buffer, len(buffer)):
            raise WorkspaceError("unsupported_location", reason="mount_point")
        return buffer.value

    def drive_type(root: str) -> int:
        return int(_GetDriveTypeW(root))


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def root_key(path: str | Path) -> str:
    """The comparison key of a root path on this OS."""
    return _key_for(str(path), sys.platform)


def _key_for(text: str, platform: str) -> str:
    if platform == "win32":
        return ntpath.normcase(text)
    if platform == "darwin":
        # APFS is case- and normalization-insensitive by default. On a case-sensitive volume
        # this key merges more names than the disk does, so it refuses more, never less.
        return unicodedata.normalize("NFC", text).casefold()
    return text


def _under(child: str, parent: str) -> bool:
    """True when the key `child` lies strictly beneath the key `parent`."""
    prefix = parent if parent.endswith(os.sep) else parent + os.sep
    return child.startswith(prefix)


def _syntax_ok(raw: str) -> bool:
    """The Windows path gate, applied to the raw and the resolved string: a drive-letter path
    with no device syntax, second colon, control character, component ending in a space or dot,
    or reserved device name."""
    if not _DRIVE_SYNTAX.match(raw) or "\\\\?\\" in raw or "\\\\.\\" in raw:
        return False
    if raw.count(":") != 1 or any(character < " " for character in raw):
        return False
    for component in raw[3:].split("\\"):
        if not component:
            continue  # a trailing separator or a doubled one is normalised by resolve()
        if component[-1] in " ." or component.split(".", 1)[0].upper() in RESERVED_NAMES:
            return False
    return True


def _syntax_ok_posix(raw: str) -> bool:
    """The POSIX path gate, applied to the raw and the resolved string."""
    if not raw.startswith("/") or any(ch < " " or ch == "\x7f" for ch in raw):
        return False
    try:
        encoded = raw.encode("utf-8")
    except UnicodeEncodeError:  # a lone surrogate, as a non-UTF-8 name decodes
        return False
    if len(encoded) > 4096:
        return False
    return all(part not in (b".", b"..") and len(part) <= 255 for part in encoded.split(b"/"))


def _reparse(info: os.stat_result) -> bool:
    return bool(
        stat.S_ISLNK(info.st_mode)
        or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    )


def chain_has_reparse(path: Path, *, strict: bool = False) -> bool:
    """Whether the chain has a reparse point; strict inspection propagates an unreadable part."""
    for candidate in (path, *path.parents):
        try:
            if _reparse(candidate.lstat()):
                return True
        except OSError:
            if strict:
                raise
            continue
    return False


def refuse_denied(key: str, deny: Sequence[CanonicalRoot]) -> None:
    """The deny set, applied on keys: a key that equals, lies under or holds a denied root is
    refused. A path the app has not created yet is judged by this too."""
    for entry in deny:
        prefix = entry.prefix if isinstance(entry, DenyEntry) else True
        category = entry.category if isinstance(entry, DenyEntry) else "denied"
        carve_outs = entry.carve_outs if isinstance(entry, DenyEntry) else ()
        below = prefix and _under(key, entry.key) and not any(_under(key, c) for c in carve_outs)
        if key == entry.key or below or _under(entry.key, key):
            if category == "onedrive":
                raise WorkspaceError("unsupported_location", reason="cloud_sync")
            raise WorkspaceError("denied_location", denied=category)


def canonical_root(raw: str, *, deny: Sequence[CanonicalRoot]) -> CanonicalRoot:
    """Canonicalise one root; every refusal is a typed `WorkspaceError`."""
    if sys.platform != "win32":
        return _canonical_root_posix(raw, deny)
    if not isinstance(raw, str) or not 3 <= len(raw) <= 4096 or not _syntax_ok(raw):
        raise WorkspaceError("invalid_path")
    try:
        resolved = Path(raw).resolve(strict=True)
    except FileNotFoundError:
        raise WorkspaceError("not_found") from None
    except OSError, ValueError:
        raise WorkspaceError("invalid_path") from None
    display = str(resolved)
    # resolve() falls back to a device form for volume-GUID and other unusual inputs; the
    # second gate refuses exactly those results, which no longer name a drive-letter path.
    if not _syntax_ok(display):
        raise WorkspaceError("unsafe_path")
    try:
        info = resolved.lstat()
    except OSError:
        raise WorkspaceError("not_found") from None
    if not stat.S_ISDIR(info.st_mode):
        raise WorkspaceError("not_directory")
    if chain_has_reparse(resolved):
        # resolve() already followed every link, so a reparse point here means it gave up.
        raise WorkspaceError("unsafe_path")
    drive = resolved.drive + "\\"
    if root_key(volume_path(display)) != root_key(drive):
        raise WorkspaceError("unsupported_location", reason="mount_point")
    if drive_type(drive) not in (_DRIVE_FIXED, _DRIVE_REMOVABLE):
        raise WorkspaceError("unsupported_location", reason="drive_type")
    return _identified(resolved, display, deny, aliased=chain_has_reparse(Path(raw)))


def _canonical_root_posix(raw: str, deny: Sequence[CanonicalRoot]) -> CanonicalRoot:
    """The same steps on macOS and Linux, with the POSIX grammar and a network-mount check.

    A symlink anywhere in the typed path refuses as `unsafe_path`; only a Windows junction is
    taken as an alias of its target.
    """
    if sys.platform == "win32":
        raise WorkspaceError("invalid_path")
    if not isinstance(raw, str) or not 1 <= len(raw) <= 4096 or not _syntax_ok_posix(raw):
        raise WorkspaceError("invalid_path")
    try:
        resolved = Path(raw).resolve(strict=True)
    except FileNotFoundError:
        raise WorkspaceError("not_found") from None
    except OSError, ValueError:
        raise WorkspaceError("invalid_path") from None
    try:
        info = resolved.lstat()
    except OSError:
        raise WorkspaceError("not_found") from None
    if not stat.S_ISDIR(info.st_mode):
        raise WorkspaceError("not_directory")
    display = str(resolved)
    if sys.platform == "darwin":
        # The spelling on disk, which is the one the CLI's working directory carries.
        try:
            fd = os.open(resolved, os.O_RDONLY | os.O_DIRECTORY)
            try:
                display = osfs.final_path(fd)
            finally:
                os.close(fd)
        except OSError:
            raise WorkspaceError("not_found") from None
    if not _syntax_ok_posix(display):
        raise WorkspaceError("unsafe_path")
    if chain_has_reparse(Path(raw)) or chain_has_reparse(resolved):
        raise WorkspaceError("unsafe_path")
    try:
        network = osfs.filesystem_type(display) in NETWORK_FILESYSTEMS
    except OSError:
        raise WorkspaceError("unsupported_location", reason="mount_point") from None
    if network:
        raise WorkspaceError("unsupported_location", reason="network")
    return _identified(resolved, display, deny, aliased=False)


def _identified(
    resolved: Path, display: str, deny: Sequence[CanonicalRoot], *, aliased: bool
) -> CanonicalRoot:
    """The physical identity, the key and the deny set: the last steps on every OS."""
    try:
        identity = os.stat(resolved)
    except OSError:
        raise WorkspaceError("not_found") from None
    if not identity.st_dev or not identity.st_ino:
        # A volume without a usable file index (FAT32 is unmeasured) is refused
        # rather than registered under an identity two directories could share.
        raise WorkspaceError("unsupported_location", reason="no_identity")
    key = root_key(display)
    refuse_denied(key, deny)
    return CanonicalRoot(
        key=key,
        display=display,
        physical_id=f"{identity.st_dev}:{identity.st_ino}",
        aliased=aliased,
    )


def denied_roots(
    env: Mapping[str, str], *, data_dir: Path, home: Path, install: Path
) -> tuple[CanonicalRoot, ...]:
    """The deny set, derived only from `env` and these parameters — never `os.environ`.

    `%TEMP%` lies beneath `%LOCALAPPDATA%` on a standard profile, so a set read from the
    real environment would deny every `tmp_path` root and the e2e harness's scratch home.
    """

    def entry(
        path: str | Path, category: str, *, prefix: bool = True, carve_outs: tuple[str, ...] = ()
    ) -> DenyEntry:
        if sys.platform == "win32":
            text = os.path.abspath(str(path))
            key = root_key(text.rstrip("\\") if len(text) > 3 else text)
        else:
            # A candidate is keyed after resolve(), so an entry reached through a link (macOS
            # /tmp, /var and /etc live under /private) is keyed by its target as well.
            text = os.path.realpath(path)
            key = root_key(text)
        return DenyEntry(
            key=key,
            display=text,
            physical_id="0:0",  # the deny set is compared on keys only
            aliased=False,
            category=category,
            prefix=prefix,
            carve_outs=tuple(root_key(os.path.realpath(c)) for c in carve_outs),
        )

    entries = [
        entry(data_dir, "data_dir"),
        entry(data_dir / "edge", "edge_profile"),
        entry(data_dir / "scratch", "scratch"),
        entry(install, "install"),
        entry(home / ".claude", "claude_home"),
        entry(home / ".codex", "codex_home"),
        entry(home, "profile", prefix=False),
    ]
    if sys.platform != "win32":
        entries.append(entry("/", "drive_root", prefix=False))  # the one POSIX drive root
        if env.get("TMPDIR"):
            entries.append(entry(env["TMPDIR"], "temp"))
        entries += [entry(home / relative, category) for relative, category in _HOME_DENY]
        entries += [
            entry(path, category, prefix=prefix, carve_outs=_CARVE_OUTS.get(path, ()))
            for path, category, prefix in SYSTEM_DENY
        ]
        return tuple(entries)
    # Windows names are case-insensitive, and a scrubbed `os.environ` holds them upper-cased.
    named = {name.upper(): value for name, value in env.items()}
    entries += [
        entry(named[name.upper()], category)
        for name, category in _PREFIX_VARIABLES
        if named.get(name.upper())
    ]
    entries += [
        entry(named[name.upper()], "onedrive")
        for name in _CLOUD_VARIABLES
        if named.get(name.upper())
    ]
    # A drive root is never a workspace; naming all 26 needs neither the environment nor a
    # volume scan, and each denies only itself so ordinary folders below stay registrable.
    entries += [entry(f"{letter}:\\", "drive_root", prefix=False) for letter in _DRIVE_LETTERS]
    return tuple(entries)


def _measure_file(path: Path) -> tuple[int, str | None] | None:
    """(bytes, sha256) of one instruction file, read once and closed.

    None when the file is absent or unreadable; a null digest when it is larger than
    `MAX_INSTRUCTION_BYTES`, so the inspection can still report that it is there.
    """
    try:
        data = osfs.read_regular(path, MAX_INSTRUCTION_BYTES + 1)
        if len(data) > MAX_INSTRUCTION_BYTES:
            return path.stat().st_size, None
    except OSError:
        return None
    return len(data), hashlib.sha256(data).hexdigest()


def _has_git_metadata(folder: Path) -> bool:
    """Whether Git opens `folder` as a repository of its own: a `.git` entry, or a bare layout
    of plain files (`HEAD`, `objects/`, `refs/`) with no `.git` at all, as an unpacked archive
    can hold. The app's own reads refuse that layout (`winproc.untrusted_git`); Git outside the
    app, Claude Code's included, does not."""
    return (folder / ".git").exists() or (
        (folder / "HEAD").is_file() and (folder / "objects").is_dir() and (folder / "refs").is_dir()
    )


def probe_identity(root: CanonicalRoot, *, git_available: bool) -> RootIdentity:
    """Record what identifies this directory: Git root commits, common dir, instruction hashes.

    Git runs read-only in `winproc.untrusted_git` with two capped calls; an absent Git, a scope
    override or any failure leaves the physical identity and the hashes to carry the binding.
    """
    oids: tuple[str, ...] = ()
    common_dir: str | None = None
    if git_available and not rails._has_git_scope_override():

        def try_git(*args: str) -> str | None:
            try:
                return winproc.git(root.display, *args, timeout=GIT_TIMEOUT_S).strip()
            except Exception as exc:  # a path or Git stderr never reaches the log
                logger.warning("workspace git probe failed: %s (%s)", args[0], type(exc).__name__)
                return None

        with winproc.untrusted_git():
            # A missing object would start a lazy promisor fetch, which runs the folder's own
            # `uploadpack` or `sshCommand`: nothing in a folder runs before it is trusted. The
            # closing `--` keeps a top-level entry named `HEAD` (or `head` on a case-insensitive
            # volume) from making the revision ambiguous, which would fail the probe and leave
            # a copy of a protected root bound by its instruction files alone.
            listed = try_git(
                "rev-list", "--missing=allow-promisor", "--max-parents=0", "HEAD", "--"
            )
            named = try_git("rev-parse", "--git-common-dir")
        if listed:
            oids = tuple(sorted(listed.split()))[:MAX_IDENTITY_OIDS]
        if named:
            try:
                common_dir = root_key(Path(root.display, named).resolve(strict=True))
            except OSError:
                common_dir = None
    hashes = {}
    for name in INSTRUCTION_FILES:
        measured = _measure_file(Path(root.display, name))
        if measured is not None and measured[1] is not None:
            hashes[name] = measured[1]
    return RootIdentity(
        git_root_oids=oids,
        git_common_dir=common_dir,
        instruction_hashes=hashes,
        established=bool(oids or common_dir),
        inherits_from=None,
        probed_at=_now(),
    )


_ID_NAMESPACE = "viki-agent"  # persisted: ids derived from this fixed string must not change


def scratch_path(data_dir: Path) -> Path:
    """Where the shared chat-only working directory lives. Creates nothing, checks nothing."""
    return data_dir.joinpath(*SCRATCH_CHAT)


def scratch_root(data_dir: Path) -> Path:
    """The one shared chat-only working directory, app-created private and chain-checked."""
    root = scratch_path(data_dir)
    for level in (data_dir, root.parent, root):
        if not level.exists():
            protection.create_private_directory(level)
    # `check_file` walks the unresolved chain and refuses a link anywhere in it. The sentinel
    # is never created; naming a file inside the root is what makes the root itself an ancestor.
    storage.check_file(root / _SCRATCH_SENTINEL)
    return root


def scratch_workspace(data_dir: Path) -> Workspace:
    """The built-in chat-only workspace. Never a row: `persistent` is False."""
    root = scratch_root(data_dir)
    info = os.stat(root)
    stamp = _now()
    return Workspace(
        workspace_id=SCRATCH_WORKSPACE_ID,
        kind="scratch",
        root=CanonicalRoot(
            key=root_key(root),
            display=str(root),
            physical_id=f"{info.st_dev}:{info.st_ino}",
            aliased=False,
        ),
        name=SCRATCH_NAME,
        policy=SCRATCH_POLICY,
        policy_revision=1,
        revision=1,
        source="builtin",
        layout="scratch",
        portable_id=None,
        identity=RootIdentity((), None, {}, False, None, stamp),
        face_dir=None,
        bootstrap="none",
        persistent=False,
        created_at=stamp,
        updated_at=stamp,
    )


def _identity_json(identity: RootIdentity) -> str:
    """The stored identity document, kept inside the column's 4 KiB bound.

    Only `git_common_dir` is unbounded (it is a path), so dropping it is what makes an
    oversize document fit; the row's CHECK must never be what refuses a registration.
    """
    document: dict[str, Any] = {
        "git_root_oids": list(identity.git_root_oids),
        "git_common_dir": identity.git_common_dir,
        "instruction_hashes": dict(identity.instruction_hashes),
        "established": identity.established,
        "inherits_from": identity.inherits_from,
        "probed_at": identity.probed_at,
    }
    for attempt in range(2):
        text = json.dumps(document, ensure_ascii=True, separators=(",", ":"))
        if len(text.encode("utf-8")) <= MAX_IDENTITY_BYTES:
            return text
        if attempt == 0:
            document["git_common_dir"] = None
    raise WorkspaceError("unavailable")


def _identity_of(row: WorkspaceRow) -> RootIdentity:
    try:
        stored = json.loads(row["identity"])
        if not isinstance(stored, dict):
            raise ValueError
    except ValueError:
        return RootIdentity((), None, {}, False, None, "")
    oids = stored.get("git_root_oids") or []
    hashes = stored.get("instruction_hashes") or {}
    return RootIdentity(
        git_root_oids=tuple(str(oid) for oid in oids if isinstance(oid, str)),
        git_common_dir=stored.get("git_common_dir"),
        instruction_hashes={k: v for k, v in hashes.items() if isinstance(v, str)},
        established=bool(stored.get("established")),
        inherits_from=stored.get("inherits_from"),
        probed_at=str(stored.get("probed_at") or ""),
    )


def _to_workspace(row: WorkspaceRow) -> Workspace:
    return Workspace(
        workspace_id=row["workspace_id"],
        kind=row["kind"],
        root=CanonicalRoot(
            key=row["canonical_key"],
            display=row["display_root"],
            physical_id=row["physical_id"],
            aliased=False,  # the stored row records the final path; aliasing is re-observed
        ),
        name=row["name"],
        policy=row["policy"],
        policy_revision=row["policy_revision"],
        revision=row["revision"],
        source=row["source"],
        layout=row["layout"],
        portable_id=row["portable_id"],
        identity=_identity_of(row),
        face_dir=row["face_dir"],
        bootstrap=row["bootstrap"],
        persistent=True,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _new_row(
    workspace_id: str,
    *,
    root: CanonicalRoot,
    name: str,
    policy: PolicyKind,
    source: WorkspaceSource,
    layout: Layout,
    identity: RootIdentity,
    portable_id: str | None = None,
    face_dir: str | None = None,
    bootstrap: Bootstrap = "none",
    command_id: str | None = None,
    active: int = 0,
    kind: RowKind = "vault",
) -> WorkspaceRow:
    stamp = _now()
    return {
        "workspace_id": workspace_id,
        "kind": kind,
        "canonical_key": root.key,
        "display_root": root.display,
        "physical_id": root.physical_id,
        "name": name[:256],
        "policy": policy,
        "policy_revision": 1,
        "revision": 1,
        "source": source,
        "layout": layout,
        "portable_id": portable_id,
        "identity": _identity_json(identity),
        "face_dir": face_dir,
        "bootstrap": bootstrap,
        "command_id": command_id,
        "active": active,
        "created_at": stamp,
        "updated_at": stamp,
    }


# The policies each one may move to. `protected` is one-way: nothing leaves it.
_TRANSITIONS: dict[str, tuple[PolicyKind, ...]] = {
    "ordinary": ("existing_readonly", "protected"),
    "existing_readonly": ("ordinary", "protected"),
    "protected": (),
}


def _refuse_transition(current: str, policy: PolicyKind) -> None:
    if current == "protected":
        raise WorkspaceError("protected_root")
    if policy not in _TRANSITIONS[current]:
        raise WorkspaceError("invalid_transition")


def _with_policy(current: WorkspaceRow, policy: PolicyKind, command_id: str) -> WorkspaceRow:
    """`current` moved to `policy`: the row both `set_policy` and a protected registration write."""
    _refuse_transition(current["policy"], policy)
    row = dict(current)
    row.update(
        policy=policy,
        policy_revision=current["policy_revision"] + 1,
        revision=current["revision"] + 1,
        command_id=command_id,
        updated_at=_now(),
    )
    return row


def _shares_identity(candidate: RootIdentity, row: WorkspaceRow) -> bool:
    stored = _identity_of(row)
    if candidate.git_root_oids and set(candidate.git_root_oids) & set(stored.git_root_oids):
        return True
    if candidate.git_common_dir and candidate.git_common_dir == stored.git_common_dir:
        return True
    return any(
        digest == stored.instruction_hashes.get(name)
        for name, digest in candidate.instruction_hashes.items()
    )


def _overlaps(key: str, other: str) -> bool:
    return _under(key, other) or _under(other, key)


def _untrusted_code(row: WorkspaceRow) -> bool:
    """A code folder is trusted only at `ordinary`; untrusted, it is never selected or run."""
    return row["kind"] == "code" and row["policy"] != "ordinary"


def _require_bootstrap(row: WorkspaceRow, command_id: str | None) -> None:
    """Only a created-vault bootstrap row may resume or replay a command."""
    if (
        command_id is None
        or row["command_id"] != command_id
        or row["kind"] != "vault"
        or row["source"] != "created"
        or row["bootstrap"] not in {"pending", "complete"}
        or (
            row["bootstrap"] == "pending"
            and (row["policy"] != "ordinary" or row["physical_id"] != f"pending:{command_id}")
        )
    ):
        raise WorkspaceError("not_app_created")


class WorkspaceService:
    """Registrations, policy and the active workspace. Writes nothing inside a root."""

    def __init__(
        self,
        store: SessionStore,
        *,
        env: Mapping[str, str],
        data_dir: Path,
        home: Path,
        install_dir: Path,
        git_available: Callable[[], bool],
        hide_previews: Callable[[], bool],
    ) -> None:
        self._store = store
        self._env = dict(env)
        self._data_dir = data_dir
        self._home = home
        self._install_dir = install_dir
        self._git_available = git_available
        self._hide_previews = hide_previews
        self._active: ActiveWorkspace | None = None
        # One registry lock; use destination reservations if unrelated updates need
        # concurrency. Bootstrap holds it across its guarded service calls; a backup holds it
        # without keeping a SQLite transaction open. Both acquire it in their worker thread.
        self.registration_lock = threading.RLock()

    def _update_workspace(
        self,
        workspace_id: str,
        changed: Callable[[WorkspaceRow | None, list[WorkspaceRow]], WorkspaceRow | None],
    ) -> WorkspaceRow | None:
        with self.registration_lock:
            return self._store.update_workspace(workspace_id, changed)

    # --- canonicalisation -----------------------------------------------------------------

    def deny(self) -> tuple[CanonicalRoot, ...]:
        return denied_roots(
            self._env, data_dir=self._data_dir, home=self._home, install=self._install_dir
        )

    def canonicalise(self, raw: str) -> CanonicalRoot:
        return canonical_root(raw, deny=self.deny())

    def planned_key(self, path: Path) -> str:
        """The canonical key `path` would take, judged through its nearest existing ancestor.

        Nothing is created here. The ancestor passes the full rules (deny set, mount point,
        drive type, reparse chain), so a path under a junctioned home is judged by where it
        really lands rather than by the name the caller typed.
        """
        missing: list[str] = []
        candidate = path
        try:
            while not candidate.exists():
                if candidate.parent == candidate or len(missing) > 64:
                    raise WorkspaceError("invalid_path")
                missing.append(candidate.name)
                candidate = candidate.parent
        except OSError:
            raise WorkspaceError("invalid_path") from None
        # The ancestor is the anchor, not the candidate: it is resolved without the deny set
        # (every folder above the app data directory "contains a denied root"), and the deny
        # set is then applied to the path that would actually be created.
        root = canonical_root(str(candidate), deny=())
        key = root_key(Path(root.display, *reversed(missing)))
        refuse_denied(key, self.deny())
        return key

    def refuse_inside_registration(self, key: str) -> None:
        """Refuse a path that is, or lies under, a registration - before anything is created.

        Containment the other way round is fine: a parent folder may hold registrations,
        which is what the suggested `%USERPROFILE%\\VIKI\\Vaults` does after the first vault.
        """
        for row in sorted(
            self._store.list_workspaces(), key=lambda row: row["policy"] != "protected"
        ):
            if key == row["canonical_key"] or _under(key, row["canonical_key"]):
                code = (
                    "protected_overlap" if row["policy"] == "protected" else "overlaps_registration"
                )
                raise WorkspaceError(code, workspace_id=row["workspace_id"])

    def refuse_while_frozen(self) -> None:
        """Raise `StateError("recovery_required")` while a live publication or operation row
        freezes every registration change. It is `update_workspace`'s own probe, run for an id
        no row has, so it changes nothing."""
        self._update_workspace(str(uuid.uuid4()), lambda _current, _rows: None)

    def current_root(self, workspace: Workspace) -> CanonicalRoot | None:
        """Re-canonicalise a registration; None when it no longer resolves to its identity."""
        try:
            root = canonical_root(workspace.root.display, deny=self.deny())
        except WorkspaceError:
            return None
        return root if root.physical_id == workspace.root.physical_id else None

    # --- reads ----------------------------------------------------------------------------

    # Vaults by default, so no vault service ever sees a code folder; `kind=None` is every row.

    def list(self, *, kind: WorkspaceKind | None = "vault") -> list[Workspace]:
        return [
            _to_workspace(row)
            for row in self._store.list_workspaces()
            if kind is None or row["kind"] == kind
        ]

    def get(self, workspace_id: str, *, kind: WorkspaceKind | None = "vault") -> Workspace:
        row = self._store.get_workspace(workspace_id)
        if row is None or (kind is not None and row["kind"] != kind):
            raise WorkspaceError("unknown_workspace")
        return _to_workspace(row)

    def by_command(self, command_id: str) -> Workspace | None:
        """The registration a bootstrap command already made, for resume and replay."""
        for row in self._store.list_workspaces():
            if row["command_id"] == command_id:
                _require_bootstrap(row, command_id)
                return _to_workspace(row)
        return None

    def contains_data_dir(self, data_dir: Path) -> Workspace | None:
        """The registration that holds the app's own data directory, for the launcher's stop."""
        key = root_key(os.path.abspath(str(data_dir)))
        for row in self._store.list_workspaces():
            if key == row["canonical_key"] or _under(key, row["canonical_key"]):
                return _to_workspace(row)
        return None

    # --- registration ---------------------------------------------------------------------

    def register_existing(
        self,
        command_id: str,
        raw_path: str,
        *,
        bind_protected: bool = False,
        kind: RowKind = "vault",
    ) -> tuple[Workspace, bool]:
        """Register an existing folder read-only, or return the registration it already has.

        `bind_protected` binds a new row protected in the transaction that creates it, through
        `set_policy`'s own transition, so no commit holds it at a policy the user could later
        make writable. A registration that already exists is returned as it is. A code folder
        starts untrusted (`existing_readonly`); the other kind's row for it is `kind_mismatch`.
        """
        root = self.canonicalise(raw_path)
        identity = probe_identity(root, git_available=self._git_available())
        return self._bind(
            command_id,
            root,
            identity=identity,
            policy="existing_readonly",
            source="opened",
            layout="unknown",
            name=Path(root.display).name or root.display,
            bind_protected=bind_protected,
            kind=kind,
        )

    def register_created(
        self, command_id: str, root: CanonicalRoot, name: str, *, portable_id: str | None
    ) -> Workspace:
        """Reserve the destination of a bootstrap before the folder exists."""
        workspace_id = str(uuid.uuid4())
        placeholder = CanonicalRoot(
            key=root.key,
            display=root.display,
            # No candidate can match this under criterion 1, so a pending row is only ever
            # reached by its canonical key — which is what makes criterion 2 decidable.
            physical_id=f"pending:{command_id}",
            aliased=root.aliased,
        )
        result: dict[str, WorkspaceRow] = {}

        def changed(current: WorkspaceRow | None, rows: list[WorkspaceRow]) -> WorkspaceRow | None:
            for row in rows:
                if row["command_id"] == command_id:
                    _require_bootstrap(row, command_id)
                    if row["canonical_key"] != root.key or row["display_root"] != root.display:
                        raise WorkspaceError("not_app_created")
                    result["row"] = row  # the same command resumes or replays its own work
                    return None  # its row already exists under its own id; nothing to write
                if row["canonical_key"] != root.key:
                    continue
                if row["bootstrap"] == "pending":
                    raise WorkspaceError(
                        "destination_not_empty",
                        hint="cancel_bootstrap",
                        workspace_id=row["workspace_id"],
                    )
                raise WorkspaceError("destination_not_empty", hint="open_existing")
            self._refuse_overlaps(root.key, rows)
            row = _new_row(
                workspace_id,
                root=placeholder,
                name=name,
                policy="ordinary",
                source="created",
                layout="unknown",
                identity=RootIdentity((), None, {}, False, None, _now()),
                portable_id=portable_id,
                bootstrap="pending",
                command_id=command_id,
            )
            result["row"] = row
            return row

        self._update_workspace(workspace_id, changed)
        return _to_workspace(result["row"])

    def complete_bootstrap(self, workspace_id: str, root: CanonicalRoot) -> Workspace:
        """Record the created folder's real identity and mark the reservation complete."""
        with self.registration_lock:
            current = self._store.get_workspace(workspace_id)
            if current is None:
                raise WorkspaceError("unknown_workspace")
            command_id = current["command_id"]
            _require_bootstrap(current, command_id)
            if current["bootstrap"] == "complete":
                if (
                    root.key != current["canonical_key"]
                    or root.display != current["display_root"]
                    or root.physical_id != current["physical_id"]
                ):
                    raise WorkspaceError("not_app_created")
                return _to_workspace(current)
            identity = probe_identity(root, git_available=self._git_available())
            result: dict[str, WorkspaceRow] = {}

            def changed(
                current: WorkspaceRow | None, rows: list[WorkspaceRow]
            ) -> WorkspaceRow | None:
                if current is None:
                    raise WorkspaceError("unknown_workspace")
                _require_bootstrap(current, command_id)
                if current["bootstrap"] != "pending":
                    raise WorkspaceError("not_app_created")
                others = [row for row in rows if row["workspace_id"] != workspace_id]
                if any(row["physical_id"] == root.physical_id for row in others):
                    raise WorkspaceError("overlaps_registration")
                if root.key != current["canonical_key"]:
                    # Preserve the existing specific refusal for an inadmissible new key,
                    # then refuse a different destination even when it would be admissible.
                    refuse_denied(root.key, self.deny())
                    for row in others:
                        if row["canonical_key"] == root.key:
                            raise WorkspaceError("case_collision", display=row["display_root"])
                    self._refuse_overlaps(root.key, others)
                    raise WorkspaceError("not_app_created")
                if root.display != current["display_root"]:
                    # Older reservations may spell a case-insensitive directory differently.
                    # A folded key alone cannot prove that on a case-sensitive volume.
                    try:
                        reserved = os.stat(current["display_root"])
                        observed = os.stat(root.display)
                    except OSError, ValueError:
                        raise WorkspaceError("not_app_created") from None
                    if (
                        not os.path.samestat(reserved, observed)
                        or root.physical_id != f"{observed.st_dev}:{observed.st_ino}"
                    ):
                        raise WorkspaceError("not_app_created")
                row = dict(current)
                row.update(
                    display_root=root.display,
                    physical_id=root.physical_id,
                    identity=_identity_json(identity),
                    bootstrap="complete",
                    revision=current["revision"] + 1,
                    updated_at=_now(),
                )
                result["row"] = row
                return row

            self._update_workspace(workspace_id, changed)
            return _to_workspace(result["row"])

    def remove_pending(self, workspace_id: str) -> None:
        """Drop a pending bootstrap row. A protected row is never removed by this service."""

        def changed(current: WorkspaceRow | None, rows: list[WorkspaceRow]) -> WorkspaceRow | None:
            if current is None:
                return None
            if current["bootstrap"] != "pending" or current["policy"] == "protected":
                raise WorkspaceError("not_app_created")
            return None

        self._update_workspace(workspace_id, changed)

    def remove(self, workspace_id: str) -> None:
        """Delete a code folder's registration row. Nothing inside the root is touched (ADR-050)."""
        active_id = self.resolve_active().workspace.workspace_id

        def refuse(current: WorkspaceRow | None) -> None:
            if current is None:
                raise WorkspaceError("unknown_workspace")
            if current["policy"] == "protected":
                raise WorkspaceError("protected_root")
            if current["kind"] != "code":
                raise WorkspaceError("not_removable")
            if current["active"] or workspace_id == active_id:
                raise WorkspaceError("workspace_in_use")

        refuse(self._store.get_workspace(workspace_id))
        # Worktrees live in their own table, unreachable from the `update_workspace`
        # callback below, so this read and the delete cannot share one transaction; a worktree
        # created in the gap slips through. Add a lock shared with worktree creation if that
        # race starts mattering.
        worktrees = self._store.worktrees_in_states(("creating", "ready", "removing", "kept"))
        if any(row["workspace_id"] == workspace_id for row in worktrees):
            raise WorkspaceError("worktrees_present")

        def changed(current: WorkspaceRow | None, rows: list[WorkspaceRow]) -> WorkspaceRow | None:
            refuse(current)
            return None

        self._update_workspace(workspace_id, changed)

    def _refuse_overlaps(self, key: str, rows: list[WorkspaceRow]) -> None:
        # Protection first: an overlap with a protected root has its own typed reason.
        for row in sorted(rows, key=lambda candidate: candidate["policy"] != "protected"):
            if _overlaps(key, row["canonical_key"]):
                code = (
                    "protected_overlap" if row["policy"] == "protected" else "overlaps_registration"
                )
                raise WorkspaceError(code, workspace_id=row["workspace_id"])

    def _bind(
        self,
        command_id: str,
        root: CanonicalRoot,
        *,
        identity: RootIdentity,
        policy: PolicyKind,
        source: WorkspaceSource,
        layout: Layout,
        name: str,
        face_dir: str | None = None,
        select: bool = False,
        bind_protected: bool = False,
        kind: RowKind = "vault",
    ) -> tuple[Workspace, bool]:
        """Bind a registration in `_evaluate`'s numbered order, decided inside the writer
        transaction that writes it."""
        workspace_id = str(uuid.uuid4())
        result: dict[str, Any] = {}

        def changed(current: WorkspaceRow | None, rows: list[WorkspaceRow]) -> WorkspaceRow | None:
            row, created, reattach = self._evaluate(
                command_id,
                root,
                rows,
                identity=identity,
                policy=policy,
                source=source,
                layout=layout,
                name=name,
                face_dir=face_dir,
                workspace_id=workspace_id,
                kind=kind,
            )
            if bind_protected and created and row["policy"] != "protected":
                # `set_policy`'s checks for `protected`: the transition rule runs here, and its
                # root is available because this call canonicalised it a moment ago.
                row = _with_policy(row, "protected", command_id)
            result["row"], result["created"], result["reattach"] = row, created, reattach
            # The store writes only under the id it was given, so an existing row is written
            # by its own call below; this transaction still read every row before deciding.
            return row if created else None

        self._update_workspace(workspace_id, changed)
        if result["reattach"]:
            result["row"] = self._reattach(result["row"]["workspace_id"], root)
        row: WorkspaceRow = result["row"]
        if (
            select
            and result["created"]
            and not any(other["active"] for other in self._store.list_workspaces())
        ):
            self._store.select_workspace(row["workspace_id"])
        return _to_workspace(row), bool(result["created"])

    def _evaluate(
        self,
        command_id: str,
        root: CanonicalRoot,
        rows: list[WorkspaceRow],
        *,
        identity: RootIdentity,
        policy: PolicyKind,
        source: WorkspaceSource,
        layout: Layout,
        name: str,
        face_dir: str | None,
        workspace_id: str,
        kind: RowKind,
    ) -> tuple[WorkspaceRow, bool, bool]:
        """(row, created, reattach): `created` rows are written by the calling transaction."""
        # 1. the same directory through any alias, reattached when it moved within its volume;
        # a row of the other kind is never relabelled
        for row in rows:
            if row["physical_id"] != root.physical_id:
                continue
            if row["kind"] != kind:
                raise WorkspaceError("kind_mismatch")
            moved = row["canonical_key"] != root.key or row["display_root"] != root.display
            if moved:
                # The new path is a new place in the tree: a folder moved inside a protected
                # root must not keep its old policy there, and it must not take another
                # registration's key. Criteria 3, 4 and 6 run again before the row moves.
                others = [other for other in rows if other["workspace_id"] != row["workspace_id"]]
                for other in others:
                    if other["canonical_key"] == root.key:
                        raise WorkspaceError("case_collision", display=other["display_root"])
                self._refuse_overlaps(root.key, others)
            return row, False, moved
        for row in rows:
            if row["canonical_key"] != root.key:
                continue
            # 2. a reservation still holds this key; its own command resumes it
            if row["bootstrap"] == "pending":
                if row["command_id"] == command_id:
                    return row, False, False
                raise WorkspaceError(
                    "destination_not_empty",
                    hint="cancel_bootstrap",
                    workspace_id=row["workspace_id"],
                )
            # 3. same key, different directory: a per-directory case difference, never reused
            raise WorkspaceError("case_collision", display=row["display_root"])
        protected = [row for row in rows if row["policy"] == "protected"]
        # 4. an ancestor or descendant of a protected root never becomes a row
        for row in protected:
            if _overlaps(root.key, row["canonical_key"]):
                raise WorkspaceError("protected_overlap", workspace_id=row["workspace_id"])
        # 5. a clone, copy, worktree or cross-volume move of a protected root stays protected
        for row in protected:
            if _shares_identity(identity, row):
                inherited = RootIdentity(
                    git_root_oids=identity.git_root_oids,
                    git_common_dir=identity.git_common_dir,
                    instruction_hashes=identity.instruction_hashes,
                    established=identity.established,
                    inherits_from=row["workspace_id"],
                    probed_at=identity.probed_at,
                )
                return (
                    _new_row(
                        workspace_id,
                        root=root,
                        name=name,
                        policy="protected",
                        source=source,
                        layout=layout,
                        identity=inherited,
                        face_dir=face_dir,
                        command_id=command_id,
                        kind=kind,
                    ),
                    True,
                    False,
                )
        # 6. overlap with any other registration
        self._refuse_overlaps(root.key, rows)
        # 7. the route's own policy
        return (
            _new_row(
                workspace_id,
                root=root,
                name=name,
                policy=policy,
                source=source,
                layout=layout,
                identity=identity,
                face_dir=face_dir,
                command_id=command_id,
                kind=kind,
            ),
            True,
            False,
        )

    def _reattach(self, workspace_id: str, root: CanonicalRoot) -> WorkspaceRow:
        """Point an existing registration at the same directory's new path, policy unchanged.

        A name the user never customised — still the old root's own basename — follows
        the move to the new one, the same way a fresh registration derives a name; any other
        name stays.
        """
        result: dict[str, WorkspaceRow] = {}

        def changed(current: WorkspaceRow | None, rows: list[WorkspaceRow]) -> WorkspaceRow | None:
            if current is None or current["physical_id"] != root.physical_id:
                raise WorkspaceError("unknown_workspace")
            others = [row for row in rows if row["workspace_id"] != workspace_id]
            for other in others:
                if other["canonical_key"] == root.key:
                    raise WorkspaceError("case_collision", display=other["display_root"])
            self._refuse_overlaps(root.key, others)
            row = dict(current)
            name = row["name"]
            if name == Path(current["display_root"]).name:
                name = Path(root.display).name or root.display
            row.update(
                canonical_key=root.key,
                display_root=root.display,
                name=name,
                revision=current["revision"] + 1,
                updated_at=_now(),
            )
            result["row"] = row
            return row

        self._update_workspace(workspace_id, changed)
        return result["row"]

    # --- policy ---------------------------------------------------------------------------

    def inspect_policy(self, workspace_id: str) -> PolicyInspection:
        workspace = self.get(workspace_id, kind=None)
        root = self.current_root(workspace)
        available = root is not None
        if root is not None:
            # The identity is recorded at registration AND at every inspection, so a vault
            # whose instructions or Git history changed still binds its own copies.
            workspace = self._refresh_identity(workspace, root)
        display = Path(workspace.root.display)
        markers = {
            "claude_md": (display / "CLAUDE.md").is_file(),
            "agents_md": (display / "AGENTS.md").is_file(),
            "claude_dir": (display / ".claude").is_dir(),
            "hooks": self._has_hooks(display),
            "git": _has_git_metadata(display),
            "viki_manifest": (display / ".viki" / "vault.json").is_file(),
        }
        if workspace.kind == "code":
            markers["mcp_json"] = (display / ".mcp.json").is_file()
        hidden = self._hide_previews()
        instructions = []
        for name in INSTRUCTION_FILES:
            measured = _measure_file(display / name)
            if measured is None:
                continue
            size, digest = measured
            instructions.append(
                InstructionFile(
                    path=name,
                    bytes=size,
                    sha256=digest,
                    head=None if hidden else self._head(display / name),
                    too_large=digest is None,
                )
            )
        overlaps = [
            row["workspace_id"]
            for row in self._store.list_workspaces()
            if row["workspace_id"] != workspace_id
            and _overlaps(workspace.root.key, row["canonical_key"])
        ]
        required = self._acknowledgements(markers, workspace.kind)
        protected = workspace.policy == "protected"
        reason = "protected_root" if protected else None if available else "root_unavailable"
        return PolicyInspection(
            workspace_id=workspace_id,
            policy=workspace.policy,
            policy_revision=workspace.policy_revision,
            root_available=available,
            aliased=root.aliased if root is not None else False,
            markers=markers,
            instructions=instructions,
            identity={
                "established": workspace.identity.established,
                "git_root_oids": len(workspace.identity.git_root_oids),
                "physical_id_matches": None if root is None else True,
                "inherits_from": workspace.identity.inherits_from,
            },
            overlaps=overlaps,
            admissible={
                "ordinary": available and not protected,
                "protected": available and not protected,  # already protected: nothing to set
                "reason": reason,
            },
            excluded_from_writes=list(
                CODE_EDITOR_REFUSES if workspace.kind == "code" else EXCLUDED_FROM_WRITES
            ),
            acknowledgements_required=required,
        )

    def _refresh_identity(self, workspace: Workspace, root: CanonicalRoot) -> Workspace:
        """Re-probe and store this root's identity, keeping what it was bound from.

        The row `revision` is not touched: observing the same directory again is not a
        change to the registration, and the caller of `set_policy` compares that number.
        Recording is best effort; an inspection answers even when the store cannot be written.
        """
        observed = probe_identity(root, git_available=self._git_available())
        probed = RootIdentity(
            git_root_oids=observed.git_root_oids,
            git_common_dir=observed.git_common_dir,
            instruction_hashes=observed.instruction_hashes,
            established=observed.established,
            inherits_from=workspace.identity.inherits_from,
            probed_at=observed.probed_at,
        )
        result: dict[str, WorkspaceRow] = {}

        def changed(current: WorkspaceRow | None, rows: list[WorkspaceRow]) -> WorkspaceRow | None:
            if current is None:
                raise WorkspaceError("unknown_workspace")
            row = dict(current)
            row["identity"] = _identity_json(probed)
            row["updated_at"] = _now()
            result["row"] = row
            return row

        try:
            self._update_workspace(workspace.workspace_id, changed)
        except StateError as exc:
            # Recording is best effort: a busy or read-only store must still answer the
            # inspection with a document. The response carries what was just observed and
            # the row keeps what it had. The code is the whole log entry; never a path.
            logger.warning("workspace identity not recorded: %s", exc.code)
            return replace(workspace, identity=probed)
        return _to_workspace(result["row"])

    @staticmethod
    def _acknowledgements(markers: Mapping[str, bool], kind: WorkspaceKind) -> list[str]:
        required = []
        if markers["claude_md"] or markers["agents_md"]:
            required.append("instructions_present")
        if markers["hooks"]:
            required.append("hooks_present")
        if markers["git"]:
            required.append("git_present")
        if kind == "code" and (markers["claude_dir"] or markers["mcp_json"]):
            required.append("claude_config_present")
        return required

    @staticmethod
    def _has_hooks(display: Path) -> bool:
        if (display / ".claude" / "hooks").is_dir():
            return True
        for name in ("settings.json", "settings.local.json"):
            path = display / ".claude" / name
            try:
                raw = osfs.read_regular(path, MAX_INSTRUCTION_BYTES).decode("utf-8")
                document = json.loads(raw)
            except OSError, ValueError, RecursionError:
                continue
            if isinstance(document, dict) and document.get("hooks"):
                return True
        return False

    @staticmethod
    def _head(path: Path) -> str | None:
        """At most `HEAD_BYTES` BYTES of the file; a character read would quote four times that."""
        try:
            data = osfs.read_regular(path, HEAD_BYTES)
        except OSError:
            return None
        return data.decode("utf-8", errors="replace")

    def set_policy(
        self,
        command_id: str,
        workspace_id: str,
        expected_policy_revision: int,
        policy: PolicyKind,
        acknowledged: Sequence[str],
    ) -> Workspace:
        active = self._active
        if (
            active is not None
            and active.workspace.kind == "code"
            and active.workspace.workspace_id == workspace_id
        ):
            # This launch's CLI runs in it; revoking means selecting another and restarting.
            raise WorkspaceError("workspace_active")
        inspection = self.inspect_policy(workspace_id)
        _refuse_transition(inspection.policy, policy)
        if not inspection.root_available:
            raise WorkspaceError("root_unavailable")
        if policy == "ordinary":
            missing = [
                name for name in inspection.acknowledgements_required if name not in acknowledged
            ]
            if missing:
                raise WorkspaceError("acknowledgement_required", required=missing)
        result: dict[str, WorkspaceRow] = {}

        def changed(current: WorkspaceRow | None, rows: list[WorkspaceRow]) -> WorkspaceRow | None:
            if current is None:
                raise WorkspaceError("unknown_workspace")
            if current["policy"] == "protected":
                raise WorkspaceError("protected_root")
            if current["policy_revision"] != expected_policy_revision:
                raise WorkspaceError(
                    "stale_revision", current_policy_revision=current["policy_revision"]
                )
            row = _with_policy(current, policy, command_id)
            result["row"] = row
            return row

        self._update_workspace(workspace_id, changed)
        return _to_workspace(result["row"])

    # --- selection and the active workspace -----------------------------------------------

    def select(self, command_id: str, workspace_id: str | None) -> None:
        """Record the choice. Selection is idempotent, so `command_id` only names the intent."""
        if workspace_id is not None:
            row = self._store.get_workspace(workspace_id)
            if row is None:
                raise WorkspaceError("unknown_workspace")
            if _untrusted_code(row):
                raise WorkspaceError("trust_required")
        self._store.select_workspace(workspace_id)

    def selected_id(self) -> str | None:
        return next(
            (row["workspace_id"] for row in self._store.list_workspaces() if row["active"]), None
        )

    def resolve_active(self) -> ActiveWorkspace:
        """The launch's active workspace, resolved once; selecting another needs a restart."""
        if self._active is None:
            self._active = self._resolve_active()
        return self._active

    def _resolve_active(self) -> ActiveWorkspace:
        rows = self._store.list_workspaces()
        selected = next((row["workspace_id"] for row in rows if row["active"]), None)
        override = self._env.get("VIKI_VAULT")
        if override:
            workspace = self._environment_workspace(override, rows)
            if workspace is not None:
                return ActiveWorkspace(
                    workspace=workspace,
                    cwd=Path(workspace.root.display),
                    selected_id=selected,
                    selected_by="environment",
                    chat_only=False,
                )
        if selected is not None:
            row = next(row for row in rows if row["workspace_id"] == selected)
            workspace = _to_workspace(row)
            if (
                not _untrusted_code(row)
                and workspace.bootstrap != "pending"
                and self.current_root(workspace) is not None
            ):
                return ActiveWorkspace(
                    workspace=workspace,
                    cwd=Path(workspace.root.display),
                    selected_id=selected,
                    selected_by="registration",
                    chat_only=False,
                )
        scratch = scratch_workspace(self._data_dir)
        return ActiveWorkspace(
            workspace=scratch,
            cwd=Path(scratch.root.display),
            selected_id=selected,
            selected_by="builtin",
            chat_only=True,
        )

    def _environment_workspace(self, override: str, rows: list[WorkspaceRow]) -> Workspace | None:
        """`VIKI_VAULT` runs criteria 1 and 3–5; its result is never persisted."""
        try:
            root = canonical_root(override, deny=self.deny())
        except WorkspaceError as exc:
            logger.warning("VIKI_VAULT ignored: %s", exc.code)
            return None
        for row in rows:
            if row["physical_id"] == root.physical_id or row["canonical_key"] == root.key:
                if row["bootstrap"] == "pending":
                    logger.warning("VIKI_VAULT ignored: pending_bootstrap")
                    return None
                if row["physical_id"] != root.physical_id:
                    logger.warning("VIKI_VAULT ignored: case_collision")
                    return None
                if row["kind"] == "code":
                    logger.warning("VIKI_VAULT ignored: code_root")
                    return None
                return _to_workspace(row)
        identity = probe_identity(root, git_available=self._git_available())
        protected = [row for row in rows if row["policy"] == "protected"]
        for row in protected:
            if _overlaps(root.key, row["canonical_key"]):
                logger.warning("VIKI_VAULT ignored: protected_overlap")
                return None
        policy: PolicyKind = "existing_readonly"
        inherits = None
        for row in protected:
            if _shares_identity(identity, row):
                policy, inherits = "protected", row["workspace_id"]
                break
        stamp = _now()
        return Workspace(
            workspace_id=str(
                uuid.uuid5(uuid.NAMESPACE_URL, f"{_ID_NAMESPACE}:environment:{root.key}")
            ),
            kind="vault",
            root=root,
            name=Path(root.display).name or root.display,
            policy=policy,
            policy_revision=1,
            revision=1,
            source="environment",
            layout="unknown",
            portable_id=None,
            identity=RootIdentity(
                git_root_oids=identity.git_root_oids,
                git_common_dir=identity.git_common_dir,
                instruction_hashes=identity.instruction_hashes,
                established=identity.established,
                inherits_from=inherits,
                probed_at=identity.probed_at,
            ),
            face_dir=None,
            bootstrap="none",
            persistent=False,
            created_at=stamp,
            updated_at=stamp,
        )
