"""The plan brake: whether VIKI holds the chats it starts on its own, and until when.

Every start VIKI makes on its own asks `plan_hold` first; the user's own start does not. The
rule, over the newest plan reading (`conversations.limits_api.sanitize_reading`'s shape):

- No reading yet (None) holds with no end, at every brake, "off" included: the reading lives in
  memory, so until Claude Code reports the plan's use in this launch, VIKI cannot tell a reached
  limit from a free one.
- A rejected reading holds until its own `resets_at`, at every brake, "off" included: a start
  into a reached limit fails. Without a usable `resets_at` it holds with no end, until a newer
  reading replaces it.
- Unless the brake is "off", a window whose reset is still ahead and whose use is at or above
  the brake's share holds until that reset. The windows are the reading's own top-level pair
  (the limiting window, which can be one `windows` lacks), `windows.five_hour` and
  `windows.seven_day`.
- The hold ends at the latest of those resets. A reading with none holding holds nothing.

Pure: no I/O, no clock read (the caller passes `now`, an aware datetime), no log.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

# Local, as `vaults.vault.UNIX_EPOCH`: a hold's end is epoch plus seconds, never
# `datetime.fromtimestamp`, which fails on the platform's calendar limits.
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

PlanReading = Callable[[], Mapping[str, object] | None]


def no_reading() -> None:
    """The default `PlanReading`: no reading, so every start VIKI makes on its own is held."""
    return None


@dataclass(frozen=True)
class Hold:
    """VIKI's own starts are held; `until` is when the newest reading says that ends, or None
    when it names no end it can state."""

    until: datetime | None


def _int(value: object) -> int | None:
    """`value` when it is an int and not a bool, else None."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _holding_reset(part: object, share: float, at: float) -> int | None:
    """The reset of a `{utilization, resets_at}` part that is at or above `share` with its reset
    ahead of `at`; None for anything else, including a part that is not a mapping."""
    if not isinstance(part, Mapping):
        return None
    use = part.get("utilization")
    if isinstance(use, bool) or not isinstance(use, (int, float)) or use < share:
        return None
    reset = _int(part.get("resets_at"))
    return reset if reset is not None and reset > at else None


def plan_hold(reading: Mapping[str, object] | None, brake: str, now: datetime) -> Hold | None:
    """The hold `reading` puts on VIKI's own starts at `now`, or None when none holds.

    `brake` is one of "off", "50", "80", "95" (another value may raise `ValueError`); `now` is
    aware."""
    if reading is None:
        return Hold(None)
    at = now.timestamp()
    ends: list[int] = []
    if reading.get("status") == "rejected":
        # Its own reset only: the weekly window's reset says nothing about a five-hour rejection.
        own = _int(reading.get("resets_at"))
        if own is None:
            return Hold(None)
        if own > at:
            ends.append(own)
    if brake != "off":
        share = int(brake) / 100
        windows = reading.get("windows")
        parts = [reading]
        if isinstance(windows, Mapping):
            parts += [windows.get("five_hour"), windows.get("seven_day")]
        for part in parts:
            end = _holding_reset(part, share, at)
            if end is not None:
                ends.append(end)
    if not ends:
        return None
    try:
        return Hold(_EPOCH + timedelta(seconds=max(ends)))
    except OverflowError:
        # A reset past the calendar is still a hold, with no end to name.
        return Hold(None)
