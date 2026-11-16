"""The Decision arbiter: p, the value, reach and cost terms and the score; learned numbers (a
topic offset, T and C) within their ranges; the gates, alike for an item in any script, the
own-question gate among them; hysteresis; timing for quiet hours, typing and Focus, rules
deferrals and context; work within the grants; the rules-only mapping; the reversal check; pacing
and a stored λ; the start values deciding as before; and the authority property over 2,000 seeded
random inputs, own answers among them, at the start numbers and with learned ones."""

from __future__ import annotations

import math
import random
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import Any

import pytest

from viki_agent.decision_loop.arbiter import (
    FEATURES,
    GATES,
    LAMBDA_MAX,
    MARGIN,
    START_COSTS,
    START_LAMBDA,
    START_REACH,
    START_WEIGHTS,
    ArbiterInput,
    decide,
    lambda_of,
    logit_feature,
    needs_reversal,
    p_of,
    pace,
)
from viki_agent.decision_loop.contracts import (
    CANDIDATES,
    CARDS,
    GRANTS,
    KINDS,
    LEVELS,
    NUDGE_CAP,
    TIMINGS,
    WORK,
    Answer,
    Candidate,
    ChoiceAnswer,
    DecisionSettings,
    NoulAnswer,
    Outcome,
    Presence,
    ScoreAnswer,
    canonical_json,
    quiet_until,
    sha256_hex,
)
from viki_agent.decision_loop.playbook import OWN

NOW = datetime(2026, 10, 21, 9, 0, tzinfo=UTC)
SHOWN = ("list", "badge", "nudge", "alert")
LOUD = ("nudge", "alert")
NOULS = ("goal_link", "owner_only", "news", "actionable", "same_as_muted", "fits_now", "sensitive")
QUIET_TIMES = tuple(f"{hour:02d}:{minute:02d}" for hour in range(24) for minute in (0, 30))
FLAT = dict.fromkeys(START_WEIGHTS, 0.0)  # p = 0.5 whatever the answers
FOCUS = DecisionSettings(alerts=True, focus_until=NOW + timedelta(minutes=30))
# A title in Han and kana, built from code points: an item is decided alike in any script.
OTHER_SCRIPT = "".join(map(chr, (0x8A08, 0x753B, 0x3092, 0x898B, 0x76F4, 0x3059)))

# Every answer high and every gate open: p = 0.987 and V = 3, so at λ 0.3 a new item scores list
# 0.89, badge 1.70, nudge 1.97 and alert 0.91, and becomes a nudge.
STRONG: Mapping[str, Answer] = {
    "goal_link": NoulAnswer(0.95),
    "owner_only": NoulAnswer(0.9),
    "news": NoulAnswer(0.9),
    "actionable": NoulAnswer(0.9),
    "same_as_muted": NoulAnswer(0.0),
    "fits_now": NoulAnswer(0.9),
    "sensitive": NoulAnswer(0.0),
    "importance": ScoreAnswer(3.0, 0.9, {3: 0.9, 2: 0.1}),
    "kind": ChoiceAnswer("ask", 0.9, {"ask": 0.9, "inform": 0.1}),
    "work": ChoiceAnswer("none", 0.9, {"none": 0.9, "prepare": 0.1}),
}

# One unusable answer per question; the arbiter reads each as missing.
UNUSABLE: Mapping[str, Answer] = {
    **{name: NoulAnswer(math.nan) for name in NOULS},
    "importance": ScoreAnswer(math.nan, 0.9, {}),
    "kind": ChoiceAnswer("shout", 0.9, {}),
    "work": NoulAnswer(0.9),
}


@dataclass(frozen=True)
class Verdict:
    """The part of a rules verdict the arbiter reads."""

    attention: str
    reason: str = "waiting_on_you"
    defer_until: datetime | None = None


def answers(**changes: Answer | None) -> dict[str, Answer]:
    """STRONG with `changes` applied; None drops that answer."""
    merged = dict(STRONG)
    for name, answer in changes.items():
        if answer is None:
            merged.pop(name, None)
        else:
            merged[name] = answer
    return merged


def candidate(**changes: Any) -> Candidate:
    """A plain approvals item in English that may lead to prepare or run work."""
    item = Candidate(
        item_key="0" * 64,
        type="approvals",
        title="Plan review",
        facts={},
        words={},
        evidence=(),
        related_chat=None,
        related_ref=None,
        deadline=None,
        shown_elsewhere=(),
        rules_evidence={},
        origin_session=None,
        work_allowed=("prepare", "run"),
    )
    return replace(item, **changes)


def presence(**changes: Any) -> Presence:
    """The user at the app, neither typing nor away."""
    here = Presence(
        focused=True,
        typing=False,
        away=False,
        chat_id=None,
        chat_title=None,
        note_ref=None,
        launcher_open=False,
        page=None,
    )
    return replace(here, **changes)


def arbiter_input(**changes: Any) -> ArbiterInput:
    """A new item with STRONG answers, the reversal agreeing, the rules notifying, alerts on and
    every other setting at its default; `changes` replace fields."""
    inp = ArbiterInput(
        candidate=candidate(),
        answers=STRONG,
        reversal_agrees=True,
        rules_verdict=Verdict("notify"),
        settings=DecisionSettings(alerts=True),
        presence=presence(),
        quiet_until=None,
        current_level="silent",
        exposures_24h=0,
        loud_last_hour=0,
        lam=START_LAMBDA,
        weights=START_WEIGHTS,
        now=NOW,
    )
    return replace(inp, **changes)


def sigmoid(x: float) -> float:
    return 1 / (1 + math.exp(-x))


def logit(p: float) -> float:
    return math.log(p / (1 - p))


def by_level(outcome: Outcome, term: str) -> list[float]:
    """One term for list, badge, nudge and alert."""
    return [outcome.terms[f"{term}.{level}"] for level in SHOWN]


# ---- the score ------------------------------------------------------------------------------


def test_p_of_blends_clipped_logits_of_the_yes_no_answers() -> None:
    assert START_WEIGHTS == {
        "bias": -0.5,
        "goal_link": 0.6,
        "news": 0.5,
        "actionable": 0.4,
        "owner_only": 0.3,
        "fits_now": 0.2,
    }
    given: dict[str, Answer] = {
        "goal_link": NoulAnswer(0.8),
        "news": NoulAnswer(0.3),
        "actionable": NoulAnswer(1.0),  # clipped below 1 before its logit, then to +4
        "owner_only": NoulAnswer(0.0),  # and to -4
        "sensitive": NoulAnswer(1.0),  # carries no weight
    }  # fits_now is missing, so it adds nothing
    expected = -0.5 + 0.6 * logit(0.8) + 0.5 * logit(0.3) + 0.4 * 4 + 0.3 * -4
    assert p_of(given, START_WEIGHTS) == pytest.approx(sigmoid(expected))
    # A given weight replaces its start value; one the mapping lacks keeps it.
    expected = 0.25 + 1.0 * logit(0.8) + 0.5 * logit(0.3) + 0.4 * 4 + 0.3 * -4
    assert p_of(given, {"bias": 0.25, "goal_link": 1.0}) == pytest.approx(sigmoid(expected))
    # Unusable or missing answers add nothing; extreme weights saturate without overflowing.
    unusable: dict[str, Answer] = {
        "goal_link": NoulAnswer(math.nan),
        "news": ChoiceAnswer("yes", 1.0, {}),
    }
    assert p_of(unusable, START_WEIGHTS) == pytest.approx(sigmoid(-0.5))
    assert p_of({}, START_WEIGHTS) == pytest.approx(sigmoid(-0.5))
    assert p_of({"goal_link": NoulAnswer(0.0)}, {"goal_link": 1e300}) == 0.0
    assert p_of({"goal_link": NoulAnswer(1.0)}, {"goal_link": 1e300}) == 1.0


