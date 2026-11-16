"""voice_api: the dictation REST routes over a fake service, in a minimal app (no middleware)."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, cast

import httpx
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.testclient import TestClient
from starlette.websockets import WebSocket, WebSocketDisconnect

from viki_agent.voice.voice_api import MAX_VOICE_REQUESTS, voice_routes
from viki_agent.voice.voice_service import VoiceService

HANG_GUARD_S = 10
ORIGIN = "http://127.0.0.1:8765"
BEARER = {"authorization": "Bearer t"}
JSON = "application/json"
VIEW = {"state": "ready", "running": False}


class FakeService:
    """The two calls the routes make; `gate`, when set, holds each call until it is set."""

    def __init__(self) -> None:
        self.cleared = 0
        self.entered = 0
        self.gate: asyncio.Event | None = None
        self.failure: Exception | None = None

    async def _call(self) -> dict[str, object]:
        self.entered += 1
        if self.failure is not None:
            raise self.failure
        if self.gate is not None:
            await self.gate.wait()
        return VIEW

    async def status(self) -> dict[str, object]:
        return await self._call()

    async def clear_fallback(self) -> dict[str, object]:
        self.cleared += 1
        return await self._call()


async def refuse_auth(websocket: WebSocket) -> bool:
    await websocket.close(code=4401)
    return False


def build(service: FakeService, workers: set[asyncio.Task[Any]] | None = None) -> Starlette:
    def authorized(request: Request) -> bool:
        return request.headers.get("authorization") == BEARER["authorization"]

    routes = voice_routes(
        cast(VoiceService, service),
        authorized=authorized,
        origins=frozenset({ORIGIN}),
        workers=set() if workers is None else workers,
        authenticate=refuse_auth,
    )
    return Starlette(routes=routes)


def test_status_has_its_shape_and_is_not_cached() -> None:
    with TestClient(build(FakeService())) as client:
        response = client.get("/api/v2/voice", headers=BEARER)
    assert (response.status_code, response.json()) == (200, {"voice": VIEW})
    assert response.headers["cache-control"] == "no-store"


def test_both_routes_need_the_bearer() -> None:
    with TestClient(build(FakeService())) as client:
        for headers in ({}, {"authorization": "Bearer other"}, {"authorization": "t"}):
            for response in (
                client.get("/api/v2/voice", headers=headers),
                client.post("/api/v2/voice/fallback/clear", json={}, headers=headers),
            ):
                assert response.status_code == 401
                assert response.json()["error"]["code"] == "unauthorized"
                assert response.headers["cache-control"] == "no-store"


def test_a_get_may_carry_no_origin_but_a_post_needs_a_listed_one() -> None:
    with TestClient(build(FakeService())) as client:
        assert client.get("/api/v2/voice", headers=BEARER).status_code == 200
        foreign = {**BEARER, "origin": "https://evil.example"}
        assert client.get("/api/v2/voice", headers=foreign).status_code == 403
        clear = "/api/v2/voice/fallback/clear"
        assert client.post(clear, json={}, headers=BEARER).status_code == 403
        assert client.post(clear, json={}, headers=foreign).status_code == 403
        listed = {**BEARER, "origin": ORIGIN}
        assert client.post(clear, json={}, headers=listed).status_code == 200


def test_a_query_is_refused() -> None:
    service = FakeService()
    with TestClient(build(service)) as client:
        for response in (
            client.get("/api/v2/voice?x=1", headers=BEARER),
            client.post(
                "/api/v2/voice/fallback/clear?x=1", json={}, headers={**BEARER, "origin": ORIGIN}
            ),
        ):
            assert response.status_code == 400
            assert response.json()["error"]["code"] == "invalid_query"
    assert service.entered == 0


@pytest.mark.parametrize(
    "request_args",
    [
        {"json": {"a": 1}},
        {"json": []},
        {"content": b"{", "headers": {"content-type": JSON}},
        {"content": b"{}", "headers": {"content-type": "text/plain"}},
        {"content": b'{"x":' + b"1" * 2_000 + b"}", "headers": {"content-type": JSON}},
    ],
)
def test_the_clear_body_must_be_an_empty_object(request_args: dict[str, Any]) -> None:
    service = FakeService()
    headers = {**BEARER, "origin": ORIGIN, **request_args.pop("headers", {})}
    with TestClient(build(service)) as client:
        response = client.post("/api/v2/voice/fallback/clear", headers=headers, **request_args)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_body"
    assert service.cleared == 0


def test_clear_calls_the_service_and_answers_the_view() -> None:
    service = FakeService()
    with TestClient(build(service)) as client:
        response = client.post(
            "/api/v2/voice/fallback/clear", json={}, headers={**BEARER, "origin": ORIGIN}
        )
    assert (response.status_code, response.json()) == (200, {"voice": VIEW})
    assert service.cleared == 1


def test_a_failure_answers_503_without_its_text(caplog: pytest.LogCaptureFixture) -> None:
    service = FakeService()
    service.failure = RuntimeError("secret detail")
    with TestClient(build(service)) as client, caplog.at_level(logging.WARNING):
        response = client.get("/api/v2/voice", headers=BEARER)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "unavailable"
    assert "secret detail" not in response.text + caplog.text
    assert "RuntimeError" in caplog.text


async def test_a_fifth_request_in_flight_is_overloaded() -> None:
    service = FakeService()
    service.gate = asyncio.Event()
    workers: set[asyncio.Task[Any]] = set()
    transport = httpx.ASGITransport(app=build(service, workers))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        held = [
            asyncio.create_task(client.get("/api/v2/voice", headers=BEARER))
            for _ in range(MAX_VOICE_REQUESTS)
        ]
        try:
            async with asyncio.timeout(HANG_GUARD_S):
                while service.entered < MAX_VOICE_REQUESTS:
                    await asyncio.sleep(0)
            response = await client.get("/api/v2/voice", headers=BEARER)
            assert response.status_code == 429
            assert response.json()["error"]["code"] == "overloaded"
        finally:
            service.gate.set()
        answers = await asyncio.gather(*held)
        assert [answer.status_code for answer in answers] == [200] * MAX_VOICE_REQUESTS
    await asyncio.gather(*workers)
    assert workers == set()


def test_the_socket_route_runs_the_given_authenticate() -> None:
    with (
        TestClient(build(FakeService())) as client,
        client.websocket_connect("/ws/voice") as ws,
        pytest.raises(WebSocketDisconnect) as refused,
    ):
        ws.receive_json()
    assert refused.value.code == 4401
