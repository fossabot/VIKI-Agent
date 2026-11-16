"""Synthetic resource-limit regressions; no native provider, transcript or vault access."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny, ToolPermissionContext

from tests.conversations.test_session import interrupt_in
from viki_agent.drivers.base import PermissionAnswer, StartError
from viki_agent.drivers.claude import driver as driver_module
from viki_agent.drivers.claude.control import Launch
from viki_agent.drivers.claude.driver import MSG_STOP_KILLED, ClaudeDriver, DriverConfig
from viki_agent.events import (
    Error,
    Event,
    PermissionRequested,
    RequestCancelled,
    TextDelta,
    TurnDone,
)

from replay import (
    DEFAULT_INIT,
    SID,
    Entry,
    FakeSpawner,
    expect,
    init_frame,
    out,
    permission_request,
    permission_response_in,
    result_frame,
    text_delta,
    user_in,
)


class Events(list[Event]):
    def __init__(self) -> None:
        super().__init__()
        self.changed = asyncio.Event()

    def emit(self, event: Event) -> None:
        self.append(event)
        self.changed.set()

    async def until(self, kind: type[Event], count: int = 1) -> None:
        async with asyncio.timeout(0.05):
            while sum(isinstance(event, kind) for event in self) < count:
                self.changed.clear()
                await self.changed.wait()


def make_driver(
    script: list[Entry], **spawner_options: Any
) -> tuple[ClaudeDriver, FakeSpawner, Events]:
    spawner = FakeSpawner(script, **spawner_options)
    events = Events()
    driver = ClaudeDriver(
        Launch(SID),
        mode="auto",
        effort="low",
        model=None,
        config=DriverConfig(
            cli=("claude.exe",),
            vault=Path("C:/SyntheticVault"),
            env={},
            spawner=spawner,
            grace_s=0.01,
            poll_s=0.01,
            control_timeout_s=0.05,
        ),
        emit=events.emit,
    )
    return driver, spawner, events


def limits(monkeypatch: pytest.MonkeyPatch, *, count: int = 4, size: int = 1024 * 1024) -> None:
    monkeypatch.setattr(driver_module, "FRAME_QUEUE_ITEMS", count, raising=False)
    monkeypatch.setattr(driver_module, "FRAME_QUEUE_BYTES", size, raising=False)


def feed(spawner: FakeSpawner, *frames: dict[str, Any]) -> None:
    spawner.children[0].process.stdout.feed_data(
        b"".join(json.dumps(frame, ensure_ascii=False).encode("utf-8") + b"\n" for frame in frames)
    )


def assert_terminal(driver: ClaudeDriver, events: Events) -> None:
    errors = [event for event in events if isinstance(event, Error)]
    assert len(errors) == 1
    assert errors[0].code == "frame_error" and not errors[0].recoverable
    assert [event.status for event in events if isinstance(event, TurnDone)] == ["error"]
    assert not driver.alive and not driver.busy


@pytest.mark.parametrize("by_bytes", [False, True])
async def test_overflow_is_one_terminal_error_without_success_or_retry(
    monkeypatch: pytest.MonkeyPatch, by_bytes: bool
) -> None:
    limits(monkeypatch, count=100 if by_bytes else 4, size=1024 if by_bytes else 1024 * 1024)
    script = [user_in(), out(init_frame()), *[out(text_delta("界" * 100))] * 8, out(result_frame())]
    driver, spawner, events = make_driver(script)
    try:
        await driver.open()
        await driver.send("synthetic")
        await events.until(Error)
        assert_terminal(driver, events)
    finally:
        await driver.close()
    assert spawner.children[0].released and len(spawner.children) == 1


async def test_exact_admission_boundary_reserves_overflow_and_exit_slots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    limits(monkeypatch, count=4, size=100)
    driver, _, _ = make_driver([])
    for _ in range(4):
        driver._enqueue_frame(text_delta("bounded"), 25)
    assert (driver._queued_count, driver._queued_bytes, driver._frames.qsize()) == (4, 100, 4)
    driver._enqueue_frame(text_delta("over"), 1)
    driver._enqueue_frame({"type": "viki.exit", "code": 0}, 0)
    for _ in range(1000):
        driver._enqueue_frame(text_delta("discard"), 100)
    assert driver._overflowed
    assert (driver._queued_count, driver._queued_bytes, driver._frames.qsize()) == (4, 100, 6)
    await driver.close()
    assert driver._frames.empty() and driver._queued_bytes == 0


async def test_transport_frame_limit_is_terminal_before_a_later_success_result() -> None:
    driver, spawner, events = make_driver(
        [user_in(), out(init_frame()), out(text_delta("x" * 5000)), out(result_frame())]
    )
    driver._transport._max_frame = 4096
    try:
        await driver.open()
        await driver.send("synthetic")
        await events.until(Error)
        assert_terminal(driver, events)
    finally:
        await driver.close()
    assert spawner.children[0].released


async def test_overflow_releases_a_wedged_child_without_an_explicit_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    limits(monkeypatch)
    driver, spawner, events = make_driver(
        [user_in(), out(init_frame()), *[out(text_delta("backlog"))] * 8], ignore_eof=True
    )
    try:
        await driver.open()
        await driver.send("synthetic")
        await events.until(Error)
        assert driver._pump_task is not None
        async with asyncio.timeout(0.05):
            await asyncio.shield(driver._pump_task)
        assert_terminal(driver, events)
        assert spawner.children[0].killed and spawner.children[0].released
        assert driver._frames.empty() and driver._queued_bytes == 0
    finally:
        await driver.close()


async def test_concurrent_close_and_overflow_share_one_disconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    limits(monkeypatch)
    driver, spawner, events = make_driver([user_in(), out(init_frame())])
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0
    original_close = driver._control.close

    async def gated_close() -> None:
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        await original_close()

    monkeypatch.setattr(driver._control, "close", gated_close)
    closing: asyncio.Task[None] | None = None
    try:
        await driver.open()
        await driver.send("synthetic")
        closing = asyncio.create_task(driver.close())
        async with asyncio.timeout(0.05):
            await entered.wait()
        for _ in range(8):
            driver._enqueue_frame(text_delta("backlog"), 100)
        await events.until(Error)
        release.set()
        async with asyncio.timeout(0.05):
            await closing
        assert calls == 1 and spawner.children[0].released
        assert_terminal(driver, events)
    finally:
        release.set()
        if closing:
            await closing
        await driver.close()


async def test_card_limit_is_32_pending_futures_and_33_is_denied() -> None:
    driver, spawner, events = make_driver([user_in(), out(init_frame())])
    tasks: list[asyncio.Task[Any]] = []
    try:
        await driver.open()
        await driver.send("synthetic")
        for number in range(32):
            tasks.append(
                asyncio.create_task(
                    driver._can_use_tool(
                        "WebFetch",
                        {"url": "https://example.com"},
                        ToolPermissionContext(tool_use_id=f"card-{number}"),
                    )
                )
            )
        await events.until(PermissionRequested, count=32)
        assert len(driver.pending()) == 32 and not driver._overflowed
        answer = await driver._can_use_tool(
            "WebFetch", {"url": "https://example.com"}, ToolPermissionContext(tool_use_id="card-33")
        )
        assert isinstance(answer, PermissionResultDeny)
        await events.until(Error)
        assert_terminal(driver, events)
        assert len([event for event in events if isinstance(event, RequestCancelled)]) == 32
    finally:
        await driver.close()
        await asyncio.gather(*tasks, return_exceptions=True)
    assert spawner.children[0].released


async def test_duplicate_tool_use_id_cannot_replace_the_original_decision() -> None:
    driver, _, events = make_driver([])
    task: asyncio.Task[Any] | None = None
    try:
        await driver.open()
        ctx = ToolPermissionContext(tool_use_id="same")
        task = asyncio.create_task(driver._can_use_tool("WebFetch", {"url": "first"}, ctx))
        await events.until(PermissionRequested)
        original = driver._cards["same"]
        duplicate = await driver._can_use_tool("WebFetch", {"url": "different"}, ctx)
        assert isinstance(duplicate, PermissionResultDeny)
        assert driver._cards["same"] is original and len(driver.pending()) == 1
        assert driver.respond("same", PermissionAnswer(allow=True))
        assert isinstance(await task, PermissionResultAllow)
    finally:
        await driver.close()
        if task:
            await asyncio.gather(task, return_exceptions=True)


async def test_overflow_between_local_allow_and_native_delivery_denies_and_cancels_card(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    limits(monkeypatch)
    driver, _, events = make_driver([user_in(), out(init_frame())])
    task: asyncio.Task[Any] | None = None
    try:
        await driver.open()
        await driver.send("synthetic")
        task = asyncio.create_task(
            driver._can_use_tool(
                "WebFetch",
                {"url": "https://example.com"},
                ToolPermissionContext(tool_use_id="racing"),
            )
        )
        await events.until(PermissionRequested)
        assert driver.respond("racing", PermissionAnswer(allow=True))
        for _ in range(8):
            driver._enqueue_frame(text_delta("backlog"), 100)
        assert isinstance(await task, PermissionResultDeny)
        await events.until(Error)
        assert_terminal(driver, events)
        assert len([event for event in events if isinstance(event, RequestCancelled)]) == 1
    finally:
        await driver.close()
        if task:
            await asyncio.gather(task, return_exceptions=True)


async def test_overflow_revokes_an_allow_already_waiting_for_the_stdin_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    limits(monkeypatch)
    driver, spawner, events = make_driver(
        [
            user_in(),
            out(init_frame()),
            out(permission_request("pending")),
            permission_response_in("pending"),
            out(result_frame()),
        ]
    )
    writing = asyncio.Event()
    original_write = driver._transport.write

    async def observed_write(data: str) -> None:
        if '"allow"' in data:
            writing.set()
        await original_write(data)

    monkeypatch.setattr(driver._transport, "write", observed_write)
    try:
        await driver.open()
        await driver.send("synthetic")
        await events.until(PermissionRequested)
        async with driver._transport._stdin_lock:
            assert driver.respond("toolu_1", PermissionAnswer(allow=True))
            async with asyncio.timeout(0.05):
                await writing.wait()
            for _ in range(8):
                driver._enqueue_frame(text_delta("backlog"), 100)
            assert not driver._transport.is_ready()
        await events.until(Error)
        assert_terminal(driver, events)
    finally:
        await driver.close()
    assert not any(
        frame.get("response", {}).get("response", {}).get("behavior") == "allow"
        for frame in spawner.children[0].process.replayer.seen
    )
    assert spawner.children[0].released


@pytest.mark.parametrize("answer_initialize", [False, True])
async def test_initialization_overflow_is_typed_and_reaps_the_child(
    monkeypatch: pytest.MonkeyPatch, answer_initialize: bool
) -> None:
    limits(monkeypatch)
    flood = [out(text_delta("startup"))] * 8
    script = (
        flood
        if answer_initialize
        else [
            expect({"type": "control_request", "request": {"subtype": "initialize"}}),
            *flood,
        ]
    )
    driver, spawner, events = make_driver(script)
    try:
        async with asyncio.timeout(0.05):
            with pytest.raises(StartError) as failure:
                await driver.open()
        assert failure.value.code == "frame_error"
        assert not driver.alive and not events
        assert spawner.children[0].released
    finally:
        await driver.close()


async def test_overflow_cancels_a_stalled_optional_context_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    limits(monkeypatch)
    driver, spawner, events = make_driver([user_in(), out(init_frame()), out(result_frame())])
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def stalled() -> dict[str, Any]:
        entered.set()
        try:
            await asyncio.Future()
        finally:
            cancelled.set()
        return {}

    monkeypatch.setattr(driver._control, "context_usage", stalled)
    try:
        await driver.open()
        await driver.send("synthetic")
        async with asyncio.timeout(0.05):
            await entered.wait()
        feed(spawner, *[text_delta("backlog")] * 8, result_frame())
        await events.until(Error)
        assert cancelled.is_set()
        assert_terminal(driver, events)
    finally:
        await driver.close()


async def test_overflow_cancels_existing_cards_and_refuses_late_allow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    limits(monkeypatch)
    driver, spawner, events = make_driver(
        [user_in(), out(init_frame()), out(permission_request("pending"))]
    )
    try:
        await driver.open()
        await driver.send("synthetic")
        await events.until(PermissionRequested)
        feed(spawner, *[text_delta("backlog")] * 8, result_frame())
        await events.until(Error)
        assert_terminal(driver, events)
        assert len([event for event in events if isinstance(event, RequestCancelled)]) == 1
        assert not driver.pending()
        assert not driver.respond("toolu_1", PermissionAnswer(allow=True))
        assert not any(
            frame.get("type") == "control_response"
            and frame.get("response", {}).get("response", {}).get("behavior") == "allow"
            for frame in spawner.children[0].process.replayer.seen
        )
    finally:
        await driver.close()


async def test_an_unacknowledged_interrupt_reports_forced_stop_not_crashed() -> None:
    """Pins `ClaudeDriver._on_exit`: both stop-ladder rungs must read a `_killed` exit as
    `forced_stop`, never `crashed`. This is rung 3: the CLI is inside the turn, the interrupt is
    sent but never acknowledged, so the control call times out and the driver kills the tree
    itself. Rung 1 (a stop asked for before the CLI picks the turn up) needs a short
    `stop_pickup_s`; `make_driver` has no parameter for that (its `DriverConfig` is built inline
    with the 30 s default), so that rung is pinned only at the SessionActor level
    (tests/test_session.py, `test_a_stop_the_cli_never_picks_up_kills_the_tree`)."""
    script = [user_in(), out(init_frame()), out(text_delta("x")), interrupt_in()]
    driver, spawner, events = make_driver(script)
    try:
        await driver.open()
        await driver.send("synthetic")
        await events.until(TextDelta)  # the CLI is inside the turn: interrupt() sends now
        await driver.interrupt()
        await events.until(Error)
        turn_done, error = events[-2], events[-1]
        assert isinstance(turn_done, TurnDone) and turn_done.status == "cancelled"
        assert isinstance(error, Error)
        assert error.code == "forced_stop"
        assert error.message == MSG_STOP_KILLED
        assert error.recoverable is True
        assert sum(isinstance(event, Error) for event in events) == 1
        assert spawner.children[0].killed
    finally:
        await driver.close()


async def test_started_mode_is_the_mode_the_cli_reports() -> None:
    script = [
        expect(
            {
                "type": "control_request",
                "request_id": "rec-init",
                "request": {"subtype": "initialize"},
            }
        ),
        out(
            {
                "type": "control_response",
                "response": {
                    "subtype": "success",
                    "request_id": "rec-init",
                    "response": {**DEFAULT_INIT, "current_permission_mode": "default"},
                },
            }
        ),
    ]
    driver, _, _ = make_driver(script)
    try:
        assert (await driver.open()).mode == "default"
    finally:
        await driver.close()
