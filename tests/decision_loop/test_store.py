"""Tests for the Decision loop's episode store: record with per-day dedupe and pruning, observe,
report, feedback, preferences, spend and usage, the learned snapshot and its restart stamp, the
showings log, each item's showings, the arbiter's JSON values, delete, and its refusals.

A port of the earlier engine's store tests without their study, mode and block cases.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import sqlite3
import uuid
from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from viki_agent import protection, storage
from viki_agent.automation.routines import stamp
from viki_agent.decision_loop import store as dl_store
from viki_agent.decision_loop.contracts import (
    FEEDBACK_HELD,
    FEEDBACK_SHOWN,
    MAX_EPISODES,
    MAX_OPEN_ITEMS,
    MODEL_IDS,
    RETENTION_DAYS,
    SOURCES,
    TOPICS,
    TRIGGERS,
    DecisionLoopError,
    canonical_json,
    item_key,
    sha256_hex,
)
from viki_agent.decision_loop.rules import POLICY_VERSION, evidence_key
from viki_agent.decision_loop.store import DecisionLoopStore, EpisodeInput, Recorded, Usage
from viki_agent.state import AppState, Prefs
from viki_agent.storage import SessionStore, StateError

JST = timezone(timedelta(hours=9))
NOW = datetime(2026, 10, 9, 9, 30, tzinfo=JST)
DAY = "2026-10-09"
LATER = NOW + timedelta(hours=4)
SCOPE = "00000000-0000-4000-8000-0000000000a1"
OTHER = "00000000-0000-4000-8000-0000000000b2"
THIRD = "00000000-0000-4000-8000-0000000000d4"
MISSION = "00000000-0000-4000-8000-0000000000f1"
STATE = sha256_hex("the state the model saw")
CEILING = 1_000_000
TOKENS_CEILING = 10**12
APPROVALS_KEY = item_key("approvals", SCOPE)

LIST = EpisodeInput(
    candidate="approvals",
    evidence={"native": 2, "proposals": None},
    policy="rules",
    policy_version=POLICY_VERSION,
    level="list",
    timing="now",
    work="none",
    reason="waiting_on_you",
    item_key=APPROVALS_KEY,
    rules_level="list",
)
SILENT = EpisodeInput(
    candidate="tasks_due",
    evidence={"overdue": 0, "today": 0, "soon": 2, "files": 3, "files_key": "c" * 64},
    policy="rules",
    policy_version=POLICY_VERSION,
    level="silent",
    timing="now",
    work="none",
    reason="due_soon",
    item_key=item_key("tasks_due", SCOPE),
    rules_level="silent",
)
DEFER = EpisodeInput(
    candidate="inbox",
    evidence={"count": 5, "oldest": datetime(2026, 10, 1, 8, 0, tzinfo=JST)},
    policy="rules",
    policy_version=POLICY_VERSION,
    level="silent",
    timing="at",
    work="none",
    reason="later_by_owner",
    item_key=item_key("inbox", SCOPE),
    defer_until=LATER,
    rules_level="silent",
)
NUDGE = EpisodeInput(  # a model decision with every optional column set
    candidate="agent_runs",
    evidence={"working": 0, "review": 1},
    policy="model",
    policy_version="playbook-1",
    level="nudge",
    timing="after_focus",
    work="prepare",
    reason="score",
    item_key=item_key("agent_runs", SCOPE),
    state_hash=STATE,
    model=MODEL_IDS["pinned"],
    answers_json='{"goal_link":0.8,"importance":[0.1,0.9],"kind":{"ask":0.25,"inform":0.75}}',
    terms={"p": 0.7, "alpha": 1.0},
    kind="ask",
    rules_level="list",
    latency_ms=420,
    input_tokens=1800,
    cost_micro_usd=76,
    mission_id=MISSION,
)


@pytest.fixture
def store(tmp_path: Path) -> SessionStore:
    return AppState(tmp_path / "state.json", Prefs()).store


@pytest.fixture
def episodes(store: SessionStore) -> DecisionLoopStore:
    return DecisionLoopStore(store)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _record(episodes: DecisionLoopStore, entries: list[Any], **overrides: Any) -> list[Recorded]:
    arguments: dict[str, Any] = {
        "scope": SCOPE,
        "trigger": "change",
        "now": NOW,
        "day": DAY,
        **overrides,
    }
    return episodes.record(arguments.pop("scope"), entries, **arguments)


def _report(episodes: DecisionLoopStore, **overrides: Any) -> str:
    arguments: dict[str, Any] = {
        "scope": SCOPE,
        "topic": "unwatched",
        "source": "page",
        "trigger": "review",
        "now": NOW,
        "day": DAY,
        **overrides,
    }
    return episodes.report(arguments.pop("scope"), **arguments)


def _observe(episodes: DecisionLoopStore, ids: list[Any], **overrides: Any) -> int:
    arguments: dict[str, Any] = {"scope": SCOPE, "event": "shown", "now": NOW, **overrides}
    return episodes.observe(arguments.pop("scope"), episode_ids=ids, **arguments)


_KNOWN = "00000000-0000-4000-8000-0000000000c3"


def _feedback(episodes: DecisionLoopStore, **overrides: Any) -> Recorded:
    arguments: dict[str, Any] = {
        "scope": SCOPE,
        "episode_id": _KNOWN,
        "value": "useful",
        "reason": None,
        "source": "launcher",
        "now": NOW,
        **overrides,
    }
    return episodes.feedback(arguments.pop("scope"), **arguments)[0]


def _rows(store: SessionStore) -> dict[str, dict[str, Any]]:
    with store._connection() as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute("SELECT * FROM decision_episodes").fetchall()
    return {row["episode_id"]: dict(row) for row in rows}


def _execute(store: SessionStore, sql: str, values: tuple[Any, ...] = ()) -> None:
    with store._connection(write=True) as connection:
        connection.execute(sql, values)


def _row(**overrides: Any) -> dict[str, Any]:
    """A stored list-level decision row, written straight to the table."""
    episode_id = overrides.get("episode_id", str(uuid.uuid4()))
    return {
        "episode_id": episode_id,
        "scope": SCOPE,
        "entry": "decision",
        "created_at": stamp(NOW),
        "day": DAY,
        "trigger": "change",
        "candidate": "approvals",
        "item_key": None,
        "state_hash": None,
        "evidence": "{}",
        "evidence_key": _sha256("{}"),
        "policy": "rules",
        "policy_version": POLICY_VERSION,
        "model": None,
        "answers": None,
        "terms": None,
        "kind": None,
        "work": "none",
        "level": "list",
        "timing": "now",
        "defer_until": None,
        "reason": "waiting_on_you",
        "rules_level": None,
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
        "dedupe_key": _sha256(episode_id),
        **overrides,
    }


def _report_row(**overrides: Any) -> dict[str, Any]:
    """A stored report row, written straight to the table."""
    return _row(
        **{
            "entry": "report",
            "candidate": "unwatched",
            "work": None,
            "level": None,
            "timing": None,
            "reason": "owner_report",
            "feedback": "missed",
            "feedback_source": "page",
            "feedback_revision": 1,
            "feedback_at": stamp(NOW),
            **overrides,
        }
    )


def _seed(store: SessionStore, rows: list[dict[str, Any]]) -> None:
    columns = list(rows[0])
    with store._connection(write=True) as connection:
        connection.executemany(
            f"INSERT INTO decision_episodes ({','.join(columns)}) "
            f"VALUES ({','.join('?' for _ in columns)})",
            [tuple(row[column] for column in columns) for row in rows],
        )


def _code(call: Callable[[], object]) -> str:
    with pytest.raises(DecisionLoopError) as failure:
        call()
    return failure.value.code


def _preference(store: SessionStore, scope: str, name: str) -> tuple[Any, ...] | None:
    with store._connection() as connection:
        row = connection.execute(
            "SELECT value, revision FROM decision_preferences WHERE scope=? AND name=?",
            (scope, name),
        ).fetchone()
    return None if row is None else tuple(row)


def _preferences(store: SessionStore) -> list[tuple[Any, ...]]:
    with store._connection() as connection:
        return connection.execute(
            "SELECT scope, name, value, revision FROM decision_preferences ORDER BY scope, name"
        ).fetchall()


def _seed_preferences(
    store: SessionStore, pairs: list[tuple[str, str]], scope: str = SCOPE, revision: int = 1
) -> None:
    with store._connection(write=True) as connection:
        connection.executemany(
            "INSERT INTO decision_preferences VALUES (?,?,?,?,?)",
            [(scope, name, value, revision, stamp(NOW)) for name, value in pairs],
        )


# ---- record ------------------------------------------------------------------------------------


def _data_rows(store: SessionStore) -> dict[str, str]:
    rows = [
        _row(
            episode_id=f"00000000-0000-4000-8000-{number:012x}",
            created_at=stamp(NOW + timedelta(milliseconds=number)),
            candidate=topic,
            level=level,
            feedback=answer,
            feedback_at=stamp(NOW) if answer else None,
            feedback_source="page" if answer else None,
        )
        for number, topic, level, answer in (
            (1, "approvals", "silent", None),
            (2, "approvals", "list", "useful"),
            (3, "inbox", "badge", "wrong"),
            (4, "inbox", "nudge", None),
            (5, "agent_runs", "alert", "later"),
        )
    ]
    rows.append(
        _report_row(
            episode_id="00000000-0000-4000-8000-000000000006",
            created_at=stamp(NOW + timedelta(milliseconds=6)),
        )
    )
    rows.append(
        _row(
            episode_id="00000000-0000-4000-8000-000000000007",
            scope=OTHER,
            created_at=stamp(NOW + timedelta(milliseconds=7)),
        )
    )
    _seed(store, rows)
    return {str(number): rows[number - 1]["episode_id"] for number in range(1, 8)}


@pytest.mark.parametrize(
    ("filters", "expected"),
    [
        ({"topic": "inbox"}, {"3", "4"}),
        ({"shown": "held"}, {"1"}),
        ({"shown": "listed"}, {"2"}),
        ({"shown": "attention"}, {"3", "4", "5"}),
        ({"shown": "report"}, {"6"}),
        ({"answer": "none"}, {"1", "4"}),
        ({"answer": "useful"}, {"2"}),
        ({"since": NOW + timedelta(milliseconds=3)}, {"3", "4", "5", "6"}),
        ({"until": NOW + timedelta(milliseconds=4)}, {"1", "2", "3"}),
        ({"topic": "inbox", "shown": "attention", "answer": "none"}, {"4"}),
    ],
)
def test_decision_records_filters(
    episodes: DecisionLoopStore, store: SessionStore, filters: dict[str, Any], expected: set[str]
) -> None:
    ids = _data_rows(store)
    result = episodes.decision_records(SCOPE, limit=50, **filters)
    assert result["total"] == len(expected)
    assert result["deletable"] == len(expected)
    assert result["next"] is None
    assert {row["id"] for row in result["rows"]} == {ids[number] for number in expected}
    assert all(
        set(row) == {"id", "at", "entry", "topic", "level", "policy", "answer", "kept"}
        and row["kept"] is None
        for row in result["rows"]
    )


def test_decision_records_page_and_open_suggestion(
    episodes: DecisionLoopStore, store: SessionStore
) -> None:
    ids = _data_rows(store)
    with store._connection(write=True) as connection:
        connection.execute(
            "INSERT INTO decision_items (scope,item_key,candidate,state,level,timing,episode_id,"
            "body,revision,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                SCOPE,
                "a" * 64,
                "inbox",
                "open",
                "list",
                "now",
                ids["4"],
                b"{}",
                1,
                stamp(NOW),
                stamp(NOW),
            ),
        )
        connection.execute(
            "INSERT INTO decision_items (scope,item_key,candidate,state,level,timing,episode_id,"
            "body,revision,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                OTHER,
                "b" * 64,
                "approvals",
                "open",
                "list",
                "now",
                ids["2"],
                b"{}",
                1,
                stamp(NOW),
                stamp(NOW),
            ),
        )
    result = episodes.decision_records(SCOPE, limit=50)
    assert (result["total"], result["deletable"]) == (6, 5)
    assert [row["id"] for row in result["rows"]] == [ids[str(n)] for n in range(6, 0, -1)]
    assert next(row for row in result["rows"] if row["id"] == ids["4"])["kept"] == "open_suggestion"

    many = [
        _row(
            episode_id=f"10000000-0000-4000-8000-{n:012x}",
            created_at=stamp(NOW + timedelta(seconds=1, milliseconds=n)),
        )
        for n in range(45)
    ]
    _seed(store, many)
    first = episodes.decision_records(SCOPE, limit=50)
    assert (first["total"], first["deletable"], len(first["rows"])) == (51, 50, 50)
    assert first["next"] == {"at": stamp(NOW + timedelta(milliseconds=2)), "id": ids["2"]}
    second = episodes.decision_records(
        SCOPE, limit=50, before=(first["next"]["at"], first["next"]["id"])
    )
    assert (second["total"], second["deletable"], len(second["rows"])) == (51, 50, 1)
    assert second["next"] is None
    assert set(row["id"] for row in first["rows"] + second["rows"]) == {
        *[row["episode_id"] for row in many],
        *[ids[str(n)] for n in range(1, 7)],
    }


def test_delete_decisions_keeps_open_and_other_data(
    episodes: DecisionLoopStore, store: SessionStore
) -> None:
    ids = _data_rows(store)
    with store._connection(write=True) as connection:
        connection.execute(
            "INSERT INTO decision_items (scope,item_key,candidate,state,level,timing,episode_id,"
            "body,revision,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                SCOPE,
                "a" * 64,
                "inbox",
                "open",
                "list",
                "now",
                ids["4"],
                b"{}",
                1,
                stamp(NOW),
                stamp(NOW),
            ),
        )
        connection.execute(
            "INSERT INTO decision_calls (call_id,scope,created_at,item_key,outcome,body) "
            "VALUES (?,?,?,?,?,?)",
            (MISSION, SCOPE, stamp(NOW), "a" * 64, "answered", b"{}"),
        )
        connection.execute(
            "INSERT INTO decision_missions (mission_id,scope,session_id,item_key,work,state,"
            "reason,body,created_at,updated_at,deadline_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                MISSION,
                SCOPE,
                OTHER,
                "a" * 64,
                "run",
                "running",
                "",
                b"{}",
                stamp(NOW),
                stamp(NOW),
                stamp(LATER),
            ),
        )
    _seed_preferences(store, [("learned", "{}")])
    with store._connection() as connection:
        untouched = {
            table: connection.execute(f"SELECT * FROM {table}").fetchall()
            for table in (
                "decision_items",
                "decision_calls",
                "decision_missions",
                "decision_preferences",
            )
        }
    assert episodes.delete_decisions(SCOPE, ids=[ids["3"], ids["4"], str(uuid.uuid4())]) == (1, 1)
    assert ids["3"] not in _rows(store) and ids["4"] in _rows(store)
    assert episodes.delete_decisions(
        SCOPE, topic="inbox", until=NOW + timedelta(milliseconds=5)
    ) == (0, 1)
    assert episodes.delete_decisions(SCOPE, since=NOW, until=NOW + timedelta(milliseconds=5)) == (
        2,
        1,
    )
    assert ids["5"] in _rows(store) and ids["7"] in _rows(store)
    with store._connection() as connection:
        for table, rows in untouched.items():
            assert connection.execute(f"SELECT * FROM {table}").fetchall() == rows


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"topic": "inbox"},
        {"until": NOW},
        {"ids": []},
        {"ids": [MISSION, MISSION]},
        {"ids": ["bad"]},
        {"ids": [MISSION], "topic": "inbox"},
        {"topic": "bad", "until": NOW},
        {"shown": "bad", "until": NOW},
        {"answer": "bad", "until": NOW},
        {"since": NOW, "until": NOW},
        {"since": NOW.replace(tzinfo=None), "until": LATER},
    ],
)
def test_delete_decisions_refuses_invalid_requests(
    episodes: DecisionLoopStore, store: SessionStore, kwargs: dict[str, Any]
) -> None:
    ids = _data_rows(store)
    before = _rows(store)
    assert _code(lambda: episodes.delete_decisions(SCOPE, **kwargs)) == "invalid_request"
    assert _rows(store) == before and ids["7"] in before


@pytest.mark.parametrize(
    "kwargs",
    [
        {"topic": "bad"},
        {"shown": "bad"},
        {"answer": "bad"},
        {"since": NOW.replace(tzinfo=None)},
        {"until": NOW.replace(tzinfo=None)},
        {"since": LATER, "until": NOW},
        {"limit": 0},
        {"limit": 51},
        {"before": ("2026-10-09T00:30:00Z", MISSION)},
        {"before": ("2026-10-09T00:30:00.000Z", "bad")},
    ],
)
def test_decision_records_refuses_invalid_requests(
    episodes: DecisionLoopStore, kwargs: dict[str, Any]
) -> None:
    kwargs.setdefault("limit", 50)
    assert _code(lambda: episodes.decision_records(SCOPE, **kwargs)) == "invalid_request"


def test_record_writes_one_decision_row_per_input_in_input_order(
    episodes: DecisionLoopStore, store: SessionStore
) -> None:
    recorded = _record(episodes, [SILENT, LIST, DEFER, NUDGE])
    assert [(item.candidate, item.level, item.reason, item.defer_until) for item in recorded] == [
        ("tasks_due", "silent", "due_soon", None),
        ("approvals", "list", "waiting_on_you", None),
        ("inbox", "silent", "later_by_owner", stamp(LATER)),
        ("agent_runs", "nudge", "score", None),
    ]
    assert {(item.feedback, item.feedback_reason, item.feedback_revision) for item in recorded} == {
        (None, None, 0)
    }
    assert all(uuid.UUID(item.episode_id).version == 4 for item in recorded)
    rows = _rows(store)
    assert set(rows) == {item.episode_id for item in recorded}

    evidence = '{"review":1,"working":0}'
    parts = [SCOPE, DAY, NUDGE.item_key, _sha256(evidence), STATE, "playbook-1", "nudge"]
    assert rows[recorded[3].episode_id] == {
        "episode_id": recorded[3].episode_id,
        "scope": SCOPE,
        "entry": "decision",
        "created_at": "2026-10-09T00:30:00.000Z",
        "day": DAY,
        "trigger": "change",
        "candidate": "agent_runs",
        "item_key": NUDGE.item_key,
        "state_hash": STATE,
        "evidence": evidence,
        "evidence_key": evidence_key(NUDGE.evidence),
        "policy": "model",
        "policy_version": "playbook-1",
        "model": MODEL_IDS["pinned"],
        "answers": NUDGE.answers_json,
        "terms": '{"alpha":1.0,"p":0.7}',
        "kind": "ask",
        "work": "prepare",
        "level": "nudge",
        "timing": "after_focus",
        "defer_until": None,
        "reason": "score",
        "rules_level": "list",
        "language": None,  # earlier versions stored a detected language here
        "latency_ms": 420,
        "input_tokens": 1800,
        "cost_micro_usd": 76,
        "mission_id": MISSION,
        "exposures": 0,
        "first_exposed_at": None,
        "last_exposed_at": None,
        "opened_at": None,
        "acted_at": None,
        "reviewed_at": None,
        "observed_until": "2026-10-09T00:30:00.000Z",
        "feedback": None,
        "feedback_reason": None,
        "feedback_source": None,
        "feedback_revision": 0,
        "feedback_at": None,
        "dedupe_key": _sha256("|".join([*parts, "after_focus", "prepare", "ask", ""])),
    }
    assert rows[recorded[3].episode_id]["evidence_key"] == _sha256(evidence)

    deferred = '{"count":5,"oldest":"2026-10-01T08:00:00+09:00"}'
    parts = [SCOPE, DAY, DEFER.item_key, evidence_key(DEFER.evidence), "", POLICY_VERSION]
    assert rows[recorded[2].episode_id] == _row(
        episode_id=recorded[2].episode_id,
        candidate="inbox",
        item_key=DEFER.item_key,
        evidence=deferred,
        evidence_key=_sha256(deferred),
        level="silent",
        timing="at",
        defer_until="2026-10-09T04:30:00.000Z",
        reason="later_by_owner",
        rules_level="silent",
        dedupe_key=_sha256("|".join([*parts, "silent", "at", "none", "", stamp(LATER)])),
    )
    silent = rows[recorded[0].episode_id]
    assert silent["evidence"] == (
        '{"files":3,"files_key":"' + "c" * 64 + '","overdue":0,"soon":2,"today":0}'
    )
    assert silent["dedupe_key"] == _sha256(
        "|".join(
            [SCOPE, DAY, SILENT.item_key, evidence_key(SILENT.evidence), ""]
            + [POLICY_VERSION, "silent", "now", "none", "", ""]
        )
    )


def test_record_takes_every_trigger(episodes: DecisionLoopStore, store: SessionStore) -> None:
    for trigger in TRIGGERS:
        _record(episodes, [LIST], trigger=trigger, scope=str(uuid.uuid4()))
    assert sorted(row["trigger"] for row in _rows(store).values()) == sorted(TRIGGERS)


def test_same_day_same_episode_reuses_one_row(
    episodes: DecisionLoopStore, store: SessionStore
) -> None:
    [first] = _record(episodes, [LIST])
    assert _observe(episodes, [first.episode_id]) == 1
    _execute(
        store,
        "UPDATE decision_episodes SET feedback='useful',feedback_source='launcher',"
        "feedback_revision=1,feedback_at=?",
        (stamp(NOW),),
    )
    later = NOW + timedelta(hours=3)
    [again] = _record(episodes, [replace(LIST, evidence=dict(LIST.evidence))], now=later)
    [context] = _record(episodes, [LIST], now=later, trigger="context")
    expected = Recorded(
        first.episode_id, "approvals", "list", "waiting_on_you", None, "useful", None, 1
    )
    assert again == context == expected
    [row] = _rows(store).values()
    assert (row["created_at"], row["trigger"], row["exposures"]) == (stamp(NOW), "change", 1)


@pytest.mark.parametrize(
    "changes",
    [
        {"reason": "show_all"},
        {"policy": "model"},
        {"model": MODEL_IDS["latest"]},
        {"answers_json": '{"news":0.9}'},
        {"terms": {"p": 0.1}},
        {"rules_level": "silent"},
        {"latency_ms": 9},
        {"input_tokens": 9},
        {"cost_micro_usd": 9},
        {"mission_id": str(uuid.uuid4())},
    ],
    ids=lambda changes: next(iter(changes)),
)
def test_columns_outside_the_dedupe_key_answer_the_stored_episode(
    episodes: DecisionLoopStore, store: SessionStore, changes: dict[str, Any]
) -> None:
    [first] = _record(episodes, [LIST])
    before = _rows(store)
    [second] = _record(episodes, [replace(LIST, **changes)], now=NOW + timedelta(hours=1))
    assert second == first
    assert _rows(store) == before


@pytest.mark.parametrize(
    "overrides",
    [
        {"scope": OTHER},
        {"day": "2026-10-10", "now": NOW + timedelta(days=1)},
        {"entries": [replace(LIST, item_key=item_key("approvals", OTHER))]},
        {"entries": [replace(LIST, item_key=None)]},
        {"entries": [replace(LIST, evidence={"native": 3, "proposals": None})]},
        {"entries": [replace(LIST, state_hash=STATE)]},
        {"entries": [replace(LIST, policy_version="rules-2")]},
        {"entries": [replace(LIST, level="badge")]},
        {"entries": [replace(LIST, timing="in_context")]},
        {"entries": [replace(LIST, work="prepare")]},
        {"entries": [replace(LIST, kind="inform")]},
    ],
    ids=[
        "scope",
        "day",
        "item_key",
        "no item_key",
        "evidence",
        "state_hash",
        "policy_version",
        "level",
        "timing",
        "work",
        "kind",
    ],
)
def test_any_other_key_part_opens_a_new_episode(
    episodes: DecisionLoopStore, store: SessionStore, overrides: dict[str, Any]
) -> None:
    arguments = {"entries": [LIST], **overrides}
    [first] = _record(episodes, [LIST])
    [second] = _record(episodes, **arguments)
    assert first.episode_id != second.episode_id
    assert len(_rows(store)) == 2


def test_a_different_defer_until_opens_a_new_episode(
    episodes: DecisionLoopStore, store: SessionStore
) -> None:
    [first] = _record(episodes, [DEFER])
    [second] = _record(episodes, [replace(DEFER, defer_until=LATER + timedelta(minutes=1))])
    assert first.episode_id != second.episode_id
    assert len(_rows(store)) == 2


_BAD_ENTRIES: list[tuple[str, dict[str, Any]]] = [
    ("report topic", {"candidate": "unwatched"}),
    ("unknown candidate", {"candidate": "weather"}),
    ("upper-case item key", {"item_key": APPROVALS_KEY.upper()}),
    ("short item key", {"item_key": APPROVALS_KEY[:-1]}),
    ("short state hash", {"state_hash": STATE[:-1]}),
    ("evidence not a mapping", {"evidence": ["native"]}),
    ("unserializable evidence", {"evidence": {"native": object()}}),
    ("nan evidence", {"evidence": {"native": float("nan")}}),
    ("2049-byte evidence", {"evidence": {"x": "a" * 2041}}),
    ("unknown policy", {"policy": "B1"}),
    ("empty policy version", {"policy_version": ""}),
    ("33-character policy version", {"policy_version": "p" * 33}),
    ("policy version with a lone surrogate", {"policy_version": "\ud800"}),
    ("33-character model", {"model": "m" * 33}),
    ("model with a lone surrogate", {"model": "\ud800"}),
    ("answers not JSON", {"answers_json": "{goal_link: 1}"}),
    ("answers not an object", {"answers_json": "[0.5]"}),
    ("answers with text", {"answers_json": '{"kind":"inform"}'}),
    ("answers with a flag", {"answers_json": '{"news":true}'}),
    ("answers with null", {"answers_json": '{"news":null}'}),
    ("answers with nan", {"answers_json": '{"news":NaN}'}),
    ("answers past the float range", {"answers_json": '{"news":1e999}'}),
    ("answers nested too deep", {"answers_json": '{"x":' + "[" * 1020 + "]" * 1020 + "}"}),
    ("2049-character answers", {"answers_json": '{"' + "k" * 2043 + '":1}'}),
    ("answers not text", {"answers_json": {"news": 0.5}}),
    ("answers key with a lone surrogate", {"answers_json": '{"\ud800":1}'}),
    ("terms with text", {"terms": {"p": "high"}}),
    ("terms with a flag", {"terms": {"p": True}}),
    ("nan terms", {"terms": {"p": float("nan")}}),
    ("nested terms", {"terms": {"p": {"q": 1.0}}}),
    ("terms too long to write out", {"terms": {"p": 10**5000}}),
    ("513-character terms", {"terms": {"k" * 507: 1}}),
    ("terms not a mapping", {"terms": [1.0]}),
    ("unknown kind", {"kind": "tell"}),
    ("unknown work", {"work": "draft"}),
    ("no work", {"work": None}),
    ("unknown level", {"level": "loud"}),
    ("no level", {"level": None}),
    ("unknown timing", {"timing": "later"}),
    ("at without defer_until", {"timing": "at"}),
    ("defer_until without at", {"defer_until": LATER}),
    ("defer_until not an instant", {"timing": "at", "defer_until": "2026-10-09"}),
    ("empty reason", {"reason": ""}),
    ("65-character reason", {"reason": "r" * 65}),
    ("reason with a lone surrogate", {"reason": "\ud800"}),
    ("unknown rules level", {"rules_level": "notify"}),
    ("negative latency", {"latency_ms": -1}),
    ("latency over a minute", {"latency_ms": 60_001}),
    ("latency as a flag", {"latency_ms": True}),
    ("too many input tokens", {"input_tokens": 1_000_001}),
    ("fractional input tokens", {"input_tokens": 1.5}),
    ("negative cost", {"cost_micro_usd": -1}),
    ("cost over the bound", {"cost_micro_usd": 1_000_000_001}),
    ("mission id not a uuid", {"mission_id": "mission-1"}),
    ("upper-case mission id", {"mission_id": MISSION.upper()}),
]
_BAD_RECORDS: list[tuple[str, dict[str, Any]]] = [
    ("short scope", {"scope": SCOPE[:-1]}),
    ("long scope", {"scope": SCOPE + "0"}),
    ("unknown trigger", {"trigger": "closed"}),
    ("malformed day", {"day": "2026-10-9"}),
    ("not an entry", {"entries": [{"candidate": "approvals"}]}),
    *((name, {"entries": [replace(LIST, **changes)]}) for name, changes in _BAD_ENTRIES),
]


@pytest.mark.parametrize(
    "overrides", [case for _, case in _BAD_RECORDS], ids=[name for name, _ in _BAD_RECORDS]
)
def test_record_refuses_an_invalid_request_and_writes_nothing(
    episodes: DecisionLoopStore, store: SessionStore, overrides: dict[str, Any]
) -> None:
    arguments = {"entries": [SILENT], **overrides}
    entries = [LIST, *arguments.pop("entries")]
    assert _code(lambda: _record(episodes, entries, **arguments)) == "invalid_request"
    assert _rows(store) == {}


def test_evidence_of_2048_bytes_is_stored(episodes: DecisionLoopStore, store: SessionStore) -> None:
    [recorded] = _record(episodes, [replace(LIST, evidence={"x": "a" * 2040})])
    assert len(_rows(store)[recorded.episode_id]["evidence"]) == 2048


def test_every_bound_the_columns_allow_is_stored(
    episodes: DecisionLoopStore, store: SessionStore
) -> None:
    high = replace(
        NUDGE,
        policy_version="p" * 32,
        model="m" * 32,
        answers_json='{"' + "k" * 2042 + '":1}',
        terms={"k" * 506: 1},
        reason="r" * 64,
        latency_ms=60_000,
        input_tokens=1_000_000,
        cost_micro_usd=1_000_000_000,
    )
    low = replace(
        SILENT, answers_json="{}", terms={}, latency_ms=0, input_tokens=0, cost_micro_usd=0
    )
    top, bottom = _record(episodes, [high, low])
    rows = _rows(store)
    stored = rows[top.episode_id]
    assert [len(stored[name]) for name in ("policy_version", "model", "answers", "terms")] == [
        32,
        32,
        2048,
        512,
    ]
    assert (len(stored["reason"]), stored["latency_ms"], stored["input_tokens"]) == (
        64,
        60_000,
        1_000_000,
    )
    assert stored["cost_micro_usd"] == 1_000_000_000
    stored = rows[bottom.episode_id]
    assert [stored[name] for name in ("answers", "terms", "latency_ms", "input_tokens")] == [
        "{}",
        "{}",
        0,
        0,
    ]
    assert stored["cost_micro_usd"] == 0


def test_the_tables_checks_back_the_stores_own(
    episodes: DecisionLoopStore, store: SessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The store checks every value before it writes, so the DDL's CHECKs stay a backstop: a
    value let past the store's checks is still refused, as unavailable, and nothing lands."""
    monkeypatch.setattr(dl_store, "_require", lambda ok: None)
    with pytest.raises(DecisionLoopError) as failure:
        _record(episodes, [LIST, replace(LIST, timing="at")])  # "at" without defer_until
    refusal = failure.value.__context__
    assert failure.value.code == "unavailable"
    assert isinstance(refusal, StateError) and refusal.code == "invalid_input"  # SQLite's CHECK
    assert _rows(store) == {}


