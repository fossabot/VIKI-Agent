"""loopback() and request(): the raw transport primitives for Voicebox calls (ADR-047)."""

from __future__ import annotations

import http.client  # noqa: TID251 -- monkeypatches HTTPConnection.connect (ADR-047)
import socket
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from viki_agent.voice.voicebox_http import (
    CODES,
    DEFAULT_URL,
    LANGUAGES,
    MAX_JSON_BYTES,
    MAX_TEXT_CHARS,
    MAX_WAV_BYTES,
    MODEL_SIZES,
    SPEECH_TIMEOUT_S,
    STATUS_TIMEOUT_S,
    VoiceboxError,
    loopback,
    request,
)

HANG_GUARD_S = 5.0


def test_the_constants_match_the_design() -> None:
    assert DEFAULT_URL == "http://127.0.0.1:17493"
    assert len(LANGUAGES) == 23
    assert len(set(LANGUAGES)) == 23
    assert MODEL_SIZES == ("0.6B", "1.7B", "1B", "3B")
    assert MAX_TEXT_CHARS == 500
    assert MAX_WAV_BYTES == 8 * 1024 * 1024
    assert MAX_JSON_BYTES == 256 * 1024
    assert STATUS_TIMEOUT_S == 5.0
    assert SPEECH_TIMEOUT_S == 120.0
    assert len(CODES) == 10
    assert len(set(CODES)) == 10


def test_an_unknown_code_becomes_voicebox_refused() -> None:
    assert VoiceboxError("not_a_real_code").code == "voicebox_refused"
    assert VoiceboxError("voicebox_timeout").code == "voicebox_timeout"


LOOPBACK_CASES = [
    pytest.param("http://127.0.0.1:17493", ("127.0.0.1", 17493), id="ipv4"),
    pytest.param("http://127.0.0.1:17493/", ("127.0.0.1", 17493), id="ipv4-trailing-slash"),
    pytest.param("http://localhost:17493", ("localhost", 17493), id="localhost"),
    pytest.param("http://[::1]:17493", ("::1", 17493), id="ipv6"),
    pytest.param("http://[::1]:17493/", ("::1", 17493), id="ipv6-trailing-slash"),
    pytest.param("http://127.0.0.1:1", ("127.0.0.1", 1), id="min-port"),
    pytest.param("http://127.0.0.1:65535", ("127.0.0.1", 65535), id="max-port"),
]


@pytest.mark.parametrize("url, expected", LOOPBACK_CASES)
def test_loopback_accepts_bare_loopback_urls(url: str, expected: tuple[str, int]) -> None:
    assert loopback(url) == expected


LOOPBACK_REJECTIONS = [
    pytest.param("https://127.0.0.1:17493", id="other-scheme"),
    pytest.param("http://example.com:17493", id="other-host"),
    pytest.param("http://user@127.0.0.1:17493", id="userinfo"),
    pytest.param("http://127.0.0.1:17493/speech", id="path"),
    pytest.param("http://127.0.0.1:17493?x=1", id="query"),
    pytest.param("http://127.0.0.1:17493#x", id="fragment"),
    pytest.param("http://127.0.0.1:0", id="port-zero"),
    pytest.param("http://127.0.0.1:017493", id="leading-zero-port"),
    pytest.param("http://127.0.0.1:70000", id="port-over-65535"),
    pytest.param("http://127.0.0.1", id="no-port"),
    pytest.param(" http://127.0.0.1:17493", id="leading-whitespace"),
    pytest.param("http://127.0.0.1:17493 ", id="trailing-whitespace"),
    pytest.param("http://127.0.0.1: 17493", id="whitespace-before-port"),
    pytest.param(17493, id="non-str-int"),
    pytest.param(None, id="non-str-none"),
    pytest.param(b"http://127.0.0.1:17493", id="non-str-bytes"),
]


@pytest.mark.parametrize("url", LOOPBACK_REJECTIONS)
def test_loopback_refuses_everything_else(url: object) -> None:
    assert loopback(url) is None


