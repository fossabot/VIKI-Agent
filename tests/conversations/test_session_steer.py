"""Sends while a turn runs: the session actor writes them into that turn as steers, holds each one
until its driver ends it, and settles each exactly once. Synthetic drivers only."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any, Literal

import pytest
from claude_agent_sdk import CLIConnectionError

from tests.conversations.test_session import (
    HANG_GUARD_S,
    Drivers,
    GatedDriver,
    Seen,
    eventually,
    gated,
    hooked_profile,
    watch,
)
from viki_agent.conversations.stream_state import MAX_STEERS, STEER_TEXT_CHARS
from viki_agent.drivers.base import SendError
from viki_agent.drivers.claude.control import Launch
from viki_agent.drivers.claude.driver import MSG_CRASHED
from viki_agent.events import (
    Command,
    Error,
    Event,
    SessionStarted,
    SteerEnded,
    TurnDone,
    TurnStarted,
    Usage,
)
from viki_agent.profiles.profile_capabilities import FIXTURE_VERSION
from viki_agent.session import (
    MSG_ATTACHMENT_UNAVAILABLE,
    STOP_FLUSH_S,
    CommandDispatchRejected,
    CommandDispatchResult,
    SessionActor,
)
from viki_agent.state import Prefs
from viki_agent.turn_input import Attachment, AttachmentUnavailable, TurnInput

from replay import SID

SENT = CommandDispatchResult("unknown", "native_unconfirmed")
IMAGE = Attachment("00000000-0000-4000-8000-000000000015", "image/png", 4, 1, 1, "synthetic.png")


class SteeringDriver(GatedDriver):
    """A GatedDriver that also takes steers (`SteerDriver`) and images (`TurnInputDriver`), in a
    session that offers /compact and reports `version` as its Claude Code version. It records
    each steer's id and runs `on_steer` on it, which may raise; `interrupt` runs `on_interrupt`;
    `settle_steers` records its bound and runs `on_settle`; `end` and `crash` emit what the real
    driver's lifecycle frames and exit do."""

    def __init__(self, emit: Callable[[Event], None], version: str = "") -> None:
        super().__init__()
        self.release.set()
        self.emit = emit
        self.version = version
        self.steers: list[str] = []  # steer ids, in write order
        self.on_steer: Callable[[str], None] | None = None
        self.on_interrupt: Callable[[], None] | None = None
        self.on_settle: Callable[[], None] | None = None

    async def open(self) -> SessionStarted:
        started = await super().open()
        return replace(
            started,
            commands=[Command(name="compact", description="Compact")],
            cli_version=self.version,
        )

    async def interrupt(self) -> None:
        await super().interrupt()
        if self.on_interrupt is not None:
            self.on_interrupt()

    async def send_turn(self, turn: TurnInput) -> None:
        self.calls.append(f"send {turn.text}")

    async def steer(self, turn: TurnInput, steer_id: str, /) -> None:
        self.calls.append(f"steer {turn.text}")
        self.steers.append(steer_id)
        if self.on_steer is not None:
            self.on_steer(steer_id)

    async def settle_steers(self, timeout_s: float, /) -> None:
        self.calls.append(f"settle {timeout_s}")
        if self.on_settle is not None:
            self.on_settle()

    def end(self, index: int, outcome: Literal["started", "dropped"]) -> None:
        self.emit(SteerEnded(steer_id=self.steers[index], outcome=outcome))

    def crash(self, pending: list[str]) -> None:
        """An exit the app did not ask for: the steers still pending drop, then the turn ends."""
        self.alive = False
        for steer_id in pending:
            self.emit(SteerEnded(steer_id=steer_id, outcome="dropped"))
        if self.busy:
            self.busy = False
            self.emit(TurnDone(status="error", usage=Usage()))
        self.emit(Error(code="crashed", message=MSG_CRASHED, recoverable=True))