def test_value_caps_at_one_without_a_goal_link_and_a_near_deadline_rescales_reach() -> None:
    assert decide(arbiter_input()).terms["V"] == 3.0
    assert decide(arbiter_input(answers=answers(goal_link=NoulAnswer(0.5)))).terms["V"] == 3.0
    assert decide(arbiter_input(answers=answers(goal_link=NoulAnswer(0.45)))).terms["V"] == 1.0
    assert decide(arbiter_input(answers=answers(goal_link=None))).terms["V"] == 1.0
    assert by_level(decide(arbiter_input()), "T") == [0.3, 0.6, 0.9, 0.95]
    for deadline in (NOW + timedelta(hours=24), NOW - timedelta(hours=1)):
        out = decide(arbiter_input(candidate=candidate(deadline=deadline)))
        assert by_level(out, "T") == [0.15, 0.48, 0.9, 0.95]
    out = decide(arbiter_input(candidate=candidate(deadline=NOW + timedelta(hours=25))))
    assert by_level(out, "T") == [0.3, 0.6, 0.9, 0.95]


@pytest.mark.parametrize(
    ("changes", "costs"),
    [
        ({}, [0.0, 0.05, 0.4, 1.0]),
        ({"presence": presence(typing=True)}, [0.0, 0.05, 1.2, 3.0]),
        ({"settings": FOCUS}, [0.0, 0.05, 1.6, 2.0]),
        ({"settings": DecisionSettings(focus_until=NOW)}, [0.0, 0.05, 0.4, 1.0]),
        ({"presence": presence(away=True)}, [0.0, 0.05, 0.1, 0.3]),
        ({"presence": presence(away=True), "settings": FOCUS}, [0.0, 0.05, 0.4, 0.6]),
        ({"exposures_24h": 3}, [0.3, 0.35, 0.7, 1.3]),
        ({"exposures_24h": 9}, [0.5, 0.55, 0.9, 1.5]),
        ({"answers": answers(same_as_muted=NoulAnswer(0.4))}, [0.0, 0.25, 0.6, 1.2]),
        ({"answers": answers(same_as_muted=None)}, [0.0, 0.55, 0.9, 1.5]),
    ],
    ids=[
        "base",
        "typing",
        "focus",
        "focus_over",
        "away",
        "away_in_focus",
        "three_showings",
        "showings_cap",
        "like_muted",
        "like_muted_unknown",
    ],
)
def test_costs_follow_presence_focus_showings_and_likeness_to_muted(
    changes: dict[str, Any], costs: list[float]
) -> None:
    assert by_level(decide(arbiter_input(**changes)), "C") == pytest.approx(costs)


def test_each_level_scores_p_v_t_less_its_cost_pace_and_work() -> None:
    run = ChoiceAnswer("run", 0.9, {"run": 0.8})
    out = decide(arbiter_input(answers=answers(work=run), lam=1.2, exposures_24h=2))
    p = sigmoid(-0.5 + 0.6 * logit(0.95) + (0.5 + 0.4 + 0.3 + 0.2) * logit(0.9))
    assert out.terms["p"] == pytest.approx(p, abs=1e-4)
    assert out.terms["muK"] == 0.5
    assert by_level(out, "lamA") == pytest.approx([0.0, 0.12, 1.2, 3.6])
    reach, cost, alpha = (0.3, 0.6, 0.9, 0.95), (0.2, 0.25, 0.6, 1.2), (0.0, 0.1, 1.0, 3.0)
    for level, t, c, a in zip(SHOWN, reach, cost, alpha, strict=True):
        assert out.terms[f"S.{level}"] == pytest.approx(p * 3 * t - c - 1.2 * a - 0.5, abs=1e-3)
    prepare = ChoiceAnswer("prepare", 0.9, {"prepare": 0.9})
    assert decide(arbiter_input(answers=answers(work=prepare))).terms["muK"] == 0.2
    assert decide(arbiter_input()).terms["muK"] == 0.0


# ---- learned numbers ------------------------------------------------------------------------


def test_start_numbers_and_the_logit_feature() -> None:
    assert FEATURES == ("goal_link", "news", "actionable", "owner_only", "fits_now")
    assert START_REACH == {"list": 0.3, "badge": 0.6, "nudge": 0.9, "alert": 0.95}
    assert START_COSTS == {"badge": 0.05, "nudge": 0.4, "alert": 1.0}
    # Read-only, so every input shares them as its defaults.
    assert isinstance(START_REACH, MappingProxyType) and isinstance(START_COSTS, MappingProxyType)
    inp = arbiter_input()
    assert (inp.reach, inp.costs) == (START_REACH, START_COSTS)
    assert logit_feature(0.8) == pytest.approx(logit(0.8))
    assert logit_feature(0.5) == 0.0
    # Clipped within [1e-6, 1 - 1e-6] before the logit, then to ±4.
    assert (logit_feature(1.0), logit_feature(0.0)) == (4.0, -4.0)
    assert (logit_feature(1.5), logit_feature(-0.5)) == (4.0, -4.0)
    for unusable in (None, math.nan, math.inf, -math.inf):
        assert logit_feature(unusable) == 0.0


def test_p_adds_the_learned_offset_of_the_items_topic() -> None:
    given: dict[str, Answer] = {"goal_link": NoulAnswer(0.8), "news": NoulAnswer(0.3)}
    start = -0.5 + 0.6 * logit(0.8) + 0.5 * logit(0.3)
    topical = {**START_WEIGHTS, "topic.approvals": 0.7, "topic.inbox": -1.0}
    assert p_of(given, topical, "approvals") == pytest.approx(sigmoid(start + 0.7))
    assert p_of(given, topical, "inbox") == pytest.approx(sigmoid(start - 1.0))
    # No topic, a topic without an offset or an offset that is not a finite number adds nothing.
    for weights, topic in (
        (topical, None),
        (topical, "routine"),
        ({**START_WEIGHTS, "topic.routine": math.nan}, "routine"),
        ({**START_WEIGHTS, "topic.routine": math.inf}, "routine"),
    ):
        assert p_of(given, weights, topic) == pytest.approx(sigmoid(start)), topic
    # `decide` passes the item's type.
    for item_type in ("approvals", "inbox"):
        out = decide(arbiter_input(weights=topical, candidate=candidate(type=item_type)))
        assert out.terms["p"] == round(p_of(STRONG, topical, item_type), 4)
    assert decide(arbiter_input(weights=topical)).terms["p"] > decide(arbiter_input()).terms["p"]


