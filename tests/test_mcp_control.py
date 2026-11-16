"""Switching an MCP server on or off, and reconnecting one: the driver's checks and outcomes, and
the actor's pass-through. A synthetic replay or patched control calls stand in for claude: no
real CLI, no MCP server, and nothing under ~/.claude is read or written."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import CLIConnectionError, ProcessError

from tests.conversations.test_session import (
    HANG_GUARD_S,
    SID,
    GatedDriver,
    eventually,
    gated,
    setup,
)
from tests.profiles.test_inventory import status_script
from tests.test_driver_limits import make_driver
from viki_agent.drivers.base import McpControlDriver, McpControlError
from viki_agent.drivers.claude import control
from viki_agent.drivers.claude.driver import ClaudeDriver

from replay import Entry, FakeSpawner, expect, init_frame, out, user_in

SERVERS = {
    "mcpServers": [
        {"name": "github", "status": "connected"},
        {"name": "notes", "status": "disabled"},
        {"name": "viki_echo", "status": "connected"},
    ]
}
STATUS = {"subtype": "mcp_status"}
OFF = {"subtype": "mcp_toggle", "serverName": "github", "enabled": False}
ON = {"subtype": "mcp_toggle", "serverName": "github", "enabled": True}
RECONNECT = {"subtype": "mcp_reconnect", "serverName": "github"}
SENT = [OFF, ON, RECONNECT]
# What an error reply other than approval or policy gives: the CLI saves an enable before it
# connects, so a failed enable or reconnect leaves the server on but not connected.
OTHER_ERROR = ["refused", "not_connected", "not_connected"]
APPROVAL = 'MCP server "github" is not approved for this project. Run /mcp to approve it.'
POLICY = 'MCP server "github" is blocked by enterprise managed policy.'
EXIT = out({"type": "fake.exit", "code": 1})


def call(driver: ClaudeDriver, sent: dict[str, Any], name: str = "github") -> Awaitable[None]:
    """The driver call that writes `sent`, for `name`."""
    if sent["subtype"] == "mcp_reconnect":
        return driver.reconnect_mcp_server(name)
    return driver.set_mcp_server(name, sent["enabled"])


async def code_of(pending: Awaitable[None]) -> str:
    """The McpControlError code a call raises, or "done"."""
    try:
        await pending
    except McpControlError as error:
        return error.code
    return "done"


def mcp_requests(spawner: FakeSpawner) -> list[dict[str, Any]]:
    """Every MCP control request the fake claude was sent, in order."""
    return [
        frame["request"]
        for child in spawner.children
        for frame in child.process.replayer.seen
        if frame.get("type") == "control_request"
        and str(frame["request"].get("subtype")).startswith("mcp_")
    ]


def answered(*entries: Entry) -> list[Entry]:
    """A status read that lists SERVERS, then `entries`."""
    return [*status_script({"response": SERVERS}), *entries]


def call_in(sent: dict[str, Any]) -> Entry:
    return expect({"type": "control_request", "request_id": "req_call", "request": sent})


def reply_out(**fields: Any) -> Entry:
    return out({"type": "control_response", "response": {"request_id": "req_call", **fields}})


def patch_control(
    monkeypatch: pytest.MonkeyPatch,
    driver: ClaudeDriver,
    order: list[str],
    *,
    status: BaseException | None = None,
    error: BaseException | None = None,
    gate: asyncio.Event | None = None,
) -> None:
    """Stand-in control calls that record their order in `order`. The status read lists SERVERS
    or raises `status`; the call waits for `gate`, then raises `error` if one is given."""

    async def mcp_status() -> dict[str, Any]:
        order.append("status")
        if status is not None:
            raise status
        return SERVERS

    async def finish() -> None:
        if gate is not None:
            await gate.wait()
        if error is not None:
            raise error

    async def toggle_mcp_server(name: str, enabled: bool) -> None:
        order.append(f"toggle {name} {enabled}")
        await finish()

    async def reconnect_mcp_server(name: str) -> None:
        order.append(f"reconnect {name}")
        await finish()

    monkeypatch.setattr(driver._control, "mcp_status", mcp_status)
    monkeypatch.setattr(driver._control, "toggle_mcp_server", toggle_mcp_server)
    monkeypatch.setattr(driver._control, "reconnect_mcp_server", reconnect_mcp_server)


@pytest.mark.parametrize("sent", SENT)
async def test_a_call_writes_its_request_after_one_fresh_status_read(sent: dict[str, Any]) -> None:
    driver, spawner, _ = make_driver(answered())
    await driver.open()
    try:
        assert isinstance(driver, McpControlDriver)
        assert await code_of(call(driver, sent)) == "done"
    finally:
        await driver.close()
    assert mcp_requests(spawner) == [STATUS, sent]


@pytest.mark.parametrize(("sent", "other"), list(zip(SENT, OTHER_ERROR, strict=True)))
@pytest.mark.parametrize(
    ("text", "code"),
    [("PLANTED Connection failed", None), (APPROVAL, "not_approved"), (POLICY, "blocked")],
)
async def test_an_error_reply_gives_its_code_and_keeps_no_cli_text(
    caplog: pytest.LogCaptureFixture,
    sent: dict[str, Any],
    other: str,
    text: str,
    code: str | None,
) -> None:
    caplog.set_level(logging.DEBUG)
    script = answered(call_in(sent), reply_out(subtype="error", error=text))
    driver, spawner, _ = make_driver(script)
    await driver.open()
    try:
        with pytest.raises(McpControlError) as failure:
            await call(driver, sent)
    finally:
        await driver.close()
    error = failure.value
    assert error.code == (code or other)
    assert error.args == (error.code,)
    assert error.__cause__ is None and error.__suppress_context__
    assert not [record for record in caplog.records if text in record.getMessage()]
    assert mcp_requests(spawner) == [STATUS, sent]


async def test_a_slow_reply_outlasts_the_control_timeout() -> None:
    """An enable replies only once the server connects, so its bound is MCP_TOGGLE_TIMEOUT_S and
    not the driver's 0.05 s control timeout."""
    slow = out({"type": "fake.sleep", "s": 0.2})
    script = answered(call_in(ON), slow, reply_out(subtype="success", response={}))
    driver, spawner, _ = make_driver(script)
    await driver.open()
    try:
        assert await code_of(call(driver, ON)) == "done"
    finally:
        await driver.close()
    assert mcp_requests(spawner) == [STATUS, ON]


