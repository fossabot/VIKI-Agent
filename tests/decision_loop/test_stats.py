"""Tests for DecisionLoopStats: the page's 30-day counts, recent decisions, muted candidates,
the stored episode range, today's count, the rows the learning fit reads (`learning_rows`,
`label_of`), the shadow comparison reads (`shadow_rows`) and the trial comparison reads
(`trial_rows`), `owner_rows` (the rows `decision_loop.owner.fill` turns into the Decision
model's `owner` part), and the weekly held-back audit and its launcher nudge (`audit`, `review`,
`week_start`), and the message drafts' outcomes and today's count (`draft_rows`,
`drafts_today`, also for the saved checks and the inbox suggestions).

A port of the earlier engine's statistics tests onto decision_episodes/decision_preferences,
without the study and by-mode breakdowns; it adds cases for the members that engine never had:
the 30-day window, `today`, `muted` and a recent row's `why`. `held_back` is `audit`'s stable
weekly sample.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from viki_agent import protection
from viki_agent.automation.routines import stamp
from viki_agent.decision_loop import stats as stats_module
from viki_agent.decision_loop.agenda import CLOSED_KEEP_DAYS, DecisionAgenda, ItemWrite
from viki_agent.decision_loop.changes import MAX_CHANGES
from viki_agent.decision_loop.contracts import (
    AUDIT_SIZE,
    CHANGES,
    INBOX_TRIAGE,
    MAX_CALLS,
    MAX_EPISODES,
    MAX_LEARN_ROWS,
    MAX_OPEN_ITEMS,
    RETENTION_DAYS,
    SAVED_CHECKS,
    DecisionLoopError,
    EvidenceLabel,
    ItemBody,
    LookRow,
    NoulAnswer,
    sha256_hex,
)
from viki_agent.decision_loop.cycle import ShadowSummary, TrialSummary, encode_answers
from viki_agent.decision_loop.missions import _day
from viki_agent.decision_loop.proposals import MAX_PROPOSALS
from viki_agent.decision_loop.state import MUTED_OR_WRONG, RECENT, RECENT_FEEDBACK
from viki_agent.decision_loop.stats import (
    COUNT_WINDOW_DAYS,
    MAX_DRAFT_ROWS,
    MAX_RECENT,
    DecisionLoopStats,
    OwnerRows,
    label_of,
    week_start,
)
from viki_agent.state import AppState, Prefs
from viki_agent.storage import SessionStore

JST = timezone(timedelta(hours=9))
LONDON = ZoneInfo("Europe/London")  # observes DST; clocks go back 2026-10-25 at 01:00 UTC
NOW = datetime(2026, 10, 9, 9, 30, tzinfo=JST)  # a Friday
DAY = "2026-10-09"
SCOPE = "00000000-0000-4000-8000-0000000000a1"
OTHER = "00000000-0000-4000-8000-0000000000b2"

_ZERO_FEEDBACK = {"useful": 0, "wrong": 0, "later": 0, "mute": 0, "missed": 0, "right_to_hold": 0}


@pytest.fixture
def store(tmp_path: Path) -> SessionStore:
    return AppState(tmp_path / "state.json", Prefs()).store


@pytest.fixture
def stats(store: SessionStore) -> DecisionLoopStats:
    return DecisionLoopStats(store)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _code(call: Callable[[], object]) -> str:
    with pytest.raises(DecisionLoopError) as failure:
        call()
    return failure.value.code


def _row(**overrides: Any) -> dict[str, Any]:
    """A stored decision row, written straight to the table (mirrors test_store.py's seeding)."""
    episode_id = overrides.get("episode_id", str(uuid.uuid4()))
    return {
        "episode_id": episode_id,
        "scope": SCOPE,
        "entry": "decision",
        "created_at": stamp(NOW),
        "day": DAY,
        "trigger": "app_open",
        "candidate": "approvals",
        "item_key": None,
        "state_hash": None,
        "evidence": "{}",
        "evidence_key": _sha256("e" + episode_id),
        "policy": "rules",
        "policy_version": "rules-1",
        "model": None,
        "answers": None,
        "terms": None,
        "kind": None,
        "work": "none",
        "level": "list",
        "timing": "now",
        "defer_until": None,
        "reason": "waiting_on_you",
        "rules_level": "list",
        "language": None,
        "latency_ms": None,
        "input_tokens": None,
        "cost_micro_usd": None,
        "mission_id": None,
        "exposures": 0,
        "first_exposed_at": None,
        "last_exposed_at": None,
        "opened_at": None,
        "acted_at": None,
        "reviewed_at": None,
        "observed_until": stamp(NOW),
        "feedback": None,
        "feedback_reason": None,
        "feedback_source": None,
        "feedback_revision": 0,
        "feedback_at": None,
        "dedupe_key": _sha256("d" + episode_id),
        **overrides,
    }


def _report_row(**overrides: Any) -> dict[str, Any]:
    """A stored report row: level/work/timing are NULL, feedback defaults to 'missed' the way
    `DecisionLoopStore.report` writes it."""
    defaults = {
        "entry": "report",
        "candidate": "unwatched",
        "work": None,
        "level": None,
        "timing": None,
        "rules_level": None,
        "reason": "owner_report",
        "feedback": "missed",
        "feedback_source": "page",
        "feedback_at": stamp(NOW),
        "feedback_revision": 1,
    }
    return _row(**{**defaults, **overrides})


def _seed(store: SessionStore, rows: list[dict[str, Any]]) -> None:
    columns = list(rows[0])
    with store._connection(write=True) as connection:
        connection.executemany(
            f"INSERT INTO decision_episodes ({','.join(columns)}) "
            f"VALUES ({','.join('?' for _ in columns)})",
            [tuple(row[column] for column in columns) for row in rows],
        )


def _seed_preferences(store: SessionStore, scope: str, values: dict[str, str]) -> None:
    with store._connection(write=True) as connection:
        connection.executemany(
            "INSERT INTO decision_preferences (scope,name,value,revision,updated_at) "
            "VALUES (?,?,?,1,?)",
            [(scope, name, value, stamp(NOW)) for name, value in values.items()],
        )


# ---- data summary and decision detail ---------------------------------------------------------


def test_data_summary_empty_scope(stats: DecisionLoopStats) -> None:
    assert stats.data_summary(SCOPE, NOW) == {
        "decisions": {
            "decisions": 0,
            "reports": 0,
            "oldest": None,
            "newest": None,
            "max": MAX_EPISODES,
            "retention_days": RETENTION_DAYS,
        },
        "suggestions": {
            "open": 0,
            "closed": 0,
            "oldest": None,
            "newest": None,
            "max_open": MAX_OPEN_ITEMS,
            "closed_days": CLOSED_KEEP_DAYS,
        },
        "calls": {"count": 0, "oldest": None, "newest": None, "max": MAX_CALLS},
        "chats": {"count": 0, "running": 0, "oldest": None, "newest": None},
        "proposals": {
            "wordings": 0,
            "changes": 0,
            "oldest": None,
            "newest": None,
            "max_wordings": MAX_PROPOSALS,
            "max_changes": MAX_CHANGES,
        },
        "preferences": {"muted": 0, "learned": False, "updated": None},
        "days": [],
        "topics": [],
    }


def test_data_summary_counts_days_topics_and_isolates_every_table(
    stats: DecisionLoopStats, store: SessionStore
) -> None:
    earlier = stamp(NOW - timedelta(days=1))
    oldest = stamp(NOW - timedelta(days=366))
    rows = [
        _row(
            level="silent",
            feedback="right_to_hold",
            feedback_source="review",
            feedback_at=stamp(NOW),
            created_at=earlier,
            day="2026-10-08",
        ),
        _row(level="list", feedback="useful", feedback_source="page", feedback_at=stamp(NOW)),
        _row(level="badge", feedback="wrong", feedback_source="page", feedback_at=stamp(NOW)),
        _row(level="nudge", feedback="later", feedback_source="page", feedback_at=stamp(NOW)),
        _row(level="alert", feedback="mute", feedback_source="page", feedback_at=stamp(NOW)),
        _row(level="silent", feedback="missed", feedback_source="review", feedback_at=stamp(NOW)),
        _report_row(),
        _row(created_at=oldest, day="2025-10-08"),
        _row(
            scope=OTHER,
            level="list",
            feedback="useful",
            feedback_source="page",
            feedback_at=stamp(NOW),
            candidate="other",
        ),
    ]
    _seed(store, rows)
    with store._connection(write=True) as connection:
        for scope in (SCOPE, OTHER):
            connection.executemany(
                "INSERT INTO decision_items (scope,item_key,candidate,state,level,timing,"
                "episode_id,body,revision,created_at,updated_at) "
                "VALUES (?,?,? ,?,'list','now',?,x'7b7d',1,?,?)",
                [
                    (
                        scope,
                        _sha256(scope + state),
                        "approvals",
                        state,
                        str(uuid.uuid4()),
                        earlier,
                        stamp(NOW),
                    )
                    for state in ("open", "done")
                ],
            )
            connection.execute(
                "INSERT INTO decision_calls (call_id,scope,created_at,item_key,outcome,body) "
                "VALUES (?,?,?,?,'answered',x'7b7d')",
                (str(uuid.uuid4()), scope, earlier, _sha256(scope)),
            )
            connection.execute(
                "INSERT INTO decision_missions (mission_id,scope,session_id,item_key,work,state,"
                "reason,body,created_at,updated_at,deadline_at) "
                "VALUES (?,?,?,?,'prepare','running','',x'7b7d',?,?,?)",
                (
                    str(uuid.uuid4()),
                    scope,
                    str(uuid.uuid4()),
                    _sha256(scope),
                    earlier,
                    stamp(NOW),
                    stamp(NOW),
                ),
            )
            for name in ("wording", CHANGES[0]):
                connection.execute(
                    "INSERT INTO decision_proposals (proposal_id,scope,name,proposed_sha256,"
                    "body,created_at,updated_at) VALUES (?,?,?,?,x'7b7d',?,?)",
                    (str(uuid.uuid4()), scope, name, _sha256(name), earlier, stamp(NOW)),
                )
        connection.execute(
            "INSERT INTO decision_missions (mission_id,scope,session_id,item_key,work,state,"
            "reason,body,created_at,updated_at,deadline_at,finished_at) "
            "VALUES (?,?,?,?,'run','done','',x'7b7d',?,?,?,?)",
            (
                str(uuid.uuid4()),
                SCOPE,
                str(uuid.uuid4()),
                _sha256("done"),
                stamp(NOW),
                stamp(NOW),
                stamp(NOW),
                stamp(NOW),
            ),
        )
    _seed_preferences(
        store,
        SCOPE,
        {
            "mute:approvals": "1",
            "mute:inbox": "0",
            "learned": "{}",
            "lambda": "1",
            "unrelated": "x",
        },
    )
    _seed_preferences(store, OTHER, {"mute:other": "1", "learned": "{}"})
    with store._connection(write=True) as connection:
        connection.execute(
            "UPDATE decision_preferences SET updated_at=? WHERE scope=? AND name='unrelated'",
            (stamp(NOW + timedelta(days=1)), SCOPE),
        )

    found = stats.data_summary(SCOPE, NOW)
    assert found["decisions"] == {
        "decisions": 7,
        "reports": 1,
        "oldest": oldest,
        "newest": stamp(NOW),
        "max": MAX_EPISODES,
        "retention_days": RETENTION_DAYS,
    }
    assert found["suggestions"] == {
        "open": 1,
        "closed": 1,
        "oldest": earlier,
        "newest": stamp(NOW),
        "max_open": MAX_OPEN_ITEMS,
        "closed_days": CLOSED_KEEP_DAYS,
    }
    assert found["calls"] == {"count": 1, "oldest": earlier, "newest": earlier, "max": MAX_CALLS}
    assert found["chats"] == {"count": 2, "running": 1, "oldest": earlier, "newest": stamp(NOW)}
    assert found["proposals"] == {
        "wordings": 1,
        "changes": 1,
        "oldest": earlier,
        "newest": earlier,
        "max_wordings": MAX_PROPOSALS,
        "max_changes": MAX_CHANGES,
    }
    assert found["preferences"] == {"muted": 1, "learned": True, "updated": stamp(NOW)}
    assert found["days"] == [
        {
            "day": "2026-10-08",
            "held": 1,
            "listed": 0,
            "attention": 0,
            "reports": 0,
            "useful": 0,
            "wrong": 0,
            "later": 0,
            "mute": 0,
            "right_to_hold": 1,
            "missed": 0,
        },
        {
            "day": DAY,
            "held": 1,
            "listed": 1,
            "attention": 3,
            "reports": 1,
            "useful": 1,
            "wrong": 1,
            "later": 1,
            "mute": 1,
            "right_to_hold": 0,
            "missed": 1,
        },
    ]
    assert found["topics"] == [
        {"topic": "approvals", "held": 2, "shown": 5, "reports": 0, "useful": 1, "wrong": 1},
        {"topic": "unwatched", "held": 0, "shown": 0, "reports": 1, "useful": 0, "wrong": 0},
    ]


def test_data_summary_day_floor_is_inclusive_but_topics_use_all_rows(
    stats: DecisionLoopStats, store: SessionStore
) -> None:
    floor = NOW.date() - timedelta(days=365)
    before = floor - timedelta(days=1)
    _seed(
        store,
        [
            _row(created_at=stamp(NOW - timedelta(days=365)), day=floor.isoformat()),
            _row(created_at=stamp(NOW - timedelta(days=366)), day=before.isoformat()),
        ],
    )
    found = stats.data_summary(SCOPE, NOW)
    assert [row["day"] for row in found["days"]] == [floor.isoformat()]
    assert found["days"][0]["listed"] == 1
    assert found["decisions"]["decisions"] == 2
    assert found["topics"][0]["shown"] == 2


@pytest.mark.parametrize(
    ("level", "candidate", "age"),
    [("list", "approvals", 0), ("silent", "tasks_due", 1)],
)
def test_data_summary_unanswered_groups_have_zero_feedback_counts(
    stats: DecisionLoopStats, store: SessionStore, level: str, candidate: str, age: int
) -> None:
    created = NOW - timedelta(days=age)
    _seed(
        store,
        [
            _row(
                level=level,
                candidate=candidate,
                created_at=stamp(created),
                day=created.date().isoformat(),
            )
        ],
    )
    found = stats.data_summary(SCOPE, NOW)
    feedback = ("useful", "wrong", "later", "mute", "right_to_hold", "missed")
    day = found["days"][0]
    topic = found["topics"][0]
    assert {key: day[key] for key in feedback} == dict.fromkeys(feedback, 0)
    assert topic["useful"] == 0
    assert topic["wrong"] == 0


def test_decision_record_uses_current_item_and_scoped_open_kept(
    stats: DecisionLoopStats, store: SessionStore
) -> None:
    episode_id = str(uuid.uuid4())
    key = _sha256("detail")
    labels = tuple(EvidenceLabel(f"label {i}") for i in range(22))
    agenda = DecisionAgenda(store)
    agenda.upsert(
        SCOPE,
        ItemWrite(
            item_key=key,
            episode_id=episode_id,
            candidate="approvals",
            kind=None,
            level="list",
            timing="now",
            defer_until=None,
            state_hash=None,
            answers_json=None,
            mission_id=None,
            body=ItemBody("Title", "What", "Current why", labels, ()),
        ),
        NOW,
    )
    row = _row(
        episode_id=episode_id,
        item_key=key,
        reason="stored reason",
        exposures=3,
        input_tokens=12,
        cost_micro_usd=123456,
        feedback="useful",
        feedback_source="page",
        feedback_at=stamp(NOW),
        feedback_reason="already_knew",
        opened_at=stamp(NOW),
    )
    report = _report_row(scope=SCOPE)
    other = _row(scope=OTHER)
    _seed(store, [row, report, other])
    assert stats.decision_record(SCOPE, episode_id, agenda=agenda) == {
        "id": episode_id,
        "at": stamp(NOW),
        "entry": "decision",
        "topic": "approvals",
        "level": "list",
        "policy": "rules",
        "why": "Current why",
        "evidence": labels[:20],
        "answer": "useful",
        "answer_reason": "already_knew",
        "answered_at": stamp(NOW),
        "shown": 3,
        "opened_at": stamp(NOW),
        "acted_at": None,
        "tokens": 12,
        "cost_usd": 0.123456,
        "kept": "open_suggestion",
    }
    without_agenda = stats.decision_record(SCOPE, episode_id)
    assert without_agenda is not None
    assert without_agenda["evidence"] == ()
    report_detail = stats.decision_record(SCOPE, report["episode_id"], agenda=agenda)
    assert report_detail is not None
    assert report_detail["entry"] == "report"
    assert report_detail["topic"] == "unwatched"
    assert report_detail["level"] is None
    assert report_detail["why"] == ""
    assert report_detail["evidence"] == ()
    assert report_detail["answer"] == "missed"
    assert report_detail["kept"] is None
    assert stats.decision_record(SCOPE, other["episode_id"], agenda=agenda) is None
    assert stats.decision_record(SCOPE, str(uuid.uuid4()), agenda=agenda) is None


# ---- page_parts: empty scope ---------------------------------------------------------------


def test_page_parts_on_an_empty_scope_is_all_zero(stats: DecisionLoopStats) -> None:
    assert stats.page_parts(SCOPE, NOW) == {
        "counts": {**_ZERO_FEEDBACK, "shown": 0},
        "recent": [],
        "held_back": [],
        "muted": frozenset(),
        "data": {
            "episodes": 0,
            "oldest": None,
            "max": MAX_EPISODES,
            "retention_days": RETENTION_DAYS,
        },
        "today": {"decisions": 0},
    }


# ---- counts ----------------------------------------------------------------------------------


def test_counts_use_current_feedback_once(stats: DecisionLoopStats, store: SessionStore) -> None:
    """One row, feedback revised four times over: still exactly one count."""
    row = _row(
        level="list",
        feedback="useful",
        feedback_source="brief",
        feedback_at=stamp(NOW),
        feedback_revision=4,
    )
    _seed(store, [row])
    counts = stats.page_parts(SCOPE, NOW)["counts"]
    assert counts == {**_ZERO_FEEDBACK, "useful": 1, "shown": 1}


def test_counts_include_a_reports_missed_feedback(
    stats: DecisionLoopStats, store: SessionStore
) -> None:
    silent_unlabeled = _row(level="silent")
    held_missed = _row(
        level="silent",
        timing="at",
        defer_until=stamp(NOW + timedelta(hours=4)),
        reason="later_by_owner",
        feedback="missed",
        feedback_source="review",
        feedback_at=stamp(NOW),
        feedback_revision=1,
    )
    shown_useful = _row(
        level="list",
        feedback="useful",
        feedback_source="brief",
        feedback_at=stamp(NOW),
        feedback_revision=1,
    )
    report = _report_row()
    _seed(store, [silent_unlabeled, held_missed, shown_useful, report])
    counts = stats.page_parts(SCOPE, NOW)["counts"]
    # shown: shown_useful (level list) + the report; the two silent rows stay out.
    assert counts == {**_ZERO_FEEDBACK, "useful": 1, "missed": 2, "shown": 2}


def test_shown_counts_decisions_at_a_shown_level_plus_every_report(
    stats: DecisionLoopStats, store: SessionStore
) -> None:
    rows = [_row(level=level) for level in ("silent", "list", "badge", "nudge", "alert")]
    report = _report_row()
    _seed(store, [*rows, report])
    assert stats.page_parts(SCOPE, NOW)["counts"]["shown"] == 5  # every level but silent, + report


def test_counts_only_cover_the_last_30_days(stats: DecisionLoopStats, store: SessionStore) -> None:
    recent_enough = _row(level="list", created_at=stamp(NOW - timedelta(days=COUNT_WINDOW_DAYS)))
    too_old = _row(
        level="list", created_at=stamp(NOW - timedelta(days=COUNT_WINDOW_DAYS, milliseconds=1))
    )
    _seed(store, [recent_enough, too_old])
    assert stats.page_parts(SCOPE, NOW)["counts"]["shown"] == 1


# ---- recent list -------------------------------------------------------------------------------


def test_recent_list_excludes_reports_and_caps_at_20(
    stats: DecisionLoopStats, store: SessionStore
) -> None:
    decisions = [
        _row(level="list", created_at=stamp(NOW - timedelta(minutes=n)))
        for n in range(MAX_RECENT + 3)
    ]
    report = _report_row(created_at=stamp(NOW))
    _seed(store, [*decisions, report])
    recent = stats.page_parts(SCOPE, NOW)["recent"]
    assert len(recent) == MAX_RECENT
    assert report["episode_id"] not in {item["episode_id"] for item in recent}
    assert recent[0] == {
        "episode_id": decisions[0]["episode_id"],
        "created_at": stamp(NOW),
        "candidate": "approvals",
        "level": "list",
        "policy": "rules",
        "reason": "waiting_on_you",
        "why": "",
        "feedback": None,
    }
    assert [item["episode_id"] for item in recent] == [
        row["episode_id"] for row in decisions[:MAX_RECENT]
    ]


# ---- data and today -----------------------------------------------------------------------


def test_data_counts_every_row_kind_and_reports_the_oldest(
    stats: DecisionLoopStats, store: SessionStore
) -> None:
    older = _row(created_at=stamp(NOW - timedelta(days=10)))
    newer_report = _report_row(created_at=stamp(NOW))
    _seed(store, [older, newer_report])
    assert stats.page_parts(SCOPE, NOW)["data"] == {
        "episodes": 2,
        "oldest": stamp(NOW - timedelta(days=10)),
        "max": MAX_EPISODES,
        "retention_days": RETENTION_DAYS,
    }


def test_today_counts_only_decisions_on_the_local_day(
    stats: DecisionLoopStats, store: SessionStore
) -> None:
    today_decision = _row(day=DAY, created_at=stamp(NOW))
    today_report = _report_row(day=DAY, created_at=stamp(NOW))
    yesterday = _row(day="2026-10-08", created_at=stamp(NOW - timedelta(days=1)))
    _seed(store, [today_decision, today_report, yesterday])
    assert stats.page_parts(SCOPE, NOW)["today"] == {"decisions": 1}


def test_today_uses_nows_own_date_not_its_utc_date(
    stats: DecisionLoopStats, store: SessionStore
) -> None:
    """JST 02:00 on the 9th is UTC 17:00 on the 8th: `today` must follow the 9th, not the 8th.

    Two rows on the 9th and one on the 8th, so a count of 2 (not 1 or 3) pins which date won.
    """
    now = datetime(2026, 10, 9, 2, 0, tzinfo=JST)
    assert now.date().isoformat() == "2026-10-09"
    assert stamp(now)[:10] == "2026-10-08"
    on_nows_date = [
        _row(day="2026-10-09", created_at=stamp(now - timedelta(minutes=n))) for n in range(2)
    ]
    on_utc_date = _row(day="2026-10-08", created_at=stamp(now - timedelta(minutes=5)))
    _seed(store, [*on_nows_date, on_utc_date])
    assert stats.page_parts(SCOPE, now)["today"] == {"decisions": 2}


# ---- muted ----------------------------------------------------------------------------------


def test_muted_lists_candidates_whose_preference_is_1(
    stats: DecisionLoopStats, store: SessionStore
) -> None:
    _seed_preferences(
        store,
        SCOPE,
        {
            "mute:inbox": "1",
            "mute:routine": "0",
            "later:tasks_due": stamp(NOW + timedelta(hours=4)),
            "spend": f"{DAY}:500",
        },
    )
    assert stats.page_parts(SCOPE, NOW)["muted"] == frozenset({"inbox"})


# ---- recent why, via the agenda --------------------------------------------------------------


def test_recent_why_is_empty_without_an_agenda_or_item_key(
    stats: DecisionLoopStats, store: SessionStore
) -> None:
    key = _sha256("approvals" + SCOPE)
    with_key = _row(item_key=key)
    without_key = _row(item_key=None)
    _seed(store, [with_key, without_key])
    assert {item["why"] for item in stats.page_parts(SCOPE, NOW)["recent"]} == {""}
    agenda = DecisionAgenda(store)  # no item ever upserted under `key`: still ""
    assert {item["why"] for item in stats.page_parts(SCOPE, NOW, agenda=agenda)["recent"]} == {""}


def test_recent_why_comes_from_the_items_current_body_via_the_agenda(
    stats: DecisionLoopStats, store: SessionStore
) -> None:
    key = _sha256("approvals" + SCOPE)
    episode_id = str(uuid.uuid4())
    body = ItemBody(
        title="Approvals waiting",
        what="3 approvals are waiting.",
        why="A deploy needs your sign-off.",
        evidence=(),
        actions=(),
    )
    write = ItemWrite(
        item_key=key,
        episode_id=episode_id,
        candidate="approvals",
        kind=None,
        level="list",
        timing="now",
        defer_until=None,
        state_hash=None,
        answers_json=None,
        mission_id=None,
        body=body,
    )
    DecisionAgenda(store).upsert(SCOPE, write, NOW)
    _seed(store, [_row(episode_id=episode_id, item_key=key)])
    recent = stats.page_parts(SCOPE, NOW, agenda=DecisionAgenda(store))["recent"]
    assert recent[0]["why"] == "A deploy needs your sign-off."


# ---- page_parts: scope isolation ---------------------------------------------------------------


def test_page_parts_is_scoped_to_one_workspace(
    stats: DecisionLoopStats, store: SessionStore
) -> None:
    start = week_start(NOW)
    mine = _row(level="silent", created_at=stamp(start - timedelta(days=1)))
    theirs = _row(scope=OTHER, level="silent", created_at=stamp(start - timedelta(days=1)))
    _seed(store, [mine, theirs])
    result = stats.page_parts(SCOPE, NOW)
    assert result["data"]["episodes"] == 1
    assert [item["episode_id"] for item in result["held_back"]] == [mine["episode_id"]]


# ---- label_of -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("feedback", "opened_at", "acted_at", "expected"),
    [
        ("useful", None, None, 1),
        ("missed", None, None, 1),
        ("wrong", None, None, 0),
        ("mute", None, None, 0),
        ("right_to_hold", None, None, 0),
        ("wrong", NOW, NOW, 0),  # an explicit verdict outranks mere engagement
        ("later", NOW, None, 1),  # Not now, but opened: engagement counts
        ("later", None, NOW, 1),  # Not now, but acted
        ("later", None, None, None),  # Not now alone: no label
        (None, NOW, None, 1),  # no reply, but opened
        (None, None, NOW, 1),  # no reply, but acted
        (None, None, None, None),  # no reply at all
    ],
)
def test_label_of_each_case(
    feedback: str | None,
    opened_at: datetime | None,
    acted_at: datetime | None,
    expected: int | None,
) -> None:
    assert label_of(feedback, opened_at, acted_at) == expected


