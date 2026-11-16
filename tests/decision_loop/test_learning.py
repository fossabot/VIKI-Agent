"""`labelled`'s filter, order and cap; `relearn`'s label floors and class balance, its fit over
the terms `arbiter.p_of` weighs, its T and C from `measures`, its support counts and its
determinism; `encode`'s stored form; `parse`'s round trip, bounds included, every refusal, and
a snapshot an earlier version wrote; and `inputs`' learned and start values."""

from __future__ import annotations

import json
import math
import random
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest

from viki_agent.decision_loop import measures
from viki_agent.decision_loop.arbiter import (
    FEATURES,
    START_COSTS,
    START_REACH,
    START_WEIGHTS,
    logit_feature,
    p_of,
)
from viki_agent.decision_loop.contracts import (
    CANDIDATES,
    MAX_FIT_ROWS,
    MIN_EACH,
    MIN_LABELS,
    Answer,
    ChoiceAnswer,
    Learned,
    NoulAnswer,
    canonical_json,
)
from viki_agent.decision_loop.cycle import decode_answers, encode_answers
from viki_agent.decision_loop.fit import fit
from viki_agent.decision_loop.learning import (
    BOUNDS,
    PRECISION,
    encode,
    inputs,
    labelled,
    parse,
    relearn,
)
from viki_agent.decision_loop.stats import LearningRow
from viki_agent.decision_loop.store import MAX_JSON_BYTES

NOW = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)
START = {**START_WEIGHTS, **{f"topic.{candidate}": 0.0 for candidate in CANDIDATES}}
SUPPORT = (
    *(f"topic.{candidate}" for candidate in CANDIDATES),
    *(f"reach.{level}" for level in ("list", "badge", "nudge", "alert")),
    *(f"costs.{level}" for level in ("badge", "nudge", "alert")),
)
_MISSING = object()
# Fixed so a change that adds a candidate type does not thin each type's synthetic labels; real
# labels come only from types that occur.
_SYNTHETIC_TYPES = (
    "approvals",
    "tasks_due",
    "agent_runs",
    "inbox",
    "uncommitted",
    "routine",
    "mission_report",
    "card_needs_you",
    "card_stale",
    "seat_waiting",
    "seat_working",
    "seat_start",
    "seat_report",
    "vault_recovery",
    "memory_drift",
    "bridge_stopped",
    "chat_waiting",
    "send_uncertain",
    "mcp_context",
    "skill_offer",
    "skill_suggest",
    "learn_source",
    "retro_due",
    "question_tune",
    "note_link",
    "note_superseded",
    "tasks_ticked",
)


def test_synthetic_types_are_candidates() -> None:
    assert set(_SYNTHETIC_TYPES) <= set(CANDIDATES)


def _answers(**noul: float) -> str:
    """Stored answers holding one yes/no answer per keyword."""
    return encode_answers({name: NoulAnswer(p) for name, p in noul.items()})


def _row(index: int = 0, **overrides: Any) -> LearningRow:
    """A labelled "model" nudge, `index` minutes older than the newest (`index` 0), its window
    closed and seen; override one test's point."""
    created_at = NOW - timedelta(hours=5, minutes=index)
    defaults: dict[str, Any] = {
        "episode_id": f"ep-{index:05d}",
        "created_at": created_at,
        "candidate": "approvals",
        "level": "nudge",
        "timing": "now",
        "policy": "model",
        "answers_json": _answers(goal_link=0.9, news=0.6),
        "p": 0.7,
        "feedback": "useful",
        "feedback_reason": None,
        "feedback_at": created_at + timedelta(minutes=5),
        "first_exposed_at": created_at,
        "opened_at": None,
        "acted_at": None,
        "label": 1,
    }
    return LearningRow(**{**defaults, **overrides})


