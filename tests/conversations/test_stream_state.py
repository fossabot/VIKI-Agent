"""Volatile recovery preserves authority and terminal state independently of replay eviction."""

from __future__ import annotations

import asyncio
import copy
import uuid
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import CLIConnectionError

from tests.conversations.test_session import Drivers, GatedDriver, gated, watch
from viki_agent.conversations import stream_state
from viki_agent.conversations.stream_state import StreamState, encoded_size
from viki_agent.drivers.base import Answer, PermissionAnswer
from viki_agent.events import (
    Error,
    PermissionRequested,
    RequestAnswered,
    RequestCancelled,
    SessionClosed,
    SessionStarted,
    Status,
    SteerEnded,
    SteerPending,
    SubagentDone,
    SubagentStarted,
    TextDelta,
    TextDone,
    TurnDone,
    TurnStarted,
    Usage,
    UserMessage,
)
from viki_agent.session import CommandDispatchRejected

SID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
OTHER = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


def _append(state: StreamState, event: Any, seq: int, **extra: Any) -> None:
    state.append({**event.envelope(SID, seq, OTHER), **extra})


def _permission(plan: str = "") -> PermissionRequested:
    return PermissionRequested(
        tool_use_id="tool-1", kind="plan", tool="ExitPlanMode", summary="", plan=plan
    )


def test_replay_count_eviction_has_explicit_floor_and_separate_terminal_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(stream_state, "MAX_REPLAY_EVENTS", 2)
    state = StreamState()
    _append(state, UserMessage(text="synthetic"), 1)
    _append(state, TurnStarted(turn_id=OTHER), 2)
    _append(state, TextDelta(block_id="text", text="first"), 3)
    _append(state, TextDone(block_id="text", text="final"), 4)
    _append(state, TurnDone(status="error", usage=Usage(), context_percent=61), 5)
    _append(state, SessionClosed(reason="requested"), 6)
    assert state.replay_floor == 4
    assert [event["seq"] for event in state.events_after(0)] == [5, 6]
    snapshot = state.snapshot()
    assert snapshot["state"]["closed"] is True
    assert snapshot["state"]["context_percent"] == 61
    assert [item["type"] for item in snapshot["events"]] == [
        "user.message",
        "turn.started",
        "text.done",
        "turn.done",
    ]
    assert snapshot["events"][2]["data"]["text"] == "final"
    assert snapshot["events"][-1]["data"]["status"] == "error"


def test_wire_byte_budget_counts_unicode_escapes_and_oversized_single_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(stream_state, "MAX_REPLAY_BYTES", 512)
    state = StreamState()
    _append(state, UserMessage(text="界" * 100), 1)
    assert state.replay_bytes == 0
    assert state.replay_floor == 1
    assert state.snapshot()["events"][0]["data"]["text"] == "界" * 100


def test_compacted_deltas_do_not_invalidate_a_replay_cursor() -> None:
    state = StreamState()
    _append(state, UserMessage(text="synthetic"), 1)
    _append(state, TextDelta(block_id="b", text="a"), 2)
    _append(state, TextDelta(block_id="b", text="b"), 3)
    _append(state, TextDone(block_id="b", text="done"), 4)
    assert state.replay_floor == 0
    assert [event["seq"] for event in state.events_after(2)] == [4]


def test_only_snapshot_subagent_terminals_carry_the_original_envelope_sequence() -> None:
    state = StreamState()
    _append(state, UserMessage(text="synthetic"), 10)
    _append(
        state,
        SubagentStarted(block_id="run", agent="researcher", model="inherit", description="Find"),
        20,
    )
    envelope = SubagentDone(block_id="run", status="completed", summary="Found").envelope(
        SID, 30, OTHER
    )
    before = copy.deepcopy(envelope)
    state.append(envelope)
    snapshot = state.snapshot()
    terminal = snapshot["events"][-1]
    assert terminal == {"type": "subagent.done", "data": envelope["data"], "source_seq": 30}
    assert all(set(item) == {"type", "data"} for item in snapshot["events"][:-1])
    assert envelope == before and state.events_after(20) == [before]
    assert "source_seq" not in envelope and "source_seq" not in envelope["data"]
    assert state.snapshot() == snapshot
    terminal["source_seq"] = -1
    terminal["data"]["summary"] = "changed"
    assert state.snapshot()["events"][-1] == {
        "type": "subagent.done",
        "data": before["data"],
        "source_seq": 30,
    }