def test_record_and_report_prune_rows_older_than_the_retention_age(
    episodes: DecisionLoopStore, store: SessionStore
) -> None:
    edge = NOW - timedelta(days=RETENTION_DAYS)
    old = _row(created_at=stamp(edge - timedelta(milliseconds=1)))
    kept = _row(created_at=stamp(edge))
    theirs = _row(scope=OTHER, created_at=stamp(edge - timedelta(days=1)))
    _seed(store, [old, kept, theirs])
    [new] = _record(episodes, [LIST])
    assert set(_rows(store)) == {kept["episode_id"], theirs["episode_id"], new.episode_id}
    report_id = _report(episodes, now=NOW + timedelta(milliseconds=1))
    assert set(_rows(store)) == {theirs["episode_id"], new.episode_id, report_id}


def test_record_and_report_keep_the_newest_max_episodes_of_the_scope(
    episodes: DecisionLoopStore, store: SessionStore
) -> None:
    tie = stamp(NOW - timedelta(hours=10))
    low = "00000000-0000-4000-8000-000000000001"
    high = "00000000-0000-4000-8000-000000000002"
    rows = [_row(created_at=stamp(NOW - timedelta(seconds=n))) for n in range(1, MAX_EPISODES - 1)]
    rows += [_row(episode_id=low, created_at=tie), _row(episode_id=high, created_at=tie)]
    theirs = _row(scope=OTHER, created_at=tie)
    _seed(store, [*rows, theirs])
    [new] = _record(episodes, [LIST])
    stored = _rows(store)
    mine = {key for key, row in stored.items() if row["scope"] == SCOPE}
    assert len(mine) == MAX_EPISODES
    assert new.episode_id in mine and high in mine and low not in mine
    report_id = _report(episodes)
    stored = _rows(store)
    mine = {key for key, row in stored.items() if row["scope"] == SCOPE}
    assert len(mine) == MAX_EPISODES
    assert {new.episode_id, report_id} <= mine and high not in mine
    assert theirs["episode_id"] in stored