def _labelled_rows(ones: int, zeros: int) -> list[LearningRow]:
    """`ones` rows marked useful, each one the model called goal-linked, then `zeros` marked
    wrong that it did not: something a fit learns from (goal_link rises)."""
    useful = [_row(i, answers_json=_answers(goal_link=0.9, news=0.5)) for i in range(ones)]
    wrong = [
        _row(
            ones + i,
            answers_json=_answers(goal_link=0.1, news=0.5),
            feedback="wrong",
            label=0,
        )
        for i in range(zeros)
    ]
    return useful + wrong


def _synthetic(
    count: int, weights: dict[str, float], seed: int
) -> list[tuple[LearningRow, dict[str, Answer]]]:
    """`count` rows whose labels `p_of` drew under `weights`, with the answers behind each."""
    rng = random.Random(seed)
    made: list[tuple[LearningRow, dict[str, Answer]]] = []
    for index in range(count):
        answers: dict[str, Answer] = {
            name: NoulAnswer(rng.uniform(0.03, 0.97)) for name in FEATURES
        }
        candidate = rng.choice(_SYNTHETIC_TYPES)
        label = int(rng.random() < p_of(answers, weights, candidate))
        row = _row(
            index,
            candidate=candidate,
            answers_json=encode_answers(answers),
            feedback="useful" if label else "wrong",
            label=label,
        )
        made.append((row, answers))
    return made


def _snapshot(**overrides: Any) -> Learned:
    """A snapshot in its stored form: 6-place numbers inside their bounds, a millisecond stamp."""
    defaults: dict[str, Any] = {
        "fitted_at": NOW,
        "labels": 42,
        "weights": {**START, "goal_link": 0.812345, "topic.inbox": -0.25, "topic.routine": 0.5},
        "reach": {"list": 0.31, "badge": 0.62, "nudge": 0.88, "alert": 0.9},
        "costs": {"badge": 0.06, "nudge": 0.35, "alert": 1.2},
        "support": {name: index for index, name in enumerate(SUPPORT)},
    }
    return Learned(**{**defaults, **overrides})


# ---- settings ----------------------------------------------------------------------------------


def test_the_fit_settings_follow_the_design() -> None:
    assert PRECISION == 4.0
    assert dict(BOUNDS) == {
        "bias": (-3.0, 1.0),
        **{name: (0.0, 2.0) for name in FEATURES},
        **{f"topic.{candidate}": (-1.5, 1.5) for candidate in CANDIDATES},
    }


# ---- labelled ----------------------------------------------------------------------------------


def test_labelled_keeps_model_rows_with_a_label_and_answers_that_decode() -> None:
    kept = [_row(0), _row(1, feedback="wrong", label=0)]
    dropped = [
        _row(2, policy="rules"),
        _row(3, feedback=None, label=None),
        _row(4, answers_json=None),
        _row(5, answers_json="not json"),
        _row(6, answers_json='{"goal_link":0.9}'),  # a member that is no answer
        _row(7, answers_json='{"goal_link":{"noul":' + "9" * 400 + "}}"),  # no float holds it
    ]
    assert labelled([*dropped, *kept]) == kept


def test_labelled_answers_the_newest_first_and_at_most_max_fit_rows() -> None:
    rows = [_row(index) for index in range(MAX_FIT_ROWS + 5)]
    shuffled = rows[:]
    random.Random(7).shuffle(shuffled)
    assert labelled(shuffled) == rows[:MAX_FIT_ROWS]
    tied = [_row(0, episode_id="a"), _row(0, episode_id="b")]
    assert labelled(tied) == tied[::-1]  # the same stamp: the larger episode id first


# ---- relearn: the weights ----------------------------------------------------------------------


