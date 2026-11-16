"""Plain-text answers to the one own request waiting, and own requests that never expire."""

from __future__ import annotations

import asyncio
import dataclasses
import math
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from tests.messaging.test_desktop_questions import Desk, on, shown
from tests.messaging.test_desktop_questions import ask as desk_ask
from tests.messaging.test_mattermost_bot import Driver, Rig, take
from tests.messaging.test_question_picks import two_questions
from tests.messaging.test_remote_conversation import Conversation
from tests.messaging.test_remote_steer import reads
from viki_agent.conversations.approvals import PermissionChoice, QuestionChoice
from viki_agent.drivers.base import PermissionAnswer, QuestionAnswer
from viki_agent.events import PermissionRequested, QuestionAsked, RequestCancelled
from viki_agent.messaging import mattermost_bot as module
from viki_agent.messaging.messaging_channel import Provider

GONE = (
    "That request is no longer waiting, so this message answered nothing and was not sent to "
    "Claude."
)
SEVERAL = (
    "Several requests are waiting, so this message answered none of them and was not sent to "
    "Claude. Use the command shown under the request you mean. On a permission request you can "
    "also react with a thumbs up to allow it once or a thumbs down to deny it."
)
HINT = (
    "A permission request is waiting. Reply allow or deny to answer it; this message was not "
    "sent to Claude. On a permission request you can also react with a thumbs up to allow it "
    "once or a thumbs down to deny it."
)
NO_ALLOW = (
    "Allow once is not available here because this request was too long to show completely. "
    "Reply deny, or answer it in VIKI Agent desktop."
)
PICKED = {"Which branch?": "dev", "Which checks?": "lint, tests"}


@pytest.fixture
async def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Rig]:
    value = Rig(tmp_path)
    value.manager.bind_observer(value.bot.observe)
    await value.start(monkeypatch)
    yield value
    await value.bot.close()
    if value.run is not None:
        await value.run
    await value.approvals.close()
    await value.manager.close_all()


def choices(rig: Rig) -> list[object]:
    """Every recorded choice, in a fixed order (the store lists rows by random choice id)."""
    service = rig.approvals.service
    return sorted((service.get(row.choice_id).choice for row in service.list().approvals), key=repr)


async def question(rig: Rig, driver: Driver, tool_id: str) -> str:
    """Show an own two-question card; return its nonce."""
    driver.inputs[tool_id] = {"tool": "AskUserQuestion", "input": {"hidden": "synthetic"}}
    driver.emit(QuestionAsked(tool_use_id=tool_id, questions=two_questions()))
    await take(rig.http.sent)
    return (await take(rig.http.sent))[0].split("!viki answer ")[1].split()[0]


async def test_plain_text_answers_the_one_waiting_question(rig: Rig) -> None:
    _, driver = await rig.chat()  # The turn is still running: an answer is not a prompt.
    await question(rig, driver, "first")
    rig.post(" 2 3,1 ")
    assert await take(driver.answers) == ("first", QuestionAnswer(PICKED, ""))
    assert (await take(rig.http.sent))[0] == (
        "Picked: dev; lint, tests. Choice receipt: delivered_locally."
    )
    await question(rig, driver, "second")
    rig.post("  Neither, use main  ")
    assert await take(driver.answers) == ("second", QuestionAnswer({}, "Neither, use main"))
    assert (await take(rig.http.sent))[0] == (
        "Answered in your own words. Choice receipt: delivered_locally."
    )
    expected = [QuestionChoice(PICKED, ""), QuestionChoice({}, "Neither, use main")]
    assert choices(rig) == sorted(expected, key=repr)
    assert driver.sent.empty() and driver.answers.empty() and rig.bot._armed


@pytest.mark.parametrize("text", ["allow", " Allow\n", "DENY", "deny "])
async def test_plain_allow_and_deny(rig: Rig, text: str) -> None:
    _, driver = await rig.chat()
    await rig.nonce(driver)
    rig.post(text)
    allow = text.strip().lower() == "allow"
    assert await take(driver.answers) == ("card", PermissionAnswer(allow))
    message, root = await take(rig.http.sent)
    label = "Allowed once." if allow else "Denied."
    assert message == f"{label} Choice receipt: delivered_locally." and root
    assert choices(rig) == [PermissionChoice(allow)]
    assert driver.sent.empty() and driver.answers.empty() and rig.bot._armed


