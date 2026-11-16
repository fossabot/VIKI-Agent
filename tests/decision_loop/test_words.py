"""words.py: due dates, ages, counts, parts of the day and candidate types as words, with
every bucket edge pinned and no numeral in any word."""

from __future__ import annotations

import unicodedata
from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from viki_agent.decision_loop import contracts, words
from viki_agent.decision_loop.words import (
    age_words,
    count_words,
    due_words,
    time_of_day,
    type_words,
    wait_words,
)

TOKYO = timezone(timedelta(hours=9))
NOW = datetime(2026, 10, 22, 10, 30, tzinfo=TOKYO)  # a Thursday morning in Tokyo


def _has_numeral(text: str) -> bool:
    return any(unicodedata.category(ch).startswith("N") for ch in text)


# ---- due_words ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("days", "expected"),
    [
        (-30, "overdue"),
        (-1, "overdue"),
        (0, "due today"),
        (1, "due tomorrow"),
        (2, "due this week"),
        (words.DUE_THIS_WEEK_DAYS - 1, "due this week"),
        (words.DUE_THIS_WEEK_DAYS, "due next week"),
        (words.DUE_NEXT_WEEK_DAYS - 1, "due next week"),
        (words.DUE_NEXT_WEEK_DAYS, "due later"),
        (400, "due later"),
    ],
)
def test_due_words_buckets_by_calendar_day(days: int, expected: str) -> None:
    assert due_words(NOW + timedelta(days=days), NOW) == expected
    assert due_words(NOW.date() + timedelta(days=days), NOW) == expected


def test_due_boundaries_are_one_and_two_weeks() -> None:
    assert (words.DUE_THIS_WEEK_DAYS, words.DUE_NEXT_WEEK_DAYS) == (7, 14)


def test_due_words_reads_the_day_not_the_hour() -> None:
    start = NOW.replace(hour=0, minute=0)
    last = NOW.replace(hour=23, minute=59, second=59)
    assert due_words(start, NOW) == "due today"  # earlier today, already past
    assert due_words(last, NOW) == "due today"
    assert due_words(start - timedelta(microseconds=1), NOW) == "overdue"
    assert due_words(last + timedelta(seconds=1), NOW) == "due tomorrow"


def test_due_words_reads_the_deadline_on_nows_wall_clock() -> None:
    # 23:30 UTC on the 22nd is 08:30 on the 23rd in Tokyo.
    assert due_words(datetime(2026, 10, 22, 23, 30, tzinfo=UTC), NOW) == "due tomorrow"
    # 14:00 UTC on the 21st is 23:00 on the 21st in Tokyo.
    assert due_words(datetime(2026, 10, 21, 14, 0, tzinfo=UTC), NOW) == "overdue"


def test_due_words_takes_a_naive_time_as_it_is() -> None:
    assert due_words(datetime(2026, 10, 23, 1, 0), NOW) == "due tomorrow"
    assert due_words(datetime(2026, 10, 23, 1, 0, tzinfo=TOKYO), NOW.replace(tzinfo=None)) == (
        "due tomorrow"
    )


# ---- age_words ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("days_ago", "expected"),
    [
        (-3, "today"),
        (0, "today"),
        (1, "yesterday"),
        (2, "this week"),
        (words.AGE_THIS_WEEK_DAYS - 1, "this week"),
        (words.AGE_THIS_WEEK_DAYS, "last week"),
        (words.AGE_LAST_WEEK_DAYS - 1, "last week"),
        (words.AGE_LAST_WEEK_DAYS, "this month"),
        (words.AGE_THIS_MONTH_DAYS - 1, "this month"),
        (words.AGE_THIS_MONTH_DAYS, "older"),
        (900, "older"),
    ],
)
def test_age_words_buckets_by_calendar_day(days_ago: int, expected: str) -> None:
    assert age_words(NOW - timedelta(days=days_ago), NOW) == expected
    assert age_words(NOW.date() - timedelta(days=days_ago), NOW) == expected


def test_age_boundaries_are_a_week_two_weeks_and_thirty_days() -> None:
    assert (words.AGE_THIS_WEEK_DAYS, words.AGE_LAST_WEEK_DAYS, words.AGE_THIS_MONTH_DAYS) == (
        7,
        14,
        30,
    )


def test_age_words_reads_the_day_on_nows_wall_clock() -> None:
    start = NOW.replace(hour=0, minute=0)
    assert age_words(start, NOW) == "today"
    assert age_words(start - timedelta(microseconds=1), NOW) == "yesterday"
    # 14:00 UTC on the 21st is 23:00 on the 21st in Tokyo: yesterday there, not today.
    assert age_words(datetime(2026, 10, 21, 14, 0, tzinfo=UTC), NOW) == "yesterday"
    # 15:00 UTC on the 21st is midnight on the 22nd in Tokyo.
    assert age_words(datetime(2026, 10, 21, 15, 0, tzinfo=UTC), NOW) == "today"


# ---- count_words --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("n", "expected"),
    [
        (-5, "none"),
        (-1, "none"),
        (0, "none"),
        (1, "one"),
        (2, "two"),
        (3, "a few"),
        (4, "a few"),
        (5, "several"),
        (9, "several"),
        (10, "many"),
        (10_000, "many"),
    ],
)
def test_count_words_edges(n: int, expected: str) -> None:
    assert count_words(n) == expected


