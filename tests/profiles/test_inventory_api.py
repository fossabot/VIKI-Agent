"""The inventory route and its sanitizer: only known keys, no secret-shaped or overlong string,
bounded lists, a bounded answer, and a private, bounded GET."""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import uuid
from typing import Any

import httpx
import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from viki_agent.conversations.inventory_api import inventory_routes, sanitize

TOKEN = "route-token-7f3a"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
ORIGIN = "http://127.0.0.1:8765"
HANG_GUARD_S = 5  # a guard on an event that normally fires in milliseconds

# The fixture guard scans tests too, so the provider key prefix is spelled in halves.
API_KEY = "sk-" + "ant-" + "abc"
# The 300-character string has no long run, so only the length limit drops it.
PLANTED = ["Bearer x", API_KEY, "A" * 40, "b. " * 100]


def _server(name: str = "viki_echo", tools: list[Any] | None = None) -> dict[str, Any]:
    return {
        "name": name,
        "status": "connected",
        "scope": "dynamic",
        "source": "dynamic",
        "server_name": "viki-echo",
        "server_version": "1",
        "egress": None,
        "tools": ["echo"] if tools is None else tools,
    }


def _document() -> dict[str, Any]:
    return {
        "observed_at": "2026-09-28T02:00:00.000Z",
        "mcp_servers": [_server()],
        "mcp_tools": [{"name": "mcp__viki_echo__echo", "server": "viki_echo", "loaded": True}],
        "skills": ["example-skill"],
        "memory_files": [{"path": "%VAULT%/CLAUDE.md", "type": "Project"}],
        "system_tools": ["Read"],
        "agents": [{"type": "Explore", "source": "built-in"}],
        "reasons": {},
    }


def test_a_clean_document_passes_whole_and_is_never_mutated() -> None:
    unknown = {"config": {"env": {"X": "y"}}}
    document: dict[str, Any] = _document() | unknown | {"extra": 1}
    for part in ("mcp_servers", "mcp_tools", "memory_files", "agents"):
        document[part][0] |= copy.deepcopy(unknown)
    before = copy.deepcopy(document)
    result: dict[str, Any] = sanitize(document)
    expected = _document()
    expected["mcp_servers"][0]["tools_omitted"] = False
    assert result == expected  # the unknown keys are gone, at the top and inside each object
    assert document == before


@pytest.mark.parametrize("planted", PLANTED)
def test_a_secret_shaped_or_overlong_string_vanishes_from_every_part(planted: str) -> None:
    document = _document()
    document["observed_at"] = planted
    document["mcp_servers"] += [_server(tools=["echo", planted]), _server(name=planted)]
    document["mcp_servers"].append(_server() | {"egress": planted})
    document["mcp_tools"].append({"name": "t", "server": planted, "loaded": False})
    document["skills"].append(planted)
    document["memory_files"].append({"path": planted, "type": "User"})
    document["system_tools"].append(planted)
    document["agents"].append({"type": planted, "source": "user"})
    result: dict[str, Any] = sanitize(document)
    assert planted not in json.dumps(result)
    assert result["observed_at"] is None
    # A list of strings loses the element; a server row loses the field, or goes whole when the
    # string is its name; any other object carrying the string goes whole.
    assert [s["tools"] for s in result["mcp_servers"]] == [["echo"]] * 3
    assert result["mcp_servers"][2]["egress"] is None
    for part in ("mcp_tools", "skills", "memory_files", "system_tools", "agents"):
        assert result[part] == _document()[part]


GIT_HASH = "3e320108de6831eb996e9a3f7795152073cc0d0c"
LABEL = "mcp-gateway-production-eu-west-1"  # a legal DNS label of 32 characters


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("server_version", f"0.1.0+{GIT_HASH}"),
        ("egress", f"https://{LABEL}.example.com"),
        ("source", API_KEY),
    ],
)
def test_a_server_row_keeps_its_name_and_nulls_an_unfit_field(field: str, value: str) -> None:
    assert len(GIT_HASH) == 40 and len(LABEL) == 32
    document = _document()
    document["mcp_servers"] = [_server() | {field: value}]
    row = {**_server(), field: None, "tools_omitted": False}
    assert sanitize(document)["mcp_servers"] == [row]


