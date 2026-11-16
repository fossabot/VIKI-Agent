"""Progress call scheduling, driven through step() with a recording fake and a fake clock."""

from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest

from viki_agent.messaging import mattermost_progress
from viki_agent.messaging.mattermost_http import ProgressError, ProgressHttp
from viki_agent.messaging.mattermost_progress import (
    MAX_CLOSING,
    MAX_SEEN,
    MAX_TYPING_CALLS,
    MattermostProgress,
)

HANG_GUARD_S = 30  # only for an event that normally fires in milliseconds
THREAD, PROMPT, OTHER = (char * 26 for char in "tpo")
STATUS = "u" * 26
Call = tuple[str, ...]


class FakeHttp:
    def __init__(self) -> None:
        self.calls: list[Call] = []
        self.fail: set[str] = set()
        self.closed = False
        self.typed = asyncio.Event()
        self.creates = 0

    def _record(self, *call: str) -> None:
        self.calls.append(call)
        if call[0] in self.fail:
            raise ProgressError
        if call[0] == "typing":
            self.typed.set()

    async def react(self, post_id: str, emoji: str) -> None:
        self._record("react", post_id, emoji)

    async def unreact(self, post_id: str) -> None:
        self._record("unreact", post_id)

    async def typing(self, parent_id: str) -> None:
        self._record("typing", parent_id)

    async def create(self, message: str, root_id: str) -> str:
        self._record("create", message, root_id)
        self.creates += 1
        return STATUS[:-1] + str(self.creates)

    async def edit(self, post_id: str, message: str) -> None:
        self._record("edit", post_id, message)

    async def close(self) -> None:
        self.closed = True

    def take(self) -> list[Call]:
        calls, self.calls = self.calls, []
        return calls


class Rig:
    def __init__(self, **kwargs: float) -> None:
        self.http = FakeHttp()
        self.now = 0.0
        self.text = "line 1"
        self.is_waiting = False
        self.is_idle = True
        self.progress = MattermostProgress(
            cast(ProgressHttp, self.http),
            line=lambda: self.text,
            waiting=lambda: self.is_waiting,
            idle=lambda: self.is_idle,
            clock=lambda: self.now,
            **kwargs,
        )

    async def step(self, advance: float = 0.0) -> tuple[list[Call], float | None]:
        self.now += advance
        delay = await self.progress.step()
        return self.http.take(), delay


def status_id(n: int = 1) -> str:
    return STATUS[:-1] + str(n)


async def test_turn_progress_calls_in_order() -> None:
    rig = Rig()
    rig.progress.turn_started(THREAD, PROMPT)

    calls, delay = await rig.step()
    assert calls == [("react", PROMPT, "eyes"), ("typing", THREAD)]
    assert delay == 5.0

    calls, delay = await rig.step(20.0)
    assert calls == [("typing", THREAD), ("create", "line 1", THREAD)]
    assert delay == 5.0


async def test_typing_and_status_cadence() -> None:
    rig = Rig()
    rig.progress.turn_started(THREAD, None)
    assert (await rig.step())[0] == [("typing", THREAD)]

    assert await rig.step(4.0) == ([], 1.0)
    assert (await rig.step(1.0))[0] == [("typing", THREAD)]
    assert (await rig.step(5.0))[0] == [("typing", THREAD)]
    assert (await rig.step(5.0))[0] == [("typing", THREAD)]
    assert (await rig.step(5.0))[0] == [("typing", THREAD), ("create", "line 1", THREAD)]

    # Same text: the status is due again but nothing is edited.
    assert (await rig.step(20.0))[0] == [("typing", THREAD)]
    rig.text = "line 2"
    assert (await rig.step(20.0))[0] == [("typing", THREAD), ("edit", status_id(), "line 2")]
    assert (await rig.step(20.0))[0] == [("typing", THREAD)]


async def test_waiting_stops_typing_but_not_the_status() -> None:
    rig = Rig()
    rig.progress.turn_started(THREAD, None)
    await rig.step()
    rig.is_waiting = True

    assert await rig.step(5.0) == ([], 15.0)
    assert await rig.step(15.0) == ([("create", "line 1", THREAD)], 20.0)

    rig.is_waiting = False
    assert (await rig.step(0.0))[0] == [("typing", THREAD)]


