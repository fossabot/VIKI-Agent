"""Serves GET /api/v2/between-chats: refusals in a fixed order, at most MAX_REQUESTS reads at a
time, a caller going away keeps its permit, the scan's own body passes straight through, and
nothing is logged."""

from __future__ import annotations

import asyncio
import logging
import queue
import threading
from pathlib import Path
from typing import Any

import httpx
import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from tests.test_server import BEARER, running
from viki_agent.conversations.between_chats_api import MAX_REQUESTS, between_chats_routes

TOKEN = "route-token-7f3a"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
ORIGIN = "http://127.0.0.1:8765"
HANG_GUARD_S = 5  # a guard on an event that normally fires in milliseconds
PATH = "/api/v2/between-chats"


class Gated:
    """A `scan` that blocks while `gate` is clear, announcing each wait, and otherwise answers
    `result`."""

    def __init__(self, result: dict[str, object]) -> None:
        self.result = result
        self.gate = threading.Event()
        self.gate.set()
        self.waiting: queue.Queue[None] = queue.Queue()

    def __call__(self) -> dict[str, object]:
        if not self.gate.is_set():
            self.waiting.put(None)
            self.gate.wait(HANG_GUARD_S)
        return self.result


def _app(scan: Any, workers: set[asyncio.Task[Any]] | None = None) -> Starlette:
    routes = between_chats_routes(
        scan,
        authorized=lambda request: request.headers.get("authorization") == f"Bearer {TOKEN}",
        origins=frozenset({ORIGIN}),
        workers=set() if workers is None else workers,
    )
    return Starlette(routes=routes)


def test_body_passes_through_untouched() -> None:
    """The route neither inspects nor reshapes `scan`'s result: whatever it returns is the
    response body, verbatim."""
    body = {"messages": [{"at": "x", "from_name": "y"}], "partial": True, "extra": [1, 2, None]}
    with TestClient(_app(lambda: body)) as client:
        response = client.get(PATH, headers=AUTH)
    assert response.status_code == 200 and response.json() == body
    assert response.headers["cache-control"] == "no-store"


def test_an_oserror_from_the_scan_is_unavailable() -> None:
    def boom() -> dict[str, object]:
        raise OSError("disk gone")

    with TestClient(_app(boom)) as client:
        response = client.get(PATH, headers=AUTH)
    assert (response.status_code, response.json()["error"]["code"]) == (503, "unavailable")
    assert response.headers["cache-control"] == "no-store"


def test_nothing_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="viki_agent")

    def boom() -> dict[str, object]:
        raise OSError("disk gone")

    with TestClient(_app(boom)) as client:
        client.get(PATH, headers=AUTH)
        client.get(PATH + "?q=1", headers=AUTH)
        client.get(PATH, headers={})
        client.get(PATH, headers={**AUTH, "Origin": "http://evil.invalid"})
    assert not caplog.records


async def test_refusals() -> None:
    gated = Gated({"messages": [], "partial": False})
    workers: set[asyncio.Task[Any]] = set()
    foreign = {**AUTH, "Origin": "http://evil.invalid"}
    refused = [
        ({}, 401, "unauthorized"),
        ({"Authorization": "Bearer other"}, 401, "unauthorized"),
        (foreign, 403, "forbidden_origin"),
    ]
    transport = httpx.ASGITransport(app=_app(gated, workers))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        for headers, status, code in refused:
            response = await client.get(PATH, headers=headers)
            assert (response.status_code, response.json()["error"]["code"]) == (status, code)
            assert response.headers["cache-control"] == "no-store"
        bad_input = await client.get(PATH + "?q=1", headers=AUTH)
        assert (bad_input.status_code, bad_input.json()["error"]["code"]) == (400, "invalid_input")
        assert bad_input.headers["cache-control"] == "no-store"
        for headers in (AUTH, {**AUTH, "Origin": ORIGIN}):  # no Origin, or the app's own
            answer = await client.get(PATH, headers=headers)
            assert answer.status_code == 200 and answer.headers["cache-control"] == "no-store"

        gated.gate.clear()
        calls = [asyncio.create_task(client.get(PATH, headers=AUTH)) for _ in range(MAX_REQUESTS)]
        for _ in range(MAX_REQUESTS):
            await asyncio.to_thread(gated.waiting.get, timeout=HANG_GUARD_S)
        busy = await client.get(PATH, headers=AUTH)
        assert (busy.status_code, busy.json()["error"]["code"]) == (429, "overloaded")
        assert busy.headers["cache-control"] == "no-store"
        # A caller that goes away keeps its permit: the shielded scan runs on, tracked.
        calls[0].cancel()
        with pytest.raises(asyncio.CancelledError):
            await calls[0]
        assert len(workers) == MAX_REQUESTS
        assert (await client.get(PATH, headers=AUTH)).status_code == 429
        gated.gate.set()
        async with asyncio.timeout(HANG_GUARD_S):
            answers = await asyncio.gather(*calls[1:])
            await asyncio.gather(*list(workers))
        assert [answer.status_code for answer in answers] == [200] * (MAX_REQUESTS - 1)
        await asyncio.sleep(0)  # the done callbacks run on the next loop pass
        assert not workers
        assert (await client.get(PATH, headers=AUTH)).status_code == 200


def test_the_route_is_registered(tmp_path: Path) -> None:
    with running(tmp_path) as app:
        response = app.client.get(PATH, headers=BEARER)
    assert response.status_code == 200
    assert response.json() == {"messages": [], "partial": False}