def test_learned_numbers_change_terms_within_range() -> None:
    weights = {**START_WEIGHTS, "topic.approvals": 1.0}
    learned = {
        "weights": weights,
        "reach": {"list": 0.0, "badge": 0.5, "nudge": 1.0, "alert": 0.7},
        "costs": {"badge": 0.0, "nudge": 3.0, "alert": 0.25},
    }
    out = decide(arbiter_input(**learned))
    p = out.terms["p"]
    assert p == round(p_of(STRONG, weights, "approvals"), 4) != decide(arbiter_input()).terms["p"]
    reach, cost, alpha = (0.0, 0.5, 1.0, 0.7), (0.0, 0.0, 3.0, 0.25), (0.0, 0.1, 1.0, 3.0)
    assert by_level(out, "T") == list(reach)
    assert by_level(out, "C") == list(cost)
    for level, t, c, a in zip(SHOWN, reach, cost, alpha, strict=True):
        assert out.terms[f"S.{level}"] == pytest.approx(p * 3 * t - c - 0.3 * a, abs=1e-3)
    # The costly nudge leaves the item at badge, held by no gate.
    assert (out.level, out.blocked_by, out.reason) == ("badge", (), "model:badge")
    # A deadline within a day scales the learned list and badge reach as it does the start ones.
    soon = candidate(deadline=NOW + timedelta(hours=3))
    assert by_level(decide(arbiter_input(candidate=soon, **learned)), "T") == [0.0, 0.4, 1.0, 0.7]
    # Out of range, not a finite number or missing reads as the start value; list costs 0.
    odd = {
        "reach": {"list": -0.01, "badge": 1.01, "nudge": math.nan, "extra": 0.5},
        "costs": {"list": 2.0, "badge": -0.01, "nudge": 3.01, "alert": math.inf},
    }
    out = decide(arbiter_input(**odd))
    assert by_level(out, "T") == [0.3, 0.6, 0.9, 0.95]
    assert by_level(out, "C") == [0.0, 0.05, 0.4, 1.0]
    assert out == decide(arbiter_input(reach={}, costs={})) == decide(arbiter_input())


def test_away_scales_the_learned_nudge_and_alert_costs() -> None:
    away = presence(away=True)
    # 0.25 and 0.3 of the start costs are exactly the 0.1 and 0.3 away set before learning.
    assert (START_COSTS["nudge"] * 0.25, START_COSTS["alert"] * 0.3) == (0.1, 0.3)
    assert by_level(decide(arbiter_input(presence=away)), "C") == [0.0, 0.05, 0.1, 0.3]
    learned = {"badge": 0.2, "nudge": 2.0, "alert": 3.0}
    out = decide(arbiter_input(presence=away, costs=learned))
    assert by_level(out, "C") == pytest.approx([0.0, 0.2, 0.5, 0.9])
    # Typing still triples the scaled nudge and alert.
    typing = presence(away=True, typing=True)
    out = decide(arbiter_input(presence=typing, costs=learned))
    assert by_level(out, "C") == pytest.approx([0.0, 0.2, 1.5, 2.7])


# ---- gates ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("changes", "level", "blocked_by"),
    [
        ({}, "nudge", ()),
        ({"reversal_agrees": False}, "badge", ("reversal",)),
        ({"reversal_agrees": None}, "badge", ("reversal",)),
        ({"answers": answers(kind=ChoiceAnswer("ask", 0.55, {}))}, "badge", ("kind_confidence",)),
        ({"answers": answers(kind=ChoiceAnswer("ask", 0.45, {}))}, "list", ("kind_confidence",)),
        (
            {"answers": answers(importance=ScoreAnswer(3.0, 0.45, {}))},
            "badge",
            ("importance_confidence",),
        ),
        ({"answers": answers(goal_link=NoulAnswer(0.65))}, "badge", ("goal_link",)),
        (
            {"answers": answers(goal_link=NoulAnswer(0.4)), "current_level": "nudge"},
            "badge",
            ("goal_link",),
        ),
        ({"loud_last_hour": 2}, "badge", ("nudge_cap",)),
        (
            {"reversal_agrees": None, "loud_last_hour": 2},
            "badge",
            ("reversal", "nudge_cap"),
        ),
    ],
    ids=[
        "open",
        "reversal_disagrees",
        "reversal_unasked",
        "kind_below_nudge",
        "kind_below_badge",
        "importance_unsure",
        "goal_link_weak",
        "goal_link_below_half",
        "hour_used",
        "two_gates",
    ],
)
def test_gates_cap_the_level_and_name_what_held_it_back(
    changes: dict[str, Any], level: str, blocked_by: tuple[str, ...]
) -> None:
    out = decide(arbiter_input(**changes))
    assert (out.level, out.blocked_by) == (level, blocked_by)
    assert out.reason == (f"gate:{blocked_by[0]}" if blocked_by else f"model:{level}")


@pytest.mark.parametrize(
    ("kind_confidence", "importance_confidence", "level"),
    [
        (0.9, 0.9, "nudge"),
        (0.55, 0.9, "badge"),  # below the nudge floor, 0.6
        (0.65, 0.9, "nudge"),
        (0.9, 0.55, "nudge"),  # past the importance floor, 0.5
        (0.45, 0.9, "list"),  # below the badge floor, 0.5
    ],
)
def test_the_confidence_floors_are_the_same_for_a_title_in_any_script(
    kind_confidence: float, importance_confidence: float, level: str
) -> None:
    given = answers(
        kind=ChoiceAnswer("ask", kind_confidence, {}),
        importance=ScoreAnswer(3.0, importance_confidence, {}),
    )
    for title in ("Plan review", OTHER_SCRIPT):
        out = decide(arbiter_input(answers=given, candidate=candidate(title=title)))
        assert out.level == level, title


def due_task(**changes: Any) -> Candidate:
    """A task due in 90 minutes: with STRONG answers its nudge meets the alert rule."""
    return replace(
        candidate(type="tasks_due", deadline=NOW + timedelta(hours=1, minutes=30)), **changes
    )


def test_an_alert_fires_when_the_rule_holds_with_alerts_on() -> None:
    for deadline in (NOW + timedelta(hours=2), NOW - timedelta(hours=5)):  # or overdue
        out = decide(arbiter_input(candidate=due_task(deadline=deadline)))
        assert (out.level, out.timing, out.defer_until) == ("alert", "now", None)
        assert (out.reason, out.blocked_by) == ("model:alert", ())
        assert out.terms["S.alert"] < out.terms["S.nudge"]  # promoted, not out-scored
    # The rule needs a task due within 2 hours with a goal link of at least 0.7.
    for item, given, level in (
        (due_task(type="approvals"), STRONG, "nudge"),
        (due_task(deadline=NOW + timedelta(hours=2, minutes=1)), STRONG, "nudge"),
        (due_task(), answers(goal_link=NoulAnswer(0.65)), "badge"),
    ):
        assert decide(arbiter_input(candidate=item, answers=given)).level == level


def test_an_alert_never_fires_with_alerts_off() -> None:
    off = DecisionSettings(alerts=False)
    out = decide(arbiter_input(candidate=due_task(), settings=off))
    assert (out.level, out.timing) == ("nudge", "now")
    assert (out.blocked_by, out.reason) == (("alerts_off",), "gate:alerts_off")
    # The switch is named only when the alert rule holds.
    assert decide(arbiter_input(settings=off)).blocked_by == ()
    weak = answers(goal_link=NoulAnswer(0.65))
    out = decide(arbiter_input(candidate=due_task(), settings=off, answers=weak))
    assert (out.level, out.blocked_by) == ("badge", ("goal_link",))


