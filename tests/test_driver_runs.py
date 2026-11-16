"""Driver-level run tracking: a card's run and raise time, and stop_run's outcomes."""

from __future__ import annotations

import asyncio
import re
from typing import Any

import pytest
from claude_agent_sdk import PermissionResultAllow, ToolPermissionContext

from tests.test_driver_limits import feed, make_driver
from viki_agent.drivers.base import PermissionAnswer, RunStopError
from viki_agent.events import PermissionRequested, TextDelta

from replay import Entry, expect, init_frame, out, text_delta, user_in

STAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z")


def task_started(task_id: str, tool_use_id: str) -> dict[str, Any]:
    return {
        "type": "system",
        "subtype": "task_started",
        "task_id": task_id,
        "tool_use_id": tool_use_id,
        "subagent_type": "general-purpose",
    }


def task_notification(task_id: str, tool_use_id: str, status: str = "completed") -> dict[str, Any]:
    return {
        "type": "system",
        "subtype": "task_notification",
        "task_id": task_id,
        "tool_use_id": tool_use_id,
        "status": status,
    }


def task_updated(task_id: str, status: str) -> dict[str, Any]:
    return {
        "type": "system",
        "subtype": "task_updated",
        "task_id": task_id,
        "patch": {"status": status},
    }


def stop_task_in(task_id: str, recorded_id: str = "rec-stop") -> Entry:
    return expect(
        {
            "type": "control_request",
            "request_id": recorded_id,
            "request": {"subtype": "stop_task", "task_id": task_id},
        }
    )


def stop_task_error_out(recorded_id: str = "rec-stop") -> Entry:
    return out(
        {
            "type": "control_response",
            "response": {"subtype": "error", "request_id": recorded_id, "error": "boom"},
        }
    )


async def test_a_subagent_card_names_its_run_and_leaves_requests_once_answered() -> None:
    driver, spawner, events = make_driver([user_in(), out(init_frame())])
    task: asyncio.Task[Any] | None = None
    try:
        await driver.open()
        await driver.send("synthetic")
        feed(spawner, task_started("a1", "blk1"), text_delta("x"))
        await events.until(TextDelta)  # task_started has been handled by now

        ctx = ToolPermissionContext(tool_use_id="card1", agent_id="a1")
        task = asyncio.create_task(
            driver._can_use_tool("WebFetch", {"url": "https://example.com"}, ctx)
        )
        await events.until(PermissionRequested)

        rows = driver.requests()
        assert len(rows) == 1
        row = rows[0]
        assert set(row) == {"tool_use_id", "run", "raised_at"}
        assert row["tool_use_id"] == "card1"
        assert row["run"] == "blk1"
        assert isinstance(row["raised_at"], str) and STAMP_RE.fullmatch(row["raised_at"])
        assert len(row["raised_at"]) == 24

        assert driver.respond("card1", PermissionAnswer(allow=True))
        assert isinstance(await task, PermissionResultAllow)
        assert driver.requests() == []
    finally:
        await driver.close()
        if task:
            await asyncio.gather(task, return_exceptions=True)


async def test_a_main_thread_card_has_no_run() -> None:
    driver, _, events = make_driver([])
    task: asyncio.Task[Any] | None = None
    try:
        await driver.open()
        ctx = ToolPermissionContext(tool_use_id="card2")
        task = asyncio.create_task(
            driver._can_use_tool("WebFetch", {"url": "https://example.com"}, ctx)
        )
        await events.until(PermissionRequested)

        rows = driver.requests()
        assert len(rows) == 1
        assert rows[0]["tool_use_id"] == "card2"
        assert rows[0]["run"] is None
    finally:
        await driver.close()
        if task:
            await asyncio.gather(task, return_exceptions=True)


