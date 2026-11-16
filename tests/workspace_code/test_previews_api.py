"""The preview routes over a stub service recording each call.

The gate (Bearer, Origin, the platform, path ids, query and body) refuses before any service
call; each code the service raises answers its status with a fixed message that holds no
command, port or id; a client that leaves does not cancel a start partway.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict
from typing import Any

import httpx
import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from tests.workspace_code.test_code_api import ORIGIN
from tests.workspace_code.test_roots_api import ALPHA, AUTH, BEARER, HANG_GUARD_S, JSON
from viki_agent.code import previews, previews_api, roots_api
from viki_agent.code.check_output import Slice
from viki_agent.code.code_checks import CodeRunError
from viki_agent.code.previews import PreviewView
from viki_agent.code.previews_api import MAX_BODY_BYTES, MAX_PREVIEW_REQUESTS, preview_routes
from viki_agent.storage import StateError

WS = "11111111-1111-4111-8111-111111111111"
WT = "22222222-2222-4222-8222-222222222222"
PREVIEW = "33333333-3333-4333-8333-333333333333"
OTHER = "44444444-4444-4444-8444-444444444444"
COMMAND = "secret-command && npm run dev"
PORT = 43219
SECRETS = (WS, WT, PREVIEW, OTHER, COMMAND, str(PORT), "secret")
CREATED = "2026-10-04T00:00:00.000Z"
ENDED = "2026-10-04T00:01:00.000Z"

LIST = f"/api/v2/code/{WS}/previews"
GETS = {"list": LIST, "output": f"/api/v2/previews/{PREVIEW}/output?after=10"}
POSTS: dict[str, tuple[str, dict[str, Any]]] = {
    "start": (LIST, {"worktree_id": WT, "command": COMMAND, "port": PORT}),
    "stop": (f"/api/v2/previews/{PREVIEW}/stop", {}),
    "open": (f"/api/v2/previews/{PREVIEW}/open", {}),
    "remove": (f"/api/v2/previews/{PREVIEW}/remove", {}),
}
ROUTES = [*GETS, *POSTS]
PATH_IDS = {route: (PREVIEW, "unknown_preview") for route in ROUTES} | {
    "list": (WS, "unknown_workspace"),
    "start": (WS, "unknown_workspace"),
}
SUCCESS = {"start": 201}
P_KEYS = [
    "preview_id",
    "workspace_id",
    "worktree_id",
    "command",
    "port",
    "state",
    "pid",
    "listener_pid",
    "exit_code",
    "started_at",
    "ended_at",
]
LISTENING = PreviewView(
    PREVIEW, WS, WT, COMMAND, PORT, "listening", 4242, 4343, None, CREATED, None
)
EXITED = PreviewView(OTHER, WS, None, COMMAND, PORT + 1, "exited", 4244, None, 3, CREATED, ENDED)


def _p(view: PreviewView) -> dict[str, Any]:
    """The preview as the wire answers it."""
    answer = asdict(view)
    assert list(answer) == P_KEYS
    return answer


class Stub:
    """Stands in for `PreviewService`: records each call and answers a well-formed result, or
    raises what it was given. With `gate` a call waits there."""

    def __init__(self, raises: BaseException | None = None) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.raises = raises
        self.gate: asyncio.Event | None = None
        self.entered: asyncio.Event | None = None
        self.expect = 1  # `entered` is set by this many calls
        self.log: list[str] = []
        self.slice = Slice(10, 14, "viki", True)
        self.done = True

    async def _operate(self, name: str, args: tuple[Any, ...]) -> None:
        self.calls.append((name, args))
        if self.entered is not None and len(self.calls) == self.expect:
            self.entered.set()
        self.log.append("started")
        if self.gate is not None:
            await self.gate.wait()
        if self.raises is not None:
            raise self.raises
        self.log.append("finished")

    async def list(self, *args: Any) -> list[PreviewView]:
        await self._operate("list", args)
        return [LISTENING, EXITED]

    async def start(self, *args: Any) -> PreviewView:
        await self._operate("start", args)
        return LISTENING

    async def stop(self, *args: Any) -> PreviewView:
        await self._operate("stop", args)
        return EXITED

    async def open(self, *args: Any) -> None:
        await self._operate("open", args)

    async def remove(self, *args: Any) -> None:
        await self._operate("remove", args)

    async def output(self, *args: Any) -> tuple[PreviewView, Slice, bool]:
        await self._operate("output", args)
        return EXITED, self.slice, self.done


@pytest.fixture(autouse=True)
def _on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """The routes run on every OS here; the platform refusal has its own test."""
    monkeypatch.setattr(previews, "PREVIEWS_SUPPORTED", True)


def _app(stub: Any, workers: set[asyncio.Task[Any]] | None = None) -> Starlette:
    return Starlette(
        routes=preview_routes(
            stub,
            authorized=lambda request: request.headers.get("authorization") == "Bearer secret",
            origins=frozenset({ORIGIN}),
            workers=set() if workers is None else workers,
        )
    )


def post(client: TestClient, url: str, fields: Any = None, **kwargs: Any) -> Any:
    content = kwargs.pop("content", None)
    return client.post(
        url, content=json.dumps(fields) if content is None else content, headers=JSON
    )


def call(client: TestClient, route: str, **headers: str) -> Any:
    """The route's well-formed request, with `headers` replacing the defaults."""
    if route in GETS:
        return client.get(GETS[route], headers=headers if headers else AUTH)
    url, fields = POSTS[route]
    return client.post(url, content=json.dumps(fields), headers=headers if headers else JSON)


