"""Reduce a `rate_limit_event` payload to plan-limit fields safe for the browser.

Before CLI 2.1.281, `rate_limit_info` carries `overageStatus` and
`overageDisabledReason`; 2.1.281 adds top-level `utilization` and drops those fields. The
reducer ignores overage details, identifiers and session metadata.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, get_args

from claude_agent_sdk import RateLimitStatus, RateLimitType

_STATUSES = frozenset(get_args(RateLimitStatus))
_TYPES = frozenset(get_args(RateLimitType))
_MAX_SAFE_INTEGER = 2**53 - 1  # unix seconds; matches Number.MAX_SAFE_INTEGER on the wire


def _enum(value: Any, allowed: frozenset[str]) -> str | None:
    """`value` when it is one of `allowed`, else None (a non-`str` never matches)."""
    return value if isinstance(value, str) and value in allowed else None


def _utilization(value: Any) -> int | float | None:
    """A non-bool number in 0..10, else None. The range check alone refuses NaN and both
    infinities (every comparison against NaN is False; infinity fails `<= 10`), and never
    converts to float, so a huge int cannot overflow it the way `math.isfinite` would."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if 0 <= value <= 10 else None


def _resets_at(value: Any) -> int | None:
    """A non-bool int in 1..2^53-1 (unix seconds), else None — a float is rejected even when
    whole-valued."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 1 <= value <= _MAX_SAFE_INTEGER else None


def _window(value: Any) -> dict[str, object] | None:
    """One `unifiedWindows` entry as `{utilization, resets_at}`; None when `value` is not an
    object or both fields are off-shape."""
    if not isinstance(value, Mapping):
        return None
    utilization = _utilization(value.get("utilization"))
    resets_at = _resets_at(value.get("resetsAt"))
    if utilization is None and resets_at is None:
        return None
    return {"utilization": utilization, "resets_at": resets_at}


def plan_limit_reading(info: Mapping[str, Any], observed_at: str) -> dict[str, object] | None:
    """Reduce one `rate_limit_info` mapping, or return None for an unknown status."""
    status = _enum(info.get("status"), _STATUSES)
    if status is None:
        return None
    windows = info.get("unifiedWindows")
    windows = windows if isinstance(windows, Mapping) else {}
    return {
        "observed_at": observed_at,
        "status": status,
        "limiting": _enum(info.get("rateLimitType"), _TYPES),
        "utilization": _utilization(info.get("utilization")),
        "resets_at": _resets_at(info.get("resetsAt")),
        "windows": {
            "five_hour": _window(windows.get("five_hour")),
            "seven_day": _window(windows.get("seven_day")),
        },
    }
