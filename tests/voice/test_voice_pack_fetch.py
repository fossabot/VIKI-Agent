"""voice_pack_fetch: every answer is served from memory by a fake; no socket is opened."""

from __future__ import annotations

import errno
import hashlib
import io
from collections.abc import Callable
from email.message import Message
from http.client import BadStatusLine, IncompleteRead  # noqa: TID251
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import (  # noqa: TID251
    BaseHandler,
    HTTPDefaultErrorHandler,
    HTTPErrorProcessor,
    OpenerDirector,
    Request,
)

import pytest

from viki_agent.voice import voice_pack_fetch
from viki_agent.voice.voice_models import PinnedFile

DATA = b"pinned voice bytes\n" * 100  # 1900 bytes
URL = "https://example.invalid/voices/af.bin"
# The name holds a folder, as a voice pack's pinned files may.
PIN = PinnedFile("voices/af.bin", len(DATA), hashlib.sha256(DATA).hexdigest(), URL)


class Answer(io.BytesIO):
    """One answer from memory: a status, headers and a body, as urllib's handlers read it."""

    def __init__(
        self, status: int, body: bytes = b"", headers: dict[str, str] | None = None
    ) -> None:
        super().__init__(body)
        self.status = self.code = status
        self.msg = "fake"
        self.headers = Message()
        for name, value in (headers or {}).items():
            self.headers[name] = value

    def info(self) -> Message:
        return self.headers


class Cut(Answer):
    """An answer whose connection drops at the end of its body."""

    def read(self, size: int | None = -1, /) -> bytes:
        if chunk := super().read(size):
            return chunk
        raise IncompleteRead(b"")


def honest(offset: int) -> Answer:
    """The answer of a host that honours Range, as both pinned hosts were probed to."""
    if offset == 0:
        return Answer(200, DATA, {"Content-Length": "1900"})
    return Answer(206, DATA[offset:], {"Content-Range": f"bytes {offset}-1899/1900"})


class Server:
    """A recording opener: `asked` holds each offset asked, `answer` builds each reply."""

    def __init__(self, answer: Callable[[int], Answer] = honest) -> None:
        self.answer = answer
        self.asked: list[int] = []

    def __call__(self, url: str, offset: int) -> Answer:
        assert url == URL
        self.asked.append(offset)
        return self.answer(offset)


def _part(directory: Path, data: bytes) -> Path:
    part = directory / "voices" / "af.bin.part"
    part.parent.mkdir()
    part.write_bytes(data)
    return part


def _names(directory: Path) -> list[str]:
    return sorted(path.relative_to(directory).as_posix() for path in directory.rglob("*"))


# ---- fetch ---------------------------------------------------------------------------------


def test_a_fresh_fetch_creates_the_parents_and_lands_the_file(tmp_path: Path) -> None:
    server = Server()

    assert voice_pack_fetch.fetch(PIN, tmp_path, opener=server) is None

    assert server.asked == [0]
    assert (tmp_path / PIN.name).read_bytes() == DATA
    assert _names(tmp_path) == ["voices", "voices/af.bin"]


def test_a_part_resumes_with_a_range_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(voice_pack_fetch, "CHUNK", 500)
    _part(tmp_path, DATA[:700])
    server = Server()
    done: list[int] = []

    assert voice_pack_fetch.fetch(PIN, tmp_path, opener=server, progress=done.append) is None

    assert server.asked == [700]
    assert (tmp_path / PIN.name).read_bytes() == DATA
    assert _names(tmp_path) == ["voices", "voices/af.bin"]
    # The part is re-read into the digest first, then the rest is appended chunk by chunk.
    assert done == [500, 700, 1200, 1700, 1900]


def test_a_tampered_part_fails_the_digest_and_is_removed(tmp_path: Path) -> None:
    _part(tmp_path, bytes(700))
    server = Server()

    assert voice_pack_fetch.fetch(PIN, tmp_path, opener=server) == "hash_mismatch"

    assert server.asked == [700]
    assert _names(tmp_path) == ["voices"]


def test_a_part_past_the_size_is_dropped_and_fetched_from_zero(tmp_path: Path) -> None:
    _part(tmp_path, DATA + b"tail")
    server = Server()

    assert voice_pack_fetch.fetch(PIN, tmp_path, opener=server) is None

    assert server.asked == [0]
    assert (tmp_path / PIN.name).read_bytes() == DATA


def test_a_200_answer_to_a_range_request_restarts_from_zero(tmp_path: Path) -> None:
    _part(tmp_path, b"stale")  # not DATA's prefix: a restart that kept it would fail
    server = Server(lambda offset: honest(0))

    assert voice_pack_fetch.fetch(PIN, tmp_path, opener=server) is None

    assert server.asked == [5]
    assert (tmp_path / PIN.name).read_bytes() == DATA


@pytest.mark.parametrize("answer", [Answer, Cut], ids=["short", "dropped"])
def test_a_short_body_keeps_the_part_for_resume(tmp_path: Path, answer: type[Answer]) -> None:
    short = Server(lambda offset: answer(200, DATA[:700], {"Content-Length": "1900"}))

    assert voice_pack_fetch.fetch(PIN, tmp_path, opener=short) == "network"
    assert (tmp_path / "voices" / "af.bin.part").read_bytes() == DATA[:700]

    server = Server()
    assert voice_pack_fetch.fetch(PIN, tmp_path, opener=server) is None
    assert server.asked == [700]
    assert (tmp_path / PIN.name).read_bytes() == DATA