async def test_permission_hint_and_incomplete_allow_reach_no_driver(rig: Rig) -> None:
    _, driver = await rig.chat()
    driver.inputs["card"] = None  # Too long to show: the card offers Deny only.
    driver.emit(PermissionRequested(tool_use_id="card", kind="tool", tool="Edit", summary="Tiny"))
    assert "!viki allow" not in (await take(rig.http.sent))[0]
    rig.post("please allow it")
    assert (await take(rig.http.sent))[0] == HINT
    rig.post(" ALLOW ")
    assert (await take(rig.http.sent))[0] == NO_ALLOW
    assert driver.answers.empty() and driver.sent.empty() and not choices(rig)
    assert driver.pending() == ["card"] and rig.bot._armed
    rig.post("deny")
    assert await take(driver.answers) == ("card", PermissionAnswer(False))
    assert (await take(rig.http.sent))[0] == "Denied. Choice receipt: delivered_locally."
    assert choices(rig) == [PermissionChoice(False)]


async def test_own_cards_wait_without_expiry(rig: Rig) -> None:
    _, driver = await rig.chat()
    nonce = await rig.nonce(driver)
    card = rig.bot._cards[nonce]
    assert card.deadline == math.inf and card.thread == rig.bot._thread
    rig.now = 10**6
    rig.post(f"!viki allow {nonce}")
    assert await take(driver.answers) == ("card", PermissionAnswer(True))
    assert (await take(rig.http.sent))[0] == "Allowed once. Choice receipt: delivered_locally."
    nonce = await question(rig, driver, "question")
    rig.now = 2 * 10**6
    rig.post(f"!viki pick {nonce} 2 1,3")
    assert await take(driver.answers) == ("question", QuestionAnswer(PICKED, ""))
    assert (await take(rig.http.sent))[0].startswith("Picked: dev; lint, tests.")
    assert rig.bot._armed


async def test_several_waiting_requests_answer_none(rig: Rig) -> None:
    _, driver = await rig.chat()
    await rig.nonce(driver)
    driver.ask("second")
    await take(rig.http.sent)
    await take(rig.http.sent)
    rig.post("allow")
    assert (await take(rig.http.sent))[0] == SEVERAL
    assert driver.answers.empty() and driver.sent.empty() and not choices(rig)
    assert driver.pending() == ["card", "second"] and rig.bot._armed


@pytest.mark.parametrize("gone", ["cancelled", "new", "replaced"])
async def test_card_gone_since_arrival_answers_nothing(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, gone: str
) -> None:
    actor, driver = await rig.chat()
    await rig.nonce(driver)
    entered, release = asyncio.Event(), asyncio.Event()
    original = rig.http.post

    async def paused(post_id: str) -> dict[str, Any]:
        entered.set()
        await release.wait()
        return await original(post_id)

    monkeypatch.setattr(rig.http, "post", paused)
    rig.post("allow")  # Bound to the one card waiting when it arrives.
    try:
        async with asyncio.timeout(0.05):
            await entered.wait()
        if gone in ("cancelled", "replaced"):
            actor._emit(RequestCancelled(tool_use_id="card"))
        if gone == "replaced":
            driver.ask("second")  # A new card; the message never saw it.
            await take(rig.http.sent)
            await take(rig.http.sent)
        if gone == "new":
            await rig.bot._control("new", "")
            assert await take(driver.answers) == ("card", PermissionAnswer(False))
            assert "new conversation" in (await take(rig.http.sent))[0]
    finally:
        release.set()
    assert (await take(rig.http.sent))[0] == GONE
    assert driver.answers.empty() and driver.sent.empty() and not choices(rig)
    assert rig.created.empty() and rig.bot._armed


async def test_revalidation_failure_stops_with_safety_check(rig: Rig) -> None:
    _, driver = await rig.chat()
    await rig.nonce(driver)
    assert driver.inputs["card"] is not None
    driver.inputs["card"]["input"]["complete"] = "Changed after display"
    rig.post("allow")
    assert await take(driver.answers) == ("card", PermissionAnswer(False))
    assert not rig.bot._armed and rig.bot.stop_reason == "safety_check"
    assert not choices(rig)


