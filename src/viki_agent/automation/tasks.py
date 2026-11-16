"""Task attempts for routine runs: their receipts, store and runner.

A run is an ordinary profile chat sent through the command outbox. Nothing here retries, re-sends
or answers a card except to deny it: at its deadline or, for a routine that stops at a draft,
at once.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import sqlite3
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Literal

from viki_agent.automation.routines import (
    MAX_INTEGER,
    MAX_RECEIPTS,
    UNATTENDED_MODES,
    RoutineError,
    RoutineService,
    _uuid,
    record_tested,
    stamp,
)
from viki_agent.conversations.command_runtime import CommandRunner, storage_call
from viki_agent.conversations.commands import CommandError, CommandInput
from viki_agent.conversations.drafts import DraftError, DraftService
from viki_agent.drivers.base import PermissionAnswer
from viki_agent.profiles.service import ProfileError, ProfileService
from viki_agent.session import CommandDispatchRejected, SessionManager
from viki_agent.storage import SessionStore
from viki_agent.turn_input import TurnInput

if TYPE_CHECKING:
    from viki_agent.automation.scheduler import Clock
    from viki_agent.session import SessionActor

logger = logging.getLogger(__name__)

MAX_ACTIVE = 2
MAX_OCCURRENCES = 1000

Outcome = Literal["succeeded", "failed", "cancelled", "expired", "blocked", "unknown"]

FAILED_CODES = frozenset({"crashed", "provider_disconnected", "frame_error", "internal"})
BLOCKED_CODES = frozenset({"cli_missing", "not_logged_in", "billing", "too_many_sessions"})


@dataclass(eq=False)  # hashable by identity: the actor keeps its subscribers in a set
class Observation:
    """Folds one attempt's session envelopes into the state `outcome()` reads.

    A session subscriber (`actor.subscribe(observation)`): call it with each envelope. It reads
    only `envelope["type"]` and the named `envelope["data"]` fields, never assistant text
    (HARNESS-02).
    """

    native_version: str | None = None
    open_cards: set[str] = field(default_factory=set)
    questions: set[str] = field(default_factory=set)  # the open cards that are questions
    rate_limited: bool = False
    turn: str | None = None
    error: str | None = None
    closed: str | None = None
    finished: str | None = None
    needs_decision: bool = False  # the runner denied a card to stop at a draft
    ended: asyncio.Event = field(default_factory=asyncio.Event)

    def __call__(self, envelope: dict[str, Any]) -> None:
        etype = envelope["type"]
        data = envelope["data"]
        if etype == "session.started":
            if self.native_version is None:
                value = data.get("cli_version")
                if isinstance(value, str) and value and len(value) <= 64:
                    self.native_version = value
        elif etype in ("permission.requested", "question.asked"):
            self.open_cards.add(data["tool_use_id"])
            if etype == "question.asked":
                self.questions.add(data["tool_use_id"])
        elif etype in ("request.answered", "request.cancelled"):
            self.open_cards.discard(data["tool_use_id"])
            self.questions.discard(data["tool_use_id"])
        elif etype == "rate_limit":
            if data.get("status") == "rejected":
                self.rate_limited = True
        elif etype == "turn.done":
            if self.turn is None:
                self.turn = data.get("status")
            self.open_cards.clear()
            self.questions.clear()
            self.ended.set()
        elif etype == "error":
            code = data.get("code")
            if self.error is None and code in FAILED_CODES | BLOCKED_CODES:
                self.error = code
        elif etype == "session.closed":
            if self.closed is None:
                self.closed = data.get("reason")
            self.open_cards.clear()
            self.questions.clear()
            self.ended.set()

    def finish(self, status: str) -> None:
        """The `finished=` callback given to `commands.submit`: records the terminal command
        state and, for `"not_dispatched"`, ends the wait (an unresolved `"unknown"` may still
        resolve later, so only the deadline ends that wait)."""
        self.finished = status
        if status == "not_dispatched":
            self.ended.set()


def outcome(
    seen: Observation,
    *,
    deadline: bool,
    command_reason: str = "internal",
    closing: bool = False,
) -> tuple[Outcome, str] | None:
    """Map one attempt's `Observation` to its outcome, first match wins.

    `closing`: the runner's own admission check refused this command (recorded when it
    refused), or shutdown cancelled the run. A command cancelled by `CommandRunner.close()`
    reads `app_closed` by its `cancelled` reason.
    """
    if deadline and seen.open_cards:
        return ("expired", "waiting_for_user")
    if deadline:
        return ("expired", "deadline")
    if seen.rate_limited and (seen.turn or seen.closed or seen.finished == "not_dispatched"):
        return ("blocked", "quota")
    if seen.turn == "success":
        return ("blocked", "needs_decision") if seen.needs_decision else ("succeeded", "")
    if seen.turn == "cancelled":
        return ("cancelled", "stopped")
    # Below a completed or stopped turn, and only with an end that carries the code: a
    # recoverable error during a turn that the app's close or shutdown then cuts off must not
    # outrank that end. No crash loses its code: a crash while busy emits turn.done error
    # before it, and a process that died before the send leaves the command not_dispatched.
    error = seen.error
    if (
        error is not None
        and error in FAILED_CODES
        and (seen.turn == "error" or seen.finished == "not_dispatched")
    ):
        return ("failed", error)
    if seen.turn == "error":
        return ("failed", "turn_error")
    if error is not None and error in BLOCKED_CODES and seen.finished == "not_dispatched":
        return ("blocked", error)
    if seen.closed == "requested":
        return ("cancelled", "session_closed")
    if seen.closed == "browser_gone":
        return ("cancelled", "app_closed")
    if seen.finished == "not_dispatched":
        # A command the app's closing stopped before its send: CommandRunner.close() cancelled
        # it (its reason is `cancelled`), or the runner's own admission check refused it, as
        # recorded when it refused, never the window state when this is read.
        if closing or command_reason == "cancelled":
            return ("cancelled", "app_closed")
        return ("failed", command_reason)
    if closing and seen.finished == "unknown":
        return ("unknown", command_reason)
    return None


@dataclass(frozen=True)
class Attempt:
    attempt_id: str
    occurrence_id: str
    routine_id: str
    revision: int
    kind: Literal["schedule", "manual", "decision"]
    session_id: str


@dataclass(frozen=True)
class Waiting:
    """A run with an open card; `question` when every open card is a question."""

    attempt: Attempt
    question: bool


def _manual_request(
    routine_id: str, command_id: str, expected_revision: int
) -> tuple[str, str, str]:
    routine_id = _uuid(routine_id)
    command_id = _uuid(command_id)
    if type(expected_revision) is not int or not 1 <= expected_revision <= MAX_INTEGER:
        raise RoutineError("invalid_revision")
    request = json.dumps([routine_id, expected_revision], separators=(",", ":"))
    return routine_id, command_id, hashlib.sha256(request.encode()).hexdigest()


class TaskStore:
    """Routine occurrences and their task attempts. The `connection` helpers run inside the
    caller's write transaction; the other methods open their own."""

    def __init__(self, store: SessionStore, scope: str, owner: str) -> None:
        self.store = store
        self.scope = scope
        self.owner = owner

    def occupancy(
        self, connection: sqlite3.Connection, routine_id: str, slots: int = MAX_ACTIVE
    ) -> Literal["run_active", "busy"] | None:
        """`run_active` while the routine has an open attempt, `busy` while `slots` attempts of
        any routine are open, else None."""
        own, total = connection.execute(
            "SELECT COALESCE(max(o.routine_id=?),0),count(*) FROM task_attempts a "
            "LEFT JOIN routine_occurrences o ON o.occurrence_id=a.origin_id "
            "WHERE a.state<>'done'",
            (routine_id,),
        ).fetchone()
        if own:
            return "run_active"
        return "busy" if total >= slots else None

    def team_count(self, connection: sqlite3.Connection, start: str, end: str) -> int:
        """Decision attempts claimed in `[start, end)` that were sent or may still be: one
        cancelled before its send, such as by a crash, uses none of the daily limit."""
        (count,) = connection.execute(
            "SELECT count(*) FROM task_attempts a "
            "JOIN routine_occurrences o ON o.occurrence_id=a.origin_id "
            "WHERE o.kind='decision' AND a.created_at>=? AND a.created_at<? "
            "AND (a.dispatched_at IS NOT NULL OR a.state<>'done')",
            (start, end),
        ).fetchone()
        return count

    def last_completed_start(
        self, connection: sqlite3.Connection, routine_id: str, revision: int
    ) -> str | None:
        """The claim stamp of the routine's latest run at `revision` that did its work: one that
        succeeded or stopped at a draft for the user's decision. Any kind of run counts. The
        claim, not the finish, so a note changed while that run went on still counts as new."""
        (claimed,) = connection.execute(
            "SELECT max(a.created_at) FROM task_attempts a "
            "JOIN routine_occurrences o ON o.occurrence_id=a.origin_id "
            "WHERE o.routine_id=? AND o.revision=? AND (a.outcome='succeeded' "
            "OR (a.outcome='blocked' AND a.reason='needs_decision'))",
            (routine_id, revision),
        ).fetchone()
        return claimed

    def newest(self, connection: sqlite3.Connection, routine_id: str) -> str | None:
        """The routine's newest attempt id of any kind, which a decision claim compares."""
        row = connection.execute(
            "SELECT a.attempt_id FROM task_attempts a "
            "JOIN routine_occurrences o ON o.occurrence_id=a.origin_id WHERE o.routine_id=? "
            "ORDER BY a.created_at DESC,a.attempt_id DESC LIMIT 1",
            (routine_id,),
        ).fetchone()
        return None if row is None else row[0]

    def insert_claim(
        self,
        connection: sqlite3.Connection,
        *,
        routine_id: str,
        kind: Literal["schedule", "manual", "decision"],
        revision: int,
        now: str,
        scheduled_for: str | None = None,
        trigger_id: str | None = None,
        request_hash: str | None = None,
    ) -> Attempt:
        attempt = Attempt(
            str(uuid.uuid4()), str(uuid.uuid4()), routine_id, revision, kind, str(uuid.uuid4())
        )
        connection.execute(
            "INSERT INTO routine_occurrences VALUES (?,?,?,?,?,?,?,'claimed','',?)",
            (
                attempt.occurrence_id,
                routine_id,
                kind,
                scheduled_for,
                trigger_id,
                request_hash,
                revision,
                now,
            ),
        )
        connection.execute(
            "INSERT INTO task_attempts VALUES "
            "(?,'routine',?,?,?,?,'claimed',NULL,'',NULL,NULL,NULL,?,NULL,NULL)",
            (
                attempt.attempt_id,
                attempt.occurrence_id,
                self.scope,
                attempt.session_id,
                self.owner,
                now,
            ),
        )
        return attempt

    def insert_skip(
        self,
        connection: sqlite3.Connection,
        *,
        routine_id: str,
        scheduled_for: str,
        revision: int,
        reason: Literal["missed", "overlap", "busy", "paused", "no_card", "no_change"],
        now: str,
    ) -> None:
        connection.execute(
            "INSERT INTO routine_occurrences VALUES (?,?,'schedule',?,NULL,NULL,?,'skipped',?,?)",
            (str(uuid.uuid4()), routine_id, scheduled_for, revision, reason, now),
        )

    def prune(self, connection: sqlite3.Connection, routine_id: str) -> None:
        (count,) = connection.execute(
            "SELECT count(*) FROM routine_occurrences WHERE routine_id=?", (routine_id,)
        ).fetchone()
        if count <= MAX_OCCURRENCES:
            return
        # An open attempt's occurrence stays, even past the cap: its run still reports into it.
        victims = connection.execute(
            "SELECT o.occurrence_id FROM routine_occurrences o LEFT JOIN task_attempts a "
            "ON a.origin_id=o.occurrence_id WHERE o.routine_id=? "
            "AND (a.state IS NULL OR a.state='done') "
            "ORDER BY COALESCE(o.scheduled_for,o.recorded_at),o.occurrence_id LIMIT ?",
            (routine_id, count - MAX_OCCURRENCES),
        ).fetchall()
        connection.executemany(
            "DELETE FROM bot_deliveries WHERE attempt_id IN "
            "(SELECT attempt_id FROM task_attempts WHERE origin_id=?)",
            victims,
        )
        connection.executemany("DELETE FROM task_attempts WHERE origin_id=?", victims)
        connection.executemany("DELETE FROM routine_occurrences WHERE occurrence_id=?", victims)

    def _manual_receipt(
        self, connection: sqlite3.Connection, command_id: str, request_hash: str
    ) -> Attempt | None:
        receipt = connection.execute(
            "SELECT operation,request_hash,result_attempt_id,result_occurrence_id,routine_id,"
            "result_revision,result_session_id FROM routine_commands WHERE command_id=?",
            (command_id,),
        ).fetchone()
        if receipt is None:
            return None
        if receipt[0] != "run" or receipt[1] != request_hash:
            raise RoutineError("command_conflict")
        return Attempt(receipt[2], receipt[3], receipt[4], receipt[5], "manual", receipt[6])

    def lookup_manual(
        self, routine_id: str, command_id: str, expected_revision: int
    ) -> Attempt | None:
        """Read a durable manual receipt, including after its visible history was pruned."""
        _, command_id, request_hash = _manual_request(routine_id, command_id, expected_revision)
        with self.store._connection() as connection:
            return self._manual_receipt(connection, command_id, request_hash)

    def claim_manual(
        self, routine_id: str, command_id: str, expected_revision: int, now: str
    ) -> tuple[Attempt, bool]:
        routine_id, command_id, request_hash = _manual_request(
            routine_id, command_id, expected_revision
        )
        with self.store._connection(write=True) as connection:
            receipt = self._manual_receipt(connection, command_id, request_hash)
            if receipt is not None:
                return receipt, False
            if connection.execute("SELECT count(*) FROM routine_commands").fetchone()[0] >= (
                MAX_RECEIPTS
            ):
                raise RoutineError("capacity")
            routine = connection.execute(
                "SELECT state,current_revision FROM routines WHERE routine_id=?", (routine_id,)
            ).fetchone()
            if routine is None:
                raise RoutineError("not_found")
            if routine[0] == "archived":
                raise RoutineError("invalid_transition")
            if routine[1] != expected_revision:
                raise RoutineError("conflict")
            busy = self.occupancy(connection, routine_id)
            if busy is not None:
                raise RoutineError(busy)
            attempt = self.insert_claim(
                connection,
                routine_id=routine_id,
                kind="manual",
                revision=expected_revision,
                now=now,
                trigger_id=command_id,
                request_hash=request_hash,
            )
            connection.execute(
                "INSERT INTO routine_commands VALUES (?,'run',?,?,?,NULL,NULL,?,?,?,?)",
                (
                    command_id,
                    routine_id,
                    request_hash,
                    expected_revision,
                    attempt.occurrence_id,
                    attempt.attempt_id,
                    attempt.session_id,
                    now,
                ),
            )
            self.prune(connection, routine_id)
        return attempt, True

    def claim_decision(
        self,
        routine_id: str,
        revision: int,
        *,
        newest: str | None,
        request_hash: str | None,
        limit: int,
        day: tuple[str, str],
        now: str,
    ) -> Attempt:
        """Claim a run Decision started, checked and written in one write transaction, so a
        refusal stores nothing. `newest` is the attempt id the plan saw: a run since then
        refuses the claim. `request_hash` is None for the user's Start, which may take either
        of the `MAX_ACTIVE` slots; VIKI's own start (with its cards' hash) is `busy` while any
        routine run is going, so a scheduled slot always finds the other one free."""
        with self.store._connection(write=True) as connection:
            routine = connection.execute(
                "SELECT state,current_revision,tested_revision FROM routines WHERE routine_id=?",
                (routine_id,),
            ).fetchone()
            if routine is None:
                raise RoutineError("not_found")
            if routine[0] != "enabled":
                raise RoutineError("invalid_transition")
            if routine[1] != revision:
                raise RoutineError("conflict")
            if routine[2] != revision:
                raise RoutineError("not_ready")
            if self.newest(connection, routine_id) != newest:
                raise RoutineError("conflict")
            busy = self.occupancy(connection, routine_id, MAX_ACTIVE if request_hash is None else 1)
            if busy is not None:
                raise RoutineError(busy)
            if self.team_count(connection, *day) >= limit:
                raise RoutineError("limit")
            attempt = self.insert_claim(
                connection,
                routine_id=routine_id,
                kind="decision",
                revision=revision,
                now=now,
                request_hash=request_hash,
            )
            self.prune(connection, routine_id)
        return attempt

    def _update(self, sql: str, values: tuple[Any, ...]) -> bool:
        with self.store._connection(write=True) as connection:
            return connection.execute(sql, values).rowcount == 1

    def mark_dispatching(self, attempt_id: str, profile_id: str, profile_revision: int) -> bool:
        return self._update(
            "UPDATE task_attempts SET state='dispatching',profile_id=?,profile_revision=? "
            "WHERE attempt_id=? AND state='claimed'",
            (profile_id, profile_revision, attempt_id),
        )

    def mark_running(self, attempt_id: str, now: str) -> bool:
        return self._update(
            "UPDATE task_attempts SET state='running',dispatched_at=? "
            "WHERE attempt_id=? AND state='dispatching'",
            (now, attempt_id),
        )

    def finish(
        self,
        attempt_id: str,
        outcome: Outcome,
        reason: str,
        now: str,
        *,
        native_version: str | None = None,
        tested: tuple[str, int] | None = None,
    ) -> bool:
        """End an open attempt once; a second call changes nothing and answers False.

        `tested` is the routine and revision a manual run tested (succeeded, or blocked at a
        draft for the user's decision), recorded in the same transaction as the outcome.
        """
        with self.store._connection(write=True) as connection:
            changed = (
                connection.execute(
                    "UPDATE task_attempts SET state='done',outcome=?,reason=?,finished_at=?,"
                    "native_version=COALESCE(?,native_version) "
                    "WHERE attempt_id=? AND state<>'done'",
                    (outcome, reason, now, native_version, attempt_id),
                ).rowcount
                == 1
            )
            if changed and tested is not None:
                record_tested(connection, *tested, now)
        return changed

    def reconcile(self, now: str) -> int:
        """End every open attempt of a dead launch in one transaction; answer how many.

        Only a `claimed` attempt or a `not_dispatched` command proves a run was never sent. A
        missing command row proves nothing: a `commands` data reset drops a dead launch's rows, so
        a `dispatching` or `running` attempt without one may still have been sent.
        """
        with self.store._connection(write=True) as connection:
            rows = connection.execute(
                "SELECT a.attempt_id,a.state,c.state FROM task_attempts a LEFT JOIN commands c "
                "ON c.scope=a.scope AND c.command_id=a.attempt_id "
                "WHERE a.state<>'done' AND a.owner<>?",
                (self.owner,),
            ).fetchall()
            for attempt_id, state, command in rows:
                unsent = command == "not_dispatched" or (command is None and state == "claimed")
                connection.execute(
                    "UPDATE task_attempts SET state='done',outcome=?,reason='restarted',"
                    "finished_at=? WHERE attempt_id=? AND state<>'done'",
                    ("cancelled" if unsent else "unknown", now, attempt_id),
                )
        return len(rows)

    def runs(self, routine_id: str, limit: int, before: str | None) -> list[dict[str, Any]]:
        """One routine's occurrences with their attempt receipts, newest first."""
        if type(limit) is not int or not 1 <= limit <= 50:
            raise RoutineError("invalid_input")
        with self.store._connection() as connection:
            page: tuple[str, ...] = ()
            if before is not None:
                page = connection.execute(
                    "SELECT COALESCE(scheduled_for,recorded_at),occurrence_id "
                    "FROM routine_occurrences "
                    "WHERE occurrence_id=? AND routine_id=?",
                    (before, routine_id),
                ).fetchone()
                if page is None:
                    raise RoutineError("invalid_input")
            rows = connection.execute(
                f"SELECT {_RUN_COLUMNS} FROM routine_occurrences o LEFT JOIN task_attempts a "
                "ON a.origin_id=o.occurrence_id WHERE o.routine_id=? "
                + (
                    "AND (COALESCE(o.scheduled_for,o.recorded_at),o.occurrence_id)<(?,?) "
                    if page
                    else ""
                )
                + "ORDER BY COALESCE(o.scheduled_for,o.recorded_at) DESC,"
                "o.occurrence_id DESC LIMIT ?",
                (routine_id, *page, limit),
            ).fetchall()
        return [_run(row) for row in rows]

    def summaries(self) -> dict[str, dict[str, Any]]:
        """Each routine's newest run, its newest run with an attempt (a newer skip, such as an
        overlap recorded while a run goes on, does not hide that run's end) and its open
        attempt, if any, in one statement; the open attempt carries its claim time and the
        revision it runs."""
        newest = (
            "row_number() OVER (PARTITION BY routine_id "
            "ORDER BY COALESCE(scheduled_for,recorded_at) DESC,occurrence_id DESC) AS n"
        )
        with self.store._connection() as connection:
            rows = connection.execute(
                f"SELECT o.routine_id,{_RUN_COLUMNS},{_run_columns('p', 'b')},"
                "x.attempt_id,x.session_id,x.state,x.created_at,x.revision "
                f"FROM (SELECT *,{newest} FROM routine_occurrences) o "
                "LEFT JOIN task_attempts a ON a.origin_id=o.occurrence_id "
                f"LEFT JOIN (SELECT *,{newest} FROM routine_occurrences "
                "WHERE occurrence_id IN (SELECT origin_id FROM task_attempts)) p "
                "ON p.routine_id=o.routine_id AND p.n=1 "
                "LEFT JOIN task_attempts b ON b.origin_id=p.occurrence_id "
                "LEFT JOIN (SELECT r.routine_id,t.attempt_id,t.session_id,t.state,t.created_at,"
                "r.revision FROM task_attempts t "
                "JOIN routine_occurrences r ON r.occurrence_id=t.origin_id "
                "WHERE t.state<>'done') x ON x.routine_id=o.routine_id WHERE o.n=1"
            ).fetchall()
        width = len(_OCCURRENCE_FIELDS) + len(_ATTEMPT_FIELDS)
        return {
            row[0]: {
                "last_run": _run(row[1 : 1 + width]),
                "last_attempt": None if row[1 + width] is None else _run(row[1 + width : -5]),
                "active_run": None
                if row[-5] is None
                else dict(zip(_ACTIVE_FIELDS, row[-5:], strict=True)),
            }
            for row in rows
        }