# ---- learning_rows --------------------------------------------------------------------------


def test_learning_rows_keeps_shown_or_labelled_decisions_only(
    stats: DecisionLoopStats, store: SessionStore
) -> None:
    shown_unlabeled = _row(level="list")
    silent_labeled = _row(
        level="silent",
        feedback="right_to_hold",
        feedback_source="review",
        feedback_at=stamp(NOW),
        feedback_revision=1,
    )
    silent_unlabeled = _row(level="silent")
    report = _report_row()  # always carries feedback, but is no "decision" row
    _seed(store, [shown_unlabeled, silent_labeled, silent_unlabeled, report])
    found = {row.episode_id for row in stats.learning_rows(SCOPE, since=None)}
    assert found == {shown_unlabeled["episode_id"], silent_labeled["episode_id"]}


def test_learning_rows_filters_by_since(stats: DecisionLoopStats, store: SessionStore) -> None:
    before = _row(level="list", created_at=stamp(NOW - timedelta(days=1)))
    at_since = _row(level="list", created_at=stamp(NOW))
    after = _row(level="list", created_at=stamp(NOW + timedelta(days=1)))
    _seed(store, [before, at_since, after])
    since_now = {row.episode_id for row in stats.learning_rows(SCOPE, since=NOW)}
    assert since_now == {at_since["episode_id"], after["episode_id"]}
    assert len(stats.learning_rows(SCOPE, since=None)) == 3