async def test_desktop_question_is_not_a_waiting_own_request(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    desk = Desk(rig, monkeypatch)
    await on(rig)
    _, desk_driver = await desk.chat()
    desk_ask(desk_driver)
    await shown(rig.http.sent)
    _, driver = await rig.chat()  # With only a desktop card waiting, plain text is a prompt.
    await rig.nonce(driver)
    post = rig.post("deny")  # An own card waits beside the desktop one: this answers neither.
    assert await take(rig.http.sent) == (SEVERAL, post["id"])
    assert driver.answers.empty() and driver.pending() == ["card"]
    assert desk_driver.answers.empty() and desk_driver.pending() == ["desk"]
    assert len(rig.bot._desktop) == 1 and not choices(rig) and rig.bot._armed


@pytest.mark.parametrize("thread", ["", "desktop"])
async def test_plain_picks_beside_a_desktop_question_answer_nothing(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, thread: str
) -> None:
    desk = Desk(rig, monkeypatch)
    await on(rig)
    _, desk_driver = await desk.chat()
    desk_ask(desk_driver)
    await shown(rig.http.sent)  # The desktop question is shown first.
    _, driver = await rig.chat()
    await question(rig, driver, "own")  # The own question is shown second.
    # As a reply in the desktop question's own thread too (the fake gives every bot post the
    # id "s" * 26).
    post = rig.post("2 1", root_id="s" * 26 if thread else "")
    assert await take(rig.http.sent) == (SEVERAL, post["root_id"] or post["id"])
    assert driver.answers.empty() and driver.pending() == ["own"]
    assert desk_driver.answers.empty() and desk_driver.pending() == ["desk"]
    assert driver.sent.empty() and not choices(rig) and rig.bot._armed


async def test_plain_allow_beside_an_own_card_being_posted_answers_nothing(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, driver = await rig.chat()
    await rig.nonce(driver)  # Card A is shown.
    confirmed = reads(rig, monkeypatch)
    rig.http.gate.clear()
    driver.ask("second")
    assert "synthetic hidden argument" in (await take(rig.http.sent))[0]  # B's detail, held.
    post = rig.post("allow")
    assert await take(confirmed) == post["id"]  # Read while B is still being posted.
    rig.http.gate.set()
    second = (await take(rig.http.sent))[0].split("!viki allow ")[1].split()[0]
    assert await take(rig.http.sent) == (SEVERAL, post["id"])
    assert driver.answers.empty() and driver.pending() == ["card", "second"]
    assert not choices(rig) and rig.bot._armed
    # With B answered by its command, A is the only request again: plain text answers it.
    rig.post(f"!viki deny {second}")
    assert await take(driver.answers) == ("second", PermissionAnswer(False))
    assert (await take(rig.http.sent))[0] == "Denied. Choice receipt: delivered_locally."
    rig.post("allow")
    assert await take(driver.answers) == ("card", PermissionAnswer(True))
    assert (await take(rig.http.sent))[0] == "Allowed once. Choice receipt: delivered_locally."


async def test_desktop_question_answered_while_being_posted_does_not_block_answers(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    desk = Desk(rig, monkeypatch)
    await on(rig)
    _, driver = await rig.chat()
    await question(rig, driver, "own")  # The own question is shown.
    _, desk_driver = await desk.chat()
    rig.http.gate.clear()
    desk_ask(desk_driver)
    assert "Which branch?" in (await take(rig.http.sent))[0]  # Its first post, held.
    # Answered on the desktop first: its card is never fully posted and is not pruned.
    desk_driver.respond("desk", QuestionAnswer({}, "Synthetic desktop answer"))
    rig.http.gate.set()
    rig.post("Synthetic own words")
    assert await take(driver.answers) == ("own", QuestionAnswer({}, "Synthetic own words"))
    assert (await take(rig.http.sent))[0] == (
        "Answered in your own words. Choice receipt: delivered_locally."
    )
    assert rig.bot._armed


async def test_plain_picks_beside_a_desktop_question_being_posted_answer_nothing(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    desk = Desk(rig, monkeypatch)
    await on(rig)
    _, driver = await rig.chat()
    await question(rig, driver, "own")  # The own question is shown.
    _, desk_driver = await desk.chat()
    confirmed = reads(rig, monkeypatch)
    rig.http.gate.clear()
    desk_ask(desk_driver)  # Relayed: its first post is held.
    assert "Which branch?" in (await take(rig.http.sent))[0]
    post = rig.post("1 1")
    assert await take(confirmed) == post["id"]  # Read while the desktop card is being posted.
    rig.http.gate.set()
    assert "!viki answer" in (await take(rig.http.sent))[0]
    assert await take(rig.http.sent) == (SEVERAL, post["id"])
    assert driver.answers.empty() and driver.pending() == ["own"]
    assert desk_driver.answers.empty() and desk_driver.pending() == ["desk"]
    assert not choices(rig) and rig.bot._armed


async def test_command_receipt_names_the_choice_and_points_to_desktop_when_unsure(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, driver = await rig.chat()
    nonce = await rig.nonce(driver)
    original = rig.approvals.decide

    async def unknown(value: Any) -> Any:
        summary, created = await original(value)
        return dataclasses.replace(summary, state="unknown"), created

    monkeypatch.setattr(rig.approvals, "decide", unknown)
    rig.post(f"!viki deny {nonce}")
    await take(driver.answers)
    assert (await take(rig.http.sent))[0] == (
        "Denied. Choice receipt: unknown. Check VIKI Agent desktop."
    )


def test_picked_label_collapses_whitespace_and_is_cut() -> None:
    assert module._label(QuestionChoice({"a": " one \n two", "b": "three  "}, "")) == (
        "Picked: one two; three."
    )
    long = module._label(QuestionChoice({"a": "x" * 300}, ""))
    assert long == "Picked: " + "x" * 200 + "….", long


def test_help_names_plain_answers_and_no_expiry() -> None:
    assert module._HELP.split("\n") == [
        "Send a message to start a dedicated VIKI Agent conversation. While Claude is working, a "
        "new message is added to the running turn.",
        "!viki help | !viki status | !viki new | !viki stop | !viki questions on | "
        "!viki questions off",
        "When a request from this conversation is the only request waiting, answer it in plain "
        "text: allow or deny for a permission, or option numbers or your own words for a "
        "question. On Mattermost, a thumbs-up or thumbs-down reaction on a permission request "
        "allows it once or denies it.",
        "Each request also shows its own commands: !viki allow or !viki deny for a permission, "
        "and !viki pick with option numbers or !viki answer with your own words for a question.",
        "!viki questions on also sends questions from your desktop chats here until this bot "
        "stops or you send !viki questions off; those stay answerable with their commands while "
        "they wait.",
        "Requests wait until you answer them or send !viki stop or !viki new. If this bot stops, "
        "it says why here when it can; turn it on again in Messaging. Workspace invalidation "
        "requires an app restart. Messages sent offline are not backfilled.",
    ]


@pytest.mark.parametrize("provider", ["slack", "discord"])
async def test_other_providers_answer_in_plain_text(tmp_path: Path, provider: Provider) -> None:
    rig = Rig(tmp_path)
    conversation = Conversation(rig, provider)
    try:
        await conversation.start()
        _, driver = await conversation.chat()
        await conversation.nonce(driver)
        conversation.channel.post("Allow")
        assert await take(driver.answers) == ("card", PermissionAnswer(True))
        receipt = (await take(conversation.channel.sent))[0]
        assert receipt == "Allowed once. Choice receipt: delivered_locally."
        driver.ask("question", question=True)
        await take(conversation.channel.sent)
        await take(conversation.channel.sent)
        conversation.channel.post("Synthetic own words")
        assert await take(driver.answers) == ("question", QuestionAnswer({}, "Synthetic own words"))
        await take(conversation.channel.sent)
        expected = [PermissionChoice(True), QuestionChoice({}, "Synthetic own words")]
        assert choices(rig) == sorted(expected, key=repr)
        assert driver.sent.empty() and conversation.bot._armed
    finally:
        await conversation.close()
        await rig.approvals.close()
        await rig.manager.close_all()