def test_the_prune_walks_the_scope_by_index_and_sorts_nothing(store: SessionStore) -> None:
    """The prune runs inside record's write transaction. Read through the index on (scope,
    created_at, episode_id), a scope at the cap is not sorted first."""
    executed: list[tuple[str, tuple[object, ...]]] = []
    recording = SimpleNamespace(execute=lambda sql, values: executed.append((sql, values)))
    dl_store._prune(cast(sqlite3.Connection, recording), SCOPE, NOW)
    [(sql, values)] = executed
    with store._connection() as connection:
        plan = [row[3] for row in connection.execute(f"EXPLAIN QUERY PLAN {sql}", values)]
    assert not [step for step in plan if "TEMP B-TREE" in step], plan


# ---- observe -----------------------------------------------------------------------------------


def test_shown_counts_exposures_on_shown_episodes_only(
    episodes: DecisionLoopStore, store: SessionStore
) -> None:
    silent, listed, defer, nudge = _record(episodes, [SILENT, LIST, DEFER, NUDGE])
    report_id = _report(episodes)
    before = _rows(store)
    ids = [silent.episode_id, listed.episode_id, defer.episode_id, nudge.episode_id, report_id]
    first, second = NOW + timedelta(minutes=1), NOW + timedelta(minutes=2)
    assert _observe(episodes, ids, now=first) == 2
    assert _observe(episodes, ids, now=second) == 2
    rows = _rows(store)
    exposure = ("exposures", "first_exposed_at", "last_exposed_at", "observed_until")
    for shown in (listed, nudge):
        assert tuple(rows[shown.episode_id][name] for name in exposure) == (
            2,
            stamp(first),
            stamp(second),
            stamp(second),
        )
    for held in (silent, defer):
        assert tuple(rows[held.episode_id][name] for name in exposure) == (
            0,
            None,
            None,
            stamp(NOW),
        )
    assert rows[report_id] == before[report_id]


def test_every_shown_level_and_no_other_is_observed(
    episodes: DecisionLoopStore, store: SessionStore
) -> None:
    rows = [_row(level=level) for level in ("silent", "list", "badge", "nudge", "alert")]
    rows += [_report_row()]
    _seed(store, rows)
    ids = [row["episode_id"] for row in rows]
    assert _observe(episodes, ids) == 4
    exposed = {row["level"] for row in _rows(store).values() if row["exposures"] == 1}
    assert exposed == {"list", "badge", "nudge", "alert"}


@pytest.mark.parametrize(("event", "column"), [("opened", "opened_at"), ("acted", "acted_at")])
def test_opened_and_acted_stamp_shown_episodes_once(
    episodes: DecisionLoopStore, store: SessionStore, event: str, column: str
) -> None:
    silent, listed = _record(episodes, [SILENT, LIST])
    ids = [silent.episode_id, listed.episode_id]
    first, second = NOW + timedelta(minutes=1), NOW + timedelta(minutes=2)
    assert _observe(episodes, ids, event=event, now=first) == 1
    assert _observe(episodes, ids, event=event, now=second) == 1
    rows = _rows(store)
    assert (rows[listed.episode_id][column], rows[listed.episode_id]["observed_until"]) == (
        stamp(first),
        stamp(second),
    )
    assert rows[listed.episode_id]["exposures"] == 0
    assert (rows[silent.episode_id][column], rows[silent.episode_id]["observed_until"]) == (
        None,
        stamp(NOW),
    )


def test_observe_never_moves_observed_until_back(
    episodes: DecisionLoopStore, store: SessionStore
) -> None:
    [listed] = _record(episodes, [LIST])
    assert _observe(episodes, [listed.episode_id], event="acted", now=NOW - timedelta(hours=1))
    row = _rows(store)[listed.episode_id]
    assert (row["acted_at"], row["observed_until"]) == (stamp(NOW - timedelta(hours=1)), stamp(NOW))