class SteeringDrivers(Drivers):
    version = ""  # the Claude Code version each driver it makes reports

    def __call__(self, launch: Launch, prefs: Prefs, emit: Callable[[Event], None]) -> GatedDriver:
        driver = SteeringDriver(emit, self.version)
        self.made.append((driver, launch, prefs, emit))
        self.created.set()
        return driver

    def driver(self, index: int = 0) -> SteeringDriver:
        driver = self.made[index][0]
        assert isinstance(driver, SteeringDriver)
        return driver


async def busy_chat(tmp_path: Path) -> tuple[SessionActor, SteeringDriver]:
    """A new chat whose process runs a turn it started itself, so nothing was sent yet."""
    drivers = SteeringDrivers()
    actor = gated(tmp_path, drivers).create(SID)
    assert await actor.start()
    driver = drivers.driver()
    driver.busy = True
    return actor, driver


async def _intent() -> None:
    return None


async def send(actor: SessionActor, turn: TurnInput | str) -> CommandDispatchResult:
    value = TurnInput(turn) if isinstance(turn, str) else turn

    async def accept() -> TurnInput:
        return value

    return await actor.send_command(
        expected_generation=actor.generation, accept=accept, dispatch_intent=_intent
    )


def dropped(steer_ids: list[str]) -> list[dict[str, str]]:
    return [{"steer_id": steer_id, "outcome": "dropped"} for steer_id in steer_ids]


def ended(seen: Seen) -> list[dict[str, Any]]:
    return [event["data"] for event in seen if event["type"] == "steer.ended"]


async def test_a_send_while_a_turn_runs_steers_into_it(tmp_path: Path) -> None:
    actor, driver = await busy_chat(tmp_path)
    seen = watch(actor)
    assert await send(actor, "first") == SENT
    driver.busy = False  # the turn ends before Claude Code reads the steer
    assert actor.busy  # the waiting steer keeps the chat busy, so the next send steers too
    assert await send(actor, "second") == SENT
    assert driver.calls == ["open", "opened", "steer first", "steer second"]
    assert seen.types == ["steer.pending", "steer.pending"]  # no user.message, no turn of its own
    assert [event["data"]["steer_id"] for event in seen] == driver.steers
    await actor.close()


async def test_a_ninth_waiting_steer_is_refused_before_acceptance(tmp_path: Path) -> None:
    actor, driver = await busy_chat(tmp_path)
    for index in range(MAX_STEERS):
        assert await send(actor, f"steer {index}") == SENT

    async def forbidden() -> TurnInput:
        pytest.fail("acceptance ran for a steer over the limit")

    with pytest.raises(CommandDispatchRejected) as caught:
        await actor.send_command(
            expected_generation=actor.generation, accept=forbidden, dispatch_intent=_intent
        )
    assert caught.value.code == "turn_running"
    assert len(driver.steers) == MAX_STEERS
    driver.end(0, "started")  # Claude Code reads one: room for one more
    assert await send(actor, "one more") == SENT
    await actor.close()


@pytest.mark.parametrize("stage", ["accept", "intent"])
@pytest.mark.parametrize("busy", [True, False])
async def test_busy_changing_after_entry_is_decided_at_the_write(
    tmp_path: Path, stage: str, busy: bool
) -> None:
    actor, driver = await busy_chat(tmp_path)
    driver.busy = busy
    seen = watch(actor)
    entered, release = asyncio.Event(), asyncio.Event()

    async def pause(at: str) -> None:
        if stage == at:
            entered.set()
            await release.wait()

    async def accept() -> TurnInput:
        await pause("accept")
        return TurnInput("flipped")

    async def intent() -> None:
        await pause("intent")

    task = asyncio.create_task(
        actor.send_command(
            expected_generation=actor.generation, accept=accept, dispatch_intent=intent
        )
    )
    await asyncio.wait_for(entered.wait(), HANG_GUARD_S)
    driver.busy = not busy  # the running turn ends, or the CLI starts one
    release.set()
    assert await asyncio.wait_for(task, HANG_GUARD_S) == SENT
    if busy:  # idle by the write: an ordinary turn
        assert driver.calls[-1] == "send flipped" and driver.steers == []
        assert seen.types == ["user.message", "turn.started"]
    else:  # busy by the write: a steer into the turn that began
        assert driver.calls[-1] == "steer flipped" and len(driver.steers) == 1
        assert seen.types == ["steer.pending"]
    await actor.close()


