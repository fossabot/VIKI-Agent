"""Tests for `Observation`, `outcome()` and the task store: claims, manual runs, receipts,
pruning, reconcile and the run history; and for the runner, over the profile launch rig."""

from __future__ import annotations

import asyncio
import contextlib
import sqlite3
import sys
import threading
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import httpx
import pytest
from anyio import CapacityLimiter
from anyio.to_thread import current_default_thread_limiter

from tests.automation.test_routines import _document_raw
from tests.automation.test_scheduler import FakeClock
from tests.profiles.test_profile_document import document as profile_document
from tests.profiles.test_profile_launch_api import BASE, HANG_GUARD_S, TOKEN, FakeDriver, Rig
from viki_agent import storage
from viki_agent.app.data_api import ResetService
from viki_agent.automation.routines import MAX_RECEIPTS, RoutineError, RoutineService, stamp
from viki_agent.automation.tasks import (
    MAX_OCCURRENCES,
    Attempt,
    Observation,
    TaskRunner,
    TaskStore,
    Waiting,
    outcome,
)
from viki_agent.conversations.drafts import Draft, DraftError, DraftService, draft_scope
from viki_agent.drivers.base import PermissionAnswer, StartError
from viki_agent.drivers.claude.control import Launch
from viki_agent.drivers.claude.driver import DriverConfig
from viki_agent.events import (
    Error,
    Event,
    PermissionRequested,
    Question,
    QuestionAsked,
    QuestionOption,
    RateLimit,
    RequestAnswered,
    RequestCancelled,
    SessionClosed,
    SessionStarted,
    TextDelta,
    TurnDone,
    Usage,
)
from viki_agent.profiles.profile_capabilities import FIXTURE_VERSION
from viki_agent.profiles.profile_launch import ProfileLaunchService
from viki_agent.profiles.service import ProfileError, ProfileService
from viki_agent.server import create_app
from viki_agent.session import SessionManager
from viki_agent.state import AppState, Prefs
from viki_agent.storage import SessionStore, StateError
from viki_agent.turn_input import TurnInput

SID = "s1"


def envelopes(*events: Event) -> list[dict[str, Any]]:
    """Give each event the next sequence number, mimicking the actor's `_emit`."""
    return [event.envelope(SID, i + 1) for i, event in enumerate(events)]


def observe(*events: Event) -> Observation:
    seen = Observation()
    for envelope in envelopes(*events):
        seen(envelope)
    return seen


# --- the outcome table, one test per row ------------------------------------------------------


def test_row1_expired_waiting_for_user() -> None:
    seen = observe(PermissionRequested(tool_use_id="t1", kind="tool", tool="Bash", summary="run"))
    assert outcome(seen, deadline=True) == ("expired", "waiting_for_user")


def test_row2_expired_deadline() -> None:
    seen = observe()
    assert outcome(seen, deadline=True) == ("expired", "deadline")


def test_row3_blocked_quota() -> None:
    seen = observe(RateLimit(status="rejected"), TurnDone(status="success", usage=Usage()))
    assert outcome(seen, deadline=False) == ("blocked", "quota")


def test_row4_succeeded() -> None:
    seen = observe(TurnDone(status="success", usage=Usage()))
    assert outcome(seen, deadline=False) == ("succeeded", "")


def test_row4_a_recoverable_error_before_or_after_a_completed_turn_is_succeeded() -> None:
    """The driver's unmappable frame (`internal`) or bad JSON line (`frame_error`) leaves the
    session going; a turn that still completes succeeded."""
    before = observe(
        Error(code="internal", message="", recoverable=True),
        TurnDone(status="success", usage=Usage()),
    )
    after = observe(
        TurnDone(status="success", usage=Usage()),
        Error(code="frame_error", message="", recoverable=True),
    )
    assert outcome(before, deadline=False) == outcome(after, deadline=False) == ("succeeded", "")


def test_row5_cancelled_stopped_after_forced_stop() -> None:
    seen = observe(
        Error(code="forced_stop", message="", recoverable=True),
        TurnDone(status="cancelled", usage=Usage()),
    )
    assert outcome(seen, deadline=False) == ("cancelled", "stopped")


def test_row5_a_recoverable_error_in_a_stopped_turn_is_cancelled_stopped() -> None:
    seen = observe(
        TurnDone(status="cancelled", usage=Usage()),
        Error(code="internal", message="", recoverable=True),
    )
    assert outcome(seen, deadline=False) == ("cancelled", "stopped")


def test_row6_failed_crash_over_turn_done() -> None:
    """A crash mid-turn emits `turn.done` error then `error crashed` together; the crash code
    wins, so this is `failed/crashed`, not `failed/turn_error`."""
    seen = observe(
        TurnDone(status="error", usage=Usage()),
        Error(code="crashed", message="", recoverable=True),
    )
    assert outcome(seen, deadline=False) == ("failed", "crashed")


def test_row7_failed_turn_error() -> None:
    seen = observe(TurnDone(status="error", usage=Usage()))
    assert outcome(seen, deadline=False) == ("failed", "turn_error")


def test_a_stopped_card_turns_only_success_into_needs_decision() -> None:
    card = PermissionRequested(tool_use_id="t1", kind="tool", tool="Bash", summary="run")
    done = observe(card, TurnDone(status="success", usage=Usage()))
    failed = observe(card, TurnDone(status="error", usage=Usage()))
    waiting, streaming = observe(card), observe()
    for seen in (done, failed, waiting, streaming):
        seen.needs_decision = True
    assert outcome(done, deadline=False) == ("blocked", "needs_decision")
    assert outcome(failed, deadline=False) == ("failed", "turn_error")
    assert outcome(waiting, deadline=True) == ("expired", "waiting_for_user")
    assert outcome(streaming, deadline=True) == ("expired", "deadline")
    assert outcome(done, deadline=True) == ("expired", "deadline")


def test_row8_blocked_needs_not_dispatched() -> None:
    seen = observe(Error(code="billing", message="", recoverable=False))
    seen.finish("not_dispatched")
    assert outcome(seen, deadline=False) == ("blocked", "billing")


def test_row9_cancelled_session_closed() -> None:
    seen = observe(SessionClosed(reason="requested"))
    assert outcome(seen, deadline=False) == ("cancelled", "session_closed")


def test_row10_cancelled_app_closed() -> None:
    seen = observe(SessionClosed(reason="browser_gone"))
    assert outcome(seen, deadline=False) == ("cancelled", "app_closed")


def test_row11_failed_command_reason() -> None:
    seen = observe(Error(code="bad_request", message="", recoverable=False))
    seen.finish("not_dispatched")
    assert outcome(seen, deadline=False, command_reason="start_failed") == (
        "failed",
        "start_failed",
    )


def test_row12_unknown_while_closing() -> None:
    seen = observe()
    seen.finish("unknown")
    assert outcome(seen, deadline=False, command_reason="internal", closing=True) == (
        "unknown",
        "internal",
    )


def test_row12_unknown_needs_closing() -> None:
    seen = observe()
    seen.finish("unknown")
    assert outcome(seen, deadline=False, closing=False) is None


def test_row13_nothing_ended_is_none() -> None:
    seen = observe()
    assert outcome(seen, deadline=False) is None


# --- precedence cases beyond the table ---------------------------------------------------------


def test_a_rejected_rate_limit_is_quota_only_once_the_run_ended() -> None:
    assert outcome(observe(RateLimit(status="rejected")), deadline=False) is None
    unended = observe(RateLimit(status="rejected"))
    unended.finish("unknown")
    assert outcome(unended, deadline=False, command_reason="cancelled", closing=True) == (
        "unknown",
        "cancelled",
    )


def test_a_blocked_code_needs_a_command_that_was_not_dispatched() -> None:
    seen = observe(Error(code="billing", message="", recoverable=False))
    seen.finish("unknown")
    assert outcome(seen, deadline=False) is None


def test_the_first_turn_status_wins() -> None:
    seen = observe(
        TurnDone(status="success", usage=Usage()), TurnDone(status="error", usage=Usage())
    )
    assert outcome(seen, deadline=False) == ("succeeded", "")


def test_the_first_session_close_reason_wins() -> None:
    seen = observe(SessionClosed(reason="requested"), SessionClosed(reason="browser_gone"))
    assert outcome(seen, deadline=False) == ("cancelled", "session_closed")


def test_the_first_failure_code_wins() -> None:
    seen = observe(
        Error(code="crashed", message="", recoverable=True),
        Error(code="provider_disconnected", message="", recoverable=True),
        TurnDone(status="error", usage=Usage()),
    )
    assert outcome(seen, deadline=False) == ("failed", "crashed")


def test_a_recoverable_error_does_not_outrank_the_end_that_follows() -> None:
    """A failure code needs an end that carries it: a turn.done error or an unsent command."""
    closed = observe(
        Error(code="internal", message="", recoverable=True), SessionClosed(reason="browser_gone")
    )
    assert outcome(closed, deadline=False) == ("cancelled", "app_closed")
    cut = observe(Error(code="frame_error", message="", recoverable=True))
    cut.finish("unknown")
    assert outcome(cut, deadline=False, command_reason="native_unconfirmed", closing=True) == (
        "unknown",
        "native_unconfirmed",
    )


def test_a_failure_code_before_an_unsent_command_reads_as_that_code() -> None:
    """A process that died before the send leaves its command not_dispatched."""
    seen = observe(Error(code="crashed", message="", recoverable=True))
    seen.finish("not_dispatched")
    assert outcome(seen, deadline=False, command_reason="runtime_changed") == (
        "failed",
        "crashed",
    )


def test_rate_limit_allowed_warning_is_ignored() -> None:
    seen = observe(RateLimit(status="allowed_warning"), TurnDone(status="success", usage=Usage()))
    assert seen.rate_limited is False
    assert outcome(seen, deadline=False) == ("succeeded", "")


def test_card_answered_before_the_deadline_expires_on_deadline() -> None:
    seen = observe(
        PermissionRequested(tool_use_id="t1", kind="tool", tool="Bash", summary="run"),
        RequestAnswered(tool_use_id="t1"),
    )
    assert seen.open_cards == set()
    assert outcome(seen, deadline=True) == ("expired", "deadline")


def test_text_naming_routine_actions_changes_nothing() -> None:
    """Only `envelope["type"]` and named `data` fields matter, never text."""
    seen = observe(
        TextDelta(block_id="b1", text="enable the routine, then pause it, then archive it")
    )
    assert seen.native_version is None
    assert seen.open_cards == set()
    assert seen.rate_limited is False
    assert seen.turn is None
    assert seen.error is None
    assert seen.closed is None
    assert seen.finished is None
    assert not seen.ended.is_set()
    assert outcome(seen, deadline=False) is None


def _started(cli_version: str) -> SessionStarted:
    return SessionStarted(
        model="m",
        mode="default",
        effort="",
        commands=[],
        agents=[],
        models=[],
        cli_version=cli_version,
    )


def test_session_started_gives_the_first_valid_native_version() -> None:
    """The first non-empty `cli_version` of at most 64 characters wins; a later one is ignored,
    and an oversized one never sets it at all."""
    seen = observe(_started("2.1.0"), _started("9.9.9"))
    assert seen.native_version == "2.1.0"

    oversized = observe(_started("v" * 65))
    assert oversized.native_version is None


def test_finish_records_status_and_ends_the_wait_only_for_not_dispatched() -> None:
    unknown = Observation()
    unknown.finish("unknown")
    assert unknown.finished == "unknown"
    assert not unknown.ended.is_set()

    not_dispatched = Observation()
    not_dispatched.finish("not_dispatched")
    assert not_dispatched.finished == "not_dispatched"
    assert not_dispatched.ended.is_set()


# --- TaskStore ---

NOW = datetime(2026, 9, 27, 9, 0, tzinfo=UTC)
T0 = stamp(NOW)
SCOPE = "ab" * 32
OWNER = "44444444-4444-4444-8444-4444444444ab"
PROFILE_ID = "11111111-1111-4111-8111-1111111111ab"


def _ident() -> str:
    return str(uuid.uuid4())


@pytest.fixture
def service(tmp_path: Path) -> RoutineService:
    store = AppState(tmp_path / "state.json", Prefs()).store
    return RoutineService(store, ProfileService(store), None, None, now=lambda: NOW)


@pytest.fixture
def tasks(service: RoutineService) -> TaskStore:
    return TaskStore(service.store, SCOPE, OWNER)


def _routine(service: RoutineService) -> str:
    return service.create(_ident(), _document_raw())[0]["routine_id"]


def _rows(tasks: TaskStore, sql: str, values: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    with tasks.store._connection() as connection:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute(sql, values)]


def _attempt(tasks: TaskStore, attempt_id: str) -> dict[str, Any]:
    return _rows(tasks, "SELECT * FROM task_attempts WHERE attempt_id=?", (attempt_id,))[0]


def _code(call: Any) -> str:
    with pytest.raises(RoutineError) as err:
        call()
    return err.value.code


def test_task_store_keeps_its_arguments(tasks: TaskStore, service: RoutineService) -> None:
    assert (tasks.store, tasks.scope, tasks.owner) == (service.store, SCOPE, OWNER)


def test_occupancy_is_none_then_run_active_then_busy(
    tasks: TaskStore, service: RoutineService
) -> None:
    first, second, third = (_routine(service) for _ in range(3))
    with tasks.store._connection(write=True) as connection:
        assert tasks.occupancy(connection, first) is None
        tasks.insert_claim(
            connection,
            routine_id=first,
            kind="manual",
            revision=1,
            now=T0,
            trigger_id=_ident(),
            request_hash="0" * 64,
        )
        assert tasks.occupancy(connection, first) == "run_active"
        assert tasks.occupancy(connection, second) is None
        tasks.insert_claim(
            connection, routine_id=second, kind="schedule", revision=1, now=T0, scheduled_for=T0
        )
        assert tasks.occupancy(connection, third) == "busy"
        assert tasks.occupancy(connection, second) == "run_active"


def test_a_done_attempt_no_longer_occupies(tasks: TaskStore, service: RoutineService) -> None:
    routine_id = _routine(service)
    attempt, _ = tasks.claim_manual(routine_id, _ident(), 1, T0)
    assert tasks.finish(attempt.attempt_id, "succeeded", "", T0)
    with tasks.store._connection() as connection:
        assert tasks.occupancy(connection, routine_id) is None


def test_claim_manual_writes_a_claimed_occurrence_and_attempt(
    tasks: TaskStore, service: RoutineService
) -> None:
    routine_id = _routine(service)
    command_id = _ident()
    attempt, created = tasks.claim_manual(routine_id, command_id, 1, T0)
    assert created is True
    assert (attempt.routine_id, attempt.revision, attempt.kind) == (routine_id, 1, "manual")
    assert len({attempt.attempt_id, attempt.occurrence_id, attempt.session_id}) == 3
    for value in (attempt.attempt_id, attempt.occurrence_id, attempt.session_id):
        assert uuid.UUID(value).version == 4
    [occurrence] = _rows(tasks, "SELECT * FROM routine_occurrences")
    assert occurrence == {
        "occurrence_id": attempt.occurrence_id,
        "routine_id": routine_id,
        "kind": "manual",
        "scheduled_for": None,
        "trigger_id": command_id,
        "request_hash": occurrence["request_hash"],
        "revision": 1,
        "state": "claimed",
        "reason": "",
        "recorded_at": T0,
    }
    assert len(occurrence["request_hash"]) == 64
    assert _attempt(tasks, attempt.attempt_id) == {
        "attempt_id": attempt.attempt_id,
        "origin_kind": "routine",
        "origin_id": attempt.occurrence_id,
        "scope": SCOPE,
        "session_id": attempt.session_id,
        "owner": OWNER,
        "state": "claimed",
        "outcome": None,
        "reason": "",
        "profile_id": None,
        "profile_revision": None,
        "native_version": None,
        "created_at": T0,
        "dispatched_at": None,
        "finished_at": None,
    }
    assert _rows(tasks, "SELECT last_slot FROM routines") == [{"last_slot": None}]
    assert _rows(tasks, "SELECT * FROM routine_commands WHERE command_id=?", (command_id,)) == [
        {
            "command_id": command_id,
            "operation": "run",
            "routine_id": routine_id,
            "request_hash": occurrence["request_hash"],
            "result_revision": 1,
            "result_state_revision": None,
            "result_state": None,
            "result_occurrence_id": attempt.occurrence_id,
            "result_attempt_id": attempt.attempt_id,
            "result_session_id": attempt.session_id,
            "created_at": T0,
        }
    ]


def test_a_manual_replay_returns_the_same_attempt(
    tasks: TaskStore, service: RoutineService
) -> None:
    routine_id = _routine(service)
    command_id = _ident()
    attempt, _ = tasks.claim_manual(routine_id, command_id, 1, T0)
    # A replay answers before the occupancy check its own open attempt would fail.
    later = stamp(NOW + timedelta(minutes=1))
    assert tasks.claim_manual(routine_id, command_id, 1, later) == (attempt, False)
    assert len(_rows(tasks, "SELECT * FROM task_attempts")) == 1


def test_a_reused_command_id_with_another_request_is_command_conflict(
    tasks: TaskStore, service: RoutineService
) -> None:
    routine_id, other = _routine(service), _routine(service)
    command_id = _ident()
    tasks.claim_manual(routine_id, command_id, 1, T0)
    assert _code(lambda: tasks.claim_manual(routine_id, command_id, 2, T0)) == ("command_conflict")
    assert _code(lambda: tasks.claim_manual(other, command_id, 1, T0)) == "command_conflict"


def test_manual_receipt_lookup_is_read_only(
    tasks: TaskStore, service: RoutineService, monkeypatch: pytest.MonkeyPatch
) -> None:
    routine_id, command_id = _routine(service), _ident()
    assert tasks.lookup_manual(routine_id, command_id, 1) is None
    attempt, _ = tasks.claim_manual(routine_id, command_id, 1, T0)
    connect = tasks.store._connection
    opened = []

    @contextlib.contextmanager
    def read_only(*args: Any, **kwargs: Any) -> Iterator[sqlite3.Connection]:
        assert not kwargs.get("write", False)
        with connect(*args, **kwargs) as connection:
            opened.append(True)
            yield connection

    monkeypatch.setattr(tasks.store, "_connection", read_only)
    assert tasks.lookup_manual(routine_id, command_id, 1) == attempt
    assert tasks.lookup_manual(routine_id, _ident(), 1) is None
    assert _code(lambda: tasks.lookup_manual(routine_id, command_id, 2)) == "command_conflict"
    assert _code(lambda: tasks.lookup_manual(_ident(), command_id, 1)) == "command_conflict"
    assert len(opened) == 4


def test_manual_and_definition_commands_share_one_namespace(
    tasks: TaskStore, service: RoutineService
) -> None:
    definition_command, run_command = _ident(), _ident()
    document = _document_raw()
    routine_id = service.create(definition_command, document)[0]["routine_id"]
    tasks.claim_manual(routine_id, run_command, 1, T0)
    assert _code(lambda: service.create(run_command, document)) == "command_conflict"
    # Even an equal hash cannot turn a definition receipt into a run receipt.
    with tasks.store._connection(write=True) as connection:
        connection.execute(
            "UPDATE routine_commands SET request_hash=(SELECT request_hash FROM "
            "routine_commands WHERE command_id=?) WHERE command_id=?",
            (run_command, definition_command),
        )
    assert _code(lambda: tasks.lookup_manual(routine_id, definition_command, 1)) == (
        "command_conflict"
    )
    assert _code(lambda: tasks.claim_manual(routine_id, definition_command, 1, T0)) == (
        "command_conflict"
    )
    assert len(_rows(tasks, "SELECT * FROM task_attempts")) == 1


def test_manual_claim_and_receipt_roll_back_together_before_pruning(
    tasks: TaskStore, service: RoutineService, monkeypatch: pytest.MonkeyPatch
) -> None:
    routine_id, command_id = _routine(service), _ident()

    def refuse_prune(connection: sqlite3.Connection, target: str) -> None:
        assert target == routine_id
        assert (
            connection.execute(
                "SELECT result_attempt_id,result_occurrence_id,result_session_id FROM "
                "routine_commands WHERE command_id=? AND operation='run'",
                (command_id,),
            ).fetchone()
            == connection.execute(
                "SELECT a.attempt_id,o.occurrence_id,a.session_id FROM task_attempts a "
                "JOIN routine_occurrences o ON o.occurrence_id=a.origin_id"
            ).fetchone()
        )
        raise RuntimeError("stop before pruning")

    monkeypatch.setattr(tasks, "prune", refuse_prune)
    with pytest.raises(RuntimeError, match="^stop before pruning$"):
        tasks.claim_manual(routine_id, command_id, 1, T0)
    assert _rows(tasks, "SELECT * FROM routine_commands WHERE operation='run'") == []
    assert _rows(tasks, "SELECT * FROM routine_occurrences") == []
    assert _rows(tasks, "SELECT * FROM task_attempts") == []


