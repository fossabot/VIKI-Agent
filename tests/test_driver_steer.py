"""Driver-level steers: the CLI's command_lifecycle frames end each one, in the orders captured
live; an exit drops what is still pending; a close first settles them. Synthetic frames only."""

from __future__ import annotations

import asyncio
import dataclasses
from typing import Any

import pytest

from tests.conversations.test_session import interrupt_ack, interrupt_in
from tests.test_driver_limits import Events, feed, limits, make_driver
from viki_agent.drivers.base import SendError
from viki_agent.drivers.claude.driver import MSG_OVERFLOW
from viki_agent.events import Error, SteerEnded, TextDelta, ToolStarted, TurnDone, TurnStarted
from viki_agent.turn_input import Attachment, AttachmentUnavailable, TurnInput

from replay import SID, FakeSpawner, init_frame, out, result_frame, text_delta, user_in

STEER = "22222222-2222-4222-8222-222222222222"
OTHER = "33333333-3333-4333-8333-333333333333"
LATER = "0aaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"  # sorts before STEER, written after it
FRAME_ID = "44444444-4444-4444-8444-444444444444"
IMAGE = Attachment("00000000-0000-4000-8000-000000000012", "image/png", 4, 1, 1, "synthetic.png")
HANG_GUARD_S = 5  # Bounds a settle that normally ends in milliseconds, not a wait on real work.
SETTLE_S = 0.02


def lifecycle(command_uuid: object, state: object) -> dict[str, Any]:
    """The stdout lifecycle frame for the stdin frame whose `uuid` was `command_uuid`."""
    return {
        "type": "command_lifecycle",
        "command_uuid": command_uuid,
        "state": state,
        "uuid": FRAME_ID,
        "session_id": SID,
    }


def tool_use(tool_id: str) -> dict[str, Any]:
    block = {"type": "tool_use", "id": tool_id, "name": "Read", "input": {"file_path": "a.md"}}
    message = {"id": f"msg_{tool_id}", "role": "assistant", "content": [block]}
    return {"type": "assistant", "message": message, "parent_tool_use_id": None, "session_id": SID}


def user_frame(block: dict[str, Any]) -> dict[str, Any]:
    message = {"role": "user", "content": [block]}
    return {"type": "user", "message": message, "parent_tool_use_id": None, "session_id": SID}


def tool_result(tool_id: str) -> dict[str, Any]:
    return user_frame({"type": "tool_result", "tool_use_id": tool_id, "content": "ok"})


def aborted_result() -> dict[str, Any]:
    """The result of a turn an interrupt ended during a tool call."""
    frame = result_frame(subtype="error_during_execution")
    return {**frame, "is_error": True, "terminal_reason": "aborted_tools"}


def trace(events: Events) -> list[str]:
    """The turn and steer story, and any error, in emit order."""
    rows: list[str] = []
    for event in events:
        if isinstance(event, TurnStarted):
            rows.append("turn.started")
        elif isinstance(event, TurnDone):
            rows.append(f"turn.done {event.status}")
        elif isinstance(event, SteerEnded):
            rows.append(f"steer.ended {event.steer_id} {event.outcome}")
        elif isinstance(event, Error):
            rows.append(f"error {event.code}")
    return rows


async def settle(spawner: FakeSpawner, events: Events) -> None:
    """Return once every frame fed so far has been handled: a marker fed after them came out."""
    marker = f"marker {len(events)}"
    feed(spawner, text_delta(marker))
    async with asyncio.timeout(0.05):
        while not any(isinstance(event, TextDelta) and event.text == marker for event in events):
            events.changed.clear()
            await events.changed.wait()


def user_frames(spawner: FakeSpawner) -> list[dict[str, Any]]:
    return [f for f in spawner.children[0].process.replayer.seen if f.get("type") == "user"]


def interrupts(spawner: FakeSpawner) -> int:
    return sum(
        f.get("type") == "control_request" and f["request"].get("subtype") == "interrupt"
        for f in spawner.children[0].process.replayer.seen
    )


