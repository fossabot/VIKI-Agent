"""What Decision learns from the user's answers: re-fitted, stored and read back.

`relearn` turns a scope's learning rows (`stats.LearningRow`) into a `contracts.Learned`
snapshot: p's weights re-fitted by `fit.fit` over the `labelled` rows once there are
`MIN_LABELS` of them with `MIN_EACH` of each label (else the start weights, every topic at 0),
and T and C from `measures.reach` and `measures.costs`. `encode` writes a snapshot as the
"learned" preference stores it, `parse` reads one back, answering None for anything but an
exact, bounded snapshot, and `inputs` hands the arbiter its numbers: a snapshot's, or the start
values without one.

Every number stays inside its bounds (`BOUNDS`, `measures.REACH_BOUNDS`, `measures.COST_BOUNDS`),
and `relearn` answers each in its stored form (rounded to 6 places inside those bounds,
`fitted_at` to the millisecond), so `parse(encode(learned))` gives a relearned snapshot back
unchanged.

Pure: no I/O, no randomness and no logging; the only clock is the `now` passed to `relearn`.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import datetime
from types import MappingProxyType
from typing import TypeGuard

from viki_agent.automation.routines import parse_stamp, stamp
from viki_agent.decision_loop import measures
from viki_agent.decision_loop.arbiter import (
    FEATURES,
    START_COSTS,
    START_REACH,
    START_WEIGHTS,
    logit_feature,
)
from viki_agent.decision_loop.contracts import (
    CANDIDATES,
    MAX_FIT_ROWS,
    MIN_EACH,
    MIN_LABELS,
    Answer,
    Learned,
    NoulAnswer,
)
from viki_agent.decision_loop.cycle import decode_answers
from viki_agent.decision_loop.fit import fit
from viki_agent.decision_loop.measures import COST_BOUNDS, REACH_BOUNDS
from viki_agent.decision_loop.stats import LearningRow

PRECISION = 4.0  # the prior's precision for every weight: a standard deviation of 0.5
# Each weight's (low, high): the bias, the yes/no features, then one offset per candidate topic.
BOUNDS: Mapping[str, tuple[float, float]] = MappingProxyType(
    {"bias": (-3.0, 1.0)}
    | {name: (0.0, 2.0) for name in FEATURES}
    | {f"topic.{candidate}": (-1.5, 1.5) for candidate in CANDIDATES}
)

_VERSION = 1
_PLACES = 6
# The fit's prior, and the weights while nothing is learned: `START_WEIGHTS`, every topic at 0.
_START: Mapping[str, float] = MappingProxyType(
    dict(START_WEIGHTS) | {f"topic.{candidate}": 0.0 for candidate in CANDIDATES}
)
_PRECISIONS: Mapping[str, float] = MappingProxyType(dict.fromkeys(_START, PRECISION))
_SUPPORT = (
    *(f"topic.{candidate}" for candidate in CANDIDATES),
    *(f"reach.{level}" for level in REACH_BOUNDS),
    *(f"costs.{level}" for level in COST_BOUNDS),
)
_KEYS = frozenset(("v", "fitted_at", "labels", "weights", "reach", "costs", "support"))
# What a snapshot from an earlier version also carries, which `parse` reads past whatever it
# holds: the confidence-floor lift that version gave items it took to be Japanese, and the
# per-language label counts behind it.
_RETIRED_KEYS = frozenset(("ja_lift",))
_RETIRED_SUPPORT = frozenset(("labels.en", "labels.ja"))


def labelled(rows: Sequence[LearningRow]) -> list[LearningRow]:
    """The rows the fit reads: "model" rows labelled 0 or 1 whose stored answers decode, newest
    first (by `created_at`, then `episode_id`, as `stats.learning_rows` orders them), at most
    `MAX_FIT_ROWS`."""
    return [row for row, _, _ in _fit_rows(rows)]


def relearn(rows: Sequence[LearningRow], *, now: datetime) -> Learned:
    """The snapshot *rows* teach, as of *now*.

    Weights: `fit` from the start weights (every topic at 0) under `PRECISION` and `BOUNDS`
    over the `labelled` rows, each row's x being the terms `arbiter.p_of` weighs for its
    decision -- "bias" 1, each `FEATURES` answer's `logit_feature` (0 when missing or not a
    yes/no answer) and "topic.<candidate>" 1 -- or the start weights unchanged below
    `MIN_LABELS` such rows or `MIN_EACH` of either label. T and C: `measures.reach` and
    `measures.costs` over every row.

    `labels` counts the `labelled` rows. `support` holds each topic's `labelled` rows, fitted
    or not ("topic.<candidate>"), and the measures' own counts ("reach.<level>",
    "costs.<level>"). Every number comes back in its stored form, rounded to 6 places inside
    its bounds, and `fitted_at` is *now* to the millisecond.
    """
    fitted = _fit_rows(rows)
    ones = sum(label for _, _, label in fitted)
    weights: Mapping[str, float] = _START
    if len(fitted) >= MIN_LABELS and min(ones, len(fitted) - ones) >= MIN_EACH:
        weights = fit(
            [(_terms(row, answers), label) for row, answers, label in fitted],
            prior=_START,
            precision=_PRECISIONS,
            bounds=BOUNDS,
        )
    reach, reach_support = measures.reach(rows, now)
    costs, cost_support = measures.costs(rows)
    topics = Counter(row.candidate for row, _, _ in fitted)
    support = {f"topic.{candidate}": topics[candidate] for candidate in CANDIDATES}
    support |= {f"reach.{level}": reach_support[level] for level in REACH_BOUNDS}
    support |= {f"costs.{level}": cost_support[level] for level in COST_BOUNDS}
    return Learned(
        fitted_at=parse_stamp(stamp(now)),
        labels=len(fitted),
        weights=_rounded(weights, BOUNDS),
        reach=_rounded(reach, REACH_BOUNDS),
        costs=_rounded(costs, COST_BOUNDS),
        support=support,
    )


def encode(learned: Learned) -> dict[str, object]:
    """*learned* as the "learned" preference stores it: {"v": 1, "fitted_at": its `stamp`,
    "labels", "weights", "reach", "costs", "support"}, with every number rounded to 6 places
    and kept inside its bounds (a bound such as 1.5 x 0.3 is no 6-place number, so rounding
    alone could carry a number at it past it). Reads exactly the snapshot's names from
    *learned*: a missing one raises KeyError, another one is left out."""
    return {
        "v": _VERSION,
        "fitted_at": stamp(learned.fitted_at),
        "labels": learned.labels,
        "weights": _rounded(learned.weights, BOUNDS),
        "reach": _rounded(learned.reach, REACH_BOUNDS),
        "costs": _rounded(learned.costs, COST_BOUNDS),
        "support": {name: learned.support[name] for name in _SUPPORT},
    }


def parse(value: object) -> Learned | None:
    """The snapshot `encode` wrote, or None, so the start values decide, unless *value* is
    exactly one: an object with exactly the snapshot's members at every level, "v" 1,
    "fitted_at" a `stamp`, every weight, T and C a number inside `BOUNDS`, `REACH_BOUNDS` or
    `COST_BOUNDS`, and "labels" and every support count a non-negative int. A flag is no
    number. A snapshot from an earlier version may also hold `_RETIRED_KEYS` and, in its
    "support", `_RETIRED_SUPPORT`: they are skipped whatever they hold, and the rest must be
    exact. Never raises."""
    if not isinstance(value, dict) or value.keys() - _RETIRED_KEYS != _KEYS:
        return None
    version, labels = value["v"], value["labels"]
    if type(version) is not int or version != _VERSION or not _count(labels):
        return None
    try:
        fitted_at = parse_stamp(value["fitted_at"])
    except ValueError:
        return None
    weights = _numbers(value["weights"], BOUNDS)
    reach = _numbers(value["reach"], REACH_BOUNDS)
    costs = _numbers(value["costs"], COST_BOUNDS)
    support = _counts(value["support"])
    if weights is None or reach is None or costs is None or support is None:
        return None
    return Learned(
        fitted_at=fitted_at,
        labels=labels,
        weights=weights,
        reach=reach,
        costs=costs,
        support=support,
    )


def inputs(learned: Learned | None) -> tuple[dict[str, float], dict[str, float], dict[str, float]]:
    """The arbiter's weights, reach (T) and costs (C): *learned*'s, or for None the start values
    (`START_WEIGHTS` with every topic at 0, `START_REACH` and `START_COSTS`). Fresh dicts on
    every call."""
    if learned is None:
        return dict(_START), dict(START_REACH), dict(START_COSTS)
    return dict(learned.weights), dict(learned.reach), dict(learned.costs)


def _fit_rows(rows: Sequence[LearningRow]) -> list[tuple[LearningRow, dict[str, Answer], int]]:
    """`labelled`'s rows, each with its decoded answers and its label."""
    found: list[tuple[LearningRow, dict[str, Answer], int]] = []
    for row in sorted(rows, key=lambda one: (one.created_at, one.episode_id), reverse=True):
        label = row.label
        if row.policy != "model" or label not in (0, 1) or row.answers_json is None:
            continue
        try:
            answers, _ = decode_answers(row.answers_json)
        except ValueError, OverflowError:  # no answers encoding, or an int no float holds
            continue
        found.append((row, answers, label))
        if len(found) == MAX_FIT_ROWS:
            break
    return found


