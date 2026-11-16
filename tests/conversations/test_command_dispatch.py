"""Guarded actor dispatch with synthetic drivers and committed-callback stand-ins."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path

import pytest
from claude_agent_sdk import CLIConnectionError, ProcessError

from tests.conversations.test_session import Drivers, GatedDriver, gated, setup, watch
from viki_agent.conversations.commands import CommandInput, CommandService
from viki_agent.conversations.drafts import DraftService
from viki_agent.drivers.base import SendError, StartError
from viki_agent.drivers.claude.control import Launch
from viki_agent.drivers.claude.driver import MSG_CRASHED, ClaudeDriver
from viki_agent.events import Error, Event, SessionStarted
from viki_agent.session import (
    MSG_ATTACHMENT_UNAVAILABLE,
    MSG_NOT_SENT,
    CommandDispatchRejected,
    CommandDispatchResult,
)
from viki_agent.state import AppState, Prefs
from viki_agent.turn_input import (
    CLAUDE_MAX_ATTACHMENT_BYTES,
    Attachment,
    AttachmentUnavailable,
    TurnInput,
)

from replay import text_delta

SID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
GEN = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
CID = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
OWNER = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
# Only stops a test whose awaited event or task never comes; each normally does in ms.
HANG_GUARD_S = 5


async def _accept() -> str:
    return "synthetic command"


async def _intent() -> None:
    return None


async def test_context_is_read_only_and_replacement_actor_gets_new_generation(
    tmp_path: Path,
) -> None:
    drivers = Drivers()
    manager = gated(tmp_path, drivers)
    actor = manager.open(SID)
    assert actor.command_context() == {
        "session_id": SID,
        "generation": actor.generation,
        "closed": False,
        "busy": False,
        "live": False,
    }
    assert drivers.made == []
    await manager.close(SID)
    assert actor.command_context()["closed"] is True
    replacement = manager.open(SID)
    assert replacement.generation != actor.generation
    assert drivers.made == []


@pytest.mark.parametrize("guard", ["stale_generation", "closed", "turn_running"])
async def test_actor_guards_run_before_acceptance(tmp_path: Path, guard: str) -> None:
    drivers = Drivers()
    actor = gated(tmp_path, drivers).open(SID)
    generation = actor.generation
    if guard == "stale_generation":
        generation = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    elif guard == "closed":
        await actor.close()
    else:
        assert await actor.start()
        drivers.made[0][0].busy = True

    async def forbidden() -> str:
        pytest.fail("acceptance ran before actor guard")

    with pytest.raises(CommandDispatchRejected) as caught:
        await actor.send_command(
            expected_generation=generation, accept=forbidden, dispatch_intent=_intent
        )
    assert caught.value.code == guard
    assert not any(call.startswith("send ") for item in drivers.made for call in item[0].calls)
    await actor.close()


async def test_acceptance_then_start_then_intent_then_exact_native_text(tmp_path: Path) -> None:
    driver = GatedDriver()
    driver.release.set()
    actor = gated(tmp_path, driver).open(SID)
    text = "synthetic café\n・unchanged\n"

    async def accept() -> str:
        driver.calls.append("accepted")
        return text

    async def intent() -> None:
        driver.calls.append("intent committed")

    result = await actor.send_command(
        expected_generation=actor.generation, accept=accept, dispatch_intent=intent
    )
    assert driver.calls == ["accepted", "open", "opened", "intent committed", f"send {text}"]
    assert result == CommandDispatchResult("unknown", "native_unconfirmed")
    assert actor.busy is False  # Fake send returning does not manufacture an observed turn.
    await actor.close()


async def test_typed_attachment_command_reaches_the_actor_driver_unchanged(tmp_path: Path) -> None:
    class AttachmentDriver(GatedDriver):
        sent: str | TurnInput | None = None

        async def send_turn(self, turn: TurnInput) -> None:
            self.sent = turn
            self.calls.append("send attachment")

    driver = AttachmentDriver()
    driver.release.set()
    actor = gated(tmp_path, driver).open(SID)
    seen = watch(actor)
    attachment = Attachment(
        "00000000-0000-4000-8000-000000000011", "image/png", 4, 1, 1, "synthetic.png"
    )
    turn = TurnInput("describe this", (attachment,))

    async def accept() -> TurnInput:
        return turn

    result = await actor.send_command(
        expected_generation=actor.generation, accept=accept, dispatch_intent=_intent
    )
    assert result == CommandDispatchResult("unknown", "native_unconfirmed")
    assert driver.sent == turn
    message = next(event["data"] for event in seen if event["type"] == "user.message")
    assert message["text"] == "describe this" and message["attachments"] == [attachment.data()]
    await actor.close()


async def test_oversized_attachment_is_refused_before_any_driver_starts(tmp_path: Path) -> None:
    drivers = Drivers()
    actor = gated(tmp_path, drivers).open(SID)
    image = Attachment(
        "00000000-0000-4000-8000-000000000016",
        "image/png",
        CLAUDE_MAX_ATTACHMENT_BYTES + 1,
        1,
        1,
        "synthetic.png",
    )

    async def accept() -> TurnInput:
        return TurnInput("describe this", (image,))

    result = await actor.send_command(
        expected_generation=actor.generation, accept=accept, dispatch_intent=_intent
    )
    assert result == CommandDispatchResult("not_dispatched", "unsupported_attachment")
    assert drivers.made == []


async def test_duplicate_does_not_prepare_or_dispatch_a_runtime(tmp_path: Path) -> None:
    drivers = Drivers()
    actor = gated(tmp_path, drivers).open(SID)

    async def duplicate() -> None:
        return None

    async def forbidden_intent() -> None:
        pytest.fail("duplicate dispatch intent")

    result = await actor.send_command(
        expected_generation=actor.generation, accept=duplicate, dispatch_intent=forbidden_intent
    )
    assert result == CommandDispatchResult("duplicate")
    assert drivers.made == []


@pytest.mark.parametrize("stage", ["accept", "intent"])
async def test_callback_failure_propagates_without_native_send(tmp_path: Path, stage: str) -> None:
    drivers = Drivers()
    actor = gated(tmp_path, drivers).open(SID)

    async def failed():
        raise RuntimeError("synthetic persistence failure")

    with pytest.raises(RuntimeError, match="synthetic persistence failure"):
        await actor.send_command(
            expected_generation=actor.generation,
            accept=failed if stage == "accept" else _accept,
            dispatch_intent=failed if stage == "intent" else _intent,
        )
    assert len(drivers.made) == (stage == "intent")
    assert not any(call.startswith("send ") for item in drivers.made for call in item[0].calls)
    await actor.close()


async def test_start_refusal_after_acceptance_has_no_dispatch_intent(tmp_path: Path) -> None:
    drivers = Drivers()
    actor = gated(tmp_path, drivers).open(SID)
    actor._admit = lambda _: False

    async def forbidden_intent() -> None:
        pytest.fail("intent before runtime preparation")

    result = await actor.send_command(
        expected_generation=actor.generation, accept=_accept, dispatch_intent=forbidden_intent
    )
    assert result == CommandDispatchResult("not_dispatched", "start_failed")
    assert drivers.made == []


async def test_runtime_lost_during_open_is_closed_and_never_announced_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver = GatedDriver()
    driver.release.set()
    actor = gated(tmp_path, driver).open(SID)
    original = driver.open
    seen = watch(actor)

    async def late_open() -> SessionStarted:
        assert actor._token is not None
        actor._emit_from(
            actor._token, Error(code="crashed", message="synthetic startup loss", recoverable=False)
        )
        return await original()

    monkeypatch.setattr(driver, "open", late_open)
    result = await actor.send_command(
        expected_generation=actor.generation, accept=_accept, dispatch_intent=_intent
    )
    assert result.status == "not_dispatched"
    assert driver.calls[-1] == "close" and not actor.live
    assert "session.started" not in seen.types
    assert not any(call.startswith("send ") for call in driver.calls)


async def test_unexpected_open_failure_cleans_runtime_before_return(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver = GatedDriver()
    actor = gated(tmp_path, driver).open(SID)

    async def failed_open() -> SessionStarted:
        raise RuntimeError("synthetic private startup error")

    monkeypatch.setattr(driver, "open", failed_open)
    result = await actor.send_command(
        expected_generation=actor.generation, accept=_accept, dispatch_intent=_intent
    )
    assert result == CommandDispatchResult("not_dispatched", "internal")
    assert driver.calls == ["close"] and not actor.live


async def test_invalidated_startup_keeps_pid_owned_until_close_finishes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver = GatedDriver()
    driver.pid = 12345
    driver.release.set()
    driver.close_gate.clear()
    manager = gated(tmp_path, driver)
    actor = manager.open(SID)
    original = driver.open

    async def late_open() -> SessionStarted:
        assert actor._token is not None
        actor._emit_from(
            actor._token, Error(code="crashed", message="synthetic startup loss", recoverable=False)
        )
        return await original()

    monkeypatch.setattr(driver, "open", late_open)
    task = asyncio.create_task(
        actor.send_command(
            expected_generation=actor.generation, accept=_accept, dispatch_intent=_intent
        )
    )
    await asyncio.wait_for(driver.closing.wait(), HANG_GUARD_S)
    try:
        assert driver.alive and not task.done()
        assert actor.pids == {12345} and manager.own_pids() == {12345}
    finally:
        driver.close_gate.set()
        result = await asyncio.wait_for(task, HANG_GUARD_S)
    assert result.status == "not_dispatched"
    assert not driver.alive and actor.pids == set() and manager.own_pids() == set()
    assert not any(call.startswith("send ") for call in driver.calls)


async def test_command_waiting_behind_close_never_accepts(tmp_path: Path) -> None:
    driver = GatedDriver()
    driver.release.set()
    actor = gated(tmp_path, driver).open(SID)
    assert await actor.start()
    driver.close_gate.clear()
    closing = asyncio.create_task(actor.close())
    await asyncio.wait_for(driver.closing.wait(), HANG_GUARD_S)

    async def forbidden() -> str:
        pytest.fail("closed actor accepted a command")

    command = asyncio.create_task(
        actor.send_command(
            expected_generation=actor.generation, accept=forbidden, dispatch_intent=_intent
        )
    )
    driver.close_gate.set()
    await asyncio.wait_for(closing, HANG_GUARD_S)
    with pytest.raises(CommandDispatchRejected) as caught:
        await asyncio.wait_for(command, HANG_GUARD_S)
    assert caught.value.code == "closed"


@pytest.mark.parametrize("stage", ["accept", "intent"])
@pytest.mark.parametrize("change", ["effort", "drop", "busy", "dead"])
async def test_runtime_changes_during_durable_callback_never_reach_native(
    tmp_path: Path,
    stage: str,
    change: str,
) -> None:
    drivers = Drivers()
    actor = gated(tmp_path, drivers).open(SID)
    assert await actor.start()
    driver = drivers.made[0][0]
    entered, release = asyncio.Event(), asyncio.Event()
    intent_calls: list[str] = []

    async def pause() -> None:
        entered.set()
        await release.wait()

    async def accept() -> str:
        if stage == "accept":
            await pause()
        return "synthetic"

    async def intent() -> None:
        intent_calls.append("committed")
        if stage == "intent":
            await pause()

    task = asyncio.create_task(
        actor.send_command(
            expected_generation=actor.generation, accept=accept, dispatch_intent=intent
        )
    )
    await asyncio.wait_for(entered.wait(), HANG_GUARD_S)
    if change == "effort":
        actor.set_effort("max")
    elif change == "drop":
        driver.alive = False
        drivers.made[0][3](Error(code="crashed", message="synthetic loss", recoverable=False))
    elif change == "busy":
        driver.busy = True
    else:
        driver.alive = False
    release.set()
    result = await asyncio.wait_for(task, HANG_GUARD_S)
    assert result == CommandDispatchResult("not_dispatched", "runtime_changed")
    assert intent_calls == (["committed"] if stage == "intent" else [])
    assert not any(call.startswith("send ") for call in driver.calls)
    await actor.close()


async def test_intent_wait_revalidates_same_actor_replacement_token(tmp_path: Path) -> None:
    drivers = Drivers()
    actor = gated(tmp_path, drivers).open(SID)
    assert await actor.start()
    original = drivers.made[0][0]

    async def intent() -> None:
        # A native terminal path may replace/drop the token without acquiring actor._lock.
        actor._token = object()

    result = await actor.send_command(
        expected_generation=actor.generation, accept=_accept, dispatch_intent=intent
    )
    assert result == CommandDispatchResult("not_dispatched", "runtime_changed")
    assert not any(call.startswith("send ") for call in original.calls)
    await actor.close()


async def test_effort_changed_before_command_prepares_one_new_runtime(tmp_path: Path) -> None:
    drivers = Drivers()
    actor = gated(tmp_path, drivers).open(SID)
    assert await actor.start()
    actor.set_effort("max")
    result = await actor.send_command(
        expected_generation=actor.generation, accept=_accept, dispatch_intent=_intent
    )
    assert result.status == "unknown"
    assert len(drivers.made) == 2
    assert drivers.made[0][0].calls[-1] == "close"
    assert drivers.made[1][2].effort == "max"
    assert drivers.made[1][0].calls[-1] == "send synthetic command"
    await actor.close()


async def test_cancellation_during_effort_retirement_keeps_cleanup_owned(tmp_path: Path) -> None:
    drivers = Drivers()
    actor = gated(tmp_path, drivers).open(SID)
    assert await actor.start()
    driver = drivers.made[0][0]
    driver.pid = 12345
    driver.close_gate.clear()
    actor.set_effort("max")
    task = asyncio.create_task(
        actor.send_command(
            expected_generation=actor.generation, accept=_accept, dispatch_intent=_intent
        )
    )
    await asyncio.wait_for(driver.closing.wait(), HANG_GUARD_S)
    task.cancel()
    # Event-loop barriers let both cancellation attempts reach the cleanup await.
    for _ in range(2):
        barrier = asyncio.Event()
        asyncio.get_running_loop().call_soon(barrier.set)
        await barrier.wait()
        task.cancel()
    try:
        assert not task.done() and actor.pids == {12345}
    finally:
        driver.close_gate.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, HANG_GUARD_S)
    assert not driver.alive and driver.calls[-1] == "close"
    assert actor.pids == set() and not actor.live
    assert len(drivers.made) == 1
    assert not any(call.startswith("send ") for call in driver.calls)


async def test_configuration_change_during_open_stops_before_intent(tmp_path: Path) -> None:
    drivers = Drivers(gate_first=True)
    actor = gated(tmp_path, drivers).open(SID)

    async def forbidden_intent() -> None:
        pytest.fail("configuration changed while runtime opened")

    task = asyncio.create_task(
        actor.send_command(
            expected_generation=actor.generation, accept=_accept, dispatch_intent=forbidden_intent
        )
    )
    await asyncio.wait_for(drivers.created.wait(), HANG_GUARD_S)
    actor.set_effort("max")
    drivers.made[0][0].release.set()
    result = await asyncio.wait_for(task, HANG_GUARD_S)
    assert result == CommandDispatchResult("not_dispatched", "runtime_changed")
    assert not any(call.startswith("send ") for call in drivers.made[0][0].calls)
    await actor.close()


@pytest.mark.parametrize("stage", ["open", "intent", "send"])
async def test_cancellation_never_returns_a_native_receipt(tmp_path: Path, stage: str) -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    class PausedDriver(GatedDriver):
        async def open(self) -> SessionStarted:
            if stage == "open":
                entered.set()
                await release.wait()
            return await super().open()

        async def send(self, text: str) -> None:
            self.calls.append(f"send {text}")
            if stage == "send":
                entered.set()
                await release.wait()

    driver = PausedDriver()
    driver.release.set()
    actor = gated(tmp_path, driver).open(SID)

    async def intent() -> None:
        if stage == "intent":
            entered.set()
            await release.wait()

    task = asyncio.create_task(
        actor.send_command(
            expected_generation=actor.generation, accept=_accept, dispatch_intent=intent
        )
    )
    await asyncio.wait_for(entered.wait(), HANG_GUARD_S)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, HANG_GUARD_S)
    sends = [call for call in driver.calls if call.startswith("send ")]
    assert len(sends) == (stage == "send")
    if stage in {"open", "send"}:
        assert not actor.live and driver.calls[-1] == "close"
    await actor.close()


@pytest.mark.parametrize(
    "failure",
    [
        "connection",
        "overflow",
        "unexpected",
    ],
)
async def test_native_failure_is_unknown_and_later_explicit_send_resumes(
    tmp_path: Path,
    failure: str,
) -> None:
    drivers = Drivers()
    actor = gated(tmp_path, drivers).create(SID)
    assert await actor.start()
    driver = drivers.made[0][0]
    driver.send_error = {
        "connection": CLIConnectionError("synthetic native failure"),
        "overflow": SendError("frame_error", "synthetic terminal overflow"),
        "unexpected": RuntimeError("synthetic unexpected failure"),
    }[failure]
    expected_code = {
        "connection": "provider_disconnected",
        "overflow": "frame_error",
        "unexpected": "internal",
    }[failure]
    seen = watch(actor)
    result = await actor.send_command(
        expected_generation=actor.generation, accept=_accept, dispatch_intent=_intent
    )
    assert result.status == "unknown"
    assert not actor.live and driver.calls[-1] == "close"
    errors = [event for event in seen if event["type"] == "error"]
    assert len(errors) == 1 and errors[0]["data"]["recoverable"] is False
    code = errors[0]["data"]["code"]
    # code is checked last so a wrong code cannot stop the test before the resume checks below
    # have run for every parametrization.
    assert await actor.start()
    assert drivers.made[-1][1].resume is True
    assert len(drivers.made) == 2
    assert code == expected_code
    await actor.close()


_IMAGE = Attachment("00000000-0000-4000-8000-000000000014", "image/png", 4, 1, 1, "synthetic.png")


class UnavailableDriver(GatedDriver):
    """A typed driver whose attachment resolution fails before any provider write."""

    async def send_turn(self, turn: TurnInput) -> None:
        self.calls.append("send attachment")
        raise AttachmentUnavailable


class UnavailableDrivers(Drivers):
    def __call__(self, launch: Launch, prefs: Prefs, emit: Callable[[Event], None]) -> GatedDriver:
        driver = UnavailableDriver()
        driver.release.set()
        self.made.append((driver, launch, prefs, emit))
        return driver


async def _image_turn() -> TurnInput:
    return TurnInput("describe this", (_IMAGE,))


class BusyDriver(GatedDriver):
    """A typed driver whose attachment resolution fails transiently, before any provider write."""

    async def send_turn(self, turn: TurnInput) -> None:
        self.calls.append("send attachment")
        raise AttachmentUnavailable(transient=True)


class BusyDrivers(Drivers):
    def __call__(self, launch: Launch, prefs: Prefs, emit: Callable[[Event], None]) -> GatedDriver:
        driver = BusyDriver()
        driver.release.set()
        self.made.append((driver, launch, prefs, emit))
        return driver


async def test_unavailable_attachment_is_not_dispatched_and_keeps_the_runtime(
    tmp_path: Path,
) -> None:
    drivers = UnavailableDrivers()
    actor = gated(tmp_path, drivers).open(SID)
    seen = watch(actor)
    result = await actor.send_command(
        expected_generation=actor.generation, accept=_image_turn, dispatch_intent=_intent
    )
    assert result == CommandDispatchResult("not_dispatched", "attachment_unavailable")
    driver = drivers.made[0][0]
    assert actor.live and driver.alive and "close" not in driver.calls
    # The turn the view opened is closed by exactly one terminal pair.
    assert seen.types[-4:] == ["user.message", "turn.started", "turn.done", "error"]
    assert seen[-2]["data"]["status"] == "error"
    assert seen[-1]["data"] == {
        "code": "bad_request",
        "message": MSG_ATTACHMENT_UNAVAILABLE,
        "recoverable": True,
    }
    later = await actor.send_command(
        expected_generation=actor.generation, accept=_accept, dispatch_intent=_intent
    )
    assert later == CommandDispatchResult("unknown", "native_unconfirmed")
    assert len(drivers.made) == 1 and driver.calls[-1] == "send synthetic command"
    await actor.close()


async def test_refused_first_turn_leaves_no_transcript_to_resume(tmp_path: Path) -> None:
    drivers = UnavailableDrivers()
    actor = gated(tmp_path, drivers).create(SID)
    result = await actor.send_command(
        expected_generation=actor.generation, accept=_image_turn, dispatch_intent=_intent
    )
    assert result == CommandDispatchResult("not_dispatched", "attachment_unavailable")
    drivers.made[0][0].alive = False  # the process ends later for an unrelated reason
    drivers.made[0][3](Error(code="crashed", message="synthetic loss", recoverable=False))
    assert await actor.start()
    assert drivers.made[1][1] == Launch(SID)  # a fresh start, never --resume
    await actor.close()


async def test_attachment_unavailable_on_a_first_send_restores_sent_and_launch(
    tmp_path: Path,
) -> None:
    class CrashingUnavailableDriver(GatedDriver):
        """Crashes while reading the attachment (re-arming a resume launch), then refuses
        it, so the restore must be the thing that puts `_launch` back."""

        def __init__(self, emit: Callable[[Event], None]) -> None:
            super().__init__()
            self.release.set()
            self.emit = emit

        async def send_turn(self, turn: TurnInput) -> None:
            self.alive = False
            self.emit(Error(code="crashed", message=MSG_CRASHED, recoverable=True))
            raise AttachmentUnavailable

    class CrashingUnavailableDrivers(Drivers):
        def __call__(
            self, launch: Launch, prefs: Prefs, emit: Callable[[Event], None]
        ) -> GatedDriver:
            driver = CrashingUnavailableDriver(emit)
            self.made.append((driver, launch, prefs, emit))
            return driver

    drivers = CrashingUnavailableDrivers()
    actor = gated(tmp_path, drivers).create(SID)
    launch_before = actor._launch
    result = await actor.send_command(
        expected_generation=actor.generation, accept=_image_turn, dispatch_intent=_intent
    )
    assert result == CommandDispatchResult("not_dispatched", "attachment_unavailable")
    assert actor._sent is False
    assert actor._launch == launch_before
    await actor.close()


async def test_attachment_unavailable_after_a_sent_turn_keeps_sent_and_launch(
    tmp_path: Path,
) -> None:
    drivers = UnavailableDrivers()
    actor = gated(tmp_path, drivers).create(SID)
    first = await actor.send_command(
        expected_generation=actor.generation, accept=_accept, dispatch_intent=_intent
    )
    assert first == CommandDispatchResult("unknown", "native_unconfirmed")
    assert actor._sent is True
    launch_after_first_send = actor._launch
    result = await actor.send_command(
        expected_generation=actor.generation, accept=_image_turn, dispatch_intent=_intent
    )
    assert result == CommandDispatchResult("not_dispatched", "attachment_unavailable")
    assert actor._sent is True
    assert actor._launch == launch_after_first_send
    await actor.close()


async def test_transient_attachment_unavailable_says_send_again(tmp_path: Path) -> None:
    drivers = BusyDrivers()
    actor = gated(tmp_path, drivers).open(SID)
    seen = watch(actor)
    result = await actor.send_command(
        expected_generation=actor.generation, accept=_image_turn, dispatch_intent=_intent
    )
    assert result == CommandDispatchResult("not_dispatched", "attachment_unavailable")
    assert seen[-1]["data"] == {
        "code": "bad_request",
        "message": (
            "Couldn't load the attached image right now, so nothing was sent. "
            "You can review the text in Drafts and send history → Send history → Restore text. "
            "Wait a moment, then send again."
        ),
        "recoverable": True,
    }
    await actor.close()


@pytest.mark.parametrize("reason", ["unsupported_attachment", "attachment_unavailable"])
def test_attachment_refusal_reasons_are_recorded(tmp_path: Path, reason: str) -> None:
    state = AppState(tmp_path / "data" / "state.json", Prefs())
    DraftService(state, tmp_path / "vault").save(SID, 0, "synthetic")
    service = CommandService(state, tmp_path / "vault", owner=OWNER)
    service.accept(CommandInput(CID, SID, GEN, 1))
    service.mark_dispatching(CID)
    finished = service.finish(CID, "not_dispatched", reason)
    assert (finished.state, finished.reason) == ("not_dispatched", reason)
    assert service.get(CID) == finished


async def test_legacy_send_refuses_an_invalid_turn_as_not_sent(tmp_path: Path) -> None:
    drivers = Drivers()
    actor = gated(tmp_path, drivers).open(SID)
    seen = watch(actor)
    assert await actor.send("cut emoji \ud83d") is False
    assert [event["data"] for event in seen if event["type"] == "error"] == [
        {"code": "bad_request", "message": MSG_NOT_SENT, "recoverable": True}
    ]
    assert drivers.made == []


async def test_legacy_send_hands_off_as_true(tmp_path: Path) -> None:
    drivers = Drivers()
    actor = gated(tmp_path, drivers).open(SID)
    assert await actor.send("synthetic") is True
    await actor.close()


async def test_legacy_send_while_busy_without_a_generation_is_not_sent(tmp_path: Path) -> None:
    drivers = Drivers()
    actor = gated(tmp_path, drivers).open(SID)
    assert await actor.start()
    drivers.made[0][0].busy = True
    assert await actor.send("synthetic") is False
    assert not any(call.startswith("send ") for call in drivers.made[0][0].calls)
    await actor.close()


async def test_legacy_send_whose_start_is_refused_is_not_sent(tmp_path: Path) -> None:
    class RefusingDriver(GatedDriver):
        async def open(self) -> SessionStarted:
            raise StartError("not_logged_in", "synthetic refusal")

    driver = RefusingDriver()
    actor = gated(tmp_path, driver).open(SID)
    assert await actor.send("synthetic") is False
    assert driver.calls == []
    await actor.close()


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (SendError("frame_error", "synthetic refusal before any write", unsent=True), False),
        (SendError("frame_error", "synthetic failure after the write"), None),
        (CLIConnectionError("synthetic pipe failure"), None),
        (ProcessError("synthetic process failure"), None),
    ],
)
async def test_legacy_send_failure_says_whether_anything_left(
    tmp_path: Path, error: Exception, expected: bool | None
) -> None:
    drivers = Drivers()
    actor = gated(tmp_path, drivers).open(SID)
    assert await actor.start()
    drivers.made[0][0].send_error = error
    assert await actor.send("synthetic") is expected
    await actor.close()


async def test_claude_driver_refusal_after_overflow_is_not_sent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("viki_agent.drivers.claude.driver.FRAME_QUEUE_ITEMS", 4)
    manager, spawner = setup(tmp_path, [])
    actor = manager.create(SID)
    assert await actor.start()
    driver = actor._driver
    assert isinstance(driver, ClaudeDriver)
    try:
        # Same event-loop step: the intake closes before send, so the driver refuses unwritten.
        for _ in range(5):
            driver._enqueue_frame(text_delta("synthetic backlog"), 100)
        assert await actor.send("must not reach the provider") is False
    finally:
        await manager.close_all()


async def test_claude_driver_refusal_after_a_write_overflow_leaves_receipt_unknown(
    tmp_path: Path,
) -> None:
    manager, spawner = setup(tmp_path, [])
    actor = manager.create(SID)
    assert await actor.start()
    driver = actor._driver
    assert isinstance(driver, ClaudeDriver)

    async def send_then_overflow(*args: object, **kwargs: object) -> None:
        # The overflow is detected only after bytes may already have left for the CLI's pipe;
        # the pipe then fails too, so the driver must not claim the turn was never sent.
        driver._overflow()
        raise CLIConnectionError("synthetic pipe failure after the write")

    try:
        driver._control.send = send_then_overflow
        assert await actor.send("must answer unknown, not unsent") is None
    finally:
        await manager.close_all()


async def test_legacy_send_and_model_change_wait_for_command_transaction(tmp_path: Path) -> None:
    drivers = Drivers()
    actor = gated(tmp_path, drivers).open(SID)
    entered, release = asyncio.Event(), asyncio.Event()

    async def accept() -> str:
        entered.set()
        await release.wait()
        return "first"

    command = asyncio.create_task(
        actor.send_command(
            expected_generation=actor.generation, accept=accept, dispatch_intent=_intent
        )
    )
    await asyncio.wait_for(entered.wait(), HANG_GUARD_S)
    legacy = asyncio.create_task(actor.send("second"))
    model = asyncio.create_task(actor.set_model("synthetic-model"))
    assert drivers.made == []
    release.set()
    await asyncio.wait_for(asyncio.gather(command, legacy, model), HANG_GUARD_S)
    calls = drivers.made[0][0].calls
    assert (
        calls.index("send first")
        < calls.index("send second")
        < calls.index("model synthetic-model")
    )
    await actor.close()
