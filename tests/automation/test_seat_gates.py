"""What holds a Bot run VIKI starts on its own, over the real routine store: a routine run
already going, so a scheduled slot always finds a free slot, and the user's Stop of the seat's
last run."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from tests.automation.test_routines import PROFILE_ID, WORKSPACE_ID
from tests.automation.test_scheduler import (
    NINE,
    FakeClock,
    _create,
    _enable,
    _move,
    _occurrences,
    _pass,
    _scheduler,
)
from tests.automation.test_seat_host import DAY, NOW, SEAT, Rig, _at
from viki_agent.automation.kanban import Card, KanbanRead
from viki_agent.automation.routines import RoutineError, stamp
from viki_agent.automation.seat_host import _bounds
from viki_agent.automation.tasks import Attempt, TaskStore
from viki_agent.decision_loop import seats
from viki_agent.decision_loop.contracts import DecisionSettings
from viki_agent.state import AppState, Prefs
from viki_agent.storage import SessionStore

CARDS = "d" * 64  # a start VIKI made on its own carries its cards' hash; the user's has none
DAY_BOUNDS = (stamp(NINE - timedelta(hours=12)), stamp(NINE + timedelta(hours=12)))


@pytest.fixture
def store(tmp_path: Path) -> SessionStore:
    return AppState(tmp_path / "state.json", Prefs()).store


def _tested(tasks: TaskStore, routine_id: str) -> None:
    with tasks.store._connection(write=True) as connection:
        connection.execute(
            "UPDATE routines SET tested_revision=1 WHERE routine_id=?", (routine_id,)
        )


def _start(tasks: TaskStore, routine_id: str, now: str, cards: str | None = CARDS) -> Attempt:
    return tasks.claim_decision(
        routine_id, 1, newest=None, request_hash=cards, limit=4, day=DAY_BOUNDS, now=now
    )


async def test_a_bot_run_viki_starts_leaves_the_scheduled_slot_free(store: SessionStore) -> None:
    clock = FakeClock(NINE - timedelta(minutes=30))
    scheduler, runner = _scheduler(store, clock)
    seat, other, owners = (_create(scheduler, ["23:00"]) for _ in range(3))
    daily = _create(scheduler, ["09:00"])
    for routine_id in (seat, other, owners, daily):
        _enable(scheduler, routine_id)
    for routine_id in (seat, other, owners):
        _tested(runner.store, routine_id)
    early = stamp(clock.wall())

    running = _start(runner.store, seat, early)
    with pytest.raises(RoutineError, match="^busy$"):
        _start(runner.store, other, early)  # a second Bot run VIKI starts while one is going
    _move(clock, NINE + timedelta(seconds=1))
    await _pass(scheduler)
    assert _occurrences(store, daily) == [(stamp(NINE), "claimed", "", 1)]
    assert sorted(attempt.routine_id for attempt in runner.started) == [daily]
    # The user's own Start keeps the shared cap: refused while both slots are taken, then taken
    # beside another run once one ends, which VIKI's own start never is.
    with pytest.raises(RoutineError, match="^busy$"):
        _start(runner.store, owners, stamp(clock.wall()), cards=None)
    assert runner.store.finish(running.attempt_id, "succeeded", "", stamp(clock.wall()))
    assert _start(runner.store, owners, stamp(clock.wall()), cards=None).kind == "decision"


# ---- the user's Stop -------------------------------------------------------------------------


def _stopped_seat(rig: Rig, reason: str) -> tuple[str, Attempt]:
    """A tested seat routine whose run VIKI started at minute 0 ended at minute 10 as
    (`cancelled`, `reason`), the way a run ends after its turn was cut off."""
    routine_id = rig.routine(run_only_if=SEAT)
    rig.update(
        "UPDATE routines SET state='enabled',tested_revision=1,enabled_since=? WHERE routine_id=?",
        _at(-60),
        routine_id,
    )
    run = rig.tasks.claim_decision(
        routine_id, 1, newest=None, request_hash=CARDS, limit=4, day=_bounds(DAY), now=_at(0)
    )
    assert rig.tasks.mark_dispatching(run.attempt_id, PROFILE_ID, 1)
    assert rig.tasks.mark_running(run.attempt_id, _at(0))
    assert rig.tasks.finish(run.attempt_id, "cancelled", reason, _at(10))
    return routine_id, run


def _card(ref: str, status: str, minute: int) -> Card:
    return Card(ref, ref, status, ("owner/research",), NOW + timedelta(minutes=minute))


@pytest.mark.parametrize(
    ("reason", "starts"), [("stopped", False), ("app_closed", True), ("session_closed", True)]
)
def test_the_owners_stop_holds_vikis_own_start_of_that_seat(
    tmp_path: Path, reason: str, starts: bool
) -> None:
    """The run set its card working, then the user pressed Stop (or Stop all): the next plan
    over the real store's facts starts nothing, while Ask first still offers Start and a card the
    user changes after the stop starts the seat again. A run an app close cut off was not
    stopped by the user, so what it moved still counts."""
    rig = Rig(tmp_path)
    routine_id, run = _stopped_seat(rig, reason)
    moved, waiting = _card("kanban/a.md", "working", 5), _card("kanban/b.md", "todo", -30)

    def step(*cards: Card, grant: str = "allowed") -> seats.SeatPlan:
        kanban = KanbanRead(cards, (), True, NOW + timedelta(hours=1))
        facts = rig.host.facts(WORKSPACE_ID, seats.cards_since(kanban), DAY)
        assert facts.complete and facts.newest[routine_id].reason == reason
        settings = replace(DecisionSettings(), grant_team=grant)
        return seats.plan(kanban, facts, (), settings, policy="ordinary", hold=None, quiet=False)

    allowed = step(moved, waiting)
    assert [start.newest for start in allowed.starts] == ([run.attempt_id] if starts else [])
    assert allowed.offers == ()
    assert [report.reason for report in allowed.reports] == ["stopped"]
    assert [offer.routine_id for offer in step(moved, waiting, grant="ask").offers] == [routine_id]
    changed = replace(waiting, changed=NOW + timedelta(minutes=20))
    assert [start.routine_id for start in step(moved, changed).starts] == [routine_id]