def test_observe_changes_nothing_for_other_scopes_and_unknown_ids(
    episodes: DecisionLoopStore, store: SessionStore
) -> None:
    [mine] = _record(episodes, [LIST])
    [theirs] = _record(episodes, [LIST], scope=OTHER)
    before = _rows(store)
    unknown = [str(uuid.uuid4()) for _ in range(MAX_OPEN_ITEMS - 2)]  # with mine, the most
    later = NOW + timedelta(minutes=1)
    assert _observe(episodes, [theirs.episode_id, *unknown], now=later) == 0
    assert _rows(store) == before
    assert _observe(episodes, [theirs.episode_id, *unknown, mine.episode_id], now=later) == 1
    assert _rows(store)[theirs.episode_id] == before[theirs.episode_id]


_BAD_OBSERVATIONS: list[tuple[str, dict[str, Any]]] = [
    ("short scope", {"scope": SCOPE[:-1]}),
    ("rendered event", {"event": "rendered"}),
    ("reviewed event", {"event": "reviewed"}),
    ("closed event", {"event": "closed"}),
    ("no ids", {"ids": []}),
    ("51 ids", {"ids": [str(uuid.uuid4()) for _ in range(MAX_OPEN_ITEMS)] + [_KNOWN]}),
    ("repeated id", {"ids": [_KNOWN, _KNOWN]}),
    ("upper-case id", {"ids": [_KNOWN.upper()]}),
    ("not an id", {"ids": ["approvals"]}),
    ("ids not a list", {"ids": None}),
]


@pytest.mark.parametrize(
    "overrides",
    [case for _, case in _BAD_OBSERVATIONS],
    ids=[name for name, _ in _BAD_OBSERVATIONS],
)
def test_observe_refuses_an_invalid_request_and_writes_nothing(
    episodes: DecisionLoopStore, store: SessionStore, overrides: dict[str, Any]
) -> None:
    _seed(store, [_row(episode_id=_KNOWN)])
    before = _rows(store)
    arguments = {"ids": [_KNOWN], **overrides}
    ids = arguments.pop("ids")
    assert _code(lambda: _observe(episodes, ids, **arguments)) == "invalid_request"
    assert _rows(store) == before


