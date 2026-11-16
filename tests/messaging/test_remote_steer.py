"""Messages sent while a turn runs are steered into it through the actor."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any, cast

import pytest

from tests.messaging.test_mattermost_bot import HANG_GUARD_S, Driver, Rig, take
from tests.messaging.test_remote_conversation import Conversation
from viki_agent.conversations.stream_state import MAX_STEERS
from viki_agent.drivers.base import PermissionAnswer
from viki_agent.events import Event, SteerEnded, TurnStarted
from viki_agent.messaging.messaging_channel import Provider
from viki_agent.session import CommandDispatchResult, SessionActor
from viki_agent.state import Prefs
from viki_agent.turn_input import TurnInput

ADDED = "Added to the running turn."
BUSY = "Conversation is busy. Send your message again after the turn ends."
DROPPED = "A message you sent during the turn was not added to it. Send it again."
NOT_ADDED = "Your message was not added to the running turn. Send it again after the reply."
SHOWING = (
    "A request is being shown here. Answer it once it has appeared; this message was not sent to "
    "Claude."
)


class Steering(Driver):
    """A driver that takes steers; none ends until the test emits its SteerEnded."""

    def __init__(self, emit: Callable[[Event], None]) -> None:
        super().__init__(emit)
        self.steers: asyncio.Queue[tuple[str, str]] = asyncio.Queue()

    async def steer(self, turn: TurnInput, steer_id: str) -> None:
        self.steers.put_nowait((turn.text, steer_id))

    async def settle_steers(self, timeout_s: float) -> None:
        return None


def steering(rig: Rig) -> None:
    def make(_launch: Any, prefs: Prefs, emit: Callable[[Event], None]) -> Steering:
        assert prefs.mode == "default"
        made = Steering(emit)
        rig.drivers.append(made)
        return made

    rig.manager._make_driver = make


@pytest.fixture
async def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Rig]:
    value = Rig(tmp_path)
    steering(value)
    await value.start(monkeypatch)
    yield value
    await value.bot.close()
    if value.run is not None:
        await value.run
    await value.approvals.close()
    await value.manager.close_all()


async def chat(rig: Rig) -> tuple[SessionActor, Steering]:
    actor, driver = await rig.chat()  # The turn is still running.
    return actor, cast(Steering, driver)


def reads(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> asyncio.Queue[str]:
    """Each post id the bridge confirms, once _listen has bound its input."""
    seen: asyncio.Queue[str] = asyncio.Queue()
    original = rig.http.post

    async def read(post_id: str) -> dict[str, Any]:
        seen.put_nowait(post_id)
        return await original(post_id)

    monkeypatch.setattr(rig.http, "post", read)
    return seen


async def test_message_during_turn_steers(rig: Rig) -> None:
    _, driver = await chat(rig)
    thread = rig.bot._thread
    post = rig.post("Synthetic steer")
    assert await take(rig.http.sent) == (ADDED, post["id"])
    text, steer_id = await take(driver.steers)
    assert text == "Synthetic steer" and driver.sent.empty()
    driver.emit(SteerEnded(steer_id=steer_id, outcome="started"))
    driver.finish("Synthetic reply")
    assert await take(rig.http.sent) == ("Synthetic reply", thread)
    assert rig.bot._armed


async def test_steer_started_turn_posts_its_reply(rig: Rig) -> None:
    _, driver = await chat(rig)
    thread = rig.bot._thread
    rig.post("Synthetic steer")
    assert (await take(rig.http.sent))[0] == ADDED
    _, steer_id = await take(driver.steers)
    driver.finish("First reply")
    assert await take(rig.http.sent) == ("First reply", thread)
    # The steer was read only after that turn ended: the CLI runs it as a turn of its own.
    driver.emit(SteerEnded(steer_id=steer_id, outcome="started"))
    driver.busy = True
    driver.emit(TurnStarted(turn_id=str(uuid.uuid4())))
    driver.finish("Steered reply")
    assert await take(rig.http.sent) == ("Steered reply", thread)
    assert rig.bot._armed


async def test_message_after_turn_ended_keeps_refusal(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, driver = await chat(rig)
    entered, release = asyncio.Event(), asyncio.Event()
    original = rig.http.post

    async def paused(post_id: str) -> dict[str, Any]:
        entered.set()
        await release.wait()
        return await original(post_id)

    monkeypatch.setattr(rig.http, "post", paused)
    rig.post("Synthetic late steer")  # Arrives during the turn.
    async with asyncio.timeout(HANG_GUARD_S):
        await entered.wait()
    driver.finish()
    assert (await take(rig.http.sent))[0] == "Synthetic reply"
    release.set()
    assert (await take(rig.http.sent))[0] == BUSY
    assert driver.steers.empty() and driver.sent.empty() and rig.bot._armed


async def test_turn_ending_while_steer_waits_on_actor_starts_nothing(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    actor, driver = await chat(rig)
    seen = reads(rig, monkeypatch)
    await actor._lock.acquire()
    try:
        post = rig.post("Synthetic steer")
        assert await take(seen) == post["id"]  # Intake now waits on the actor lock.
        driver.finish()
        assert (await take(rig.http.sent))[0] == "Synthetic reply"
    finally:
        actor._lock.release()
    assert (await take(rig.http.sent))[0] == BUSY
    assert driver.steers.empty() and driver.sent.empty() and not actor.busy


async def test_turn_ends_during_root_context_await(rig: Rig) -> None:
    actor, driver = await chat(rig)
    thread = rig.bot._thread

    async def context(_session_id: str) -> str | None:
        driver.finish("First reply")  # The result frame lands while the folder context reads.
        await asyncio.sleep(0)
        return None

    actor._root_context = context
    post = rig.post("Synthetic steer")
    assert await take(rig.http.sent) == ("First reply", thread)
    assert await take(rig.http.sent) == (BUSY, post["id"])
    assert driver.steers.empty() and driver.sent.empty() and not actor.busy


async def test_message_after_stop_while_turn_winds_down(rig: Rig) -> None:
    actor, driver = await chat(rig)
    rig.post("!viki stop")
    assert (await take(rig.http.sent))[0] == "Stop requested for this conversation."
    assert actor.busy  # The CLI has not ended the interrupted turn yet.
    post = rig.post("Synthetic new instruction")
    assert await take(rig.http.sent) == (BUSY, post["id"])
    assert driver.steers.empty() and driver.sent.empty() and rig.bot._armed


async def test_max_steers_gives_busy_reply(rig: Rig) -> None:
    _, driver = await chat(rig)
    for index in range(MAX_STEERS):
        rig.post(f"Synthetic steer {index}")
        assert (await take(rig.http.sent))[0] == ADDED
    rig.post("Synthetic steer over the cap")
    assert (await take(rig.http.sent))[0] == BUSY
    assert driver.steers.qsize() == MAX_STEERS and rig.bot._armed


async def test_unsent_steer_says_so(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    actor, driver = await chat(rig)

    async def refused(**_: Any) -> CommandDispatchResult:
        return CommandDispatchResult("not_dispatched", "runtime_changed")

    monkeypatch.setattr(actor, "send_command", refused)
    rig.post("Synthetic steer")
    assert (await take(rig.http.sent))[0] == NOT_ADDED
    assert driver.steers.empty() and rig.bot._armed


async def test_dropped_steer_is_reported(rig: Rig) -> None:
    _, driver = await chat(rig)
    rig.post("Synthetic steer")
    assert (await take(rig.http.sent))[0] == ADDED
    _, steer_id = await take(driver.steers)
    driver.emit(SteerEnded(steer_id=steer_id, outcome="dropped"))
    assert await take(rig.http.sent) == (DROPPED, rig.bot._thread)
    assert rig.bot._armed


async def test_stop_leaves_claude_started_turn_unprojected(rig: Rig) -> None:
    _, driver = await chat(rig)
    driver.finish()
    await take(rig.http.sent)
    rig.post("!viki stop")
    assert (await take(rig.http.sent))[0] == "Stop requested for this conversation."
    driver.busy = True
    driver.emit(TurnStarted(turn_id=str(uuid.uuid4())))  # Claude starts a turn by itself.
    driver.ask()
    assert await take(driver.answers) == ("card", PermissionAnswer(False))
    driver.finish("Synthetic hidden reply")
    rig.post("!viki status")
    assert (await take(rig.http.sent))[0] == "VIKI Agent is ready."
    assert not rig.bot._cards and rig.bot._armed


async def test_one_waiting_card_means_an_answer(rig: Rig) -> None:
    _, driver = await chat(rig)
    await rig.nonce(driver)
    rig.post("allow")
    assert await take(driver.answers) == ("card", PermissionAnswer(True))
    assert (await take(rig.http.sent))[0] == "Allowed once. Choice receipt: delivered_locally."
    assert driver.steers.empty() and rig.bot._armed


async def test_message_while_a_card_is_being_shown_is_not_steered(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, driver = await chat(rig)
    seen = reads(rig, monkeypatch)
    rig.http.gate.clear()
    driver.ask()
    assert "synthetic hidden argument" in (await take(rig.http.sent))[0]  # Held on the gate.
    post = rig.post("allow")
    assert await take(seen) == post["id"]
    rig.http.gate.set()
    assert "!viki allow" in (await take(rig.http.sent))[0]
    assert await take(rig.http.sent) == (SHOWING, post["id"])
    assert driver.steers.empty() and driver.answers.empty() and driver.pending() == ["card"]
    assert not rig.approvals.service.list().approvals and rig.bot._armed


@pytest.mark.parametrize("provider", ["slack", "discord"])
async def test_other_providers_steer(tmp_path: Path, provider: Provider) -> None:
    rig = Rig(tmp_path)
    steering(rig)
    conversation = Conversation(rig, provider)
    try:
        await conversation.start()
        _, driver = await conversation.chat()
        post = conversation.channel.post("Synthetic steer")
        assert await take(conversation.channel.sent) == (ADDED, post.id)
        assert (await take(cast(Steering, driver).steers))[0] == "Synthetic steer"
        assert conversation.bot._armed
    finally:
        await conversation.close()
        await rig.approvals.close()
        await rig.manager.close_all()