def test_learning_rows_orders_newest_first_and_caps_at_limit(
    stats: DecisionLoopStats, store: SessionStore
) -> None:
    rows = [_row(level="list", created_at=stamp(NOW - timedelta(minutes=n))) for n in range(5)]
    _seed(store, rows)
    found = stats.learning_rows(SCOPE, since=None, limit=3)
    assert [row.episode_id for row in found] == [row["episode_id"] for row in rows[:3]]


def test_learning_rows_reads_p_from_terms_when_finite_in_bounds(
    stats: DecisionLoopStats, store: SessionStore
) -> None:
    good = _row(level="list", terms='{"p":0.73,"T":0.9}')
    missing = _row(level="list", terms=None)
    out_of_range = _row(level="list", terms='{"p":1.5}')
    malformed = _row(level="list", terms="not json")
    no_p = _row(level="list", terms='{"T":0.9}')
    # An int so large float() cannot hold it (OverflowError) still answers None.
    huge = _row(level="list", terms=f'{{"p":{10**400}}}')
    _seed(store, [good, missing, out_of_range, malformed, no_p, huge])
    by_id = {row.episode_id: row.p for row in stats.learning_rows(SCOPE, since=None)}
    assert by_id[good["episode_id"]] == 0.73
    assert by_id[missing["episode_id"]] is None
    assert by_id[out_of_range["episode_id"]] is None
    assert by_id[malformed["episode_id"]] is None
    assert by_id[no_p["episode_id"]] is None
    assert by_id[huge["episode_id"]] is None


