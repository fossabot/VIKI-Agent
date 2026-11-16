"""`plan_limit_reading`: the whitelist for a `rate_limit_info` mapping."""

from __future__ import annotations

from viki_agent.drivers.claude.plan_limits import plan_limit_reading


def test_a_recorded_frame_gives_both_windows() -> None:
    # tests/fixtures/claude/ask-user-question.jsonl, scenario 07's rate_limit_event frame
    # (the pre-2.1.281 shape: no top-level `utilization`, both unifiedWindows entries present).
    info = {
        "status": "allowed",
        "resetsAt": 1789128000,
        "rateLimitType": "five_hour",
        "overageStatus": "rejected",
        "overageDisabledReason": "out_of_credits",
        "isUsingOverage": False,
        "unifiedWindows": {
            "five_hour": {"utilization": 0.15, "resetsAt": 1789128000},
            "seven_day": {"utilization": 0.09, "resetsAt": 1789560000},
        },
    }
    assert plan_limit_reading(info, "2026-10-09T00:00:00.000Z") == {
        "observed_at": "2026-10-09T00:00:00.000Z",
        "status": "allowed",
        "limiting": "five_hour",
        "utilization": None,
        "resets_at": 1789128000,
        "windows": {
            "five_hour": {"utilization": 0.15, "resets_at": 1789128000},
            "seven_day": {"utilization": 0.09, "resets_at": 1789560000},
        },
    }


def test_the_2_1_281_shape_gives_the_top_level_utilization() -> None:
    # CLI 2.1.281 adds a top-level `utilization` and drops
    # `overageStatus`/`overageDisabledReason`.
    info = {
        "status": "allowed_warning",
        "resetsAt": 1789128000,
        "rateLimitType": "seven_day",
        "utilization": 0.73,
    }
    assert plan_limit_reading(info, "t") == {
        "observed_at": "t",
        "status": "allowed_warning",
        "limiting": "seven_day",
        "utilization": 0.73,
        "resets_at": 1789128000,
        "windows": {"five_hour": None, "seven_day": None},
    }


def test_off_shape_fields_read_null_and_a_bad_status_reads_none() -> None:
    null_reading = {
        "observed_at": "t",
        "status": "rejected",
        "limiting": None,
        "utilization": None,
        "resets_at": None,
        "windows": {"five_hour": None, "seven_day": None},
    }

    # bool: bool is an int subclass, but is never a valid utilization.
    assert plan_limit_reading({"status": "rejected", "utilization": True}, "t") == null_reading
    # NaN and inf: the range check alone excludes them, no math.isfinite needed.
    assert (
        plan_limit_reading({"status": "rejected", "utilization": float("nan")}, "t") == null_reading
    )
    assert (
        plan_limit_reading({"status": "rejected", "utilization": float("inf")}, "t") == null_reading
    )
    # huge int: out of range, and compared directly as an int — never raises OverflowError.
    assert plan_limit_reading({"status": "rejected", "utilization": 10**400}, "t") == null_reading
    # negative: resets_at's range starts at 1.
    assert plan_limit_reading({"status": "rejected", "resetsAt": -5}, "t") == null_reading
    # float resetsAt: rejected even whole-valued — resets_at must be an int.
    assert plan_limit_reading({"status": "rejected", "resetsAt": 1789128000.0}, "t") == null_reading
    # non-object window: a unifiedWindows entry that isn't itself a mapping reads as None.
    assert (
        plan_limit_reading(
            {"status": "rejected", "unifiedWindows": {"five_hour": "not-an-object"}}, "t"
        )
        == null_reading
    )

    # a bad status: the whole reading is None, not a dict of nulls.
    assert plan_limit_reading({"status": "bogus"}, "t") is None
    assert plan_limit_reading({"status": True}, "t") is None
    assert plan_limit_reading({}, "t") is None


def test_nothing_else_is_copied() -> None:
    info = {
        "status": "allowed",
        "resetsAt": 1789128000,
        "rateLimitType": "five_hour",
        "overageStatus": "rejected",
        "overageDisabledReason": "out_of_credits",
        "isUsingOverage": False,
        "uuid": "4aa6deaa-2581-41b8-8081-7370691684bd",
        "session_id": "3253e09a-7232-45f7-86fb-a175bf8ddf3c",
        "seven_day_overage_included": True,
        "unifiedWindows": {
            "five_hour": {"utilization": 0.15, "resetsAt": 1789128000, "overageIncluded": True},
            "seven_day": {"utilization": 0.09, "resetsAt": 1789560000},
        },
    }
    assert plan_limit_reading(info, "t") == {
        "observed_at": "t",
        "status": "allowed",
        "limiting": "five_hour",
        "utilization": None,
        "resets_at": 1789128000,
        "windows": {
            "five_hour": {"utilization": 0.15, "resets_at": 1789128000},
            "seven_day": {"utilization": 0.09, "resets_at": 1789560000},
        },
    }