async def test_not_idle_makes_no_call() -> None:
    rig = Rig()
    rig.progress.accepted(OTHER)
    rig.progress.turn_started(THREAD, PROMPT)
    rig.is_idle = False

    assert await rig.step(100.0) == ([], None)

    rig.is_idle = True
    calls, _ = await rig.step()
    assert calls[:2] == [("react", OTHER, "eyes"), ("react", PROMPT, "eyes")]


async def test_idle_is_checked_between_calls() -> None:
    rig = Rig()
    rig.progress.turn_started(THREAD, PROMPT)
    original = rig.http.react

    async def react_then_busy(post_id: str, emoji: str) -> None:
        await original(post_id, emoji)
        rig.is_idle = False

    rig.http.react = react_then_busy  # type: ignore[method-assign]
    calls, delay = await rig.step()
    assert calls == [("react", PROMPT, "eyes")]
    assert delay is None


async def test_failure_ends_that_turn_and_the_next_turn_works() -> None:
    rig = Rig()
    rig.http.fail = {"react"}
    rig.progress.turn_started(THREAD, PROMPT)

    calls, delay = await rig.step()
    assert calls == [("react", PROMPT, "eyes")]
    assert delay is None
    assert await rig.step(100.0) == ([], None)

    rig.http.fail = set()
    rig.progress.turn_started(THREAD, OTHER)
    assert (await rig.step())[0] == [("react", OTHER, "eyes"), ("typing", THREAD)]


async def test_failing_call_is_not_retried() -> None:
    rig = Rig()
    rig.http.fail = {"create"}
    rig.progress.turn_started(THREAD, None)
    await rig.step()
    assert (await rig.step(20.0))[0] == [("typing", THREAD), ("create", "line 1", THREAD)]
    assert await rig.step(100.0) == ([], None)


async def test_line_failure_fails_the_turn() -> None:
    rig = Rig()

    def broken() -> str:
        raise RuntimeError

    rig.progress = MattermostProgress(
        cast(ProgressHttp, rig.http),
        line=broken,
        waiting=lambda: False,
        idle=lambda: True,
        clock=lambda: rig.now,
    )
    rig.progress.turn_started(THREAD, None)
    await rig.step()
    assert await rig.step(20.0) == ([("typing", THREAD)], None)


async def test_typing_is_capped() -> None:
    rig = Rig(status_interval_s=1e9)
    rig.progress.turn_started(THREAD, None)
    calls: list[Call] = []
    for _ in range(MAX_TYPING_CALLS + 5):
        calls += (await rig.step(5.0))[0]
    assert calls == [("typing", THREAD)] * MAX_TYPING_CALLS
    delay = (await rig.step(5.0))[1]
    assert delay is not None and delay > 1e8


async def test_stop_before_the_turn_closes_the_marked_prompt() -> None:
    rig = Rig()
    rig.progress.accepted(PROMPT)
    assert await rig.step() == ([("react", PROMPT, "eyes")], None)
    rig.progress.turn_ended("stopped", "final", prompt=PROMPT)
    assert await rig.step() == (
        [("unreact", PROMPT), ("react", PROMPT, "black_square_for_stop")],
        None,
    )


async def test_stop_before_the_turn_drops_a_queued_eyes() -> None:
    rig = Rig()
    rig.progress.accepted(PROMPT)
    rig.progress.turn_ended("stopped", "final", prompt=PROMPT)
    assert await rig.step() == ([("react", PROMPT, "black_square_for_stop")], None)


async def test_stop_while_the_eyes_call_is_in_flight() -> None:
    rig = Rig()
    original = rig.http.react

    async def react_then_stop(post_id: str, emoji: str) -> None:
        await original(post_id, emoji)
        if emoji == "eyes":
            rig.progress.turn_ended("stopped", "final", prompt=PROMPT)

    rig.http.react = react_then_stop  # type: ignore[method-assign]
    rig.progress.accepted(PROMPT)
    assert await rig.step() == (
        [
            ("react", PROMPT, "eyes"),
            ("unreact", PROMPT),
            ("react", PROMPT, "black_square_for_stop"),
        ],
        None,
    )


