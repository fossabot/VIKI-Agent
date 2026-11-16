"""Desktop chats' question cards over a messaging DM after `!viki questions on`: the relay, its
limits, picks and answers, and that no bridge path ever denies a desktop card."""

from __future__ import annotations

import asyncio
import functools
import math
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from tests.messaging.test_mattermost_bot import HANG_GUARD_S, Driver, Rig, checkpoint, take
from tests.messaging.test_question_picks import OTHER, PICK, recorded, two_questions
from tests.messaging.test_remote_conversation import Conversation
from viki_agent.conversations.approval_runtime import ApprovalDispatchRejected
from viki_agent.conversations.approvals import (
    ApprovalError,
    ChoiceInput,
    NativeRequest,
    QuestionChoice,
)
from viki_agent.drivers.base import Answer, PermissionAnswer, QuestionAnswer
from viki_agent.events import Question, QuestionAsked, QuestionOption, RequestCancelled
from viki_agent.messaging import messaging_format
from viki_agent.messaging.mattermost_bot import _HELP
from viki_agent.messaging.mattermost_format import chat_text, literal_text
from viki_agent.session import SessionActor

ON = "Desktop chat questions are on until this bot stops or you send !viki questions off."
REFUSED = "Choice refused. Use a current displayed request and its matching command."
READY = "VIKI Agent is ready."
TOO_LONG = (
    "A desktop chat is waiting on a question that is too long to show here. Open VIKI Agent "
    "desktop to answer it."
)
FULLWIDTH_TWO = "\N{FULLWIDTH DIGIT TWO}"
# The map a desktop Submit builds for `!viki pick <nonce> 2 1` on two_questions().
PICKED = {"Which branch?": "dev", "Which checks?": "lint"}


