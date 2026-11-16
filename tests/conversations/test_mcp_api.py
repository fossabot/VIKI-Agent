"""The MCP control routes: gate order (401/403/400/429/404), the McpControlError -> status/message
mapping, the shared overload budget, and the real server wiring (create_app)."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from starlette.applications import Starlette
from starlette.requests import ClientDisconnect, Request
from starlette.routing import Route
from starlette.testclient import TestClient
from starlette.types import Message

from tests.test_server import BEARER, NEW, ORIGIN, TOKEN, EchoDriver, _open_v2, authed, running
from viki_agent.conversations import mcp_api
from viki_agent.conversations.mcp_api import mcp_routes
from viki_agent.drivers.base import McpControlCode, McpControlError
from viki_agent.drivers.claude.control import Launch
from viki_agent.events import Event
from viki_agent.state import Prefs

HANG_GUARD_S = 5  # a guard on an event that normally fires in milliseconds
# The fixture guard scans tests too, so the provider key prefix is spelled in halves.
API_KEY = "sk-" + "ant-" + "abc"


class Actor:
    """A session actor whose set_mcp_server/reconnect_mcp_server record their call, optionally
    hold until `release`, and optionally raise `error` once reached."""

    def __init__(self, error: McpControlError | None = None, release: asyncio.Event | None = None):
        self.error = error
        self.release = release
        self.calls: list[tuple[str, str, bool | None]] = []
        self.started = asyncio.Event()

    async def _run(self, action: str, name: str, enabled: bool | None) -> None:
        self.started.set()
        if self.release is not None:
            await self.release.wait()
        self.calls.append((action, name, enabled))
        if self.error is not None:
            raise self.error

    async def set_mcp_server(self, name: str, enabled: bool) -> None:
        await self._run("set", name, enabled)

    async def reconnect_mcp_server(self, name: str) -> None:
        await self._run("reconnect", name, None)


class Sessions:
    def __init__(self, actors: dict[str, Actor]) -> None:
        self.actors = actors
        self.asked: list[str] = []

    def get(self, session_id: str) -> Actor | None:
        self.asked.append(session_id)
        return self.actors.get(session_id)


def _app(sessions: Sessions, workers: set[asyncio.Task[Any]] | None = None) -> Starlette:
    routes = mcp_routes(
        sessions,
        authorized=lambda request: request.headers.get("authorization") == f"Bearer {TOKEN}",
        origins=frozenset({ORIGIN}),
        workers=set() if workers is None else workers,
    )
    return Starlette(routes=routes)


def _toggle(session_id: str) -> str:
    return f"/api/v2/sessions/{session_id}/mcp/toggle"


def _reconnect(session_id: str) -> str:
    return f"/api/v2/sessions/{session_id}/mcp/reconnect"


def _headers() -> dict[str, str]:
    return {**BEARER, "origin": ORIGIN, "content-type": "application/json"}


def _raw(body: dict[str, Any]) -> bytes:
    return json.dumps(body).encode()


def test_toggle_is_gated_in_order_and_never_logs(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="viki_agent")
    good, unknown = str(uuid.uuid4()), str(uuid.uuid4())
    sessions = Sessions({good: Actor()})
    on = _raw({"name": "s", "enabled": True})
    cases: list[tuple[str, dict[str, str], bytes, int, str]] = [
        (
            _toggle(good),
            {"origin": ORIGIN, "content-type": "application/json"},
            on,
            401,
            "unauthorized",
        ),
        (
            _toggle(good),
            {**BEARER, "content-type": "application/json"},
            on,
            403,
            "forbidden_origin",
        ),
        (_toggle(good) + "?x=1", _headers(), on, 400, "invalid_input"),
        (_toggle(good.upper()), _headers(), on, 400, "invalid_input"),
        (
            _toggle(good),
            {**BEARER, "origin": ORIGIN, "content-type": "text/plain"},
            on,
            400,
            "invalid_input",
        ),
        (
            _toggle(good),
            _headers(),
            b'{"name":"s","name":"t","enabled":true}',
            400,
            "invalid_input",
        ),
        (
            _toggle(good),
            _headers(),
            _raw({"name": "s", "enabled": True, "extra": 1}),
            400,
            "invalid_input",
        ),
        (_toggle(good), _headers(), _raw({"name": "s"}), 400, "invalid_input"),
        (_toggle(good), _headers(), _raw({"name": "s", "enabled": "yes"}), 400, "invalid_input"),
        (_toggle(good), _headers(), _raw({"name": API_KEY, "enabled": True}), 400, "invalid_input"),
        (
            _toggle(good),
            _headers(),
            _raw({"name": "a" * 300, "enabled": True}),
            400,
            "invalid_input",
        ),
        (
            _toggle(good),
            _headers(),
            _raw({"name": "x" * 2000, "enabled": True}),
            400,
            "invalid_input",
        ),
        (_toggle(unknown), _headers(), on, 404, "not_found"),
    ]
    with TestClient(_app(sessions)) as client:
        for path, headers, body, status, code in cases:
            response = client.post(path, headers=headers, content=body)
            assert (response.status_code, response.json()["error"]["code"]) == (status, code), path
            assert response.headers["cache-control"] == "no-store"
        ok = client.post(
            _toggle(good), headers=_headers(), content=_raw({"name": "s", "enabled": False})
        )
        assert (ok.status_code, ok.json()) == (200, {"status": "done"})
        assert ok.headers["cache-control"] == "no-store"
    # Every refusal above reached no actor; only the 404 and the final success did.
    assert sessions.asked == [unknown, good]
    assert sessions.actors[good].calls == [("set", "s", False)]
    assert TOKEN not in caplog.text and API_KEY not in caplog.text
    assert not caplog.records


def test_a_padded_body_over_the_cap_is_invalid_input_not_the_text_limit() -> None:
    """Unpadded, `{"name":"s","enabled":true}` is well under MAX_BODY and would reach the actor.
    Padding it with spaces keeps the JSON valid (trailing whitespace, not extra data), so only
    the 1 KiB cap -- not `_text`'s 256-character limit -- can be refusing the padded one."""
    session_id = str(uuid.uuid4())
    sessions = Sessions({session_id: Actor()})
    padded = b'{"name":"s","enabled":true}' + b" " * 2048
    assert len(padded) > mcp_api.MAX_BODY
    with TestClient(_app(sessions)) as client:
        response = client.post(_toggle(session_id), headers=_headers(), content=padded)
    assert (response.status_code, response.json()["error"]["code"]) == (400, "invalid_input")
    assert response.headers["cache-control"] == "no-store"
    assert sessions.asked == []  # the cap refuses it before the actor lookup


