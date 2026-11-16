"""Pinned download and zip member extraction for the voice setup (stdlib only).

Every file lands through `<name>.part`, created exclusively, and is renamed over its pinned name
only after its size and SHA-256 match the pin, so a short, long or tampered download never
replaces a file and a member name inside an archive never becomes a path. The result is None or one
of `network`, `write_failed`, `not_regular`, `size_mismatch`, `hash_mismatch`, `missing_member`
and `bad_archive`; a body is refused as soon as it passes the pinned size.
"""

from __future__ import annotations

import hashlib
import os
import stat
import urllib.request
import zipfile
from collections.abc import Callable
from contextlib import AbstractContextManager, suppress
from http.client import HTTPException
from pathlib import Path
from typing import IO, TYPE_CHECKING, BinaryIO, Protocol

if TYPE_CHECKING:
    from http.client import HTTPMessage

CHUNK = 1 << 20
USER_AGENT = "viki-agent-voice-setup"
TIMEOUT_S = 60


class _HttpsOnlyRedirects(urllib.request.HTTPRedirectHandler):
    """Follow a redirect only to https; refusing makes the 3xx surface as an HTTPError."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: HTTPMessage,
        newurl: str,
    ) -> urllib.request.Request | None:
        if not newurl.startswith("https://"):
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def open_https(url: str) -> BinaryIO:
    if not url.startswith("https://"):
        raise ValueError("only https URLs are fetched")
    opener = urllib.request.build_opener(_HttpsOnlyRedirects)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    return opener.open(request, timeout=TIMEOUT_S)


def fetch(
    url: str,
    name: str,
    size: int,
    sha256: str,
    directory: Path,
    *,
    opener: Callable[[str], BinaryIO] = open_https,
) -> str | None:
    if _verified(directory / name, size, sha256):
        return None
    try:
        return _publish(lambda: _Guarded(opener, url), name, size, sha256, directory)
    except _NetworkError:
        return "network"
    except OSError:
        # Anything else is local file work (part file, write, fsync, rename): retrying the
        # download would not help.
        return "write_failed"


def extract(
    archive: Path, member: str, name: str, size: int, sha256: str, directory: Path
) -> str | None:
    if _verified(directory / name, size, sha256):
        return None
    try:
        with zipfile.ZipFile(archive) as zf:
            try:
                info = zf.getinfo(member)
            except KeyError:
                return "missing_member"
            if info.file_size != size:
                return "size_mismatch"
            # The member is only a source of bytes: its name never joins a path.
            return _publish(lambda: zf.open(info), name, size, sha256, directory)
    except zipfile.BadZipFile:
        return "bad_archive"
    except OSError:
        return "write_failed"


class _Reader(Protocol):
    def read(self, n: int, /) -> bytes: ...


class _NetworkError(Exception):
    """The opener or a read of the response failed; local file errors stay OSError."""


class _Guarded:
    """The response stream, with its OSError and HTTPException turned into `_NetworkError`."""

    def __init__(self, opener: Callable[[str], BinaryIO], url: str) -> None:
        try:
            self._stream = opener(url)
        except (OSError, HTTPException) as exc:
            raise _NetworkError from exc

    def read(self, n: int) -> bytes:
        try:
            return self._stream.read(n)
        except (OSError, HTTPException) as exc:
            raise _NetworkError from exc

    def __enter__(self) -> _Guarded:
        return self

    def __exit__(self, *exc_info: object) -> None:
        with suppress(OSError):
            self._stream.close()


def _verified(path: Path, size: int, sha256: str) -> bool:
    try:
        if path.stat().st_size != size:
            return False
        digest = hashlib.sha256()
        with path.open("rb") as f:
            while chunk := f.read(CHUNK):
                digest.update(chunk)
    except OSError:
        return False
    return digest.hexdigest() == sha256


def _publish(
    open_source: Callable[[], AbstractContextManager[_Reader]],
    name: str,
    size: int,
    sha256: str,
    directory: Path,
) -> str | None:
    part = directory / (name + ".part")
    try:
        mode = part.lstat().st_mode
    except FileNotFoundError:
        pass
    else:
        # A folder or link here is not ours to remove; a stale regular part is.
        if not stat.S_ISREG(mode):
            return "not_regular"
        part.unlink()
    fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    try:
        with open(fd, "wb") as out:
            with open_source() as source:
                error = _copy(source, out, size, sha256)
            if error is None:
                out.flush()
                os.fsync(out.fileno())
        if error is None:
            os.replace(part, directory / name)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    if error is not None:
        part.unlink()
    return error


def _copy(source: _Reader, out: IO[bytes], size: int, sha256: str) -> str | None:
    digest = hashlib.sha256()
    written = 0
    while chunk := source.read(CHUNK):
        written += len(chunk)
        if written > size:
            return "size_mismatch"
        digest.update(chunk)
        out.write(chunk)
    if written != size:
        return "size_mismatch"
    if digest.hexdigest() != sha256:
        return "hash_mismatch"
    return None
