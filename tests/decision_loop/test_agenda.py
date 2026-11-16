"""The Decision agenda: items and call records per scope, sealed at rest under a context that
names the row's scope and key; the open-item cap, call retention, state changes and refusals."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from viki_agent import protection
from viki_agent.automation.routines import stamp
from viki_agent.decision_loop.agenda import (
    CALL_BODY_BYTES,
    CLOSED_KEEP_DAYS,
    ITEM_BODY_BYTES,
    CallEntry,
    CallWrite,
    DecisionAgenda,
    ItemWrite,
)
from viki_agent.decision_loop.contracts import (
    ITEM_STATES,
    LEVELS,
    MAX_CALLS,
    MAX_OPEN_ITEMS,
    MODEL_IDS,
    ActionSpec,
    CallBody,
    CallRecord,
    DecisionLoopError,
    EvidenceLabel,
    ItemBody,
    canonical_json,
    item_key,
    sha256_hex,
)
from viki_agent.decision_loop.store import DecisionLoopStore
from viki_agent.state import AppState, Prefs
from viki_agent.storage import SessionStore, StateError

NOW = datetime(2026, 10, 21, 9, 0, tzinfo=UTC)
SCOPE = "00000000-0000-4000-8000-0000000000a1"
OTHER = "00000000-0000-4000-8000-0000000000b2"
EPISODE = "00000000-0000-4000-8000-0000000000e1"
MISSION = "00000000-0000-4000-8000-0000000000f1"
SESSION = "00000000-0000-4000-8000-0000000000c1"
TITLE = "Marker 7f3a: review the quarterly plan"
WHAT = "Marker 2c4e: three tasks are due today"
STATE_TEXT = "Marker 91bd: the state the model saw"
ITEM_CONTEXT = b"VIKI decision item v1\x00"
CALL_CONTEXT = b"VIKI decision call v1\x00"


@pytest.fixture
def store(tmp_path: Path) -> SessionStore:
    return AppState(tmp_path / "state.json", Prefs()).store


@pytest.fixture
def agenda(store: SessionStore) -> DecisionAgenda:
    return DecisionAgenda(store)


def key(n: object) -> str:
    return item_key("test", str(n))


def body(title: str = TITLE) -> ItemBody:
    return ItemBody(
        title=title,
        what=WHAT,
        why="due today; needs your decision",
        evidence=(EvidenceLabel("Quarterly plan", workspace_id=SCOPE, ref="projects/plan.md"),),
        actions=(
            ActionSpec(
                "open_note", "Open the note", True, workspace_id=SCOPE, ref="projects/plan.md"
            ),
            ActionSpec("open_context", "Why now", False, call_id=SESSION),
        ),
    )


def write(n: object = 1, **overrides: Any) -> ItemWrite:
    values: dict[str, Any] = {
        "item_key": key(n),
        "episode_id": EPISODE,
        "candidate": "tasks_due",
        "kind": "inform",
        "level": "list",
        "timing": "now",
        "defer_until": None,
        "state_hash": sha256_hex("state"),
        "answers_json": '{"importance":{"score":1}}',
        "mission_id": None,
        "body": body(),
        **overrides,
    }
    return ItemWrite(**values)


def call_body(**overrides: Any) -> CallBody:
    values: dict[str, Any] = {
        "title": TITLE,
        "candidate": "tasks_due",
        "state_text": STATE_TEXT,
        "questions_text": "Is this worth bringing up now?",
        "split": (("state", 120), ("questions", 40)),
        "answers_json": '{"kind":{"choice":"inform"}}',
        "held_back": 1,
        "version_setting": "pinned",
        "playbook_version": "1",
        **overrides,
    }
    return CallBody(**values)


def call_write(**overrides: Any) -> CallWrite:
    values: dict[str, Any] = {
        "item_key": key(1),
        "episode_id": EPISODE,
        "session_id": SESSION,
        "model": MODEL_IDS["pinned"],
        "outcome": "answered",
        "failure": None,
        "latency_ms": 420,
        "input_tokens": 1234,
        "cost_micro_usd": 52,
        "body": call_body(),
        **overrides,
    }
    return CallWrite(**values)


def rows(store: SessionStore, table: str) -> list[dict[str, Any]]:
    with store._connection() as connection:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute(f"SELECT * FROM {table}")]


def item_row(store: SessionStore, scope: str, n: object) -> dict[str, Any]:
    (row,) = [
        row
        for row in rows(store, "decision_items")
        if row["scope"] == scope and row["item_key"] == key(n)
    ]
    return row


def execute(store: SessionStore, sql: str, values: tuple[object, ...]) -> None:
    with store._connection(write=True) as connection:
        connection.execute(sql, values)


def item_json(**changes: object) -> dict[str, Any]:
    """The JSON an item's body seals: `body()` with kind "inform" and no mission."""
    return {**asdict(body()), "kind": "inform", "mission_id": None, **changes}


def old_item_json(**changes: object) -> dict[str, Any]:
    """`item_json`'s shape as a body sealed before `ActionSpec.text` existed: each action
    missing the "text" key entirely (not merely null) -- the shape `agenda._OLD_ACTION_FIELDS`
    still reads."""
    base = item_json(**changes)
    old_actions = [{k: v for k, v in action.items() if k != "text"} for action in base["actions"]]
    return {**base, "actions": old_actions}