@pytest.mark.parametrize("seq", [None, True, False, "30", 30.5])
def test_snapshot_terminal_sequence_is_not_invented_or_coerced(seq: Any) -> None:
    envelope = {"type": "subagent.done", "data": {"block_id": "run", "status": "completed"}}
    assert StreamState._item(envelope) == envelope
    envelope["seq"] = seq
    assert StreamState._item(envelope) == {"type": "subagent.done", "data": envelope["data"]}


def test_snapshot_terminal_identity_survives_eviction_and_distinguishes_resumed_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(stream_state, "MAX_CONTENT_RECORDS", 4)
    monkeypatch.setattr(stream_state, "MAX_REPLAY_EVENTS", 2)
    state = StreamState()
    started = SubagentStarted(
        block_id="run", agent="researcher", model="inherit", description="Find"
    )
    completed = SubagentDone(block_id="run", status="completed", summary="Found")
    _append(state, UserMessage(text="synthetic"), 10)
    _append(state, TextDone(block_id="old", text="old"), 20)
    _append(state, started, 30)
    _append(state, completed, 40)
    original = state.snapshot()["events"][-1]
    assert original["source_seq"] == 40
    _append(state, TextDone(block_id="new", text="new"), 50)
    after_eviction = state.snapshot()
    assert after_eviction["truncated"] is True
    assert after_eviction["events"][2] == original  # its replay index changed, its identity did not
    assert state.snapshot() == after_eviction

    _append(state, started, 60)
    _append(state, completed, 70)
    resumed = state.snapshot()
    terminals = [item for item in resumed["events"] if item["type"] == "subagent.done"]
    assert terminals == [{**original, "source_seq": 70}]
    assert (OTHER, terminals[0]["source_seq"]) != (OTHER, original["source_seq"])
    assert [event["seq"] for event in state.events_after(0)] == [60, 70]
    assert state.replay_floor == 50
    assert state.snapshot() == resumed
    assert state.content_records == len(resumed["events"]) == 4
    assert state.content_bytes == sum(encoded_size(item) for item in resumed["events"])
    assert state.replay_bytes == sum(encoded_size(event) for event in state.events_after(0))
    assert encoded_size(resumed) < stream_state.MAX_SNAPSHOT_BYTES


def test_bounded_snapshot_terminal_accounts_for_sequence_metadata_without_changing_replay() -> None:
    state = StreamState()
    _append(state, UserMessage(text="synthetic"), 1)
    summary = "é" * (stream_state.MAX_TEXT_CHARS + 10)
    envelope = SubagentDone(block_id="run", status="completed", summary=summary).envelope(
        SID, 999, OTHER
    )
    state.append(envelope)
    snapshot = state.snapshot()
    terminal = snapshot["events"][-1]
    assert terminal["source_seq"] == 999
    assert terminal["data"]["summary"] == summary[: stream_state.MAX_TEXT_CHARS]
    assert terminal["original_bytes"] == encoded_size(
        {"type": "subagent.done", "data": envelope["data"], "source_seq": 999}
    )
    assert state.events_after(1) == [envelope]
    assert envelope["data"]["summary"] == summary and "source_seq" not in envelope
    assert state.content_bytes == sum(encoded_size(item) for item in snapshot["events"])
    assert state.content_bytes <= stream_state.MAX_CONTENT_BYTES
    assert state.content_records <= stream_state.MAX_CONTENT_RECORDS
    assert encoded_size(snapshot) < stream_state.MAX_SNAPSHOT_BYTES