async def test_prompt_is_ignored_while_a_turn_is_open() -> None:
    rig = Rig()
    rig.progress.turn_started(THREAD, PROMPT)
    rig.progress.turn_ended("stopped", "final", prompt=OTHER)
    calls, _ = await rig.step()
    assert calls == [("react", PROMPT, "black_square_for_stop")]


@pytest.mark.parametrize(
    ("outcome", "emoji"),
    [("done", "white_check_mark"), ("error", "warning"), ("stopped", "black_square_for_stop")],
)
async def test_closing_swaps_eyes_for_the_outcome(outcome: Any, emoji: str) -> None:
    rig = Rig()
    rig.progress.turn_started(THREAD, PROMPT)
    await rig.step()
    await rig.step(20.0)

    rig.progress.turn_ended(outcome, "final text")
    calls, delay = await rig.step()
    assert calls == [
        ("edit", status_id(), "final text"),
        ("unreact", PROMPT),
        ("react", PROMPT, emoji),
    ]
    assert delay is None


async def test_closing_without_status_or_eyes_only_adds_the_outcome() -> None:
    rig = Rig()
    rig.progress.turn_started(THREAD, PROMPT)
    rig.progress.turn_ended("done", "final")
    assert await rig.step() == ([("react", PROMPT, "white_check_mark")], None)


async def test_closing_failure_drops_its_remaining_calls() -> None:
    rig = Rig()
    rig.progress.turn_started(THREAD, PROMPT)
    await rig.step()
    await rig.step(20.0)
    rig.http.fail = {"edit"}
    rig.progress.turn_ended("done", "final")
    calls, _ = await rig.step()
    assert calls == [("edit", status_id(), "final")]


async def test_turn_closing_while_the_next_one_starts() -> None:
    rig = Rig()
    rig.progress.turn_started(THREAD, PROMPT)
    await rig.step()
    await rig.step(20.0)

    rig.progress.turn_ended("done", "first final")
    rig.progress.turn_started(THREAD, OTHER)
    calls, delay = await rig.step()
    assert calls == [
        ("edit", status_id(), "first final"),
        ("unreact", PROMPT),
        ("react", PROMPT, "white_check_mark"),
        ("react", OTHER, "eyes"),
        ("typing", THREAD),
    ]
    assert delay == 5.0

    # The next turn keeps its own status reply.
    assert (await rig.step(20.0))[0] == [("typing", THREAD), ("create", "line 1", THREAD)]


async def test_final_edit_after_status_create_in_flight() -> None:
    rig = Rig()
    rig.progress.turn_started(THREAD, PROMPT)
    await rig.step()
    entered, gate = asyncio.Event(), asyncio.Event()
    original = rig.http.create

    async def create_when_released(message: str, root_id: str) -> str:
        entered.set()
        await gate.wait()
        return await original(message, root_id)

    rig.http.create = create_when_released  # type: ignore[method-assign]
    rig.now = 20.0
    step = asyncio.create_task(rig.progress.step())
    await asyncio.wait_for(entered.wait(), HANG_GUARD_S)

    rig.progress.turn_ended("done", "Done.")
    gate.set()
    await asyncio.wait_for(step, HANG_GUARD_S)
    await rig.progress.step()

    assert rig.http.take() == [
        ("typing", THREAD),
        ("create", "line 1", THREAD),
        ("edit", status_id(), "Done."),
        ("unreact", PROMPT),
        ("react", PROMPT, "white_check_mark"),
    ]


async def test_closing_queue_keeps_the_newest_turns() -> None:
    rig = Rig()
    ids = [f"{n:026d}" for n in range(MAX_CLOSING + 2)]
    for post in ids:
        rig.progress.turn_started(THREAD, post)
        rig.progress.turn_ended("stopped", "x")
    calls, _ = await rig.step()
    assert calls == [("react", post, "black_square_for_stop") for post in ids[2:]]


