"""The newest plan-limit reading, held in memory, and its private, bounded GET.

`sanitize_reading` rechecks the plan reading's shape (`viki_agent.drivers.claude.plan_limits`)
exactly: a reading is kept at all only when its `status` is one of the three known
values; every other field is individually validated or turned null, never rejecting the reading
as a whole. `PlanLimits` keeps only the newest fit reading in memory; an unfit `observe` call
changes nothing.

`limits_routes` serves one `GET /api/v2/limits` behind the Bearer token and the Origin check, as
`inventory_api.inventory_routes` does: no session, no limiter, no worker, just an in-memory read.
It logs nothing.
"""

from __future__ import annotations

import copy
from collections.abc import Callable, Mapping
from typing import get_args

from claude_agent_sdk import RateLimitStatus, RateLimitType
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from viki_agent.api_json import failure, json_response
from viki_agent.conversations.inventory_api import _text

_STATUSES = frozenset(get_args(RateLimitStatus))
_TYPES = frozenset(get_args(RateLimitType))
_MAX_SAFE_INTEGER = 2**53 - 1  # unix seconds; matches Number.MAX_SAFE_INTEGER on the wire


def _enum(value: object, allowed: frozenset[str]) -> str | None:
    """`value` when it is one of `allowed`, else None (a non-`str` never matches)."""
    return value if isinstance(value, str) and value in allowed else None


def _utilization(value: object) -> int | float | None:
    """A non-bool number in 0..10, else None. The range check alone refuses NaN and both
    infinities (every comparison against NaN is False; infinity fails `<= 10`), and never
    converts to float, so a huge int cannot overflow it the way `math.isfinite` would."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if 0 <= value <= 10 else None


def _resets_at(value: object) -> int | None:
    """A non-bool int in 1..2^53-1 (unix seconds), else None — a float is rejected even when
    whole-valued."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 1 <= value <= _MAX_SAFE_INTEGER else None


def _window(value: object) -> dict[str, object] | None:
    """One `windows` entry re-read as `{utilization, resets_at}`; None when `value` is not an
    object or both fields are off-shape."""
    if not isinstance(value, Mapping):
        return None
    utilization = _utilization(value.get("utilization"))
    resets_at = _resets_at(value.get("resets_at"))
    if utilization is None and resets_at is None:
        return None
    return {"utilization": utilization, "resets_at": resets_at}


def sanitize_reading(reading: object) -> dict[str, object] | None:
    """A new plan reading re-validated field by field, or None when `reading` is not an object or
    its `status` is off the three known values — the one check that rejects the reading whole."""
    if not isinstance(reading, Mapping):
        return None
    status = _enum(reading.get("status"), _STATUSES)
    if status is None:
        return None
    windows = reading.get("windows")
    windows = windows if isinstance(windows, Mapping) else {}
    return {
        "observed_at": _text(reading.get("observed_at")),
        "status": status,
        "limiting": _enum(reading.get("limiting"), _TYPES),
        "utilization": _utilization(reading.get("utilization")),
        "resets_at": _resets_at(reading.get("resets_at")),
        "windows": {
            "five_hour": _window(windows.get("five_hour")),
            "seven_day": _window(windows.get("seven_day")),
        },
    }


class PlanLimits:
    """The newest fit plan-limit reading, kept in memory until the next fit one replaces it."""

    def __init__(self) -> None:
        self._reading: dict[str, object] | None = None

    def observe(self, reading: object) -> None:
        """Keep a sanitized copy of `reading` when `sanitize_reading` finds it fit; the newest fit
        call wins. An unfit reading changes nothing."""
        sanitized = sanitize_reading(reading)
        if sanitized is not None:
            self._reading = sanitized

    def latest(self) -> dict[str, object] | None:
        """A copy of the newest fit reading, or None before any was observed."""
        return copy.deepcopy(self._reading) if self._reading is not None else None


def limits_routes(
    limits: PlanLimits,
    *,
    authorized: Callable[[Request], bool],
    origins: frozenset[str],
) -> list[Route]:
    async def endpoint(request: Request) -> Response:
        if not authorized(request):
            response = failure("unauthorized", "Authentication is required.", 401)
        elif request.headers.get("origin") not in {None, *origins}:
            response = failure("forbidden_origin", "This origin is not allowed.", 403)
        elif request.scope.get("query_string"):
            response = failure("invalid_input", "The limits request is invalid.", 400)
        else:
            response = json_response({"observation": limits.latest()})
        response.headers["Cache-Control"] = "no-store"
        return response

    return [Route("/api/v2/limits", endpoint, methods=["GET"])]