def test_an_alert_never_fires_when_the_cap_blocks_the_nudge() -> None:
    for per_hour, loud in ((2, 2), (1, 1), (0, 0)):
        settings = DecisionSettings(alerts=True, nudges_per_hour=per_hour)
        out = decide(arbiter_input(candidate=due_task(), settings=settings, loud_last_hour=loud))
        assert (out.level, out.blocked_by) == ("badge", ("nudge_cap",))
    capped_and_off = arbiter_input(
        candidate=due_task(), settings=DecisionSettings(), loud_last_hour=2
    )
    assert decide(capped_and_off).blocked_by == ("nudge_cap", "alerts_off")


def test_an_alert_in_quiet_hours_waits_at_the_quiet_end_like_a_nudge() -> None:
    ends = NOW + timedelta(hours=7)
    out = decide(arbiter_input(candidate=due_task(), quiet_until=ends))
    assert (out.level, out.timing, out.defer_until) == ("nudge", "at", ends)
    assert (out.blocked_by, out.reason) == (("quiet_hours",), "gate:quiet_hours")
    # Once quiet hours end, the held nudge is promoted.
    assert decide(arbiter_input(candidate=due_task(), current_level="nudge")).level == "alert"
    # Typing, Focus and a rules deferral hold it as a nudge too; they are holds, not gates.
    for changes in (
        {"presence": presence(typing=True)},
        {"settings": FOCUS},
        {"rules_verdict": Verdict("defer", "later_by_owner", NOW + timedelta(hours=1))},
    ):
        out = decide(arbiter_input(candidate=due_task(), current_level="nudge", **changes))
        assert (out.level, out.blocked_by) == ("nudge", ()), changes
        assert out.timing != "now", changes


def test_a_held_alert_stays_while_its_promotion_holds() -> None:
    held = arbiter_input(candidate=due_task(), current_level="alert")
    assert decide(held).level == "alert"
    cases = [
        (replace(held, settings=DecisionSettings(alerts=False)), "nudge", ("alerts_off",)),
        (replace(held, candidate=due_task(type="approvals")), "nudge", ()),
        (replace(held, candidate=due_task(deadline=NOW + timedelta(hours=3))), "nudge", ()),
        (replace(held, quiet_until=NOW + timedelta(hours=7)), "nudge", ("quiet_hours",)),
        # Hysteresis treats it as a nudge: typing makes badge score higher, yet it stays a nudge.
        (replace(held, presence=presence(typing=True)), "nudge", ()),
        (replace(held, reversal_agrees=False), "badge", ("reversal",)),
        (replace(held, loud_last_hour=2), "badge", ("nudge_cap",)),
        (replace(held, answers=answers(importance=ScoreAnswer(0.0, 0.9, {}))), "silent", ()),
    ]
    for inp, level, blocked_by in cases:
        out = decide(inp)
        assert (out.level, out.blocked_by) == (level, blocked_by)


def test_nudge_cap() -> None:
    def level(per_hour: int, loud: int) -> str:
        settings = DecisionSettings(alerts=True, nudges_per_hour=per_hour)
        return decide(arbiter_input(settings=settings, loud_last_hour=loud)).level

    assert NUDGE_CAP == 2
    assert level(2, 1) == "nudge"
    assert level(2, 2) == "badge"
    assert level(1, 0) == "nudge"
    assert level(1, 1) == "badge"
    assert level(0, 0) == "badge"
    assert level(5, 2) == "badge"  # the cap bounds a larger setting
    assert level(5, 1) == "nudge"


def test_a_muted_topic_stays_silent_whatever_the_answers() -> None:
    muted = Verdict("silent", "muted_by_owner")
    for current in ("silent", "nudge"):
        out = decide(arbiter_input(rules_verdict=muted, current_level=current))
        assert (out.level, out.timing, out.work) == ("silent", "now", "none")
        assert (out.reason, out.blocked_by) == ("rules:muted_by_owner", ("muted",))


@pytest.mark.parametrize("name", sorted(STRONG))
def test_a_missing_or_unusable_answer_fails_quiet(name: str) -> None:
    missing = decide(arbiter_input(answers=answers(**{name: None})))
    expected = {"kind": "list", "importance": "silent", "goal_link": "badge"}.get(name, "nudge")
    assert missing.level == expected
    assert decide(arbiter_input(answers=answers(**{name: UNUSABLE[name]}))) == missing


def test_an_item_with_no_answer_the_arbiter_reads_stays_silent() -> None:
    out = decide(arbiter_input(answers={"sensitive": NoulAnswer(1.0)}))
    assert (out.level, out.kind, out.work) == ("silent", None, "none")
    wrong_type = answers(goal_link=ScoreAnswer(1.0, 1.0, {}))
    assert decide(arbiter_input(answers=wrong_type)) == decide(
        arbiter_input(answers=answers(goal_link=None))
    )


def test_own_question_gate_holds_silent() -> None:
    """An item of a type with own questions (`playbook.OWN`) shows only when each own answer
    reaches its floor: one missing, unusable or below it holds the item silent, and names the
    "own_question" gate, second only to a muted topic, when the score would have shown it. The
    rules alone still decide an item with no answers, and other types ignore own answers."""
    learn = candidate(type="learn_source", work_allowed=())
    skill = candidate(type="skill_offer", work_allowed=())
    pairs = ((learn, "worth_note", "worth_skill"), (skill, "worth_skill", "worth_note"))
    for item, name, other in pairs:
        assert OWN[item.type] == {name: 0.6}
        for p in (0.6, 0.61, 1.0, 1.5):  # at the floor or above (1.5 reads as 1)
            out = decide(arbiter_input(candidate=item, answers=answers(**{name: NoulAnswer(p)})))
            assert (out.level, out.blocked_by, out.reason) == ("nudge", (), "model:nudge"), p
        for given in (
            answers(),  # missing
            answers(**{name: NoulAnswer(0.59)}),
            answers(**{name: NoulAnswer(0.0)}),
            answers(**{name: NoulAnswer(-0.5)}),
            answers(**{name: NoulAnswer(math.nan)}),
            answers(**{name: NoulAnswer(math.inf)}),
            answers(**{name: ChoiceAnswer("yes", 0.9, {"yes": 0.9})}),
            answers(**{name: ScoreAnswer(3.0, 0.9, {3: 0.9})}),
            answers(**{other: NoulAnswer(0.9)}),  # another type's own question
        ):
            out = decide(arbiter_input(candidate=item, answers=given))
            assert (out.level, out.timing, out.work) == ("silent", "now", "none"), given
            assert (out.blocked_by, out.reason) == (("own_question",), "gate:own_question")
        held = answers(**{name: NoulAnswer(0.2)})
        # Named before the gates after it, and behind a muted topic.
        out = decide(arbiter_input(candidate=item, answers=held, reversal_agrees=False))
        assert (out.level, out.blocked_by) == ("silent", ("own_question", "reversal"))
        muted = Verdict("silent", "muted_by_owner")
        out = decide(arbiter_input(candidate=item, answers=held, rules_verdict=muted))
        assert (out.level, out.blocked_by) == ("silent", ("muted", "own_question"))
        assert out.reason == "rules:muted_by_owner"
        # The reversed kind question is not asked for an item the gate holds.
        unasked = arbiter_input(candidate=item, answers=held, reversal_agrees=None)
        assert not needs_reversal(decide(unasked), unasked)
        # With no answers at all, the rules decide.
        out = decide(arbiter_input(candidate=item, answers={}))
        assert (out.level, out.reason) == ("list", "rules:waiting_on_you")
    # A type without own questions ignores own answers.
    for given in (answers(worth_note=NoulAnswer(0.0)), answers(worth_skill=NoulAnswer(math.nan))):
        assert decide(arbiter_input(answers=given)) == decide(arbiter_input())
    assert GATES == (
        "muted",
        "own_question",
        "kind_confidence",
        "reversal",
        "importance_confidence",
        "goal_link",
        "nudge_cap",
        "alerts_off",
        "quiet_hours",
    )