def test_learning_rows_drops_a_row_whose_created_at_does_not_read_back(
    stats: DecisionLoopStats, store: SessionStore
) -> None:
    bad = _row(level="list", created_at="2026-99-99T99:99:99.999Z")  # 24 chars, unparseable
    good = _row(level="list", created_at=stamp(NOW))
    _seed(store, [bad, good])
    found = stats.learning_rows(SCOPE, since=None)
    assert [row.episode_id for row in found] == [good["episode_id"]]


def test_learning_rows_blanks_an_unreadable_stamp_without_dropping_the_row(
    stats: DecisionLoopStats, store: SessionStore
) -> None:
    row = _row(
        level="list",
        opened_at="2026-99-99T99:99:99.999Z",
        acted_at=stamp(NOW),
        first_exposed_at=None,
    )
    _seed(store, [row])
    (found,) = stats.learning_rows(SCOPE, since=None)
    assert found.opened_at is None
    assert found.acted_at == NOW
    assert found.first_exposed_at is None
    assert found.label == 1  # acted_at alone still counts as engagement


def test_learning_rows_carries_the_episodes_fields_through(
    stats: DecisionLoopStats, store: SessionStore
) -> None:
    row = _row(
        level="badge",
        timing="now",
        policy="model",
        answers='{"goal_link":0.9}',
        terms='{"p":0.4}',
    )
    _seed(store, [row])
    (found,) = stats.learning_rows(SCOPE, since=None)
    assert found.episode_id == row["episode_id"]
    assert found.created_at == NOW
    assert found.candidate == "approvals"
    assert found.level == "badge"
    assert found.timing == "now"
    assert found.policy == "model"
    assert found.answers_json == row["answers"]
    assert found.p == 0.4
    assert found.feedback is None
    assert found.label is None


def test_learning_rows_read_past_a_language_an_earlier_version_stored(
    stats: DecisionLoopStats, store: SessionStore
) -> None:
    """Earlier versions stored each decision's detected language, "en" or "ja": such a row reads
    back like one without it, so nothing that scores the rows can tell them apart."""
    same = {"policy": "model", "answers": '{"goal_link":0.9}', "terms": '{"p":0.4}'}
    _seed(store, [_row(language=language, **same) for language in ("ja", "en", None)])
    found = stats.learning_rows(SCOPE, since=None)
    assert len(found) == 3
    read = {tuple((k, v) for k, v in asdict(one).items() if k != "episode_id") for one in found}
    assert len(read) == 1
    assert "language" not in asdict(found[0])


def test_learning_rows_is_scoped_to_one_workspace(
    stats: DecisionLoopStats, store: SessionStore
) -> None:
    mine = _row(level="list")
    theirs = _row(scope=OTHER, level="list")
    _seed(store, [mine, theirs])
    found = stats.learning_rows(SCOPE, since=None)
    assert [row.episode_id for row in found] == [mine["episode_id"]]


# ---- shadow_rows -------------------------------------------------------------------------------

LOUDER = ShadowSummary(agree=False, kind_agree=True, level="nudge", p=0.8, setting="latest")


def test_wrong_reasons_count_not_useful_reasons_by_candidate_since_the_stamp(
    stats: DecisionLoopStats, store: SessionStore
) -> None:
    def wrong(candidate: str, reason: str | None, at: datetime = NOW, **more: Any) -> dict:
        return _row(
            candidate=candidate,
            feedback="wrong",
            feedback_reason=reason,
            feedback_source="page",
            feedback_at=stamp(at),
            created_at=stamp(at),
            **more,
        )

    asked = ("long_task_look", "correction_look")
    _seed(
        store,
        [
            wrong("long_task_look", "already_knew"),
            wrong("long_task_look", "already_knew", NOW + timedelta(days=1)),
            wrong("long_task_look", "bad_timing"),
            wrong("correction_look", "wrong_topic"),
            wrong("long_task_look", "already_knew", NOW - timedelta(seconds=1)),  # before since
            wrong("long_task_look", None),  # no reason given
            wrong("tasks_due", "already_knew"),  # not asked for
            wrong("long_task_look", "already_knew", scope=OTHER),
            _row(
                candidate="long_task_look",
                feedback="useful",
                feedback_source="page",
                feedback_at=stamp(NOW),
            ),
            _row(candidate="correction_look"),  # no feedback
        ],
    )
    assert stats.wrong_reasons(SCOPE, asked, NOW) == {
        "long_task_look": {"already_knew": 2, "bad_timing": 1},
        "correction_look": {"wrong_topic": 1},
    }
    assert stats.wrong_reasons(SCOPE, asked[1:], NOW) == {"correction_look": {"wrong_topic": 1}}
    assert stats.wrong_reasons(SCOPE, (), NOW) == {}


def _draft_item(
    store: SessionStore,
    label: str,
    *,
    candidate: str = "context_handoff",
    state: str = "open",
    at: datetime = NOW,
    shown: bool = True,
    scope: str = SCOPE,
    level: str = "list",
) -> str:
    """A stored item written straight to the table (the body is never read here); its key."""
    key = _sha256(label)
    with store._connection(write=True) as connection:
        connection.execute(
            "INSERT INTO decision_items (scope,item_key,candidate,state,level,timing,episode_id,"
            "body,revision,created_at,updated_at,shown_at) "
            "VALUES (?,?,?,?,?,'now',?,?,1,?,?,?)",
            (
                scope,
                key,
                candidate,
                state,
                level,
                str(uuid.uuid4()),
                b"{}",
                stamp(at),
                stamp(at),
                stamp(at) if shown else None,
            ),
        )
    return key


def _draft_episode(
    key: str, candidate: str = "context_handoff", **overrides: Any
) -> dict[str, Any]:
    return _row(candidate=candidate, item_key=key, **overrides)


def _answered(feedback: str) -> dict[str, Any]:
    return {
        "feedback": feedback,
        "feedback_source": "launcher",
        "feedback_at": stamp(NOW),
        "feedback_revision": 1,
    }


def test_draft_rows_read_used_turned_down_and_closed_unused(
    stats: DecisionLoopStats, store: SessionStore
) -> None:
    def at(minutes: int) -> datetime:
        return NOW + timedelta(minutes=minutes)

    # Oldest first here; `draft_rows` answers newest first.
    acted_before = _draft_item(store, "acted", at=at(0))
    useful = _draft_item(store, "useful", at=at(1), candidate="source_note")
    wrong_after_acted = _draft_item(store, "wrong", at=at(2))
    mute_after_acted = _draft_item(store, "mute", at=at(3), candidate="source_note")
    wrong_beside_acted = _draft_item(store, "beside", at=at(4))
    stale = _draft_item(store, "stale", at=at(5), state="stale")
    acted_state = _draft_item(store, "closed-acted", at=at(6), state="acted")
    waiting = _draft_item(store, "waiting", at=at(7))
    later = _draft_item(store, "later", at=at(8))
    _seed(
        store,
        [
            _draft_episode(acted_before, acted_at=stamp(at(0)), created_at=stamp(at(0))),
            _draft_episode(acted_before, created_at=stamp(at(9))),  # a newer episode, unanswered
            _draft_episode(useful, "source_note", **_answered("useful")),
            _draft_episode(wrong_after_acted, acted_at=stamp(NOW), **_answered("wrong")),
            _draft_episode(
                mute_after_acted, "source_note", acted_at=stamp(NOW), **_answered("mute")
            ),
            _draft_episode(wrong_beside_acted, acted_at=stamp(NOW)),
            _draft_episode(wrong_beside_acted, **_answered("wrong"), created_at=stamp(at(9))),
            _draft_episode(stale),
            _draft_episode(acted_state, acted_at=stamp(NOW)),
            _draft_episode(waiting),
            _draft_episode(later, **_answered("later")),
        ],
    )
    got = stats.draft_rows(SCOPE, NOW)
    assert got == [
        LookRow("context_handoff", "open", "", at(8)),  # later alone
        LookRow("context_handoff", "open", "", at(7)),  # open, unanswered
        LookRow("context_handoff", "acted", "accepted:other", at(6)),
        LookRow("context_handoff", "stale", "nothing", at(5)),  # closed unused
        LookRow("context_handoff", "open", "rejected:other", at(4)),  # on another episode
        LookRow("source_note", "open", "rejected:other", at(3)),  # Stop these beats acted
        LookRow("context_handoff", "open", "rejected:other", at(2)),  # Not useful beats acted
        LookRow("source_note", "open", "accepted:other", at(1)),  # Useful
        LookRow("context_handoff", "open", "accepted:other", at(0)),  # acted on an older episode
    ]


