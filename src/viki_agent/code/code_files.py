"""Read-only files of a registered code folder: ref grammar, the tree and text previews.

A client path is a ref, `/`-joined parts. It is refused before any filesystem call, then
resolved and kept inside the root, so a link pointing outside or at a private name is refused.
"""

from __future__ import annotations

import hashlib
import os
import stat
import sys
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from viki_agent import osfs
from viki_agent.vaults.vault import excluded_part, same_final_path
from viki_agent.workspaces import RESERVED_NAMES, WorkspaceError, WorkspaceService

if sys.platform == "win32":
    import msvcrt

MAX_REF_CHARS = 1024
MAX_ENTRIES = 2000
MAX_PREVIEW_BYTES = 1_048_576
BINARY_SNIFF_BYTES = 8000

# Credential and key files a code folder may hold, on top of the vault's private set.
PRIVATE_NAMES = frozenset(
    {
        ".git-credentials",
        ".netrc",
        "_netrc",
        ".npmrc",
        ".pypirc",
        ".ssh",
        ".aws",
        ".gnupg",
        "id_rsa",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
    }
)
PRIVATE_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".ppk", ".kdbx")
# (byte-order mark, reported encoding, codec that decodes the bytes after it)
BOMS = (
    (b"\xef\xbb\xbf", "utf-8-bom", "utf-8"),
    (b"\xff\xfe", "utf-16-le", "utf-16-le"),
    (b"\xfe\xff", "utf-16-be", "utf-16-be"),
)


class CodeError(Exception):
    """A safe reader error: only `.code`, never a path."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class Entry:
    name: str
    type: Literal["file", "dir", "link", "other"]
    size: int | None


@dataclass(frozen=True)
class Listing:
    path: str
    entries: tuple[Entry, ...]
    truncated: bool


@dataclass(frozen=True)
class FileText:
    path: str
    size: int
    sha256: str | None
    encoding: str | None
    newline: str | None
    text: str | None
    reason: str | None


def _bad_part(part: str) -> bool:
    """A component no ref may hold. A drive, UNC or absolute ref always fails here too: it needs
    a `:`, a `\\` or an empty first part. A trailing space or dot is refused because Windows
    aliases `a.` to `a`."""
    return (
        not part
        or part[-1] in " ."
        or "\\" in part
        or ":" in part
        or any(unicodedata.category(ch) == "Cc" or 0xD800 <= ord(ch) <= 0xDFFF for ch in part)
        or part.split(".", 1)[0].upper() in RESERVED_NAMES
    )


def split_ref(raw: str) -> tuple[str, ...]:
    """The parts of a client ref; `""` is the root. Pure string work, no filesystem call."""
    if len(raw) > MAX_REF_CHARS:
        raise CodeError("invalid_path")
    if not raw:
        return ()
    # Split by hand: PurePosixPath would silently drop the empty part of `a//b` and the `.`
    # of `a/./b`.
    parts = tuple(raw.split("/"))
    if any(_bad_part(part) for part in parts):
        raise CodeError("invalid_path")
    return parts


def is_private(parts: Sequence[str]) -> bool:
    """Whether any part, at any depth, is in the vault's private set or the lists above."""
    return any(
        excluded_part(part, write=False)
        or part.casefold() in PRIVATE_NAMES
        or part.casefold().endswith(PRIVATE_SUFFIXES)
        for part in parts
    )


def _os_error(error: OSError) -> CodeError:
    missing = isinstance(error, FileNotFoundError | NotADirectoryError)
    return CodeError("not_found" if missing else "unavailable")


def _entry(item: os.DirEntry[str]) -> Entry:
    if item.is_junction() or item.is_symlink():
        return Entry(item.name, "link", None)
    if item.is_dir(follow_symlinks=False):
        return Entry(item.name, "dir", None)
    if item.is_file(follow_symlinks=False):
        return Entry(item.name, "file", item.stat(follow_symlinks=False).st_size)
    return Entry(item.name, "other", None)


def _newline(text: str) -> str:
    crlf = text.count("\r\n")
    counts = {"crlf": crlf, "lf": text.count("\n") - crlf, "cr": text.count("\r") - crlf}
    kinds = [kind for kind, count in counts.items() if count]
    if not kinds:
        return "none"
    return kinds[0] if len(kinds) == 1 else "mixed"


def _preview(ref: str, data: bytes) -> FileText:
    digest = hashlib.sha256(data).hexdigest()
    encoding, codec, body = "utf-8", "utf-8", data
    for bom, name, bom_codec in BOMS:
        if data.startswith(bom):
            encoding, codec, body = name, bom_codec, data[len(bom) :]
            break
    if not codec.startswith("utf-16") and b"\x00" in data[:BINARY_SNIFF_BYTES]:
        return FileText(ref, len(data), digest, None, None, None, "binary")
    try:
        text = body.decode(codec)  # strict: odd UTF-16 length or a lone surrogate fails
    except UnicodeDecodeError:
        return FileText(ref, len(data), digest, None, None, None, "unsupported_encoding")
    return FileText(ref, len(data), digest, encoding, _newline(text), text, None)


def _checked(ref: str) -> tuple[str, ...]:
    """The parts of a ref no grammar or private rule refuses. Pure string work."""
    parts = split_ref(ref)
    if is_private(parts):
        raise CodeError("private_path")
    return parts