def refused(code: str, operation: Callable[..., object], *args: object, **kwargs: object) -> None:
    with pytest.raises(DecisionLoopError) as caught:
        operation(*args, **kwargs)
    assert caught.value.code == code and str(caught.value) == code


# ---- sealing -----------------------------------------------------------------------------------


def test_bodies_are_sealed_at_rest(agenda: DecisionAgenda, store: SessionStore) -> None:
    stored = agenda.upsert(SCOPE, write(1, mission_id=MISSION), NOW)
    call_id = agenda.record_call(SCOPE, call_write(), NOW)

    (item,) = rows(store, "decision_items")
    (call,) = rows(store, "decision_calls")
    for raw in (item["body"], call["body"], store.path.read_bytes()):
        for text in (TITLE, WHAT, STATE_TEXT):
            assert text.encode() not in raw
    # Each body is canonical JSON sealed under the scope and key it belongs to.
    item_context = ITEM_CONTEXT + SCOPE.encode() + key(1).encode()
    call_context = CALL_CONTEXT + SCOPE.encode() + call_id.encode()
    assert protection.unprotect(item["body"], context=item_context) == (
        canonical_json(item_json(mission_id=MISSION)).encode()
    )
    assert protection.unprotect(call["body"], context=call_context) == (
        canonical_json(asdict(call_body())).encode()
    )
    assert (stored.body, stored.kind, stored.mission_id) == (body(), "inform", MISSION)
    assert agenda.item(SCOPE, key(1)) == stored
    assert agenda.call(SCOPE, call_id).body == call_body()


def test_japanese_text_round_trips_sealed(agenda: DecisionAgenda, store: SessionStore) -> None:
    title = "四半期の計画を見直す"
    stored = agenda.upsert(SCOPE, write(1, body=body(title)), NOW)
    assert stored.body.title == title
    assert agenda.item(SCOPE, key(1)) == stored
    raw = item_row(store, SCOPE, 1)["body"]
    assert title.encode() not in raw and title.encode("utf-16-le") not in raw