class Desk:
    """Desktop chats on a rig's manager, outside every bridge. Every answer their drivers get is
    logged, so a test can show that no bridge path denied a desktop card."""

    def __init__(self, rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
        self.rig, self.monkeypatch = rig, monkeypatch
        self.answers: list[tuple[str, Answer]] = []

    async def chat(self) -> tuple[SessionActor, Driver]:
        session_id = str(uuid.uuid4())
        self.rig.state.remember(session_id, mode="default")
        actor = self.rig.manager.create(session_id)
        assert await actor.send("Desktop synthetic prompt")
        driver = self.rig.drivers[-1]
        assert await take(driver.sent) == "Desktop synthetic prompt"
        respond = driver.respond

        def logged(tool_use_id: str, answer: Answer) -> bool:
            self.answers.append((tool_use_id, answer))
            return respond(tool_use_id, answer)

        self.monkeypatch.setattr(driver, "respond", logged)
        return actor, driver

    def denied(self) -> list[str]:
        return [tool_id for tool_id, answer in self.answers if answer == PermissionAnswer(False)]


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


@pytest.fixture
async def desk(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Desk]:
    value = Desk(rig, monkeypatch)
    yield value
    # The bridge's own close is the last bridge path; only the app's shutdown after it
    # (close_all) may deny what still waits.
    await rig.bot.close()
    assert not value.denied()


@pytest.fixture
async def bridges(tmp_path: Path) -> AsyncIterator[tuple[Conversation, Conversation]]:
    """test_remote_conversation's two bridges on one rig, both observing every chat."""
    rig = Rig(tmp_path)
    first, second = Conversation(rig, "slack"), Conversation(rig, "discord")

    def observe(actor: SessionActor, envelope: dict[str, Any]) -> None:
        first.bot.observe(actor, envelope)
        second.bot.observe(actor, envelope)

    rig.manager.bind_observer(observe)
    try:
        await first.start()
        await second.start()
        yield first, second
    finally:
        await first.close()
        await second.close()
        await rig.approvals.close()
        await rig.manager.close_all()


def ask(
    driver: Driver,
    tool_id: str = "desk",
    questions: list[Question] | None = None,
    *,
    hidden: str = "synthetic hidden argument",
) -> None:
    """Raise a question card on a chat: its native input (the completeness gate), then its event."""
    driver.inputs[tool_id] = {"tool": "AskUserQuestion", "input": {"hidden": hidden}}
    driver.emit(
        QuestionAsked(
            tool_use_id=tool_id, questions=two_questions() if questions is None else questions
        )
    )


def one(question: str, *labels: str) -> list[Question]:
    return [
        Question(
            header="",
            question=question,
            options=[QuestionOption(label=label) for label in labels or ("Yes",)],
        )
    ]


async def shown(sent: asyncio.Queue[tuple[str, str]]) -> tuple[str, str, str]:
    """The next desktop card on a channel: its post, its instructions and its nonce."""
    post, post_root = await take(sent)
    instructions, root = await take(sent)
    assert post_root == root == ""  # Top-level, outside the bridge's own conversation.
    return post, instructions, instructions.split("!viki answer ")[1].split()[0]


async def on(rig: Rig) -> None:
    rig.post("!viki questions on")
    assert (await take(rig.http.sent))[0] == ON


async def refused(rig: Rig, text: str) -> None:
    rig.post(text)
    assert (await take(rig.http.sent))[0] == REFUSED


async def test_desktop_questions_stay_on_the_desktop_until_turned_on(rig: Rig, desk: Desk) -> None:
    _, driver = await desk.chat()
    ask(driver)
    rig.post("!viki status")
    assert (await take(rig.http.sent))[0] == READY
    assert not rig.bot._questions_on and not rig.bot._desktop
    assert driver.pending() == ["desk"] and driver.answers.empty()


async def test_new_question_is_one_readable_post_and_a_pick_answers_like_submit(
    rig: Rig, desk: Desk
) -> None:
    await on(rig)
    actor, driver = await desk.chat()
    questions = [
        Question(
            header="Confirm @all",
            question="Which branch <b>?",
            options=[
                QuestionOption(label="Production", description="main `branch`"),
                QuestionOption(label="Development"),
            ],
        ),
        two_questions()[1],
    ]
    ask(driver, questions=questions)
    post, instructions, nonce = await shown(rig.http.sent)
    assert post == literal_text(
        "Question from a desktop chat\n"
        "Question 1 of 2: Confirm @all - Which branch <b>?\n"
        "  1. Production - main `branch`\n"
        "  2. Development\n"
        "Question 2 of 2: Which checks?\n"
        "  1. lint\n"
        "  2. types\n"
        "  3. tests\n"
        "  Pick one or more."
    )
    body = post.removeprefix("```text\n").removesuffix("\n```")
    assert "Confirm" in body and "Production" in body and not set("`@<>") & set(body)
    assert instructions == (PICK + OTHER).format(nonce=nonce)
    rig.post(f"!viki pick {nonce} 2 3,1")
    answers = {"Which branch <b>?": "Development", "Which checks?": "lint, tests"}
    assert await take(driver.answers) == ("desk", QuestionAnswer(answers, ""))
    assert "delivered_locally" in (await take(rig.http.sent))[0]
    row = rig.approvals.service.get(rig.approvals.service.list().approvals[0].choice_id)
    assert row.choice == QuestionChoice(answers, "") and row.session_id == actor.session_id
    await refused(rig, f"!viki pick {nonce} 2 3,1")
    assert len(rig.approvals.service.list().approvals) == 1


async def test_malformed_picks_are_refused_and_answer_sends_own_words(rig: Rig, desk: Desk) -> None:
    await on(rig)
    _, driver = await desk.chat()
    ask(driver)
    _, _, nonce = await shown(rig.http.sent)
    # Too few or too many numbers, zero, out of range, a duplicate, two on a single-select
    # question, a fullwidth digit, a leading zero, then a permission card's commands.
    for picks in ("", "2", "2 1 1", "0 1", "3 1", "2 4", "2 1,1", "1,2 1", "02 1"):
        await refused(rig, f"!viki pick {nonce} {picks}".rstrip())
    await refused(rig, f"!viki pick {nonce} {FULLWIDTH_TWO} 1")
    await refused(rig, f"!viki allow {nonce}")
    await refused(rig, f"!viki deny {nonce}")
    assert rig.bot._armed and driver.answers.empty()
    assert not rig.approvals.service.list().approvals
    rig.post(f"!viki answer {nonce} Use the staging branch")
    assert await take(driver.answers) == ("desk", QuestionAnswer({}, "Use the staging branch"))
    assert "delivered_locally" in (await take(rig.http.sent))[0]
    assert recorded(rig) == QuestionChoice({}, "Use the staging branch")


async def test_waiting_questions_are_posted_once_when_turned_on(rig: Rig, desk: Desk) -> None:
    _, own = await rig.chat()
    own.ask("own", question=True)  # The bridge's own card stays its own.
    await take(rig.http.sent)
    await take(rig.http.sent)
    _, driver = await desk.chat()
    ask(driver, "first")
    driver.ask("permission")  # A desktop permission card stays on the desktop.
    _, other = await desk.chat()
    ask(other, "second", one("Ship it?", "Yes", "No"))
    rig.post("!viki questions on")
    assert (await take(rig.http.sent))[0] == ON
    first_post, _, _ = await shown(rig.http.sent)
    second_post, _, nonce = await shown(rig.http.sent)
    assert "Which branch?" in first_post and "Ship it?" in second_post
    rig.post("!viki questions on")
    assert (await take(rig.http.sent))[0] == "Desktop chat questions are already on."
    driver.ask("later-permission")
    own.ask("own-later", question=True)
    assert "Which?" in (await take(rig.http.sent))[0]
    assert "!viki answer" in (await take(rig.http.sent))[0]
    rig.post("!viki help")
    assert (await take(rig.http.sent))[0] == chat_text(_HELP)  # Nothing else was posted.
    assert sorted(card.tool_id for card in rig.bot._desktop.values()) == ["first", "second"]
    assert sorted(card.tool_id for card in rig.bot._cards.values()) == ["own", "own-later"]
    rig.post(f"!viki pick {nonce} 1")
    assert await take(other.answers) == ("second", QuestionAnswer({"Ship it?": "Yes"}, ""))
    assert "delivered_locally" in (await take(rig.http.sent))[0]
    assert driver.pending() == ["first", "permission", "later-permission"]


async def test_desktop_nonce_has_no_timer(rig: Rig, desk: Desk) -> None:
    await on(rig)
    _, driver = await desk.chat()
    ask(driver)
    _, _, nonce = await shown(rig.http.sent)
    card = rig.bot._desktop[nonce]
    assert card.deadline == math.inf
    rig.now = 10_000.0
    rig.post(f"!viki pick {nonce} 2 1")
    assert await take(driver.answers) == ("desk", QuestionAnswer(PICKED, ""))
    assert "delivered_locally" in (await take(rig.http.sent))[0]
    assert rig.bot._armed


async def test_off_then_on_posts_the_question_again_with_a_fresh_nonce(
    rig: Rig, desk: Desk
) -> None:
    await on(rig)
    _, driver = await desk.chat()
    ask(driver)
    first_post, _, old = await shown(rig.http.sent)
    rig.post("!viki questions off")
    assert (await take(rig.http.sent))[0] == "Desktop chat questions are off."
    await on(rig)
    post, _, nonce = await shown(rig.http.sent)
    assert post == first_post and nonce != old
    await refused(rig, f"!viki pick {old} 2 1")
    rig.post(f"!viki pick {nonce} 2 1")
    assert await take(driver.answers) == ("desk", QuestionAnswer(PICKED, ""))
    assert "delivered_locally" in (await take(rig.http.sent))[0]


@pytest.mark.parametrize("end", ["answered", "cancelled", "turn", "session", "replaced", "off"])
async def test_ended_desktop_question_refuses_its_nonce_and_stays_armed(
    rig: Rig, desk: Desk, end: str
) -> None:
    await on(rig)
    actor, driver = await desk.chat()
    ask(driver)
    _, _, nonce = await shown(rig.http.sent)
    if end == "answered":  # At the desk, through the app's own runner, like a Submit.
        native = NativeRequest(**actor._native_requests["desk"])
        choice = QuestionChoice({"Which branch?": "main", "Which checks?": "tests"}, "")
        value = ChoiceInput(
            str(uuid.uuid4()), actor.session_id, actor.generation, "desk", native, choice
        )
        assert (await rig.approvals.decide(value))[0].state == "delivered_locally"
        await take(driver.answers)
    elif end == "cancelled":
        driver.emit(RequestCancelled(tool_use_id="desk"))
    elif end == "turn":
        driver.finish("Desktop reply must stay local")
    elif end == "session":
        await rig.manager.close(actor.session_id)
    elif end == "replaced":
        rig.manager._actors.pop(actor.session_id)
        rig.manager.open(actor.session_id, profile=False)
    else:
        rig.post("!viki questions off")
        assert (await take(rig.http.sent))[0] == "Desktop chat questions are off."
    await refused(rig, f"!viki pick {nonce} 2 1")
    assert rig.bot._armed and driver.answers.empty()
    assert len(rig.approvals.service.list().approvals) == int(end == "answered")
    if end == "replaced":
        await actor.close()


@pytest.mark.parametrize("path", ["new", "stop", "disarm", "disconnect", "output", "close"])
async def test_bridge_paths_never_deny_a_desktop_card(rig: Rig, desk: Desk, path: str) -> None:
    _, own = await rig.chat()
    await rig.nonce(own)  # The bridge's own permission card, which each path here denies.
    await on(rig)
    actor, driver = await desk.chat()
    ask(driver)
    _, _, nonce = await shown(rig.http.sent)
    if path in {"new", "stop"}:
        rig.post(f"!viki {path}")
        assert (await take(own.answers))[1] == PermissionAnswer(False)
        await take(rig.http.sent)
        rig.post(f"!viki pick {nonce} 2 1")
        assert await take(driver.answers) == ("desk", QuestionAnswer(PICKED, ""))
        assert "delivered_locally" in (await take(rig.http.sent))[0]
        assert rig.bot._armed
    else:
        if path == "disarm":
            rig.bot.disarm()
        elif path == "disconnect":
            rig.incoming.put_nowait(RuntimeError("Synthetic WebSocket disconnect"))
        elif path == "output":
            rig.http.failure = True
            rig.post("!viki status")
        else:
            await rig.bot.close()
        assert (await take(own.answers))[1] == PermissionAnswer(False)
        assert rig.run is not None
        async with asyncio.timeout(HANG_GUARD_S):
            await rig.run
        assert not rig.bot._questions_on and not rig.bot._desktop
        assert rig.manager.get(actor.session_id) is actor and actor.live
        assert driver.pending() == ["desk"] and driver.answers.empty()
    assert not desk.denied()


async def test_new_while_a_desktop_card_is_in_flight_keeps_its_nonce(rig: Rig, desk: Desk) -> None:
    await on(rig)
    _, driver = await desk.chat()
    rig.http.gate.clear()
    ask(driver)
    post, _ = await take(rig.http.sent)  # Its send waits on the gate.
    assert post.startswith("```text\nQuestion from a desktop chat\n")
    epoch = rig.bot._epoch
    rig.post("!viki new")
    async with asyncio.timeout(HANG_GUARD_S):
        while rig.bot._epoch == epoch:
            await checkpoint()
    rig.http.gate.set()
    instructions, _ = await take(rig.http.sent)
    assert instructions.startswith("Pick: !viki pick ")
    assert "new conversation" in (await take(rig.http.sent))[0]
    nonce = instructions.split("!viki answer ")[1].split()[0]
    rig.post(f"!viki pick {nonce} 2 1")
    assert await take(driver.answers) == ("desk", QuestionAnswer(PICKED, ""))
    assert "delivered_locally" in (await take(rig.http.sent))[0]


@pytest.mark.parametrize("change", ["projected", "native"])
async def test_changed_desktop_question_disarms_and_stays_on_the_desktop(
    rig: Rig, desk: Desk, change: str
) -> None:
    await on(rig)
    actor, driver = await desk.chat()
    ask(driver)
    _, _, nonce = await shown(rig.http.sent)
    if change == "projected":
        actor._stream.cards["desk"].item["data"]["questions"][0]["question"] = "Changed"
    else:
        details = driver.inputs["desk"]
        assert details is not None
        details["input"]["hidden"] = "Changed after display"
    rig.post(f"!viki pick {nonce} 1 1")
    async with asyncio.timeout(HANG_GUARD_S):
        await rig.bot._stopped.wait()
    assert not rig.bot._armed and not rig.approvals.service.list().approvals
    assert driver.pending() == ["desk"] and driver.answers.empty()


@pytest.mark.parametrize(
    ("error", "armed"),
    [
        (ApprovalError("already_decided"), True),
        (ApprovalDispatchRejected("stale_request"), True),
        (OSError("synthetic"), False),
    ],
    ids=["approval", "dispatch", "other"],
)
async def test_runner_refusal_on_a_desktop_card_keeps_the_bridge(
    rig: Rig, desk: Desk, monkeypatch: pytest.MonkeyPatch, error: Exception, armed: bool
) -> None:
    await on(rig)
    _, driver = await desk.chat()
    ask(driver)
    _, _, nonce = await shown(rig.http.sent)

    async def refuse(_value: ChoiceInput) -> Any:
        raise error

    monkeypatch.setattr(rig.approvals, "decide", refuse)
    rig.post(f"!viki pick {nonce} 1 1")
    if armed:
        message = (await take(rig.http.sent))[0]
        assert message == "Choice not recorded. Check VIKI Agent desktop."
        assert rig.bot._armed
        await refused(rig, f"!viki pick {nonce} 1 1")
    else:
        async with asyncio.timeout(HANG_GUARD_S):
            await rig.bot._stopped.wait()
    assert driver.pending() == ["desk"] and driver.answers.empty()


async def test_runner_refusal_on_an_own_question_still_disarms(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, driver = await rig.chat()
    nonce = await rig.nonce(driver, question=True)

    async def refuse(_value: ChoiceInput) -> Any:
        raise ApprovalError("already_decided")

    monkeypatch.setattr(rig.approvals, "decide", refuse)
    rig.post(f"!viki answer {nonce} Synthetic Other")
    assert (await take(driver.answers))[1] == PermissionAnswer(False)
    assert not rig.bot._armed


async def test_ninth_held_desktop_card_is_not_posted(rig: Rig, desk: Desk) -> None:
    await on(rig)
    _, driver = await desk.chat()
    for index in range(9):
        ask(driver, f"desk-{index}", one(f"Question {index}?"))
    nonces = [(await shown(rig.http.sent))[2] for _ in range(8)]
    rig.post("!viki status")
    assert (await take(rig.http.sent))[0] == READY
    assert len(rig.bot._desktop) == 8 and rig.bot._armed
    rig.post(f"!viki pick {nonces[0]} 1")
    assert await take(driver.answers) == ("desk-0", QuestionAnswer({"Question 0?": "Yes"}, ""))
    assert "delivered_locally" in (await take(rig.http.sent))[0]
    ask(driver, "tenth", one("Question 10?"))  # The answered card's place is free again.
    assert "Question 10?" in (await shown(rig.http.sent))[0]
    assert len(rig.bot._desktop) == 8 and "desk-8" in driver.pending()


async def test_full_output_skips_a_desktop_question_without_disarm(rig: Rig, desk: Desk) -> None:
    await on(rig)
    _, driver = await desk.chat()
    rig.http.gate.clear()
    rig.bot._enqueue("x" * (12 * 1024), "")
    await take(rig.http.sent)
    for _ in range(31):
        rig.bot._enqueue("x" * (12 * 1024), "")
    assert rig.bot._output_count == 32
    ask(driver)
    assert rig.bot._armed and not rig.bot._desktop
    rig.http.gate.set()
    for _ in range(31):
        await take(rig.http.sent)
    rig.post("!viki status")
    assert (await take(rig.http.sent))[0] == READY
    assert rig.bot._armed and driver.pending() == ["desk"]


@pytest.mark.parametrize("too_long", ["native", "readable"])
async def test_undisplayable_desktop_question_posts_a_notice_without_nonce(
    rig: Rig, desk: Desk, too_long: str
) -> None:
    await on(rig)
    _, driver = await desk.chat()
    if too_long == "native":  # Over the 8 KiB native JSON gate.
        ask(driver, hidden="x" * 8192)
    else:  # Over the 12 KiB readable post.
        ask(driver, questions=one("Long?", "x" * (12 * 1024)))
    assert await take(rig.http.sent) == (TOO_LONG, "")
    rig.post("!viki status")
    assert (await take(rig.http.sent))[0] == READY
    assert rig.bot._armed and not rig.bot._desktop and driver.pending() == ["desk"]


async def test_long_desktop_question_is_split_for_discord(
    rig: Rig, desk: Desk, monkeypatch: pytest.MonkeyPatch
) -> None:
    render = functools.partial(messaging_format.render, "discord")
    monkeypatch.setattr(rig.bot.channel, "render", render)
    await on(rig)
    _, driver = await desk.chat()
    ask(driver, questions=one("Which one?", "option " * 400, "none"))
    parts: list[str] = []
    while not (message := (await take(rig.http.sent))[0]).startswith("Pick: "):
        parts.append(message)
    assert len(parts) == 2 and all(len(part) <= 2000 for part in parts)
    assert parts[0].startswith("Part 1 of 2\n```text\nQuestion from a desktop chat\n")
    nonce = message.split("!viki answer ")[1].split()[0]
    rig.post(f"!viki pick {nonce} 2")
    assert await take(driver.answers) == ("desk", QuestionAnswer({"Which one?": "none"}, ""))


async def test_two_bridges_relay_one_question_and_the_first_pick_wins(
    bridges: tuple[Conversation, Conversation], monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = bridges
    desk = Desk(first.rig, monkeypatch)
    for bridge in (first, second):
        bridge.channel.post("!viki questions on")
        assert (await take(bridge.channel.sent))[0] == ON
    _, own = await second.chat()  # A bridge conversation's question stays with its bridge.
    ask(own, "own", one("Bridge only?"))
    own_post, _ = await take(second.channel.sent)
    assert own_post.startswith("```text\nQuestion\n") and "Bridge only?" in own_post
    await take(second.channel.sent)
    _, driver = await desk.chat()
    ask(driver)
    first_post, _, first_nonce = await shown(first.channel.sent)
    second_post, _, second_nonce = await shown(second.channel.sent)
    assert first_post == second_post and "Which branch?" in first_post
    first.channel.post(f"!viki pick {second_nonce} 2 1")  # Another bridge's nonce.
    assert (await take(first.channel.sent))[0] == REFUSED
    second.channel.post(f"!viki pick {second_nonce} 2 1")
    assert await take(driver.answers) == ("desk", QuestionAnswer(PICKED, ""))
    assert "delivered_locally" in (await take(second.channel.sent))[0]
    first.channel.post(f"!viki pick {first_nonce} 1 1")
    assert (await take(first.channel.sent))[0] == REFUSED
    assert first.bot._armed and second.bot._armed
    assert len(first.rig.approvals.service.list().approvals) == 1
    await first.close()
    await second.close()
    assert not desk.denied() and not driver.pending()


async def test_help_and_the_questions_command_grammar(rig: Rig) -> None:
    rig.post("!viki help")
    lines = (await take(rig.http.sent))[0].split("\n")
    assert lines[1] == (
        "!viki help | !viki status | !viki new | !viki stop | !viki questions on | "
        "!viki questions off"
    )
    assert lines[4] == (
        "!viki questions on also sends questions from your desktop chats here until this bot "
        "stops or you send !viki questions off; those stay answerable with their commands while "
        "they wait."
    )
    assert lines[5].startswith("Requests wait until you answer them")
    for text in ("!viki questions", "!viki questions maybe", "!viki questions on now"):
        rig.post(text)
        assert (await take(rig.http.sent))[0] == "Command refused. Use !viki help."
    rig.post("!viki questions ON")
    assert (await take(rig.http.sent))[0] == "Command refused. Use !viki help."
    assert not rig.bot._questions_on
    rig.post("!viki questions off")
    assert (await take(rig.http.sent))[0] == "Desktop chat questions are off."


async def test_observe_skips_repeats_and_strangers_and_never_raises(
    rig: Rig, desk: Desk, monkeypatch: pytest.MonkeyPatch
) -> None:
    await on(rig)
    actor, driver = await desk.chat()
    ask(driver)
    await shown(rig.http.sent)
    envelope: dict[str, Any] = {
        "type": "question.asked",
        "session_id": actor.session_id,
        "generation": actor.generation,
        "data": {"tool_use_id": "desk"},
        "native_request": dict(actor._native_requests["desk"]),
    }
    changes: list[dict[str, Any]] = [
        {},  # The same question again.
        {"type": "text.delta"},
        {"session_id": str(uuid.uuid4())},
        {"generation": str(uuid.uuid4())},
        {"native_request": None},
    ]
    for change in changes:
        rig.bot.observe(actor, {**envelope, **change})
    rig.post("!viki status")
    assert (await take(rig.http.sent))[0] == READY
    assert len(rig.bot._desktop) == 1

    def fail(*_: object) -> None:
        raise RuntimeError("synthetic relay failure")

    monkeypatch.setattr(rig.bot, "_relay", fail)
    rig.bot.observe(actor, envelope)
    assert not rig.bot._armed and driver.pending() == ["desk"] and driver.answers.empty()


async def test_desktop_posts_leave_half_the_output_budget_to_the_own_conversation(
    rig: Rig, desk: Desk, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Several long waiting desktop questions, swept on `!viki questions on` while sends are
    slow, queue only within half the shared output budget, so the own conversation's next card
    still fits and the bridge is never disarmed for want of room."""
    monkeypatch.setattr(
        rig.bot.channel, "render", functools.partial(messaging_format.render, "discord")
    )
    _, own = await rig.chat()  # The bridge's own conversation is running.
    for index in range(7):
        _, driver = await desk.chat()
        ask(driver, f"q{index}", one(f"Long {index}?", "word " * 1500, "none"), hidden="x" * 7600)
    rig.http.gate.clear()  # A slow (rate-limited) provider: nothing drains the output queue yet.
    rig.post("!viki questions on")
    assert (await take(rig.http.sent))[0] == ON
    for _ in range(5):
        await checkpoint()
    own.ask("own-permission")  # The own conversation's next card: two messages.
    for _ in range(5):
        await checkpoint()
    assert rig.bot._armed, "desktop relay filled the shared output budget; own card disarmed"
    denied = own.answers.get_nowait() if own.answers.qsize() else None
    assert denied != ("own-permission", PermissionAnswer(False))


async def test_pick_while_the_desktop_chat_closes_is_refused_and_stays_armed(
    rig: Rig, desk: Desk
) -> None:
    """SessionActor.close sets _closed and then awaits the driver interrupt; in that window
    request_pending is still True but remote_permission turns None. A pick arriving in that
    window must be refused as stale, not treated as a changed request that disarms."""
    _, own = await rig.chat()
    await rig.nonce(own)  # The bridge's own card: would be wrongly denied if this disarmed.
    await on(rig)
    actor, driver = await desk.chat()
    ask(driver)
    _, _, nonce = await shown(rig.http.sent)
    driver.interrupt_gate.clear()
    closing = asyncio.create_task(rig.manager.close(actor.session_id))
    await driver.interrupting.wait()  # close() has set _closed and is now awaiting the interrupt.
    rig.post(f"!viki pick {nonce} 2 1")
    for _ in range(20):
        await checkpoint()
    assert rig.bot._armed, "a pick during a desktop chat's close disarmed the bridge"
    assert (await take(rig.http.sent))[0] == "Choice not recorded. Check VIKI Agent desktop."
    assert driver.pending() == ["desk"] and driver.answers.empty()
    denied = own.answers.get_nowait() if own.answers.qsize() else None
    assert denied != ("card", PermissionAnswer(False))
    driver.interrupt_gate.set()
    await closing


async def test_off_drops_queued_desktop_posts(rig: Rig, desk: Desk) -> None:
    """A desktop card's post, already queued ahead of `!viki questions off`'s own reply,
    must be dropped by the desktop branch of `_stale` once off lands, so the off reply is the
    very next post the channel sees."""
    _, driver = await desk.chat()
    ask(driver)
    rig.http.gate.clear()  # Holds the first send so the card's two parts queue behind it.
    rig.post("!viki questions on")
    assert (await take(rig.http.sent))[0] == ON
    assert rig.bot._output_count == 3
    rig.post("!viki questions off")
    while rig.bot._questions_on:
        await checkpoint()
    rig.http.gate.set()
    assert (await take(rig.http.sent))[0] == "Desktop chat questions are off."


async def test_desktop_pick_without_current_details_is_refused(rig: Rig, desk: Desk) -> None:
    """A desktop card whose native details grew past the 8 KiB completeness gate after it
    was posted (the actor itself is not closing, unlike the close race) has no current details
    to revalidate against: the pick is refused, never decided, and the bridge stays armed."""
    await on(rig)
    _, driver = await desk.chat()
    ask(driver)
    _, _, nonce = await shown(rig.http.sent)
    details = driver.inputs["desk"]
    assert details is not None
    details["input"]["hidden"] = "x" * 8192  # Past the 8 KiB native-JSON completeness gate.
    rig.post(f"!viki pick {nonce} 2 1")
    assert (await take(rig.http.sent))[0] == "Choice not recorded. Check VIKI Agent desktop."
    assert rig.bot._armed
    assert driver.pending() == ["desk"] and driver.answers.empty()
    assert not rig.approvals.service.list().approvals
