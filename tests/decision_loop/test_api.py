"""The Decision routes. A stub service shows admission (the Bearer, then a listed Origin, then one
of four permits), body handling, path ids and the error map on every route; the real service over
the shared fakes shows each route's view and refusals, and that no response carries the key or a
model id."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import threading
from collections.abc import AsyncIterator, Mapping, Sequence
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from starlette.applications import Starlette

from tests.decision_loop.fakes import WALL, FakeInboxHost
from tests.decision_loop.test_service import (
    ARCHIVES,
    ASKED,
    FOLDER,
    GARDEN,
    HANG_GUARD_S,
    KEY,
    LEARNED,
    MISSING,
    PROPOSED,
    RIVAL,
    ROUTINE,
    SCOPE,
    SEEDLINGS,
    STRONG,
    TASKS,
    TRIAGE,
    WINDOW,
    Rig,
    check_folder,
    check_host,
    check_item,
    filed,
    filed_episode,
    label,
    observation,
    pretty,
    recorded,
    seat,
    seat_host,
    task,
    triage_item,
    worded,
)
from viki_agent.automation.routines import stamp
from viki_agent.decision_loop import api, playbook, wire
from viki_agent.decision_loop.api import decision_loop_routes
from viki_agent.decision_loop.changes import change_draft
from viki_agent.decision_loop.contracts import (
    LOOP_ERROR_CODES,
    MAX_OPEN_ITEMS,
    MODEL_IDS,
    DecisionLoopError,
    Presence,
    item_key,
)
from viki_agent.decision_loop.missions import MISSION_ERROR_CODES, MissionError
from viki_agent.decision_loop.service import DecisionLoopService
from viki_agent.decision_loop.wire import FeedbackRequest
from viki_agent.storage import StateError

BASE = "/api/v2/decision"
AUTH = {"Authorization": "Bearer secret"}
ORIGIN = "https://app.invalid"
FOREIGN = "https://evil.invalid"
JSON_TYPE = {"Content-Type": "application/json"}
CAPACITY = 4  # requests served at once
ID = "00000000-0000-4000-8000-0000000000e1"
ITEM = item_key("tasks_due", TASKS, "Repot the basil")

PRESENCE: dict[str, Any] = {
    "window": WINDOW,
    "focused": True,
    "typing": False,
    "chat": None,
    "chat_title": None,
    "note": {"workspace_id": SCOPE, "ref": TASKS},
    "launcher": False,
    "page": None,
}
FEEDBACK: dict[str, Any] = {
    "item_key": ITEM,
    "episode_id": None,
    "value": "useful",
    "reason": None,
    "source": "launcher",
}
OBSERVE: dict[str, Any] = {"event": "shown", "item_keys": [ITEM]}
REPORT: dict[str, Any] = {"topic": "unwatched", "source": "page"}
PREFERENCE: dict[str, Any] = {"name": "review_seen"}
MISSION: dict[str, Any] = {"item_key": ITEM, "work": "prepare"}
INBOX_SUGGEST: dict[str, Any] = {"item_key": TRIAGE}
PROPOSAL: dict[str, Any] = {"name": "goal_link", "question": PROPOSED}
ACT: dict[str, Any] = {"action": "approve", "revision": "5" * 64, "reason": None}
DATA_QUERY: dict[str, Any] = {
    "kind": "decisions",
    "ids": None,
    "topic": None,
    "shown": None,
    "answer": None,
    "state": None,
    "outcome": None,
    "since": None,
    "until": None,
    "before": None,
}
DATA_RECORD: dict[str, Any] = {"kind": "decisions", "id": ID}
DATA_DELETE: dict[str, Any] = {**DATA_QUERY, "ids": [ID]}
PROPOSAL_CAP = 8192  # the create route's body: a wording of up to 3,000 canonical characters

# Every route: its method, its path under BASE, the JSON body it takes (None: it takes none, and
# the client sends no body and no content type) and its success status.
Case = tuple[str, str, Mapping[str, Any] | None, int]
ROUTES: tuple[Case, ...] = (
    ("GET", "launcher", None, 200),
    ("POST", "launcher/open", None, 200),
    ("POST", "presence", PRESENCE, 204),
    ("POST", "feedback", FEEDBACK, 200),
    ("POST", "observe", OBSERVE, 204),
    ("POST", "reports", REPORT, 200),
    ("GET", "page", None, 200),
    ("POST", "preferences", PREFERENCE, 200),
    ("DELETE", "episodes", None, 200),
    ("POST", "key", {"key": KEY}, 200),
    ("DELETE", "key", None, 200),
    ("POST", "test", None, 200),
    ("GET", "calls", None, 200),
    ("GET", f"calls/{ID}", None, 200),
    ("GET", "missions", None, 200),
    ("POST", "missions", MISSION, 200),
    ("POST", f"missions/{ID}/stop", None, 200),
    ("POST", "missions/stop", None, 200),
    ("POST", f"seats/{ID}/start", None, 200),
    ("POST", f"seats/{ID}/stop", None, 200),
    ("POST", f"checks/{ID}/run", None, 200),
    ("POST", f"checks/{ID}/stop", None, 200),
    ("POST", "inbox/suggest", INBOX_SUGGEST, 200),
    ("POST", "proposals", PROPOSAL, 200),
    ("GET", f"proposals/{ID}", None, 200),
    ("POST", f"proposals/{ID}", ACT, 200),
    ("POST", f"changes/{ID}", ACT, 200),
    ("GET", "data", None, 200),
    ("POST", "data/records", DATA_QUERY, 200),
    ("POST", "data/record", DATA_RECORD, 200),
    ("POST", "data/delete", DATA_DELETE, 200),
)
JSON_ROUTES = tuple(case for case in ROUTES if case[2] is not None)
BODYLESS = tuple(case for case in ROUTES if case[2] is None)


def name(case: Case) -> str:
    return f"{case[0]} {case[1]}"


# What the stub hears from each route: the service method, and the route parser's output.
HEARD: dict[str, tuple[str, tuple[object, ...]]] = {
    "GET launcher": ("launcher", ()),
    "POST launcher/open": ("open_launcher", ()),
    "POST presence": ("presence", wire.parse_presence(PRESENCE)),
    "POST feedback": ("feedback", (wire.parse_feedback(FEEDBACK),)),
    "POST observe": ("observe", wire.parse_observe(OBSERVE)),
    "POST reports": ("report", wire.parse_report(REPORT)),
    "GET page": ("page", ()),
    "POST preferences": ("preference", wire.parse_preference(PREFERENCE)),
    "DELETE episodes": ("delete_all", ()),
    "POST key": ("save_key", (KEY,)),
    "DELETE key": ("remove_key", ()),
    "POST test": ("test", ()),
    "GET calls": ("calls", ()),
    f"GET calls/{ID}": ("call", (ID,)),
    "GET missions": ("missions", ()),
    "POST missions": ("start_mission", wire.parse_mission_start(MISSION)),
    f"POST missions/{ID}/stop": ("stop_mission", (ID,)),
    "POST missions/stop": ("stop_missions", ()),
    f"POST seats/{ID}/start": ("start_seat", (ID,)),
    f"POST seats/{ID}/stop": ("stop_seat", (ID,)),
    f"POST checks/{ID}/run": ("run_checks", (ID,)),
    f"POST checks/{ID}/stop": ("stop_checks", (ID,)),
    "POST inbox/suggest": ("suggest_inbox", (wire.parse_inbox_suggest(INBOX_SUGGEST),)),
    "POST proposals": ("create_proposal", wire.parse_proposal(PROPOSAL)),
    f"GET proposals/{ID}": ("proposal", (ID,)),
    f"POST proposals/{ID}": ("act_proposal", (ID, *wire.parse_proposal_act(ACT))),
    f"POST changes/{ID}": ("act_change", (ID, *wire.parse_proposal_act(ACT))),
    "GET data": ("data", ()),
    "POST data/records": ("records", (wire.parse_data_query(DATA_QUERY, delete=False),)),
    "POST data/record": ("record", wire.parse_data_record(DATA_RECORD)),
    "POST data/delete": ("delete", (wire.parse_data_query(DATA_DELETE, delete=True),)),
}

# The listed codes, each with its status and fixed English message.
STATUS = {
    "invalid_request": 400,
    "unknown_item": 404,
    "not_applicable": 409,
    "limit": 409,
    "too_large": 413,
    "invalid_representation": 415,
    "busy": 429,
    "unavailable": 503,
}
MESSAGES = {
    "invalid_request": "The request is invalid.",
    "unknown_item": "This item was not found.",
    "not_applicable": "That does not apply to this item now.",
    "limit": "The daily limit is reached.",
    "too_large": "The request is too large.",
    "invalid_representation": "Send the request as uncompressed JSON, or with no body.",
    "busy": "Decision is busy. Try again in a moment.",
    "unavailable": "Decision is unavailable right now.",
}
UNAUTHORIZED = {"error": {"code": "unauthorized", "message": "Authentication is required."}}
FORBIDDEN = {"error": {"code": "forbidden_origin", "message": "This origin is not allowed."}}
# A refused mission start's code, as the routes answer it.
MISSION_CODES = {
    "limit": "limit",
    "busy": "busy",
    "off": "not_applicable",
    "from_mission": "not_applicable",
    "no_work": "not_applicable",
    "no_folder": "not_applicable",
    "not_sent": "unavailable",
    "tried": "not_applicable",
}


def refusal(code: str) -> dict[str, Any]:
    return {"error": {"code": code, "message": MESSAGES[code]}}


class Stub:
    """The service's interface. Each call notes its method, its arguments and the thread it ran
    on, raises `error` when one is set, waits at the gate `hold` sets up, and answers a small
    view; presence and observe answer nothing."""

    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error
        self.seen: list[tuple[str, tuple[object, ...]]] = []
        self.threads: set[int] = set()
        self.gate: asyncio.Event | None = None
        self.full = asyncio.Event()
        self._waiting = 0
        self._expected = 0

    def hold(self, count: int) -> asyncio.Event:
        """Hold every call at a gate; `full` is set once `count` calls wait there."""
        self.gate = asyncio.Event()
        self._expected = count
        return self.gate

    async def _answer(self, method: str, *args: object) -> dict[str, Any]:
        self.seen.append((method, args))
        self.threads.add(threading.get_ident())
        if self.error is not None:
            raise self.error
        if self.gate is not None:
            self._waiting += 1
            if self._waiting == self._expected:
                self.full.set()
            await self.gate.wait()
        return {"answered": method}

    async def launcher(self) -> dict[str, Any]:
        return await self._answer("launcher")

    async def open_launcher(self) -> dict[str, Any]:
        return await self._answer("open_launcher")

    async def presence(self, window: str, presence: Presence) -> None:
        await self._answer("presence", window, presence)

    async def feedback(self, request: FeedbackRequest) -> dict[str, Any]:
        return await self._answer("feedback", request)

    async def observe(self, event: str, item_keys: Sequence[str]) -> None:
        await self._answer("observe", event, tuple(item_keys))

    async def page(self) -> dict[str, Any]:
        return await self._answer("page")

    async def data(self) -> dict[str, Any]:
        return await self._answer("data")

    async def records(self, query: wire.DataQuery) -> dict[str, Any]:
        return await self._answer("records", query)

    async def record(self, kind: str, record_id: str) -> dict[str, Any]:
        return await self._answer("record", kind, record_id)

    async def delete(self, query: wire.DataQuery) -> dict[str, Any]:
        return await self._answer("delete", query)

    async def report(self, topic: str, source: str) -> dict[str, Any]:
        return await self._answer("report", topic, source)

    async def preference(self, name: str, candidate: str | None) -> dict[str, Any]:
        return await self._answer("preference", name, candidate)

    async def delete_all(self) -> dict[str, Any]:
        return await self._answer("delete_all")

    async def save_key(self, key: str) -> dict[str, Any]:
        return await self._answer("save_key", key)

    async def remove_key(self) -> dict[str, Any]:
        return await self._answer("remove_key")

    async def test(self) -> dict[str, Any]:
        return await self._answer("test")

    async def calls(self) -> dict[str, Any]:
        return await self._answer("calls")

    async def call(self, call_id: str) -> dict[str, Any]:
        return await self._answer("call", call_id)

    async def missions(self) -> dict[str, Any]:
        return await self._answer("missions")

    async def start_mission(self, item_key: str, work: str) -> dict[str, Any]:
        return await self._answer("start_mission", item_key, work)

    async def stop_mission(self, mission_id: str) -> dict[str, Any]:
        return await self._answer("stop_mission", mission_id)

    async def stop_missions(self) -> dict[str, Any]:
        return await self._answer("stop_missions")

    async def start_seat(self, routine_id: str) -> dict[str, Any]:
        return await self._answer("start_seat", routine_id)

    async def stop_seat(self, routine_id: str) -> dict[str, Any]:
        return await self._answer("stop_seat", routine_id)

    async def run_checks(self, workspace_id: str) -> dict[str, Any]:
        return await self._answer("run_checks", workspace_id)

    async def stop_checks(self, workspace_id: str) -> dict[str, Any]:
        return await self._answer("stop_checks", workspace_id)

    async def suggest_inbox(self, item_key: str) -> dict[str, Any]:
        return await self._answer("suggest_inbox", item_key)

    async def create_proposal(self, name: str, question: Mapping[str, object]) -> dict[str, Any]:
        return await self._answer("create_proposal", name, question)

    async def proposal(self, proposal_id: str) -> dict[str, Any]:
        return await self._answer("proposal", proposal_id)

    async def act_proposal(
        self, proposal_id: str, action: str, revision: str, reason: str | None
    ) -> dict[str, Any]:
        return await self._answer("act_proposal", proposal_id, action, revision, reason)

    async def act_change(
        self, proposal_id: str, action: str, revision: str, reason: str | None
    ) -> dict[str, Any]:
        return await self._answer("act_change", proposal_id, action, revision, reason)


def routes_app(service: object, workers: set[asyncio.Task[Any]] | None = None) -> Starlette:
    return Starlette(
        routes=decision_loop_routes(
            cast(DecisionLoopService, service),
            authorized=lambda request: request.headers.get("authorization") == "Bearer secret",
            origins=frozenset({ORIGIN}),
            workers=set[asyncio.Task[Any]]() if workers is None else workers,
        )
    )


@contextlib.asynccontextmanager
async def opened(
    service: object, workers: set[asyncio.Task[Any]] | None = None
) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=routes_app(service, workers))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def call(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    content: bytes | Mapping[str, Any] | None = None,
    *,
    headers: Mapping[str, str | None] | None = None,
) -> httpx.Response:
    """One request as the client sends it: the Bearer, a listed Origin on a POST or DELETE (a
    browser leaves it off a same-origin GET) and a JSON body's content type; `headers` add or
    replace headers, and a None value leaves one out."""
    sent: dict[str, str] = {**AUTH}
    if method != "GET":
        sent["Origin"] = ORIGIN
    if isinstance(content, Mapping):
        sent.update(JSON_TYPE)
        content = json.dumps(content).encode()
    for header, value in (headers or {}).items():
        if value is None:
            sent.pop(header, None)
        else:
            sent[header] = value
    return await client.request(method, f"{BASE}/{path}", content=content, headers=sent)


def answered(response: httpx.Response, status: int = 200) -> Any:
    """The body of a response that succeeded with `status`."""
    assert (response.status_code, response.headers["cache-control"]) == (status, "no-store"), (
        response.text
    )
    if status == 204:
        assert response.content == b""
        return None
    return response.json()


def refused_with(response: httpx.Response, status: int, body: Mapping[str, Any]) -> None:
    assert (response.status_code, response.json()) == (status, body)
    assert response.headers["cache-control"] == "no-store"


async def chunks(content: bytes) -> AsyncIterator[bytes]:
    for start in range(0, len(content), 1024):
        yield content[start : start + 1024]


# ---- admission: the Bearer, then the Origin, then a permit -------------------------------------


@pytest.mark.parametrize("case", ROUTES, ids=name)
async def test_every_route_answers_401_without_the_bearer_whatever_its_origin(case: Case) -> None:
    method, path, body, _status = case
    stub = Stub()
    async with opened(stub) as client:
        refused = [
            await call(client, method, path, body, headers=headers)
            for headers in (
                {"Authorization": None},
                {"Authorization": "Bearer wrong"},
                {"Authorization": "secret"},
                # The Bearer is checked first: with a foreign Origin too, the answer is 401.
                {"Authorization": None, "Origin": FOREIGN},
                {"Authorization": "Bearer wrong", "Origin": FOREIGN},
            )
        ]
    for response in refused:
        refused_with(response, 401, UNAUTHORIZED)
    assert stub.seen == []


@pytest.mark.parametrize("case", ROUTES, ids=name)
async def test_every_route_answers_403_for_an_unlisted_origin_and_only_a_get_may_omit_one(
    case: Case,
) -> None:
    method, path, body, status = case
    stub = Stub()
    async with opened(stub) as client:
        refused = [
            await call(client, method, path, body, headers={"Origin": origin})
            for origin in (FOREIGN, "null", ORIGIN.upper(), f"{ORIGIN}/", "")
        ]
        omitted = await call(client, method, path, body, headers={"Origin": None})
        listed = await call(client, method, path, body, headers={"Origin": ORIGIN})
    for response in refused:
        refused_with(response, 403, FORBIDDEN)
    answered(listed, status)
    if method == "GET":
        answered(omitted, status)
        assert len(stub.seen) == 2
    else:
        refused_with(omitted, 403, FORBIDDEN)
        assert len(stub.seen) == 1


async def test_a_full_house_answers_busy_only_after_the_bearer_and_the_origin_pass() -> None:
    stub = Stub()
    gate = stub.hold(CAPACITY)
    workers: set[asyncio.Task[Any]] = set()
    async with opened(stub, workers) as client:
        held = [asyncio.create_task(call(client, "GET", "launcher")) for _ in range(CAPACITY)]
        await asyncio.wait_for(stub.full.wait(), HANG_GUARD_S)
        assert len(workers) == CAPACITY  # each admitted request runs as a task kept here

        busy = await call(client, "POST", "observe", OBSERVE)
        no_bearer = await call(client, "GET", "page", headers={"Authorization": None})
        foreign = await call(client, "POST", "test", headers={"Origin": FOREIGN})

        refused_with(busy, 429, refusal("busy"))
        refused_with(no_bearer, 401, UNAUTHORIZED)
        refused_with(foreign, 403, FORBIDDEN)
        gate.set()
        assert [response.status_code for response in await asyncio.gather(*held)] == [200] * 4
        assert not workers
        answered(await call(client, "GET", "launcher"))  # every permit came back
    assert [method for method, _args in stub.seen] == ["launcher"] * (CAPACITY + 1)


async def test_every_refusal_releases_its_permit() -> None:
    stub = Stub()
    async with opened(stub) as client:
        for _ in range(CAPACITY + 1):
            text = {"Content-Type": "text/plain"}
            assert (await call(client, "POST", "feedback", b"{}", headers=text)).status_code == 415
            nope = await call(client, "POST", "feedback", b"nope", headers=JSON_TYPE)
            assert nope.status_code == 400
            assert (await call(client, "GET", "launcher?x=1")).status_code == 400
            assert (await call(client, "GET", "calls/not-an-id")).status_code == 404
            assert (await call(client, "POST", "test", b"{}")).status_code == 400
            big = await call(client, "POST", "observe", b" " * 4097, headers=JSON_TYPE)
            assert big.status_code == 413
        for error, status in (
            (DecisionLoopError("not_applicable"), 409),
            (MissionError("busy"), 429),
            (StateError("busy"), 503),
            (RuntimeError("boom"), 503),
        ):
            stub.error = error
            for _ in range(CAPACITY + 1):
                assert (await call(client, "GET", "page")).status_code == status
        stub.error = None
        answered(await call(client, "GET", "page"))


# ---- bodies, query strings and path ids --------------------------------------------------------


@pytest.mark.parametrize("case", ROUTES, ids=name)
async def test_a_query_string_is_400_on_every_route(case: Case) -> None:
    method, path, body, _status = case
    stub = Stub()
    async with opened(stub) as client:
        refused = [
            await call(client, method, f"{path}?{query}", body) for query in ("x=1", "x", "=")
        ]
    for response in refused:
        refused_with(response, 400, refusal("invalid_request"))
    assert stub.seen == []


@pytest.mark.parametrize("case", JSON_ROUTES, ids=name)
async def test_a_route_with_parameters_takes_only_uncompressed_json(case: Case) -> None:
    method, path, body, status = case
    content = json.dumps(body).encode()
    stub = Stub()
    async with opened(stub) as client:
        refused = [
            await call(client, method, path, content, headers=headers)
            for headers in (
                {},
                {"Content-Type": "text/plain"},
                {"Content-Type": "application/x-www-form-urlencoded"},
                {"Content-Type": "application/json-seq"},
                {**JSON_TYPE, "Content-Encoding": "gzip"},
            )
        ]
        accepted = [
            await call(client, method, path, content, headers=headers)
            for headers in (
                {"Content-Type": "application/json; charset=utf-8"},
                {"Content-Type": "Application/JSON", "Content-Encoding": "identity"},
            )
        ]
    for response in refused:
        refused_with(response, 415, refusal("invalid_representation"))
    for response in accepted:
        answered(response, status)
    assert len(stub.seen) == 2


@pytest.mark.parametrize("case", BODYLESS, ids=name)
async def test_a_route_without_parameters_takes_an_empty_body_and_no_content_type(
    case: Case,
) -> None:
    method, path, _body, status = case
    stub = Stub()
    async with opened(stub) as client:
        sent = await call(client, method, path)  # as the client sends it
        bodies = [await call(client, method, path, content) for content in (b"{}", b"x", b" ")]
        typed = [
            await call(client, method, path, content, headers=headers)
            for content, headers in (
                (b"", JSON_TYPE),
                (b"{}", JSON_TYPE),
                (b"", {"Content-Type": "text/plain"}),
                (b"", {"Content-Encoding": "gzip"}),
            )
        ]
    answered(sent, status)
    for response in bodies:
        refused_with(response, 400, refusal("invalid_request"))
    for response in typed:
        refused_with(response, 415, refusal("invalid_representation"))
    assert stub.seen == [HEARD[name(case)]]


@pytest.mark.parametrize("chunked", [False, True], ids=["whole", "chunked"])
@pytest.mark.parametrize("case", JSON_ROUTES, ids=name)
async def test_a_json_body_at_its_cap_is_read_and_one_byte_more_is_413(
    case: Case, chunked: bool
) -> None:
    method, path, body, status = case
    # The key route reads a key of up to 4,096 characters, the proposal route a wording.
    cap = {"key": 8192, "proposals": PROPOSAL_CAP}.get(path, 4096)
    at_cap = json.dumps(body).encode().ljust(cap)  # JSON allows the trailing spaces
    stub = Stub()
    async with opened(stub) as client:

        async def send(content: bytes) -> httpx.Response:
            sent = chunks(content) if chunked else content
            headers = {**AUTH, "Origin": ORIGIN, **JSON_TYPE}
            return await client.request(method, f"{BASE}/{path}", content=sent, headers=headers)

        read = await send(at_cap)
        big = await send(at_cap + b" ")
    answered(read, status)
    refused_with(big, 413, refusal("too_large"))
    assert stub.seen == [HEARD[name(case)]]


@pytest.mark.parametrize("case", BODYLESS, ids=name)
async def test_a_body_past_the_cap_of_a_route_without_parameters_is_413(case: Case) -> None:
    method, path, _body, _status = case
    stub = Stub()
    async with opened(stub) as client:
        at_cap = await call(client, method, path, b" " * 4096)
        big = await call(client, method, path, b" " * 4097)
    refused_with(at_cap, 400, refusal("invalid_request"))  # read, then refused as a body
    refused_with(big, 413, refusal("too_large"))
    assert stub.seen == []


async def test_an_observe_body_naming_the_launchers_most_items_fits_its_cap() -> None:
    # The client observes the items the launcher lists, at most MAX_OPEN_ITEMS of them.
    keys = [item_key("tasks_due", TASKS, f"Task {n}") for n in range(MAX_OPEN_ITEMS)]
    stub = Stub()
    async with opened(stub) as client:
        observed = await call(client, "POST", "observe", {"event": "opened", "item_keys": keys})
    answered(observed, 204)
    assert stub.seen == [("observe", ("opened", tuple(keys)))]


async def test_fifty_suggestion_ids_fit_the_delete_body_cap() -> None:
    ids = [f"{index:064x}" for index in range(50)]
    body = {**DATA_DELETE, "kind": "suggestions", "ids": ids}
    encoded = json.dumps(body).encode()
    assert len(encoded) <= api.MAX_DECISION_BODY_BYTES
    stub = Stub()
    async with opened(stub) as client:
        accepted = await call(client, "POST", "data/delete", body)
        refused = await call(
            client,
            "POST",
            "data/delete",
            encoded.ljust(api.MAX_DECISION_BODY_BYTES + 1),
            headers=JSON_TYPE,
        )
    answered(accepted)
    refused_with(refused, 413, refusal("too_large"))
    assert stub.seen == [("delete", (wire.parse_data_query(body, delete=True),))]


async def test_data_records_refuses_an_invalid_query() -> None:
    stub = Stub()
    body = {**DATA_QUERY, "kind": "calls", "topic": "tasks_due"}
    async with opened(stub) as client:
        response = await call(client, "POST", "data/records", body)
    refused_with(response, 400, refusal("invalid_request"))
    assert stub.seen == []


MALFORMED: dict[str, bytes] = {
    "empty": b"",
    "not json": b"not json",
    "an array": b"[]",
    "a string": b'"text"',
    "null": b"null",
    "not utf-8": b"\xff\xfe",
    "too deep": b"[" * 4096,
}


@pytest.mark.parametrize("content", MALFORMED.values(), ids=list(MALFORMED))
@pytest.mark.parametrize("case", JSON_ROUTES, ids=name)
async def test_a_body_that_is_not_one_json_object_is_400(case: Case, content: bytes) -> None:
    method, path, _body, _status = case
    stub = Stub()
    async with opened(stub) as client:
        refused = await call(client, method, path, content, headers=JSON_TYPE)
    refused_with(refused, 400, refusal("invalid_request"))
    assert stub.seen == []


def misshapen(body: Mapping[str, Any], kind: str) -> bytes:
    """`body` with its first key missing, with an extra key, with its first value in the wrong
    type, or with its first key twice."""
    first, value = next(iter(body.items()))
    if kind == "a missing key":
        return json.dumps({key: kept for key, kept in body.items() if key != first}).encode()
    if kind == "an extra key":
        return json.dumps({**body, "extra": None}).encode()
    if kind == "a wrong type":
        return json.dumps({**body, first: [value]}).encode()
    return ("{" + f"{json.dumps(first)}: {json.dumps(value)}, " + json.dumps(body)[1:]).encode()


KINDS = ("a missing key", "an extra key", "a wrong type", "a duplicate key")


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("case", JSON_ROUTES, ids=name)
async def test_a_route_with_parameters_takes_exactly_its_keys(case: Case, kind: str) -> None:
    method, path, body, _status = case
    assert body is not None
    stub = Stub()
    async with opened(stub) as client:
        refused = await call(client, method, path, misshapen(body, kind), headers=JSON_TYPE)
    refused_with(refused, 400, refusal("invalid_request"))
    assert stub.seen == []


BAD_IDS: dict[str, str] = {
    "upper case": ID.upper(),
    "no hyphens": ID.replace("-", ""),
    "braces": "{" + ID + "}",
    "a urn": f"urn:uuid:{ID}",
    "short": ID[:-1],
    "long": f"{ID}0",
    "not hex": f"{ID[:-1]}g",
    "a fullwidth digit": ID[:-1] + chr(0xFF11),
    "a word": "stop",
}


@pytest.mark.parametrize("bad", BAD_IDS.values(), ids=list(BAD_IDS))
@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "calls/{}"),
        ("POST", "missions/{}/stop"),
        ("POST", "seats/{}/start"),
        ("POST", "seats/{}/stop"),
        ("GET", "proposals/{}"),
        ("POST", "proposals/{}"),
        ("POST", "changes/{}"),
    ],
    ids=["call", "stop", "start_seat", "stop_seat", "proposal", "act", "change"],
)
async def test_a_path_id_that_is_not_a_canonical_uuid_is_unknown(
    method: str, path: str, bad: str
) -> None:
    stub = Stub()
    async with opened(stub) as client:
        refused = await call(client, method, path.format(bad))
    refused_with(refused, 404, refusal("unknown_item"))
    assert refusal("unknown_item") == wire.example_views()["error"]  # the client's example
    assert stub.seen == []


# ---- the error map -----------------------------------------------------------------------------


def test_the_error_map_covers_every_code_the_loop_raises() -> None:
    assert set(LOOP_ERROR_CODES) <= set(STATUS) == set(MESSAGES)
    assert set(MISSION_CODES) == set(MISSION_ERROR_CODES)


ERRORS: list[tuple[BaseException, str]] = [
    *((DecisionLoopError(code), code) for code in LOOP_ERROR_CODES),
    (DecisionLoopError("unlisted"), "unavailable"),
    *((MissionError(code), answer) for code, answer in MISSION_CODES.items()),
    (MissionError("unlisted"), "unavailable"),
    (StateError("busy"), "unavailable"),
    (TimeoutError(), "unavailable"),  # the service's own timeout is not a stalled body
]


@pytest.mark.parametrize(
    ("error", "code"), ERRORS, ids=[f"{type(error).__name__}-{error}" for error, _code in ERRORS]
)
async def test_the_error_map_answers_a_listed_code_and_its_fixed_message(
    error: BaseException, code: str
) -> None:
    stub = Stub(error)
    async with opened(stub) as client:
        for case in ROUTES:
            method, path, body, _status = case
            refused = await call(client, method, path, body)
            assert (refused.status_code, refused.json()) == (STATUS[code], refusal(code)), name(
                case
            )
            assert refused.headers["cache-control"] == "no-store"
    assert len(stub.seen) == len(ROUTES)


async def test_an_unexpected_failure_answers_503_with_one_fixed_log_line(
    caplog: pytest.LogCaptureFixture,
) -> None:
    stub = Stub(RuntimeError(f"boom near {KEY}"))
    with caplog.at_level(logging.DEBUG, logger="viki_agent"):
        async with opened(stub) as client:
            refused = [await call(client, method, path, body) for method, path, body, _ in ROUTES]
    for response in refused:
        refused_with(response, 503, refusal("unavailable"))
    records = [record for record in caplog.records if record.name == api.__name__]
    assert [(record.levelno, record.getMessage()) for record in records] == [
        (logging.WARNING, "decision route failed")
    ] * len(ROUTES)
    assert all(record.args == () and record.exc_info is None for record in records)
    assert "boom" not in caplog.text and KEY not in caplog.text


async def test_a_body_that_stalls_is_408_and_releases_its_permit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(api, "BODY_TIMEOUT_S", 0.01)
    never = asyncio.Event()

    async def stalled() -> AsyncIterator[bytes]:
        yield b'{"event": '
        await never.wait()
        yield b'"shown"}'

    stub = Stub()
    headers = {**AUTH, "Origin": ORIGIN, **JSON_TYPE}
    async with opened(stub) as client:
        for _ in range(CAPACITY + 1):
            slow = await asyncio.wait_for(
                client.post(f"{BASE}/observe", content=stalled(), headers=headers), HANG_GUARD_S
            )
            refused_with(
                slow,
                408,
                {"error": {"code": "intake_timeout", "message": "The request timed out."}},
            )
        answered(await call(client, "GET", "launcher"))
    assert stub.seen == [("launcher", ())]


async def test_a_client_gone_mid_body_is_400() -> None:
    stub = Stub()
    sent: list[Mapping[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.disconnect"}

    async def send(message: Mapping[str, Any]) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": f"{BASE}/observe",
        "raw_path": f"{BASE}/observe".encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"authorization", b"Bearer secret"),
            (b"origin", ORIGIN.encode()),
            (b"content-type", b"application/json"),
        ],
        "server": ("test", 80),
        "client": ("127.0.0.1", 50000),
    }
    await routes_app(stub)(scope, receive, send)
    start, body = sent
    assert start["status"] == 400 and (b"cache-control", b"no-store") in start["headers"]
    assert json.loads(body["body"]) == {
        "error": {"code": "disconnected", "message": "The request was interrupted."}
    }
    assert stub.seen == []


# ---- the service behind the routes -------------------------------------------------------------


async def test_every_route_awaits_its_service_method_on_the_event_loop() -> None:
    stub = Stub()
    async with opened(stub) as client:
        responses = [await call(client, method, path, body) for method, path, body, _ in ROUTES]
    for case, response in zip(ROUTES, responses, strict=True):
        view = answered(response, case[3])
        assert view is None or view == {"answered": HEARD[name(case)][0]}
    # Each route hands its parser's output to its method, presence's report included, on the
    # event loop (here, the test's own thread).
    assert stub.seen == [HEARD[name(case)] for case in ROUTES]
    assert stub.threads == {threading.get_ident()}


@pytest.fixture
async def rig(tmp_path: Path) -> AsyncIterator[Rig]:
    rig = Rig(tmp_path)
    yield rig
    await asyncio.wait_for(rig.engine.stop(), HANG_GUARD_S)


async def test_data_routes_round_trip_over_the_real_service(rig: Rig) -> None:
    episode_id = recorded(rig, "Held record")
    async with opened(rig.service) as client:
        summary = answered(await call(client, "GET", "data"))
        records = answered(await call(client, "POST", "data/records", DATA_QUERY))
        detail = answered(
            await call(client, "POST", "data/record", {**DATA_RECORD, "id": episode_id})
        )
        deleted = answered(
            await call(client, "POST", "data/delete", {**DATA_DELETE, "ids": [episode_id]})
        )
    assert summary["decisions"]["decisions"] == 1
    assert (records["kind"], [row["id"] for row in records["rows"]]) == (
        "decisions",
        [episode_id],
    )
    assert (detail["kind"], detail["id"]) == ("decisions", episode_id)
    assert deleted == {"deleted": 1, "kept": 0}
    assert rig.rows("decision_episodes") == []


async def test_every_route_answers_its_view_over_the_real_service(rig: Rig) -> None:
    examples = wire.example_views()
    listed = item_key("tasks_due", TASKS, SEEDLINGS)
    rig.observation = observation(task(SEEDLINGS), inbox=1)
    async with opened(rig.service) as client:
        view = answered(await call(client, "GET", "launcher"))
        assert set(view) == set(examples["launcher"]) and view["items"] == []
        view = answered(await call(client, "POST", "launcher/open"))  # a cycle runs first
        assert [item["item_key"] for item in view["items"]] == [listed]
        held = rig.agenda.item(SCOPE, item_key("inbox", SCOPE))
        assert held is not None and held.level == "silent"

        answered(await call(client, "POST", "presence", PRESENCE), 204)
        assert rig.presence.current().note_ref == (SCOPE, TASKS)
        shown = {"event": "shown", "item_keys": [listed]}
        answered(await call(client, "POST", "observe", shown), 204)
        found = rig.agenda.item(SCOPE, listed)
        assert found is not None and found.shown_at == stamp(WALL)

        missed = {**FEEDBACK, "item_key": None, "episode_id": held.episode_id, "value": "missed"}
        view = answered(await call(client, "POST", "feedback", missed))
        assert set(view) == set(examples["launcher"])
        labels = {row["episode_id"]: row["feedback"] for row in rig.rows("decision_episodes")}
        assert labels[held.episode_id] == "missed"
        for path, body in (("reports", REPORT), ("preferences", PREFERENCE)):
            assert set(answered(await call(client, "POST", path, body))) == set(examples["page"])
        reports = [row for row in rig.rows("decision_episodes") if row["entry"] == "report"]
        assert [(row["candidate"], row["feedback_source"]) for row in reports] == [
            ("unwatched", "page")
        ]
        assert rig.store.review_seen_at(SCOPE) == WALL
        assert set(answered(await call(client, "GET", "page"))) == set(examples["page"])

        page = answered(await call(client, "POST", "key", {"key": KEY}))
        assert page["model"]["key"] == "saved" and rig.keys.load() == KEY
        tested = answered(await call(client, "POST", "test"))
        assert tested == {"ok": True, "latency_ms": 0, "version": "1.14.0", "error": None}

        rig.model.answers = dict(STRONG)
        rig.clock.advance(60)
        answered(await call(client, "POST", "launcher/open"))  # the Decision model decides
        calls = answered(await call(client, "GET", "calls"))
        assert set(calls) == set(examples["calls"]) and calls["calls"]
        detail = answered(await call(client, "GET", f"calls/{calls['calls'][0]['call_id']}"))
        assert set(detail) == set(examples["call_detail"])

        start = {"item_key": listed, "work": "prepare"}
        mission = answered(await call(client, "POST", "missions", start))
        assert set(mission) == set(examples["mission"]) and mission["state"] == "running"
        block = answered(await call(client, "GET", "missions"))
        assert set(block) == set(examples["page"]["missions"])
        assert block == {
            "running": [mission],
            "today": {"prepare": 1, "run": 0},
            "limits": {"prepare": 5, "run": 3},
            "hold": None,
            "team": {"today": 0, "limit": 4, "running": 0},
            "look": {"today": 0, "limit": 3},
            "second_look": {"today": 0, "limit": 3},
            "project_plan": {"today": 0, "limit": 3},
            "inbox": {"today": 0, "limit": 3},
            "checks": None,
        }
        stopped = answered(await call(client, "POST", f"missions/{mission['mission_id']}/stop"))
        assert (stopped["mission_id"], stopped["state"], stopped["reason"]) == (
            mission["mission_id"],
            "stopped",
            "owner",
        )
        answered(await call(client, "POST", "missions", {**start, "work": "run"}))
        block = answered(await call(client, "POST", "missions/stop"))
        assert (block["running"], block["today"]) == ([], {"prepare": 1, "run": 1})

        muted = {**FEEDBACK, "item_key": listed, "value": "mute"}
        view = answered(await call(client, "POST", "feedback", muted))
        assert listed not in [item["item_key"] for item in view["items"]]
        assert answered(await call(client, "GET", "page"))["muted"] == ["tasks_due"]
        unmute = {"name": "unmute", "candidate": "tasks_due"}
        assert answered(await call(client, "POST", "preferences", unmute))["muted"] == []
        page = answered(await call(client, "DELETE", "key"))
        assert page["model"]["key"] == "missing" and rig.keys.load() is None
        deleted = answered(await call(client, "DELETE", "episodes"))
        assert list(deleted) == ["deleted"] and deleted["deleted"] > 0
    assert rig.rows("decision_episodes") == []


async def test_the_real_services_refusals_answer_their_codes(rig: Rig) -> None:
    listed = item_key("tasks_due", TASKS, SEEDLINGS)
    start = {"item_key": listed, "work": "prepare"}
    rig.observation = observation(task(SEEDLINGS))
    async with opened(rig.service) as client:
        answered(await call(client, "POST", "launcher/open"))

        async def refused(method: str, path: str, body: Mapping[str, Any] | None = None) -> Any:
            response = await call(client, method, path, body)
            assert response.headers["cache-control"] == "no-store"
            return response.status_code, response.json()

        rig.values["decision.grant.prepare"] = "off"
        assert await refused("POST", "missions", start) == (409, refusal("not_applicable"))
        rig.values["decision.grant.prepare"] = "allowed"
        rig.values["decision.limit.drafts"] = 0
        assert await refused("POST", "missions", start) == (409, refusal("limit"))
        rig.values["decision.limit.drafts"] = 5
        answered(await call(client, "POST", "missions", start))
        busy = await refused("POST", "missions", {**start, "work": "run"})  # one is open
        assert busy == (429, refusal("busy"))
        answered(await call(client, "POST", "missions/stop"))

        unknown = {**FEEDBACK, "item_key": item_key("tasks_due", TASKS, "Not a task")}
        assert await refused("POST", "feedback", unknown) == (404, refusal("unknown_item"))
        answered(await call(client, "POST", "feedback", {**FEEDBACK, "item_key": listed}))
        closed = await refused("POST", "feedback", {**FEEDBACK, "item_key": listed})
        assert closed == (409, refusal("not_applicable"))
        assert await refused("POST", "missions", start) == (409, refusal("not_applicable"))
        assert await refused("GET", f"calls/{MISSING}") == (404, refusal("unknown_item"))
        stop = await refused("POST", f"missions/{MISSING}/stop")
        assert stop == (404, refusal("unknown_item"))
        bad_key = await refused("POST", "key", {"key": f"{KEY} {KEY}"})
        assert bad_key == (400, refusal("invalid_request"))
    assert rig.keys.load() is None


async def test_the_seat_routes_map_refusals(tmp_path: Path) -> None:
    host = seat_host(seat())
    rig = Rig(tmp_path, seats=host)
    try:
        async with opened(rig.service) as client:
            start = f"seats/{ROUTINE}/start"
            block = answered(await call(client, "POST", start))
            assert block["team"] == {"today": 0, "limit": 4, "running": 0}
            assert answered(await call(client, "POST", f"seats/{ROUTINE}/stop"))["running"] == []
            assert [name for name, *_rest in host.calls if name in ("start", "stop")] == [
                "start",
                "stop",
            ]
            for refusal_code, status in (("limit", 409), ("busy", 429), ("unavailable", 503)):
                host.refuse = refusal_code
                refused_with(await call(client, "POST", start), status, refusal(refusal_code))
            host.refuse = "not_applicable"
            refused_with(await call(client, "POST", start), 409, refusal("not_applicable"))
            host.refuse = None
            rig.policy = "existing_readonly"  # the service itself, before the port
            refused_with(await call(client, "POST", start), 409, refusal("not_applicable"))
            rig.policy = "ordinary"
            other = f"seats/{RIVAL}/start"  # a routine the facts do not list
            refused_with(await call(client, "POST", other), 409, refusal("not_applicable"))
            for path in ("seats/not-an-id/start", f"seats/{ROUTINE.upper()}/stop"):
                refused_with(await call(client, "POST", path), 404, refusal("unknown_item"))
            refused_with(
                await call(client, "POST", start, {"routine_id": ROUTINE}),
                415,
                refusal("invalid_representation"),
            )
    finally:
        await asyncio.wait_for(rig.engine.stop(), HANG_GUARD_S)
    assert [call_[0] for call_ in host.calls if call_[0] == "start"] == ["start"] * 5


async def test_the_check_routes_map_refusals(tmp_path: Path) -> None:
    host = check_host()
    rig = Rig(tmp_path, checks=host)
    try:
        check_item(rig)
        async with opened(rig.service) as client:
            run, stop = f"checks/{FOLDER}/run", f"checks/{FOLDER}/stop"
            block = answered(await call(client, "POST", run))
            assert block["checks"] == {"today": 1, "limit": 3, "running": 0}
            assert answered(await call(client, "POST", stop))["checks"]["running"] == 0
            assert [name for name, *_rest in host.calls if name in ("start", "stop")] == [
                "start",
                "stop",
            ]
            host.refuse = "unavailable"
            refused_with(await call(client, "POST", run), 503, refusal("unavailable"))
            host.refuse = "not_applicable"  # the commit moved under the start
            refused_with(await call(client, "POST", run), 409, refusal("not_applicable"))
            host.refuse = None
            host.started = 0  # every check was running or refused already
            refused_with(await call(client, "POST", run), 429, refusal("busy"))
            host.started = 1
            rig.policy = "existing_readonly"  # the service itself, before the start
            refused_with(await call(client, "POST", run), 409, refusal("not_applicable"))
            rig.policy = "ordinary"
            host.one_folder = check_folder("running")  # not waiting
            refused_with(await call(client, "POST", run), 409, refusal("not_applicable"))
            host.one_folder = None
            refused_with(await call(client, "POST", run), 409, refusal("not_applicable"))
            for path in ("checks/not-an-id/run", f"checks/{FOLDER.upper()}/stop"):
                refused_with(await call(client, "POST", path), 404, refusal("unknown_item"))
            refused_with(
                await call(client, "POST", run, {"workspace_id": FOLDER}),
                415,
                refusal("invalid_representation"),
            )
    finally:
        await asyncio.wait_for(rig.engine.stop(), HANG_GUARD_S)
    # The runs the service refused itself never reached the port.
    assert [call_[0] for call_ in host.calls if call_[0] == "start"] == ["start"] * 4


async def test_the_inbox_route_maps_refusals(tmp_path: Path) -> None:
    host = FakeInboxHost()
    rig = Rig(tmp_path, inbox=host)
    try:
        triage_item(rig)
        async with opened(rig.service) as client:
            suggest = "inbox/suggest"
            block = answered(await call(client, "POST", suggest, INBOX_SUGGEST))
            assert block["inbox"] == {"today": 1, "limit": 3}
            assert host.calls == [("start", SCOPE, TRIAGE)]
            for refusal_code, status in (
                ("busy", 429),
                ("not_applicable", 409),
                ("unavailable", 503),
            ):
                host.refuse = refusal_code
                response = await call(client, "POST", suggest, INBOX_SUGGEST)
                refused_with(response, status, refusal(refusal_code))
            host.refuse = None
            rig.policy = "existing_readonly"  # the service itself, before the port
            refused_with(
                await call(client, "POST", suggest, INBOX_SUGGEST), 409, refusal("not_applicable")
            )
            rig.policy = "ordinary"
            unknown = {"item_key": item_key("inbox_triage", SCOPE, "another state")}
            refused_with(
                await call(client, "POST", suggest, unknown), 409, refusal("not_applicable")
            )
            ready = item_key("inbox_triage", SCOPE, "suggestions wait")
            triage_item(rig, kind="ready", key=ready)  # not a suggestion: nothing to start
            refused_with(
                await call(client, "POST", suggest, {"item_key": ready}),
                409,
                refusal("not_applicable"),
            )
            for bad in (
                {"item_key": "not-a-key"},
                {"item_key": TRIAGE.upper()},
                {},
                {"key": TRIAGE},
            ):
                refused_with(
                    await call(client, "POST", suggest, bad), 400, refusal("invalid_request")
                )
            refused_with(
                await call(client, "POST", suggest), 415, refusal("invalid_representation")
            )
    finally:
        await asyncio.wait_for(rig.engine.stop(), HANG_GUARD_S)
    # Only the click and the host's three refusals reached the port.
    assert [call_[0] for call_ in host.calls] == ["start"] * 4


async def test_the_inbox_route_needs_a_port(rig: Rig) -> None:
    triage_item(rig)
    async with opened(rig.service) as client:
        response = await call(client, "POST", "inbox/suggest", INBOX_SUGGEST)
    refused_with(response, 503, refusal("unavailable"))


async def test_reset_learning_over_http(rig: Rig) -> None:
    rig.store.set_json(SCOPE, "learned", LEARNED, WALL)
    reset = {"name": "reset_learning"}
    async with opened(rig.service) as client:
        assert answered(await call(client, "GET", "page"))["learning"]["fitted_at"] == stamp(WALL)
        page = answered(await call(client, "POST", "preferences", reset))
        named = await call(client, "POST", "preferences", {**reset, "candidate": "inbox"})
    assert set(page) == set(wire.example_views()["page"])
    assert (page["learning"]["fitted_at"], page["learning"]["since"]) == (None, stamp(WALL))
    assert rig.store.get_json(SCOPE, "learned") is None and rig.store.learn_since(SCOPE) == WALL
    refused_with(named, 400, refusal("invalid_request"))


# ---- question wording proposals -------------------------------------------------


async def test_create_proposal_route(rig: Rig) -> None:
    """The user's Propose this wording: a wording `playbook.check_question` takes is sealed as
    the workspace's open proposal, on trial from the next cycle, and answered as its Approvals
    inbox item, which its view embeds beside the wording in use."""
    async with opened(rig.service) as client:
        created = answered(await call(client, "POST", "proposals", PROPOSAL))
        assert list(created) == ["proposal"]
        proposal = created["proposal"]
        view = answered(await call(client, "GET", f"proposals/{proposal['id']}"))
    (record,) = rig.engine.proposals.list(SCOPE)
    examples = wire.example_views()["question_proposal"]
    assert proposal == rig.engine.proposals.item(record)
    assert set(proposal) == set(examples["proposal"])
    assert (proposal["state"], proposal["attention"], proposal["name"]) == (
        "open",
        True,
        "goal_link",
    )
    assert proposal["origin"] == {"workspace_id": SCOPE, "session_id": None}
    assert (proposal["title"], proposal["created_at"]) == (
        "Question change: Linked to a goal?",
        stamp(WALL),
    )
    assert set(view) == set(examples)
    assert (view["proposal"], view["label"]) == (proposal, "Linked to a goal?")
    assert view["current_text"] == pretty(playbook.questions()["goal_link"])
    assert view["proposed_text"] == pretty(PROPOSED)
    assert view["tried"]["decisions"] == 0
    (row,) = rig.rows("decision_proposals")
    assert ASKED.encode() not in row["body"]  # sealed at rest
    assert rig.engine.proposals.wording(SCOPE)[2] == ("goal_link", PROPOSED)
    assert rig.engine.wakes == ["change"]


async def test_proposal_routes_refuse_as_their_codes_say(rig: Rig) -> None:
    """A wording the check refuses is invalid_request; the wording in use, or a second proposal
    while one is open, not_applicable. An act on an id the workspace does not hold is
    unknown_item; one at a revision that moved, or that does not fit the state, not_applicable."""
    async with opened(rig.service) as client:

        async def refused(method: str, path: str, body: Mapping[str, Any] | None = None) -> Any:
            response = await call(client, method, path, body)
            assert response.headers["cache-control"] == "no-store"
            return response.status_code, response.json()

        unfocused = {"question": ASKED, "focus": "Weigh the goals."}
        for body in (
            {"name": "importance", "question": PROPOSED},  # only a yes/no question is reworded
            {"name": "goal_link", "question": worded(f"{ASKED}​")},  # a hidden character
            {"name": "goal_link", "question": {**PROPOSED, "instructions": unfocused}},
            {"name": "goal_link", "question": {**PROPOSED, "extra": True}},
        ):
            assert await refused("POST", "proposals", body) == (400, refusal("invalid_request"))
        in_use = {"name": "goal_link", "question": playbook.questions()["goal_link"]}
        assert await refused("POST", "proposals", in_use) == (409, refusal("not_applicable"))
        assert rig.rows("decision_proposals") == []

        proposal = answered(await call(client, "POST", "proposals", PROPOSAL))["proposal"]
        second = {"name": "goal_link", "question": worded("Does `candidate` serve a goal?")}
        assert await refused("POST", "proposals", second) == (409, refusal("not_applicable"))
        path, revision = f"proposals/{proposal['id']}", proposal["revision"]
        approve = {"action": "approve", "revision": revision, "reason": None}
        reject = {"action": "reject", "revision": revision, "reason": " Too vague "}
        assert await refused("GET", f"proposals/{MISSING}") == (404, refusal("unknown_item"))
        unknown = await refused("POST", f"proposals/{MISSING}", approve)
        assert unknown == (404, refusal("unknown_item"))
        moved = await refused("POST", path, {**approve, "revision": "0" * 64})
        assert moved == (409, refusal("not_applicable"))
        reasoned = await refused("POST", path, {**approve, "reason": "Reads better"})
        assert reasoned == (400, refusal("invalid_request"))  # a reason comes with a rejection

        rejected = answered(await call(client, "POST", path, reject))["proposal"]
        assert (rejected["state"], rejected["attention"], rejected["reason"]) == (
            "rejected",
            False,
            "Too vague",
        )
        again = await refused("POST", path, reject)  # its revision moved with the rejection
        assert again == (409, refusal("not_applicable"))
        late = await refused("POST", path, {**approve, "revision": rejected["revision"]})
        assert late == (409, refusal("not_applicable"))  # a rejected proposal is not approved
    assert [(record.state, record.reason) for record in rig.engine.proposals.list(SCOPE)] == [
        ("rejected", "Too vague")
    ]


async def test_proposal_routes_approve_then_undo(rig: Rig) -> None:
    """Approving an open proposal puts its wording in use under a version of its own and ends
    its trial; Undo, a rejection of the wording in use, brings back the one before."""
    wording = {"goal_link": PROPOSED}
    async with opened(rig.service) as client:
        proposal = answered(await call(client, "POST", "proposals", PROPOSAL))["proposal"]
        path = f"proposals/{proposal['id']}"
        approve = {"action": "approve", "revision": proposal["revision"], "reason": None}
        approved = answered(await call(client, "POST", path, approve))["proposal"]
        assert (approved["state"], approved["attention"], approved["approved_at"]) == (
            "approved",
            False,
            stamp(WALL),
        )
        assert approved["revision"] != proposal["revision"]
        assert rig.engine.proposals.wording(SCOPE) == (wording, playbook.version(wording), None)
        view = answered(await call(client, "GET", path))
        assert view["current_text"] == view["proposed_text"] == pretty(PROPOSED)
        undo = {"action": "reject", "revision": approved["revision"], "reason": None}
        undone = answered(await call(client, "POST", path, undo))["proposal"]
    assert (undone["state"], undone["rejected_at"], undone["reason"]) == (
        "rejected",
        stamp(WALL),
        None,
    )
    assert rig.engine.proposals.wording(SCOPE) == ({}, playbook.PLAYBOOK_VERSION, None)
    assert rig.engine.wakes == ["change"] * 3


# ---- vault-change proposals ------------------------------------------------------------


async def test_change_route_approves_drafts_again_and_rejects(rig: Rig) -> None:
    """Approve answers the change approved with its draft, Draft again the same answer, and
    Reject the change rejected, its reason stripped, with no draft: each in the shape of the
    client's example, the change as the inbox now lists it. An act that moves a change's state
    labels the episode it was filed for, from the page."""
    rows = await filed(rig, TASKS, GARDEN)
    example = wire.example_views()["change_act"]
    async with opened(rig.service) as client:

        async def acted(row: Mapping[str, Any], action: str, reason: str | None = None) -> Any:
            body = {"action": action, "revision": row["revision"], "reason": reason}
            return answered(await call(client, "POST", f"changes/{row['id']}", body))

        approved = await acted(rows[TASKS], "approve")
        again = await acted(approved["change"], "approve")
        rejected = await acted(rows[GARDEN], "reject", " Not now ")
    assert set(approved) == set(example) and set(approved["change"]) == set(example["change"])
    draft = change_draft("tasks_ticked", TASKS, None, ARCHIVES[TASKS])
    assert (approved["change"]["state"], approved["draft"]) == ("approved", draft)
    assert again == approved
    assert (rejected["change"]["state"], rejected["change"]["reason"], rejected["draft"]) == (
        "rejected",
        "Not now",
        None,
    )
    listed = {item["id"]: item for item in rig.engine.changes.inbox_items(SCOPE)}
    assert listed == {rows[TASKS]["id"]: approved["change"], rows[GARDEN]["id"]: rejected["change"]}
    assert [label(filed_episode(rig, ref))[:3] for ref in (TASKS, GARDEN)] == [
        ("useful", None, "page"),
        ("wrong", None, "page"),
    ]
    assert rig.engine.wakes == ["change"] * 3


async def test_change_route_refuses_as_its_codes_say(rig: Rig) -> None:
    """A malformed act is 400 invalid_request; an id that names no change of the workspace (a
    question proposal's among them) 404 unknown_item; a revision that moved, or a state the
    action does not fit, 409 not_applicable. A refusal writes and labels nothing."""
    plan = (await filed(rig, TASKS))[TASKS]
    path = f"changes/{plan['id']}"
    approve = {"action": "approve", "revision": plan["revision"], "reason": None}
    async with opened(rig.service) as client:

        async def refused(path: str, body: Mapping[str, Any]) -> Any:
            response = await call(client, "POST", path, body)
            assert response.headers["cache-control"] == "no-store"
            return response.status_code, response.json()

        question = answered(await call(client, "POST", "proposals", PROPOSAL))["proposal"]
        before = (rig.rows("decision_proposals"), rig.rows("decision_episodes"))
        for body in (
            {**approve, "reason": "Reads well"},  # a reason comes with a rejection
            {**approve, "action": "undo"},
            {**approve, "revision": str(plan["revision"]).upper()},
            {**approve, "action": "reject", "reason": "One\nTwo"},
            {**approve, "action": "reject", "reason": "   "},
            {**approve, "action": "reject", "reason": "x" * 201},
        ):
            assert await refused(path, body) == (400, refusal("invalid_request")), body
        assert await refused(f"changes/{MISSING}", approve) == (404, refusal("unknown_item"))
        crossed = {**approve, "revision": question["revision"]}
        assert await refused(f"changes/{question['id']}", crossed) == (
            404,
            refusal("unknown_item"),
        )
        moved = {**approve, "revision": "0" * 64}
        assert await refused(path, moved) == (409, refusal("not_applicable"))
        assert (rig.rows("decision_proposals"), rig.rows("decision_episodes")) == before

        reject = {**approve, "action": "reject"}
        rejected = answered(await call(client, "POST", path, reject))["change"]
        late = {**approve, "revision": rejected["revision"]}
        assert await refused(path, late) == (409, refusal("not_applicable"))
        again = {**reject, "revision": rejected["revision"]}
        assert await refused(path, again) == (409, refusal("not_applicable"))
    assert label(filed_episode(rig, TASKS))[:3] == ("wrong", None, "page")


# ---- what no response holds -------------------------------------------------------------


async def test_no_response_carries_the_key(
    rig: Rig, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = KEY.encode()
    listed = item_key("tasks_due", TASKS, SEEDLINGS)
    rig.observation = observation(task(SEEDLINGS))
    rig.model.answers = dict(STRONG)
    responses: list[httpx.Response] = []
    with caplog.at_level(logging.DEBUG, logger="viki_agent"):
        async with opened(rig.service) as client:

            async def kept(
                method: str,
                path: str,
                content: bytes | Mapping[str, Any] | None = None,
                headers: Mapping[str, str | None] | None = None,
            ) -> httpx.Response:
                response = await call(client, method, path, content, headers=headers)
                responses.append(response)
                return response

            answered(await kept("POST", "key", {"key": KEY}))
            answered(await kept("POST", "launcher/open"))  # the Decision model decides
            assert set(rig.model.keys) == {KEY}  # the key went to the model alone
            call_id = answered(await kept("GET", "calls"))["calls"][0]["call_id"]
            start = {"item_key": listed, "work": "prepare"}
            mission_id = answered(await kept("POST", "missions", start))["mission_id"]
            every: list[tuple[str, str, Mapping[str, Any] | None]] = [
                ("GET", "launcher", None),
                ("POST", "launcher/open", None),
                ("POST", "presence", PRESENCE),
                ("POST", "observe", {"event": "shown", "item_keys": [listed]}),
                ("POST", "reports", REPORT),
                ("GET", "page", None),
                ("POST", "preferences", PREFERENCE),
                ("POST", "test", None),
                ("GET", "calls", None),
                ("GET", f"calls/{call_id}", None),
                ("GET", "missions", None),
                ("POST", "missions", start),
                ("POST", f"missions/{mission_id}/stop", None),
                ("POST", "missions/stop", None),
                ("POST", "feedback", {**FEEDBACK, "item_key": listed}),
                ("POST", "key", {"key": KEY}),
                ("POST", "proposals", PROPOSAL),
                ("GET", f"proposals/{MISSING}", None),
                ("POST", f"proposals/{MISSING}", ACT),
                ("POST", f"changes/{MISSING}", ACT),
                ("DELETE", "episodes", None),
            ]
            for method, path, body in every:
                await kept(method, path, body)
                await kept(method, path, body, {"Authorization": f"Bearer {KEY}"})
                await kept(method, path, body, {"Origin": FOREIGN})
                await kept(method, f"{path}?key={KEY}", body)
                await kept(method, path, secret, {"Content-Type": "text/plain"})
                representation = None if body is None else JSON_TYPE["Content-Type"]
                await kept(method, path, secret * 500, {"Content-Type": representation})
                echo = json.dumps({"key": KEY, "extra": KEY}).encode()
                await kept(method, path, echo, {"Content-Type": representation})
            await kept("POST", "key", {"key": f"{KEY} {KEY}"})
            twice = f'{{"key": "{KEY}", "key": "{KEY}"}}'.encode()
            await kept("POST", "key", twice, JSON_TYPE)
            await kept("GET", f"calls/{KEY}")
            await kept("POST", f"missions/{KEY}/stop")

            def failing(*args: object, **kwargs: object) -> None:
                raise RuntimeError(f"agenda failed near {KEY}")

            with monkeypatch.context() as patch:
                patch.setattr(rig.agenda, "calls", failing)
                await kept("GET", "calls")
            answered(await kept("DELETE", "key"))
    statuses = {response.status_code for response in responses}
    assert statuses >= {200, 204, 400, 401, 403, 404, 413, 415, 429, 503}
    for response in responses:
        assert secret not in response.content, response.request.url
        assert all(
            secret not in name and secret not in value for name, value in response.headers.raw
        )
    for record in caplog.records:
        assert KEY not in record.getMessage() and KEY not in repr(record.args)
    assert KEY not in caplog.text and rig.keys.load() is None


async def test_no_response_names_a_model_id(rig: Rig) -> None:
    rig.observation = observation(task(SEEDLINGS))
    rig.model.answers = dict(STRONG)
    async with opened(rig.service) as client:
        answered(await call(client, "POST", "key", {"key": KEY}))
        answered(await call(client, "POST", "launcher/open"))  # the Decision model decides
        listed = answered(await call(client, "GET", "calls"))["calls"]
        responses = [await call(client, "GET", f"calls/{entry['call_id']}") for entry in listed]
        responses.append(await call(client, "POST", "test"))
    assert len(responses) == 3  # two calls (the playbook's questions and the reversed one), a test
    for response in responses:
        assert answered(response)["version"] == "1.14.0"
        text = response.text.lower()
        assert "jev" not in text
        assert not [model for model in MODEL_IDS.values() if model.lower() in text]
    assert {row["model"] for row in rig.rows("decision_calls")} == {MODEL_IDS["pinned"]}