async def test_two_concurrent_runs_each_card_names_its_own_run() -> None:
    driver, spawner, events = make_driver([user_in(), out(init_frame())])
    tasks: list[asyncio.Task[Any]] = []
    try:
        await driver.open()
        await driver.send("synthetic")
        feed(spawner, task_started("a1", "blk1"), task_started("a2", "blk2"), text_delta("x"))
        await events.until(TextDelta)

        # card2 (agent a2) is raised before card1 (agent a1): arrival order must not decide this.
        for card, agent in (("card2", "a2"), ("card1", "a1")):
            ctx = ToolPermissionContext(tool_use_id=card, agent_id=agent)
            tasks.append(
                asyncio.create_task(
                    driver._can_use_tool("WebFetch", {"url": "https://example.com"}, ctx)
                )
            )
        await events.until(PermissionRequested, count=2)

        rows = {row["tool_use_id"]: row["run"] for row in driver.requests()}
        assert rows == {"card1": "blk1", "card2": "blk2"}
    finally:
        await driver.close()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_stop_run_writes_exactly_the_stop_task_request() -> None:
    driver, spawner, events = make_driver([user_in(), out(init_frame())])
    try:
        await driver.open()
        await driver.send("synthetic")
        feed(spawner, task_started("a1", "blk1"), text_delta("x"))
        await events.until(TextDelta)

        await driver.stop_run("blk1")

        requests = [
            frame["request"]
            for frame in spawner.children[0].process.replayer.seen
            if frame.get("type") == "control_request"
            and frame["request"].get("subtype") == "stop_task"
        ]
        assert requests == [{"subtype": "stop_task", "task_id": "a1"}]
    finally:
        await driver.close()


async def test_unknown_run_for_a_never_started_block_and_one_already_notified() -> None:
    driver, spawner, events = make_driver([user_in(), out(init_frame())])
    try:
        await driver.open()
        await driver.send("synthetic")

        with pytest.raises(RunStopError) as missing:
            await driver.stop_run("never-started")
        assert missing.value.code == "unknown_run"

        feed(spawner, task_started("a1", "blk1"), text_delta("x"))
        await events.until(TextDelta)
        feed(spawner, task_notification("a1", "blk1"), text_delta("y"))
        await events.until(TextDelta, count=2)

        with pytest.raises(RunStopError) as done:
            await driver.stop_run("blk1")
        assert done.value.code == "unknown_run"
    finally:
        await driver.close()


async def test_stop_run_after_task_updated_killed_only_is_unknown_run_and_sends_nothing() -> None:
    """A task_updated "killed" ends the run on its own (no task_notification need ever follow,
    per the SDK's stop_task docstring); stop_run must see it as already gone, and never reach
    the CLI to ask again."""
    driver, spawner, events = make_driver([user_in(), out(init_frame())])
    try:
        await driver.open()
        await driver.send("synthetic")
        feed(spawner, task_started("a1", "blk1"), text_delta("x"))
        await events.until(TextDelta)
        feed(spawner, task_updated("a1", "killed"), text_delta("y"))
        await events.until(TextDelta, count=2)

        with pytest.raises(RunStopError) as failure:
            await driver.stop_run("blk1")
        assert failure.value.code == "unknown_run"

        requests = [
            frame["request"]
            for frame in spawner.children[0].process.replayer.seen
            if frame.get("type") == "control_request"
            and frame["request"].get("subtype") == "stop_task"
        ]
        assert requests == []
    finally:
        await driver.close()


@pytest.mark.parametrize("signal", ["task_started", "task_updated"])
async def test_stop_run_is_available_again_after_known_task_resumes(signal: str) -> None:
    driver, spawner, events = make_driver([user_in(), out(init_frame())])
    try:
        await driver.open()
        await driver.send("synthetic")
        feed(spawner, task_started("a1", "blk1"), task_updated("a1", "stopped"), text_delta("x"))
        await events.until(TextDelta)
        with pytest.raises(RunStopError) as stopped:
            await driver.stop_run("blk1")
        assert stopped.value.code == "unknown_run"

        resume = (
            task_started("a1", "blk1")
            if signal == "task_started"
            else task_updated("a1", "running")
        )
        feed(spawner, resume, text_delta("y"))
        await events.until(TextDelta, count=2)
        await driver.stop_run("blk1")
        requests = [
            frame["request"]
            for frame in spawner.children[0].process.replayer.seen
            if frame.get("type") == "control_request"
            and frame["request"].get("subtype") == "stop_task"
        ]
        assert requests == [{"subtype": "stop_task", "task_id": "a1"}]
        feed(spawner, task_notification("a1", "blk1"), text_delta("z"))
        await events.until(TextDelta, count=3)
        with pytest.raises(RunStopError) as completed:
            await driver.stop_run("blk1")
        assert completed.value.code == "unknown_run"
    finally:
        await driver.close()


