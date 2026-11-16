"""`reach` and `costs`: the start values with no rows, the closed-window and "seen" rule, each
feedback/engagement case `costs` classifies as bad, good or neither, and the clamp to
`REACH_BOUNDS`/`COST_BOUNDS` at both ends."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from viki_agent.decision_loop.arbiter import START_COSTS, START_REACH
from viki_agent.decision_loop.measures import (
    BAD_RATE,
    COST_BOUNDS,
    PRIOR_N,
    REACH_BOUNDS,
    SEEN_WITHIN,
    costs,
    reach,
)
from viki_agent.decision_loop.stats import LearningRow

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
OLD = NOW - SEEN_WITHIN - timedelta(minutes=1)  # created well before the window closes
_SAMPLE = 300  # enough rows that every level's clamp engages on both ends (checked by hand)


def _row(**overrides: Any) -> LearningRow:
    """A `LearningRow` with every field defaulted, mirroring test_stats.py's `_row` builder but
    straight to the dataclass -- `measures` is pure and never touches the store."""
    defaults: dict[str, Any] = {
        "episode_id": "e0",
        "created_at": OLD,
        "candidate": "approvals",
        "level": "nudge",
        "timing": "now",
        "policy": "model",
        "answers_json": None,
        "p": None,
        "feedback": None,
        "feedback_reason": None,
        "feedback_at": None,
        "first_exposed_at": None,
        "opened_at": None,
        "acted_at": None,
        "label": None,
    }
    return LearningRow(**{**defaults, **overrides})


def test_constants_match_the_spec() -> None:
    assert timedelta(hours=4) == SEEN_WITHIN
    assert PRIOR_N == 10
    assert BAD_RATE == 0.3


def test_reach_bounds_are_half_to_capped_one_and_a_half_times_the_start() -> None:
    assert set(REACH_BOUNDS) == set(START_REACH)
    expected = {
        "list": (0.15, 0.45),
        "badge": (0.3, 0.9),
        "nudge": (0.45, 0.99),
        "alert": (0.475, 0.99),
    }
    for level, bounds in expected.items():
        assert REACH_BOUNDS[level] == pytest.approx(bounds)


def test_cost_bounds_are_half_to_double_the_start() -> None:
    assert set(COST_BOUNDS) == set(START_COSTS)
    expected = {"badge": (0.025, 0.1), "nudge": (0.2, 0.8), "alert": (0.5, 2.0)}
    for level, bounds in expected.items():
        assert COST_BOUNDS[level] == pytest.approx(bounds)


# ---- reach --------------------------------------------------------------------------------


def test_reach_with_no_rows_returns_the_start_values_exactly() -> None:
    values, support = reach([], NOW)
    assert values == dict(START_REACH)
    assert support == dict.fromkeys(START_REACH, 0)


def test_reach_counts_only_closed_windows() -> None:
    """A row created less than `SEEN_WITHIN` ago is excluded outright -- not counted as unseen --
    even though, were it included, its own exposure would score it as seen."""
    open_window = _row(level="badge", created_at=NOW, first_exposed_at=NOW)
    assert reach([open_window], NOW) == reach([], NOW)


def test_reach_ignores_rows_at_a_different_level_or_timing() -> None:
    other_level = _row(level="alert", created_at=OLD, opened_at=OLD)
    other_timing = _row(level="badge", timing="at", created_at=OLD, opened_at=OLD)
    values, support = reach([other_level, other_timing], NOW)
    assert values["badge"] == START_REACH["badge"]
    assert support["badge"] == 0


@pytest.mark.parametrize("field", ["first_exposed_at", "opened_at", "acted_at", "feedback_at"])
def test_reach_seen_counts_any_single_stamp(field: str) -> None:
    row = _row(level="badge", created_at=OLD, **{field: OLD + timedelta(hours=1)})
    values, support = reach([row], NOW)
    assert support["badge"] == 1
    expected = (1 + PRIOR_N * START_REACH["badge"]) / (1 + PRIOR_N)
    assert values["badge"] == pytest.approx(expected)


@pytest.mark.parametrize(
    ("offset", "is_seen"),
    [
        (timedelta(minutes=-1), False),  # before created_at: not seen
        (timedelta(0), True),  # at created_at: the lower edge is inclusive
        (timedelta(hours=2), True),  # mid-window
        (SEEN_WITHIN, True),  # at the deadline: the upper edge is inclusive
        (SEEN_WITHIN + timedelta(milliseconds=1), False),  # just past: not seen
    ],
)
def test_reach_seen_window_is_closed_on_both_ends(offset: timedelta, is_seen: bool) -> None:
    row = _row(level="badge", created_at=OLD, opened_at=OLD + offset)
    values, support = reach([row], NOW)
    assert support["badge"] == 1
    seen = 1 if is_seen else 0
    expected = (seen + PRIOR_N * START_REACH["badge"]) / (1 + PRIOR_N)
    assert values["badge"] == pytest.approx(expected)


@pytest.mark.parametrize("level", list(START_REACH))
def test_reach_all_seen_clamps_to_the_upper_bound(level: str) -> None:
    rows = [_row(level=level, created_at=OLD, opened_at=OLD) for _ in range(_SAMPLE)]
    values, support = reach(rows, NOW)
    assert support[level] == _SAMPLE
    assert values[level] == REACH_BOUNDS[level][1]


@pytest.mark.parametrize("level", list(START_REACH))
def test_reach_none_seen_clamps_to_the_lower_bound(level: str) -> None:
    rows = [_row(level=level, created_at=OLD) for _ in range(_SAMPLE)]
    values, support = reach(rows, NOW)
    assert support[level] == _SAMPLE
    assert values[level] == REACH_BOUNDS[level][0]


# ---- costs ------------------------------------------------------------------------------


def test_costs_with_no_rows_returns_the_start_values_exactly() -> None:
    values, support = costs([])
    assert values == dict(START_COSTS)
    assert support == dict.fromkeys(START_COSTS, 0)


def test_costs_ignores_rows_at_a_different_level_or_timing() -> None:
    other_level = _row(level="alert", feedback="mute")
    other_timing = _row(level="badge", timing="at", feedback="mute")
    values, support = costs([other_level, other_timing])
    assert values["badge"] == START_COSTS["badge"]
    assert support["badge"] == 0


@pytest.mark.parametrize(
    ("feedback", "reason", "opened_at", "acted_at", "classification"),
    [
        ("later", None, None, None, "bad"),  # Not now
        ("mute", None, None, None, "bad"),
        ("wrong", "bad_timing", None, None, "bad"),
        ("wrong", "out_of_date", None, None, "neutral"),  # wrong, but not for timing
        ("wrong", None, None, None, "neutral"),  # wrong with no reason at all
        ("useful", None, None, None, "good"),
        (None, None, OLD, None, "good"),  # no reply, but opened
        (None, None, None, OLD, "good"),  # no reply, but acted
        (None, None, None, None, "neutral"),  # no reply at all
        ("missed", None, None, None, "neutral"),  # a weekly-audit label, not bad or good here
        ("right_to_hold", None, None, None, "neutral"),  # likewise
    ],
)
def test_costs_classifies_each_feedback_case(
    feedback: str | None,
    reason: str | None,
    opened_at: datetime | None,
    acted_at: datetime | None,
    classification: str,
) -> None:
    row = _row(
        level="nudge",
        feedback=feedback,
        feedback_reason=reason,
        opened_at=opened_at,
        acted_at=acted_at,
    )
    values, support = costs([row])
    bad = 1 if classification == "bad" else 0
    good = 1 if classification == "good" else 0
    assert support["nudge"] == bad + good
    rate = (bad + PRIOR_N * BAD_RATE) / (bad + good + PRIOR_N)
    low, high = COST_BOUNDS["nudge"]
    expected = min(max(START_COSTS["nudge"] * rate / BAD_RATE, low), high)
    assert values["nudge"] == pytest.approx(expected)


def test_costs_blends_several_bad_and_good_rows_at_once() -> None:
    rows = [
        _row(level="nudge", feedback="later"),
        _row(level="nudge", feedback="mute"),
        _row(level="nudge", feedback="useful"),
        _row(level="nudge", feedback="useful"),
        _row(level="nudge", feedback="useful"),
    ]
    values, support = costs(rows)
    assert support["nudge"] == 5
    rate = (2 + PRIOR_N * BAD_RATE) / (2 + 3 + PRIOR_N)
    expected = START_COSTS["nudge"] * rate / BAD_RATE
    assert values["nudge"] == pytest.approx(expected)


@pytest.mark.parametrize("level", list(START_COSTS))
def test_costs_all_bad_clamps_to_the_upper_bound(level: str) -> None:
    rows = [_row(level=level, feedback="mute") for _ in range(_SAMPLE)]
    values, support = costs(rows)
    assert support[level] == _SAMPLE
    assert values[level] == COST_BOUNDS[level][1]


@pytest.mark.parametrize("level", list(START_COSTS))
def test_costs_all_good_clamps_to_the_lower_bound(level: str) -> None:
    rows = [_row(level=level, feedback="useful") for _ in range(_SAMPLE)]
    values, support = costs(rows)
    assert support[level] == _SAMPLE
    assert values[level] == COST_BOUNDS[level][0]
