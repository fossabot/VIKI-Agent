"""The checks routes over stub services recording each call.

The gate (Bearer, Origin, the platform, path ids, query and body) refuses before any service
call; each code a service raises answers its status with a fixed message that holds no name,
command or id; a client that leaves does not cancel a start partway.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import httpx
import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from tests.workspace_code.test_code_api import ORIGIN
from tests.workspace_code.test_roots_api import ALPHA, AUTH, BEARER, HANG_GUARD_S, JSON
from viki_agent.code import checks_api, code_checks
from viki_agent.code.check_output import Slice
from viki_agent.code.checks_api import MAX_BODY_BYTES, MAX_CHECK_REQUESTS, check_routes
from viki_agent.code.code_checks import CheckView, CodeRunError
from viki_agent.storage import CheckRunRow, StateError

WS = "11111111-1111-4111-8111-111111111111"
WT = "22222222-2222-4222-8222-222222222222"
CHECK = "33333333-3333-4333-8333-333333333333"
RUN = "44444444-4444-4444-8444-444444444444"
OTHER_RUN = "55555555-5555-4555-8555-555555555555"
OID = "a" * 40
NAME = "secret-check-name"
COMMAND = "secret-command && echo done"
SECRETS = (WS, WT, CHECK, RUN, NAME, COMMAND, "secret")
CREATED = "2026-10-04T00:00:00.000Z"
ENDED = "2026-10-04T00:01:00.000Z"

CHECKS = f"/api/v2/code/{WS}/checks"
RUNS = f"/api/v2/checks/{CHECK}/runs"
GETS = {
    "list": f"{CHECKS}?worktree={WT}",
    "runs": RUNS,
    "output": f"/api/v2/check-runs/{RUN}/output?after=10",
}
POSTS: dict[str, tuple[str, dict[str, Any]]] = {
    "create": (CHECKS, {"name": NAME, "command": COMMAND, "timeout_s": 600}),
    "delete": (f"/api/v2/checks/{CHECK}/delete", {}),
    "start": (RUNS, {"worktree_id": WT}),
    "cancel": (f"/api/v2/check-runs/{RUN}/cancel", {}),
}
ROUTES = [*GETS, *POSTS]
PATH_IDS = {
    "list": (WS, "unknown_workspace"),
    "create": (WS, "unknown_workspace"),
    "delete": (CHECK, "unknown_check"),
    "start": (CHECK, "unknown_check"),
    "runs": (CHECK, "unknown_check"),
    "cancel": (RUN, "unknown_run"),
    "output": (RUN, "unknown_run"),
}
SUCCESS = {"create": 201, "start": 201}
R_KEYS = [
    "run_id",
    "check_id",
    "workspace_id",
    "worktree_id",
    "head_oid",
    "dirty",
    "outcome",
    "exit_code",
    "duration_ms",
    "output_bytes",
    "started_at",
    "ended_at",
]
C_KEYS = ["check_id", "name", "command", "timeout_s", "created_at", "readable", "last_run"]


def _row(run_id: str, *, outcome: str, dirty: int | None) -> CheckRunRow:
    finished = outcome != "running"
    return {
        "run_id": run_id,
        "check_id": CHECK,
        "workspace_id": WS,
        "worktree_id": WT,
        "head_oid": None if dirty is None else OID,
        "dirty": dirty,
        "outcome": outcome,
        "exit_code": 3 if outcome == "failed" else None,
        "duration_ms": 1500 if finished else None,
        "output_bytes": 14 if finished else None,
        "tail": b"sealed-secret-tail" if finished else None,
        "started_at": CREATED,
        "ended_at": ENDED if finished else None,
    }


RUNNING = _row(RUN, outcome="running", dirty=1)
FAILED = _row(OTHER_RUN, outcome="failed", dirty=0)
NO_GIT = _row(OTHER_RUN, outcome="passed", dirty=None)
VIEW = CheckView(CHECK, NAME, COMMAND, 600, CREATED, True)
UNREADABLE = CheckView(OTHER_RUN, None, None, 60, ENDED, False)


def _r(row: CheckRunRow, dirty: bool | None) -> dict[str, Any]:
    """The run as the wire answers it."""
    answer: dict[str, Any] = {key: value for key, value in row.items() if key != "tail"}
    answer["dirty"] = dirty
    assert list(answer) == R_KEYS
    return answer


class Stub:
    """Stands in for `CheckService` and `CheckRunner`: records each call and answers a
    well-formed result, or raises what it was given. With `gate` a runner call waits there."""

    def __init__(self, raises: BaseException | None = None) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.raises = raises
        self.gate: asyncio.Event | None = None
        self.entered: asyncio.Event | None = None
        self.expect = 1  # `entered` is set by this many calls
        self.log: list[str] = []

    def _record(self, name: str, args: tuple[Any, ...]) -> None:
        self.calls.append((name, args))
        if self.raises is not None:
            raise self.raises

    def list(self, *args: Any) -> list[tuple[CheckView, CheckRunRow | None]]:
        self._record("list", args)
        return [(VIEW, RUNNING), (UNREADABLE, None)]

    def add_check(self, *args: Any) -> CheckView:
        self._record("add_check", args)
        return VIEW

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

    async def start(self, *args: Any) -> CheckRunRow:
        await self._operate("start", args)
        return RUNNING

    async def cancel(self, *args: Any) -> CheckRunRow:
        await self._operate("cancel", args)
        return RUNNING

    async def output(self, *args: Any) -> tuple[CheckRunRow, Slice, bool]:
        await self._operate("output", args)
        return FAILED, Slice(10, 14, "viki", True), True

    async def runs(self, *args: Any) -> list[CheckRunRow]:
        await self._operate("runs", args)
        return [RUNNING, FAILED, NO_GIT]

    async def delete(self, *args: Any) -> None:
        await self._operate("delete", args)


@pytest.fixture(autouse=True)
def _on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """The routes run on every OS here; the platform refusal has its own test."""
    monkeypatch.setattr(code_checks, "CHECKS_SUPPORTED", True)


def _app(stub: Any, workers: set[asyncio.Task[Any]] | None = None) -> Starlette:
    return Starlette(
        routes=check_routes(
            stub,
            stub,
            authorized=lambda request: request.headers.get("authorization") == "Bearer secret",
            origins=frozenset({ORIGIN}),
            workers=set() if workers is None else workers,
        )
    )


def post(client: TestClient, url: str, fields: Any = None, **kwargs: Any) -> Any:
    headers = {**JSON, **kwargs.pop("headers", {})}
    content = kwargs.pop("content", None)
    return client.post(
        url, content=json.dumps(fields) if content is None else content, headers=headers
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
    assert "body" not in response.text and "tail" not in response.text
    return response.json()


# --- the seven answers --------------------------------------------------------------------------


def test_checks_list_answers_the_contract() -> None:
    stub = Stub()
    with TestClient(_app(stub)) as client:
        body = _answer(client.get(GETS["list"], headers=AUTH))
        plain = _answer(client.get(CHECKS, headers=BEARER))
    assert list(body) == ["checks"]
    assert [list(check) for check in body["checks"]] == [C_KEYS, C_KEYS]
    assert list(body["checks"][0]["last_run"]) == R_KEYS
    assert body == {
        "checks": [
            {
                "check_id": CHECK,
                "name": NAME,
                "command": COMMAND,
                "timeout_s": 600,
                "created_at": CREATED,
                "readable": True,
                "last_run": _r(RUNNING, True),
            },
            {
                "check_id": OTHER_RUN,
                "name": None,
                "command": None,
                "timeout_s": 60,
                "created_at": ENDED,
                "readable": False,
                "last_run": None,
            },
        ]
    }
    assert plain == body
    assert stub.calls == [("list", (WS, WT)), ("list", (WS, None))]


def test_create_answers_201_with_the_check_and_leaves_its_content_to_the_service() -> None:
    stub = Stub()
    url, fields = POSTS["create"]
    with TestClient(_app(stub)) as client:
        body = _answer(post(client, url, fields), 201)
        # The content rules (blank, a line break, the time limit's range) are add_check's.
        _answer(post(client, url, {"name": "", "command": "a\nb", "timeout_s": 5}), 201)
    assert body == {
        "check": {
            "check_id": CHECK,
            "name": NAME,
            "command": COMMAND,
            "timeout_s": 600,
            "created_at": CREATED,
            "readable": True,
            "last_run": None,
        }
    }
    assert list(body["check"]) == C_KEYS
    assert stub.calls == [
        ("add_check", (WS, NAME, COMMAND, 600)),
        ("add_check", (WS, "", "a\nb", 5)),
    ]


def test_the_run_routes_answer_the_contract() -> None:
    stub = Stub()
    with TestClient(_app(stub)) as client:
        started = _answer(call(client, "start"), 201)
        folder = _answer(post(client, RUNS, {"worktree_id": None}), 201)
        runs = _answer(call(client, "runs"))
        cancelled = _answer(call(client, "cancel"))
        output = _answer(call(client, "output"))
        deleted = _answer(call(client, "delete"))
    assert started == folder == cancelled == {"run": _r(RUNNING, True)}
    assert list(started["run"]) == R_KEYS
    assert runs == {"runs": [_r(RUNNING, True), _r(FAILED, False), _r(NO_GIT, None)]}
    assert output == {
        "run": _r(FAILED, False),
        "start": 10,
        "next": 14,
        "text": "viki",
        "dropped": True,
        "done": True,
    }
    assert list(output) == ["run", "start", "next", "text", "dropped", "done"]
    assert deleted == {"deleted": True}
    assert stub.calls == [
        ("start", (CHECK, WT)),
        ("start", (CHECK, None)),
        ("runs", (CHECK,)),
        ("cancel", (RUN,)),
        ("output", (RUN, 10)),
        ("delete", (CHECK,)),
    ]


@pytest.mark.parametrize(
    ("query", "after"),
    [("", 0), ("?after=9007199254740991", 9_007_199_254_740_991), ("?after=0000000000000001", 1)],
)
def test_output_reads_from_the_offset_asked(query: str, after: int) -> None:
    stub = Stub()
    with TestClient(_app(stub)) as client:
        _answer(client.get(f"/api/v2/check-runs/{RUN}/output{query}", headers=AUTH))
    assert stub.calls == [("output", (RUN, after))]


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
    monkeypatch.setattr(code_checks, "CHECKS_SUPPORTED", False)
    stub = Stub()
    real, _ = PATH_IDS[route]
    with TestClient(_app(stub)) as client:
        _refused(call(client, route), 409, "unsupported_platform")
        if route in POSTS:
            # Before the body too: a malformed one answers the platform.
            response = post(client, POSTS[route][0], {"unexpected": True})
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
        for route in ("delete", "cancel"):
            assert client.get(POSTS[route][0], headers=AUTH).status_code == 405
    assert stub.calls == []


# --- the query ----------------------------------------------------------------------------------

BAD_QUERIES = {
    "list": [
        f"worktree={WT}&worktree={WT}",
        "x=1",
        "worktree",
        f"worktree={WT}&",
        "worktree=not-a-uuid",
        f"worktree={ALPHA.upper()}",
        "worktree=",
        "after=1",
        "worktree=%zz",
    ],
    "runs": ["x=1", "after=1", f"worktree={WT}", "="],
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
    "name": [5, None, ["x"], True],
    "command": [5, None, ["x"], {"x": 1}],
    "timeout_s": ["600", True, False, 600.0, None, [600]],
    "worktree_id": ["", "x", ALPHA.upper(), ALPHA.replace("-", ""), 5, True],
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


@pytest.mark.parametrize("route", ["create", "start"])
def test_a_duplicate_key_is_400_invalid_body(route: str) -> None:
    url, fields = POSTS[route]
    first = next(iter(fields))
    raw = json.dumps(fields)[:-1] + "," + json.dumps({first: fields[first]})[1:]
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
        {"Content-Type": "application/json", "Content-Encoding": "br"},
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


# --- every code a service can raise -------------------------------------------------------------

STATUS = {
    code: status
    for status, codes in {
        400: "invalid_body invalid_query invalid_name invalid_command invalid_timeout disconnected",
        401: "unauthorized",
        403: "forbidden_origin",
        404: "unknown_workspace unknown_worktree unknown_check unknown_run",
        408: "intake_timeout",
        409: "trust_required root_unavailable worktree_not_ready check_limit check_running "
        "run_limit check_unreadable unsupported_platform",
        413: "too_large",
        429: "overloaded",
        503: "start_failed unavailable",
    }.items()
    for code in codes.split()
}
MESSAGES = {
    "invalid_name": "Use 1 to 100 characters on one line.",
    "invalid_command": "Write the command on one line, up to 8,000 characters.",
    "invalid_timeout": "Set a time limit of 1 to 60 minutes.",
    "unknown_check": "This check no longer exists. Refresh the list.",
    "unknown_run": "This check no longer exists. Refresh the list.",
    "check_limit": "A folder keeps at most 32 checks. Delete one first.",
    "check_running": "This check is running. Cancel it or wait until it ends.",
    "run_limit": "Four checks are running. Wait until one ends.",
    "check_unreadable": "This check can't be read on this computer. Delete it and add it again.",
    "unsupported_platform": "Checks are unavailable on this platform.",
    "start_failed": "The command could not start. Check that the folder still exists.",
    "trust_required": "Trust this folder, then try again.",  # roots_api's
    "worktree_not_ready": "This worktree isn't available right now.",  # roots_api's
}


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
        for code in ("a_code_nobody_mapped", "worktree_in_use", "terminal_limit"):
            stub.raises = CodeRunError(code)
            _refused(call(client, route), 503, "unavailable")
        stub.raises = StateError("unavailable")
        _refused(call(client, route), 503, "unavailable")
    assert {code: seen[code] for code in MESSAGES} == MESSAGES
    assert len(stub.calls) == len(STATUS) + 4
    assert [r for r in caplog.records if r.name == checks_api.__name__] == []


@pytest.mark.parametrize("route", ROUTES)
def test_anything_else_is_503_unavailable_with_one_warning_naming_the_type(
    caplog: pytest.LogCaptureFixture, route: str
) -> None:
    stub = Stub(OSError(f"C:\\secret\\{NAME} {COMMAND} {CHECK} token"))
    with caplog.at_level(logging.WARNING), TestClient(_app(stub)) as client:
        _refused(call(client, route), 503, "unavailable")
    records = [r for r in caplog.records if r.name == checks_api.__name__]
    assert [(r.levelno, r.getMessage()) for r in records] == [
        (logging.WARNING, "checks route failed (OSError)")
    ]


# --- capacity and cancellation ------------------------------------------------------------------


async def test_a_ninth_request_is_429_overloaded_and_the_permits_come_back() -> None:
    assert MAX_CHECK_REQUESTS == 8
    stub = Stub()
    stub.gate, stub.entered, stub.expect = asyncio.Event(), asyncio.Event(), MAX_CHECK_REQUESTS
    workers: set[asyncio.Task[Any]] = set()
    transport = httpx.ASGITransport(app=_app(stub, workers))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        url, fields = POSTS["start"]

        async def start() -> httpx.Response:
            return await client.post(url, content=json.dumps(fields), headers=JSON)

        calls = [asyncio.create_task(start()) for _ in range(MAX_CHECK_REQUESTS)]
        await asyncio.wait_for(stub.entered.wait(), HANG_GUARD_S)
        assert len(workers) == MAX_CHECK_REQUESTS
        _refused(await start(), 429, "overloaded")
        _refused(await client.get(GETS["list"], headers=AUTH), 429, "overloaded")
        stub.gate.set()
        assert all(r.status_code == 201 for r in await asyncio.gather(*calls))
        await asyncio.sleep(0)
        assert not workers
        assert (await client.get(GETS["list"], headers=AUTH)).status_code == 200


@pytest.mark.parametrize("route", ["start", "delete", "cancel"])
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
