"""voice_api's voicebox routes: status and speech, over a fake Voicebox, plus the no-settings
case wired through create_app."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.testclient import TestClient
from starlette.websockets import WebSocket

from tests.test_server import BEARER, ORIGIN, running
from viki_agent.voice.voice_api import MAX_VOICE_REQUESTS, voice_routes
from viki_agent.voice.voice_service import VoiceService
from viki_agent.voice.voicebox import Voicebox
from viki_agent.voice.voicebox_http import CODES, MAX_TEXT_CHARS, VoiceboxError

JSON = "application/json"
HANG_GUARD_S = 10
STATUS_PATH = "/api/v2/voice/voicebox"
SPEECH_PATH = "/api/v2/voice/voicebox/speech"
STATUS_VIEW = {"state": "ready", "gpu": True, "profiles": []}
WAV = b"RIFF-fake-wav-bytes"


class FakeService:
    """The dictation service `voice_routes` always requires; only the limiter-isolation test
    below calls `status`, to prove dictation stays up while Voicebox's own limiter is full."""

    async def status(self) -> dict[str, object]:
        return {"state": "ready"}


class FakeVoicebox:
    def __init__(self) -> None:
        self.status_view: dict[str, object] = STATUS_VIEW
        self.answer: bytes | VoiceboxError = WAV
        self.calls: list[tuple[str, str]] = []
        self.gate: asyncio.Event | None = None  # set to hold `speech` open mid-synthesis

    async def status(self) -> dict[str, object]:
        return self.status_view

    async def speech(self, text: str, language: str) -> bytes:
        self.calls.append((text, language))
        if self.gate is not None:
            await self.gate.wait()
        if isinstance(self.answer, VoiceboxError):
            raise self.answer
        return self.answer


async def refuse_auth(websocket: WebSocket) -> bool:
    await websocket.close(code=4401)
    return False


def build(voicebox: FakeVoicebox, workers: set[asyncio.Task[Any]] | None = None) -> Starlette:
    def authorized(request: Request) -> bool:
        return request.headers.get("authorization") == BEARER["authorization"]

    routes = voice_routes(
        cast(VoiceService, FakeService()),
        authorized=authorized,
        origins=frozenset({ORIGIN}),
        workers=set() if workers is None else workers,
        authenticate=refuse_auth,
        voicebox=cast(Voicebox, voicebox),
    )
    return Starlette(routes=routes)


def test_status_has_its_shape_and_is_not_cached() -> None:
    with TestClient(build(FakeVoicebox())) as client:
        response = client.get(STATUS_PATH, headers=BEARER)
    assert (response.status_code, response.json()) == (200, {"voicebox": STATUS_VIEW})
    assert response.headers["cache-control"] == "no-store"


def test_status_query_is_refused() -> None:
    with TestClient(build(FakeVoicebox())) as client:
        response = client.get(STATUS_PATH + "?x=1", headers=BEARER)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_query"
    assert response.headers["cache-control"] == "no-store"


def test_both_routes_need_the_bearer() -> None:
    with TestClient(build(FakeVoicebox())) as client:
        for headers in ({}, {"authorization": "Bearer other"}):
            for response in (
                client.get(STATUS_PATH, headers=headers),
                client.post(SPEECH_PATH, json={"text": "hi", "language": "en"}, headers=headers),
            ):
                assert response.status_code == 401
                assert response.json()["error"]["code"] == "unauthorized"
                assert response.headers["cache-control"] == "no-store"


def test_speech_needs_a_listed_origin() -> None:
    body = {"text": "hi", "language": "en"}
    with TestClient(build(FakeVoicebox())) as client:
        assert client.post(SPEECH_PATH, json=body, headers=BEARER).status_code == 403
        foreign = {**BEARER, "origin": "https://evil.example"}
        assert client.post(SPEECH_PATH, json=body, headers=foreign).status_code == 403
        listed = {**BEARER, "origin": ORIGIN}
        assert client.post(SPEECH_PATH, json=body, headers=listed).status_code == 200


def test_speech_returns_wav() -> None:
    voicebox = FakeVoicebox()
    with TestClient(build(voicebox)) as client:
        response = client.post(
            SPEECH_PATH,
            json={"text": "hello", "language": "ja"},
            headers={**BEARER, "origin": ORIGIN},
        )
    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/wav"
    assert response.content == WAV
    assert response.headers["cache-control"] == "no-store"
    assert voicebox.calls == [("hello", "ja")]


def _sized_body(total_bytes: int) -> bytes:
    """A `{"text": ..., "language": "en"}` JSON object encoded to exactly `total_bytes` bytes."""
    empty = len(json.dumps({"text": "", "language": "en"}).encode())
    body = json.dumps({"text": "a" * (total_bytes - empty), "language": "en"}).encode()
    assert len(body) == total_bytes
    return body