async def test_accepted_marks_eyes_before_turn_start() -> None:
    rig = Rig()
    rig.progress.accepted(PROMPT)
    assert await rig.step() == ([("react", PROMPT, "eyes")], None)

    rig.progress.turn_started(THREAD, PROMPT)
    assert (await rig.step())[0] == [("typing", THREAD)]

    rig.progress.turn_ended("done", "final")
    assert (await rig.step())[0] == [
        ("unreact", PROMPT),
        ("react", PROMPT, "white_check_mark"),
    ]


async def test_accepted_still_pending_at_turn_start_is_not_marked_twice() -> None:
    rig = Rig()
    rig.progress.accepted(PROMPT)
    rig.progress.turn_started(THREAD, PROMPT)
    calls, _ = await rig.step()
    assert calls == [("react", PROMPT, "eyes"), ("typing", THREAD)]


async def test_accepted_is_capped_and_ignores_repeats() -> None:
    rig = Rig()
    for n in range(MAX_SEEN + 3):
        rig.progress.accepted(f"{n:026d}")
    rig.progress.accepted(f"{0:026d}")
    calls, _ = await rig.step()
    assert calls == [("react", f"{n:026d}", "eyes") for n in range(MAX_SEEN)]


async def test_worker_loop_runs_calls_and_closes() -> None:
    rig = Rig()
    rig.progress.start()
    rig.progress.start()  # a second start keeps the one worker
    rig.progress.turn_started(THREAD, None)
    rig.progress.wake()
    await asyncio.wait_for(rig.http.typed.wait(), HANG_GUARD_S)
    assert rig.http.calls == [("typing", THREAD)]

    await asyncio.wait_for(rig.progress.close(), HANG_GUARD_S)
    assert rig.http.closed


async def test_wake_during_call_is_not_lost() -> None:
    rig = Rig()
    entered, gate, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    typing, react = rig.http.typing, rig.http.react

    async def typing_when_released(parent_id: str) -> None:
        entered.set()
        await gate.wait()
        await typing(parent_id)

    async def react_and_note_close(post_id: str, emoji: str) -> None:
        await react(post_id, emoji)
        if emoji == "white_check_mark":
            closed.set()

    # The chat reads busy once, right after the in-flight call: the step then ends with nothing
    # to do, and only the wake that arrived during that call makes the worker look again.
    busy_once = [True]

    def idle() -> bool:
        if gate.is_set() and busy_once:
            busy_once.clear()
            return False
        return True

    rig.progress = MattermostProgress(
        cast(ProgressHttp, rig.http),
        line=lambda: rig.text,
        waiting=lambda: False,
        idle=idle,
        clock=lambda: rig.now,
    )
    rig.http.typing = typing_when_released  # type: ignore[method-assign]
    rig.http.react = react_and_note_close  # type: ignore[method-assign]
    rig.progress.start()
    rig.progress.turn_started(THREAD, PROMPT)
    await asyncio.wait_for(entered.wait(), HANG_GUARD_S)

    rig.progress.turn_ended("done", "final")
    gate.set()
    try:
        await asyncio.wait_for(closed.wait(), HANG_GUARD_S)
    finally:
        await asyncio.wait_for(rig.progress.close(), HANG_GUARD_S)

    assert rig.http.calls[-1] == ("react", PROMPT, "white_check_mark")


async def test_stop_cancels_the_worker() -> None:
    rig = Rig()
    rig.progress.start()
    await asyncio.sleep(0)
    task = rig.progress._task  # noqa: SLF001
    assert task is not None and not task.done()
    rig.progress.stop()
    await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), HANG_GUARD_S)
    assert task.cancelled()


def test_constants() -> None:
    assert mattermost_progress.STATUS_INTERVAL_S == 20.0
    assert mattermost_progress.TYPING_INTERVAL_S == 5.0
    assert (MAX_TYPING_CALLS, MAX_SEEN, MAX_CLOSING) == (360, 8, 4)
