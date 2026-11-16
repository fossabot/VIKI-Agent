"""The /ws/voice session over a real VoiceService and tests/fake_voice_worker.py.

A minimal app: SecurityMiddleware, this route, and a stub `authenticate` that reads one message.
"""

from __future__ import annotations

import contextlib
import json
import logging
from collections.abc import AsyncIterator, Iterator, Mapping
from pathlib import Path
from typing import Any

import anyio
import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient, WebSocketTestSession
from starlette.types import Message
from starlette.websockets import WebSocket, WebSocketDisconnect

from tests.voice.test_voice_engine import (
    CAPTURE,
    CHUNK,
    FAKE,
    HANG_GUARD_S,
    OTHER,
    gone,
    install_model,
    pins,  # noqa: F401 (autouse: the fake's modes as pinned models)
)
from tests.voice.test_voice_service import Factory
from viki_agent.server import SecurityMiddleware
from viki_agent.voice import voice_engine
from viki_agent.voice.voice_service import VoiceService
from viki_agent.voice.voice_socket import (
    MAX_AUDIO_BYTES,
    MAX_TEXT_BYTES,
    MAX_UNAUTHENTICATED,
    voice_socket_route,
)

HEADERS = {"host": "127.0.0.1:8765", "origin": "http://127.0.0.1:8765"}
AUTH = {"type": "auth", "token": "t", "v": 2}
CPU_OK = {"voice.model": "fake-ok", "voice.backend": "cpu"}
FAST = 0.05  # a timer that must fire


async def authenticate(websocket: WebSocket) -> bool:
    message = await websocket.receive()
    try:
        ok = json.loads(message.get("text") or "") == AUTH
    except ValueError:
        ok = False
    if not ok and message["type"] != "websocket.disconnect":
        await websocket.close(code=4401)
    return ok


@contextlib.contextmanager
def running(
    data: Path,
    settings: Mapping[str, object] = CPU_OK,
    *,
    receive_gap_s: float = HANG_GUARD_S,
    final_timeout_s: float = HANG_GUARD_S,
    **engine: Any,
) -> Iterator[tuple[TestClient, Factory]]:
    factory = Factory(**engine)
    service = VoiceService(data, read_settings=lambda: settings, worker=FAKE, engine=factory)

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await service.close()

    route = voice_socket_route(
        service,
        authenticate=authenticate,
        receive_gap_s=receive_gap_s,
        final_timeout_s=final_timeout_s,
    )
    app = SecurityMiddleware(Starlette(routes=[route], lifespan=lifespan), dev=False)
    with TestClient(app) as client:  # one portal, so the engine lives on one loop
        yield client, factory


def connect(client: TestClient, origin: str = HEADERS["origin"]) -> WebSocketTestSession:
    return client.websocket_connect("/ws/voice", headers={**HEADERS, "origin": origin})


async def _next_message(ws: WebSocketTestSession) -> Message:
    with anyio.fail_after(HANG_GUARD_S):
        return await ws._send_rx.receive()


def recv(ws: WebSocketTestSession) -> dict[str, Any]:
    message = ws.portal.call(_next_message, ws)
    if message["type"] == "websocket.close":
        raise WebSocketDisconnect(message.get("code", 1000))
    return json.loads(message["text"])


def until_closed(ws: WebSocketTestSession) -> tuple[list[dict[str, Any]], int]:
    messages: list[dict[str, Any]] = []
    while True:
        try:
            messages.append(recv(ws))
        except WebSocketDisconnect as closed:
            return messages, closed.code


def send(ws: WebSocketTestSession, kind: str, capture_id: str = CAPTURE) -> None:
    ws.send_json({"type": kind, "capture_id": capture_id})


def begin(ws: WebSocketTestSession, capture_id: str = CAPTURE) -> list[str]:
    """Authenticate and begin; the message types up to and including capturing."""
    ws.send_json(AUTH)
    send(ws, "begin", capture_id)
    kinds = [recv(ws)["type"]]
    while kinds[-1] != "capturing":
        kinds.append(recv(ws)["type"])
    return kinds


def capturing(capture_id: str = CAPTURE, model: str = "fake-ok") -> dict[str, Any]:
    return {
        "type": "capturing",
        "capture_id": capture_id,
        "model": model,
        "backend": "cpu",
        "device": None,
        "language": "auto",
        "language_unavailable": False,
        "fallback": None,
    }