def resolve_path_at(root: Path, ref: str, *, strict: bool = True) -> tuple[Path, Path]:
    """The resolved root and ref, refusing outside and private targets. A non-strict ref
    may have a missing tail, but its existing ancestors still determine containment."""
    parts = _checked(ref)
    try:
        base = root.resolve(strict=True)
        target = root.joinpath(*parts).resolve(strict=strict)
    except OSError as error:
        raise _os_error(error) from None
    if not target.is_relative_to(base):
        raise CodeError("invalid_path")
    # A link inside the root can still land on a private name.
    if is_private(target.relative_to(base).parts):
        raise CodeError("private_path")
    return base, target


def resolve_at(root: Path, ref: str, *, want: Literal["file", "dir"]) -> tuple[Path, Path]:
    """The resolved `root` and the resolved file or directory `ref` names inside it. The ref
    is checked before any filesystem call."""
    base, target = resolve_path_at(root, ref)
    try:
        mode = target.stat().st_mode
    except OSError as error:
        raise _os_error(error) from None
    if want == "file" and not stat.S_ISREG(mode):
        raise CodeError("not_a_file")
    if want == "dir" and not stat.S_ISDIR(mode):
        raise CodeError("not_a_directory")
    return base, target


def read_at(
    root: Path,
    ref: str,
    *,
    max_bytes: int = MAX_PREVIEW_BYTES,
    on_read: Callable[[int], None] | None = None,
) -> FileText:
    """A text preview of `ref` in `root`: over `MAX_PREVIEW_BYTES` is `too_large` without
    reading the file. A smaller `max_bytes` bounds a review's remaining budget; `on_read`
    receives the bytes consumed, including the extra byte that detects a growing file."""
    limit = min(MAX_PREVIEW_BYTES, max(0, max_bytes))
    base, target = resolve_at(root, ref, want="file")
    try:
        root_info = base.stat()
        target_info = target.stat()
        size = target_info.st_size
        if size > limit:
            return FileText(ref, size, None, None, None, None, "too_large")

        def validate(fd: int) -> None:
            if not os.path.samestat(target_info, os.fstat(fd)) or not os.path.samestat(
                root_info, base.stat()
            ):
                raise CodeError("invalid_path")
            if sys.platform == "win32":
                if not same_final_path(msvcrt.get_osfhandle(fd), str(target)):
                    raise CodeError("invalid_path")
            else:
                final = Path(osfs.final_path(fd))
                depth = len(target.relative_to(base).parts)
                if len(final.parents) < depth:
                    raise CodeError("invalid_path")
                # The kernel's spelling can differ on a case-insensitive macOS volume.
                observed_root = final.parents[depth - 1]
                if not os.path.samestat(root_info, observed_root.stat()):
                    raise CodeError("invalid_path")
                if is_private(final.relative_to(observed_root).parts):
                    raise CodeError("private_path")

        data = osfs.read_regular(target, limit + 1, follow_symlinks=False, pre_read=validate)
    except OSError as error:
        raise _os_error(error) from None
    if on_read is not None:
        on_read(len(data))
    if len(data) > limit:  # the file grew after the stat
        return FileText(ref, len(data), None, None, None, None, "too_large")
    return _preview(ref, data)


class CodeFiles:
    def __init__(self, workspaces: WorkspaceService) -> None:
        self._workspaces = workspaces

    def root(self, workspace_id: str) -> Path:
        try:
            workspace = self._workspaces.get(workspace_id, kind="code")
        except WorkspaceError:
            raise CodeError("unknown_workspace") from None
        root = self._workspaces.current_root(workspace)
        if root is None:
            raise CodeError("root_unavailable")
        return Path(root.display)

    def resolve(
        self, workspace_id: str, ref: str, *, want: Literal["file", "dir"]
    ) -> tuple[Path, Path]:
        """The resolved root and the resolved file or directory `ref` names inside it. The ref
        is checked before the workspace lookup."""
        _checked(ref)
        return resolve_at(self.root(workspace_id), ref, want=want)

    def tree(self, workspace_id: str, ref: str = "") -> Listing:
        """One directory, directories first, then by casefolded name; private names and names
        no ref could open are skipped. The scan stops at the first listable entry past
        `MAX_ENTRIES`, so at most that many entries are held."""
        _, target = self.resolve(workspace_id, ref, want="dir")
        entries: list[Entry] = []
        truncated = False
        try:
            with os.scandir(target) as scan:
                for item in scan:
                    if _bad_part(item.name) or is_private((item.name,)):
                        continue
                    if len(entries) == MAX_ENTRIES:
                        truncated = True
                        break
                    entries.append(_entry(item))
        except OSError as error:
            raise _os_error(error) from None
        entries.sort(key=lambda entry: (entry.type != "dir", entry.name.casefold(), entry.name))
        return Listing(ref, tuple(entries), truncated)

    def read(self, workspace_id: str, ref: str) -> FileText:
        """A text preview: over `MAX_PREVIEW_BYTES` is `too_large` without reading the file."""
        _checked(ref)
        return read_at(self.root(workspace_id), ref)

    def open_target(self, workspace_id: str, ref: str) -> Path | None:
        """The resolved file `ref` names, or the root when `ref` is empty; None on any refusal."""
        try:
            return self.resolve(workspace_id, ref, want="dir" if ref == "" else "file")[1]
        except CodeError:
            return None
