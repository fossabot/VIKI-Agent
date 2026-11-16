"""The one-writer accessors over `skill_proposals` (schema step 17)."""

from __future__ import annotations

import contextlib
import sqlite3
import uuid
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest

from viki_agent.state import AppState, Prefs
from viki_agent.storage import SessionStore, StateError

WID = "00000000-0000-4000-8000-0000000000aa"
OTHER_WID = "00000000-0000-4000-8000-0000000000bb"
PID = "00000000-0000-4000-8000-0000000000c1"
OTHER_PID = "00000000-0000-4000-8000-0000000000c2"
STAMP = "2026-09-17T00:00:00.000Z"
OTHER_STAMP = "2026-09-18T00:00:00.000Z"
HASH = "a" * 64
OTHER_HASH = "b" * 64
SESSIONS_ONE = '["00000000-0000-4000-8000-000000000001"]'


@pytest.fixture
def state(tmp_path: Path) -> AppState:
    return AppState(tmp_path / "state.json", Prefs())


@pytest.fixture
def store(state: AppState) -> SessionStore:
    return state.store


def _proposal(proposal_id: str, name: str, **overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "proposal_id": proposal_id,
        "workspace_id": WID,
        "name": name,
        "trigger": "button",
        "sessions": SESSIONS_ONE,
        "base_sha256": None,
        "proposed_sha256": HASH,
        "body": b"sealed",
        "text_bytes": 6,
        "approved_at": None,
        "rejected_at": None,
        "reason": None,
        "created_at": STAMP,
        "updated_at": STAMP,
    }
    return {**row, **overrides}


