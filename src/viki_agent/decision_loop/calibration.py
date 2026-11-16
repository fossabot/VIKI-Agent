"""Calibration, question usefulness, shadow-version and trial-wording comparison over learning
rows.

`calibration` scores recent labelled model decisions against the user's labels: a Brier score
plus five probability bins. `questions` breaks the same rows down by each of the playbook's
seven yes/no questions (`QUESTIONS`): how useful the user found it when that question was
answered yes and when no. `shadow` compares one shadow version's decisions
against the live ones decided alongside them, by level, kind and Brier score. `trial` compares a
wording on trial for one yes/no question against the live wording, over the decisions it was
tried on: how often the two agreed, and each wording's usefulness rate when it said yes and when
no.

Pure: no I/O, no clock reads. Every function reads `stats.LearningRow` fields only and mutates
none of them.
"""

from __future__ import annotations

from collections.abc import Sequence

from viki_agent.decision_loop import playbook
from viki_agent.decision_loop.contracts import (
    Answer,
    Calibration,
    CalibrationBin,
    NoulAnswer,
    QuestionRate,
    ShadowStats,
    TrialStats,
)
from viki_agent.decision_loop.cycle import (
    ShadowSummary,
    TrialSummary,
    decode_answers,
    decode_shadow,
    decode_trial,
)
from viki_agent.decision_loop.stats import LearningRow

# The playbook's yes/no questions, in its own order (the three choice/score questions left out).
QUESTIONS: tuple[str, ...] = tuple(
    name for name, question in playbook.questions().items() if question["type"] == "noul"
)

_BINS = 5


def brier(pairs: Sequence[tuple[float, int]]) -> float | None:
    """The mean squared error between each predicted probability and its 0/1 label in *pairs*;
    None when *pairs* is empty."""
    if not pairs:
        return None
    return sum((p - label) ** 2 for p, label in pairs) / len(pairs)


def _scored(rows: Sequence[LearningRow]) -> list[tuple[LearningRow, float, int]]:
    """*rows* that count toward calibration and the question rates: a model decision with both
    a label and a `p`, as (row, p, label)."""
    return [
        (row, row.p, row.label)
        for row in rows
        if row.policy == "model" and row.p is not None and row.label is not None
    ]


def _bin_bounds(index: int) -> tuple[float, float]:
    return index / _BINS, (index + 1) / _BINS


def _bin_index(p: float) -> int:
    """Which of the five equal `[low, high)` bins *p* falls in; the last bin alone includes its
    top edge, so a `p` of 1.0 lands there too."""
    for index in range(_BINS - 1):
        _, high = _bin_bounds(index)
        if p < high:
            return index
    return _BINS - 1


def _bins(scored: Sequence[tuple[LearningRow, float, int]]) -> tuple[CalibrationBin, ...]:
    buckets: list[list[tuple[float, int]]] = [[] for _ in range(_BINS)]
    for _, p, label in scored:
        buckets[_bin_index(p)].append((p, label))
    bins: list[CalibrationBin] = []
    for index, members in enumerate(buckets):
        low, high = _bin_bounds(index)
        n = len(members)
        mean_p = sum(p for p, _ in members) / n if n else None
        rate = sum(label for _, label in members) / n if n else None
        bins.append(CalibrationBin(low=low, high=high, n=n, mean_p=mean_p, rate=rate))
    return tuple(bins)


def calibration(rows: Sequence[LearningRow]) -> Calibration:
    """How well recent model decisions' `p` matched the user's labels: the Brier score plus five
    probability bins, over the scored rows of *rows* (`_scored`)."""
    scored = _scored(rows)
    return Calibration(
        n=len(scored),
        brier=brier([(p, label) for _, p, label in scored]),
        bins=_bins(scored),
    )


def _said_yes(answer: Answer | None) -> bool | None:
    """Whether a decoded answer is a yes/no answer saying yes (`p` ≥ 0.5), saying no, or, for
    anything else -- missing, a choice or a score -- neither."""
    return None if not isinstance(answer, NoulAnswer) else answer.p >= 0.5