async def test_a_started_steer_echoes_the_whole_message_when_claude_code_reads_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    actor, driver = await busy_chat(tmp_path)
    stamps = iter(["2026-10-30T00:00:01.000Z", "2026-10-30T00:00:02.000Z"])
    monkeypatch.setattr("viki_agent.session.now_iso", lambda: next(stamps))
    seen = watch(actor)
    text = "x" * (STEER_TEXT_CHARS + 5)
    assert await send(actor, TurnInput(text, (IMAGE,))) == SENT
    assert driver.calls[-1] == f"steer {text}"  # Claude Code gets all of it
    [steer_id] = driver.steers
    assert seen.types == ["steer.pending"]
    assert seen[0]["data"] == {
        "steer_id": steer_id,
        "text": text[:STEER_TEXT_CHARS],
        "command": "",
        "images": 1,
        "at": "2026-10-30T00:00:01.000Z",
    }
    driver.end(0, "started")
    assert seen.types == ["steer.pending", "steer.ended", "user.message"]
    assert seen[1]["data"] == {"steer_id": steer_id, "outcome": "started"}
    # The message joins the conversation where Claude Code read it: whole, and stamped then.
    assert seen[2]["data"] == {
        "text": text,
        "command": "",
        "at": "2026-10-30T00:00:02.000Z",
        "attachments": [IMAGE.data()],
    }
    await actor.close()


async def test_an_end_for_a_steer_the_actor_does_not_hold_is_dropped(tmp_path: Path) -> None:
    actor, driver = await busy_chat(tmp_path)
    assert await send(actor, "held") == SENT
    seen = watch(actor)
    driver.emit(SteerEnded(steer_id=str(uuid.uuid4()), outcome="started"))  # never held
    assert seen.types == []
    driver.end(0, "started")
    driver.end(0, "dropped")  # a second end for the same steer
    assert seen.types == ["steer.ended", "user.message"]
    await actor.close()


async def test_a_dropped_steer_ends_without_an_echo(tmp_path: Path) -> None:
    actor, driver = await busy_chat(tmp_path)
    assert await send(actor, "never read") == SENT
    seen = watch(actor)
    driver.busy = False
    driver.end(0, "dropped")
    assert seen.types == ["steer.ended"]
    assert ended(seen) == dropped(driver.steers)
    assert not actor.busy and actor.live
    await actor.close()


@pytest.mark.parametrize("loss", ["exit", "close"])
async def test_losing_the_driver_ends_each_waiting_steer_once(tmp_path: Path, loss: str) -> None:
    actor, driver = await busy_chat(tmp_path)
    assert await send(actor, "one") == SENT
    assert await send(actor, "two") == SENT
    seen = watch(actor)
    if loss == "exit":
        driver.crash(driver.steers)  # the driver drops each one, then reports the exit
    else:
        await actor.close()  # a user close: the driver reports nothing, the actor drops them
    driver.crash(driver.steers)  # whatever the old driver says later is stale
    assert ended(seen) == dropped(driver.steers)
    assert not actor.busy and not actor.live
    await actor.close()