def test_pending_requests_survive_content_eviction_and_resolved_ones_stay_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(stream_state, "MAX_CONTENT_RECORDS", 4)
    monkeypatch.setattr(stream_state, "MAX_REPLAY_EVENTS", 2)
    state = StreamState()
    binding = {"runtime_id": OTHER, "request_id": SID, "turn_id": OTHER}
    _append(state, UserMessage(text="synthetic"), 1)
    _append(state, _permission("full plan"), 2, native_request=binding)
    for seq in range(3, 20):
        _append(state, TextDone(block_id=str(seq), text="bounded"), seq)
    snapshot = state.snapshot()
    assert snapshot["truncated"] is True
    assert snapshot["events"][-1]["native_request"] == binding
    _append(state, RequestAnswered(tool_use_id="tool-1"), 20, native_request=binding)
    for seq in range(21, 30):
        _append(state, Status(state="requesting"), seq)
    assert not any("native_request" in item for item in state.snapshot()["events"])


def test_old_resolution_cannot_remove_reused_tool_id_with_a_new_binding() -> None:
    state = StreamState()
    old = {"runtime_id": OTHER, "request_id": SID, "turn_id": OTHER}
    new = {**old, "request_id": OTHER}
    _append(state, _permission(), 1, native_request=old)
    _append(state, _permission(), 2, native_request=new)
    _append(state, RequestCancelled(tool_use_id="tool-1"), 3, native_request=old)
    assert state.snapshot()["events"][0]["native_request"] == new


def test_snapshot_text_is_bounded_without_changing_live_final_content() -> None:
    state = StreamState()
    _append(state, UserMessage(text="synthetic"), 1)
    complete = "界" * (stream_state.MAX_TEXT_CHARS + 10)
    _append(state, TextDone(block_id="b", text=complete), 2)
    snapshot = state.snapshot()
    assert snapshot["truncated"] is True
    assert snapshot["events"][-1]["data"]["text"] == complete[: stream_state.MAX_TEXT_CHARS]
    assert snapshot["events"][-1]["original_chars"] == len(complete)
    assert state.events_after(1)[0]["data"]["text"] == complete


def test_snapshot_is_independent_and_retains_state_after_turn_eviction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(stream_state, "MAX_TURNS", 1)
    state = StreamState()
    _append(state, UserMessage(text="old"), 1)
    _append(state, TurnDone(status="success", usage=Usage(), context_percent=30), 2)
    _append(state, UserMessage(text="new"), 3)
    _append(state, TurnDone(status="error", usage=Usage()), 4)
    _append(state, Error(code="crashed", message="synthetic", recoverable=False), 5)
    _append(state, Status(mode="auto", state="compacting"), 6)
    snapshot = state.snapshot()
    assert snapshot["events"][0]["data"]["text"] == "new"
    assert snapshot["state"]["context_percent"] == 30
    assert snapshot["state"]["error"]["recoverable"] is False
    assert snapshot["state"]["mode"] == "auto"
    assert snapshot["state"]["compacting"] is True
    snapshot["events"][0]["data"]["text"] = "mutated"
    assert state.snapshot()["events"][0]["data"]["text"] == "new"


