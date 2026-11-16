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

from viki_agent.conversations.context_api import context_routes, sanitize_context
from viki_agent.drivers.claude.context_report import REPORT_PARTS

TOKEN = "route-token-c0d3"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
ORIGIN = "http://127.0.0.1:8765"
HANG_GUARD_S = 5  # a guard on an event that normally fires in milliseconds

# The fixture guard scans tests too, so the provider key prefix is spelled in halves.
API_KEY = "sk-" + "ant-" + "abc"


def _category(name: str = "System prompt", tokens: int = 500, kind: str = "used") -> dict[str, Any]:
    return {"name": name, "tokens": tokens, "kind": kind}


def _memory_file(
    name: str = "CLAUDE.md", type_: str = "Project", tokens: int = 120
) -> dict[str, Any]:
    return {"name": name, "type": type_, "tokens": tokens}


def _mcp_server(
    server: str = "viki_echo", tools: int = 2, loaded: int = 1, loaded_tokens: int = 50
) -> dict[str, Any]:
    return {"server": server, "tools": tools, "loaded": loaded, "loaded_tokens": loaded_tokens}


def _skill_item(
    name: str = "example-skill",
    source: str = "project",
    plugin: str | None = None,
    tokens: int = 80,
) -> dict[str, Any]:
    return {"name": name, "source": source, "plugin": plugin, "tokens": tokens}


def _agent(type_: str = "Explore", source: str = "built-in", tokens: int = 300) -> dict[str, Any]:
    return {"type": type_, "source": source, "tokens": tokens}


def _document() -> dict[str, Any]:
    return {
        "observed_at": "2026-09-28T02:00:00.000Z",
        "total_tokens": 1000,
        "max_tokens": 600_000,
        "raw_max_tokens": 1_000_000,
        "percentage": 17,
        "auto_compact": {"enabled": True, "threshold": 500_000, "source": "settings"},
        "categories": [_category()],
        "memory_files": [_memory_file()],
        "mcp_servers": [_mcp_server()],
        "skills": {"total": 3, "included": 1, "tokens": 80, "items": [_skill_item()]},
        "agents": [_agent()],
        "reasons": {},
    }


def _no_hidden() -> dict[str, int]:
    return dict.fromkeys(REPORT_PARTS, 0)


def test_a_clean_report_passes_whole_and_is_never_mutated() -> None:
    unknown = {"description": "secret prompt text", "color": "#fff"}
    document: dict[str, Any] = _document() | unknown | {"extra": 1}
    for part in ("categories", "memory_files", "mcp_servers", "agents"):
        document[part][0] |= copy.deepcopy(unknown)
    document["skills"] |= copy.deepcopy(unknown)
    document["skills"]["items"][0] |= copy.deepcopy(unknown)
    document["auto_compact"] |= copy.deepcopy(unknown)
    before = copy.deepcopy(document)
    result: dict[str, Any] = sanitize_context(document)
    expected = _document()
    expected["hidden"] = _no_hidden()
    assert result == expected  # the unknown keys are gone, at the top and inside each object
    assert document == before


def test_unfit_strings_and_rows_past_a_cap_are_counted_hidden() -> None:
    overlong = "x" * 300  # measured in UTF-16 units, well past the 256-unit bound
    document = _document()
    document["categories"] = [
        _category(name=API_KEY),
        _category(name=overlong),
        _category(kind="bogus"),
    ] + [_category(f"cat-{i}") for i in range(32)]
    document["memory_files"] = [
        _memory_file(name=API_KEY),
        _memory_file(name=overlong),
        _memory_file(type_=API_KEY),
        _memory_file(type_=overlong),
    ]
    document["mcp_servers"] = [_mcp_server(server=API_KEY), _mcp_server(server=overlong)]
    document["skills"]["items"] = [
        _skill_item(name=API_KEY),
        _skill_item(plugin=overlong),
        _skill_item(source=API_KEY),
        _skill_item(source=overlong),
    ]
    document["agents"] = [
        _agent(source=API_KEY),
        _agent(type_=API_KEY),
        _agent(type_=overlong),
    ] + [_agent(f"Explore-{i}") for i in range(257)]
    result: dict[str, Any] = sanitize_context(document)
    assert len(result["categories"]) == 32
    assert result["hidden"]["categories"] == 3  # 1 secret + 1 overlong + 1 off-kind
    assert result["memory_files"] == []
    assert result["hidden"]["memory_files"] == 4  # name: 1 secret + 1 overlong; type: likewise
    assert result["mcp_servers"] == []
    assert result["hidden"]["mcp_servers"] == 2  # 1 secret server + 1 overlong server
    skills = result["skills"]
    assert isinstance(skills, dict) and skills["items"] == []
    assert result["hidden"]["skills"] == 4  # name: 1 secret; plugin: 1 overlong; source: both
    assert len(result["agents"]) == 256
    assert result["hidden"]["agents"] == 4  # source: 1 secret; type: 1 secret + 1 overlong; +cap
    dumped = json.dumps(result)
    assert API_KEY not in dumped
    assert overlong not in dumped
    # observed_at and auto_compact.source are scalars, not rows: an unfit value there is refused
    # (nulled) rather than counted in `hidden`, since neither is a REPORT_PARTS entry.
    for bad in (API_KEY, overlong):
        observed: dict[str, Any] = sanitize_context(_document() | {"observed_at": bad})
        assert observed["observed_at"] is None and observed["hidden"] == _no_hidden()
        solo = _document()
        solo["auto_compact"] |= {"source": bad}
        compact: dict[str, Any] = sanitize_context(solo)
        assert compact["auto_compact"]["source"] is None and compact["hidden"] == _no_hidden()


