"""The plan brake: which readings hold VIKI's own starts, and until when."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from viki_agent.conversations.limits_api import sanitize_reading
from viki_agent.decision_loop.plan_brake import Hold, no_reading, plan_hold
from viki_agent.drivers.claude.plan_limits import plan_limit_reading

NOW = datetime(2026, 10, 21, 9, 0, tzinfo=UTC)
_NOW_S = int(NOW.timestamp())
HOUR = 3600


def at(seconds: int) -> int:
    """A reset `seconds` after NOW, as the reading carries it (unix seconds)."""
    return _NOW_S + seconds


def until(seconds: int) -> Hold:
    return Hold(NOW + timedelta(seconds=seconds))


def window(use: object, reset: object) -> dict[str, object]:
    return {"utilization": use, "resets_at": reset}


def reading(
    *,
    status: str = "allowed",
    five_hour: dict[str, object] | None = None,
    seven_day: dict[str, object] | None = None,
    use: object = None,
    reset: object = None,
    limiting: str | None = None,
) -> dict[str, object]:
    """A reading in `sanitize_reading`'s shape."""
    return {
        "observed_at": "2026-10-21T08:59:00.000Z",
        "status": status,
        "limiting": limiting,
        "utilization": use,
        "resets_at": reset,
        "windows": {"five_hour": five_hour, "seven_day": seven_day},
    }


@pytest.mark.parametrize("brake", ["off", "50", "80", "95"])
def test_no_reading_yet_holds_with_no_end_at_every_brake(brake: str) -> None:
    # The reading lives in memory: until this launch's first one, a reached limit cannot be told
    # from a free one.
    assert no_reading() is None
    assert plan_hold(no_reading(), brake, NOW) == Hold(None)


@pytest.mark.parametrize("which", ["five_hour", "seven_day", "top_level"])
def test_a_window_at_or_above_the_brake_holds_until_its_reset(which: str) -> None:
    held = window(0.8, at(2 * HOUR))
    found = {
        "five_hour": reading(five_hour=held),
        "seven_day": reading(seven_day=held),
        "top_level": reading(use=0.8, reset=at(2 * HOUR), limiting="seven_day_opus"),
    }[which]
    assert plan_hold(found, "80", NOW) == until(2 * HOUR)


@pytest.mark.parametrize(
    "found",
    [
        reading(five_hour=window(0.79, at(HOUR))),
        reading(use=0.79, reset=at(HOUR)),
        reading(five_hour=window(0.99, at(-HOUR))),
        reading(five_hour=window(0.99, at(0))),
        reading(use=0.99, reset=at(-1)),
        reading(five_hour=window(None, at(HOUR))),
        reading(five_hour=window(0.99, None)),
        reading(five_hour=window(0.99, at(HOUR) + 0.5)),
        reading(five_hour=window(True, at(HOUR))),
        reading(five_hour=window("0.99", at(HOUR))),
        reading(five_hour=window(0.99, True)),
        reading(five_hour=None, seven_day=None),
        {"status": "allowed"},
        {"status": "allowed", "windows": "x"},
        {"status": "allowed", "windows": {"five_hour": "x", "seven_day": [0.99]}},
    ],
)
def test_below_the_brake_or_after_its_reset_nothing_holds(found: dict[str, object]) -> None:
    assert plan_hold(found, "80", NOW) is None


def test_a_reset_equal_to_now_holds_nothing() -> None:
    assert plan_hold(reading(seven_day=window(0.9, at(0))), "80", NOW) is None
    assert plan_hold(reading(status="rejected", reset=at(0)), "off", NOW) is None


def test_the_hold_ends_at_the_latest_holding_reset() -> None:
    found = reading(
        five_hour=window(0.85, at(HOUR)),
        seven_day=window(0.9, at(3 * HOUR)),
        use=0.81,
        reset=at(2 * HOUR),
    )
    assert plan_hold(found, "80", NOW) == until(3 * HOUR)


def test_a_window_below_the_brake_does_not_extend_the_hold() -> None:
    found = reading(five_hour=window(0.85, at(HOUR)), seven_day=window(0.2, at(100 * HOUR)))
    assert plan_hold(found, "80", NOW) == until(HOUR)