def test_relearn_fits_only_with_enough_labels() -> None:
    # Unlabelled, rules and undecodable rows count toward no floor.
    others = [
        _row(100, feedback=None, label=None),
        _row(101, policy="rules"),
        _row(102, answers_json="not json"),
    ]
    below = (
        _labelled_rows(MIN_LABELS - 1 - MIN_EACH, MIN_EACH) + others,  # one label short
        _labelled_rows(MIN_LABELS - MIN_EACH + 1, MIN_EACH - 1),  # too few marked wrong
        _labelled_rows(MIN_EACH - 1, MIN_LABELS - MIN_EACH + 1),  # too few marked useful
    )
    for rows in below:
        learned = relearn(rows, now=NOW)
        assert learned.weights == START
        assert learned.labels == len(labelled(rows))
    for rows in (
        _labelled_rows(MIN_LABELS - MIN_EACH, MIN_EACH),
        _labelled_rows(MIN_EACH, MIN_LABELS - MIN_EACH),
    ):
        learned = relearn(rows, now=NOW)
        assert learned.labels == MIN_LABELS
        assert learned.weights["goal_link"] > START["goal_link"]
        assert all(low <= learned.weights[name] <= high for name, (low, high) in BOUNDS.items())


def test_relearn_fits_the_terms_p_of_weighs() -> None:
    rng = random.Random(11)
    rows: list[LearningRow] = []
    for index in range(60):
        answers: dict[str, Answer] = {
            name: NoulAnswer(rng.random()) for name in FEATURES if rng.random() < 0.8
        }
        if index % 7 == 0:  # a feature's name holding no yes/no answer adds nothing
            answers["news"] = ChoiceAnswer("ask", 0.9, {"ask": 0.9, "inform": 0.1})
        candidate = "unwatched" if index % 11 == 0 else CANDIDATES[index % len(CANDIDATES)]
        label = int(rng.random() < 0.5)
        rows.append(
            _row(
                index,
                candidate=candidate,
                answers_json=encode_answers(answers),
                feedback="useful" if label else "wrong",
                label=label,
            )
        )
    terms: list[tuple[dict[str, float], int]] = []
    for row in rows:
        assert row.answers_json is not None and row.label is not None
        decoded, _ = decode_answers(row.answers_json)
        x = {"bias": 1.0, f"topic.{row.candidate}": 1.0}
        for name in FEATURES:
            answer = decoded.get(name)
            x[name] = logit_feature(answer.p if isinstance(answer, NoulAnswer) else None)
        terms.append((x, row.label))

    learned = relearn(rows, now=NOW)

    expected = fit(terms, prior=START, precision=dict.fromkeys(START, 4.0), bounds=dict(BOUNDS))
    assert expected != START
    assert learned.weights == pytest.approx(expected, abs=1e-6)
    # Read by p_of, the fitted weights give each row sigmoid(weights . x): one model, not two.
    for row, (x, _) in zip(rows, terms, strict=True):
        assert row.answers_json is not None
        decoded, _ = decode_answers(row.answers_json)
        z = sum(learned.weights.get(name, 0.0) * value for name, value in x.items())
        p = p_of(decoded, learned.weights, row.candidate)
        assert p == pytest.approx(1 / (1 + math.exp(-z)))


def test_relearn_recovers_the_weights_behind_the_labels() -> None:
    true = {
        **START,
        "bias": -0.3,
        "goal_link": 1.1,
        "news": 0.2,
        "actionable": 0.7,
        "owner_only": 0.0,
        "fits_now": 0.4,
        "topic.approvals": 0.6,
        "topic.inbox": -0.6,
    }
    made = _synthetic(MAX_FIT_ROWS, true, seed=2026)

    learned = relearn([row for row, _ in made], now=NOW)

    for name in FEATURES:
        assert learned.weights[name] == pytest.approx(true[name], abs=0.2)
    gaps = [
        abs(p_of(answers, learned.weights, row.candidate) - p_of(answers, true, row.candidate))
        for row, answers in made
    ]
    assert sum(gaps) / len(gaps) < 0.05