def test_draft_rows_skip_unshown_items_and_read_the_window(
    stats: DecisionLoopStats, store: SessionStore
) -> None:
    kept = _draft_item(store, "kept", at=NOW)
    _draft_item(store, "unshown", shown=False)
    _draft_item(store, "early", at=NOW - timedelta(seconds=1))
    _draft_item(store, "other-scope", scope=OTHER)
    _draft_item(store, "other-type", candidate="stalled_project")
    # An episode of another scope or type, or from before the window, answers nothing for `kept`.
    _seed(
        store,
        [
            _draft_episode(kept, **_answered("wrong"), created_at=stamp(NOW - timedelta(days=1))),
            _draft_episode(kept, scope=OTHER, **_answered("wrong")),
            _draft_episode(kept, "stalled_project", **_answered("wrong")),
        ],
    )
    assert stats.draft_rows(SCOPE, NOW) == [LookRow("context_handoff", "open", "", NOW)]
    assert stats.draft_rows(OTHER, NOW) == [LookRow("context_handoff", "open", "", NOW)]
    assert stats.draft_rows("00000000-0000-4000-8000-0000000000ff", NOW) == []

    with store._connection(write=True) as connection:
        connection.execute("DELETE FROM decision_items")
        connection.executemany(
            "INSERT INTO decision_items (scope,item_key,candidate,state,level,timing,episode_id,"
            "body,revision,created_at,updated_at,shown_at) "
            "VALUES (?,?,'source_note','open','list','now',?,?,1,?,?,?)",
            [
                (
                    SCOPE,
                    _sha256(f"capped{number}"),
                    str(uuid.uuid4()),
                    b"{}",
                    stamp(NOW + timedelta(seconds=number)),
                    stamp(NOW),
                    stamp(NOW),
                )
                for number in range(MAX_DRAFT_ROWS + 1)
            ],
        )
    capped = stats.draft_rows(SCOPE, NOW)
    assert len(capped) == MAX_DRAFT_ROWS
    assert capped[0].started == NOW + timedelta(seconds=MAX_DRAFT_ROWS)  # newest first
    assert capped[-1].started == NOW + timedelta(seconds=1)  # the oldest one is the one left out


def test_drafts_today_counts_every_scope_and_names_the_stored_keys(
    stats: DecisionLoopStats, store: SessionStore
) -> None:
    start = datetime.fromisoformat(_day(DAY)[0].replace("Z", "+00:00"))
    end = datetime.fromisoformat(_day(DAY)[1].replace("Z", "+00:00"))
    milli = timedelta(milliseconds=1)
    in_scope = _draft_item(store, "today", at=start)
    _draft_item(store, "other-scope", scope=OTHER, state="stale", at=end - milli)
    _draft_item(
        store, "acted", candidate="source_note", state="acted", at=start + timedelta(hours=1)
    )
    yesterday = _draft_item(store, "yesterday", at=start - milli)
    _draft_item(store, "tomorrow", at=end)
    _draft_item(store, "not-a-draft", candidate="stalled_project", at=start)
    foreign = _sha256("other-scope")
    count, stored = stats.drafts_today(
        SCOPE, DAY, [in_scope, yesterday, foreign, _sha256("never-stored")]
    )
    assert (count, stored) == (3, frozenset({in_scope, yesterday}))
    assert stats.drafts_today(SCOPE, DAY, []) == (3, frozenset())
    assert _code(lambda: stats.drafts_today(SCOPE, "2026-10-9", [])) == "invalid_request"


def test_drafts_today_counts_silent_drafts_too(
    stats: DecisionLoopStats, store: SessionStore
) -> None:
    """A listed draft, a silent one that was shown and a silent one never shown all count, and
    the stored keys name all three."""
    keys = [
        _draft_item(store, "listed"),
        _draft_item(store, "shown-silent", level="silent"),
        _draft_item(store, "unseen", candidate="source_note", level="silent", shown=False),
    ]
    assert stats.drafts_today(SCOPE, DAY, keys) == (3, frozenset(keys))


def test_check_rows_count_used_and_turned_down_and_leave_unanswered_out(
    stats: DecisionLoopStats, store: SessionStore
) -> None:
    """A saved check reads like a draft, but one that closed unanswered reads "" (closed = ""),
    not "nothing": a commit moving past it is no miss. Message drafts stay apart."""

    def at(minutes: int) -> datetime:
        return NOW + timedelta(minutes=minutes)

    check = "commit_checks"
    used = _draft_item(store, "used", candidate=check, at=at(0))
    turned_down = _draft_item(store, "down", candidate=check, at=at(1))
    closed = _draft_item(store, "closed", candidate=check, at=at(2), state="stale")
    waiting = _draft_item(store, "waiting", candidate=check, at=at(3))
    _draft_item(store, "draft", at=at(4))  # a message draft is not a check
    _seed(
        store,
        [
            _draft_episode(used, check, acted_at=stamp(NOW)),
            _draft_episode(turned_down, check, **_answered("mute")),
            _draft_episode(closed, check),
            _draft_episode(waiting, check),
        ],
    )
    got = stats.draft_rows(SCOPE, NOW, types=SAVED_CHECKS, closed="")
    assert got == [
        LookRow(check, "open", "", at(3)),
        LookRow(check, "stale", "", at(2)),  # closed unanswered: not "nothing"
        LookRow(check, "open", "rejected:other", at(1)),
        LookRow(check, "open", "accepted:other", at(0)),
    ]
    assert stats.draft_rows(SCOPE, NOW, types=SAVED_CHECKS)[1].reason == "nothing"
    assert [row.look for row in stats.draft_rows(SCOPE, NOW)] == ["context_handoff"]


def test_check_items_count_every_scope_and_level(
    stats: DecisionLoopStats, store: SessionStore
) -> None:
    """The day's count of checks takes every scope, state and level, apart from the message
    drafts' count; the stored keys name any item of the scope."""
    check = "commit_checks"
    silent = _draft_item(store, "silent", candidate=check, level="silent", shown=False)
    listed = _draft_item(store, "listed", candidate=check)
    _draft_item(store, "other-scope", candidate=check, scope=OTHER, state="stale")
    draft = _draft_item(store, "draft")
    assert stats.drafts_today(SCOPE, DAY, [silent, draft], types=SAVED_CHECKS) == (
        3,
        frozenset({silent, draft}),
    )
    assert stats.drafts_today(SCOPE, DAY, [listed]) == (1, frozenset({listed}))


def test_triage_rows_read_turned_down_found_nothing_used_and_neither(
    stats: DecisionLoopStats, store: SessionStore
) -> None:
    """An inbox suggestion item reads like a check, and a "done" one that was not turned down
    reads "nothing" (VIKI found no destination), ahead of used: turned down beats found
    nothing, which beats used; unanswered open and stale items read neither."""

    def at(minutes: int) -> datetime:
        return NOW + timedelta(minutes=minutes)

    triage = "inbox_triage"
    wrong_done = _draft_item(store, "wrong-done", candidate=triage, at=at(0), state="done")
    done = _draft_item(store, "done", candidate=triage, at=at(1), state="done")
    done_acted = _draft_item(store, "done-acted", candidate=triage, at=at(2), state="done")
    done_useful = _draft_item(store, "done-useful", candidate=triage, at=at(3), state="done")
    acted = _draft_item(store, "acted", candidate=triage, at=at(4))
    useful = _draft_item(store, "useful", candidate=triage, at=at(5))
    wrong = _draft_item(store, "wrong", candidate=triage, at=at(6))
    stale = _draft_item(store, "stale", candidate=triage, at=at(7), state="stale")
    waiting = _draft_item(store, "waiting", candidate=triage, at=at(8))
    _seed(
        store,
        [
            _draft_episode(wrong_done, triage, acted_at=stamp(NOW), **_answered("wrong")),
            _draft_episode(done, triage),
            _draft_episode(done_acted, triage, acted_at=stamp(NOW)),
            _draft_episode(done_useful, triage, **_answered("useful")),
            _draft_episode(acted, triage, acted_at=stamp(NOW)),
            _draft_episode(useful, triage, **_answered("useful")),
            _draft_episode(wrong, triage, **_answered("mute")),
            _draft_episode(stale, triage),
            _draft_episode(waiting, triage),
        ],
    )
    got = stats.draft_rows(SCOPE, NOW, types=INBOX_TRIAGE, closed="", done="nothing")
    assert got == [
        LookRow(triage, "open", "", at(8)),  # open, unanswered
        LookRow(triage, "stale", "", at(7)),  # closed unanswered: neither
        LookRow(triage, "open", "rejected:other", at(6)),
        LookRow(triage, "open", "accepted:other", at(5)),  # Useful
        LookRow(triage, "open", "accepted:other", at(4)),  # acted
        LookRow(triage, "done", "nothing", at(3)),  # found nothing beats Useful
        LookRow(triage, "done", "nothing", at(2)),  # found nothing beats acted
        LookRow(triage, "done", "nothing", at(1)),
        LookRow(triage, "done", "rejected:other", at(0)),  # turned down beats found nothing
    ]
    # Without `done`, a "done" item reads as any other state does.
    plain = stats.draft_rows(SCOPE, NOW, types=INBOX_TRIAGE, closed="")
    assert [row.reason for row in plain][-4:] == [
        "accepted:other",
        "accepted:other",
        "",
        "rejected:other",
    ]


