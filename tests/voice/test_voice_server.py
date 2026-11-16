"""The dictation routes wired through `create_app`: Bearer, the socket's own authentication, a
capture over the fake worker, and shutdown leaving no worker process."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
from starlette.applications import Starlette
from starlette.websockets import WebSocketDisconnect

from tests.test_server import BEARER, ORIGIN, TOKEN, WS_HEADERS, recv, running
from tests.voice.test_voice_engine import (
    CAPTURE,
    CHUNK,
    FAKE,
    HANG_GUARD_S,
    gone,
    install_model,
    pins,  # noqa: F401 (autouse: the fake's modes as pinned models)
)
from viki_agent import server
from viki_agent.voice import voice_models as vm
from viki_agent.voice.voice_service import VoiceService

AUTH = {"type": "auth", "token": TOKEN, "v": 2}
BEGIN = {"type": "begin", "capture_id": CAPTURE}


@pytest.fixture(autouse=True)
def fake_voice(monkeypatch: pytest.MonkeyPatch) -> None:
    """The CPU ranking names the pinned fake, and the app spawns the fake worker."""
    monkeypatch.setattr(vm, "RANKING", {"cpu": ("fake-ok",), "cuda": (), "metal": ()})
    real = server.create_app

    def create_app(**kwargs: Any) -> Starlette:
        return real(**kwargs, voice_worker=FAKE)

    monkeypatch.setattr(server, "create_app", create_app)


def refused_with(ws: Any) -> int:
    with pytest.raises(WebSocketDisconnect) as closed:
        recv(ws, HANG_GUARD_S)
    return closed.value.code


def test_the_status_needs_the_bearer_and_reports_setup_needed(tmp_path: Path) -> None:
    with running(tmp_path) as app:
        assert app.client.get("/api/v2/voice").status_code == 401
        response = app.client.get("/api/v2/voice", headers=BEARER)
    assert response.status_code == 200
    voice = response.json()["voice"]
    assert (voice["state"], voice["model"], voice["languages"]) == ("setup_needed", None, ["auto"])
    assert response.headers["content-security-policy"] == server.CSP


def test_clear_fallback_answers_the_view(tmp_path: Path) -> None:
    with running(tmp_path) as app:
        response = app.client.post(
            "/api/v2/voice/fallback/clear", json={}, headers={**BEARER, "origin": ORIGIN}
        )
    assert response.status_code == 200
    assert response.json()["voice"]["fallback"] is None


def test_a_foreign_origin_is_refused_before_accept(tmp_path: Path) -> None:
    foreign = {**WS_HEADERS, "origin": "https://evil.example"}
    with (
        running(tmp_path) as app,
        pytest.raises(WebSocketDisconnect) as refused,
        app.client.websocket_connect("/ws/voice", headers=foreign),
    ):
        pass
    assert refused.value.code == 1008


def test_no_auth_within_the_timeout_is_4401(tmp_path: Path) -> None:
    # running() sets auth_timeout_s to 0.05
    with (
        running(tmp_path) as app,
        app.client.websocket_connect("/ws/voice", headers=WS_HEADERS) as ws,
    ):
        assert refused_with(ws) == server.UNAUTHORIZED


def test_a_protocol_version_other_than_the_servers_is_4406(tmp_path: Path) -> None:
    with (
        running(tmp_path) as app,
        app.client.websocket_connect("/ws/voice", headers=WS_HEADERS) as ws,
    ):
        ws.send_json({**AUTH, "v": 1})
        assert refused_with(ws) == server.PROTOCOL_MISMATCH


def test_voice_socket_needs_auth(tmp_path: Path) -> None:
    install_model(tmp_path / "data", "fake-ok")
    with running(tmp_path) as app:
        service = cast(VoiceService, cast(Starlette, app.client.app).state.voice)
        with app.client.websocket_connect("/ws/voice", headers=WS_HEADERS) as ws:
            ws.send_json(BEGIN)  # no auth first
            assert refused_with(ws) == server.UNAUTHORIZED
        with app.client.websocket_connect("/ws/voice", headers=WS_HEADERS) as ws:
            ws.send_json({**AUTH, "token": "wrong"})
            ws.send_json(BEGIN)
            assert refused_with(ws) == server.UNAUTHORIZED
        assert service._engine is None  # no capture reached the service


def test_a_capture_ends_done_and_shutdown_leaves_no_worker(tmp_path: Path) -> None:
    install_model(tmp_path / "data", "fake-ok")
    with running(tmp_path) as app:
        service = cast(VoiceService, cast(Starlette, app.client.app).state.voice)
        with app.client.websocket_connect("/ws/voice", headers=WS_HEADERS) as ws:
            ws.send_json(AUTH)
            ws.send_json(BEGIN)
            assert recv(ws, HANG_GUARD_S) == {"type": "starting", "capture_id": CAPTURE}
            assert recv(ws, HANG_GUARD_S)["type"] == "capturing"
            ws.send_bytes(CHUNK)
            assert recv(ws, HANG_GUARD_S)["type"] == "partial"
            ws.send_json({"type": "end", "capture_id": CAPTURE})
            assert recv(ws, HANG_GUARD_S) == {
                "type": "final",
                "capture_id": CAPTURE,
                "segment": 0,
                "text": "final-16000",
            }
            assert recv(ws, HANG_GUARD_S) == {"type": "done", "capture_id": CAPTURE}
            assert refused_with(ws) == 1000
        assert service._engine is not None and service._engine.pid is not None
        pid = service._engine.pid
        assert not gone(pid)  # the engine stays up, idle, until the idle close or shutdown
    assert gone(pid)