def test_the_limiting_windows_own_pair_counts() -> None:
    # Opus, Sonnet and overage have no entry in `windows`; only the top-level pair names them.
    found = reading(limiting="seven_day_sonnet", use=0.9, reset=at(5 * HOUR))
    assert plan_hold(found, "80", NOW) == until(5 * HOUR)


@pytest.mark.parametrize(
    ("brake", "use", "holds"),
    [
        ("50", 0.5, True),
        ("50", 0.49, False),
        ("80", 0.5, False),
        ("80", 0.8, True),
        ("95", 0.94, False),
        ("95", 0.95, True),
        ("95", 0.99, True),
    ],
)
def test_the_brake_sets_the_share_that_holds(brake: str, use: float, holds: bool) -> None:
    found = reading(five_hour=window(use, at(HOUR)))
    assert plan_hold(found, brake, NOW) == (until(HOUR) if holds else None)


def test_an_unknown_brake_is_refused() -> None:
    with pytest.raises(ValueError, match="invalid literal"):
        plan_hold(reading(), "70%", NOW)


@pytest.mark.parametrize("brake", ["off", "50", "80", "95"])
def test_a_rejected_reading_holds_until_its_own_reset_at_any_brake(brake: str) -> None:
    # The weekly window resets much later and sits below every brake: it must not extend the hold.
    found = reading(
        status="rejected",
        limiting="five_hour",
        use=1.0,
        reset=at(HOUR),
        seven_day=window(0.3, at(100 * HOUR)),
    )
    assert plan_hold(found, brake, NOW) == until(HOUR)


@pytest.mark.parametrize("brake", ["off", "50", "80", "95"])
def test_after_a_rejected_readings_reset_a_later_weekly_reset_does_not_extend_it(
    brake: str,
) -> None:
    found = reading(
        status="rejected",
        reset=at(-HOUR),
        seven_day=window(0.3, at(100 * HOUR)),
    )
    assert plan_hold(found, brake, NOW) is None


def test_a_rejected_reading_without_a_reset_holds_with_no_end() -> None:
    for brake in ("off", "80"):
        assert plan_hold(reading(status="rejected"), brake, NOW) == Hold(None)
        # Even with a window that has a reset: the rejection names none of its own.
        found = reading(status="rejected", seven_day=window(0.99, at(HOUR)))
        assert plan_hold(found, brake, NOW) == Hold(None)
    assert plan_hold(reading(status="rejected", reset=True), "80", NOW) == Hold(None)


def test_a_reset_past_the_calendar_holds_with_no_end() -> None:
    far = 2**53 - 1
    assert plan_hold(reading(status="rejected", reset=far), "off", NOW) == Hold(None)
    assert plan_hold(reading(five_hour=window(0.9, far)), "80", NOW) == Hold(None)
    # The latest end decides: a far reset beside a near one still names no end.
    found = reading(five_hour=window(0.9, at(HOUR)), seven_day=window(0.9, far))
    assert plan_hold(found, "80", NOW) == Hold(None)


def test_off_holds_only_a_rejected_reading() -> None:
    assert plan_hold(reading(five_hour=window(0.99, at(HOUR))), "off", NOW) is None
    assert plan_hold(reading(use=0.99, reset=at(HOUR)), "off", NOW) is None
    assert (
        plan_hold(reading(status="allowed_warning", use=0.99, reset=at(HOUR)), "off", NOW) is None
    )
    assert plan_hold(reading(status="rejected", reset=at(HOUR)), "off", NOW) == until(HOUR)


def test_the_brake_reads_the_sanitized_shape() -> None:
    reset = at(2 * HOUR)
    info = {
        "status": "allowed_warning",
        "rateLimitType": "five_hour",
        "utilization": 0.9,
        "resetsAt": reset,
        "unifiedWindows": {
            "five_hour": {"utilization": 0.9, "resetsAt": reset},
            "seven_day": {"utilization": 0.1, "resetsAt": at(100 * HOUR)},
        },
    }
    sanitized = sanitize_reading(plan_limit_reading(info, "2026-10-21T08:59:00.000Z"))
    assert sanitized is not None
    assert plan_hold(sanitized, "80", NOW) == until(2 * HOUR)
    assert plan_hold(sanitized, "95", NOW) is None
    assert plan_hold(sanitized, "off", NOW) is None
