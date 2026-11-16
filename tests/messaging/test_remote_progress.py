"""Mattermost progress calls wired into the bridge: a recording fake shares one log with sends."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

import httpx2
import pytest

from tests.messaging.test_mattermost_bot import (
    BINDING,
    CONFIG,
    HANG_GUARD_S,
    Rig,
    checkpoint,
    take,
)
from tests.messaging.test_remote_conversation import Conversation
from tests.messaging.test_remote_steer import Steering, reads, steering
from viki_agent.drivers.base import PermissionAnswer
from viki_agent.events import TextDone, ThinkingDone, TurnDone, TurnStarted, Usage
from viki_agent.messaging import mattermost_bot as module
from viki_agent.messaging.mattermost_format import chat_text
from viki_agent.messaging.mattermost_http import MattermostHttp, ProgressError, ProgressHttp

Call = tuple[str, ...]
READY = "VIKI Agent is ready."
STOPPED = "Stop requested for this conversation."
ADDED = "Added to the running turn."
DENIED = "Denied. Choice receipt: delivered_locally."
PERMISSION_HINT = (
    "A permission request is waiting. Reply allow or deny to answer it; this message was not "
    "sent to Claude." + module._REACT_HINT
)
SEVERAL = (
    "Several requests are waiting, so this message answered none of them and was not sent to "
    "Claude. Use the command shown under the request you mean." + module._REACT_HINT
)
# Markup and a bidi override that only chat_text makes safe.
NOTE = "Synthetic note @channel ![x](https://h/p.png) ‮ end"


def nonce(sent: tuple[str, str]) -> str:
    return sent[0].split("!viki allow ")[1].split()[0]


class FakeProgress:
    """Records each progress call in the shared log and on a queue the test awaits."""

    def __init__(self, log: list[Call]) -> None:
        self.log = log
        self.calls: asyncio.Queue[Call] = asyncio.Queue()
        self.bound: tuple[str, str] | None = None
        self.fail = False
        self.closed = False

    def _record(self, *call: str) -> None:
        self.log.append(call)
        self.calls.put_nowait(call)
        if self.fail:
            raise ProgressError

    def bind(self, bot_user_id: str, channel_id: str) -> None:
        self.bound = (bot_user_id, channel_id)

    async def react(self, post_id: str, emoji: str) -> None:
        self._record("react", post_id, emoji)

    async def unreact(self, post_id: str) -> None:
        self._record("unreact", post_id)

    async def typing(self, parent_id: str) -> None:
        self._record("typing", parent_id)

    async def create(self, message: str, root_id: str) -> str:
        self._record("create", message, root_id)
        return "u" * 26

    async def edit(self, post_id: str, message: str) -> None:
        self._record("edit", post_id, message)

    async def close(self) -> None:
        self.closed = True


class ProgressRig(Rig):
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        super().__init__(tmp_path)
        self.log: list[Call] = []
        self.progress = FakeProgress(self.log)
        self.bot = module.MattermostBot(
            CONFIG,
            cast(MattermostHttp, self.http),
            self.manager,
            self.approvals,
            create_actor=self.create,
            clock=lambda: self.now,
            progress_http=cast(ProgressHttp, self.progress),
        )
        original = self.http.send

        async def send(binding: Any, message: str, root_id: str = "") -> str:
            sent = await original(binding, message, root_id)
            self.log.append(("sent", message))
            return sent

        monkeypatch.setattr(self.http, "send", send)

    async def call(self) -> Call:
        return await take(self.progress.calls)


@pytest.fixture
async def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[ProgressRig]:
    value = ProgressRig(tmp_path, monkeypatch)
    await value.start(monkeypatch)
    yield value
    await value.bot.close()
    if value.run is not None:
        await value.run
    await value.approvals.close()
    await value.manager.close_all()
    assert value.progress.closed


async def started(rig: ProgressRig) -> str:
    """Start a turn; return the prompt's id once 👀 and the first typing call are made."""
    post = rig.post("Synthetic prompt")
    assert await rig.call() == ("react", post["id"], "eyes")
    assert await rig.call() == ("typing", post["id"])
    return post["id"]