def test_a_corrupt_body_is_skipped_and_counted(agenda: DecisionAgenda, store: SessionStore) -> None:
    for n in range(3):
        agenda.upsert(SCOPE, write(n), NOW + timedelta(minutes=n))
    flipped = bytearray(item_row(store, SCOPE, 1)["body"])
    flipped[len(flipped) // 2] ^= 0x01
    execute(
        store,
        "UPDATE decision_items SET body=? WHERE scope=? AND item_key=?",
        (bytes(flipped), SCOPE, key(1)),
    )

    found, skipped = agenda.open_items(SCOPE)
    assert [record.item_key for record in found] == [key(0), key(2)] and skipped == 1
    assert agenda.item(SCOPE, key(1)) is None
    # Its plain columns still take state changes, and the next upsert of its key replaces it.
    assert agenda.mark(SCOPE, [key(1)], "shown", NOW) == 1
    agenda.upsert(SCOPE, write(1), NOW + timedelta(hours=1))
    found, skipped = agenda.open_items(SCOPE)
    assert [record.item_key for record in found] == [key(0), key(1), key(2)] and skipped == 0


@pytest.mark.parametrize("target", ["key", "scope"])
def test_an_item_body_copied_to_another_row_does_not_open(
    agenda: DecisionAgenda, store: SessionStore, target: str
) -> None:
    scope, n = (SCOPE, 2) if target == "key" else (OTHER, 1)
    agenda.upsert(SCOPE, write(1), NOW)
    agenda.upsert(scope, write(n, body=body("Another title")), NOW)
    execute(
        store,
        "UPDATE decision_items SET body=? WHERE scope=? AND item_key=?",
        (item_row(store, SCOPE, 1)["body"], scope, key(n)),
    )
    assert agenda.item(scope, key(n)) is None
    assert agenda.item(SCOPE, key(1)) is not None
    found, skipped = agenda.open_items(scope)
    assert [record.item_key for record in found] == ([key(1)] if target == "key" else [])
    assert skipped == 1


def test_a_call_body_copied_to_another_call_does_not_open(
    agenda: DecisionAgenda, store: SessionStore
) -> None:
    first = agenda.record_call(SCOPE, call_write(), NOW)
    second = agenda.record_call(
        SCOPE, call_write(body=call_body(title="Another")), NOW + timedelta(seconds=1)
    )
    (sealed,) = [row["body"] for row in rows(store, "decision_calls") if row["call_id"] == first]
    execute(store, "UPDATE decision_calls SET body=? WHERE call_id=?", (sealed, second))

    listed = agenda.calls(SCOPE)
    assert [(entry.call_id, entry.body) for entry in listed] == [
        (second, None),
        (first, call_body()),
    ]
    assert listed[0] == CallEntry(
        second,
        stamp(NOW + timedelta(seconds=1)),
        key(1),
        EPISODE,
        SESSION,
        MODEL_IDS["pinned"],
        "answered",
        None,
        420,
        1234,
        52,
        None,
    )
    refused("unknown_item", agenda.call, SCOPE, second)
    assert agenda.call(SCOPE, first).body == call_body()


def _encoded(value: object) -> bytes:
    return canonical_json(value).encode()


WRONG_ITEM_BODIES = {
    "not_json": b"not json",
    "array": b"[]",
    "not_utf8": b"\xff\xfe",
    "deep": b"[" * 30_000 + b"]" * 30_000,
    "duplicate": b'{"title":"x",' + _encoded(item_json())[1:],
    "missing": _encoded({name: value for name, value in item_json().items() if name != "why"}),
    "extra": _encoded(item_json(note="x")),
    "number": _encoded(item_json(title=5)),
    "kind": _encoded(item_json(kind=1)),
    "label": _encoded(item_json(evidence=[{"label": "x"}])),
    "primary": _encoded(item_json(actions=[{**item_json()["actions"][0], "primary": "yes"}])),
}


@pytest.mark.parametrize("plain", WRONG_ITEM_BODIES.values(), ids=WRONG_ITEM_BODIES.keys())
def test_a_sealed_item_body_of_the_wrong_shape_is_skipped(
    agenda: DecisionAgenda, store: SessionStore, plain: bytes
) -> None:
    agenda.upsert(SCOPE, write(1), NOW)
    context = ITEM_CONTEXT + SCOPE.encode() + key(1).encode()
    update = "UPDATE decision_items SET body=? WHERE scope=? AND item_key=?"
    execute(
        store, update, (protection.protect(_encoded(item_json()), context=context), SCOPE, key(1))
    )
    assert agenda.item(SCOPE, key(1)) is not None
    execute(store, update, (protection.protect(plain, context=context), SCOPE, key(1)))
    assert agenda.item(SCOPE, key(1)) is None
    assert agenda.open_items(SCOPE) == ([], 1)


def test_body_stored_before_drafts_reads(agenda: DecisionAgenda, store: SessionStore) -> None:
    """An item body sealed before `ActionSpec.text` existed -- each action missing the
    "text" key entirely, not merely null -- still opens, every action reading with `text`
    None."""
    agenda.upsert(SCOPE, write(1), NOW)
    context = ITEM_CONTEXT + SCOPE.encode() + key(1).encode()
    execute(
        store,
        "UPDATE decision_items SET body=? WHERE scope=? AND item_key=?",
        (protection.protect(_encoded(old_item_json()), context=context), SCOPE, key(1)),
    )
    read = agenda.item(SCOPE, key(1))
    assert read is not None and read.body == body()
    assert all(action.text is None for action in read.body.actions)
    found, skipped = agenda.open_items(SCOPE)
    assert [record.item_key for record in found] == [key(1)] and skipped == 0


def call_json(**changes: object) -> dict[str, Any]:
    return {**asdict(call_body()), **changes}


WRONG_CALL_BODIES = {
    "array": b"[]",
    "missing": _encoded(
        {name: value for name, value in call_json().items() if name != "playbook_version"}
    ),
    "split_triple": _encoded(call_json(split=[["state", 1, 2]])),
    "split_text": _encoded(call_json(split=[["state", "12"]])),
    "held_back_bool": _encoded(call_json(held_back=True)),
    "held_back_negative": _encoded(call_json(held_back=-1)),
}


@pytest.mark.parametrize("plain", WRONG_CALL_BODIES.values(), ids=WRONG_CALL_BODIES.keys())
def test_a_sealed_call_body_of_the_wrong_shape_lists_without_a_body(
    agenda: DecisionAgenda, store: SessionStore, plain: bytes
) -> None:
    call_id = agenda.record_call(SCOPE, call_write(), NOW)
    context = CALL_CONTEXT + SCOPE.encode() + call_id.encode()
    update = "UPDATE decision_calls SET body=? WHERE call_id=?"
    execute(store, update, (protection.protect(_encoded(call_json()), context=context), call_id))
    assert agenda.call(SCOPE, call_id).body == call_body()
    execute(store, update, (protection.protect(plain, context=context), call_id))
    assert [entry.body for entry in agenda.calls(SCOPE)] == [None]
    refused("unknown_item", agenda.call, SCOPE, call_id)


# ---- items -------------------------------------------------------------------------------------


def test_open_items_are_capped(agenda: DecisionAgenda, store: SessionStore) -> None:
    # Twelve items at each level, the n-th created n minutes after NOW.
    total = MAX_OPEN_ITEMS + 10
    for n in range(total):
        agenda.upsert(SCOPE, write(n, level=LEVELS[n % 5]), NOW + timedelta(minutes=n))
    agenda.upsert(OTHER, write(0), NOW)
    by_level = {level: [n for n in range(total) if LEVELS[n % 5] == level] for level in LEVELS}
    # The ten oldest silent items fall past the cap; the rest show loudest, then oldest first.
    shown = [n for level in reversed(LEVELS) for n in by_level[level]]
    shown = [n for n in shown if n not in by_level["silent"][:10]]

    before, skipped = agenda.open_items(SCOPE)
    assert [record.item_key for record in before] == [key(n) for n in shown] and skipped == 0
    assert len(before) == MAX_OPEN_ITEMS

    assert agenda.prune(SCOPE, NOW + timedelta(hours=2)) == (10, 0)
    after, skipped = agenda.open_items(SCOPE)
    assert after == before and skipped == 0
    states = {
        row["item_key"]: row["state"]
        for row in rows(store, "decision_items")
        if row["scope"] == SCOPE
    }
    assert list(states.values()).count("open") == MAX_OPEN_ITEMS
    assert sorted(n for n in range(total) if states[key(n)] == "stale") == sorted(
        by_level["silent"][:10]
    )
    assert agenda.prune(SCOPE, NOW + timedelta(hours=2)) == (0, 0)
    assert len(agenda.open_items(OTHER)[0]) == 1


def test_upsert_keeps_first_stamps_and_counts_revisions(
    agenda: DecisionAgenda, store: SessionStore
) -> None:
    first = agenda.upsert(SCOPE, write(1), NOW)
    assert (first.state, first.created_at, first.shown_at) == ("open", stamp(NOW), None)
    assert item_row(store, SCOPE, 1)["revision"] == 1
    agenda.mark(SCOPE, [key(1)], "shown", NOW + timedelta(minutes=1))
    agenda.mark(SCOPE, [key(1)], "opened", NOW + timedelta(minutes=2))

    # The same level with a new body is a new revision.
    later = NOW + timedelta(minutes=5)
    second = agenda.upsert(SCOPE, write(1, body=body("A new title")), later)
    assert second.body.title == "A new title"
    assert (second.created_at, second.shown_at) == (stamp(NOW), stamp(NOW + timedelta(minutes=1)))
    row = item_row(store, SCOPE, 1)
    assert (row["revision"], row["updated_at"]) == (2, stamp(later))
    assert row["opened_at"] == stamp(NOW + timedelta(minutes=2))

    until = NOW + timedelta(hours=4)
    third = agenda.upsert(
        SCOPE,
        write(
            1,
            level="nudge",
            kind=None,
            timing="at",
            defer_until=until,
            state_hash=None,
            answers_json=None,
            mission_id=MISSION,
        ),
        later,
    )
    assert (third.level, third.kind, third.timing, third.defer_until) == (
        "nudge",
        None,
        "at",
        stamp(until),
    )
    assert (third.state_hash, third.answers_json, third.mission_id) == (None, None, MISSION)
    assert item_row(store, SCOPE, 1)["revision"] == 3
    assert agenda.item(SCOPE, key(1)) == third


@pytest.mark.parametrize("state", ITEM_STATES)
def test_upsert_reopens_only_a_stale_item(agenda: DecisionAgenda, state: str) -> None:
    agenda.upsert(SCOPE, write(1), NOW)
    agenda.set_state(SCOPE, key(1), state, NOW)
    again = agenda.upsert(SCOPE, write(1), NOW + timedelta(minutes=1))
    assert again.state == ("open" if state in ("open", "stale") else state)


def test_set_state_and_later(agenda: DecisionAgenda, store: SessionStore) -> None:
    agenda.upsert(SCOPE, write(1), NOW)
    until = NOW + timedelta(hours=4)
    agenda.set_state(SCOPE, key(1), "open", NOW, defer_until=until)
    record = agenda.item(SCOPE, key(1))
    assert record is not None
    assert (record.state, record.timing, record.defer_until) == ("open", "at", stamp(until))

    agenda.set_state(SCOPE, key(1), "dismissed", NOW + timedelta(minutes=1))
    record = agenda.item(SCOPE, key(1))
    assert record is not None
    assert (record.state, record.timing, record.defer_until) == ("dismissed", "at", stamp(until))
    assert agenda.open_items(SCOPE) == ([], 0)
    row = item_row(store, SCOPE, 1)
    assert (row["revision"], row["updated_at"]) == (3, stamp(NOW + timedelta(minutes=1)))

    refused("unknown_item", agenda.set_state, SCOPE, key(2), "acted", NOW)
    refused("unknown_item", agenda.set_state, OTHER, key(1), "acted", NOW)
    refused("invalid_request", agenda.set_state, SCOPE, key(1), "closed", NOW)
    refused("invalid_request", agenda.set_state, SCOPE, key(1), "dismissed", NOW, defer_until=until)
    naive = until.replace(tzinfo=None)
    refused("invalid_request", agenda.set_state, SCOPE, key(1), "open", NOW, defer_until=naive)


def test_mark_stamps_each_event_once(agenda: DecisionAgenda, store: SessionStore) -> None:
    agenda.upsert(SCOPE, write(1), NOW)
    agenda.upsert(SCOPE, write(2), NOW)
    minute = timedelta(minutes=1)
    assert agenda.mark(SCOPE, [key(1)], "shown", NOW + minute) == 1
    assert agenda.mark(SCOPE, (key(1), key(2)), "shown", NOW + 2 * minute) == 1
    assert agenda.mark(SCOPE, [key(1)], "opened", NOW + 3 * minute) == 1
    assert agenda.mark(SCOPE, [key(1)], "opened", NOW + 4 * minute) == 0
    assert agenda.mark(OTHER, [key(1)], "shown", NOW) == 0
    stamps = {
        row["item_key"]: (row["shown_at"], row["opened_at"], row["revision"])
        for row in rows(store, "decision_items")
    }
    assert stamps == {
        key(1): (stamp(NOW + minute), stamp(NOW + 3 * minute), 1),
        key(2): (stamp(NOW + 2 * minute), None, 1),
    }
    for keys, event in (
        ([key(1)], "rendered"),
        ([], "shown"),
        ([key(1), key(1)], "shown"),
        ([key(n) for n in range(MAX_OPEN_ITEMS + 1)], "shown"),
        (["A" * 64], "shown"),
        (key(1), "shown"),
    ):
        refused("invalid_request", agenda.mark, SCOPE, keys, event, NOW)


def test_close_missing_makes_absent_open_items_stale(
    agenda: DecisionAgenda, store: SessionStore
) -> None:
    for n in range(4):
        agenda.upsert(SCOPE, write(n), NOW)
    agenda.set_state(SCOPE, key(3), "dismissed", NOW)
    agenda.upsert(OTHER, write(1), NOW)

    assert agenda.close_missing(SCOPE, [key(0), key(9)], NOW + timedelta(minutes=1)) == 2
    states = {
        (row["scope"], row["item_key"]): (row["state"], row["revision"])
        for row in rows(store, "decision_items")
    }
    assert states == {
        (SCOPE, key(0)): ("open", 1),
        (SCOPE, key(1)): ("stale", 2),
        (SCOPE, key(2)): ("stale", 2),
        (SCOPE, key(3)): ("dismissed", 2),
        (OTHER, key(1)): ("open", 1),
    }
    assert agenda.close_missing(SCOPE, [], NOW) == 1
    assert agenda.open_items(SCOPE) == ([], 0)
    refused("invalid_request", agenda.close_missing, SCOPE, ["0" * 63], NOW)


def test_prune_deletes_closed_items_after_thirty_days(
    agenda: DecisionAgenda, store: SessionStore
) -> None:
    old = NOW - timedelta(days=CLOSED_KEEP_DAYS, milliseconds=1)
    for n, state in enumerate(ITEM_STATES):
        agenda.upsert(SCOPE, write(n), old)
        agenda.set_state(SCOPE, key(n), state, old)
    agenda.upsert(SCOPE, write(9), old)
    agenda.set_state(SCOPE, key(9), "acted", NOW - timedelta(days=CLOSED_KEEP_DAYS))
    agenda.upsert(OTHER, write(1), old)
    agenda.set_state(OTHER, key(1), "done", old)

    assert agenda.prune(SCOPE, NOW) == (0, 4)
    left = {(row["scope"], row["item_key"]) for row in rows(store, "decision_items")}
    assert left == {(SCOPE, key(0)), (SCOPE, key(9)), (OTHER, key(1))}


# ---- calls -------------------------------------------------------------------------------------


def test_twenty_calls_are_kept(agenda: DecisionAgenda) -> None:
    ids = [
        agenda.record_call(
            SCOPE, call_write(body=call_body(title=f"Call {n}")), NOW + timedelta(seconds=n)
        )
        for n in range(MAX_CALLS + 5)
    ]
    other = agenda.record_call(OTHER, call_write(), NOW)

    listed = agenda.calls(SCOPE)
    assert [entry.call_id for entry in listed] == ids[::-1][:MAX_CALLS]
    assert [entry.body.title if entry.body else None for entry in listed] == [
        f"Call {n}" for n in reversed(range(5, MAX_CALLS + 5))
    ]
    for gone in ids[:5]:
        refused("unknown_item", agenda.call, SCOPE, gone)
    assert [entry.call_id for entry in agenda.calls(OTHER)] == [other]

    # A call stamped before the others, as after the clock moved back, is kept all the same.
    behind = agenda.record_call(SCOPE, call_write(), NOW - timedelta(days=1))
    listed = agenda.calls(SCOPE)
    assert len(listed) == MAX_CALLS and listed[-1].call_id == behind
    assert [entry.call_id for entry in listed[:-1]] == ids[::-1][: MAX_CALLS - 1]


def test_a_failed_call_round_trips(agenda: DecisionAgenda) -> None:
    sent = call_body(answers_json=None, split=())
    call_id = agenda.record_call(
        SCOPE,
        call_write(
            episode_id=None,
            session_id=None,
            model=None,
            outcome="failed",
            failure="unreachable",
            latency_ms=None,
            input_tokens=None,
            cost_micro_usd=None,
            body=sent,
        ),
        NOW,
    )
    assert agenda.call(SCOPE, call_id) == CallRecord(
        call_id,
        stamp(NOW),
        key(1),
        None,
        None,
        None,
        "failed",
        "unreachable",
        None,
        None,
        None,
        sent,
    )
    refused("unknown_item", agenda.call, OTHER, call_id)
    refused("unknown_item", agenda.call, SCOPE, EPISODE)


def test_delete_all_removes_the_scopes_items_and_calls(agenda: DecisionAgenda) -> None:
    agenda.upsert(SCOPE, write(1), NOW)
    agenda.upsert(SCOPE, write(2), NOW)
    agenda.set_state(SCOPE, key(2), "dismissed", NOW)
    agenda.record_call(SCOPE, call_write(), NOW)
    agenda.upsert(OTHER, write(1), NOW)
    kept = agenda.record_call(OTHER, call_write(), NOW)

    assert agenda.delete_all(SCOPE) == (2, 1)
    assert agenda.item(SCOPE, key(2)) is None and agenda.calls(SCOPE) == []
    assert len(agenda.open_items(OTHER)[0]) == 1
    assert [entry.call_id for entry in agenda.calls(OTHER)] == [kept]
    assert agenda.delete_all(SCOPE) == (0, 0)


def hold_item(store: SessionStore, scope: str, n: int) -> None:
    execute(
        store,
        "INSERT INTO decision_missions (mission_id,scope,session_id,item_key,work,state,"
        "reason,body,created_at,updated_at,deadline_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            f"00000000-0000-4000-8000-{n + 1:012x}",
            scope,
            f"00000000-0000-4000-8001-{n + 1:012x}",
            key(n),
            "run",
            "running",
            "",
            b"{}",
            stamp(NOW),
            stamp(NOW),
            stamp(NOW + timedelta(days=1)),
        ),
    )