def test_manual_commands_share_the_lifetime_cap_and_replay_before_it(
    tasks: TaskStore, service: RoutineService
) -> None:
    assert MAX_RECEIPTS == 65_536
    routine_id, command_id = _routine(service), _ident()
    attempt, _ = tasks.claim_manual(routine_id, command_id, 1, T0)
    tasks.finish(attempt.attempt_id, "succeeded", "", T0)
    with tasks.store._connection(write=True) as connection:
        connection.executemany(
            "INSERT INTO routine_commands (command_id,operation,routine_id,request_hash,"
            "result_revision,result_state_revision,result_state,created_at) "
            "VALUES (?,'pause',?,?,1,1,'paused',?)",
            (
                (str(uuid.UUID(int=i + 1)), routine_id, "0" * 64, T0)
                for i in range(MAX_RECEIPTS - 3)
            ),
        )
        assert connection.execute("SELECT count(*) FROM routine_commands").fetchone() == (
            MAX_RECEIPTS - 1,
        )
    last_command = _ident()
    last, created = tasks.claim_manual(routine_id, last_command, 1, _at(1))
    assert created
    assert _code(lambda: tasks.claim_manual(routine_id, _ident(), 1, T0)) == "capacity"
    assert _code(lambda: tasks.claim_manual(routine_id, _ident(), 2, T0)) == "capacity"
    assert _code(lambda: tasks.claim_manual(_ident(), _ident(), 1, T0)) == "capacity"
    assert _code(lambda: service.create(_ident(), _document_raw())) == "capacity"
    with tasks.store._connection(write=True) as connection:
        connection.execute("UPDATE routines SET state='archived' WHERE routine_id=?", (routine_id,))
    assert tasks.claim_manual(routine_id, command_id, 1, _at(2)) == (attempt, False)
    assert tasks.claim_manual(routine_id, last_command, 1, _at(2)) == (last, False)
    assert _code(lambda: tasks.claim_manual(routine_id, command_id, 2, T0)) == "command_conflict"
    assert _rows(tasks, "SELECT count(*) AS total FROM routine_commands") == [
        {"total": MAX_RECEIPTS}
    ]
    assert len(_rows(tasks, "SELECT * FROM task_attempts")) == 2


def test_claim_manual_refusals(tasks: TaskStore, service: RoutineService) -> None:
    routine_id = _routine(service)
    assert _code(lambda: tasks.claim_manual(_ident(), _ident(), 1, T0)) == "not_found"
    assert _code(lambda: tasks.claim_manual(routine_id, _ident(), 2, T0)) == "conflict"
    assert _code(lambda: tasks.claim_manual("x", _ident(), 1, T0)) == "invalid_id"
    assert _code(lambda: tasks.claim_manual(routine_id, "x", 1, T0)) == "invalid_id"
    bad: tuple[Any, ...] = (0, True, 1.0, 1 << 53)
    for revision in bad:
        with pytest.raises(RoutineError, match="^invalid_revision$"):
            tasks.claim_manual(routine_id, _ident(), revision, T0)
    with tasks.store._connection(write=True) as connection:
        connection.execute("UPDATE routines SET state='archived' WHERE routine_id=?", (routine_id,))
    assert _code(lambda: tasks.claim_manual(routine_id, _ident(), 1, T0)) == ("invalid_transition")
    assert _rows(tasks, "SELECT * FROM routine_occurrences") == []


def test_manual_runs_refuse_run_active_then_busy(tasks: TaskStore, service: RoutineService) -> None:
    first, second, third = (_routine(service) for _ in range(3))
    tasks.claim_manual(first, _ident(), 1, T0)
    assert _code(lambda: tasks.claim_manual(first, _ident(), 1, T0)) == "run_active"
    tasks.claim_manual(second, _ident(), 1, T0)
    assert _code(lambda: tasks.claim_manual(third, _ident(), 1, T0)) == "busy"
    assert len(_rows(tasks, "SELECT * FROM task_attempts")) == 2


def test_attempt_transitions_move_forward_once(tasks: TaskStore, service: RoutineService) -> None:
    attempt, _ = tasks.claim_manual(_routine(service), _ident(), 1, T0)
    later = stamp(NOW + timedelta(seconds=1))
    assert tasks.mark_running(attempt.attempt_id, later) is False
    assert tasks.mark_dispatching(attempt.attempt_id, PROFILE_ID, 3) is True
    assert tasks.mark_dispatching(attempt.attempt_id, PROFILE_ID, 4) is False
    assert tasks.mark_running(attempt.attempt_id, later) is True
    assert tasks.mark_running(attempt.attempt_id, T0) is False
    row = _attempt(tasks, attempt.attempt_id)
    assert (row["state"], row["profile_id"], row["profile_revision"], row["dispatched_at"]) == (
        "running",
        PROFILE_ID,
        3,
        later,
    )
    assert tasks.mark_dispatching(_ident(), PROFILE_ID, 1) is False


def test_finish_twice_leaves_the_first_result(tasks: TaskStore, service: RoutineService) -> None:
    attempt, _ = tasks.claim_manual(_routine(service), _ident(), 1, T0)
    later = stamp(NOW + timedelta(seconds=5))
    assert tasks.finish(attempt.attempt_id, "succeeded", "", later, native_version="2.1.0")
    done = _attempt(tasks, attempt.attempt_id)
    assert (done["state"], done["outcome"], done["finished_at"], done["native_version"]) == (
        "done",
        "succeeded",
        later,
        "2.1.0",
    )
    assert tasks.finish(attempt.attempt_id, "failed", "internal", T0, native_version="9") is False
    assert _attempt(tasks, attempt.attempt_id) == done


def test_finish_keeps_a_recorded_native_version(tasks: TaskStore, service: RoutineService) -> None:
    attempt, _ = tasks.claim_manual(_routine(service), _ident(), 1, T0)
    with tasks.store._connection(write=True) as connection:
        connection.execute("UPDATE task_attempts SET native_version='2.0.0'")
    assert tasks.finish(attempt.attempt_id, "expired", "deadline", T0)
    assert _attempt(tasks, attempt.attempt_id)["native_version"] == "2.0.0"


def _at(minutes: int) -> str:
    return stamp(NOW + timedelta(minutes=minutes))


def test_prune_keeps_the_cap_and_every_open_attempt(
    tasks: TaskStore, service: RoutineService
) -> None:
    routine_id, other = _routine(service), _routine(service)
    with tasks.store._connection(write=True) as connection:
        # The oldest occurrence has an open attempt; the next one has a done attempt.
        open_one = tasks.insert_claim(
            connection,
            routine_id=routine_id,
            kind="schedule",
            revision=1,
            now=_at(0),
            scheduled_for=_at(0),
        )
        done_one = tasks.insert_claim(
            connection,
            routine_id=routine_id,
            kind="schedule",
            revision=1,
            now=_at(1),
            scheduled_for=_at(1),
        )
        connection.execute(
            "UPDATE task_attempts SET state='done',outcome='failed',reason='internal',"
            "finished_at=? WHERE attempt_id=?",
            (_at(1), done_one.attempt_id),
        )
        for minute in range(2, MAX_OCCURRENCES + 2):
            tasks.insert_skip(
                connection,
                routine_id=routine_id,
                scheduled_for=_at(minute),
                revision=1,
                reason="missed",
                now=_at(minute),
            )
        tasks.insert_skip(
            connection,
            routine_id=other,
            scheduled_for=_at(0),
            revision=1,
            reason="busy",
            now=_at(0),
        )
        tasks.prune(connection, routine_id)
        tasks.prune(connection, other)
    kept = _rows(
        tasks,
        "SELECT occurrence_id,scheduled_for FROM routine_occurrences WHERE routine_id=? "
        "ORDER BY recorded_at",
        (routine_id,),
    )
    assert len(kept) == MAX_OCCURRENCES
    assert kept[0]["occurrence_id"] == open_one.occurrence_id
    # The done one and the oldest skip went; the open one stayed.
    assert kept[1]["scheduled_for"] == _at(3)
    attempts = _rows(tasks, "SELECT attempt_id FROM task_attempts")
    assert attempts == [{"attempt_id": open_one.attempt_id}]
    assert len(_rows(tasks, "SELECT * FROM routine_occurrences WHERE routine_id=?", (other,))) == 1


def test_prune_never_deletes_open_attempts_even_past_the_cap(
    tasks: TaskStore, service: RoutineService
) -> None:
    routine_id = _routine(service)
    with tasks.store._connection(write=True) as connection:
        for minute in range(MAX_OCCURRENCES + 1):
            tasks.insert_claim(
                connection,
                routine_id=routine_id,
                kind="schedule",
                revision=1,
                now=_at(minute),
                scheduled_for=_at(minute),
            )
        tasks.prune(connection, routine_id)
    assert len(_rows(tasks, "SELECT * FROM routine_occurrences")) == MAX_OCCURRENCES + 1


def test_a_manual_run_prunes_its_routine_to_the_cap(
    tasks: TaskStore, service: RoutineService
) -> None:
    routine_id = _routine(service)
    with tasks.store._connection(write=True) as connection:
        for minute in range(MAX_OCCURRENCES):
            tasks.insert_skip(
                connection,
                routine_id=routine_id,
                scheduled_for=_at(minute),
                revision=1,
                reason="missed",
                now=_at(minute),
            )
    [oldest] = _rows(
        tasks, "SELECT occurrence_id FROM routine_occurrences WHERE scheduled_for=?", (_at(0),)
    )
    attempt, _ = tasks.claim_manual(routine_id, _ident(), 1, _at(MAX_OCCURRENCES))
    ids = {row["occurrence_id"] for row in _rows(tasks, "SELECT * FROM routine_occurrences")}
    assert len(ids) == MAX_OCCURRENCES
    assert oldest["occurrence_id"] not in ids and attempt.occurrence_id in ids


def test_every_written_row_meets_the_schema(tasks: TaskStore, service: RoutineService) -> None:
    routine_id = _routine(service)
    with tasks.store._connection(write=True) as connection:
        scheduled = tasks.insert_claim(
            connection,
            routine_id=routine_id,
            kind="schedule",
            revision=1,
            now=_at(0),
            scheduled_for=_at(0),
        )
        reasons: tuple[Literal["missed", "overlap", "busy"], ...] = ("missed", "overlap", "busy")
        for minute, reason in enumerate(reasons, 1):
            tasks.insert_skip(
                connection,
                routine_id=routine_id,
                scheduled_for=_at(minute),
                revision=1,
                reason=reason,
                now=_at(minute),
            )
    tasks.mark_dispatching(scheduled.attempt_id, PROFILE_ID, 1)
    tasks.mark_running(scheduled.attempt_id, _at(4))
    tasks.finish(scheduled.attempt_id, "succeeded", "", _at(5), native_version="2.1.0")
    manual, _ = tasks.claim_manual(routine_id, _ident(), 1, _at(6))
    assert isinstance(manual, Attempt)
    skipped = _rows(
        tasks,
        "SELECT kind,trigger_id,request_hash,state,reason "
        "FROM routine_occurrences WHERE state='skipped' ORDER BY recorded_at",
    )
    assert skipped == [
        {
            "kind": "schedule",
            "trigger_id": None,
            "request_hash": None,
            "state": "skipped",
            "reason": reason,
        }
        for reason in ("missed", "overlap", "busy")
    ]
    assert [
        row["state"] for row in _rows(tasks, "SELECT state FROM task_attempts ORDER BY created_at")
    ] == ["done", "claimed"]
    # The STRICT tables refuse a CHECK-breaking row on write; integrity_check re-reads them all.
    assert _rows(tasks, "PRAGMA integrity_check") == [{"integrity_check": "ok"}]


# --- decision claims ---

DAY = (T0, stamp(NOW + timedelta(days=1)))
CARDS = "d" * 64


def _ready(tasks: TaskStore, service: RoutineService) -> str:
    """An enabled routine tested at its one revision."""
    routine_id = _routine(service)
    with tasks.store._connection(write=True) as connection:
        connection.execute(
            "UPDATE routines SET state='enabled',tested_revision=1,enabled_since=? "
            "WHERE routine_id=?",
            (T0, routine_id),
        )
    return routine_id


def _decide(
    tasks: TaskStore,
    routine_id: str,
    *,
    revision: int = 1,
    newest: str | None = None,
    limit: int = 4,
    now: str = T0,
) -> Attempt:
    return tasks.claim_decision(
        routine_id, revision, newest=newest, request_hash=CARDS, limit=limit, day=DAY, now=now
    )


def _stored(tasks: TaskStore) -> list[list[dict[str, Any]]]:
    return [
        _rows(tasks, f"SELECT * FROM {table} ORDER BY rowid")
        for table in ("routine_occurrences", "task_attempts", "routines", "routine_commands")
    ]


def test_claim_decision_writes_a_decision_occurrence_and_attempt(
    tasks: TaskStore, service: RoutineService
) -> None:
    routine_id, owners = _ready(tasks, service), _ready(tasks, service)
    commands = _rows(tasks, "SELECT * FROM routine_commands")
    attempt = _decide(tasks, routine_id)
    assert (attempt.routine_id, attempt.revision, attempt.kind) == (routine_id, 1, "decision")
    assert _rows(tasks, "SELECT * FROM routine_occurrences") == [
        {
            "occurrence_id": attempt.occurrence_id,
            "routine_id": routine_id,
            "kind": "decision",
            "scheduled_for": None,
            "trigger_id": None,
            "request_hash": CARDS,
            "revision": 1,
            "state": "claimed",
            "reason": "",
            "recorded_at": T0,
        }
    ]
    row = _attempt(tasks, attempt.attempt_id)
    assert (row["origin_id"], row["session_id"], row["owner"], row["state"]) == (
        attempt.occurrence_id,
        attempt.session_id,
        OWNER,
        "claimed",
    )
    assert (row["created_at"], row["dispatched_at"]) == (T0, None)
    # The user's Start stores no cards hash; neither claim leaves a command receipt.
    started = tasks.claim_decision(
        owners, 1, newest=None, request_hash=None, limit=4, day=DAY, now=T0
    )
    assert _rows(
        tasks,
        "SELECT kind,request_hash FROM routine_occurrences WHERE occurrence_id=?",
        (started.occurrence_id,),
    ) == [{"kind": "decision", "request_hash": None}]
    assert _rows(tasks, "SELECT * FROM routine_commands") == commands
    with tasks.store._connection() as connection:
        assert tasks.newest(connection, routine_id) == attempt.attempt_id
        assert tasks.newest(connection, _ident()) is None
    assert _rows(tasks, "PRAGMA integrity_check") == [{"integrity_check": "ok"}]


def test_claim_decision_refusals_store_nothing(tasks: TaskStore, service: RoutineService) -> None:
    draft, paused, revised = _routine(service), _ready(tasks, service), _ready(tasks, service)
    with tasks.store._connection(write=True) as connection:
        connection.execute(
            "UPDATE routines SET state='paused',enabled_since=NULL WHERE routine_id=?", (paused,)
        )
    # A revision keeps the routine enabled and its tested revision at 1.
    assert service.revise(revised, _ident(), 1, _document_raw())[0]["revision"] == 2
    ready, third, other = (_ready(tasks, service) for _ in range(3))
    running = _decide(tasks, ready)
    # The user's Start takes the second slot; VIKI's own start could not.
    started = tasks.claim_decision(
        third, 1, newest=None, request_hash=None, limit=4, day=DAY, now=T0
    )
    before = _stored(tasks)
    # Each case also breaks every later check it can, so only the first in order answers.
    assert _code(lambda: _decide(tasks, _ident())) == "not_found"
    assert _code(lambda: _decide(tasks, draft, newest=_ident(), limit=0)) == "invalid_transition"
    assert _code(lambda: _decide(tasks, paused)) == "invalid_transition"
    assert _code(lambda: _decide(tasks, revised, revision=3, newest=_ident(), limit=0)) == (
        "conflict"
    )
    assert _code(lambda: _decide(tasks, revised, revision=2, newest=_ident())) == "not_ready"
    assert _code(lambda: _decide(tasks, ready, limit=0)) == "conflict"  # it ran since the plan
    assert _code(lambda: _decide(tasks, ready, newest=running.attempt_id, limit=0)) == (
        "run_active"
    )
    assert _code(lambda: _decide(tasks, other, limit=0)) == "busy"
    assert _stored(tasks) == before
    assert tasks.finish(started.attempt_id, "blocked", "profile_blocked", _at(1))
    # With no run going, only the day's count is left to refuse: the sent run counts in it.
    assert tasks.mark_dispatching(running.attempt_id, PROFILE_ID, 1)
    assert tasks.mark_running(running.attempt_id, _at(1))
    assert tasks.finish(running.attempt_id, "succeeded", "", _at(1))
    before = _stored(tasks)
    assert _code(lambda: _decide(tasks, other, limit=1)) == "limit"
    assert _stored(tasks) == before


def test_vikis_own_claim_is_busy_while_any_routine_run_is_going(
    tasks: TaskStore, service: RoutineService
) -> None:
    """VIKI's own start (a cards hash) takes a slot only while no routine run is going, so a
    scheduled slot always finds the other one free; the user's Start (no hash) takes either."""
    first, second, third = (_ready(tasks, service) for _ in range(3))

    def owners(routine_id: str) -> Attempt:
        return tasks.claim_decision(
            routine_id, 1, newest=None, request_hash=None, limit=4, day=DAY, now=T0
        )

    running = _decide(tasks, first)
    before = _stored(tasks)
    assert _code(lambda: _decide(tasks, second)) == "busy"
    assert _stored(tasks) == before
    started = owners(second)
    assert _code(lambda: owners(third)) == "busy"  # both slots are taken
    for attempt in (running, started):
        assert tasks.finish(attempt.attempt_id, "blocked", "profile_blocked", _at(1))
    # Any routine run holds it, whoever started it.
    manual, _ = tasks.claim_manual(third, _ident(), 1, _at(2))
    assert _code(lambda: _decide(tasks, second, newest=started.attempt_id)) == "busy"
    assert tasks.finish(manual.attempt_id, "succeeded", "", _at(3))
    assert _decide(tasks, second, newest=started.attempt_id).kind == "decision"


def test_the_daily_count_takes_only_dispatched_or_open_decision_attempts(
    tasks: TaskStore, service: RoutineService
) -> None:
    routine_id = _routine(service)
    end = DAY[1]
    late = stamp(NOW + timedelta(days=1, minutes=-1))

    def claim(kind: Literal["schedule", "manual", "decision"], now: str) -> Attempt:
        with tasks.store._connection(write=True) as connection:
            return tasks.insert_claim(
                connection,
                routine_id=routine_id,
                kind=kind,
                revision=1,
                now=now,
                scheduled_for=now if kind == "schedule" else None,
                trigger_id=_ident() if kind == "manual" else None,
                request_hash=None if kind == "schedule" else CARDS,
            )

    claim("decision", T0)  # open, at the day's first instant
    sent = claim("decision", _at(1))
    assert tasks.mark_dispatching(sent.attempt_id, PROFILE_ID, 1)
    assert tasks.mark_running(sent.attempt_id, _at(1))
    assert tasks.finish(sent.attempt_id, "succeeded", "", _at(2))
    # Ended before the outbox accepted them: never sent, so they use none of the limit.
    unsent = claim("decision", _at(3))
    assert tasks.finish(unsent.attempt_id, "blocked", "profile_blocked", _at(3))
    marked = claim("decision", late)
    assert tasks.mark_dispatching(marked.attempt_id, PROFILE_ID, 1)
    assert tasks.finish(marked.attempt_id, "failed", "internal", late)
    claim("decision", end)  # tomorrow's
    claim("decision", stamp(NOW - timedelta(milliseconds=1)))  # yesterday's
    claim("schedule", _at(4))
    claim("manual", _at(5))
    with tasks.store._connection() as connection:
        assert tasks.team_count(connection, *DAY) == 2
    with tasks.store._connection(write=True) as connection:
        connection.execute(
            "UPDATE task_attempts SET state='done',outcome='failed',finished_at=?,"
            "dispatched_at=COALESCE(dispatched_at,created_at) WHERE state<>'done'",
            (end,),
        )
        # Sent and ended now, so they free their slots and today's count stays the same.
        assert tasks.team_count(connection, *DAY) == 2
    # A claim reads the same count against its limit.
    other = _ready(tasks, service)
    assert _code(lambda: _decide(tasks, other, limit=2)) == "limit"
    assert _decide(tasks, other, limit=3).kind == "decision"


def test_a_claim_left_by_a_crash_counts_for_nothing(
    tasks: TaskStore, service: RoutineService
) -> None:
    routine_id = _ready(tasks, service)
    crashed = _decide(TaskStore(tasks.store, SCOPE, DEAD), routine_id, limit=1)
    assert TaskStore(tasks.store, SCOPE, NEW).reconcile(LATER) == 1
    row = _attempt(tasks, crashed.attempt_id)
    assert (row["state"], row["outcome"], row["reason"], row["dispatched_at"]) == (
        "done",
        "cancelled",
        "restarted",
        None,
    )
    with tasks.store._connection() as connection:
        assert tasks.team_count(connection, *DAY) == 0
        assert tasks.newest(connection, routine_id) == crashed.attempt_id
    # Its slot of the day is free again; the next claim names it as the newest.
    assert _code(lambda: _decide(tasks, routine_id, limit=1, now=_at(1))) == "conflict"
    again = _decide(tasks, routine_id, newest=crashed.attempt_id, limit=1, now=_at(1))
    with tasks.store._connection() as connection:
        assert tasks.newest(connection, routine_id) == again.attempt_id


# --- reconcile and the run history ---