def test_a_server_row_goes_only_when_its_name_is_secret_shaped() -> None:
    longest = "viki_" + "a" * 26  # an app server's live name: the longest bare name is 26
    document = _document()
    document["mcp_servers"] = [_server(longest), _server(longest + "a")]
    result: dict[str, Any] = sanitize(document)
    assert [s["name"] for s in result["mcp_servers"]] == [longest]


def test_a_string_is_measured_in_utf16_units_as_the_browser_does() -> None:
    fits, over = "\U0001f600" * 128, "\U0001f600" * 129  # 256 and 258 units
    document = _document()
    document["skills"] = [fits, over]
    assert sanitize(document)["skills"] == [fits]


def test_an_object_off_its_shape_goes_whole() -> None:
    document = _document()
    document["mcp_servers"] += [_server() | {"status": "odd"}, _server() | {"tools": "echo"}, "x"]
    document["mcp_tools"].append({"name": "t", "server": "s", "loaded": "yes"})
    document["memory_files"].append({"path": "p"})
    document["skills"] += [1, None]
    result: dict[str, Any] = sanitize(document)
    for part in ("mcp_servers", "mcp_tools", "memory_files", "skills"):
        assert len(result[part]) == 1


def test_lists_are_capped() -> None:
    document = _document()
    tools = [f"t{j}" for j in range(257)]
    document["mcp_servers"] = [_server(f"viki_{i}", tools) for i in range(65)]
    document["skills"] = [f"skill-{i}" for i in range(257)]
    result: dict[str, Any] = sanitize(document)
    assert len(result["mcp_servers"]) == 64
    assert {len(s["tools"]) for s in result["mcp_servers"]} == {256}
    assert result["skills"] == [f"skill-{i}" for i in range(256)]


def test_an_oversized_answer_drops_every_servers_tools() -> None:
    document = _document()
    # 6 servers x 256 tool names of 203 characters: about 310 KiB. The dots keep each name
    # clear of the long-run secret shape, so every name would otherwise be sent.
    names = [f"{j:03d}" + ".t" * 100 for j in range(256)]
    document["mcp_servers"] = [_server(f"viki_{i}", names) for i in range(6)]
    result: dict[str, Any] = sanitize(document)
    assert [(s["tools"], s["tools_omitted"]) for s in result["mcp_servers"]] == [([], True)] * 6
    assert len(json.dumps(result)) < 256 * 1024


def test_a_null_part_keeps_its_reason_and_always_has_one() -> None:
    document = _document()
    document["mcp_servers"] = None
    document["skills"] = None
    document["agents"] = {"type": "Explore"}  # not a list
    document["reasons"] = {"mcp_servers": "timeout", "skills": "made-up", "system_tools": "absent"}
    result: dict[str, Any] = sanitize(document)
    assert (result["mcp_servers"], result["skills"], result["agents"]) == (None, None, None)
    # A listed part carries no reason; an unknown or missing one reads as unrecognized.
    assert result["reasons"] == {
        "mcp_servers": "timeout",
        "skills": "unrecognized",
        "agents": "unrecognized",
    }


def test_reasons_that_are_not_an_object_are_ignored() -> None:
    document = _document() | {"reasons": ["timeout"], "mcp_tools": None}
    assert sanitize(document)["reasons"] == {"mcp_tools": "unrecognized"}


class Actor:
    """A session actor whose inventory is `document`, held until `release` is set."""

    def __init__(self, document: dict[str, Any] | None, release: asyncio.Event | None = None):
        self.document = document
        self.release = release
        self.entered = 0
        self.two_entered = asyncio.Event()

    async def inventory(self) -> dict[str, object] | None:
        self.entered += 1
        if self.entered == 2:
            self.two_entered.set()
        if self.release is not None:
            await self.release.wait()
        return self.document


class Sessions:
    def __init__(self, actors: dict[str, Actor]) -> None:
        self.actors = actors
        self.asked: list[str] = []

    def get(self, session_id: str) -> Actor | None:
        self.asked.append(session_id)
        return self.actors.get(session_id)


def _app(sessions: Sessions, workers: set[asyncio.Task[Any]] | None = None) -> Starlette:
    routes = inventory_routes(
        sessions,
        authorized=lambda request: request.headers.get("authorization") == f"Bearer {TOKEN}",
        origins=frozenset({ORIGIN}),
        workers=set() if workers is None else workers,
    )
    return Starlette(routes=routes)