def test_item_records_filters_pages_and_opens_bodies_after_read(
    agenda: DecisionAgenda, store: SessionStore
) -> None:
    for n, topic, state in (
        (1, "tasks_due", "open"),
        (2, "inbox", "dismissed"),
        (3, "tasks_due", "done"),
        (4, "tasks_due", "acted"),
    ):
        at = NOW + timedelta(minutes=n)
        agenda.upsert(SCOPE, write(n, candidate=topic, body=body(f"Title {n}")), at)
        if state != "open":
            agenda.set_state(SCOPE, key(n), state, at)
    agenda.upsert(OTHER, write(9), NOW)
    hold_item(store, SCOPE, 3)
    execute(
        store,
        "UPDATE decision_items SET body=? WHERE scope=? AND item_key=?",
        (b"{}", SCOPE, key(4)),
    )

    page = agenda.item_records(SCOPE, limit=2)
    assert (page["total"], page["deletable"]) == (4, 2)
    assert [(row["id"], row["title"], row["kept"]) for row in page["rows"]] == [
        (key(4), "", None),
        (key(3), "Title 3", "chat"),
    ]
    assert page["next"] == {"at": stamp(NOW + timedelta(minutes=3)), "id": key(3)}
    second = agenda.item_records(SCOPE, before=(page["next"]["at"], page["next"]["id"]), limit=2)
    assert [(row["id"], row["kept"]) for row in second["rows"]] == [
        (key(2), None),
        (key(1), "open"),
    ]
    assert second["next"] is None
    assert second["rows"][0]["created_at"] == stamp(NOW + timedelta(minutes=2))
    assert second["rows"][0]["at"] == stamp(NOW + timedelta(minutes=2))
    assert agenda.item_records(SCOPE, topic="tasks_due", limit=10)["total"] == 3
    assert agenda.item_records(SCOPE, state="dismissed", limit=10)["total"] == 1
    assert agenda.item_records(SCOPE, since=NOW + timedelta(minutes=2), limit=10)["total"] == 3
    assert agenda.item_records(SCOPE, until=NOW + timedelta(minutes=4), limit=10)["total"] == 3
    assert agenda.item_records(SCOPE, before=(stamp(NOW + timedelta(minutes=2)), key(2)), limit=10)[
        "rows"
    ][0]["id"] == key(1)
    same_at = NOW + timedelta(minutes=5)
    agenda.upsert(SCOPE, write(5), same_at)
    agenda.upsert(SCOPE, write(6), same_at)
    tied = agenda.item_records(SCOPE, limit=1)
    assert tied["rows"][0]["id"] == max(key(5), key(6))
    assert agenda.item_records(SCOPE, before=(tied["next"]["at"], tied["next"]["id"]), limit=1)[
        "rows"
    ][0]["id"] == min(key(5), key(6))