async def test_s1_a_steer_started_inside_the_turn_opens_no_second_turn() -> None:
    driver, spawner, events = make_driver([user_in(), out(init_frame()), user_in()])
    try:
        await driver.open()
        await driver.send("synthetic")
        feed(spawner, tool_use("toolu_a"))
        await events.until(ToolStarted)
        await driver.steer(TurnInput("go"), STEER)
        feed(spawner, lifecycle(STEER, "queued"), tool_result("toolu_a"))
        feed(spawner, lifecycle(STEER, "started"))
        await events.until(SteerEnded)
        assert driver.busy
        feed(spawner, tool_use("toolu_b"), tool_result("toolu_b"))
        feed(spawner, lifecycle(STEER, "completed"), result_frame())
        await events.until(TurnDone)
        assert trace(events) == [f"steer.ended {STEER} started", "turn.done success"]
        assert not driver.busy
    finally:
        await driver.close()


async def test_s2_a_steer_started_after_the_result_runs_as_its_own_turn() -> None:
    driver, spawner, events = make_driver([user_in(), out(init_frame()), user_in()])
    try:
        await driver.open()
        await driver.send("synthetic")
        feed(spawner, text_delta("final"))
        await events.until(TextDelta)
        await driver.steer(TurnInput("go"), STEER)
        feed(spawner, lifecycle(STEER, "queued"), result_frame(), lifecycle(STEER, "started"))
        await events.until(TurnStarted)
        assert trace(events) == [
            "turn.done success",
            f"steer.ended {STEER} started",
            "turn.started",
        ]
        assert driver.busy  # from `started` on, before the steer's own system/init
        feed(spawner, init_frame(), text_delta("steered"), result_frame())
        feed(spawner, lifecycle(STEER, "completed"))
        await events.until(TurnDone, 2)
        await settle(spawner, events)
        assert trace(events) == [
            "turn.done success",
            f"steer.ended {STEER} started",
            "turn.started",
            "turn.done success",
        ]
        assert not driver.busy
    finally:
        await driver.close()


async def test_s3_a_steer_after_a_stop_runs_as_its_own_turn_that_stop_can_end() -> None:
    driver, spawner, events = make_driver([user_in(), out(init_frame()), user_in()])
    try:
        await driver.open()
        await driver.send("synthetic")
        feed(spawner, tool_use("toolu_a"))
        await events.until(ToolStarted)
        await driver.steer(TurnInput("go"), STEER)
        await driver.interrupt()
        assert interrupts(spawner) == 1
        feed(spawner, lifecycle(STEER, "queued"), tool_result("toolu_a"))
        feed(spawner, user_frame({"type": "text", "text": "[Request interrupted by user]"}))
        feed(spawner, aborted_result(), lifecycle(STEER, "started"))
        await events.until(TurnStarted)
        assert trace(events) == [
            "turn.done cancelled",
            f"steer.ended {STEER} started",
            "turn.started",
        ]
        feed(spawner, init_frame(), tool_use("toolu_b"))
        await events.until(ToolStarted, 2)
        await driver.interrupt()
        assert interrupts(spawner) == 2
    finally:
        await driver.close()


async def test_a_stop_after_a_steer_starts_waits_for_its_turns_system_init() -> None:
    driver, spawner, events = make_driver([user_in(), out(init_frame()), user_in()])
    try:
        await driver.open()
        await driver.send("synthetic")
        await driver.steer(TurnInput("go"), STEER)
        feed(spawner, result_frame(), lifecycle(STEER, "started"))
        await events.until(TurnStarted)
        await driver.interrupt()
        await settle(spawner, events)
        assert interrupts(spawner) == 0 and driver.busy
        stop = driver._stop_task
        assert stop is not None
        feed(spawner, init_frame())  # the CLI is inside the steer's turn: the stop goes out
        await asyncio.wait({stop}, timeout=0.05)
        assert stop.done() and interrupts(spawner) == 1
    finally:
        await driver.close()