def test_change_own_floor() -> None:
    """The paired vault-change types gate on their own question at 0.7, exactly as
    `test_own_question_gate_holds_silent` proves for learn_source/skill_offer at 0.6 -- one
    missing, unusable or below the floor holds the item silent behind the "own_question" gate; at
    or above it, the item shows."""
    link = candidate(type="note_link", work_allowed=())
    superseded = candidate(type="note_superseded", work_allowed=())
    pairs = ((link, "should_link", "superseded"), (superseded, "superseded", "should_link"))
    for item, name, other in pairs:
        assert OWN[item.type] == {name: 0.7}
        for p in (0.7, 0.71, 1.0, 1.5):  # at the floor or above (1.5 reads as 1)
            out = decide(arbiter_input(candidate=item, answers=answers(**{name: NoulAnswer(p)})))
            assert (out.level, out.blocked_by, out.reason) == ("nudge", (), "model:nudge"), p
        for given in (
            answers(),  # missing
            answers(**{name: NoulAnswer(0.69)}),
            answers(**{name: NoulAnswer(0.0)}),
            answers(**{name: NoulAnswer(-0.5)}),
            answers(**{name: NoulAnswer(math.nan)}),
            answers(**{name: NoulAnswer(math.inf)}),
            answers(**{name: ChoiceAnswer("yes", 0.9, {"yes": 0.9})}),
            answers(**{other: NoulAnswer(0.9)}),  # the other paired type's own question
        ):
            out = decide(arbiter_input(candidate=item, answers=given))
            assert (out.level, out.timing, out.work) == ("silent", "now", "none"), given
            assert (out.blocked_by, out.reason) == (("own_question",), "gate:own_question")
        # With no answers at all, the rules decide (PAIRED is silent "needs_decision_model").
        rules_silent = Verdict("silent", "needs_decision_model")
        out = decide(arbiter_input(candidate=item, answers={}, rules_verdict=rules_silent))
        assert (out.level, out.reason) == ("silent", "rules:needs_decision_model")


# ---- hysteresis -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("current", "importance", "showings", "like_muted", "expected"),
    [
        ("list", 1.2, 0, 0.0, "list"),  # badge 0.31 < list 0.18 + 0.15
        ("list", 1.5, 0, 0.0, "badge"),  # badge 0.40 >= list 0.225 + 0.15
        ("list", 2.4, 5, 0.1, "list"),  # badge 0.12 beats list -0.14 by more, but not 0 by 0.15
        ("list", 2.4, 5, 0.0, "badge"),  # badge 0.17 >= 0 + 0.15
        ("badge", 0.0, 0, 0.0, "badge"),  # badge -0.05 is not below -0.15
        ("badge", 0.0, 0, 0.3, "silent"),  # badge -0.2 is: the best quieter level, quieter on a tie
    ],
)
def test_hysteresis_margin(
    current: str, importance: float, showings: int, like_muted: float, expected: str
) -> None:
    assert MARGIN == 0.15
    given = answers(
        importance=ScoreAnswer(importance, 0.9, {}), same_as_muted=NoulAnswer(like_muted)
    )
    out = decide(
        arbiter_input(
            answers=given,
            weights=FLAT,
            lam=0.0,
            reversal_agrees=False,  # caps the level at badge
            current_level=current,
            exposures_24h=showings,
        )
    )
    assert out.level == expected


def test_a_closed_gate_moves_the_item_down_at_once() -> None:
    for held in ("nudge", "alert"):  # a held alert counts as a nudge
        out = decide(arbiter_input(current_level=held, reversal_agrees=False))
        assert (out.level, out.blocked_by) == ("badge", ("reversal",))
    assert decide(arbiter_input(current_level="nudge")).level == "nudge"
    assert decide(arbiter_input(current_level="alert")).level == "nudge"  # no alert rule here
    assert decide(arbiter_input(current_level="loudest")).level == "nudge"  # unknown reads silent


# ---- timing ---------------------------------------------------------------------------------


def test_quiet_hours_move_to_at() -> None:
    ends = NOW + timedelta(hours=7)
    out = decide(arbiter_input(quiet_until=ends))
    assert (out.level, out.timing, out.defer_until) == ("nudge", "at", ends)
    typing = arbiter_input(quiet_until=ends, current_level="nudge", presence=presence(typing=True))
    assert (decide(typing).timing, decide(typing).defer_until) == ("at", ends)
    for later in (NOW + timedelta(hours=9), NOW + timedelta(hours=1)):
        deferred = Verdict("defer", "later_by_owner", later)
        out = decide(arbiter_input(quiet_until=ends, rules_verdict=deferred))
        assert (out.timing, out.defer_until) == ("at", max(later, ends))
    # Quiet hours hold only loud levels.
    out = decide(arbiter_input(quiet_until=ends, reversal_agrees=False))
    assert (out.level, out.timing, out.defer_until) == ("badge", "now", None)


def test_a_rules_deferral_holds_the_model_outcome_until_its_time() -> None:
    later = NOW + timedelta(hours=4)
    out = decide(arbiter_input(rules_verdict=Verdict("defer", "later_by_owner", later)))
    assert (out.level, out.timing, out.defer_until, out.reason) == (
        "nudge",
        "at",
        later,
        "model:nudge",
    )
    # A deferral without its time cannot be held to one.
    out = decide(arbiter_input(rules_verdict=Verdict("defer", "later_by_owner", None)))
    assert (out.timing, out.defer_until) == ("now", None)


def test_typing_or_focus_holds_a_loud_level_until_after_focus() -> None:
    for changes in ({"presence": presence(typing=True)}, {"settings": FOCUS}):
        out = decide(arbiter_input(current_level="nudge", **changes))
        assert (out.level, out.timing, out.defer_until) == ("nudge", "after_focus", None)
    ended = DecisionSettings(alerts=True, focus_until=NOW - timedelta(minutes=1))
    assert decide(arbiter_input(current_level="nudge", settings=ended)).timing == "now"
    out = decide(arbiter_input(presence=presence(typing=True), reversal_agrees=False))
    assert (out.level, out.timing) == ("badge", "now")


