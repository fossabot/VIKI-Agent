"""The inbox host: the real `CaptureSuggester` over the suggestion run's
test fakes, so every start below is the run **Ask VIKI to suggest** makes."""

from __future__ import annotations

import asyncio
import inspect
import re
import threading
from dataclasses import replace
from typing import Any

import pytest

from tests.vaults.test_capture_suggest import (
    HANG_GUARD_S,
    REF_A,
    REF_B,
    STAMP,
    WS,
    FakeCaptures,
    FakeHost,
    FakeSession,
    answer,
    hang,
    listing,
    make,
    text,
)
from tests.vaults.test_capture_suggest import done as turn_done
from tests.vaults.test_capture_suggest import reply as viki_reply
from viki_agent.decision_loop import inbox_host
from viki_agent.decision_loop.contracts import DecisionLoopError, InboxLook, InboxRun
from viki_agent.decision_loop.inbox_host import SuggestionHost
from viki_agent.decision_loop.inbox_triage import state_key
from viki_agent.storage import StateError
from viki_agent.vaults.capture_suggest import CaptureSuggester
from viki_agent.vaults.captures import CaptureListing


def key_of(value: CaptureListing, scope: str = WS) -> str:
    """The inbox item's key for the state `value` lists, as the cycle computes it."""
    return state_key(scope, [(capture.ref, capture.modified) for capture in value.captures])


ONE_CAPTURE = replace(listing(), captures=listing().captures[:1])
KEY = key_of(listing())
OTHER = key_of(ONE_CAPTURE)
DEADLINE_S = 0.05


def port(
    host: Any, captures: FakeCaptures | None = None
) -> tuple[SuggestionHost, CaptureSuggester, set[asyncio.Task[None]]]:
    suggester, tasks = make(host, captures, deadline_s=DEADLINE_S)
    return SuggestionHost(suggester), suggester, tasks


def creates(host: FakeHost) -> list[tuple[Any, ...]]:
    return [call for call in host.calls if call[0] == "create"]


async def finish(tasks: set[asyncio.Task[None]]) -> None:
    """Wait for the suggester's run tasks; one may be done already."""
    async with asyncio.timeout(HANG_GUARD_S):
        await asyncio.gather(*tasks, return_exceptions=True)


async def refused(start: Any) -> str:
    with pytest.raises(DecisionLoopError) as caught:
        await start
    return caught.value.code


async def test_start_runs_the_suggesters_own_run_once_per_key() -> None:
    host = FakeHost(answer(REF_A), answer(REF_A))
    host_port, suggester, tasks = port(host)

    first, second = await asyncio.gather(
        host_port.start(WS, KEY), host_port.start(WS, KEY), return_exceptions=True
    )
    await finish(tasks)

    assert isinstance(second, DecisionLoopError) and second.code == "busy"  # the key is pending
    run = suggester.run_for(WS)
    assert run is not None and first == run.run_id
    assert await refused(host_port.start(WS, KEY)) == "not_applicable"
    (create,) = creates(host)  # one session, the suggestion run's own read-only one
    assert create[2] == {"read_only_tools": True}
    assert host_port.read(WS).started == {KEY: run.run_id}


async def test_a_busy_suggester_is_asked_again_later() -> None:
    host = FakeHost(hang, answer(REF_A))
    captures = FakeCaptures(listing())
    host_port, suggester, tasks = port(host, captures)

    first = await host_port.start(WS, KEY)
    async with asyncio.timeout(HANG_GUARD_S):
        await host.sent.wait()

    assert await refused(host_port.start(WS, OTHER)) == "busy"
    look = host_port.read(WS)
    assert look.run == InboxRun(first, "running", None, STAMP)
    assert look.started == {KEY: first}  # the busy key is not kept
    for task in tasks:
        task.cancel()
    await finish(tasks)
    captures.value = ONE_CAPTURE
    second = await host_port.start(WS, OTHER)
    await finish(tasks)
    assert second != first
    assert host_port.read(WS).started == {KEY: first, OTHER: second}


class RefusingHost(FakeHost):
    def remember(self, session_id: str, **changes: Any) -> None:
        raise StateError("unavailable")


