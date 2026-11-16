"""`fit`: convergence to the prior with no rows, recovery of known weights from seeded data, the
bound clip under a one-sided pull and under perfect separation, `max_iter` and determinism, and
every ValueError `fit` raises."""

from __future__ import annotations

import math
import random

import pytest

from viki_agent.decision_loop.fit import fit

# A small logistic model used to generate synthetic rows: _logistic_rows draws features uniformly
# and labels Bernoulli at the true probability, so a correct fit should recover these weights.
_TRUE_WEIGHTS = {"bias": -0.4, "f1": 0.8, "f2": -0.6, "f3": 1.1}


def _true_p(z: float) -> float:
    """A plain sigmoid; the synthetic rows' |z| never comes close to overflowing it."""
    return 1.0 / (1.0 + math.exp(-z))


def _logistic_rows(seed: int, count: int = 1000) -> list[tuple[dict[str, float], int]]:
    """`count` rows from the model `_TRUE_WEIGHTS` names: every feature but bias uniform in
    [-2, 2], the label a Bernoulli draw at that row's true probability."""
    rng = random.Random(seed)
    names = tuple(_TRUE_WEIGHTS)
    rows: list[tuple[dict[str, float], int]] = []
    for _ in range(count):
        x: dict[str, float] = {"bias": 1.0}
        for name in names[1:]:
            x[name] = rng.uniform(-2.0, 2.0)
        z = sum(_TRUE_WEIGHTS[name] * x[name] for name in names)
        rows.append((x, 1 if rng.random() < _true_p(z) else 0))
    return rows


def test_fit_with_no_rows_returns_the_prior_exactly() -> None:
    prior = {"bias": -0.5, "a": 0.6, "b": 0.0}
    precision = {"bias": 4.0, "a": 4.0, "b": 4.0}
    bounds = {"bias": (-3.0, 1.0), "a": (0.0, 2.0), "b": (-1.5, 1.5)}
    assert fit([], prior=prior, precision=precision, bounds=bounds) == prior


def test_fit_recovers_known_weights() -> None:
    rows = _logistic_rows(seed=3)
    names = tuple(_TRUE_WEIGHTS)
    prior = dict.fromkeys(names, 0.0)
    precision = dict.fromkeys(names, 0.25)
    bounds = {name: (-3.0, 3.0) for name in names}
    result = fit(rows, prior=prior, precision=precision, bounds=bounds)
    for name in names:
        assert result[name] == pytest.approx(_TRUE_WEIGHTS[name], abs=0.2)


def test_fit_pushes_all_one_labels_to_the_bias_bound() -> None:
    prior = {"bias": -0.5, "other": 0.0}
    precision = {"bias": 4.0, "other": 4.0}
    bounds = {"bias": (-3.0, 1.0), "other": (-1.5, 1.5)}
    rows = [({"bias": 1.0}, 1) for _ in range(200)]
    result = fit(rows, prior=prior, precision=precision, bounds=bounds)
    assert result["bias"] == bounds["bias"][1]
    for name, (low, high) in bounds.items():
        assert low <= result[name] <= high


def test_fit_keeps_separable_rows_finite() -> None:
    # x1 alone perfectly separates the two labels; an unregularised MLE would send its weight to
    # infinity. The Gaussian prior plus the per-step clip must keep every weight finite and bound.
    prior = {"bias": 0.0, "x1": 0.0}
    precision = {"bias": 4.0, "x1": 4.0}
    bounds = {"bias": (-5.0, 5.0), "x1": (-5.0, 5.0)}
    rows = [({"bias": 1.0, "x1": 2.0}, 1) for _ in range(20)] + [
        ({"bias": 1.0, "x1": -2.0}, 0) for _ in range(20)
    ]
    result = fit(rows, prior=prior, precision=precision, bounds=bounds)
    for name, (low, high) in bounds.items():
        assert math.isfinite(result[name])
        assert low <= result[name] <= high


def test_fit_max_iter_one_takes_a_single_step() -> None:
    rows = _logistic_rows(seed=3)
    names = tuple(_TRUE_WEIGHTS)
    prior = dict.fromkeys(names, 0.0)
    precision = dict.fromkeys(names, 0.25)
    bounds = {name: (-3.0, 3.0) for name in names}
    one_step = fit(rows, prior=prior, precision=precision, bounds=bounds, max_iter=1)
    full = fit(rows, prior=prior, precision=precision, bounds=bounds)
    assert one_step != prior
    assert any(abs(one_step[name] - full[name]) > 1e-3 for name in names)


def test_fit_repeats_the_same_result() -> None:
    rows = _logistic_rows(seed=3)
    names = tuple(_TRUE_WEIGHTS)
    prior = dict.fromkeys(names, 0.0)
    precision = dict.fromkeys(names, 0.25)
    bounds = {name: (-3.0, 3.0) for name in names}
    first = fit(rows, prior=prior, precision=precision, bounds=bounds)
    second = fit(rows, prior=prior, precision=precision, bounds=bounds)
    assert first == second


def test_fit_raises_when_parameter_names_differ() -> None:
    with pytest.raises(ValueError, match="parameter names"):
        fit(
            [],
            prior={"bias": 0.0, "a": 0.0},
            precision={"bias": 4.0},
            bounds={"bias": (-1.0, 1.0), "a": (-1.0, 1.0)},
        )


def test_fit_raises_when_a_precision_is_not_positive() -> None:
    with pytest.raises(ValueError, match="precision"):
        fit([], prior={"bias": 0.0}, precision={"bias": 0.0}, bounds={"bias": (-1.0, 1.0)})


def test_fit_raises_when_bounds_are_inverted() -> None:
    with pytest.raises(ValueError, match="bounds"):
        fit([], prior={"bias": 0.0}, precision={"bias": 4.0}, bounds={"bias": (1.0, -1.0)})


def test_fit_raises_when_the_prior_sits_outside_its_bounds() -> None:
    with pytest.raises(ValueError, match="prior"):
        fit([], prior={"bias": 5.0}, precision={"bias": 4.0}, bounds={"bias": (-1.0, 1.0)})


def test_fit_raises_when_a_label_is_not_zero_or_one() -> None:
    with pytest.raises(ValueError, match="label"):
        fit(
            [({"bias": 1.0}, 2)],
            prior={"bias": 0.0},
            precision={"bias": 4.0},
            bounds={"bias": (-1.0, 1.0)},
        )


def test_fit_raises_when_a_feature_is_not_finite() -> None:
    with pytest.raises(ValueError, match="feature"):
        fit(
            [({"bias": math.nan}, 1)],
            prior={"bias": 0.0},
            precision={"bias": 4.0},
            bounds={"bias": (-1.0, 1.0)},
        )