def _path(session_id: str) -> str:
    return f"/api/v2/sessions/{session_id}/inventory"


def test_each_answer_is_gated_in_order_and_never_stored(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="viki_agent")
    running, stopped, unknown = (str(uuid.uuid4()) for _ in range(3))
    document = _document()
    document["skills"].append(API_KEY)
    sessions = Sessions({running: Actor(document), stopped: Actor(None)})
    foreign = {**AUTH, "Origin": "http://evil.invalid"}
    refused = [
        (_path("x") + "?q=1", {"Origin": "http://evil.invalid"}, 401, "unauthorized"),
        (_path(running), {"Authorization": "Bearer other"}, 401, "unauthorized"),
        (_path("x") + "?q=1", foreign, 403, "forbidden_origin"),
        (_path(running) + "?q=1", AUTH, 400, "invalid_input"),
        (_path(running.upper()), AUTH, 400, "invalid_input"),
        (_path(running.replace("-", "")), AUTH, 400, "invalid_input"),
        (_path("x"), AUTH, 400, "invalid_input"),
        (_path(unknown), AUTH, 404, "not_found"),
        (_path(stopped), AUTH, 409, "not_running"),
    ]
    with TestClient(_app(sessions)) as client:
        for url, headers, status, code in refused:
            response = client.get(url, headers=headers)
            assert (response.status_code, response.json()["error"]["code"]) == (status, code), url
            assert response.headers["cache-control"] == "no-store"
        expected = _document()
        expected["mcp_servers"][0]["tools_omitted"] = False
        for headers in (AUTH, {**AUTH, "Origin": ORIGIN}):
            answer = client.get(_path(running), headers=headers)
            assert answer.status_code == 200 and answer.headers["cache-control"] == "no-store"
            assert answer.json() == expected  # sanitized: the planted key is gone
    # Every refusal before the lookup reached no session.
    assert sessions.asked == [unknown, stopped, running, running]
    assert TOKEN not in caplog.text and API_KEY not in caplog.text
    assert not any(session_id in caplog.text for session_id in (running, stopped, unknown))


def test_an_answer_past_the_browser_s_bound_is_too_large() -> None:
    session_id = str(uuid.uuid4())
    wide = "日" * 256  # fits every string rule, and escapes to 6 bytes a character
    document = _document()
    document["skills"] = document["system_tools"] = [wide] * 256
    document["memory_files"] = [{"path": wide, "type": wide}] * 256
    assert len(json.dumps(sanitize(document))) > 1 << 20
    with TestClient(_app(Sessions({session_id: Actor(document)}))) as client:
        response = client.get(_path(session_id), headers=AUTH)
    assert (response.status_code, response.json()["error"]["code"]) == (413, "too_large")
    assert response.headers["cache-control"] == "no-store"


async def test_a_third_request_while_two_are_held_is_overloaded() -> None:
    release = asyncio.Event()
    session_id = str(uuid.uuid4())
    held = Actor(_document(), release)
    workers: set[asyncio.Task[Any]] = set()
    transport = httpx.ASGITransport(app=_app(Sessions({session_id: held}), workers))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        calls = [asyncio.create_task(client.get(_path(session_id), headers=AUTH)) for _ in range(2)]
        async with asyncio.timeout(HANG_GUARD_S):
            await held.two_entered.wait()
        third = await client.get(_path(session_id), headers=AUTH)
        assert third.status_code == 429 and third.json()["error"]["code"] == "overloaded"
        assert third.headers["cache-control"] == "no-store"

        # A caller that goes away keeps its permit: the shielded read runs on, tracked.
        calls[0].cancel()
        with pytest.raises(asyncio.CancelledError):
            await calls[0]
        assert len(workers) == 2
        assert (await client.get(_path(session_id), headers=AUTH)).status_code == 429

        release.set()
        async with asyncio.timeout(HANG_GUARD_S):
            assert (await calls[1]).status_code == 200
            await asyncio.gather(*list(workers))
        await asyncio.sleep(0)  # the done callbacks run on the next loop pass
        assert not workers
        assert (await client.get(_path(session_id), headers=AUTH)).status_code == 200
    assert held.entered == 3