def test_repeated_terminal_events_cannot_exhaust_protected_projection_records(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(stream_state, "MAX_CONTENT_RECORDS", 3)
    state = StreamState()
    _append(state, UserMessage(text="synthetic"), 1)
    _append(state, TurnStarted(turn_id=OTHER), 2)
    for seq in range(3, 30):
        _append(state, TurnDone(status="error", usage=Usage(output_tokens=seq)), seq)
    assert state.content_records == 3
    assert state.snapshot()["events"][-1]["data"]["usage"]["output_tokens"] == 29


def test_long_stream_and_large_initialization_obey_separate_wire_budgets() -> None:
    state = StreamState()
    _append(
        state,
        SessionStarted(
            model="synthetic",
            mode="auto",
            effort="low",
            commands=[],
            models=[],
            agents=["界" * 20_000] * 100,
            cli_version="synthetic",
        ),
        1,
    )
    _append(state, UserMessage(text="synthetic"), 2)
    for seq in range(3, 6_003):
        _append(state, TextDelta(block_id="text", text="界" * 10), seq)
    assert len(state.replay) <= stream_state.MAX_REPLAY_EVENTS
    assert state.replay_bytes == sum(encoded_size(event) for event, _size in state.replay)
    assert state.replay_bytes <= stream_state.MAX_REPLAY_BYTES
    assert state.content_bytes <= stream_state.MAX_CONTENT_BYTES
    assert state.content_records <= stream_state.MAX_CONTENT_RECORDS
    assert encoded_size(state.state) <= stream_state.MAX_STATE_BYTES
    snapshot = state.snapshot()
    assert encoded_size(snapshot) < stream_state.MAX_SNAPSHOT_BYTES
    assert snapshot["events"][-1]["original_chars"] == 60_000


async def test_model_completion_cannot_write_preferences_for_replaced_runtime(
    tmp_path: Path,
) -> None:
    driver = GatedDriver()
    driver.release.set()
    actor = gated(tmp_path, driver).open(SID)
    await actor.start()
    waiting, release = asyncio.Event(), asyncio.Event()

    async def controlled_model(model: str) -> None:
        assert model == "changed"
        waiting.set()
        await release.wait()

    driver.set_model = controlled_model
    before = actor._state.prefs(SID)
    task = asyncio.create_task(actor.set_model("changed", expected_generation=actor.generation))
    await waiting.wait()
    actor._token = object()
    release.set()
    with pytest.raises(CommandDispatchRejected, match="stale_generation"):
        await task
    assert actor._state.prefs(SID) == before
    await actor.close()


async def test_queued_guarded_send_checks_membership_after_taking_actor_lock(
    tmp_path: Path,
) -> None:
    drivers = Drivers()
    actor = gated(tmp_path, drivers).open(SID)
    current = True
    await actor._lock.acquire()
    task = asyncio.create_task(
        actor.send("synthetic", expected_generation=actor.generation, current=lambda: current)
    )
    await asyncio.sleep(0)
    current = False
    actor._lock.release()
    with pytest.raises(CommandDispatchRejected, match="no_target"):
        await task
    assert drivers.made == []


async def test_send_boolean_reports_invocation_return_not_native_pickup(tmp_path: Path) -> None:
    drivers = Drivers()
    actor = gated(tmp_path, drivers).open(SID)
    assert await actor.send("synthetic", expected_generation=actor.generation) is True
    assert actor.busy is False
    driver = drivers.made[0][0]
    driver.send_error = CLIConnectionError("synthetic failure")
    assert await actor.send("failed", expected_generation=actor.generation) is None
    await actor.close()


async def test_reentrant_new_request_keeps_authority_after_old_response(tmp_path: Path) -> None:
    driver = _AnswerDriver()
    actor = gated(tmp_path, driver).open(SID)
    await actor.start()
    first = actor._emit(_permission())["native_request"]

    def answer_then_reask(event: Any) -> None:
        actor._emit(event)
        actor._emit(_permission("new native operation"))

    driver.emit = answer_then_reask
    assert actor.respond(
        "tool-1",
        PermissionAnswer(allow=False),
        expected_generation=actor.generation,
        native_request=first,
    )
    new = actor._native_requests["tool-1"]
    assert new != first
    driver.emit = None
    assert actor.respond(
        "tool-1",
        PermissionAnswer(allow=False),
        expected_generation=actor.generation,
        native_request=new,
    )
    await actor.close()


async def test_actor_recovery_checks_generation_even_when_replacement_seq_is_higher(
    tmp_path: Path,
) -> None:
    manager = gated(tmp_path, Drivers())
    old = manager.open(SID)
    old._emit(Status(mode="auto"))
    generation, cursor = old.generation, old.seq
    await manager.close(SID)
    actor = manager.open(SID)
    for _ in range(3):
        actor._emit(Status(mode="auto"))
    recovery = actor.recovery(generation, cursor)
    assert len(recovery) == 1 and recovery[0]["type"] == "session.snapshot"
    assert recovery[0]["generation"] == actor.generation
    assert actor.recovery(actor.generation, actor.seq) == []
    assert actor.recovery(actor.generation, actor.seq + 1)[0]["type"] == "session.snapshot"


async def test_recovery_then_subscription_has_no_event_gap(tmp_path: Path) -> None:
    actor = gated(tmp_path, Drivers()).open(SID)
    first = actor._emit(UserMessage(text="first"))
    recovered = actor.recovery(None, 0)
    received: list[dict[str, Any]] = []
    unsubscribe = actor.subscribe(received.append)
    second = actor._emit(TextDone(block_id="b", text="next"))
    assert recovered[0]["seq"] == first["seq"]
    assert [event["seq"] for event in received] == [second["seq"]]
    unsubscribe()


@pytest.mark.parametrize(
    "action", ["start", "send", "interrupt", "set_model", "set_effort", "close"]
)
async def test_all_guarded_mutations_refuse_stale_generation_before_native_effect(
    tmp_path: Path,
    action: str,
) -> None:
    drivers = Drivers()
    actor = gated(tmp_path, drivers).open(SID)
    args = (
        ["synthetic"]
        if action in {"send", "set_model"}
        else ["high"]
        if action == "set_effort"
        else []
    )
    with pytest.raises(CommandDispatchRejected, match="stale_generation"):
        result = getattr(actor, action)(*args, expected_generation=OTHER)
        if action != "set_effort":
            await result
    assert drivers.made == []
    assert actor.seq == 0


async def test_send_rechecks_manager_membership_after_start_await(tmp_path: Path) -> None:
    drivers = Drivers(gate_first=True)
    actor = gated(tmp_path, drivers).open(SID)
    current = True
    task = asyncio.create_task(
        actor.send("private", expected_generation=actor.generation, current=lambda: current)
    )
    await drivers.created.wait()
    current = False
    drivers.made[0][0].release.set()
    with pytest.raises(CommandDispatchRejected, match="no_target"):
        await task
    assert "send private" not in drivers.made[0][0].calls
    await actor.close()


async def test_guarded_close_does_not_forget_actor_when_generation_is_stale(tmp_path: Path) -> None:
    manager = gated(tmp_path, Drivers())
    actor = manager.open(SID)
    with pytest.raises(CommandDispatchRejected):
        await manager.close(SID, expected_generation=OTHER)
    assert manager.get(SID) is actor
    assert actor.command_context()["closed"] is False


class _AnswerDriver(GatedDriver):
    def __init__(self) -> None:
        super().__init__()
        self.release.set()
        self.answers: list[tuple[str, Answer]] = []
        self.emit: Any = None

    def respond(self, tool_use_id: str, answer: Answer) -> bool:
        self.answers.append((tool_use_id, answer))
        if self.emit is not None:
            self.emit(RequestAnswered(tool_use_id=tool_use_id))
        return True


async def test_request_binding_rotates_and_old_same_actor_allow_is_refused(tmp_path: Path) -> None:
    driver = _AnswerDriver()
    actor = gated(tmp_path, driver).open(SID)
    await actor.start()
    first = actor._emit(_permission())["native_request"]
    actor._emit(RequestCancelled(tool_use_id="tool-1"))
    second = actor._emit(_permission())["native_request"]
    assert first != second
    for value in second.values():
        assert str(uuid.UUID(value)) == value
    assert not actor.respond(
        "tool-1",
        PermissionAnswer(allow=True),
        expected_generation=actor.generation,
        native_request=first,
    )
    assert actor.respond(
        "tool-1",
        PermissionAnswer(allow=False),
        expected_generation=actor.generation,
        native_request=second,
    )
    assert not actor.respond(
        "tool-1",
        PermissionAnswer(allow=True),
        expected_generation=actor.generation,
        native_request=second,
    )
    assert driver.answers == [("tool-1", PermissionAnswer(allow=False))]
    ended = actor._emit(RequestAnswered(tool_use_id="tool-1"))
    assert ended["native_request"] == second
    assert actor._stream.cards == {}
    await actor.close()


async def test_runtime_replacement_invalidates_cards_without_waiting_for_native_cancel(
    tmp_path: Path,
) -> None:
    drivers = Drivers()
    actor = gated(tmp_path, drivers).open(SID)
    await actor.start()
    first = actor._emit(_permission())["native_request"]
    await actor._retire()
    assert actor._stream.cards == {}
    await actor.start()
    second = actor._emit(_permission())["native_request"]
    assert second["runtime_id"] != first["runtime_id"]
    assert not actor.respond(
        "tool-1",
        PermissionAnswer(allow=True),
        expected_generation=actor.generation,
        native_request=first,
    )
    await actor.close()


@pytest.mark.parametrize("reentrant", [False, True])
async def test_oversized_approval_is_denied_without_displaying_a_truncated_allow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reentrant: bool,
) -> None:
    monkeypatch.setattr(stream_state, "MAX_CARD_BYTES", 512)
    driver = _AnswerDriver()
    actor = gated(tmp_path, driver).open(SID)
    await actor.start()
    seen = watch(actor)
    if reentrant:
        driver.emit = actor._emit
    actor._emit(_permission("界" * 1000))
    assert driver.answers == [("tool-1", PermissionAnswer(allow=False))]
    assert "permission.requested" not in seen.types
    assert "request.cancelled" in seen.types
    assert seen[-1]["type"] == "error"
    assert actor._stream.cards == {}
    assert actor._native_requests == {}
    assert encoded_size(actor.recovery(None, 0)) < stream_state.MAX_SNAPSHOT_BYTES
    await actor.close()


