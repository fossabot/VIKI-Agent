"""The service's Stop all, the launcher's `started` and the user's mission start under the vault's
live policy, over `test_service`'s rig: Stop all tells the engine before it stops anything and
runs every stop even when one fails, answering the first failure; the launcher keeps each source's
share of its chats; and the user's start needs a vault whose policy allows the work."""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest

from tests.decision_loop.fakes import FakeCheckHost, FakeSeatHost
from tests.decision_loop.test_service import (
    HANG_GUARD_S,
    Rig,
    check_host,
    decided,
    seat_host,
)
from viki_agent.decision_loop import service, wire
from viki_agent.decision_loop.contracts import DecisionLoopError, SeatTeam
from viki_agent.storage import StateError


@contextlib.asynccontextmanager
async def stopping(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[_Stops]:
    """A rig with a seat port and a check port whose engine and three stops note their order in
    `order`; a part named in `failing` raises its error instead of stopping."""
    stops = _Stops(Rig(tmp_path, seats=seat_host(), checks=check_host()), monkeypatch)
    try:
        yield stops
    finally:
        await asyncio.wait_for(stops.rig.engine.stop(), HANG_GUARD_S)


class _Stops:
    def __init__(self, rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
        self.rig = rig
        self.order: list[str] = []
        self.failing: dict[str, Exception] = {}
        # A fake of the engine's side of Stop all (`note_stop_all`): this file tests the call.
        monkeypatch.setattr(
            rig.engine, "note_stop_all", lambda: self.order.append("engine"), raising=False
        )
        seats, checks = rig.service._seats, rig.service._checks
        assert isinstance(seats, FakeSeatHost) and isinstance(checks, FakeCheckHost)
        for name, part in (("missions", rig.missions), ("seats", seats), ("checks", checks)):
            monkeypatch.setattr(part, "stop_all", self._noted(name, part.stop_all))

    def _noted(self, name: str, stop_all: Callable[[], Any]) -> Callable[[], Any]:
        async def noted() -> Any:
            self.order.append(name)
            if name in self.failing:
                raise self.failing[name]
            return await stop_all()

        return noted


async def test_stop_all_tells_the_engine_before_it_stops_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with stopping(tmp_path, monkeypatch) as stops:
        block = await stops.rig.service.stop_missions()
        assert stops.order == ["engine", "missions", "seats", "checks"]
        assert block["running"] == [] and stops.rig.engine.wakes == ["mission"]


FAILURES: dict[str, Exception] = {
    "missions": DecisionLoopError("unavailable"),
    "seats": StateError("busy"),  # `SeatHost.stop_all` lets the store's own error out
    "checks": DecisionLoopError("unavailable"),
}


@pytest.mark.parametrize("part", FAILURES)
async def test_a_failing_stop_leaves_no_other_part_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, part: str
) -> None:
    async with stopping(tmp_path, monkeypatch) as stops:
        stops.failing[part] = FAILURES[part]
        with pytest.raises(type(FAILURES[part])) as failed:
            await stops.rig.service.stop_missions()
        assert failed.value is FAILURES[part]  # today's error, so the user sees it
        assert stops.order == ["engine", "missions", "seats", "checks"]
        assert stops.rig.engine.wakes == ["mission"]  # the stops that held count


async def test_the_first_failure_is_the_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with stopping(tmp_path, monkeypatch) as stops:
        first, later = DecisionLoopError("unavailable"), DecisionLoopError("busy")
        stops.failing.update(missions=first, checks=later)
        with pytest.raises(DecisionLoopError) as failed:
            await stops.rig.service.stop_missions()
        assert failed.value is first
        assert stops.order == ["engine", "missions", "seats", "checks"]


# ---- the launcher's `started` ------------------------------------------------------------------


def ids(count: int) -> list[str]:
    return [str(uuid.uuid4()) for _ in range(count)]


@pytest.mark.parametrize(
    ("missions", "sessions", "kept"),
    [
        (60, 60, (25, 25)),  # a busy month of both: half each
        (60, 1, (49, 1)),  # one source's unused share goes to the other
        (1, 60, (1, 49)),
        (3, 2, (3, 2)),
    ],
)
async def test_the_launcher_keeps_each_sources_share_of_started(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    missions: int,
    sessions: int,
    kept: tuple[int, int],
) -> None:
    chats, runs = ids(missions), ids(sessions)
    # The store answers at most `MAX_STARTED` chats, newest first, as the seat port does.
    monkeypatch.setattr(service.MissionService, "started_since", lambda _self, _since: chats[:50])
    host = seat_host(team=SeatTeam(0, 0, tuple(runs[:50])))
    rig = Rig(tmp_path, seats=host)
    try:
        started = (await rig.service.launcher())["started"]
    finally:
        await asyncio.wait_for(rig.engine.stop(), HANG_GUARD_S)
    assert len(started) <= wire.MAX_STARTED
    assert started == [*chats[: kept[0]], *runs[: kept[1]]]


# ---- the user's mission start under the vault's live policy -----------------------------------

POLICIES = {
    # policy, kind: the works the user may start
    "ordinary vault": ("ordinary", "vault", ("prepare", "run")),
    "protected vault": ("protected", "vault", ("prepare",)),
    "read-only vault": ("existing_readonly", "vault", ()),
    "unreadable policy": (None, "vault", ()),
    "code folder": ("ordinary", "code", ()),
}


@pytest.mark.parametrize("case", POLICIES)
@pytest.mark.parametrize("work", ["prepare", "run"])
async def test_the_owners_mission_start_reads_the_vault_policy_live(
    tmp_path: Path, case: str, work: str
) -> None:
    policy, kind, allowed = POLICIES[case]
    rig = Rig(tmp_path, kind=kind)
    try:
        listed, _held = await decided(rig)  # raised while the policy was ordinary
        rig.values["decision.grant.prepare"] = rig.values["decision.grant.run"] = "ask"
        rig.policy = policy  # then changed, before the user's start
        rig.engine.wakes.clear()
        if work in allowed:
            mission = await rig.service.start_mission(listed.item_key, work)
            assert (mission["work"], mission["state"]) == (work, "running")
            assert rig.engine.wakes == ["mission"]
        else:
            with pytest.raises(DecisionLoopError) as refused:
                await rig.service.start_mission(listed.item_key, work)
            assert refused.value.code == "not_applicable"
            assert rig.host.calls == [] and rig.engine.wakes == []
    finally:
        await asyncio.wait_for(rig.engine.stop(), HANG_GUARD_S)
