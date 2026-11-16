"""Tests for `SeatHost`: what Decision reads of a vault's routines, runs and counts, over the
routine store with a stand-in runner and scheduler."""

from __future__ import annotations

import logging
import os
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, cast

import pytest

from tests.automation.test_routines import PROFILE_ID, WORKSPACE_ID, _document_raw
from viki_agent import protection
from viki_agent.automation import kanban, seat_host
from viki_agent.automation.routines import RoutineError, RoutineService, stamp
from viki_agent.automation.scheduler import Scheduler
from viki_agent.automation.seat_host import MAX_SESSIONS, MAX_WINDOWS, SeatHost
from viki_agent.automation.tasks import Attempt, TaskRunner, TaskStore, Waiting
from viki_agent.decision_loop import contracts, seats
from viki_agent.decision_loop.contracts import DecisionLoopError, SeatWaiting
from viki_agent.profiles.service import ProfileService
from viki_agent.session import SessionManager
from viki_agent.state import AppState, Prefs
from viki_agent.storage import StateError

NOW = datetime(2026, 9, 27, 9, 0, tzinfo=UTC)
DAY = NOW.astimezone().date().isoformat()  # the local day NOW falls on
SCOPE = "ab" * 32
OWNER = "44444444-4444-4444-8444-4444444444ab"
OTHER_VAULT = "33333333-3333-4333-8333-3333333333ab"
MISSION_SCOPE = "55555555-5555-4555-8555-5555555555ab"
SEAT = {"kind": "card", "seat": "research"}


def _ident() -> str:
    return str(uuid.uuid4())


def _at(minutes: int) -> str:
    return stamp(NOW + timedelta(minutes=minutes))


class _Runner:
    """The things `SeatHost` reads of, and asks of, a `TaskRunner`."""

    def __init__(self, store: TaskStore) -> None:
        self.store = store
        self.cards: tuple[Waiting, ...] = ()
        self.claims: list[dict[str, Any]] = []
        self.refusal: BaseException | None = None

    async def run_decision(self, routine_id: str, revision: int, **claim: Any) -> Attempt:
        self.claims.append({"routine_id": routine_id, "revision": revision, **claim})
        if self.refusal is not None:
            raise self.refusal
        return Attempt(_ident(), _ident(), routine_id, revision, "decision", "session-new")

    def waiting(self) -> tuple[Waiting, ...]:
        return self.cards


class _Scheduler:
    def __init__(self) -> None:
        self.paused = False
        self.state = "running"
        self.wakes = 0

    def _paused(self) -> bool:
        return self.paused

    def wake(self) -> None:
        self.wakes += 1


class _Session:
    def __init__(self, *, busy: bool = True, fails: bool = False) -> None:
        self.busy = busy
        self.fails = fails
        self.interrupts = 0

    async def interrupt(self) -> None:
        if self.fails:
            raise RuntimeError("prompt text must not be logged")
        self.interrupts += 1


class _Manager:
    def __init__(self) -> None:
        self.sessions: dict[str, _Session] = {}

    def get(self, session_id: str) -> _Session | None:
        return self.sessions.get(session_id)


class Rig:
    def __init__(self, tmp_path: Path) -> None:
        store = AppState(tmp_path / "state.json", Prefs()).store
        self.service = RoutineService(store, ProfileService(store), None, None, now=lambda: NOW)
        self.tasks = TaskStore(store, SCOPE, OWNER)
        self.runner = _Runner(self.tasks)
        self.scheduler = _Scheduler()
        self.manager = _Manager()
        self.host = SeatHost(
            cast(TaskRunner, self.runner),
            cast(Scheduler, self.scheduler),
            cast(SessionManager, self.manager),
        )

    def routine(self, workspace_id: str = WORKSPACE_ID, **document: Any) -> str:
        raw = _document_raw(workspace_id=workspace_id, profile_id=PROFILE_ID, **document)
        return self.service.create(_ident(), raw)[0]["routine_id"]

    def update(self, sql: str, *values: Any) -> None:
        with self.tasks.store._connection(write=True) as connection:
            connection.execute(sql, values)

    def attempt(
        self,
        routine_id: str,
        minute: int,
        *,
        kind: Literal["schedule", "manual", "decision"] = "decision",
        end: int | None = None,
        dispatched: bool = True,
    ) -> Attempt:
        """A claimed attempt at `minute`, finished at `end` (open when None)."""
        with self.tasks.store._connection(write=True) as connection:
            claim = self.tasks.insert_claim(
                connection,
                routine_id=routine_id,
                kind=kind,
                revision=1,
                now=_at(minute),
                scheduled_for=_at(minute) if kind == "schedule" else None,
                trigger_id=_ident() if kind == "manual" else None,
                request_hash=None if kind == "schedule" else "d" * 64,
            )
        if dispatched:
            assert self.tasks.mark_dispatching(claim.attempt_id, PROFILE_ID, 1)
            assert self.tasks.mark_running(claim.attempt_id, _at(minute))
        if end is not None:
            assert self.tasks.finish(claim.attempt_id, "succeeded", "", _at(end))
        return claim


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    return Rig(tmp_path)


