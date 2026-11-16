"""Optional Bot hooks preserve routine outcomes and retire native actors before delivery."""

from __future__ import annotations

import asyncio
import weakref
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from tests.automation.test_tasks import HANG_GUARD_S, NOW, Stack, _attempt, _made
from tests.automation.test_tasks import stack as stack_fixture
from tests.messaging.test_bots import _attempt as bot_attempt
from tests.messaging.test_bots import _bot, _id, _routine
from tests.messaging.test_bots import service as service_fixture
from tests.messaging.test_mattermost_bot import Rig, take
from viki_agent.automation import tasks
from viki_agent.automation.routines import RoutineError, stamp
from viki_agent.automation.tasks import Attempt, Outcome, TaskRunner, TaskStore
from viki_agent.decision_loop.store import DecisionLoopStore
from viki_agent.events import TextDone, TurnDone, Usage
from viki_agent.messaging.bot_runtime import BotRuntime
from viki_agent.messaging.bots import BotService
from viki_agent.session import SessionActor
from viki_agent.storage import StateError

stack = stack_fixture
service = service_fixture


class Hooks:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.failure = ""
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()
        self.runner: TaskRunner | None = None
        self.observers: list[weakref.ReferenceType[Probe]] = []

    def record(self, phase: str) -> None:
        self.events.append(phase)
        if self.failure == phase:
            raise ValueError("Private synthetic callback content")

    async def begin(self, attempt: Attempt, document: dict[str, Any]) -> None:
        assert document["profile_id"] and attempt.revision > 0
        self.record("begin")

    def observe(
        self, attempt: Attempt, actor: SessionActor, preview: dict[str, Any]
    ) -> Callable[[dict[str, Any]], None]:
        assert actor.session_id == attempt.session_id == preview["session_id"]
        assert actor.generation == preview["generation"]
        self.record("observe")
        observer = Probe(self, actor, preview)
        self.observers.append(weakref.ref(observer))
        return observer

    async def complete(self, attempt: Attempt, outcome: Outcome, reason: str) -> None:
        assert self.runner is not None
        row = _attempt(self.runner.store, attempt.attempt_id)
        assert row["state"] == "done" and row["outcome"] == outcome and row["reason"] == reason
        assert self.runner.manager.get(attempt.session_id) is None
        assert all(observer() is None for observer in self.observers)
        self.record("complete")
        self.entered.set()
        await self.release.wait()

    def discard(self, _attempt: Attempt) -> None:
        self.record("discard")


class Probe:
    def __init__(self, hooks: Hooks, actor: SessionActor, preview: dict[str, Any]) -> None:
        self.hooks, self.actor, self.preview = hooks, actor, preview

    def __call__(self, _event: dict[str, Any]) -> None:
        self.hooks.record("event")


def hooked(stack: Stack, hooks: Hooks, **overrides: Any) -> tuple[TaskRunner, str]:
    original, routine_id = _made(stack)
    callbacks = {
        "begin_attempt": hooks.begin,
        "observe_attempt": hooks.observe,
        "attempt_finished": hooks.complete,
        "discard_attempt": hooks.discard,
        **overrides,
    }
    runner = TaskRunner(
        original.store,
        original.routines,
        original.commands,
        original.manager,
        original.drafts,
        original.profiles,
        prepare=original.prepare,
        clock=original.clock,
        attended=original.attended,
        **callbacks,
    )
    hooks.runner = runner
    return runner, routine_id


async def drain(runner: TaskRunner) -> None:
    async with asyncio.timeout(HANG_GUARD_S):
        await asyncio.gather(*runner._runs)