@pytest.mark.parametrize("waiting", ["no steer", "a steer that starts", "a steer that stays"])
async def test_closing_lets_waiting_steers_settle_before_the_process_goes(
    tmp_path: Path, waiting: str
) -> None:
    actor, driver = await busy_chat(tmp_path)
    if waiting != "no steer":
        assert await send(actor, "waiting") == SENT
    seen = watch(actor)

    def starts() -> None:
        # The stopped turn ends, Claude Code runs its waiting steer next, and that is stopped too.
        driver.busy = False
        driver.emit(TurnDone(status="cancelled", usage=Usage()))
        driver.end(0, "started")
        driver.busy = True
        driver.emit(TurnStarted(turn_id=str(uuid.uuid4())))
        driver.busy = False
        driver.emit(TurnDone(status="cancelled", usage=Usage()))

    if waiting == "a steer that starts":
        driver.on_settle = starts
    await actor.close()
    driver.crash(driver.steers)  # what the closed driver says afterwards is stale
    # Asked after the stop, with its bound, while the driver is still bound: before it closes.
    # With nothing waiting it is asked all the same, and returns at once.
    assert driver.calls[-3:] == ["interrupt", f"settle {STOP_FLUSH_S}", "close"]
    if waiting == "no steer":
        assert seen.types == ["session.closed"]
    elif waiting == "a steer that starts":
        assert ended(seen) == [{"steer_id": driver.steers[0], "outcome": "started"}]
        assert seen.types == [
            "turn.done",
            "steer.ended",
            "user.message",
            "turn.started",
            "turn.done",
            "session.closed",
        ]
    else:  # still waiting once the bound passed: dropped, once
        assert ended(seen) == dropped(driver.steers)
        assert seen.types == ["steer.ended", "session.closed"]


async def test_a_steer_that_starts_while_close_stops_the_turn_is_still_settled(
    tmp_path: Path,
) -> None:
    actor, driver = await busy_chat(tmp_path)
    assert await send(actor, "waiting") == SENT
    seen = watch(actor)

    def stopped() -> None:
        # While the stop is awaited, the turn ends and Claude Code runs the waiting steer next:
        # the actor holds no steer any more, but a turn a steer started now runs.
        driver.busy = False
        driver.emit(TurnDone(status="cancelled", usage=Usage()))
        driver.end(0, "started")
        driver.busy = True
        driver.emit(TurnStarted(turn_id=str(uuid.uuid4())))

    def settled() -> None:  # the driver stops the turn the steer started
        driver.busy = False
        driver.emit(TurnDone(status="cancelled", usage=Usage()))

    driver.on_interrupt = stopped
    driver.on_settle = settled
    await actor.close()
    assert driver.calls[-3:] == ["interrupt", f"settle {STOP_FLUSH_S}", "close"]
    assert ended(seen) == [{"steer_id": driver.steers[0], "outcome": "started"}]
    assert seen.types == [
        "turn.done",
        "steer.ended",
        "user.message",
        "turn.started",
        "turn.done",
        "session.closed",
    ]


async def test_a_slash_command_is_never_steered(tmp_path: Path) -> None:
    actor, driver = await busy_chat(tmp_path)
    seen = watch(actor)
    result = await send(actor, "/compact")
    assert result == CommandDispatchResult("not_dispatched", "runtime_changed")
    assert seen.types == [] and driver.calls == ["open", "opened"]  # nothing emitted or written
    assert actor._sent is False
    await actor.close()


@pytest.mark.parametrize("crash", [False, True])
async def test_an_image_that_cannot_be_read_drops_only_that_steer(
    tmp_path: Path, crash: bool
) -> None:
    actor, driver = await busy_chat(tmp_path)
    launch = actor._launch
    seen = watch(actor)

    def unreadable(steer_id: str) -> None:
        if crash:  # the process dies while the image is read
            driver.crash([steer_id])
        raise AttachmentUnavailable

    driver.on_steer = unreadable
    result = await send(actor, TurnInput("describe this", (IMAGE,)))
    assert result == CommandDispatchResult("not_dispatched", "attachment_unavailable")
    assert ended(seen) == dropped(driver.steers)
    # Nothing of it was written, so no transcript came of it: the next start must not resume.
    assert actor._sent is False and actor._launch == launch
    if crash:
        assert seen.types == ["steer.pending", "steer.ended", "turn.done", "error"]
        assert seen[-1]["data"]["code"] == "crashed" and not actor.live
    else:  # the running turn goes on: no turn.done, only the notice
        assert seen.types == ["steer.pending", "steer.ended", "error"]
        assert seen[-1]["data"] == {
            "code": "bad_request",
            "message": MSG_ATTACHMENT_UNAVAILABLE,
            "recoverable": True,
        }
        assert actor.live and actor.busy and "close" not in driver.calls
    await actor.close()