_EXPECTED_OUTCOMES: dict[McpControlCode, tuple[int, str]] = {
    "not_running": (409, "Claude Code is not running for this chat."),
    "busy": (409, "Available when this reply finishes."),
    "unknown_server": (409, "This chat's Claude Code does not list that server."),
    "managed": (409, "This server comes from a profile. Change it there."),
    "not_approved": (409, "Approve this server with /mcp in a terminal first."),
    "blocked": (409, "Your organization's policy blocks this server."),
    "refused": (409, "Claude Code did not change this server."),
    "not_connected": (409, "The server is on but did not connect. Try Retry."),
    "timeout": (503, "Claude Code did not answer in time. The change may still apply."),
}


def test_the_outcome_table_matches_the_spec_exactly() -> None:
    assert mcp_api._OUTCOMES == _EXPECTED_OUTCOMES


@pytest.mark.parametrize(("code", "status_and_message"), _EXPECTED_OUTCOMES.items())
def test_each_control_error_code_reaches_the_browser_as_its_own_status(
    caplog: pytest.LogCaptureFixture, code: McpControlCode, status_and_message: tuple[int, str]
) -> None:
    caplog.set_level(logging.DEBUG, logger="viki_agent")
    status, message = status_and_message
    session_id = str(uuid.uuid4())
    sessions = Sessions({session_id: Actor(error=McpControlError(code))})
    with TestClient(_app(sessions)) as client:
        response = client.post(
            _toggle(session_id), headers=_headers(), content=_raw({"name": "s", "enabled": True})
        )
    assert response.status_code == status
    assert response.json()["error"] == {"code": code, "message": message}
    assert response.headers["cache-control"] == "no-store"
    assert not caplog.records