@pytest.mark.parametrize(
    "request_args",
    [
        {"json": {"text": "hi"}},  # missing key
        {"json": {"text": "hi", "language": "en", "extra": 1}},  # extra key
        {"json": {"text": "   ", "language": "en"}},  # blank
        {"json": {"text": "a" * (MAX_TEXT_CHARS + 1), "language": "en"}},  # 501 chars
        {"json": {"text": "hi\x00", "language": "en"}},  # control char
        {"json": {"text": "hi", "language": "xx"}},  # unknown language
        {
            "content": b'{"text": "hi", "language": "en"}',
            "headers": {"content-type": "text/plain"},
        },  # non-JSON type
        {"content": _sized_body(4097), "headers": {"content-type": JSON}},  # too large
    ],
)
def test_bad_speech_bodies_are_refused(request_args: dict[str, Any]) -> None:
    voicebox = FakeVoicebox()
    headers = {**BEARER, "origin": ORIGIN, **request_args.pop("headers", {})}
    with TestClient(build(voicebox)) as client:
        response = client.post(SPEECH_PATH, headers=headers, **request_args)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_body"
    assert voicebox.calls == []


_SPEECH_FAILED = "Voicebox could not make the speech."
_EXPECTED = {
    "voicebox_off": (409, "Choose a Voicebox voice in Settings first."),
    "voicebox_busy": (429, "Voicebox is busy. Try again in a moment."),
    "voicebox_unreachable": (502, _SPEECH_FAILED),
    "voicebox_timeout": (502, _SPEECH_FAILED),
    "voicebox_profile_missing": (502, _SPEECH_FAILED),
    "voicebox_model_missing": (502, _SPEECH_FAILED),
    "voicebox_refused": (502, _SPEECH_FAILED),
    "voicebox_invalid": (502, _SPEECH_FAILED),
    "voicebox_pack_missing": (409, "Natural voices aren't installed."),
    "voicebox_pack_failed": (502, "Natural voices stopped working."),
}


def test_every_voicebox_code_has_its_status_and_fixed_message() -> None:
    assert set(_EXPECTED) == set(CODES)
    for code, (status, message) in _EXPECTED.items():
        voicebox = FakeVoicebox()
        voicebox.answer = VoiceboxError(code)
        with TestClient(build(voicebox)) as client:
            response = client.post(
                SPEECH_PATH,
                json={"text": "hi", "language": "en"},
                headers={**BEARER, "origin": ORIGIN},
            )
        assert (response.status_code, response.json()["error"]) == (
            status,
            {"code": code, "message": message},
        ), code


async def test_voicebox_limiter_is_its_own_and_dictation_stays_available() -> None:
    """The two Voicebox routes get their own CapacityLimiter, separate from dictation's. A
    speech request can wait on Voicebox's one-at-a-time lock for up to SPEECH_TIMEOUT_S; filling
    it must not turn GET /api/v2/voice (dictation, which never touches Voicebox) into a false
    429, and a fifth Voicebox request must still be refused as overloaded."""
    voicebox = FakeVoicebox()
    voicebox.gate = asyncio.Event()
    workers: set[asyncio.Task[Any]] = set()
    transport = httpx.ASGITransport(app=build(voicebox, workers))
    body = {"text": "hi", "language": "en"}
    speech_headers = {**BEARER, "origin": ORIGIN}
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        held = [
            asyncio.create_task(client.post(SPEECH_PATH, json=body, headers=speech_headers))
            for _ in range(MAX_VOICE_REQUESTS)
        ]
        try:
            async with asyncio.timeout(HANG_GUARD_S):
                while len(voicebox.calls) < MAX_VOICE_REQUESTS:
                    await asyncio.sleep(0)
            status_response = await client.get("/api/v2/voice", headers=BEARER)
            assert status_response.status_code == 200
            overloaded = await client.post(SPEECH_PATH, json=body, headers=speech_headers)
            assert overloaded.status_code == 429
            assert overloaded.json()["error"]["code"] == "overloaded"
        finally:
            voicebox.gate.set()
        answers = await asyncio.gather(*held)
        assert [answer.status_code for answer in answers] == [200] * MAX_VOICE_REQUESTS
    await asyncio.gather(*workers)
    assert workers == set()


def test_status_and_speech_without_settings(tmp_path: Path) -> None:
    """Through `running()` (no SettingsService): the real `Voicebox` answers `no_settings` and
    refuses speech with `voicebox_off`."""
    with running(tmp_path) as app:
        status_response = app.client.get(STATUS_PATH, headers=BEARER)
        assert status_response.status_code == 200
        assert status_response.json() == {
            "voicebox": {"state": "no_settings", "gpu": None, "profiles": []}
        }
        speech_response = app.client.post(
            SPEECH_PATH,
            json={"text": "hi", "language": "en"},
            headers={**BEARER, "origin": ORIGIN},
        )
        assert speech_response.status_code == 409
        assert speech_response.json()["error"]["code"] == "voicebox_off"