@pytest.mark.parametrize("bad_kind", [{"type": "used"}, ["used"]])
def test_an_unhashable_kind_is_hidden_without_raising(bad_kind: object) -> None:
    document = _document()
    document["categories"] = [_category(), {**_category(), "kind": bad_kind}]
    result: dict[str, Any] = sanitize_context(document)
    assert result["categories"] == [_category()]
    assert result["hidden"]["categories"] == 1


def test_nothing_outside_the_contract_reaches_the_browser() -> None:
    document = _document()
    document["unexpected_top"] = "x"
    document["auto_compact"] |= {"unexpected": "x"}
    document["categories"][0] |= {"unexpected": "x"}
    document["memory_files"][0] |= {"unexpected": "x"}
    document["mcp_servers"][0] |= {"unexpected": "x"}
    document["skills"] |= {"unexpected": "x"}
    document["skills"]["items"][0] |= {"unexpected": "x"}
    document["agents"][0] |= {"unexpected": "x"}
    result: dict[str, Any] = sanitize_context(document)
    assert set(result) == {
        "observed_at",
        "total_tokens",
        "max_tokens",
        "raw_max_tokens",
        "percentage",
        "auto_compact",
        "categories",
        "memory_files",
        "mcp_servers",
        "skills",
        "agents",
        "hidden",
        "reasons",
    }
    assert set(result["auto_compact"]) == {"enabled", "threshold", "source"}
    assert set(result["categories"][0]) == {"name", "tokens", "kind"}
    assert set(result["memory_files"][0]) == {"name", "type", "tokens"}
    assert set(result["mcp_servers"][0]) == {"server", "tools", "loaded", "loaded_tokens"}
    assert set(result["skills"]) == {"total", "included", "tokens", "items"}
    assert set(result["skills"]["items"][0]) == {"name", "source", "plugin", "tokens"}
    assert set(result["agents"][0]) == {"type", "source", "tokens"}
    assert set(result["hidden"]) == set(REPORT_PARTS)


def test_a_null_part_keeps_one_known_reason() -> None:
    document = _document()
    document["categories"] = None
    document["memory_files"] = "nope"
    document["mcp_servers"] = {"not": "a list"}
    document["skills"] = {"total": 1}  # included and items are both missing
    document["agents"] = None
    document["reasons"] = {
        "categories": "timeout",
        "memory_files": "not_observed",
        "mcp_servers": "made-up-reason",
        "agents": "unavailable",
    }
    result: dict[str, Any] = sanitize_context(document)
    assert result["categories"] is None
    assert result["memory_files"] is None
    assert result["mcp_servers"] is None
    assert result["skills"] is None
    assert result["agents"] is None
    assert result["reasons"] == {
        "categories": "timeout",
        "memory_files": "not_observed",
        "mcp_servers": "unrecognized",  # not one of REASONS
        "skills": "unrecognized",  # the document names no reason for it
        "agents": "unavailable",
    }


class Actor:
    """A session actor whose context is `document`, held until `release` is set."""

    def __init__(self, document: dict[str, Any] | None, release: asyncio.Event | None = None):
        self.document = document
        self.release = release
        self.entered = 0
        self.two_entered = asyncio.Event()

    async def context(self) -> dict[str, object] | None:
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
    routes = context_routes(
        sessions,
        authorized=lambda request: request.headers.get("authorization") == f"Bearer {TOKEN}",
        origins=frozenset({ORIGIN}),
        workers=set() if workers is None else workers,
    )
    return Starlette(routes=routes)


def _path(session_id: str) -> str:
    return f"/api/v2/sessions/{session_id}/context"


def test_each_answer_is_gated_in_order_and_logs_nothing(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="viki_agent")
    running, stopped, unknown = (str(uuid.uuid4()) for _ in range(3))
    document = _document()
    document["agents"] = [_agent(source=API_KEY)]  # dropped by the sanitizer, never logged either
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
        for headers in (AUTH, {**AUTH, "Origin": ORIGIN}):
            answer = client.get(_path(running), headers=headers)
            assert answer.status_code == 200 and answer.headers["cache-control"] == "no-store"
            assert API_KEY not in json.dumps(answer.json())
            assert answer.json()["hidden"]["agents"] == 1
    # Every refusal before the lookup reached no session.
    assert sessions.asked == [unknown, stopped, running, running]
    assert TOKEN not in caplog.text and API_KEY not in caplog.text
    assert not any(session_id in caplog.text for session_id in (running, stopped, unknown))


def test_an_answer_past_the_browser_s_bound_is_too_large() -> None:
    session_id = str(uuid.uuid4())
    wide = "é" * 256  # fits every string rule, and escapes to 6 bytes a character
    document = _document()
    document["memory_files"] = [_memory_file(name=wide, type_=wide)] * 256
    document["agents"] = [_agent(type_=wide, source=wide)] * 256
    document["skills"]["items"] = [_skill_item(name=wide, source=wide)] * 256
    assert len(json.dumps(sanitize_context(document))) > 1 << 20
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
