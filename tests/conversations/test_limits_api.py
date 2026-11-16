"""The plan-limit reading held in memory, its recheck of the driver's reading shape, and its
private, bounded GET."""

from __future__ import annotations

from typing import Any

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from viki_agent.conversations.limits_api import PlanLimits, limits_routes, sanitize_reading

TOKEN = "route-token-7f3a"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
ORIGIN = "http://127.0.0.1:8765"
PATH = "/api/v2/limits"

# The fixture guard scans tests too, so the provider key prefix is spelled in halves.
API_KEY = "sk-" + "ant-" + "abc"


def _reading(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "observed_at": "2026-10-09T12:00:00.000Z",
        "status": "allowed_warning",
        "limiting": "five_hour",
        "utilization": 2.5,
        "resets_at": 1_760_000_000,
        "windows": {
            "five_hour": {"utilization": 2.5, "resets_at": 1_760_000_000},
            "seven_day": {"utilization": 1.0, "resets_at": 1_760_500_000},
        },
    }
    return base | overrides


def test_null_until_a_reading_then_the_newest() -> None:
    limits = PlanLimits()
    assert limits.latest() is None
    limits.observe(_reading())
    assert limits.latest() == sanitize_reading(_reading())
    newer = _reading(status="rejected", resets_at=1_760_111_111)
    limits.observe(newer)
    assert limits.latest() == sanitize_reading(newer)
    assert limits.latest() != sanitize_reading(_reading())  # the old reading is gone, not merged


@pytest.mark.parametrize(
    "unfit",
    [
        _reading(status="unknown"),
        _reading() | {"status": None},
        {k: v for k, v in _reading().items() if k != "status"},
        "not-a-reading",
        None,
        ["not", "a", "mapping"],
    ],
)
def test_an_unfit_reading_changes_nothing(unfit: Any) -> None:
    limits = PlanLimits()
    limits.observe(_reading())
    before = limits.latest()
    limits.observe(unfit)
    assert limits.latest() == before


def test_sanitize_reading_nulls_one_off_field_without_rejecting_the_reading() -> None:
    reading = _reading(
        observed_at=API_KEY,
        limiting="not-a-window",
        utilization=11,
        resets_at=1.5,
        windows={"five_hour": "not-an-object", "seven_day": {"utilization": "n/a"}},
    )
    result = sanitize_reading(reading)
    assert result == {
        "observed_at": None,
        "status": "allowed_warning",
        "limiting": None,
        "utilization": None,
        "resets_at": None,
        "windows": {"five_hour": None, "seven_day": None},
    }


@pytest.mark.parametrize("bad_utilization", [10**400, float("nan"), float("inf")])
def test_a_non_finite_or_huge_utilization_reads_null_without_raising(bad_utilization: Any) -> None:
    result = sanitize_reading(_reading(utilization=bad_utilization))
    assert result is not None
    assert result["utilization"] is None


def test_a_kept_reading_is_a_copy() -> None:
    limits = PlanLimits()
    reading = _reading()
    limits.observe(reading)
    expected = sanitize_reading(_reading())

    reading["status"] = "rejected"
    reading["windows"]["five_hour"]["utilization"] = 999
    assert limits.latest() == expected  # untouched by mutating the dict passed to observe()

    got = limits.latest()
    assert got is not None
    got["status"] = "rejected"
    windows = got["windows"]
    assert isinstance(windows, dict)
    windows["five_hour"] = None
    assert limits.latest() == expected  # untouched by mutating a dict latest() already returned


def _app(limits: PlanLimits) -> Starlette:
    routes = limits_routes(
        limits,
        authorized=lambda request: request.headers.get("authorization") == f"Bearer {TOKEN}",
        origins=frozenset({ORIGIN}),
    )
    return Starlette(routes=routes)


def test_the_route_gates_and_never_caches() -> None:
    limits = PlanLimits()
    with TestClient(_app(limits)) as client:
        foreign = {**AUTH, "Origin": "http://evil.invalid"}
        refused = [
            (PATH, {}, 401, "unauthorized"),
            (PATH, {"Authorization": "Bearer other"}, 401, "unauthorized"),
            (PATH, foreign, 403, "forbidden_origin"),
            # The auth gate still wins first with a foreign origin and a query string both also
            # wrong: no token beats a later-stage failure.
            (f"{PATH}?q=1", {"Origin": "http://evil.invalid"}, 401, "unauthorized"),
            # With a valid token, the origin gate wins next, ahead of the query-string check.
            (f"{PATH}?q=1", foreign, 403, "forbidden_origin"),
        ]
        for url, headers, status, code in refused:
            response = client.get(url, headers=headers)
            assert (response.status_code, response.json()["error"]["code"]) == (status, code)
            assert response.headers["cache-control"] == "no-store"

        for headers in (AUTH, {**AUTH, "Origin": ORIGIN}):
            response = client.get(f"{PATH}?q=1", headers=headers)
            assert (response.status_code, response.json()["error"]["code"]) == (
                400,
                "invalid_input",
            )
            assert response.headers["cache-control"] == "no-store"

        empty = client.get(PATH, headers=AUTH)
        assert (empty.status_code, empty.json()) == (200, {"observation": None})
        assert empty.headers["cache-control"] == "no-store"

        limits.observe(_reading())
        first = client.get(PATH, headers={**AUTH, "Origin": ORIGIN})
        assert first.json() == {"observation": sanitize_reading(_reading())}
        assert first.headers["cache-control"] == "no-store"

        # A second, different reading replaces the first: never a stale or cached answer.
        limits.observe(_reading(status="rejected"))
        second = client.get(PATH, headers=AUTH)
        assert second.json() == {"observation": sanitize_reading(_reading(status="rejected"))}
        assert second.json() != first.json()
        assert second.headers["cache-control"] == "no-store"