def _terms(row: LearningRow, answers: Mapping[str, Answer]) -> dict[str, float]:
    """A fit row's x: "bias" 1, each `FEATURES` answer's `logit_feature` (0 when missing or not
    a yes/no answer) and "topic.<candidate>" 1 -- `fit` reads no topic outside `CANDIDATES`,
    as `p_of` adds none for it."""
    terms = {"bias": 1.0}
    for name in FEATURES:
        answer = answers.get(name)
        terms[name] = logit_feature(answer.p if isinstance(answer, NoulAnswer) else None)
    terms[f"topic.{row.candidate}"] = 1.0
    return terms


def _rounded(
    values: Mapping[str, float], bounds: Mapping[str, tuple[float, float]]
) -> dict[str, float]:
    """*values* by *bounds*' names, in their order, each `_place`d inside its bounds."""
    return {name: _place(values[name], low, high) for name, (low, high) in bounds.items()}


def _place(value: float, low: float, high: float) -> float:
    """*value* rounded to `_PLACES` places, then clamped to [low, high]."""
    return min(max(round(float(value), _PLACES), low), high)


def _numbers(value: object, bounds: Mapping[str, tuple[float, float]]) -> dict[str, float] | None:
    """*value* as {name: number} when it is an object naming exactly *bounds*' keys, each a
    number inside its bounds; else None."""
    if not isinstance(value, dict) or value.keys() != set(bounds):
        return None
    numbers: dict[str, float] = {}
    for name, (low, high) in bounds.items():
        number = value[name]
        if not _within(number, low, high):
            return None
        numbers[name] = float(number)
    return numbers


def _counts(value: object) -> dict[str, int] | None:
    """*value* as the support counts when it is an object naming exactly the snapshot's
    support names, besides any of `_RETIRED_SUPPORT`, each a non-negative int; else None."""
    if not isinstance(value, dict) or value.keys() - _RETIRED_SUPPORT != set(_SUPPORT):
        return None
    counts: dict[str, int] = {}
    for name in _SUPPORT:
        count = value[name]
        if not _count(count):
            return None
        counts[name] = count
    return counts


def _within(value: object, low: float, high: float) -> TypeGuard[float]:
    """A number, not a flag, in [low, high]; NaN, an infinity and an int too large for a float
    all compare outside."""
    return not isinstance(value, bool) and isinstance(value, int | float) and low <= value <= high


def _count(value: object) -> TypeGuard[int]:
    """A non-negative int, not a flag."""
    return type(value) is int and value >= 0