def test_reconnect_shares_the_gate_and_the_mapping_with_toggle(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="viki_agent")
    good, unknown = str(uuid.uuid4()), str(uuid.uuid4())
    sessions = Sessions({good: Actor(error=McpControlError("timeout"))})
    with TestClient(_app(sessions)) as client:
        no_auth = client.post(
            _reconnect(good), headers={"origin": ORIGIN}, content=_raw({"name": "s"})
        )
        extra_key = client.post(
            _reconnect(good), headers=_headers(), content=_raw({"name": "s", "x": 1})
        )
        not_found = client.post(
            _reconnect(unknown), headers=_headers(), content=_raw({"name": "s"})
        )
        timed_out = client.post(_reconnect(good), headers=_headers(), content=_raw({"name": "s"}))
    assert no_auth.status_code == 401
    assert (extra_key.status_code, extra_key.json()["error"]["code"]) == (400, "invalid_input")
    assert not_found.status_code == 404
    assert (timed_out.status_code, timed_out.json()["error"]["code"]) == (503, "timeout")
    for response in (no_auth, extra_key, not_found, timed_out):
        assert response.headers["cache-control"] == "no-store"
    assert not caplog.records


def test_reconnect_passes_the_exact_name_through() -> None:
    session_id = str(uuid.uuid4())
    actor = Actor()
    sessions = Sessions({session_id: actor})
    with TestClient(_app(sessions)) as client:
        ok = client.post(
            _reconnect(session_id), headers=_headers(), content=_raw({"name": "github"})
        )
    assert (ok.status_code, ok.json()) == (200, {"status": "done"})
    assert actor.calls == [("reconnect", "github", None)]