def questions(rows: Sequence[LearningRow]) -> tuple[QuestionRate, ...]:
    """Each of the playbook's yes/no questions (`QUESTIONS`): the user's usefulness rate (the
    mean label) when the scored rows of *rows* (`_scored`) answered it yes and when no. A row
    whose answers do not decode, or that carries no yes/no answer for a question, is left out of
    that question's counts."""
    decoded: list[tuple[dict[str, Answer], int]] = []
    for row, _, label in _scored(rows):
        if row.answers_json is None:
            continue
        try:
            answers, _ = decode_answers(row.answers_json)
        except ValueError:
            continue
        decoded.append((answers, label))
    rates: list[QuestionRate] = []
    for name in QUESTIONS:
        yes = [label for answers, label in decoded if _said_yes(answers.get(name)) is True]
        no = [label for answers, label in decoded if _said_yes(answers.get(name)) is False]
        rates.append(
            QuestionRate(
                name=name,
                yes_n=len(yes),
                yes_rate=sum(yes) / len(yes) if yes else None,
                no_n=len(no),
                no_rate=sum(no) / len(no) if no else None,
            )
        )
    return tuple(rates)


def shadow(rows: Sequence[LearningRow], setting: str) -> ShadowStats:
    """How the shadowed version *setting* (a `contracts.SHADOWS` member) compared to the live
    one: decisions, level and kind agreement over every row of *rows* whose stored answers carry
    a shadow summary for *setting*, then, of those with a label, the Brier score of the shadow's
    `p` and of the live `p` against it. "off" never matches a row -- `decode_shadow`'s setting is
    always a `contracts.MODEL_IDS` key -- so it answers all zeros and no Brier. The page passes
    `DecisionLoopStats.shadow_rows`, silent and unlabelled decisions included."""
    matches: list[tuple[LearningRow, ShadowSummary]] = []
    for row in rows:
        if row.answers_json is None:
            continue
        summary = decode_shadow(row.answers_json)
        if summary is not None and summary.setting == setting:
            matches.append((row, summary))
    shadow_pairs: list[tuple[float, int]] = []
    live_pairs: list[tuple[float, int]] = []
    labels = 0
    for row, summary in matches:
        if row.label is None:
            continue
        labels += 1
        shadow_pairs.append((summary.p, row.label))
        if row.p is not None:
            live_pairs.append((row.p, row.label))
    return ShadowStats(
        setting=setting,
        decisions=len(matches),
        agree=sum(1 for _, summary in matches if summary.agree),
        kind_agree=sum(1 for _, summary in matches if summary.kind_agree),
        labels=labels,
        brier=brier(shadow_pairs),
        live_brier=brier(live_pairs),
    )


def trial(rows: Sequence[LearningRow], name: str) -> TrialStats:
    """How the wording on trial for the yes/no question *name* compared with the live wording,
    over the decisions it was tried on: `agree` counts the rows of *rows* whose stored answers
    carry a trial summary (`decode_trial`) and whose two probabilities fell on the same side of
    0.5; of those with a label, `live` and `trial` are each wording's `QuestionRate` -- the
    user's usefulness rate when that wording's probability said yes (>= 0.5) and when it said
    no. The page passes `DecisionLoopStats.trial_rows`, silent and unlabelled decisions
    included."""
    matches: list[tuple[LearningRow, TrialSummary]] = []
    for row in rows:
        if row.answers_json is None:
            continue
        summary = decode_trial(row.answers_json)
        if summary is not None:
            matches.append((row, summary))
    live_pairs: list[tuple[float, int]] = []
    trial_pairs: list[tuple[float, int]] = []
    labels = 0
    for row, summary in matches:
        if row.label is None:
            continue
        labels += 1
        live_pairs.append((summary.live, row.label))
        trial_pairs.append((summary.trial, row.label))
    return TrialStats(
        name=name,
        decisions=len(matches),
        agree=sum(1 for _, summary in matches if (summary.live >= 0.5) == (summary.trial >= 0.5)),
        labels=labels,
        live=_rate(name, live_pairs),
        trial=_rate(name, trial_pairs),
    )


def _rate(name: str, pairs: Sequence[tuple[float, int]]) -> QuestionRate:
    """One wording's `QuestionRate` named *name* over (p, label) *pairs*: the user's usefulness
    rate (the mean label) when `p` said yes (>= 0.5) and when it said no."""
    yes = [label for p, label in pairs if p >= 0.5]
    no = [label for p, label in pairs if p < 0.5]
    return QuestionRate(
        name=name,
        yes_n=len(yes),
        yes_rate=sum(yes) / len(yes) if yes else None,
        no_n=len(no),
        no_rate=sum(no) / len(no) if no else None,
    )