async def test_hooks_observe_live_events_then_complete_after_commit_and_retirement(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    hooks = Hooks()
    runner, routine = hooked(stack, hooks)
    readiness = runner.routines.readiness

    def ready(*args: Any, **kwargs: Any) -> list[str]:
        assert hooks.events == ["begin"]
        return readiness(*args, **kwargs)

    monkeypatch.setattr(runner.routines, "readiness", ready)
    stack.script = (
        TextDone(block_id="final", text="Useful response"),
        TurnDone(status="success", usage=Usage()),
    )
    attempt, _ = await runner.run_now(routine, _id(), 1)
    await drain(runner)
    assert hooks.events[0:2] == ["begin", "observe"]
    assert hooks.events[-2:] == ["complete", "discard"]
    assert hooks.events.count("discard") == 1
    assert (
        "event" in hooks.events
        and _attempt(runner.store, attempt.attempt_id)["outcome"] == "succeeded"
    )
    assert stack.manager.live() == []


async def test_false_finish_receipt_cannot_complete_again(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    hooks = Hooks()
    runner, routine = hooked(stack, hooks)
    finish = runner.store.finish

    def false_receipt(*args: Any, **kwargs: Any) -> bool:
        finish(*args, **kwargs)
        return False

    monkeypatch.setattr(runner.store, "finish", false_receipt)
    attempt, _ = await runner.run_now(routine, _id(), 1)
    await drain(runner)
    assert _attempt(runner.store, attempt.attempt_id)["state"] == "done"
    assert "complete" not in hooks.events and hooks.events[-1] == "discard"
    assert stack.manager.get(attempt.session_id) is None


@pytest.mark.parametrize("phase", ["begin", "observe", "event", "complete", "discard"])
async def test_hook_exceptions_keep_success_and_log_only_fixed_messages(
    stack: Stack, caplog: pytest.LogCaptureFixture, phase: str
) -> None:
    hooks = Hooks()
    hooks.failure = phase
    runner, routine = hooked(stack, hooks)
    attempt, _ = await runner.run_now(routine, _id(), 1)
    await drain(runner)
    assert _attempt(runner.store, attempt.attempt_id)["outcome"] == "succeeded"
    assert hooks.events.count("discard") == 1
    records = [record for record in caplog.records if record.name == "viki_agent.automation.tasks"]
    assert len(records) == 1
    assert "Private" not in records[0].getMessage() and records[0].exc_info is None
    assert stack.manager.get(attempt.session_id) is None


@pytest.mark.parametrize("phase", ["frozen", "begin", "readiness"])
async def test_cancellation_before_actor_attachment_discards_without_completion(
    stack: Stack, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    hooks = Hooks()
    entered = asyncio.Event()

    async def hold_begin(attempt: Attempt, document: dict[str, Any]) -> None:
        await hooks.begin(attempt, document)
        entered.set()
        await asyncio.Event().wait()

    runner, routine = hooked(
        stack, hooks, **({"begin_attempt": hold_begin} if phase == "begin" else {})
    )
    to_thread = asyncio.to_thread

    async def hold_thread(function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        if (phase == "frozen" and function == runner.routines.frozen) or (
            phase == "readiness" and function == runner.routines.readiness
        ):
            entered.set()
            await asyncio.Event().wait()
        return await to_thread(function, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", hold_thread)
    attempt, _ = await runner.run_now(routine, _id(), 1)
    async with asyncio.timeout(HANG_GUARD_S):
        await entered.wait()
        await runner.close()
    assert hooks.events.count("begin") == (phase != "frozen")
    assert hooks.events.count("discard") == 1 and "complete" not in hooks.events
    assert _attempt(runner.store, attempt.attempt_id)["state"] == "claimed"
    assert stack.manager.get(attempt.session_id) is None


async def test_failed_frozen_read_still_discards_without_inventing_owner(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    hooks = Hooks()
    runner, routine = hooked(stack, hooks)

    def corrupt(_routine: str, _revision: int) -> dict[str, Any]:
        raise RoutineError("corrupt")

    monkeypatch.setattr(runner.routines, "frozen", corrupt)
    attempt, _ = await runner.run_now(routine, _id(), 1)
    await drain(runner)
    assert "begin" not in hooks.events and "observe" not in hooks.events
    assert hooks.events.count("discard") == 1
    assert _attempt(runner.store, attempt.attempt_id)["outcome"] == "failed"


@pytest.mark.parametrize("failed_after_retirement", [False, True])
async def test_native_cleanup_failure_skips_completion_but_keeps_known_outcome(
    stack: Stack,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failed_after_retirement: bool,
) -> None:
    hooks = Hooks()
    runner, routine = hooked(stack, hooks)

    close = stack.manager.close

    async def failed_close(_session_id: str) -> None:
        if failed_after_retirement:
            await close(_session_id)
        raise ValueError("Private cleanup content")

    monkeypatch.setattr(stack.manager, "close", failed_close)
    attempt, _ = await runner.run_now(routine, _id(), 1)
    await drain(runner)
    assert "complete" not in hooks.events and hooks.events.count("discard") == 1
    assert _attempt(runner.store, attempt.attempt_id)["outcome"] == "succeeded"
    records = [record for record in caplog.records if record.name == "viki_agent.automation.tasks"]
    assert [(record.getMessage(), record.exc_info) for record in records] == [
        ("routine session close failed", None)
    ]


async def test_cancelled_native_cleanup_still_discards_and_preserves_committed_success(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    hooks = Hooks()
    runner, routine = hooked(stack, hooks)
    entered = asyncio.Event()

    async def held_close(_session_id: str) -> None:
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(stack.manager, "close", held_close)
    attempt, _ = await runner.run_now(routine, _id(), 1)
    async with asyncio.timeout(HANG_GUARD_S):
        await entered.wait()
    [run] = runner._runs
    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run
    assert hooks.events.count("discard") == 1 and "complete" not in hooks.events
    assert _attempt(runner.store, attempt.attempt_id)["outcome"] == "succeeded"


async def test_reconcile_never_calls_bot_callbacks(stack: Stack) -> None:
    hooks = Hooks()
    runner, _ = hooked(stack, hooks)
    assert await runner.reconcile() == 0
    assert hooks.events == []


async def test_actual_blocked_writer_keeps_terminal_waits_without_occupying_native_slots(
    stack: Stack, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original, routine = _made(stack)
    service = BotService(
        original.store.store,
        original.profiles,
        original.routines,
        stack.rig.workspaces,
        now=stack.clock.wall,
    )
    bot, _ = service.create(_id(), stack.rig.profile_id, 1, stack.rig.workspace_id)
    bridge = Rig(tmp_path / "bridge")
    await bridge.start(monkeypatch)
    runtime = BotRuntime(
        service,
        DecisionLoopStore(service.store),
        bridge=lambda: bridge.bot,
        bridge_workspace=lambda: stack.rig.workspace_id,
        attended=lambda: True,
        now=stack.clock.wall,
    )
    await runtime.set_delivery(bot["bot_id"], 1, True)
    hooks = Hooks()
    runner, unused = hooked(
        stack,
        hooks,
        begin_attempt=runtime.begin_attempt,
        observe_attempt=runtime.observe_attempt,
        attempt_finished=runtime.attempt_finished,
        discard_attempt=runtime.discard_attempt,
    )
    assert unused != routine
    stack.script = (
        TextDone(block_id="final", text="Useful scheduled output"),
        TurnDone(status="success", usage=Usage()),
    )
    admitted: asyncio.Queue[None] = asyncio.Queue()
    enqueue = bridge.bot._enqueue

    def notice_queued(*args: Any, **kwargs: Any) -> bool:
        result = enqueue(*args, **kwargs)
        if kwargs.get("notice") is not None:
            admitted.put_nowait(None)
        return result

    monkeypatch.setattr(bridge.bot, "_enqueue", notice_queued)
    bridge.http.gate.clear()
    try:
        for index in range(6):
            attempt, _ = await runner.run_now(routine, _id(), 1)
            await take(admitted)
            if index == 0:
                assert "Useful scheduled output" in (await take(bridge.http.sent))[0]
            assert _attempt(runner.store, attempt.attempt_id)["outcome"] == "succeeded"
            assert stack.manager.get(attempt.session_id) is None and stack.manager.live() == []
        assert len(runner._runs) == len(runtime._contexts) == 6
        assert bridge.bot._output_count == 6
    finally:
        runtime.close()
        bridge.bot.disarm()
        await runner.close()
        bridge.http.gate.set()
        await bridge.bot.close()
        if bridge.run is not None:
            await bridge.run
        await bridge.approvals.close()
        await bridge.manager.close_all()
    assert not runner._runs and not runtime._contexts and stack.manager.live() == []


def receipts(service: BotService) -> set[str]:
    with service.store._connection() as connection:
        return {row[0] for row in connection.execute("SELECT attempt_id FROM bot_deliveries")}


def prune_setup(service: BotService) -> tuple[TaskStore, str, set[str], set[str]]:
    bot, other = _bot(service), _bot(service)
    routine, other_routine = _routine(service, bot), _routine(service, other)
    done = [bot_attempt(service, routine) for _ in range(3)]
    elsewhere = bot_attempt(service, other_routine)
    for index, attempt in enumerate([*done, elsewhere]):
        service.record_delivery(
            attempt.attempt_id,
            bot["bot_id"] if index < 3 else other["bot_id"],
            attention="notify",
            reason="runs_to_review",
            state="sent",
            delivery_reason="none",
        )
        with service.store._connection(write=True) as connection:
            connection.execute(
                "UPDATE routine_occurrences SET recorded_at=? WHERE occurrence_id=?",
                (stamp(NOW + timedelta(minutes=index)), attempt.occurrence_id),
            )
    return (
        TaskStore(service.store, "a" * 64, _id()),
        routine,
        {done[0].attempt_id},
        {done[1].attempt_id, done[2].attempt_id, elsewhere.attempt_id},
    )


def test_prune_deletes_only_exact_victim_receipts_before_parent_attempts(
    service: BotService, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, routine, removed, kept = prune_setup(service)
    monkeypatch.setattr(tasks, "MAX_OCCURRENCES", 2)
    trace: list[str] = []
    with service.store._connection(write=True) as connection:
        connection.set_trace_callback(trace.append)
        store.prune(connection, routine)
    assert receipts(service) == kept and not receipts(service) & removed
    children = next(
        index
        for index, statement in enumerate(trace)
        if statement.startswith("DELETE FROM bot_deliveries")
    )
    parents = next(
        index
        for index, statement in enumerate(trace)
        if statement.startswith("DELETE FROM task_attempts")
    )
    assert children < parents


def test_prune_receipts_and_parents_rollback_in_same_transaction(
    service: BotService, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, routine, removed, kept = prune_setup(service)
    monkeypatch.setattr(tasks, "MAX_OCCURRENCES", 2)
    with pytest.raises(StateError), service.store._connection(write=True) as connection:
        connection.execute(
            "CREATE TEMP TRIGGER fail_parent BEFORE DELETE ON task_attempts "
            "BEGIN SELECT RAISE(ABORT,'synthetic refusal'); END"
        )
        store.prune(connection, routine)
    assert receipts(service) == removed | kept