_OCCURRENCE_FIELDS = (
    "occurrence_id",
    "kind",
    "scheduled_for",
    "trigger_id",
    "revision",
    "state",
    "reason",
    "recorded_at",
)
_ATTEMPT_FIELDS = (
    "attempt_id",
    "session_id",
    "state",
    "outcome",
    "reason",
    "profile_id",
    "profile_revision",
    "native_version",
    "created_at",
    "dispatched_at",
    "finished_at",
)
_ACTIVE_FIELDS = ("attempt_id", "session_id", "state", "created_at", "revision")


def _run_columns(occurrence: str, attempt: str) -> str:
    return ",".join(
        [f"{occurrence}.{name}" for name in _OCCURRENCE_FIELDS]
        + [f"{attempt}.{name}" for name in _ATTEMPT_FIELDS]
    )


_RUN_COLUMNS = _run_columns("o", "a")


def _run(row: tuple[Any, ...]) -> dict[str, Any]:
    split = len(_OCCURRENCE_FIELDS)
    run: dict[str, Any] = dict(zip(_OCCURRENCE_FIELDS, row[:split], strict=True))
    attempt = row[split:]
    run["attempt"] = (
        None if attempt[0] is None else dict(zip(_ATTEMPT_FIELDS, attempt, strict=True))
    )
    return run


