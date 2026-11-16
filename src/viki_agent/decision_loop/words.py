"""Word forms of the code values the Decision model reads.

The model reads facts as words, not numbers: a due date reads "due tomorrow", an age "last
week", a count "a few", a clock time "morning", a candidate type "task due". The code keeps the
numbers and does the arithmetic. Every bucket boundary is a constant here, and no word holds a
digit.

Pure: no I/O and no clock reads; every time is passed in.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime, timedelta

# Calendar days from today to a deadline's day: below 0 is overdue, 0 today, 1 tomorrow; then
# the day count is "this week" below DUE_THIS_WEEK_DAYS, "next week" below DUE_NEXT_WEEK_DAYS
# and "later" from there.
DUE_THIS_WEEK_DAYS = 7
DUE_NEXT_WEEK_DAYS = 14

# Calendar days from a creation day to today: 0 or below is today, 1 yesterday; then "this
# week" below AGE_THIS_WEEK_DAYS, "last week" below AGE_LAST_WEEK_DAYS, "this month" below
# AGE_THIS_MONTH_DAYS and "older" from there.
AGE_THIS_WEEK_DAYS = 7
AGE_LAST_WEEK_DAYS = 14
AGE_THIS_MONTH_DAYS = 30

# (lowest count, word), ascending: a count reads as the last word whose lowest count it reaches;
# below zero reads "none".
COUNT_WORDS: tuple[tuple[int, str], ...] = (
    (0, "none"),
    (1, "one"),
    (2, "two"),
    (3, "a few"),
    (5, "several"),
    (10, "many"),
)

# (first hour, word), ascending: an hour reads as the last part of the day it has reached.
DAY_PARTS: tuple[tuple[int, str], ...] = (
    (0, "night"),
    (6, "morning"),
    (12, "afternoon"),
    (18, "evening"),
)

# (shortest wait, word), ascending: a wait reads as the last word whose shortest wait it reaches.
WAIT_WORDS: tuple[tuple[timedelta, str], ...] = (
    (timedelta(0), "a few minutes"),
    (timedelta(minutes=20), "about half an hour"),
    (timedelta(minutes=45), "about an hour"),
    (timedelta(minutes=90), "over an hour"),
    (timedelta(hours=3), "hours"),
    (timedelta(days=1), "over a day"),
)

# Each candidate type as the model reads it; a type missing here reads with spaces for "_".
TYPE_WORDS: Mapping[str, str] = {
    "approvals": "approvals waiting",
    "tasks_due": "task due",
    "agent_runs": "agent runs",
    "inbox": "inbox captures",
    "uncommitted": "uncommitted changes",
    "routine": "routine",
    "mission_report": "report from a chat VIKI started",
    "skill_offer": "offer to keep work as a skill",
    "skill_suggest": "suggestion to use one of your own skills in this chat",
    "learn_source": "captured source to learn",
    "retro_due": "suggestion to run a retro",
    "question_tune": "better wording for one of Decision's questions",
    "note_link": "proposal to link two notes",
    "note_superseded": "proposal to mark a note replaced by a newer one",
    "tasks_ticked": "proposal to archive ticked tasks",
    "card_needs_you": "Kanban card that needs you",
    "card_stale": "Kanban card that stopped moving",
    "seat_waiting": "Bot run waiting for you",
    "seat_working": "Bot working on a card",
    "seat_start": "Bot that could start on a card",
    "seat_report": "report on a Bot run",
    "vault_recovery": "vault recovery that needs a look",
    "memory_drift": "memory whose source note changed",
    "bridge_stopped": "messaging bridge that stopped",
    "chat_waiting": "chat waiting for your answer",
    "send_uncertain": "message that may not have reached Claude Code",
    "mcp_context": "MCP server filling a chat's context",
    "code_look": "code folder with changes left uncommitted",
    "long_task_look": "second look at a finished long task",
    "correction_look": "second look at corrected work",
    "stalled_project": "project with no recent change",
    "overdue_tasks": "overdue tasks to check",
    "context_handoff": "handoff for a chat that is nearly full",
    "source_note": "literature notes for fetched web pages",
    "commit_checks": "saved checks on a code folder's new commit",
    "inbox_triage": "suggested destinations for inbox captures",
}


def due_words(deadline: date, now: datetime) -> str:
    """How soon `deadline` falls, by calendar day on `now`'s wall clock: "overdue", "due today",
    "due tomorrow", "due this week", "due next week" or "due later". A deadline earlier today
    still reads "due today"; a bare date is that day."""
    days = (_day(deadline, now) - now.date()).days
    if days < 0:
        return "overdue"
    if days == 0:
        return "due today"
    if days == 1:
        return "due tomorrow"
    if days < DUE_THIS_WEEK_DAYS:
        return "due this week"
    if days < DUE_NEXT_WEEK_DAYS:
        return "due next week"
    return "due later"


def age_words(created: date, now: datetime) -> str:
    """How long ago `created` was, by calendar day on `now`'s wall clock: "today", "yesterday",
    "this week", "last week", "this month" or "older". A future day reads "today"."""
    days = (now.date() - _day(created, now)).days
    if days <= 0:
        return "today"
    if days == 1:
        return "yesterday"
    if days < AGE_THIS_WEEK_DAYS:
        return "this week"
    if days < AGE_LAST_WEEK_DAYS:
        return "last week"
    if days < AGE_THIS_MONTH_DAYS:
        return "this month"
    return "older"


def count_words(n: int) -> str:
    """`n` in words: "none", "one", "two", "a few" (3-4), "several" (5-9) or "many" (10+)."""
    word = COUNT_WORDS[0][1]
    for lowest, name in COUNT_WORDS:
        if n >= lowest:
            word = name
    return word


def wait_words(since: datetime, now: datetime) -> str:
    """How long it has been from `since` to `now`: "a few minutes", "about half an hour",
    "about an hour", "over an hour", "hours" or "over a day". A time ahead reads the first."""
    waited = now - since
    word = WAIT_WORDS[0][1]
    for shortest, name in WAIT_WORDS:
        if waited >= shortest:
            word = name
    return word


def time_of_day(now: datetime) -> str:
    """The part of the day `now`'s own hour falls in: "night" (0-5), "morning" (6-11),
    "afternoon" (12-17) or "evening" (18-23). Pass the user's local time; nothing converts it."""
    word = DAY_PARTS[0][1]
    for first, name in DAY_PARTS:
        if now.hour >= first:
            word = name
    return word


def type_words(candidate: str) -> str:
    """A candidate type as the model reads it ("tasks_due" is "task due")."""
    return TYPE_WORDS.get(candidate, candidate.replace("_", " "))


def _day(value: date, now: datetime) -> date:
    """The calendar day of `value` on `now`'s wall clock; a bare date, or a time when either side
    lacks a zone, is taken as it is."""
    if not isinstance(value, datetime):
        return value
    if value.tzinfo is not None and now.tzinfo is not None:
        return value.astimezone(now.tzinfo).date()
    return value.date()