def test_relearn_is_deterministic_whatever_the_row_order() -> None:
    rows = [row for row, _ in _synthetic(300, {**START, "goal_link": 1.0}, seed=5)]
    rows += [_row(1000 + index, level="badge", policy="rules", label=None) for index in range(9)]
    first = relearn(rows, now=NOW)
    assert first.weights != START
    shuffled = rows[:]
    random.Random(3).shuffle(shuffled)
    assert relearn(rows, now=NOW) == first
    assert relearn(shuffled, now=NOW) == first


# ---- relearn: T, C and support -----------------------------------------------------------------


def test_relearn_without_rows_answers_the_start_values() -> None:
    learned = relearn([], now=NOW)
    assert learned == Learned(
        fitted_at=NOW,
        labels=0,
        weights=START,
        reach=dict(START_REACH),
        costs=dict(START_COSTS),
        support=dict.fromkeys(SUPPORT, 0),
    )
    assert inputs(learned) == inputs(None)


def test_relearn_takes_seen_in_time_and_costs_from_measures() -> None:
    old = NOW - timedelta(hours=6)
    rows = [
        *(_row(index, level="list", policy="rules", label=None) for index in range(12)),
        *(_row(20 + index, level="badge", first_exposed_at=None) for index in range(15)),
        *(_row(40 + index, level="nudge", feedback="later", label=None) for index in range(8)),
        *(_row(60 + index, level="alert", feedback="mute", label=0) for index in range(5)),
        _row(
            80,
            level="alert",
            created_at=old,
            first_exposed_at=old + timedelta(hours=5),  # seen, but too late
            feedback=None,
            feedback_at=None,
            label=None,
        ),
        _row(81, level="nudge", created_at=NOW - timedelta(hours=1)),  # its window is open
    ]
    learned = relearn(rows, now=NOW)
    reach, reach_support = measures.reach(rows, NOW)
    costs, cost_support = measures.costs(rows)
    assert learned.reach != START_REACH and learned.costs != START_COSTS
    assert learned.reach == pytest.approx(reach, abs=1e-6)
    assert learned.costs == pytest.approx(costs, abs=1e-6)
    assert {level: learned.support[f"reach.{level}"] for level in reach} == reach_support
    assert {level: learned.support[f"costs.{level}"] for level in costs} == cost_support
    # 17 / 18 and 0.4 x (11 / 19) / 0.3 come back as stored: 6 places, read back unchanged.
    assert (learned.reach["nudge"], learned.costs["nudge"]) == (0.944444, 0.77193)
    assert parse(encode(learned)) == learned


def test_a_reach_at_its_bound_reads_back_from_the_stored_form() -> None:
    # 1.5 x 0.3 and 1.5 x 0.6 are no 6-place numbers: rounded alone, a reach at either bound
    # would land just past it and the whole snapshot would read back as None.
    rows = [_row(index, level="list", policy="rules", label=None) for index in range(40)]
    rows += [_row(100 + index, level="badge", policy="rules", label=None) for index in range(100)]
    learned = relearn(rows, now=NOW)
    assert learned.reach["list"] == measures.REACH_BOUNDS["list"][1]
    assert learned.reach["badge"] == measures.REACH_BOUNDS["badge"][1]
    assert parse(encode(learned)) == learned


def test_support_counts_the_rows_behind_each_number() -> None:
    rows = [
        *(_row(index, candidate="inbox") for index in range(4)),
        *(_row(10 + index, candidate="routine") for index in range(3)),
        _row(20, candidate="inbox", policy="rules"),  # not fitted
        _row(21, candidate="inbox", p=None),  # fitted: its answers decode, though it has no p
    ]
    learned = relearn(rows, now=NOW)
    assert tuple(learned.support) == SUPPORT
    assert learned.labels == 8
    topics = {candidate: learned.support[f"topic.{candidate}"] for candidate in CANDIDATES}
    assert topics == {**dict.fromkeys(CANDIDATES, 0), "inbox": 5, "routine": 3}


