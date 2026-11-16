"""The run requests route and the run-stop route: a chat's open tool requests, each joined to its
run, and stopping one of its live runs. Both sit behind the Bearer token and the Origin check,
answer `Cache-Control: no-store`, and log nothing."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx
import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from tests.test_server import BEARER, ORIGIN, TOKEN
from viki_agent.conversations.runs_api import run_routes
from viki_agent.drivers.base import RunStopError

SESSION = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
OTHER_SESSION = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
TOOL_USE = "toolu_01ABCxyz-9"
STAMP = "2026-10-10T00:00:00.000Z"
WRITE = {**BEARER, "Origin": ORIGIN}
HANG_GUARD_S = 5


class Actor:
    """A fake actor: `requests()` answers fixed items; `stop_run` records its calls and can
    raise a scripted `RunStopError`."""

    def __init__(
        self, items: list[Any] | None = None, stop_error: RunStopError | None = None
    ) -> None:
        self._items: list[Any] = [] if items is None else items
        self._stop_error = stop_error
        self.stopped: list[str] = []

    def requests(self) -> list[Any]:
        return self._items

    async def stop_run(self, block_id: str) -> None:
        self.stopped.append(block_id)
        if self._stop_error is not None:
            raise self._stop_error


class Sessions:
    """The open actors: the route asks only `get`."""

    def __init__(self, actors: dict[str, Actor] | None = None) -> None:
        self._actors = actors or {}

    def get(self, session_id: str) -> Actor | None:
        return self._actors.get(session_id)


def _app(sessions: Sessions | None = None) -> Starlette:
    routes = run_routes(
        Sessions() if sessions is None else sessions,
        authorized=lambda request: request.headers.get("authorization") == f"Bearer {TOKEN}",
        origins=frozenset({ORIGIN}),
    )
    return Starlette(routes=routes)


def _requests_path(session_id: str) -> str:
    return f"/api/v2/sessions/{session_id}/requests"


def _stop_path(session_id: str, tool_use_id: str) -> str:
    return f"/api/v2/sessions/{session_id}/runs/{tool_use_id}/stop"


def test_requests_list_open_requests_with_their_run() -> None:
    with_run = {"tool_use_id": TOOL_USE, "run": "task-1", "raised_at": STAMP}
    without_run = {"tool_use_id": "toolu_b", "run": None, "raised_at": STAMP}
    sessions = Sessions({SESSION: Actor(items=[with_run, without_run])})
    with TestClient(_app(sessions)) as client:
        response = client.get(_requests_path(SESSION), headers=BEARER)
    assert response.status_code == 200
    assert response.json() == {"requests": [with_run, without_run]}
    assert response.headers["cache-control"] == "no-store"


def test_requests_drops_off_shape_items_and_caps_at_64() -> None:
    good = {"tool_use_id": TOOL_USE, "run": None, "raised_at": STAMP}
    off_shape: list[Any] = [
        "not-a-dict",
        {"tool_use_id": "bad!id", "run": None, "raised_at": STAMP},  # id off TOOL_USE_ID
        {"tool_use_id": TOOL_USE, "run": 7, "raised_at": STAMP},  # run not str|null
        {"tool_use_id": TOOL_USE, "run": "bad!id", "raised_at": STAMP},  # run off TOOL_USE_ID
        {"tool_use_id": TOOL_USE, "run": None, "raised_at": "not-a-stamp"},
        # non-ASCII digits: \d is unicode-greedy without re.ASCII
        {"tool_use_id": TOOL_USE, "run": None, "raised_at": "२०२६-10-10T00:00:00.000Z"},
        {
            "tool_use_id": TOOL_USE,
            "run": None,
            "raised_at": "2026-99-99T99:99:99.999Z",
        },  # no such date
        {"tool_use_id": TOOL_USE, "run": None},  # raised_at missing
    ]
    many_good = [
        {"tool_use_id": f"toolu_{i:03d}", "run": None, "raised_at": STAMP} for i in range(70)
    ]
    sessions = Sessions({SESSION: Actor(items=[good, *off_shape, *many_good])})
    with TestClient(_app(sessions)) as client:
        response = client.get(_requests_path(SESSION), headers=BEARER)
    kept = response.json()["requests"]
    assert len(kept) == 64
    assert kept[0] == good
    assert [item["tool_use_id"] for item in kept[1:]] == [f"toolu_{i:03d}" for i in range(63)]


def test_requests_and_stop_404_without_an_actor() -> None:
    with TestClient(_app()) as client:
        got = client.get(_requests_path(SESSION), headers=BEARER)
        stopped = client.post(_stop_path(SESSION, TOOL_USE), headers=WRITE)
    for response in (got, stopped):
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"
        assert response.headers["cache-control"] == "no-store"


def test_gates_run_in_order() -> None:
    foreign = "http://evil.invalid"
    sessions = Sessions({SESSION: Actor()})
    refusals = [
        # The token is checked first, whatever else is wrong with the request.
        ("GET", _requests_path(SESSION) + "?q=1", {"Origin": foreign}, 401, "unauthorized"),
        ("POST", _stop_path(SESSION, TOOL_USE), {"Origin": foreign}, 401, "unauthorized"),
        # Then the Origin, before the request's shape is even looked at.
        (
            "GET",
            _requests_path(SESSION) + "?q=1",
            {**BEARER, "Origin": foreign},
            403,
            "forbidden_origin",
        ),
        ("POST", _stop_path(SESSION, TOOL_USE), BEARER, 403, "forbidden_origin"),  # names none
        (
            "POST",
            _stop_path(SESSION, TOOL_USE),
            {**BEARER, "Origin": foreign},
            403,
            "forbidden_origin",
        ),
        # Then the shape: a query string, a non-canonical id, or (stop only) a bad tool-use id.
        ("GET", _requests_path(SESSION) + "?q=1", BEARER, 400, "invalid_input"),
        ("POST", _stop_path(SESSION, TOOL_USE) + "?q=1", WRITE, 400, "invalid_input"),
        ("GET", _requests_path(SESSION.upper()), BEARER, 400, "invalid_input"),
        ("POST", _stop_path(SESSION, "bad!id"), WRITE, 400, "invalid_input"),
        ("POST", _stop_path(SESSION.upper(), TOOL_USE), WRITE, 400, "invalid_input"),
    ]
    with TestClient(_app(sessions)) as client:
        for method, url, headers, status, code in refusals:
            response = client.request(method, url, headers=headers)
            assert (response.status_code, response.json()["error"]["code"]) == (
                status,
                code,
            ), (method, url)
            assert response.headers["cache-control"] == "no-store"
        # A read needs no Origin; a write from the app's own Origin is admitted.
        assert client.get(_requests_path(SESSION), headers=BEARER).status_code == 200
        assert client.post(_stop_path(SESSION, TOOL_USE), headers=WRITE).status_code == 200


def test_stop_with_a_body_is_invalid() -> None:
    actor = Actor()
    sessions = Sessions({SESSION: actor})
    with TestClient(_app(sessions)) as client:
        with_length = client.post(_stop_path(SESSION, TOOL_USE), headers=WRITE, content=b"{}")
        with_chunked = client.post(
            _stop_path(SESSION, TOOL_USE), headers={**WRITE, "Transfer-Encoding": "chunked"}
        )
    for response in (with_length, with_chunked):
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_input"
        assert response.headers["cache-control"] == "no-store"
    assert actor.stopped == []


def test_stop_sends_stop_task_and_answers_sent() -> None:
    actor = Actor()
    sessions = Sessions({SESSION: actor})
    with TestClient(_app(sessions)) as client:
        response = client.post(_stop_path(SESSION, TOOL_USE), headers=WRITE)
    assert response.status_code == 200
    assert response.json() == {"status": "sent"}
    assert response.headers["cache-control"] == "no-store"
    assert actor.stopped == [TOOL_USE]


@pytest.mark.parametrize(
    ("code", "status"),
    [("not_running", 409), ("unknown_run", 409), ("refused", 409), ("timeout", 503)],
)
def test_stop_answers_the_runstoperror_code_and_status(code: str, status: int) -> None:
    sessions = Sessions({SESSION: Actor(stop_error=RunStopError(code))})  # type: ignore[arg-type]
    with TestClient(_app(sessions)) as client:
        response = client.post(_stop_path(SESSION, TOOL_USE), headers=WRITE)
    assert response.status_code == status
    assert response.json()["error"]["code"] == code
    assert response.headers["cache-control"] == "no-store"


async def test_a_fifth_stop_while_four_are_held_is_overloaded() -> None:
    release, four_held = asyncio.Event(), asyncio.Event()
    held = 0

    class SlowActor:
        def requests(self) -> list[dict[str, object]]:
            return []

        async def stop_run(self, block_id: str) -> None:
            nonlocal held
            held += 1
            if held == 4:
                four_held.set()
            await release.wait()

    sessions = Sessions({SESSION: SlowActor()})  # type: ignore[arg-type]
    transport = httpx.ASGITransport(app=_app(sessions))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        calls = [
            asyncio.create_task(client.post(_stop_path(SESSION, TOOL_USE), headers=WRITE))
            for _ in range(4)
        ]
        async with asyncio.timeout(HANG_GUARD_S):
            await four_held.wait()
        fifth = await client.post(_stop_path(SESSION, TOOL_USE), headers=WRITE)
        assert (fifth.status_code, fifth.json()["error"]["code"]) == (429, "overloaded")
        assert fifth.headers["cache-control"] == "no-store"
        release.set()
        async with asyncio.timeout(HANG_GUARD_S):
            assert [(await call).status_code for call in calls] == [200] * 4
        stopped = await client.post(_stop_path(SESSION, TOOL_USE), headers=WRITE)
        assert stopped.status_code == 200


def test_nothing_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="viki_agent")
    good = {"tool_use_id": TOOL_USE, "run": None, "raised_at": STAMP}
    sessions = Sessions(
        {
            SESSION: Actor(items=[good]),
            OTHER_SESSION: Actor(stop_error=RunStopError("refused")),
        }
    )
    with TestClient(_app(sessions)) as client:
        client.get(_requests_path(SESSION), headers=BEARER)
        client.post(_stop_path(SESSION, TOOL_USE), headers=WRITE)
        client.post(_stop_path(OTHER_SESSION, TOOL_USE), headers=WRITE)
        client.get(_requests_path(SESSION), headers={})
        client.get(_requests_path("ffffffff-ffff-4fff-8fff-ffffffffffff"), headers=BEARER)
        client.get(_requests_path(SESSION) + "?x=1", headers=BEARER)
    assert not caplog.records
