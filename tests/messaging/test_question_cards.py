"""Pure rendering (`question_post`) and parsing of the bridge owner's picks (`pickable`,
`pick_answers`)."""

from __future__ import annotations

import re
from typing import Any

import pytest

from viki_agent.messaging import messaging_format, question_cards
from viki_agent.messaging.messaging_channel import Provider


def _option(label: str, description: str = "") -> dict[str, str]:
    return {"label": label, "description": description}


def _question(
    header: str = "",
    question: str = "Pick one",
    options: list[dict[str, str]] | None = None,
    multi_select: bool = False,
) -> dict[str, Any]:
    return {
        "header": header,
        "question": question,
        "options": [_option("Yes")] if options is None else options,
        "multi_select": multi_select,
    }


def _item(questions: list[dict[str, Any]]) -> dict[str, Any]:
    return {"type": "question.asked", "data": {"tool_use_id": "t1", "questions": questions}}


def _two_question_item() -> dict[str, Any]:
    return _item(
        [
            _question(question="Single", options=[_option("A"), _option("B"), _option("C")]),
            _question(
                question="Multi",
                options=[_option("X"), _option("Y"), _option("Z")],
                multi_select=True,
            ),
        ]
    )


def _unfence(post: str) -> str:
    assert post.startswith("```text\n") and post.endswith("\n```")
    return post[len("```text\n") : -len("\n```")]


def _part_body(part: str) -> str:
    """The chunk inside one `Part X of Y` ```text post, as `render` produces it."""
    part = re.sub(r"^Part \d+ of \d+\n", "", part)
    return _unfence(part)


# --- question_post ----------------------------------------------------------------------------


def test_question_post_title_desktop_vs_own() -> None:
    item = _item([_question(question="Q1")])
    own = _unfence(question_cards.question_post(item, desktop=False)).splitlines()[0]
    desktop = _unfence(question_cards.question_post(item, desktop=True)).splitlines()[0]
    assert own == "Question"
    assert desktop == "Question from a desktop chat"


def test_question_post_formats_header_options_and_multi_select() -> None:
    item = _item(
        [
            _question(
                header="Scope",
                question="Which files?",
                options=[_option("All", "Every file"), _option("Some")],
            ),
            _question(
                question="Which colors?",
                options=[_option("Red"), _option("Blue", "Cool")],
                multi_select=True,
            ),
        ]
    )
    body = _unfence(question_cards.question_post(item, desktop=False))
    assert body == "\n".join(
        [
            "Question",
            "Question 1 of 2: Scope - Which files?",
            "  1. All - Every file",
            "  2. Some",
            "Question 2 of 2: Which colors?",
            "  1. Red",
            "  2. Blue - Cool",
            "  Pick one or more.",
        ]
    )


def test_question_post_no_questions() -> None:
    body = _unfence(question_cards.question_post(_item([]), desktop=False))
    assert body == "Question\nNo questions are listed."


@pytest.mark.parametrize("gap", ["\n", "\u2028", "  \n  "])
def test_question_post_collapses_whitespace_so_a_field_cannot_start_a_line(gap: str) -> None:
    item = _item(
        [
            _question(
                header=f"H{gap}eader",
                question=f"Q{gap}uestion",
                options=[_option(f"La{gap}bel", f"De{gap}sc")],
            )
        ]
    )
    body = _unfence(question_cards.question_post(item, desktop=False))
    assert body.split("\n") == [
        "Question",
        "Question 1 of 1: H eader - Q uestion",
        "  1. La bel - De sc",
    ]


def test_question_post_blank_header_and_description_are_dropped() -> None:
    item = _item([_question(header="   ", question="Q", options=[_option("A", "  ")])])
    body = _unfence(question_cards.question_post(item, desktop=False))
    assert body == "Question\nQuestion 1 of 1: Q\n  1. A"