# ---- encode and parse --------------------------------------------------------------------------


def test_encode_writes_the_stored_form() -> None:
    tokyo = timezone(timedelta(hours=9))
    learned = _snapshot(
        fitted_at=datetime(2026, 10, 22, 21, 0, 0, 123456, tzinfo=tokyo),
        weights={**START, "goal_link": 0.1234567891, "topic.inbox": -1.4999996},
    )
    assert encode(learned) == {
        "v": 1,
        "fitted_at": "2026-10-22T12:00:00.123Z",
        "labels": 42,
        "weights": {**START, "goal_link": 0.123457, "topic.inbox": -1.5},
        "reach": {"list": 0.31, "badge": 0.62, "nudge": 0.88, "alert": 0.9},
        "costs": {"badge": 0.06, "nudge": 0.35, "alert": 1.2},
        "support": dict(learned.support),
    }
    widest = _snapshot(
        labels=99_999,
        weights={name: low + 0.123456 for name, (low, _) in BOUNDS.items()},
        support=dict.fromkeys(SUPPORT, 99_999),
    )
    # The store refuses past MAX_JSON_BYTES; the widest snapshot keeps half of it free for the
    # topics added later.
    assert len(canonical_json(encode(widest))) < MAX_JSON_BYTES // 2


def test_parse_reads_back_what_encode_wrote() -> None:
    top = _snapshot(
        weights={name: high for name, (_, high) in BOUNDS.items()},
        reach={level: high for level, (_, high) in measures.REACH_BOUNDS.items()},
        costs={level: high for level, (_, high) in measures.COST_BOUNDS.items()},
    )
    bottom = _snapshot(
        weights={name: low for name, (low, _) in BOUNDS.items()},
        reach={level: low for level, (low, _) in measures.REACH_BOUNDS.items()},
        costs={level: low for level, (low, _) in measures.COST_BOUNDS.items()},
        labels=0,
        support=dict.fromkeys(SUPPORT, 0),
    )
    fitted = relearn(_labelled_rows(30, 10), now=NOW + timedelta(microseconds=1_500))
    assert fitted.fitted_at == NOW + timedelta(milliseconds=1)  # the stored stamp's precision
    for learned in (_snapshot(), top, bottom, fitted, relearn([], now=NOW)):
        assert parse(encode(learned)) == learned
        assert parse(json.loads(canonical_json(encode(learned)))) == learned


def _changed(path: tuple[str, ...], value: object) -> dict[str, Any]:
    """`encode(_snapshot())`, copied, with the member at `path` set to `value` (`_MISSING`
    deletes it)."""
    stored: dict[str, Any] = json.loads(canonical_json(encode(_snapshot())))
    target = stored
    for key in path[:-1]:
        target = target[key]
    if value is _MISSING:
        del target[path[-1]]
    else:
        target[path[-1]] = value
    return stored