def test_pending_request_is_a_deep_copy_and_ends_with_the_card() -> None:
    """The recorder captures the card the app displayed, and cannot alter what it captured."""
    state = StreamState()
    binding = {"runtime_id": OTHER, "request_id": OTHER, "turn_id": OTHER}
    assert state.pending_request("toolu_1") is None
    _append(
        state,
        PermissionRequested(tool_use_id="toolu_1", kind="tool", tool="Read", summary="read a.md"),
        1,
        native_request=binding,
    )
    item = state.pending_request("toolu_1")
    assert item is not None and set(item) == {"type", "data", "native_request"}
    assert item["type"] == "permission.requested" and item["native_request"] == binding
    assert item["data"]["tool_use_id"] == "toolu_1" and item["data"]["summary"] == "read a.md"
    item["data"]["summary"] = "mutated by the recorder"
    again = state.pending_request("toolu_1")
    assert again is not None and again["data"]["summary"] == "read a.md"
    assert state.snapshot()["events"][0]["data"]["summary"] == "read a.md"
    _append(state, RequestAnswered(tool_use_id="toolu_1"), 2, native_request=binding)
    assert state.pending_request("toolu_1") is None


# --- steers (a message folded into a running turn) ---------------------------------------------


def test_snapshot_lists_turn_records_then_steers_then_cards() -> None:
    state = StreamState()
    _append(state, UserMessage(text="synthetic"), 1)
    _append(state, SteerPending(steer_id="st-1", text="go faster"), 2)
    _append(state, _permission(), 3)
    assert set(state.steers) == {"st-1"}  # a steers bucket of its own, not a turn record
    assert [item["type"] for item in state.snapshot()["events"]] == [
        "user.message",
        "steer.pending",
        "permission.requested",
    ]


