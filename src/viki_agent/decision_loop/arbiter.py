"""The Decision arbiter: how loud, when and with what work VIKI brings up one item.

`decide` scores each level d as p·V·T(d) − C(d) − λ·α(d) − μ·K(w), silent scoring 0: p blends
the yes/no answers and the item's topic (`p_of`), V is the importance score, T is how well a
level reaches the user and C what it costs them (more while they type or are in Focus, for an
item already shown that day and for one like the items they muted), α is `contracts.ALPHA`
priced by λ (`pace`) and K is the cost of the work the answers ask for. p's weights, T and C
come in `ArbiterInput`, learned or at their start values (`START_WEIGHTS`, `START_REACH`,
`START_COSTS`); a T or C out of its range reads as its start value. Gates cap the level (an
item of a type with own questions, `playbook.OWN`, stays silent while an own answer is missing,
not a finite number or below its floor), hysteresis picks among silent to nudge and keeps an
item where it is unless a move gains `MARGIN`, and the timing holds a loud level through quiet
hours, typing and Focus and keeps an item about another chat or note until that opens. An alert
is a promoted nudge: a nudge shown now becomes an alert when the user has alerts on and the item
is a goal-linked task due within 2 hours or overdue; the alert's own score is kept for display
only. Without answers the rules verdict maps straight to a level.

The user's bounds are caps applied after scoring: the alert switch, the hourly nudge cap, quiet
hours, typing and Focus, a muted topic and the work grants. No answer, weight, learned number or
λ lifts an item past them and `coordinate` is never chosen; `test_authority_invariance` pins
both, and the own-question gate's silence, with own answers and learned numbers drawn in and
out of their ranges.

Pure: no I/O, no clock reads (time comes in `ArbiterInput.now`) and no logging.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Protocol, TypeGuard

from viki_agent.decision_loop.contracts import (
    ALPHA,
    KINDS,
    LEVELS,
    NUDGE_CAP,
    Answers,
    Candidate,
    ChoiceAnswer,
    DecisionSettings,
    NoulAnswer,
    Outcome,
    Presence,
    ScoreAnswer,
)
from viki_agent.decision_loop.playbook import OWN

START_WEIGHTS: Mapping[str, float] = {
    "bias": -0.5,
    "goal_link": 0.6,
    "news": 0.5,
    "actionable": 0.4,
    "owner_only": 0.3,
    "fits_now": 0.2,
}
FEATURES = tuple(name for name in START_WEIGHTS if name != "bias")  # the yes/no answers p weighs
# T per shown level, and C per level louder than list (list costs 0), before any learning.
START_REACH: Mapping[str, float] = MappingProxyType(
    {"list": 0.3, "badge": 0.6, "nudge": 0.9, "alert": 0.95}
)
START_COSTS: Mapping[str, float] = MappingProxyType({"badge": 0.05, "nudge": 0.4, "alert": 1.0})
START_LAMBDA = 0.3
LAMBDA_MAX = 3.0
MARGIN = 0.15  # what a move between levels must gain
# Every gate, in the order `Outcome.blocked_by` names them.
GATES = (
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

_SHOWN = ("list", "badge", "nudge", "alert")  # every level but silent
_SCORED = ("silent", "list", "badge", "nudge")  # what the score picks from; alert is promoted
_LOUD = ("nudge", "alert")
_RANK: Mapping[str, int] = {level: rank for rank, level in enumerate(LEVELS)}
_COST_MAX = 3.0  # the most a learned C may be; T's range is 0..1
_WORK_COST: Mapping[str, float] = {"none": 0.0, "prepare": 0.2, "run": 0.5}  # K
_MU = 1.0
_LOGIT_CLIP = 4.0
_P_CLIP = 1e-6
_WORK_FLOOR = 0.7
_SOON = timedelta(hours=24)
_ALERT_WINDOW = timedelta(hours=2)
_REASON_CHARS = 64
_PLACES = 4
_MUTED = "muted_by_owner"


class RulesVerdict(Protocol):
    """What the arbiter reads of a rules verdict: `attention` is silent, defer or notify, and
    `defer_until` is set for defer."""

    @property
    def attention(self) -> str: ...

    @property
    def reason(self) -> str: ...

    @property
    def defer_until(self) -> datetime | None: ...


@dataclass(frozen=True)
class ArbiterInput:
    """One item's decision inputs.

    `answers` may be empty (the rules decide) or partial. `reversal_agrees` is None until the
    reversed kind question is asked. `quiet_until` is `contracts.quiet_until` at `now`, None
    outside quiet hours. `current_level` is the item's stored level, "silent" for a new item
    (any other value reads as silent). `exposures_24h` counts the item's showings in the last day
    and `loud_last_hour` the nudges and alerts shown in the last hour. `lam` is λ from `pace` and
    `weights` are `p_of`'s. `now` is an aware datetime. `reach` (T per shown level) and `costs`
    (C of badge, nudge and alert) are learned or the start values: a T outside 0..1 or a C
    outside 0..3, missing or not a finite number, reads as its start value.
    """

    candidate: Candidate
    answers: Answers
    reversal_agrees: bool | None
    rules_verdict: RulesVerdict
    settings: DecisionSettings
    presence: Presence
    quiet_until: datetime | None
    current_level: str
    exposures_24h: int
    loud_last_hour: int
    lam: float
    weights: Mapping[str, float]
    now: datetime
    reach: Mapping[str, float] = START_REACH
    costs: Mapping[str, float] = START_COSTS


@dataclass(frozen=True)
class _Read:
    """The answers `decide` reads; None marks one that is missing or unusable."""

    goal_link: float | None
    same_as_muted: float | None
    fits_now: float | None
    importance: float | None  # the score, within 0..3
    importance_confidence: float | None
    kind: str | None
    kind_confidence: float | None


def decide(inp: ArbiterInput) -> Outcome:
    """The outcome for one item.

    With no answers the rules verdict decides: notify → list, silent → silent, defer → silent at
    its time, with no work and no terms. Otherwise a missing or unusable answer fails every gate
    that reads it, makes V 0 (importance), counts as below 0.5 (goal_link) or as 1
    (same_as_muted), and starts no in-context hold (fits_now) and no work; an own answer
    (`playbook.OWN`) missing, unusable or below its floor fails the own_question gate, which
    holds the item silent. A rules deferral, or quiet hours for a loud level, hold the item to a
    time (the later of the two); typing or Focus hold a loud level until after focus; then an
    item about another chat or note waits for it when showing it there scores at least `MARGIN`
    more. A nudge left to show now becomes an alert when alerts are on, quiet hours are off and
    the alert rule holds: a task due within 2 hours or overdue with a goal link of at least 0.7.
    A held alert counts as a nudge for hysteresis, so it stays an alert while that promotion
    holds. A silent item carries no work.

    `terms` holds p, V and muK, and per shown level `T.`, `C.`, `lamA.` (λ·α) and `S.` (its
    score), rounded to 4 places. When a gate kept the item below the level it would take with
    every gate open (an alert when the rule holds), `blocked_by` names every gate that level
    fails. `reason` is "rules:" and the rules reason for a rules decision or a muted topic,
    "gate:" and the first gate when one held the item back, else "model:" and the level.
    """
    verdict = inp.rules_verdict
    if not inp.answers:
        return _rules_only(verdict)
    read = _read(inp.answers)
    focus = inp.settings.focus_until is not None and inp.settings.focus_until > inp.now
    muted = verdict.attention == "silent" and verdict.reason == _MUTED
    p = p_of(inp.answers, inp.weights, inp.candidate.type)
    value = 0.0 if read.importance is None else read.importance
    if read.goal_link is None or read.goal_link < 0.5:
        value = min(value, 1.0)
    reach = _reach(inp)
    cost = _cost(inp, read, focus)
    lam = min(max(inp.lam, 0.0), LAMBDA_MAX) if _finite(inp.lam) else LAMBDA_MAX
    work = _work(inp)
    mu_k = _MU * _WORK_COST[work]
    score = {"silent": 0.0} | {
        level: p * value * reach[level] - cost[level] - lam * ALPHA[level] - mu_k
        for level in _SHOWN
    }
    failing = _failing(inp, read, muted)
    cap = "silent"
    for level in _SCORED[1:]:
        if failing[level]:
            break
        cap = level
    # The score picks up to nudge; a held alert is a promoted nudge, so it counts as one here.
    current = inp.current_level if inp.current_level in _RANK else "silent"
    if current == "alert":
        current = "nudge"
    level = _choose(current, cap, score)
    wanted = _choose(current, "nudge", score)
    if wanted == "nudge" and _alert_rule(inp, read):
        wanted = "alert"
    in_context = (
        level != "silent"
        and _fits_elsewhere(inp, read)
        and p * value - 0.5 * cost[level] - lam * ALPHA[level] - mu_k >= score[level] + MARGIN
    )
    timing, defer_until = _timing(inp, level, focus, in_context)
    if level == "nudge" and wanted == "alert" and timing == "now" and not failing["alert"]:
        level = "alert"
    blocked_by: tuple[str, ...] = ()
    if _RANK[wanted] > _RANK[level]:
        blocked_by = tuple(gate for gate in GATES if gate in failing[wanted])
    if muted:
        reason = _cut("rules:" + verdict.reason)
    elif blocked_by:
        reason = "gate:" + blocked_by[0]
    else:
        reason = "model:" + level
    terms = {"p": p, "V": value, "muK": mu_k}
    for shown in _SHOWN:
        terms[f"T.{shown}"] = reach[shown]
        terms[f"C.{shown}"] = cost[shown]
        terms[f"lamA.{shown}"] = lam * ALPHA[shown]
        terms[f"S.{shown}"] = score[shown]
    return Outcome(
        level=level,
        timing=timing,
        defer_until=defer_until,
        work="none" if level == "silent" else work,
        kind=read.kind,
        reason=reason,
        terms={name: round(amount, _PLACES) for name, amount in terms.items()},
        blocked_by=blocked_by,
    )


def p_of(answers: Answers, weights: Mapping[str, float], topic: str | None = None) -> float:
    """p = σ(b + t + Σ w·`logit_feature`(answer)) over the yes/no questions `FEATURES` names.

    `weights` replaces start weights by name (b is "bias"); a weight it lacks, or holds as a
    number that is not finite, keeps its start value. t is `weights["topic.<topic>"]` when that
    is a finite number, else 0. A missing or unusable answer adds nothing, as an answer of 0.5
    would.
    """
    total = _weight(weights, "bias")
    if topic is not None:
        offset = weights.get(f"topic.{topic}")
        if _finite(offset):
            total += offset
    for name in FEATURES:
        answer = _noul(answers, name)
        if answer is not None:
            total += _weight(weights, name) * logit_feature(answer)
    if math.isnan(total):
        return 0.0
    if total >= 0:
        return 1 / (1 + math.exp(-total))
    tail = math.exp(total)
    return tail / (1 + tail)


def logit_feature(p: float | None) -> float:
    """A yes/no answer as `p_of` weighs it: p clipped to [1e-6, 1 − 1e-6], its logit clipped to
    ±4. None, or a value that is not a finite number, gives 0.0, as p 0.5 does."""
    if not _finite(p):
        return 0.0
    clipped = min(max(p, _P_CLIP), 1 - _P_CLIP)
    logit = math.log(clipped / (1 - clipped))
    return min(max(logit, -_LOGIT_CLIP), _LOGIT_CLIP)


def needs_reversal(outcome: Outcome, inp: ArbiterInput) -> bool:
    """Whether to ask the reversed kind question and decide again: it is unasked, it held
    `outcome` back, and with it agreeing `decide` would choose a nudge or an alert."""
    if inp.reversal_agrees is not None or "reversal" not in outcome.blocked_by:
        return False
    return decide(replace(inp, reversal_agrees=True)).level in _LOUD


def pace(lam: float, spent: float, per_hour: float) -> float:
    """λ after one 5-minute window: λ + 0.5·(spent − per_hour/12) within 0..`LAMBDA_MAX`, so a
    window that spends more than its share of the user's nudges per hour raises λ and one that
    spends less lowers it. A result that is not a finite number reads as `LAMBDA_MAX`."""
    value = lam + 0.5 * (spent - per_hour / 12)
    return min(max(value, 0.0), LAMBDA_MAX) if math.isfinite(value) else LAMBDA_MAX


def lambda_of(value: object) -> float:
    """A stored λ within 0..`LAMBDA_MAX`; `START_LAMBDA` when none is stored or it is not a
    finite number."""
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        return START_LAMBDA
    return min(max(float(value), 0.0), LAMBDA_MAX)


def _rules_only(verdict: RulesVerdict) -> Outcome:
    level, timing, defer_until = "silent", "now", None
    if verdict.attention == "notify":
        level = "list"
    elif verdict.attention == "defer" and verdict.defer_until is not None:
        timing, defer_until = "at", verdict.defer_until
    return Outcome(
        level=level,
        timing=timing,
        defer_until=defer_until,
        work="none",
        kind=None,
        reason=_cut("rules:" + verdict.reason),
        terms={},
        blocked_by=(),
    )


def _read(answers: Answers) -> _Read:
    importance = answers.get("importance")
    score = importance_confidence = None
    if (
        isinstance(importance, ScoreAnswer)
        and _finite(importance.score)
        and _finite(importance.confidence)
    ):
        score = min(max(importance.score, 0.0), 3.0)
        importance_confidence = importance.confidence
    answer = answers.get("kind")
    kind = kind_confidence = None
    if isinstance(answer, ChoiceAnswer) and answer.choice in KINDS and _finite(answer.confidence):
        kind, kind_confidence = answer.choice, answer.confidence
    return _Read(
        goal_link=_noul(answers, "goal_link"),
        same_as_muted=_noul(answers, "same_as_muted"),
        fits_now=_noul(answers, "fits_now"),
        importance=score,
        importance_confidence=importance_confidence,
        kind=kind,
        kind_confidence=kind_confidence,
    )


def _reach(inp: ArbiterInput) -> dict[str, float]:
    """T per shown level, `inp.reach`'s when it is a finite number in 0..1, else the start one;
    then a deadline within a day (or past) scales list ×0.5 and badge ×0.8."""
    reach = {level: _in_range(inp.reach, level, 1.0, START_REACH) for level in _SHOWN}
    if _due_within(inp.candidate, inp.now, _SOON):
        reach["list"] *= 0.5
        reach["badge"] *= 0.8
    return reach


def _cost(inp: ArbiterInput, read: _Read, focus: bool) -> dict[str, float]:
    """C per shown level: list 0, and badge, nudge and alert `inp.costs`' when it is a finite
    number in 0..3, else the start one. Away scales nudge ×0.25 and alert ×0.3 (0.1 and 0.3 from
    the start costs), then typing triples both and Focus multiplies nudge by 4 and alert by 2;
    every level adds 0.1 per showing in the last day (at most 0.5), and badge and louder add
    0.5·P(same_as_muted)."""
    cost = {"list": 0.0} | {
        level: _in_range(inp.costs, level, _COST_MAX, START_COSTS) for level in START_COSTS
    }
    if inp.presence.away:
        cost["nudge"] *= 0.25
        cost["alert"] *= 0.3
    if inp.presence.typing:
        cost["nudge"] *= 3
        cost["alert"] *= 3
    if focus:
        cost["nudge"] *= 4
        cost["alert"] *= 2
    showings = min(0.1 * max(inp.exposures_24h, 0), 0.5)
    like_muted = 1.0 if read.same_as_muted is None else read.same_as_muted
    return {
        level: cost[level] + showings + (0.0 if level == "list" else 0.5 * like_muted)
        for level in _SHOWN
    }


def _work(inp: ArbiterInput) -> str:
    """When P(prepare) + P(run) + P(coordinate) reaches 0.7, the likelier of prepare and run
    (prepare on a tie) among those the item allows and the user has not turned off; else none."""
    answer = inp.answers.get("work")
    if not isinstance(answer, ChoiceAnswer):
        return "none"
    chance = {
        name: _unit(answer.probabilities.get(name)) for name in ("prepare", "run", "coordinate")
    }
    if sum(chance.values()) < _WORK_FLOOR:
        return "none"
    grants = {"prepare": inp.settings.grant_prepare, "run": inp.settings.grant_run}
    open_work = [
        name
        for name in ("prepare", "run")
        if name in inp.candidate.work_allowed and grants[name] in ("ask", "allowed")
    ]
    return max(open_work, key=chance.__getitem__) if open_work else "none"


def _failing(inp: ArbiterInput, read: _Read, muted: bool) -> dict[str, set[str]]:
    """The gates each level fails, the alert's being those of its promotion; a level fails every
    gate of the quieter levels too. List and louder fail own_question when an own answer of the
    item's type (`playbook.OWN`) is missing, not a finite number or below its floor."""
    settings = inp.settings
    per_hour = settings.nudges_per_hour
    own = OWN.get(inp.candidate.type, {})
    shown = _named(
        muted=muted,
        own_question=any(
            not _at_least(_noul(inp.answers, name), floor) for name, floor in own.items()
        ),
    )
    badge = shown | _named(kind_confidence=not _at_least(read.kind_confidence, 0.5))
    nudge = badge | _named(
        kind_confidence=not _at_least(read.kind_confidence, 0.6),
        reversal=inp.reversal_agrees is not True,
        importance_confidence=not _at_least(read.importance_confidence, 0.5),
        # The 0.7 floor also keeps a goal link below 0.5 at badge or quieter.
        goal_link=not _at_least(read.goal_link, 0.7),
        nudge_cap=per_hour <= 0 or max(inp.loud_last_hour, 0) >= min(per_hour, NUDGE_CAP),
    )
    alert = nudge | _named(alerts_off=not settings.alerts, quiet_hours=inp.quiet_until is not None)
    return {"silent": set(), "list": shown, "badge": badge, "nudge": nudge, "alert": alert}


