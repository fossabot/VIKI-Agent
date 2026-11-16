"""Bounded additional code roots remain pinned with their session."""

from __future__ import annotations

import contextlib
import json
import sqlite3
import tracemalloc
from pathlib import Path
from typing import Any

import pytest

from tests.persistence.test_storage_roots import (
    _WORKSPACE,
    _code,
    _id,
    _race,
    _root,
    _stamp,
    _store,
)
from viki_agent.code.code_checkout import busy_in


def _extra(n: int = 102, **changes: Any) -> dict[str, str]:
    return {
        "workspace_id": _id(n),
        "display_root": f"<repo>/folder-{n}",
        "physical_id": f"1:{n}",
        **changes,
    }


async def test_additional_root_reopen_and_secondary_busy_lookup(tmp_path: Path) -> None:
    row = _root(1, additional_roots=[_extra(), _extra(103)])
    assert _store(tmp_path).put_session_root(row)
    store = _store(tmp_path)
    assert store.get_session_root(_id(1)) == row
    assert store.list_session_roots(1) == ([row], False)
    for workspace_id in (_WORKSPACE, _id(102), _id(103)):
        assert store.session_roots_in_folder(workspace_id) == [_id(1)]
        assert await busy_in(store, lambda sid: sid == _id(1), workspace_id, None)
    assert not await busy_in(store, lambda _: True, _id(104), None)


def test_empty_additional_roots_keep_the_old_row_shape(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.put_session_root(_root(1, additional_roots=[]))
    assert not store.put_session_root(_root(1))
    assert store.get_session_root(_id(1)) == _root(1)
    assert store.list_session_roots(1) == ([_root(1)], False)


def test_additional_root_snapshots_are_part_of_idempotency(tmp_path: Path) -> None:
    store = _store(tmp_path)
    row = _root(1, additional_roots=[_extra(), _extra(103)])
    assert store.put_session_root(row)
    reordered_keys = [dict(reversed(list(extra.items()))) for extra in row["additional_roots"]]
    assert not store.put_session_root(
        _root(1, additional_roots=reordered_keys, created_at=_stamp(2))
    )
    for extras in ([], [_extra()], [_extra(103), _extra()], [_extra(display_root="<repo>/moved")]):
        assert (
            _code(lambda extras=extras: store.put_session_root(_root(1, additional_roots=extras)))
            == "conflict"
        )
    assert store.get_session_root(_id(1)) == row


@pytest.mark.parametrize(
    "extras",
    [
        None,
        "[]",
        {},
        [None],
        [_extra(), _extra()],
        [_extra(101)],
        [_extra(workspace_id="A" * 36)],
        [_extra(workspace_id=_id(102).replace("-", ""))],
        [_extra(workspace_id="FFFFFFFF-FFFF-4FFF-8FFF-FFFFFFFFFFFF")],
        [{**_extra(), "other": "value"}],
        [{key: value for key, value in _extra().items() if key != "physical_id"}],
        [_extra(display_root=3)],
        [_extra(display_root="ab")],
        [_extra(display_root="x" * 4097)],
        [_extra(display_root="<repo>/\x00")],
        [_extra(physical_id="ab")],
        [_extra(physical_id="x" * 81)],
        [_extra(n) for n in range(102, 110)],
    ],
)
def test_invalid_additional_roots_do_not_publish_any_root(tmp_path: Path, extras: Any) -> None:
    store = _store(tmp_path)
    assert (
        _code(lambda: store.put_session_root(_root(1, additional_roots=extras))) == "invalid_input"
    )
    assert store.list_session_roots(10) == ([], False)


def test_ascii_json_budget_counts_escaped_path_bytes(tmp_path: Path) -> None:
    store = _store(tmp_path)
    large = [_extra(n, display_root="x" * 4096) for n in range(102, 109)]
    assert store.put_session_root(_root(1, additional_roots=large))
    escaped = [_extra(n, display_root="\u00e9" * 4096) for n in range(102, 104)]
    assert (
        _code(lambda: store.put_session_root(_root(2, additional_roots=escaped))) == "invalid_input"
    )
    with contextlib.closing(sqlite3.connect(store.path)) as connection:
        (raw,) = connection.execute("SELECT additional_roots FROM session_roots").fetchone()
    assert raw.isascii() and len(raw.encode("ascii")) <= 32768
    assert json.loads(raw) == large


@pytest.mark.parametrize(
    "raw",
    [
        "{}",
        "[ ",
        '[{"workspace_id":"x"}]',
        json.dumps([_extra(), _extra()]),
        json.dumps([_extra(101)]),
        json.dumps([_extra(display_root="\u00e9")], ensure_ascii=False),
        "[" * 1100 + "]" * 1100,
    ],
)
def test_untrusted_stored_snapshots_fail_closed(tmp_path: Path, raw: str) -> None:
    store = _store(tmp_path)
    store.put_session_root(_root(1))
    with contextlib.closing(sqlite3.connect(store.path)) as connection:
        connection.execute("UPDATE session_roots SET additional_roots=?", (raw,))
        connection.commit()
    assert _code(lambda: store.get_session_root(_id(1))) == "corrupt"
    assert _code(lambda: store.list_session_roots(10)) == "corrupt"
    assert _code(lambda: store.session_roots_in_folder(_id(102))) == "corrupt"
    assert _code(lambda: store.put_session_root(_root(1))) == "corrupt"


def test_folder_busy_lookup_keeps_worktree_queries_primary_only(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.put_session_root(_root(3, additional_roots=[_extra()]))
    store.put_session_root(_root(1, workspace_id=_id(102)))
    store.put_session_root(_root(2, worktree_id=_id(7)))
    assert store.session_roots_in_folder(_id(102)) == [_id(1), _id(3)]
    assert store.session_roots_in_folder(_WORKSPACE) == [_id(3)]
    assert store.session_roots_in_worktree(_id(7)) == [_id(2)]


def test_folder_lookup_memory_does_not_retain_unrelated_root_snapshots(tmp_path: Path) -> None:
    store = _store(tmp_path)
    large = [_extra(n, display_root="x" * 4096) for n in range(102, 109)]
    assert store.put_session_root(_root(1, additional_roots=large))
    with contextlib.closing(sqlite3.connect(store.path)) as connection:
        (raw,) = connection.execute("SELECT additional_roots FROM session_roots").fetchone()
        assert raw.isascii() and 28 * 1024 <= len(raw.encode("ascii")) <= 32768
        connection.executemany(
            "INSERT INTO session_roots "
            "(session_id,workspace_id,worktree_id,display_root,created_at,additional_roots) "
            "SELECT ?,workspace_id,worktree_id,display_root,created_at,additional_roots "
            "FROM session_roots WHERE session_id=?",
            ((_id(n), _id(1)) for n in range(2, 257)),
        )
        connection.commit()
        assert connection.execute("SELECT count(*) FROM session_roots").fetchone() == (256,)

    # Seed before tracing, so the peak measures the lookup rather than fixture construction.
    tracemalloc.start()
    try:
        assert store.session_roots_in_folder(_id(999)) == []
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 1024 * 1024, f"lookup allocated {peak:,} bytes for unrelated root snapshots"


def test_competing_snapshots_pin_one_complete_selection(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = _root(1, additional_roots=[_extra()])
    second = _root(1, additional_roots=[_extra(103)])
    assert _race(lambda: store.put_session_root(first), lambda: store.put_session_root(second)) == [
        "conflict",
        "ok",
    ]
    held = store.get_session_root(_id(1))
    assert held in (first, second)
    assert store.list_session_roots(10) == ([held], False)
