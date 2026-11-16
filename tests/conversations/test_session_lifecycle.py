"""Session interruption and owned-close boundaries exercised with gated drivers."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import pytest

from tests.conversations.test_session import Drivers, GatedDriver, gated
from viki_agent.drivers.base import StartError, StartFacts
from viki_agent.events import TurnDone, TurnStarted, TurnStatus, Usage, UserMessage
from viki_agent.session import CommandDispatchRejected

from replay import SID


class Gate:
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def wait(self) -> None:
        self.entered.set()
        await self.release.wait()


@pytest.mark.parametrize(
    "command,stage",
    [
        (command, stage)
        for stage in ("preflight", "mcp", "memory", "open", "context")
        for command in (False, True)
    ]
    + [(True, "accept"), (True, "intent")],
)
async def test_stop_fences_pending_work_and_later_explicit_send_works(
    tmp_path: Path, command: bool, stage: str
) -> None:
    gate = Gate()
    drivers = Drivers()
    manager = gated(tmp_path, drivers)

    async def guard(*_args: object) -> None:
        if stage == "preflight":
            await gate.wait()

    async def initialized(*_args: object) -> None:
        return None

    async def context(_session_id: str) -> str | None:
        if stage == "context":
            await gate.wait()
        return None

    async def mcp(_session_id: str) -> str | None:
        if stage == "mcp":
            await gate.wait()
        return None

    async def memory(_session_id: str) -> str | None:
        if stage == "memory":
            await gate.wait()
        return None

    manager.bind_profiles(guard, initialized, initialized=initialized)
    manager.bind_roots(initialized, context=context)
    manager.bind_mcp(mcp)
    manager.bind_bot_memory(memory)
    actor = manager.attach_prepared(SID, profile=True)
    if stage == "open":
        drivers._gate_first = True

    async def accept() -> str:
        if stage == "accept":
            await gate.wait()
        return "cancelled command"

    async def intent() -> None:
        if stage == "intent":
            await gate.wait()

    async def attempt() -> bool:
        try:
            if command:
                result = await actor.send_command(
                    expected_generation=actor.generation, accept=accept, dispatch_intent=intent
                )
                return result.status != "not_dispatched"
            return bool(
                await actor.send("cancelled ordinary turn", expected_generation=actor.generation)
            )
        except CommandDispatchRejected:
            return False

    job = asyncio.create_task(attempt())
    try:
        if stage == "open":
            await asyncio.wait_for(drivers.created.wait(), 0.05)
            await asyncio.wait_for(drivers.made[0][0].opening.wait(), 0.05)
        else:
            await asyncio.wait_for(gate.entered.wait(), 0.05)
        await actor.interrupt(expected_generation=actor.generation)
        await actor.interrupt(expected_generation=actor.generation)
    finally:
        gate.release.set()
        if drivers.made:
            drivers.made[0][0].release.set()
    assert not await job
    assert not any(call.startswith("send ") for driver, *_ in drivers.made for call in driver.calls)
    if stage in {"preflight", "mcp", "memory", "accept"}:
        assert drivers.made == []
    if stage == "open":
        assert drivers.made[0][0].calls == ["open", "interrupt", "interrupt", "opened", "close"]
        assert not actor.live
    if stage in {"context", "intent"}:
        assert not actor.live and drivers.made[0][0].calls[-1] == "close"
    assert await actor.send("later explicit turn", expected_generation=actor.generation)
    assert drivers.made[-1][0].calls[-1] == "send later explicit turn"
    await actor.close()


@pytest.mark.parametrize("command", [False, True])
async def test_stop_before_send_preserves_a_preexisting_user_runtime(
    tmp_path: Path, command: bool
) -> None:
    gate = Gate()
    drivers = Drivers()
    manager = gated(tmp_path, drivers)

    async def root(_session_id: str) -> None:
        return None

    async def context(_session_id: str) -> None:
        await gate.wait()

    async def accept() -> str:
        return "cancelled command"

    async def intent() -> None:
        return None

    manager.bind_roots(root, context=context)
    actor = manager.create(SID)
    assert await actor.start()

    async def attempt() -> object:
        if command:
            return await actor.send_command(
                expected_generation=actor.generation, accept=accept, dispatch_intent=intent
            )
        return await actor.send("cancelled ordinary turn")

    job = asyncio.create_task(attempt())
    await asyncio.wait_for(gate.entered.wait(), 0.05)
    await actor.interrupt()
    gate.release.set()
    result = await job
    assert result is False or getattr(result, "status", None) == "not_dispatched"
    assert actor.live and drivers.made[0][0].calls == ["open", "opened", "interrupt"]
    assert await actor.send("later explicit turn")
    assert len(drivers.made) == 1
    await actor.close()


@pytest.mark.parametrize("stage", ["context", "intent"])
async def test_stopped_fresh_runtime_is_retired_when_the_pending_callback_fails(
    tmp_path: Path, stage: str
) -> None:
    gate = Gate()
    drivers = Drivers()
    manager = gated(tmp_path, drivers)

    async def root(_session_id: str) -> None:
        return None

    async def context(_session_id: str) -> None:
        if stage == "context":
            await gate.wait()
            raise StartError("bad_request", "synthetic context refusal")

    async def accept() -> str:
        return "cancelled command"

    async def intent() -> None:
        if stage == "intent":
            await gate.wait()
            raise RuntimeError("synthetic intent failure")

    manager.bind_roots(root, context=context)
    actor = manager.create(SID)
    job = asyncio.create_task(
        actor.send_command(
            expected_generation=actor.generation, accept=accept, dispatch_intent=intent
        )
    )
    await asyncio.wait_for(gate.entered.wait(), 0.05)
    await actor.interrupt()
    gate.release.set()
    if stage == "intent":
        with pytest.raises(RuntimeError, match="synthetic intent failure"):
            await job
    else:
        assert (await job).status == "not_dispatched"
    assert not actor.live
    assert drivers.made[0][0].calls == ["open", "opened", "interrupt", "close"]


@pytest.mark.parametrize("command", [False, True])
async def test_stop_also_fences_an_invocation_waiting_for_the_actor_lock(
    tmp_path: Path, command: bool
) -> None:
    drivers = Drivers()
    actor = gated(tmp_path, drivers).create(SID)
    entered = asyncio.Event()
    accepted: list[str] = []

    async def accept() -> str:
        accepted.append("accepted")
        return "old command"

    async def intent() -> None:
        return None

    async def attempt() -> object:
        entered.set()
        if command:
            return await actor.send_command(
                expected_generation=actor.generation, accept=accept, dispatch_intent=intent
            )
        return await actor.send("old ordinary turn")

    await actor._lock.acquire()
    job = asyncio.create_task(attempt())
    try:
        await asyncio.wait_for(entered.wait(), 0.05)
        await actor.interrupt(expected_generation=actor.generation)
    finally:
        actor._lock.release()
    result = await job
    assert result is False or getattr(result, "status", None) == "not_dispatched"
    assert accepted == [] and drivers.made == []
    assert await actor.send("fresh invocation")
    await actor.close()


@pytest.mark.parametrize("stage", ["facts", "initialized"])
async def test_stop_during_profile_initialization_closes_only_that_open(
    tmp_path: Path, stage: str
) -> None:
    gate = Gate()

    class Driver(GatedDriver):
        async def start_facts(self) -> StartFacts:
            if stage == "facts":
                await gate.wait()
            return StartFacts(version="synthetic", mode="auto", models=(), mcp_servers=())

    driver = Driver()
    driver.release.set()
    manager = gated(tmp_path, driver)

    async def allow(*_args: object) -> None:
        return None

    async def initialized(*_args: object) -> None:
        if stage == "initialized":
            await gate.wait()

    manager.bind_profiles(allow, allow, initialized=initialized)
    actor = manager.attach_prepared(SID, profile=True)
    job = asyncio.create_task(actor.send("must not send"))
    await asyncio.wait_for(gate.entered.wait(), 0.05)
    await actor.interrupt()
    gate.release.set()
    assert not await job
    assert driver.calls[-1] == "close" and not actor.live
    assert actor.started is None


async def test_cancelled_open_does_not_retire_a_rebound_successor(tmp_path: Path) -> None:
    first, successor = GatedDriver(), GatedDriver()
    manager = gated(tmp_path, first)
    actor = manager.create(SID)
    job = asyncio.create_task(actor.send("old invocation"))
    await asyncio.wait_for(first.opening.wait(), 0.05)
    await actor.interrupt()
    actor._drop_driver()
    actor._driver, actor._token = successor, object()
    successor.alive = True
    first.release.set()
    assert not await job
    assert first.calls[-1] == "close"
    assert successor.calls == [] and successor.alive and actor.live
    await actor.close()


async def test_stale_interrupt_cannot_cancel_the_current_invocation(tmp_path: Path) -> None:
    gate = Gate()
    drivers = Drivers()
    manager = gated(tmp_path, drivers)

    async def guard(*_args: object) -> None:
        await gate.wait()

    manager.bind_profiles(guard, guard, initialized=guard)
    actor = manager.attach_prepared(SID, profile=True)
    job = asyncio.create_task(actor.send("current invocation"))
    await asyncio.wait_for(gate.entered.wait(), 0.05)
    with pytest.raises(CommandDispatchRejected, match="stale_generation"):
        await actor.interrupt(expected_generation=str(uuid.uuid4()))
    gate.release.set()
    assert await job
    assert drivers.made[-1][0].calls[-1] == "send current invocation"
    await actor.close()


@pytest.mark.parametrize("status", ["success", "error", "cancelled"])
async def test_scoped_close_retires_latest_owned_turn_after_any_terminal_status(
    tmp_path: Path, status: TurnStatus
) -> None:
    drivers = Drivers()
    manager = gated(tmp_path, drivers)
    actor = manager.create(SID)
    assert await actor.send("owned")
    turn_id = actor._turn_id
    actor._emit(TurnDone(status=status, usage=Usage()))
    await manager.close(SID, expected_generation=actor.generation, expected_turn_id=turn_id)
    assert manager.get(SID) is None
    assert drivers.made[-1][0].calls[-1] == "close"


@pytest.mark.parametrize("changed", ["turn", "steer"])
async def test_scoped_close_checks_turn_and_steers_after_taking_lock(
    tmp_path: Path, changed: str
) -> None:
    drivers = Drivers()
    manager = gated(tmp_path, drivers)
    actor = manager.create(SID)
    assert await actor.send("owned")
    turn_id, generation = actor._turn_id, actor.generation
    entered = asyncio.Event()

    async def close() -> None:
        entered.set()
        await manager.close(SID, expected_generation=generation, expected_turn_id=turn_id)

    await actor._lock.acquire()
    job = asyncio.create_task(close())
    try:
        await asyncio.wait_for(entered.wait(), 0.05)
        if changed == "turn":
            actor._emit(TurnStarted(turn_id=str(uuid.uuid4())))
        else:
            actor._steers[str(uuid.uuid4())] = UserMessage(text="newer user work")
    finally:
        actor._lock.release()
    with pytest.raises(CommandDispatchRejected, match="runtime_changed"):
        await job
    assert actor.generation == generation and not actor._closed
    assert manager.get(SID) is actor and actor.live
    assert "close" not in drivers.made[-1][0].calls
    await manager.close(SID)