def test_an_item_about_another_chat_or_note_waits_for_it() -> None:
    elsewhere = answers(fits_now=NoulAnswer(0.1))
    for related in ({"related_chat": "chat-2"}, {"related_ref": "notes/plan.md"}):
        out = decide(arbiter_input(answers=elsewhere, candidate=candidate(**related)))
        assert (out.level, out.timing, out.defer_until) == ("nudge", "in_context", None)
    base = {"answers": elsewhere, "candidate": candidate(related_chat="chat-2")}
    for changes in (
        {"settings": DecisionSettings(alerts=True, in_context=False)},
        {"candidate": candidate()},
        {"answers": answers(fits_now=NoulAnswer(0.5))},
        {"answers": answers(fits_now=None)},
        {"candidate": candidate(related_chat="chat-2", deadline=NOW + timedelta(hours=20))},
        {"current_level": "nudge", "presence": presence(typing=True)},
    ):
        out = decide(arbiter_input(**(base | changes)))
        assert out.timing == ("after_focus" if "presence" in changes else "now"), changes
    # Showing it there must gain the margin: list 0.03 now against 0.10 there stays now.
    listed = {"weights": FLAT, "current_level": "list"} | base
    low = answers(fits_now=NoulAnswer(0.1), importance=ScoreAnswer(0.2, 0.9, {}))
    assert decide(arbiter_input(**(listed | {"answers": low}))).timing == "now"
    high = answers(fits_now=NoulAnswer(0.1), importance=ScoreAnswer(1.0, 0.9, {}))
    out = decide(arbiter_input(**(listed | {"answers": high})))
    assert (out.level, out.timing) == ("list", "in_context")


# ---- work -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("probabilities", "changes", "work"),
    [
        ({"prepare": 0.5, "run": 0.3}, {}, "prepare"),
        ({"prepare": 0.2, "run": 0.6}, {}, "run"),  # run's default grant, ask, is not off
        ({"prepare": 0.3, "run": 0.39, "none": 0.31}, {}, "none"),  # 0.69 < 0.7
        ({"coordinate": 0.9}, {}, "prepare"),  # never coordinate; a tie goes to prepare
        (
            {"prepare": 0.2, "run": 0.6},
            {"settings": DecisionSettings(alerts=True, grant_run="off")},
            "prepare",
        ),
        (
            {"prepare": 0.5, "run": 0.3},
            {"settings": DecisionSettings(alerts=True, grant_prepare="off", grant_run="off")},
            "none",
        ),
        ({"prepare": 0.5, "run": 0.3}, {"candidate": candidate(work_allowed=("run",))}, "run"),
        (
            {"prepare": 0.5, "coordinate": 0.4},
            {"candidate": candidate(work_allowed=("coordinate",))},
            "none",
        ),
    ],
    ids=[
        "prepare",
        "run",
        "below_floor",
        "coordinate",
        "run_off",
        "both_off",
        "only_run_allowed",
        "only_coordinate_allowed",
    ],
)
def test_work_follows_the_answer_within_the_grants(
    probabilities: dict[str, float], changes: dict[str, Any], work: str
) -> None:
    given = answers(work=ChoiceAnswer("prepare", 0.9, probabilities))
    assert decide(arbiter_input(answers=given, **changes)).work == work


def test_a_silent_item_carries_no_work() -> None:
    given = answers(
        work=ChoiceAnswer("prepare", 0.9, {"prepare": 0.9}), importance=ScoreAnswer(0.0, 0.9, {})
    )
    out = decide(arbiter_input(answers=given))
    assert (out.level, out.work) == ("silent", "none")


# ---- rules only, the reversal and pacing -----------------------------------------------------


@pytest.mark.parametrize(
    ("verdict", "expected"),
    [
        (Verdict("notify", "waiting_on_you"), ("list", "now", None)),
        (Verdict("silent", "runs_working"), ("silent", "now", None)),
        (Verdict("silent", "muted_by_owner"), ("silent", "now", None)),
        (
            Verdict("defer", "later_by_owner", NOW + timedelta(hours=4)),
            ("silent", "at", NOW + timedelta(hours=4)),
        ),
        (Verdict("defer", "later_by_owner", None), ("silent", "now", None)),
    ],
)
def test_rules_only_mapping(verdict: Verdict, expected: tuple[str, str, datetime | None]) -> None:
    out = decide(arbiter_input(answers={}, rules_verdict=verdict, current_level="nudge"))
    assert (out.level, out.timing, out.defer_until) == expected
    assert (out.work, out.kind, out.terms, out.blocked_by) == ("none", None, {}, ())
    assert out.reason == f"rules:{verdict.reason}"


def test_a_rules_verdict_from_the_rules_module_drives_a_rules_only_outcome() -> None:
    from viki_agent.decision_loop.rules import Verdict as RulesVerdict

    verdict = RulesVerdict("approvals", {}, "notify", "waiting_on_you", None)
    out = decide(arbiter_input(answers={}, rules_verdict=verdict))
    assert (out.level, out.reason) == ("list", "rules:waiting_on_you")
    long = decide(arbiter_input(answers={}, rules_verdict=Verdict("notify", "x" * 80)))
    assert long.reason == ("rules:" + "x" * 80)[:64]


def test_needs_reversal_only_when_its_agreement_would_make_the_item_loud() -> None:
    unasked = arbiter_input(reversal_agrees=None)
    out = decide(unasked)
    assert (out.level, out.blocked_by) == ("badge", ("reversal",))
    assert needs_reversal(out, unasked)
    for inp in (
        arbiter_input(reversal_agrees=True),
        arbiter_input(reversal_agrees=False),
        arbiter_input(reversal_agrees=None, loud_last_hour=2),  # the cap holds it anyway
        arbiter_input(reversal_agrees=None, answers=answers(importance=ScoreAnswer(1.0, 0.9, {}))),
        arbiter_input(reversal_agrees=None, answers={}),
    ):
        assert not needs_reversal(decide(inp), inp)


def test_pace_clamps() -> None:
    assert (START_LAMBDA, LAMBDA_MAX) == (0.3, 3.0)
    assert pace(0.3, 0.0, 2) == pytest.approx(0.3 - 0.5 * 2 / 12)  # under its share: λ falls
    assert pace(0.3, 1.0, 2) == pytest.approx(0.3 + 0.5 * (1 - 2 / 12))  # over it: λ rises
    assert pace(1.0, 2.0, 12) == pytest.approx(1.5)
    assert pace(0.3, 0.0, 0) == 0.3
    assert pace(0.05, 0.0, 2) == 0.0
    assert pace(2.9, 5.0, 2) == 3.0
    assert pace(math.nan, 0.0, 2) == LAMBDA_MAX
    assert pace(0.3, math.inf, 2) == LAMBDA_MAX


def test_lambda_of_reads_a_stored_lambda_within_its_range() -> None:
    assert lambda_of(1.2) == 1.2
    assert (lambda_of(-1), lambda_of(5), lambda_of(2)) == (0.0, LAMBDA_MAX, 2.0)
    assert isinstance(lambda_of(2), float)
    for odd in (None, True, "0.5", [1.0], math.nan, math.inf):
        assert lambda_of(odd) == START_LAMBDA, odd


# ---- authority ------------------------------------------------------------------------------

