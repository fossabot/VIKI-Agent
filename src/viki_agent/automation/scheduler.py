"""Routine schedules: pure slot math over ``zoneinfo``, the one in-process clock, and the
scheduler that decides each due instant once.

Nothing here starts a process; a claimed attempt is handed to the task runner.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sqlite3
import time as time_module
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, timedelta
from datetime import time as clock_time
from functools import partial
from typing import TYPE_CHECKING, Any, Literal, Protocol
from zoneinfo import ZoneInfo

from viki_agent import protection
from viki_agent.automation.routines import (
    RevisionPlan,
    RoutineService,
    parse_schedule,
    parse_stamp,
    stamp,
)
from viki_agent.conversations.command_runtime import storage_call
from viki_agent.storage import StateError

if TYPE_CHECKING:
    from viki_agent.automation.tasks import Attempt, TaskStore

logger = logging.getLogger(__name__)

MAX_TICK_S = 30.0
ON_TIME = timedelta(minutes=5)
MISFIRE_WINDOW = timedelta(days=7)
JUMP_S = 2.0
SCAN_DAYS = 400

_Decision = Literal["claim", "missed", "paused"]
# A routine condition's answer that nothing waits: the governing revision and its skip reason.
_Gate = tuple[int, Literal["no_card", "no_change"]]


class Clock(Protocol):
    def wall(self) -> datetime: ...

    def monotonic(self) -> float: ...

    async def wait(self, event: asyncio.Event, seconds: float) -> None: ...


class SystemClock:
    """The real clock: wall time, a monotonic counter, and a cancellable wait."""

    def wall(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time_module.monotonic()

    async def wait(self, event: asyncio.Event, seconds: float) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(event.wait(), max(seconds, 0.0))


def _local_slot(day: date, hhmm: str, zone: ZoneInfo) -> tuple[datetime, bool, str]:
    """One local wall time as a ``(basis, is_gap, note)`` triple.

    ``basis`` is the ``fold=0`` UTC instant, computed even for a gap, so callers can place
    a gap in time (which revision governs it, whether it is strictly after "now") without
    ever reporting it as a real instant.
    """
    local = datetime.combine(day, clock_time(int(hhmm[:2]), int(hhmm[3:])), tzinfo=zone)
    basis = local.astimezone(UTC)
    is_gap = basis.astimezone(zone).replace(tzinfo=None) != local.replace(tzinfo=None)
    if is_gap:
        return basis, True, "dst_gap"
    if local.replace(fold=1).astimezone(UTC) != basis:
        return basis, False, "dst_fold"
    return basis, False, ""


def _bounds(schedule: dict[str, Any]) -> tuple[date | None, date | None, set[int] | None]:
    start_date = date.fromisoformat(schedule["start_date"]) if schedule["start_date"] else None
    end_date = date.fromisoformat(schedule["end_date"]) if schedule["end_date"] else None
    days = set(schedule["days"]) if schedule["kind"] == "weekly" else None
    return start_date, end_date, days


def _governs(ordered: Sequence[RevisionPlan], index: int) -> tuple[datetime, datetime | None]:
    governs_from = parse_stamp(ordered[index].activates_at)
    governs_to = parse_stamp(ordered[index + 1].activates_at) if index + 1 < len(ordered) else None
    return governs_from, governs_to


def slots(
    revisions: Sequence[RevisionPlan], lo: datetime, hi: datetime
) -> list[tuple[datetime, int]]:
    """Every real slot in ``[lo, hi]``, ascending ``(instant, revision)``; never a gap."""
    ordered = sorted(revisions, key=lambda plan: plan.revision)
    result: list[tuple[datetime, int]] = []
    local_slots: dict[tuple[str, date, str], tuple[datetime, bool, str]] = {}
    for index, plan in enumerate(ordered):
        schedule = plan.schedule
        if schedule["kind"] == "manual":
            continue
        governs_from, governs_to = _governs(ordered, index)
        if (governs_to is not None and governs_to <= lo) or governs_from > hi:
            continue
        zone = ZoneInfo(schedule["timezone"])
        start_date, end_date, days = _bounds(schedule)
        # A midnight offset rollback can visit the next local date inside a UTC window whose
        # endpoints both show the previous date. Keep one adjacent date on either side.
        first = max(lo, governs_from)
        last = min(hi, governs_to) if governs_to is not None else hi
        day = first.astimezone(zone).date()
        if day > date.min:
            day -= timedelta(days=1)
        last_day = last.astimezone(zone).date()
        if last_day < date.max:
            last_day += timedelta(days=1)
        while day <= last_day:
            in_range = (start_date is None or day >= start_date) and (
                end_date is None or day <= end_date
            )
            if in_range and (days is None or day.isoweekday() in days):
                for hhmm in schedule["times"]:
                    try:
                        key = (schedule["timezone"], day, hhmm)
                        local = local_slots.get(key)
                        if local is None:
                            local = _local_slot(day, hhmm, zone)
                            local_slots[key] = local
                        basis, is_gap, _note = local
                    except OverflowError:
                        continue  # a local slot beyond datetime's UTC limit is not representable
                    if (
                        not is_gap
                        and lo <= basis <= hi
                        and basis >= governs_from
                        and (governs_to is None or basis < governs_to)
                    ):
                        result.append((basis, plan.revision))
            if day == last_day:
                break
            day += timedelta(days=1)
    result.sort(key=lambda item: (item[0], item[1]))
    return result


def next_five(revisions: Sequence[RevisionPlan], now: datetime) -> list[dict[str, Any]]:
    """The next five slots strictly after ``now``; a gap day is listed but never counted."""
    ordered = sorted(revisions, key=lambda plan: plan.revision)
    items: list[dict[str, Any]] = []
    counted = 0
    for index, plan in enumerate(ordered):
        if counted >= 5:
            break
        schedule = plan.schedule
        if schedule["kind"] == "manual":
            continue
        governs_from, governs_to = _governs(ordered, index)
        if governs_to is not None and governs_to <= now:
            continue
        zone = ZoneInfo(schedule["timezone"])
        start_date, end_date, days = _bounds(schedule)
        day = now.astimezone(zone).date()
        for _ in range(SCAN_DAYS):
            if counted >= 5 or (end_date is not None and day > end_date):
                break
            in_range = start_date is None or day >= start_date
            if in_range and (days is None or day.isoweekday() in days):
                for hhmm in schedule["times"]:
                    try:
                        basis, is_gap, note = _local_slot(day, hhmm, zone)
                    except OverflowError:
                        continue
                    if basis <= now or basis < governs_from:
                        continue
                    if governs_to is not None and basis >= governs_to:
                        continue
                    if is_gap:
                        items.append(
                            {
                                "at": None,
                                "local": f"{day.isoformat()}T{hhmm}",
                                "zone": schedule["timezone"],
                                "offset_minutes": None,
                                "note": "dst_gap",
                            }
                        )
                        continue
                    local_resolved = basis.astimezone(zone)
                    offset = local_resolved.utcoffset()
                    assert offset is not None
                    items.append(
                        {
                            "at": stamp(basis),
                            "local": local_resolved.isoformat(timespec="minutes"),
                            "zone": schedule["timezone"],
                            "offset_minutes": int(offset.total_seconds()) // 60,
                            "note": note,
                        }
                    )
                    counted += 1
                    if counted >= 5:
                        break
            if day == date.max:
                break
            day += timedelta(days=1)
    return items


class Runner(Protocol):
    """What the scheduler uses of the task runner: its store, whether it can run, and starting a
    claimed attempt."""

    store: TaskStore

    @property
    def available(self) -> bool: ...

    def start(self, attempt: Attempt) -> None: ...


class _MissBatchChanged(Exception):
    """A selected historical slot no longer belongs in the missed group."""


class Scheduler:
    """The one in-process scheduler: it decides each due instant of an enabled routine once."""

    def __init__(
        self,
        routines: RoutineService,
        runner: Runner,
        clock: Clock,
        attended: Callable[[], bool],
        *,
        admission_lock: asyncio.Lock | None = None,
        card_waiting: Callable[[str, str], bool | None] | None = None,
        notes_changed: Callable[[str, str, datetime], bool | None] | None = None,
    ) -> None:
        self.routines = routines
        self.runner = runner
        self.clock = clock
        self.attended = attended
        self.card_waiting = card_waiting
        self.notes_changed = notes_changed
        self._admission_lock = admission_lock if admission_lock is not None else asyncio.Lock()
        self.state: Literal["running", "stopped", "unavailable"] = "unavailable"
        self._changed = True
        self._event = asyncio.Event()
        self._closing = False
        self._loop_task: asyncio.Task[None] | None = None

    def status(self) -> dict[str, Any]:
        return {"state": self.state, "attended": self.attended(), "paused": self._paused()}

    def _paused(self) -> bool:
        """Pause all: an untrusted settings document counts as paused (fail closed)."""
        settings = self.routines.settings
        if settings is None:
            return False
        paused = settings.flag("routines.paused")
        return True if paused is None else paused

    def wake(self) -> None:
        """Mark that a routine changed, and set the scheduler's event."""
        self._changed = True
        self._event.set()

    def start(self) -> None:
        # A closed scheduler stays closed: a second start would report "running" with no loop.
        if self.runner.available and not self._closing:
            self.state = "running"
            self._loop_task = asyncio.create_task(self._loop())

    async def close(self) -> None:
        self.state = "stopped"
        self._closing = True
        self._event.set()
        if self._loop_task is None:
            return
        # A canceled shutdown waiter must still join the pass and its committed handoff.
        cancelled = False
        while True:
            try:
                await asyncio.shield(self._loop_task)
                break
            except asyncio.CancelledError:
                if self._loop_task.cancelled():
                    raise
                cancelled = True
        if cancelled:
            raise asyncio.CancelledError

    async def _loop(self) -> None:
        next_due: datetime | None = None
        last: tuple[datetime, float] | None = None
        while not self._closing:
            wall, mono = self.clock.wall(), self.clock.monotonic()
            jumped = (
                last is not None
                and abs((wall - last[0]).total_seconds() - (mono - last[1])) > JUMP_S
            )
            last = (wall, mono)
            attended = self.attended()
            if attended and (
                self._changed or (next_due is not None and wall >= next_due) or jumped
            ):
                self._changed = False
                try:
                    next_due = await self.pass_once()
                except Exception:
                    logger.warning("routine pass unavailable")
                    next_due = wall + timedelta(seconds=MAX_TICK_S)
            seconds = MAX_TICK_S
            # Unattended, a slot still ahead is waited for, so a window that reopens just before
            # it claims on time; a slot already due waits a full tick, so the loop never spins
            # without a window.
            if next_due is not None:
                remaining = (next_due - self.clock.wall()).total_seconds()
                if attended or remaining > 0:
                    seconds = min(MAX_TICK_S, max(0.0, remaining))
            await self.clock.wait(self._event, seconds)
            self._event.clear()

    async def pass_once(self) -> datetime | None:
        """Decide every due instant once; answer the earliest next one, or None.

        After a busy store it answers at most one tick ahead, to decide the instant it left.
        """
        now = self.clock.wall()
        candidates, due = await asyncio.to_thread(self._plan, now)
        blocked_routine: str | None = None
        fallback_routines: set[str] = set()
        index = 0
        while index < len(candidates):
            routine_id, instant, decision = candidates[index]
            index += 1
            if routine_id == blocked_routine:
                continue  # a busy older slot must be retried before this routine advances
            # Consecutive old slots of one routine, missed or paused, share one transaction.
            old: list[datetime] = []
            if decision != "claim":
                for candidate_id, candidate_at, candidate_decision in candidates[index - 1 :]:
                    if (
                        candidate_id != routine_id
                        or candidate_decision != decision
                        or now - candidate_at <= ON_TIME
                    ):
                        break
                    old.append(candidate_at)
            # Off the loop like `_plan`; outside the lock, so a slow vault holds nothing.
            gate = (
                await asyncio.to_thread(self._gate, routine_id, instant)
                if decision == "claim"
                and (self.card_waiting is not None or self.notes_changed is not None)
                and not self._closing
                else None
            )
            try:
                async with self._admission_lock:
                    if (
                        self._closing
                        or not self.runner.available
                        # TaskRunner.close marks this under the same gate; test runners may not.
                        or getattr(self.runner, "_closing", False)
                    ):
                        return None
                    if not self.attended():
                        # A window may reopen during grace without a scheduler wake.
                        self._changed = True
                        return None
                    if len(old) > 1 and routine_id not in fallback_routines:
                        reason = "paused" if decision == "paused" else "missed"
                        processed = await storage_call(
                            partial(self._decide_missed, routine_id, old, reason)
                        )
                        if processed:
                            index += processed - 1
                            continue
                        if self._closing:
                            return None
                        fallback_routines.add(routine_id)
                    outcome = await storage_call(
                        partial(self._decide, routine_id, instant, decision == "paused", gate),
                        lambda result: (
                            self.runner.start(result)
                            if result is not None and result != "future"
                            else None
                        ),
                    )
                    if outcome == "future":
                        # The joined decision left this slot unwatermarked, even if time crossed
                        # it before the result reached this event-loop pass.
                        due.append(stamp(instant))
                        blocked_routine = routine_id
            except StateError as err:
                if err.code != "busy":
                    raise
                due.append(stamp(now + timedelta(seconds=MAX_TICK_S)))
                blocked_routine = routine_id
        return parse_stamp(min(due)) if due else None

    def _decide_missed(
        self, routine_id: str, instants: list[datetime], reason: Literal["missed", "paused"]
    ) -> int:
        """Record consecutive old slots in order, skipped for ``reason``, with one durable
        transaction per routine."""
        tasks = self.runner.store
        try:
            with tasks.store._connection(write=True) as connection:
                row = connection.execute(
                    "SELECT state,last_slot FROM routines WHERE routine_id=?", (routine_id,)
                ).fetchone()
                if row is None or row[0] != "enabled":
                    raise _MissBatchChanged
                last_slot = row[1]
                for instant in instants:
                    now = self.clock.wall()
                    if now - instant <= ON_TIME:
                        raise _MissBatchChanged  # recheck a clock correction singly
                    slot = stamp(instant)
                    if last_slot is not None and last_slot >= slot:
                        raise _MissBatchChanged
                    revision = self._revision(connection, routine_id, instant, slot)
                    if revision is None:
                        raise _MissBatchChanged
                    tasks.insert_skip(
                        connection,
                        routine_id=routine_id,
                        scheduled_for=slot,
                        revision=revision,
                        reason=reason,
                        now=stamp(now),
                    )
                    connection.execute(
                        "UPDATE routines SET last_slot=? WHERE routine_id=?",
                        (slot, routine_id),
                    )
                    last_slot = slot
                tasks.prune(connection, routine_id)
        except _MissBatchChanged:
            return 0
        except StateError as err:
            if err.code != "invalid_input":
                raise
            return 0  # retry singly after the batch rolls back
        return len(instants)

    def _revision(
        self, connection: sqlite3.Connection, routine_id: str, instant: datetime, slot: str
    ) -> int | None:
        # Recheck against current revisions; a plan may be stale after an edit.
        revisions = [
            RevisionPlan(revision, activates_at, parse_schedule(json.loads(schedule)))
            for revision, activates_at, schedule in connection.execute(
                "SELECT revision,activates_at,schedule FROM routine_revisions "
                "WHERE routine_id=? AND revision >= (SELECT max(revision) "
                "FROM routine_revisions WHERE routine_id=? AND activates_at <= ?) "
                "ORDER BY revision LIMIT 2",
                (routine_id, routine_id, slot),
            )
        ]
        governing = slots(revisions, instant, instant)
        return governing[0][1] if len(governing) == 1 else None

    def _gate(self, routine_id: str, instant: datetime) -> _Gate | None:
        """The governing revision and its skip reason when the routine's condition answered that
        nothing waits: no todo card for its seat (`no_card`), or no note changed under its folder
        since its latest completed run at that revision was claimed (`no_change`). Else None, so an
        ungated routine, an unreadable revision or stamp, a folder routine with no completed run
        at that revision, an undecided vault or a failed check claims."""
        try:
            with self.runner.store.store._connection() as connection:
                revision = self._revision(connection, routine_id, instant, stamp(instant))
            if revision is None:
                return None
            document = self.routines.frozen(routine_id, revision)
        except ValueError, StateError, protection.ProtectionError:
            return None
        condition = document.get("run_only_if")
        if condition is None:
            return None
        if condition["kind"] == "card":
            if self.card_waiting is None:
                return None
            try:
                waiting = self.card_waiting(document["workspace_id"], condition["seat"])
            except Exception:
                logger.warning("routine Kanban check failed")
                return None
            return (revision, "no_card") if waiting is False else None
        if self.notes_changed is None:
            return None
        try:
            with self.runner.store.store._connection() as connection:
                claimed = self.runner.store.last_completed_start(connection, routine_id, revision)
            if claimed is None:
                return None
            since = parse_stamp(claimed)
        except ValueError, StateError:
            return None
        try:
            changed = self.notes_changed(document["workspace_id"], condition["folder"], since)
        except Exception:
            logger.warning("routine folder check failed")
            return None
        return (revision, "no_change") if changed is False else None

    def _plan(self, now: datetime) -> tuple[list[tuple[str, datetime, _Decision]], list[str]]:
        # Pause all is read once per pass: a pause landing mid-pass may let this pass's remaining
        # claims proceed, since a read inside each decision would couple it to the settings table.
        paused = self._paused()
        plans = self.routines.plans()
        due: list[str] = []
        urgent: list[tuple[str, datetime, _Decision]] = []
        backlog: list[tuple[str, datetime, _Decision]] = []
        for plan in plans:
            preview = next_five(plan.revisions, now)
            upcoming = [item["at"] for item in preview if isinstance(item["at"], str)]
            due.extend(upcoming)
            if not upcoming:
                # A past revision can still govern a future slot before a later manual cutover.
                for index, revision in enumerate(plan.revisions):
                    schedule = revision.schedule
                    if schedule["kind"] == "manual":
                        continue
                    governs_from, governs_to = _governs(plan.revisions, index)
                    first = max(now, parse_stamp(plan.enabled_since), governs_from)
                    start_date, end_date, _days = _bounds(schedule)
                    zone = ZoneInfo(schedule["timezone"])
                    if start_date is not None:
                        first = max(
                            first,
                            datetime.combine(start_date, clock_time.min, tzinfo=zone).astimezone(
                                UTC
                            ),
                        )
                    if (governs_to is not None and first >= governs_to) or (
                        end_date is not None and first.astimezone(zone).date() > end_date
                    ):
                        continue
                    # Daily/weekly schedules show a real slot within two weeks even if the first
                    # week's local time falls in a DST gap. The revision bounds still govern it.
                    try:
                        last_local = datetime.combine(
                            date.max, clock_time.max, tzinfo=zone
                        ).astimezone(UTC)
                    except OverflowError:
                        last_local = datetime.max.replace(tzinfo=UTC)
                    if first > last_local:
                        continue
                    search_end = first + min(timedelta(days=15), last_local - first)
                    if governs_to is not None:
                        search_end = min(search_end, governs_to)
                    if any(
                        scheduled_revision == revision.revision
                        for _instant, scheduled_revision in slots(plan.revisions, first, search_end)
                    ):
                        due.append(stamp(now + timedelta(days=SCAN_DAYS - 1)))
                        break
            lo = max(parse_stamp(plan.enabled_since), now - MISFIRE_WINDOW)
            if plan.last_slot is not None:
                lo = max(lo, parse_stamp(plan.last_slot) + timedelta(milliseconds=1))
            candidates: list[tuple[str, datetime, _Decision]] = []
            for instant, _revision in slots(plan.revisions, lo, now):
                decision: _Decision = (
                    "paused" if paused else "claim" if now - instant <= ON_TIME else "missed"
                )
                candidates.append((plan.routine_id, instant, decision))
            # Due routines precede other routines' misfires, preserving each watermark order.
            (urgent if any(item[2] == "claim" for item in candidates) else backlog).extend(
                candidates
            )
        return urgent + backlog, due

    def _decide(
        self,
        routine_id: str,
        instant: datetime,
        paused: bool = False,
        gate: _Gate | None = None,
    ) -> Attempt | Literal["future"] | None:
        """One write transaction: record the instant as skipped, or claim it with its attempt.

        While paused, an instant is recorded skipped as "paused", whether due or old. An instant
        it would otherwise claim is skipped with `gate`'s reason ("no_card" or "no_change") when
        `gate` names its governing revision (`_gate`'s answer)."""
        tasks = self.runner.store
        slot = stamp(instant)
        try:
            with tasks.store._connection(write=True) as connection:
                row = connection.execute(
                    "SELECT state,last_slot FROM routines WHERE routine_id=?", (routine_id,)
                ).fetchone()
                if row is None or row[0] != "enabled" or (row[1] is not None and row[1] >= slot):
                    return None
                revision = self._revision(connection, routine_id, instant, slot)
                if revision is None:
                    return None
                now = self.clock.wall()  # pass-start time never authorizes a claim
                if now < instant:
                    return "future"
                recorded = stamp(now)
                skip = (
                    "paused"
                    if paused
                    else "missed"
                    if now - instant > ON_TIME
                    else tasks.occupancy(connection, routine_id)
                )
                if skip is None and gate is not None and gate[0] == revision:
                    skip = gate[1]
                attempt = None
                if skip is None:
                    attempt = tasks.insert_claim(
                        connection,
                        routine_id=routine_id,
                        kind="schedule",
                        revision=revision,
                        now=recorded,
                        scheduled_for=slot,
                    )
                else:
                    tasks.insert_skip(
                        connection,
                        routine_id=routine_id,
                        scheduled_for=slot,
                        revision=revision,
                        reason="overlap" if skip == "run_active" else skip,
                        now=recorded,
                    )
                connection.execute(
                    "UPDATE routines SET last_slot=? WHERE routine_id=?", (slot, routine_id)
                )
                tasks.prune(connection, routine_id)
        except StateError as err:
            # Another claimer won the instant (its unique key): nothing this pass. A busy store
            # reaches the pass, which leaves the instant undecided.
            if err.code != "invalid_input":
                raise
            return None
        return attempt
