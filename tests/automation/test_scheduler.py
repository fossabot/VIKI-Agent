from __future__ import annotations

import asyncio
import contextlib
import functools
import os
import sys
import threading
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

import pytest

from tests.automation.test_brief import service as vault_service
from tests.automation.test_routines import WORKSPACE_ID, _daily_schedule, _document_raw, _ident
from viki_agent.app.settings import SettingsService
from viki_agent.automation import scheduler as scheduler_module
from viki_agent.automation.folder_changes import notes_changed
from viki_agent.automation.routines import (
    RevisionPlan,
    RoutinePlan,
    RoutineService,
    parse_schedule,
    parse_stamp,
    record_tested,
    stamp,
)
from viki_agent.automation.scheduler import (
    MAX_TICK_S,
    ON_TIME,
    SCAN_DAYS,
    Scheduler,
    SystemClock,
    next_five,
    slots,
)
from viki_agent.automation.tasks import MAX_OCCURRENCES, Attempt, Outcome, TaskStore
from viki_agent.profiles.service import ProfileService
from viki_agent.state import AppState, Prefs
from viki_agent.storage import SessionStore, StateError

EARLY = stamp(datetime(2020, 1, 1, tzinfo=UTC))


class FakeClock:
    """The scheduler's injected clock: settable wall and monotonic time.

    ``wait`` records the requested duration and returns as soon as either the passed-in
    event is set or the test calls ``tick()``; it never sleeps and never advances time
    on its own.
    """

    def __init__(self, wall: datetime, monotonic: float = 1000.0) -> None:
        self._wall = wall
        self._monotonic = monotonic
        self.waits: list[float] = []
        self._ticked = asyncio.Event()

    def wall(self) -> datetime:
        return self._wall

    def monotonic(self) -> float:
        return self._monotonic

    def advance(self, seconds: float, *, wall: bool = True, monotonic: bool = True) -> None:
        if wall:
            self._wall += timedelta(seconds=seconds)
        if monotonic:
            self._monotonic += seconds

    def tick(self) -> None:
        self._ticked.set()

    async def wait(self, event: asyncio.Event, seconds: float) -> None:
        self.waits.append(seconds)
        tick_task = asyncio.ensure_future(self._ticked.wait())
        event_task = asyncio.ensure_future(event.wait())
        try:
            await asyncio.wait({tick_task, event_task}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (tick_task, event_task):
                if not task.done():
                    task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.gather(tick_task, event_task)
            self._ticked.clear()


def _schedule(
    kind: str,
    times: list[str],
    *,
    days: list[int] | None = None,
    zone: str | None = None,
    start: str | None = None,
    end: str | None = None,
) -> dict[str, Any]:
    return parse_schedule(
        {
            "kind": kind,
            "times": times,
            "days": days or [],
            "timezone": zone,
            "start_date": start,
            "end_date": end,
            "catch_up": "skip",
        }
    )


def test_next_five_tokyo_daily() -> None:
    schedule = _schedule("daily", ["09:00"], zone="Asia/Tokyo")
    plan = RevisionPlan(1, EARLY, schedule)
    now = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
    items = next_five([plan], now)
    assert items[0] == {
        "at": "2026-09-28T00:00:00.000Z",
        "local": "2026-09-28T09:00+09:00",
        "zone": "Asia/Tokyo",
        "offset_minutes": 540,
        "note": "",
    }


def test_slots_new_york_spring_forward_gap() -> None:
    schedule = _schedule("daily", ["02:30"], zone="America/New_York")
    plan = RevisionPlan(1, EARLY, schedule)
    lo = datetime(2026, 3, 7, tzinfo=UTC)
    hi = datetime(2026, 3, 9, 12, 0, tzinfo=UTC)
    result = slots([plan], lo, hi)
    assert [stamp(instant) for instant, _rev in result] == [
        "2026-03-07T07:30:00.000Z",
        "2026-03-09T06:30:00.000Z",
    ]


def test_next_five_new_york_spring_forward_includes_gap() -> None:
    schedule = _schedule("daily", ["02:30"], zone="America/New_York")
    plan = RevisionPlan(1, EARLY, schedule)
    now = datetime(2026, 3, 6, 12, 0, tzinfo=UTC)
    items = next_five([plan], now)
    assert [item["at"] for item in items] == [
        "2026-03-07T07:30:00.000Z",
        None,
        "2026-03-09T06:30:00.000Z",
        "2026-03-10T06:30:00.000Z",
        "2026-03-11T06:30:00.000Z",
        "2026-03-12T06:30:00.000Z",
    ]
    assert [item["offset_minutes"] for item in items[:3]] == [-300, None, -240]
    assert items[1] == {
        "at": None,
        "local": "2026-03-08T02:30",
        "zone": "America/New_York",
        "offset_minutes": None,
        "note": "dst_gap",
    }
    assert sum(1 for item in items if item["at"] is not None) == 5


def test_slots_new_york_fall_back_fold() -> None:
    schedule = _schedule("daily", ["01:30"], zone="America/New_York")
    plan = RevisionPlan(1, EARLY, schedule)
    lo = datetime(2026, 10, 31, tzinfo=UTC)
    hi = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    result = slots([plan], lo, hi)
    assert [stamp(instant) for instant, _rev in result] == [
        "2026-10-31T05:30:00.000Z",
        "2026-11-01T05:30:00.000Z",
        "2026-11-02T06:30:00.000Z",
    ]


def test_slots_midnight_rollback_inside_same_local_endpoint_dates() -> None:
    zone = ZoneInfo("America/Goose_Bay")
    lo = datetime(2010, 11, 7, 2, 59, tzinfo=UTC)
    hi = datetime(2010, 11, 7, 3, 2, tzinfo=UTC)
    assert lo.astimezone(zone).date() == hi.astimezone(zone).date()
    before_window = stamp(datetime(2010, 1, 1, tzinfo=UTC))
    plan = RevisionPlan(1, before_window, _schedule("daily", ["00:00"], zone=zone.key))
    assert slots([plan], lo, hi) == [(datetime(2010, 11, 7, 3, 0, tzinfo=UTC), 1)]
    ordinary = RevisionPlan(1, before_window, _schedule("daily", ["03:00"], zone="UTC"))
    assert slots([ordinary], lo, hi) == [(datetime(2010, 11, 7, 3, 0, tzinfo=UTC), 1)]


def test_next_five_new_york_fall_back_notes() -> None:
    schedule = _schedule("daily", ["01:30"], zone="America/New_York")
    plan = RevisionPlan(1, EARLY, schedule)
    now = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
    items = next_five([plan], now)
    assert [(item["at"], item["offset_minutes"], item["note"]) for item in items[:3]] == [
        ("2026-10-31T05:30:00.000Z", -240, ""),
        ("2026-11-01T05:30:00.000Z", -240, "dst_fold"),
        ("2026-11-02T06:30:00.000Z", -300, ""),
    ]


def test_slots_lord_howe_spring_forward_gap() -> None:
    schedule = _schedule("daily", ["02:15"], zone="Australia/Lord_Howe")
    plan = RevisionPlan(1, EARLY, schedule)
    lo = datetime(2026, 10, 2, tzinfo=UTC)
    hi = datetime(2026, 10, 4, 20, 0, tzinfo=UTC)
    result = slots([plan], lo, hi)
    assert [(stamp(instant), rev) for instant, rev in result] == [
        ("2026-10-02T15:45:00.000Z", 1),
        ("2026-10-04T15:15:00.000Z", 1),
    ]


def test_slots_lord_howe_fall_back_fold() -> None:
    schedule = _schedule("daily", ["01:45"], zone="Australia/Lord_Howe")
    plan = RevisionPlan(1, EARLY, schedule)
    lo = datetime(2026, 4, 3, tzinfo=UTC)
    hi = datetime(2026, 4, 6, tzinfo=UTC)
    result = slots([plan], lo, hi)
    assert [stamp(instant) for instant, _rev in result] == [
        "2026-04-03T14:45:00.000Z",
        "2026-04-04T14:45:00.000Z",
        "2026-04-05T15:15:00.000Z",
    ]


@pytest.mark.parametrize(
    ("hhmm", "now", "expected"),
    [
        (
            "02:15",
            datetime(2026, 10, 2, tzinfo=UTC),
            [
                ("2026-10-02T15:45:00.000Z", 630, ""),
                (None, None, "dst_gap"),
                ("2026-10-04T15:15:00.000Z", 660, ""),
            ],
        ),
        (
            "01:45",
            datetime(2026, 4, 3, tzinfo=UTC),
            [
                ("2026-04-03T14:45:00.000Z", 660, ""),
                ("2026-04-04T14:45:00.000Z", 660, "dst_fold"),
                ("2026-04-05T15:15:00.000Z", 630, ""),
            ],
        ),
    ],
)
def test_next_five_lord_howe_half_hour_offsets_and_notes(
    hhmm: str, now: datetime, expected: list[tuple[str | None, int | None, str]]
) -> None:
    schedule = _schedule("daily", [hhmm], zone="Australia/Lord_Howe")
    items = next_five([RevisionPlan(1, EARLY, schedule)], now)
    assert [(item["at"], item["offset_minutes"], item["note"]) for item in items[:3]] == expected


def test_next_five_utc_daily() -> None:
    schedule = _schedule("daily", ["23:59"], zone="UTC")
    plan = RevisionPlan(1, EARLY, schedule)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    items = next_five([plan], now)
    assert items[0] == {
        "at": "2026-01-01T23:59:00.000Z",
        "local": "2026-01-01T23:59+00:00",
        "zone": "UTC",
        "offset_minutes": 0,
        "note": "",
    }


def test_slots_weekly_days_filter() -> None:
    schedule = _schedule("weekly", ["09:00"], days=[1, 3], zone="UTC")  # Mon, Wed
    plan = RevisionPlan(1, EARLY, schedule)
    lo = datetime(2026, 9, 21, tzinfo=UTC)  # Monday
    hi = datetime(2026, 9, 27, 23, 59, tzinfo=UTC)  # Sunday
    result = slots([plan], lo, hi)
    found = sorted({instant.date().isoformat() for instant, _rev in result})
    assert found == ["2026-09-21", "2026-09-23"]


def test_slots_date_bounds() -> None:
    schedule = _schedule("daily", ["09:00"], zone="UTC", start="2026-01-05", end="2026-01-07")
    plan = RevisionPlan(1, EARLY, schedule)
    lo = datetime(2026, 1, 1, tzinfo=UTC)
    hi = datetime(2026, 1, 31, tzinfo=UTC)
    result = slots([plan], lo, hi)
    assert [instant.date().isoformat() for instant, _rev in result] == [
        "2026-01-05",
        "2026-01-06",
        "2026-01-07",
    ]


def test_next_five_after_end_date_is_empty() -> None:
    schedule = _schedule("daily", ["09:00"], zone="UTC", start="2026-01-05", end="2026-01-07")
    plan = RevisionPlan(1, EARLY, schedule)
    now = datetime(2026, 1, 10, tzinfo=UTC)
    assert next_five([plan], now) == []


def test_next_five_previews_unsaved_schedule() -> None:
    now = datetime(2026, 9, 27, tzinfo=UTC)
    raw = {
        "kind": "daily",
        "times": ["09:00"],
        "days": [],
        "timezone": "Asia/Tokyo",
        "start_date": None,
        "end_date": None,
        "catch_up": "skip",
    }
    unsaved = RevisionPlan(1, stamp(now), parse_schedule(raw))
    saved = RevisionPlan(1, EARLY, parse_schedule(raw))
    assert next_five([unsaved], now) == next_five([saved], now)


def test_slots_two_revisions_cutover_mid_day() -> None:
    schedule = _schedule("daily", ["09:00"], zone="UTC")
    cutover = stamp(datetime(2026, 1, 2, 12, 0, tzinfo=UTC))
    plan1 = RevisionPlan(1, EARLY, schedule)
    plan2 = RevisionPlan(2, cutover, schedule)
    lo = datetime(2026, 1, 1, tzinfo=UTC)
    hi = datetime(2026, 1, 3, 12, 0, tzinfo=UTC)
    result = slots([plan1, plan2], lo, hi)
    assert [(stamp(instant), rev) for instant, rev in result] == [
        ("2026-01-01T09:00:00.000Z", 1),
        ("2026-01-02T09:00:00.000Z", 1),
        ("2026-01-03T09:00:00.000Z", 2),
    ]


def test_slots_bounds_inclusive() -> None:
    schedule = _schedule("daily", ["09:00"], zone="UTC")
    plan = RevisionPlan(1, EARLY, schedule)
    exact = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
    assert [stamp(instant) for instant, _rev in slots([plan], exact, exact)] == [
        "2026-01-01T09:00:00.000Z"
    ]
    just_before = exact - timedelta(milliseconds=1)
    assert slots([plan], just_before, just_before) == []


def test_slots_skips_non_governing_revisions(monkeypatch: pytest.MonkeyPatch) -> None:
    # 256 revisions of one daily 4-time schedule, each activating a minute after the last;
    # only the last (governing) revision should be walked for a window after them all.
    schedule = _schedule("daily", ["01:00", "09:00", "17:00", "23:00"], zone="UTC")
    base = datetime(2026, 1, 10, tzinfo=UTC)
    revisions = [
        RevisionPlan(revision, stamp(base + timedelta(minutes=revision)), schedule)
        for revision in range(1, 257)
    ]
    # A window inside one UTC calendar date walks it and its two adjacent dates.
    lo = datetime(2026, 1, 15, 1, 0, tzinfo=UTC)
    hi = datetime(2026, 1, 15, 22, 0, tzinfo=UTC)

    calls = 0
    original = scheduler_module._local_slot

    def counting(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(scheduler_module, "_local_slot", counting)
    result = slots(revisions, lo, hi)

    assert result == [
        (datetime(2026, 1, 15, 1, 0, tzinfo=UTC), 256),
        (datetime(2026, 1, 15, 9, 0, tzinfo=UTC), 256),
        (datetime(2026, 1, 15, 17, 0, tzinfo=UTC), 256),
    ]
    assert calls == 12


def test_slots_dense_revisions_walk_only_active_dates(monkeypatch: pytest.MonkeyPatch) -> None:
    schedule = _schedule("daily", ["01:00", "03:00", "05:00", "09:00"], zone="UTC")
    lo = NINE - timedelta(days=7)
    revisions = [
        RevisionPlan(revision, stamp(lo + timedelta(seconds=revision * 2300)), schedule)
        for revision in range(1, 257)
    ]
    calls = 0
    local_slot = scheduler_module._local_slot

    def counting(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        return local_slot(*args, **kwargs)

    monkeypatch.setattr(scheduler_module, "_local_slot", counting)
    result = slots(revisions, lo, NINE)
    assert len(result) == 28
    assert result[-1][0] == NINE
    assert calls < 100  # adjacent dates are reused across the 256 short revisions


def test_slots_manual_yields_none() -> None:
    schedule = _schedule("manual", [])
    plan = RevisionPlan(1, EARLY, schedule)
    lo = datetime(2026, 1, 1, tzinfo=UTC)
    hi = datetime(2026, 12, 31, tzinfo=UTC)
    assert slots([plan], lo, hi) == []
    assert next_five([plan], lo) == []


async def test_fake_clock_wait_records_seconds_and_ticks() -> None:
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
    event = asyncio.Event()

    async def waiter() -> None:
        await clock.wait(event, 12.5)

    task = asyncio.ensure_future(waiter())
    await asyncio.sleep(0)
    assert clock.waits == [12.5]
    assert not task.done()
    clock.tick()
    await asyncio.wait_for(task, timeout=0.05)


async def test_fake_clock_wait_unblocks_on_event() -> None:
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
    event = asyncio.Event()
    task = asyncio.ensure_future(clock.wait(event, 5.0))
    await asyncio.sleep(0)
    event.set()
    await asyncio.wait_for(task, timeout=0.05)


def test_fake_clock_advance() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    clock = FakeClock(start)
    clock.advance(5.0)
    assert clock.wall() == start + timedelta(seconds=5)
    assert clock.monotonic() == 1005.0
    clock.advance(2.0, wall=False)
    assert clock.wall() == start + timedelta(seconds=5)
    assert clock.monotonic() == 1007.0


async def test_system_clock_wait_times_out_without_raising() -> None:
    clock = SystemClock()
    event = asyncio.Event()
    await clock.wait(event, 0.01)
    assert isinstance(clock.wall(), datetime)
    assert isinstance(clock.monotonic(), float)


# --- Scheduler: each due instant decided once ---

SCOPE = "ab" * 32
OWNER = "44444444-4444-4444-8444-4444444444ab"
NINE = datetime(2026, 9, 27, 9, 0, tzinfo=UTC)
# A hang guard, not a sleep: a pass's store work runs in a worker thread. The 64-routine
# backlog pass takes seconds under a parallel run's load, past five; only a hang reaches thirty.
HANG_GUARD_S = 30


class StubRunner:
    """The task runner seam: a real task store; `start` records the attempt and runs nothing."""

    available = True
    _closing = False

    def __init__(self, store: TaskStore) -> None:
        self.store = store
        self.started: list[Attempt] = []

    def start(self, attempt: Attempt) -> None:
        self.started.append(attempt)


@pytest.fixture
def store(tmp_path: Path) -> SessionStore:
    return AppState(tmp_path / "state.json", Prefs()).store


def _scheduler(
    store: SessionStore,
    clock: FakeClock,
    attended: Callable[[], bool] = lambda: True,
    admission_lock: asyncio.Lock | None = None,
    settings: SettingsService | None = None,
    card_waiting: Callable[[str, str], bool | None] | None = None,
    notes_changed: Callable[[str, str, datetime], bool | None] | None = None,
) -> tuple[Scheduler, StubRunner]:
    routines = RoutineService(store, ProfileService(store), settings, None, now=clock.wall)
    runner = StubRunner(TaskStore(store, SCOPE, OWNER))
    scheduler = Scheduler(
        routines,
        runner,
        clock,
        attended,
        admission_lock=admission_lock,
        card_waiting=card_waiting,
        notes_changed=notes_changed,
    )
    return scheduler, runner


def _raw(times: list[str], zone: str = "UTC", **overrides: Any) -> dict[str, Any]:
    return _document_raw(schedule=_daily_schedule(times=times, timezone=zone), **overrides)


def _create(scheduler: Scheduler, times: list[str]) -> str:
    return scheduler.routines.create(_ident(), _raw(times))[0]["routine_id"]


def _enable(scheduler: Scheduler, routine_id: str) -> None:
    """Enable at the clock's now by a raw state update: the service's enable needs a tested
    routine and a ready profile and workspace, which `test_routines.py` covers."""
    with scheduler.routines.store._connection(write=True) as connection:
        connection.execute(
            "UPDATE routines SET state='enabled',enabled_since=? WHERE routine_id=?",
            (stamp(scheduler.clock.wall()), routine_id),
        )


def _move(clock: FakeClock, to: datetime) -> None:
    clock.advance((to - clock.wall()).total_seconds())


def _worker_stages() -> str:
    frames = sys._current_frames()
    stages: list[tuple[bool, str]] = []
    for worker in threading.enumerate():
        ident = worker.ident
        if not worker.name.startswith("asyncio_") or ident is None:
            continue
        frame = frames.get(ident)
        locations: list[str] = []
        while frame is not None and len(locations) < 16:
            locations.append(
                f"{Path(frame.f_code.co_filename).name}:{frame.f_code.co_name}:{frame.f_lineno}"
            )
            frame = frame.f_back
        stages.append(
            (
                any(location.startswith("scheduler.py:") for location in locations),
                " > ".join(locations),
            )
        )
    stages.sort(key=lambda stage: not stage[0])
    return f"workers={len(stages)} [{'; '.join(stage for _, stage in stages[:8])}]"


async def _pass(scheduler: Scheduler) -> datetime | None:
    task = asyncio.create_task(scheduler.pass_once())
    try:
        done, _ = await asyncio.wait((task,), timeout=HANG_GUARD_S)
    except asyncio.CancelledError:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        raise
    if not done:
        stages = _worker_stages()  # inspect before cancelling the suspended pass
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        raise TimeoutError(f"scheduler pass exceeded {HANG_GUARD_S}s: {stages}")
    return await task


def _occurrences(store: SessionStore, routine_id: str) -> list[tuple[Any, ...]]:
    with store._connection() as connection:
        return connection.execute(
            "SELECT scheduled_for,state,reason,revision FROM routine_occurrences "
            "WHERE routine_id=? ORDER BY scheduled_for",
            (routine_id,),
        ).fetchall()


def _attempt_states(store: SessionStore) -> list[str]:
    with store._connection() as connection:
        return [state for (state,) in connection.execute("SELECT state FROM task_attempts")]


@pytest.mark.parametrize(
    "change", ["unattended", "unavailable", "scheduler_closing", "runner_closing"]
)
async def test_a_pass_rechecks_admission_after_waiting_for_the_window_lock(
    store: SessionStore, change: str
) -> None:
    class SignalingLock(asyncio.Lock):
        def __init__(self) -> None:
            super().__init__()
            self.waiting = asyncio.Event()

        async def acquire(self) -> Literal[True]:
            if self.locked():
                self.waiting.set()
            return await super().acquire()

    attended = True
    lock = SignalingLock()
    clock = FakeClock(NINE - timedelta(hours=1))
    scheduler, runner = _scheduler(store, clock, lambda: attended, lock)
    routine_id = _create(scheduler, ["09:00"])
    _enable(scheduler, routine_id)
    _move(clock, NINE + timedelta(seconds=1))
    scheduler._changed = False

    async with lock:
        passing = asyncio.create_task(scheduler.pass_once())
        await asyncio.wait_for(lock.waiting.wait(), HANG_GUARD_S)
        if change == "unattended":
            attended = False  # the last-window removal owns this lock
        elif change == "unavailable":
            runner.available = False
        elif change == "scheduler_closing":
            scheduler._closing = True
        else:
            runner._closing = True
    await asyncio.wait_for(passing, HANG_GUARD_S)
    assert _occurrences(store, routine_id) == []
    assert _attempt_states(store) == [] and runner.started == []
    if change == "unattended":
        assert scheduler._changed  # a reopened window must get a later pass


@pytest.mark.parametrize("cancel_pass", [False, True])
async def test_claim_handoff_precedes_last_window_removal_even_if_the_pass_is_cancelled(
    store: SessionStore, monkeypatch: pytest.MonkeyPatch, cancel_pass: bool
) -> None:
    attended = True
    lock = asyncio.Lock()
    clock = FakeClock(NINE - timedelta(hours=1))
    scheduler, runner = _scheduler(store, clock, lambda: attended, lock)
    routine_id = _create(scheduler, ["09:00"])
    _enable(scheduler, routine_id)
    _move(clock, NINE + timedelta(seconds=1))
    decide, entered, release = scheduler._decide, threading.Event(), threading.Event()
    order: list[str] = []
    start = runner.start

    def held(*args: Any) -> Attempt | Literal["future"] | None:
        entered.set()  # the admission check has passed; the decision has not committed
        assert release.wait(HANG_GUARD_S)
        return decide(*args)

    def started(attempt: Attempt) -> None:
        order.append("started")
        start(attempt)

    async def remove_last_window() -> None:
        nonlocal attended
        async with lock:
            attended = False
            order.append("last_removed")

    monkeypatch.setattr(scheduler, "_decide", held)
    monkeypatch.setattr(runner, "start", started)
    passing = asyncio.create_task(scheduler.pass_once())
    assert await asyncio.wait_for(asyncio.to_thread(entered.wait, HANG_GUARD_S), HANG_GUARD_S)
    removing = asyncio.create_task(remove_last_window())
    await asyncio.sleep(0)
    assert attended and not removing.done()
    if cancel_pass:
        passing.cancel()
    release.set()
    if cancel_pass:
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(passing, HANG_GUARD_S)
    else:
        await asyncio.wait_for(passing, HANG_GUARD_S)
    await asyncio.wait_for(removing, HANG_GUARD_S)
    assert order == ["started", "last_removed"]
    assert _occurrences(store, routine_id) == [(stamp(NINE), "claimed", "", 1)]
    assert len(runner.started) == 1 and _attempt_states(store) == ["claimed"]


def test_status_reads_state_and_attendance_and_wake_marks_a_change(store: SessionStore) -> None:
    attended = False
    scheduler, _ = _scheduler(store, FakeClock(NINE), lambda: attended)
    assert scheduler.status() == {"state": "unavailable", "attended": False, "paused": False}
    attended = True
    assert scheduler.status() == {"state": "unavailable", "attended": True, "paused": False}
    scheduler._changed = False
    scheduler.wake()
    assert scheduler._changed and scheduler._event.is_set()


async def test_misfires_are_skipped_and_a_rollback_claims_nothing_again(
    store: SessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock(NINE - timedelta(days=10, hours=1))
    scheduler, runner = _scheduler(store, clock)
    week_old, recent = _create(scheduler, ["09:00"]), _create(scheduler, ["09:00"])
    assert await _pass(scheduler) is None  # nothing is enabled yet
    _enable(scheduler, week_old)
    _move(clock, NINE - timedelta(days=3, hours=1))
    _enable(scheduler, recent)
    _move(clock, NINE - timedelta(hours=1))
    assert await _pass(scheduler) == NINE

    def missed(days: int) -> list[tuple[Any, ...]]:
        return [
            (stamp(NINE - timedelta(back)), "skipped", "missed", 1) for back in range(days, 0, -1)
        ]

    # No pass ran while daily slots went by: the misfire window keeps a week, enabling the rest.
    assert _occurrences(store, week_old) == missed(7)
    assert _occurrences(store, recent) == missed(3)
    assert _attempt_states(store) == [] and runner.started == []
    _move(clock, NINE + timedelta(seconds=1))
    assert await _pass(scheduler) == NINE + timedelta(days=1)
    with store._connection() as connection:
        claims = connection.execute(
            "SELECT scheduled_for,recorded_at FROM routine_occurrences WHERE state='claimed'"
        ).fetchall()
    lags = [parse_stamp(at) - parse_stamp(slot) for slot, at in claims]
    # Recorded when claimed, one second late: inside the five-second bound.
    assert lags == [timedelta(seconds=1)] * 2 and len(runner.started) == 2
    # A one-hour rollback after the claims, and the way back to the present, decide nothing:
    # the watermark keeps every decided instant out of the pass.
    monkeypatch.setattr(scheduler, "_decide", lambda *args: pytest.fail("decided again"))
    for moment in (NINE + timedelta(seconds=1) - timedelta(hours=1), NINE + timedelta(seconds=2)):
        _move(clock, moment)
        await _pass(scheduler)
    assert len(_occurrences(store, recent)) == 4 and len(runner.started) == 2


async def test_two_misses_in_one_pass_order_history_cursor_and_last_run(
    store: SessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock(NINE - timedelta(hours=1))
    scheduler, runner = _scheduler(store, clock)
    routine_id = _create(scheduler, ["09:00", "09:01"])
    _enable(scheduler, routine_id)
    _move(clock, NINE + timedelta(minutes=10))
    fixed = iter(
        (
            uuid.UUID("00000000-0000-4000-8000-000000000002"),
            uuid.UUID("00000000-0000-4000-8000-000000000001"),
        )
    )
    with monkeypatch.context() as patch:
        patch.setattr(uuid, "uuid4", lambda: next(fixed))
        await _pass(scheduler)

    older, newer = stamp(NINE), stamp(NINE + timedelta(minutes=1))
    assert _occurrences(store, routine_id) == [
        (older, "skipped", "missed", 1),
        (newer, "skipped", "missed", 1),
    ]
    assert runner.started == []
    with store._connection() as connection:
        recorded = connection.execute(
            "SELECT recorded_at FROM routine_occurrences WHERE routine_id=?", (routine_id,)
        ).fetchall()
    assert recorded == [(stamp(clock.wall()),)] * 2
    first = runner.store.runs(routine_id, 1, None)
    second = runner.store.runs(routine_id, 1, first[0]["occurrence_id"])
    assert [row["scheduled_for"] for row in first + second] == [newer, older]
    assert runner.store.runs(routine_id, 1, second[0]["occurrence_id"]) == []
    assert runner.store.summaries()[routine_id]["last_run"]["scheduled_for"] == newer


async def test_delayed_plan_records_an_expired_slot_as_missed_at_decision_time(
    store: SessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock(NINE - timedelta(hours=1))
    scheduler, runner = _scheduler(store, clock)
    routine_id = _create(scheduler, ["09:00"])
    _enable(scheduler, routine_id)
    _move(clock, NINE + timedelta(seconds=1))
    read, entered, release = scheduler.routines.plans, threading.Event(), threading.Event()

    def held_plan() -> list[RoutinePlan]:
        entered.set()
        assert release.wait(HANG_GUARD_S)
        return read()

    monkeypatch.setattr(scheduler.routines, "plans", held_plan)
    passing = asyncio.create_task(scheduler.pass_once())
    try:
        assert await asyncio.wait_for(asyncio.to_thread(entered.wait, HANG_GUARD_S), HANG_GUARD_S)
        _move(clock, NINE + timedelta(minutes=6))
    finally:
        release.set()
    await asyncio.wait_for(passing, HANG_GUARD_S)
    assert _occurrences(store, routine_id) == [(stamp(NINE), "skipped", "missed", 1)]
    with store._connection() as connection:
        recorded = connection.execute(
            "SELECT recorded_at FROM routine_occurrences WHERE routine_id=?", (routine_id,)
        ).fetchone()
    assert recorded == (stamp(NINE + timedelta(minutes=6)),)
    assert runner.started == []


async def test_clock_reversed_before_decision_leaves_the_future_slot_undecided(
    store: SessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock(NINE - timedelta(hours=1))
    scheduler, runner = _scheduler(store, clock)
    routine_id = _create(scheduler, ["09:00"])
    _enable(scheduler, routine_id)
    _move(clock, NINE + timedelta(seconds=1))
    decide, entered, release = scheduler._decide, threading.Event(), threading.Event()

    def held_decision(*args: Any) -> Attempt | Literal["future"] | None:
        entered.set()
        assert release.wait(HANG_GUARD_S)
        return decide(*args)

    monkeypatch.setattr(scheduler, "_decide", held_decision)
    passing = asyncio.create_task(scheduler.pass_once())
    try:
        assert await asyncio.wait_for(asyncio.to_thread(entered.wait, HANG_GUARD_S), HANG_GUARD_S)
        clock.advance(-62, monotonic=False)  # a wall correction leaves the slot one minute ahead
    finally:
        release.set()
    assert await asyncio.wait_for(passing, HANG_GUARD_S) == NINE
    assert _occurrences(store, routine_id) == []
    assert scheduler.routines.get(routine_id)["last_slot"] is None and runner.started == []
    passes = _count_passes(scheduler, monkeypatch)
    scheduler.start()
    await _waits(clock, 1)
    connection, reads = store._connection, []

    def spy(*args: Any, **kwargs: Any) -> Any:
        reads.append(kwargs)
        return connection(*args, **kwargs)

    monkeypatch.setattr(store, "_connection", spy)
    for _ in range(2):
        clock.advance(MAX_TICK_S)
        await _tick(clock)
    assert reads == [] and len(passes) == 1
    _move(clock, NINE)
    await _tick(clock)
    assert _occurrences(store, routine_id) == [(stamp(NINE), "claimed", "", 1)]
    assert len(runner.started) == 1
    await _close(scheduler)


async def test_short_reversal_rearms_a_slot_even_after_wall_crosses_it_before_handoff(
    store: SessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock(NINE - timedelta(hours=1))
    scheduler, runner = _scheduler(store, clock)
    routine_id = _create(scheduler, ["09:00"])
    _enable(scheduler, routine_id)
    _move(clock, NINE + timedelta(seconds=1))
    decide = scheduler._decide
    entered, release = threading.Event(), threading.Event()
    decided, handoff = threading.Event(), threading.Event()

    def held_decision(*args: Any) -> Attempt | Literal["future"] | None:
        entered.set()
        assert release.wait(HANG_GUARD_S)
        result = decide(*args)
        decided.set()  # the write transaction has returned without a claim
        assert handoff.wait(HANG_GUARD_S)
        return result

    monkeypatch.setattr(scheduler, "_decide", held_decision)
    passes = _count_passes(scheduler, monkeypatch)
    scheduler.start()
    try:
        assert await asyncio.wait_for(asyncio.to_thread(entered.wait, HANG_GUARD_S), HANG_GUARD_S)
        clock.advance(-1.5, monotonic=False)
        release.set()
        assert await asyncio.wait_for(asyncio.to_thread(decided.wait, HANG_GUARD_S), HANG_GUARD_S)
        assert clock.wall() == NINE - timedelta(milliseconds=500)
        clock.advance(0.6)  # natural time crosses the slot before worker-result observation
        handoff.set()
        await _waits(clock, 1)
        assert _occurrences(store, routine_id) == []
        assert scheduler.routines.get(routine_id)["last_slot"] is None
        await _tick(clock)  # due rearm runs another pass even though the jump is below JUMP_S
        assert _occurrences(store, routine_id) == [(stamp(NINE), "claimed", "", 1)]
        assert len(runner.started) == 1 and len(passes) == 2

        connection, reads = store._connection, []

        def spy(*args: Any, **kwargs: Any) -> Any:
            reads.append(kwargs)
            return connection(*args, **kwargs)

        monkeypatch.setattr(store, "_connection", spy)
        for _ in range(3):
            clock.advance(MAX_TICK_S)
            await _tick(clock)
        assert reads == [] and len(passes) == 2
    finally:
        release.set()
        handoff.set()
        await _close(scheduler)


async def test_future_slot_blocks_later_slots_of_its_routine_but_not_other_routines(
    store: SessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock(NINE - timedelta(hours=1))
    scheduler, runner = _scheduler(store, clock)
    routine_id = _create(scheduler, ["09:00", "09:01"])
    other_id = _create(scheduler, ["09:01"])
    _enable(scheduler, routine_id)
    _enable(scheduler, other_id)
    read = scheduler.routines.plans
    monkeypatch.setattr(
        scheduler.routines,
        "plans",
        lambda: sorted(read(), key=lambda plan: plan.routine_id != routine_id),
    )
    _move(clock, NINE + timedelta(minutes=1, seconds=1))
    decide = scheduler._decide
    entered, release = threading.Event(), threading.Event()
    decided, handoff = threading.Event(), threading.Event()

    def held_decision(*args: Any) -> Attempt | Literal["future"] | None:
        if args[:2] == (routine_id, NINE):
            entered.set()
            assert release.wait(HANG_GUARD_S)
            result = decide(*args)
            decided.set()
            assert handoff.wait(HANG_GUARD_S)
            return result
        return decide(*args)

    monkeypatch.setattr(scheduler, "_decide", held_decision)
    passing = asyncio.create_task(scheduler.pass_once())
    try:
        assert await asyncio.wait_for(asyncio.to_thread(entered.wait, HANG_GUARD_S), HANG_GUARD_S)
        clock.advance(-62, monotonic=False)  # first slot is now one second ahead
        release.set()
        assert await asyncio.wait_for(asyncio.to_thread(decided.wait, HANG_GUARD_S), HANG_GUARD_S)
        clock.advance(63)  # 09:01:02 before the pass can try its next candidate
    finally:
        release.set()
        handoff.set()
    assert await asyncio.wait_for(passing, HANG_GUARD_S) == NINE
    assert _occurrences(store, routine_id) == []
    assert scheduler.routines.get(routine_id)["last_slot"] is None
    assert _occurrences(store, other_id) == [(stamp(NINE + timedelta(minutes=1)), "claimed", "", 1)]

    await _pass(scheduler)
    assert _occurrences(store, routine_id) == [
        (stamp(NINE), "claimed", "", 1),
        (stamp(NINE + timedelta(minutes=1)), "skipped", "overlap", 1),
    ]
    assert [attempt.routine_id for attempt in runner.started] == [other_id, routine_id]


async def test_rollback_during_missed_batch_leaves_the_future_slots_undecided(
    store: SessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock(NINE - timedelta(hours=1))
    scheduler, runner = _scheduler(store, clock)
    routine_id = _create(scheduler, ["09:00", "09:01"])
    _enable(scheduler, routine_id)
    _move(clock, NINE + timedelta(minutes=10))
    decide, entered, release = scheduler._decide_missed, threading.Event(), threading.Event()

    def held(*args: Any) -> int:
        entered.set()
        assert release.wait(HANG_GUARD_S)
        return decide(*args)

    monkeypatch.setattr(scheduler, "_decide_missed", held)
    passing = asyncio.create_task(scheduler.pass_once())
    try:
        assert await asyncio.wait_for(asyncio.to_thread(entered.wait, HANG_GUARD_S), HANG_GUARD_S)
        _move(clock, NINE - timedelta(minutes=1))
    finally:
        release.set()
    assert await asyncio.wait_for(passing, HANG_GUARD_S) == NINE
    assert _occurrences(store, routine_id) == []
    _move(clock, NINE + timedelta(minutes=1, seconds=1))
    await _pass(scheduler)
    assert _occurrences(store, routine_id) == [
        (stamp(NINE), "claimed", "", 1),
        (stamp(NINE + timedelta(minutes=1)), "skipped", "overlap", 1),
    ]
    assert len(runner.started) == 1


async def test_changed_middle_miss_rolls_back_to_single_slot_decisions(
    store: SessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock(NINE - timedelta(hours=1))
    scheduler, runner = _scheduler(store, clock)
    routine_id = _create(scheduler, ["09:00", "09:01", "09:02"])
    _enable(scheduler, routine_id)
    _move(clock, NINE + timedelta(minutes=10))
    revision, decide = scheduler._revision, scheduler._decide
    middle = NINE + timedelta(minutes=1)
    individual: list[datetime] = []

    def changed(*args: Any) -> int | None:
        return None if args[2] == middle else revision(*args)

    def decided(*args: Any) -> Attempt | Literal["future"] | None:
        if not individual:
            assert _occurrences(store, routine_id) == []
            assert scheduler.routines.get(routine_id)["last_slot"] is None
        individual.append(args[1])
        return decide(*args)

    monkeypatch.setattr(scheduler, "_revision", changed)
    monkeypatch.setattr(scheduler, "_decide", decided)
    await _pass(scheduler)
    assert individual == [NINE, middle, NINE + timedelta(minutes=2)]
    assert _occurrences(store, routine_id) == [
        (stamp(NINE), "skipped", "missed", 1),
        (stamp(NINE + timedelta(minutes=2)), "skipped", "missed", 1),
    ]
    assert scheduler.routines.get(routine_id)["last_slot"] == stamp(NINE + timedelta(minutes=2))
    assert runner.started == []


@pytest.mark.parametrize("fail_on", [1, 3])
async def test_failed_missed_batch_rolls_back_all_writes(
    store: SessionStore, monkeypatch: pytest.MonkeyPatch, fail_on: int
) -> None:
    clock = FakeClock(NINE - timedelta(hours=1))
    scheduler, runner = _scheduler(store, clock)
    routine_id = _create(scheduler, ["09:00", "09:01", "09:02"])
    _enable(scheduler, routine_id)
    _move(clock, NINE + timedelta(minutes=10))
    insert_skip = runner.store.insert_skip
    calls = 0

    def fail_after_insert(*args: Any, **kwargs: Any) -> None:
        nonlocal calls
        insert_skip(*args, **kwargs)
        calls += 1
        if calls == fail_on:
            raise StateError("unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(runner.store, "insert_skip", fail_after_insert)
        with pytest.raises(StateError) as err:
            await _pass(scheduler)
    assert err.value.code == "unavailable"
    assert _occurrences(store, routine_id) == []
    assert scheduler.routines.get(routine_id)["last_slot"] is None
    await _pass(scheduler)
    assert _occurrences(store, routine_id) == [
        (stamp(NINE), "skipped", "missed", 1),
        (stamp(NINE + timedelta(minutes=1)), "skipped", "missed", 1),
        (stamp(NINE + timedelta(minutes=2)), "skipped", "missed", 1),
    ]
    assert scheduler.routines.get(routine_id)["last_slot"] == stamp(NINE + timedelta(minutes=2))
    assert runner.started == []


async def test_due_tail_commits_within_five_seconds_ahead_of_other_misfires(
    store: SessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock(NINE - timedelta(days=8))
    scheduler, runner = _scheduler(store, clock)
    ids = []
    for _ in range(64):
        routine_id = _create(scheduler, ["01:00", "04:00", "07:00"])
        _enable(scheduler, routine_id)
        ids.append(routine_id)
    tail = max(ids)  # plans are ordered by routine ID; the due routine is last
    _move(clock, NINE - timedelta(minutes=1))
    scheduler.routines.revise(tail, _ident(), 1, _raw(["09:00"]))
    with store._connection(write=True) as connection:
        connection.execute(
            "UPDATE routines SET enabled_since=? WHERE routine_id=?", (stamp(clock.wall()), tail)
        )
    _move(clock, NINE)
    decide, committed, committed_at = scheduler._decide, threading.Event(), []

    def measured(*args: Any) -> Attempt | Literal["future"] | None:
        result = decide(*args)
        if args[0] == tail and isinstance(result, Attempt):
            committed_at.append(time.monotonic())  # _decide returned after the write COMMIT
            committed.set()
        return result

    monkeypatch.setattr(scheduler, "_decide", measured)
    at_slot = time.monotonic()  # fake wall time is the due instant
    passing = asyncio.create_task(scheduler.pass_once())
    try:
        assert await asyncio.wait_for(
            asyncio.to_thread(committed.wait, HANG_GUARD_S * 4), HANG_GUARD_S * 4
        )
    finally:
        passing.cancel()  # the due claim is measured; no need to drain every old misfire
        with contextlib.suppress(asyncio.CancelledError):
            await passing
    assert committed_at[0] - at_slot <= 5
    assert _occurrences(store, tail) == [(stamp(NINE), "claimed", "", 2)]
    assert [attempt.routine_id for attempt in runner.started] == [tail]


async def test_due_claims_follow_each_routines_backlog_within_five_seconds(
    store: SessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock(NINE - timedelta(days=8))
    scheduler, runner = _scheduler(store, clock)
    ids = []
    for _ in range(64):
        routine_id = _create(scheduler, ["01:00", "03:00", "05:00", "09:00"])
        _enable(scheduler, routine_id)
        ids.append(routine_id)

    # Charge virtual wall time per write. The separate scratch probe measures real COMMIT time.
    connection = store._connection

    @contextlib.contextmanager
    def charged_connection(*args: Any, **kwargs: Any) -> Any:
        with connection(*args, **kwargs) as opened:
            yield opened
            if kwargs.get("write"):
                clock.advance(0.015)

    monkeypatch.setattr(store, "_connection", charged_connection)
    start = runner.start

    def finish(attempt: Attempt) -> None:
        start(attempt)
        assert runner.store.finish(attempt.attempt_id, "succeeded", "", stamp(clock.wall()))

    monkeypatch.setattr(runner, "start", finish)
    _move(clock, NINE - timedelta(seconds=5))
    await _pass(scheduler)
    assert clock.wall() < NINE
    _move(clock, NINE)
    await _pass(scheduler)
    assert clock.wall() <= NINE + timedelta(seconds=5)
    assert sorted(attempt.routine_id for attempt in runner.started) == sorted(ids)
    expected_misses = []
    oldest = NINE - timedelta(days=7, seconds=5)
    for days in range(7, -1, -1):
        for hour in (1, 3, 5, 9):
            instant = (NINE - timedelta(days=days)).replace(hour=hour)
            if oldest <= instant < NINE:
                expected_misses.append(stamp(instant))
    rows_before_restart = {}
    for routine_id in ids:
        rows = _occurrences(store, routine_id)
        rows_before_restart[routine_id] = rows
        assert [row[0] for row in rows] == expected_misses + [stamp(NINE)]
        assert all(row[1:3] == ("skipped", "missed") for row in rows[:-1])
        assert rows[-1][:3] == (stamp(NINE), "claimed", "")
        assert scheduler.routines.get(routine_id)["last_slot"] == stamp(NINE)
    await _pass(scheduler)
    assert len(runner.started) == 64
    restarted, fresh = _scheduler(store, clock)
    await _pass(restarted)
    assert fresh.started == []
    assert all(
        _occurrences(store, routine_id) == rows_before_restart[routine_id] for routine_id in ids
    )


@pytest.mark.parametrize(
    ("late", "state"), [(ON_TIME, "claimed"), (ON_TIME + timedelta(milliseconds=1), "skipped")]
)
async def test_a_slot_is_on_time_for_five_minutes(
    store: SessionStore, late: timedelta, state: str
) -> None:
    clock = FakeClock(NINE - timedelta(hours=1))
    scheduler, runner = _scheduler(store, clock)
    routine_id = _create(scheduler, ["09:00"])
    _enable(scheduler, routine_id)
    _move(clock, NINE + late)
    await _pass(scheduler)
    assert [row[1] for row in _occurrences(store, routine_id)] == [state]
    assert len(runner.started) == (state == "claimed")


async def test_an_edit_keeps_a_claimed_run_and_rebinds_the_next_slot(store: SessionStore) -> None:
    clock = FakeClock(NINE - timedelta(hours=1))
    scheduler, runner = _scheduler(store, clock)
    routine_id = _create(scheduler, ["09:00"])
    _enable(scheduler, routine_id)
    _move(clock, NINE + timedelta(seconds=1))
    await _pass(scheduler)
    # A rename after the claim leaves that run on revision 1 and binds tomorrow's pending slot.
    scheduler.routines.revise(routine_id, _ident(), 1, _raw(["09:00"], name="Renamed"))
    assert runner.store.finish(runner.started[0].attempt_id, "succeeded", "", stamp(clock.wall()))
    tomorrow = NINE + timedelta(days=1)
    _move(clock, tomorrow + timedelta(seconds=1))
    await _pass(scheduler)
    restarted, fresh = _scheduler(store, clock)
    await _pass(restarted)
    assert _occurrences(store, routine_id) == [
        (stamp(NINE), "claimed", "", 1),
        (stamp(tomorrow), "claimed", "", 2),
    ]
    assert [attempt.revision for attempt in runner.started] == [1, 2] and fresh.started == []


async def test_a_removed_time_does_not_run_and_one_restored_before_it_passes_runs_once(
    store: SessionStore,
) -> None:
    clock = FakeClock(NINE - timedelta(hours=1))
    scheduler, runner = _scheduler(store, clock)
    routine_id = _create(scheduler, ["09:00", "10:00"])
    _enable(scheduler, routine_id)
    ten = NINE + timedelta(hours=1)
    _move(clock, NINE - timedelta(minutes=40))
    scheduler.routines.revise(routine_id, _ident(), 1, _raw(["10:00"]))
    _move(clock, NINE + timedelta(seconds=1))
    assert await _pass(scheduler) == ten
    assert _occurrences(store, routine_id) == []
    # 09:20 removes 10:00 and 09:40 restores it; restoring 09:00 does not replay it.
    for revision, (minutes, times) in enumerate([(20, ["09:00"]), (40, ["09:00", "10:00"])], 2):
        _move(clock, NINE + timedelta(minutes=minutes))
        scheduler.routines.revise(routine_id, _ident(), revision, _raw(times))
    for late in (1, 2):
        _move(clock, ten + timedelta(seconds=late))
        await _pass(scheduler)
    assert _occurrences(store, routine_id) == [(stamp(ten), "claimed", "", 4)]
    assert len(runner.started) == 1


async def test_a_timezone_change_adds_no_occurrence_before_it_activates(
    store: SessionStore,
) -> None:
    clock = FakeClock(NINE - timedelta(hours=21))
    scheduler, runner = _scheduler(store, clock)
    routine_id = _create(scheduler, ["09:00"])
    _enable(scheduler, routine_id)
    # At 08:00 UTC the zone becomes Tokyo, whose 09:00 was 00:00 UTC today.
    _move(clock, NINE - timedelta(hours=1))
    scheduler.routines.revise(routine_id, _ident(), 1, _raw(["09:00"], zone="Asia/Tokyo"))
    _move(clock, NINE + timedelta(seconds=1))
    tokyo_nine = NINE + timedelta(hours=15)
    assert await _pass(scheduler) == tokyo_nine
    assert _occurrences(store, routine_id) == [] and runner.started == []
    _move(clock, tokyo_nine + timedelta(seconds=1))
    await _pass(scheduler)
    assert _occurrences(store, routine_id) == [(stamp(tokyo_nine), "claimed", "", 2)]


async def test_two_schedulers_racing_one_instant_across_an_edit_claim_it_once(
    store: SessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock(NINE - timedelta(hours=1))
    first, first_runner = _scheduler(store, clock)
    second, second_runner = _scheduler(store, clock)
    routine_id = _create(first, ["09:00"])
    _enable(first, routine_id)
    _move(clock, NINE)
    read_first, read_second = first.routines.plans, second.routines.plans
    decide_first = first._decide
    both_read, first_decided = threading.Event(), threading.Event()

    # The worst interleaving: both passes read their plans, a rename lands and activates at the
    # instant, the first decides from its stale plan, and only then does the second decide.
    def first_plans() -> list[RoutinePlan]:
        plans = read_first()
        assert both_read.wait(HANG_GUARD_S)
        first.routines.revise(routine_id, _ident(), 1, _raw(["09:00"], name="Renamed"))
        return plans

    def second_plans() -> list[RoutinePlan]:
        plans = read_second()
        both_read.set()
        assert first_decided.wait(HANG_GUARD_S)
        return plans

    def first_decide(*args: Any) -> Attempt | Literal["future"] | None:
        try:
            return decide_first(*args)
        finally:
            first_decided.set()

    monkeypatch.setattr(first.routines, "plans", first_plans)
    monkeypatch.setattr(second.routines, "plans", second_plans)
    monkeypatch.setattr(first, "_decide", first_decide)
    # The second finds the instant past the watermark and writes nothing.
    for name in ("insert_claim", "insert_skip"):
        monkeypatch.setattr(second_runner.store, name, lambda *a, **k: pytest.fail("wrote"))
    await asyncio.wait_for(asyncio.gather(first.pass_once(), second.pass_once()), HANG_GUARD_S)
    [attempt] = first_runner.started
    assert attempt.revision == 2 and second_runner.started == []
    assert _occurrences(store, routine_id) == [(stamp(NINE), "claimed", "", 2)]
    assert _attempt_states(store) == ["claimed"]


async def test_edit_after_slot_before_decision_keeps_the_slot_governing_revision(
    store: SessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock(NINE - timedelta(hours=1))
    scheduler, runner = _scheduler(store, clock)
    routine_id = _create(scheduler, ["09:00"])
    _enable(scheduler, routine_id)
    _move(clock, NINE + timedelta(seconds=1))
    decide, entered, release = scheduler._decide, threading.Event(), threading.Event()

    def held_decision(*args: Any) -> Attempt | Literal["future"] | None:
        entered.set()
        assert release.wait(HANG_GUARD_S)
        return decide(*args)

    monkeypatch.setattr(scheduler, "_decide", held_decision)
    passing = asyncio.create_task(scheduler.pass_once())
    try:
        assert await asyncio.wait_for(asyncio.to_thread(entered.wait, HANG_GUARD_S), HANG_GUARD_S)
        _move(clock, NINE + timedelta(seconds=30))
        scheduler.routines.revise(routine_id, _ident(), 1, _raw(["09:00"], name="Edited"))
    finally:
        release.set()
    await asyncio.wait_for(passing, HANG_GUARD_S)
    assert scheduler.routines.get(routine_id)["current_revision"] == 2
    assert _occurrences(store, routine_id) == [(stamp(NINE), "claimed", "", 1)]
    assert len(runner.started) == 1 and runner.started[0].revision == 1


@pytest.mark.parametrize("edit", ["pause", "remove"])
async def test_a_pause_or_a_removal_after_the_plan_read_drops_the_instant(
    store: SessionStore, monkeypatch: pytest.MonkeyPatch, edit: str
) -> None:
    clock = FakeClock(NINE - timedelta(hours=1))
    scheduler, runner = _scheduler(store, clock)
    routine_id = _create(scheduler, ["09:00", "10:00"])
    _enable(scheduler, routine_id)
    _move(clock, NINE)
    read = scheduler.routines.plans

    def plans_then_edit() -> list[RoutinePlan]:
        plans = read()
        if edit == "pause":
            scheduler.routines.pause(routine_id, _ident(), 1)
        else:  # the removal activates at the instant, so it governs it
            scheduler.routines.revise(routine_id, _ident(), 1, _raw(["10:00"]))
        return plans

    monkeypatch.setattr(scheduler.routines, "plans", plans_then_edit)
    await _pass(scheduler)
    assert _occurrences(store, routine_id) == [] and runner.started == []


async def test_a_pass_prunes_to_the_cap_and_a_rollback_reclaims_no_pruned_instant(
    store: SessionStore,
) -> None:
    clock = FakeClock(NINE - timedelta(days=5))
    scheduler, runner = _scheduler(store, clock)
    routine_id = _create(scheduler, ["09:00"])
    _enable(scheduler, routine_id)
    recent = [stamp(NINE - timedelta(days)) for days in (4, 3, 2, 1)]
    old = datetime(2020, 1, 1, tzinfo=UTC)
    filler = [stamp(old + timedelta(minutes=minutes)) for minutes in range(MAX_OCCURRENCES)]
    # The four recent due instants have early recorded_at values; pruning uses due time, so
    # the oldest 2020 filler goes first while the watermark stays at the newest slot.
    with store._connection(write=True) as connection:
        for index, slot in enumerate(recent + filler):
            runner.store.insert_skip(
                connection,
                routine_id=routine_id,
                scheduled_for=slot,
                revision=1,
                reason="missed",
                now=stamp(old + timedelta(milliseconds=index)),
            )
        connection.execute(
            "UPDATE routines SET last_slot=? WHERE routine_id=?", (recent[-1], routine_id)
        )
    _move(clock, NINE + timedelta(seconds=1))
    await _pass(scheduler)
    kept = [row[0] for row in _occurrences(store, routine_id)]
    assert len(kept) == MAX_OCCURRENCES and stamp(NINE) in kept
    assert set(recent + filler[5:]) <= set(kept)
    assert not set(filler[:5]) & set(kept)
    _move(clock, NINE + timedelta(seconds=1) - timedelta(days=2))
    await _pass(scheduler)
    assert [row[0] for row in _occurrences(store, routine_id)] == kept
    assert len(runner.started) == 1


def test_pruned_occurrence_cannot_bypass_a_newer_last_slot(
    store: SessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock(NINE - timedelta(hours=1))
    scheduler, runner = _scheduler(store, clock)
    routine_id = _create(scheduler, ["09:00", "09:01"])
    _enable(scheduler, routine_id)
    _move(clock, NINE + timedelta(minutes=1, seconds=1))
    newer = stamp(NINE + timedelta(minutes=1))
    monkeypatch.setattr("viki_agent.automation.tasks.MAX_OCCURRENCES", 1)
    with store._connection(write=True) as connection:
        for slot in (stamp(NINE), newer):
            runner.store.insert_skip(
                connection,
                routine_id=routine_id,
                scheduled_for=slot,
                revision=1,
                reason="missed",
                now=stamp(clock.wall()),
            )
        connection.execute(
            "UPDATE routines SET last_slot=? WHERE routine_id=?", (newer, routine_id)
        )
        runner.store.prune(connection, routine_id)
    assert _occurrences(store, routine_id) == [(newer, "skipped", "missed", 1)]

    assert scheduler._decide(routine_id, NINE) is None
    assert _occurrences(store, routine_id) == [(newer, "skipped", "missed", 1)]
    assert scheduler.routines.get(routine_id)["last_slot"] == newer
    assert _attempt_states(store) == []


async def test_overlap_and_busy_are_skipped_and_nothing_is_closed(store: SessionStore) -> None:
    clock = FakeClock(NINE - timedelta(hours=1))
    scheduler, runner = _scheduler(store, clock)
    first = _create(scheduler, ["09:00", "10:00"])
    second = _create(scheduler, ["09:00"])
    third = _create(scheduler, ["09:30"])
    for routine_id in (first, second, third):
        _enable(scheduler, routine_id)
    for moment in (NINE, NINE + timedelta(minutes=30), NINE + timedelta(hours=1)):
        _move(clock, moment + timedelta(seconds=1))
        await _pass(scheduler)
    assert _occurrences(store, first) == [
        (stamp(NINE), "claimed", "", 1),
        (stamp(NINE + timedelta(hours=1)), "skipped", "overlap", 1),
    ]
    assert _occurrences(store, second) == [(stamp(NINE), "claimed", "", 1)]
    assert _occurrences(store, third) == [
        (stamp(NINE + timedelta(minutes=30)), "skipped", "busy", 1)
    ]
    # The stub runner has no close, and both runs are still open in the store.
    assert sorted(attempt.routine_id for attempt in runner.started) == sorted([first, second])
    assert _attempt_states(store) == ["claimed", "claimed"]


def _settings(store: SessionStore) -> SettingsService:
    return SettingsService(store, provider_defaults=Prefs, on_chat_defaults=lambda _prefs: None)


def _pause_all(settings: SettingsService, paused: bool) -> None:
    revision = settings.read().document.revision
    settings.patch(str(uuid.uuid4()), revision, {"routines.paused": paused})


def _paused_scheduler(
    store: SessionStore, clock: FakeClock
) -> tuple[Scheduler, StubRunner, SettingsService]:
    settings = _settings(store)
    scheduler, runner = _scheduler(store, clock, settings=settings)
    _pause_all(settings, True)
    return scheduler, runner, settings


async def test_pause_all_records_a_due_instant_skipped_paused_and_starts_nothing(
    store: SessionStore,
) -> None:
    clock = FakeClock(NINE - timedelta(hours=1))
    scheduler, runner, _settings_service = _paused_scheduler(store, clock)
    routine_id = _create(scheduler, ["09:00"])
    _enable(scheduler, routine_id)
    _move(clock, NINE + timedelta(seconds=1))
    assert await _pass(scheduler) == NINE + timedelta(days=1)
    assert _occurrences(store, routine_id) == [(stamp(NINE), "skipped", "paused", 1)]
    assert scheduler.routines.get(routine_id)["last_slot"] == stamp(NINE)
    assert _attempt_states(store) == [] and runner.started == []


async def test_pause_all_records_old_instants_paused_not_missed(store: SessionStore) -> None:
    clock = FakeClock(NINE - timedelta(days=2, hours=1))
    scheduler, runner, _settings_service = _paused_scheduler(store, clock)
    routine_id = _create(scheduler, ["09:00"])
    _enable(scheduler, routine_id)
    _move(clock, NINE + timedelta(minutes=10))
    await _pass(scheduler)
    assert _occurrences(store, routine_id) == [
        (stamp(NINE - timedelta(days=back)), "skipped", "paused", 1) for back in (2, 1, 0)
    ]
    assert scheduler.routines.get(routine_id)["last_slot"] == stamp(NINE)
    assert _attempt_states(store) == [] and runner.started == []


async def test_pause_all_batches_a_routines_old_instants_and_decides_a_due_one_singly(
    store: SessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock(NINE - timedelta(days=2, hours=1))
    scheduler, runner, _settings_service = _paused_scheduler(store, clock)
    routine_id = _create(scheduler, ["09:00"])
    _enable(scheduler, routine_id)
    _move(clock, NINE + timedelta(seconds=1))
    decide_missed, decide = scheduler._decide_missed, scheduler._decide
    batches: list[tuple[Any, ...]] = []
    singles: list[tuple[Any, ...]] = []

    def batched(*args: Any) -> int:
        batches.append(args)
        return decide_missed(*args)

    def single(*args: Any) -> Attempt | Literal["future"] | None:
        singles.append(args)
        return decide(*args)

    monkeypatch.setattr(scheduler, "_decide_missed", batched)
    monkeypatch.setattr(scheduler, "_decide", single)
    await _pass(scheduler)
    old = [NINE - timedelta(days=2), NINE - timedelta(days=1)]
    # One write transaction records both old instants; the due one keeps its own decision.
    assert batches == [(routine_id, old, "paused")]
    assert singles == [(routine_id, NINE, True, None)]
    assert _occurrences(store, routine_id) == [
        (stamp(instant), "skipped", "paused", 1) for instant in [*old, NINE]
    ]
    assert scheduler.routines.get(routine_id)["last_slot"] == stamp(NINE)
    assert _attempt_states(store) == [] and runner.started == []


async def test_resume_claims_the_next_instant_and_never_a_paused_one(store: SessionStore) -> None:
    clock = FakeClock(NINE - timedelta(hours=1))
    scheduler, runner, settings = _paused_scheduler(store, clock)
    routine_id = _create(scheduler, ["09:00", "09:01"])
    _enable(scheduler, routine_id)
    _move(clock, NINE + timedelta(seconds=1))
    await _pass(scheduler)
    _pause_all(settings, False)
    # Still on time for the paused instant: resuming does not replay it.
    _move(clock, NINE + timedelta(seconds=2))
    await _pass(scheduler)
    assert runner.started == []
    _move(clock, NINE + timedelta(minutes=1, seconds=1))
    await _pass(scheduler)
    assert _occurrences(store, routine_id) == [
        (stamp(NINE), "skipped", "paused", 1),
        (stamp(NINE + timedelta(minutes=1)), "claimed", "", 1),
    ]
    assert [attempt.routine_id for attempt in runner.started] == [routine_id]


async def test_untrusted_settings_count_as_paused(store: SessionStore) -> None:
    clock = FakeClock(NINE - timedelta(hours=1))
    settings = _settings(store)
    scheduler, runner = _scheduler(store, clock, settings=settings)
    _pause_all(settings, False)
    with store._connection(write=True) as connection:
        connection.execute("UPDATE settings SET version=version+1 WHERE scope='app'")
    assert settings.flag("routines.paused") is None
    assert scheduler.status()["paused"] is True
    routine_id = _create(scheduler, ["09:00"])
    _enable(scheduler, routine_id)
    _move(clock, NINE + timedelta(seconds=1))
    await _pass(scheduler)
    assert _occurrences(store, routine_id) == [(stamp(NINE), "skipped", "paused", 1)]
    assert runner.started == []


def test_status_reports_paused(store: SessionStore) -> None:
    clock = FakeClock(NINE)
    unset, _ = _scheduler(store, clock)
    settings = _settings(store)
    scheduler, _ = _scheduler(store, clock, settings=settings)
    assert scheduler.status() == {"state": "unavailable", "attended": True, "paused": False}
    _pause_all(settings, True)
    assert scheduler.status() == {"state": "unavailable", "attended": True, "paused": True}
    # Without a settings service there is no pause all.
    assert unset.status()["paused"] is False


@pytest.mark.parametrize("outcome", ["failed", "blocked", "expired", "unknown"])
async def test_a_failed_run_is_never_retried_by_later_passes(
    store: SessionStore, outcome: Outcome
) -> None:
    clock = FakeClock(NINE - timedelta(hours=1))
    scheduler, runner = _scheduler(store, clock)
    routine_id = _create(scheduler, ["09:00", "10:00"])
    _enable(scheduler, routine_id)
    _move(clock, NINE + timedelta(seconds=1))
    await _pass(scheduler)
    (attempt,) = runner.started
    assert runner.store.finish(attempt.attempt_id, outcome, "internal", stamp(clock.wall()))
    # Later passes inside the instant's on-time window, and one after a clock rollback.
    for moment in (
        NINE + timedelta(seconds=2),
        NINE + ON_TIME,
        NINE - timedelta(minutes=30),
        NINE + timedelta(seconds=3),
    ):
        _move(clock, moment)
        await _pass(scheduler)
    assert runner.started == [attempt]
    _move(clock, NINE + timedelta(hours=1, seconds=1))
    await _pass(scheduler)
    assert [started.attempt_id for started in runner.started][0] == attempt.attempt_id
    assert len(runner.started) == 2
    assert _occurrences(store, routine_id) == [
        (stamp(NINE), "claimed", "", 1),
        (stamp(NINE + timedelta(hours=1)), "claimed", "", 1),
    ]


@pytest.mark.parametrize(
    ("code", "retry"),
    [("invalid_input", timedelta(days=1)), ("busy", timedelta(seconds=1 + MAX_TICK_S))],
)
async def test_a_lost_claim_decides_nothing_this_pass(
    store: SessionStore, monkeypatch: pytest.MonkeyPatch, code: str, retry: timedelta
) -> None:
    clock = FakeClock(NINE - timedelta(hours=1))
    scheduler, runner = _scheduler(store, clock)
    routine_id = _create(scheduler, ["09:00"])
    _enable(scheduler, routine_id)

    def lost(*args: Any, **kwargs: Any) -> Attempt:
        raise StateError(code)

    monkeypatch.setattr(runner.store, "insert_claim", lost)
    _move(clock, NINE + timedelta(seconds=1))
    # A lost race waits for tomorrow; a busy store passes again within one tick.
    assert await _pass(scheduler) == NINE + retry
    assert _occurrences(store, routine_id) == [] and runner.started == []
    assert scheduler.routines.get(routine_id)["last_slot"] is None


async def test_a_failed_decision_still_starts_the_claims_committed_before_it(
    store: SessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock(NINE - timedelta(hours=1))
    scheduler, runner = _scheduler(store, clock)
    for _ in range(2):
        _enable(scheduler, _create(scheduler, ["09:00"]))
    insert_claim = runner.store.insert_claim
    claims: list[Attempt] = []

    def second_fails(*args: Any, **kwargs: Any) -> Attempt:
        if claims:
            raise StateError("unavailable")
        claims.append(insert_claim(*args, **kwargs))
        return claims[0]

    monkeypatch.setattr(runner.store, "insert_claim", second_fails)
    _move(clock, NINE + timedelta(seconds=1))
    with pytest.raises(StateError) as err:
        await _pass(scheduler)
    assert err.value.code == "unavailable" and runner.started == claims
    assert _occurrences(store, claims[0].routine_id) == [(stamp(NINE), "claimed", "", 1)]


async def test_a_busy_store_leaves_the_instant_for_the_next_pass(
    store: SessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock(NINE - timedelta(hours=1))
    scheduler, runner = _scheduler(store, clock)
    routine_id = _create(scheduler, ["09:00", "09:02"])
    _enable(scheduler, routine_id)
    connection = store._connection
    refused: list[bool] = []

    def busy_once(*args: Any, **kwargs: Any) -> Any:
        if kwargs.get("write") and not refused:
            refused.append(True)
            raise StateError("busy")
        return connection(*args, **kwargs)

    monkeypatch.setattr(store, "_connection", busy_once)
    now = NINE + timedelta(minutes=3)
    _move(clock, now)
    # 09:02 is not decided either: its commit would move the watermark past 09:00.
    due = await _pass(scheduler)
    assert due is not None and due <= now + timedelta(seconds=MAX_TICK_S)
    assert _occurrences(store, routine_id) == [] and runner.started == []
    assert scheduler.routines.get(routine_id)["last_slot"] is None
    _move(clock, now + timedelta(seconds=MAX_TICK_S))
    await _pass(scheduler)
    assert _occurrences(store, routine_id) == [
        (stamp(NINE), "claimed", "", 1),
        (stamp(NINE + timedelta(minutes=2)), "skipped", "overlap", 1),
    ]
    assert len(runner.started) == 1


async def test_busy_missed_batch_does_not_block_another_due_routine(
    store: SessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock(NINE - timedelta(hours=1))
    scheduler, runner = _scheduler(store, clock)
    backlogged = _create(scheduler, ["09:00", "09:01", "09:10"])
    other = _create(scheduler, ["09:10"])
    for routine_id in (backlogged, other):
        _enable(scheduler, routine_id)
    _move(clock, NINE + timedelta(minutes=10, seconds=1))
    plans = scheduler.routines.plans
    monkeypatch.setattr(
        scheduler.routines,
        "plans",
        lambda: sorted(plans(), key=lambda plan: plan.routine_id != backlogged),
    )
    connection, refused = store._connection, False

    def busy_once(*args: Any, **kwargs: Any) -> Any:
        nonlocal refused
        if kwargs.get("write") and not refused:
            refused = True
            raise StateError("busy")
        return connection(*args, **kwargs)

    monkeypatch.setattr(store, "_connection", busy_once)
    due = await _pass(scheduler)
    assert refused and due is not None and due <= clock.wall() + timedelta(seconds=MAX_TICK_S)
    assert _occurrences(store, backlogged) == []
    assert _occurrences(store, other) == [(stamp(NINE + timedelta(minutes=10)), "claimed", "", 1)]
    assert [attempt.routine_id for attempt in runner.started] == [other]


async def test_a_decision_parses_only_the_governing_revision_and_its_successor(
    store: SessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock(NINE - timedelta(hours=1))
    scheduler, runner = _scheduler(store, clock)
    routine_id = _create(scheduler, ["09:00"])
    _enable(scheduler, routine_id)
    for revision in (1, 2, 3):
        clock.advance(60)
        scheduler.routines.revise(
            routine_id, _ident(), revision, _raw(["09:00"], name=f"R{revision}")
        )
    parsed: list[object] = []
    parse = scheduler_module.parse_schedule

    def counting(raw: object) -> dict[str, Any]:
        parsed.append(raw)
        return parse(raw)

    monkeypatch.setattr(scheduler_module, "parse_schedule", counting)
    _move(clock, NINE + timedelta(seconds=1))
    await _pass(scheduler)
    assert _occurrences(store, routine_id) == [(stamp(NINE), "claimed", "", 4)]
    assert 1 <= len(parsed) <= 2


# --- The loop: attended ticks, clock jumps, idle ---


def _count_passes(scheduler: Scheduler, monkeypatch: pytest.MonkeyPatch) -> list[datetime]:
    """Every pass reads the plans once: record the wall time of each read."""
    read, passes = scheduler.routines.plans, []

    def counted() -> list[RoutinePlan]:
        passes.append(scheduler.clock.wall())
        return read()

    monkeypatch.setattr(scheduler.routines, "plans", counted)
    return passes


async def _waits(clock: FakeClock, count: int) -> None:
    """Yield until the loop has entered its ``count``-th wait (a pass runs in a worker thread)."""

    async def reached() -> None:
        while len(clock.waits) < count:
            await asyncio.sleep(0)

    await asyncio.wait_for(reached(), HANG_GUARD_S)


async def _tick(clock: FakeClock) -> None:
    count = len(clock.waits)
    clock.tick()
    await _waits(clock, count + 1)


def _jump(clock: FakeClock, wall_s: float, monotonic_s: float) -> None:
    clock.advance(wall_s, monotonic=False)
    clock.advance(monotonic_s, wall=False)


async def _close(scheduler: Scheduler) -> None:
    await asyncio.wait_for(scheduler.close(), HANG_GUARD_S)


async def test_a_clock_jump_or_a_sleep_is_passed_on_the_next_tick(
    store: SessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock(NINE - timedelta(hours=2))
    scheduler, runner = _scheduler(store, clock)
    routine_id = _create(scheduler, ["09:00", "09:57"])
    _enable(scheduler, routine_id)
    passes = _count_passes(scheduler, monkeypatch)
    scheduler.start()
    await _waits(clock, 1)
    assert len(passes) == 1
    clock.advance(MAX_TICK_S)  # both clocks agree and nothing is due: no pass
    await _tick(clock)
    assert len(passes) == 1
    _jump(clock, 3600, MAX_TICK_S)  # a forward jump that lands before 09:00
    await _tick(clock)
    assert len(passes) == 2 and _occurrences(store, routine_id) == []
    _jump(clock, (NINE + timedelta(minutes=58) - clock.wall()).total_seconds(), MAX_TICK_S)
    await _tick(clock)
    # 09:00 is 58 minutes late and 09:57 one: the five-minute rule decides each.
    assert _occurrences(store, routine_id) == [
        (stamp(NINE), "skipped", "missed", 1),
        (stamp(NINE + timedelta(minutes=57)), "claimed", "", 1),
    ]
    assert len(runner.started) == 1
    _jump(clock, 8 * 3600, 0)  # a sleep: the monotonic clock stood still
    await _tick(clock)
    assert len(passes) == 4
    assert all(0 <= seconds <= MAX_TICK_S for seconds in clock.waits)
    await _close(scheduler)


async def test_no_window_claims_nothing_until_one_returns(
    store: SessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    attended = True
    clock = FakeClock(NINE - timedelta(minutes=1))
    scheduler, runner = _scheduler(store, clock, lambda: attended)
    routine_id = _create(scheduler, ["09:00"])
    _enable(scheduler, routine_id)
    passes = _count_passes(scheduler, monkeypatch)
    scheduler.start()
    await _waits(clock, 1)
    attended = False
    clock.advance(90)
    await _tick(clock)
    # Due but unattended: no pass, and a full tick rather than an immediate wake.
    assert len(passes) == 1 and _occurrences(store, routine_id) == []
    assert clock.waits[-1] == MAX_TICK_S
    attended = True
    clock.advance(MAX_TICK_S)
    await _tick(clock)
    assert len(passes) == 2 and _occurrences(store, routine_id) == [(stamp(NINE), "claimed", "", 1)]
    assert len(runner.started) == 1
    await _close(scheduler)


async def test_an_idle_loop_reads_no_store(
    store: SessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock(NINE)
    scheduler, _ = _scheduler(store, clock)
    for _ in range(64):
        _enable(scheduler, _create(scheduler, ["21:00"]))
    scheduler.start()
    await _waits(clock, 1)
    connection, reads = store._connection, []

    def spy(*args: Any, **kwargs: Any) -> Any:
        reads.append(kwargs)
        return connection(*args, **kwargs)

    monkeypatch.setattr(store, "_connection", spy)
    for _ in range(10):
        clock.advance(MAX_TICK_S)
        await _tick(clock)
    assert reads == [] and clock.waits == [MAX_TICK_S] * 11
    await _close(scheduler)


async def test_a_start_beyond_the_preview_horizon_keeps_a_wake_armed(
    store: SessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock(NINE)
    scheduler, runner = _scheduler(store, clock)
    start = NINE + timedelta(days=SCAN_DAYS + 1)
    routine_id = scheduler.routines.create(
        _ident(),
        _document_raw(
            schedule=_daily_schedule(
                times=["09:00"], timezone="UTC", start_date=start.date().isoformat()
            )
        ),
    )[0]["routine_id"]
    _enable(scheduler, routine_id)
    assert next_five(scheduler.routines.plans()[0].revisions, NINE) == []
    horizon = await _pass(scheduler)
    assert horizon is not None and NINE < horizon <= NINE + timedelta(days=SCAN_DAYS - 1)

    passes = _count_passes(scheduler, monkeypatch)
    scheduler.start()
    await _waits(clock, 1)
    connection, reads = store._connection, []

    def spy(*args: Any, **kwargs: Any) -> Any:
        reads.append(kwargs)
        return connection(*args, **kwargs)

    monkeypatch.setattr(store, "_connection", spy)
    for _ in range(10):
        clock.advance(MAX_TICK_S)
        await _tick(clock)
    assert reads == [] and len(passes) == 1

    _move(clock, horizon)
    await _tick(clock)
    assert len(passes) == 2
    _move(clock, start)
    await _tick(clock)
    assert len(passes) == 3
    assert _occurrences(store, routine_id) == [(stamp(start), "claimed", "", 1)]
    assert len(runner.started) == 1
    await _close(scheduler)


async def test_a_backward_clock_correction_keeps_a_future_activation_armed(
    store: SessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock(NINE - timedelta(minutes=1))
    scheduler, runner = _scheduler(store, clock)
    routine_id = _create(scheduler, ["09:00"])
    _enable(scheduler, routine_id)  # no start_date; activation and enable precede the correction
    passes = _count_passes(scheduler, monkeypatch)
    scheduler.start()
    await _waits(clock, 1)

    _jump(clock, -timedelta(days=SCAN_DAYS + 1).total_seconds(), MAX_TICK_S)
    corrected = clock.wall()
    await _tick(clock)  # one clock-jump pass; ordinary ticks must not reread the store
    assert len(passes) == 2 and _occurrences(store, routine_id) == []
    horizon = await _pass(scheduler)
    assert horizon is not None and corrected < horizon <= corrected + timedelta(days=SCAN_DAYS - 1)
    connection, reads = store._connection, []

    def spy(*args: Any, **kwargs: Any) -> Any:
        reads.append(kwargs)
        return connection(*args, **kwargs)

    monkeypatch.setattr(store, "_connection", spy)
    for _ in range(10):
        clock.advance(MAX_TICK_S)
        await _tick(clock)
    assert reads == [] and len(passes) == 3  # includes the direct horizon probe

    _move(clock, horizon)
    await _tick(clock)
    assert len(passes) == 4
    _move(clock, NINE)
    await _tick(clock)
    assert len(passes) == 5
    assert _occurrences(store, routine_id) == [(stamp(NINE), "claimed", "", 1)]
    assert len(runner.started) == 1
    await _close(scheduler)


async def test_future_manual_cutover_does_not_hide_the_daily_slot_it_follows(
    store: SessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock(NINE - timedelta(minutes=1))
    scheduler, runner = _scheduler(store, clock)
    monkeypatch.setattr(scheduler.routines, "readiness", lambda document, *, scheduled: [])
    routine_id = _create(scheduler, ["09:00"])
    with store._connection(write=True) as connection:
        record_tested(connection, routine_id, 1, stamp(clock.wall()))
    state_revision = scheduler.routines.get(routine_id)["state_revision"]
    scheduler.routines.enable(routine_id, _ident(), state_revision)
    _move(clock, NINE + timedelta(seconds=1))
    scheduler.routines.revise(
        routine_id, _ident(), 1, _document_raw(schedule=_schedule("manual", []))
    )
    assert scheduler.routines.get(routine_id)["state"] == "enabled"
    plan = scheduler.routines.plan(routine_id)
    assert [revision.schedule["kind"] for revision in plan.revisions] == ["daily", "manual"]

    clock.advance(-timedelta(days=SCAN_DAYS + 1).total_seconds())
    corrected = clock.wall()
    assert next_five(plan.revisions, corrected) == []
    horizon = await _pass(scheduler)
    assert horizon is not None and corrected < horizon <= corrected + timedelta(days=SCAN_DAYS - 1)

    passes = _count_passes(scheduler, monkeypatch)
    scheduler.start()
    await _waits(clock, 1)
    connection, reads = store._connection, []

    def spy(*args: Any, **kwargs: Any) -> Any:
        reads.append(kwargs)
        return connection(*args, **kwargs)

    monkeypatch.setattr(store, "_connection", spy)
    for _ in range(10):
        clock.advance(MAX_TICK_S)
        await _tick(clock)
    assert reads == [] and len(passes) == 1

    _move(clock, horizon)
    await _tick(clock)
    assert len(passes) == 2
    _move(clock, NINE)
    await _tick(clock)
    assert len(passes) == 3
    assert _occurrences(store, routine_id) == [(stamp(NINE), "claimed", "", 1)]
    assert len(runner.started) == 1 and runner.started[0].revision == 1
    await _close(scheduler)


@pytest.mark.parametrize("start_date", ["9999-12-15", "9999-12-31"])
async def test_upper_start_date_does_not_block_another_due_routine(
    store: SessionStore, start_date: str
) -> None:
    clock = FakeClock(NINE - timedelta(minutes=1))
    scheduler, runner = _scheduler(store, clock)
    future = scheduler.routines.create(
        _ident(),
        _document_raw(
            schedule=_daily_schedule(times=["09:00"], timezone="UTC", start_date=start_date)
        ),
    )[0]["routine_id"]
    _enable(scheduler, future)
    due = _create(scheduler, ["09:00"])
    _enable(scheduler, due)
    _move(clock, NINE)
    assert next_five(scheduler.routines.plan(future).revisions, clock.wall()) == []

    await _pass(scheduler)
    assert _occurrences(store, due) == [(stamp(NINE), "claimed", "", 1)]
    assert _occurrences(store, future) == []
    assert [attempt.routine_id for attempt in runner.started] == [due]

    with store._connection(write=True) as connection:
        connection.execute(
            "UPDATE routines SET state='paused',enabled_since=NULL WHERE routine_id=?", (due,)
        )
    horizon = await _pass(scheduler)
    assert horizon is not None and NINE < horizon <= NINE + timedelta(days=SCAN_DAYS - 1)


async def test_wake_passes_and_close_stops_the_loop(
    store: SessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock(NINE)
    scheduler, _ = _scheduler(store, clock)
    passes = _count_passes(scheduler, monkeypatch)
    scheduler.start()
    assert scheduler.status()["state"] == "running"
    await _waits(clock, 1)
    scheduler.wake()
    await _waits(clock, 2)
    assert len(passes) == 2
    await _close(scheduler)
    assert scheduler.status()["state"] == "stopped" and len(clock.waits) == 2
    idle, idle_runner = _scheduler(store, clock)
    idle_runner.available = False
    idle.start()
    assert idle.status()["state"] == "unavailable"


async def test_an_attended_wait_ends_at_the_next_slot(store: SessionStore) -> None:
    clock = FakeClock(NINE - timedelta(seconds=10))
    scheduler, runner = _scheduler(store, clock)
    _enable(scheduler, _create(scheduler, ["09:00"]))
    scheduler.start()
    await _waits(clock, 1)
    assert clock.waits == [10.0]
    clock.advance(10)
    await _tick(clock)
    with store._connection() as connection:
        claims = connection.execute(
            "SELECT scheduled_for,recorded_at FROM routine_occurrences WHERE state='claimed'"
        ).fetchall()
    assert claims == [(stamp(NINE), stamp(NINE))] and len(runner.started) == 1
    await _close(scheduler)


async def test_an_unattended_loop_waits_for_a_slot_still_ahead(store: SessionStore) -> None:
    attended = True
    clock = FakeClock(NINE - timedelta(seconds=20))
    scheduler, _ = _scheduler(store, clock, lambda: attended)
    routine_id = _create(scheduler, ["09:00"])
    _enable(scheduler, routine_id)
    scheduler.start()
    await _waits(clock, 1)
    attended = False
    clock.advance(10)
    await _tick(clock)
    clock.advance(15)
    await _tick(clock)
    # A slot still ahead is waited for; one already due waits a full tick rather than spinning.
    assert clock.waits == [20.0, 10.0, MAX_TICK_S]
    assert _occurrences(store, routine_id) == []
    await _close(scheduler)


async def test_a_wake_during_a_pass_is_not_lost(
    store: SessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock(NINE)
    scheduler, _ = _scheduler(store, clock)
    read, entered, release = scheduler.routines.plans, threading.Event(), threading.Event()
    passes: list[datetime] = []

    def first_held() -> list[RoutinePlan]:
        passes.append(clock.wall())
        if len(passes) == 1:
            entered.set()
            assert release.wait(HANG_GUARD_S)
        return read()

    monkeypatch.setattr(scheduler.routines, "plans", first_held)
    scheduler.start()
    assert await asyncio.wait_for(asyncio.to_thread(entered.wait, HANG_GUARD_S), HANG_GUARD_S)
    scheduler.wake()
    release.set()
    await _waits(clock, 2)
    assert len(passes) == 2
    await _close(scheduler)


async def _close_inside_the_first_decision(
    scheduler: Scheduler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Start the loop, and close it while its first pass is inside its first decision."""
    decide, entered, release = scheduler._decide, threading.Event(), threading.Event()

    def held(*args: Any) -> Attempt | Literal["future"] | None:
        entered.set()
        assert release.wait(HANG_GUARD_S)
        return decide(*args)

    monkeypatch.setattr(scheduler, "_decide", held)
    scheduler.start()
    assert await asyncio.wait_for(asyncio.to_thread(entered.wait, HANG_GUARD_S), HANG_GUARD_S)
    closing = asyncio.ensure_future(scheduler.close())
    await asyncio.sleep(0)
    release.set()
    await asyncio.wait_for(closing, HANG_GUARD_S)


async def test_close_waits_for_the_decision_it_interrupts(
    store: SessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock(NINE - timedelta(hours=1))
    scheduler, runner = _scheduler(store, clock)
    _enable(scheduler, _create(scheduler, ["09:00"]))
    _move(clock, NINE + timedelta(seconds=1))
    passes = _count_passes(scheduler, monkeypatch)
    await _close_inside_the_first_decision(scheduler, monkeypatch)
    # The claim the pass committed was started, and the loop began no second pass.
    assert len(runner.started) == 1 and len(passes) == 1
    assert scheduler.status()["state"] == "stopped"


async def test_cancelled_close_joins_a_claim_and_repeated_close_is_safe(
    store: SessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock(NINE - timedelta(hours=1))
    scheduler, runner = _scheduler(store, clock)
    routine_id = _create(scheduler, ["09:00"])
    _enable(scheduler, routine_id)
    _move(clock, NINE + timedelta(seconds=1))
    decide, entered, release = scheduler._decide, threading.Event(), threading.Event()

    def held(*args: Any) -> Attempt | Literal["future"] | None:
        entered.set()
        assert release.wait(HANG_GUARD_S)
        return decide(*args)  # commit only after close has been cancelled

    monkeypatch.setattr(scheduler, "_decide", held)
    scheduler.start()
    assert await asyncio.wait_for(asyncio.to_thread(entered.wait, HANG_GUARD_S), HANG_GUARD_S)
    closing = asyncio.create_task(scheduler.close())
    await asyncio.sleep(0)
    closing.cancel()
    await asyncio.sleep(0)
    assert not closing.done()  # cancellation must wait for the worker and handoff
    assert scheduler._loop_task is not None and not scheduler._loop_task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(closing, HANG_GUARD_S)
    assert scheduler._loop_task.done() and not scheduler._loop_task.cancelled()
    assert _occurrences(store, routine_id) == [(stamp(NINE), "claimed", "", 1)]
    assert len(runner.started) == 1 and _attempt_states(store) == ["claimed"]
    await _close(scheduler)
    assert len(runner.started) == 1 and scheduler.status()["state"] == "stopped"


async def test_close_after_a_changed_missed_group_starts_no_fallback_decision(
    store: SessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock(NINE - timedelta(hours=1))
    scheduler, runner = _scheduler(store, clock)
    routine_id = _create(scheduler, ["09:00", "09:01"])
    _enable(scheduler, routine_id)
    _move(clock, NINE + timedelta(minutes=10))
    entered, release = threading.Event(), threading.Event()
    decisions: list[datetime] = []
    decide = scheduler._decide

    def changed_group(*args: Any) -> int:
        entered.set()
        assert release.wait(HANG_GUARD_S)
        return 0

    def observed_decision(rid: str, instant: datetime) -> Attempt | Literal["future"] | None:
        decisions.append(instant)
        return decide(rid, instant)

    monkeypatch.setattr(scheduler, "_decide_missed", changed_group)
    monkeypatch.setattr(scheduler, "_decide", observed_decision)
    scheduler.start()
    try:
        assert await asyncio.wait_for(asyncio.to_thread(entered.wait, HANG_GUARD_S), HANG_GUARD_S)
        closing = asyncio.create_task(scheduler.close())
        await asyncio.sleep(0)
        assert scheduler._closing and not closing.done()
    finally:
        release.set()
    await asyncio.wait_for(closing, HANG_GUARD_S)
    assert decisions == []
    assert _occurrences(store, routine_id) == []
    assert scheduler.routines.get(routine_id)["last_slot"] is None
    assert _attempt_states(store) == [] and runner.started == []


async def test_cancelled_close_joins_the_whole_missed_batch(
    store: SessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock(NINE - timedelta(days=4, hours=1))
    scheduler, runner = _scheduler(store, clock)
    routine_id = _create(scheduler, ["09:00"])
    _enable(scheduler, routine_id)
    _move(clock, NINE + timedelta(seconds=1))
    insert_skip, entered, release = runner.store.insert_skip, threading.Event(), threading.Event()

    def held_skip(*args: Any, **kwargs: Any) -> None:
        entered.set()
        assert release.wait(HANG_GUARD_S)
        insert_skip(*args, **kwargs)

    monkeypatch.setattr(runner.store, "insert_skip", held_skip)
    scheduler.start()
    assert await asyncio.wait_for(asyncio.to_thread(entered.wait, HANG_GUARD_S), HANG_GUARD_S)
    closing = asyncio.create_task(scheduler.close())
    await asyncio.sleep(0)
    assert scheduler._closing
    closing.cancel()
    await asyncio.sleep(0)
    assert not closing.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(closing, HANG_GUARD_S)
    expected = [
        (stamp(NINE - timedelta(days=days)), "skipped", "missed", 1) for days in range(4, 0, -1)
    ]
    assert _occurrences(store, routine_id) == expected
    assert scheduler.routines.get(routine_id)["last_slot"] == expected[-1][0]
    assert runner.started == []


async def test_start_after_close_starts_no_loop(store: SessionStore) -> None:
    clock = FakeClock(NINE)
    scheduler, _ = _scheduler(store, clock)
    scheduler.start()
    await _waits(clock, 1)
    await _close(scheduler)
    loop_task = scheduler._loop_task
    scheduler.start()
    assert scheduler.status()["state"] == "stopped" and scheduler._loop_task is loop_task


# --- Run only if a card waits ---


def _create_gated(scheduler: Scheduler, times: list[str]) -> str:
    raw = _raw(times, run_only_if={"kind": "card", "seat": "research"})
    return scheduler.routines.create(_ident(), raw)[0]["routine_id"]


class FakeKanban:
    """The `card_waiting` seam: records each call and its thread, answers `answer`."""

    def __init__(self, answer: bool | None) -> None:
        self.answer = answer
        self.calls: list[tuple[str, str]] = []
        self.threads: list[int] = []

    def __call__(self, workspace_id: str, seat: str) -> bool | None:
        self.calls.append((workspace_id, seat))
        self.threads.append(threading.get_ident())
        return self.answer


async def test_a_due_slot_without_a_card_is_skipped_no_card_and_starts_nothing(
    store: SessionStore,
) -> None:
    kanban = FakeKanban(False)
    clock = FakeClock(NINE - timedelta(hours=1))
    scheduler, runner = _scheduler(store, clock, card_waiting=kanban)
    routine_id = _create_gated(scheduler, ["09:00"])
    _enable(scheduler, routine_id)
    _move(clock, NINE + timedelta(seconds=1))
    await _pass(scheduler)
    assert _occurrences(store, routine_id) == [(stamp(NINE), "skipped", "no_card", 1)]
    assert runner.started == [] and _attempt_states(store) == []
    assert scheduler.routines.get(routine_id)["last_slot"] == stamp(NINE)
    assert kanban.calls == [(WORKSPACE_ID, "research")]


@pytest.mark.parametrize("answer", [True, None])
async def test_a_waiting_or_undecided_kanban_claims_the_slot(
    store: SessionStore, answer: bool | None
) -> None:
    kanban = FakeKanban(answer)
    clock = FakeClock(NINE - timedelta(hours=1))
    scheduler, runner = _scheduler(store, clock, card_waiting=kanban)
    routine_id = _create_gated(scheduler, ["09:00"])
    _enable(scheduler, routine_id)
    _move(clock, NINE + timedelta(seconds=1))
    await _pass(scheduler)
    assert _occurrences(store, routine_id) == [(stamp(NINE), "claimed", "", 1)]
    assert len(runner.started) == 1 and kanban.calls == [(WORKSPACE_ID, "research")]


@pytest.mark.parametrize("case", ["ungated", "missed", "paused"])
async def test_an_ungated_routine_and_missed_or_paused_slots_read_no_kanban(
    store: SessionStore, case: str
) -> None:
    kanban = FakeKanban(False)
    clock = FakeClock(NINE - timedelta(hours=1))
    settings = _settings(store) if case == "paused" else None
    scheduler, runner = _scheduler(store, clock, settings=settings, card_waiting=kanban)
    if settings is not None:
        _pause_all(settings, True)
    routine_id = (
        _create(scheduler, ["09:00"]) if case == "ungated" else _create_gated(scheduler, ["09:00"])
    )
    _enable(scheduler, routine_id)
    late = timedelta(hours=1) if case == "missed" else timedelta(seconds=1)
    _move(clock, NINE + late)
    await _pass(scheduler)
    state, reason = {
        "ungated": ("claimed", ""),
        "missed": ("skipped", "missed"),
        "paused": ("skipped", "paused"),
    }[case]
    assert _occurrences(store, routine_id) == [(stamp(NINE), state, reason, 1)]
    assert len(runner.started) == (1 if case == "ungated" else 0)
    assert kanban.calls == []


async def test_overlap_and_busy_outrank_no_card(store: SessionStore) -> None:
    kanban = FakeKanban(True)
    clock = FakeClock(NINE - timedelta(hours=1))
    scheduler, runner = _scheduler(store, clock, card_waiting=kanban)
    first = _create_gated(scheduler, ["09:00", "10:00"])
    second = _create(scheduler, ["09:00"])
    third = _create_gated(scheduler, ["09:30"])
    for routine_id in (first, second, third):
        _enable(scheduler, routine_id)
    _move(clock, NINE + timedelta(seconds=1))
    await _pass(scheduler)
    kanban.answer = False  # both runs stay open: the later slots are overlap and busy
    for moment in (NINE + timedelta(minutes=30), NINE + timedelta(hours=1)):
        _move(clock, moment + timedelta(seconds=1))
        await _pass(scheduler)
    assert _occurrences(store, first) == [
        (stamp(NINE), "claimed", "", 1),
        (stamp(NINE + timedelta(hours=1)), "skipped", "overlap", 1),
    ]
    assert _occurrences(store, third) == [
        (stamp(NINE + timedelta(minutes=30)), "skipped", "busy", 1)
    ]
    assert len(kanban.calls) == 3 and len(runner.started) == 2


async def test_the_kanban_read_runs_off_the_event_loop(store: SessionStore) -> None:
    kanban = FakeKanban(False)
    clock = FakeClock(NINE - timedelta(hours=1))
    scheduler, _runner = _scheduler(store, clock, card_waiting=kanban)
    routine_id = _create_gated(scheduler, ["09:00"])
    _enable(scheduler, routine_id)
    _move(clock, NINE + timedelta(seconds=1))
    await _pass(scheduler)
    assert len(kanban.threads) == 1 and kanban.threads[0] != threading.get_ident()


async def test_the_kanban_read_holds_no_admission_lock(store: SessionStore) -> None:
    lock = asyncio.Lock()
    held: list[bool] = []

    def kanban(_workspace_id: str, _seat: str) -> bool | None:
        held.append(lock.locked())
        return False

    clock = FakeClock(NINE - timedelta(hours=1))
    scheduler, _runner = _scheduler(store, clock, admission_lock=lock, card_waiting=kanban)
    routine_id = _create_gated(scheduler, ["09:00"])
    _enable(scheduler, routine_id)
    _move(clock, NINE + timedelta(seconds=1))
    await _pass(scheduler)
    assert held == [False]


async def test_a_kanban_answer_for_another_revision_does_not_skip(
    store: SessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock(NINE - timedelta(hours=1))
    scheduler, runner = _scheduler(store, clock, card_waiting=FakeKanban(False))
    routine_id = _create_gated(scheduler, ["09:00"])
    _enable(scheduler, routine_id)
    monkeypatch.setattr(scheduler, "_gate", lambda _routine_id, _instant: (2, "no_card"))
    _move(clock, NINE + timedelta(seconds=1))
    await _pass(scheduler)
    assert _occurrences(store, routine_id) == [(stamp(NINE), "claimed", "", 1)]
    assert len(runner.started) == 1


async def test_a_failing_kanban_check_runs_the_slot_and_logs_one_fixed_line(
    store: SessionStore, caplog: pytest.LogCaptureFixture
) -> None:
    def failing(_workspace_id: str, _seat: str) -> bool | None:
        raise RuntimeError("vault detail")

    clock = FakeClock(NINE - timedelta(hours=1))
    scheduler, runner = _scheduler(store, clock, card_waiting=failing)
    routine_id = _create_gated(scheduler, ["09:00"])
    _enable(scheduler, routine_id)
    _move(clock, NINE + timedelta(seconds=1))
    with caplog.at_level("WARNING", logger=scheduler_module.__name__):
        await _pass(scheduler)
    assert _occurrences(store, routine_id) == [(stamp(NINE), "claimed", "", 1)]
    assert len(runner.started) == 1
    assert [record.getMessage() for record in caplog.records] == ["routine Kanban check failed"]
    assert "vault detail" not in caplog.text


# --- Run only if a note changed under a folder ---

TEN = NINE + timedelta(hours=1)


def _create_watching(scheduler: Scheduler, times: list[str]) -> str:
    raw = _raw(times, run_only_if={"kind": "changed", "folder": "00-inbox"})
    return scheduler.routines.create(_ident(), raw)[0]["routine_id"]


class FakeFolder:
    """The `notes_changed` seam: records each call and its thread, answers `answer`."""

    def __init__(self, answer: bool | None) -> None:
        self.answer = answer
        self.calls: list[tuple[str, str, datetime]] = []
        self.threads: list[int] = []

    def __call__(self, workspace_id: str, folder: str, since: datetime) -> bool | None:
        self.calls.append((workspace_id, folder, since))
        self.threads.append(threading.get_ident())
        return self.answer


CLAIMED = NINE + timedelta(seconds=1)
ENDED = NINE + timedelta(minutes=5)


async def _first_run_ends(
    scheduler: Scheduler, runner: StubRunner, clock: FakeClock, outcome: Outcome, reason: str
) -> str:
    """A folder routine at 09:00 and 10:00 whose 09:00 run is claimed at `CLAIMED` and ends at
    `ENDED` as `outcome`."""
    routine_id = _create_watching(scheduler, ["09:00", "10:00"])
    _enable(scheduler, routine_id)
    _move(clock, CLAIMED)
    await _pass(scheduler)
    with runner.store.store._connection() as connection:
        (claimed,) = connection.execute("SELECT created_at FROM task_attempts").fetchone()
    assert claimed == stamp(CLAIMED)
    _move(clock, ENDED)
    assert runner.store.finish(runner.started[0].attempt_id, outcome, reason, stamp(ENDED))
    return routine_id


async def test_a_folder_routine_runs_its_first_slot_without_reading_the_vault(
    store: SessionStore,
) -> None:
    folder = FakeFolder(False)
    clock = FakeClock(NINE - timedelta(hours=1))
    scheduler, runner = _scheduler(store, clock, notes_changed=folder)
    routine_id = _create_watching(scheduler, ["09:00"])
    _enable(scheduler, routine_id)
    _move(clock, NINE + timedelta(seconds=1))
    await _pass(scheduler)
    assert _occurrences(store, routine_id) == [(stamp(NINE), "claimed", "", 1)]
    assert len(runner.started) == 1 and folder.calls == []


@pytest.mark.parametrize(("outcome", "reason"), [("succeeded", ""), ("blocked", "needs_decision")])
async def test_no_change_since_the_last_completed_run_skips_and_starts_nothing(
    store: SessionStore, outcome: Outcome, reason: str
) -> None:
    folder = FakeFolder(False)
    clock = FakeClock(NINE - timedelta(hours=1))
    scheduler, runner = _scheduler(store, clock, notes_changed=folder)
    routine_id = await _first_run_ends(scheduler, runner, clock, outcome, reason)
    _move(clock, TEN + timedelta(seconds=1))
    await _pass(scheduler)
    assert _occurrences(store, routine_id) == [
        (stamp(NINE), "claimed", "", 1),
        (stamp(TEN), "skipped", "no_change", 1),
    ]
    assert len(runner.started) == 1
    assert folder.calls == [(WORKSPACE_ID, "00-inbox", CLAIMED)]
    assert folder.threads[0] != threading.get_ident()


@pytest.mark.parametrize("answer", [True, None])
async def test_a_changed_or_unknown_folder_claims_the_slot(
    store: SessionStore, answer: bool | None
) -> None:
    folder = FakeFolder(answer)
    clock = FakeClock(NINE - timedelta(hours=1))
    scheduler, runner = _scheduler(store, clock, notes_changed=folder)
    routine_id = await _first_run_ends(scheduler, runner, clock, "succeeded", "")
    _move(clock, TEN + timedelta(seconds=1))
    await _pass(scheduler)
    assert _occurrences(store, routine_id)[-1] == (stamp(TEN), "claimed", "", 1)
    assert len(runner.started) == 2 and len(folder.calls) == 1


@pytest.mark.parametrize(
    ("outcome", "reason"),
    [("failed", "internal"), ("expired", "deadline"), ("blocked", "quota"), ("cancelled", "")],
)
async def test_a_run_that_did_not_complete_its_work_is_no_baseline(
    store: SessionStore, outcome: Outcome, reason: str
) -> None:
    folder = FakeFolder(False)
    clock = FakeClock(NINE - timedelta(hours=1))
    scheduler, runner = _scheduler(store, clock, notes_changed=folder)
    routine_id = await _first_run_ends(scheduler, runner, clock, outcome, reason)
    _move(clock, TEN + timedelta(seconds=1))
    await _pass(scheduler)
    assert _occurrences(store, routine_id)[-1] == (stamp(TEN), "claimed", "", 1)
    assert folder.calls == []


async def test_a_run_of_an_earlier_revision_is_no_baseline(store: SessionStore) -> None:
    folder = FakeFolder(False)
    clock = FakeClock(NINE - timedelta(hours=1))
    scheduler, runner = _scheduler(store, clock, notes_changed=folder)
    routine_id = await _first_run_ends(scheduler, runner, clock, "succeeded", "")
    _move(clock, NINE + timedelta(minutes=30))
    raw = _raw(["09:00", "10:00"], run_only_if={"kind": "changed", "folder": "40-sources"})
    scheduler.routines.revise(routine_id, _ident(), 1, raw)
    _move(clock, TEN + timedelta(seconds=1))
    await _pass(scheduler)
    assert _occurrences(store, routine_id)[-1] == (stamp(TEN), "claimed", "", 2)
    assert folder.calls == []


async def test_the_latest_completed_run_of_any_kind_is_the_baseline(store: SessionStore) -> None:
    folder = FakeFolder(False)
    clock = FakeClock(NINE - timedelta(hours=1))
    scheduler, runner = _scheduler(store, clock, notes_changed=folder)
    routine_id = await _first_run_ends(scheduler, runner, clock, "succeeded", "")
    later = NINE + timedelta(minutes=30)
    _move(clock, later)
    with store._connection(write=True) as connection:
        manual = runner.store.insert_claim(
            connection,
            routine_id=routine_id,
            kind="manual",
            revision=1,
            now=stamp(clock.wall()),
            trigger_id=_ident(),
            request_hash="ab" * 32,
        )
    assert runner.store.finish(
        manual.attempt_id, "succeeded", "", stamp(later + timedelta(minutes=9))
    )
    _move(clock, TEN + timedelta(seconds=1))
    await _pass(scheduler)
    assert _occurrences(store, routine_id)[-1] == (stamp(TEN), "skipped", "no_change", 1)
    assert [call[2] for call in folder.calls] == [later]


async def test_a_card_routine_never_reads_the_folder_and_a_folder_routine_no_kanban(
    store: SessionStore,
) -> None:
    kanban, folder = FakeKanban(False), FakeFolder(False)
    clock = FakeClock(NINE - timedelta(hours=1))
    scheduler, runner = _scheduler(store, clock, card_waiting=kanban, notes_changed=folder)
    carded = _create_gated(scheduler, ["10:00"])
    _enable(scheduler, carded)
    watching = await _first_run_ends(scheduler, runner, clock, "succeeded", "")
    _move(clock, TEN + timedelta(seconds=1))
    await _pass(scheduler)
    assert _occurrences(store, carded) == [(stamp(TEN), "skipped", "no_card", 1)]
    assert _occurrences(store, watching)[-1] == (stamp(TEN), "skipped", "no_change", 1)
    assert kanban.calls == [(WORKSPACE_ID, "research")] and len(folder.calls) == 1


async def test_without_the_folder_seam_a_folder_routine_runs(store: SessionStore) -> None:
    clock = FakeClock(NINE - timedelta(hours=1))
    scheduler, runner = _scheduler(store, clock, card_waiting=FakeKanban(False))
    routine_id = await _first_run_ends(scheduler, runner, clock, "succeeded", "")
    _move(clock, TEN + timedelta(seconds=1))
    await _pass(scheduler)
    assert _occurrences(store, routine_id)[-1] == (stamp(TEN), "claimed", "", 1)


async def test_a_failing_folder_check_runs_the_slot_and_logs_one_fixed_line(
    store: SessionStore, caplog: pytest.LogCaptureFixture
) -> None:
    def failing(_workspace_id: str, _folder: str, _since: datetime) -> bool | None:
        raise RuntimeError("vault detail")

    clock = FakeClock(NINE - timedelta(hours=1))
    scheduler, runner = _scheduler(store, clock, notes_changed=failing)
    routine_id = await _first_run_ends(scheduler, runner, clock, "succeeded", "")
    _move(clock, TEN + timedelta(seconds=1))
    with caplog.at_level("WARNING", logger=scheduler_module.__name__):
        await _pass(scheduler)
    assert _occurrences(store, routine_id)[-1] == (stamp(TEN), "claimed", "", 1)
    assert [record.getMessage() for record in caplog.records] == ["routine folder check failed"]
    assert "vault detail" not in caplog.text


@pytest.mark.parametrize(("minutes", "state"), [(-1, "skipped"), (3, "claimed")])
async def test_over_a_real_listing_a_note_changed_while_the_last_run_went_on_counts(
    store: SessionStore, tmp_path: Path, minutes: int, state: str
) -> None:
    """A capture made between the 09:00 run's claim and its end runs the 10:00 slot; one made
    before that claim does not."""
    note = tmp_path / "vault" / "00-inbox" / "captured.md"
    note.parent.mkdir(parents=True)
    note.write_text("# Captured\n", encoding="utf-8")
    at = (NINE + timedelta(minutes=minutes)).timestamp()
    os.utime(note, (at, at))
    clock = FakeClock(NINE - timedelta(hours=1))
    reader = functools.partial(notes_changed, vault_service(tmp_path / "vault"))
    scheduler, runner = _scheduler(store, clock, notes_changed=reader)
    routine_id = await _first_run_ends(scheduler, runner, clock, "succeeded", "")
    _move(clock, TEN + timedelta(seconds=1))
    await _pass(scheduler)
    reason = "no_change" if state == "skipped" else ""
    assert _occurrences(store, routine_id)[-1] == (stamp(TEN), state, reason, 1)
