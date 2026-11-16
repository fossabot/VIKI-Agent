"""Seen-in-time and attention-cost estimates: what `learning.relearn` feeds into
`ArbiterInput.reach`/`ArbiterInput.costs` for the arbiter to use inside its own bounds.

`reach` is how reliably a shown level actually reaches the user within `SEEN_WITHIN`: among
rows at that level whose window has already closed (an open one is left out, not counted as
unseen), the share first exposed, opened, acted on or given feedback inside that window. `costs`
is how much attention a level (badge, nudge or alert; list always costs 0 and is never estimated
here) costs the user: a rate of "bad" against "bad" plus "good" feedback and engagement. Both
shrink towards `arbiter.START_REACH`/`START_COSTS` by a virtual `PRIOR_N` rows at the start value
(for cost, at `BAD_RATE`), so a handful of real rows moves the number only a little and a stream
of them moves it further, and both clamp to `REACH_BOUNDS`/`COST_BOUNDS` around the start value.

Pure: no I/O, no randomness, and the only clock read is the `now` each caller passes to `reach`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from types import MappingProxyType

from viki_agent.decision_loop.arbiter import START_COSTS, START_REACH
from viki_agent.decision_loop.stats import LearningRow

SEEN_WITHIN = timedelta(hours=4)
PRIOR_N = 10
BAD_RATE = 0.3

# {level: (0.5*start, min(0.99, 1.5*start))}, around `arbiter.START_REACH`.
REACH_BOUNDS: Mapping[str, tuple[float, float]] = MappingProxyType(
    {level: (0.5 * start, min(0.99, 1.5 * start)) for level, start in START_REACH.items()}
)
# {level: (0.5*start, 2*start)}, around `arbiter.START_COSTS`; list is never estimated.
COST_BOUNDS: Mapping[str, tuple[float, float]] = MappingProxyType(
    {level: (0.5 * start, 2.0 * start) for level, start in START_COSTS.items()}
)


def reach(rows: Sequence[LearningRow], now: datetime) -> tuple[dict[str, float], dict[str, int]]:
    """T per shown level (`arbiter.START_REACH`'s keys): among *rows* at that level, timing
    "now", created at or before `now - SEEN_WITHIN` (a row whose window is still open is left out
    outright, not counted as unseen), the share "seen" -- first exposed, opened, acted on or
    given feedback within `SEEN_WITHIN` of being created -- shrunk towards the start value by a
    virtual `PRIOR_N` rows at it, then clamped to `REACH_BOUNDS`.

    The second mapping is each level's row count after the window filter (its support).
    """
    values: dict[str, float] = {}
    support: dict[str, int] = {}
    for level, start in START_REACH.items():
        closed = [
            row
            for row in rows
            if row.level == level and row.timing == "now" and row.created_at <= now - SEEN_WITHIN
        ]
        seen = sum(1 for row in closed if _seen_within(row))
        n = len(closed)
        value = (seen + PRIOR_N * start) / (n + PRIOR_N)
        low, high = REACH_BOUNDS[level]
        values[level] = min(max(value, low), high)
        support[level] = n
    return values, support


def costs(rows: Sequence[LearningRow]) -> tuple[dict[str, float], dict[str, int]]:
    """C per badge, nudge and alert (`arbiter.START_COSTS`'s keys; list always costs 0 and is
    never estimated): among *rows* at that level, timing "now", a rate of "bad" feedback (later
    or mute, or wrong whose reason is "bad_timing") against "bad" plus "good" (useful feedback,
    or no feedback at all with the item opened or acted on -- anything else, including missed and
    right_to_hold feedback, counts as neither), shrunk towards the start value by a virtual
    `PRIOR_N` rows at `BAD_RATE`, then clamped to `COST_BOUNDS`.

    The second mapping is each level's bad-plus-good count (its support).
    """
    values: dict[str, float] = {}
    support: dict[str, int] = {}
    for level, start in START_COSTS.items():
        matching = [row for row in rows if row.level == level and row.timing == "now"]
        bad = sum(1 for row in matching if _bad(row))
        good = sum(1 for row in matching if _good(row))
        rate = (bad + PRIOR_N * BAD_RATE) / (bad + good + PRIOR_N)
        low, high = COST_BOUNDS[level]
        values[level] = min(max(start * rate / BAD_RATE, low), high)
        support[level] = bad + good
    return values, support


def _seen_within(row: LearningRow) -> bool:
    """Whether any of the row's four engagement stamps falls in `[created_at, created_at +
    SEEN_WITHIN]`."""
    deadline = row.created_at + SEEN_WITHIN
    return any(
        when is not None and row.created_at <= when <= deadline
        for when in (row.first_exposed_at, row.opened_at, row.acted_at, row.feedback_at)
    )


def _bad(row: LearningRow) -> bool:
    """Later or mute feedback, or wrong feedback whose reason is "bad_timing"."""
    return row.feedback in ("later", "mute") or (
        row.feedback == "wrong" and row.feedback_reason == "bad_timing"
    )


def _good(row: LearningRow) -> bool:
    """Useful feedback, or no feedback at all with the item opened or acted on."""
    return row.feedback == "useful" or (
        row.feedback is None and (row.opened_at is not None or row.acted_at is not None)
    )