def test_the_caps_are_the_reviewed_ones() -> None:
    assert (MAX_WINDOWS, MAX_SESSIONS) == (5000, 50)


def test_facts_name_the_vault_routines_with_their_seats(rig: Rig) -> None:
    plain = rig.routine()
    seated = rig.routine(run_only_if=SEAT, on_confirmation="stop")
    rig.routine(OTHER_VAULT, run_only_if=SEAT)
    archived = rig.routine(run_only_if=SEAT)
    rig.update("UPDATE routines SET state='archived' WHERE routine_id=?", archived)
    rig.update("UPDATE routines SET tested_revision=1 WHERE routine_id=?", seated)
    facts = rig.host.facts(WORKSPACE_ID, None, DAY)
    # Both were created at one instant, so their order is by id.
    assert sorted(
        (r.routine_id, r.state, r.revision, r.tested_revision, r.seat, r.stop_at_draft)
        for r in facts.routines
    ) == sorted(
        [
            (plain, "draft", 1, None, None, False),
            (seated, "draft", 1, 1, "research", True),
        ]
    )
    assert facts.complete


def test_facts_skip_an_unreadable_routine_and_mark_incomplete(rig: Rig) -> None:
    good, broken = rig.routine(), rig.routine()
    rig.update(
        "UPDATE routine_revisions SET body=? WHERE routine_id=?", b"not a sealed body", broken
    )
    facts = rig.host.facts(WORKSPACE_ID, None, DAY)
    assert [r.routine_id for r in facts.routines] == [good]
    assert not facts.complete


def test_facts_read_no_runs_without_a_window(rig: Rig) -> None:
    routine = rig.routine()
    claim = rig.attempt(routine, 0, end=1)
    facts = rig.host.facts(WORKSPACE_ID, None, DAY)
    assert facts.runs == ()
    # The newest run does not depend on the window.
    assert facts.newest[routine].attempt_id == claim.attempt_id
    assert facts.complete


def test_facts_list_every_routines_open_or_late_runs_newest_first(rig: Rig) -> None:
    mine, other = rig.routine(), rig.routine(OTHER_VAULT)
    rig.attempt(mine, 0, end=5)  # ended before the window
    ended = rig.attempt(mine, 10, kind="schedule", end=20)
    elsewhere = rig.attempt(other, 15, kind="manual")
    unsent = rig.attempt(mine, 30, dispatched=False)
    facts = rig.host.facts(WORKSPACE_ID, NOW + timedelta(minutes=12), DAY)
    assert [run.attempt_id for run in facts.runs] == [
        unsent.attempt_id,
        elsewhere.attempt_id,
        ended.attempt_id,
    ]
    first, second, third = facts.runs
    assert (first.routine_id, first.kind, first.dispatched, first.finished, first.outcome) == (
        mine,
        "decision",
        False,
        None,
        None,
    )
    assert (second.routine_id, second.kind, second.finished) == (other, "manual", None)
    assert (third.kind, third.outcome, third.reason, third.session_id) == (
        "schedule",
        "succeeded",
        "",
        ended.session_id,
    )
    # Every time is aware UTC, as the Kanban's card times are.
    assert third.started == NOW + timedelta(minutes=10)
    assert third.finished == NOW + timedelta(minutes=20)
    assert {run.started.utcoffset() for run in facts.runs} == {timedelta(0)}
    assert facts.complete