def _alert_rule(inp: ArbiterInput, read: _Read) -> bool:
    """A task due within 2 hours, or overdue, with a goal link of at least 0.7."""
    return (
        inp.candidate.type == "tasks_due"
        and _due_within(inp.candidate, inp.now, _ALERT_WINDOW)
        and _at_least(read.goal_link, 0.7)
    )


def _choose(current: str, cap: str, score: Mapping[str, float]) -> str:
    """Hysteresis from `current`: louder (up to `cap`) only to a level scoring at least `MARGIN`
    above max(0, the current score); quieter only when the current score is below −`MARGIN` or
    the current level is above `cap`. A move takes the best-scoring level it may, the quieter on
    a tie."""
    here, top = _RANK[current], _RANK[cap]
    if here > top:
        return _best(LEVELS[: top + 1], score)
    floor = max(0.0, score[current]) + MARGIN
    louder = [level for level in LEVELS[here + 1 : top + 1] if score[level] >= floor]
    if louder:
        return _best(louder, score)
    if score[current] < -MARGIN:
        return _best(LEVELS[:here], score)
    return current


def _best(levels: Sequence[str], score: Mapping[str, float]) -> str:
    return max(levels, key=score.__getitem__)  # the first of equal scores is the quietest


def _timing(
    inp: ArbiterInput, level: str, focus: bool, in_context: bool
) -> tuple[str, datetime | None]:
    loud = level in _LOUD
    holds: list[datetime] = []
    verdict = inp.rules_verdict
    if verdict.attention == "defer" and verdict.defer_until is not None:
        holds.append(verdict.defer_until)
    if loud and inp.quiet_until is not None:
        holds.append(inp.quiet_until)
    if holds:
        return "at", max(holds)
    if loud and (inp.presence.typing or focus):
        return "after_focus", None
    if in_context:
        return "in_context", None
    return "now", None