async def test_eyes_before_the_actor_starts(
    rig: ProgressRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered, gate = asyncio.Event(), asyncio.Event()

    async def held() -> Any:
        rig.log.append(("factory",))
        entered.set()
        await gate.wait()
        return await rig.create()

    monkeypatch.setattr(rig.bot, "_create_actor", held)
    post = rig.post("Synthetic prompt")
    async with asyncio.timeout(HANG_GUARD_S):
        await entered.wait()
    assert rig.log.index(("react", post["id"], "eyes")) < rig.log.index(("factory",))
    gate.set()
    assert await take(rig.created) is not None
    assert await rig.call() == ("react", post["id"], "eyes")
    assert await rig.call() == ("typing", post["id"])  # No second 👀 when the turn starts.
    assert rig.progress.bound == (BINDING.bot_user_id, BINDING.channel_id)


async def test_outcome_follows_the_final_reply(rig: ProgressRig) -> None:
    prompt = await started(rig)
    rig.http.gate.clear()
    rig.drivers[-1].finish()
    assert await take(rig.http.sent) == ("Synthetic reply", prompt)  # Held on the gate.
    rig.now = 100.0
    assert rig.bot._progress is not None
    assert await rig.bot._progress.step() is None  # Nothing may overtake the final reply.
    assert rig.progress.calls.empty()
    rig.http.gate.set()
    assert await rig.call() == ("unreact", prompt)
    assert await rig.call() == ("react", prompt, "white_check_mark")
    assert rig.log.index(("sent", "Synthetic reply")) < rig.log.index(("unreact", prompt))
    assert rig.bot._armed


async def test_progress_waits_for_authority_output(rig: ProgressRig) -> None:
    prompt = await started(rig)
    rig.http.gate.clear()
    rig.drivers[-1].ask()
    assert "synthetic hidden argument" in (await take(rig.http.sent))[0]  # Held on the gate.
    rig.now = 30.0
    assert rig.bot._progress is not None
    rig.bot._progress.wake()
    assert await rig.bot._progress.step() is None
    assert rig.progress.calls.empty()
    rig.http.gate.set()
    assert "!viki allow" in (await take(rig.http.sent))[0]
    # The card now waits: no typing, and the status reply says so.
    call = await rig.call()
    assert call[0] == "create" and call[2] == prompt
    assert call[1] == chat_text("Waiting for you · 30s · 0 steps")
    assert rig.bot._output_count == 0 and rig.bot._output_bytes == 0


async def test_stop_marks_the_prompt_stopped(rig: ProgressRig) -> None:
    prompt = await started(rig)
    rig.post("!viki stop")
    assert (await take(rig.http.sent))[0] == STOPPED
    assert await rig.call() == ("unreact", prompt)
    assert await rig.call() == ("react", prompt, "black_square_for_stop")
    assert rig.bot._armed


async def test_error_status_warns(rig: ProgressRig) -> None:
    prompt = await started(rig)
    driver = rig.drivers[-1]
    driver.emit(TextDone(block_id="final", text="Synthetic partial reply"))
    driver.busy = False
    driver.emit(TurnDone(status="error", usage=Usage()))
    assert (await take(rig.http.sent))[0] == "Synthetic partial reply"
    assert await rig.call() == ("unreact", prompt)
    assert await rig.call() == ("react", prompt, "warning")
    assert rig.bot._armed


async def test_progress_failure_never_disarms(rig: ProgressRig) -> None:
    rig.progress.fail = True
    post = rig.post("Synthetic prompt")
    assert await rig.call() == ("react", post["id"], "eyes")
    # The turn's own first call (👀 again, or typing) fails too: progress ends for this turn.
    assert (await rig.call())[0] in {"react", "typing"}
    rig.now = 100.0
    assert rig.bot._progress is not None
    rig.bot._progress.wake()
    assert await rig.bot._progress.step() is None
    assert rig.progress.calls.empty()
    assert rig.bot._armed
    assert rig.bot._output_count == 0 and rig.bot._output_bytes == 0
    rig.drivers[-1].finish()
    assert (await take(rig.http.sent))[0] == "Synthetic reply"
    assert await rig.call() == ("react", post["id"], "white_check_mark")  # Fails too.
    rig.post("!viki status")
    assert (await take(rig.http.sent))[0] == READY
    assert rig.bot._armed
    assert rig.bot._output_count == 0 and rig.bot._output_bytes == 0


async def test_status_command_returns_the_line(rig: ProgressRig) -> None:
    await started(rig)
    rig.now = 65.0
    rig.post("!viki status")
    assert (await take(rig.http.sent))[0] == chat_text("Working · 1m 05s · 0 steps · working")
    await rig.nonce(rig.drivers[-1])
    rig.post("!viki status")
    assert (await take(rig.http.sent))[0] == chat_text("Waiting for you · 1m 05s · 0 steps")
    rig.drivers[-1].finish()
    assert (await take(rig.http.sent))[0] == "Synthetic reply"
    rig.post("!viki status")
    assert (await take(rig.http.sent))[0] == READY


def reacted(rig: ProgressRig, post_id: str) -> bool:
    """A reaction on the post was made or is still queued."""
    assert rig.bot._progress is not None
    return post_id in rig.bot._progress._accepted or any(
        call[0] in {"react", "unreact"} and call[1] == post_id for call in rig.log
    )


async def test_status_of_a_turn_that_is_not_posted_is_the_busy_line(rig: ProgressRig) -> None:
    prompt = await started(rig)
    driver = rig.drivers[-1]
    driver.finish()
    assert await take(rig.http.sent) == ("Synthetic reply", prompt)
    rig.post("!viki stop")
    assert (await take(rig.http.sent))[0] == STOPPED
    driver.busy = True
    driver.emit(TurnStarted(turn_id=str(uuid.uuid4())))  # Claude starts a turn by itself.
    driver.emit(TextDone(block_id="hidden", text="Synthetic hidden note"))
    rig.post("!viki status")
    assert (await take(rig.http.sent))[0] == "VIKI Agent conversation is busy."
    assert rig.bot._armed


async def test_steer_gets_the_text_receipt_and_no_reaction(rig: ProgressRig) -> None:
    steering(rig)
    prompt = await started(rig)
    steer = rig.post("Synthetic steer")
    assert await take(rig.http.sent) == (ADDED, steer["id"])
    assert (await take(cast(Steering, rig.drivers[-1]).steers))[0] == "Synthetic steer"
    rig.drivers[-1].finish()
    assert await take(rig.http.sent) == ("Synthetic reply", prompt)
    assert await rig.call() == ("unreact", prompt)
    assert await rig.call() == ("react", prompt, "white_check_mark")
    assert not reacted(rig, steer["id"]) and rig.bot._armed


async def test_steer_gets_the_text_receipt_while_progress_fails(rig: ProgressRig) -> None:
    steering(rig)
    await started(rig)
    rig.progress.fail = True
    steer = rig.post("Synthetic steer")
    assert await take(rig.http.sent) == (ADDED, steer["id"])
    assert (await take(cast(Steering, rig.drivers[-1]).steers))[0] == "Synthetic steer"
    assert not reacted(rig, steer["id"]) and rig.bot._armed


async def test_steer_gets_the_text_receipt_when_the_turn_ends_first(rig: ProgressRig) -> None:
    steering(rig)
    prompt = await started(rig)
    rig.http.gate.clear()
    rig.post("!viki help")
    assert (await take(rig.http.sent))[0] == chat_text(module._HELP)  # Held on the gate.
    steer = rig.post("Synthetic steer")
    assert (await take(cast(Steering, rig.drivers[-1]).steers))[0] == "Synthetic steer"
    rig.drivers[-1].finish()  # The turn ends before progress could act on the steer.
    rig.http.gate.set()
    assert await take(rig.http.sent) == (ADDED, steer["id"])
    assert await take(rig.http.sent) == ("Synthetic reply", prompt)
    assert await rig.call() == ("unreact", prompt)
    assert await rig.call() == ("react", prompt, "white_check_mark")
    assert not reacted(rig, steer["id"]) and rig.bot._armed


async def test_status_text_is_escaped(rig: ProgressRig) -> None:
    prompt = await started(rig)
    driver = rig.drivers[-1]
    driver.emit(TextDone(block_id="note", text=NOTE))
    rig.now = 20.0
    raw = rig.bot._status.line(rig.now)
    assert "@channel" in raw and "‮" in raw  # The note reaches the line unescaped.
    assert rig.bot._progress is not None
    rig.bot._progress.wake()
    assert await rig.call() == ("typing", prompt)
    assert await rig.call() == ("create", chat_text(raw), prompt)
    assert "@channel" not in chat_text(raw) and "‮" not in chat_text(raw)
    rig.now = 25.0
    driver.finish()
    assert (await take(rig.http.sent))[1] == prompt
    call = await rig.call()
    assert call[0] == "edit" and call[2] == chat_text(rig.bot._status.line(rig.now))
    assert "@channel" not in call[2] and "‮" not in call[2]


async def test_only_an_accepted_prompt_gets_eyes(
    rig: ProgressRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    prompt = await started(rig)
    driver = rig.drivers[-1]
    confirmed = reads(rig, monkeypatch)
    replied: list[str] = []

    async def reply(text: str, expected: str) -> None:
        post = rig.post(text)
        assert await take(rig.http.sent) == (expected, post["id"])
        replied.append(post["id"])

    # A plain message while a card is still being posted.
    rig.http.gate.clear()
    driver.ask("first")
    assert "synthetic hidden argument" in (await take(rig.http.sent))[0]
    showing = rig.post("allow")
    assert await take(confirmed) == showing["id"]
    rig.http.gate.set()
    first = nonce(await take(rig.http.sent))
    assert await take(rig.http.sent) == (module._SHOWING, showing["id"])
    replied.append(showing["id"])
    # One permission waits: a hint. Two wait: neither is answered.
    await reply("maybe", PERMISSION_HINT)
    driver.ask("second")
    await take(rig.http.sent)
    second = nonce(await take(rig.http.sent))
    rig.post("!viki help")  # Its reply follows the card's last post, so both cards wait.
    assert (await take(rig.http.sent))[0] == chat_text(module._HELP)
    await reply("hello", SEVERAL)
    rig.post(f"!viki deny {second}")
    assert (await take(rig.http.sent))[0] == DENIED
    assert first != second
    # One waits again: plain text answers it.
    await reply("deny", DENIED)
    assert [await take(driver.answers) for _ in range(2)] == [
        ("second", PermissionAnswer(False)),
        ("first", PermissionAnswer(False)),
    ]
    # After a stop the turn winds down: busy.
    rig.post("!viki stop")
    assert (await take(rig.http.sent))[0] == STOPPED
    await reply("Synthetic message", module._BUSY)
    assert not any(reacted(rig, post_id) for post_id in replied)
    assert [call for call in rig.log if call[-1] == "eyes"] == [("react", prompt, "eyes")]


async def test_stop_before_the_first_turn_marks_the_prompt_stopped(
    rig: ProgressRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered, gate = asyncio.Event(), asyncio.Event()

    async def held() -> Any:
        entered.set()
        await gate.wait()
        return await rig.create()

    monkeypatch.setattr(rig.bot, "_create_actor", held)
    post = rig.post("Synthetic prompt")
    async with asyncio.timeout(HANG_GUARD_S):
        await entered.wait()
    assert await rig.call() == ("react", post["id"], "eyes")
    rig.post("!viki stop")
    assert (await take(rig.http.sent))[0] == STOPPED
    assert await rig.call() == ("unreact", post["id"])
    assert await rig.call() == ("react", post["id"], "black_square_for_stop")
    assert rig.bot._turn_prompt is None and rig.bot._armed


async def test_typing_resumes_when_a_card_is_answered_elsewhere(rig: ProgressRig) -> None:
    progress = rig.bot._progress
    assert progress is not None
    progress._status_interval_s = 1e6  # Only typing can come due.
    prompt = await started(rig)
    driver = rig.drivers[-1]
    await rig.nonce(driver)  # The card waits: no typing.
    # The worker has taken the wake that followed the card's last post.
    for _ in range(100):
        if rig.bot._output_count == 0 and not progress._wake.is_set():
            break
        await checkpoint()
    else:
        raise AssertionError("progress never settled")
    rig.now = 10.0
    driver.respond("card", PermissionAnswer(False))  # Answered outside the bridge.
    driver.emit(ThinkingDone(block_id="synthetic", text="", duration_ms=0))
    assert await rig.call() == ("typing", prompt)
    assert rig.bot._output_count == 0 and rig.bot._armed


async def test_non_mattermost_bridges_have_no_progress(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    assert rig.bot._progress is None  # The stub client is not a MattermostHttp.
    conversation = Conversation(rig, "slack")
    try:
        await conversation.start()
        assert conversation.bot._progress is None
        _, driver = await conversation.chat()
        rig.now = 65.0
        conversation.channel.post("!viki status")
        assert (await take(conversation.channel.sent))[0] == chat_text(
            "Working · 1m 05s · 0 steps · working"
        )
        driver.finish()
        assert (await take(conversation.channel.sent))[0] == "Synthetic reply"
        assert conversation.bot._armed
    finally:
        await conversation.close()
        await rig.approvals.close()
        await rig.manager.close_all()


async def test_a_real_client_gets_progress() -> None:
    http = MattermostHttp(CONFIG, transport=httpx2.MockTransport(lambda _: httpx2.Response(500)))
    bot = module.MattermostBot(
        CONFIG,
        http,
        cast(Any, None),
        cast(Any, None),
        create_actor=cast(Any, None),
    )
    try:
        assert bot._progress is not None
    finally:
        await bot.close()