def test_triage_items_count_every_scope_and_level(
    stats: DecisionLoopStats, store: SessionStore
) -> None:
    """The day's count of inbox suggestions takes every scope, state and level, apart from the
    other families; the stored keys name any item of the scope."""
    triage = "inbox_triage"
    silent = _draft_item(store, "silent", candidate=triage, level="silent", shown=False)
    done = _draft_item(store, "done", candidate=triage, state="done")
    _draft_item(store, "other-scope", candidate=triage, scope=OTHER, state="stale")
    check = _draft_item(store, "check", candidate="commit_checks")
    draft = _draft_item(store, "draft")
    assert stats.drafts_today(SCOPE, DAY, [silent, check], types=INBOX_TRIAGE) == (
        3,
        frozenset({silent, check}),
    )
    assert stats.drafts_today(SCOPE, DAY, [done], types=SAVED_CHECKS) == (1, frozenset({done}))
    assert stats.drafts_today(SCOPE, DAY, [draft]) == (1, frozenset({draft}))


def _shadowed(**overrides: Any) -> dict[str, Any]:
    """A model decision whose answers carry a shadow summary: by default one the live version
    held back silent and the user has not labelled, which the shadow would have shown."""
    answers = encode_answers({"goal_link": NoulAnswer(0.4)}, shadow=LOUDER)
    defaults = {"level": "silent", "policy": "model", "answers": answers, "terms": '{"p":0.3}'}
    return _row(**{**defaults, **overrides})


def test_shadow_rows_keep_every_decision_with_a_summary_silent_ones_included(
    stats: DecisionLoopStats, store: SessionStore
) -> None:
    """The shadow is asked about each fresh answer, shown or held back, so its comparison reads
    every decision whose answers carry a summary -- not the shown or labelled rows alone that
    `learning_rows` keeps for the fit."""
    silent = _shadowed()
    shown = _shadowed(level="badge")
    labelled = _shadowed(
        feedback="right_to_hold",
        feedback_source="review",
        feedback_at=stamp(NOW),
        feedback_revision=1,
    )
    no_summary = _shadowed(answers=encode_answers({"goal_link": NoulAnswer(0.4)}))
    by_the_rules = _row(level="silent")  # no answers at all
    theirs = _shadowed(scope=OTHER)
    _seed(store, [silent, shown, labelled, no_summary, by_the_rules, theirs])
    found = {row.episode_id: row for row in stats.shadow_rows(SCOPE, since=None)}
    assert set(found) == {silent["episode_id"], shown["episode_id"], labelled["episode_id"]}
    held = found[silent["episode_id"]]
    assert (held.level, held.label, held.p, held.answers_json) == (
        "silent",
        None,
        0.3,
        silent["answers"],
    )
    fitted = {row.episode_id for row in stats.learning_rows(SCOPE, since=None)}
    assert silent["episode_id"] not in fitted


def test_shadow_rows_filter_by_since(stats: DecisionLoopStats, store: SessionStore) -> None:
    before = _shadowed(created_at=stamp(NOW - timedelta(days=1)))
    at_since = _shadowed(created_at=stamp(NOW))
    after = _shadowed(created_at=stamp(NOW + timedelta(days=1)))
    _seed(store, [before, at_since, after])
    since_now = {row.episode_id for row in stats.shadow_rows(SCOPE, since=NOW)}
    assert since_now == {at_since["episode_id"], after["episode_id"]}
    assert len(stats.shadow_rows(SCOPE, since=None)) == 3


def test_shadow_rows_order_newest_first_and_cap_at_max_learn_rows(
    stats: DecisionLoopStats, store: SessionStore
) -> None:
    rows = [
        _shadowed(created_at=stamp(NOW - timedelta(seconds=n))) for n in range(MAX_LEARN_ROWS + 1)
    ]
    _seed(store, rows)
    newest = [row["episode_id"] for row in rows]
    assert [row.episode_id for row in stats.shadow_rows(SCOPE, since=None)] == newest[:-1]
    assert [row.episode_id for row in stats.shadow_rows(SCOPE, since=None, limit=3)] == newest[:3]


# ---- trial_rows --------------------------------------------------------------------------------

TRIED = TrialSummary(live=0.8, trial=0.3)


def _tried(**overrides: Any) -> dict[str, Any]:
    """A model decision whose answers carry a trial summary: by default one the live version
    held back silent and the user has not labelled."""
    answers = encode_answers({"goal_link": NoulAnswer(0.4)}, trial=TRIED)
    defaults = {"level": "silent", "policy": "model", "answers": answers, "terms": '{"p":0.3}'}
    return _row(**{**defaults, **overrides})


def test_trial_rows_keep_every_decision_with_a_summary_silent_ones_included(
    stats: DecisionLoopStats, store: SessionStore
) -> None:
    """The trial is asked about each fresh answer to the question it rewords, whether the live
    version showed it or held it back, so `trial_rows` must read silent decisions too -- not the
    shown or labelled rows alone that `learning_rows` keeps for the fit."""
    silent = _tried()
    shown = _tried(level="badge")
    no_summary = _tried(answers=encode_answers({"goal_link": NoulAnswer(0.4)}))
    by_the_rules = _row(level="silent")  # no answers at all
    theirs = _tried(scope=OTHER)
    _seed(store, [silent, shown, no_summary, by_the_rules, theirs])
    found = {row.episode_id: row for row in stats.trial_rows(SCOPE, since=None)}
    assert set(found) == {silent["episode_id"], shown["episode_id"]}
    assert found[silent["episode_id"]].level == "silent"


def test_trial_rows_filter_by_since_and_until(
    stats: DecisionLoopStats, store: SessionStore
) -> None:
    before = _tried(created_at=stamp(NOW - timedelta(days=1)))
    at_since = _tried(created_at=stamp(NOW))
    inside = _tried(created_at=stamp(NOW + timedelta(days=1)))
    at_until = _tried(created_at=stamp(NOW + timedelta(days=2)))
    after = _tried(created_at=stamp(NOW + timedelta(days=3)))
    _seed(store, [before, at_since, inside, at_until, after])
    bounded = {
        row.episode_id for row in stats.trial_rows(SCOPE, since=NOW, until=NOW + timedelta(days=2))
    }
    assert bounded == {at_since["episode_id"], inside["episode_id"]}
    assert len(stats.trial_rows(SCOPE, since=None)) == 5


def test_trial_rows_order_newest_first_and_cap_at_max_learn_rows(
    stats: DecisionLoopStats, store: SessionStore
) -> None:
    rows = [_tried(created_at=stamp(NOW - timedelta(seconds=n))) for n in range(MAX_LEARN_ROWS + 1)]
    _seed(store, rows)
    newest = [row["episode_id"] for row in rows]
    assert [row.episode_id for row in stats.trial_rows(SCOPE, since=None)] == newest[:-1]


# ---- owner_rows ---------------------------------------------------------------------------


def test_owner_rows_seen_excludes_silent_and_filters_by_since(
    stats: DecisionLoopStats, store: SessionStore
) -> None:
    key = _sha256("seen" + SCOPE)
    shown = _row(item_key=key, level="list", created_at=stamp(NOW))
    too_old = _row(
        item_key=_sha256("old" + SCOPE), level="list", created_at=stamp(NOW - timedelta(days=1))
    )
    silent = _row(item_key=_sha256("silent" + SCOPE), level="silent", created_at=stamp(NOW))
    _seed(store, [shown, too_old, silent])
    assert stats.owner_rows(SCOPE, since=NOW) == OwnerRows(seen=((key, "list"),))


def test_owner_rows_seen_orders_newest_first_and_caps_at_recent(
    stats: DecisionLoopStats, store: SessionStore
) -> None:
    rows_in = [
        _row(
            item_key=_sha256(f"seen{n}{SCOPE}"),
            level="list",
            created_at=stamp(NOW - timedelta(minutes=n)),
        )
        for n in range(RECENT + 2)
    ]
    _seed(store, rows_in)
    rows = stats.owner_rows(SCOPE, since=NOW - timedelta(days=1))
    assert rows.seen == tuple((row["item_key"], "list") for row in rows_in[:RECENT])


def test_owner_rows_seen_skips_rows_with_no_item_key(
    stats: DecisionLoopStats, store: SessionStore
) -> None:
    keyed = _row(item_key=_sha256("keyedseen" + SCOPE), level="list")
    keyless = _row(item_key=None, level="list")
    _seed(store, [keyed, keyless])
    rows = stats.owner_rows(SCOPE, since=NOW - timedelta(days=1))
    assert rows.seen == ((keyed["item_key"], "list"),)


def test_owner_rows_marked_keeps_mute_and_wrong_only(
    stats: DecisionLoopStats, store: SessionStore
) -> None:
    muted_key = _sha256("muted" + SCOPE)
    wrong_key = _sha256("wrong" + SCOPE)
    useful_key = _sha256("useful" + SCOPE)
    muted = _row(
        item_key=muted_key,
        level="list",
        feedback="mute",
        feedback_source="page",
        feedback_at=stamp(NOW),
        feedback_revision=1,
    )
    wrong = _row(
        item_key=wrong_key,
        level="list",
        feedback="wrong",
        feedback_source="page",
        feedback_at=stamp(NOW),
        feedback_revision=1,
    )
    useful = _row(
        item_key=useful_key,
        level="list",
        feedback="useful",
        feedback_source="page",
        feedback_at=stamp(NOW),
        feedback_revision=1,
    )
    _seed(store, [muted, wrong, useful])
    rows = stats.owner_rows(SCOPE, since=NOW - timedelta(days=30))
    assert set(rows.marked) == {muted_key, wrong_key}