async def test_a_stop_in_a_turn_the_cli_starts_itself_goes_out_at_once() -> None:
    driver, spawner, events = make_driver([user_in(), out(init_frame())])
    try:
        await driver.open()
        await driver.send("synthetic")
        feed(spawner, result_frame(), init_frame())  # a background agent wakes the CLI
        await events.until(TurnStarted)
        await driver.interrupt()
        assert interrupts(spawner) == 1
    finally:
        await driver.close()


@pytest.mark.parametrize("state", ["cancelled", "discarded", "refused"])
async def test_a_steer_the_cli_drops_ends_dropped_and_opens_no_turn(state: str) -> None:
    driver, spawner, events = make_driver([user_in(), out(init_frame()), user_in()])
    try:
        await driver.open()
        await driver.send("synthetic")
        await driver.steer(TurnInput("go"), STEER)
        feed(spawner, result_frame(), lifecycle(STEER, state), lifecycle(STEER, "started"))
        await settle(spawner, events)
        assert trace(events) == ["turn.done success", f"steer.ended {STEER} dropped"]
        assert not driver.busy
    finally:
        await driver.close()


async def test_completed_without_started_ends_it_started_and_opens_no_turn() -> None:
    driver, spawner, events = make_driver([user_in(), out(init_frame()), user_in()])
    try:
        await driver.open()
        await driver.send("synthetic")
        await driver.steer(TurnInput("go"), STEER)
        feed(spawner, result_frame(), lifecycle(STEER, "completed"), lifecycle(STEER, "started"))
        await settle(spawner, events)
        assert trace(events) == ["turn.done success", f"steer.ended {STEER} started"]
        assert not driver.busy
    finally:
        await driver.close()


@pytest.mark.parametrize(
    ("command_uuid", "state"),
    [
        (STEER, "queued"),
        (STEER, "paused"),  # a state the CLI has not named
        (OTHER, "started"),  # an id this driver never wrote
        ([STEER], "started"),  # not a string, nor hashable
        (None, "started"),
    ],
)
async def test_queued_unknown_or_foreign_lifecycle_frames_change_nothing(
    command_uuid: object, state: str
) -> None:
    driver, spawner, events = make_driver([user_in(), out(init_frame()), user_in()])
    try:
        await driver.open()
        await driver.send("synthetic")
        await driver.steer(TurnInput("go"), STEER)
        feed(spawner, result_frame(), lifecycle(command_uuid, state))
        await settle(spawner, events)
        assert trace(events) == ["turn.done success"]
        assert not driver.busy
        feed(spawner, lifecycle(STEER, "started"))  # still pending
        await events.until(TurnStarted)
        assert trace(events) == [
            "turn.done success",
            f"steer.ended {STEER} started",
            "turn.started",
        ]
    finally:
        await driver.close()


async def test_an_exit_drops_every_pending_steer_in_write_order_before_the_turn_end() -> None:
    script = [user_in(), out(init_frame()), user_in(), user_in(), user_in()]
    driver, spawner, events = make_driver(script)
    try:
        await driver.open()
        await driver.send("synthetic")
        await driver.steer(TurnInput("first"), STEER)
        await driver.steer(TurnInput("middle"), OTHER)
        await driver.steer(TurnInput("last"), LATER)
        feed(spawner, lifecycle(OTHER, "started"))
        await events.until(SteerEnded)
        spawner.children[0].kill_tree()
        await events.until(Error)
        assert trace(events) == [
            f"steer.ended {OTHER} started",
            f"steer.ended {STEER} dropped",
            f"steer.ended {LATER} dropped",
            "turn.done error",
            "error crashed",
        ]
        assert not driver._steers
    finally:
        await driver.close()


