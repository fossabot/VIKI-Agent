"""The live-driver bound includes pending starts and is advertised in full recovery."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import pytest

from tests.conversations.test_session import HANG_GUARD_S, Drivers, GatedDriver, gated, watch
from viki_agent.drivers.base import StartError
from viki_agent.drivers.claude.control import Launch
from viki_agent.events import SessionStarted, Status

IDS = [str(uuid.UUID(int=index)) for index in range(1, 25)]


async def test_eight_pending_starts_refuse_ninth_before_driver_factory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = GatedDriver.open

    async def pending_open(driver: GatedDriver) -> SessionStarted:
        driver.release.clear()
        return await original(driver)

    monkeypatch.setattr(GatedDriver, "open", pending_open)
    drivers = Drivers()
    manager = gated(tmp_path, drivers)
    actors = [manager.create(sid) for sid in IDS[:9]]
    seen = watch(actors[-1])
    starts = [asyncio.create_task(actor.start()) for actor in actors]
    try:
        await asyncio.wait_for(drivers.created.wait(), HANG_GUARD_S)
        assert len(drivers.made) == len(manager.live()) == 8
        assert all(not driver.alive for driver, *_ in drivers.made)
        assert [task.done() for task in starts] == [False] * 8 + [True]
        assert seen[-1]["data"] == {
            "code": "too_many_sessions",
            "message": (
                "You can run at most 8 sessions at the same time. Close one, then send again."
            ),
            "recoverable": True,
        }
    finally:
        for driver, *_ in drivers.made:
            driver.release.set()
        results = await asyncio.gather(*starts)
        await manager.close_all()
    assert results == [True] * 8 + [False]


@pytest.mark.parametrize("kind", ["new", "resume", "fork"])
async def test_history_does_not_take_slots_and_explicit_starts_share_released_slot(
    tmp_path: Path, kind: str
) -> None:
    drivers = Drivers()
    manager = gated(tmp_path, drivers)
    actors = [manager.create(sid) for sid in IDS[:8]]
    for actor in actors:
        assert await actor.start()
    history = [manager.open(sid) for sid in IDS[8:20]]
    assert all(not actor.live for actor in history)
    assert len(drivers.made) == 8
    waiting = (
        manager.create(IDS[20])
        if kind == "new"
        else history[0]
        if kind == "resume"
        else manager.fork(actors[0].session_id, IDS[20])
    )
    seen = watch(waiting)
    assert not await waiting.send("synthetic")
    assert seen[-1]["data"]["code"] == "too_many_sessions"
    assert len(drivers.made) == 8
    assert await actors[0].send("already live")
    assert drivers.made[0][0].calls[-1] == "send already live"
    assert len(drivers.made) == 8
    await manager.close(actors[0].session_id)
    assert await waiting.send("explicit retry")
    assert len(drivers.made) == 9 and len(manager.live()) == 8
    assert drivers.made[-1][1] == Launch(
        waiting.session_id,
        resume=kind == "resume",
        fork_of=actors[0].session_id if kind == "fork" else None,
    )
    await manager.close_all()


async def test_failed_eighth_initialization_frees_slot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    drivers = Drivers()
    manager = gated(tmp_path, drivers)
    for sid in IDS[:7]:
        assert await manager.create(sid).start()

    async def failed_open(driver: GatedDriver) -> SessionStarted:
        raise StartError("internal", "synthetic initialization failure")

    with monkeypatch.context() as patch:
        patch.setattr(GatedDriver, "open", failed_open)
        failed = manager.create(IDS[7])
        assert not await failed.start()
    assert not failed.live and len(manager.live()) == 7
    assert len(drivers.made) == 8
    assert await manager.create(IDS[8]).start()
    assert len(drivers.made) == 9 and len(manager.live()) == 8
    await manager.close_all()


@pytest.mark.parametrize("kind", ["new", "history", "live"])
async def test_full_recovery_advertises_eight_and_preserves_replay(
    tmp_path: Path, kind: str
) -> None:
    manager = gated(tmp_path, Drivers())
    actor = manager.open(IDS[0]) if kind == "history" else manager.create(IDS[0])
    if kind == "live":
        assert await actor.start()
    initial = actor.recovery(None, 0)[0]
    assert initial["data"]["state"]["max_live_sessions"] == 8
    if kind != "live":
        assert initial["seq"] == 0 and initial["data"]["events"] == []
    cursor = actor.seq
    event = actor._emit(Status(mode="auto"))
    assert actor.recovery(actor.generation, cursor) == [event]
    assert actor.recovery(actor.generation, actor.seq) == []
    for generation, after_seq in [(None, 0), (IDS[1], 0), (actor.generation, actor.seq + 1)]:
        snapshot = actor.recovery(generation, after_seq)[0]
        assert snapshot["v"] == 2 and snapshot["type"] == "session.snapshot"
        assert snapshot["data"]["state"]["live"] is (kind == "live")
        assert snapshot["data"]["state"]["max_live_sessions"] == 8
    await manager.close_all()