def test_item_detail_kept_and_unreadable_body(agenda: DecisionAgenda, store: SessionStore) -> None:
    agenda.upsert(SCOPE, write(1), NOW)
    agenda.upsert(SCOPE, write(2), NOW)
    agenda.set_state(SCOPE, key(2), "dismissed", NOW)
    hold_item(store, SCOPE, 2)
    agenda.mark(SCOPE, [key(1)], "shown", NOW)
    agenda.mark(SCOPE, [key(1)], "opened", NOW)
    detail = agenda.item_detail(SCOPE, key(1))
    assert detail == {
        "id": key(1),
        "title": TITLE,
        "topic": "tasks_due",
        "state": "open",
        "level": "list",
        "created_at": stamp(NOW),
        "updated_at": stamp(NOW),
        "shown_at": stamp(NOW),
        "opened_at": stamp(NOW),
        "what": WHAT,
        "why": body().why,
        "evidence": body().evidence,
        "kept": "open",
    }
    chat = agenda.item_detail(SCOPE, key(2))
    assert chat is not None and chat["kept"] == "chat"
    execute(
        store,
        "UPDATE decision_items SET body=? WHERE scope=? AND item_key=?",
        (b"{}", SCOPE, key(2)),
    )
    unreadable = agenda.item_detail(SCOPE, key(2))
    assert unreadable is not None
    assert {name: unreadable[name] for name in ("title", "what", "why", "evidence")} == {
        "title": "",
        "what": "",
        "why": "",
        "evidence": (),
    }
    assert agenda.item_detail(OTHER, key(1)) is None


