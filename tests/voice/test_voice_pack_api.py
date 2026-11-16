"""voice_api's pack routes (`/api/v2/voice/pack*`), the `warm` action, and the Windows
speech-settings opener route, over fakes."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any, cast

import httpx
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.testclient import TestClient
from starlette.websockets import WebSocket

from tests.test_server import BEARER, ORIGIN
from viki_agent.voice.voice_api import voice_routes
from viki_agent.voice.voice_pack_install import PackRefused
from viki_agent.voice.voice_pack_service import VoicePack
from viki_agent.voice.voice_service import VoiceService
from viki_agent.voice.voicebox import Voicebox
from viki_agent.voice.voicebox_http import VoiceboxError

HANG_GUARD_S = 10
POST_HEADERS = {**BEARER, "origin": ORIGIN}
STATUS_VIEW = {"state": "not_installed"}
WARM_PATH = "/api/v2/voice/pack/warm"
REPORT_PATH = "/api/v2/voice/handsfree/report"
_ACTIONS = ("check", "install", "repair", "cancel", "remove", "retry")


class FakeService:
    """The dictation service `voice_routes` always requires; unused by any test here."""

    async def status(self) -> dict[str, object]:
        return {"state": "ready"}


class FakePack:
    def __init__(self) -> None:
        self.status_view: dict[str, object] = dict(STATUS_VIEW)
        self.calls: list[str] = []
        self.refusal: PackRefused | None = None
        self.fallback_calls: list[str] = []

    async def status(self) -> dict[str, object]:
        return self.status_view

    def note_fallback(self, code: str) -> None:
        self.fallback_calls.append(code)
        self.status_view = {**self.status_view, "fallbacks": [{"code": code}]}

    async def _perform(self, name: str) -> dict[str, object]:
        self.calls.append(name)
        if self.refusal is not None:
            raise self.refusal
        return self.status_view

    async def check(self) -> dict[str, object]:
        return await self._perform("check")

    async def install(self) -> dict[str, object]:
        return await self._perform("install")

    async def repair(self) -> dict[str, object]:
        return await self._perform("repair")

    async def cancel(self) -> dict[str, object]:
        return await self._perform("cancel")

    async def remove(self) -> dict[str, object]:
        return await self._perform("remove")

    async def retry(self) -> dict[str, object]:
        return await self._perform("retry")


class FakeWarmBox:
    """The `voicebox` side of a warm call: only `engine()` and `warm()` are ever touched."""

    def __init__(self, engine: str = "pack") -> None:
        self._engine = engine
        self.warm_calls = 0
        self.gate: asyncio.Event | None = None
        self.error: VoiceboxError | None = None

    def engine(self) -> str:
        return self._engine

    async def warm(self) -> None:
        self.warm_calls += 1
        if self.gate is not None:
            await self.gate.wait()
        if self.error is not None:
            raise self.error


async def refuse_auth(websocket: WebSocket) -> bool:
    await websocket.close(code=4401)
    return False


def build(
    *,
    pack: FakePack | None = None,
    voicebox: FakeWarmBox | None = None,
    opener: Callable[[str], None] | None = None,
    platform: str = "win32",
    workers: set[asyncio.Task[Any]] | None = None,
) -> Starlette:
    def authorized(request: Request) -> bool:
        return request.headers.get("authorization") == BEARER["authorization"]

    routes = voice_routes(
        cast(VoiceService, FakeService()),
        authorized=authorized,
        origins=frozenset({ORIGIN}),
        workers=set() if workers is None else workers,
        authenticate=refuse_auth,
        voicebox=cast(Voicebox, voicebox) if voicebox is not None else None,
        pack=cast(VoicePack, pack) if pack is not None else None,
        opener=opener,
        platform=platform,
    )
    return Starlette(routes=routes)


# --- GET /api/v2/voice/pack ---------------------------------------------------------------------


def test_pack_status_has_its_shape_and_is_not_cached() -> None:
    pack = FakePack()
    with TestClient(build(pack=pack)) as client:
        response = client.get("/api/v2/voice/pack", headers=BEARER)
    assert (response.status_code, response.json()) == (200, {"pack": pack.status_view})
    assert response.headers["cache-control"] == "no-store"


def test_pack_status_query_is_refused() -> None:
    with TestClient(build(pack=FakePack())) as client:
        response = client.get("/api/v2/voice/pack?x=1", headers=BEARER)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_query"


def test_pack_routes_not_registered_without_pack() -> None:
    with TestClient(build()) as client:
        assert client.get("/api/v2/voice/pack", headers=BEARER).status_code == 404


def test_pack_status_route_never_spawns_warm() -> None:
    pack = FakePack()
    pack.status_view = {"state": "ready"}
    box = FakeWarmBox(engine="pack")
    workers: set[asyncio.Task[Any]] = set()
    with TestClient(build(pack=pack, voicebox=box, workers=workers)) as client:
        for _ in range(3):
            assert client.get("/api/v2/voice/pack", headers=BEARER).status_code == 200
    assert box.warm_calls == 0
    assert workers == set()


def test_fallback_reports_reach_pack_status() -> None:
    """A hands-free `fallback` report calls `pack.note_fallback`, and a later GET reflects it; any
    other event leaves the pack untouched."""
    pack = FakePack()
    with TestClient(build(pack=pack)) as client:
        other = client.post(
            REPORT_PATH, json={"event": "dropped", "code": "esc"}, headers=POST_HEADERS
        )
        assert other.status_code == 200
        assert pack.fallback_calls == []

        report = client.post(
            REPORT_PATH,
            json={"event": "fallback", "code": "voicebox_timeout"},
            headers=POST_HEADERS,
        )
        assert report.status_code == 200
        status = client.get("/api/v2/voice/pack", headers=BEARER).json()["pack"]
    assert pack.fallback_calls == ["voicebox_timeout"]
    assert status["fallbacks"] == [{"code": "voicebox_timeout"}]


# --- the Bearer and Origin checks every route shares ---------------------------------------------


def test_pack_routes_need_the_bearer() -> None:
    with TestClient(build(pack=FakePack())) as client:
        for headers in ({}, {"authorization": "Bearer other"}):
            responses = [client.get("/api/v2/voice/pack", headers=headers)]
            responses += [
                client.post(f"/api/v2/voice/pack/{name}", json={}, headers=headers)
                for name in (*_ACTIONS, "warm")
            ]
            for response in responses:
                assert response.status_code == 401
                assert response.json()["error"]["code"] == "unauthorized"


def test_post_needs_origin() -> None:
    pack = FakePack()
    with TestClient(build(pack=pack, opener=lambda _target: None)) as client:
        for path in (
            "/api/v2/voice/pack/check",
            WARM_PATH,
            "/api/v2/voice/speech-settings",
        ):
            assert client.post(path, json={}, headers=BEARER).status_code == 403
            foreign = {**BEARER, "origin": "https://evil.example"}
            assert client.post(path, json={}, headers=foreign).status_code == 403
            assert client.post(path, json={}, headers=POST_HEADERS).status_code == 200


# --- the six installer actions --------------------------------------------------------------------


@pytest.mark.parametrize("name", _ACTIONS)
def test_pack_actions_call_the_facade(name: str) -> None:
    pack = FakePack()
    with TestClient(build(pack=pack)) as client:
        response = client.post(f"/api/v2/voice/pack/{name}", json={}, headers=POST_HEADERS)
    assert (response.status_code, response.json()) == (200, {"pack": pack.status_view})
    assert pack.calls == [name]


def test_pack_action_bad_body_refused() -> None:
    pack = FakePack()
    with TestClient(build(pack=pack)) as client:
        response = client.post(
            "/api/v2/voice/pack/install", json={"extra": 1}, headers=POST_HEADERS
        )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_body"
    assert pack.calls == []


_PACK_REFUSALS = {
    "pack_unsupported": (409, "Natural voices can't run on this computer."),
    "pack_busy": (409, "The voice pack is busy. Try again when it finishes."),
}


@pytest.mark.parametrize("code", sorted(_PACK_REFUSALS))
def test_pack_action_refusals_map_to_fixed_status_and_message(code: str) -> None:
    pack = FakePack()
    pack.refusal = PackRefused(code)
    with TestClient(build(pack=pack)) as client:
        response = client.post("/api/v2/voice/pack/install", json={}, headers=POST_HEADERS)
    status, message = _PACK_REFUSALS[code]
    assert (response.status_code, response.json()["error"]) == (
        status,
        {"code": code, "message": message},
    )


# --- warm ---------------------------------------------------------------------------------------


def test_warm_skips_other_engines() -> None:
    pack = FakePack()
    pack.status_view = {"state": "stopped"}
    box = FakeWarmBox(engine="voicebox")
    with TestClient(build(pack=pack, voicebox=box)) as client:
        response = client.post(WARM_PATH, json={}, headers=POST_HEADERS)
    assert response.status_code == 200
    assert box.warm_calls == 0


@pytest.mark.parametrize("state", ["not_installed", "installing", "failed", "removing"])
def test_warm_skips_disallowed_states(state: str) -> None:
    pack = FakePack()
    pack.status_view = {"state": state}
    box = FakeWarmBox(engine="pack")
    with TestClient(build(pack=pack, voicebox=box)) as client:
        response = client.post(WARM_PATH, json={}, headers=POST_HEADERS)
    assert response.status_code == 200
    assert box.warm_calls == 0


def test_warm_skips_without_a_voicebox() -> None:
    pack = FakePack()
    pack.status_view = {"state": "ready"}
    with TestClient(build(pack=pack)) as client:
        response = client.post(WARM_PATH, json={}, headers=POST_HEADERS)
    assert response.status_code == 200


async def test_warm_does_not_duplicate_while_live() -> None:
    """Two warm POSTs while the first is still running start exactly one `voicebox.warm()`."""
    pack = FakePack()
    pack.status_view = {"state": "stopped"}
    box = FakeWarmBox(engine="pack")
    box.gate = asyncio.Event()
    workers: set[asyncio.Task[Any]] = set()
    transport = httpx.ASGITransport(app=build(pack=pack, voicebox=box, workers=workers))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post(WARM_PATH, json={}, headers=POST_HEADERS)
        assert first.status_code == 200
        async with asyncio.timeout(HANG_GUARD_S):
            while box.warm_calls < 1:
                await asyncio.sleep(0)
        second = await client.post(WARM_PATH, json={}, headers=POST_HEADERS)
        assert second.status_code == 200
        box.gate.set()
        async with asyncio.timeout(HANG_GUARD_S):
            while workers:
                await asyncio.sleep(0)
    assert box.warm_calls == 1


async def test_warm_task_ignores_a_voicebox_error() -> None:
    pack = FakePack()
    pack.status_view = {"state": "ready"}
    box = FakeWarmBox(engine="pack")
    box.error = VoiceboxError("voicebox_pack_failed")
    workers: set[asyncio.Task[Any]] = set()
    transport = httpx.ASGITransport(app=build(pack=pack, voicebox=box, workers=workers))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(WARM_PATH, json={}, headers=POST_HEADERS)
        assert response.status_code == 200
        async with asyncio.timeout(HANG_GUARD_S):
            while workers:
                await asyncio.sleep(0)
    assert box.warm_calls == 1


async def test_a_failed_warm_logs_its_code(caplog: pytest.LogCaptureFixture) -> None:
    """The warm task logs the VoiceboxError's code, not its text."""
    pack = FakePack()
    pack.status_view = {"state": "ready"}
    box = FakeWarmBox(engine="pack")
    box.error = VoiceboxError("voicebox_pack_failed")
    workers: set[asyncio.Task[Any]] = set()
    transport = httpx.ASGITransport(app=build(pack=pack, voicebox=box, workers=workers))
    with caplog.at_level(logging.WARNING):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(WARM_PATH, json={}, headers=POST_HEADERS)
            assert response.status_code == 200
            async with asyncio.timeout(HANG_GUARD_S):
                while workers:
                    await asyncio.sleep(0)
    assert "voice pack warm failed: voicebox_pack_failed" in caplog.text


# --- POST /api/v2/voice/speech-settings -----------------------------------------------------------


def test_speech_settings_not_registered_without_opener() -> None:
    with TestClient(build()) as client:
        response = client.post("/api/v2/voice/speech-settings", json={}, headers=POST_HEADERS)
    assert response.status_code == 404


def test_speech_settings_opens_only_ms_settings_speech_on_windows() -> None:
    calls: list[str] = []
    with TestClient(build(opener=calls.append, platform="win32")) as client:
        response = client.post("/api/v2/voice/speech-settings", json={}, headers=POST_HEADERS)
    assert (response.status_code, response.json()) == (200, {"opened": True})
    assert calls == ["ms-settings:speech"]

    calls.clear()
    with TestClient(build(opener=calls.append, platform="linux")) as client:
        response = client.post("/api/v2/voice/speech-settings", json={}, headers=POST_HEADERS)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "unsupported_platform"
    assert calls == []


def test_speech_settings_needs_empty_body() -> None:
    with TestClient(build(opener=lambda _target: None)) as client:
        response = client.post("/api/v2/voice/speech-settings", json={"x": 1}, headers=POST_HEADERS)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_body"