DEAD = "55555555-5555-4555-8555-5555555555ab"
NEW = "66666666-6666-4666-8666-6666666666ab"
LATER = stamp(NOW + timedelta(hours=1))
OPEN = ("claimed", "dispatching", "running")
# Only a claimed attempt without a command, or a not_dispatched command, was certainly never sent.
RECONCILE: dict[str | None, dict[str, str]] = {
    None: {"claimed": "cancelled", "dispatching": "unknown", "running": "unknown"},
    "accepted": dict.fromkeys(OPEN, "unknown"),
    "dispatching": dict.fromkeys(OPEN, "unknown"),
    "unknown": dict.fromkeys(OPEN, "unknown"),
    "not_dispatched": dict.fromkeys(OPEN, "cancelled"),
}


def _open(tasks: TaskStore, routine_id: str, minute: int, state: str) -> Attempt:
    with tasks.store._connection(write=True) as connection:
        attempt = tasks.insert_claim(
            connection,
            routine_id=routine_id,
            kind="schedule",
            revision=1,
            now=_at(minute),
            scheduled_for=_at(minute),
        )
    if state != "claimed":
        assert tasks.mark_dispatching(attempt.attempt_id, PROFILE_ID, 1)
    if state == "running":
        assert tasks.mark_running(attempt.attempt_id, _at(minute))
    return attempt


def _seed_command(tasks: TaskStore, command_id: str, state: str) -> None:
    """A raw command row of the dead launch, keyed as the runner keys it (`command_id =
    attempt_id`)."""
    with tasks.store._connection(write=True) as connection:
        connection.execute(
            f"INSERT INTO commands VALUES ({','.join('?' * 15)})",
            (
                SCOPE,
                command_id,
                _ident(),
                _ident(),
                _ident(),
                1,
                "c" * 64,
                DEAD,
                state,
                "internal" if state in ("unknown", "not_dispatched") else "",
                b"x",
                1,
                1,
                T0,
                T0,
            ),
        )


def _results(tasks: TaskStore) -> dict[str, tuple[str, str | None, str, str | None]]:
    return {
        row["attempt_id"]: (row["state"], row["outcome"], row["reason"], row["finished_at"])
        for row in _rows(tasks, "SELECT * FROM task_attempts")
    }


def _untouched(tasks: TaskStore) -> list[list[dict[str, Any]]]:
    return [
        _rows(tasks, f"SELECT * FROM {table} ORDER BY rowid")
        for table in ("commands", "routine_occurrences", "routines", "routine_revisions")
    ]


def test_reconcile_ends_a_dead_launchs_attempts_by_the_table(
    tasks: TaskStore, service: RoutineService
) -> None:
    routine_id = _routine(service)
    dead = TaskStore(tasks.store, SCOPE, DEAD)
    expected: dict[str, tuple[str, str | None, str, str | None]] = {}
    for minute, (command, by_state) in enumerate(
        (command, by_state) for command, table in RECONCILE.items() for by_state in table.items()
    ):
        state, result = by_state
        attempt = _open(dead, routine_id, minute, state)
        if command is not None:
            _seed_command(tasks, attempt.attempt_id, command)
        expected[attempt.attempt_id] = ("done", result, "restarted", LATER)
    # Neither this launch's open attempt nor a finished one is the dead launch's to settle.
    mine = _open(tasks, routine_id, 100, "running")
    finished = _open(dead, routine_id, 101, "running")
    assert dead.finish(finished.attempt_id, "succeeded", "", _at(102))
    before = _results(tasks)
    rest = _untouched(tasks)
    assert tasks.reconcile(LATER) == len(expected) == 15
    assert _results(tasks) == {
        **expected,
        mine.attempt_id: before[mine.attempt_id],
        finished.attempt_id: before[finished.attempt_id],
    }
    assert _untouched(tasks) == rest
    assert tasks.reconcile(LATER) == 0


def test_reconcile_after_a_failed_one_and_a_commands_reset_reads_sent_as_unknown(
    tasks: TaskStore, service: RoutineService
) -> None:
    routine_id = _routine(service)
    dead = TaskStore(tasks.store, SCOPE, DEAD)
    claimed, dispatching, running = (
        _open(dead, routine_id, minute, state) for minute, state in enumerate(OPEN)
    )
    _seed_command(tasks, dispatching.attempt_id, "accepted")
    _seed_command(tasks, running.attempt_id, "accepted")
    before = _results(tasks)
    after = TaskStore(tasks.store, SCOPE, NEW)
    # A stamp that breaks the finished_at CHECK makes this reconcile fail inside its transaction.
    with pytest.raises(StateError) as err:
        after.reconcile("bad")
    assert err.value.code == "invalid_input"
    assert _results(tasks) == before
    tasks.store.delete_scope_rows("commands", SCOPE, NEW)
    assert _rows(tasks, "SELECT * FROM commands") == []
    assert TaskStore(tasks.store, SCOPE, NEW).reconcile(LATER) == 3
    assert _results(tasks) == {
        claimed.attempt_id: ("done", "cancelled", "restarted", LATER),
        dispatching.attempt_id: ("done", "unknown", "restarted", LATER),
        running.attempt_id: ("done", "unknown", "restarted", LATER),
    }


def _history(tasks: TaskStore, service: RoutineService) -> tuple[str, list[str]]:
    """A routine with a finished run, a skip, an open manual run and two skips recorded at the
    same instant; another routine's skip. Returns the occurrence ids, oldest first."""
    routine_id, other = _routine(service), _routine(service)
    done = _open(tasks, routine_id, 0, "running")
    tasks.finish(done.attempt_id, "succeeded", "", _at(1), native_version="2.1.0")
    with tasks.store._connection(write=True) as connection:
        tasks.insert_skip(
            connection,
            routine_id=routine_id,
            scheduled_for=_at(1),
            revision=1,
            reason="overlap",
            now=_at(1),
        )
        for scheduled in (_at(3), _at(4)):
            tasks.insert_skip(
                connection,
                routine_id=routine_id,
                scheduled_for=scheduled,
                revision=1,
                reason="missed",
                now=_at(5),
            )
        tasks.insert_skip(
            connection,
            routine_id=other,
            scheduled_for=_at(9),
            revision=1,
            reason="busy",
            now=_at(9),
        )
    manual, _ = tasks.claim_manual(routine_id, _ident(), 1, _at(2))
    ids = [
        row["occurrence_id"]
        for row in _rows(
            tasks,
            "SELECT occurrence_id FROM routine_occurrences WHERE routine_id=? "
            "ORDER BY COALESCE(scheduled_for,recorded_at),occurrence_id",
            (routine_id,),
        )
    ]
    assert ids[0] == done.occurrence_id and ids[2] == manual.occurrence_id
    return routine_id, ids


def test_runs_lists_newest_first_with_attempt_receipts(
    tasks: TaskStore, service: RoutineService
) -> None:
    routine_id, ids = _history(tasks, service)
    runs = tasks.runs(routine_id, 50, None)
    assert [run["occurrence_id"] for run in runs] == ids[::-1]
    assert runs[-1] == {
        "occurrence_id": ids[0],
        "kind": "schedule",
        "scheduled_for": _at(0),
        "trigger_id": None,
        "revision": 1,
        "state": "claimed",
        "reason": "",
        "recorded_at": _at(0),
        "attempt": {
            "attempt_id": runs[-1]["attempt"]["attempt_id"],
            "session_id": runs[-1]["attempt"]["session_id"],
            "state": "done",
            "outcome": "succeeded",
            "reason": "",
            "profile_id": PROFILE_ID,
            "profile_revision": 1,
            "native_version": "2.1.0",
            "created_at": _at(0),
            "dispatched_at": _at(0),
            "finished_at": _at(1),
        },
    }
    assert runs[-2]["attempt"] is None and runs[-2]["reason"] == "overlap"
    assert runs[-3]["kind"] == "manual" and runs[-3]["attempt"]["state"] == "claimed"


def test_runs_pages_with_before(tasks: TaskStore, service: RoutineService) -> None:
    routine_id, ids = _history(tasks, service)
    first = tasks.runs(routine_id, 2, None)
    second = tasks.runs(routine_id, 2, first[-1]["occurrence_id"])
    third = tasks.runs(routine_id, 2, second[-1]["occurrence_id"])
    assert [run["occurrence_id"] for run in first + second + third] == ids[::-1]
    assert tasks.runs(routine_id, 2, ids[0]) == []


def test_runs_refusals(tasks: TaskStore, service: RoutineService) -> None:
    routine_id, ids = _history(tasks, service)
    [foreign] = _rows(
        tasks, "SELECT occurrence_id FROM routine_occurrences WHERE routine_id<>?", (routine_id,)
    )
    bad_limits: tuple[Any, ...] = (0, 51, True, 1.0, "1")
    for limit in bad_limits:
        with pytest.raises(RoutineError, match="^invalid_input$"):
            tasks.runs(routine_id, limit, None)
    for before in (_ident(), foreign["occurrence_id"], "x"):
        with pytest.raises(RoutineError, match="^invalid_input$"):
            tasks.runs(routine_id, 1, before)
    assert len(tasks.runs(routine_id, 1, ids[-1])) == 1


def test_summaries_give_each_routines_last_and_active_run(
    tasks: TaskStore, service: RoutineService
) -> None:
    assert tasks.summaries() == {}
    routine_id, _ = _history(tasks, service)
    finished, idle = _routine(service), _routine(service)
    done = _open(tasks, finished, 0, "claimed")
    tasks.finish(done.attempt_id, "failed", "internal", _at(1))
    summaries = tasks.summaries()
    [active] = _rows(tasks, "SELECT * FROM task_attempts WHERE state<>'done'")
    runs = tasks.runs(routine_id, 50, None)
    # Two newer skips lead `last_run`; the newest run with an attempt is the open manual one.
    assert runs[0]["attempt"] is None and runs[2]["kind"] == "manual"
    assert summaries[routine_id] == {
        "last_run": runs[0],
        "last_attempt": runs[2],
        "active_run": {
            "attempt_id": active["attempt_id"],
            "session_id": active["session_id"],
            "state": "claimed",
            "created_at": active["created_at"],
            "revision": 1,
        },
    }
    [last] = tasks.runs(finished, 1, None)
    assert summaries[finished] == {"last_run": last, "last_attempt": last, "active_run": None}
    assert summaries[finished]["last_run"]["attempt"]["outcome"] == "failed"
    assert idle not in summaries
    assert len(summaries) == 3
    [other] = set(summaries) - {routine_id, finished}
    assert summaries[other]["last_run"]["reason"] == "busy"
    assert summaries[other]["last_attempt"] is None


def test_effective_history_order_pages_and_survives_restart(
    tasks: TaskStore, service: RoutineService, monkeypatch: pytest.MonkeyPatch
) -> None:
    routine_id = _routine(service)
    manual, _ = tasks.claim_manual(routine_id, _ident(), 1, _at(2))
    fixed = iter(uuid.UUID(f"00000000-0000-4000-8000-{number:012x}") for number in (4, 3, 2))
    with monkeypatch.context() as patch:
        patch.setattr(uuid, "uuid4", lambda: next(fixed))
        with tasks.store._connection(write=True) as connection:
            for minute in (1, 3, 4):
                tasks.insert_skip(
                    connection,
                    routine_id=routine_id,
                    scheduled_for=_at(minute),
                    revision=1,
                    reason="missed",
                    now=_at(5),
                )
    by_due = {
        row["scheduled_for"]: row["occurrence_id"]
        for row in _rows(tasks, "SELECT occurrence_id,scheduled_for FROM routine_occurrences")
        if row["scheduled_for"] is not None
    }
    expected = [by_due[_at(4)], by_due[_at(3)], manual.occurrence_id, by_due[_at(1)]]

    def check_history(reader: TaskStore) -> None:
        first = reader.runs(routine_id, 2, None)
        second = reader.runs(routine_id, 2, first[-1]["occurrence_id"])
        assert [row["occurrence_id"] for row in first + second] == expected
        assert reader.runs(routine_id, 2, second[-1]["occurrence_id"]) == []
        assert reader.summaries()[routine_id]["last_run"]["occurrence_id"] == expected[0]
        assert _code(lambda: reader.runs(routine_id, 1, _ident())) == "invalid_input"

    check_history(tasks)
    reopened = SessionStore(tasks.store.path, lambda: {})
    check_history(TaskStore(reopened, SCOPE, NEW))


def test_prune_keeps_the_newest_due_slots_from_one_missed_pass(
    tasks: TaskStore, service: RoutineService, monkeypatch: pytest.MonkeyPatch
) -> None:
    routine_id = _routine(service)
    fixed = iter(
        uuid.UUID(f"00000000-0000-4000-8000-{number:012x}")
        for number in range(MAX_OCCURRENCES + 1, 0, -1)
    )
    with monkeypatch.context() as patch:
        patch.setattr(uuid, "uuid4", lambda: next(fixed))
        with tasks.store._connection(write=True) as connection:
            for minute in range(MAX_OCCURRENCES + 1):
                tasks.insert_skip(
                    connection,
                    routine_id=routine_id,
                    scheduled_for=_at(minute),
                    revision=1,
                    reason="missed",
                    now=_at(MAX_OCCURRENCES + 1),
                )
            tasks.prune(connection, routine_id)
    kept = _rows(
        tasks,
        "SELECT scheduled_for FROM routine_occurrences WHERE routine_id=?",
        (routine_id,),
    )
    assert len(kept) == MAX_OCCURRENCES
    kept_times = {row["scheduled_for"] for row in kept}
    assert _at(0) not in kept_times and _at(MAX_OCCURRENCES) in kept_times
    assert tasks.runs(routine_id, 1, None)[0]["scheduled_for"] == _at(MAX_OCCURRENCES)
    assert tasks.summaries()[routine_id]["last_run"]["scheduled_for"] == _at(MAX_OCCURRENCES)


# --- the runner, over the profile launch rig served in this loop ---


async def _nothing() -> None:
    pass


class TurnDriver(FakeDriver):
    """Ends each sent turn with its stack's scripted events, after the stack's `before_send`,
    and opens after its `before_open`. A scripted card stays pending until answered; its stack
    records every answer."""

    def __init__(self, stack: Stack, launch: Launch, emit: Callable[[Event], None]) -> None:
        super().__init__(launch, emit, stack.rig.state, stack.rig.catalog)
        self.stack = stack
        self.cards: list[str] = []

    async def send(self, text: str) -> None:
        self.stack.before_send()
        await super().send(text)
        self.stack.texts.append(text)
        for event in self.stack.script:
            if isinstance(event, PermissionRequested | QuestionAsked):
                self.cards.append(event.tool_use_id)
            if isinstance(event, RequestCancelled):
                self.cards.remove(event.tool_use_id)
            self.emit(event)

    async def open(self) -> SessionStarted:
        await self.stack.before_open()
        return await super().open()

    def pending(self) -> list[str]:
        return list(self.cards)

    def respond(self, tool_use_id: str, answer: Any) -> bool:
        if tool_use_id not in self.cards:  # as the real driver: answered or cancelled
            return False
        self.cards.remove(tool_use_id)
        self.stack.answers.append(answer)
        self.emit(RequestAnswered(tool_use_id=tool_use_id))  # as the real driver closes a card
        return True


class Stack:
    """`Rig` served by `create_app` under its lifespan in this loop. `prepare` is the app's own
    routine preparation, which raises `ProfileError` as a run receives it."""

    def __init__(self, tmp_path: Path) -> None:
        self.rig = Rig(tmp_path)
        self.clock = FakeClock(NOW)
        self.script: tuple[Event, ...] = (TurnDone(status="success", usage=Usage()),)
        self.before_send: Callable[[], object] = lambda: None
        self.before_open: Callable[[], Awaitable[object]] = _nothing
        self.texts: list[str] = []
        self.answers: list[object] = []
        self.manager = SessionManager(
            config=DriverConfig(cli=("synthetic",), vault=self.rig.vault, env={}),
            state=self.rig.state,
            claude_home=self.rig.home,
            make_driver=self._driver,
        )
        rig = self.rig
        # Rig.app's arguments, plus this clock, so the app's scheduler runs on the test's time.
        self.app = create_app(
            config=rig.config,
            token=TOKEN,
            manager=self.manager,
            catalog=rig.catalog,
            static_dir=None,
            settings=rig.settings,
            workspaces=rig.workspaces,
            readiness=rig.readiness,
            vault_poll_s=60,
            routine_clock=self.clock,
        )
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url=BASE)

    def _driver(self, launch: Launch, _prefs: Prefs, emit: Callable[[Event], None]) -> TurnDriver:
        driver = TurnDriver(self, launch, emit)
        self.rig.drivers.append(driver)
        return driver

    def pass_deadline(self) -> None:
        """Move the clocks to a run's deadline and wake its wait."""
        self.clock.advance(60 * _document_raw()["deadline_minutes"])
        self.clock.tick()

    async def prepare(self, profile_id: str, raw: dict[str, Any]) -> dict[str, Any]:
        # The server's own routine preparation (`prepare_routine`), as its runner calls it: a
        # routine session is stamped `routine`, which the public start-preview never stamps.
        return await self.app.state.routine_tasks.prepare(profile_id, raw)

    def runner(self, *, admission_lock: asyncio.Lock | None = None) -> TaskRunner:
        store = self.rig.state.store
        return TaskRunner(
            TaskStore(store, draft_scope(self.rig.vault), OWNER),
            RoutineService(
                store,
                ProfileService(store),
                self.rig.settings,
                self.rig.workspaces,
                now=self.clock.wall,
            ),
            self.app.state.commands,
            self.manager,
            DraftService(self.rig.state, self.rig.vault),
            ProfileService(store),
            prepare=self.prepare,
            clock=self.clock,
            attended=lambda: True,
            admission_lock=admission_lock,
        )


@pytest.fixture
async def stack(tmp_path: Path) -> AsyncIterator[Stack]:
    stack = Stack(tmp_path)
    async with stack.app.router.lifespan_context(stack.app), stack.client:
        yield stack


def _raw(stack: Stack) -> dict[str, Any]:
    return _document_raw(profile_id=stack.rig.profile_id, workspace_id=stack.rig.workspace_id)


def _made(stack: Stack, *, admission_lock: asyncio.Lock | None = None) -> tuple[TaskRunner, str]:
    """A runner and a routine over the stack's profile and workspace."""
    runner = stack.runner(admission_lock=admission_lock)
    return runner, runner.routines.create(_ident(), _raw(stack))[0]["routine_id"]


def _await_path(task: asyncio.Task[Any]) -> str:
    current: Any = task.get_coro()
    path: list[str] = []
    for _ in range(8):
        code = getattr(current, "cr_code", None) or getattr(current, "gi_code", None)
        frame = getattr(current, "cr_frame", None) or getattr(current, "gi_frame", None)
        if code is None:
            path.append(type(current).__name__)
            break
        location = f"{Path(code.co_filename).name}:{code.co_name}"
        path.append(f"{location}:{frame.f_lineno if frame else 0}")
        current = getattr(current, "cr_await", None) or getattr(current, "gi_yieldfrom", None)
        if current is None:
            break
    else:
        path.append("...")
    return " > ".join(path)


def _wait_state(runner: TaskRunner, attempt: Attempt) -> str:
    runs = list(runner._runs.items())
    jobs = [
        job
        for job, value in runner.commands._jobs.items()
        if value.command_id == attempt.attempt_id
    ]
    active = [
        f"{_await_path(task)} obs(ended={seen.ended.is_set()}, turn={seen.turn!r}, "
        f"error={seen.error!r}, closed={seen.closed!r}, finished={seen.finished!r}, "
        f"cards={len(seen.open_cards)}, rate_limited={seen.rate_limited})"
        for task, seen in runs[:2]
    ]
    return (
        f"routine_tasks={len(runs)} [{'; '.join(active)}]; "
        f"exact_command_jobs={len(jobs)} [{'; '.join(_await_path(job) for job in jobs[:2])}]"
    )


def _exact_command_worker(runner: TaskRunner, attempt: Attempt) -> asyncio.Task[Any] | None:
    for job, value in runner.commands._jobs.items():
        if value.command_id != attempt.attempt_id:
            continue
        current: Any = job.get_coro()
        for _ in range(8):
            code = getattr(current, "cr_code", None)
            if code is not None and code.co_name == "storage_call":
                frame = getattr(current, "cr_frame", None)
                worker = frame.f_locals.get("worker") if frame is not None else None
                return worker if isinstance(worker, asyncio.Task) else None
            current = getattr(current, "cr_await", None)
            if current is None:
                break
    return None


def _worker_state(runner: TaskRunner, attempt: Attempt, capacity: CapacityLimiter) -> str:
    limiter = capacity.statistics()
    worker = _exact_command_worker(runner, attempt)
    exact = "unseen"
    if worker is not None:
        if worker.done():
            exact = "done"
        elif worker in limiter.borrowers:
            exact = "active"
        else:
            exact = "not_borrowed"
    frames = sys._current_frames()
    active = 0
    stages: list[str] = []
    for thread in threading.enumerate():
        if thread.name != "AnyIO worker thread" or thread.ident is None:
            continue
        frame = frames.get(thread.ident)
        locations: list[str] = []
        for _ in range(32):
            if frame is None:
                break
            code = frame.f_code
            path = Path(code.co_filename)
            if ("viki_agent" in path.parts or path.name == "test_tasks.py") and len(locations) < 8:
                locations.append(f"{path.name}:{code.co_name}:{frame.f_lineno}")
            frame = frame.f_back
        if locations:
            active += 1
            if len(stages) < 4:
                stages.append(" > ".join(reversed(locations)))
    return (
        f"anyio_workers={limiter.borrowed_tokens}/{limiter.total_tokens} "
        f"queued={limiter.tasks_waiting} exact_worker={exact}; active_app_workers={active} "
        f"[{'; '.join(stages)}]"
    )