def _refused(response: Any, status: int, code: str) -> dict[str, Any]:
    assert response.status_code == status, response.text
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert list(body) == ["error"]
    error = body["error"]
    assert set(error) == {"code", "message"}
    assert error["code"] == code and error["message"] and error["message"].isascii()
    for secret in SECRETS:
        assert secret not in response.text and secret not in error["message"]
    assert response.content.isascii()
    return error


def _answer(response: Any, status: int = 200) -> Any:
    assert response.status_code == status, response.text
    assert response.headers["cache-control"] == "no-store"
    return response.json()


# --- the six answers ----------------------------------------------------------------------------


def test_previews_list_answers_the_contract() -> None:
    stub = Stub()
    with TestClient(_app(stub)) as client:
        body = _answer(client.get(LIST, headers=AUTH))
        plain = _answer(client.get(LIST, headers=BEARER))
    assert list(body) == ["previews"]
    assert [list(preview) for preview in body["previews"]] == [P_KEYS, P_KEYS]
    assert body == {
        "previews": [
            {
                "preview_id": PREVIEW,
                "workspace_id": WS,
                "worktree_id": WT,
                "command": COMMAND,
                "port": PORT,
                "state": "listening",
                "pid": 4242,
                "listener_pid": 4343,
                "exit_code": None,
                "started_at": CREATED,
                "ended_at": None,
            },
            {
                "preview_id": OTHER,
                "workspace_id": WS,
                "worktree_id": None,
                "command": COMMAND,
                "port": PORT + 1,
                "state": "exited",
                "pid": 4244,
                "listener_pid": None,
                "exit_code": 3,
                "started_at": CREATED,
                "ended_at": ENDED,
            },
        ]
    }
    assert plain == body
    assert stub.calls == [("list", (WS,)), ("list", (WS,))]


def test_start_answers_201_and_leaves_the_command_and_the_port_range_to_the_service() -> None:
    stub = Stub()
    url, fields = POSTS["start"]
    with TestClient(_app(stub)) as client:
        body = _answer(post(client, url, fields), 201)
        # The command's rules and the port's range (80, 8765) are the service's.
        _answer(post(client, url, {"worktree_id": None, "command": "a\nb", "port": 80}), 201)
        _answer(post(client, url, {"worktree_id": None, "command": "", "port": 8765}), 201)
    assert body == {"preview": _p(LISTENING)}
    assert list(body["preview"]) == P_KEYS
    assert stub.calls == [
        ("start", (WS, WT, COMMAND, PORT)),
        ("start", (WS, None, "a\nb", 80)),
        ("start", (WS, None, "", 8765)),
    ]