# The candidate types `START_DECISIONS` was recorded over: `random_input` draws from these, so the
# record holds while `CANDIDATES` grows.
RECORDED_TYPES = (
    "approvals",
    "tasks_due",
    "agent_runs",
    "inbox",
    "uncommitted",
    "routine",
    "mission_report",
)
# Every type the authority property draws: each candidate type, and each type with own
# questions (`playbook.OWN`) even while `CANDIDATES` does not list it yet.
TYPES = (*CANDIDATES, *(name for name in OWN if name not in CANDIDATES))


def random_input(rng: random.Random) -> ArbiterInput:
    """A random input. The user's side (switches, grants, quiet hours, presence, counts) is drawn
    evenly; half the draws lean the model's side loud (high answers, the reversal mostly agreeing,
    a low λ, often a task due soon) so that those bounds have something to hold back. Current
    levels are drawn evenly, with no extra held alerts."""
    lean = rng.random() < 0.5
    now = NOW + timedelta(minutes=rng.randrange(24 * 60))

    def chance(low: float = 0.0) -> float:
        roll = rng.random()
        if roll < 0.03:
            return math.nan
        if roll < 0.06:
            return rng.choice((-0.5, 0.0, 1.0, 1.5))
        return rng.uniform(low if lean else 0.0, 1.0)

    def noul(name: str) -> NoulAnswer:
        if name == "same_as_muted":
            return NoulAnswer(1 - chance(0.7))
        return NoulAnswer(chance(0.0 if name == "fits_now" else 0.7))

    given: dict[str, Answer] = {name: noul(name) for name in NOULS if rng.random() < 0.9}
    if rng.random() < 0.9:
        score = rng.uniform(2.5, 3.0) if lean else rng.uniform(-0.5, 3.5)
        given["importance"] = ScoreAnswer(score, chance(0.75), {})
    if rng.random() < 0.9:
        given["kind"] = ChoiceAnswer(rng.choice((*KINDS, "shout")), chance(0.75), {})
    if rng.random() < 0.9:
        given["work"] = ChoiceAnswer(rng.choice(WORK), chance(), {w: chance() for w in WORK})
    if rng.random() < 0.05:
        given = {}
    if rng.random() < (0.8 if lean else 0.3):
        deadline = now + timedelta(minutes=rng.uniform(-60, 120))
    else:
        deadline = rng.choice((None, now + timedelta(hours=rng.uniform(2, 30))))
    item = candidate(
        type="tasks_due" if rng.random() < (0.8 if lean else 0.3) else rng.choice(RECORDED_TYPES),
        work_allowed=tuple(w for w in WORK if rng.random() < 0.6),
        title=rng.choice(("Plan review", "Plan review", OTHER_SCRIPT)),
        deadline=deadline,
        related_chat=rng.choice((None, "chat-2")),
        related_ref=rng.choice((None, "notes/plan.md")),
    )
    attention = rng.choice(("notify", "notify", "silent", "defer"))
    reason = rng.choice(("muted_by_owner", "runs_working"))
    later = now + timedelta(hours=rng.uniform(0.5, 4)) if rng.random() < 0.9 else None
    verdict = Verdict(
        attention,
        reason if attention == "silent" else "later_by_owner",
        later if attention == "defer" else None,
    )
    quiet_start, quiet_end = rng.sample(QUIET_TIMES, 2)
    settings = DecisionSettings(
        alerts=rng.random() < 0.5,
        nudges_per_hour=rng.choice((0, 1, 2)),
        grant_prepare=rng.choice(GRANTS),
        grant_run=rng.choice(GRANTS),
        in_context=rng.random() < 0.5,
        focus_until=rng.choice((None, None, now - timedelta(minutes=5), now + timedelta(hours=1))),
        quiet=(quiet_start, quiet_end) if rng.random() < 0.5 else None,
    )
    return ArbiterInput(
        candidate=item,
        answers=given,
        reversal_agrees=rng.choice((True, True, True, None) if lean else (True, False, None)),
        rules_verdict=verdict,
        settings=settings,
        presence=presence(
            focused=rng.random() < 0.7, typing=rng.random() < 0.2, away=rng.random() < 0.15
        ),
        quiet_until=quiet_until(settings.quiet, now),
        current_level=rng.choice(LEVELS),
        exposures_24h=rng.randrange(3 if lean else 9),
        loud_last_hour=rng.choice((0, 0, 1, 2, 3)),
        lam=rng.uniform(0.0, 0.3) if lean else rng.uniform(0.0, 3.0),
        weights={name: w + rng.uniform(-0.5, 0.5) for name, w in START_WEIGHTS.items()},
        now=now,
    )


def own_answers(inp: ArbiterInput, rng: random.Random, type_rng: random.Random) -> ArbiterInput:
    """`inp` with a draw of own answers: about a third of the items take a type with own
    questions (`playbook.OWN`) and a tenth any of `TYPES`; such an item with answers gets each
    own answer missing, below its floor, at it or above, or out of range, not a finite number or
    of another shape. The type itself is `type_rng`'s draw, a stream of its own, so a new entry
    in `OWN` or `TYPES` changes only which type a seed gets, never `rng`'s own later draws."""
    roll = rng.random()
    if roll < 0.35:
        item_type = type_rng.choice(tuple(OWN))
    elif roll < 0.45:
        item_type = type_rng.choice(TYPES)
    else:
        item_type = inp.candidate.type
    given = dict(inp.answers)
    floors = OWN.get(item_type, {}) if given else {}  # with no answers the rules decide
    for name, floor in floors.items():
        draw = rng.random()
        if draw < 0.25:
            given.pop(name, None)
        elif draw < 0.5:
            given[name] = NoulAnswer(rng.uniform(0.0, floor))
        elif draw < 0.85:
            given[name] = NoulAnswer(rng.choice((floor, rng.uniform(floor, 1.0))))
        else:
            odd: tuple[Answer, ...] = (
                NoulAnswer(math.nan),
                NoulAnswer(math.inf),
                NoulAnswer(-0.5),
                NoulAnswer(1.5),
                ChoiceAnswer("yes", 0.9, {"yes": 0.9}),
            )
            given[name] = rng.choice(odd)
    return replace(inp, candidate=replace(inp.candidate, type=item_type), answers=given)


def own_held(inp: ArbiterInput) -> bool:
    """Whether an own answer of the item's type is missing, not a finite yes/no answer or below
    its floor."""
    for name, floor in OWN.get(inp.candidate.type, {}).items():
        answer = inp.answers.get(name)
        if not (isinstance(answer, NoulAnswer) and math.isfinite(answer.p) and answer.p >= floor):
            return True
    return False


def learned_numbers(inp: ArbiterInput, rng: random.Random, type_rng: random.Random) -> ArbiterInput:
    """`inp` with a second draw, of learned numbers in and out of their ranges: an offset in ±2
    for each type's topic (`TYPES`, drawn from `type_rng` so a new type only adds its own offset
    and never shifts `rng`'s later draws), T per shown level in −0.2..1.2 and C per shown level
    in −0.5..3.5; 3% of the numbers are NaN."""

    def number(source: random.Random, low: float, high: float) -> float:
        return math.nan if source.random() < 0.03 else source.uniform(low, high)

    topics = {f"topic.{name}": number(type_rng, -2.0, 2.0) for name in TYPES}
    return replace(
        inp,
        weights={**inp.weights, **topics},
        reach={level: number(rng, -0.2, 1.2) for level in SHOWN},
        costs={level: number(rng, -0.5, 3.5) for level in SHOWN},
    )