class TaskRunner:
    """Runs each claimed attempt as one ordinary profile chat turn, sent through the command
    outbox: one task per attempt. A run answers a card only to deny it: at its deadline or, for a
    routine that stops at a draft, at once. It folds only metadata for outcomes. Optional Bot
    observers receive live envelopes."""

    def __init__(
        self,
        store: TaskStore,
        routines: RoutineService,
        commands: CommandRunner,
        manager: SessionManager,
        drafts: DraftService,
        profiles: ProfileService,
        *,
        prepare: Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]] | None,
        clock: Clock,
        attended: Callable[[], bool],
        admission_lock: asyncio.Lock | None = None,
        begin_attempt: Callable[[Attempt, dict[str, Any]], Awaitable[None]] | None = None,
        observe_attempt: Callable[
            [Attempt, SessionActor, dict[str, Any]], Callable[[dict[str, Any]], None] | None
        ]
        | None = None,
        attempt_finished: Callable[[Attempt, Outcome, str], Awaitable[None]] | None = None,
        discard_attempt: Callable[[Attempt], None] | None = None,
    ) -> None:
        self.store, self.routines, self.commands = store, routines, commands
        self.manager, self.drafts, self.profiles = manager, drafts, profiles
        self.prepare, self.clock, self.attended = prepare, clock, attended
        self.begin_attempt, self.observe_attempt = begin_attempt, observe_attempt
        self.attempt_finished, self.discard_attempt = attempt_finished, discard_attempt
        self._admission_lock = admission_lock if admission_lock is not None else asyncio.Lock()
        self._runs: dict[asyncio.Task[None], Observation] = {}
        self._attempts: dict[asyncio.Task[None], Attempt] = {}
        self._watch: Callable[[], None] | None = None
        self._closing = False

    @property
    def available(self) -> bool:
        return self.prepare is not None

    def start(self, attempt: Attempt) -> None:
        if self._closing:
            return  # it stays claimed, which the next launch's reconcile reads as never sent
        seen = Observation()
        task = asyncio.create_task(self._run(attempt, seen))
        self._runs[task] = seen
        self._attempts[task] = attempt

        def done(run: asyncio.Task[None]) -> None:
            del self._runs[run]
            del self._attempts[run]
            if not run.cancelled() and run.exception() is not None:
                logger.warning("routine run failed")  # never the exception: it may hold text

        task.add_done_callback(done)

    async def run_now(
        self,
        routine_id: str,
        command_id: str,
        expected_revision: int,
        *,
        scheduler_ready: Callable[[], bool] = lambda: True,
    ) -> tuple[Attempt, bool]:
        receipt = await storage_call(
            lambda: self.store.lookup_manual(routine_id, command_id, expected_revision)
        )
        if receipt is not None:
            return receipt, False
        async with self._admission_lock:
            receipt = await storage_call(
                lambda: self.store.lookup_manual(routine_id, command_id, expected_revision)
            )
            if receipt is not None:
                return receipt, False
            if not self.available or self._closing or not self.attended() or not scheduler_ready():
                raise RoutineError("unavailable")
            now = stamp(self.clock.wall())

            def claimed(result: tuple[Attempt, bool]) -> None:
                attempt, created = result
                if created:
                    self.start(attempt)  # register before cancellation propagates and unlocks

            return await storage_call(
                lambda: self.store.claim_manual(routine_id, command_id, expected_revision, now),
                claimed,
            )

    async def run_decision(
        self,
        routine_id: str,
        revision: int,
        *,
        newest: str | None,
        request_hash: str | None,
        limit: int,
        day: tuple[str, str],
        ready: Callable[[], bool] = lambda: True,
    ) -> Attempt:
        """Claim and start a run Decision started, admitted as `run_now` admits one. `ready` is
        read under the admission lock just before the claim (Pause all, a running scheduler)."""
        async with self._admission_lock:
            if not self.available or self._closing or not self.attended() or not ready():
                raise RoutineError("unavailable")
            now = stamp(self.clock.wall())
            return await storage_call(
                lambda: self.store.claim_decision(
                    routine_id,
                    revision,
                    newest=newest,
                    request_hash=request_hash,
                    limit=limit,
                    day=day,
                    now=now,
                ),
                self.start,  # register before cancellation propagates and unlocks
            )

    def bind_cards(self, watch: Callable[[], None]) -> None:
        """Call `watch` on the event loop each time a run's open cards change."""
        self._watch = watch

    def waiting(self) -> tuple[Waiting, ...]:
        return tuple(
            Waiting(self._attempts[task], seen.open_cards <= seen.questions)
            for task, seen in self._runs.items()
            if seen.open_cards
        )

    async def reconcile(self) -> int:
        now = stamp(self.clock.wall())
        return await storage_call(lambda: self.store.reconcile(now))

    async def close(self) -> None:
        """Stop new runs and drain each run, including an end held before its command callback."""
        cancelled = False
        while True:
            try:
                await self._admission_lock.acquire()
                break
            except asyncio.CancelledError:
                cancelled = True  # still join an earlier claim before closing admission
        try:
            self._closing = True
            runs = list(self._runs)
        finally:
            self._admission_lock.release()
        for task in runs:
            seen = self._runs[task]
            if (
                (not seen.ended.is_set() or seen.finished is None)
                and not task.done()
                and not task.cancelling()
            ):
                task.cancel()
        if runs:
            waiter = asyncio.gather(*runs, return_exceptions=True)
            while True:
                try:
                    await asyncio.shield(waiter)
                    break
                except asyncio.CancelledError:
                    if waiter.cancelled():
                        raise
                    cancelled = True
        if cancelled:
            raise asyncio.CancelledError

    async def _run(self, attempt: Attempt, seen: Observation) -> None:
        attempt_id, session_id = attempt.attempt_id, attempt.session_id
        unsubscribe: Callable[[], None] | None = None
        bot_unsubscribe: Callable[[], None] | None = None
        bot_observer: Callable[[dict[str, Any]], None] | None = None
        prepared = dispatching = False
        leftover: int | None = None  # the draft's revision while no command holds its text
        submitted: CommandInput | None = None
        accepted_at: str | None = None
        deadline_at = float("inf")
        ended_at: float | None = None
        deadline_refused = False
        retained: tuple[Outcome, str] | None = None
        retained_seen: Observation | None = None
        running_marked = close_attempted = close_finished = False
        close_succeeded = False

        def detach_observers() -> None:
            nonlocal unsubscribe, bot_unsubscribe, bot_observer
            normal, bot = unsubscribe, bot_unsubscribe
            unsubscribe = bot_unsubscribe = bot_observer = None
            try:
                if normal is not None:
                    normal()
            finally:
                if bot is not None:
                    bot()

        async def close_session() -> None:
            nonlocal close_attempted, close_finished, close_succeeded
            close_attempted = True
            try:
                await self.manager.close(session_id)
            except Exception:
                logger.warning("routine session close failed")
            else:
                close_succeeded = self.manager.get(session_id) is None
            close_finished = True

        async def mark_running() -> None:
            nonlocal running_marked
            when = accepted_at
            if running_marked or when is None:
                return

            def marked(_changed: bool) -> None:
                nonlocal running_marked
                running_marked = True

            await storage_call(lambda: self.store.mark_running(attempt_id, when), marked)

        async def clear_draft() -> None:
            # At that revision the text is app-composed, never the user's typing, so emptying
            # it loses nothing; left behind, it counts toward the store's non-empty draft cap.
            nonlocal leftover
            if leftover is not None:
                revision = leftover

                def cleared(_draft: object) -> None:
                    nonlocal leftover
                    leftover = None

                try:
                    await storage_call(lambda: self.drafts.save(session_id, revision, ""), cleared)
                except DraftError:
                    logger.warning("routine draft clear failed")
                    leftover = None

        async def settled_outcome(
            snapshot: Observation, *, closing: bool
        ) -> tuple[Outcome, str] | None:
            mapped = outcome(snapshot, deadline=False, closing=closing)
            # An observed end is sufficient even if reset has removed its command row.
            if mapped is not None and (
                snapshot.turn is not None
                or snapshot.closed in {"requested", "browser_gone"}
                or (
                    snapshot.finished == "not_dispatched"
                    and (snapshot.rate_limited or snapshot.error is not None or closing)
                )
            ):
                return mapped
            reason = "internal"
            if snapshot.finished is not None:
                try:
                    reason = (await self.commands.get(attempt_id)).reason
                except CommandError as err:
                    if err.code != "not_found":
                        raise
            return outcome(snapshot, deadline=False, command_reason=reason, closing=closing)

        async def steps() -> tuple[Outcome, str]:
            nonlocal unsubscribe, prepared, dispatching, leftover, submitted
            nonlocal bot_unsubscribe, bot_observer
            nonlocal accepted_at, deadline_at, retained, retained_seen
            try:
                document = await asyncio.to_thread(
                    self.routines.frozen, attempt.routine_id, attempt.revision
                )
                deadline_at = self.clock.monotonic() + 60 * document["deadline_minutes"]
                if self.begin_attempt is not None:
                    try:
                        await self.begin_attempt(attempt, document)
                    except Exception:
                        logger.warning("routine bot begin failed")
                blockers = await asyncio.to_thread(
                    self.routines.readiness, document, scheduled=attempt.kind != "manual"
                )
            except RoutineError as err:
                return "failed", err.code
            if self.clock.monotonic() >= deadline_at:
                return "expired", "deadline"
            if blockers:
                return "blocked", blockers[0]
            profile_id = document["profile_id"]
            prepared = True
            try:
                if self.prepare is None:
                    raise ProfileError("provider_unavailable")  # as prepare_profile answers
                profile = await asyncio.to_thread(self.profiles.get, profile_id)
                if self.clock.monotonic() >= deadline_at:
                    return "expired", "deadline"
                preparing = asyncio.ensure_future(
                    self.prepare(
                        profile_id,
                        {
                            "command_id": attempt_id,
                            "session_id": session_id,
                            "expected_profile_revision": profile["current_revision"],
                            "workspace_id": document["workspace_id"],
                        },
                    )
                )
                try:
                    result = await asyncio.shield(preparing)
                except asyncio.CancelledError:
                    # Preparation can commit and attach an actor after any of its awaits.
                    while not preparing.done():
                        with contextlib.suppress(asyncio.CancelledError, Exception):
                            await asyncio.shield(preparing)
                    if not preparing.cancelled():
                        preparing.exception()  # shutdown retains the unsent claim even on failure
                    await close_session()
                    raise
            except ProfileError as err:
                code = err.code
                if code == "conflict":
                    profile = await asyncio.to_thread(self.profiles.get, profile_id)
                    code = "profile_archived" if profile["archived"] else "profile_changed"
                elif code == "not_found":
                    code = "profile_missing"
                elif code not in {
                    "workspace_restart_required",
                    "workspace_unavailable",
                    "provider_unavailable",
                    "capacity",
                    "settings_unavailable",
                }:
                    return "failed", "internal"
                return "blocked", code
            if "session_id" in result:
                leftover = result["draft"]["revision"]
            if self.clock.monotonic() >= deadline_at:
                return "expired", "deadline"
            if "session_id" not in result or not result["launchable"]:
                return "blocked", "profile_blocked"
            # Readiness read the gate before the bind; the bound snapshot is what will run.
            if attempt.kind != "manual" and result["policy"] != "ordinary":
                return "blocked", "unattended_policy"
            if attempt.kind != "manual" and result["prefs"]["mode"] not in UNATTENDED_MODES:
                return "blocked", "unattended_mode"
            actor = self.manager.get(session_id)
            if actor is None:
                return "failed", "internal"
            progress = asyncio.Event()
            command_settled = asyncio.Event()
            stop_at_draft = document.get("on_confirmation") == "stop"

            def stop_card(tool_use_id: str) -> None:
                try:
                    stop = PermissionAnswer(allow=False, stop_at_draft=True)
                    if actor.respond(tool_use_id, stop):
                        seen.needs_decision = True
                except Exception:
                    logger.warning("routine card stop failed")

            def observe(envelope: dict[str, Any]) -> None:
                nonlocal ended_at
                cards = len(seen.open_cards)  # one envelope opens or closes cards, never both
                seen(envelope)
                if self._watch is not None and cards != len(seen.open_cards):
                    try:
                        self._watch()
                    except Exception:
                        logger.warning("routine card watch failed")
                if (
                    stop_at_draft
                    and envelope["type"] == "permission.requested"
                    and envelope["data"].get("tool") != "AskUserQuestion"
                ):
                    # Deferred past the actor's subscriber loop, which an answer can re-enter;
                    # it still runs before a browser or bridge can answer.
                    asyncio.get_running_loop().call_soon(stop_card, envelope["data"]["tool_use_id"])
                if seen.ended.is_set():
                    if ended_at is None:
                        ended_at = self.clock.monotonic()
                    progress.set()

            def finished(status: str) -> None:
                nonlocal ended_at
                seen.finish(status)
                if seen.ended.is_set() and ended_at is None:
                    ended_at = self.clock.monotonic()
                command_settled.set()
                progress.set()

            unsubscribe = actor.subscribe(observe)
            if self.observe_attempt is not None:
                try:
                    bot_observer = self.observe_attempt(attempt, actor, result)
                except Exception:
                    logger.warning("routine bot observation failed")
                if bot_observer is not None:

                    def observe_bot(envelope: dict[str, Any]) -> None:
                        nonlocal bot_observer
                        if bot_observer is not None:
                            try:
                                bot_observer(envelope)
                            except Exception:
                                bot_observer = None
                                logger.warning("routine bot observation failed")

                    bot_unsubscribe = actor.subscribe(observe_bot)
            draft, prompt = result["draft"], document["prompt"]
            text = f"{draft['text']}\n\n{prompt}" if draft["text"] else prompt
            try:
                saved = await storage_call(
                    lambda: self.drafts.save(session_id, draft["revision"], TurnInput(text))
                )
            except DraftError as err:
                if err.code == "capacity":
                    return "blocked", "capacity"
                return "failed", "draft_conflict" if err.code == "conflict" else "internal"
            leftover = saved.revision
            if self.clock.monotonic() >= deadline_at:
                return "expired", "deadline"
            profile_revision = result["profile_revision"]
            if not await storage_call(
                lambda: self.store.mark_dispatching(attempt_id, profile_id, profile_revision)
            ):
                return "failed", "internal"
            # Committed before submit: from here on the turn may have been sent, so an
            # unexpected error ends unknown, never failed.
            dispatching = True
            refused = False

            def admitted() -> bool:
                nonlocal refused, deadline_refused
                ok = self.attended() and not self._closing
                refused = refused or not ok
                deadline_refused = deadline_refused or self.clock.monotonic() >= deadline_at
                return ok and not deadline_refused

            try:
                submitted = CommandInput(
                    attempt_id, session_id, result["generation"], saved.revision
                )
                if self.clock.monotonic() >= deadline_at:
                    return "expired", "deadline"
                _record, created = await self.commands.submit(
                    submitted,
                    admitted=admitted,
                    finished=finished,
                )
            except CommandError as err:
                return ("blocked" if err.code == "capacity" else "failed"), err.code
            except CommandDispatchRejected as err:
                if deadline_refused:
                    return "expired", "deadline"
                if err.code == "closing" or refused:
                    return "cancelled", "app_closed"
                return "failed", err.code
            if not created:
                submitted = None  # this receipt belongs to another submission
                return "unknown", "duplicate"
            accepted_at = stamp(self.clock.wall())
            while True:
                progress.clear()  # clear before inspecting either source of progress
                if self.clock.monotonic() >= deadline_at:
                    known = (
                        replace(seen, open_cards=set(seen.open_cards))
                        if ended_at is not None and ended_at <= deadline_at and not deadline_refused
                        else None
                    )
                    # Freeze expiry before denial or any await: denial empties open_cards.
                    retained_seen = known
                    retained = outcome(seen, deadline=True) if known is None else None
                    if seen.open_cards:
                        actor.deny_pending()
                    await self.commands.stop(submitted)
                    await close_session()
                    await mark_running()
                    await clear_draft()
                    if known is None:
                        return retained or ("failed", "internal")
                    mapped = await settled_outcome(known, closing=refused)
                    return mapped or ("failed", "internal")
                if command_settled.is_set():
                    if not running_marked:
                        await mark_running()
                        continue
                    if leftover is not None:
                        await clear_draft()
                        continue
                    if seen.ended.is_set():
                        if self.clock.monotonic() >= deadline_at:
                            continue
                        mapped = await settled_outcome(seen, closing=refused)
                        return mapped or ("failed", "internal")
                left = deadline_at - self.clock.monotonic()
                if left > 0:
                    await self.clock.wait(progress, left)

        cancelled = close_after_finish = False
        final_seen = seen
        try:
            try:
                ended = await steps()
            except asyncio.CancelledError:
                had_end = seen.ended.is_set()
                snapshot = retained_seen or replace(seen, open_cards=set(seen.open_cards))
                if (
                    retained is None
                    and retained_seen is None
                    and ended_at is not None
                    and (ended_at > deadline_at or deadline_refused)
                ):
                    retained = outcome(snapshot, deadline=True)
                if submitted is not None:
                    await self.commands.stop(submitted)
                if retained is not None:
                    if not close_finished:
                        await close_session()
                    await mark_running()
                    await clear_draft()
                    ended = retained
                elif retained_seen is not None or had_end:
                    if retained_seen is not None and not close_finished:
                        await close_session()
                    if accepted_at is not None:
                        await mark_running()
                        await clear_draft()
                    mapped = await settled_outcome(snapshot, closing=True)
                    if mapped is None:
                        raise
                    ended = mapped
                    close_after_finish = not close_attempted
                elif snapshot.finished == "unknown":
                    await mark_running()
                    await clear_draft()
                    mapped = await settled_outcome(snapshot, closing=True)
                    ended = mapped or ("unknown", "internal")
                else:
                    raise
                final_seen = snapshot
                cancelled = True
            except Exception:
                if submitted is not None:
                    await self.commands.stop(submitted)
                ended = ("unknown" if dispatching else "failed"), "internal"
            tested = None
            if attempt.kind == "manual" and ended in (
                ("succeeded", ""),
                ("blocked", "needs_decision"),
            ):
                tested = (attempt.routine_id, attempt.revision)
            now = stamp(self.clock.wall())
            # Outside the recovery handler: cancellation joins this one write, then propagates.
            changed = await storage_call(
                lambda: self.store.finish(
                    attempt_id, *ended, now, native_version=final_seen.native_version, tested=tested
                )
            )
            detach_observers()
            await clear_draft()
            task = asyncio.current_task()
            if close_after_finish or (
                prepared and task is not None and not task.cancelling() and not close_attempted
            ):
                await close_session()
            if cancelled:
                raise asyncio.CancelledError
            if (
                changed is True
                and self.attempt_finished is not None
                and not self._closing
                and ended[0] != "cancelled"
                and task is not None
                and not task.cancelling()
                and (not prepared or close_succeeded)
                and self.manager.get(session_id) is None
            ):
                try:
                    await self.attempt_finished(attempt, *ended)
                except Exception:
                    logger.warning("routine bot completion failed")
        finally:
            try:
                detach_observers()
                task = asyncio.current_task()
                if prepared and task is not None and not task.cancelling() and not close_attempted:
                    # Frees its live slot; the session stays listed and reopens by resume.
                    await close_session()
            finally:
                if self.discard_attempt is not None:
                    try:
                        self.discard_attempt(attempt)
                    except Exception:
                        logger.warning("routine bot discard failed")