def test_facts_name_the_newest_run_of_each_vault_routine_as_the_claim_does(rig: Rig) -> None:
    ran, idle = rig.routine(), rig.routine()
    rig.routine(OTHER_VAULT)
    rig.attempt(ran, 0, end=1)
    latest = rig.attempt(ran, 2, kind="manual", end=3)
    facts = rig.host.facts(WORKSPACE_ID, None, DAY)
    assert set(facts.newest) == {ran}
    assert idle not in facts.newest
    with rig.tasks.store._connection() as connection:
        assert rig.tasks.newest(connection, ran) == latest.attempt_id
    assert facts.newest[ran].attempt_id == latest.attempt_id


def test_facts_past_the_cap_stand_one_window_for_the_runs_left_out(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(seat_host, "MAX_WINDOWS", 3)
    routine = rig.routine()
    claims = [rig.attempt(routine, minute, end=minute + 1) for minute in range(4)]
    since = NOW - timedelta(minutes=1)
    facts = rig.host.facts(WORKSPACE_ID, since, DAY)
    # The three that finished last, then one window up to the finish of the one left out.
    *kept, unread = facts.runs
    assert [run.attempt_id for run in kept] == [c.attempt_id for c in reversed(claims[1:])]
    assert (unread.kind, unread.started, unread.finished) == (
        "unread",
        since,
        NOW + timedelta(minutes=1),
    )
    assert facts.complete
    assert not seats.owners(_card(0), facts)  # the run left out may have written it
    assert seats.owners(_card(5), facts)
    rig.update("DELETE FROM task_attempts WHERE attempt_id=?", claims[0].attempt_id)
    assert [run.kind for run in rig.host.facts(WORKSPACE_ID, since, DAY).runs] == ["decision"] * 3


def test_facts_fail_closed_when_an_open_run_is_left_out(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(seat_host, "MAX_WINDOWS", 1)
    rig.attempt(rig.routine(), 0)
    rig.attempt(rig.routine(), 1)
    facts = rig.host.facts(WORKSPACE_ID, NOW - timedelta(minutes=1), DAY)
    # An open run has no end, so no window can stand for it.
    assert len(facts.runs) == 1 and not facts.complete


def test_facts_fail_closed_on_a_bad_stamp_the_cap_would_leave_out(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(seat_host, "MAX_WINDOWS", 2)
    oldest = _mission(rig, 0, 1)
    _mission(rig, 2, 3)
    _mission(rig, 4, 5)
    since = NOW - timedelta(minutes=1)
    assert rig.host.facts(WORKSPACE_ID, since, DAY).complete
    rig.update(
        "UPDATE decision_missions SET created_at='x' || substr(created_at,2) WHERE mission_id=?",
        oldest,
    )
    # By its finish it would be the one left out; a misshapen stamp is read first instead.
    assert not rig.host.facts(WORKSPACE_ID, since, DAY).complete


def test_facts_keep_the_owners_new_card_past_thousands_of_windows(rig: Rig) -> None:
    """More than `MAX_WINDOWS` runs since a stale todo card still leave a later card the user's
    and start its seat, with the real cap and one read."""
    routine = rig.routine(run_only_if=SEAT)
    rig.update(
        "UPDATE routines SET state='enabled',tested_revision=1,enabled_since=? WHERE routine_id=?",
        _at(-1),
        routine,
    )
    start = NOW - timedelta(days=30)
    rows = [
        (
            _ident(),
            MISSION_SCOPE,
            _ident(),
            "c" * 64,
            stamp(start + timedelta(minutes=2 * n)),
            stamp(start + timedelta(minutes=2 * n + 1)),
        )
        for n in range(MAX_WINDOWS + 1)
    ]
    with rig.tasks.store._connection(write=True) as connection:
        connection.executemany(
            "INSERT INTO decision_missions (mission_id,scope,session_id,item_key,work,state,"
            "reason,body,created_at,updated_at,deadline_at,finished_at) "
            "VALUES (?1,?2,?3,?4,'prepare','done','',x'7878',?5,?5,?5,?6)",
            rows,
        )
    stale = kanban.Card(
        "kanban/someday.md", "someday", "todo", ("owner/archive",), start - timedelta(days=90)
    )
    fresh = kanban.Card(
        "kanban/deploy.md", "deploy", "todo", ("owner/research",), NOW + timedelta(minutes=5)
    )
    read = kanban.KanbanRead((stale, fresh), (), True)
    facts = rig.host.facts(WORKSPACE_ID, seats.cards_since(read), DAY)
    assert facts.complete and len(facts.runs) == MAX_WINDOWS + 1
    assert facts.runs[-1].finished == start + timedelta(minutes=1)  # the one left out
    assert not seats.owners(stale, facts) and seats.owners(fresh, facts)
    allowed = replace(contracts.DecisionSettings(), grant_team="allowed")
    planned = seats.plan(read, facts, (), allowed, policy="ordinary", hold=None, quiet=False)
    assert [planned_start.routine_id for planned_start in planned.starts] == [routine]


def _mission(rig: Rig, minute: int, end: int | None, scope: str = MISSION_SCOPE) -> str:
    """A Decision mission chat created at `minute`, finished at `end` (open when None)."""
    mission_id = _ident()
    rig.update(
        "INSERT INTO decision_missions (mission_id,scope,session_id,item_key,work,state,reason,"
        "body,created_at,updated_at,deadline_at,finished_at) "
        "VALUES (?,?,?,?,'prepare',?,'',?,?,?,?,?)",
        mission_id,
        scope,
        _ident(),
        "c" * 64,
        "running" if end is None else "done",
        b"xx",
        _at(minute),
        _at(minute),
        _at(minute),
        None if end is None else _at(end),
    )
    return mission_id


def _card(minute: int) -> kanban.Card:
    return kanban.Card(
        "kanban/x.md", "x", "todo", ("owner/research",), NOW + timedelta(minutes=minute)
    )


def test_a_card_a_mission_chat_wrote_is_not_the_owners(rig: Rig) -> None:
    open_id = _mission(rig, 10, None)
    done_id = _mission(rig, 20, 30, scope=OTHER_VAULT)
    _mission(rig, 0, 5)  # ended before the window
    facts = rig.host.facts(WORKSPACE_ID, NOW + timedelta(minutes=8), DAY)
    assert {run.attempt_id for run in facts.runs} == {open_id, done_id}
    run = next(run for run in facts.runs if run.attempt_id == done_id)
    assert (run.routine_id, run.kind, run.dispatched, run.outcome, run.reason) == (
        "",
        "mission",
        True,
        None,
        "",
    )
    assert (run.started, run.finished) == (NOW + timedelta(minutes=20), NOW + timedelta(minutes=30))
    assert facts.complete
    assert not seats.owners(_card(12), facts)  # inside the open mission
    assert not seats.owners(_card(25), facts)  # inside the finished mission
    assert seats.owners(_card(7), facts)  # outside every window


def test_facts_count_mission_rows_against_the_cap(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(seat_host, "MAX_WINDOWS", 2)
    routine = rig.routine()
    rig.attempt(routine, 0, end=1)
    _mission(rig, 2, 3)
    since = NOW - timedelta(minutes=1)
    facts = rig.host.facts(WORKSPACE_ID, since, DAY)
    assert [run.kind for run in facts.runs] == ["mission", "decision"] and facts.complete
    _mission(rig, 4, 5)
    facts = rig.host.facts(WORKSPACE_ID, since, DAY)
    # One read, in finish order across both tables: the run is the one left out.
    assert [run.kind for run in facts.runs] == ["mission", "mission", "unread"]
    assert facts.runs[-1].finished == NOW + timedelta(minutes=1) and facts.complete


def test_facts_mark_incomplete_a_mission_with_a_bad_stamp(rig: Rig) -> None:
    _mission(rig, 0, 1)
    rig.update("UPDATE decision_missions SET created_at='x' || substr(created_at,2)")
    facts = rig.host.facts(WORKSPACE_ID, NOW - timedelta(minutes=1), DAY)
    assert facts.runs == () and not facts.complete


def test_facts_mark_incomplete_a_run_with_a_bad_stamp(rig: Rig) -> None:
    routine = rig.routine()
    rig.attempt(routine, 0, end=1)
    rig.update("UPDATE task_attempts SET created_at='x' || substr(created_at,2)")
    facts = rig.host.facts(WORKSPACE_ID, NOW - timedelta(minutes=1), DAY)
    assert facts.runs == () and facts.newest == {}
    assert not facts.complete


def test_free_is_one_slot_only_while_no_routine_run_is_going(rig: Rig) -> None:
    """VIKI's own start takes a slot only while no routine run is going, as its claim checks,
    so a scheduled slot always finds the other one."""
    routine = rig.routine()
    assert rig.host.facts(WORKSPACE_ID, None, DAY).free == 1
    claim = rig.attempt(routine, 0)
    assert rig.host.facts(WORKSPACE_ID, None, DAY).free == 0
    assert rig.tasks.finish(claim.attempt_id, "succeeded", "", _at(1))
    assert rig.host.facts(WORKSPACE_ID, None, DAY).free == 1
    rig.attempt(rig.routine(OTHER_VAULT), 1, kind="manual")  # any routine's, any vault's
    assert rig.host.facts(WORKSPACE_ID, None, DAY).free == 0


def test_today_counts_the_local_days_sent_or_open_decision_attempts(rig: Rig) -> None:
    routine = rig.routine()
    rig.attempt(routine, 0, end=1)
    rig.attempt(routine, 2, end=3, dispatched=False)  # never sent
    rig.attempt(routine, 4, kind="manual", end=5)
    assert rig.host.facts(WORKSPACE_ID, None, DAY).today == 1
    assert rig.host.team(DAY, None).today == 1
    yesterday = (NOW.astimezone().date() - timedelta(days=1)).isoformat()
    assert rig.host.facts(WORKSPACE_ID, None, yesterday).today == 0


@pytest.mark.parametrize("day", ["", "2026-9-27", "2026-09-27T00:00", "tomorrow", "0000-00-00"])
def test_a_bad_day_is_refused(rig: Rig, day: str) -> None:
    with pytest.raises(DecisionLoopError) as err:
        rig.host.facts(WORKSPACE_ID, None, day)
    assert err.value.code == "invalid_request"
    with pytest.raises(DecisionLoopError):
        rig.host.team(day, None)


def test_facts_report_pause_all_from_the_scheduler(rig: Rig) -> None:
    assert not rig.host.facts(WORKSPACE_ID, None, DAY).paused
    rig.scheduler.paused = True
    assert rig.host.facts(WORKSPACE_ID, None, DAY).paused


def test_team_counts_open_decision_runs_and_lists_their_sessions(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(seat_host, "MAX_SESSIONS", 2)
    routine = rig.routine()
    rig.attempt(routine, 0, end=1)  # before the window
    done = rig.attempt(routine, 10, end=11)
    rig.attempt(routine, 15, kind="schedule", end=16)  # not a decision run
    open_one = rig.attempt(routine, 20)
    open_two = rig.attempt(rig.routine(OTHER_VAULT), 30, dispatched=False)
    rig.attempt(rig.routine(), 40, kind="manual")  # open, but not a decision run
    since = NOW + timedelta(minutes=5)
    team = rig.host.team(DAY, since)
    assert team.running == 2
    assert team.today == 4
    # Newest first and cut at the cap.
    assert team.sessions == (open_two.session_id, open_one.session_id)
    monkeypatch.undo()
    assert rig.host.team(DAY, since).sessions == (
        open_two.session_id,
        open_one.session_id,
        done.session_id,
    )
    assert rig.host.team(DAY, None).sessions == ()


def test_waiting_maps_the_runners_open_cards(rig: Rig) -> None:
    assert rig.host.waiting() == ()
    asked = Attempt(_ident(), _ident(), _ident(), 1, "decision", _ident())
    mixed = Attempt(_ident(), _ident(), _ident(), 1, "schedule", _ident())
    rig.runner.cards = (Waiting(asked, True), Waiting(mixed, False))
    assert rig.host.waiting() == (
        SeatWaiting(asked.routine_id, asked.attempt_id, asked.session_id, True),
        SeatWaiting(mixed.routine_id, mixed.attempt_id, mixed.session_id, False),
    )


@pytest.mark.skipif(os.name == "nt", reason="a POSIX mode bit makes the store not private")
def test_reads_refuse_a_store_that_is_not_private(rig: Rig) -> None:
    rig.routine()
    os.chmod(rig.tasks.store.path, 0o666)
    with pytest.raises(protection.ProtectionError):
        rig.host.facts(WORKSPACE_ID, None, DAY)
    with pytest.raises(protection.ProtectionError):
        rig.host.team(DAY, None)


def test_the_host_satisfies_the_seat_port(rig: Rig) -> None:
    port: contracts.SeatHost = rig.host
    assert port is rig.host


async def _start(rig: Rig, **overrides: Any) -> str:
    kwargs: dict[str, Any] = {"newest": "n", "cards": "c" * 64, "limit": 4, "day": DAY}
    return await rig.host.start("routine-1", 3, **{**kwargs, **overrides})


async def test_start_claims_through_the_runner_and_maps_refusals(rig: Rig) -> None:
    assert await _start(rig) == "session-new"
    (claim,) = rig.runner.claims
    ready = claim.pop("ready")
    assert claim == {
        "routine_id": "routine-1",
        "revision": 3,
        "newest": "n",
        "request_hash": "c" * 64,
        "limit": 4,
        "day": seat_host._bounds(DAY),
    }
    assert rig.scheduler.wakes == 1
    # The runner reads Pause all and a running scheduler under its lock, just before the claim.
    assert ready()
    rig.scheduler.paused = True
    assert not ready()
    rig.scheduler.paused = False
    rig.scheduler.state = "stopped"
    assert not ready()
    assert await _start(rig, cards=None, newest=None) == "session-new"
    assert rig.runner.claims[-1]["request_hash"] is None

    expected = {
        "limit": "limit",
        "run_active": "busy",
        "busy": "busy",
        "unavailable": "unavailable",
        "not_found": "not_applicable",
        "invalid_transition": "not_applicable",
        "conflict": "not_applicable",
        "not_ready": "not_applicable",
    }
    wakes = rig.scheduler.wakes
    for refusal, code in expected.items():
        rig.runner.refusal = RoutineError(refusal)
        with pytest.raises(DecisionLoopError) as err:
            await _start(rig)
        assert err.value.code == code, refusal
    for failure in (StateError("incompatible"), protection.ProtectionError()):
        rig.runner.refusal = failure
        with pytest.raises(DecisionLoopError) as err:
            await _start(rig)
        assert err.value.code == "unavailable"
    # A refused claim wakes nobody.
    assert rig.scheduler.wakes == wakes


async def test_start_refuses_a_bad_day_before_the_claim(rig: Rig) -> None:
    with pytest.raises(DecisionLoopError) as err:
        await _start(rig, day="tomorrow")
    assert err.value.code == "invalid_request"
    assert rig.runner.claims == []


def _sessions(rig: Rig, routine: str, *, open_run: bool = True) -> _Session:
    claim = rig.attempt(routine, 0, end=None if open_run else 1)
    session = rig.manager.sessions[claim.session_id] = _Session()
    return session


async def test_stop_interrupts_the_open_decision_run(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    mine, other, idle = rig.routine(), rig.routine(), rig.routine()
    running = _sessions(rig, mine)
    elsewhere = _sessions(rig, other)
    finished = _sessions(rig, idle, open_run=False)
    assert await rig.host.stop(mine)
    assert (running.interrupts, elsewhere.interrupts, finished.interrupts) == (1, 0, 0)
    # A finished run, a run with no chat and a chat between turns are not interrupted.
    assert not await rig.host.stop(idle)
    assert not await rig.host.stop(rig.routine())
    running.busy = False
    assert not await rig.host.stop(mine)
    assert running.interrupts == 1
    # A manual run of the routine is not a decision run.
    manual = rig.attempt(rig.routine(), 5, kind="manual")
    rig.manager.sessions[manual.session_id] = _Session()
    assert not await rig.host.stop(manual.routine_id)
    # An interrupt that raises answers False and logs a fixed line only.
    running.busy, running.fails = True, True
    with caplog.at_level(logging.WARNING):
        assert not await rig.host.stop(mine)
    assert [r.getMessage() for r in caplog.records] == ["seat run interrupt failed"]
    assert "prompt text" not in caplog.text


async def test_stop_all_interrupts_every_open_decision_run(rig: Rig) -> None:
    first, second = _sessions(rig, rig.routine()), _sessions(rig, rig.routine(OTHER_VAULT))
    broken = _sessions(rig, rig.routine())
    broken.fails = True
    quiet = _sessions(rig, rig.routine())
    quiet.busy = False
    _sessions(rig, rig.routine(), open_run=False)
    manual = _Session()
    rig.manager.sessions[rig.attempt(rig.routine(), 5, kind="manual").session_id] = manual
    rig.attempt(rig.routine(), 6)  # an open run whose chat is gone
    assert await rig.host.stop_all() == 2
    assert (first.interrupts, second.interrupts, broken.interrupts, quiet.interrupts) == (
        1,
        1,
        0,
        0,
    )
    assert manual.interrupts == 0
    assert await rig.host.stop_all() == 2