@pytest.mark.parametrize(
    ("host", "captures", "code"),
    [
        pytest.param(
            FakeHost(),
            FakeCaptures(replace(listing(), captures=())),
            "not_applicable",
            id="no-captures",
        ),
        pytest.param(RefusingHost(), FakeCaptures(listing()), "unavailable", id="state-error"),
    ],
)
async def test_a_refusal_is_remembered_and_not_asked_again(
    host: FakeHost, captures: FakeCaptures, code: str
) -> None:
    host_port, suggester, tasks = port(host, captures)

    assert await refused(host_port.start(WS, KEY)) == code
    assert await refused(host_port.start(WS, KEY)) == "not_applicable"

    assert captures.calls == [WS]  # the suggester was asked once
    assert host_port.read(WS).started == {KEY: ""}
    assert suggester.run_for(WS) is None and not tasks


class BlockingCaptures(FakeCaptures):
    """`list` runs in a worker thread: it says it was entered, then waits for `release`."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        super().__init__(listing())
        self.loop = loop
        self.entered = asyncio.Event()
        self.release = threading.Event()
        self.returned = asyncio.Event()

    def list(self, workspace_id: str) -> Any:
        self.loop.call_soon_threadsafe(self.entered.set)
        self.release.wait(HANG_GUARD_S)
        try:
            return super().list(workspace_id)
        finally:
            self.loop.call_soon_threadsafe(self.returned.set)


async def test_a_cancelled_start_still_records_its_run() -> None:
    host = FakeHost(answer(REF_A))
    captures = BlockingCaptures(asyncio.get_running_loop())
    host_port, suggester, tasks = port(host, captures)

    caller = asyncio.create_task(host_port.start(WS, KEY))
    async with asyncio.timeout(HANG_GUARD_S):
        await captures.entered.wait()
    caller.cancel()
    try:
        with pytest.raises(asyncio.CancelledError):
            async with asyncio.timeout(HANG_GUARD_S):
                await caller
        assert host_port.read(WS).started == {}
    finally:
        captures.release.set()
    async with asyncio.timeout(HANG_GUARD_S):
        await host.sent.wait()  # the run's task exists once its prompt is sent
    await finish(tasks)

    run = suggester.run_for(WS)
    assert run is not None and run.state == "done"
    assert host_port.read(WS).started == {KEY: run.run_id}


async def test_close_cancels_a_start_in_flight_and_refuses_the_next() -> None:
    host = FakeHost(answer(REF_A))
    captures = BlockingCaptures(asyncio.get_running_loop())
    host_port, suggester, tasks = port(host, captures)

    caller = asyncio.create_task(host_port.start(WS, KEY))
    async with asyncio.timeout(HANG_GUARD_S):
        await captures.entered.wait()
    try:
        await host_port.close()
        with pytest.raises(asyncio.CancelledError):
            async with asyncio.timeout(DEADLINE_S):
                await caller  # the start was cancelled, so its caller is released at once
    finally:
        captures.release.set()
    async with asyncio.timeout(HANG_GUARD_S):
        await captures.returned.wait()  # the listing is over: a surviving start would go on now
    await asyncio.sleep(0)

    assert creates(host) == [] and not tasks and suggester.run_for(WS) is None
    assert host_port.read(WS).started == {}
    assert await refused(host_port.start(WS, OTHER)) == "unavailable"
    assert captures.calls == [WS]  # the refused start never reached the suggester


async def test_read_answers_the_suggesters_memory() -> None:
    host = FakeHost(answer(REF_A, REF_B))
    host_port, suggester, tasks = port(host)
    before = host_port.read(WS)

    run_id = await host_port.start(WS, KEY)
    await finish(tasks)

    assert before == InboxLook(None, {}, {})  # a copy: the start did not change it
    assert host_port.read(WS) == InboxLook(
        InboxRun(run_id, "done", None, STAMP), {REF_A: STAMP, REF_B: STAMP}, {KEY: run_id}
    )
    assert suggester.run_for(WS) is not None
    assert host_port.read("another") == InboxLook(None, {}, {})  # each vault reads its own keys


async def test_started_keys_are_capped_oldest_first() -> None:
    captures = FakeCaptures(replace(listing(), captures=()))
    host_port, _, _ = port(FakeHost(), captures)
    keys = [f"{index:064x}" for index in range(inbox_host.MAX_STARTED + 1)]

    for key in keys:
        assert await refused(host_port.start(WS, key)) == "not_applicable"

    assert list(host_port.read(WS).started) == keys[1:]


class EmptyElsewhere(FakeCaptures):
    """Lists its captures in `WS` only: another vault's start is refused, and remembered."""

    def list(self, workspace_id: str) -> Any:
        value = super().list(workspace_id)
        return value if workspace_id == WS else replace(value, captures=())