async def test_a_body_not_read_in_time_is_invalid_input(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # httpx's ASGI transport hands the whole body over without ever yielding, so the deadline can
    # only be exercised by driving the ASGI app directly with a receive() that never delivers.
    caplog.set_level(logging.DEBUG, logger="viki_agent")
    monkeypatch.setattr(mcp_api, "INTAKE_TIMEOUT_S", 0.01)
    session_id = str(uuid.uuid4())
    sessions = Sessions({session_id: Actor()})
    sent: list[Message] = []

    async def receive() -> Message:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def send(message: Message) -> None:
        sent.append(message)

    path = _toggle(session_id)
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": [
            (b"authorization", f"Bearer {TOKEN}".encode()),
            (b"origin", ORIGIN.encode()),
            (b"content-type", b"application/json"),
            (b"content-length", b"20"),
        ],
        "client": ("127.0.0.1", 1),
        "server": ("127.0.0.1", 8765),
    }
    async with asyncio.timeout(HANG_GUARD_S):
        await _app(sessions)(scope, receive, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    assert start["status"] == 400
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    assert json.loads(body)["error"]["code"] == "invalid_input"
    assert sessions.asked == []  # the timeout never reached the actor lookup
    assert not caplog.records


async def test_a_client_that_disconnects_mid_body_is_invalid_input_and_logs_nothing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Starlette's own `ClientDisconnect` -- the client gone mid-read -- is just another
    unreadable body: `_body` returns None for it like any other, never Starlette's default 500.
    The module promises no-store and no log line on every answer, this case included."""
    caplog.set_level(logging.DEBUG, logger="viki_agent")
    session_id = str(uuid.uuid4())
    sessions = Sessions({session_id: Actor()})

    async def receive() -> Message:
        raise ClientDisconnect

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": _toggle(session_id),
            "path_params": {"session_id": session_id},
            "query_string": b"",
            "headers": [(k.encode("ascii"), v.encode("ascii")) for k, v in _headers().items()],
        },
        receive=receive,
    )
    route = _app(sessions).routes[0]
    assert isinstance(route, Route)
    response = await route.endpoint(request)
    assert (response.status_code, json.loads(response.body)["error"]["code"]) == (
        400,
        "invalid_input",
    )
    assert response.headers["cache-control"] == "no-store"
    assert sessions.asked == []
    assert not caplog.records


async def test_a_third_call_while_two_are_held_is_overloaded_before_the_lookup() -> None:
    """The capacity gate (2, shared by both routes) runs before the actor lookup: a third call,
    even for an unknown session, is overloaded rather than not_found."""
    release = asyncio.Event()
    session_a, session_b, unknown = (str(uuid.uuid4()) for _ in range(3))
    held_a, held_b = Actor(release=release), Actor(release=release)
    sessions = Sessions({session_a: held_a, session_b: held_b})
    transport = httpx.ASGITransport(app=_app(sessions))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        pending = [
            asyncio.create_task(
                client.post(
                    _toggle(session_a),
                    headers=_headers(),
                    content=_raw({"name": "s", "enabled": True}),
                )
            ),
            asyncio.create_task(
                client.post(_reconnect(session_b), headers=_headers(), content=_raw({"name": "s"}))
            ),
        ]
        async with asyncio.timeout(HANG_GUARD_S):
            await asyncio.gather(held_a.started.wait(), held_b.started.wait())
        third = await client.post(
            _toggle(unknown), headers=_headers(), content=_raw({"name": "s", "enabled": True})
        )
        assert (third.status_code, third.json()["error"]["code"]) == (429, "overloaded")
        assert third.headers["cache-control"] == "no-store"
        release.set()
        async with asyncio.timeout(HANG_GUARD_S):
            results = await asyncio.gather(*pending)
    assert [r.status_code for r in results] == [200, 200]
    assert held_a.calls == [("set", "s", True)]
    assert held_b.calls == [("reconnect", "s", None)]


async def test_a_cancelled_caller_does_not_stop_the_shielded_call() -> None:
    """A caller that goes away (cancelled, as an HTTP disconnect would do) does not cancel the
    actor call: `asyncio.shield` keeps the worker task, tracked in `workers`, running to
    completion -- the same guarantee `inventory_api.inventory_routes` gives its own callers."""
    release = asyncio.Event()
    session_id = str(uuid.uuid4())
    actor = Actor(release=release)
    sessions = Sessions({session_id: actor})
    workers: set[asyncio.Task[Any]] = set()
    transport = httpx.ASGITransport(app=_app(sessions, workers))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        caller = asyncio.create_task(
            client.post(
                _toggle(session_id),
                headers=_headers(),
                content=_raw({"name": "s", "enabled": True}),
            )
        )
        async with asyncio.timeout(HANG_GUARD_S):
            await actor.started.wait()
        held = set(workers)
        assert len(held) == 1
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        assert workers == held  # still tracked and running -- the cancel did not reach it
        release.set()
        async with asyncio.timeout(HANG_GUARD_S):
            await asyncio.gather(*held)
    assert actor.calls == [("set", "s", True)]


class McpEchoDriver(EchoDriver):
    """An EchoDriver that also reports McpControlDriver: records each call, and refuses one
    chosen server name -- proof the route passes the real name/enabled through rather than fixed
    ones."""

    def __init__(self, launch: Launch, emit: Callable[[Event], None]) -> None:
        super().__init__(launch, emit)
        self.calls: list[tuple[str, str, bool | None]] = []

    async def set_mcp_server(self, name: str, enabled: bool) -> None:
        if name == "bad-server":
            raise McpControlError("unknown_server")
        self.calls.append(("set", name, enabled))

    async def reconnect_mcp_server(self, name: str) -> None:
        if name == "bad-server":
            raise McpControlError("unknown_server")
        self.calls.append(("reconnect", name, None))


def make_driver(launch: Launch, prefs: Prefs, emit: Callable[[Event], None]) -> Any:
    return McpEchoDriver(launch, emit)


def test_the_real_app_wires_toggle_and_reconnect_to_a_live_actor(tmp_path: Path) -> None:
    """Proof of server.py's wiring (test_mcp_api's other tests cover the gate/mapping logic
    itself): registered beside inventory_routes, the real actor's live driver is reached with the
    exact name/enabled sent, and the driver's own refusal reaches the browser. Before this
    wiring, both paths 404 at the router."""
    with running(tmp_path, make_driver=make_driver) as app:
        with authed(app.client) as ws:
            _open_v2(ws)
        client = app.client
        off = client.post(
            f"/api/v2/sessions/{NEW}/mcp/toggle", headers=_headers(),
            content=_raw({"name": "github", "enabled": False}),
        )  # fmt: skip
        reconnected = client.post(
            f"/api/v2/sessions/{NEW}/mcp/reconnect",
            headers=_headers(),
            content=_raw({"name": "github"}),
        )
        refused = client.post(
            f"/api/v2/sessions/{NEW}/mcp/toggle", headers=_headers(),
            content=_raw({"name": "bad-server", "enabled": True}),
        )  # fmt: skip
    assert (off.status_code, off.json()) == (200, {"status": "done"})
    assert (reconnected.status_code, reconnected.json()) == (200, {"status": "done"})
    assert (refused.status_code, refused.json()["error"]["code"]) == (409, "unknown_server")