def _fits_elsewhere(inp: ArbiterInput, read: _Read) -> bool:
    """In-context holds are on, the item belongs to a chat or note, the model says it does not
    fit what is open now, and no deadline falls within a day."""
    candidate = inp.candidate
    return (
        inp.settings.in_context
        and (candidate.related_chat is not None or candidate.related_ref is not None)
        and read.fits_now is not None
        and read.fits_now < 0.5
        and not _due_within(candidate, inp.now, _SOON)
    )


def _due_within(candidate: Candidate, now: datetime, window: timedelta) -> bool:
    """Whether the deadline is at most `window` away, or already past."""
    return candidate.deadline is not None and candidate.deadline - now <= window


def _noul(answers: Answers, name: str) -> float | None:
    """A yes/no answer's p within 0..1, or None when it is missing or unusable."""
    answer = answers.get(name)
    if isinstance(answer, NoulAnswer) and _finite(answer.p):
        return min(max(answer.p, 0.0), 1.0)
    return None


def _weight(weights: Mapping[str, float], name: str) -> float:
    weight = weights.get(name)
    return weight if _finite(weight) else START_WEIGHTS[name]


def _in_range(
    given: Mapping[str, float], name: str, top: float, start: Mapping[str, float]
) -> float:
    """`given[name]` when it is a finite number in 0..`top`, else `start[name]`."""
    value = given.get(name)
    return value if _finite(value) and 0.0 <= value <= top else start[name]


def _unit(value: object) -> float:
    """A probability within 0..1; one that is missing or not a finite number reads as 0."""
    return min(max(value, 0.0), 1.0) if _finite(value) else 0.0


def _finite(value: object) -> TypeGuard[float]:
    return isinstance(value, int | float) and math.isfinite(value)


def _at_least(value: float | None, floor: float) -> bool:
    return value is not None and value >= floor


def _named(**failed: bool) -> set[str]:
    return {name for name, fails in failed.items() if fails}


def _cut(reason: str) -> str:
    return reason[:_REASON_CHARS]