@pytest.mark.parametrize("sent", SENT)
async def test_an_unanswered_call_times_out_at_the_bound_read_at_call_time(
    monkeypatch: pytest.MonkeyPatch, sent: dict[str, Any]
) -> None:
    driver, spawner, _ = make_driver(answered(call_in(sent)))
    await driver.open()
    monkeypatch.setattr(control, "MCP_TOGGLE_TIMEOUT_S", 0.05)  # set after the driver was built
    try:
        assert await asyncio.wait_for(code_of(call(driver, sent)), HANG_GUARD_S) == "timeout"
    finally:
        await driver.close()
    assert mcp_requests(spawner) == [STATUS, sent]


@pytest.mark.parametrize("sent", SENT)
async def test_the_cli_exiting_during_the_call_is_not_running(sent: dict[str, Any]) -> None:
    driver, spawner, _ = make_driver(answered(call_in(sent), EXIT))
    await driver.open()
    try:
        assert await code_of(call(driver, sent)) == "not_running"
    finally:
        await driver.close()
    assert mcp_requests(spawner) == [STATUS, sent]


@pytest.mark.parametrize("sent", SENT)
@pytest.mark.parametrize(
    ("script", "code"),
    [
        (status_script(None), "timeout"),
        (status_script({"error": "no status"}, subtype="error"), "refused"),
        (status_script({"response": {"mcpServers": "none"}}), "refused"),
        (status_script({"response": {"mcpServers": [{"name": "notes", "status": "connected"}]}}),
         "unknown_server"),
    ],
)  # fmt: skip
async def test_the_status_read_decides_before_any_call(
    sent: dict[str, Any], script: list[Entry], code: str
) -> None:
    """A failed or off-shape status read refuses even an enable (nothing was saved), and a name
    the status does not list is unknown; neither writes the call."""
    driver, spawner, _ = make_driver(script)
    await driver.open()
    try:
        assert await code_of(call(driver, sent)) == code
    finally:
        await driver.close()
    assert mcp_requests(spawner) == [STATUS]


@pytest.mark.parametrize("stage", ["status", "call"])
@pytest.mark.parametrize(
    ("error", "code"),
    [
        (TimeoutError(), "timeout"),
        (ProcessError("gone"), "not_running"),
        (CLIConnectionError("gone"), "not_running"),
    ],
)
async def test_a_timeout_or_a_gone_process_at_either_stage(
    monkeypatch: pytest.MonkeyPatch, stage: str, error: BaseException, code: str
) -> None:
    driver, _, _ = make_driver([])
    driver.alive = True  # past the initialize reply; the patched calls need no process
    order: list[str] = []
    if stage == "status":
        patch_control(monkeypatch, driver, order, status=error)
    else:
        patch_control(monkeypatch, driver, order, error=error)
    assert await code_of(driver.set_mcp_server("github", True)) == code
    assert await code_of(driver.reconnect_mcp_server("github")) == code
    calls = ["toggle github True", "reconnect github"] if stage == "call" else []
    assert [step for step in order if step != "status"] == calls