def a_next_begin_reuses_the_engine(client: TestClient, factory: Factory) -> bool:
    """The last capture was cancelled at the engine (not aborted): the next begin starts nothing."""
    with connect(client) as ws:
        kinds = begin(ws, OTHER)
    return kinds == ["capturing"] and len(factory.recorder.calls) == 1


def tree(root: Path) -> list[tuple[str, int, int]]:
    return sorted(
        (str(path.relative_to(root)), path.stat().st_size, path.stat().st_mtime_ns)
        for path in [root, *root.rglob("*")]
    )


def test_a_capture_streams_its_results_and_closes_1000(tmp_path: Path) -> None:
    install_model(tmp_path, "fake-ok")
    with running(tmp_path) as (client, factory):
        with connect(client) as ws:
            ws.send_json(AUTH)
            send(ws, "begin")
            assert recv(ws) == {"type": "starting", "capture_id": CAPTURE}
            assert recv(ws) == capturing()
            ws.send_bytes(CHUNK)
            assert recv(ws) == {
                "type": "partial",
                "capture_id": CAPTURE,
                "segment": 0,
                "revision": 1,
                "text": "partial-1",
            }
            send(ws, "end")
            assert until_closed(ws) == (
                [
                    {"type": "final", "capture_id": CAPTURE, "segment": 0, "text": "final-16000"},
                    {"type": "done", "capture_id": CAPTURE},
                ],
                1000,
            )
        assert a_next_begin_reuses_the_engine(client, factory)  # running: no starting


def test_cancel_closes_1000(tmp_path: Path) -> None:
    install_model(tmp_path, "fake-ok")
    with running(tmp_path) as (client, factory), connect(client) as ws:
        begin(ws)
        send(ws, "cancel")
        send(ws, "cancel")  # a repeat is ignored
        assert until_closed(ws) == ([{"type": "cancelled", "capture_id": CAPTURE}], 1000)


def test_a_second_authenticated_connection_is_busy(tmp_path: Path) -> None:
    install_model(tmp_path, "fake-ok")
    with running(tmp_path) as (client, _), connect(client) as first:
        begin(first)
        with connect(client) as second:  # accepted first, so the browser sees the code
            second.send_json(AUTH)
            assert until_closed(second) == ([], 1013)


def test_unauthenticated_connection_does_not_hold_the_slot(tmp_path: Path) -> None:
    install_model(tmp_path, "fake-ok")
    with running(tmp_path) as (client, _), connect(client) as waiting:
        with connect(client) as ws:
            assert begin(ws) == ["starting", "capturing"]
        waiting.send_json({**AUTH, "token": "wrong"})
        assert until_closed(waiting) == ([], 4401)  # refused by authenticate, never 1013


def open_unauthenticated(
    client: TestClient, stack: contextlib.ExitStack, count: int
) -> list[WebSocketTestSession]:
    return [stack.enter_context(connect(client)) for _ in range(count)]


def refused_before_accept(client: TestClient) -> int:
    with pytest.raises(WebSocketDisconnect) as refused, connect(client):
        pass
    return refused.value.code


def test_a_ninth_unauthenticated_connection_is_refused_1013(tmp_path: Path) -> None:
    with running(tmp_path) as (client, _), contextlib.ExitStack() as stack:
        open_unauthenticated(client, stack, MAX_UNAUTHENTICATED)
        assert refused_before_accept(client) == 1013


def test_authenticating_frees_a_pre_auth_place(tmp_path: Path) -> None:
    install_model(tmp_path, "fake-ok")
    with running(tmp_path) as (client, _), contextlib.ExitStack() as stack:
        first, *_ = open_unauthenticated(client, stack, MAX_UNAUTHENTICATED)
        assert refused_before_accept(client) == 1013
        begin(first)  # authenticated: its place is free, the capture slot is taken
        with connect(client) as accepted:  # accepted, then refused by the one-capture slot
            accepted.send_json(AUTH)
            assert until_closed(accepted) == ([], 1013)


def test_an_authenticated_capture_holds_no_pre_auth_place(tmp_path: Path) -> None:
    install_model(tmp_path, "fake-ok")
    with running(tmp_path) as (client, _), connect(client) as capture:
        begin(capture)
        with contextlib.ExitStack() as stack:
            open_unauthenticated(client, stack, MAX_UNAUTHENTICATED)  # none is refused
            assert refused_before_accept(client) == 1013


