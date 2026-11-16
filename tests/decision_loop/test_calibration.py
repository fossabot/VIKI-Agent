"""`brier`'s mean squared error and empty case; `calibration`'s scored-row filter, its Brier
score and its five bins, bin edges included (`p` of 1.0); `questions`' yes/no useful
rates and its skip of rows whose answers do not decode or lack an answer for the question;
`shadow`'s filter by setting, its agreement counts and its Brier of the shadow and live `p`
against the same labels; and `trial`'s agreement count and its two `QuestionRate`s for the
wording on trial and the live one, over rows with and without a label."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from viki_agent.decision_loop.calibration import (
    QUESTIONS,
    brier,
    calibration,
    questions,
    shadow,
    trial,
)
from viki_agent.decision_loop.contracts import (
    Calibration,
    CalibrationBin,
    NoulAnswer,
    QuestionRate,
    ShadowStats,
    TrialStats,
)
from viki_agent.decision_loop.cycle import ShadowSummary, TrialSummary, encode_answers
from viki_agent.decision_loop.stats import LearningRow

NOW = datetime(2026, 10, 21, 9, 0, tzinfo=UTC)


def _row(**overrides: Any) -> LearningRow:
    """A labelled "model" decision row with a `p`, ready to override for one test's point."""
    defaults: dict[str, Any] = {
        "episode_id": "ep",
        "created_at": NOW,
        "candidate": "approvals",
        "level": "nudge",
        "timing": "now",
        "policy": "model",
        "answers_json": None,
        "p": 0.7,
        "feedback": "useful",
        "feedback_reason": None,
        "feedback_at": NOW,
        "first_exposed_at": NOW,
        "opened_at": NOW,
        "acted_at": None,
        "label": 1,
    }
    return LearningRow(**{**defaults, **overrides})


# ---- brier -------------------------------------------------------------------------------------


def test_brier_is_the_mean_squared_error_and_none_when_empty() -> None:
    assert brier([]) is None
    assert brier([(0.9, 1), (0.2, 0), (0.5, 1)]) == pytest.approx(0.1)


# ---- calibration ---------------------------------------------------------------------------------


def test_calibration_on_no_rows_is_all_empty() -> None:
    assert calibration([]) == Calibration(
        n=0,
        brier=None,
        bins=tuple(
            CalibrationBin(low=i / 5, high=(i + 1) / 5, n=0, mean_p=None, rate=None)
            for i in range(5)
        ),
    )


def test_calibration_counts_only_labelled_model_rows_with_a_p() -> None:
    scored = _row(label=1, p=0.5)
    unlabelled = _row(label=None, p=0.5)
    rules_row = _row(label=1, p=0.5, policy="rules")
    no_p = _row(label=1, p=None)
    result = calibration([scored, unlabelled, rules_row, no_p])
    assert result.n == 1
    assert result.brier == pytest.approx(0.25)  # (0.5 - 1) ** 2


def test_brier_and_bins() -> None:
    rows = [
        _row(p=0.1, label=0),
        _row(p=0.9, label=1),
        _row(p=0.3, label=1),
        _row(p=1.0, label=1),
    ]
    result = calibration(rows)
    assert result.n == 4
    assert result.brier == pytest.approx(0.1275)  # (0.01 + 0.01 + 0.49 + 0.0) / 4
    assert [one.n for one in result.bins] == [1, 1, 0, 0, 2]
    assert (result.bins[0].low, result.bins[0].high) == (pytest.approx(0.0), pytest.approx(0.2))
    assert result.bins[0].mean_p == pytest.approx(0.1)
    assert result.bins[0].rate == pytest.approx(0.0)
    assert result.bins[1].mean_p == pytest.approx(0.3)
    assert result.bins[1].rate == pytest.approx(1.0)
    assert result.bins[2].mean_p is None
    assert result.bins[2].rate is None
    # p = 1.0 lands in the last, inclusive bin alongside p = 0.9, not spilling past it.
    assert (result.bins[4].low, result.bins[4].high) == (pytest.approx(0.8), pytest.approx(1.0))
    assert result.bins[4].mean_p == pytest.approx(0.95)
    assert result.bins[4].rate == pytest.approx(1.0)


# ---- questions ---------------------------------------------------------------------------------


def test_questions_lists_the_seven_noul_names_in_playbook_order() -> None:
    assert QUESTIONS == (
        "goal_link",
        "owner_only",
        "news",
        "actionable",
        "same_as_muted",
        "fits_now",
        "sensitive",
    )
    assert [rate.name for rate in questions([])] == list(QUESTIONS)


def test_questions_computes_yes_and_no_useful_rates() -> None:
    rows = [
        _row(label=1, answers_json=encode_answers({"goal_link": NoulAnswer(0.9)})),
        _row(label=0, answers_json=encode_answers({"goal_link": NoulAnswer(0.8)})),
        _row(label=0, answers_json=encode_answers({"goal_link": NoulAnswer(0.1)})),
        _row(label=1, answers_json=encode_answers({"goal_link": NoulAnswer(0.2)})),
    ]
    rates = {rate.name: rate for rate in questions(rows)}
    goal_link = rates["goal_link"]
    assert (goal_link.yes_n, goal_link.yes_rate) == (2, pytest.approx(0.5))
    assert (goal_link.no_n, goal_link.no_rate) == (2, pytest.approx(0.5))
    owner_only = rates["owner_only"]
    assert (owner_only.yes_n, owner_only.yes_rate) == (0, None)
    assert (owner_only.no_n, owner_only.no_rate) == (0, None)