async def test_the_checks_before_any_frame_run_in_order(tmp_path: Path) -> None:
    """not_running, then unknown_server (a read-only launch), then busy, then managed (an app
    server of a launch with an --mcp-config): each refuses all three calls with nothing written."""
    driver, spawner, _ = make_driver([user_in(), out(init_frame())])
    await driver.open()
    plain = driver._launch
    try:
        await driver.send("synthetic")  # a turn runs from here on
        driver._launch, driver.alive = replace(plain, read_only_tools=True), False
        assert [await code_of(call(driver, s, "viki_echo")) for s in SENT] == ["not_running"] * 3
        driver.alive = True
        assert [await code_of(call(driver, s, "viki_echo")) for s in SENT] == ["unknown_server"] * 3
        driver._launch = replace(plain, mcp_config=str(tmp_path / "mcp.json"))
        assert [await code_of(call(driver, s, "viki_echo")) for s in SENT] == ["busy"] * 3
        driver._busy = False
        assert [await code_of(call(driver, s, "viki_echo")) for s in SENT] == ["managed"] * 3
    finally:
        driver._launch = plain
        await driver.close()
    assert mcp_requests(spawner) == []


@pytest.mark.parametrize("flag", ["_closing", "_overflowed"])
async def test_a_closing_or_overflowed_driver_is_not_running(flag: str) -> None:
    driver, spawner, _ = make_driver([])
    await driver.open()
    try:
        setattr(driver, flag, True)
        assert [await code_of(call(driver, s)) for s in SENT] == ["not_running"] * 3
    finally:
        setattr(driver, flag, False)
        await driver.close()
    assert mcp_requests(spawner) == []


async def test_nothing_is_written_before_open_or_after_close() -> None:
    driver, spawner, _ = make_driver([])
    assert [await code_of(call(driver, s)) for s in SENT] == ["not_running"] * 3
    await driver.open()
    await driver.close()
    assert [await code_of(call(driver, s)) for s in SENT] == ["not_running"] * 3
    assert mcp_requests(spawner) == []


@pytest.mark.parametrize(("config", "name"), [(False, "viki_echo"), (True, "github")])
async def test_only_an_app_server_of_a_launch_with_an_mcp_config_is_managed(
    tmp_path: Path, config: bool, name: str
) -> None:
    driver, spawner, _ = make_driver(answered())
    if config:
        driver._launch = replace(driver._launch, mcp_config=str(tmp_path / "mcp.json"))
    await driver.open()
    try:
        assert await code_of(driver.set_mcp_server(name, False)) == "done"
    finally:
        await driver.close()
    toggle = {"subtype": "mcp_toggle", "serverName": name, "enabled": False}
    assert mcp_requests(spawner) == [STATUS, toggle]


@pytest.mark.parametrize("sent", SENT)
async def test_a_turn_that_begins_during_the_status_read_is_busy(
    monkeypatch: pytest.MonkeyPatch, sent: dict[str, Any]
) -> None:
    """The busy check runs again once the status is read: a turn that began while a call waited
    for the lock or for its status read stops that call before it is written."""
    driver, _, _ = make_driver([])
    driver.alive = True
    order: list[str] = []
    patch_control(monkeypatch, driver, order)
    status = driver._control.mcp_status

    async def status_then_send() -> dict[str, Any]:
        reply = await status()
        driver._busy = True  # a send went out meanwhile
        return reply

    monkeypatch.setattr(driver._control, "mcp_status", status_then_send)
    assert await code_of(call(driver, sent)) == "busy"
    assert order == ["status"]


async def test_calls_run_one_at_a_time_each_after_its_own_status_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver, _, _ = make_driver([])
    driver.alive = True
    order: list[str] = []
    gate = asyncio.Event()
    patch_control(monkeypatch, driver, order, gate=gate)
    first = asyncio.create_task(driver.set_mcp_server("github", True))
    await eventually(lambda: order == ["status", "toggle github True"])
    second = asyncio.create_task(driver.reconnect_mcp_server("github"))
    for _ in range(100):
        await asyncio.sleep(0)
    assert order == ["status", "toggle github True"]  # the reconnect waits, status read and all
    gate.set()
    await asyncio.wait_for(asyncio.gather(first, second), HANG_GUARD_S)
    assert order == ["status", "toggle github True", "status", "reconnect github"]


