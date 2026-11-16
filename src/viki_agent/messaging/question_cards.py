"""Readable chat posts for question cards, and strict parsing of a typed option pick.

Pure rendering and parsing: no I/O, no bot or session state. `question_post` turns a
`question.asked` item (the shape `SessionActor.displayed_request` and `StreamState._item`
produce: `{"type": "question.asked", "data": {"questions": [...], ...}, ...}`) into one readable
```text post through `mattermost_format.literal_text`, which also makes markup and control
characters inert and enforces the 12 KiB post cap. `pickable` and `pick_answers` parse a
user-typed `!viki pick` argument into the same answer map `QuestionCard.submit`
(frontend/src/components/Cards/Cards.tsx) builds from a desktop Submit, so a messaging pick and a
desktop Submit are indistinguishable to the driver.
"""

from __future__ import annotations

import re
from typing import Any

from viki_agent.messaging.mattermost_format import literal_text

# One to three ASCII digits, no leading zero; several selections are comma-joined. re.ASCII is
# belt-and-suspenders here: the literal 0-9 ranges already exclude any non-ASCII digit such as
# the fullwidth "２" (U+FF12).
_PICK = re.compile(r"[1-9][0-9]{0,2}(?:,[1-9][0-9]{0,2})*", re.ASCII)


def _clean(value: str) -> str:
    """Collapse any run of whitespace (including a bare newline or U+2028) to one space, so a
    user-authored field can never start a fake line in the rendered post."""
    return " ".join(value.split())


def _questions(item: object) -> list[dict[str, Any]] | None:
    """The item's own question list, shape-checked and copied, or None if `item` does not match
    `{"type": "question.asked", "data": {"questions": [...]}}`. Every type check uses `type(x)
    is` (never `isinstance`): the only producer is the app's own event encoding, and a bool where
    an int is expected, for instance, is as malformed as a wrong class."""
    if (
        type(item) is not dict
        or item.get("type") != "question.asked"
        or type(item.get("data")) is not dict
    ):
        return None
    raw_questions = item["data"].get("questions")
    if type(raw_questions) is not list:
        return None
    questions: list[dict[str, Any]] = []
    for raw_question in raw_questions:
        if type(raw_question) is not dict:
            return None
        header = raw_question.get("header")
        text = raw_question.get("question")
        raw_options = raw_question.get("options")
        multi_select = raw_question.get("multi_select")
        if (
            type(header) is not str
            or type(text) is not str
            or type(multi_select) is not bool
            or type(raw_options) is not list
        ):
            return None
        options: list[dict[str, str]] = []
        for raw_option in raw_options:
            if type(raw_option) is not dict:
                return None
            label = raw_option.get("label")
            description = raw_option.get("description")
            if type(label) is not str or type(description) is not str:
                return None
            options.append({"label": label, "description": description})
        questions.append(
            {"header": header, "question": text, "options": options, "multi_select": multi_select}
        )
    return questions


def _pickable(questions: list[dict[str, Any]]) -> bool:
    if not questions:
        return False
    texts = [question["question"] for question in questions]
    return len(set(texts)) == len(texts) and all(len(q["options"]) >= 1 for q in questions)


def question_post(item: object, *, desktop: bool) -> str:
    """A complete, readable ```text post for one question.asked item: a title, then every
    question numbered with its header and text, then its options numbered with their
    descriptions, then a multi-select note. Raises ValueError("question_card_invalid") if `item`
    does not hold a valid question list; `literal_text`'s own ValueError (oversized or invalid
    content) passes through unchanged."""
    questions = _questions(item)
    if questions is None:
        raise ValueError("question_card_invalid")
    n = len(questions)
    lines = ["Question from a desktop chat" if desktop else "Question"]
    if n == 0:
        lines.append("No questions are listed.")
    for index, question in enumerate(questions, 1):
        header = _clean(question["header"])
        text = _clean(question["question"])
        prefix = f"{header} - " if header else ""
        lines.append(f"Question {index} of {n}: {prefix}{text}")
        for position, option in enumerate(question["options"], 1):
            label = _clean(option["label"])
            description = _clean(option["description"])
            suffix = f" - {description}" if description else ""
            lines.append(f"  {position}. {label}{suffix}")
        if question["multi_select"]:
            lines.append("  Pick one or more.")
    return literal_text("\n".join(lines))


def pickable(item: object) -> bool:
    """Whether item holds at least one question, every question has at least one option, and the
    raw (uncleaned) question texts are unique -- the last so each can key an answer map."""
    questions = _questions(item)
    return questions is not None and _pickable(questions)


def pick_answers(item: object, picks: str) -> dict[str, str] | None:
    """The Submit answer map for a `!viki pick` argument, or None if `item` is not pickable or
    `picks` is malformed.

    `picks` must hold exactly one whitespace-separated token per question, in question order.
    Each token is one or more 1-based option numbers, comma-joined, in range, duplicate-free, and
    (on a single-select question) exactly one. The returned labels follow option order -- not
    the order typed in `picks` -- so a messaging pick produces the same map as
    `QuestionCard.submit`'s desktop click-through regardless of how the user typed the numbers.
    """
    questions = _questions(item)
    if questions is None or not _pickable(questions):
        return None
    tokens = picks.split()
    if len(tokens) != len(questions):
        return None
    answers: dict[str, str] = {}
    for token, question in zip(tokens, questions, strict=True):
        if not _PICK.fullmatch(token):
            return None
        numbers = [int(part) for part in token.split(",")]
        options = question["options"]
        if len(set(numbers)) != len(numbers) or any(
            not 1 <= number <= len(options) for number in numbers
        ):
            return None
        if not question["multi_select"] and len(numbers) != 1:
            return None
        answers[question["question"]] = ", ".join(
            options[number - 1]["label"] for number in sorted(numbers)
        )
    return answers
