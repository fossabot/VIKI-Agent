"""The Decision model's client: TypeSafe's answering API over the fixed HTTPS boundary.

`TypeSafeHttp` is `FixedJsonHttp` on `https://api.typesafe.ai` that admits exactly one request,
POST /v1/systemone, and ignores a set-cookie header instead of refusing the reply. `DecisionModel`
is the `ModelPort` over it. Each `ask` call checks the key's shape, then makes one request that
carries the key in its own Authorization header and nowhere else. A refused or failed request
becomes a `ModelError` code; a reply that does not answer exactly the asked questions, each in its
question's shape, is `invalid_reply`. Nothing is retried and nothing is logged.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Collection, Mapping
from typing import Any

from viki_agent.decision_loop.contracts import (
    Answer,
    ChoiceAnswer,
    ModelError,
    ModelReply,
    ModelRequest,
    NoulAnswer,
    ScoreAnswer,
)
from viki_agent.messaging.mattermost_http import FixedHttpError, FixedJsonHttp

_BASE = "https://api.typesafe.ai"
_ASK = ("POST", "/v1/systemone")
_KEY = re.compile(r"[\x21-\x7e]{1,4096}")
# A stored call's bounds (agenda's `_MODEL_CHARS` and `_INPUT_TOKENS`): a reply past them is
# `invalid_reply` here rather than a refusal when the call is recorded.
_MODEL_CHARS = 32
_MODEL = re.compile(rf"[\x21-\x7e]{{1,{_MODEL_CHARS}}}")  # printable ASCII, so SQLite can bind it
_TOKENS = 1_000_000
_REFUSED = (400, 415, 422)
_WAIT_S = 60.0
_MAX_WAIT_S = 600


def _allowed(method: str, path: str) -> bool:
    return (method, path) == _ASK


class TypeSafeHttp(FixedJsonHttp):
    """`FixedJsonHttp` for TypeSafe: its one path only, and a set-cookie header ignored.
    `transport` stands in for the network in tests."""

    def __init__(self, *, transport: Any = None) -> None:
        super().__init__(
            _BASE,
            {"User-Agent": "VIKI Agent"},
            allowed=_allowed,
            transport=transport,
            set_cookie="drop",
        )


class DecisionModel:
    """`ModelPort` over `TypeSafeHttp`; `monotonic` times each ask. It keeps no key: every call
    is handed one."""

    def __init__(
        self, http: TypeSafeHttp | None = None, *, monotonic: Callable[[], float] = time.monotonic
    ) -> None:
        self._http = TypeSafeHttp() if http is None else http
        self._monotonic = monotonic

    async def ask(self, request: ModelRequest, *, key: str) -> ModelReply:
        """One POST /v1/systemone for `request`; the reply, checked against its questions."""
        body = {"model": request.model, "state": request.state, "questions": request.questions}
        started = self._monotonic()
        value = await self._send(_ASK, body, key)
        latency_ms = round((self._monotonic() - started) * 1000)
        reply = _reply(value, request.questions, latency_ms)
        if reply is None:
            raise ModelError("invalid_reply")
        return reply

    async def close(self) -> None:
        await self._http.close()

    async def _send(self, route: tuple[str, str], body: object, key: str) -> Any:
        """The JSON reply to one request that carries `key` in its Authorization header."""
        if not isinstance(key, str) or _KEY.fullmatch(key) is None:
            raise ModelError("no_key")
        method, path = route
        try:
            value, _ = await self._http.request(method, path, body, authorization="Bearer " + key)
            return value
        except FixedHttpError as error:
            failure = _failure(error)
        # Raised outside the handler, so no chained exception travels with the code.
        raise failure


def _failure(error: FixedHttpError) -> ModelError:
    """The code for a request without a usable reply: unreachable when nothing answered (a
    transport error, or no status before the deadline), else by status, else failed."""
    status = error.status_code
    if error.unanswered:
        return ModelError("unreachable")
    if status == 401:
        return ModelError("key_rejected")
    if status in _REFUSED:
        return ModelError("refused")
    if status == 413:
        return ModelError("too_large")
    if status == 429:
        return ModelError("rate_limited", _wait_s(error.headers))
    if status == 529:
        return ModelError("overloaded", _wait_s(error.headers))
    return ModelError("failed")


def _wait_s(headers: Mapping[str, str]) -> float:
    """`retry-after` in seconds when it is a whole number from 1 to 600, else 60."""
    value = headers.get("retry-after", "")
    if re.fullmatch(r"[0-9]{1,3}", value) and 1 <= int(value) <= _MAX_WAIT_S:
        return float(value)
    return _WAIT_S


def _reply(
    value: object, questions: Mapping[str, Mapping[str, object]], latency_ms: int
) -> ModelReply | None:
    """`value` as the reply to `questions`, or None when it does not fit: a model name of 1 to 32
    printable ASCII characters (0x21-0x7E, so storage can always bind it), answers for exactly
    the asked names, each in its question's shape, and both token counts from 0 to 1,000,000.
    Keys the reply adds are ignored."""
    if not isinstance(value, dict):
        return None
    model, answers, usage = value.get("model"), value.get("answers"), value.get("usage")
    if (
        not isinstance(model, str)
        or _MODEL.fullmatch(model) is None
        or not isinstance(answers, dict)
        or answers.keys() != set(questions)
        or not isinstance(usage, dict)
        or not _count(usage.get("input_tokens"))
        or not _count(usage.get("output_tokens"))
    ):
        return None
    checked: dict[str, Answer] = {}
    for name, question in questions.items():
        answer = _answer(question, answers[name])
        if answer is None:
            return None
        checked[name] = answer
    return ModelReply(model, checked, usage["input_tokens"], latency_ms)


def _answer(question: Mapping[str, object], raw: object) -> Answer | None:
    """`raw` as the answer to `question`, of the question's type, or None when it does not fit.
    Every number lies in [0, 1] except a score, which lies in [0, top] (the last level's index).
    A choice names one of the question's options and gives a probability for each of them; a
    score's probabilities are keyed by levels "0" to "top" and come back keyed by int."""
    kind, criteria = question.get("type"), question.get("criteria")
    if not isinstance(raw, dict) or raw.get("type") != kind:
        return None
    if kind == "noul":
        p = _number(raw.get("noul"), 1)
        return None if p is None else NoulAnswer(p)
    confidence = _number(raw.get("confidence"), 1)
    if confidence is None:
        return None
    if kind == "choice" and isinstance(criteria, Mapping):
        options = list(criteria)
        choice = raw.get("choice")
        probabilities = _probabilities(raw.get("probabilities"), options)
        if (
            not isinstance(choice, str)
            or choice not in options
            or probabilities is None
            or probabilities.keys() != set(options)
        ):
            return None
        return ChoiceAnswer(
            choice, confidence, {option: probabilities[option] for option in options}
        )
    if kind == "score" and isinstance(criteria, (list, tuple)):
        top = len(criteria) - 1
        levels = [str(level) for level in range(top + 1)]
        score = _number(raw.get("score"), top)
        probabilities = _probabilities(raw.get("probabilities"), levels)
        if score is None or probabilities is None:
            return None
        return ScoreAnswer(
            score,
            confidence,
            {int(level): probabilities[level] for level in levels if level in probabilities},
        )
    return None


def _probabilities(value: object, names: Collection[str]) -> dict[str, float] | None:
    """`value` as {name: probability} when it is an object keyed by some of `names` whose values
    are numbers from 0 to 1, else None."""
    if not isinstance(value, dict) or not value.keys() <= set(names):
        return None
    numbers: dict[str, float] = {}
    for name, p in value.items():
        number = _number(p, 1)
        if number is None:
            return None
        numbers[name] = number
    return numbers


def _number(value: object, high: float) -> float | None:
    """`value` as a float when it is a JSON number (a bool is not one) from 0 to `high`."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= high:
        return None
    return float(value)


def _count(value: object) -> bool:
    return type(value) is int and 0 <= value <= _TOKENS