def test_the_routes_by_id_answer_the_contract() -> None:
    stub = Stub()
    with TestClient(_app(stub)) as client:
        stopped = _answer(call(client, "stop"))
        opened = _answer(call(client, "open"))
        removed = _answer(call(client, "remove"))
        output = _answer(call(client, "output"))
    assert stopped == {"preview": _p(EXITED)}
    assert opened == {"opened": True}
    assert removed == {"removed": True}
    assert output == {
        "preview": _p(EXITED),
        "start": 10,
        "next": 14,
        "text": "viki",
        "dropped": True,
        "done": True,
    }
    assert list(output) == ["preview", "start", "next", "text", "dropped", "done"]
    assert stub.calls == [
        ("stop", (PREVIEW,)),
        ("open", (PREVIEW,)),
        ("remove", (PREVIEW,)),
        ("output", (PREVIEW, 10)),
    ]


@pytest.mark.parametrize(("dropped", "done"), [(True, True), (False, False), (True, False)])
def test_output_carries_the_services_dropped_and_done(dropped: bool, done: bool) -> None:
    # A live preview's view is not done: the UI stops reading at the first `done`.
    stub = Stub()
    stub.slice, stub.done = Slice(10, 14, "viki", dropped), done
    with TestClient(_app(stub)) as client:
        output = _answer(call(client, "output"))
    assert (output["dropped"], output["done"]) == (dropped, done)


@pytest.mark.parametrize(
    ("query", "after"),
    [("", 0), ("?after=9007199254740991", 9_007_199_254_740_991), ("?after=0000000000000001", 1)],
)
def test_output_reads_from_the_offset_asked(query: str, after: int) -> None:
    stub = Stub()
    with TestClient(_app(stub)) as client:
        _answer(client.get(f"/api/v2/previews/{PREVIEW}/output{query}", headers=AUTH))
    assert stub.calls == [("output", (PREVIEW, after))]


# --- the gate: Bearer, Origin, the platform, path ids -------------------------------------------


@pytest.mark.parametrize("route", ROUTES)
def test_every_route_needs_bearer(route: str) -> None:
    stub = Stub()
    extra = {"Content-Type": "application/json"} if route in POSTS else {}
    with TestClient(_app(stub)) as client:
        for bearer in ({}, {"Authorization": "Bearer other"}):
            _refused(call(client, route, Origin=ORIGIN, **bearer, **extra), 401, "unauthorized")
    assert stub.calls == []


@pytest.mark.parametrize("route", POSTS)
def test_a_post_needs_a_listed_origin(route: str) -> None:
    stub = Stub()
    with TestClient(_app(stub)) as client:
        _refused(
            call(client, route, **BEARER, **{"Content-Type": "application/json"}),
            403,
            "forbidden_origin",
        )
        _refused(
            call(client, route, **{**JSON, "Origin": "https://evil.invalid"}),
            403,
            "forbidden_origin",
        )
        assert stub.calls == []
        _answer(call(client, route), SUCCESS.get(route, 200))
    assert len(stub.calls) == 1


@pytest.mark.parametrize("route", GETS)
def test_a_get_may_omit_origin_but_not_name_a_foreign_one(route: str) -> None:
    stub = Stub()
    with TestClient(_app(stub)) as client:
        _answer(call(client, route, **BEARER))
        _answer(call(client, route))
        _refused(
            call(client, route, **{**BEARER, "Origin": "https://evil.invalid"}),
            403,
            "forbidden_origin",
        )
    assert len(stub.calls) == 2


@pytest.mark.parametrize("route", ROUTES)
def test_off_windows_every_route_answers_unsupported_platform_before_any_lookup(
    monkeypatch: pytest.MonkeyPatch, route: str
) -> None:
    monkeypatch.setattr(previews, "PREVIEWS_SUPPORTED", False)
    stub = Stub()
    real, _ = PATH_IDS[route]
    with TestClient(_app(stub)) as client:
        _refused(call(client, route), 409, "unsupported_platform")
        if route in POSTS:
            # Before the path id and the body too.
            response = post(client, POSTS[route][0].replace(real, "w"), {"unexpected": True})
        else:
            url = GETS[route].replace(real, "w")
            response = client.get(url + ("&" if "?" in url else "?") + "x=1", headers=AUTH)
        _refused(response, 409, "unsupported_platform")
    assert stub.calls == []