def test_delete_items_keeps_open_and_scope_mission_then_revives_deleted_key(
    agenda: DecisionAgenda, store: SessionStore
) -> None:
    DecisionLoopStore(store).report(
        SCOPE,
        topic="unwatched",
        source="page",
        trigger="review",
        now=NOW,
        day=NOW.date().isoformat(),
    )
    episodes_before = rows(store, "decision_episodes")
    for n in range(1, 6):
        agenda.upsert(SCOPE, write(n), NOW + timedelta(minutes=n))
        if n != 1:
            agenda.set_state(SCOPE, key(n), "dismissed", NOW + timedelta(minutes=n))
    hold_item(store, SCOPE, 2)
    hold_item(store, OTHER, 3)
    agenda.upsert(OTHER, write(5), NOW)
    assert agenda.delete_items(SCOPE, topic="tasks_due", until=NOW + timedelta(minutes=4)) == (1, 2)
    assert {row["item_key"] for row in rows(store, "decision_items") if row["scope"] == SCOPE} == {
        key(1),
        key(2),
        key(4),
        key(5),
    }
    assert agenda.delete_items(SCOPE, ids=[key(1), key(2), key(4), key(9)]) == (1, 2)
    kept = agenda.item(SCOPE, key(5))
    assert kept is not None and kept.state == "dismissed"
    assert agenda.upsert(SCOPE, write(4), NOW + timedelta(days=1)).state == "open"
    assert agenda.upsert(SCOPE, write(5), NOW + timedelta(days=1)).state == "dismissed"
    assert rows(store, "decision_episodes") == episodes_before
    assert agenda.item(OTHER, key(5)) is not None


