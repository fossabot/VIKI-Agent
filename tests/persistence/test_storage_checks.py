"""Store accessors for checks and their runs (schema step 16)."""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from viki_agent.state import AppState, Prefs
from viki_agent.storage import CheckRow, CheckRunRow, SessionStore, StateError

_FOLDER = "00000000-0000-4000-8000-000000000101"
_OTHER_FOLDER = "00000000-0000-4000-8000-000000000102"
_TREE_A = "00000000-0000-4000-8000-000000000201"
_TREE_B = "00000000-0000-4000-8000-000000000202"
_CHECK = "00000000-0000-4000-8000-000000000301"
_OID = "a" * 40
_ENDED = "2026-10-04T01:00:00.000Z"
_JOIN_GUARD_S = 10  # a bound on a race that settles in milliseconds, never on real work


def _id(n: int) -> str:
    return f"00000000-0000-4000-8000-{n:012d}"


def _stamp(second: int) -> str:
    return f"2026-10-04T00:{second // 60:02d}:{second % 60:02d}.000Z"


def _store(tmp_path: Path) -> SessionStore:
    return AppState(tmp_path / "state.json", Prefs())._store


def _check(check_id: str = _CHECK, *, folder: str = _FOLDER, second: int = 0) -> CheckRow:
    return {
        "check_id": check_id,
        "workspace_id": folder,
        "body": b"sealed",
        "timeout_s": 600,
        "created_at": _stamp(second),
    }


def _run(run: int, *, check_id: str = _CHECK, worktree_id: str | None = None) -> CheckRunRow:
    return {
        "run_id": _id(run),
        "check_id": check_id,
        "workspace_id": _FOLDER,
        "worktree_id": worktree_id,
        "head_oid": _OID,
        "dirty": 0,
        "outcome": "running",
        "exit_code": None,
        "duration_ms": None,
        "output_bytes": None,
        "tail": None,
        "started_at": _stamp(run),
        "ended_at": None,
    }


def _finish(store: SessionStore, run: int, keep: int = 20) -> bool:
    return store.finish_check_run(
        _id(run),
        outcome="failed",
        exit_code=1,
        duration_ms=1500,
        output_bytes=12,
        tail=b"sealed tail",
        ended_at=_ENDED,
        keep=keep,
    )


def _ids(store: SessionStore, check_id: str = _CHECK) -> list[str]:
    return [row["run_id"] for row in store.check_runs_of(check_id, 100)]


def _code(call: Callable[[], object]) -> str:
    with pytest.raises(StateError) as failure:
        call()
    return failure.value.code


def _race(*calls: Callable[[], Any]) -> list[str]:
    """Run each call on its own thread, released together; answer each result or error code."""
    barrier = threading.Barrier(len(calls))
    outcomes: list[str] = []
    lock = threading.Lock()

    def run(call: Callable[[], Any]) -> None:
        barrier.wait()
        try:
            outcome = repr(call())
        except StateError as exc:
            outcome = exc.code
        with lock:
            outcomes.append(outcome)

    threads = [threading.Thread(target=run, args=(call,)) for call in calls]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(_JOIN_GUARD_S)
        assert not thread.is_alive()
    return sorted(outcomes)


# Checks.


