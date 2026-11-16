"""A Bot copy no session ran in frees its owner: the `bound` binding of a deleted `creating` row
goes with the row, and a finished removal (`removing` to `removed`) drops the binding of a copy
whose chat, or every member of whose Bot thread, never started natively. A started owner, a
`released` or a `former` binding stays. Synthetic, current-format SQLite store."""

from __future__ import annotations

from tests.persistence.test_storage_bot_code import _id
from tests.persistence.test_storage_bot_code_queries import (
    STAMP,
    _copy,
    _session,
    _thread,
    _worktree,
)
from tests.persistence.test_storage_bot_code_queries import store as store
from viki_agent import storage


def _states(store: storage.SessionStore, *numbers: int) -> list[str | None]:
    found = store.copy_states([_id(number) for number in numbers])
    return [found[_id(n)]["state"] if _id(n) in found else None for n in numbers]


def _release(store: storage.SessionStore, number: int) -> None:
    with store._connection(write=True) as connection:
        storage.SessionStore._release_copies(connection, [_id(number)])


def test_a_deleted_creating_row_takes_its_bound_binding_only(store: storage.SessionStore) -> None:
    _session(store, 1)  # started, yet never in this copy: a `creating` row was never ready
    _session(store, 3)
    store.insert_bot_copy(_worktree(200), _copy(200, session=1), limit=32)
    store.insert_bot_copy(_worktree(201), _copy(201, session=3), limit=32)
    _release(store, 201)
    store.delete_worktree(_id(200), expected_state="creating")
    store.delete_worktree(_id(201), expected_state="creating")
    assert _states(store, 200, 201) == [None, "released"]


def test_a_finished_removal_frees_only_an_owner_that_never_started(
    store: storage.SessionStore,
) -> None:
    for number in (1, 2, 3, 4, 6, 8, 10, 12):
        _session(store, number)  # odd numbers started natively
    _thread(store, 100, [_id(4), _id(6)])
    _thread(store, 101, [_id(8), _id(3)])
    owners = {
        200: {"session": 2},
        201: {"session": 1},
        202: {"thread": 100},
        203: {"thread": 101},
        204: {"session": 10},
        205: {"session": 12},
    }
    for number, owner in owners.items():
        store.insert_bot_copy(_worktree(number), _copy(number, **owner), limit=32)
        store.update_worktree_state(_id(number), "creating", "ready", STAMP)
        store.update_worktree_state(_id(number), "ready", "removing", STAMP)
    _release(store, 204)
    _release(store, 205)
    assert store.finish_release(_id(205))
    for number in owners:
        store.update_worktree_state(_id(number), "removing", "removed", STAMP)
    assert _states(store, *owners) == [None, "bound", None, "bound", "released", "former"]