def test_delete_calls_is_scoped_and_requires_distinct_uuids(agenda: DecisionAgenda) -> None:
    first = agenda.record_call(SCOPE, call_write(), NOW)
    second = agenda.record_call(SCOPE, call_write(), NOW)
    other = agenda.record_call(OTHER, call_write(), NOW)
    assert agenda.delete_calls(SCOPE, [first, other]) == 1
    assert agenda.delete_calls(SCOPE, [first, second]) == 1
    assert agenda.calls(SCOPE) == []
    assert [entry.call_id for entry in agenda.calls(OTHER)] == [other]
    for ids in ([], [first, first], ["bad"], [first] * 51, first):
        refused("invalid_request", agenda.delete_calls, SCOPE, ids)


def test_item_data_methods_refuse_invalid_filters_and_cursors(agenda: DecisionAgenda) -> None:
    agenda.upsert(SCOPE, write(1), NOW)
    for options in (
        {"topic": "bad"},
        {"state": "bad"},
        {"since": NOW.replace(tzinfo=None)},
        {"until": NOW.replace(tzinfo=None)},
        {"since": NOW, "until": NOW},
    ):
        refused("invalid_request", agenda.item_records, SCOPE, limit=1, **options)
        refused(
            "invalid_request",
            agenda.delete_items,
            SCOPE,
            **({"until": NOW + timedelta(days=1), **options}),
        )
    for before in (
        ("bad", key(1)),
        (stamp(NOW), "A" * 64),
        (stamp(NOW), key(1)[:-1]),
        [stamp(NOW), key(1)],
        (stamp(NOW), key(1), key(1)),
    ):
        refused("invalid_request", agenda.item_records, SCOPE, before=before, limit=1)
    for limit in (0, 51, True):
        refused("invalid_request", agenda.item_records, SCOPE, limit=limit)
    for options in (
        {},
        {"topic": "tasks_due"},
        {"until": NOW},
        {"ids": []},
        {"ids": [key(1), key(1)]},
        {"ids": ["bad"]},
        {"ids": [key(1)], "state": "open"},
        {"ids": [key(1)] * 51},
    ):
        refused("invalid_request", agenda.delete_items, SCOPE, **options)


# ---- refusals ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"item_key": "A" * 64},
        {"item_key": key(1)[:-1]},
        {"episode_id": "e" * 36},
        {"candidate": "unwatched"},
        {"kind": "shout"},
        {"level": "loud"},
        {"timing": "soon"},
        {"timing": "at"},
        {"defer_until": NOW},
        {"timing": "at", "defer_until": NOW.replace(tzinfo=None)},
        {"state_hash": "f" * 63},
        {"answers_json": "{"},
        {"answers_json": "1" * 2049},
        {"answers_json": '{"\ud800":1}'},
        {"mission_id": "m"},
        {"body": replace(body(), title=cast(Any, 5))},
        {"body": replace(body(), actions=(cast(Any, "open_note"),))},
        {"body": replace(body(), evidence=(EvidenceLabel(cast(Any, None)),))},
        {"body": cast(Any, call_body())},
        {"body": replace(body(), why=cast(Any, float("nan")))},
        {"body": body("x" * ITEM_BODY_BYTES)},
    ],
)
def test_upsert_refuses_a_bad_write(
    agenda: DecisionAgenda, store: SessionStore, overrides: dict[str, Any]
) -> None:
    refused("invalid_request", agenda.upsert, SCOPE, write(1, **overrides), NOW)
    assert rows(store, "decision_items") == []