def test_a_check_round_trips_and_lists_oldest_first(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.get_check(_CHECK) is None
    store.insert_check(_check(_id(2), second=5), limit=32)
    store.insert_check(_check(_id(3), second=1), limit=32)
    store.insert_check(_check(_id(1), second=5), limit=32)
    store.insert_check(_check(_id(4), folder=_OTHER_FOLDER), limit=32)
    assert store.get_check(_id(2)) == _check(_id(2), second=5)
    assert [row["check_id"] for row in store.list_checks(_FOLDER)] == [_id(3), _id(1), _id(2)]
    assert store.list_checks(_OTHER_FOLDER) == [_check(_id(4), folder=_OTHER_FOLDER)]


def test_the_33rd_check_of_a_folder_is_capacity(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.insert_check(_check(_id(100), folder=_OTHER_FOLDER), limit=32)  # another folder's
    for n in range(32):
        store.insert_check(_check(_id(n + 1)), limit=32)
    assert _code(lambda: store.insert_check(_check(_id(33)), limit=32)) == "capacity"
    assert len(store.list_checks(_FOLDER)) == 32


def test_check_cap_holds_under_racing_writers(tmp_path: Path) -> None:
    store = _store(tmp_path)
    for n in range(31):
        store.insert_check(_check(_id(n + 1)), limit=32)
    outcomes = _race(
        lambda: store.insert_check(_check(_id(32)), limit=32),
        lambda: store.insert_check(_check(_id(33)), limit=32),
    )
    assert outcomes == ["None", "capacity"]
    assert len(store.list_checks(_FOLDER)) == 32


def test_delete_check_removes_its_runs_and_answers_false_the_second_time(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.insert_check(_check(), limit=32)
    store.insert_check(_check(_id(2)), limit=32)
    store.insert_check_run(_run(1), keep=20)
    store.insert_check_run(_run(2, check_id=_id(2)), keep=20)
    assert store.delete_check(_CHECK) is True
    assert store.get_check(_CHECK) is None
    assert store.get_check_run(_id(1)) is None
    assert store.get_check_run(_id(2)) == _run(2, check_id=_id(2))  # another check's run stays
    assert store.delete_check(_CHECK) is False


# Runs.


def test_a_run_for_an_absent_check_is_not_found(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert _code(lambda: store.insert_check_run(_run(1), keep=20)) == "not_found"
    assert store.get_check_run(_id(1)) is None


def test_insert_keeps_the_newest_finished_runs_and_every_running_one(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.insert_check(_check(), limit=32)
    store.insert_check(_check(_id(2)), limit=32)
    store.insert_check_run(_run(50, check_id=_id(2)), keep=20)
    _finish(store, 50)
    store.insert_check_run(_run(1), keep=20)  # the oldest, still running
    for run in range(2, 24):
        store.insert_check_run(_run(run), keep=20)
        assert _finish(store, run) is True
    store.insert_check_run(_run(24), keep=20)  # running; its insert trims to 20 finished
    runs = store.check_runs_of(_CHECK, 30)
    assert [row["run_id"] for row in runs] == [_id(run) for run in range(24, 3, -1)] + [_id(1)]
    assert [row["outcome"] for row in runs].count("running") == 2
    assert store.get_check_run(_id(50)) is not None  # another check's runs are not trimmed
    assert [row["run_id"] for row in store.check_runs_of(_CHECK, 2)] == [_id(24), _id(23)]


def test_finish_check_run_moves_only_from_running(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.insert_check(_check(), limit=32)
    store.insert_check_run(_run(1), keep=20)
    assert _finish(store, 1) is True
    assert store.get_check_run(_id(1)) == {
        **_run(1),
        "outcome": "failed",
        "exit_code": 1,
        "duration_ms": 1500,
        "output_bytes": 12,
        "tail": b"sealed tail",
        "ended_at": _ENDED,
    }
    assert _finish(store, 1) is False
    assert _finish(store, 2) is False  # no such run


def test_a_settle_trims_its_checks_finished_runs_to_keep(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.insert_check(_check(), limit=32)
    # Runs 1 and 2 share a start, so the run id breaks the tie: 2 is the newer of the two.
    for run, second in ((1, 10), (2, 10), (3, 20)):
        store.insert_check_run({**_run(run), "started_at": _stamp(second)}, keep=20)
    assert _finish(store, 1, keep=2) is True
    assert _finish(store, 2, keep=2) is True
    assert _ids(store) == [_id(3), _id(2), _id(1)]  # run 3 still running
    assert _finish(store, 3, keep=2) is True
    assert _ids(store) == [_id(3), _id(2)]


def test_a_settle_that_does_not_set_trims_nothing(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.insert_check(_check(), limit=32)
    for run in (1, 2, 3):
        store.insert_check_run(_run(run), keep=20)
        assert _finish(store, run) is True
    assert _finish(store, 1, keep=1) is False  # already settled
    assert _finish(store, 9, keep=1) is False  # no such run
    assert _ids(store) == [_id(3), _id(2), _id(1)]


def test_a_settle_keeps_running_rows_and_other_checks_runs(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.insert_check(_check(), limit=32)
    store.insert_check(_check(_id(2)), limit=32)
    for run in (50, 51):
        store.insert_check_run(_run(run, check_id=_id(2)), keep=20)
        assert _finish(store, run) is True
    store.insert_check_run(_run(1), keep=20)  # the oldest, still running
    for run in (2, 3, 4):
        store.insert_check_run(_run(run), keep=20)
    assert _finish(store, 2) is True
    assert _finish(store, 3) is True
    assert _finish(store, 4, keep=1) is True
    assert _ids(store) == [_id(4), _id(1)]
    assert _ids(store, _id(2)) == [_id(51), _id(50)]


def test_two_finishes_racing_from_running_change_one_row(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.insert_check(_check(), limit=32)
    store.insert_check_run(_run(1), keep=20)
    outcomes = _race(
        lambda: _finish(store, 1),
        lambda: store.finish_check_run(
            _id(1),
            outcome="passed",
            exit_code=0,
            duration_ms=10,
            output_bytes=0,
            tail=None,
            ended_at=_ENDED,
            keep=20,
        ),
    )
    assert outcomes == ["False", "True"]
    row = store.get_check_run(_id(1))
    assert row is not None and row["outcome"] in {"failed", "passed"}


def test_last_check_runs_keeps_the_folder_and_each_worktree_apart(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.insert_check(_check(), limit=32)
    store.insert_check(_check(_id(2)), limit=32)
    store.insert_check(_check(_id(3), folder=_OTHER_FOLDER), limit=32)
    store.insert_check_run(_run(1), keep=20)
    store.insert_check_run(_run(2), keep=20)  # the folder's newest of the check
    store.insert_check_run(_run(3, worktree_id=_TREE_A), keep=20)
    store.insert_check_run(_run(4, worktree_id=_TREE_B), keep=20)
    store.insert_check_run(_run(5, worktree_id=_TREE_B), keep=20)
    store.insert_check_run(_run(6, check_id=_id(2), worktree_id=_TREE_B), keep=20)
    store.insert_check_run(_run(7, check_id=_id(3)), keep=20)  # another folder's check
    assert store.last_check_runs(_FOLDER, None) == {_CHECK: _run(2)}
    assert store.last_check_runs(_FOLDER, _TREE_A) == {_CHECK: _run(3, worktree_id=_TREE_A)}
    assert store.last_check_runs(_FOLDER, _TREE_B) == {
        _CHECK: _run(5, worktree_id=_TREE_B),
        _id(2): _run(6, check_id=_id(2), worktree_id=_TREE_B),
    }


def test_check_run_states_read_the_folders_runs_without_their_output(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.insert_check(_check(), limit=32)
    store.insert_check(_check(_id(2)), limit=32)
    store.insert_check(_check(_id(3), folder=_OTHER_FOLDER), limit=32)
    store.insert_check_run(_run(1), keep=20)
    assert _finish(store, 1) is True  # failed, with a sealed tail
    store.insert_check_run(_run(2, worktree_id=_TREE_A), keep=20)
    store.insert_check_run(_run(3, check_id=_id(2)), keep=20)
    # Runs 4 and 5 share a start, so the run id breaks the tie: 5 is the newer of the two.
    store.insert_check_run({**_run(4), "head_oid": None, "dirty": None}, keep=20)
    store.insert_check_run({**_run(5, check_id=_id(2)), "started_at": _stamp(4)}, keep=20)
    store.insert_check_run(_run(6, check_id=_id(3)), keep=20)  # another folder's check

    def state(check_id: str, worktree_id: str | None, head_oid: str | None, outcome: str) -> Any:
        return {
            "check_id": check_id,
            "worktree_id": worktree_id,
            "head_oid": head_oid,
            "outcome": outcome,
        }

    states = store.check_run_states(_FOLDER)
    assert states == [
        state(_id(2), None, _OID, "running"),  # run 5
        state(_CHECK, None, None, "running"),  # run 4
        state(_id(2), None, _OID, "running"),  # run 3
        state(_CHECK, _TREE_A, _OID, "running"),  # run 2
        state(_CHECK, None, _OID, "failed"),  # run 1, read without its tail
    ]
    for check_id in (_CHECK, _id(2)):  # each check's runs in the order `check_runs_of` reads
        assert [s for s in states if s["check_id"] == check_id] == [
            state(r["check_id"], r["worktree_id"], r["head_oid"], r["outcome"])
            for r in store.check_runs_of(check_id, 100)
        ]
    assert store.check_run_states(_OTHER_FOLDER) == [state(_id(3), None, _OID, "running")]
    assert store.check_run_states(_id(9)) == []


def test_count_running_check_runs_counts_only_the_named_running_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    store.insert_check(_check(), limit=32)
    store.insert_check(_check(_id(2)), limit=32)
    for run in (1, 2, 3):
        store.insert_check_run(_run(run), keep=20)
    store.insert_check_run(_run(4, check_id=_id(2)), keep=20)  # running, but not named
    assert _finish(store, 3) is True
    assert store.count_running_check_runs([_id(1), _id(2), _id(3), _id(9)]) == 2
    assert store.count_running_check_runs([_id(3)]) == 0  # ended
    monkeypatch.setattr(store, "_connection", _forbidden_connection)
    assert store.count_running_check_runs([]) == 0  # no id opens no connection


def _forbidden_connection(*_args: object, **_kwargs: object) -> Any:
    raise AssertionError("opened the store")


def test_interrupt_check_runs_ends_every_running_row(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.insert_check(_check(), limit=32)
    store.insert_check_run(_run(1), keep=20)
    store.insert_check_run(_run(2), keep=20)
    store.insert_check_run(_run(3), keep=20)
    assert _finish(store, 3) is True
    assert store.interrupt_check_runs(_ENDED, keep=20) == 2
    for run in (1, 2):
        assert store.get_check_run(_id(run)) == {
            **_run(run),
            "outcome": "interrupted",
            "ended_at": _ENDED,
        }
    finished = store.get_check_run(_id(3))
    assert finished is not None and finished["outcome"] == "failed"
    assert store.interrupt_check_runs(_ENDED, keep=20) == 0


def test_interrupt_check_runs_trims_every_checks_finished_runs_to_keep(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.insert_check(_check(), limit=32)
    store.insert_check(_check(_id(2)), limit=32)
    for run in (1, 2, 3):
        store.insert_check_run(_run(run), keep=20)
        assert _finish(store, run) is True
    store.insert_check_run(_run(4), keep=20)
    store.insert_check_run(_run(5), keep=20)
    for run in (50, 51, 52):
        store.insert_check_run(_run(run, check_id=_id(2)), keep=20)
        assert _finish(store, run) is True
    assert store.interrupt_check_runs(_ENDED, keep=2) == 2
    assert _ids(store) == [_id(5), _id(4)]
    assert _ids(store, _id(2)) == [_id(52), _id(51)]
