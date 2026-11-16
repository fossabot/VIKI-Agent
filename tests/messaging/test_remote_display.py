"""Expanded posts keep real actor approval and notice authority behind complete delivery."""

from __future__ import annotations

import asyncio
import math
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from tests.messaging.test_mattermost_bot import HANG_GUARD_S, Driver, Rig, take
from tests.messaging.test_remote_conversation import Channel, Conversation
from viki_agent.conversations import approval_runtime
from viki_agent.drivers.base import PermissionAnswer
from viki_agent.events import PermissionRequested, QuestionAsked, RequestCancelled
from viki_agent.messaging.mattermost_bot import _STOP_LINES
from viki_agent.messaging.mattermost_format import chat_text, literal_text


class DisplayChannel(Channel):
    def __init__(self) -> None:
        super().__init__("discord")
        self.count = 0
        self.pause_at = 0
        self.fail_at = 0
        self.release = asyncio.Event()

    def render(self, formatted: str) -> tuple[str, ...]:
        from viki_agent.messaging.messaging_format import render

        return render("discord", formatted)

    async def send(self, part: str, reply_to: str) -> None:
        self.count += 1
        self.sent.put_nowait((part, reply_to))
        if self.count == self.pause_at:
            await self.release.wait()
        if self.count == self.fail_at:
            raise ValueError("synthetic send failure")


@pytest.fixture
async def display(tmp_path: Path) -> AsyncIterator[Conversation]:
    rig = Rig(tmp_path)
    value = Conversation(rig, "discord")
    channel = DisplayChannel()
    value.channel, value.bot.channel = channel, channel
    try:
        await value.start()
        channel.count = 0
        yield value
    finally:
        channel.release.set()
        await value.close()
        await rig.approvals.close()
        await rig.manager.close_all()


def large_card(driver: Driver, *, question: bool = False) -> None:
    driver.inputs["card"] = {"tool": "Edit", "input": {"hidden": "x" * 7000}}
    driver.emit(
        QuestionAsked(tool_use_id="card", questions=[])
        if question
        else PermissionRequested(tool_use_id="card", kind="tool", tool="Edit", summary="Edit")
    )


@pytest.mark.parametrize("terminal", ["failure", "disable"])
async def test_partial_detail_never_activates_allow(display: Conversation, terminal: str) -> None:
    _, driver = await display.chat()
    channel = display.channel
    assert isinstance(channel, DisplayChannel)
    channel.pause_at = 2
    channel.fail_at = 2 if terminal == "failure" else 0
    large_card(driver)
    assert display.bot._armed
    first, second = (await take(channel.sent))[0], (await take(channel.sent))[0]
    assert "!viki allow" not in first + second
    card = next(iter(display.bot._cards.values()))
    assert card.deadline is None
    if terminal == "disable":
        display.bot.disarm()
    channel.release.set()
    assert await take(driver.answers) == ("card", PermissionAnswer(False))
    await display.close()
    assert await take(channel.sent) == (
        channel.render(chat_text(_STOP_LINES["connection_lost"]))[0],
        "",
    )
    assert channel.sent.empty() and not display.bot._cards
    assert not display.rig.approvals.service.list().approvals
    assert display.bot._output_count == display.bot._output_bytes == 0


