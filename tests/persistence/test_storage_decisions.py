"""The one-writer accessors over `vault_proposals` (schema step 11)."""

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
HASH = "a" * 64
OTHER_HASH = "b" * 64


@pytest.fixture
def state(tmp_path: Path) -> AppState:
    return AppState(tmp_path / "state.json", Prefs())


@pytest.fixture
def store(state: AppState) -> SessionStore:
    return state.store


def _proposal(proposal_id: str, ref: str, **overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "proposal_id": proposal_id,
        "workspace_id": WID,
        "ref": ref,
        "origin": "note_edit",
        "base_sha256": HASH,
        "proposed_sha256": OTHER_HASH,
        "body": b"sealed",
        "text_bytes": 6,
        "handed_at": None,
        "created_at": STAMP,
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


def test_proposals_are_written_read_listed_and_deleted(store: SessionStore) -> None:
    store.write_vault_proposal(_proposal(PID, "b-note.md"))
    store.write_vault_proposal(_proposal(OTHER_PID, "a-note.md"))
    store.write_vault_proposal(_proposal(str(uuid.uuid4()), "c.md", workspace_id=OTHER_WID))
    assert store.read_vault_proposal(PID) == _proposal(PID, "b-note.md")
    assert store.read_vault_proposal("00000000-0000-4000-8000-0000000000ff") is None
    assert [row["ref"] for row in store.list_vault_proposals(WID)] == ["a-note.md", "b-note.md"]
    assert store.delete_vault_proposal(PID) is True
    assert store.delete_vault_proposal(PID) is False
    assert [row["proposal_id"] for row in store.list_vault_proposals(WID)] == [OTHER_PID]


def test_one_id_replaces_its_own_proposal_for_the_same_ref(store: SessionStore) -> None:
    store.write_vault_proposal(_proposal(PID, "note.md"))
    store.write_vault_proposal(_proposal(PID, "note.md", body=b"resealed", text_bytes=8))
    rows = store.list_vault_proposals(WID)
    assert [(row["body"], row["text_bytes"]) for row in rows] == [(b"resealed", 8)]


def test_a_second_open_proposal_for_one_ref_is_refused(
    store: SessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.write_vault_proposal(_proposal(PID, "note.md"))
    with _traced(store, monkeypatch) as seen:
        with pytest.raises(StateError) as failure:
            store.write_vault_proposal(_proposal(OTHER_PID, "note.md"))
        assert failure.value.code == "conflict"
        assert _written(seen) == []
    assert [row["proposal_id"] for row in store.list_vault_proposals(WID)] == [PID]
    # The same id and ref in another workspace is a different note, so it is admitted.
    store.write_vault_proposal(_proposal(OTHER_PID, "note.md", workspace_id=OTHER_WID))
    assert [row["proposal_id"] for row in store.list_vault_proposals(OTHER_WID)] == [OTHER_PID]


def test_a_handed_stamp_updates_the_row_and_a_withdrawn_one_is_not_brought_back(
    store: SessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.write_vault_proposal(_proposal(PID, "note.md"))
    with _traced(store, monkeypatch) as seen:
        assert store.mark_vault_proposal_handed(PID, STAMP) is True
        assert [sql.split(" ")[0] for sql in _written(seen)] == ["UPDATE"]
    assert store.read_vault_proposal(PID) == _proposal(PID, "note.md", handed_at=STAMP)

    assert store.delete_vault_proposal(PID) is True
    assert store.mark_vault_proposal_handed(PID, STAMP) is False
    assert store.list_vault_proposals(WID) == []


@pytest.mark.parametrize(
    "bad",
    [
        {key: value for key, value in _proposal(PID, "note.md").items() if key != "handed_at"},
        {**_proposal(PID, "note.md"), "surprise": 1},
        {**_proposal(PID, "note.md"), "body": "sealed"},
        {**_proposal(PID, "note.md"), "body": None},
    ],
)
def test_a_proposal_row_that_is_not_the_column_set_is_refused(
    store: SessionStore, monkeypatch: pytest.MonkeyPatch, bad: dict[str, Any]
) -> None:
    with _traced(store, monkeypatch) as seen:
        with pytest.raises(StateError) as failure:
            store.write_vault_proposal(bad)
        assert failure.value.code == "invalid_input"
        assert _written(seen) == []
    assert store.list_vault_proposals(WID) == []