async def test_unknown_task_running_update_does_not_make_a_stop_target() -> None:
    driver, spawner, events = make_driver([user_in(), out(init_frame())])
    try:
        await driver.open()
        await driver.send("synthetic")
        feed(spawner, task_updated("unknown", "running"), text_delta("x"))
        await events.until(TextDelta)
        with pytest.raises(RunStopError) as failure:
            await driver.stop_run("unknown")
        assert failure.value.code == "unknown_run"
    finally:
        await driver.close()


@pytest.mark.parametrize("subagent_type", [None, "reviewer"])
def test_resumed_start_retains_permission_identity_unless_explicitly_replaced(
    subagent_type: str | None,
) -> None:
    driver, _, _ = make_driver([])
    driver._watch(task_started("a1", "blk1"))
    driver._watch(task_updated("a1", "stopped"))
    resumed = task_started("a1", "blk1")
    if subagent_type is None:
        resumed.pop("subagent_type")
    else:
        resumed["subagent_type"] = subagent_type
    driver._watch(resumed)
    card = driver._card("WebFetch", {}, ToolPermissionContext(tool_use_id="card", agent_id="a1"))
    assert isinstance(card, PermissionRequested)
    assert card.agent == (subagent_type or "general-purpose")


async def test_stop_run_is_refused_on_a_scripted_error_reply() -> None:
    driver, spawner, events = make_driver(
        [user_in(), out(init_frame()), stop_task_in("a1"), stop_task_error_out()]
    )
    try:
        await driver.open()
        await driver.send("synthetic")
        feed(spawner, task_started("a1", "blk1"), text_delta("x"))
        await events.until(TextDelta)

        with pytest.raises(RunStopError) as failure:
            await driver.stop_run("blk1")
        assert failure.value.code == "refused"
    finally:
        await driver.close()


async def test_stop_run_times_out_without_an_answer() -> None:
    driver, spawner, events = make_driver([user_in(), out(init_frame()), stop_task_in("a1")])
    try:
        await driver.open()
        await driver.send("synthetic")
        feed(spawner, task_started("a1", "blk1"), text_delta("x"))
        await events.until(TextDelta)

        with pytest.raises(RunStopError) as failure:
            await driver.stop_run("blk1")
        assert failure.value.code == "timeout"
    finally:
        await driver.close()


async def test_stop_run_right_after_the_cli_exits_mid_stop_is_not_running() -> None:
    driver, spawner, events = make_driver(
        [user_in(), out(init_frame()), stop_task_in("a1"), out({"type": "fake.exit", "code": 1})]
    )
    driver._control._timeout = 2.0  # margin so exit-detection races the control timeout and wins
    try:
        await driver.open()
        await driver.send("synthetic")
        feed(spawner, task_started("a1", "blk1"), text_delta("x"))
        await events.until(TextDelta)

        with pytest.raises(RunStopError) as failure:
            await driver.stop_run("blk1")
        assert failure.value.code == "not_running"
        query = driver._control._client._query
        assert not (query.pending_control_responses if query else {})
    finally:
        await driver.close()


async def test_stop_run_is_not_running_after_close() -> None:
    driver, _, _ = make_driver([])
    await driver.open()
    await driver.close()

    with pytest.raises(RunStopError) as failure:
        await driver.stop_run("blk1")
    assert failure.value.code == "not_running"