@contextlib.contextmanager
def _traced(store: SessionStore, monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """Record every statement the accessor itself runs, after `_connection` has validated."""
    seen: list[str] = []
    real = store._connection

    @contextlib.contextmanager
    def spy(**kwargs: Any) -> Iterator[sqlite3.Connection]:
        with real(**kwargs) as connection:
            connection.set_trace_callback(seen.append)
            try:
                yield connection
            finally:
                connection.set_trace_callback(None)

    monkeypatch.setattr(store, "_connection", spy)
    yield seen


def _written(seen: Sequence[str]) -> list[str]:
    return [sql for sql in seen if sql.split(" ")[0] in ("INSERT", "DELETE", "UPDATE", "REPLACE")]


def test_proposals_are_inserted_read_and_listed_in_name_order(store: SessionStore) -> None:
    store.insert_skill_proposal(_proposal(PID, "weekly-review"), limit=10)
    store.insert_skill_proposal(_proposal(OTHER_PID, "daily-brief"), limit=10)
    store.insert_skill_proposal(
        _proposal(str(uuid.uuid4()), "other-vault", workspace_id=OTHER_WID), limit=10
    )
    assert store.read_skill_proposal(PID) == _proposal(PID, "weekly-review")
    assert store.read_skill_proposal("00000000-0000-4000-8000-0000000000ff") is None
    assert [row["name"] for row in store.list_skill_proposals(WID)] == [
        "daily-brief",
        "weekly-review",
    ]


def test_another_id_holding_the_same_name_is_a_conflict(
    store: SessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.insert_skill_proposal(_proposal(PID, "weekly-review"), limit=10)
    with _traced(store, monkeypatch) as seen:
        with pytest.raises(StateError) as failure:
            store.insert_skill_proposal(_proposal(OTHER_PID, "weekly-review"), limit=10)
        assert failure.value.code == "conflict"
        assert _written(seen) == []
    assert [row["proposal_id"] for row in store.list_skill_proposals(WID)] == [PID]
    # The same name in another workspace is a different skill, so it is admitted.
    store.insert_skill_proposal(
        _proposal(OTHER_PID, "weekly-review", workspace_id=OTHER_WID), limit=10
    )
    assert [row["proposal_id"] for row in store.list_skill_proposals(OTHER_WID)] == [OTHER_PID]


def test_a_duplicate_id_is_also_a_conflict(
    store: SessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.insert_skill_proposal(_proposal(PID, "weekly-review"), limit=10)
    with _traced(store, monkeypatch) as seen:
        with pytest.raises(StateError) as failure:
            store.insert_skill_proposal(
                _proposal(PID, "daily-brief", workspace_id=OTHER_WID), limit=10
            )
        assert failure.value.code == "conflict"
        assert _written(seen) == []


def test_capacity_is_enforced_per_workspace(
    store: SessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.insert_skill_proposal(_proposal(PID, "weekly-review"), limit=1)
    # A second row for another workspace is unaffected by this workspace's cap.
    store.insert_skill_proposal(
        _proposal(OTHER_PID, "weekly-review", workspace_id=OTHER_WID), limit=1
    )
    with _traced(store, monkeypatch) as seen:
        with pytest.raises(StateError) as failure:
            store.insert_skill_proposal(_proposal(str(uuid.uuid4()), "daily-brief"), limit=1)
        assert failure.value.code == "capacity"
        assert _written(seen) == []
    assert len(store.list_skill_proposals(WID)) == 1
    assert len(store.list_skill_proposals(OTHER_WID)) == 1


@pytest.mark.parametrize(
    "bad",
    [
        {key: value for key, value in _proposal(PID, "x").items() if key != "reason"},
        {**_proposal(PID, "x"), "surprise": 1},
        {**_proposal(PID, "x"), "body": "sealed"},
        {**_proposal(PID, "x"), "body": None},
    ],
)
def test_insert_refuses_a_row_that_is_not_the_column_set(
    store: SessionStore, monkeypatch: pytest.MonkeyPatch, bad: dict[str, Any]
) -> None:
    with _traced(store, monkeypatch) as seen:
        with pytest.raises(StateError) as failure:
            store.insert_skill_proposal(bad, limit=10)
        assert failure.value.code == "invalid_input"
        assert _written(seen) == []
    assert store.list_skill_proposals(WID) == []


def test_update_applies_changes_only_when_the_sha_still_matches(store: SessionStore) -> None:
    store.insert_skill_proposal(_proposal(PID, "weekly-review"), limit=10)
    assert (
        store.update_skill_proposal(
            PID,
            HASH,
            {"proposed_sha256": OTHER_HASH, "body": b"resealed", "updated_at": OTHER_STAMP},
        )
        is True
    )
    assert store.read_skill_proposal(PID) == _proposal(
        PID,
        "weekly-review",
        proposed_sha256=OTHER_HASH,
        body=b"resealed",
        updated_at=OTHER_STAMP,
    )


def test_update_returns_false_on_a_moved_revision_and_leaves_the_row_unchanged(
    store: SessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.insert_skill_proposal(_proposal(PID, "weekly-review"), limit=10)
    with _traced(store, monkeypatch) as seen:
        assert store.update_skill_proposal(PID, OTHER_HASH, {"updated_at": OTHER_STAMP}) is False
        # The UPDATE itself still runs (and matches zero rows); it is not skipped client-side.
        assert [sql.split(" ")[0] for sql in _written(seen)] == ["UPDATE"]
    assert store.read_skill_proposal(PID) == _proposal(PID, "weekly-review")


def test_update_returns_false_on_a_missing_id_and_leaves_the_store_unchanged(
    store: SessionStore,
) -> None:
    assert (
        store.update_skill_proposal(
            "00000000-0000-4000-8000-0000000000ff", HASH, {"updated_at": OTHER_STAMP}
        )
        is False
    )
    assert store.list_skill_proposals(WID) == []


@pytest.mark.parametrize(
    "changes",
    [
        {},
        {"name": "renamed", "updated_at": OTHER_STAMP},
        {"workspace_id": OTHER_WID, "updated_at": OTHER_STAMP},
        {"body": b"x"},
    ],
)
def test_update_refuses_bad_changes_before_any_statement_runs(
    store: SessionStore, monkeypatch: pytest.MonkeyPatch, changes: dict[str, Any]
) -> None:
    store.insert_skill_proposal(_proposal(PID, "weekly-review"), limit=10)
    with _traced(store, monkeypatch) as seen:
        with pytest.raises(StateError) as failure:
            store.update_skill_proposal(PID, HASH, changes)
        assert failure.value.code == "invalid_input"
        assert _written(seen) == []
    assert store.read_skill_proposal(PID) == _proposal(PID, "weekly-review")


def test_a_check_failure_on_update_leaves_the_file_byte_identical(
    state: AppState, store: SessionStore
) -> None:
    store.insert_skill_proposal(_proposal(PID, "weekly-review"), limit=10)
    before = state.database_path.read_bytes()
    with pytest.raises(StateError) as failure:
        # approved_at and rejected_at cannot both be set (CHECK(approved_at IS NULL OR
        # rejected_at IS NULL)).
        store.update_skill_proposal(
            PID, HASH, {"approved_at": STAMP, "rejected_at": STAMP, "updated_at": OTHER_STAMP}
        )
    assert failure.value.code == "invalid_input"
    assert state.database_path.read_bytes() == before
    assert store.read_skill_proposal(PID) == _proposal(PID, "weekly-review")


def test_delete_is_the_same_compare_and_set(
    store: SessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.insert_skill_proposal(_proposal(PID, "weekly-review"), limit=10)
    with _traced(store, monkeypatch) as seen:
        assert store.delete_skill_proposal(PID, OTHER_HASH) is False
        assert [sql.split(" ")[0] for sql in _written(seen)] == ["DELETE"]
    assert store.read_skill_proposal(PID) == _proposal(PID, "weekly-review")

    assert store.delete_skill_proposal(PID, HASH) is True
    assert store.delete_skill_proposal(PID, HASH) is False
    assert store.list_skill_proposals(WID) == []