def test_owner_rows_marked_orders_newest_first_and_caps_at_muted_or_wrong(
    stats: DecisionLoopStats, store: SessionStore
) -> None:
    rows_in = [
        _row(
            item_key=_sha256(f"marked{n}{SCOPE}"),
            level="list",
            feedback="wrong",
            feedback_source="page",
            feedback_at=stamp(NOW),
            feedback_revision=1,
            created_at=stamp(NOW - timedelta(minutes=n)),
        )
        for n in range(MUTED_OR_WRONG + 2)
    ]
    _seed(store, rows_in)
    rows = stats.owner_rows(SCOPE, since=NOW - timedelta(days=1))
    assert rows.marked == tuple(row["item_key"] for row in rows_in[:MUTED_OR_WRONG])


def test_owner_rows_marked_skips_rows_with_no_item_key(
    stats: DecisionLoopStats, store: SessionStore
) -> None:
    shared = {
        "level": "silent",
        "feedback": "mute",
        "feedback_source": "page",
        "feedback_at": stamp(NOW),
        "feedback_revision": 1,
    }
    keyed = _row(item_key=_sha256("keyedmarked" + SCOPE), **shared)
    keyless = _row(item_key=None, **shared)
    _seed(store, [keyed, keyless])
    rows = stats.owner_rows(SCOPE, since=NOW - timedelta(days=1))
    assert rows.marked == (keyed["item_key"],)


def test_owner_rows_feedback_keeps_any_feedback_including_silent_held_back(
    stats: DecisionLoopStats, store: SessionStore
) -> None:
    held_key = _sha256("held" + SCOPE)
    shown_key = _sha256("shownfb" + SCOPE)
    unlabeled_key = _sha256("unlabeled" + SCOPE)
    held = _row(
        item_key=held_key,
        level="silent",
        feedback="right_to_hold",
        feedback_source="review",
        feedback_at=stamp(NOW),
        feedback_revision=1,
    )
    shown = _row(
        item_key=shown_key,
        level="badge",
        feedback="useful",
        feedback_source="brief",
        feedback_at=stamp(NOW),
        feedback_revision=1,
    )
    unlabeled = _row(item_key=unlabeled_key, level="list")
    _seed(store, [held, shown, unlabeled])
    rows = stats.owner_rows(SCOPE, since=NOW - timedelta(days=30))
    assert set(rows.feedback) == {
        (held_key, "silent", "right_to_hold"),
        (shown_key, "badge", "useful"),
    }


def test_owner_rows_feedback_orders_newest_first_and_caps_at_recent_feedback(
    stats: DecisionLoopStats, store: SessionStore
) -> None:
    rows_in = [
        _row(
            item_key=_sha256(f"feedback{n}{SCOPE}"),
            level="badge",
            feedback="useful",
            feedback_source="page",
            feedback_at=stamp(NOW),
            feedback_revision=1,
            created_at=stamp(NOW - timedelta(minutes=n)),
        )
        for n in range(RECENT_FEEDBACK + 2)
    ]
    _seed(store, rows_in)
    rows = stats.owner_rows(SCOPE, since=NOW - timedelta(days=1))
    assert rows.feedback == tuple(
        (row["item_key"], "badge", "useful") for row in rows_in[:RECENT_FEEDBACK]
    )


def test_owner_rows_feedback_skips_rows_with_no_item_key(
    stats: DecisionLoopStats, store: SessionStore
) -> None:
    shared = {
        "level": "silent",
        "feedback": "useful",
        "feedback_source": "page",
        "feedback_at": stamp(NOW),
        "feedback_revision": 1,
    }
    keyed = _row(item_key=_sha256("keyedfeedback" + SCOPE), **shared)
    keyless = _row(item_key=None, **shared)
    _seed(store, [keyed, keyless])
    rows = stats.owner_rows(SCOPE, since=NOW - timedelta(days=1))
    assert rows.feedback == ((keyed["item_key"], "silent", "useful"),)


def test_owner_rows_is_scoped_to_one_workspace(
    stats: DecisionLoopStats, store: SessionStore
) -> None:
    """A row of each kind in the other workspace too, so a broken scope filter on the
    `marked` or `feedback` read (not just `seen`) would also be caught here."""
    shared_feedback = {
        "feedback": "wrong",
        "feedback_source": "page",
        "feedback_at": stamp(NOW),
        "feedback_revision": 1,
    }
    mine_seen = _row(item_key=_sha256("mineseen" + SCOPE), level="list")
    theirs_seen = _row(item_key=_sha256("theirsseen" + OTHER), scope=OTHER, level="list")
    mine_marked = _row(item_key=_sha256("minemarked" + SCOPE), level="silent", **shared_feedback)
    theirs_marked = _row(
        item_key=_sha256("theirsmarked" + OTHER), scope=OTHER, level="silent", **shared_feedback
    )
    _seed(store, [mine_seen, theirs_seen, mine_marked, theirs_marked])
    rows = stats.owner_rows(SCOPE, since=NOW - timedelta(days=1))
    assert [item_key for item_key, _level in rows.seen] == [mine_seen["item_key"]]
    assert rows.marked == (mine_marked["item_key"],)
    assert [item_key for item_key, _level, _feedback in rows.feedback] == [mine_marked["item_key"]]


# ---- week_start --------------------------------------------------------------------------------


def test_week_start_pins_to_the_owners_local_monday_midnight(stats: DecisionLoopStats) -> None:
    """Pin the exact instant, not just the weekday, so a change that moves the edge off
    *now*'s own zone (e.g. converting to UTC first) cannot pass by accident."""
    assert week_start(NOW) == datetime(2026, 10, 5, tzinfo=JST)


def test_week_start_at_monday_midnight_and_sunday_end_of_day(stats: DecisionLoopStats) -> None:
    monday_midnight = datetime(2026, 10, 5, 0, 0, 0, 0, tzinfo=JST)
    sunday_end = datetime(2026, 10, 11, 23, 59, 59, 999999, tzinfo=JST)
    assert week_start(monday_midnight) == datetime(2026, 10, 5, tzinfo=JST)
    assert week_start(sunday_end) == datetime(2026, 10, 5, tzinfo=JST)