def test_loopback_refuses_lan_hosts() -> None:
    assert loopback("http://192.168.1.5:17493") is None
    assert loopback("http://10.0.0.5:17493") is None
    assert loopback("http://127.0.0.2:17493") is None


Behavior = Callable[[BaseHTTPRequestHandler], None]


@contextmanager
def _running_server(behave: Behavior) -> Iterator[int]:
    """A real ThreadingHTTPServer on an ephemeral loopback port; `behave` writes its response."""

    class Handler(BaseHTTPRequestHandler):
        def _handle(self) -> None:
            behave(self)

        do_GET = do_POST = do_PUT = do_DELETE = _handle

        def log_message(self, format: str, *args: object) -> None:
            pass  # keep test output quiet

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        thread.join(HANG_GUARD_S)
        server.server_close()


def test_request_sends_the_request_as_given_and_returns_the_wav_body() -> None:
    seen: dict[str, object] = {}
    wav = b"RIFF\x00\x00\x00\x00WAVEfmt "

    def behave(handler: BaseHTTPRequestHandler) -> None:
        length = int(handler.headers.get("Content-Length", "0"))
        seen["method"] = handler.command
        seen["path"] = handler.path
        seen["accept"] = handler.headers.get("Accept")
        seen["content_type"] = handler.headers.get("Content-Type")
        seen["body"] = handler.rfile.read(length)
        handler.send_response(200)
        handler.send_header("Content-Type", "audio/wav")
        handler.send_header("Content-Length", str(len(wav)))
        handler.end_headers()
        handler.wfile.write(wav)

    with _running_server(behave) as port:
        status, data = request(
            "127.0.0.1",
            port,
            "POST",
            "/api/speak",
            b'{"text": "hi"}',
            STATUS_TIMEOUT_S,
            MAX_WAV_BYTES,
        )

    assert status == 200
    assert data == wav
    assert seen == {
        "method": "POST",
        "path": "/api/speak",
        "accept": "application/json, audio/wav",
        "content_type": "application/json",
        "body": b'{"text": "hi"}',
    }


def test_request_with_no_body_sends_no_content_type() -> None:
    seen: dict[str, object] = {}

    def behave(handler: BaseHTTPRequestHandler) -> None:
        seen["content_type"] = handler.headers.get("Content-Type")
        handler.send_response(200)
        handler.send_header("Content-Length", "0")
        handler.end_headers()

    with _running_server(behave) as port:
        status, data = request(
            "127.0.0.1", port, "GET", "/api/voicebox", None, STATUS_TIMEOUT_S, 1024
        )

    assert (status, data) == (200, b"")
    assert seen["content_type"] is None


def test_a_body_over_the_limit_is_invalid() -> None:
    def behave(handler: BaseHTTPRequestHandler) -> None:
        payload = b"x" * 11
        handler.send_response(200)
        handler.send_header("Content-Length", str(len(payload)))
        handler.end_headers()
        handler.wfile.write(payload)

    with _running_server(behave) as port, pytest.raises(VoiceboxError) as excinfo:
        request("127.0.0.1", port, "GET", "/", None, STATUS_TIMEOUT_S, 10)

    assert excinfo.value.code == "voicebox_invalid"


def test_a_redirect_status_is_returned_without_a_second_request() -> None:
    calls = 0

    def behave(handler: BaseHTTPRequestHandler) -> None:
        nonlocal calls
        calls += 1
        handler.send_response(302)
        handler.send_header("Location", "http://127.0.0.1:1/elsewhere")
        handler.send_header("Content-Length", "0")
        handler.end_headers()

    with _running_server(behave) as port:
        status, data = request("127.0.0.1", port, "GET", "/", None, STATUS_TIMEOUT_S, 1024)

    assert (status, data) == (302, b"")
    assert calls == 1