async def test_a_failed_steer_write_retires_the_process_and_drops_every_steer(
    tmp_path: Path,
) -> None:
    actor, driver = await busy_chat(tmp_path)
    assert await send(actor, "waiting") == SENT
    seen = watch(actor)

    def refused(_steer_id: str) -> None:
        raise SendError("frame_error", "synthetic overflow")

    driver.on_steer = refused
    assert await send(actor, "failing") == CommandDispatchResult("unknown", "send_failed")
    assert ended(seen) == dropped(driver.steers)
    assert seen.types[-2:] == ["turn.done", "error"]
    assert seen[-1]["data"]["code"] == "frame_error"
    assert not actor.live and driver.calls[-1] == "close"


@pytest.mark.parametrize("during", ["nothing", "a turn starts", "the turn ends"])
async def test_a_failed_steer_write_ends_only_a_turn_the_driver_runs(
    tmp_path: Path, during: str
) -> None:
    actor, driver = await busy_chat(tmp_path)
    if during != "the turn ends":
        assert await send(actor, "waiting") == SENT
        driver.busy = False  # the turn ended (its result came), the steer is not read yet
    seen = watch(actor)

    def failing(_steer_id: str) -> None:
        if during == "a turn starts":  # Claude Code reads the waiting steer: a turn opens
            driver.end(0, "started")
            driver.busy = True
            driver.emit(TurnStarted(turn_id=str(uuid.uuid4())))
        elif during == "the turn ends":
            driver.busy = False
            driver.emit(TurnDone(status="success", usage=Usage()))
        raise SendError("frame_error", "synthetic overflow")

    driver.on_steer = failing
    assert await send(actor, "failing") == CommandDispatchResult("unknown", "send_failed")
    # Only a turn the driver runs when the write fails ends here; an ended one keeps its status.
    done = [event["data"]["status"] for event in seen if event["type"] == "turn.done"]
    assert done == {"nothing": [], "a turn starts": ["error"], "the turn ends": ["success"]}[during]
    assert ended(seen)[-1] == {"steer_id": driver.steers[-1], "outcome": "dropped"}
    assert seen.types[-1] == "error" and seen[-1]["data"]["code"] == "frame_error"
    assert not actor.live and driver.calls[-1] == "close"


async def busy_profile_chat(
    tmp_path: Path, version: str, busy_at: str
) -> tuple[SessionActor, SteeringDriver, Seen]:
    """A profile chat whose process reports `version`, past its first send (its start), busy now
    when `busy_at` is "entry"; watched from here on."""
    drivers = SteeringDrivers()
    drivers.version = version
    actor = hooked_profile(gated(tmp_path, drivers), lambda *_: None)
    assert await send(actor, "first") == SENT  # a profile chat starts on its first send
    driver = drivers.driver()
    driver.busy = busy_at == "entry"
    return actor, driver, watch(actor)


@pytest.mark.parametrize("busy_at", ["entry", "acceptance"])
async def test_a_busy_profile_chat_steers_on_the_evidenced_version(
    tmp_path: Path, busy_at: str
) -> None:
    # Capture 38 recorded a profile chat's steer on FIXTURE_VERSION.
    actor, driver, seen = await busy_profile_chat(tmp_path, FIXTURE_VERSION, busy_at)

    async def accept() -> TurnInput:
        driver.busy = True  # still busy, or a turn began while the command was recorded
        return TurnInput("second")

    result = await actor.send_command(
        expected_generation=actor.generation, accept=accept, dispatch_intent=_intent
    )
    assert result == SENT
    assert driver.calls[-1] == "steer second" and len(driver.steers) == 1
    assert seen.types == ["steer.pending"]
    await actor.close()


