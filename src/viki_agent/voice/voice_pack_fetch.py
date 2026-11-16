"""The voice pack's pinned download with resume, for the setup child (stdlib only, ADR-048).

`fetch` lands one pinned file through `<name>.part` and renames it over its pinned name only
after its size and SHA-256 match the pin. A part an earlier attempt left is re-read into the
digest, then continued with a Range request, so network bytes count only through the pin. The
result is None or one of `network`, `write_failed`, `not_regular`, `size_mismatch`,
`hash_mismatch` and `bad_range`. A size or hash mismatch removes the part; any other failure
keeps it.
"""

from __future__ import annotations

import hashlib
import http.client  # noqa: TID251
import os
import stat
import urllib.request  # noqa: TID251 -- the voice pack's pinned download, setup child only (ADR-048)
from collections.abc import Callable
from contextlib import suppress
from email.message import Message
from pathlib import Path
from typing import BinaryIO, Protocol

from viki_agent.voice import voice_models

CHUNK = 1 << 20
TIMEOUT_S = 60
USER_AGENT = "viki-agent-voice-pack"
# What the opener or a read of its answer raises when the request fails: urllib's URLError and
# socket errors are OSError, and urllib raises ValueError for a URL it cannot open.
_NETWORK_ERRORS = (OSError, http.client.HTTPException, ValueError)  # noqa: TID251


class Response(Protocol):
    """What `fetch` reads of an answer: urllib's response, or a test's fake."""

    status: int
    headers: Message

    def read(self, n: int, /) -> bytes: ...

    def close(self) -> None: ...


class _HttpsOnlyRedirects(urllib.request.HTTPRedirectHandler):  # noqa: TID251
    """Follow a redirect only to https; refusing makes the 3xx surface as an HTTPError."""

    # Unannotated: the parameters take the base class's types.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not newurl.startswith("https://"):
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def open_range(url: str, offset: int) -> Response:
    if not url.startswith("https://"):
        raise ValueError("only https URLs are fetched")
    headers = {"User-Agent": USER_AGENT}
    if offset > 0:
        headers["Range"] = f"bytes={offset}-"
    request = urllib.request.Request(url, headers=headers)  # noqa: TID251
    return urllib.request.build_opener(_HttpsOnlyRedirects).open(request, timeout=TIMEOUT_S)  # noqa: TID251


def fetch(
    pin: voice_models.PinnedFile,
    directory: Path,
    *,
    opener: Callable[[str, int], Response] = open_range,
    progress: Callable[[int], None] = lambda done: None,
) -> str | None:
    if voice_models.verify(directory, (pin,)) is None:
        progress(pin.size)
        return None
    part = directory / (pin.name + ".part")
    try:
        part.parent.mkdir(parents=True, exist_ok=True)
        try:
            info = part.lstat()
        except FileNotFoundError:
            pass
        else:
            reparse = getattr(info, "st_file_attributes", 0) & getattr(
                stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0
            )
            # A folder or link here is not ours to touch; a part past the size is no prefix.
            if not stat.S_ISREG(info.st_mode) or reparse:
                return "not_regular"
            if info.st_size > pin.size:
                part.unlink()
        fd = os.open(part, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o600)
        with open(fd, "rb+") as out:
            error = _resume(out, pin, opener, progress)
            if error is None:
                out.flush()
                os.fsync(out.fileno())
        if error is None:
            os.replace(part, directory / pin.name)
        elif error in ("size_mismatch", "hash_mismatch"):
            part.unlink()
        return error
    except _NetworkError:
        # Raised inside the `with`, so the part was flushed and closed first.
        return "network"
    except OSError:
        # Anything else is local file work: retrying the download would not help.
        return "write_failed"


def _resume(
    out: BinaryIO,
    pin: voice_models.PinnedFile,
    opener: Callable[[str, int], Response],
    progress: Callable[[int], None],
) -> str | None:
    digest = hashlib.sha256()
    done = 0
    while chunk := out.read(CHUNK):
        digest.update(chunk)
        done += len(chunk)
        progress(done)
    # A whole part needs no request: its digest decides.
    if done < pin.size:
        with _Guarded(opener, pin.url, done) as answer:
            status, headers = answer.response.status, answer.response.headers
            content_range = f"bytes {done}-{pin.size - 1}/{pin.size}"
            if status == 200:
                length = headers.get("Content-Length")
                if length is not None and length != str(pin.size):
                    return "size_mismatch"
                out.seek(0)
                out.truncate()
                digest, done = hashlib.sha256(), 0
            elif status != 206:
                return "network"
            elif done == 0 or headers.get("Content-Range") != content_range:
                return "bad_range"
            while chunk := answer.read():
                done += len(chunk)
                if done > pin.size:
                    return "size_mismatch"
                digest.update(chunk)
                out.write(chunk)
                progress(done)
    if done < pin.size:
        return "network"
    return None if digest.hexdigest() == pin.sha256 else "hash_mismatch"


class _NetworkError(Exception):
    """The opener or a read of its answer failed; local file errors stay OSError."""


class _Guarded:
    """The opener's answer, with the errors of the request and its reads as `_NetworkError`."""

    def __init__(self, opener: Callable[[str, int], Response], url: str, offset: int) -> None:
        try:
            self.response = opener(url, offset)
        except _NETWORK_ERRORS as exc:
            raise _NetworkError from exc

    def read(self) -> bytes:
        try:
            return self.response.read(CHUNK)
        except _NETWORK_ERRORS as exc:
            raise _NetworkError from exc

    def __enter__(self) -> _Guarded:
        return self

    def __exit__(self, *exc_info: object) -> None:
        with suppress(OSError):
            self.response.close()