@pytest.mark.parametrize("route", ROUTES)
def test_a_non_canonical_path_id_is_404_with_the_route_s_code(route: str) -> None:
    real, unknown = PATH_IDS[route]
    stub = Stub()
    with TestClient(_app(stub)) as client:
        for raw in (
            ALPHA.upper(),
            f"{{{ALPHA}}}",
            ALPHA.replace("-", ""),
            f"urn:uuid:{ALPHA}",
            "w",
        ):
            if route in GETS:
                response = client.get(GETS[route].replace(real, raw, 1), headers=AUTH)
            else:
                url, fields = POSTS[route]
                response = post(client, url.replace(real, raw, 1), fields)
            _refused(response, 404, unknown)
    assert stub.calls == []


def test_another_method_is_405() -> None:
    stub = Stub()
    with TestClient(_app(stub)) as client:
        assert client.post(GETS["output"], headers=JSON).status_code == 405
        for route in ("stop", "open", "remove"):
            assert client.get(POSTS[route][0], headers=AUTH).status_code == 405
        assert client.delete(LIST, headers=AUTH).status_code == 405
    assert stub.calls == []


# --- the query ----------------------------------------------------------------------------------

BAD_QUERIES = {
    "list": ["x=1", f"worktree={WT}", "after=1", "a", "="],
    "output": [
        "after=-1",
        "after=1e3",
        "after=" + "1" * 17,
        "after=9007199254740992",
        "after=1&after=1",
        "after=",
        "after",
        "after=+1",
        "after=%201",
        "after=0x10",
        "after=%D9%A1",  # an Arabic-Indic digit: decimal, not ASCII
        "x=1",
        f"worktree={WT}",
    ],
}


@pytest.mark.parametrize(
    ("route", "query"),
    [(route, query) for route, queries in BAD_QUERIES.items() for query in queries],
)
def test_a_query_of_any_other_shape_is_400_invalid_query(route: str, query: str) -> None:
    url = GETS[route].split("?", 1)[0]
    stub = Stub()
    with TestClient(_app(stub)) as client:
        _refused(client.get(f"{url}?{query}", headers=AUTH), 400, "invalid_query")
    assert stub.calls == []


@pytest.mark.parametrize("query", ["?x=1", f"?worktree={WT}", "?after=1", "?a", "?="])
@pytest.mark.parametrize("route", POSTS)
def test_any_query_on_a_post_is_400_invalid_query(route: str, query: str) -> None:
    url, fields = POSTS[route]
    stub = Stub()
    with TestClient(_app(stub)) as client:
        _refused(post(client, url + query, fields), 400, "invalid_query")
    assert stub.calls == []


# --- the body -----------------------------------------------------------------------------------

DROP = object()
BAD_VALUES: dict[str, list[Any]] = {
    "worktree_id": ["", "x", ALPHA.upper(), ALPHA.replace("-", ""), 5, True],
    "command": [5, None, [COMMAND], {"x": 1}, True],
    "port": [str(PORT), True, False, float(PORT), None, [PORT], {"port": PORT}],
}


def _bad_bodies() -> list[Any]:
    cases = []
    for route, (_, fields) in POSTS.items():
        for key in fields:
            for index, value in enumerate([DROP, *BAD_VALUES[key]]):
                cases.append(pytest.param(route, key, value, id=f"{route}-{key}-{index}"))
        cases.append(pytest.param(route, "extra", 1, id=f"{route}-extra"))
    return cases


@pytest.mark.parametrize(("route", "key", "value"), _bad_bodies())
def test_a_body_of_any_other_shape_is_400_invalid_body(route: str, key: str, value: Any) -> None:
    url, fields = POSTS[route]
    if value is DROP:
        body = {name: item for name, item in fields.items() if name != key}
    else:
        body = {**fields, key: value}
    stub = Stub()
    with TestClient(_app(stub)) as client:
        _refused(post(client, url, body), 400, "invalid_body")
    assert stub.calls == []


@pytest.mark.parametrize("route", POSTS)
@pytest.mark.parametrize("raw", ["", "{", "[]", "null", '"x"', "5", "NaN", b"\xff\xfe"])
def test_a_body_that_is_not_one_json_object_is_400_invalid_body(
    route: str, raw: str | bytes
) -> None:
    url, _ = POSTS[route]
    stub = Stub()
    with TestClient(_app(stub)) as client:
        _refused(post(client, url, content=raw), 400, "invalid_body")
    assert stub.calls == []