async def test_a_cancelled_call_stays_cancelled_and_frees_the_next_one() -> None:
    driver, spawner, _ = make_driver(answered(call_in(OFF), *status_script({"response": SERVERS})))
    await driver.open()
    try:
        pending = asyncio.create_task(driver.set_mcp_server("github", False))
        await eventually(lambda: mcp_requests(spawner) == [STATUS, OFF])
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert await code_of(driver.set_mcp_server("github", True)) == "done"
    finally:
        await driver.close()
    assert mcp_requests(spawner) == [STATUS, OFF, STATUS, ON]


async def test_a_call_cancelled_during_its_status_read_stays_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver, _, _ = make_driver([])
    driver.alive = True
    order: list[str] = []
    patch_control(monkeypatch, driver, order)
    reading = asyncio.Event()

    async def held_status() -> dict[str, Any]:
        reading.set()
        await asyncio.Event().wait()  # no reply comes
        return {}

    monkeypatch.setattr(driver._control, "mcp_status", held_status)
    pending = asyncio.create_task(driver.reconnect_mcp_server("github"))
    await reading.wait()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert order == [] and not driver._mcp_lock.locked()


class McpGatedDriver(GatedDriver):
    """A GatedDriver that also reports McpControlDriver: it records each call in `calls`, and
    refuses the name "busy" as a running turn would."""

    async def set_mcp_server(self, name: str, enabled: bool) -> None:
        if name == "busy":
            raise McpControlError("busy")
        self.calls.append(f"set {name} {enabled}")

    async def reconnect_mcp_server(self, name: str) -> None:
        if name == "busy":
            raise McpControlError("busy")
        self.calls.append(f"reconnect {name}")


async def test_the_actor_passes_both_calls_and_their_errors_through(tmp_path: Path) -> None:
    driver = McpGatedDriver()
    driver.release.set()
    actor = gated(tmp_path, driver).create(SID)
    try:
        assert await actor.start()
        await actor.set_mcp_server("github", False)
        await actor.reconnect_mcp_server("github")
        assert driver.calls[-2:] == ["set github False", "reconnect github"]
        assert await code_of(actor.set_mcp_server("busy", True)) == "busy"
        assert await code_of(actor.reconnect_mcp_server("busy")) == "busy"
    finally:
        await actor.close()


async def test_the_actor_is_not_running_before_start_and_after_close(tmp_path: Path) -> None:
    manager, spawner = setup(tmp_path, answered())
    actor = manager.create(SID)
    assert await code_of(actor.set_mcp_server("github", False)) == "not_running"
    assert await code_of(actor.reconnect_mcp_server("github")) == "not_running"
    assert await actor.start()
    await actor.set_mcp_server("github", False)  # the live claude driver has the capability
    await manager.close_all()
    assert await code_of(actor.set_mcp_server("github", False)) == "not_running"
    assert await code_of(actor.reconnect_mcp_server("github")) == "not_running"
    assert mcp_requests(spawner) == [STATUS, OFF]


async def test_the_actor_is_not_running_for_a_driver_without_the_capability(
    tmp_path: Path,
) -> None:
    driver = GatedDriver()
    driver.release.set()
    actor = gated(tmp_path, driver).create(SID)
    try:
        assert await actor.start()
        assert not isinstance(driver, McpControlDriver)
        assert await code_of(actor.set_mcp_server("github", True)) == "not_running"
        assert await code_of(actor.reconnect_mcp_server("github")) == "not_running"
    finally:
        await actor.close()


async def test_the_actor_s_calls_answer_while_a_start_holds_its_lock(tmp_path: Path) -> None:
    """A start holds the actor's lock while the driver opens; neither call waits behind it
    (mutant: take the lock, and the bounded waits below time out)."""
    driver = McpGatedDriver()
    actor = gated(tmp_path, driver).create(SID)
    sending = asyncio.create_task(actor.send("hello"))
    try:
        await driver.opening.wait()  # inside send()'s lock, waiting for driver.open() to return
        await asyncio.wait_for(actor.set_mcp_server("github", True), HANG_GUARD_S)
        await asyncio.wait_for(actor.reconnect_mcp_server("github"), HANG_GUARD_S)
        assert driver.calls[-2:] == ["set github True", "reconnect github"]
    finally:
        driver.release.set()
        assert await sending
        await actor.close()
