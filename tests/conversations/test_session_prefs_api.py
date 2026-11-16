"""The chat prefs route: GET answers what a chat's next spawn uses and writes nothing; PATCH
writes a new chat's model and effort, and refuses an open, transcript or profile chat and every
bad request without writing."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import uuid
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from tests.test_server import BEARER, NEW, ORIGIN, TOKEN, EchoDriver, EmptyCatalog, running
from tests.test_server_v2 import connected, subscribe
from viki_agent.conversations.session_prefs_api import session_prefs_routes
from viki_agent.drivers.claude.control import Launch
from viki_agent.events import Event
from viki_agent.state import AppState, Prefs

SESSION = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
WRITE = {**BEARER, "Origin": ORIGIN}
PLANTED = "planted-7c1e"  # a body value no answer or log may echo
HANG_GUARD_S = 5  # a guard on an event that normally fires in milliseconds


class Sessions:
    """The open actors: the route asks only whether one exists."""

    def __init__(self, *open_ids: str) -> None:
        self.open_ids = open_ids

    def get(self, session_id: str) -> object | None:
        return object() if session_id in self.open_ids else None


def _state(tmp_path: Path) -> AppState:
    return AppState(tmp_path / "data" / "state.json", Prefs(mode="auto", effort="low"))


def _app(
    state: AppState,
    sessions: Sessions | None = None,
    exists: Callable[[str], bool] = lambda _session_id: False,
    workers: set[asyncio.Task[Any]] | None = None,
) -> Starlette:
    routes = session_prefs_routes(
        state,
        Sessions() if sessions is None else sessions,
        exists,
        authorized=lambda request: request.headers.get("authorization") == f"Bearer {TOKEN}",
        origins=frozenset({ORIGIN}),
        workers=set() if workers is None else workers,
    )
    return Starlette(routes=routes)


def _path(session_id: str) -> str:
    return f"/api/v2/sessions/{session_id}/prefs"


URL = _path(SESSION)


def test_get_reads_defaults_for_an_unknown_chat_and_stored_prefs_for_a_known_one(
    tmp_path: Path,
) -> None:
    state = _state(tmp_path)
    known = str(uuid.uuid4())
    state.remember(known, model="sonnet", effort="medium")
    with TestClient(_app(state)) as client:
        before = client.get(URL, headers=BEARER)
        state.set_defaults(Prefs(effort="max", model="opus"))  # a Settings commit
        after = client.get(URL, headers=BEARER)
        stored = client.get(_path(known), headers=WRITE)
    assert [answer.status_code for answer in (before, after, stored)] == [200] * 3
    assert before.json() == {"model": None, "effort": "low"}
    assert after.json() == {"model": "opus", "effort": "max"}
    assert stored.json() == {"model": "sonnet", "effort": "medium"}
    assert not state.ours(SESSION)  # a read writes no row


def test_patch_writes_a_new_chat_and_answers_what_its_first_spawn_will_use(
    tmp_path: Path,
) -> None:
    state = _state(tmp_path)
    with TestClient(_app(state)) as client:
        first = client.patch(URL, json={"model": "sonnet"}, headers=WRITE)
        # A second pick before the first send merges into the row the first one wrote.
        second = client.patch(URL, json={"effort": "max"}, headers=WRITE)
        read = client.get(URL, headers=BEARER)
    assert (first.status_code, first.json()) == (200, {"model": "sonnet", "effort": "low"})
    assert (second.status_code, second.json()) == (200, {"model": "sonnet", "effort": "max"})
    assert read.json() == second.json()
    # Registration merges into the row, then the first spawn reads it. The mode stays the
    # default: the route never sets one.
    state.remember(SESSION)
    assert state.prefs(SESSION) == Prefs(mode="auto", effort="max", model="sonnet")


def test_fresh_effort_pick_keeps_qualified_default(tmp_path: Path) -> None:
    state = _state(tmp_path)
    model = f"viki-llm/{uuid.uuid4()}/synthetic-model"
    state.set_defaults(Prefs(model=model, effort="high"))
    with TestClient(_app(state)) as client:
        response = client.patch(URL, json={"effort": "low"}, headers=WRITE)
    assert response.status_code == 200
    assert response.json() == {"model": model, "effort": "low"}
    assert state.launch_prefs(SESSION).model == model


def test_patch_refuses_an_open_actor_a_transcript_or_a_profile_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _state(tmp_path)
    actor, transcript, profile = (str(uuid.uuid4()) for _ in range(3))
    # An open or profile chat has its row; a transcript made in the terminal may have none.
    for session_id in (actor, profile):
        state.remember(session_id, model="haiku", effort="medium")
    real = state.store.session_origin
    monkeypatch.setattr(
        state.store, "session_origin", lambda sid: "profile" if sid == profile else real(sid)
    )
    before = {sid: state.store.get(sid) for sid in (actor, transcript, profile)}
    app = _app(state, Sessions(actor), lambda sid: sid == transcript)
    with TestClient(app) as client:
        for session_id in before:
            response = client.patch(_path(session_id), json={"model": "sonnet"}, headers=WRITE)
            assert response.status_code == 409, session_id
            assert response.json()["error"]["code"] == "session_exists"
    assert {sid: state.store.get(sid) for sid in before} == before


# Bodies no chat may be given; a dict goes as JSON, bytes as they are.
INVALID_BODIES = {
    "empty": {},
    "not-an-object": ["model"],
    "unknown-key": {"model": "sonnet", "colour": PLANTED},
    "mode": {"mode": "acceptEdits"},
    "empty-model": {"model": ""},
    "long-model": {"model": "m" * 1025},  # a model is at most 1,024 UTF-8 bytes
    "null-model": {"model": None},
    "number-effort": {"effort": 3},
    "unknown-effort": {"effort": "huge"},
    "lone-surrogate": b'{"model": "\\ud800"}',
    "bad-json": f'{{"model": "{PLANTED}"'.encode(),
    "duplicate-key": f'{{"model": "a", "model": "{PLANTED}"}}'.encode(),
}
GOOD = {"model": PLANTED}
INVALID_INPUT = (400, "invalid_input")
UNSUPPORTED = (415, "invalid_representation")
REFUSALS = [
    *(pytest.param(URL, body, {}, INVALID_INPUT, id=name) for name, body in INVALID_BODIES.items()),
    pytest.param(_path(SESSION.upper()), GOOD, {}, INVALID_INPUT, id="id"),
    pytest.param(URL + "?model=sonnet", GOOD, {}, INVALID_INPUT, id="query"),
    pytest.param(URL, GOOD, {"Content-Type": "text/plain"}, UNSUPPORTED, id="not-json"),
    pytest.param(URL, GOOD, {"Content-Encoding": "gzip"}, UNSUPPORTED, id="compressed"),
    pytest.param(URL, {"model": PLANTED * 400}, {}, (413, "too_large"), id="too-large"),
]


@pytest.mark.parametrize(("url", "body", "headers", "refusal"), REFUSALS)
def test_patch_rejects_bad_requests_and_writes_nothing(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    url: str,
    body: object,
    headers: dict[str, str],
    refusal: tuple[int, str],
) -> None:
    caplog.set_level(logging.DEBUG)
    state = _state(tmp_path)
    content = body if isinstance(body, bytes) else json.dumps(body).encode()
    with TestClient(_app(state)) as client:
        response = client.patch(
            url, content=content, headers={**WRITE, "Content-Type": "application/json", **headers}
        )
    assert (response.status_code, response.json()["error"]["code"]) == refusal
    assert response.headers["cache-control"] == "no-store"
    assert PLANTED not in response.text and PLANTED not in caplog.text
    assert not state.ours(SESSION) and not state.ours(SESSION.upper())


def test_auth_and_origin(tmp_path: Path) -> None:
    state = _state(tmp_path)
    foreign = "http://evil.invalid"
    refusals = [
        # The token is checked first, then the Origin, then the id.
        ("GET", _path("x") + "?q=1", {"Origin": foreign}, 401, "unauthorized"),
        ("PATCH", URL, {"Authorization": "Bearer other", "Origin": ORIGIN}, 401, "unauthorized"),
        ("GET", _path("x") + "?q=1", {**BEARER, "Origin": foreign}, 403, "forbidden_origin"),
        ("PATCH", URL, BEARER, 403, "forbidden_origin"),  # a write names its Origin
        ("PATCH", URL, {**BEARER, "Origin": foreign}, 403, "forbidden_origin"),
    ]
    with TestClient(_app(state)) as client:
        for method, url, headers, status, code in refusals:
            response = client.request(method, url, headers=headers, json=GOOD)
            assert (response.status_code, response.json()["error"]["code"]) == (status, code)
            assert response.headers["cache-control"] == "no-store"
        assert not state.ours(SESSION)
        # A read needs no Origin; a write from the app's own Origin is admitted.
        for method, headers in (("GET", BEARER), ("PATCH", WRITE)):
            response = client.request(method, URL, headers=headers, json=GOOD)
            assert response.status_code == 200, method
            assert response.headers["cache-control"] == "no-store"


async def test_a_fifth_request_while_four_are_held_is_overloaded(tmp_path: Path) -> None:
    state = _state(tmp_path)
    release, four_held = asyncio.Event(), asyncio.Event()
    held = 0

    async def slow_body() -> AsyncIterator[bytes]:
        nonlocal held
        held += 1
        if held == 4:
            four_held.set()
        await release.wait()
        yield b'{"effort": "max"}'

    workers: set[asyncio.Task[Any]] = set()
    transport = httpx.ASGITransport(app=_app(state, workers=workers))
    headers = {**WRITE, "Content-Type": "application/json"}
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        calls = [
            asyncio.create_task(client.patch(URL, content=slow_body(), headers=headers))
            for _ in range(4)
        ]
        async with asyncio.timeout(HANG_GUARD_S):
            await four_held.wait()
        fifth = await client.get(URL, headers=BEARER)
        assert (fifth.status_code, fifth.json()["error"]["code"]) == (429, "overloaded")
        assert fifth.headers["cache-control"] == "no-store"
        release.set()
        async with asyncio.timeout(HANG_GUARD_S):
            assert [(await call).status_code for call in calls] == [200] * 4
        assert not workers  # each permit is back
        assert (await client.get(URL, headers=BEARER)).status_code == 200


def test_a_new_chat_pick_reaches_the_first_spawn(tmp_path: Path) -> None:
    spawned: list[Prefs] = []

    def make_driver(launch: Launch, prefs: Prefs, emit: Callable[[Event], None]) -> EchoDriver:
        spawned.append(prefs)
        return EchoDriver(launch, emit)

    pick = {"model": "sonnet", "effort": "max"}
    with running(tmp_path, make_driver=make_driver) as app, connected(app.client) as (ws, _):
        written = app.client.patch(_path(NEW), json=pick, headers=WRITE)
        assert written.status_code == 200
        assert written.json() == pick
        subscribe(ws, NEW)  # session.open: create, register, start
        assert [(prefs.model, prefs.effort) for prefs in spawned] == [("sonnet", "max")]
        again = app.client.patch(_path(NEW), json={"model": "opus"}, headers=WRITE)
        assert (again.status_code, again.json()["error"]["code"]) == (409, "session_exists")


class GatedCatalog(EmptyCatalog):
    """The first `exists` call after `arm()` (the PATCH's, in its worker thread) parks until
    released; every other call answers False at once, like EmptyCatalog."""

    def __init__(self, home: Path, vault: Path) -> None:
        super().__init__(home, vault)
        self._armed = False
        self._lock = threading.Lock()
        self.entered = threading.Event()
        self.release = threading.Event()

    def arm(self) -> None:
        self._armed = True

    def exists(self, session_id: str) -> bool:
        with self._lock:
            mine, self._armed = self._armed, False
        if mine:
            self.entered.set()
            assert self.release.wait(10)
        return False


def test_a_pick_racing_the_first_open_never_lands_on_a_running_chat(tmp_path: Path) -> None:
    """A PATCH that already passed its open-actor check, while a session.open for the same new
    id runs in the gap before the commit: the pick either lands before the first spawn (and that
    spawn uses it) or the PATCH answers `session_exists`. It never lands on a chat already
    running under the old pick."""
    spawned: list[Prefs] = []

    def make_driver(launch: Launch, prefs: Prefs, emit: Callable[[Event], None]) -> EchoDriver:
        spawned.append(prefs)
        return EchoDriver(launch, emit)

    catalog = GatedCatalog(tmp_path / "home", tmp_path / "vault")
    result: dict[str, int] = {}
    with (
        running(tmp_path, make_driver=make_driver, catalog=catalog) as app,
        connected(app.client) as (ws, _),
    ):
        catalog.arm()

        def patch() -> None:
            answer = app.client.patch(
                _path(NEW), json={"model": "sonnet", "effort": "max"}, headers=WRITE
            )
            result["status"] = answer.status_code

        writer = threading.Thread(target=patch)
        writer.start()
        assert catalog.entered.wait(10)  # the PATCH passed sessions.get() and sits in its thread
        subscribe(ws, NEW)  # session.open: create, register, start: the first spawn
        catalog.release.set()
        writer.join(10)
    # The contract a respawn relies on: a pick never lands on a chat that is already running.
    assert result["status"] == 409 or [(p.model, p.effort) for p in spawned] == [("sonnet", "max")]