def test_a_duplicate_key_is_400_invalid_body() -> None:
    url, fields = POSTS["start"]
    raw = json.dumps(fields)[:-1] + ', "port": 4000}'
    stub = Stub()
    with TestClient(_app(stub)) as client:
        _refused(post(client, url, content=raw), 400, "invalid_body")
    assert stub.calls == []


@pytest.mark.parametrize("route", POSTS)
@pytest.mark.parametrize(
    "headers",
    [
        {"Content-Type": "text/plain"},
        {"Content-Type": "application/json", "Content-Encoding": "gzip"},
    ],
)
def test_another_content_type_or_encoding_is_400_invalid_body(
    route: str, headers: dict[str, str]
) -> None:
    url, fields = POSTS[route]
    stub = Stub()
    with TestClient(_app(stub)) as client:
        response = client.post(url, content=json.dumps(fields), headers={**AUTH, **headers})
        _refused(response, 400, "invalid_body")
    assert stub.calls == []


@pytest.mark.parametrize("route", POSTS)
def test_a_body_over_65536_bytes_is_413_too_large_and_exactly_65536_passes(route: str) -> None:
    url, fields = POSTS[route]
    assert MAX_BODY_BYTES == 65_536
    stub = Stub()
    with TestClient(_app(stub)) as client:
        too_big = post(client, url, content=json.dumps(fields).ljust(MAX_BODY_BYTES + 1))
        _refused(too_big, 413, "too_large")
        assert stub.calls == []
        _answer(
            post(client, url, content=json.dumps(fields).ljust(MAX_BODY_BYTES)),
            SUCCESS.get(route, 200),
        )
    assert len(stub.calls) == 1


# --- every code the service can raise -----------------------------------------------------------

STATUS = {
    code: status
    for status, codes in {
        400: "invalid_body invalid_query invalid_command invalid_port disconnected",
        401: "unauthorized",
        403: "forbidden_origin",
        404: "unknown_workspace unknown_worktree unknown_preview",
        408: "intake_timeout",
        409: "trust_required root_unavailable worktree_not_ready port_reserved port_in_use "
        "port_taken preview_limit window_limit not_listening preview_running "
        "unsupported_platform",
        413: "too_large",
        429: "overloaded",
        503: "start_failed browser_missing unavailable",
    }.items()
    for code in codes.split()
}
MESSAGES = {
    "invalid_command": "Write the command on one line, up to 8,000 characters.",
    "invalid_port": "Use a port from 1024 to 65535.",
    "port_reserved": "Port 8765 is VIKI's own. Choose another.",
    "port_in_use": "Another preview uses this port.",
    "port_taken": "Another program uses this port, so VIKI does not open it.",
    "preview_limit": "Two previews are running. Stop one first.",
    "window_limit": "Four preview windows are open. Close one first.",
    "not_listening": "Nothing listens on this port yet. Wait for the server, then try again.",
    "preview_running": "Stop the preview first.",
    "unknown_preview": "This preview no longer exists. Refresh the list.",
    "unsupported_platform": "Previews work on Windows in this version.",
    "browser_missing": "No browser for preview windows was found.",
    "start_failed": "The command could not start. Check that the folder still exists.",
}
# The inert set's codes and the other route groups' are not this group's: they answer unavailable.
UNLISTED = (
    "a_code_nobody_mapped",
    "invalid_path",
    "not_a_file",
    "private_path",
    "not_found",
    "unsupported_file",
    "file_too_large",
    "check_limit",
    "worktree_in_use",
)


@pytest.mark.parametrize("route", ROUTES)
def test_each_service_code_has_its_status_and_a_fixed_message(
    caplog: pytest.LogCaptureFixture, route: str
) -> None:
    stub = Stub()
    seen: dict[str, str] = {}
    with caplog.at_level(logging.WARNING), TestClient(_app(stub)) as client:
        for code, status in STATUS.items():
            stub.raises = CodeRunError(code)
            seen[code] = _refused(call(client, route), status, code)["message"]
        for code in UNLISTED:
            stub.raises = CodeRunError(code)
            _refused(call(client, route), 503, "unavailable")
        stub.raises = StateError("unavailable")
        _refused(call(client, route), 503, "unavailable")
    shared = {code: roots_api._MESSAGES[code] for code in STATUS if code not in MESSAGES}
    assert seen == {**shared, **MESSAGES}
    assert len(stub.calls) == len(STATUS) + len(UNLISTED) + 1
    assert [r for r in caplog.records if r.name == previews_api.__name__] == []