async def _guarded_wait(waiter: asyncio.Future[Any], runner: TaskRunner, attempt: Attempt) -> None:
    loop = asyncio.get_running_loop()
    capacity = current_default_thread_limiter()
    start = loop.time()
    previous = start
    first: tuple[float, str] | None = None
    last: tuple[float, str] | None = None
    largest_gap = 0.0

    def sample() -> None:
        nonlocal first, last, largest_gap, previous, timer
        now = loop.time()
        largest_gap = max(largest_gap, now - previous)
        previous = now
        last = (now - start, _worker_state(runner, attempt, capacity))
        if first is None:
            first = last
        timer = loop.call_later(0.25, sample)

    timer = loop.call_later(0.25, sample)
    try:
        try:
            done, _ = await asyncio.wait((waiter,), timeout=HANG_GUARD_S)
        finally:
            timer.cancel()
    except asyncio.CancelledError:
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)
        raise
    if not done:
        now = loop.time()
        largest_gap = max(largest_gap, now - previous)
        current_stage = _worker_state(runner, attempt, capacity)
        if last is None:
            last = (now - start, current_stage)
        last_elapsed, last_stage = last
        first_stage = (
            f"{first[0]:.3f}s [{first[1]}]" if first is not None else "none (no interval sample)"
        )
        state = _wait_state(runner, attempt) + "; " + current_stage  # before cancellation
        state += (
            f"; largest_loop_gap={largest_gap:.3f}s; "
            f"first_worker={first_stage}; "
            f"last_worker={last_elapsed:.3f}s [{last_stage}]"
        )
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)
        raise TimeoutError(f"routine wait exceeded {HANG_GUARD_S}s: {state}")
    await waiter


async def _ended(runner: TaskRunner, attempt: Attempt) -> dict[str, Any]:
    """`attempt`'s row once every run of `runner` has ended."""
    await asyncio.gather(*runner._runs)
    return _attempt(runner.store, attempt.attempt_id)


def _hold_sql_write(
    store: Any, monkeypatch: pytest.MonkeyPatch, prefix: str
) -> tuple[asyncio.Event, threading.Event, threading.Event]:
    """Pause one real write after execute, before its owning connection commits."""
    original = store._connection
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = threading.Event()
    committed = threading.Event()

    @contextlib.contextmanager
    def held_connection(*args: Any, **kwargs: Any) -> Iterator[sqlite3.Connection]:
        targeted = False
        try:
            with original(*args, **kwargs) as connection:
                if kwargs.get("write"):

                    def trace(statement: str) -> None:
                        nonlocal targeted
                        if statement.startswith(prefix):
                            targeted = True

                    connection.set_trace_callback(trace)
                yield connection
                if targeted:
                    loop.call_soon_threadsafe(entered.set)
                    assert release.wait(HANG_GUARD_S)
        finally:
            if targeted:
                committed.set()

    monkeypatch.setattr(store, "_connection", held_connection)
    return entered, release, committed