def test_a_started_steer_pops_and_adds_no_conversation_record() -> None:
    state = StreamState()
    _append(state, UserMessage(text="synthetic"), 1)
    _append(state, SteerPending(steer_id="st-1", text="go faster"), 2)
    _append(state, SteerEnded(steer_id="st-1", outcome="started"), 3)
    assert state.steers == {}
    assert [item["type"] for item in state.snapshot()["events"]] == ["user.message"]


def test_a_dropped_steer_pops_and_still_joins_the_conversation() -> None:
    state = StreamState()
    _append(state, UserMessage(text="synthetic"), 1)
    _append(state, SteerPending(steer_id="st-1", text="go faster"), 2)
    _append(state, SteerEnded(steer_id="st-1", outcome="dropped"), 3)
    assert state.steers == {}
    events = state.snapshot()["events"]
    assert [item["type"] for item in events] == ["user.message", "steer.ended"]
    assert events[-1]["data"] == {"steer_id": "st-1", "outcome": "dropped"}


def test_session_closed_clears_steers_like_cards() -> None:
    state = StreamState()
    _append(state, UserMessage(text="synthetic"), 1)
    _append(state, SteerPending(steer_id="st-1", text="go faster"), 2)
    _append(state, SessionClosed(reason="requested"), 3)
    assert state.steers == {}


