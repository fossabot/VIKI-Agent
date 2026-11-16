"""The driver's whitelisted inventory: only named fields of the provider's context reply are kept.

Replies are the scrubbed synthetic fixture or built here; no native provider, transcript or vault.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import re
from pathlib import Path
from typing import Any, cast

import pytest

from tests.conversations.test_session import HANG_GUARD_S, SID, GatedDriver, gated, setup
from tests.test_driver_limits import make_driver
from viki_agent.drivers.base import InventoryDriver
from viki_agent.drivers.claude.driver import context_snapshot, mcp_server_rows
from viki_agent.events import TurnDone

from replay import expect, init_frame, out, result_frame, user_in

FIXTURE = Path(__file__).parents[1] / "fixtures" / "claude" / "mcp-inventory.jsonl"
STAMP = "2026-09-28T02:00:00.000Z"
PARTS = ("mcp_tools", "skills", "memory_files", "system_tools", "agents")


def fixture_usage() -> dict[str, Any]:
    """The recorded `get_context_usage` reply (the one reply that carries `mcpTools`)."""
    for line in FIXTURE.read_text(encoding="utf-8").splitlines():
        frame = json.loads(line)["frame"]
        reply = frame.get("response", {}).get("response")
        if isinstance(reply, dict) and "mcpTools" in reply:
            return reply
    raise AssertionError("the fixture holds no context-usage reply")


def with_agents_and_tools() -> dict[str, Any]:
    # The fixture recorded no agents and no systemTools; these follow the SDK's documented shapes
    # (agentType, source and token counts; name and token counts).
    usage = fixture_usage()
    usage["agents"] = [{"agentType": "Explore", "source": "built-in", "tokens": 12}]
    usage["systemTools"] = [{"name": "Read", "tokens": 7}]
    return usage


def test_the_fixture_reply_gives_the_synthetic_values() -> None:
    assert context_snapshot(fixture_usage(), STAMP) == {
        "observed_at": STAMP,
        "mcp_tools": [{"name": "mcp__viki_echo__echo", "server": "viki_echo", "loaded": False}],
        "skills": ["example-skill"],
        "memory_files": [{"path": "%VAULT%\\CLAUDE.md", "type": "Project"}],
        "system_tools": None,
        "agents": [],
        "reasons": {"system_tools": "absent"},
    }


def test_agents_and_system_tools_keep_only_their_named_fields() -> None:
    snapshot = context_snapshot(with_agents_and_tools(), STAMP)
    assert snapshot["agents"] == [{"type": "Explore", "source": "built-in"}]
    assert snapshot["system_tools"] == ["Read"]
    assert snapshot["reasons"] == {}


@pytest.mark.parametrize(
    ("part", "key"),
    [
        ("mcp_tools", "mcpTools"),
        ("skills", "skills"),
        ("memory_files", "memoryFiles"),
        ("system_tools", "systemTools"),
        ("agents", "agents"),
    ],
)
def test_a_missing_key_is_absent_and_leaves_the_other_parts(part: str, key: str) -> None:
    usage = with_agents_and_tools()
    del usage[key]
    snapshot = context_snapshot(usage, STAMP)
    assert snapshot[part] is None
    assert snapshot["reasons"] == {part: "absent"}
    assert all(snapshot[other] is not None for other in PARTS if other != part)


TOOL = {"name": "mcp__viki_echo__echo", "serverName": "viki_echo", "isLoaded": True}
FILE = {"path": "%VAULT%\\CLAUDE.md", "type": "Project"}
AGENT = {"agentType": "Explore", "source": "built-in"}
SKILLS = {"totalSkills": 1, "skillFrontmatter": [{"name": "example-skill", "source": "plugin"}]}

OFF_SHAPE: list[tuple[str, str, object]] = [
    ("mcp_tools", "mcpTools", None),
    ("mcp_tools", "mcpTools", {"0": TOOL}),
    ("mcp_tools", "mcpTools", [TOOL, "mcp__x__y"]),
    ("mcp_tools", "mcpTools", [{**TOOL, "isLoaded": 1}]),
    ("mcp_tools", "mcpTools", [{"name": TOOL["name"], "isLoaded": True}]),
    ("skills", "skills", ["example-skill"]),
    ("skills", "skills", {"totalSkills": 1}),
    ("skills", "skills", {"skillFrontmatter": {"name": "example-skill"}}),
    ("skills", "skills", {"skillFrontmatter": [{"source": "plugin"}]}),
    ("skills", "skills", {"skillFrontmatter": [{"name": 3}]}),
    ("memory_files", "memoryFiles", "%VAULT%\\CLAUDE.md"),
    ("memory_files", "memoryFiles", [FILE, None]),
    ("memory_files", "memoryFiles", [{"path": FILE["path"]}]),
    ("memory_files", "memoryFiles", [{**FILE, "type": None}]),
    ("system_tools", "systemTools", {"name": "Read"}),
    ("system_tools", "systemTools", ["Read"]),
    ("system_tools", "systemTools", [{"tokens": 7}]),
    ("agents", "agents", None),
    ("agents", "agents", [{"name": "Explore", "source": "built-in"}]),
    ("agents", "agents", [{**AGENT, "source": ["built-in"]}]),
]


@pytest.mark.parametrize(("part", "key", "value"), OFF_SHAPE)
def test_an_off_shape_part_is_null_and_unrecognized(part: str, key: str, value: object) -> None:
    usage = with_agents_and_tools()
    usage[key] = value
    snapshot = context_snapshot(usage, STAMP)
    assert snapshot[part] is None
    assert snapshot["reasons"] == {part: "unrecognized"}
    assert all(snapshot[other] is not None for other in PARTS if other != part)


def test_the_on_shape_samples_are_read() -> None:
    # The off-shape table above only means something if its base elements are themselves read.
    usage = {
        "mcpTools": [TOOL],
        "skills": SKILLS,
        "memoryFiles": [FILE],
        "systemTools": [{"name": "Read"}],
        "agents": [AGENT],
    }
    assert context_snapshot(usage, STAMP)["reasons"] == {}


def test_nothing_outside_the_whitelist_is_copied() -> None:
    usage = with_agents_and_tools()
    planted = [f"PLANTED-{n}" for n in range(9)]
    usage["apiUsage"] = {"input_tokens": 91_234, "authorization": f"Bearer {planted[0]}"}
    usage["mcpTools"][0].update(description=planted[1], inputSchema={"x": planted[2]})
    usage["skills"]["skillFrontmatter"][0].update(description=planted[3])
    usage["memoryFiles"][0].update(content=planted[4], tokens=81_234)
    usage["agents"][0].update(prompt=planted[5], description=planted[6])
    usage["systemTools"][0].update(description=planted[7])
    usage["model"] = planted[8]
    text = json.dumps(context_snapshot(usage, STAMP))
    assert not [value for value in [*planted, "91234", "81234", "tokens"] if value in text]


async def test_one_turn_asks_for_context_usage_once_and_keeps_the_snapshot() -> None:
    reply = fixture_usage()
    script = [
        user_in("synthetic"),
        out(init_frame()),
        out(result_frame()),
        expect(
            {
                "type": "control_request",
                "request_id": "req_ctx",
                "request": {"subtype": "get_context_usage"},
            }
        ),
        out(
            {
                "type": "control_response",
                "response": {"subtype": "success", "request_id": "req_ctx", "response": reply},
            }
        ),
    ]
    driver, spawner, events = make_driver(script)
    assert driver._context_snapshot is None
    try:
        await driver.open()
        await driver.send("synthetic")
        await events.until(TurnDone)
    finally:
        await driver.close()
    seen = spawner.children[0].process.replayer.seen
    asked = [f["request"]["subtype"] for f in seen if f.get("type") == "control_request"]
    # The replay answers any unscripted request, so an extra one shows only in what it saw.
    assert asked.count("get_context_usage") == 1 and "mcp_status" not in asked
    snapshot = driver._context_snapshot
    assert snapshot is not None
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", str(snapshot["observed_at"]))
    expected = context_snapshot(copy.deepcopy(reply), STAMP)
    assert {**snapshot, "observed_at": STAMP} == expected


def fixture_status() -> dict[str, Any]:
    """The recorded `mcp_status` reply."""
    for line in FIXTURE.read_text(encoding="utf-8").splitlines():
        reply = json.loads(line)["frame"].get("response", {}).get("response")
        if isinstance(reply, dict) and "mcpServers" in reply:
            return reply
    raise AssertionError("the fixture holds no mcp_status reply")


ECHO_ROW = {
    "name": "viki_echo",
    "status": "connected",
    "scope": "dynamic",
    "source": "dynamic",
    "server_name": "viki-echo",
    "server_version": "1",
    "egress": None,
    "tools": ["echo"],
}


def test_the_fixture_status_gives_the_echo_row() -> None:
    assert mcp_server_rows(fixture_status()) == [ECHO_ROW]


def test_a_status_copies_no_secret_and_keeps_only_the_origin() -> None:
    planted = [f"PLANTED-{n}" for n in range(12)]
    url = f"https://user:{planted[0]}@Example.com:8443/{planted[1]}?key={planted[2]}#{planted[3]}"
    remote = {
        "name": "remote",
        "status": "needs-auth",
        "scope": "claudeai",
        "error": planted[4],
        "config": {"type": "http", "url": url, "headers": {"Authorization": planted[5]}},
        "tools": [{"name": "fetch", "description": planted[6], "annotations": {"t": planted[7]}}],
    }
    local = {
        "name": "local",
        "status": "failed",
        "error": planted[8],
        "config": {"type": "stdio", "command": planted[9], "args": [planted[10]]},
        "env": {"TOKEN": planted[11]},
    }
    rows = mcp_server_rows({"mcpServers": [remote, local], "extra": planted[0]})
    assert rows is not None
    assert not [value for value in planted if value in json.dumps(rows)]
    assert rows[0]["egress"] == "https://example.com:8443"
    assert rows[0]["tools"] == ["fetch"]
    assert rows[1] == {
        "name": "local",
        "status": "failed",
        "scope": None,
        "source": None,
        "server_name": None,
        "server_version": None,
        "egress": None,
        "tools": None,
    }


@pytest.mark.parametrize(
    ("config", "egress"),
    [
        ({"type": "sse", "url": "http://[::1]:9000/sse"}, "http://[::1]:9000"),
        (
            {"type": "sse", "url": "http://[::ffff:127.0.0.1]:9000/sse"},
            "http://[::ffff:127.0.0.1]:9000",
        ),
        ({"type": "http", "url": "https://example.com/mcp"}, "https://example.com"),
        ({"type": "stdio", "url": "https://example.com"}, None),
        ({"type": "http", "url": "https://example.com:99999/"}, None),
        ({"type": "http", "url": "https://[::1/"}, None),
        ({"type": "http", "url": "file:///etc/x"}, None),
        ({"type": "sse", "url": "ws://example.com/sse"}, None),
        ({"type": "http", "url": 3}, None),
        ("https://example.com", None),
        # The CLI connects to evil.example (a WHATWG parser reads `\` as `/`), not good.example.
        ({"type": "http", "url": "https://evil.example\\@good.example/mcp"}, None),
        ({"type": "http", "url": "https://%61.example/mcp"}, None),
        ({"type": "http", "url": "https://\u043f\u0440\u0438\u043c\u0435\u0440.example/mcp"}, None),
        # A numeric-looking host is shown only when it is a canonical IPv4 address, because the
        # CLI reads it as an IPv4 number the way WHATWG's URL parser does, so any other spelling
        # would show a host the CLI never contacts. WHATWG strips a C0 control or space at either
        # end of the url; urlsplit strips them only at the start (and drops a tab, CR or LF
        # anywhere), so a url with either end shows no host, and these rows pin the trailing end;
        # a numeric host over 15 characters cannot be canonical and is refused before it is split.
        ({"type": "http", "url": "http://010.0.0.1 "}, None),
        ({"type": "http", "url": "http://2130706433\x00"}, None),
        ({"type": "http", "url": "http://0x7f.1\x0c"}, None),
        ({"type": "http", "url": "http://" + "1." * 4096 + "1/mcp"}, None),
        ({"type": "http", "url": "http://0127.0.0.1/mcp"}, None),
        ({"type": "http", "url": "http://010.0.0.1/mcp"}, None),
        ({"type": "http", "url": "http://0x7f.1/mcp"}, None),
        ({"type": "http", "url": "http://2130706433/mcp"}, None),
        ({"type": "http", "url": "http://1.2.3/mcp"}, None),
        ({"type": "http", "url": "http://127.0.0.1/mcp"}, "http://127.0.0.1"),
        # WHATWG drops one trailing dot before this check; urlsplit keeps it, so these still read
        # as ending in a numeric label and stay non-canonical.
        ({"type": "http", "url": "http://0x7f.1./mcp"}, None),
        ({"type": "http", "url": "http://127.0.0.1./mcp"}, None),
        # No trailing dot: a `0x` with no hex digits still counts as a numeric label.
        ({"type": "http", "url": "http://1.0x/mcp"}, None),
        ({"type": "http", "url": "http://0x/mcp"}, None),
        # A trailing dot after a non-numeric label does not make the host numeric.
        ({"type": "http", "url": "http://example.com./mcp"}, "http://example.com."),
    ],
)
def test_egress_is_an_http_or_sse_origin_or_null(config: object, egress: str | None) -> None:
    rows = mcp_server_rows({"mcpServers": [{"name": "s", "status": "pending", "config": config}]})
    assert rows is not None
    assert rows[0]["egress"] == egress


@pytest.mark.parametrize(("source", "kept"), [("plugin", "plugin"), (None, None), (3, None)])
def test_a_server_row_copies_its_source_only_as_a_string(source: object, kept: object) -> None:
    rows = mcp_server_rows({"mcpServers": [{**GOOD, "scope": "dynamic", "source": source}]})
    assert rows is not None and rows[0]["source"] == kept


@pytest.mark.parametrize(
    ("error", "reason"), [(TimeoutError(), "timeout"), (RuntimeError("gone"), "unavailable")]
)
async def test_a_first_failed_context_read_says_why_until_one_succeeds(
    monkeypatch: pytest.MonkeyPatch, error: Exception, reason: str
) -> None:
    driver, _, _ = make_driver([])
    replies: list[object] = [error, fixture_usage(), error]

    async def context_usage() -> dict[str, Any]:
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return cast(dict[str, Any], reply)

    monkeypatch.setattr(driver._control, "context_usage", context_usage)
    assert await driver._context_percent() is None
    document = await driver.inventory()  # not opened: the status part reads unavailable
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", str(document["observed_at"]))
    assert {**document, "observed_at": STAMP} == {
        "observed_at": STAMP,
        **dict.fromkeys(PARTS),
        "mcp_servers": None,
        "reasons": {**dict.fromkeys(PARTS, reason), "mcp_servers": "unavailable"},
    }
    await driver._context_percent()
    succeeded = driver._context_snapshot
    assert succeeded is not None
    assert {**succeeded, "observed_at": STAMP} == context_snapshot(fixture_usage(), STAMP)
    await driver._context_percent()  # a failure after a success keeps the last good reply
    assert driver._context_snapshot is succeeded


GOOD = {"name": "s", "status": "connected"}


@pytest.mark.parametrize(
    "status",
    [
        {},
        {"mcpServers": {"s": GOOD}},
        {"mcpServers": [GOOD, "s"]},
        {"mcpServers": [{"status": "connected"}]},
        {"mcpServers": [{**GOOD, "status": "running"}]},
        {"mcpServers": [{**GOOD, "scope": 3}]},
        {"mcpServers": [{**GOOD, "serverInfo": {"name": "s"}}]},
        {"mcpServers": [{**GOOD, "serverInfo": "s 1"}]},
        {"mcpServers": [{**GOOD, "tools": [{"description": "x"}]}]},
        {"mcpServers": [{**GOOD, "tools": "echo"}]},
    ],
)
def test_an_off_shape_status_gives_no_rows(status: dict[str, Any]) -> None:
    assert mcp_server_rows(status) is None


def test_the_on_shape_sample_is_read() -> None:
    assert mcp_server_rows({"mcpServers": [GOOD]}) is not None


def status_script(response: dict[str, Any] | None, *, subtype: str = "success") -> list[Any]:
    script = [
        expect(
            {
                "type": "control_request",
                "request_id": "req_mcp",
                "request": {"subtype": "mcp_status"},
            }
        )
    ]
    if response is not None:
        reply = {"subtype": subtype, "request_id": "req_mcp", **response}
        script.append(out({"type": "control_response", "response": reply}))
    return script


async def inventory_of(script: list[Any]) -> tuple[dict[str, object], Any]:
    driver, _, _ = make_driver(script)
    await driver.open()
    try:
        return await driver.inventory(), driver
    finally:
        await driver.close()


async def test_the_inventory_joins_a_fresh_status_to_an_unobserved_snapshot() -> None:
    document, driver = await inventory_of(status_script({"response": fixture_status()}))
    assert isinstance(driver, InventoryDriver)
    assert document == {
        "observed_at": None,
        **dict.fromkeys(PARTS),
        "mcp_servers": [ECHO_ROW],
        "reasons": dict.fromkeys(PARTS, "not_observed"),
    }


@pytest.mark.parametrize(
    ("script", "reason"),
    [
        (status_script(None), "timeout"),
        (status_script({"error": "no status"}, subtype="error"), "unavailable"),
        (status_script({"response": {"mcpServers": "none"}}), "unrecognized"),
    ],
)
async def test_a_failed_status_is_null_with_its_reason(script: list[Any], reason: str) -> None:
    document, _ = await inventory_of(script)
    assert document["mcp_servers"] is None
    assert document["reasons"] == {**dict.fromkeys(PARTS, "not_observed"), "mcp_servers": reason}


async def test_a_status_timeout_logs_one_fixed_warning(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    document, _ = await inventory_of(status_script(None))
    assert document["mcp_servers"] is None
    assert [(r.levelno, r.getMessage()) for r in caplog.records] == [
        (logging.WARNING, "MCP status request timed out")
    ]


async def test_a_cancelled_inventory_request_stays_cancelled() -> None:
    driver, _, _ = make_driver(status_script(None))
    await driver.open()
    try:
        pending = asyncio.create_task(driver.inventory())
        await asyncio.sleep(0)  # the request is now waiting for its reply
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
    finally:
        await driver.close()


async def test_no_status_is_asked_while_the_driver_is_not_alive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver, _, _ = make_driver([])
    asked: list[bool] = []

    async def mcp_status() -> dict[str, Any]:
        asked.append(True)
        return fixture_status()

    monkeypatch.setattr(driver._control, "mcp_status", mcp_status)
    before = await driver.inventory()  # not opened: no initialize reply yet
    await driver.open()
    await driver.close()
    after = await driver.inventory()
    for document in (before, after):
        assert document["mcp_servers"] is None
        assert document["reasons"] == {
            **dict.fromkeys(PARTS, "not_observed"),
            "mcp_servers": "unavailable",
        }
    assert asked == []


@pytest.mark.parametrize(
    ("script", "servers", "reason"),
    [
        (status_script({"response": {"mcpServers": []}}), [], None),
        (status_script(None), None, "timeout"),
    ],
)
async def test_the_inventory_keeps_the_last_snapshot_and_never_changes_it(
    script: list[Any], servers: list[Any] | None, reason: str | None
) -> None:
    driver, _, _ = make_driver(script)
    kept = context_snapshot(fixture_usage(), STAMP)
    driver._context_snapshot = copy.deepcopy(kept)
    await driver.open()
    try:
        document = await driver.inventory()
    finally:
        await driver.close()
    reasons = {
        **cast(dict[str, str], kept["reasons"]),
        **({"mcp_servers": reason} if reason else {}),
    }
    assert document == {**kept, "mcp_servers": servers, "reasons": reasons}
    assert driver._context_snapshot == kept


async def test_inventory_reads_the_snapshot_taken_after_the_status_call_returns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The base order captured the snapshot before awaiting the status call, so a snapshot that
    changes while that call is in flight was lost; the fix reads it only once the call returns."""
    driver, _, _ = make_driver([])
    driver.alive = True
    driver._context_snapshot = context_snapshot(fixture_usage(), STAMP)
    waiting, release = asyncio.Event(), asyncio.Event()

    async def mcp_status() -> dict[str, Any]:
        waiting.set()
        await release.wait()
        return {"mcpServers": []}

    monkeypatch.setattr(driver._control, "mcp_status", mcp_status)
    pending = asyncio.create_task(driver.inventory())
    await waiting.wait()
    replaced = context_snapshot(with_agents_and_tools(), STAMP)
    driver._context_snapshot = replaced
    release.set()
    document = await pending
    assert document["agents"] == replaced["agents"]
    assert document["system_tools"] == replaced["system_tools"]
    assert document["reasons"] == replaced["reasons"]


