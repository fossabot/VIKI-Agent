"""The bridge's own question cards: a readable post, `!viki pick` and `!viki answer`."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from tests.messaging.test_mattermost_bot import Driver, Rig, take
from viki_agent.conversations.approvals import QuestionChoice
from viki_agent.drivers.base import PermissionAnswer, QuestionAnswer
from viki_agent.events import Question, QuestionAsked, QuestionOption

PICK = (
    "Pick: !viki pick {nonce} then one option number per question, in order. "
    "Join numbers with commas where several are allowed, like 1,3.\n"
)
OTHER = "Other: !viki answer {nonce} then your own words."


@pytest.fixture
async def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Rig]:
    value = Rig(tmp_path)
    await value.start(monkeypatch)
    yield value
    await value.bot.close()
    if value.run is not None:
        await value.run
    await value.approvals.close()
    await value.manager.close_all()


def two_questions() -> list[Question]:
    """A single-select question, then a multi-select one."""
    return [
        Question(
            header="Scope",
            question="Which branch?",
            options=[
                QuestionOption(label="main"),
                QuestionOption(label="dev", description="The dev worktree"),
            ],
        ),
        Question(
            header="",
            question="Which checks?",
            options=[QuestionOption(label=label) for label in ("lint", "types", "tests")],
            multi_select=True,
        ),
    ]


async def ask(rig: Rig, driver: Driver, questions: list[Question]) -> tuple[str, str, str]:
    """Emit an own question card; return its readable post, its instructions and its nonce."""
    driver.inputs["question"] = {
        "tool": "AskUserQuestion",
        "input": {"hidden": "synthetic hidden argument"},
    }
    driver.emit(QuestionAsked(tool_use_id="question", questions=questions))
    post, instructions = (await take(rig.http.sent))[0], (await take(rig.http.sent))[0]
    assert post.startswith("```text\nQuestion\n") and "synthetic hidden argument" not in post
    return post, instructions, instructions.split("!viki answer ")[1].split()[0]


async def refused(rig: Rig, text: str) -> None:
    rig.post(text)
    assert "refused" in (await take(rig.http.sent))[0].lower()


def recorded(rig: Rig) -> object:
    return rig.approvals.service.get(rig.approvals.service.list().approvals[0].choice_id).choice


async def test_pick_delivers_and_records_the_submit_map(rig: Rig) -> None:
    _, driver = await rig.chat()
    post, instructions, nonce = await ask(rig, driver, two_questions())
    assert post == (
        "```text\nQuestion\n"
        "Question 1 of 2: Scope - Which branch?\n"
        "  1. main\n"
        "  2. dev - The dev worktree\n"
        "Question 2 of 2: Which checks?\n"
        "  1. lint\n"
        "  2. types\n"
        "  3. tests\n"
        "  Pick one or more.\n```"
    )
    assert instructions == (PICK + OTHER).format(nonce=nonce)
    rig.post(f"!viki pick {nonce} 2 3,1")
    answers = {"Which branch?": "dev", "Which checks?": "lint, tests"}
    assert await take(driver.answers) == ("question", QuestionAnswer(answers, ""))
    assert "delivered_locally" in (await take(rig.http.sent))[0]
    assert recorded(rig) == QuestionChoice(answers, "")
    await refused(rig, f"!viki pick {nonce} 2 3,1")
    assert len(rig.approvals.service.list().approvals) == 1


async def test_malformed_picks_are_refused_and_the_nonce_still_works(rig: Rig) -> None:
    _, driver = await rig.chat()
    _, _, nonce = await ask(rig, driver, two_questions())
    # Missing, too few, too many, zero, out of range (single, multi), duplicate, two on a
    # single-select question, a fullwidth digit, a leading zero, a wrong separator.
    for picks in (
        "",
        "2",
        "2 1 1",
        "0 1",
        "3 1",
        "2 4",
        "2 1,1",
        "1,2 1",
        "\uff12 1",
        "02 1",
        "2 1;3",
    ):
        await refused(rig, f"!viki pick {nonce} {picks}".rstrip())
    assert driver.answers.empty() and not rig.approvals.service.list().approvals
    assert rig.bot._armed
    rig.post(f"!viki pick {nonce} 1 2")
    answers = {"Which branch?": "main", "Which checks?": "types"}
    assert await take(driver.answers) == ("question", QuestionAnswer(answers, ""))
    assert "delivered_locally" in (await take(rig.http.sent))[0]


async def test_pick_on_permission_and_allow_on_question_are_refused(rig: Rig) -> None:
    _, driver = await rig.chat()
    permission = await rig.nonce(driver)
    _, _, question = await ask(rig, driver, two_questions())
    await refused(rig, f"!viki pick {permission}")
    await refused(rig, f"!viki pick {permission} 1")
    for command in ("allow", "deny"):
        await refused(rig, f"!viki {command} {question}")
        await refused(rig, f"!viki {command} {question} 1 1")
    assert driver.answers.empty() and not rig.approvals.service.list().approvals
    rig.post(f"!viki allow {permission}")
    assert await take(driver.answers) == ("card", PermissionAnswer(True))
    assert "delivered_locally" in (await take(rig.http.sent))[0]
    rig.post(f"!viki pick {question} 1 1,2")
    answers = {"Which branch?": "main", "Which checks?": "lint, types"}
    assert await take(driver.answers) == ("question", QuestionAnswer(answers, ""))
    assert "delivered_locally" in (await take(rig.http.sent))[0]


async def test_answer_sends_own_words_as_the_reply(rig: Rig) -> None:
    _, driver = await rig.chat()
    _, _, nonce = await ask(rig, driver, two_questions())
    rig.post(f"!viki answer {nonce} Use the staging branch")
    reply = QuestionAnswer({}, "Use the staging branch")
    assert await take(driver.answers) == ("question", reply)
    assert "delivered_locally" in (await take(rig.http.sent))[0]
    assert recorded(rig) == QuestionChoice({}, "Use the staging branch")


async def test_question_without_options_offers_only_answer(rig: Rig) -> None:
    _, driver = await rig.chat()
    question = Question(header="", question="Anything else?", options=[])
    _, instructions, nonce = await ask(rig, driver, [question])
    assert instructions == OTHER.format(nonce=nonce)
    await refused(rig, f"!viki pick {nonce} 1")
    rig.post(f"!viki answer {nonce} Nothing else")
    assert await take(driver.answers) == ("question", QuestionAnswer({}, "Nothing else"))
    assert "delivered_locally" in (await take(rig.http.sent))[0]


async def test_japanese_question_is_readable_and_pickable(rig: Rig) -> None:
    _, driver = await rig.chat()
    question = Question(
        header="確認",
        question="どのブランチに入れますか",
        options=[
            QuestionOption(label="本番", description="main ブランチ"),
            QuestionOption(label="開発"),
        ],
    )
    post, _, nonce = await ask(rig, driver, [question])
    assert "1 of 1: 確認 - どのブランチに入れますか\n  1. 本番 - main ブランチ\n  2. 開発\n" in post
    assert "\\u" not in post
    rig.post(f"!viki pick {nonce} 2")
    answers = {"どのブランチに入れますか": "開発"}
    assert await take(driver.answers) == ("question", QuestionAnswer(answers, ""))
    assert "delivered_locally" in (await take(rig.http.sent))[0]


async def test_changed_projected_question_disarms_before_runner(rig: Rig) -> None:
    actor, driver = await rig.chat()
    _, _, nonce = await ask(rig, driver, two_questions())
    actor._stream.cards["question"].item["data"]["questions"][0]["question"] = "Changed"
    rig.post(f"!viki pick {nonce} 1 1")
    assert await take(driver.answers) == ("question", PermissionAnswer(False))
    assert not rig.approvals.service.list().approvals and not rig.bot._armed


async def test_help_names_pick_and_answer(rig: Rig) -> None:
    rig.post("!viki help")
    help_text = (await take(rig.http.sent))[0]
    assert "!viki pick with option numbers or !viki answer with your own words for a question" in (
        help_text
    )