def test_a_foreign_origin_is_refused_before_accept(tmp_path: Path) -> None:
    with (
        running(tmp_path) as (client, _),
        pytest.raises(WebSocketDisconnect) as refused,
        connect(client, origin="https://evil.example"),
    ):
        pass
    assert refused.value.code == 1008


def test_no_auth_is_closed_by_authenticate(tmp_path: Path) -> None:
    install_model(tmp_path, "fake-ok")
    with running(tmp_path) as (client, factory):
        with connect(client) as ws:
            send(ws, "begin")
            assert until_closed(ws) == ([], 4401)
        assert factory.recorder.calls == []


def refused_after_begin(client: TestClient, message: str | bytes) -> tuple[list[Any], int]:
    with connect(client) as ws:
        begin(ws)
        if isinstance(message, bytes):
            ws.send_bytes(message)
        else:
            ws.send_text(message)
        return until_closed(ws)


def test_extra_key_closes_4400(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    install_model(tmp_path, "fake-ok")
    extra = json.dumps({"type": "end", "capture_id": CAPTURE, "extra": 1})
    with running(tmp_path) as (client, factory):
        assert refused_after_begin(client, extra) == ([], 4400)
        assert a_next_begin_reuses_the_engine(client, factory)
    assert "voice socket closed: 4400" in caplog.text


END = json.dumps({"type": "end", "capture_id": CAPTURE})
NON_STRING_TYPES = [("array", []), ("object", {}), ("number", 1), ("null", None)]


@pytest.mark.parametrize(
    "message",
    [
        pytest.param("not json", id="not-json"),
        pytest.param(f'{{"type":"end","type":"end","capture_id":"{CAPTURE}"}}', id="duplicate"),
        pytest.param(json.dumps({"type": "end"}), id="missing-key"),
        pytest.param(json.dumps({"type": "pause", "capture_id": CAPTURE}), id="unknown-type"),
        pytest.param(json.dumps({"type": "end", "capture_id": CAPTURE.upper()}), id="uuid-case"),
        pytest.param(END + " " * MAX_TEXT_BYTES, id="over-1-kib"),
        pytest.param(json.dumps({"type": "begin", "capture_id": CAPTURE}), id="second-begin"),
        pytest.param(json.dumps({"type": "end", "capture_id": OTHER}), id="other-capture"),
        *[
            pytest.param(json.dumps({"type": kind, "capture_id": CAPTURE}), id=f"type-{name}")
            for name, kind in NON_STRING_TYPES
        ],
        pytest.param(b"\x00" * 3, id="odd-audio"),
        pytest.param(b"\x00" * (MAX_AUDIO_BYTES + 2), id="audio-over-16-kib"),
    ],
)
def test_a_bad_message_closes_4400_and_cancels_the_capture(
    tmp_path: Path, message: str | bytes
) -> None:
    install_model(tmp_path, "fake-ok")
    with running(tmp_path) as (client, factory):
        assert refused_after_begin(client, message) == ([], 4400)
        assert a_next_begin_reuses_the_engine(client, factory)


@pytest.mark.parametrize("kind", [kind for _, kind in NON_STRING_TYPES], ids=lambda k: repr(k))
def test_a_non_string_type_before_begin_closes_4400(tmp_path: Path, kind: object) -> None:
    install_model(tmp_path, "fake-ok")
    with running(tmp_path) as (client, factory), connect(client) as ws:
        ws.send_json(AUTH)
        ws.send_json({"type": kind, "capture_id": CAPTURE})
        assert until_closed(ws) == ([], 4400)
        assert factory.recorder.calls == []


def test_audio_before_begin_closes_4400(tmp_path: Path) -> None:
    install_model(tmp_path, "fake-ok")
    with running(tmp_path) as (client, factory):
        with connect(client) as ws:
            ws.send_json(AUTH)
            ws.send_bytes(CHUNK[:3200])
            assert until_closed(ws) == ([], 4400)
        assert factory.recorder.calls == []


def test_a_gap_before_begin_closes_4408(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    with running(tmp_path, receive_gap_s=FAST) as (client, _), connect(client) as ws:
        ws.send_json(AUTH)
        assert until_closed(ws) == ([], 4408)
    assert "voice socket closed: 4408" in caplog.text


def test_a_gap_during_audio_closes_4408(tmp_path: Path) -> None:
    install_model(tmp_path, "fake-ok")
    with running(tmp_path, receive_gap_s=FAST) as (client, factory):
        with connect(client) as ws:
            begin(ws)
            ws.send_bytes(CHUNK)
            messages, code = until_closed(ws)
        assert code == 4408 and {message["type"] for message in messages} <= {"partial"}
        assert a_next_begin_reuses_the_engine(client, factory)


def test_a_missing_done_ends_with_final_timeout(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    install_model(tmp_path, "fake-no-done")
    settings = {"voice.model": "fake-no-done", "voice.backend": "cpu"}
    with running(tmp_path, settings, final_timeout_s=FAST) as (client, factory):
        with connect(client) as ws:
            begin(ws)
            pid = factory.engines[0].pid
            assert pid is not None
            send(ws, "end")
            messages, code = until_closed(ws)
        assert code == 1000
        assert messages[-1] == {"type": "error", "capture_id": CAPTURE, "code": "final_timeout"}
        assert {message["type"] for message in messages[:-1]} <= {"final"}
        assert not factory.engines[0].running and gone(pid) and factory.recorder.all_reaped()
    assert "voice socket closed: final_timeout" in caplog.text


def test_the_capture_cap_stops_with_finals(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(voice_engine, "MAX_CAPTURE_BYTES", 32_000)
    install_model(tmp_path, "fake-ok")
    with running(tmp_path) as (client, _), connect(client) as ws:
        begin(ws)
        for _ in range(3):
            ws.send_bytes(CHUNK)
        messages, code = until_closed(ws)
    assert code == 1000
    assert [message for message in messages if message["type"] != "partial"] == [
        {"type": "stopping", "capture_id": CAPTURE, "reason": "capture_cap"},
        {"type": "final", "capture_id": CAPTURE, "segment": 0, "text": "final-32000"},
        {"type": "done", "capture_id": CAPTURE},
    ]


def test_an_unread_worker_stops_overloaded(tmp_path: Path) -> None:
    install_model(tmp_path, "fake-no-read")
    settings = {"voice.model": "fake-no-read", "voice.backend": "cpu"}
    with (
        running(tmp_path, settings, final_timeout_s=FAST, max_pending_bytes=16_384) as (client, _),
        connect(client) as ws,
    ):
        begin(ws)
        for _ in range(voice_engine.MAX_CAPTURE_BYTES // len(CHUNK)):
            ws.send_bytes(CHUNK)
        assert until_closed(ws) == (
            [
                {"type": "stopping", "capture_id": CAPTURE, "reason": "overloaded"},
                {"type": "error", "capture_id": CAPTURE, "code": "final_timeout"},
            ],
            1000,
        )


def test_disconnect_cancels_the_capture(tmp_path: Path) -> None:
    install_model(tmp_path, "fake-ok")
    with running(tmp_path) as (client, factory):
        with connect(client) as ws:
            begin(ws)
            ws.send_bytes(CHUNK)
            assert recv(ws)["type"] == "partial"
        assert a_next_begin_reuses_the_engine(client, factory)


def test_no_model_is_setup_needed(tmp_path: Path) -> None:
    with running(tmp_path) as (client, factory):
        with connect(client) as ws:
            ws.send_json(AUTH)
            send(ws, "begin")
            assert until_closed(ws) == (
                [
                    {"type": "starting", "capture_id": CAPTURE},
                    {"type": "error", "capture_id": CAPTURE, "code": "setup_needed"},
                ],
                1000,
            )
        assert factory.recorder.calls == []


def test_a_capture_writes_no_file_and_logs_no_text(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    install_model(tmp_path, "fake-ok")
    before = tree(tmp_path)
    with running(tmp_path) as (client, _), connect(client) as ws:
        begin(ws)
        ws.send_bytes(CHUNK)
        send(ws, "end")
        messages, code = until_closed(ws)
    assert code == 1000 and [message["type"] for message in messages] == [
        "partial",
        "final",
        "done",
    ]
    assert tree(tmp_path) == before
    for text in ("partial-", "final-", CAPTURE):
        assert text not in caplog.text