@pytest.mark.parametrize("version", ["2.1.309", ""])
@pytest.mark.parametrize("busy_at", ["entry", "acceptance"])
async def test_a_busy_profile_chat_without_steer_evidence_refuses(
    tmp_path: Path, busy_at: str, version: str
) -> None:
    actor, driver, seen = await busy_profile_chat(tmp_path, version, busy_at)

    async def accept() -> TurnInput:
        if busy_at == "entry":
            pytest.fail("acceptance ran while the profile chat was busy")
        driver.busy = True  # a turn begins while the command is recorded
        return TurnInput("second")

    attempt = actor.send_command(
        expected_generation=actor.generation, accept=accept, dispatch_intent=_intent
    )
    if busy_at == "entry":
        with pytest.raises(CommandDispatchRejected) as caught:
            await attempt
        assert caught.value.code == "turn_running"
    else:
        assert await attempt == CommandDispatchResult("not_dispatched", "runtime_changed")
    assert driver.steers == [] and driver.calls[-1] == "send first"
    assert seen.types == []  # nothing emitted or written
    await actor.close()


async def test_a_pending_effort_change_waits_while_a_steer_joins_the_turn(tmp_path: Path) -> None:
    drivers = SteeringDrivers()
    actor = gated(tmp_path, drivers).create(SID)
    assert await actor.start()
    driver = drivers.driver()
    driver.busy = True
    assert actor.set_effort("max")
    assert await send(actor, "steer") == SENT
    # No respawn under the running turn: the process and its turn go on.
    assert len(drivers.made) == 1 and driver.calls[-1] == "steer steer"
    driver.busy = False
    driver.end(0, "started")
    assert await send(actor, "next") == SENT  # the next turn respawns with the new effort
    assert driver.calls[-1] == "close" and drivers.made[1][2].effort == "max"
    assert drivers.driver(1).calls[-1] == "send next"
    await actor.close()


async def test_wait_idle_waits_for_a_waiting_steer(tmp_path: Path) -> None:
    actor, driver = await busy_chat(tmp_path)
    assert await send(actor, "waiting") == SENT
    idle = asyncio.create_task(actor.wait_idle(HANG_GUARD_S))
    await eventually(lambda: bool(actor._idle_waiters))
    driver.busy = False
    driver.emit(TurnDone(status="success", usage=Usage()))
    assert not actor._idle_waiters[0].done()  # the turn is over, the steer still waits
    driver.end(0, "dropped")
    assert await asyncio.wait_for(idle, HANG_GUARD_S) is True
    await actor.close()


async def test_an_output_overflow_drops_each_waiting_steer_once(tmp_path: Path) -> None:
    actor, driver = await busy_chat(tmp_path)
    assert await send(actor, "one") == SENT
    assert await send(actor, "two") == SENT
    seen = watch(actor)
    # What the driver emits on an output overflow: its pump has ended, so no steer.ended.
    driver.alive = driver.busy = False
    driver.emit(TurnDone(status="error", usage=Usage()))
    driver.emit(Error(code="frame_error", message="synthetic overflow", recoverable=False))
    assert seen.types == ["turn.done", "steer.ended", "steer.ended", "error"]
    assert ended(seen) == dropped(driver.steers)
    assert not actor.busy and not actor.live
    assert actor._launch == Launch(SID, resume=True)  # the steers were written: resume it


@pytest.mark.parametrize("first", ["exit", "write"])
async def test_an_exit_while_a_steer_is_written_ends_it_once(tmp_path: Path, first: str) -> None:
    actor, driver = await busy_chat(tmp_path)
    seen = watch(actor)

    def lost(steer_id: str) -> None:
        if first == "exit":  # the driver handles the exit before the write fails
            driver.crash([steer_id])
        raise CLIConnectionError("synthetic pipe failure")

    driver.on_steer = lost
    assert await send(actor, "lost") == CommandDispatchResult("unknown", "send_failed")
    driver.crash(driver.steers)  # the exit's drops after the actor retired the process: stale
    assert seen.types[0] == "steer.pending"  # announced before the write began
    assert ended(seen) == dropped(driver.steers)
    assert seen.types.count("turn.done") == 1 and seen.types.count("error") == 1
    assert not actor.live and not actor.busy