async def test_an_unreadable_image_steer_writes_nothing_and_leaves_the_turn_and_its_stop() -> None:
    # No system/init yet, so the stop stays armed while the steer fails.
    driver, spawner, events = make_driver([user_in()])
    try:
        await driver.open()
        await driver.send("synthetic")
        await driver.interrupt()
        stop = driver._stop_task
        assert stop is not None
        with pytest.raises(AttachmentUnavailable):
            await driver.steer(TurnInput("look", (IMAGE,)), STEER)  # no resolver configured
        assert driver.busy and driver._stop_wanted and driver._stop_task is stop
        assert not stop.done()
        assert len(user_frames(spawner)) == 1
        feed(spawner, init_frame())  # the armed stop goes out
        await asyncio.wait({stop}, timeout=0.05)
        assert interrupts(spawner) == 1
        feed(spawner, lifecycle(STEER, "started"), result_frame(), lifecycle(STEER, "started"))
        await settle(spawner, events)
        assert trace(events) == ["turn.done success"]
    finally:
        await driver.close()


async def test_a_steer_after_an_overflow_is_refused_unsent(monkeypatch: pytest.MonkeyPatch) -> None:
    limits(monkeypatch)
    driver, spawner, events = make_driver([user_in(), out(init_frame())])
    try:
        await driver.open()
        await driver.send("synthetic")
        for _ in range(8):
            driver._enqueue_frame(text_delta("backlog"), 100)
        await events.until(Error)
        with pytest.raises(SendError) as refused:
            await driver.steer(TurnInput("go"), STEER)
        error = refused.value
        assert (error.code, error.message, error.unsent) == ("frame_error", MSG_OVERFLOW, True)
        assert not driver._steers
        assert len(user_frames(spawner)) == 1
    finally:
        await driver.close()


async def test_a_lifecycle_frame_handled_mid_write_ends_the_steer_once() -> None:
    driver, spawner, events = make_driver([user_in(), out(init_frame()), user_in()])

    async def resolve(attachment: Attachment) -> bytes:
        # steer() is still inside its write: the CLI's report on the steer overtakes its return.
        feed(spawner, lifecycle(STEER, "started"))
        await events.until(SteerEnded)
        return b"\x89PNG"

    driver._config = dataclasses.replace(driver._config, resolve_attachment=resolve)
    try:
        await driver.open()
        await driver.send("synthetic")
        await driver.steer(TurnInput("look", (IMAGE,)), STEER)
        feed(spawner, lifecycle(STEER, "completed"))
        await settle(spawner, events)
        assert trace(events) == [f"steer.ended {STEER} started"]
        assert len(user_frames(spawner)) == 2
    finally:
        await driver.close()


# --- settle_steers: what a close waits for ---------------------------------------------------


async def test_settle_stops_the_turn_a_waiting_steer_starts_after_the_close_stop() -> None:
    script = [
        user_in(),
        out(init_frame()),
        out(tool_use("toolu_a")),
        user_in(),  # the steer
        out(lifecycle(STEER, "queued")),
        interrupt_in("rec-close"),  # the close asks first
        interrupt_ack("rec-close"),
        out(tool_result("toolu_a")),
        out(user_frame({"type": "text", "text": "[Request interrupted by user for tool use]"})),
        out(aborted_result()),
        out(lifecycle(STEER, "started")),
        interrupt_in("rec-settle"),  # the settle stops the steer's own turn
        interrupt_ack("rec-settle"),
        out(aborted_result()),
        out(lifecycle(STEER, "completed")),
    ]
    driver, spawner, events = make_driver(script)
    try:
        await driver.open()
        await driver.send("synthetic")
        await events.until(ToolStarted)
        await driver.steer(TurnInput("go"), STEER)
        await driver.interrupt()
        settling = asyncio.create_task(driver.settle_steers(HANG_GUARD_S))
        await events.until(TurnStarted)
        await settle(spawner, events)
        # The steer's turn sent no system/init yet: its stop is armed, not sent.
        assert interrupts(spawner) == 1 and driver._stop_wanted
        feed(spawner, init_frame())
        async with asyncio.timeout(0.05):
            await settling
        assert interrupts(spawner) == 2
        assert trace(events) == [
            "turn.done cancelled",
            f"steer.ended {STEER} started",
            "turn.started",
            "turn.done cancelled",
        ]
        assert not driver.busy and not driver._steers
    finally:
        await driver.close()