def test_count_bounds_are_the_designed_ones() -> None:
    assert words.COUNT_WORDS == (
        (0, "none"),
        (1, "one"),
        (2, "two"),
        (3, "a few"),
        (5, "several"),
        (10, "many"),
    )


# ---- time_of_day --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("hour", "minute", "expected"),
    [
        (0, 0, "night"),
        (5, 59, "night"),
        (6, 0, "morning"),
        (11, 59, "morning"),
        (12, 0, "afternoon"),
        (17, 59, "afternoon"),
        (18, 0, "evening"),
        (23, 59, "evening"),
    ],
)
def test_time_of_day_edges(hour: int, minute: int, expected: str) -> None:
    assert time_of_day(NOW.replace(hour=hour, minute=minute)) == expected


def test_day_parts_start_at_the_designed_hours() -> None:
    assert words.DAY_PARTS == ((0, "night"), (6, "morning"), (12, "afternoon"), (18, "evening"))


def test_time_of_day_reads_the_time_as_given() -> None:
    # 07:00 in Tokyo is 22:00 UTC the day before: the user's morning, not an evening.
    local = datetime(2026, 10, 22, 7, 0, tzinfo=TOKYO)
    assert time_of_day(local) == "morning"
    assert time_of_day(local.astimezone(UTC)) == "evening"


# ---- type_words ---------------------------------------------------------------------------------


def test_type_words_name_every_candidate() -> None:
    assert type_words("tasks_due") == "task due"
    assert set(words.TYPE_WORDS) == set(contracts.CANDIDATES)
    assert all(type_words(name) == words.TYPE_WORDS[name] for name in contracts.CANDIDATES)


def test_type_words_name_the_kanban_cards() -> None:
    assert type_words("card_needs_you") == "Kanban card that needs you"
    assert type_words("card_stale") == "Kanban card that stopped moving"


def test_type_words_name_the_seat_items() -> None:
    assert [type_words(name) for name in contracts.SEATS] == [
        "Bot run waiting for you",
        "Bot working on a card",
        "Bot that could start on a card",
        "report on a Bot run",
    ]


def test_type_words_reads_an_unlisted_type_with_spaces() -> None:
    assert type_words("some_new_type") == "some new type"
    assert type_words("retro") == "retro"


def test_type_words_name_the_look_items() -> None:
    assert [type_words(name) for name in contracts.LOOKS] == [
        "vault recovery that needs a look",
        "memory whose source note changed",
        "messaging bridge that stopped",
        "chat waiting for your answer",
        "message that may not have reached Claude Code",
        "MCP server filling a chat's context",
    ]


def test_type_words_name_the_code_look() -> None:
    assert type_words("code_look") == "code folder with changes left uncommitted"


def test_type_words_name_the_second_looks() -> None:
    assert [type_words(name) for name in contracts.SECOND_LOOKS] == [
        "second look at a finished long task",
        "second look at corrected work",
    ]


def test_type_words_name_the_project_plans() -> None:
    assert [type_words(name) for name in contracts.PROJECT_PLANS] == [
        "project with no recent change",
        "overdue tasks to check",
    ]


def test_type_words_name_the_message_drafts() -> None:
    assert [type_words(name) for name in contracts.MESSAGE_DRAFTS] == [
        "handoff for a chat that is nearly full",
        "literature notes for fetched web pages",
    ]


def test_type_words_name_the_saved_checks() -> None:
    assert [type_words(name) for name in contracts.SAVED_CHECKS] == [
        "saved checks on a code folder's new commit"
    ]


def test_type_words_name_the_inbox_triage() -> None:
    assert [type_words(name) for name in contracts.INBOX_TRIAGE] == [
        "suggested destinations for inbox captures"
    ]


# ---- wait_words ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("waited", "expected"),
    [
        (timedelta(minutes=-5), "a few minutes"),
        (timedelta(0), "a few minutes"),
        (timedelta(minutes=19), "a few minutes"),
        (timedelta(minutes=20), "about half an hour"),
        (timedelta(minutes=44), "about half an hour"),
        (timedelta(minutes=45), "about an hour"),
        (timedelta(minutes=89), "about an hour"),
        (timedelta(minutes=90), "over an hour"),
        (timedelta(hours=2, minutes=59), "over an hour"),
        (timedelta(hours=3), "hours"),
        (timedelta(hours=23), "hours"),
        (timedelta(days=1), "over a day"),
        (timedelta(days=9), "over a day"),
    ],
)
def test_wait_words_read_each_bucket(waited: timedelta, expected: str) -> None:
    assert wait_words(NOW - waited, NOW) == expected


# ---- no numerals ---------------------------------------------------------------------------------


def test_no_word_holds_a_numeral() -> None:
    produced = {
        *(due_words(NOW + timedelta(days=d), NOW) for d in range(-3, 40)),
        *(age_words(NOW - timedelta(days=d), NOW) for d in range(-3, 40)),
        *(count_words(n) for n in range(-2, 40)),
        *(time_of_day(NOW.replace(hour=h)) for h in range(24)),
        *(type_words(name) for name in contracts.CANDIDATES),
        *(wait_words(NOW - timedelta(minutes=m), NOW) for m in range(0, 3000, 5)),
    }
    assert len(produced) == 6 + 6 + 6 + 4 + len(contracts.CANDIDATES) + 6
    assert not [word for word in produced if _has_numeral(word)]


def test_due_and_age_accept_a_plain_date() -> None:
    assert due_words(date(2026, 10, 22), NOW) == "due today"
    assert age_words(date(2026, 10, 21), NOW) == "yesterday"