async def test_another_vaults_starts_never_evict_a_vaults_current_state() -> None:
    """Past `MAX_STARTED` starts in the launch, a vault's state started earlier is still known
    as started, so VIKI does not run it, and send its captures, a second time."""
    host_port, suggester, tasks = port(FakeHost(answer(REF_A, REF_B)), EmptyElsewhere(listing()))
    first = await host_port.start(WS, KEY)
    await finish(tasks)
    for index in range(inbox_host.MAX_STARTED + 1):
        assert await refused(host_port.start("another", f"{index:064x}")) == "not_applicable"

    assert host_port.read(WS).started == {KEY: first}
    assert await refused(host_port.start(WS, KEY)) == "not_applicable"
    assert len(host_port.read("another").started) == inbox_host.MAX_STARTED
    run = suggester.run_for(WS)
    assert run is not None and run.run_id == first


async def test_a_capture_written_since_the_cycle_listed_the_inbox_sends_nothing() -> None:
    """VIKI's start is bound to the state the cycle checked Never send against: a capture that
    lands before the suggester lists the inbox (perhaps in a held folder) refuses the start, and
    the state is not asked again. The user's own Ask VIKI to suggest still sends its listing."""
    held = replace(listing().captures[0], ref="00-inbox/health/lab-results.md")
    captures = FakeCaptures(replace(listing(), captures=(*listing().captures, held)))
    host = FakeHost(answer(REF_A, REF_B))
    host_port, suggester, tasks = port(host, captures)

    assert await refused(host_port.start(WS, KEY)) == "not_applicable"
    assert await refused(host_port.start(WS, KEY)) == "not_applicable"

    assert captures.calls == [WS]  # listed once, then remembered
    assert creates(host) == [] and not host.sent.is_set() and not tasks
    assert host_port.read(WS).started == {KEY: ""} and suggester.run_for(WS) is None
    await suggester.start(WS)  # no bound: the user's own run
    async with asyncio.timeout(HANG_GUARD_S):
        await host.sent.wait()
    await finish(tasks)
    assert len(creates(host)) == 1


async def test_the_run_keeps_only_the_suggesters_own_deny_path() -> None:
    async def card(session: FakeSession) -> bool | None:
        async def play() -> None:
            session.emit("permission.requested", {"tool_use_id": "t1"})
            await session.answered.wait()
            for kind, data in (text(viki_reply(REF_A)), turn_done()):
                session.emit(kind, data)

        session.player = asyncio.create_task(play())
        return True

    host = FakeHost(card)
    host_port, suggester, tasks = port(host)

    run_id = await host_port.start(WS, KEY)
    await finish(tasks)

    (create,) = creates(host)
    session = host.sessions[create[1]]
    assert create[2] == {"read_only_tools": True}
    assert session.denied == ["t1"]
    assert not session.denied_while_emitting  # denied by the suggester's task
    run = suggester.run_for(WS)
    assert run is not None and (run.run_id, run.state) == (run_id, "done")


def test_the_port_answers_no_card() -> None:
    source = inspect.getsource(inbox_host)

    for name in ("deny_pending", "respond", "interrupt", "set_permission_mode", "PermissionAnswer"):
        assert name not in source
    assert set(re.findall(r"_suggester\.(\w+)", source)) <= {"start", "run_for", "suggestions"}