def test_a_refused_connection_is_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(_self: http.client.HTTPConnection) -> None:  # noqa: TID251
        raise ConnectionRefusedError

    monkeypatch.setattr(http.client.HTTPConnection, "connect", refuse)  # noqa: TID251

    with pytest.raises(VoiceboxError) as excinfo:
        request("127.0.0.1", 1, "GET", "/", None, STATUS_TIMEOUT_S, 1024)

    assert excinfo.value.code == "voicebox_unreachable"


def test_a_refused_connect_is_unreachable_within_the_connect_limit() -> None:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()  # bound, never listened on, then freed: a connect to it is refused, not slow

    started = time.monotonic()
    with pytest.raises(VoiceboxError) as excinfo:
        request("127.0.0.1", port, "GET", "/", None, STATUS_TIMEOUT_S, 1024, connect_timeout=0.05)
    elapsed = time.monotonic() - started

    assert excinfo.value.code == "voicebox_unreachable"
    assert elapsed < 1.0


def test_a_slow_handler_times_out() -> None:
    hold = threading.Event()

    def behave(handler: BaseHTTPRequestHandler) -> None:
        hold.wait(HANG_GUARD_S)
        handler.send_response(200)
        handler.send_header("Content-Length", "0")
        handler.end_headers()

    try:
        with _running_server(behave) as port:
            with pytest.raises(VoiceboxError) as excinfo:
                request("127.0.0.1", port, "GET", "/", None, 0.05, 1024)
            assert excinfo.value.code == "voicebox_timeout"
    finally:
        hold.set()


def test_a_negative_chunk_size_is_invalid_without_reading_on() -> None:
    hold = threading.Event()
    limit = 1024

    def behave(handler: BaseHTTPRequestHandler) -> None:
        # A stdlib read(amt) of chunk size -1 reads to EOF, which this held connection never sends.
        handler.wfile.write(
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n-1\r\n" + b"x" * (20 * limit)
        )
        hold.wait(HANG_GUARD_S)

    try:
        with _running_server(behave) as port, pytest.raises(VoiceboxError) as excinfo:
            request("127.0.0.1", port, "GET", "/", None, 2.0, limit)
        assert excinfo.value.code == "voicebox_invalid"
    finally:
        hold.set()


def test_a_server_dripping_bytes_cannot_hold_the_call_past_its_timeout() -> None:
    stop = threading.Event()
    timeout = 0.5

    def behave(handler: BaseHTTPRequestHandler) -> None:
        handler.wfile.write(b"HTTP/1.1 200 OK\r\nContent-Length: 1000000\r\n\r\n")
        give_up = time.monotonic() + HANG_GUARD_S
        with suppress(OSError):  # the client hangs up at its deadline
            # One byte inside every socket timeout, for far longer than the timeout.
            while time.monotonic() < give_up and not stop.wait(0.3 * timeout):
                handler.wfile.write(b"[")

    try:
        with _running_server(behave) as port:
            started = time.monotonic()
            with pytest.raises(VoiceboxError) as excinfo:
                request("127.0.0.1", port, "GET", "/", None, timeout, 1024)
            elapsed = time.monotonic() - started
    finally:
        stop.set()

    assert excinfo.value.code == "voicebox_timeout"
    assert elapsed < timeout + 0.5


@pytest.mark.parametrize("limit", [16, 1024], ids=["at-the-limit", "under-the-limit"])
def test_a_chunked_body_within_the_limit_is_returned_whole(limit: int) -> None:
    def behave(handler: BaseHTTPRequestHandler) -> None:
        handler.wfile.write(
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
            b"4\r\nRIFF\r\n4\r\n\x24\x00\x00\x00\r\n8\r\nWAVEfmt \r\n0\r\n\r\n"
        )

    with _running_server(behave) as port:
        status, data = request("127.0.0.1", port, "GET", "/", None, STATUS_TIMEOUT_S, limit)

    assert (status, data) == (200, b"RIFF\x24\x00\x00\x00WAVEfmt ")