async def test_expanded_bundle_is_admitted_atomically(
    display: Conversation, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, driver = await display.chat()
    channel = display.channel
    assert isinstance(channel, DisplayChannel)
    channel.pause_at = 1
    assert display.bot._enqueue(literal_text("occupied"), "")
    await take(channel.sent)
    for _ in range(27):
        assert display.bot._enqueue(literal_text("queued"), "")
    assert display.bot._output_count == 28
    queued: list[str] = []
    original = display.bot._output.put_nowait

    def observed(output: Any) -> None:
        queued.append(output.message)
        original(output)

    monkeypatch.setattr(display.bot._output, "put_nowait", observed)
    large_card(driver)  # Four detail parts plus instructions exceed the four remaining slots.
    assert queued == [] and not display.bot._armed
    assert await take(driver.answers) == ("card", PermissionAnswer(False))


async def test_card_is_rechecked_after_each_send_wait(display: Conversation) -> None:
    actor, driver = await display.chat()
    channel = display.channel
    assert isinstance(channel, DisplayChannel)
    channel.pause_at = 1
    large_card(driver)
    await take(channel.sent)
    actor._emit(RequestCancelled(tool_use_id="card"))
    channel.release.set()
    display.bot._reply("Barrier output", "")
    assert "Barrier output" in (await take(channel.sent))[0]
    assert channel.sent.empty() and not display.bot._cards and display.bot._armed


@pytest.mark.parametrize("question", [False, True])
async def test_undisplayable_request_has_no_allow_authority(
    display: Conversation, monkeypatch: pytest.MonkeyPatch, question: bool
) -> None:
    _, driver = await display.chat()
    original = display.channel.render

    def refuse_literal(formatted: str) -> tuple[str, ...]:
        if formatted.startswith(("```json\n", "```text\n")):
            raise ValueError("synthetic render refusal")
        return original(formatted)

    monkeypatch.setattr(display.channel, "render", refuse_literal)
    large_card(driver, question=question)
    if question:
        assert not display.bot._armed
        assert await take(driver.answers) == ("card", PermissionAnswer(False))
    else:
        message = (await take(display.channel.sent))[0]
        assert "!viki allow" not in message and "!viki deny" in message
        nonce = message.split("!viki deny ")[1].split()[0]
        display.channel.post(f"!viki deny {nonce}")
        assert await take(driver.answers) == ("card", PermissionAnswer(False))
        assert "delivered_locally" in (await take(display.channel.sent))[0]


@pytest.mark.parametrize("failure", [False, True])
async def test_split_notice_receipt_waits_for_every_part(
    display: Conversation, failure: bool
) -> None:
    channel = display.channel
    assert isinstance(channel, DisplayChannel)
    channel.pause_at = 2
    channel.fail_at = 2 if failure else 0

    async def allowed() -> bool:
        return True

    key = display.bot.destination_key()
    assert key is not None
    task = asyncio.create_task(
        display.bot.send_notice("x" * 7000, destination_key=key, current=allowed)
    )
    await take(channel.sent)
    await take(channel.sent)
    assert not task.done()
    channel.release.set()
    assert await task == ("unknown" if failure else "sent")
    if not failure:
        await take(channel.sent)
        await take(channel.sent)
        assert channel.count == 4 and channel.sent.empty()


async def test_complete_display_is_revoked_during_recorded_allow(
    display: Conversation, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, driver = await display.chat()
    large_card(driver)
    messages = [(await take(display.channel.sent))[0] for _ in range(5)]
    nonce = messages[-1].split("!viki allow ")[1].split()[0]
    card = display.bot._cards[nonce]
    assert card.deadline == math.inf
    original = approval_runtime.storage_call
    entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls = 0

    async def paused(operation: Any, observer: Any = None) -> Any:
        nonlocal calls
        result = await original(operation, observer)
        calls += 1
        if calls == 3:
            entered.set()
            await release.wait()
        if getattr(result, "state", "") == "not_delivered":
            finished.set()
        return result

    monkeypatch.setattr(approval_runtime, "storage_call", paused)
    display.channel.post(f"!viki allow {nonce}")
    try:
        async with asyncio.timeout(HANG_GUARD_S):
            await entered.wait()
        display.bot.disarm()
        assert await take(driver.answers) == ("card", PermissionAnswer(False))
    finally:
        release.set()
    async with asyncio.timeout(HANG_GUARD_S):
        await finished.wait()
    assert display.rig.approvals.service.list().approvals[0].state == "not_delivered"
    assert driver.answers.empty()


async def test_stop_revokes_remaining_expanded_output(display: Conversation) -> None:
    _, driver = await display.chat()
    large_card(driver)
    for _ in range(5):
        await take(display.channel.sent)
    channel = display.channel
    assert isinstance(channel, DisplayChannel)
    channel.count, channel.pause_at = 0, 1
    display.bot._reply("x" * 7000, "")
    await take(channel.sent)
    display.bot.disarm()
    assert await take(driver.answers) == ("card", PermissionAnswer(False))
    await display.close()
    assert await take(channel.sent) == (
        channel.render(chat_text(_STOP_LINES["connection_lost"]))[0],
        "",
    )
    assert channel.sent.empty() and not display.bot._cards
    assert display.bot._output_count == display.bot._output_bytes == 0


async def test_conversation_change_drops_old_continuation_parts(display: Conversation) -> None:
    await display.chat()
    channel = display.channel
    assert isinstance(channel, DisplayChannel)
    channel.pause_at = 1
    display.bot._reply("x" * 7000, "")
    await take(channel.sent)
    await display.bot._control("stop", "")
    channel.release.set()
    assert "Stop requested" in (await take(channel.sent))[0]
    assert channel.sent.empty() and display.bot._armed
