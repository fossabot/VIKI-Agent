"""Loopback-only HTTP transport to a local Voicebox instance (ADR-047)."""

from __future__ import annotations

import http.client  # noqa: TID251 -- loopback-only Voicebox calls (ADR-047)
import re
import socket
import time
from collections.abc import Buffer

DEFAULT_URL = "http://127.0.0.1:17493"

LANGUAGES = (
    "zh",
    "en",
    "ja",
    "ko",
    "de",
    "fr",
    "ru",
    "pt",
    "es",
    "it",
    "he",
    "ar",
    "da",
    "el",
    "fi",
    "hi",
    "ms",
    "nl",
    "no",
    "pl",
    "sv",
    "sw",
    "tr",
)

MODEL_SIZES = ("0.6B", "1.7B", "1B", "3B")

MAX_TEXT_CHARS = 500
MAX_WAV_BYTES = 8 * 1024 * 1024
MAX_JSON_BYTES = 256 * 1024

STATUS_TIMEOUT_S = 5.0
SPEECH_TIMEOUT_S = 120.0
CONNECT_TIMEOUT_S = 0.5

CODES = (
    "voicebox_off",
    "voicebox_busy",
    "voicebox_unreachable",
    "voicebox_timeout",
    "voicebox_profile_missing",
    "voicebox_model_missing",
    "voicebox_refused",
    "voicebox_invalid",
    "voicebox_pack_missing",
    "voicebox_pack_failed",
)

_LOOPBACK = re.compile(r"http://(127\.0\.0\.1|localhost|\[::1\]):([1-9][0-9]{0,4})/?")


class VoiceboxError(RuntimeError):
    """Only the fixed Voicebox codes cross the transport boundary."""

    def __init__(self, code: str) -> None:
        self.code = code if code in CODES else "voicebox_refused"
        super().__init__(self.code)


def loopback(url: object) -> tuple[str, int] | None:
    """The (host, port) a bare loopback URL names, or None if it is anything else."""
    if not isinstance(url, str):
        return None
    match = _LOOPBACK.fullmatch(url)
    if match is None:
        return None
    port = int(match.group(2))
    if port > 65535:
        return None
    host = match.group(1)
    return ("::1" if host == "[::1]" else host, port)


class _DeadlineSocket(socket.socket):
    """A socket whose every receive ends by one absolute `deadline` (time.monotonic()).

    A socket timeout alone applies to each receive, so a server sending a byte inside every
    timeout could keep the reading thread busy long after the caller's deadline.
    """

    deadline = 0.0

    def recv_into(self, buffer: Buffer, nbytes: int = 0, flags: int = 0) -> int:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("deadline")
        self.settimeout(remaining)
        return super().recv_into(buffer, nbytes, flags)


def request(
    host: str,
    port: int,
    method: str,
    path: str,
    body: bytes | None,
    timeout: float,
    limit: int,
    *,
    connect_timeout: float = CONNECT_TIMEOUT_S,
) -> tuple[int, bytes]:
    """One loopback HTTP call on its own connection, bounded to `limit` response bytes; every
    receive ends by one deadline, `timeout` after the call starts. `connect_timeout` bounds only
    the connect step: a refused loopback connect can otherwise take seconds to report itself
    (observed on Windows) and eat the whole call's budget."""
    deadline = time.monotonic() + timeout
    connection = http.client.HTTPConnection(host, port, timeout=connect_timeout)  # noqa: TID251
    headers = {"Accept": "application/json, audio/wav"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    try:
        try:
            connection.connect()
        except OSError as error:
            raise VoiceboxError("voicebox_unreachable") from error
        plain = connection.sock
        sock = _DeadlineSocket(plain.family, plain.type, plain.proto, plain.detach())
        sock.deadline = deadline
        sock.settimeout(timeout)  # bounds the request's send; each receive narrows it
        connection.sock = sock
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        # read1, not read(amt) or readinto(): on a chunked body with chunk size -1, read(amt)
        # calls _safe_read(-1), which reads to EOF, and readinto() waits for a next chunk size.
        data = bytearray()
        while len(data) <= limit and (piece := response.read1(limit + 1 - len(data))):
            data += piece
        if len(data) > limit:
            raise VoiceboxError("voicebox_invalid")
        return response.status, bytes(data)
    except TimeoutError as error:
        raise VoiceboxError("voicebox_timeout") from error
    except http.client.HTTPException as error:  # noqa: TID251
        raise VoiceboxError("voicebox_invalid") from error
    except OSError as error:
        raise VoiceboxError("voicebox_unreachable") from error
    finally:
        connection.close()