_REFUSED: list[tuple[tuple[str, ...], object]] = [
    *(((key,), _MISSING) for key in ("v", "fitted_at", "labels", "weights")),
    *(((key,), _MISSING) for key in ("reach", "costs", "support")),
    (("extra",), 1),
    (("v",), 2),
    (("v",), True),
    (("v",), 1.0),
    (("v",), "1"),
    (("fitted_at",), "2026-10-22T12:00:00Z"),
    (("fitted_at",), "2026-10-22T12:00:00.000+00:00"),
    (("fitted_at",), "2026-02-30T12:00:00.000Z"),
    (("fitted_at",), 0),
    (("labels",), -1),
    (("labels",), 2.0),
    (("labels",), True),
    (("labels",), None),
    (("weights",), [0.0]),
    (("weights", "bias"), _MISSING),
    (("weights", "topic.unwatched"), 0.0),
    (("weights", "bias"), -3.000001),
    (("weights", "bias"), 1.000001),
    (("weights", "goal_link"), -0.000001),
    (("weights", "fits_now"), 2.000001),
    (("weights", "topic.inbox"), -1.500001),
    (("weights", "topic.mission_report"), 1.500001),
    (("weights", "news"), math.nan),
    (("weights", "news"), math.inf),
    (("weights", "news"), 10**400),
    (("weights", "news"), True),
    (("weights", "news"), "0.5"),
    (("weights", "news"), None),
    (("reach", "nudge"), _MISSING),
    (("reach", "silent"), 0.0),
    (("reach", "list"), 0.149999),
    (("reach", "badge"), 0.9),
    (("reach", "alert"), 0.990001),
    (("reach", "list"), -math.inf),
    (("costs", "nudge"), _MISSING),
    (("costs", "list"), 0.0),
    (("costs", "badge"), 0.024999),
    (("costs", "alert"), 2.000001),
    (("support",), None),
    (("support", "costs.alert"), _MISSING),
    (("support", "costs.list"), 0),
    (("support", "labels.fr"), 1),
    (("support", "topic.inbox"), -1),
    (("support", "topic.inbox"), 1.0),
    (("support", "reach.list"), True),
]


def _refusal_id(path: tuple[str, ...], value: object) -> str:
    return ".".join(path) + "=" + ("missing" if value is _MISSING else repr(value)[:12])


@pytest.mark.parametrize(
    ("path", "value"), _REFUSED, ids=[_refusal_id(path, value) for path, value in _REFUSED]
)
def test_parse_refuses_anything_but_an_exact_bounded_snapshot(
    path: tuple[str, ...], value: object
) -> None:
    assert parse(_changed(("labels",), 42)) == _snapshot()  # the copy itself reads back
    assert parse(_changed(path, value)) is None


@pytest.mark.parametrize("value", [None, [], "text", 1, canonical_json(encode(_snapshot()))])
def test_parse_refuses_what_is_no_object(value: object) -> None:
    assert parse(value) is None


@pytest.mark.parametrize("lift", [0.0, 0.1, 0.100001, -1, True, None, "0.1"])
def test_parse_reads_a_snapshot_an_earlier_version_wrote(lift: object) -> None:
    """Earlier versions also stored a confidence-floor lift for items they took to be Japanese
    ("ja_lift") and the per-language label counts behind it ("labels.en" and "labels.ja" under
    "support"). `parse` skips those members, whatever they hold, and reads the rest as it would
    read the snapshot without them; anything else still has to be exact."""
    older = _changed(("ja_lift",), lift)
    older["support"] |= {"labels.en": 31, "labels.ja": -1}
    assert parse(older) == _snapshot()
    assert parse(_changed(("ja_lift",), lift)) == _snapshot()
    assert parse(_changed(("support", "labels.ja"), lift)) == _snapshot()
    assert parse({**older, "labels": -1}) is None
    assert parse({**older, "extra": lift}) is None


# ---- inputs ------------------------------------------------------------------------------------


def test_inputs_hand_the_arbiter_learned_numbers_or_the_start_values() -> None:
    assert inputs(None) == (START, dict(START_REACH), dict(START_COSTS))
    learned = _snapshot()
    weights, reach, costs = inputs(learned)
    assert (weights, reach, costs) == (learned.weights, learned.reach, learned.costs)
    weights["bias"] = 0.9  # a fresh copy: the snapshot and the next call keep their numbers
    inputs(None)[0]["bias"] = 0.9
    assert learned.weights["bias"] == inputs(None)[0]["bias"] == START_WEIGHTS["bias"]
    # Every topic at 0 decides as `START_WEIGHTS` alone does.
    answers: dict[str, Answer] = {name: NoulAnswer(0.8) for name in FEATURES}
    for candidate in CANDIDATES:
        assert p_of(answers, inputs(None)[0], candidate) == p_of(answers, START_WEIGHTS, candidate)