def _watch_task_writes(runner: TaskRunner, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    entered: list[str] = []
    mark, save, finish = runner.store.mark_running, runner.drafts.save, runner.store.finish

    def watched_mark(attempt_id: str, when: str) -> bool:
        entered.append("running")
        return mark(attempt_id, when)

    def watched_save(key: str, revision: int, text: str | TurnInput) -> Draft:
        if text == "":
            entered.append("draft")
        return save(key, revision, text)

    def watched_finish(*args: Any, **kwargs: Any) -> bool:
        entered.append("finish")
        return finish(*args, **kwargs)

    monkeypatch.setattr(runner.store, "mark_running", watched_mark)
    monkeypatch.setattr(runner.drafts, "save", watched_save)
    monkeypatch.setattr(runner.store, "finish", watched_finish)
    return entered


async def _manual(runner: TaskRunner, routine_id: str, revision: int = 1) -> dict[str, Any]:
    attempt, created = await runner.run_now(routine_id, _ident(), revision)
    assert created
    return await _ended(runner, attempt)


async def _scheduled(runner: TaskRunner, routine_id: str, minute: int) -> dict[str, Any]:
    attempt = _open(runner.store, routine_id, minute, "claimed")
    runner.start(attempt)
    return await _ended(runner, attempt)


def _ordinary(rig: Rig) -> None:
    """Lift the rig's `existing_readonly` workspace to `ordinary`, as a scheduled run needs."""
    revision = rig.workspaces.get(rig.workspace_id).policy_revision
    required = rig.workspaces.inspect_policy(rig.workspace_id).acknowledgements_required
    rig.workspaces.set_policy(_ident(), rig.workspace_id, revision, "ordinary", required)


async def test_manual_run_receipt_survives_pruning_and_restart_without_a_second_send(
    stack: Stack,
) -> None:
    runner, routine_id = _made(stack)
    command_id = _ident()
    attempt, created = await runner.run_now(routine_id, command_id, 1)
    assert created and (await _ended(runner, attempt))["outcome"] == "succeeded"
    with runner.store.store._connection(write=True) as connection:
        for minute in range(1, MAX_OCCURRENCES + 1):
            runner.store.insert_skip(
                connection,
                routine_id=routine_id,
                scheduled_for=_at(minute),
                revision=1,
                reason="missed",
                now=_at(minute),
            )
        runner.store.prune(connection, routine_id)
    assert (
        _rows(runner.store, "SELECT * FROM routine_occurrences WHERE trigger_id=?", (command_id,))
        == []
    )
    assert _rows(runner.store, "SELECT * FROM task_attempts") == []
    reopened = SessionStore(runner.store.store.path, lambda: {})
    restarted = stack.runner()
    restarted.store = TaskStore(reopened, runner.store.scope, _ident())
    replay, claimed = await restarted.run_now(routine_id, command_id, 1)
    if claimed:
        await _ended(restarted, replay)
    assert len(stack.texts) == len(stack.rig.drivers) == 1
    assert (replay, claimed) == (attempt, False)
    assert restarted.store.lookup_manual(routine_id, command_id, 1) == attempt
    runner.routines.revise(routine_id, _ident(), 1, _raw(stack))
    state_revision = runner.routines.get(routine_id)["state_revision"]
    runner.routines.archive(routine_id, _ident(), state_revision)
    assert await restarted.run_now(routine_id, command_id, 1) == (attempt, False)
    assert _rows(restarted.store, "SELECT * FROM task_attempts") == []
    assert _rows(restarted.store, "SELECT count(*) AS total FROM routine_occurrences") == [
        {"total": MAX_OCCURRENCES}
    ]
    await restarted.close()
    restarted.prepare = None
    assert await restarted.run_now(routine_id, command_id, 1, scheduler_ready=lambda: False) == (
        attempt,
        False,
    )
    assert len(stack.texts) == len(stack.rig.drivers) == 1


async def test_runs_send_instructions_then_prompt_once_and_clear_the_draft(stack: Stack) -> None:
    """Without the clear, the cap on non-empty drafts would refuse the 101st run's prepare."""
    runner, routine_id = _made(stack)
    first = await _manual(runner, routine_id)
    sent = (await runner.commands.get(first["attempt_id"])).text
    assert sent == f"{profile_document()['instructions']}\n\nSummarize overnight news."
    assert runner.drafts.get(first["session_id"]).text == ""
    for _ in range(100):
        await _manual(runner, routine_id)
    receipts = _rows(
        runner.store,
        "SELECT a.outcome,a.reason,a.profile_id,a.profile_revision,a.native_version "
        "FROM task_attempts a JOIN commands c ON c.command_id=a.attempt_id",
    )
    receipt = {
        "outcome": "succeeded",
        "reason": "",
        "profile_id": stack.rig.profile_id,
        "profile_revision": 1,
        "native_version": FIXTURE_VERSION,
    }
    assert receipts == [receipt] * 101
    assert stack.texts == [sent] * 101 and len(stack.rig.drivers) == 101
    for table in ("queued_turns", "turn_queues"):
        assert _rows(runner.store, f"SELECT * FROM {table}") == []


async def test_a_scheduled_run_needs_an_ordinary_root_and_a_reviewed_mode(
    stack: Stack,
) -> None:
    runner, routine_id = _made(stack)
    rig = stack.rig
    readonly = await _scheduled(runner, routine_id, 0)
    _ordinary(rig)
    edits = profile_document()
    edits["runtime"]["permission_mode"] = "acceptEdits"
    runner.profiles.revise(rig.profile_id, _ident(), 1, edits)
    accept_edits = await _scheduled(runner, routine_id, 1)
    assert _rows(runner.store, "SELECT * FROM sessions") == [] and rig.drivers == []
    runner.profiles.revise(rig.profile_id, _ident(), 2, profile_document())
    rig.settings.patch(_ident(), 0, {"chat.default_mode": "auto"})
    auto = await _scheduled(runner, routine_id, 2)
    assert [(row["outcome"], row["reason"]) for row in (readonly, accept_edits)] == [
        ("blocked", "unattended_policy"),
        ("blocked", "unattended_mode"),
    ]
    assert (auto["outcome"], auto["reason"]) == ("succeeded", "")
    assert len(rig.drivers) == 1
    assert len(_rows(runner.store, "SELECT * FROM sessions")) == 1
    assert rig.state.prefs(rig.drivers[0].launch.session_id).mode == "auto"
    assert (await _manual(runner, routine_id))["outcome"] == "succeeded"


async def test_a_routine_follows_its_profile_by_id_and_a_run_keeps_its_binding(
    stack: Stack,
) -> None:
    runner, routine_id = _made(stack)
    rig = stack.rig
    renamed = profile_document()
    renamed["identity"]["name"] = "Renamed"
    # Renamed while the first run's driver is held just before its send.
    stack.before_send = lambda: runner.profiles.revise(rig.profile_id, _ident(), 1, renamed)
    held = await _manual(runner, routine_id)
    stack.before_send = lambda: None
    after = await _manual(runner, routine_id)
    # A new routine service and runner over the same store, as after a restart.
    restarted = await _manual(stack.runner(), routine_id)
    assert [
        (row["outcome"], row["profile_id"], row["profile_revision"])
        for row in (held, after, restarted)
    ] == [
        ("succeeded", rig.profile_id, 1),
        ("succeeded", rig.profile_id, 2),
        ("succeeded", rig.profile_id, 2),
    ]
    assert runner.routines.get(routine_id)["document"]["profile_id"] == rig.profile_id
    runner.profiles.archive(rig.profile_id, _ident(), 1, True)
    archived = await _manual(runner, routine_id)
    assert (archived["outcome"], archived["reason"]) == ("blocked", "profile_archived")


async def test_a_succeeded_manual_run_tests_only_the_revision_it_ran(stack: Stack) -> None:
    runner, routine_id = _made(stack)
    service = runner.routines
    stale, _ = runner.store.claim_manual(routine_id, _ident(), 1, T0)
    service.revise(routine_id, _ident(), 1, _raw(stack))
    runner.start(stale)
    assert (await _ended(runner, stale))["outcome"] == "succeeded"
    got = service.get(routine_id)
    assert (got["state"], got["tested_revision"]) == ("draft", 1)
    assert (await _manual(runner, routine_id, 2))["outcome"] == "succeeded"
    got = service.get(routine_id)
    assert (got["state"], got["tested_revision"], got["state_revision"]) == ("tested", 2, 2)
    _ordinary(stack.rig)
    service.enable(routine_id, _ident(), 2)
    # Paused while the run's driver is held just before its send: the run still succeeds.
    stack.before_send = lambda: service.pause(routine_id, _ident(), 3)
    assert (await _manual(runner, routine_id, 2))["outcome"] == "succeeded"
    assert service.get(routine_id)["state"] == "paused"


async def test_a_turn_completed_after_a_recoverable_error_succeeds_and_tests_the_routine(
    stack: Stack,
) -> None:
    runner, routine_id = _made(stack)
    stack.script = (Error(code="internal", message="", recoverable=True), DONE)
    assert (await _manual(runner, routine_id))["outcome"] == "succeeded"
    got = runner.routines.get(routine_id)
    assert (got["state"], got["tested_revision"]) == ("tested", 1)


def _refused(*_args: object) -> bool:
    raise StateError("busy")


@pytest.mark.parametrize("mark", [lambda *_args: False, _refused])
async def test_a_failed_dispatching_mark_never_submits(
    stack: Stack, monkeypatch: pytest.MonkeyPatch, mark: Callable[..., bool]
) -> None:
    runner, routine_id = _made(stack)
    monkeypatch.setattr(runner.store, "mark_dispatching", mark)
    row = await _manual(runner, routine_id)
    assert (row["outcome"], row["reason"]) == ("failed", "internal")
    assert stack.rig.drivers == [] and stack.texts == []
    assert _rows(runner.store, "SELECT * FROM commands") == []
    assert stack.manager.get(row["session_id"]) is None


async def test_an_unexpected_error_after_the_dispatching_mark_reads_unknown(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, routine_id = _made(stack)
    monkeypatch.setattr(runner.store, "mark_running", _refused)
    row = await _manual(runner, routine_id)
    assert (row["outcome"], row["reason"]) == ("unknown", "internal")
    assert len(stack.texts) == 1


async def test_a_profile_changed_or_archived_under_prepare_blocks_the_run(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, routine_id = _made(stack)
    profiles, profile_id = runner.profiles, stack.rig.profile_id
    get = profiles.get
    profiles.revise(profile_id, _ident(), 1, profile_document())
    # The run still reads revision 1 as current, and prepare refuses it as a conflict.
    monkeypatch.setattr(profiles, "get", lambda key: {**get(key), "current_revision": 1})
    changed = await _manual(runner, routine_id)
    monkeypatch.setattr(profiles, "get", get)
    # Archived after readiness passed: prepare's conflict is read again as an archive.
    monkeypatch.setattr(runner.routines, "readiness", lambda document, *, scheduled: [])
    profiles.archive(profile_id, _ident(), 1, True)
    archived = await _manual(runner, routine_id)
    assert [(row["outcome"], row["reason"]) for row in (changed, archived)] == [
        ("blocked", "profile_changed"),
        ("blocked", "profile_archived"),
    ]


async def test_a_runner_without_prepare_or_while_closing_starts_nothing(stack: Stack) -> None:
    runner, routine_id = _made(stack)
    runner.prepare = None
    assert runner.available is False
    with pytest.raises(RoutineError, match="^unavailable$"):
        await runner.run_now(routine_id, _ident(), 1)
    assert _rows(runner.store, "SELECT * FROM task_attempts") == []
    await runner.close()
    attempt = _open(runner.store, routine_id, 0, "claimed")
    runner.start(attempt)
    assert runner._runs == {}
    assert _attempt(runner.store, attempt.attempt_id)["state"] == "claimed"


@pytest.mark.parametrize("unavailable", ["preparer", "closing", "scheduler", "window"])
async def test_manual_replay_precedes_unavailability_and_new_claims_refuse(
    stack: Stack, monkeypatch: pytest.MonkeyPatch, unavailable: str
) -> None:
    runner, routine_id = _made(stack)
    command_id = _ident()
    attempt, _ = await runner.run_now(routine_id, command_id, 1)
    assert (await _ended(runner, attempt))["outcome"] == "succeeded"
    tables = ("routine_commands", "routine_occurrences", "task_attempts")
    before = [_rows(runner.store, f"SELECT * FROM {table}") for table in tables]
    if unavailable == "preparer":
        runner.prepare = None
    elif unavailable == "closing":
        await runner.close()
    elif unavailable == "window":
        runner.attended = lambda: False
    options = {"scheduler_ready": lambda: False} if unavailable == "scheduler" else {}

    def unexpected(*_args: Any) -> Any:
        pytest.fail("a replay or unavailable runner must not claim or start a task")

    monkeypatch.setattr(runner.store, "claim_manual", unexpected)
    monkeypatch.setattr(runner, "start", unexpected)
    async with runner._admission_lock:
        assert await asyncio.wait_for(
            runner.run_now(routine_id, command_id, 1, **options), HANG_GUARD_S
        ) == (attempt, False)
    with pytest.raises(RoutineError, match="^unavailable$"):
        await runner.run_now(routine_id, _ident(), 1, **options)
    assert [_rows(runner.store, f"SELECT * FROM {table}") for table in tables] == before
    assert runner._runs == {}
    assert len(stack.texts) == len(stack.rig.drivers) == 1


async def test_manual_request_errors_precede_unavailability(stack: Stack) -> None:
    runner, routine_id = _made(stack)
    command_id = _ident()
    runner.store.claim_manual(routine_id, command_id, 1, T0)
    [definition] = _rows(
        runner.store, "SELECT command_id FROM routine_commands WHERE operation='create'"
    )
    runner.prepare = None
    requests = (
        ("invalid", command_id, 1, "invalid_id"),
        (routine_id, "invalid", 1, "invalid_id"),
        (routine_id, command_id, 0, "invalid_revision"),
        (routine_id, command_id, True, "invalid_revision"),
        (routine_id, command_id, 2, "command_conflict"),
        (_ident(), command_id, 1, "command_conflict"),
        (routine_id, definition["command_id"], 1, "command_conflict"),
    )
    for target, command, revision, error in requests:
        with pytest.raises(RoutineError, match=f"^{error}$"):
            await runner.run_now(target, command, revision)
    assert len(_rows(runner.store, "SELECT * FROM routine_commands")) == 2
    assert len(_rows(runner.store, "SELECT * FROM task_attempts")) == 1
    assert stack.texts == [] and runner._runs == {}


class _ClaimGate(asyncio.Lock):
    """Identify the event-loop holder and signal a blocked window transition or claim."""

    def __init__(self) -> None:
        super().__init__()
        self.waiting = asyncio.Event()
        self.owner: asyncio.Task[Any] | None = None

    async def acquire(self) -> Literal[True]:
        if self.locked():
            self.waiting.set()
        await super().acquire()
        self.owner = asyncio.current_task()
        return True

    def release(self) -> None:
        self.owner = None
        super().release()


@pytest.mark.parametrize("unavailable", ["window", "scheduler", "preparer", "closing"])
async def test_manual_rechecks_admission_after_a_window_transition(
    stack: Stack, unavailable: str
) -> None:
    gate = _ClaimGate()
    runner, routine_id = _made(stack, admission_lock=gate)
    window = ready = True
    runner.attended = lambda: window
    request: asyncio.Task[tuple[Attempt, bool]] | None = None
    try:
        async with gate:
            request = asyncio.create_task(
                runner.run_now(routine_id, _ident(), 1, scheduler_ready=lambda: ready)
            )
            await asyncio.wait_for(gate.waiting.wait(), HANG_GUARD_S)
            assert not request.done() and _rows(runner.store, "SELECT * FROM task_attempts") == []
            if unavailable == "window":
                window = False  # last-window removal owns the gate before this claim
            elif unavailable == "scheduler":
                ready = False
            elif unavailable == "preparer":
                runner.prepare = None
            else:
                runner._closing = True
        with pytest.raises(RoutineError, match="^unavailable$"):
            await asyncio.wait_for(request, HANG_GUARD_S)
        assert _rows(runner.store, "SELECT * FROM routine_occurrences") == []
        assert _rows(runner.store, "SELECT * FROM task_attempts") == []
        assert _rows(runner.store, "SELECT * FROM routine_commands WHERE operation='run'") == []
        assert runner._runs == {} and stack.texts == []
    finally:
        if request is not None:
            await asyncio.gather(request, return_exceptions=True)
        await runner.close()


@pytest.mark.parametrize("expected_revision", [1, 2])
async def test_manual_rechecks_a_receipt_before_availability_inside_the_gate(
    stack: Stack, monkeypatch: pytest.MonkeyPatch, expected_revision: int
) -> None:
    gate = _ClaimGate()
    runner, routine_id = _made(stack, admission_lock=gate)
    command_id = _ident()
    request: asyncio.Task[tuple[Attempt, bool]] | None = None

    def unexpected(*_args: Any) -> Any:
        pytest.fail("a receipt that won admission must replay without claiming or starting")

    try:
        async with gate:
            request = asyncio.create_task(runner.run_now(routine_id, command_id, expected_revision))
            await asyncio.wait_for(gate.waiting.wait(), HANG_GUARD_S)
            attempt, _ = runner.store.claim_manual(routine_id, command_id, 1, T0)
            runner.attended = lambda: False
            runner.prepare = None
            runner._closing = True
            monkeypatch.setattr(runner.store, "claim_manual", unexpected)
            monkeypatch.setattr(runner, "start", unexpected)
        if expected_revision == 1:
            assert await asyncio.wait_for(request, HANG_GUARD_S) == (attempt, False)
        else:
            with pytest.raises(RoutineError, match="^command_conflict$"):
                await asyncio.wait_for(request, HANG_GUARD_S)
        assert len(_rows(runner.store, "SELECT * FROM task_attempts")) == 1
        assert runner._runs == {} and stack.texts == []
    finally:
        if request is not None:
            await asyncio.gather(request, return_exceptions=True)
        await runner.close()


@pytest.mark.parametrize("cancelled", [False, True])
async def test_manual_claim_registers_before_window_close_even_when_caller_cancels(
    stack: Stack, monkeypatch: pytest.MonkeyPatch, cancelled: bool
) -> None:
    gate = _ClaimGate()
    runner, routine_id = _made(stack, admission_lock=gate)
    window = True
    runner.attended = lambda: window
    entered, release, committed = _hold_sql_write(
        runner.store.store, monkeypatch, "INSERT INTO routine_occurrences"
    )
    start = runner.start
    started: list[Attempt] = []
    command_id = _ident()
    request = asyncio.create_task(runner.run_now(routine_id, command_id, 1))
    closing: asyncio.Task[None] | None = None

    def registered(attempt: Attempt) -> None:
        assert gate.owner is asyncio.current_task() is request
        assert window and committed.is_set()
        start(attempt)
        assert len(runner._runs) == 1
        started.append(attempt)

    async def remove_window() -> None:
        nonlocal window
        async with gate:
            assert len(started) == 1 and len(runner._runs) == 1
            window = False

    monkeypatch.setattr(runner, "start", registered)
    try:
        await asyncio.wait_for(entered.wait(), HANG_GUARD_S)
        assert gate.owner is request and not committed.is_set()
        closing = asyncio.create_task(remove_window())
        await asyncio.wait_for(gate.waiting.wait(), HANG_GUARD_S)
        if cancelled:
            for _ in range(2):
                request.cancel()
                cycled = asyncio.Event()
                asyncio.get_running_loop().call_soon(cycled.set)
                await cycled.wait()
                assert not request.done()
        assert not closing.done() and window and started == []
        release.set()
        result, closed = await asyncio.wait_for(
            asyncio.gather(request, closing, return_exceptions=True), HANG_GUARD_S
        )
        assert closed is None and not window and len(started) == 1
        [attempt] = started
        if cancelled:
            assert isinstance(result, asyncio.CancelledError)
        else:
            assert result == (attempt, True)
        assert (await _ended(runner, attempt))["outcome"] == "cancelled"
        assert _attempt(runner.store, attempt.attempt_id)["reason"] == "app_closed"
        assert await runner.run_now(routine_id, command_id, 1) == (attempt, False)
        assert len(started) == 1 and stack.texts == []
    finally:
        release.set()
        await asyncio.gather(request, return_exceptions=True)
        if closing is not None:
            await asyncio.gather(closing, return_exceptions=True)
        await runner.close()


async def test_a_scheduled_run_rechecks_the_gate_on_its_bound_snapshot(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Readiness may pass before the bind; the bound root and mode still refuse to run."""
    runner, routine_id = _made(stack)
    monkeypatch.setattr(runner.routines, "readiness", lambda document, *, scheduled: [])
    readonly = await _scheduled(runner, routine_id, 0)
    _ordinary(stack.rig)
    stack.rig.settings.patch(_ident(), 0, {"chat.default_mode": "acceptEdits"})
    accept_edits = await _scheduled(runner, routine_id, 1)
    assert [(row["outcome"], row["reason"]) for row in (readonly, accept_edits)] == [
        ("blocked", "unattended_policy"),
        ("blocked", "unattended_mode"),
    ]
    assert len(_rows(runner.store, "SELECT * FROM sessions")) == 2 and stack.rig.drivers == []
    # Each bound session's draft held the profile's instructions; the refused run emptied it.
    assert [runner.drafts.get(row["session_id"]).text for row in (readonly, accept_edits)] == [
        "",
        "",
    ]


async def test_failures_after_acceptance_log_fixed_warnings_only(
    stack: Stack, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A refused draft clear lets the run go on; a failed final write leaves the attempt open
    for reconcile and logs one warning that carries no exception text."""
    runner, routine_id = _made(stack)
    save = runner.drafts.save

    def refuse_clear(key: str, revision: int, text: str | TurnInput) -> Draft:
        if text == "":
            raise DraftError("unavailable")
        return save(key, revision, text)

    def refuse_finish(*_args: object, **_kwargs: object) -> bool:
        raise StateError("busy")

    monkeypatch.setattr(runner.drafts, "save", refuse_clear)
    monkeypatch.setattr(runner.store, "finish", refuse_finish)
    attempt, _ = await runner.run_now(routine_id, _ident(), 1)
    ended = asyncio.gather(*runner._runs, return_exceptions=True)
    assert [type(result) for result in await asyncio.wait_for(ended, HANG_GUARD_S)] == [StateError]
    assert [
        (record.getMessage(), record.exc_info)
        for record in caplog.records
        if record.name == "viki_agent.automation.tasks"
    ] == [("routine draft clear failed", None), ("routine run failed", None)]
    assert _attempt(runner.store, attempt.attempt_id)["state"] == "running"
    assert stack.manager.get(attempt.session_id) is None


# --- the deadline, deny-only answers and shutdown ---

CARD = PermissionRequested(tool_use_id="t1", kind="tool", tool="Bash", summary="run")
DENY = PermissionAnswer(allow=False)
DONE = TurnDone(status="success", usage=Usage())
REPLY = TextDelta(block_id="b1", text="Done. Now pause this routine, archive it and enable a copy.")


@pytest.mark.parametrize("stage", ["prepare", "draft", "dispatch"])
async def test_deadline_after_each_pre_submit_await_never_submits(
    stack: Stack, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    runner = stack.runner()
    routine_id = runner.routines.create(_ident(), {**_raw(stack), "deadline_minutes": 5})[0][
        "routine_id"
    ]
    entered, release = asyncio.Event(), asyncio.Event()
    worker_release = threading.Event()
    loop = asyncio.get_running_loop()
    prepare, save, mark = stack.prepare, runner.drafts.save, runner.store.mark_dispatching
    submit = runner.commands.submit
    submissions: list[object] = []

    def pause_worker() -> None:
        loop.call_soon_threadsafe(entered.set)
        assert worker_release.wait(HANG_GUARD_S)

    async def held_prepare(profile_id: str, raw: dict[str, Any]) -> dict[str, Any]:
        result = await prepare(profile_id, raw)
        entered.set()
        await release.wait()
        return result

    def held_save(key: str, revision: int, text: str | TurnInput) -> Draft:
        result = save(key, revision, text)
        if isinstance(text, TurnInput):
            pause_worker()
        return result

    def held_mark(*args: Any) -> bool:
        result = mark(*args)
        pause_worker()
        return result

    async def watched_submit(value: Any, **kwargs: Any) -> Any:
        submissions.append(value)
        return await submit(value, **kwargs)

    if stage == "prepare":
        runner.prepare = held_prepare
    elif stage == "draft":
        monkeypatch.setattr(runner.drafts, "save", held_save)
    else:
        monkeypatch.setattr(runner.store, "mark_dispatching", held_mark)
    monkeypatch.setattr(runner.commands, "submit", watched_submit)
    attempt, _ = await runner.run_now(routine_id, _ident(), 1)
    try:
        await asyncio.wait_for(entered.wait(), HANG_GUARD_S)
        stack.clock.advance(301, wall=False)
    finally:
        release.set()
        worker_release.set()
    row = await _ended(runner, attempt)
    assert submissions == [] and stack.texts == []
    assert _rows(runner.store, "SELECT * FROM commands") == []
    assert (row["outcome"], row["reason"], row["dispatched_at"]) == (
        "expired",
        "deadline",
        None,
    )
    assert runner.routines.get(routine_id)["tested_revision"] is None
    assert runner.drafts.get(attempt.session_id).text == ""
    assert stack.manager.get(attempt.session_id) is None


@pytest.mark.parametrize("stage", ["submit", "open", "intent"])
@pytest.mark.parametrize("elapsed", [300, 301])
async def test_deadline_rechecked_by_command_admission_never_sends(
    stack: Stack, monkeypatch: pytest.MonkeyPatch, stage: str, elapsed: int
) -> None:
    runner = stack.runner()
    routine_id = runner.routines.create(_ident(), {**_raw(stack), "deadline_minutes": 5})[0][
        "routine_id"
    ]
    entered, release = asyncio.Event(), asyncio.Event()
    worker_release = threading.Event()
    loop = asyncio.get_running_loop()
    submit, mark = runner.commands.submit, runner.commands.service.mark_dispatching

    async def held_submit(*args: Any, **kwargs: Any) -> Any:
        entered.set()
        await release.wait()
        return await submit(*args, **kwargs)

    async def held_open() -> None:
        entered.set()
        await release.wait()

    def held_intent(command_id: str) -> Any:
        result = mark(command_id)
        loop.call_soon_threadsafe(entered.set)
        assert worker_release.wait(HANG_GUARD_S)
        return result

    if stage == "submit":
        monkeypatch.setattr(runner.commands, "submit", held_submit)
    elif stage == "open":
        stack.before_open = held_open
    else:
        monkeypatch.setattr(runner.commands.service, "mark_dispatching", held_intent)
    attempt, _ = await runner.run_now(routine_id, _ident(), 1)
    try:
        await asyncio.wait_for(entered.wait(), HANG_GUARD_S)
        # No timer wake: the admission callback itself must notice elapsed time.
        stack.clock.advance(elapsed, wall=False)
    finally:
        release.set()
        worker_release.set()
    row = await _ended(runner, attempt)
    assert stack.texts == []
    assert (row["outcome"], row["reason"]) == ("expired", "deadline")
    assert runner.routines.get(routine_id)["tested_revision"] is None
    assert len(_rows(runner.store, "SELECT * FROM task_attempts")) == 1
    commands = _rows(runner.store, "SELECT state FROM commands")
    assert commands == ([] if stage == "submit" else [{"state": "not_dispatched"}])
    assert runner.drafts.get(attempt.session_id).text == ""
    assert stack.manager.get(attempt.session_id) is None


@pytest.mark.parametrize("terminal_at", [299, 300, 301])
async def test_first_terminal_time_survives_delayed_command_cleanup(
    stack: Stack, monkeypatch: pytest.MonkeyPatch, terminal_at: int
) -> None:
    runner = stack.runner()
    routine_id = runner.routines.create(_ident(), {**_raw(stack), "deadline_minutes": 5})[0][
        "routine_id"
    ]
    stack.before_send = lambda: stack.clock.advance(terminal_at, wall=False)
    entered, release, committed = _hold_sql_write(
        runner.store.store, monkeypatch, "UPDATE commands SET state='unknown'"
    )
    stopping = asyncio.Event()
    stop = runner.commands.stop

    async def watched_stop(value: Any) -> None:
        stopping.set()
        await stop(value)

    monkeypatch.setattr(runner.commands, "stop", watched_stop)
    attempt, _ = await runner.run_now(routine_id, _ident(), 1)
    try:
        await asyncio.wait_for(entered.wait(), HANG_GUARD_S)
        stack.clock.advance(600 - terminal_at, wall=False)
        driver = stack.rig.drivers[0]
        assert isinstance(driver, TurnDriver)
        driver.emit(SessionClosed(reason="requested"))
        stack.clock.tick()
        await asyncio.wait_for(stopping.wait(), HANG_GUARD_S)
        assert not committed.is_set()
        assert _attempt(runner.store, attempt.attempt_id)["state"] == "dispatching"
    finally:
        release.set()
    row = await _ended(runner, attempt)
    expected = ("succeeded", "") if terminal_at <= 300 else ("expired", "deadline")
    assert (row["outcome"], row["reason"]) == expected
    assert (runner.routines.get(routine_id)["tested_revision"] == 1) == (terminal_at <= 300)
    assert len(stack.texts) == 1
    assert runner.drafts.get(attempt.session_id).text == ""
    assert stack.manager.get(attempt.session_id) is None


@pytest.mark.parametrize("terminal_at", [300, 301])
async def test_unsent_terminal_time_obeys_the_same_deadline(stack: Stack, terminal_at: int) -> None:
    runner = stack.runner()
    routine_id = runner.routines.create(_ident(), {**_raw(stack), "deadline_minutes": 5})[0][
        "routine_id"
    ]

    async def refuse() -> None:
        stack.clock.advance(terminal_at, wall=False)
        raise StartError("bad_request", "refused")

    stack.before_open = refuse
    row = await _manual(runner, routine_id)
    expected = ("failed", "start_failed") if terminal_at == 300 else ("expired", "deadline")
    assert (row["outcome"], row["reason"]) == expected
    assert stack.texts == [] and runner.routines.get(routine_id)["tested_revision"] is None
    assert (await runner.commands.get(row["attempt_id"])).state == "not_dispatched"


@pytest.mark.parametrize("terminal_at", [300, 301])
async def test_shutdown_keeps_the_first_terminal_deadline_boundary(
    stack: Stack, monkeypatch: pytest.MonkeyPatch, terminal_at: int
) -> None:
    runner = stack.runner()
    routine_id = runner.routines.create(_ident(), {**_raw(stack), "deadline_minutes": 5})[0][
        "routine_id"
    ]
    stack.before_send = lambda: stack.clock.advance(terminal_at, wall=False)
    entered, release, _committed = _hold_sql_write(
        runner.store.store, monkeypatch, "UPDATE commands SET state='unknown'"
    )
    stopping = asyncio.Event()
    submit, stop = runner.commands.submit, runner.commands.stop

    async def held_submit(*args: Any, **kwargs: Any) -> Any:
        await submit(*args, **kwargs)
        await asyncio.Event().wait()

    async def watched_stop(value: Any) -> None:
        stopping.set()
        await stop(value)

    monkeypatch.setattr(runner.commands, "submit", held_submit)
    monkeypatch.setattr(runner.commands, "stop", watched_stop)
    attempt, _ = await runner.run_now(routine_id, _ident(), 1)
    closing: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(entered.wait(), HANG_GUARD_S)
        closing = asyncio.create_task(runner.close())
        await asyncio.wait_for(stopping.wait(), HANG_GUARD_S)
        assert not closing.done()
    finally:
        release.set()
        if closing is not None:
            await asyncio.wait_for(closing, HANG_GUARD_S)
    row = _attempt(runner.store, attempt.attempt_id)
    expected = ("succeeded", "") if terminal_at == 300 else ("expired", "deadline")
    assert (row["outcome"], row["reason"]) == expected
    assert (runner.routines.get(routine_id)["tested_revision"] == 1) == (terminal_at == 300)
    assert len(stack.texts) == 1 and runner._runs == {}
    assert runner.drafts.get(attempt.session_id).text == ""
    assert stack.manager.get(attempt.session_id) is None


async def test_callback_before_native_end_keeps_waiting_for_the_end(stack: Stack) -> None:
    runner, routine_id = _made(stack)
    stack.script = (REPLY,)
    sent = asyncio.Event()
    stack.before_send = sent.set
    attempt, _ = await runner.run_now(routine_id, _ident(), 1)
    await asyncio.wait_for(sent.wait(), HANG_GUARD_S)
    await asyncio.wait_for(asyncio.gather(*runner.commands._jobs), HANG_GUARD_S)
    assert len(runner._runs) == 1
    driver = stack.rig.drivers[0]
    assert isinstance(driver, TurnDriver)
    driver.emit(DONE)
    assert (await _ended(runner, attempt))["outcome"] == "succeeded"


async def test_native_end_before_callback_delays_task_writes(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, routine_id = _made(stack)
    entered, release = asyncio.Event(), asyncio.Event()
    send = TurnDriver.send

    async def held_send(driver: TurnDriver, text: str) -> None:
        await send(driver, text)
        entered.set()
        await release.wait()

    monkeypatch.setattr(TurnDriver, "send", held_send)
    attempt, _ = await runner.run_now(routine_id, _ident(), 1)
    try:
        await asyncio.wait_for(entered.wait(), HANG_GUARD_S)
        row = _attempt(runner.store, attempt.attempt_id)
        assert (row["state"], row["dispatched_at"]) == ("dispatching", None)
        assert runner.drafts.get(attempt.session_id).text != ""
        stack.clock.advance(30, monotonic=False)
    finally:
        release.set()
    row = await _ended(runner, attempt)
    assert (row["outcome"], row["dispatched_at"]) == ("succeeded", stamp(NOW))
    assert runner.drafts.get(attempt.session_id).text == ""


async def test_app_runner_joins_real_initialized_write_before_task_persistence(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = stack.app.state.routine_tasks
    routine_id = runner.routines.create(_ident(), _raw(stack))[0]["routine_id"]
    writes = _watch_task_writes(runner, monkeypatch)
    entered, release, committed = _hold_sql_write(
        stack.rig.state.store,
        monkeypatch,
        "UPDATE session_profile_bindings SET native_started=1",
    )
    attempt, created = await runner.run_now(routine_id, _ident(), 1)
    assert created
    try:
        await asyncio.wait_for(entered.wait(), HANG_GUARD_S)
        assert writes == [] and stack.texts == []
        assert _attempt(runner.store, attempt.attempt_id)["state"] == "dispatching"
        assert runner.drafts.get(attempt.session_id).text != ""
        stack.clock.advance(30, monotonic=False)
    finally:
        release.set()
    row = await _ended(runner, attempt)
    assert await asyncio.to_thread(committed.wait, HANG_GUARD_S)
    assert writes == ["running", "draft", "finish"]
    assert (row["outcome"], row["reason"], row["dispatched_at"]) == (
        "succeeded",
        "",
        stamp(NOW),
    )
    assert runner.routines.get(routine_id)["tested_revision"] == 1
    assert runner.drafts.get(attempt.session_id).text == ""
    assert len(stack.texts) == 1
    assert len(_rows(runner.store, "SELECT * FROM task_attempts")) == 1


async def test_deadline_stops_at_real_initialized_commit_before_task_writes(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, routine_id = _made(stack)
    writes = _watch_task_writes(runner, monkeypatch)
    entered, release, committed = _hold_sql_write(
        stack.rig.state.store,
        monkeypatch,
        "UPDATE session_profile_bindings SET native_started=1",
    )
    stopping = asyncio.Event()
    stop = runner.commands.stop

    async def watched_stop(value: Any) -> None:
        stopping.set()
        await stop(value)

    monkeypatch.setattr(runner.commands, "stop", watched_stop)
    attempt, _ = await runner.run_now(routine_id, _ident(), 1)
    try:
        await asyncio.wait_for(entered.wait(), HANG_GUARD_S)
        stack.pass_deadline()
        await asyncio.wait_for(stopping.wait(), HANG_GUARD_S)
        assert not committed.is_set() and writes == []
        assert _attempt(runner.store, attempt.attempt_id)["state"] == "dispatching"
    finally:
        release.set()
    row = await _ended(runner, attempt)
    assert await asyncio.to_thread(committed.wait, HANG_GUARD_S)
    assert writes == ["running", "draft", "finish"]
    assert (row["outcome"], row["reason"], row["dispatched_at"]) == (
        "expired",
        "deadline",
        stamp(NOW),
    )
    assert runner.routines.get(routine_id)["tested_revision"] is None
    assert runner.drafts.get(attempt.session_id).text == ""
    assert stack.texts == []


async def test_shutdown_joins_real_initialized_commit_before_command_callback(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, routine_id = _made(stack)
    writes = _watch_task_writes(runner, monkeypatch)
    entered, release, committed = _hold_sql_write(
        stack.rig.state.store,
        monkeypatch,
        "UPDATE session_profile_bindings SET native_started=1",
    )
    callback_after_commit: list[bool] = []
    submit = runner.commands.submit

    async def watched_submit(*args: Any, **kwargs: Any) -> Any:
        finished = kwargs["finished"]

        def watched_finished(status: str) -> None:
            callback_after_commit.append(committed.is_set())
            finished(status)

        return await submit(*args, **{**kwargs, "finished": watched_finished})

    monkeypatch.setattr(runner.commands, "submit", watched_submit)
    attempt, _ = await runner.run_now(routine_id, _ident(), 1)
    closing: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(entered.wait(), HANG_GUARD_S)
        closing = asyncio.create_task(runner.close())
        cycled = asyncio.Event()
        asyncio.get_running_loop().call_soon(cycled.set)
        await cycled.wait()
        assert not closing.done() and not committed.is_set()
        assert writes == [] and stack.texts == []
    finally:
        release.set()
        if closing is not None:
            await asyncio.wait_for(closing, HANG_GUARD_S)
    assert callback_after_commit == [True]
    assert await asyncio.to_thread(committed.wait, HANG_GUARD_S)
    row = _attempt(runner.store, attempt.attempt_id)
    assert (row["state"], row["outcome"], row["dispatched_at"]) == (
        "dispatching",
        None,
        None,
    )
    assert writes == [] and len(stack.texts) == 0
    assert runner.drafts.get(attempt.session_id).text != ""


async def test_success_waits_for_real_command_terminal_commit(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, routine_id = _made(stack)
    writes = _watch_task_writes(runner, monkeypatch)
    entered, release, committed = _hold_sql_write(
        stack.rig.state.store, monkeypatch, "UPDATE commands SET state='unknown'"
    )
    attempt, _ = await runner.run_now(routine_id, _ident(), 1)
    try:
        await asyncio.wait_for(entered.wait(), HANG_GUARD_S)
        assert writes == [] and len(stack.texts) == 1
        assert _attempt(runner.store, attempt.attempt_id)["state"] == "dispatching"
        assert runner.drafts.get(attempt.session_id).text != ""
        stack.clock.advance(30, monotonic=False)
    finally:
        release.set()
    row = await _ended(runner, attempt)
    assert await asyncio.to_thread(committed.wait, HANG_GUARD_S)
    assert writes == ["running", "draft", "finish"]
    assert (row["outcome"], row["reason"], row["dispatched_at"]) == (
        "succeeded",
        "",
        stamp(NOW),
    )
    assert runner.routines.get(routine_id)["tested_revision"] == 1
    assert runner.drafts.get(attempt.session_id).text == ""
    assert len(_rows(runner.store, "SELECT * FROM task_attempts")) == 1


@pytest.mark.parametrize(
    ("script", "expected", "answers"),
    [
        pytest.param((CARD,), ("expired", "waiting_for_user"), [DENY], id="card"),
        pytest.param((REPLY,), ("expired", "deadline"), [], id="no_card"),
        pytest.param((DONE,), ("succeeded", ""), [], id="known_success"),
    ],
)
async def test_deadline_stops_held_native_send_before_task_writes(
    stack: Stack,
    monkeypatch: pytest.MonkeyPatch,
    script: tuple[Event, ...],
    expected: tuple[str, str],
    answers: list[object],
) -> None:
    runner, routine_id = _made(stack)
    stack.script = script
    entered, send_release = asyncio.Event(), asyncio.Event()
    closing, close_release = asyncio.Event(), asyncio.Event()
    send, close = TurnDriver.send, TurnDriver.close

    async def held_send(driver: TurnDriver, text: str) -> None:
        await send(driver, text)
        entered.set()
        await send_release.wait()

    async def held_close(driver: TurnDriver) -> None:
        closing.set()
        await close_release.wait()
        await close(driver)

    monkeypatch.setattr(TurnDriver, "send", held_send)
    monkeypatch.setattr(TurnDriver, "close", held_close)
    attempt, _ = await runner.run_now(routine_id, _ident(), 1)
    try:
        await asyncio.wait_for(entered.wait(), HANG_GUARD_S)
        stack.pass_deadline()
        await asyncio.wait_for(closing.wait(), HANG_GUARD_S)
        assert stack.answers == answers
        assert _attempt(runner.store, attempt.attempt_id)["state"] == "dispatching"
        assert runner.drafts.get(attempt.session_id).text != ""
    finally:
        send_release.set()
        close_release.set()
    row = await _ended(runner, attempt)
    assert (row["outcome"], row["reason"]) == expected
    assert row["dispatched_at"] == stamp(NOW)
    assert runner.drafts.get(attempt.session_id).text == ""
    assert stack.manager.get(attempt.session_id) is None


async def test_unexpected_error_after_receipt_joins_its_command_before_finishing(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, routine_id = _made(stack)
    stack.script = (REPLY,)
    entered, release = asyncio.Event(), asyncio.Event()
    closing = asyncio.Event()
    send, close, submit = TurnDriver.send, TurnDriver.close, runner.commands.submit

    async def held_send(driver: TurnDriver, text: str) -> None:
        await send(driver, text)
        entered.set()
        await asyncio.Event().wait()

    async def held_close(driver: TurnDriver) -> None:
        closing.set()
        await release.wait()
        await close(driver)

    async def fail_after_receipt(*args: Any, **kwargs: Any) -> Any:
        await submit(*args, **kwargs)
        await entered.wait()
        raise RuntimeError("after receipt")

    monkeypatch.setattr(TurnDriver, "send", held_send)
    monkeypatch.setattr(TurnDriver, "close", held_close)
    monkeypatch.setattr(runner.commands, "submit", fail_after_receipt)
    attempt, _ = await runner.run_now(routine_id, _ident(), 1)
    try:
        await asyncio.wait_for(closing.wait(), HANG_GUARD_S)
        assert _attempt(runner.store, attempt.attempt_id)["state"] == "dispatching"
    finally:
        release.set()
    row = await _ended(runner, attempt)
    assert (row["outcome"], row["reason"]) == ("unknown", "internal")
    assert len(stack.texts) == 1


@pytest.mark.parametrize(
    ("script", "late", "ended", "answers"),
    [
        pytest.param((CARD,), True, ("expired", "waiting_for_user"), [DENY], id="card_open"),
        pytest.param((REPLY,), True, ("expired", "deadline"), [], id="still_streaming"),
        pytest.param(
            (CARD, RequestAnswered(tool_use_id="t1"), DONE),
            False,
            ("succeeded", ""),
            [],
            id="answered_in_time",
        ),
    ],
)
async def test_the_deadline_denies_an_open_card_and_every_run_closes_its_session(
    stack: Stack,
    script: tuple[Event, ...],
    late: bool,
    ended: tuple[str, str],
    answers: list[object],
) -> None:
    """The runner's only permission answer is a denial at the deadline. Nothing is retried:
    one command and one driver for the attempt."""
    runner, routine_id = _made(stack)
    stack.script = script
    if late:
        stack.before_send = stack.pass_deadline
    row = await _manual(runner, routine_id)
    assert (row["outcome"], row["reason"]) == ended
    assert stack.answers == answers
    assert stack.manager.get(row["session_id"]) is None
    commands = _rows(runner.store, "SELECT command_id FROM commands")
    assert commands == [{"command_id": row["attempt_id"]}] and len(stack.rig.drivers) == 1


async def test_unattended_question_card_is_denied_once_at_deadline_without_retry(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, routine_id = _made(stack)
    _ordinary(stack.rig)
    stack.script = (
        QuestionAsked(
            tool_use_id="t1",
            questions=[
                Question(
                    header="Choice",
                    question="Pick one",
                    options=[QuestionOption(label="A"), QuestionOption(label="B")],
                )
            ],
        ),
    )
    card_sent = asyncio.Event()
    send = TurnDriver.send

    async def watched_send(driver: TurnDriver, text: str) -> None:
        await send(driver, text)
        card_sent.set()

    monkeypatch.setattr(TurnDriver, "send", watched_send)
    attempt = _open(runner.store, routine_id, 0, "claimed")
    runner.start(attempt)
    await asyncio.wait_for(card_sent.wait(), HANG_GUARD_S)
    assert stack.rig.drivers[0].pending() == ["t1"]
    stack.pass_deadline()
    row = await _ended(runner, attempt)
    assert (row["outcome"], row["reason"]) == ("expired", "waiting_for_user")
    assert stack.answers == [DENY]
    assert stack.manager.get(attempt.session_id) is None
    assert len(stack.texts) == len(stack.rig.drivers) == 1
    assert _rows(runner.store, "SELECT command_id FROM commands") == [
        {"command_id": attempt.attempt_id}
    ]
    assert runner.routines.get(routine_id)["tested_revision"] is None


# --- a routine that stops at a draft ---

STOP = PermissionAnswer(allow=False, stop_at_draft=True)


def _stopping(stack: Stack) -> tuple[TaskRunner, str]:
    """A runner and a routine that stops at the first confirmation."""
    runner = stack.runner()
    raw = {**_raw(stack), "on_confirmation": "stop"}
    return runner, runner.routines.create(_ident(), raw)[0]["routine_id"]


async def test_a_stop_routine_denies_the_first_permission_card_at_once_and_records_needs_decision(
    stack: Stack,
) -> None:
    runner, routine_id = _stopping(stack)
    stack.script = (
        PermissionRequested(tool_use_id="t1", kind="tool", tool="Write", summary="x"),
        DONE,
    )
    row = await _manual(runner, routine_id)
    assert (row["outcome"], row["reason"]) == ("blocked", "needs_decision")
    assert stack.answers == [STOP]
    assert _rows(runner.store, "SELECT command_id FROM commands") == [
        {"command_id": row["attempt_id"]}
    ]
    assert len(stack.texts) == len(stack.rig.drivers) == 1
    assert stack.manager.get(row["session_id"]) is None
    assert runner.routines.get(routine_id)["tested_revision"] == 1


async def test_a_stop_routine_denies_a_plan_card_and_each_later_card(stack: Stack) -> None:
    runner, routine_id = _stopping(stack)
    stack.script = (
        PermissionRequested(tool_use_id="t1", kind="plan", tool="ExitPlanMode", summary=""),
        PermissionRequested(tool_use_id="t2", kind="tool", tool="Write", summary="x"),
        DONE,
    )
    row = await _manual(runner, routine_id)
    assert (row["outcome"], row["reason"]) == ("blocked", "needs_decision")
    assert stack.answers == [STOP, STOP]


async def test_a_stop_routine_leaves_questions_to_the_deadline(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A main-agent question and a subagent's AskUserQuestion card wait as they do today."""
    runner, routine_id = _stopping(stack)
    _ordinary(stack.rig)
    stack.script = (
        QuestionAsked(
            tool_use_id="t1",
            questions=[Question(header="Choice", question="Pick one", options=[])],
        ),
        PermissionRequested(
            tool_use_id="t2", kind="tool", tool="AskUserQuestion", summary="", agent="sub"
        ),
    )
    card_sent = asyncio.Event()
    send = TurnDriver.send

    async def watched_send(driver: TurnDriver, text: str) -> None:
        await send(driver, text)
        card_sent.set()

    monkeypatch.setattr(TurnDriver, "send", watched_send)
    attempt = _open(runner.store, routine_id, 0, "claimed")
    runner.start(attempt)
    await asyncio.wait_for(card_sent.wait(), HANG_GUARD_S)
    await asyncio.sleep(0)  # a deferred stop answer would have run by now
    assert stack.answers == [] and stack.rig.drivers[0].pending() == ["t1", "t2"]
    stack.pass_deadline()
    row = await _ended(runner, attempt)
    assert (row["outcome"], row["reason"]) == ("expired", "waiting_for_user")
    assert stack.answers == [DENY, DENY]
    assert runner.routines.get(routine_id)["tested_revision"] is None


async def test_a_manual_run_blocked_for_another_reason_leaves_its_revision_untested(
    stack: Stack,
) -> None:
    runner = stack.runner()
    routine_id = runner.routines.create(_ident(), _raw(stack))[0]["routine_id"]
    stack.script = (RateLimit(status="rejected"), DONE)
    row = await _manual(runner, routine_id)
    assert (row["outcome"], row["reason"]) == ("blocked", "quota")
    assert runner.routines.get(routine_id)["tested_revision"] is None


async def test_a_stopped_turn_that_fails_keeps_its_failure(stack: Stack) -> None:
    runner, routine_id = _stopping(stack)
    stack.script = (CARD, TurnDone(status="error", usage=Usage()))
    row = await _manual(runner, routine_id)
    assert (row["outcome"], row["reason"]) == ("failed", "turn_error")
    assert stack.answers == [STOP]
    assert runner.routines.get(routine_id)["tested_revision"] is None


async def test_a_card_withdrawn_before_the_stop_answer_is_not_a_needs_decision(
    stack: Stack,
) -> None:
    runner, routine_id = _stopping(stack)
    stack.script = (CARD, RequestCancelled(tool_use_id="t1"), DONE)
    row = await _manual(runner, routine_id)
    assert (row["outcome"], row["reason"]) == ("succeeded", "")
    assert stack.answers == []
    assert runner.routines.get(routine_id)["tested_revision"] == 1


async def test_a_failed_stop_answer_logs_one_fixed_line(
    stack: Stack, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    runner, routine_id = _stopping(stack)
    stack.script = (CARD, DONE)

    def broken(_driver: TurnDriver, _tool_use_id: str, _answer: Any) -> bool:
        raise RuntimeError("frame text")

    monkeypatch.setattr(TurnDriver, "respond", broken)
    row = await _manual(runner, routine_id)
    assert (row["outcome"], row["reason"]) == ("succeeded", "")
    assert [
        (record.getMessage(), record.exc_info)
        for record in caplog.records
        if record.name == "viki_agent.automation.tasks"
    ] == [("routine card stop failed", None)]


async def test_a_reply_naming_routine_actions_changes_no_routine_state(stack: Stack) -> None:
    """A scheduled run writes nothing to its routine, whatever the reply says."""
    runner, routine_id = _made(stack)
    _ordinary(stack.rig)
    before = runner.routines.get(routine_id)
    stack.script = (REPLY, DONE)
    assert (await _scheduled(runner, routine_id, 0))["outcome"] == "succeeded"
    # Its state, state_revision and current_revision included.
    assert runner.routines.get(routine_id) == before


# --- runs Decision starts ---


def _decision(tasks: TaskStore, routine_id: str, minute: int) -> Attempt:
    """A claimed decision attempt, without `claim_decision`'s routine checks."""
    with tasks.store._connection(write=True) as connection:
        return tasks.insert_claim(
            connection,
            routine_id=routine_id,
            kind="decision",
            revision=1,
            now=_at(minute),
            request_hash=CARDS,
        )


async def _decided(runner: TaskRunner, routine_id: str, minute: int) -> dict[str, Any]:
    attempt = _decision(runner.store, routine_id, minute)
    runner.start(attempt)
    return await _ended(runner, attempt)


async def test_a_decision_run_needs_an_ordinary_root_and_a_reviewed_mode(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Readiness refuses before a session is bound; the bound root and mode refuse after it,
    should readiness have passed before the bind."""
    runner, routine_id = _made(stack)
    rig = stack.rig

    def sessions() -> int:
        return len(_rows(runner.store, "SELECT * FROM sessions"))

    monkeypatch.setattr(runner.routines, "readiness", lambda document, *, scheduled: [])
    bound_policy = await _decided(runner, routine_id, 0)
    assert sessions() == 1
    monkeypatch.undo()
    readonly = await _decided(runner, routine_id, 1)
    _ordinary(rig)
    rig.settings.patch(_ident(), 0, {"chat.default_mode": "acceptEdits"})
    accept_edits = await _decided(runner, routine_id, 2)
    assert sessions() == 1
    monkeypatch.setattr(runner.routines, "readiness", lambda document, *, scheduled: [])
    bound_mode = await _decided(runner, routine_id, 3)
    assert sessions() == 2 and rig.drivers == []
    assert [
        (row["outcome"], row["reason"])
        for row in (bound_policy, readonly, accept_edits, bound_mode)
    ] == [
        ("blocked", "unattended_policy"),
        ("blocked", "unattended_policy"),
        ("blocked", "unattended_mode"),
        ("blocked", "unattended_mode"),
    ]
    rig.settings.patch(_ident(), 1, {"chat.default_mode": "auto"})
    auto = await _decided(runner, routine_id, 4)
    assert (auto["outcome"], auto["reason"]) == ("succeeded", "")
    assert len(rig.drivers) == 1
    assert rig.state.prefs(rig.drivers[0].launch.session_id).mode == "auto"


async def test_a_decision_run_keeps_its_deadline_confirmation_and_bot_hooks(
    stack: Stack,
) -> None:
    runner, routine_id = _stopping(stack)
    _ordinary(stack.rig)
    calls: list[tuple[str, Any]] = []
    envelopes_seen: list[str] = []

    async def begin(attempt: Attempt, document: dict[str, Any]) -> None:
        calls.append(("begin", attempt.attempt_id))

    def watch(attempt: Attempt, _actor: Any, _result: dict[str, Any]) -> Any:
        calls.append(("observe", attempt.attempt_id))
        return lambda envelope: envelopes_seen.append(envelope["type"])

    async def finished(attempt: Attempt, ended: str, reason: str) -> None:
        calls.append(("finished", (attempt.attempt_id, ended, reason)))

    def discard(attempt: Attempt) -> None:
        calls.append(("discard", attempt.attempt_id))

    runner.begin_attempt, runner.observe_attempt = begin, watch
    runner.attempt_finished, runner.discard_attempt = finished, discard
    stack.script = (
        PermissionRequested(tool_use_id="t1", kind="tool", tool="Write", summary="x"),
        DONE,
    )
    stopped = await _decided(runner, routine_id, 0)
    assert (stopped["outcome"], stopped["reason"]) == ("blocked", "needs_decision")
    assert stack.answers == [STOP]
    first = stopped["attempt_id"]
    assert calls == [
        ("begin", first),
        ("observe", first),
        ("finished", (first, "blocked", "needs_decision")),
        ("discard", first),
    ]
    assert "permission.requested" in envelopes_seen and "turn.done" in envelopes_seen
    # A question waits for no stop answer; the deadline denies it.
    calls.clear()
    stack.script = (QuestionAsked(tool_use_id="q1", questions=[]),)
    stack.before_send = stack.pass_deadline
    late = await _decided(runner, routine_id, 1)
    assert (late["outcome"], late["reason"]) == ("expired", "waiting_for_user")
    assert stack.answers == [STOP, DENY]
    assert calls[-2:] == [
        ("finished", (late["attempt_id"], "expired", "waiting_for_user")),
        ("discard", late["attempt_id"]),
    ]
    assert stack.manager.get(late["session_id"]) is None
    assert len(stack.texts) == len(stack.rig.drivers) == 2


async def test_only_a_manual_run_records_tested(stack: Stack) -> None:
    runner, routine_id = _made(stack)
    _ordinary(stack.rig)
    for row in (await _scheduled(runner, routine_id, 0), await _decided(runner, routine_id, 1)):
        assert (row["outcome"], row["reason"]) == ("succeeded", "")
        assert runner.routines.get(routine_id)["tested_revision"] is None
    assert (await _manual(runner, routine_id))["outcome"] == "succeeded"
    assert runner.routines.get(routine_id)["tested_revision"] == 1


def _enabled(runner: TaskRunner, routine_id: str) -> None:
    """Enable the routine tested at its one revision, as a decision claim needs."""
    with runner.store.store._connection(write=True) as connection:
        connection.execute(
            "UPDATE routines SET state='enabled',tested_revision=1,enabled_since=? "
            "WHERE routine_id=?",
            (T0, routine_id),
        )


async def _run_decision(runner: TaskRunner, routine_id: str, **options: Any) -> Attempt:
    return await runner.run_decision(
        routine_id, 1, newest=None, request_hash=CARDS, limit=4, day=DAY, **options
    )


@pytest.mark.parametrize("unavailable", ["preparer", "closing", "window", "ready"])
async def test_run_decision_refuses_while_unavailable_closing_unattended_or_not_ready(
    stack: Stack, monkeypatch: pytest.MonkeyPatch, unavailable: str
) -> None:
    runner, routine_id = _made(stack)
    _ordinary(stack.rig)
    _enabled(runner, routine_id)
    tables = ("routine_occurrences", "task_attempts")
    before = [_rows(runner.store, f"SELECT * FROM {table}") for table in tables]
    if unavailable == "preparer":
        runner.prepare = None
    elif unavailable == "closing":
        await runner.close()
    elif unavailable == "window":
        runner.attended = lambda: False
    options = {"ready": lambda: False} if unavailable == "ready" else {}

    def unexpected(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("an unavailable runner must not claim or start a task")

    monkeypatch.setattr(runner.store, "claim_decision", unexpected)
    monkeypatch.setattr(runner, "start", unexpected)
    with pytest.raises(RoutineError, match="^unavailable$"):
        await _run_decision(runner, routine_id, **options)
    assert [_rows(runner.store, f"SELECT * FROM {table}") for table in tables] == before
    assert runner._runs == {} and stack.texts == []


async def test_run_decision_claims_under_the_admission_lock_and_starts_the_run(
    stack: Stack,
) -> None:
    runner, routine_id = _made(stack)
    _ordinary(stack.rig)
    _enabled(runner, routine_id)
    locked: list[bool] = []

    def ready() -> bool:
        locked.append(runner._admission_lock.locked())
        return True

    async with runner._admission_lock:
        claim = asyncio.create_task(_run_decision(runner, routine_id, ready=ready))
        for _ in range(5):
            await asyncio.sleep(0)
        assert not claim.done() and locked == []
    attempt = await asyncio.wait_for(claim, HANG_GUARD_S)
    assert locked == [True]
    assert (attempt.routine_id, attempt.revision, attempt.kind) == (routine_id, 1, "decision")
    assert list(runner._attempts.values()) == [attempt]
    row = await _ended(runner, attempt)
    assert (row["outcome"], row["reason"]) == ("succeeded", "")
    assert runner._attempts == {}
    # The claim's own refusals pass through unchanged.
    with pytest.raises(RoutineError, match="^conflict$"):
        await _run_decision(runner, routine_id)
    assert len(stack.texts) == 1


async def test_a_card_opening_or_closing_calls_the_bound_watcher(
    stack: Stack, caplog: pytest.LogCaptureFixture
) -> None:
    runner, routine_id = _made(stack)
    _ordinary(stack.rig)
    seen: list[tuple[str, ...]] = []
    runner.bind_cards(
        lambda: seen.append(tuple(sorted(next(iter(runner._runs.values())).open_cards)))
    )
    stack.script = (
        CARD,
        REPLY,
        RequestAnswered(tool_use_id="t1"),
        QuestionAsked(tool_use_id="q1", questions=[]),
        DONE,
    )
    assert (await _decided(runner, routine_id, 0))["outcome"] == "succeeded"
    # Opened, answered, a question opened, then the turn's end closed it; text calls nothing.
    assert seen == [("t1",), (), ("q1",), ()]

    def broken() -> None:
        raise RuntimeError("card text")

    runner.bind_cards(broken)
    stack.script = (CARD, RequestAnswered(tool_use_id="t1"), DONE)
    assert (await _decided(runner, routine_id, 1))["outcome"] == "succeeded"
    assert [
        (record.getMessage(), record.exc_info)
        for record in caplog.records
        if record.name == "viki_agent.automation.tasks"
    ] == [("routine card watch failed", None)] * 2


async def test_waiting_names_each_run_with_an_open_card_and_its_kind(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, routine_id = _made(stack)
    _ordinary(stack.rig)
    sent = asyncio.Event()
    send = TurnDriver.send

    async def watched_send(driver: TurnDriver, text: str) -> None:
        await send(driver, text)
        sent.set()

    monkeypatch.setattr(TurnDriver, "send", watched_send)
    # The three runs end together, each writing the store from its own thread. A slow disk can
    # hold its write lock past the store's busy wait, which this test is not about.
    monkeypatch.setattr(storage, "BUSY_TIMEOUT_S", HANG_GUARD_S)
    question = QuestionAsked(tool_use_id="q1", questions=[])
    scripts = (
        (question,),
        (
            question,
            PermissionRequested(tool_use_id="t2", kind="plan", tool="ExitPlanMode", summary=""),
        ),
        (REPLY,),
    )
    attempts: list[Attempt] = []
    for minute, script in enumerate(scripts):
        stack.script = script
        sent.clear()
        attempts.append(_decision(runner.store, routine_id, minute))
        runner.start(attempts[-1])
        await asyncio.wait_for(sent.wait(), HANG_GUARD_S)
    asked, mixed, _streaming = attempts
    assert runner.waiting() == (Waiting(asked, True), Waiting(mixed, False))
    stack.pass_deadline()
    await asyncio.wait_for(asyncio.gather(*runner._runs), HANG_GUARD_S)
    assert runner.waiting() == ()


async def _sent(stack: Stack, runner: TaskRunner, routine_id: str) -> Attempt:
    """A manual run whose turn is sent and still running, once its command has ended (a send
    ends `unknown`: it is no proof of native pickup)."""
    sent = asyncio.Event()
    stack.before_send = sent.set
    attempt, _ = await runner.run_now(routine_id, _ident(), 1)
    await asyncio.wait_for(sent.wait(), HANG_GUARD_S)
    await asyncio.wait_for(asyncio.gather(*runner.commands._jobs), HANG_GUARD_S)
    return attempt


AFTER_ERROR = [
    pytest.param((), id="clean"),
    pytest.param(
        (Error(code="internal", message="", recoverable=True),), id="after_a_recoverable_error"
    ),
]


@pytest.mark.parametrize("prefix", AFTER_ERROR)
async def test_settle_then_close_all_mid_run_ends_it_app_closed(
    stack: Stack, prefix: tuple[Event, ...]
) -> None:
    """The lifespan's order: settle denies the open card and close_all ends the session; the
    runner's close then lets the run that saw that end write it and close its session. A
    recoverable error earlier in the turn does not outrank that end."""
    runner, routine_id = _made(stack)
    stack.script = (*prefix, CARD)
    attempt = await _sent(stack, runner, routine_id)
    await stack.manager.settle(0.0, until=lambda: True)
    await stack.manager.close_all()
    await asyncio.wait_for(runner.close(), HANG_GUARD_S)
    row = _attempt(runner.store, attempt.attempt_id)
    assert (row["outcome"], row["reason"]) == ("cancelled", "app_closed")
    assert stack.answers == [DENY]
    assert stack.manager.get(attempt.session_id) is None


@pytest.mark.parametrize("prefix", AFTER_ERROR)
async def test_close_during_a_sent_turn_writes_its_unknown_end(
    stack: Stack, prefix: tuple[Event, ...]
) -> None:
    runner, routine_id = _made(stack)
    stack.script = (*prefix, REPLY)
    attempt = await _sent(stack, runner, routine_id)
    await asyncio.wait_for(runner.close(), HANG_GUARD_S)
    row = _attempt(runner.store, attempt.attempt_id)
    assert (row["outcome"], row["reason"]) == ("unknown", "native_unconfirmed")
    assert runner._runs == {}
    # A stopped run leaves its session to the lifespan's close_all.
    assert stack.manager.get(attempt.session_id) is not None


@pytest.mark.parametrize("failure", [None, ProfileError("capacity"), RuntimeError("synthetic")])
async def test_close_leaves_a_run_held_in_prepare_claimed_for_reconcile(
    stack: Stack, failure: Exception | None
) -> None:
    runner, routine_id = _made(stack)
    entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def held(_profile_id: str, _raw: dict[str, Any]) -> dict[str, Any]:
        entered.set()
        await release.wait()
        finished.set()
        if failure is not None:
            raise failure
        return {"launchable": False}

    runner.prepare = held
    attempt, _ = await runner.run_now(routine_id, _ident(), 1)
    await asyncio.wait_for(entered.wait(), HANG_GUARD_S)
    closing = asyncio.create_task(runner.close())
    try:
        cycled = asyncio.Event()
        asyncio.get_running_loop().call_soon(cycled.set)
        await cycled.wait()
        assert not closing.done() and len(runner._runs) == 1
    finally:
        release.set()
        await asyncio.wait_for(closing, HANG_GUARD_S)
    assert finished.is_set()
    assert _attempt(runner.store, attempt.attempt_id)["state"] == "claimed"
    assert _rows(runner.store, "SELECT * FROM commands") == [] and stack.rig.drivers == []
    assert TaskStore(runner.store.store, runner.store.scope, NEW).reconcile(LATER) == 1
    assert _results(runner.store)[attempt.attempt_id] == ("done", "cancelled", "restarted", LATER)


@pytest.mark.parametrize("stage", ["transaction", "catalog", "reopenable"])
@pytest.mark.parametrize("cancelled", [False, True])
async def test_close_joins_each_prepare_await_and_retires_the_late_actor(
    stack: Stack, monkeypatch: pytest.MonkeyPatch, stage: str, cancelled: bool
) -> None:
    runner, routine_id = _made(stack)
    runner.prepare = stack.app.state.routine_tasks.prepare
    loop = asyncio.get_running_loop()
    entered, release, settled = asyncio.Event(), threading.Event(), threading.Event()
    closing_actor, actor_release = asyncio.Event(), asyncio.Event()
    callbacks: list[str] = []
    attach, close = stack.manager.attach_prepared, stack.manager.close

    def pause_worker() -> None:
        loop.call_soon_threadsafe(entered.set)
        try:
            assert release.wait(HANG_GUARD_S)
        finally:
            settled.set()

    if stage == "transaction":
        entered, release, settled = _hold_sql_write(
            runner.store.store, monkeypatch, "INSERT INTO session_profile_bindings"
        )
    elif stage == "catalog":
        exists = stack.rig.catalog.exists

        def held_exists(session_id: str) -> bool:
            result = exists(session_id)
            pause_worker()
            return result

        monkeypatch.setattr(stack.rig.catalog, "exists", held_exists)
    else:
        reopenable = ProfileLaunchService.reopenable

        def held_reopenable(service: ProfileLaunchService, session_id: str, exists: bool) -> None:
            reopenable(service, session_id, exists)
            pause_worker()

        monkeypatch.setattr(ProfileLaunchService, "reopenable", held_reopenable)

    def late_attach(session_id: str, *, profile: bool | None = None) -> Any:
        assert settled.is_set() and runner._closing
        callbacks.append("attach")
        return attach(session_id, profile=profile)

    async def held_close(session_id: str) -> None:
        assert settled.is_set() and callbacks == ["attach"]
        closing_actor.set()
        await actor_release.wait()
        await close(session_id)
        callbacks.append("close")

    monkeypatch.setattr(stack.manager, "attach_prepared", late_attach)
    monkeypatch.setattr(stack.manager, "close", held_close)
    attempt, _ = await runner.run_now(routine_id, _ident(), 1)
    closing: asyncio.Task[None] | None = None
    result: BaseException | None = None
    try:
        await asyncio.wait_for(entered.wait(), HANG_GUARD_S)
        assert stack.manager.get(attempt.session_id) is None
        closing = asyncio.create_task(runner.close())
        cycled = asyncio.Event()
        loop.call_soon(cycled.set)
        await cycled.wait()
        assert not closing.done() and not settled.is_set()
        assert len(runner._runs) == 1 and callbacks == []
        if cancelled:
            for _ in range(2):
                closing.cancel()
                cycled = asyncio.Event()
                loop.call_soon(cycled.set)
                await cycled.wait()
                assert not closing.done() and not settled.is_set()
        release.set()
        await asyncio.wait_for(closing_actor.wait(), HANG_GUARD_S)
        assert stack.manager.get(attempt.session_id) is not None
        assert not closing.done() and len(runner._runs) == 1
        if cancelled:
            for _ in range(2):
                closing.cancel()
                cycled = asyncio.Event()
                loop.call_soon(cycled.set)
                await cycled.wait()
                assert not closing.done()
                assert next(iter(runner._runs)).cancelling() == 1
    finally:
        release.set()
        actor_release.set()
        if closing is not None:
            [result] = await asyncio.wait_for(
                asyncio.gather(closing, return_exceptions=True), HANG_GUARD_S
            )
        await runner.close()
        assert await asyncio.to_thread(settled.wait, HANG_GUARD_S)
    assert isinstance(result, asyncio.CancelledError) if cancelled else result is None
    assert callbacks == ["attach", "close"] and runner._runs == {}
    assert stack.manager.get(attempt.session_id) is None
    for table in ("sessions", "session_profile_bindings", "drafts"):
        assert len(_rows(runner.store, f"SELECT * FROM {table}")) == 1
    assert _attempt(runner.store, attempt.attempt_id)["state"] == "claimed"
    assert stack.texts == [] and stack.rig.drivers == []
    assert _rows(runner.store, "SELECT * FROM commands") == []
    assert TaskStore(runner.store.store, runner.store.scope, NEW).reconcile(LATER) == 1
    assert _results(runner.store)[attempt.attempt_id] == ("done", "cancelled", "restarted", LATER)


@pytest.mark.parametrize("cancelled", [False, True])
@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (ProfileError("capacity"), ("blocked", "capacity")),
        (RuntimeError("synthetic"), ("failed", "internal")),
    ],
)
async def test_prepare_failure_after_actor_attachment_keeps_its_shutdown_or_normal_outcome(
    stack: Stack, failure: Exception, expected: tuple[str, str], cancelled: bool
) -> None:
    runner, routine_id = _made(stack)
    prepare = stack.app.state.routine_tasks.prepare
    entered, release = asyncio.Event(), asyncio.Event()

    async def held(profile_id: str, raw: dict[str, Any]) -> dict[str, Any]:
        await prepare(profile_id, raw)
        entered.set()
        await release.wait()
        raise failure

    runner.prepare = held
    attempt, _ = await runner.run_now(routine_id, _ident(), 1)
    closing: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(entered.wait(), HANG_GUARD_S)
        assert stack.manager.get(attempt.session_id) is not None
        if cancelled:
            closing = asyncio.create_task(runner.close())
            cycled = asyncio.Event()
            asyncio.get_running_loop().call_soon(cycled.set)
            await cycled.wait()
            assert not closing.done() and len(runner._runs) == 1
    finally:
        release.set()
        if closing is not None:
            await asyncio.wait_for(closing, HANG_GUARD_S)
    row = await _ended(runner, attempt)
    if cancelled:
        assert row["state"] == "claimed" and row["outcome"] is None
    else:
        assert (row["outcome"], row["reason"]) == expected
    assert stack.manager.get(attempt.session_id) is None
    assert stack.texts == [] and stack.rig.drivers == []
    assert _rows(runner.store, "SELECT * FROM commands") == []


async def test_a_run_refused_before_acceptance_clears_its_draft(
    stack: Stack, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Left composed, each refused run's draft would count toward the cap on non-empty drafts;
    a clear that fails is only logged, with its fixed text."""
    runner, routine_id = _made(stack)

    async def leave_after_prepare(profile_id: str, raw: dict[str, Any]) -> dict[str, Any]:
        prepared = await stack.prepare(profile_id, raw)
        runner.attended = lambda: False  # admitted claim, but nobody remains for its command
        return prepared

    runner.prepare = leave_after_prepare
    cleared = await _manual(runner, routine_id)
    save = runner.drafts.save

    def refuse_clear(key: str, revision: int, text: str | TurnInput) -> Draft:
        if text == "":
            raise DraftError("unavailable")
        return save(key, revision, text)

    monkeypatch.setattr(runner.drafts, "save", refuse_clear)
    runner.attended = lambda: True
    kept = await _manual(runner, routine_id)
    assert [(row["outcome"], row["reason"]) for row in (cleared, kept)] == [
        ("cancelled", "app_closed")
    ] * 2
    assert runner.drafts.get(cleared["session_id"]).text == ""
    assert runner.drafts.get(kept["session_id"]).text == (
        f"{profile_document()['instructions']}\n\nSummarize overnight news."
    )
    assert [
        (record.getMessage(), record.exc_info)
        for record in caplog.records
        if record.name == "viki_agent.automation.tasks"
    ] == [("routine draft clear failed", None)]
    assert _rows(runner.store, "SELECT * FROM commands") == []


async def test_the_last_window_closing_while_the_cli_starts_ends_app_closed(
    stack: Stack,
) -> None:
    """The command was accepted; the outbox refuses its send once no window is open."""
    runner, routine_id = _made(stack)

    async def leave() -> None:
        runner.attended = lambda: False

    stack.before_open = leave
    row = await _manual(runner, routine_id)
    assert (row["outcome"], row["reason"]) == ("cancelled", "app_closed")
    assert (await runner.commands.get(row["attempt_id"])).state == "not_dispatched"
    assert len(stack.rig.drivers) == 1 and stack.texts == []


async def test_the_outbox_closing_while_the_cli_starts_ends_app_closed(stack: Stack) -> None:
    runner, routine_id = _made(stack)
    starting = asyncio.Event()

    async def held() -> None:
        starting.set()
        await asyncio.get_running_loop().create_future()  # never resolved

    stack.before_open = held
    attempt, _ = await runner.run_now(routine_id, _ident(), 1)
    await asyncio.wait_for(starting.wait(), HANG_GUARD_S)
    await asyncio.wait_for(runner.commands.close(), HANG_GUARD_S)
    row = await _ended(runner, attempt)
    assert (row["outcome"], row["reason"]) == ("cancelled", "app_closed")
    command = await runner.commands.get(attempt.attempt_id)
    assert (command.state, command.reason) == ("not_dispatched", "cancelled")
    assert stack.texts == []


@pytest.mark.parametrize(
    "window", [pytest.param(True, id="window_open"), pytest.param(False, id="no_window")]
)
async def test_a_cli_start_refused_reads_its_reason_whatever_the_window(
    stack: Stack, window: bool
) -> None:
    """The runner's admission check never refused this command, so the window state when the
    outcome is read does not change it."""
    runner, routine_id = _made(stack)

    async def refuse() -> None:
        runner.attended = lambda: window
        raise StartError("bad_request", "refused")

    stack.before_open = refuse
    row = await _manual(runner, routine_id)
    assert (row["outcome"], row["reason"]) == ("failed", "start_failed")
    command = await runner.commands.get(row["attempt_id"])
    assert (command.state, command.reason) == ("not_dispatched", "start_failed")
    assert stack.texts == []


def _absent_once(runner: TaskRunner) -> None:
    """No window when the runner next checks, and one open again at every later check."""
    answers = iter([False])
    runner.attended = lambda: next(answers, True)


async def test_an_outbox_refusal_stays_app_closed_when_a_window_returns(stack: Stack) -> None:
    runner, routine_id = _made(stack)

    async def leave() -> None:
        _absent_once(runner)

    stack.before_open = leave
    row = await _manual(runner, routine_id)
    assert (row["outcome"], row["reason"]) == ("cancelled", "app_closed")
    command = await runner.commands.get(row["attempt_id"])
    assert (command.state, command.reason) == ("not_dispatched", "runtime_changed")
    assert stack.texts == []


async def test_a_refusal_before_acceptance_stays_app_closed_when_a_window_returns(
    stack: Stack,
) -> None:
    runner, routine_id = _made(stack)

    async def leave_after_prepare(profile_id: str, raw: dict[str, Any]) -> dict[str, Any]:
        prepared = await stack.prepare(profile_id, raw)
        _absent_once(runner)
        return prepared

    runner.prepare = leave_after_prepare
    row = await _manual(runner, routine_id)
    assert (row["outcome"], row["reason"]) == ("cancelled", "app_closed")
    assert _rows(runner.store, "SELECT * FROM commands") == []


@pytest.mark.parametrize(
    ("script", "expected"),
    [
        pytest.param((DONE,), ("succeeded", ""), id="success"),
        pytest.param(
            (
                Error(code="internal", message="", recoverable=True),
                TurnDone(status="error", usage=Usage()),
            ),
            ("failed", "internal"),
            id="error",
        ),
        pytest.param(
            (TurnDone(status="cancelled", usage=Usage()),),
            ("cancelled", "stopped"),
            id="stopped",
        ),
        pytest.param(
            (SessionClosed(reason="browser_gone"),),
            ("cancelled", "app_closed"),
            id="session_closed",
        ),
    ],
)
async def test_close_stops_held_callback_and_keeps_native_end(
    stack: Stack,
    monkeypatch: pytest.MonkeyPatch,
    script: tuple[Event, ...],
    expected: tuple[str, str],
) -> None:
    runner, routine_id = _made(stack)
    stack.script = script
    sent, send_release = asyncio.Event(), asyncio.Event()
    closing, close_release = asyncio.Event(), asyncio.Event()
    send, close = TurnDriver.send, TurnDriver.close

    async def held_send(driver: TurnDriver, text: str) -> None:
        await send(driver, text)
        sent.set()
        await send_release.wait()

    async def held_close(driver: TurnDriver) -> None:
        closing.set()
        await close_release.wait()
        await close(driver)

    monkeypatch.setattr(TurnDriver, "send", held_send)
    monkeypatch.setattr(TurnDriver, "close", held_close)
    attempt, _ = await runner.run_now(routine_id, _ident(), 1)
    await asyncio.wait_for(sent.wait(), HANG_GUARD_S)
    close_task = asyncio.create_task(runner.close())
    try:
        await asyncio.wait_for(closing.wait(), HANG_GUARD_S)
        assert not close_task.done()
        assert _attempt(runner.store, attempt.attempt_id)["state"] == "dispatching"
        assert runner.drafts.get(attempt.session_id).text != ""
    finally:
        send_release.set()
        close_release.set()
        await asyncio.wait_for(close_task, HANG_GUARD_S)
    row = _attempt(runner.store, attempt.attempt_id)
    assert (row["outcome"], row["reason"]) == expected
    assert row["dispatched_at"] == stamp(NOW)
    assert runner.drafts.get(attempt.session_id).text == ""
    assert stack.manager.get(attempt.session_id) is None
    assert (runner.routines.get(routine_id)["tested_revision"] == 1) == (expected[0] == "succeeded")
    assert len(stack.texts) == 1


@pytest.mark.parametrize("cancelled", [False, True])
async def test_close_joins_a_manual_claim_and_its_registered_run_before_returning(
    stack: Stack, monkeypatch: pytest.MonkeyPatch, cancelled: bool
) -> None:
    gate = _ClaimGate()
    runner, routine_id = _made(stack, admission_lock=gate)
    entered, claim_release, committed = _hold_sql_write(
        runner.store.store, monkeypatch, "INSERT INTO routine_occurrences"
    )
    draining, drain_release = asyncio.Event(), asyncio.Event()
    start, run = runner.start, runner._run
    registered: list[asyncio.Task[None]] = []
    closers: list[asyncio.Task[None]] = []
    command_id = _ident()

    def watched_start(attempt: Attempt) -> None:
        assert committed.is_set() and not runner._closing
        start(attempt)
        assert len(runner._runs) == 1
        registered.extend(runner._runs)

    async def held_drain(attempt: Attempt, seen: Observation) -> None:
        try:
            await run(attempt, seen)
        finally:
            draining.set()
            await drain_release.wait()

    monkeypatch.setattr(runner, "start", watched_start)
    monkeypatch.setattr(runner, "_run", held_drain)
    request = asyncio.create_task(runner.run_now(routine_id, command_id, 1))
    try:
        await asyncio.wait_for(entered.wait(), HANG_GUARD_S)
        closers.append(asyncio.create_task(runner.close()))
        cycled = asyncio.Event()
        asyncio.get_running_loop().call_soon(cycled.set)
        await cycled.wait()
        assert not closers[0].done() and not runner._closing and not committed.is_set()
        assert gate.waiting.is_set() and gate.owner is request
        closers.append(asyncio.create_task(runner.close()))
        if cancelled:
            for _ in range(2):
                closers[0].cancel()
                cycled = asyncio.Event()
                asyncio.get_running_loop().call_soon(cycled.set)
                await cycled.wait()
                assert not any(closer.done() for closer in closers) and not runner._closing
        claim_release.set()
        attempt, created = await asyncio.wait_for(request, HANG_GUARD_S)
        assert created and len(registered) == 1
        await asyncio.wait_for(draining.wait(), HANG_GUARD_S)
        assert runner._closing and not gate.locked()
        assert list(runner._runs) == registered and registered[0].cancelling() == 1
        assert not any(closer.done() for closer in closers)
        if cancelled:
            for _ in range(2):
                closers[0].cancel()
                cycled = asyncio.Event()
                asyncio.get_running_loop().call_soon(cycled.set)
                await cycled.wait()
                assert not any(closer.done() for closer in closers)
                assert registered[0].cancelling() == 1
        assert await runner.run_now(routine_id, command_id, 1) == (attempt, False)
        with pytest.raises(RoutineError, match="^unavailable$"):
            await runner.run_now(routine_id, _ident(), 1)
        assert len(_rows(runner.store, "SELECT * FROM task_attempts")) == 1
    finally:
        claim_release.set()
        drain_release.set()
        await asyncio.gather(request, return_exceptions=True)
        results = await asyncio.wait_for(
            asyncio.gather(*closers, return_exceptions=True), HANG_GUARD_S
        )
        await runner.close()
    assert isinstance(results[0], asyncio.CancelledError) if cancelled else results[0] is None
    assert results[1] is None and runner._runs == {}
    assert len(registered) == 1 and registered[0].done() and registered[0].cancelled()
    assert stack.texts == [] and _rows(runner.store, "SELECT * FROM commands") == []


async def test_cancelled_close_still_marks_closing_after_waiting_with_no_runs(stack: Stack) -> None:
    gate = _ClaimGate()
    runner, routine_id = _made(stack, admission_lock=gate)
    closing: asyncio.Task[None] | None = None
    try:
        async with gate:
            closing = asyncio.create_task(runner.close())
            cycled = asyncio.Event()
            asyncio.get_running_loop().call_soon(cycled.set)
            await cycled.wait()
            assert not closing.done() and not runner._closing
            for _ in range(2):
                closing.cancel()
                cycled = asyncio.Event()
                asyncio.get_running_loop().call_soon(cycled.set)
                await cycled.wait()
                assert not closing.done() and not runner._closing
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(closing, HANG_GUARD_S)
        assert runner._closing and runner._runs == {} and not gate.locked()
        with pytest.raises(RoutineError, match="^unavailable$"):
            await runner.run_now(routine_id, _ident(), 1)
        assert _rows(runner.store, "SELECT * FROM task_attempts") == []
    finally:
        if closing is not None:
            await asyncio.gather(closing, return_exceptions=True)
        await runner.close()


async def test_close_drains_pending_callback_once_even_if_callers_cancel(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, routine_id = _made(stack)
    stack.script = (REPLY,)
    sent, closing, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    close_count = 0
    send, close = TurnDriver.send, TurnDriver.close

    async def held_send(driver: TurnDriver, text: str) -> None:
        await send(driver, text)
        sent.set()
        await asyncio.Event().wait()

    async def held_close(driver: TurnDriver) -> None:
        nonlocal close_count
        close_count += 1
        closing.set()
        await release.wait()
        await close(driver)

    monkeypatch.setattr(TurnDriver, "send", held_send)
    monkeypatch.setattr(TurnDriver, "close", held_close)
    attempt, _ = await runner.run_now(routine_id, _ident(), 1)
    await _guarded_wait(asyncio.create_task(sent.wait()), runner, attempt)
    first = asyncio.create_task(runner.close())
    try:
        await asyncio.wait_for(closing.wait(), HANG_GUARD_S)
        second = asyncio.create_task(runner.close())
        first.cancel()
        first.cancel()
        cycled = asyncio.Event()
        asyncio.get_running_loop().call_soon(cycled.set)
        await cycled.wait()
        assert next(iter(runner._runs)).cancelling() == 1
        assert not first.done() and not second.done()
        assert _attempt(runner.store, attempt.attempt_id)["state"] == "dispatching"
    finally:
        release.set()
    results = await asyncio.wait_for(
        asyncio.gather(first, second, return_exceptions=True), HANG_GUARD_S
    )
    assert isinstance(results[0], asyncio.CancelledError) and results[1] is None
    assert close_count == 1 and runner._runs == {}
    row = _attempt(runner.store, attempt.attempt_id)
    assert (row["state"], row["dispatched_at"]) == ("dispatching", None)
    assert runner.drafts.get(attempt.session_id).text != ""
    assert stack.manager.get(attempt.session_id) is not None
    assert len(stack.texts) == 1
    assert TaskStore(runner.store.store, runner.store.scope, NEW).reconcile(LATER) == 1
    assert _results(runner.store)[attempt.attempt_id] == ("done", "unknown", "restarted", LATER)


async def test_close_joins_composed_draft_worker_before_leaving_claimed(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, routine_id = _made(stack)
    entered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    save = runner.drafts.save

    def held_save(key: str, revision: int, text: str | TurnInput) -> Draft:
        result = save(key, revision, text)
        if isinstance(text, TurnInput):
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(HANG_GUARD_S)
        return result

    monkeypatch.setattr(runner.drafts, "save", held_save)
    attempt, _ = await runner.run_now(routine_id, _ident(), 1)
    await asyncio.wait_for(entered.wait(), HANG_GUARD_S)
    closing = asyncio.create_task(runner.close())
    cycled = asyncio.Event()
    loop.call_soon(cycled.set)
    await cycled.wait()
    try:
        assert next(iter(runner._runs)).cancelling() == 1
        assert not closing.done()
        assert _attempt(runner.store, attempt.attempt_id)["state"] == "claimed"
    finally:
        release.set()
        await asyncio.wait_for(closing, HANG_GUARD_S)
    assert _attempt(runner.store, attempt.attempt_id)["state"] == "claimed"
    assert _rows(runner.store, "SELECT * FROM commands") == []
    assert TaskStore(runner.store.store, runner.store.scope, NEW).reconcile(LATER) == 1
    assert _results(runner.store)[attempt.attempt_id] == ("done", "cancelled", "restarted", LATER)


async def test_close_joins_running_mark_once_before_unknown_finish(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, routine_id = _made(stack)
    stack.script = (REPLY,)
    entered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    mark = runner.store.mark_running
    calls = 0

    def held_mark(attempt_id: str, when: str) -> bool:
        nonlocal calls
        calls += 1
        result = mark(attempt_id, when)
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(HANG_GUARD_S)
        return result

    monkeypatch.setattr(runner.store, "mark_running", held_mark)
    attempt, _ = await runner.run_now(routine_id, _ident(), 1)
    await asyncio.wait_for(entered.wait(), HANG_GUARD_S)
    closing = asyncio.create_task(runner.close())
    cycled = asyncio.Event()
    loop.call_soon(cycled.set)
    await cycled.wait()
    try:
        assert next(iter(runner._runs)).cancelling() == 1
        assert not closing.done()
        assert _attempt(runner.store, attempt.attempt_id)["state"] == "running"
    finally:
        release.set()
        await asyncio.wait_for(closing, HANG_GUARD_S)
    row = _attempt(runner.store, attempt.attempt_id)
    assert calls == 1
    assert (row["outcome"], row["reason"], row["dispatched_at"]) == (
        "unknown",
        "native_unconfirmed",
        stamp(NOW),
    )
    assert stack.manager.get(attempt.session_id) is not None


async def test_close_joins_terminal_worker_without_repeating_finish(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, routine_id = _made(stack)
    monkeypatch.setattr(
        runner.routines, "readiness", lambda _document, *, scheduled: ["profile_missing"]
    )
    entered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    finish = runner.store.finish
    calls = 0

    def held_finish(*args: Any, **kwargs: Any) -> bool:
        nonlocal calls
        calls += 1
        result = finish(*args, **kwargs)
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(HANG_GUARD_S)
        return result

    monkeypatch.setattr(runner.store, "finish", held_finish)
    attempt, _ = await runner.run_now(routine_id, _ident(), 1)
    await asyncio.wait_for(entered.wait(), HANG_GUARD_S)
    closing = asyncio.create_task(runner.close())
    cycled = asyncio.Event()
    loop.call_soon(cycled.set)
    await cycled.wait()
    try:
        assert next(iter(runner._runs)).cancelling() == 1
        assert not closing.done()
        assert _attempt(runner.store, attempt.attempt_id)["state"] == "done"
    finally:
        release.set()
        await asyncio.wait_for(closing, HANG_GUARD_S)
    row = _attempt(runner.store, attempt.attempt_id)
    assert calls == 1 and (row["outcome"], row["reason"]) == ("blocked", "profile_missing")
    assert _rows(runner.store, "SELECT * FROM commands") == []


@pytest.mark.parametrize(
    ("script", "expected", "answers"),
    [
        pytest.param((CARD,), ("expired", "waiting_for_user"), [DENY], id="card"),
        pytest.param((DONE,), ("succeeded", ""), [], id="known_success"),
    ],
)
async def test_close_during_deadline_stop_retains_frozen_result(
    stack: Stack,
    monkeypatch: pytest.MonkeyPatch,
    script: tuple[Event, ...],
    expected: tuple[str, str],
    answers: list[object],
) -> None:
    runner, routine_id = _made(stack)
    stack.script = script
    sent, send_release = asyncio.Event(), asyncio.Event()
    closing, close_release = asyncio.Event(), asyncio.Event()
    send, close = TurnDriver.send, TurnDriver.close
    manager_close = stack.manager.close

    async def held_send(driver: TurnDriver, text: str) -> None:
        await send(driver, text)
        sent.set()
        await send_release.wait()

    async def held_close(driver: TurnDriver) -> None:
        closing.set()
        await close_release.wait()
        await close(driver)

    async def ordered_close(session_id: str) -> None:
        assert _attempt(runner.store, attempt.attempt_id)["state"] == "dispatching"
        await manager_close(session_id)

    monkeypatch.setattr(TurnDriver, "send", held_send)
    monkeypatch.setattr(TurnDriver, "close", held_close)
    monkeypatch.setattr(stack.manager, "close", ordered_close)
    attempt, _ = await runner.run_now(routine_id, _ident(), 1)
    await asyncio.wait_for(sent.wait(), HANG_GUARD_S)
    stack.pass_deadline()
    close_task: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(closing.wait(), HANG_GUARD_S)
        assert stack.answers == answers
        close_task = asyncio.create_task(runner.close())
        cycled = asyncio.Event()
        asyncio.get_running_loop().call_soon(cycled.set)
        await cycled.wait()
        assert not close_task.done()
        assert _attempt(runner.store, attempt.attempt_id)["state"] == "dispatching"
    finally:
        send_release.set()
        close_release.set()
        if close_task is not None:
            await asyncio.wait_for(close_task, HANG_GUARD_S)
    row = _attempt(runner.store, attempt.attempt_id)
    assert (row["outcome"], row["reason"]) == expected
    assert row["dispatched_at"] == stamp(NOW)
    assert (runner.routines.get(routine_id)["tested_revision"] == 1) == (expected[0] == "succeeded")
    assert runner.drafts.get(attempt.session_id).text == ""
    assert stack.manager.get(attempt.session_id) is None


async def test_close_before_command_submit_leaves_dispatching_for_reconcile(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, routine_id = _made(stack)
    entered = asyncio.Event()

    async def held_submit(*_args: Any, **_kwargs: Any) -> Any:
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(runner.commands, "submit", held_submit)
    attempt, _ = await runner.run_now(routine_id, _ident(), 1)
    await asyncio.wait_for(entered.wait(), HANG_GUARD_S)
    await asyncio.wait_for(runner.close(), HANG_GUARD_S)
    row = _attempt(runner.store, attempt.attempt_id)
    assert (row["state"], row["dispatched_at"]) == ("dispatching", None)
    assert _rows(runner.store, "SELECT * FROM commands") == []
    assert runner.drafts.get(attempt.session_id).text != ""
    assert TaskStore(runner.store.store, runner.store.scope, NEW).reconcile(LATER) == 1
    assert _results(runner.store)[attempt.attempt_id] == ("done", "unknown", "restarted", LATER)


async def test_close_failure_preserves_known_result_and_fixed_warning(
    stack: Stack, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    runner, routine_id = _made(stack)
    stack.script = (DONE,)
    sent = asyncio.Event()
    send = TurnDriver.send
    manager_close = stack.manager.close

    async def held_send(driver: TurnDriver, text: str) -> None:
        await send(driver, text)
        sent.set()
        await asyncio.Event().wait()

    async def failed_close(_session_id: str) -> None:
        raise RuntimeError("private detail")

    monkeypatch.setattr(TurnDriver, "send", held_send)
    monkeypatch.setattr(stack.manager, "close", failed_close)
    attempt, _ = await runner.run_now(routine_id, _ident(), 1)
    try:
        await asyncio.wait_for(sent.wait(), HANG_GUARD_S)
        await asyncio.wait_for(runner.close(), HANG_GUARD_S)
    finally:
        monkeypatch.setattr(stack.manager, "close", manager_close)
    row = _attempt(runner.store, attempt.attempt_id)
    assert (row["outcome"], row["reason"]) == ("succeeded", "")
    assert runner.routines.get(routine_id)["tested_revision"] == 1
    assert stack.manager.get(attempt.session_id) is not None
    assert [
        (record.getMessage(), record.exc_info)
        for record in caplog.records
        if record.name == "viki_agent.automation.tasks"
    ] == [("routine session close failed", None)]


def _hold_final_draft_clear(
    runner: TaskRunner, monkeypatch: pytest.MonkeyPatch
) -> tuple[asyncio.Event, threading.Event]:
    """Pause after command settlement, before the task reads its final result."""
    save = runner.drafts.save
    loop = asyncio.get_running_loop()
    entered, release = asyncio.Event(), threading.Event()
    held = False

    def held_save(session_id: str, revision: int, text: str | TurnInput) -> Draft:
        nonlocal held
        if text == "" and not held:
            held = True
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(HANG_GUARD_S)
        return save(session_id, revision, text)

    monkeypatch.setattr(runner.drafts, "save", held_save)
    return entered, release


def _hold_running_mark(
    runner: TaskRunner, monkeypatch: pytest.MonkeyPatch
) -> tuple[asyncio.Event, threading.Event]:
    """Pause after the command callback so deadline or shutdown wins before settlement."""
    mark = runner.store.mark_running
    loop = asyncio.get_running_loop()
    entered, release = asyncio.Event(), threading.Event()

    def held_mark(attempt_id: str, now: str) -> bool:
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(HANG_GUARD_S)
        return mark(attempt_id, now)

    monkeypatch.setattr(runner.store, "mark_running", held_mark)
    return entered, release


async def _reset_finished_command(
    stack: Stack, runner: TaskRunner, attempt: Attempt, *, state: str = "unknown"
) -> None:
    rows = _rows(
        runner.store, "SELECT state FROM commands WHERE command_id=?", (attempt.attempt_id,)
    )
    assert rows == [{"state": state}]
    reset = ResetService(
        store=stack.rig.state.store,
        active_workspace=stack.rig.workspaces.resolve_active,
        settings_reset=lambda _command_id: None,
        scratch_root=stack.rig.data / "scratch",
        scratch_in_use=lambda: True,
        owner=runner.commands.service._owner,
    )
    result = await asyncio.to_thread(reset.reset, command_id=_ident(), categories=["commands"])
    assert result["results"] == {"commands": {"status": "done", "count": 1}}
    assert (
        _rows(runner.store, "SELECT * FROM commands WHERE command_id=?", (attempt.attempt_id,))
        == []
    )


@pytest.mark.parametrize("path", ["ordinary", "deadline", "shutdown"])
async def test_observed_success_survives_command_reset_before_settlement(
    stack: Stack, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    runner, routine_id = _made(stack)
    stack.script = (DONE,)
    mark_entered: asyncio.Event | None = None
    mark_release: threading.Event | None = None
    if path != "ordinary":
        mark_entered, mark_release = _hold_running_mark(runner, monkeypatch)
    clear_entered, clear_release = _hold_final_draft_clear(runner, monkeypatch)
    attempt, _ = await runner.run_now(routine_id, _ident(), 1)
    closing: asyncio.Task[None] | None = None
    if mark_entered is not None and mark_release is not None:
        await asyncio.wait_for(mark_entered.wait(), HANG_GUARD_S)
        if path == "deadline":
            stack.pass_deadline()
        else:
            closing = asyncio.create_task(runner.close())
            await asyncio.sleep(0)
            assert runner._closing
        mark_release.set()
    try:
        await asyncio.wait_for(clear_entered.wait(), HANG_GUARD_S)
        [seen] = runner._runs.values()
        assert seen.ended.is_set() and seen.turn == "success"
        await _reset_finished_command(stack, runner, attempt)
    finally:
        clear_release.set()
        if mark_release is not None:
            mark_release.set()
        if closing is not None:
            await asyncio.wait_for(closing, HANG_GUARD_S)
        else:
            await _ended(runner, attempt)
    row = _attempt(runner.store, attempt.attempt_id)
    assert (row["outcome"], row["reason"]) == ("succeeded", "")
    assert runner.routines.get(routine_id)["tested_revision"] == 1
    assert len(stack.texts) == 1


async def test_observed_unsent_refusal_survives_command_reset(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, routine_id = _made(stack)

    async def leave() -> None:
        runner.attended = lambda: False

    stack.before_open = leave
    clear_entered, clear_release = _hold_final_draft_clear(runner, monkeypatch)
    attempt, _ = await runner.run_now(routine_id, _ident(), 1)
    try:
        await asyncio.wait_for(clear_entered.wait(), HANG_GUARD_S)
        [seen] = runner._runs.values()
        assert seen.finished == "not_dispatched" and seen.ended.is_set()
        await _reset_finished_command(stack, runner, attempt, state="not_dispatched")
    finally:
        clear_release.set()
        await _ended(runner, attempt)
    row = _attempt(runner.store, attempt.attempt_id)
    assert (row["outcome"], row["reason"]) == ("cancelled", "app_closed")
    assert runner.routines.get(routine_id)["tested_revision"] is None
    assert stack.texts == []


async def test_unobserved_command_reset_remains_unknown(
    stack: Stack,
) -> None:
    runner, routine_id = _made(stack)
    stack.script = (REPLY,)
    attempt = await _sent(stack, runner, routine_id)
    [seen] = runner._runs.values()
    assert not seen.ended.is_set() and seen.turn is None and seen.finished == "unknown"
    await _reset_finished_command(stack, runner, attempt)
    await asyncio.wait_for(runner.close(), HANG_GUARD_S)
    row = _attempt(runner.store, attempt.attempt_id)
    assert (row["outcome"], row["reason"]) == ("unknown", "internal")
    assert runner.routines.get(routine_id)["tested_revision"] is None


@pytest.mark.parametrize("active", [False, True])
def test_occupancy_does_not_revisit_completed_history(
    tasks: TaskStore, service: RoutineService, active: bool
) -> None:
    retained, empty, own, other = (_routine(service) for _ in range(4))
    unmatched = None
    if active:
        _open(tasks, own, 0, "running")
        unmatched = _open(tasks, other, 1, "dispatching")
    with tasks.store._connection(write=True) as connection:
        if unmatched is not None:
            connection.execute(
                "UPDATE task_attempts SET origin_id=? WHERE attempt_id=?",
                (_ident(), unmatched.attempt_id),
            )
        expected = [(empty, "busy" if active else None), (retained, "busy" if active else None)]
        if active:
            expected.append((own, "run_active"))
        steps = 0

        def progressed() -> int:
            nonlocal steps
            steps += 1
            return 0

        work = []
        for history in (0, MAX_OCCURRENCES):
            for minute in range(history):
                tasks.insert_claim(
                    connection,
                    routine_id=retained,
                    kind="schedule",
                    revision=1,
                    now=_at(minute),
                    scheduled_for=_at(minute),
                )
            connection.execute(
                "UPDATE task_attempts SET state='done',outcome='succeeded',finished_at=? "
                "WHERE origin_id IN "
                "(SELECT occurrence_id FROM routine_occurrences WHERE routine_id=?)",
                (T0, retained),
            )
            # Count only admission work: both targets share the unchanged active set.
            measured = []
            for routine_id, result in expected:
                steps = 0
                connection.set_progress_handler(progressed, 1)
                try:
                    assert tasks.occupancy(connection, routine_id) == result
                finally:
                    connection.set_progress_handler(None, 0)
                measured.append(steps)
            assert measured[0] > 0
            assert measured[1] <= measured[0] + 1  # at most one instruction of counter alignment
            work.append(measured)
        # Completed history adds no work for either target, including an unmatched active origin.
        assert all(after <= before + 1 for before, after in zip(*work, strict=True)), work


@pytest.mark.parametrize("state", ["claimed", "dispatching", "running"])
def test_occupancy_counts_unmatched_active_origins_and_prioritizes_own_run(
    tasks: TaskStore, service: RoutineService, state: str
) -> None:
    own, other, empty = (_routine(service) for _ in range(3))
    owned = _open(tasks, own, 0, state)
    unmatched = _open(tasks, other, 1, state)
    with tasks.store._connection(write=True) as connection:
        connection.execute(
            "UPDATE task_attempts SET origin_id=? WHERE attempt_id=?",
            (_ident(), unmatched.attempt_id),
        )
        assert tasks.occupancy(connection, own) == "run_active"
        assert tasks.occupancy(connection, other) == "busy"
        assert tasks.occupancy(connection, empty) == "busy"
    assert _code(lambda: tasks.claim_manual(own, _ident(), 1, T0)) == "run_active"
    assert _code(lambda: tasks.claim_manual(empty, _ident(), 1, T0)) == "busy"
    assert tasks.finish(owned.attempt_id, "succeeded", "", T0)
    assert tasks.finish(unmatched.attempt_id, "succeeded", "", T0)
    with tasks.store._connection() as connection:
        assert all(tasks.occupancy(connection, rid) is None for rid in (own, other, empty))