def test_question_post_keeps_japanese_and_neutralizes_markup() -> None:
    rtl_override = chr(0x202E)  # named by code point, not typed raw, since it reorders text
    item = _item(
        [
            _question(
                header="日本語",
                question=f"続けますか? @all <b>bold</b> `code` {rtl_override}",
                options=[_option("はい", "いいえより良い")],
            )
        ]
    )
    body = _unfence(question_cards.question_post(item, desktop=False))
    assert "日本語" in body and "続けますか" in body and "はい" in body and "いいえより良い" in body
    assert "@" not in body and "<b>" not in body and "`" not in body and rtl_override not in body
    assert "\\u0040all" in body
    assert "\\u003cb\\u003ebold\\u003c/b\\u003e" in body
    assert "\\u0060code\\u0060" in body
    assert "\\u202e" in body


def test_question_post_over_12kib_raises() -> None:
    item = _item([_question(question="x" * 20000)])
    with pytest.raises(ValueError, match="^mattermost_content_too_large$"):
        question_cards.question_post(item, desktop=False)


def test_question_post_renders_through_slack_and_discord_including_a_split() -> None:
    short_post = question_cards.question_post(_two_question_item(), desktop=False)
    providers: tuple[Provider, Provider] = ("slack", "discord")
    for provider in providers:
        assert len(messaging_format.render(provider, short_post)) == 1

    long_item = _item([_question(question="a" * 5000)])
    long_post = question_cards.question_post(long_item, desktop=False)
    cases: tuple[tuple[Provider, int], ...] = (("slack", 4000), ("discord", 2000))
    for provider, limit in cases:
        parts = messaging_format.render(provider, long_post)
        assert len(parts) > 1
        assert all(len(part) <= limit for part in parts)
        assert "".join(_part_body(part) for part in parts) == _unfence(long_post)


_MALFORMED_ITEMS: list[object] = [
    None,
    "not a dict",
    [],
    {"data": {"questions": []}},
    {"type": "permission.requested", "data": {"questions": []}},
    {"type": "question.asked", "data": "nope"},
    {"type": "question.asked", "data": {}},
    {"type": "question.asked", "data": {"questions": "nope"}},
    {"type": "question.asked", "data": {"questions": ["nope"]}},
    {
        "type": "question.asked",
        "data": {"questions": [{"question": "q", "options": [], "multi_select": False}]},
    },
    {
        "type": "question.asked",
        "data": {
            "questions": [{"header": "h", "question": "q", "options": [], "multi_select": "yes"}]
        },
    },
    {
        "type": "question.asked",
        "data": {
            "questions": [
                {"header": "h", "question": "q", "options": "nope", "multi_select": False}
            ]
        },
    },
    {
        "type": "question.asked",
        "data": {
            "questions": [
                {"header": "h", "question": "q", "options": ["nope"], "multi_select": False}
            ]
        },
    },
    {
        "type": "question.asked",
        "data": {
            "questions": [
                {
                    "header": "h",
                    "question": "q",
                    "options": [{"description": "d"}],
                    "multi_select": False,
                }
            ]
        },
    },
    {
        "type": "question.asked",
        "data": {
            "questions": [
                {
                    "header": "h",
                    "question": "q",
                    "options": [{"label": "l", "description": 1}],
                    "multi_select": False,
                }
            ]
        },
    },
]


@pytest.mark.parametrize("item", _MALFORMED_ITEMS)
def test_question_post_refuses_malformed_items(item: object) -> None:
    with pytest.raises(ValueError, match="^question_card_invalid$"):
        question_cards.question_post(item, desktop=False)


@pytest.mark.parametrize("item", _MALFORMED_ITEMS)
def test_pickable_false_for_malformed_items(item: object) -> None:
    assert question_cards.pickable(item) is False


@pytest.mark.parametrize("item", _MALFORMED_ITEMS)
def test_pick_answers_none_for_malformed_items(item: object) -> None:
    assert question_cards.pick_answers(item, "1") is None


# --- pickable ------------------------------------------------------------------------------------