def test_observe_resolves_by_episode_id_instead_of_walking_the_scope_index(
    episodes: DecisionLoopStore, store: SessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The `+` on `scope` in observe's WHERE clause keeps the (scope, ...) indexes out of play,
    so SQLite plans the bounded `episode_id IN (...)` list through the primary key instead of
    scanning every row of the scope."""
    [recorded] = _record(episodes, [LIST])
    real = store._connection
    seen: list[str] = []

    @contextlib.contextmanager
    def spy(**kwargs: Any) -> Iterator[sqlite3.Connection]:
        with real(**kwargs) as connection:
            connection.set_trace_callback(seen.append)
            try:
                yield connection
            finally:
                connection.set_trace_callback(None)

    monkeypatch.setattr(store, "_connection", spy)
    assert _observe(episodes, [recorded.episode_id]) == 1
    [sql] = [s for s in seen if s.startswith("UPDATE decision_episodes")]
    with real() as connection:
        plan = [row[3] for row in connection.execute(f"EXPLAIN QUERY PLAN {sql}")]
    assert any("episode_id=?" in step for step in plan), plan
    assert not any("scope=?" in step for step in plan), plan


# ---- report ------------------------------------------------------------------------------------


def test_report_stores_a_missed_row_for_the_topic(
    episodes: DecisionLoopStore, store: SessionStore
) -> None:
    report_id = _report(episodes, source="launcher", trigger="launcher")
    second = _report(episodes)
    rows = _rows(store)
    assert set(rows) == {report_id, second}
    row = rows[report_id]
    assert uuid.UUID(report_id).version == 4
    assert len(row["dedupe_key"]) == 64 and row["dedupe_key"] != rows[second]["dedupe_key"]
    assert row == _report_row(
        episode_id=report_id,
        trigger="launcher",
        feedback_source="launcher",
        dedupe_key=row["dedupe_key"],
    )


def test_report_takes_every_topic_and_source(
    episodes: DecisionLoopStore, store: SessionStore
) -> None:
    pairs = {(topic, SOURCES[n % len(SOURCES)]) for n, topic in enumerate(TOPICS)}
    for topic, source in pairs:
        _report(episodes, topic=topic, source=source)
    rows = _rows(store).values()
    assert {(row["candidate"], row["feedback_source"]) for row in rows} == pairs
    assert {source for _, source in pairs} == set(SOURCES)


_BAD_REPORTS: list[tuple[str, dict[str, Any]]] = [
    ("short scope", {"scope": SCOPE[:-1]}),
    ("unknown topic", {"topic": "weather"}),
    ("unknown source", {"source": "panel"}),
    ("unknown trigger", {"trigger": "closed"}),
    ("malformed day", {"day": "10/09/2026"}),
]


@pytest.mark.parametrize(
    "overrides", [case for _, case in _BAD_REPORTS], ids=[name for name, _ in _BAD_REPORTS]
)
def test_report_refuses_an_invalid_request_and_writes_nothing(
    episodes: DecisionLoopStore, store: SessionStore, overrides: dict[str, Any]
) -> None:
    assert _code(lambda: _report(episodes, **overrides)) == "invalid_request"
    assert _rows(store) == {}


# ---- feedback ----------------------------------------------------------------------------------


def test_later_defers_the_topic_and_counts_once(
    episodes: DecisionLoopStore, store: SessionStore
) -> None:
    [recorded] = _record(episodes, [LIST])
    first, prefs = episodes.feedback(
        SCOPE,
        episode_id=recorded.episode_id,
        value="later",
        reason=None,
        source="launcher",
        now=NOW,
    )
    assert (first.feedback, first.feedback_revision) == ("later", 1)
    assert prefs.later == episodes.preferences(SCOPE, NOW).later == {"approvals": LATER}
    assert _preference(store, SCOPE, "later:approvals") == (stamp(LATER), 1)

    again = NOW + timedelta(hours=1)
    second, prefs = episodes.feedback(
        SCOPE, episode_id=recorded.episode_id, value="later", reason=None, source="page", now=again
    )
    assert (second.feedback, second.feedback_revision) == ("later", 2)
    assert prefs == episodes.preferences(SCOPE, again)
    assert prefs.later == {"approvals": LATER + timedelta(hours=1)}
    assert _preference(store, SCOPE, "later:approvals") == (stamp(LATER + timedelta(hours=1)), 2)


def test_mute_upserts_the_candidate_preference(
    episodes: DecisionLoopStore, store: SessionStore
) -> None:
    [recorded] = _record(episodes, [LIST])
    result, prefs = episodes.feedback(
        SCOPE, episode_id=recorded.episode_id, value="mute", reason=None, source="rail", now=NOW
    )
    assert (result.feedback, result.feedback_revision) == ("mute", 1)
    assert prefs.muted == episodes.preferences(SCOPE, NOW).muted == frozenset({"approvals"})
    assert _preference(store, SCOPE, "mute:approvals") == ("1", 1)


def test_wrong_keeps_its_reason_and_a_relabel_clears_it(
    episodes: DecisionLoopStore, store: SessionStore
) -> None:
    [recorded] = _record(episodes, [NUDGE])
    wrong = _feedback(episodes, episode_id=recorded.episode_id, value="wrong", reason="bad_timing")
    assert (wrong.feedback, wrong.feedback_reason, wrong.feedback_revision) == (
        "wrong",
        "bad_timing",
        1,
    )
    again = NOW + timedelta(minutes=5)
    useful = _feedback(
        episodes, episode_id=recorded.episode_id, value="useful", source="page", now=again
    )
    assert (useful.feedback, useful.feedback_reason, useful.feedback_revision) == (
        "useful",
        None,
        2,
    )
    row = _rows(store)[recorded.episode_id]
    feedback = ("feedback", "feedback_reason", "feedback_source", "feedback_at")
    assert tuple(row[name] for name in feedback) == ("useful", None, "page", stamp(again))
    assert (
        _feedback(episodes, episode_id=recorded.episode_id, value="wrong").feedback_reason is None
    )
    assert _preferences(store) == []  # useful and wrong set no preference


@pytest.mark.parametrize("value", ["later", "mute"])
def test_a_failing_preference_upsert_writes_no_feedback(
    episodes: DecisionLoopStore,
    store: SessionStore,
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    """The feedback and the preference its later or mute sets share one transaction."""
    [recorded] = _record(episodes, [LIST])
    before = _rows(store)

    def failing(*args: Any) -> None:
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(dl_store, "_upsert_preference", failing)
    assert _code(lambda: _feedback(episodes, episode_id=recorded.episode_id, value=value)) == (
        "unavailable"
    )
    assert _rows(store) == before  # the feedback and its revision are unchanged
    assert _preferences(store) == []


@pytest.mark.parametrize("level", ["list", "badge", "nudge", "alert"])
@pytest.mark.parametrize("value", FEEDBACK_SHOWN)
def test_shown_episodes_take_every_shown_value(
    episodes: DecisionLoopStore, store: SessionStore, level: str, value: str
) -> None:
    _seed(store, [_row(episode_id=_KNOWN, level=level)])
    result = _feedback(episodes, value=value, source="rail")
    assert (result.feedback, result.feedback_revision) == (value, 1)


@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.parametrize("value", FEEDBACK_HELD)
def test_held_episodes_take_the_held_values_from_every_source(
    episodes: DecisionLoopStore, store: SessionStore, value: str, source: str
) -> None:
    held = _record(episodes, [SILENT, DEFER])
    for entry in held:
        result = _feedback(episodes, episode_id=entry.episode_id, value=value, source=source)
        assert (result.feedback, result.feedback_revision) == (value, 1)
    rows = _rows(store)
    assert {rows[entry.episode_id]["feedback_source"] for entry in held} == {source}


@pytest.mark.parametrize(
    ("level", "value"),
    [
        ("list", "missed"),
        ("badge", "right_to_hold"),
        ("nudge", "missed"),
        ("alert", "right_to_hold"),
        ("silent", "useful"),
        ("silent", "wrong"),
        ("silent", "later"),
        ("silent", "mute"),
    ],
)
def test_feedback_refuses_a_value_for_the_other_kind_of_episode_as_not_applicable(
    episodes: DecisionLoopStore, store: SessionStore, level: str, value: str
) -> None:
    _seed(store, [_row(episode_id=_KNOWN, level=level)])
    before = _rows(store)
    assert _code(lambda: _feedback(episodes, value=value)) == "not_applicable"
    assert _rows(store) == before
    assert _preferences(store) == []


def test_feedback_on_an_unknown_foreign_or_report_episode_is_unknown_item(
    episodes: DecisionLoopStore,
) -> None:
    [theirs] = _record(episodes, [LIST], scope=OTHER)
    report_id = _report(episodes)
    assert _code(lambda: _feedback(episodes, episode_id=str(uuid.uuid4()))) == "unknown_item"
    assert _code(lambda: _feedback(episodes, episode_id=theirs.episode_id)) == "unknown_item"
    assert _code(lambda: _feedback(episodes, episode_id=report_id)) == "unknown_item"
    assert _code(lambda: _feedback(episodes, episode_id=report_id, value="missed")) == (
        "unknown_item"
    )


_BAD_FEEDBACK: list[tuple[str, dict[str, Any]]] = [
    ("short scope", {"scope": SCOPE[:-1]}),
    ("not a uuid", {"episode_id": "approvals"}),
    ("upper-case id", {"episode_id": _KNOWN.upper()}),
    ("unknown value", {"value": "ignored"}),
    ("unknown source", {"source": "panel"}),
    ("unknown reason", {"value": "wrong", "reason": "boring"}),
    ("reason with useful", {"value": "useful", "reason": "out_of_date"}),
    ("reason with later", {"value": "later", "reason": "bad_timing"}),
    ("reason with a held value", {"value": "missed", "reason": "wrong_topic"}),
]


@pytest.mark.parametrize(
    "overrides", [case for _, case in _BAD_FEEDBACK], ids=[name for name, _ in _BAD_FEEDBACK]
)
def test_feedback_refuses_an_invalid_request_and_writes_nothing(
    episodes: DecisionLoopStore, store: SessionStore, overrides: dict[str, Any]
) -> None:
    _seed(store, [_row(episode_id=_KNOWN)])
    before = _rows(store)
    assert _code(lambda: _feedback(episodes, **overrides)) == "invalid_request"
    assert _rows(store) == before


# ---- preferences -------------------------------------------------------------------------------


def test_preferences_reports_muted_and_later_ignoring_malformed_or_expired_rows(
    episodes: DecisionLoopStore, store: SessionStore
) -> None:
    _seed_preferences(
        store,
        [
            ("mute:inbox", "1"),
            ("mute:routine", "0"),
            ("later:approvals", stamp(NOW + timedelta(hours=1))),
            ("later:tasks_due", stamp(NOW - timedelta(seconds=1))),  # expired
            ("later:agent_runs", "not-a-stamp"),  # malformed
            ("study", "crossover:2026-W10:" + "10" * 13),  # not a preference this store reads
            ("spend", f"{DAY}:5"),
            ("lambda", "0.3"),
        ],
    )
    prefs = episodes.preferences(SCOPE, NOW)
    assert prefs.muted == frozenset({"inbox"})
    assert prefs.later == {"approvals": NOW + timedelta(hours=1)}
    assert not hasattr(prefs, "study")


def test_preferences_is_scoped_to_the_caller(
    episodes: DecisionLoopStore, store: SessionStore
) -> None:
    _seed_preferences(store, [("mute:inbox", "1")], scope=OTHER)
    assert episodes.preferences(SCOPE, NOW).muted == frozenset()
    assert _code(lambda: episodes.preferences(SCOPE[:-1], NOW)) == "invalid_request"


def test_unmute_clears_the_muted_topic_and_bumps_its_revision(
    episodes: DecisionLoopStore, store: SessionStore
) -> None:
    [recorded] = _record(episodes, [LIST])
    _feedback(episodes, episode_id=recorded.episode_id, value="mute")
    assert _preference(store, SCOPE, "mute:approvals") == ("1", 1)
    later = NOW + timedelta(minutes=1)
    prefs = episodes.unmute(SCOPE, "approvals", later)
    assert prefs.muted == frozenset()
    assert _preference(store, SCOPE, "mute:approvals") == ("0", 2)
    assert _code(lambda: episodes.unmute(SCOPE, "unwatched", later)) == "invalid_request"


def test_mark_review_seen_and_review_seen_at_round_trip(
    episodes: DecisionLoopStore, store: SessionStore
) -> None:
    assert episodes.review_seen_at(SCOPE) is None
    _seed_preferences(store, [("mute:inbox", "1")])
    assert episodes.mark_review_seen(SCOPE, NOW) == episodes.preferences(SCOPE, NOW)
    assert episodes.review_seen_at(SCOPE) == NOW
    assert _preference(store, SCOPE, "review_seen") == (stamp(NOW), 1)
    later = NOW + timedelta(hours=2)
    episodes.mark_review_seen(SCOPE, later)
    assert episodes.review_seen_at(SCOPE) == later
    assert _preference(store, SCOPE, "review_seen") == (stamp(later), 2)
    assert _code(lambda: episodes.review_seen_at(SCOPE[:-1])) == "invalid_request"


def test_review_seen_at_treats_a_malformed_stamp_as_absent(
    episodes: DecisionLoopStore, store: SessionStore
) -> None:
    _seed_preferences(store, [("review_seen", "not-a-stamp")])
    assert episodes.review_seen_at(SCOPE) is None


# ---- spend -------------------------------------------------------------------------------------


def test_add_spend_adds_within_a_day_and_starts_again_on_another(
    episodes: DecisionLoopStore, store: SessionStore
) -> None:
    assert episodes.add_spend(SCOPE, DAY, 120, NOW) == 120
    assert episodes.add_spend(SCOPE, DAY, 30, NOW + timedelta(minutes=1)) == 150
    assert episodes.add_spend(SCOPE, DAY, 0, NOW + timedelta(minutes=2)) == 150
    assert _preference(store, SCOPE, "spend") == (f"{DAY}:150", 3)
    assert episodes.spent_on(DAY) == 150

    next_day = "2026-10-10"
    assert episodes.add_spend(SCOPE, next_day, 7, NOW + timedelta(days=1)) == 7
    assert _preference(store, SCOPE, "spend") == (f"{next_day}:7", 4)
    assert (episodes.spent_on(next_day), episodes.spent_on(DAY)) == (7, 0)
    # A clock set back starts the row again too, so the day it now reads is counted.
    assert episodes.add_spend(SCOPE, DAY, 5, NOW + timedelta(days=1, minutes=1)) == 5
    assert (episodes.spent_on(DAY), episodes.spent_on(next_day)) == (5, 0)


def test_spent_on_sums_every_scope_whose_row_holds_that_day(
    episodes: DecisionLoopStore, store: SessionStore
) -> None:
    assert episodes.spent_on(DAY) == 0
    episodes.add_spend(SCOPE, DAY, 100, NOW)
    episodes.add_spend(OTHER, DAY, 23, NOW)
    episodes.add_spend(THIRD, "2026-10-08", 1000, NOW)  # a stale day counts nothing on DAY
    assert episodes.spent_on(DAY) == 123
    assert episodes.spent_on("2026-10-08") == 1000
    assert episodes.spent_on("2026-10-10") == 0


@pytest.mark.parametrize(
    "value",
    [
        "",
        DAY,
        f"{DAY}:",
        f"{DAY}:-5",
        f"{DAY}:1.5",
        f"{DAY}: 5",
        f"{DAY}:5x",
        "2026-10-9:5",
        "x:5",
        f"{DAY}:" + "9" * 19,
    ],
)
def test_a_malformed_spend_row_counts_nothing_and_the_next_add_replaces_it(
    episodes: DecisionLoopStore, store: SessionStore, value: str
) -> None:
    _seed_preferences(store, [("spend", value)])
    assert episodes.spent_on(DAY) == 0
    assert episodes.add_spend(SCOPE, DAY, 4, NOW) == 4
    assert _preference(store, SCOPE, "spend") == (f"{DAY}:4", 2)


_BAD_SPEND: list[tuple[str, dict[str, Any]]] = [
    ("short scope", {"scope": SCOPE[:-1]}),
    ("malformed day", {"day": "2026-10-9"}),
    ("negative amount", {"micro_usd": -1}),
    ("amount over the bound", {"micro_usd": 1_000_000_001}),
    ("fractional amount", {"micro_usd": 1.5}),
    ("amount as a flag", {"micro_usd": True}),
    ("amount as text", {"micro_usd": "5"}),
]


@pytest.mark.parametrize(
    "overrides", [case for _, case in _BAD_SPEND], ids=[name for name, _ in _BAD_SPEND]
)
def test_add_spend_refuses_an_invalid_request_and_writes_nothing(
    episodes: DecisionLoopStore, store: SessionStore, overrides: dict[str, Any]
) -> None:
    arguments: dict[str, Any] = {"scope": SCOPE, "day": DAY, "micro_usd": 5, **overrides}
    assert (
        _code(
            lambda: episodes.add_spend(
                arguments["scope"], arguments["day"], arguments["micro_usd"], NOW
            )
        )
        == "invalid_request"
    )
    assert _preferences(store) == []


def test_spent_on_refuses_a_malformed_day(episodes: DecisionLoopStore) -> None:
    assert _code(lambda: episodes.spent_on("2026-10-9")) == "invalid_request"
    assert _code(lambda: episodes.spent_on(cast(str, None))) == "invalid_request"


# ---- usage --------------------------------------------------------------------------------------


def test_add_usage_counts_one_day_over_every_scope(
    episodes: DecisionLoopStore, store: SessionStore
) -> None:
    assert episodes.usage_on(DAY) == Usage(0, 0, 0, 0)
    total = episodes.add_usage(
        SCOPE, DAY, calls=2, shadow_calls=1, tokens=500, held_back=1, micro_usd=40, now=NOW
    )
    assert total == 40
    assert episodes.add_spend(SCOPE, DAY, 0, NOW) == 40  # the same "spend" `add_spend` reads
    assert episodes.usage_on(DAY) == Usage(2, 1, 500, 1)

    again = NOW + timedelta(minutes=1)
    total = episodes.add_usage(
        SCOPE, DAY, calls=1, shadow_calls=1, tokens=250, held_back=0, micro_usd=10, now=again
    )
    assert total == 50
    assert episodes.usage_on(DAY) == Usage(3, 2, 750, 1)

    episodes.add_usage(
        OTHER, DAY, calls=5, shadow_calls=0, tokens=100, held_back=2, micro_usd=5, now=NOW
    )
    assert episodes.usage_on(DAY) == Usage(8, 2, 850, 3)
    assert episodes.spent_on(DAY) == 55
    assert _preference(store, SCOPE, "usage") == (
        canonical_json({"calls": 3, "day": DAY, "held_back": 1, "shadow_calls": 2, "tokens": 750}),
        2,
    )


def test_add_usage_restarts_the_count_on_a_new_day(
    episodes: DecisionLoopStore, store: SessionStore
) -> None:
    episodes.add_usage(
        SCOPE, DAY, calls=4, shadow_calls=2, tokens=900, held_back=3, micro_usd=70, now=NOW
    )
    next_day = "2026-10-10"
    tomorrow = NOW + timedelta(days=1)
    total = episodes.add_usage(
        SCOPE,
        next_day,
        calls=1,
        shadow_calls=0,
        tokens=20,
        held_back=0,
        micro_usd=9,
        now=tomorrow,
    )
    assert total == 9
    assert episodes.usage_on(next_day) == Usage(1, 0, 20, 0)
    assert episodes.usage_on(DAY) == Usage(0, 0, 0, 0)  # the stored row now holds `next_day`
    assert (episodes.spent_on(next_day), episodes.spent_on(DAY)) == (9, 0)


@pytest.mark.parametrize(
    "value",
    [
        "",
        "not json",
        "NaN",
        "[1,2]",
        '{"day":"' + DAY + '"}',
        '{"calls":1,"day":"' + DAY + '","held_back":0,"shadow_calls":0}',
        '{"calls":-1,"day":"' + DAY + '","held_back":0,"shadow_calls":0,"tokens":0}',
        '{"calls":1.5,"day":"' + DAY + '","held_back":0,"shadow_calls":0,"tokens":0}',
        '{"calls":true,"day":"' + DAY + '","held_back":0,"shadow_calls":0,"tokens":0}',
        '{"calls":1,"day":"2026-10-08","held_back":0,"shadow_calls":0,"tokens":0}',
        '{"calls":1,"day":1,"held_back":0,"shadow_calls":0,"tokens":0}',
        '{"calls":1,"day":"' + DAY + '","extra":0,"held_back":0,"shadow_calls":0,"tokens":0}',
    ],
)
def test_a_malformed_usage_row_counts_nothing_and_the_next_add_replaces_it(
    episodes: DecisionLoopStore, store: SessionStore, value: str
) -> None:
    _seed_preferences(store, [("usage", value)])
    assert episodes.usage_on(DAY) == Usage(0, 0, 0, 0)
    episodes.add_usage(
        SCOPE, DAY, calls=1, shadow_calls=0, tokens=4, held_back=0, micro_usd=1, now=NOW
    )
    assert _preference(store, SCOPE, "usage") == (
        canonical_json({"calls": 1, "day": DAY, "held_back": 0, "shadow_calls": 0, "tokens": 4}),
        2,
    )


def test_add_usage_counts_saturate_at_their_ceilings(
    episodes: DecisionLoopStore, store: SessionStore
) -> None:
    episodes.add_usage(
        SCOPE,
        DAY,
        calls=CEILING - 1,
        shadow_calls=CEILING,
        tokens=TOKENS_CEILING,
        held_back=CEILING - 2,
        micro_usd=1,
        now=NOW,
    )
    episodes.add_usage(
        SCOPE, DAY, calls=5, shadow_calls=5, tokens=5, held_back=5, micro_usd=1, now=NOW
    )
    assert episodes.usage_on(DAY) == Usage(CEILING, CEILING, TOKENS_CEILING, CEILING)


def test_usage_on_sums_every_scope_whose_row_holds_that_day(
    episodes: DecisionLoopStore, store: SessionStore
) -> None:
    assert episodes.usage_on(DAY) == Usage(0, 0, 0, 0)
    episodes.add_usage(
        SCOPE, DAY, calls=1, shadow_calls=0, tokens=10, held_back=0, micro_usd=1, now=NOW
    )
    episodes.add_usage(
        OTHER, DAY, calls=2, shadow_calls=1, tokens=20, held_back=1, micro_usd=1, now=NOW
    )
    episodes.add_usage(  # a stale day counts nothing on DAY
        THIRD, "2026-10-08", calls=9, shadow_calls=9, tokens=9, held_back=9, micro_usd=1, now=NOW
    )
    assert episodes.usage_on(DAY) == Usage(3, 1, 30, 1)
    assert episodes.usage_on("2026-10-08") == Usage(9, 9, 9, 9)
    assert episodes.usage_on("2026-10-10") == Usage(0, 0, 0, 0)


def test_usage_on_refuses_a_malformed_day(episodes: DecisionLoopStore) -> None:
    assert _code(lambda: episodes.usage_on("2026-10-9")) == "invalid_request"


_BAD_USAGE: list[tuple[str, dict[str, Any]]] = [
    ("short scope", {"scope": SCOPE[:-1]}),
    ("malformed day", {"day": "2026-10-9"}),
    ("negative calls", {"calls": -1}),
    ("calls over the bound", {"calls": CEILING + 1}),
    ("fractional calls", {"calls": 1.5}),
    ("calls as a flag", {"calls": True}),
    ("negative shadow_calls", {"shadow_calls": -1}),
    ("shadow_calls over the bound", {"shadow_calls": CEILING + 1}),
    ("negative tokens", {"tokens": -1}),
    ("tokens over the bound", {"tokens": TOKENS_CEILING + 1}),
    ("negative held_back", {"held_back": -1}),
    ("held_back over the bound", {"held_back": CEILING + 1}),
    ("negative amount", {"micro_usd": -1}),
    ("amount over the bound", {"micro_usd": 1_000_000_001}),
    ("fractional amount", {"micro_usd": 1.5}),
    ("amount as a flag", {"micro_usd": True}),
]


@pytest.mark.parametrize(
    "overrides", [case for _, case in _BAD_USAGE], ids=[name for name, _ in _BAD_USAGE]
)
def test_add_usage_refuses_an_invalid_request_and_writes_nothing(
    episodes: DecisionLoopStore, store: SessionStore, overrides: dict[str, Any]
) -> None:
    arguments: dict[str, Any] = {
        "scope": SCOPE,
        "day": DAY,
        "calls": 1,
        "shadow_calls": 0,
        "tokens": 10,
        "held_back": 0,
        "micro_usd": 5,
        **overrides,
    }
    assert (
        _code(
            lambda: episodes.add_usage(
                arguments["scope"],
                arguments["day"],
                calls=arguments["calls"],
                shadow_calls=arguments["shadow_calls"],
                tokens=arguments["tokens"],
                held_back=arguments["held_back"],
                micro_usd=arguments["micro_usd"],
                now=NOW,
            )
        )
        == "invalid_request"
    )
    assert _preferences(store) == []


# ---- the showings log --------------------------------------------------------------------------

_SHOWN = "a" * 64
_AGAIN = "b" * 64
_GOOD = [_SHOWN, stamp(NOW - timedelta(minutes=5))]


def _log(store: SessionStore, scope: str = SCOPE) -> list[Any] | None:
    found = _preference(store, scope, "loud")
    return None if found is None else json.loads(found[0])


def test_showings_since_counts_every_scopes_showings_from_since_leaving_out_one_item(
    episodes: DecisionLoopStore, store: SessionStore
) -> None:
    episodes.add_showing(SCOPE, _SHOWN, NOW - timedelta(minutes=50))
    episodes.add_showing(SCOPE, _AGAIN, NOW - timedelta(minutes=20))
    episodes.add_showing(SCOPE, _SHOWN, NOW)  # shown again, so logged again
    episodes.add_showing(OTHER, _SHOWN, NOW)  # an item of the same key in another workspace
    logged = [
        [_SHOWN, stamp(NOW - timedelta(minutes=50))],
        [_AGAIN, stamp(NOW - timedelta(minutes=20))],
        [_SHOWN, stamp(NOW)],
    ]
    assert _preference(store, SCOPE, "loud") == (canonical_json(logged), 3)
    assert _log(store, OTHER) == [[_SHOWN, stamp(NOW)]]  # each scope keeps its own log
    hour = NOW - timedelta(hours=1)
    # The user's attention is one: every scope counts every scope's showings, its own or not.
    for scope in (SCOPE, OTHER, THIRD):
        assert episodes.showings_since(scope, hour) == 4
    # The item left out is the asking scope's alone; its namesake elsewhere still counts.
    assert episodes.showings_since(SCOPE, hour, exclude_item=_SHOWN) == 2
    assert episodes.showings_since(SCOPE, hour, exclude_item=_AGAIN) == 3
    assert episodes.showings_since(OTHER, hour, exclude_item=_SHOWN) == 3
    assert episodes.showings_since(THIRD, hour, exclude_item=_SHOWN) == 4
    assert episodes.showings_since(SCOPE, NOW - timedelta(minutes=50)) == 4
    assert episodes.showings_since(SCOPE, NOW - timedelta(minutes=20)) == 3
    assert episodes.showings_since(SCOPE, NOW + timedelta(milliseconds=1)) == 0
    assert episodes.preferences(SCOPE, NOW) == episodes.preferences(THIRD, NOW)  # no mute, no later


def test_the_hour_and_the_newest_showings_hold_per_scope_log(
    episodes: DecisionLoopStore, store: SessionStore
) -> None:
    episodes.add_showing(OTHER, _SHOWN, NOW - timedelta(hours=2))
    keys = [f"{n:064x}" for n in range(20)]
    for n, key in enumerate(keys):
        episodes.add_showing(SCOPE, key, NOW + timedelta(minutes=n))
    # A write trims its own scope's log alone; the other keeps its stale showing, which counts
    # only for a reader whose window reaches it.
    newest = [[key, stamp(NOW + timedelta(minutes=n))] for n, key in enumerate(keys)]
    assert _log(store) == newest[-dl_store.MAX_SHOWINGS :]
    assert _log(store, OTHER) == [[_SHOWN, stamp(NOW - timedelta(hours=2))]]
    assert episodes.showings_since(SCOPE, NOW - timedelta(hours=1)) == dl_store.MAX_SHOWINGS
    assert episodes.showings_since(SCOPE, NOW - timedelta(hours=3)) == dl_store.MAX_SHOWINGS + 1
    # The other scope's next showing trims its log; the sum may pass one log's bound.
    episodes.add_showing(OTHER, _AGAIN, NOW)
    assert _log(store, OTHER) == [[_AGAIN, stamp(NOW)]]
    assert episodes.showings_since(THIRD, NOW) == dl_store.MAX_SHOWINGS + 1


def test_add_showing_keeps_the_last_hour_only(
    episodes: DecisionLoopStore, store: SessionStore
) -> None:
    episodes.add_showing(SCOPE, _SHOWN, NOW - timedelta(hours=2))
    episodes.add_showing(SCOPE, _AGAIN, NOW - timedelta(hours=1, milliseconds=1))
    episodes.add_showing(SCOPE, _SHOWN, NOW - timedelta(hours=1))
    episodes.add_showing(SCOPE, _AGAIN, NOW)
    assert _log(store) == [[_SHOWN, stamp(NOW - timedelta(hours=1))], [_AGAIN, stamp(NOW)]]


def test_the_log_keeps_its_newest_showings_within_the_value_bound(
    episodes: DecisionLoopStore, store: SessionStore
) -> None:
    keys = [f"{n:064x}" for n in range(20)]
    for n, key in enumerate(keys):
        episodes.add_showing(SCOPE, key, NOW + timedelta(minutes=n))
    logged = [[key, stamp(NOW + timedelta(minutes=n))] for n, key in enumerate(keys)]
    assert dl_store.MAX_SHOWINGS == 16
    assert _log(store) == logged[-16:]  # the oldest dropped first
    value = cast(tuple[str, int], _preference(store, SCOPE, "loud"))[0]
    assert len(value) <= dl_store.MAX_JSON_BYTES
    assert episodes.showings_since(SCOPE, NOW) == 16


@pytest.mark.parametrize(
    ("value", "kept"),
    [
        pytest.param("", [], id="empty"),
        pytest.param("not json", [], id="not-json"),
        pytest.param("NaN", [], id="nan"),
        pytest.param("[" * 4000 + "]" * 4000, [], id="deep"),
        pytest.param('{"a":1}', [], id="object"),
        pytest.param(json.dumps([_GOOD, [_SHOWN.upper(), _GOOD[1]]]), [_GOOD], id="upper-key"),
        pytest.param(json.dumps([_GOOD, [_SHOWN[1:], _GOOD[1]]]), [_GOOD], id="short-key"),
        pytest.param(json.dumps([_GOOD, [None, _GOOD[1]]]), [_GOOD], id="null-key"),
        pytest.param(json.dumps([_GOOD, [_AGAIN, DAY]]), [_GOOD], id="day-not-stamp"),
        pytest.param(json.dumps([_GOOD, [_AGAIN, _GOOD[1], 1]]), [_GOOD], id="triple"),
        pytest.param(json.dumps([_GOOD, _AGAIN, 7]), [_GOOD], id="not-pairs"),
    ],
)
def test_a_log_entry_that_does_not_read_back_counts_nothing_and_the_next_showing_drops_it(
    episodes: DecisionLoopStore, store: SessionStore, value: str, kept: list[Any]
) -> None:
    _seed_preferences(store, [("loud", value)])
    assert episodes.showings_since(SCOPE, NOW - timedelta(hours=1)) == len(kept)
    episodes.add_showing(SCOPE, _AGAIN, NOW)
    assert _preference(store, SCOPE, "loud") == (
        canonical_json([*kept, [_AGAIN, stamp(NOW)]]),
        2,
    )


def test_the_showings_log_refuses_an_invalid_request_and_writes_nothing(
    episodes: DecisionLoopStore, store: SessionStore
) -> None:
    for call in (
        lambda: episodes.add_showing(SCOPE[:-1], _SHOWN, NOW),
        lambda: episodes.add_showing(SCOPE, _SHOWN.upper(), NOW),
        lambda: episodes.add_showing(SCOPE, _SHOWN[1:], NOW),
        lambda: episodes.add_showing(SCOPE, cast(str, None), NOW),
        lambda: episodes.add_showing(SCOPE, _SHOWN, cast(datetime, stamp(NOW))),
        lambda: episodes.showings_since(SCOPE[:-1], NOW),
        lambda: episodes.showings_since(SCOPE, cast(datetime, stamp(NOW))),
        lambda: episodes.showings_since(SCOPE, NOW, exclude_item=_SHOWN.upper()),
    ):
        assert _code(call) == "invalid_request"
    assert _preferences(store) == []


# ---- each item's showings ----------------------------------------------------------------------

_DAY_BEFORE = NOW - timedelta(hours=24)  # the window's start in these tests
_THIRD_ITEM = "e" * 64
_HOUR = timedelta(hours=1)


def _shown_row(
    key: str | None, exposures: int, first: datetime, last: datetime, **overrides: Any
) -> dict[str, Any]:
    """A decision row of item *key* that the page showed *exposures* times, first at *first*
    and last at *last*, written straight to the table."""
    return _row(
        item_key=key,
        exposures=exposures,
        first_exposed_at=stamp(first),
        last_exposed_at=stamp(last),
        **overrides,
    )


def test_an_episode_first_shown_in_the_window_counts_every_showing(
    episodes: DecisionLoopStore, store: SessionStore
) -> None:
    _seed(
        store,
        [
            _shown_row(_SHOWN, 3, _DAY_BEFORE, NOW),  # first shown at the window's start
            _shown_row(_AGAIN, 2, NOW - _HOUR, NOW - _HOUR),
        ],
    )
    assert episodes.exposures_since(SCOPE, _DAY_BEFORE) == {_SHOWN: 3, _AGAIN: 2}


def test_an_episode_first_shown_before_the_window_counts_its_share_rounded_up_at_least_one(
    episodes: DecisionLoopStore, store: SessionStore
) -> None:
    """Its showings count as spread evenly from its first to its last."""
    _seed(
        store,
        [
            # 4 showings over 3 hours, the last hour inside: 4/3, rounded up to 2
            _shown_row(_SHOWN, 4, _DAY_BEFORE - 2 * _HOUR, _DAY_BEFORE + _HOUR),
            # 10 showings over a day, half of it inside: 5
            _shown_row(_AGAIN, 10, _DAY_BEFORE - 12 * _HOUR, _DAY_BEFORE + 12 * _HOUR),
            # 10 showings, the last at the window's start: a share of 0, yet that one counts
            _shown_row(_THIRD_ITEM, 10, _DAY_BEFORE - 10 * _HOUR, _DAY_BEFORE),
        ],
    )
    assert episodes.exposures_since(SCOPE, _DAY_BEFORE) == {_SHOWN: 2, _AGAIN: 5, _THIRD_ITEM: 1}


def test_an_items_episodes_add_up_and_stop_at_the_ceiling(
    episodes: DecisionLoopStore, store: SessionStore
) -> None:
    _seed(
        store,
        [
            _shown_row(_SHOWN, 4, _DAY_BEFORE - 2 * _HOUR, _DAY_BEFORE + _HOUR),  # its share: 2
            _shown_row(_SHOWN, 3, _DAY_BEFORE + 2 * _HOUR, NOW),
            _shown_row(_SHOWN, 1, NOW, NOW),
            _shown_row(_AGAIN, CEILING, NOW - _HOUR, NOW),
            _shown_row(_AGAIN, CEILING, NOW, NOW),
        ],
    )
    assert episodes.exposures_since(SCOPE, _DAY_BEFORE) == {_SHOWN: 6, _AGAIN: CEILING}


def test_other_scopes_reports_and_episodes_never_shown_count_nothing(
    episodes: DecisionLoopStore, store: SessionStore
) -> None:
    [listed] = _record(episodes, [LIST])  # listed and opened, never shown
    _observe(episodes, [listed.episode_id], event="opened")
    inside = (NOW - _HOUR, NOW)
    _seed(
        store,
        [
            _shown_row(_AGAIN, 1, *inside),  # the one showing that counts
            _shown_row(_SHOWN, 5, *inside, scope=OTHER),
            _report_row(
                item_key=_SHOWN,
                exposures=5,
                first_exposed_at=stamp(inside[0]),
                last_exposed_at=stamp(inside[1]),
            ),
            _shown_row(None, 5, *inside),  # a migrated episode names no item
            _shown_row(_SHOWN, 5, _DAY_BEFORE - 2 * _HOUR, _DAY_BEFORE - timedelta(milliseconds=1)),
        ],
    )
    assert episodes.exposures_since(SCOPE, _DAY_BEFORE) == {_AGAIN: 1}
    assert episodes.exposures_since(OTHER, _DAY_BEFORE) == {_SHOWN: 5}
    assert episodes.exposures_since(THIRD, _DAY_BEFORE) == {}


def test_an_episode_whose_stamps_do_not_read_back_counts_nothing(
    episodes: DecisionLoopStore, store: SessionStore
) -> None:
    _seed(
        store,
        [
            _row(item_key=_SHOWN, exposures=5, last_exposed_at=stamp(NOW)),  # no first stamp
            _row(
                item_key=_SHOWN,
                exposures=5,
                first_exposed_at=stamp(NOW - _HOUR),
                last_exposed_at="2026-19-39T29:59:59.999Z",  # 24 characters, no time
            ),
            _shown_row(_AGAIN, 2, NOW - _HOUR, NOW),
        ],
    )
    assert episodes.exposures_since(SCOPE, _DAY_BEFORE) == {_AGAIN: 2}


def test_an_empty_store_has_no_showings(episodes: DecisionLoopStore) -> None:
    assert episodes.exposures_since(SCOPE, _DAY_BEFORE) == {}


def test_a_since_in_any_zone_reads_as_the_same_instant(
    episodes: DecisionLoopStore, store: SessionStore
) -> None:
    _seed(
        store,
        [
            _shown_row(_SHOWN, 4, _DAY_BEFORE - 2 * _HOUR, _DAY_BEFORE + _HOUR),  # its share: 2
            _shown_row(_AGAIN, 3, _DAY_BEFORE + _HOUR, _DAY_BEFORE + 3 * _HOUR),  # all 3
            _shown_row(_THIRD_ITEM, 2, _DAY_BEFORE - 3 * _HOUR, _DAY_BEFORE - _HOUR),  # none
        ],
    )
    utc = _DAY_BEFORE.astimezone(UTC)
    west = _DAY_BEFORE.astimezone(timezone(timedelta(hours=-3, minutes=-30)))
    expected = {_SHOWN: 2, _AGAIN: 3}
    assert episodes.exposures_since(SCOPE, utc) == expected
    assert episodes.exposures_since(SCOPE, _DAY_BEFORE) == expected  # JST
    assert episodes.exposures_since(SCOPE, west) == expected


def test_exposures_since_refuses_an_invalid_request(episodes: DecisionLoopStore) -> None:
    assert _code(lambda: episodes.exposures_since(SCOPE[:-1], NOW)) == "invalid_request"
    assert _code(lambda: episodes.exposures_since(SCOPE, cast(datetime, stamp(NOW)))) == (
        "invalid_request"
    )


# ---- the arbiter's JSON values -----------------------------------------------------------------


def test_set_json_and_get_json_round_trip_lambda_and_learned(
    episodes: DecisionLoopStore, store: SessionStore
) -> None:
    assert episodes.get_json(SCOPE, "lambda") is None
    episodes.set_json(SCOPE, "lambda", 0.3, NOW)
    learned = {"news": 0.5, "b": -0.5, "goal_link": 0.6}
    episodes.set_json(SCOPE, "learned", learned, NOW)
    assert episodes.get_json(SCOPE, "lambda") == 0.3
    assert episodes.get_json(SCOPE, "learned") == learned
    assert episodes.get_json(OTHER, "lambda") is None
    assert _preference(store, SCOPE, "learned") == ('{"b":-0.5,"goal_link":0.6,"news":0.5}', 1)
    episodes.set_json(SCOPE, "lambda", 1.25, NOW + timedelta(minutes=5))
    assert _preference(store, SCOPE, "lambda") == ("1.25", 2)
    assert episodes.preferences(SCOPE, NOW) == episodes.preferences(OTHER, NOW)  # no mute, no later


def test_set_json_and_get_json_round_trip_the_second_looks_restarts(
    episodes: DecisionLoopStore, store: SessionStore
) -> None:
    assert episodes.get_json(SCOPE, "second_looks") is None
    restarts = {"looks": {"long_task_look": stamp(NOW)}, "kinds": {}}
    episodes.set_json(SCOPE, "second_looks", restarts, NOW)
    assert episodes.get_json(SCOPE, "second_looks") == restarts
    assert episodes.get_json(OTHER, "second_looks") is None
    assert _preference(store, SCOPE, "second_looks") == (
        '{"kinds":{},"looks":{"long_task_look":"2026-10-09T00:30:00.000Z"}}',
        1,
    )


def test_set_json_and_get_json_round_trip_the_project_plans_restarts(
    episodes: DecisionLoopStore, store: SessionStore
) -> None:
    assert episodes.get_json(SCOPE, "project_plans") is None
    restarts = {"looks": {"stalled_project": stamp(NOW)}, "kinds": {}}
    episodes.set_json(SCOPE, "project_plans", restarts, NOW)
    assert episodes.get_json(SCOPE, "project_plans") == restarts
    assert episodes.get_json(SCOPE, "second_looks") is None
    assert _preference(store, SCOPE, "project_plans") == (
        '{"kinds":{},"looks":{"stalled_project":"2026-10-09T00:30:00.000Z"}}',
        1,
    )


def test_a_json_value_of_8192_bytes_is_stored_and_one_more_is_refused(
    episodes: DecisionLoopStore, store: SessionStore
) -> None:
    episodes.set_json(SCOPE, "learned", {"x": "a" * 8184}, NOW)
    assert len(cast(tuple[str, int], _preference(store, SCOPE, "learned"))[0]) == 8192
    too_long = {"x": "a" * 8185}
    assert _code(lambda: episodes.set_json(SCOPE, "learned", too_long, NOW)) == "invalid_request"
    assert episodes.get_json(SCOPE, "learned") == {"x": "a" * 8184}


def test_set_json_and_get_json_round_trip_the_message_drafts_restarts(
    episodes: DecisionLoopStore, store: SessionStore
) -> None:
    assert episodes.get_json(SCOPE, "message_drafts") is None
    restarts = {"looks": {"context_handoff": stamp(NOW)}, "kinds": {}}
    episodes.set_json(SCOPE, "message_drafts", restarts, NOW)
    assert episodes.get_json(SCOPE, "message_drafts") == restarts
    assert episodes.get_json(OTHER, "message_drafts") is None
    assert episodes.get_json(SCOPE, "project_plans") is None
    assert _preference(store, SCOPE, "message_drafts") == (
        '{"kinds":{},"looks":{"context_handoff":"2026-10-09T00:30:00.000Z"}}',
        1,
    )


def test_set_json_and_get_json_round_trip_the_saved_checks_restarts(
    episodes: DecisionLoopStore, store: SessionStore
) -> None:
    assert episodes.get_json(SCOPE, "saved_checks") is None
    restarts = {"looks": {"commit_checks": stamp(NOW)}, "kinds": {}}
    episodes.set_json(SCOPE, "saved_checks", restarts, NOW)
    assert episodes.get_json(SCOPE, "saved_checks") == restarts
    assert episodes.get_json(OTHER, "saved_checks") is None
    assert episodes.get_json(SCOPE, "message_drafts") is None
    assert _preference(store, SCOPE, "saved_checks") == (
        '{"kinds":{},"looks":{"commit_checks":"2026-10-09T00:30:00.000Z"}}',
        1,
    )


def test_set_json_and_get_json_round_trip_the_inbox_triage_restarts(
    episodes: DecisionLoopStore, store: SessionStore
) -> None:
    assert episodes.get_json(SCOPE, "inbox_triage") is None
    restarts = {"looks": {"inbox_triage": stamp(NOW)}, "kinds": {}}
    episodes.set_json(SCOPE, "inbox_triage", restarts, NOW)
    assert episodes.get_json(SCOPE, "inbox_triage") == restarts
    assert episodes.get_json(OTHER, "inbox_triage") is None
    assert episodes.get_json(SCOPE, "saved_checks") is None
    assert _preference(store, SCOPE, "inbox_triage") == (
        '{"kinds":{},"looks":{"inbox_triage":"2026-10-09T00:30:00.000Z"}}',
        1,
    )


_NAMES = [
    "spend",
    "study",
    "review_seen",
    "mute:inbox",
    "later:inbox",
    "timing",
    "LAMBDA",
    "",
    "loud",
    "weights",
    "usage",
    "learn_since",
    "second_look",
    "SECOND_LOOKS",
    "project_plan",
    "PROJECT_PLANS",
    "message_draft",
    "MESSAGE_DRAFTS",
    "saved_check",
    "SAVED_CHECKS",
    "INBOX_TRIAGE",
]


@pytest.mark.parametrize("name", _NAMES)
def test_get_json_and_set_json_refuse_every_other_name(
    episodes: DecisionLoopStore, store: SessionStore, name: str
) -> None:
    assert _code(lambda: episodes.set_json(SCOPE, name, 0.3, NOW)) == "invalid_request"
    assert _code(lambda: episodes.get_json(SCOPE, name)) == "invalid_request"
    assert _preferences(store) == []


def _circular() -> dict[str, Any]:
    value: dict[str, Any] = {}
    value["self"] = value
    return value


_BAD_JSON: list[tuple[str, dict[str, Any]]] = [
    ("short scope", {"scope": SCOPE[:-1]}),
    ("nan", {"value": float("nan")}),
    ("infinity", {"value": {"b": float("inf")}}),
    ("unserializable", {"value": object()}),
    ("circular", {"value": _circular()}),
]


@pytest.mark.parametrize(
    "overrides", [case for _, case in _BAD_JSON], ids=[name for name, _ in _BAD_JSON]
)
def test_set_json_refuses_an_invalid_request_and_writes_nothing(
    episodes: DecisionLoopStore, store: SessionStore, overrides: dict[str, Any]
) -> None:
    arguments: dict[str, Any] = {"scope": SCOPE, "value": 0.3, **overrides}
    assert (
        _code(lambda: episodes.set_json(arguments["scope"], "lambda", arguments["value"], NOW))
        == "invalid_request"
    )
    assert _preferences(store) == []


@pytest.mark.parametrize("value", ["not json", "NaN", "[1,", "1e999x"])
def test_get_json_treats_an_unreadable_value_as_absent(
    episodes: DecisionLoopStore, store: SessionStore, value: str
) -> None:
    _seed_preferences(store, [("lambda", value)])
    assert episodes.get_json(SCOPE, "lambda") is None


# ---- forget_learning and learn_since -----------------------------------------------------------


def test_learn_since_is_unset_until_a_restart(episodes: DecisionLoopStore) -> None:
    assert episodes.learn_since(SCOPE) is None


def test_forget_learning_without_restart_at_drops_only_the_snapshot(
    episodes: DecisionLoopStore, store: SessionStore
) -> None:
    """Delete all decisions passes no *restart_at*: pacing and the restart stamp are left for a
    learning loop that has nothing left to relearn from."""
    episodes.set_json(SCOPE, "learned", {"bias": -1.0}, NOW)
    episodes.set_json(SCOPE, "lambda", 0.4, NOW)
    _seed_preferences(store, [("learn_since", stamp(NOW)), ("mute:inbox", "1")])
    [recorded] = _record(episodes, [LIST])

    episodes.forget_learning(SCOPE, restart_at=None)

    assert episodes.get_json(SCOPE, "learned") is None
    assert episodes.get_json(SCOPE, "lambda") == 0.4  # untouched
    assert episodes.learn_since(SCOPE) == NOW  # untouched
    assert _preference(store, SCOPE, "mute:inbox") == ("1", 1)  # other preferences stay
    assert recorded.episode_id in _rows(store)  # episodes stay


def test_forget_learning_with_restart_at_also_forgets_lambda_and_stamps_learn_since(
    episodes: DecisionLoopStore, store: SessionStore
) -> None:
    episodes.set_json(SCOPE, "learned", {"bias": -1.0}, NOW)
    episodes.set_json(SCOPE, "lambda", 0.4, NOW)
    _seed_preferences(store, [("mute:inbox", "1")])
    [recorded] = _record(episodes, [LIST])

    episodes.forget_learning(SCOPE, restart_at=NOW)

    assert episodes.get_json(SCOPE, "learned") is None
    assert episodes.get_json(SCOPE, "lambda") is None
    assert episodes.learn_since(SCOPE) == NOW
    assert _preference(store, SCOPE, "mute:inbox") == ("1", 1)
    assert recorded.episode_id in _rows(store)

    later = NOW + timedelta(hours=1)
    episodes.set_json(SCOPE, "learned", {"bias": -0.5}, later)
    episodes.forget_learning(SCOPE, restart_at=later)
    assert episodes.get_json(SCOPE, "learned") is None
    assert episodes.learn_since(SCOPE) == later


def test_forget_learning_is_scoped_to_the_caller(
    episodes: DecisionLoopStore, store: SessionStore
) -> None:
    episodes.set_json(SCOPE, "learned", {"bias": -1.0}, NOW)
    episodes.set_json(OTHER, "learned", {"bias": -1.0}, NOW)
    episodes.set_json(OTHER, "lambda", 0.4, NOW)

    episodes.forget_learning(SCOPE, restart_at=NOW)

    assert episodes.get_json(SCOPE, "learned") is None
    assert episodes.get_json(OTHER, "learned") == {"bias": -1.0}
    assert episodes.get_json(OTHER, "lambda") == 0.4
    assert episodes.learn_since(OTHER) is None


def test_learn_since_treats_a_malformed_stamp_as_absent(
    episodes: DecisionLoopStore, store: SessionStore
) -> None:
    _seed_preferences(store, [("learn_since", "not-a-stamp")])
    assert episodes.learn_since(SCOPE) is None


_BAD_FORGET: list[tuple[str, dict[str, Any]]] = [
    ("short scope", {"scope": SCOPE[:-1]}),
    ("restart_at not an instant", {"restart_at": DAY}),
]


@pytest.mark.parametrize(
    "overrides", [case for _, case in _BAD_FORGET], ids=[name for name, _ in _BAD_FORGET]
)
def test_forget_learning_refuses_an_invalid_request_and_writes_nothing(
    episodes: DecisionLoopStore, store: SessionStore, overrides: dict[str, Any]
) -> None:
    episodes.set_json(SCOPE, "learned", {"bias": -1.0}, NOW)
    before = _preferences(store)
    arguments: dict[str, Any] = {"scope": SCOPE, "restart_at": NOW, **overrides}
    assert (
        _code(
            lambda: episodes.forget_learning(arguments["scope"], restart_at=arguments["restart_at"])
        )
        == "invalid_request"
    )
    assert _preferences(store) == before


def test_learn_since_refuses_an_invalid_request(episodes: DecisionLoopStore) -> None:
    assert _code(lambda: episodes.learn_since(SCOPE[:-1])) == "invalid_request"


# ---- counters ----------------------------------------------------------------------------------


def test_counters_stop_at_their_columns_ceiling(
    episodes: DecisionLoopStore, store: SessionStore
) -> None:
    _seed(store, [_row(episode_id=_KNOWN, exposures=CEILING, feedback_revision=CEILING)])
    _seed_preferences(store, [("spend", f"{DAY}:5"), ("lambda", "0.3")], revision=CEILING)
    assert _observe(episodes, [_KNOWN]) == 1
    assert _feedback(episodes).feedback_revision == CEILING
    assert episodes.add_spend(SCOPE, DAY, 7, NOW) == 12
    episodes.set_json(SCOPE, "lambda", 0.5, NOW)
    row = _rows(store)[_KNOWN]
    assert (row["exposures"], row["feedback"], row["feedback_revision"]) == (
        CEILING,
        "useful",
        CEILING,
    )
    assert _preference(store, SCOPE, "spend") == (f"{DAY}:12", CEILING)
    assert _preference(store, SCOPE, "lambda") == ("0.5", CEILING)


# ---- delete, failures, logging -----------------------------------------------------------------


def test_delete_episodes_removes_only_this_scope_and_keeps_preferences(
    episodes: DecisionLoopStore, store: SessionStore
) -> None:
    _record(episodes, [SILENT, LIST])
    _report(episodes)
    [theirs] = _record(episodes, [LIST], scope=OTHER)
    _execute(
        store,
        "INSERT INTO decision_preferences VALUES (?,'mute:inbox','1',1,?)",
        (SCOPE, stamp(NOW)),
    )
    assert episodes.delete_episodes(SCOPE) == 3
    assert set(_rows(store)) == {theirs.episode_id}
    assert episodes.delete_episodes(SCOPE) == 0
    assert _preferences(store) == [(SCOPE, "mute:inbox", "1", 1)]
    assert _code(lambda: episodes.delete_episodes(SCOPE[:-1])) == "invalid_request"


def _every_write(episodes: DecisionLoopStore) -> list[Callable[[], object]]:
    return [
        lambda: _record(episodes, [LIST]),
        lambda: _observe(episodes, [_KNOWN], event="acted"),
        lambda: _report(episodes),
        lambda: _feedback(episodes, episode_id=_KNOWN),
        lambda: episodes.unmute(SCOPE, "inbox", NOW),
        lambda: episodes.mark_review_seen(SCOPE, NOW),
        lambda: episodes.add_spend(SCOPE, DAY, 5, NOW),
        lambda: episodes.add_usage(
            SCOPE, DAY, calls=1, shadow_calls=0, tokens=5, held_back=0, micro_usd=1, now=NOW
        ),
        lambda: episodes.set_json(SCOPE, "lambda", 0.3, NOW),
        lambda: episodes.forget_learning(SCOPE, restart_at=NOW),
        lambda: episodes.add_showing(SCOPE, _SHOWN, NOW),
        lambda: episodes.delete_decisions(SCOPE, ids=[_KNOWN]),
        lambda: episodes.delete_episodes(SCOPE),
    ]


def _every_read(episodes: DecisionLoopStore) -> list[Callable[[], object]]:
    return [
        lambda: episodes.preferences(SCOPE, NOW),
        lambda: episodes.review_seen_at(SCOPE),
        lambda: episodes.spent_on(DAY),
        lambda: episodes.usage_on(DAY),
        lambda: episodes.showings_since(SCOPE, NOW),
        lambda: episodes.exposures_since(SCOPE, NOW),
        lambda: episodes.get_json(SCOPE, "learned"),
        lambda: episodes.learn_since(SCOPE),
        lambda: episodes.decision_records(SCOPE, limit=50),
    ]


def test_a_protection_refusal_is_unavailable_before_any_sql(
    episodes: DecisionLoopStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refused(path: Path) -> None:
        raise protection.ProtectionError()

    def unexpected_sql(*args: Any, **kwargs: Any) -> None:
        pytest.fail("SQL opened before the store's privacy check")

    monkeypatch.setattr(protection, "require_private_directory", refused)
    monkeypatch.setattr(storage.sqlite3, "connect", unexpected_sql)
    for call in [*_every_write(episodes), *_every_read(episodes)]:
        assert _code(call) == "unavailable"


def test_a_busy_store_is_unavailable_for_every_write(
    episodes: DecisionLoopStore, store: SessionStore
) -> None:
    _seed(store, [_row(episode_id=_KNOWN)])
    before = _rows(store)
    with store._connection(write=True):  # another writer holds the store
        for call in _every_write(episodes):
            assert _code(call) == "unavailable"
    assert _rows(store) == before
    assert _preferences(store) == []


def test_the_reads_answer_while_another_writer_holds_the_store(
    episodes: DecisionLoopStore, store: SessionStore
) -> None:
    _seed(store, [_shown_row(_SHOWN, 2, NOW, NOW, level="alert")])
    _seed_preferences(
        store,
        [("mute:inbox", "1"), ("review_seen", stamp(NOW)), ("spend", f"{DAY}:9")],
    )
    _seed_preferences(store, [("learned", '{"b":-0.5}'), ("loud", json.dumps([_GOOD]))])
    with store._connection(write=True):  # another writer holds the store
        assert episodes.preferences(SCOPE, NOW).muted == frozenset({"inbox"})
        assert episodes.review_seen_at(SCOPE) == NOW
        assert episodes.spent_on(DAY) == 9
        assert episodes.usage_on(DAY) == Usage(0, 0, 0, 0)
        assert episodes.showings_since(SCOPE, NOW - timedelta(hours=1)) == 1
        assert episodes.exposures_since(SCOPE, NOW) == {_SHOWN: 2}
        assert episodes.get_json(SCOPE, "learned") == {"b": -0.5}
        assert episodes.learn_since(SCOPE) is None


def test_each_call_opens_the_store_once(
    episodes: DecisionLoopStore, store: SessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One call, one transaction: no other writer can land between feedback's check of the row
    and its update, or between add_spend's read of the day's total and its write."""
    _seed(store, [_row(episode_id=_KNOWN)])
    connection = store._connection
    opened: list[bool] = []

    def counted(*, write: bool = False) -> Any:
        opened.append(write)
        return connection(write=write)

    monkeypatch.setattr(store, "_connection", counted)
    for call in _every_write(episodes):
        opened.clear()
        call()
        assert opened == [True]
    for call in _every_read(episodes):
        opened.clear()
        call()
        assert opened == [False]


def test_a_failure_inside_the_transaction_writes_nothing(
    episodes: DecisionLoopStore, store: SessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing(*args: Any) -> None:
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(dl_store, "_prune", failing)
    assert _code(lambda: _record(episodes, [SILENT, LIST])) == "unavailable"
    assert _code(lambda: _report(episodes)) == "unavailable"
    assert _rows(store) == {}


def test_nothing_logs(episodes: DecisionLoopStore, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    [recorded] = _record(episodes, [LIST])
    _observe(episodes, [recorded.episode_id])
    _report(episodes)
    assert _code(lambda: _record(episodes, [replace(LIST, reason="")])) == "invalid_request"
    _feedback(episodes, episode_id=recorded.episode_id, value="wrong", reason="already_knew")
    episodes.preferences(SCOPE, NOW)
    episodes.unmute(SCOPE, "inbox", NOW)
    episodes.mark_review_seen(SCOPE, NOW)
    episodes.review_seen_at(SCOPE)
    episodes.add_spend(SCOPE, DAY, 5, NOW)
    episodes.spent_on(DAY)
    episodes.add_usage(
        SCOPE, DAY, calls=1, shadow_calls=0, tokens=5, held_back=0, micro_usd=1, now=NOW
    )
    episodes.usage_on(DAY)
    episodes.add_showing(SCOPE, _SHOWN, NOW)
    episodes.showings_since(SCOPE, NOW, exclude_item=_SHOWN)
    episodes.exposures_since(SCOPE, NOW - timedelta(hours=24))
    episodes.set_json(SCOPE, "lambda", 0.3, NOW)
    episodes.get_json(SCOPE, "lambda")
    episodes.forget_learning(SCOPE, restart_at=NOW)
    episodes.learn_since(SCOPE)
    episodes.decision_records(SCOPE, limit=50)
    episodes.delete_decisions(SCOPE, ids=[recorded.episode_id])
    episodes.delete_episodes(SCOPE)
    assert caplog.records == []
