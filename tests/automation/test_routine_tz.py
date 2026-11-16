"""IANA time-zone data is admitted and resolvable on every OS.

No network: the data ships in the tzdata wheel. No sys.platform branch: the same
assertions run on Windows, macOS and Linux.
"""

from __future__ import annotations

import importlib.metadata
import zoneinfo
from collections.abc import Iterator
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest


@pytest.fixture
def empty_tzpath() -> Iterator[None]:
    saved = zoneinfo.TZPATH
    ZoneInfo.clear_cache()
    zoneinfo.reset_tzpath(to=[])
    yield
    zoneinfo.reset_tzpath(to=list(saved))
    ZoneInfo.clear_cache()


@pytest.mark.parametrize(
    ("key", "offset_hours"),
    [
        ("UTC", 0),
        ("Asia/Tokyo", 9),
        ("America/New_York", -5),
        ("Europe/London", 0),
        ("Australia/Lord_Howe", 11),
    ],
)
def test_zone_loads_and_offsets(empty_tzpath: None, key: str, offset_hours: int) -> None:
    zone = ZoneInfo(key)
    offset = datetime(2026, 1, 15, 12, tzinfo=zone).utcoffset()
    assert offset == timedelta(hours=offset_hours)


def test_available_timezones_non_empty(empty_tzpath: None) -> None:
    assert len(zoneinfo.available_timezones()) > 0


def test_tzdata_version() -> None:
    assert importlib.metadata.version("tzdata") == "2026.3"