async def test_the_actor_returns_its_live_driver_s_inventory(tmp_path: Path) -> None:
    manager, _ = setup(tmp_path, [])
    actor = manager.create(SID)
    try:
        assert await actor.start()
        document = await actor.inventory()
    finally:
        await manager.close_all()
    assert document == {
        "observed_at": None,
        **dict.fromkeys(PARTS),
        "mcp_servers": [],
        "reasons": dict.fromkeys(PARTS, "not_observed"),
    }


async def test_the_actor_has_no_inventory_before_start_and_after_close(tmp_path: Path) -> None:
    manager, _ = setup(tmp_path, [])
    actor = manager.create(SID)
    assert await actor.inventory() is None
    assert await actor.start()
    await manager.close_all()
    assert await actor.inventory() is None


async def test_the_actor_has_no_inventory_for_a_driver_without_the_capability(
    tmp_path: Path,
) -> None:
    driver = GatedDriver()
    driver.release.set()
    actor = gated(tmp_path, driver).create(SID)
    try:
        assert await actor.start()
        assert not isinstance(driver, InventoryDriver)
        assert await actor.inventory() is None
    finally:
        await actor.close()


async def test_the_actor_s_inventory_answers_while_a_start_holds_its_lock(tmp_path: Path) -> None:
    """A running turn holds the actor's lock across the whole of its start, including the wait
    for the driver to open; inventory() must not wait behind it (mutant: take the lock, before or
    after the capability check, and the bounded wait below times out)."""

    class InventoryGatedDriver(GatedDriver):
        async def inventory(self) -> dict[str, object]:
            return {"marker": True}

    driver = InventoryGatedDriver()
    actor = gated(tmp_path, driver).create(SID)
    sending = asyncio.create_task(actor.send("hello"))
    try:
        await driver.opening.wait()  # inside send()'s lock, waiting for driver.open() to return
        document = await asyncio.wait_for(actor.inventory(), HANG_GUARD_S)
        assert document == {"marker": True}
    finally:
        driver.release.set()
        assert await sending
        await actor.close()