# `decide` over `random_input(Random(seed))` for seeds 0..1999: the SHA-256 of the canonical JSON
# of each outcome's level, timing, defer_until, work, kind, reason and blocked_by, recorded on the
# arbiter before it took learned numbers.
START_DECISIONS = "14d8cdda7a4f63a687beb879bee600639692ad91c124ccaadcd02bc1396590f6"


def test_start_values_decide_as_before() -> None:
    outcomes = (decide(random_input(random.Random(seed))) for seed in range(2000))
    rows = [
        (out.level, out.timing, out.defer_until, out.work, out.kind, out.reason, out.blocked_by)
        for out in outcomes
    ]
    assert sha256_hex(canonical_json(rows)) == START_DECISIONS


def unbounded(inp: ArbiterInput) -> ArbiterInput:
    """`inp` with every bound the user sets lifted: alerts on, two nudges an hour with none used,
    both grants allowed, no quiet hours, typing or Focus; and the reversal agreeing."""
    settings = replace(
        inp.settings,
        alerts=True,
        nudges_per_hour=2,
        grant_prepare="allowed",
        grant_run="allowed",
        focus_until=None,
    )
    return replace(
        inp,
        settings=settings,
        presence=replace(inp.presence, typing=False),
        quiet_until=None,
        loud_last_hour=0,
        reversal_agrees=True,
    )


@pytest.mark.parametrize("learned", [False, True], ids=["start", "learned"])
def test_authority_invariance(learned: bool) -> None:
    """2,000 seeded random inputs, with a draw of own answers (`own_answers`), at the start
    numbers or with a second draw of learned ones in and out of their ranges
    (`learned_numbers`), never give work beyond the grants, coordinate, an alert with alerts
    off, a nudge or alert at 0 an hour, past the hourly cap or without an agreeing reversal, a
    loud level shown now in quiet hours, while typing or in Focus, a muted topic above silent,
    an item whose own answer is missing, unusable or below its floor above silent, or an alert
    other than a nudge shown now on a task due within 2 hours. Each user-set bound held back some
    draw that would have gone past it had the user allowed it, items not held at alert were
    promoted to one, and the own-question gate held back items that would have shown while items
    whose own answers reached their floors showed."""
    levels: Counter[str] = Counter()
    works: Counter[str] = Counter()
    timings: Counter[str] = Counter()
    held: Counter[str] = Counter()
    asks = promoted = own_gated = own_shown = 0
    for seed in range(2000):
        rng = random.Random(seed)
        type_rng = random.Random(f"type:{seed}")
        inp = own_answers(random_input(rng), rng, type_rng)
        if learned:
            inp = learned_numbers(inp, rng, type_rng)
        out = decide(inp)
        settings, item = inp.settings, inp.candidate
        grants = {"prepare": settings.grant_prepare, "run": settings.grant_run}
        focus = settings.focus_until is not None and settings.focus_until > inp.now
        hour_used = settings.nudges_per_hour == 0 or inp.loud_last_hour >= min(
            settings.nudges_per_hour, NUDGE_CAP
        )
        loud = out.level in LOUD
        assert out.work in ("none", "prepare", "run"), seed
        assert out.work == "none" or (
            out.work in item.work_allowed and grants.get(out.work, "off") != "off"
        ), seed
        assert settings.alerts or out.level != "alert", seed
        if out.level == "alert":
            assert (out.timing, item.type) == ("now", "tasks_due"), seed
            assert item.deadline is not None, seed
            assert item.deadline - inp.now <= timedelta(hours=2), seed
            promoted += inp.current_level != "alert"
        assert not (loud and hour_used), seed
        assert not (loud and inp.reversal_agrees is not True), seed
        if loud and inp.quiet_until is not None:
            assert out.timing == "at", seed
            assert out.defer_until is not None and out.defer_until >= inp.quiet_until, seed
        if loud and (inp.presence.typing or focus):
            assert out.timing in ("at", "after_focus"), seed
        if not inp.answers:
            assert (out.level in ("silent", "list"), out.work) == (True, "none"), seed
        if inp.rules_verdict.reason == "muted_by_owner":
            assert out.level == "silent", seed
        if inp.answers and own_held(inp):
            assert (out.level, out.work) == ("silent", "none"), seed
            assert not out.blocked_by or "own_question" in out.blocked_by[:2], seed
            passing = dict.fromkeys(OWN[item.type], NoulAnswer(1.0))
            own_gated += decide(replace(inp, answers={**inp.answers, **passing})).level != "silent"
        elif inp.answers and item.type in OWN:
            own_shown += out.level != "silent"
        # A well-formed, storable and repeatable outcome.
        assert out == decide(inp), seed
        assert out.level in LEVELS and out.timing in TIMINGS, seed
        assert out.kind is None or out.kind in KINDS, seed
        assert (out.timing == "at") == (out.defer_until is not None), seed
        assert 1 <= len(out.reason) <= 64, seed
        assert 2 <= len(canonical_json(out.terms)) <= 512, seed
        assert set(out.blocked_by) <= set(GATES), seed
        if needs_reversal(out, inp):
            assert inp.reversal_agrees is None, seed
            assert decide(replace(inp, reversal_agrees=True)).level in LOUD, seed
            asks += 1
        levels[out.level] += 1
        works[out.work] += 1
        timings[out.timing] += 1
        free = decide(unbounded(inp))
        free_loud = free.level in LOUD
        held["alerts off"] += free.level == "alert" and not settings.alerts
        held["no nudges"] += free_loud and settings.nudges_per_hour == 0
        held["hour used"] += free_loud and hour_used and settings.nudges_per_hour > 0
        held["grant off"] += free.work != "none" and grants[free.work] == "off"
        held["quiet hours"] += free_loud and inp.quiet_until is not None
        held["typing or Focus"] += free_loud and (inp.presence.typing or focus)
    assert set(levels) == set(LEVELS), levels
    assert set(timings) == set(TIMINGS), timings
    assert works["prepare"] > 0 and works["run"] > 0, works
    assert asks > 0 and promoted > 0, (asks, promoted)
    bounds = ("alerts off", "no nudges", "hour used", "grant off", "quiet hours", "typing or Focus")
    assert all(held[bound] > 0 for bound in bounds), held
    assert own_gated > 0 and own_shown > 0, (own_gated, own_shown)


@pytest.mark.parametrize("card_type", CARDS)
def test_a_kanban_card_never_gets_work_or_an_alert(card_type: str) -> None:
    """The card types sit outside `RECORDED_TYPES` (widening it would move `START_DECISIONS`), so
    this property runs the same draws with the candidate swapped for a card with no grants and
    no deadline."""
    for seed in range(500):
        inp = random_input(random.Random(seed))
        inp = replace(
            inp, candidate=replace(inp.candidate, type=card_type, work_allowed=(), deadline=None)
        )
        out = decide(inp)
        assert out.work == "none", seed
        assert out.level != "alert", seed
        if not inp.answers:
            assert out.level in ("silent", "list"), seed