def test_upsert_refuses_a_bad_scope_or_clock(agenda: DecisionAgenda, store: SessionStore) -> None:
    for scope in ("x" * 36, SCOPE.upper(), cast(Any, None)):
        refused("invalid_request", agenda.upsert, scope, write(1), NOW)
    refused("invalid_request", agenda.upsert, SCOPE, write(1), NOW.replace(tzinfo=None))
    refused("invalid_request", agenda.upsert, SCOPE, call_write(), NOW)
    assert rows(store, "decision_items") == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"item_key": "z" * 64},
        {"episode_id": "e"},
        {"session_id": "s" * 36},
        {"model": "m" * 33},
        {"model": "\ud800"},
        {"outcome": "skipped"},
        {"outcome": "failed"},
        {"outcome": "failed", "failure": "boom"},
        {"failure": "unreachable"},
        {"latency_ms": 60_001},
        {"latency_ms": True},
        {"input_tokens": -1},
        {"cost_micro_usd": 1_000_000_001},
        {"body": call_body(split=(("state", 1.5),))},
        {"body": call_body(split=(("state", 1, 2),))},
        {"body": call_body(held_back=-1)},
        {"body": cast(Any, body())},
        {"body": call_body(state_text="x" * CALL_BODY_BYTES)},
    ],
)
def test_record_call_refuses_a_bad_write(
    agenda: DecisionAgenda, store: SessionStore, overrides: dict[str, Any]
) -> None:
    refused("invalid_request", agenda.record_call, SCOPE, call_write(**overrides), NOW)
    assert rows(store, "decision_calls") == []


# Every public method, run against a store holding one item and one call (its id is passed in).
OPERATIONS: dict[str, Callable[[DecisionAgenda, str], object]] = {
    "upsert": lambda agenda, _call: agenda.upsert(SCOPE, write(2), NOW),
    "open_items": lambda agenda, _call: agenda.open_items(SCOPE),
    "item": lambda agenda, _call: agenda.item(SCOPE, key(1)),
    "item_records": lambda agenda, _call: agenda.item_records(SCOPE, limit=1),
    "item_detail": lambda agenda, _call: agenda.item_detail(SCOPE, key(1)),
    "delete_items": lambda agenda, _call: agenda.delete_items(SCOPE, ids=[key(1)]),
    "set_state": lambda agenda, _call: agenda.set_state(SCOPE, key(1), "acted", NOW),
    "mark": lambda agenda, _call: agenda.mark(SCOPE, [key(1)], "shown", NOW),
    "close_missing": lambda agenda, _call: agenda.close_missing(SCOPE, [], NOW),
    "prune": lambda agenda, _call: agenda.prune(SCOPE, NOW),
    "record_call": lambda agenda, _call: agenda.record_call(SCOPE, call_write(), NOW),
    "calls": lambda agenda, _call: agenda.calls(SCOPE),
    "call": lambda agenda, call: agenda.call(SCOPE, call),
    "delete_calls": lambda agenda, call: agenda.delete_calls(SCOPE, [call]),
    "delete_all": lambda agenda, _call: agenda.delete_all(SCOPE),
}


@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize("fault", ["private", "store"])
def test_a_store_that_does_not_work_reads_unavailable(
    agenda: DecisionAgenda,
    store: SessionStore,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    fault: str,
) -> None:
    agenda.upsert(SCOPE, write(1), NOW)
    call_id = agenda.record_call(SCOPE, call_write(), NOW)
    before = rows(store, "decision_items"), rows(store, "decision_calls")

    def not_private(store: object) -> None:
        raise protection.ProtectionError()

    def broken(self: SessionStore, **kwargs: object) -> object:
        raise StateError("busy")

    if fault == "private":
        monkeypatch.setattr("viki_agent.decision_loop.agenda.require_private_store", not_private)
    else:
        monkeypatch.setattr(SessionStore, "_connection", broken)
    refused("unavailable", OPERATIONS[operation], agenda, call_id)
    monkeypatch.undo()
    assert (rows(store, "decision_items"), rows(store, "decision_calls")) == before


def test_sealing_that_does_not_work_reads_unavailable(
    agenda: DecisionAgenda, store: SessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    agenda.upsert(SCOPE, write(1), NOW)
    call_id = agenda.record_call(SCOPE, call_write(), NOW)
    before = rows(store, "decision_items"), rows(store, "decision_calls")

    def refuse(data: bytes, *, context: bytes) -> bytes:
        raise protection.ProtectionError()

    # Every body refuses to open and so does the probe: unavailable, not a skipped row.
    monkeypatch.setattr(protection, "unprotect", refuse)
    for operation in ("open_items", "item", "item_records", "item_detail", "calls", "call"):
        refused("unavailable", OPERATIONS[operation], agenda, call_id)
    monkeypatch.setattr(protection, "protect", refuse)
    for operation in ("upsert", "record_call"):
        refused("unavailable", OPERATIONS[operation], agenda, call_id)
    monkeypatch.undo()
    assert (rows(store, "decision_items"), rows(store, "decision_calls")) == before