def test_week_start_with_a_system_matching_fixed_offset_follows_the_machines_own_rule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fixed `datetime.timezone` offset that equals the system's current local offset is
    `_local`'s "system local" (`self._now().astimezone()` in service.py): `week_start` must then
    go through the system's own rule -- a naive local midnight's `.astimezone()` -- instead of
    treating that offset as fixed for every date. Whatever this machine's own rule actually is
    (Windows, Linux or macOS) is beside the point; this only pins that the branch defers to it,
    rather than to the given offset, by comparing against the identical call."""
    monkeypatch.setattr(stats_module, "_current_offset", lambda: timedelta(hours=3))
    now = datetime(2026, 10, 9, 9, 30, tzinfo=timezone(timedelta(hours=3)))
    naive_monday_midnight = datetime(2026, 10, 5, 0, 0)
    assert week_start(now) == naive_monday_midnight.astimezone()


# ---- audit ------------------------------------------------------------------------------------


def test_audit_keeps_silent_now_decisions_from_the_week_before_only(
    stats: DecisionLoopStats, store: SessionStore
) -> None:
    start = week_start(NOW)
    in_window = _row(level="silent", created_at=stamp(start - timedelta(days=1)))
    at_upper_edge = _row(level="silent", created_at=stamp(start))  # excluded: half-open above
    at_lower_edge = _row(level="silent", created_at=stamp(start - timedelta(days=7)))  # included
    just_before_lower = _row(
        level="silent", created_at=stamp(start - timedelta(days=7, milliseconds=1))
    )
    not_silent = _row(level="list", created_at=stamp(start - timedelta(days=1)))
    not_now = _row(
        level="silent",
        timing="at",
        defer_until=stamp(start + timedelta(hours=4)),
        created_at=stamp(start - timedelta(days=1)),
    )
    muted = _row(
        level="silent", reason="rules:muted_by_owner", created_at=stamp(start - timedelta(days=1))
    )
    _seed(
        store,
        [
            in_window,
            at_upper_edge,
            at_lower_edge,
            just_before_lower,
            not_silent,
            not_now,
            muted,
        ],
    )
    found = {row["episode_id"] for row in stats.audit(SCOPE, NOW)}
    assert found == {in_window["episode_id"], at_lower_edge["episode_id"]}


def test_audit_rows_carry_why_from_the_agenda(
    stats: DecisionLoopStats, store: SessionStore
) -> None:
    start = week_start(NOW)
    key = _sha256("approvals" + SCOPE)
    episode_id = str(uuid.uuid4())
    body = ItemBody(
        title="Approvals waiting",
        what="3 approvals are waiting.",
        why="Approvals need you.",
        evidence=(),
        actions=(),
    )
    write = ItemWrite(
        item_key=key,
        episode_id=episode_id,
        candidate="approvals",
        kind=None,
        level="silent",
        timing="now",
        defer_until=None,
        state_hash=None,
        answers_json=None,
        mission_id=None,
        body=body,
    )
    DecisionAgenda(store).upsert(SCOPE, write, NOW)
    row = _row(
        episode_id=episode_id,
        item_key=key,
        level="silent",
        created_at=stamp(start - timedelta(days=1)),
    )
    _seed(store, [row])
    found = stats.audit(SCOPE, NOW, agenda=DecisionAgenda(store))
    assert found == [
        {
            "episode_id": episode_id,
            "created_at": stamp(start - timedelta(days=1)),
            "candidate": "approvals",
            "level": "silent",
            "reason": "waiting_on_you",
            "feedback": None,
            "why": "Approvals need you.",
        }
    ]
    assert stats.audit(SCOPE, NOW)[0]["why"] == ""  # no agenda given: ""


def test_audit_is_a_stable_weekly_sample(stats: DecisionLoopStats, store: SessionStore) -> None:
    start = week_start(NOW)
    last_week = [
        _row(level="silent", created_at=stamp(start - timedelta(days=1, hours=n)))
        for n in range(AUDIT_SIZE)
    ]
    this_week = [
        _row(level="silent", created_at=stamp(start + timedelta(days=1, hours=n)))
        for n in range(AUDIT_SIZE)
    ]
    _seed(store, [*last_week, *this_week])

    first = stats.audit(SCOPE, NOW)
    again = stats.audit(SCOPE, NOW + timedelta(days=2))  # later in the same week: unchanged
    next_week = stats.audit(SCOPE, NOW + timedelta(days=7))  # a week on: a new sample

    assert [row["episode_id"] for row in first] == [row["episode_id"] for row in again]
    assert {row["episode_id"] for row in first} == {row["episode_id"] for row in last_week}
    assert {row["episode_id"] for row in next_week} == {row["episode_id"] for row in this_week}
    assert [row["episode_id"] for row in first] != [row["episode_id"] for row in next_week]


def _fixed_london(naive: datetime) -> datetime:
    """What production's `_system_local` gives on a London machine: `naive.astimezone()`
    resolves to a FIXED `datetime.timezone` carrying London's own offset for *naive*'s date,
    DST included -- never a `ZoneInfo`. Subtracting days from a `ZoneInfo`-tagged datetime
    re-resolves its offset for the shifted date, so it would quietly repair a flat
    "- timedelta(days=7)" edge instead of exposing it; only a fixed offset, production's own
    shape, keeps that bug visible."""
    aware = naive.replace(tzinfo=LONDON)
    offset = aware.utcoffset()
    assert offset is not None
    return aware.astimezone(timezone(offset))


def test_audit_and_review_hold_steady_across_a_dst_change(
    stats: DecisionLoopStats, store: SessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Production's *now* is always a FIXED offset (`_local`'s `.astimezone()`), never a
    zone -- that is the path that actually broke. Drive it through the system-local branch
    (`_current_offset`) with Europe/London's own 2026-10-25 change standing in for this
    machine's rule (`_system_local`, stood in by `_fixed_london` so it answers in production's
    own fixed-offset shape), deterministically, regardless of this machine's real configured
    zone: week of Mon 2026-10-19, clocks go back on Sun 2026-10-25 at 01:00 UTC (BST +01:00 ->
    GMT +00:00). A silent "now" Skip from local Mon 2026-10-12 00:30 (the prior week, still
    BST) must stay the sample's only row, and the launcher's due-ness must stay the same,
    whether checked before or after the change -- the edges come from each Monday's own local
    midnight, not from *now*'s current offset and a flat "- 7 days". The week after the change
    pins the opposite edge: a silent "now" decision from local Mon 2026-10-19 00:30 BST sits on
    the sample's lower edge, still BST, even though that edge's own week and *now* are already
    GMT -- a flat "- 7 days" from *now*'s GMT midnight would miss it by an hour."""
    monkeypatch.setattr(stats_module, "_system_local", _fixed_london)
    bst, gmt = timezone(timedelta(hours=1)), timezone(timedelta(hours=0))
    skip_row = _row(level="silent", created_at=stamp(datetime(2026, 10, 12, 0, 30, tzinfo=bst)))
    _seed(store, [skip_row])

    monkeypatch.setattr(stats_module, "_current_offset", lambda: timedelta(hours=1))
    on_wednesday = datetime(2026, 10, 21, 12, 0, tzinfo=bst)
    on_saturday = datetime(2026, 10, 24, 12, 0, tzinfo=bst)
    for now in (on_wednesday, on_saturday):
        found = stats.audit(SCOPE, now)
        assert [row["episode_id"] for row in found] == [skip_row["episode_id"]]
        assert stats.review(SCOPE, now) == (True, 1)

    monkeypatch.setattr(stats_module, "_current_offset", lambda: timedelta(hours=0))
    on_sunday = datetime(2026, 10, 25, 12, 0, tzinfo=gmt)  # clocks just went back
    found = stats.audit(SCOPE, on_sunday)
    assert [row["episode_id"] for row in found] == [skip_row["episode_id"]]
    assert stats.review(SCOPE, on_sunday) == (True, 1)

    edge_row = _row(level="silent", created_at=stamp(datetime(2026, 10, 19, 0, 30, tzinfo=bst)))
    _seed(store, [edge_row])
    week_after = datetime(2026, 10, 28, 12, 0, tzinfo=gmt)  # Wed, the week after the change
    found = stats.audit(SCOPE, week_after)
    assert [row["episode_id"] for row in found] == [edge_row["episode_id"]]
    assert stats.review(SCOPE, week_after) == (True, 1)


def test_audit_caps_at_audit_size(stats: DecisionLoopStats, store: SessionStore) -> None:
    start = week_start(NOW)
    rows = [
        _row(level="silent", created_at=stamp(start - timedelta(hours=n + 1)))
        for n in range(AUDIT_SIZE + 5)
    ]
    _seed(store, rows)
    found = stats.audit(SCOPE, NOW)
    assert len(found) == AUDIT_SIZE
    expected_order = sorted(
        (row["episode_id"] for row in rows),
        key=lambda episode_id: sha256_hex(f"{SCOPE}|{start.date()}|{episode_id}"),
    )[:AUDIT_SIZE]
    assert [row["episode_id"] for row in found] == expected_order


def test_audit_is_scoped_to_one_workspace(stats: DecisionLoopStats, store: SessionStore) -> None:
    start = week_start(NOW)
    mine = _row(level="silent", created_at=stamp(start - timedelta(days=1)))
    theirs = _row(scope=OTHER, level="silent", created_at=stamp(start - timedelta(days=1)))
    _seed(store, [mine, theirs])
    assert {row["episode_id"] for row in stats.audit(SCOPE, NOW)} == {mine["episode_id"]}
    assert {row["episode_id"] for row in stats.audit(OTHER, NOW)} == {theirs["episode_id"]}


# ---- review -----------------------------------------------------------------------------------


def test_review_is_due_until_labelled_or_skipped(
    stats: DecisionLoopStats, store: SessionStore
) -> None:
    start = week_start(NOW)
    row = _row(level="silent", created_at=stamp(start - timedelta(days=1)))
    _seed(store, [row])

    assert stats.review(SCOPE, NOW) == (True, 1)  # unlabelled, never seen: due

    with store._connection(write=True) as connection:
        connection.execute(
            "UPDATE decision_episodes SET feedback='right_to_hold',feedback_source='review',"
            "feedback_at=?,feedback_revision=1 WHERE episode_id=?",
            (stamp(NOW), row["episode_id"]),
        )
    assert stats.review(SCOPE, NOW) == (False, 0)  # labelled: nothing left to check

    with store._connection(write=True) as connection:
        connection.execute(
            "UPDATE decision_episodes SET feedback=NULL,feedback_source=NULL,feedback_at=NULL,"
            "feedback_revision=0 WHERE episode_id=?",
            (row["episode_id"],),
        )
    assert stats.review(SCOPE, NOW) == (True, 1)  # unlabelled again: due again

    _seed_preferences(store, SCOPE, {"review_seen": stamp(NOW)})
    assert stats.review(SCOPE, NOW) == (False, 1)  # skipped this week: not due, still counts

    next_week = NOW + timedelta(days=7)
    assert stats.review(SCOPE, next_week) == (False, 0)  # a new week starts with an empty sample


def test_review_is_due_again_once_review_seen_predates_this_week(
    stats: DecisionLoopStats, store: SessionStore
) -> None:
    start = week_start(NOW)
    row = _row(level="silent", created_at=stamp(start - timedelta(days=1)))
    _seed(store, [row])
    _seed_preferences(store, SCOPE, {"review_seen": stamp(start - timedelta(days=1))})
    assert stats.review(SCOPE, NOW) == (True, 1)


def test_review_is_scoped_to_one_workspace(stats: DecisionLoopStats, store: SessionStore) -> None:
    start = week_start(NOW)
    mine = _row(level="silent", created_at=stamp(start - timedelta(days=1)))
    theirs = _row(scope=OTHER, level="silent", created_at=stamp(start - timedelta(days=1)))
    _seed(store, [mine, theirs])
    assert stats.review(SCOPE, NOW) == (True, 1)
    assert stats.review(OTHER, NOW) == (True, 1)
    assert stats.review("00000000-0000-4000-8000-0000000000ff", NOW) == (False, 0)


# ---- protection -------------------------------------------------------------------------------


def test_learning_methods_require_a_private_store(
    stats: DecisionLoopStats, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refused(path: Path) -> None:
        raise protection.ProtectionError()

    monkeypatch.setattr(protection, "require_private_directory", refused)
    assert _code(lambda: stats.page_parts(SCOPE, NOW)) == "unavailable"
    assert _code(lambda: stats.learning_rows(SCOPE, since=None)) == "unavailable"
    assert _code(lambda: stats.shadow_rows(SCOPE, since=None)) == "unavailable"
    assert _code(lambda: stats.trial_rows(SCOPE, since=None)) == "unavailable"
    assert _code(lambda: stats.wrong_reasons(SCOPE, ("long_task_look",), NOW)) == "unavailable"
    assert _code(lambda: stats.draft_rows(SCOPE, NOW)) == "unavailable"
    assert _code(lambda: stats.drafts_today(SCOPE, DAY, [])) == "unavailable"
    assert _code(lambda: stats.owner_rows(SCOPE, since=NOW)) == "unavailable"
    assert _code(lambda: stats.audit(SCOPE, NOW)) == "unavailable"
    assert _code(lambda: stats.review(SCOPE, NOW)) == "unavailable"