@pytest.mark.parametrize("route", ROUTES)
def test_anything_else_is_503_unavailable_with_one_warning_naming_the_type(
    caplog: pytest.LogCaptureFixture, route: str
) -> None:
    stub = Stub(OSError(f"C:\\secret\\{COMMAND} {PORT} {PREVIEW} token"))
    with caplog.at_level(logging.WARNING), TestClient(_app(stub)) as client:
        _refused(call(client, route), 503, "unavailable")
    records = [r for r in caplog.records if r.name == previews_api.__name__]
    assert [(r.levelno, r.getMessage()) for r in records] == [
        (logging.WARNING, "previews route failed (OSError)")
    ]


@pytest.mark.parametrize("route", ROUTES)
def test_no_refusal_holds_the_command_a_port_or_an_id(
    caplog: pytest.LogCaptureFixture, route: str
) -> None:
    text = f"{COMMAND} on port {PORT} for {WS} {WT} {PREVIEW} {OTHER}"
    stub = Stub()
    with caplog.at_level(logging.WARNING), TestClient(_app(stub)) as client:
        for raised, status, code in (
            (RuntimeError(text), 503, "unavailable"),
            (OSError(text), 503, "unavailable"),
            (CodeRunError(text), 503, "unavailable"),
            (StateError("unavailable"), 503, "unavailable"),
            (CodeRunError("port_taken"), 409, "port_taken"),
        ):
            stub.raises = raised
            _refused(call(client, route), status, code)
        if route in POSTS:
            url, fields = POSTS[route]
            # A refused body that echoes the command, the port and an id.
            _refused(post(client, url, {**fields, "port": text}), 400, "invalid_body")
            _refused(post(client, url, {**fields, text: PORT}), 400, "invalid_body")
    for record in caplog.records:
        for secret in SECRETS:
            assert secret not in record.getMessage()


# --- capacity and cancellation ------------------------------------------------------------------


async def test_a_ninth_request_is_429_overloaded_across_the_group_and_the_permits_come_back() -> (
    None
):
    assert MAX_PREVIEW_REQUESTS == 8
    stub = Stub()
    stub.gate, stub.entered, stub.expect = asyncio.Event(), asyncio.Event(), MAX_PREVIEW_REQUESTS
    workers: set[asyncio.Task[Any]] = set()
    transport = httpx.ASGITransport(app=_app(stub, workers))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        url, fields = POSTS["start"]

        async def start() -> httpx.Response:
            return await client.post(url, content=json.dumps(fields), headers=JSON)

        calls = [asyncio.create_task(start()) for _ in range(MAX_PREVIEW_REQUESTS)]
        await asyncio.wait_for(stub.entered.wait(), HANG_GUARD_S)
        assert len(workers) == MAX_PREVIEW_REQUESTS
        _refused(await start(), 429, "overloaded")
        _refused(await client.get(GETS["output"], headers=AUTH), 429, "overloaded")
        stub.gate.set()
        assert all(r.status_code == 201 for r in await asyncio.gather(*calls))
        await asyncio.sleep(0)
        assert not workers
        assert (await client.get(LIST, headers=AUTH)).status_code == 200


@pytest.mark.parametrize("route", ["start", "stop", "open", "remove"])
async def test_a_client_that_leaves_does_not_cancel_the_operation(route: str) -> None:
    stub = Stub()
    stub.gate, stub.entered = asyncio.Event(), asyncio.Event()
    workers: set[asyncio.Task[Any]] = set()
    transport = httpx.ASGITransport(app=_app(stub, workers))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        url, fields = POSTS[route]
        request = asyncio.create_task(client.post(url, content=json.dumps(fields), headers=JSON))
        await asyncio.wait_for(stub.entered.wait(), HANG_GUARD_S)
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        # The client is gone; the start still runs, so its process gets its supervisor.
        assert stub.log == ["started"] and len(workers) == 1
        stub.gate.set()
        await asyncio.wait_for(asyncio.gather(*workers), HANG_GUARD_S)
        await asyncio.sleep(0)
    assert stub.log == ["started", "finished"] and not workers