def test_questions_skips_rows_whose_answers_do_not_decode() -> None:
    good = _row(label=1, answers_json=encode_answers({"goal_link": NoulAnswer(0.9)}))
    bad_json = _row(label=0, answers_json="not json")
    missing_question = _row(label=1, answers_json=encode_answers({"owner_only": NoulAnswer(0.9)}))
    unlabelled = _row(label=None, answers_json=encode_answers({"goal_link": NoulAnswer(0.9)}))
    rates = {rate.name: rate for rate in questions([good, bad_json, missing_question, unlabelled])}
    goal_link = rates["goal_link"]
    assert (goal_link.yes_n, goal_link.yes_rate) == (1, pytest.approx(1.0))
    assert (goal_link.no_n, goal_link.no_rate) == (0, None)


# ---- shadow ------------------------------------------------------------------------------------


def test_shadow_filters_by_setting_and_scores_against_the_label() -> None:
    matching_1 = _row(
        label=1,
        p=0.6,
        answers_json=encode_answers(
            {},
            shadow=ShadowSummary(
                agree=True, kind_agree=True, level="nudge", p=0.8, setting="latest"
            ),
        ),
    )
    matching_2 = _row(
        label=0,
        p=0.4,
        answers_json=encode_answers(
            {},
            shadow=ShadowSummary(
                agree=False, kind_agree=True, level="badge", p=0.3, setting="latest"
            ),
        ),
    )
    other_setting = _row(
        label=1,
        p=0.5,
        answers_json=encode_answers(
            {},
            shadow=ShadowSummary(
                agree=True, kind_agree=True, level="nudge", p=0.9, setting="preview"
            ),
        ),
    )
    no_shadow = _row(label=1, p=0.5, answers_json=encode_answers({}))
    result = shadow([matching_1, matching_2, other_setting, no_shadow], "latest")
    assert result.setting == "latest"
    assert result.decisions == 2
    assert result.agree == 1
    assert result.kind_agree == 2
    assert result.labels == 2
    assert result.brier == pytest.approx(0.065)  # ((0.8-1)**2 + (0.3-0)**2) / 2
    assert result.live_brier == pytest.approx(0.16)  # ((0.6-1)**2 + (0.4-0)**2) / 2


def test_shadow_off_never_matches_any_row() -> None:
    row = _row(
        label=1,
        p=0.5,
        answers_json=encode_answers(
            {},
            shadow=ShadowSummary(
                agree=True, kind_agree=True, level="nudge", p=0.9, setting="latest"
            ),
        ),
    )
    assert shadow([row], "off") == ShadowStats(
        setting="off", decisions=0, agree=0, kind_agree=0, labels=0, brier=None, live_brier=None
    )


# ---- trial -------------------------------------------------------------------------------------


def test_trial_compares_one_question() -> None:
    agreeing_useful = _row(
        label=1, answers_json=encode_answers({}, trial=TrialSummary(live=0.8, trial=0.9))
    )
    disagreeing_not_useful = _row(
        label=0, answers_json=encode_answers({}, trial=TrialSummary(live=0.3, trial=0.6))
    )
    unlabelled = _row(
        label=None, answers_json=encode_answers({}, trial=TrialSummary(live=0.7, trial=0.8))
    )
    no_trial = _row(label=1, answers_json=encode_answers({}))
    result = trial([agreeing_useful, disagreeing_not_useful, unlabelled, no_trial], "goal_link")
    assert result == TrialStats(
        name="goal_link",
        decisions=3,  # every row with a trial summary; no_trial decodes to None and drops out
        agree=2,  # agreeing_useful (0.8/0.9) and unlabelled (0.7/0.8); not disagreeing (0.3/0.6)
        labels=2,  # unlabelled counts toward decisions and agreement, not toward labels or rates
        live=QuestionRate(name="goal_link", yes_n=1, yes_rate=1.0, no_n=1, no_rate=0.0),
        trial=QuestionRate(name="goal_link", yes_n=2, yes_rate=0.5, no_n=0, no_rate=None),
    )


def test_trial_boundary_when_live_is_exactly_half() -> None:
    # Boundary case: live p = 0.5 (exactly half), trial p > 0.5 (above half)
    boundary_useful = _row(
        label=1, answers_json=encode_answers({}, trial=TrialSummary(live=0.5, trial=0.6))
    )
    # Another row for comparison, above the boundary
    above_useful = _row(
        label=1, answers_json=encode_answers({}, trial=TrialSummary(live=0.7, trial=0.8))
    )
    result = trial([boundary_useful, above_useful], "goal_link")
    # Both should agree: (0.5 >= 0.5) == (0.6 >= 0.5) and (0.7 >= 0.5) == (0.8 >= 0.5) both true
    assert result.agree == 2
    # Both should land in "yes" for live wording: 0.5 >= 0.5 and 0.7 >= 0.5
    assert result.live.yes_n == 2
    assert result.live.no_n == 0