async def test_settle_stops_the_turn_a_steer_starts_while_the_driver_is_idle() -> None:
    script = [
        user_in(),
        out(init_frame()),
        user_in(),  # the steer, written while the final text streamed
        out(lifecycle(STEER, "queued")),
        out(result_frame()),
        interrupt_in("rec-settle"),
        interrupt_ack("rec-settle"),
        out(aborted_result()),
        out(lifecycle(STEER, "completed")),
    ]
    driver, spawner, events = make_driver(script)
    try:
        await driver.open()
        await driver.send("synthetic")
        await driver.steer(TurnInput("go"), STEER)
        await events.until(TurnDone)
        assert not driver.busy  # so a close sends no interrupt of its own
        settling = asyncio.create_task(driver.settle_steers(HANG_GUARD_S))
        await settle(spawner, events)
        assert not settling.done()  # a steer is pending
        feed(spawner, lifecycle(STEER, "started"))
        await events.until(TurnStarted)
        await settle(spawner, events)
        assert interrupts(spawner) == 0 and driver._stop_wanted  # armed until its system/init
        feed(spawner, init_frame())
        async with asyncio.timeout(0.05):
            await settling
        assert interrupts(spawner) == 1
        assert trace(events) == [
            "turn.done success",
            f"steer.ended {STEER} started",
            "turn.started",
            "turn.done cancelled",
        ]
        assert not driver.busy and not driver._steers
    finally:
        await driver.close()


async def test_settle_returns_after_its_bound_when_no_lifecycle_frame_comes() -> None:
    driver, spawner, events = make_driver([user_in(), out(init_frame()), user_in()])
    try:
        await driver.open()
        await driver.send("synthetic")
        await driver.steer(TurnInput("go"), STEER)
        loop = asyncio.get_running_loop()
        started_at = loop.time()
        await driver.settle_steers(SETTLE_S)
        assert loop.time() - started_at >= SETTLE_S - 0.001
        assert list(driver._steers) == [STEER] and driver.busy
        assert interrupts(spawner) == 0 and trace(events) == []
    finally:
        await driver.close()


async def test_settle_returns_at_once_with_no_pending_steer() -> None:
    driver, spawner, events = make_driver([user_in(), out(init_frame())])
    try:
        await driver.open()
        await driver.send("synthetic")
        async with asyncio.timeout(0.05):
            await driver.settle_steers(HANG_GUARD_S)
        assert driver.busy and interrupts(spawner) == 0 and trace(events) == []
    finally:
        await driver.close()


async def test_an_exit_while_settling_ends_the_wait() -> None:
    driver, spawner, events = make_driver([user_in(), out(init_frame()), user_in()])
    try:
        await driver.open()
        await driver.send("synthetic")
        await driver.steer(TurnInput("go"), STEER)
        settling = asyncio.create_task(driver.settle_steers(HANG_GUARD_S))
        await settle(spawner, events)
        assert not settling.done()
        spawner.children[0].kill_tree()
        async with asyncio.timeout(0.05):
            await settling
        assert trace(events) == [f"steer.ended {STEER} dropped", "turn.done error", "error crashed"]
    finally:
        await driver.close()


async def test_an_overflow_while_settling_ends_the_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    limits(monkeypatch)
    driver, spawner, events = make_driver([user_in(), out(init_frame()), user_in()])
    try:
        await driver.open()
        await driver.send("synthetic")
        await driver.steer(TurnInput("go"), STEER)
        settling = asyncio.create_task(driver.settle_steers(HANG_GUARD_S))
        await settle(spawner, events)
        assert not settling.done()
        for _ in range(8):
            driver._enqueue_frame(text_delta("backlog"), 100)
        async with asyncio.timeout(0.05):
            await settling
        assert list(driver._steers) == [STEER]  # an overflow ends no steer: the wait ends anyway
    finally:
        await driver.close()