@pytest.mark.parametrize(
    ("body", "headers"),
    [(DATA + b"x", {}), (DATA, {"Content-Length": "1901"})],
    ids=["body", "content-length"],
)
def test_a_length_off_the_pin_is_a_size_mismatch_and_leaves_nothing(
    tmp_path: Path, body: bytes, headers: dict[str, str]
) -> None:
    server = Server(lambda offset: Answer(200, body, headers))

    assert voice_pack_fetch.fetch(PIN, tmp_path, opener=server) == "size_mismatch"

    assert _names(tmp_path) == ["voices"]


@pytest.mark.parametrize(
    ("offset", "status", "headers", "code"),
    [
        (700, 206, {"Content-Range": "bytes 700-1899/1901"}, "bad_range"),
        (700, 206, {"Content-Range": "bytes 600-1899/1900"}, "bad_range"),
        (700, 206, {}, "bad_range"),
        (0, 206, {"Content-Range": "bytes 0-1899/1900"}, "bad_range"),
        (700, 204, {}, "network"),
    ],
    ids=["total", "start", "none", "unasked", "status"],
)
def test_an_answer_that_does_not_fit_keeps_the_part(
    tmp_path: Path, offset: int, status: int, headers: dict[str, str], code: str
) -> None:
    part = _part(tmp_path, DATA[:offset])
    server = Server(lambda asked: Answer(status, DATA[asked:], headers))

    assert voice_pack_fetch.fetch(PIN, tmp_path, opener=server) == code

    assert server.asked == [offset]
    assert part.read_bytes() == DATA[:offset]


@pytest.mark.parametrize(
    "error",
    [OSError("connection reset"), BadStatusLine(""), ValueError("unknown url type")],
    ids=["os", "http", "value"],
)
def test_an_opener_error_is_network_and_keeps_the_part(tmp_path: Path, error: Exception) -> None:
    part = _part(tmp_path, DATA[:700])

    def broken(url: str, offset: int) -> Answer:
        raise error

    assert voice_pack_fetch.fetch(PIN, tmp_path, opener=broken) == "network"
    assert part.read_bytes() == DATA[:700]


def test_a_part_that_is_not_a_regular_file_is_left_untouched(tmp_path: Path) -> None:
    folder = tmp_path / "voices" / "af.bin.part"
    folder.mkdir(parents=True)
    (folder / "keep").write_bytes(b"keep")
    server = Server()

    assert voice_pack_fetch.fetch(PIN, tmp_path, opener=server) == "not_regular"

    assert server.asked == []
    assert (folder / "keep").read_bytes() == b"keep"


def test_a_verified_target_makes_no_request(tmp_path: Path) -> None:
    target = tmp_path / PIN.name
    target.parent.mkdir()
    target.write_bytes(DATA)
    server = Server()
    done: list[int] = []

    assert voice_pack_fetch.fetch(PIN, tmp_path, opener=server, progress=done.append) is None

    assert server.asked == []
    assert done == [1900]


def test_a_local_error_is_write_failed_and_the_whole_part_needs_no_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def full(fd: int) -> None:
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(voice_pack_fetch.os, "fsync", full)
    assert voice_pack_fetch.fetch(PIN, tmp_path, opener=Server()) == "write_failed"
    monkeypatch.undo()

    server = Server()
    assert voice_pack_fetch.fetch(PIN, tmp_path, opener=server) is None
    assert server.asked == []
    assert _names(tmp_path) == ["voices", "voices/af.bin"]


# ---- open_range, through a real director whose only way out is a fake host -------------------


class Host(BaseHandler):
    """Answers http and https from memory, so no socket opens; `seen` holds each request."""

    def __init__(self, location: str = "") -> None:
        self.location = location
        self.seen: list[Request] = []

    def https_open(self, request: Request) -> Answer:
        self.seen.append(request)
        if request.full_url == URL and self.location:
            return Answer(302, headers={"Location": self.location})
        return Answer(200, DATA)

    http_open = https_open


def _route(monkeypatch: pytest.MonkeyPatch, host: Host) -> None:
    def build(*handlers: type[BaseHandler] | BaseHandler) -> OpenerDirector:
        director = OpenerDirector()
        for handler in (*handlers, host, HTTPErrorProcessor(), HTTPDefaultErrorHandler()):
            director.add_handler(handler() if isinstance(handler, type) else handler)
        return director

    monkeypatch.setattr("urllib.request.build_opener", build)


def _asked(host: Host) -> list[tuple[str, str | None]]:
    return [(request.full_url, request.get_header("Range")) for request in host.seen]


def test_open_range_asks_a_range_only_past_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    host = Host()
    _route(monkeypatch, host)

    for offset in (0, 700):
        voice_pack_fetch.open_range(URL, offset).close()

    assert _asked(host) == [(URL, None), (URL, "bytes=700-")]
    assert host.seen[0].get_header("User-agent") == "viki-agent-voice-pack"


def test_redirects_are_followed_only_to_https(monkeypatch: pytest.MonkeyPatch) -> None:
    cdn = "https://cdn.example.invalid/af.bin"
    host = Host(location=cdn)
    _route(monkeypatch, host)

    voice_pack_fetch.open_range(URL, 700).close()

    # The Range header rides along to the CDN.
    assert _asked(host) == [(URL, "bytes=700-"), (cdn, "bytes=700-")]

    plain = Host(location="http://cdn.example.invalid/af.bin")
    _route(monkeypatch, plain)

    with pytest.raises(HTTPError) as refused:
        voice_pack_fetch.open_range(URL, 700)

    refused.value.close()
    assert refused.value.code == 302
    assert _asked(plain) == [(URL, "bytes=700-")]


def test_open_range_refuses_a_plain_http_url() -> None:
    with pytest.raises(ValueError):
        voice_pack_fetch.open_range("http://example.invalid/voices/af.bin", 0)
