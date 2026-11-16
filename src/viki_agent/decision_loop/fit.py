"""A bounded, deterministic MAP logistic-regression fit.

`fit` finds the maximum a posteriori weights of a logistic model under a Gaussian prior by
Newton-Raphson (IRLS) with a Cholesky solve, clipping every step to caller-given bounds so the
result can never leave them. Stdlib only (`math`), no I/O, no randomness, no clock: the same
rows, prior, precision and bounds always give back the same weights, bit for bit.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence


def fit(
    rows: Sequence[tuple[Mapping[str, float], int]],
    *,
    prior: Mapping[str, float],
    precision: Mapping[str, float],
    bounds: Mapping[str, tuple[float, float]],
    max_iter: int = 25,
    tol: float = 1e-6,
) -> dict[str, float]:
    """The MAP weights of p(y=1|x) = sigmoid(x . theta) under a Gaussian prior centred on `prior`
    with per-name `precision` (inverse variance), theta clipped to `bounds` after every step.

    Parameters are named and ordered by `prior`; `precision` and `bounds` must name exactly the
    same parameters. A row's x may omit a name, which then contributes 0 to that row (callers
    pass "bias": 1 themselves for an intercept).

    Starting at `prior`, each step solves (X^T W X + diag(precision))·delta = X^T(y -
    sigmoid(X theta)) - precision⊙(theta - prior) by Cholesky (W the working weights
    sigmoid(z)·(1 - sigmoid(z))), clips theta + delta to `bounds`, and stops once every
    |change| < tol or after `max_iter` steps. A non-finite value anywhere in a step (a failed
    Cholesky solve, or a non-finite proposed value) answers `prior` unchanged, so the result is
    always finite and in bounds.

    Raises ValueError when `precision` or `bounds` do not name exactly `prior`'s parameters, a
    precision is not positive, a bound's low exceeds its high, a prior value sits outside its own
    bounds, a row's label is not 0 or 1, or a row's x holds a value that is not a finite number.
    """
    names = tuple(prior)
    name_set = set(names)
    if set(precision) != name_set or set(bounds) != name_set:
        raise ValueError("parameter names")
    for name in names:
        if precision[name] <= 0:
            raise ValueError("precision")
        low, high = bounds[name]
        if low > high:
            raise ValueError("bounds")
        if not (low <= prior[name] <= high):
            raise ValueError("prior")

    vectors: list[list[float]] = []
    labels: list[int] = []
    for x, y in rows:
        if y not in (0, 1):
            raise ValueError("label")
        vector: list[float] = []
        for name in names:
            value = x.get(name, 0.0)
            if (
                isinstance(value, bool)
                or not isinstance(value, int | float)
                or not math.isfinite(value)
            ):
                raise ValueError("feature")
            vector.append(float(value))
        vectors.append(vector)
        labels.append(y)

    n = len(names)
    theta = [float(prior[name]) for name in names]
    prior_vector = list(theta)
    precision_vector = [float(precision[name]) for name in names]
    bound_list = [bounds[name] for name in names]
    fallback = dict(prior)

    for _step in range(max_iter):
        delta = _newton_step(vectors, labels, theta, precision_vector, prior_vector, n)
        if delta is None:
            return fallback

        changed = 0.0
        next_theta = [0.0] * n
        for j in range(n):
            candidate = theta[j] + delta[j]
            if not math.isfinite(candidate):
                return fallback
            low, high = bound_list[j]
            clipped = min(max(candidate, low), high)
            changed = max(changed, abs(clipped - theta[j]))
            next_theta[j] = clipped
        theta = next_theta
        if changed < tol:
            break

    return dict(zip(names, theta, strict=True))


def _newton_step(
    vectors: list[list[float]],
    labels: list[int],
    theta: list[float],
    precision: list[float],
    prior: list[float],
    n: int,
) -> list[float] | None:
    """One Newton-Raphson step's delta over `vectors`/`labels` at `theta`, or None when the
    Cholesky solve fails or any entry turns non-finite. Builds X^T W X once, over the rows, never
    quadratic in their count."""
    gradient = [0.0] * n
    upper = [[0.0] * n for _ in range(n)]
    for vector, y in zip(vectors, labels, strict=True):
        z = sum(v * t for v, t in zip(vector, theta, strict=True))
        p = _sigmoid(z)
        w = p * (1.0 - p)
        residual = y - p
        for j in range(n):
            xj = vector[j]
            if xj == 0.0:
                continue
            gradient[j] += xj * residual
            row = upper[j]
            for k in range(j, n):
                xk = vector[k]
                if xk != 0.0:
                    row[k] += xj * w * xk

    hessian = [[0.0] * n for _ in range(n)]
    for j in range(n):
        gradient[j] -= precision[j] * (theta[j] - prior[j])
        for k in range(j, n):
            value = upper[j][k] + (precision[j] if j == k else 0.0)
            hessian[j][k] = value
            hessian[k][j] = value
    return _cholesky_solve(hessian, gradient)


def _sigmoid(z: float) -> float:
    """The logistic function, branched so neither side overflows for a large |z| (the same
    technique as `arbiter.p_of`'s sigmoid)."""
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    tail = math.exp(z)
    return tail / (1.0 + tail)


def _cholesky_solve(a: list[list[float]], b: list[float]) -> list[float] | None:
    """The solution of a·x = b for a symmetric matrix `a`, by Cholesky decomposition, or
    None when `a` is not positive definite or a step turns non-finite."""
    n = len(b)
    lower = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1):
            total = sum(lower[i][k] * lower[j][k] for k in range(j))
            if i == j:
                diagonal = a[i][i] - total
                if not math.isfinite(diagonal) or diagonal <= 0.0:
                    return None
                lower[i][j] = math.sqrt(diagonal)
            else:
                if lower[j][j] == 0.0:
                    return None
                value = (a[i][j] - total) / lower[j][j]
                if not math.isfinite(value):
                    return None
                lower[i][j] = value

    forward = [0.0] * n
    for i in range(n):
        total = sum(lower[i][k] * forward[k] for k in range(i))
        forward[i] = (b[i] - total) / lower[i][i]

    x = [0.0] * n
    for i in reversed(range(n)):
        total = sum(lower[k][i] * x[k] for k in range(i + 1, n))
        x[i] = (forward[i] - total) / lower[i][i]
    return x if all(math.isfinite(v) for v in x) else None