def test_pickable_true_for_a_normal_item() -> None:
    assert question_cards.pickable(_two_question_item()) is True


def test_pickable_false_for_zero_questions() -> None:
    assert question_cards.pickable(_item([])) is False


def test_pickable_false_when_a_question_has_no_options() -> None:
    assert question_cards.pickable(_item([_question(options=[])])) is False


def test_pickable_false_when_question_texts_repeat() -> None:
    item = _item([_question(question="Same"), _question(question="Same")])
    assert question_cards.pickable(item) is False


# --- pick_answers --------------------------------------------------------------------------------


def test_pick_answers_single_question() -> None:
    item = _item([_question(question="Single", options=[_option("A"), _option("B")])])
    assert question_cards.pick_answers(item, "2") == {"Single": "B"}


def test_pick_answers_two_questions_second_multi_select() -> None:
    assert question_cards.pick_answers(_two_question_item(), "2 1,3") == {
        "Single": "B",
        "Multi": "X, Z",
    }


@pytest.mark.parametrize("picks", ["1,3", "3,1"])
def test_pick_answers_orders_labels_by_option_not_typed_order(picks: str) -> None:
    item = _item(
        [
            _question(
                question="Multi",
                options=[_option("X"), _option("Y"), _option("Z")],
                multi_select=True,
            )
        ]
    )
    assert question_cards.pick_answers(item, picks) == {"Multi": "X, Z"}


def test_pick_matches_desktop_submit() -> None:
    """Mirrors `QuestionCard.submit` (frontend/src/components/Cards/Cards.tsx): the answer map
    joins picked labels in option order, not in the order the user typed the numbers."""
    questions = [
        _question(question="Single", options=[_option("A"), _option("B"), _option("C")]),
        _question(
            question="Multi",
            options=[_option("X"), _option("Y"), _option("Z")],
            multi_select=True,
        ),
    ]
    item = _item(questions)
    selections = {"Single": {1}, "Multi": {0, 2}}  # 0-based, as submit() tracks them
    submit_like = {
        question["question"]: ", ".join(
            option["label"]
            for index, option in enumerate(question["options"])
            if index in selections[question["question"]]
        )
        for question in questions
    }
    # Typed out of option order: option 3 before option 1 on the multi-select question.
    answers = question_cards.pick_answers(item, "2 3,1")
    assert answers == submit_like == {"Single": "B", "Multi": "X, Z"}
    # A typed-order join would read "Z, X" here; pin the correct, option-order join instead.
    assert answers is not None and answers["Multi"] != "Z, X"


def test_pick_answers_refuses_wrong_count() -> None:
    assert question_cards.pick_answers(_two_question_item(), "2") is None
    assert question_cards.pick_answers(_two_question_item(), "2 1 3") is None


def test_pick_answers_refuses_zero() -> None:
    assert question_cards.pick_answers(_two_question_item(), "0 1") is None


def test_pick_answers_refuses_out_of_range() -> None:
    assert question_cards.pick_answers(_two_question_item(), "9 1") is None


def test_pick_answers_refuses_duplicate_number() -> None:
    assert question_cards.pick_answers(_two_question_item(), "1 1,1") is None


def test_pick_answers_refuses_two_on_single_select() -> None:
    assert question_cards.pick_answers(_two_question_item(), "1,2 1") is None


def test_pick_answers_refuses_fullwidth_digit() -> None:
    assert question_cards.pick_answers(_two_question_item(), "２ 1") is None


def test_pick_answers_refuses_leading_zero() -> None:
    assert question_cards.pick_answers(_two_question_item(), "01 1") is None


def test_pick_answers_refuses_wrong_separator() -> None:
    assert question_cards.pick_answers(_two_question_item(), "1;2 1") is None


def test_pick_answers_refuses_when_not_pickable() -> None:
    item = _item([_question(question="No options", options=[])])
    assert question_cards.pick_answers(item, "1") is None
