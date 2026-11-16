"""voice_api's hands-free report route (`/api/v2/voice/handsfree/report`), and the Voicebox
speech route's own refusal log line."""

from __future__ import annotations

import logging
from typing import Any, cast

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.testclient import TestClient
from starlette.websockets import WebSocket

from tests.test_server import BEARER, ORIGIN
from viki_agent.voice.voice_api import voice_routes
from viki_agent.voice.voice_service import VoiceService
from viki_agent.voice.voicebox import Voicebox
from viki_agent.voice.voicebox_http import VoiceboxError

REPORT_PATH = "/api/v2/voice/handsfree/report"
SPEECH_PATH = "/api/v2/voice/voicebox/speech"
POST_HEADERS = {**BEARER, "origin": ORIGIN}


class FakeService:
    """The dictation service `voice_routes` always requires; unused by these tests."""

    async def status(self) -> dict[str, object]:
        return {"state": "ready"}


class FakeVoicebox:
    """Only `speech` is exercised, to prove a refusal logs its code and not the text."""

    def __init__(self) -> None:
        self.answer: bytes | VoiceboxError = b"RIFF-fake-wav-bytes"

    async def status(self) -> dict[str, object]:
        return {"state": "ready", "gpu": None, "profiles": []}

    async def speech(self, text: str, language: str) -> bytes:
        if isinstance(self.answer, VoiceboxError):
            raise self.answer
        return self.answer


async def refuse_auth(websocket: WebSocket) -> bool:
    await websocket.close(code=4401)
    return False


def build(voicebox: FakeVoicebox | None = None) -> Starlette:
    def authorized(request: Request) -> bool:
        return request.headers.get("authorization") == BEARER["authorization"]

    routes = voice_routes(
        cast(VoiceService, FakeService()),
        authorized=authorized,
        origins=frozenset({ORIGIN}),
        workers=set(),
        authenticate=refuse_auth,
        voicebox=cast(Voicebox, voicebox) if voicebox is not None else None,
    )
    return Starlette(routes=routes)


# --- POST /api/v2/voice/handsfree/report ------------------------------------------------------


@pytest.mark.parametrize(
    ("event", "code"),
    [
        ("dropped", "voicebox_off"),  # a Voicebox code (voicebox_http.CODES)
        ("failed", "unauthorized"),  # a plain refusal code (_STATUS, not in CODES)
        ("fallback", "audio_failed"),  # the frontend's own hands-free reason strings
        ("silent", "esc"),
        ("dropped", "view"),
        ("dropped", "mic"),  # a mic press that cancels a still-preparing voice
    ],
)
def test_a_valid_report_logs_one_warning(
    event: str, code: str, caplog: pytest.LogCaptureFixture
) -> None:
    with TestClient(build()) as client, caplog.at_level(logging.WARNING):
        response = client.post(
            REPORT_PATH, json={"event": event, "code": code}, headers=POST_HEADERS
        )
    assert (response.status_code, response.json()) == (200, {"ok": True})
    assert response.headers["cache-control"] == "no-store"
    assert caplog.text.count("hands-free") == 1
    assert f"hands-free {event}: {code}" in caplog.text


@pytest.mark.parametrize(
    "body",
    [
        {"event": "nope", "code": "esc"},  # unknown event
        {"event": "dropped", "code": "nope"},  # unknown code
        {"event": "dropped"},  # missing code
        {"code": "esc"},  # missing event
        {"event": "dropped", "code": "esc", "extra": 1},  # extra key
        {"event": 1, "code": "esc"},  # wrong type
        {"event": "dropped", "code": None},  # wrong type
        {"event": "paused", "code": "view"},  # unknown event
        {"event": "dropped", "code": "devicechange"},  # unknown code
    ],
)
def test_an_unknown_code_is_refused_and_not_logged(
    body: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    with TestClient(build()) as client, caplog.at_level(logging.WARNING):
        response = client.post(REPORT_PATH, json=body, headers=POST_HEADERS)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_body"
    assert "hands-free" not in caplog.text


def test_report_needs_the_bearer() -> None:
    body = {"event": "dropped", "code": "esc"}
    with TestClient(build()) as client:
        for headers in ({}, {"authorization": "Bearer other"}):
            response = client.post(REPORT_PATH, json=body, headers=headers)
            assert response.status_code == 401
            assert response.json()["error"]["code"] == "unauthorized"


def test_report_needs_a_listed_origin() -> None:
    body = {"event": "dropped", "code": "esc"}
    with TestClient(build()) as client:
        assert client.post(REPORT_PATH, json=body, headers=BEARER).status_code == 403
        foreign = {**BEARER, "origin": "https://evil.example"}
        assert client.post(REPORT_PATH, json=body, headers=foreign).status_code == 403
        assert client.post(REPORT_PATH, json=body, headers=POST_HEADERS).status_code == 200


# --- POST /api/v2/voice/voicebox/speech: the refusal log line ---------------------------------


def test_a_refused_speech_logs_its_code_and_not_its_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    voicebox = FakeVoicebox()
    voicebox.answer = VoiceboxError("voicebox_unreachable")
    with TestClient(build(voicebox)) as client, caplog.at_level(logging.WARNING):
        response = client.post(
            SPEECH_PATH,
            json={"text": "a very secret sentence", "language": "en"},
            headers=POST_HEADERS,
        )
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "voicebox_unreachable"
    assert "voice speech refused: voicebox_unreachable" in caplog.text
    assert "a very secret sentence" not in caplog.text
