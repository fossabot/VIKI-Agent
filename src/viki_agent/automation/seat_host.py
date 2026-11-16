"""The routine runner's side of Decision's seat port: what the store holds of one vault's routines,
their runs and the team's counts, and starting and stopping a seat's run.

Every read fails closed: a routine whose sealed body does not decode, or a run row with a bad
stamp, leaves `complete` false, and the planner then starts and offers nothing, since a missing
window could make a card the team moved look like the user's. Past `MAX_WINDOWS` windows the
read stops at a finish time, and one window from the asked start to that finish stands for every
run it left out, so a card changed then is never taken for the user's while a later one still
is.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any

from anyio.to_thread import run_sync

from viki_agent import protection
from viki_agent.automation.routines import (
    MAX_ROUTINES,
    RoutineError,
    decode_revision,
    parse_stamp,
    stamp,
)
from viki_agent.automation.tasks import TaskRunner
from viki_agent.conversations.drafts import require_private_store
from viki_agent.decision_loop.contracts import (
    DecisionLoopError,
    SeatFacts,
    SeatRoutine,
    SeatRun,
    SeatTeam,
    SeatWaiting,
)
from viki_agent.storage import StateError

if TYPE_CHECKING:
    from viki_agent.automation.scheduler import Scheduler
    from viki_agent.session import SessionManager

MAX_WINDOWS = 5000
MAX_SESSIONS = 50  # as `wire.MAX_STARTED`: the launcher shows no more

log = logging.getLogger(__name__)

# What the runner refuses with, as Decision names it; any other refusal is `not_applicable`.
_START_REFUSALS = {
    "limit": "limit",
    "run_active": "busy",
    "busy": "busy",
    "unavailable": "unavailable",
}

_ATTEMPTS = "FROM task_attempts a JOIN routine_occurrences o ON o.occurrence_id=a.origin_id "
_RUN_COLUMNS = (
    "SELECT a.attempt_id,o.routine_id,o.kind,a.session_id,a.created_at,a.dispatched_at,"
    "a.finished_at,a.outcome,a.reason " + _ATTEMPTS
)
# The order `TaskStore.newest` uses, which a decision claim compares.
_NEWEST_FIRST = "ORDER BY a.created_at DESC,a.attempt_id DESC "
# The shape of a store stamp (`routines.stamp`), whose text order is its time order.
_STAMP_SHAPE = (
    "[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z"
)
# Every routine attempt and Decision mission chat open or finished at or after ?1, a row with a
# misshapen stamp first (it is parsed, so it fails the read), then the open ones, then by finish,
# latest first: whatever ?3 leaves out ended no later than the first row it leaves out.
_WINDOWS = (
    "SELECT id,routine_id,kind,session_id,created_at,dispatched_at,finished_at,outcome,reason "
    "FROM (SELECT a.attempt_id AS id,o.routine_id AS routine_id,o.kind AS kind,"
    "a.session_id AS session_id,a.created_at AS created_at,a.dispatched_at AS dispatched_at,"
    "a.finished_at AS finished_at,a.outcome AS outcome,a.reason AS reason "
    + _ATTEMPTS
    + "WHERE a.finished_at IS NULL OR a.finished_at>=?1 "
    "UNION ALL SELECT mission_id,'','mission',session_id,created_at,1,finished_at,NULL,'' "
    "FROM decision_missions WHERE finished_at IS NULL OR finished_at>=?1) "
    "ORDER BY (created_at NOT GLOB ?2 OR (finished_at IS NOT NULL AND finished_at NOT GLOB ?2)) "
    "DESC,finished_at IS NULL DESC,finished_at DESC,created_at DESC,id DESC LIMIT ?3"
)


class SeatHost:
    """`contracts.SeatHost` over the runner's store: the reads, and the starts and stops of
    decision runs, which `start`, `stop` and `stop_all` run on the event loop."""

    def __init__(self, runner: TaskRunner, scheduler: Scheduler, manager: SessionManager) -> None:
        self.runner, self.scheduler, self.manager = runner, scheduler, manager

    def facts(self, workspace_id: str, since: datetime | None, day: str) -> SeatFacts:
        tasks = self.runner.store
        bounds = _bounds(day)
        require_private_store(tasks.store)
        with tasks.store._connection() as connection:
            routines, complete = _vault_routines(connection, workspace_id)
            runs, windowed = _windows(connection, since)
            newest: dict[str, SeatRun] = {}
            for routine in routines:
                attempt_id = tasks.newest(connection, routine.routine_id)
                if attempt_id is None:
                    continue
                row = connection.execute(
                    _RUN_COLUMNS + "WHERE a.attempt_id=?", (attempt_id,)
                ).fetchone()
                run = None if row is None else _run(row)
                if run is None:
                    complete = False
                else:
                    newest[routine.routine_id] = run
            (open_runs,) = connection.execute(
                "SELECT count(*) FROM task_attempts WHERE state<>'done'"
            ).fetchone()
            today = tasks.team_count(connection, *bounds)
        return SeatFacts(
            routines=routines,
            runs=runs,
            newest=newest,
            # VIKI's own start takes a slot only while no routine run is going, as its claim
            # checks (`TaskStore.claim_decision`), so a scheduled slot finds the other one free.
            free=0 if open_runs else 1,
            today=today,
            paused=self.scheduler._paused(),
            complete=complete and windowed,
        )

    def team(self, day: str, since: datetime | None) -> SeatTeam:
        tasks = self.runner.store
        bounds = _bounds(day)
        require_private_store(tasks.store)
        with tasks.store._connection() as connection:
            today = tasks.team_count(connection, *bounds)
            (running,) = connection.execute(
                "SELECT count(*) " + _ATTEMPTS + "WHERE o.kind='decision' AND a.state<>'done'"
            ).fetchone()
            sessions = (
                ()
                if since is None
                else tuple(
                    session_id
                    for (session_id,) in connection.execute(
                        "SELECT a.session_id "
                        + _ATTEMPTS
                        + "WHERE o.kind='decision' AND a.created_at>=? "
                        + _NEWEST_FIRST
                        + "LIMIT ?",
                        (stamp(since), MAX_SESSIONS),
                    )
                )
            )
        return SeatTeam(today=today, running=running, sessions=sessions)

    def waiting(self) -> tuple[SeatWaiting, ...]:
        return tuple(
            SeatWaiting(
                item.attempt.routine_id,
                item.attempt.attempt_id,
                item.attempt.session_id,
                item.question,
            )
            for item in self.runner.waiting()
        )

    async def start(
        self,
        routine_id: str,
        revision: int,
        *,
        newest: str | None,
        cards: str | None,
        limit: int,
        day: str,
    ) -> str:
        bounds = _bounds(day)
        scheduler = self.scheduler
        try:
            attempt = await self.runner.run_decision(
                routine_id,
                revision,
                newest=newest,
                request_hash=cards,
                limit=limit,
                day=bounds,
                ready=lambda: scheduler.state == "running" and not scheduler._paused(),
            )
        except RoutineError as err:
            raise DecisionLoopError(_START_REFUSALS.get(err.code, "not_applicable")) from None
        except StateError, protection.ProtectionError:
            raise DecisionLoopError("unavailable") from None
        scheduler.wake()  # as the run route does, so the scheduler sees the new attempt
        return attempt.session_id

    async def stop(self, routine_id: str) -> bool:
        """Interrupt the routine's open decision run, if its chat is busy; false when nothing
        was interrupted."""
        return await self._interrupt(await run_sync(self._open_sessions, routine_id)) > 0

    async def stop_all(self) -> int:
        """Interrupt every open decision run as `stop` does; answer how many."""
        return await self._interrupt(await run_sync(self._open_sessions, None))

    def _open_sessions(self, routine_id: str | None) -> list[str]:
        tasks = self.runner.store
        require_private_store(tasks.store)
        with tasks.store._connection() as connection:
            return [
                session_id
                for (session_id,) in connection.execute(
                    "SELECT a.session_id "
                    + _ATTEMPTS
                    + "WHERE o.kind='decision' AND a.state<>'done' "
                    + "AND (?1 IS NULL OR o.routine_id=?1)",
                    (routine_id,),
                )
            ]

    async def _interrupt(self, session_ids: list[str]) -> int:
        """Interrupt each busy chat among `session_ids`; count the interrupts that held."""
        interrupted = 0
        for session_id in session_ids:
            session = self.manager.get(session_id)
            if session is None or not session.busy:
                continue
            try:
                await session.interrupt()
            except Exception:
                log.warning("seat run interrupt failed")
                continue
            interrupted += 1
        return interrupted


def _bounds(day: str) -> tuple[str, str]:
    """The stamps that bound the local day `day` ("YYYY-MM-DD") on this computer's clock, as
    `decision_loop.missions` counts a day."""
    try:
        if date.fromisoformat(day).isoformat() != day:
            raise ValueError(day)
        start = datetime.fromisoformat(day)
        return stamp(start.astimezone()), stamp((start + timedelta(days=1)).astimezone())
    except ValueError, OverflowError, OSError:
        raise DecisionLoopError("invalid_request") from None


def _vault_routines(
    connection: sqlite3.Connection, workspace_id: str
) -> tuple[tuple[SeatRoutine, ...], bool]:
    """The non-archived routines whose current revision names `workspace_id`; false when one
    could not be read, since it might have named it."""
    connection.row_factory = sqlite3.Row
    rows = connection.execute(
        "SELECT r.routine_id,r.state,r.current_revision,r.tested_revision,v.revision,v.format,"
        "v.schedule,v.body,v.body_bytes,v.body_sha256 FROM routines r "
        "LEFT JOIN routine_revisions v "
        "ON v.routine_id=r.routine_id AND v.revision=r.current_revision "
        "WHERE r.state<>'archived' ORDER BY r.created_at,r.routine_id LIMIT ?",
        (MAX_ROUTINES + 1,),
    ).fetchall()
    complete = len(rows) <= MAX_ROUTINES
    found: list[SeatRoutine] = []
    for row in rows[:MAX_ROUTINES]:
        try:
            if row["revision"] is None:
                raise RoutineError("corrupt")
            document = decode_revision(dict(row))
        except RoutineError, protection.ProtectionError:
            complete = False
            continue
        if document["workspace_id"] != workspace_id:
            continue
        found.append(
            SeatRoutine(
                routine_id=row["routine_id"],
                state=row["state"],
                revision=row["current_revision"],
                tested_revision=row["tested_revision"],
                seat=document.get("run_only_if", {}).get("seat"),
                stop_at_draft=document.get("on_confirmation") == "stop",
            )
        )
    return tuple(found), complete


def _windows(
    connection: sqlite3.Connection, since: datetime | None
) -> tuple[tuple[SeatRun, ...], bool]:
    """Every routine's attempts and every Decision mission chat's, open or finished at or after
    `since` (none when `since` is None), the open ones first, then by finish, latest first; false
    when a stamp is bad. A mission chat counts as a run because a card it wrote is not the
    user's: Decision errs toward asking. Past `MAX_WINDOWS` the last window, of kind
    "unread", runs from `since` to the finish of the first run left out, which every run left
    out ended by: one read per cycle, however long the tables grow."""
    if since is None:
        return (), True
    rows = connection.execute(_WINDOWS, (stamp(since), _STAMP_SHAPE, MAX_WINDOWS + 1)).fetchall()
    runs = [_run(row) for row in rows[:MAX_WINDOWS]]
    complete = None not in runs
    if len(rows) > MAX_WINDOWS:
        try:
            end = parse_stamp(rows[MAX_WINDOWS][6])  # an open run left out has no end: raises
        except ValueError:
            complete = False
        else:
            runs.append(SeatRun("", "", "unread", "", since, False, end, None, ""))
    return tuple(run for run in runs if run is not None), complete


def _run(row: Any) -> SeatRun | None:
    attempt_id, routine_id, kind, session_id, created, dispatched, finished, outcome, reason = row
    try:
        return SeatRun(
            attempt_id=attempt_id,
            routine_id=routine_id,
            kind=kind,
            session_id=session_id,
            started=parse_stamp(created),
            dispatched=dispatched is not None,
            finished=None if finished is None else parse_stamp(finished),
            outcome=outcome,
            reason=reason,
        )
    except ValueError:
        return None