def test_a_pending_steers_text_is_bound_to_its_own_limit() -> None:
    state = StreamState()
    _append(state, UserMessage(text="synthetic"), 1)
    huge = "x" * (stream_state.STEER_TEXT_CHARS + 10)
    _append(state, SteerPending(steer_id="st-1", text=huge), 2)
    text = state.steers["st-1"].item["data"]["text"]
    assert text == huge[: stream_state.STEER_TEXT_CHARS]
    assert len(text) == stream_state.STEER_TEXT_CHARS


def test_a_ninth_pending_steer_is_ignored_once_eight_are_held() -> None:
    state = StreamState()
    _append(state, UserMessage(text="synthetic"), 1)
    seq = 2
    for i in range(stream_state.MAX_STEERS):
        _append(state, SteerPending(steer_id=f"st-{i}", text="go"), seq)
        seq += 1
    assert set(state.steers) == {f"st-{i}" for i in range(stream_state.MAX_STEERS)}
    _append(state, SteerPending(steer_id="st-overflow", text="go"), seq)
    assert "st-overflow" not in state.steers
    assert len(state.steers) == stream_state.MAX_STEERS


def test_eight_full_steers_fit_the_snapshot_budget_with_content_cards_and_state_near_full() -> None:
    """The worst realistic case the separate budgets must coexist under: content and a card
    pushed to their own real caps, state padded near its own, and the steer dict full of its
    largest possible items — still fits the combined wire budget (MAX_SNAPSHOT_BYTES)."""
    state = StreamState()
    _append(state, UserMessage(text="synthetic"), 1)

    record_size = encoded_size(
        {
            "type": "text.done",
            "data": TextDone(block_id="b", text="x" * stream_state.MAX_TEXT_CHARS).data(),
        }
    )
    seq = 2
    for i in range((stream_state.MAX_CONTENT_BYTES // record_size) + 5):
        _append(state, TextDone(block_id=f"b{i}", text="x" * stream_state.MAX_TEXT_CHARS), seq)
        seq += 1
    assert stream_state.MAX_CONTENT_BYTES - record_size < state.content_bytes
    assert state.content_bytes <= stream_state.MAX_CONTENT_BYTES

    card_overhead = encoded_size(
        {
            "type": "permission.requested",
            "data": PermissionRequested(
                tool_use_id="tool-1", kind="plan", tool="ExitPlanMode", summary="", plan=""
            ).data(),
        }
    )
    _append(
        state,
        PermissionRequested(
            tool_use_id="tool-1",
            kind="plan",
            tool="ExitPlanMode",
            summary="",
            plan="x" * (stream_state.MAX_CARD_BYTES - card_overhead),
        ),
        seq,
    )
    seq += 1
    assert state.card_bytes <= stream_state.MAX_CARD_BYTES

    empty_state_size = encoded_size(state.state)
    padding = max(0, stream_state.MAX_STATE_BYTES - empty_state_size - 50)
    state.state["probe"] = "x" * padding
    assert encoded_size(state.state) <= stream_state.MAX_STATE_BYTES

    astral = "\U0001f600" * stream_state.STEER_TEXT_CHARS
    for i in range(stream_state.MAX_STEERS):
        _append(state, SteerPending(steer_id=f"st-{i}", text=astral), seq)
        seq += 1
    assert len(state.steers) == stream_state.MAX_STEERS

    assert encoded_size(state.snapshot()) < stream_state.MAX_SNAPSHOT_BYTES
