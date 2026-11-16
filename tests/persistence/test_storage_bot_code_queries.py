"""Bot-copy queries and owner release on synthetic, current-format SQLite stores."""

from __future__ import annotations

import contextlib
import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from tests.messaging.test_bots import _bind_chat, _bot
from tests.messaging.test_bots import service as service
from tests.persistence.test_storage import _label, _workspace
from tests.persistence.test_storage_bot_code import _binding, _id, _insert
from tests.profiles.test_profile_launch import setup as setup
from viki_agent import storage
from viki_agent.automation import tasks
from viki_agent.conversations.session_metadata import SessionMetadataService
from viki_agent.messaging.bots import BotService
from viki_agent.profiles.profile_launch import MAX_BINDINGS
from viki_agent.profiles.service import ProfileError

STAMP = "2026-11-11T00:00:00.000Z"
CODE, VAULT, OTHER_VAULT, BOT = _id(900), _id(901), _id(902), _id(903)
OWNER, DEAD = _id(904), _id(905)
HANG_GUARD_S = 10


def _scope(workspace_id: str) -> str:
    return hashlib.sha256(_workspace(workspace_id)["canonical_key"].encode("utf-8")).hexdigest()


@pytest.fixture
def store(tmp_path: Path) -> storage.SessionStore:
    result = storage.SessionStore(tmp_path / "app.sqlite3", lambda: {})
    with result._connection(write=True) as connection:
        for workspace_id in (CODE, VAULT, OTHER_VAULT):
            _insert(
                connection,
                "workspaces",
                _workspace(workspace_id, kind="code" if workspace_id == CODE else "vault"),
            )
        _insert(
            connection,
            "bots",
            {
                "bot_id": BOT,
                "profile_id": _id(20001),
                "workspace_id": VAULT,
                "chat_session_id": _id(1),
                "request_hash": "a" * 64,
                "state_revision": 1,
                "created_at": STAMP,
                "updated_at": STAMP,
            },
        )
        connection.execute("INSERT INTO bot_code_folders VALUES (?,?,?)", (BOT, CODE, STAMP))
    return result


def _session(
    store: storage.SessionStore,
    number: int,
    workspace_id: str = VAULT,
    origin: str = "interactive",
) -> str:
    session_id = _id(number)
    with store._connection(write=True) as connection:
        connection.execute(
            "INSERT INTO sessions VALUES (?,'plan','high',NULL,'profile')", (session_id,)
        )
        _binding(connection, number, workspace_id=workspace_id, launch_origin=origin)
        connection.execute("INSERT INTO session_bot_memory VALUES (?,?)", (session_id, BOT))
    return session_id


def _worktree(number: int, state: str = "creating") -> storage.WorktreeRow:
    return {
        "worktree_id": _id(number),
        "workspace_id": CODE,
        "path": f"C:/Synthetic/worktrees/{_id(number)}",
        "branch": f"viki/bot/{number}",
        "base_oid": "a" * 40,
        "state": state,
        "created_at": STAMP,
        "updated_at": STAMP,
    }


def _copy(
    number: int, *, session: int | None = None, thread: int | None = None
) -> storage.BotCodeCopyRow:
    return {
        "worktree_id": _id(number),
        "workspace_id": CODE,
        "bot_id": BOT,
        "session_id": _id(session) if session is not None else None,
        "thread_id": _id(thread) if thread is not None else None,
        "state": "bound",
        "created_at": STAMP,
    }


def _seed_copy(
    store: storage.SessionStore,
    number: int,
    *,
    session: int | None = None,
    thread: int | None = None,
) -> None:
    with store._connection(write=True) as connection:
        _insert(connection, "worktrees", _worktree(number, "ready"))
        _insert(connection, "bot_code_copies", dict(_copy(number, session=session, thread=thread)))


def _thread(
    store: storage.SessionStore, number: int, members: list[str], workspace: str = VAULT
) -> None:
    with store._connection(write=True) as connection:
        connection.execute(
            "INSERT INTO bot_threads VALUES (?,?,1,?,1,?,?,?)",
            (_id(number), workspace, b"sealed", "a" * 64, STAMP, STAMP),
        )
        connection.executemany(
            "INSERT INTO bot_thread_sessions VALUES (?,?)", [(_id(number), sid) for sid in members]
        )


def _command(
    store: storage.SessionStore,
    number: int,
    session: int,
    *,
    scope: str | None = None,
    state: str = "unknown",
    owner: str = OWNER,
) -> None:
    with store._connection(write=True) as connection:
        connection.execute(
            "INSERT INTO commands VALUES (?,?,?,?,?,1,?,?,?,'',?,1,1,?,?)",
            (
                scope or _scope(VAULT),
                _id(number),
                _id(session),
                _id(910),
                _id(number + 1000),
                "c" * 64,
                owner,
                state,
                b"x",
                STAMP,
                STAMP,
            ),
        )


def _states(store: storage.SessionStore) -> dict[str, str]:
    with store._connection() as connection:
        return dict(connection.execute("SELECT worktree_id,state FROM bot_code_copies"))


def test_delete_label_releases_session_and_its_shared_group_only(
    store: storage.SessionStore,
) -> None:
    first, second, foreign = (
        _session(store, n, OTHER_VAULT if n == 3 else VAULT) for n in (1, 2, 3)
    )
    _thread(store, 100, [first, second])
    _thread(store, 101, [first, foreign], OTHER_VAULT)
    _seed_copy(store, 200, session=1)
    _seed_copy(store, 201, thread=100)
    _seed_copy(store, 202, thread=101)
    _seed_copy(store, 203, session=2)
    store.update_session_metadata(_scope(VAULT), first, lambda _: _label(first))
    assert store.delete_session_metadata(_scope(VAULT), first, lambda _: None)
    assert _states(store) == {
        _id(200): "released",
        _id(201): "released",
        _id(202): "bound",
        _id(203): "bound",
    }
    with store._connection() as connection:
        assert connection.execute("SELECT thread_id FROM bot_threads").fetchall() == [(_id(101),)]
        assert connection.execute("SELECT session_id FROM bot_thread_sessions").fetchall() == [
            (first,),
            (foreign,),
        ]
    assert store.get(first) is not None


def test_wrong_scope_and_archive_patch_keep_binding(store: storage.SessionStore) -> None:
    sid = _session(store, 1)
    _seed_copy(store, 200, session=1)
    labels = SessionMetadataService(store, _scope(VAULT))
    labels.patch(sid, 0, {"archived": True})
    assert not store.delete_session_metadata(_scope(OTHER_VAULT), sid, lambda _: None)
    assert _states(store) == {_id(200): "bound"}
    assert store.profile_launch_origin(sid) == "interactive"
    assert labels.delete(sid, 1)
    assert _states(store) == {_id(200): "released"}
    assert store.profile_launch_origin(sid) == "interactive"


def test_deleting_unlabelled_chat_still_releases_its_copy(store: storage.SessionStore) -> None:
    sid = _session(store, 1)
    _seed_copy(store, 200, session=1)
    assert not store.delete_session_metadata(_scope(VAULT), sid, lambda _: None)
    assert _states(store) == {_id(200): "released"}


def test_command_reset_respects_scope_and_inflight_sessions_and_groups(
    store: storage.SessionStore,
) -> None:
    for n in range(1, 8):
        _session(store, n, OTHER_VAULT if n == 7 else VAULT)
    _thread(store, 100, [_id(1), _id(2)])
    _thread(store, 101, [_id(3), _id(4)])
    _thread(store, 102, [_id(7)], OTHER_VAULT)
    for n, member in ((200, 1), (201, 5), (202, 6), (203, 7)):
        _seed_copy(store, n, session=member)
    for n, group in ((204, 100), (205, 101), (206, 102)):
        _seed_copy(store, n, thread=group)
    _command(store, 300, 1)
    _command(store, 301, 2, state="accepted")
    _command(store, 302, 3, state="dispatching", owner=DEAD)
    _command(store, 303, 4)
    _command(store, 304, 5)
    _command(store, 305, 6)
    _command(store, 306, 6, state="dispatching")
    _command(store, 307, 7, scope=_scope(OTHER_VAULT))
    # A matching session id alone cannot give this scope authority over another vault's group.
    _command(store, 308, 7)
    assert store.delete_scope_rows("commands", _scope(VAULT), OWNER) == (6, 2)
    assert _states(store) == {
        _id(200): "released",
        _id(201): "released",
        _id(202): "bound",
        _id(203): "bound",
        _id(204): "bound",
        _id(205): "released",
        _id(206): "bound",
    }
    with store._connection() as connection:
        assert connection.execute(
            "SELECT thread_id FROM bot_threads ORDER BY thread_id"
        ).fetchall() == [(_id(100),), (_id(102),)]
        assert connection.execute("SELECT count(*) FROM commands").fetchone() == (3,)


@pytest.mark.parametrize("table", ["drafts", "approvals"])
def test_other_resets_do_not_release_copies(store: storage.SessionStore, table: Any) -> None:
    _session(store, 1)
    _seed_copy(store, 200, session=1)
    if table == "drafts":
        with store._connection(write=True) as connection:
            connection.execute(
                "INSERT INTO drafts VALUES (?,?,1,?,1,1,?)", (_scope(VAULT), _id(1), b"x", STAMP)
            )
    assert store.delete_scope_rows(table, _scope(VAULT), OWNER) == (
        (1, 0) if table == "drafts" else (0, 0)
    )
    assert _states(store) == {_id(200): "bound"}
    assert store.profile_launch_origin(_id(1)) == "interactive"


@pytest.mark.parametrize("workspace", [CODE, VAULT])
def test_registration_removal_releases_exact_copies_and_drops_folder_state(
    store: storage.SessionStore, workspace: str
) -> None:
    _session(store, 1)
    _session(store, 2, OTHER_VAULT)
    _thread(store, 100, [_id(1)])
    _thread(store, 101, [_id(2)], OTHER_VAULT)
    _seed_copy(store, 200, session=1)
    _seed_copy(store, 201, thread=100)
    _seed_copy(store, 202, session=2)
    _seed_copy(store, 203, thread=101)
    with store._connection(write=True) as connection:
        connection.execute("INSERT INTO bot_code_baselines VALUES (?,?,?)", (CODE, "a" * 64, STAMP))
    assert store.update_workspace(workspace, lambda _current, _rows: None) is None
    assert _states(store) == {
        _id(200): "released",
        _id(201): "released",
        _id(202): "released" if workspace == CODE else "bound",
        _id(203): "released" if workspace == CODE else "bound",
    }
    assert (store.bot_code_folder(BOT) is None) == (workspace == CODE)
    assert (store.bot_code_baseline(CODE) is None) == (workspace == CODE)
    assert store.profile_launch_origin(_id(1)) == "interactive"


@pytest.mark.parametrize("operation", ["label", "commands", "registration"])
def test_release_and_membership_deletion_roll_back_together(
    store: storage.SessionStore, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    sid = _session(store, 1)
    _thread(store, 100, [sid])
    _seed_copy(store, 200, session=1)
    _seed_copy(store, 201, thread=100)
    store.update_session_metadata(_scope(VAULT), sid, lambda _: _label(sid))
    _command(store, 300, 1)
    original = store._connection
    seen: list[str] = []

    @contextlib.contextmanager
    def fail_before_commit(**kwargs: Any):
        with original(**kwargs) as connection:
            if kwargs.get("write"):
                connection.set_trace_callback(seen.append)
            yield connection
            if kwargs.get("write"):
                assert dict(
                    connection.execute("SELECT worktree_id,state FROM bot_code_copies")
                ) == {_id(200): "released", _id(201): "released"}
                assert connection.execute(
                    "SELECT count(*) FROM bot_thread_sessions"
                ).fetchone() == (0,)
                with original() as observer:
                    assert dict(
                        observer.execute("SELECT worktree_id,state FROM bot_code_copies")
                    ) == {_id(200): "bound", _id(201): "bound"}
                    assert observer.execute(
                        "SELECT count(*) FROM bot_thread_sessions"
                    ).fetchone() == (1,)
                raise RuntimeError("synthetic commit failure")

    monkeypatch.setattr(store, "_connection", fail_before_commit)
    with pytest.raises(RuntimeError, match="synthetic commit failure"):
        if operation == "label":
            store.delete_session_metadata(_scope(VAULT), sid, lambda _: None)
        elif operation == "commands":
            store.delete_scope_rows("commands", _scope(VAULT), OWNER)
        else:
            store.update_workspace(VAULT, lambda _current, _rows: None)
    monkeypatch.setattr(store, "_connection", original)
    assert _states(store) == {_id(200): "bound", _id(201): "bound"}
    with store._connection() as connection:
        assert connection.execute("SELECT count(*) FROM bot_thread_sessions").fetchone() == (1,)
        assert connection.execute("SELECT count(*) FROM commands").fetchone() == (1,)
    assert store.get_workspace(VAULT) is not None
    assert store.get_session_metadata(_scope(VAULT), sid) is not None
    release = next(i for i, sql in enumerate(seen) if sql.startswith("UPDATE bot_code_copies"))
    erase = next(
        i for i, sql in enumerate(seen) if sql.startswith("DELETE FROM bot_thread_sessions")
    )
    assert release < erase


def test_copy_queries_union_roots_tasks_and_all_thread_sessions(
    store: storage.SessionStore,
) -> None:
    for n in (1, 2, 3, 4):
        _session(store, n)
    _thread(store, 100, [_id(2), _id(3), _id(4)])
    _seed_copy(store, 200, session=1)
    _seed_copy(store, 201, thread=100)
    with store._connection(write=True) as connection:
        task_store = tasks.TaskStore(store, _scope(VAULT), OWNER)
        attempt = task_store.insert_claim(
            connection,
            routine_id=_id(110),
            kind="manual",
            revision=1,
            now=STAMP,
            trigger_id=_id(111),
            request_hash="a" * 64,
        )
        connection.execute(
            "UPDATE task_attempts SET session_id=? WHERE attempt_id=?", (_id(1), attempt.attempt_id)
        )
    store.put_session_root(
        {
            "session_id": _id(2),
            "workspace_id": CODE,
            "worktree_id": _id(200),
            "display_root": "C:/Synthetic",
            "created_at": STAMP,
        }
    )
    assert store.sessions_using_worktree(_id(200)) == [_id(1), _id(2)]
    assert store.sessions_using_worktree(_id(201)) == [_id(2), _id(3), _id(4)]
    assert store.session_roots_in_worktree(_id(200)) == [_id(2)]
    assert store.bound_copy(_id(1)) == _copy(200, session=1)
    assert store.bound_thread_copy(_id(100), CODE) == _copy(201, thread=100)
    assert store.bound_thread_copy(_id(100), VAULT) is None
    assert store.copy_states([_id(200), _id(201), _id(999)]) == {
        _id(200): _copy(200, session=1),
        _id(201): _copy(201, thread=100),
    }
    assert store.copy_states([]) == {}
    assert store.released_copies() == []
    assert not store.finish_release(_id(200))
    baseline: storage.BotCodeBaselineRow = {
        "workspace_id": CODE,
        "git_fingerprint": "a" * 64,
        "created_at": STAMP,
    }
    assert store.insert_bot_code_baseline(baseline)
    store.delete_session_metadata(_scope(VAULT), _id(1), lambda _: None)
    assert store.bound_copy(_id(1)) is None
    assert store.released_copies() == [{**_copy(200, session=1), "state": "released"}]
    assert store.sessions_using_worktree(_id(200)) == [_id(2)]
    assert store.finish_release(_id(200))
    assert not store.finish_release(_id(200))
    # Kept as `former`, its Bot id kept: never pending again, still a Bot copy by worktree id.
    former = {**_copy(200, session=1), "state": "former"}
    assert store.copy_states([_id(200)]) == {_id(200): former}
    assert store.released_copies() == []
    assert store.sessions_using_worktree(_id(200)) == [_id(2)]
    assert store.get_worktree(_id(200)) is not None
    assert store.bot_code_baseline(CODE) == baseline
    assert store.bot_code_folder(BOT) == {"bot_id": BOT, "workspace_id": CODE, "created_at": STAMP}
    assert store.bot_code_folder(_id(999)) is None


def test_state_queries_do_not_lose_old_rows_behind_removed_history(
    store: storage.SessionStore,
) -> None:
    _session(store, 1)
    _seed_copy(store, 200, session=1)
    with store._connection(write=True) as connection:
        for n in range(5000, 9200):
            row = {**_worktree(n, "removed"), "created_at": "2026-11-12T00:00:00.000Z"}
            _insert(connection, "worktrees", row)
    assert store.worktrees_in_states(["ready"]) == [_worktree(200, "ready")]
    assert store.worktrees_in_states([]) == []
    assert store.worktree_by_branch(CODE, "viki/bot/200") == _worktree(200, "ready")
    assert store.worktree_by_branch(VAULT, "viki/bot/200") is None
    assert store.worktree_by_branch(CODE, "viki/bot/5000") is None
    assert store.list_worktrees(1) == ([_worktree(200, "ready")], False)
    store.delete_session_metadata(_scope(VAULT), _id(1), lambda _: None)
    assert store.released_copies() == [{**_copy(200, session=1), "state": "released"}]


def test_branch_lookup_refuses_ambiguous_active_mapping(store: storage.SessionStore) -> None:
    with store._connection(write=True) as connection:
        _insert(connection, "worktrees", _worktree(200, "ready"))
        _insert(connection, "worktrees", {**_worktree(201, "kept"), "branch": "viki/bot/200"})
    with pytest.raises(storage.StateError) as failure:
        store.worktree_by_branch(CODE, "viki/bot/200")
    assert failure.value.code == "conflict"


def _race(store: storage.SessionStore, action: Any) -> list[Any]:
    # Separate store instances and connections; both callers reach the write together.
    barrier = threading.Barrier(2)
    stores = [storage.SessionStore(store.path, lambda: {}) for _ in range(2)]

    def run(index: int) -> Any:
        barrier.wait(timeout=HANG_GUARD_S)
        try:
            return action(stores[index], index)
        except storage.StateError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        return list(pool.map(run, range(2)))


def test_atomic_copy_insert_shares_last_slot_with_ordinary_worktrees(
    store: storage.SessionStore,
) -> None:
    _session(store, 1)
    for n in range(31):
        store.insert_worktree(_worktree(1000 + n), limit=32)

    def insert(candidate: storage.SessionStore, index: int) -> str:
        if index:
            candidate.insert_worktree(_worktree(201), limit=32)
        else:
            candidate.insert_bot_copy(_worktree(200), _copy(200, session=1), limit=32)
        return "inserted"

    results = _race(store, insert)
    assert sorted(results) == ["capacity", "inserted"]
    with store._connection() as connection:
        assert connection.execute("SELECT count(*) FROM worktrees").fetchone() == (32,)
        assert connection.execute("SELECT count(*) FROM bot_code_copies").fetchone() == (
            int(results[0] == "inserted"),
        )


@pytest.mark.parametrize("target", ["session", "thread"])
def test_atomic_copy_insert_conflicts_on_bound_key_without_orphan(
    store: storage.SessionStore, target: str
) -> None:
    _session(store, 1)
    _thread(store, 100, [_id(1)])

    def insert(candidate: storage.SessionStore, index: int) -> str:
        binding = _copy(200 + index, **({"session": 1} if target == "session" else {"thread": 100}))
        candidate.insert_bot_copy(_worktree(200 + index), binding, limit=32)
        return "inserted"

    assert sorted(_race(store, insert)) == ["conflict", "inserted"]
    with store._connection() as connection:
        assert connection.execute("SELECT count(*) FROM worktrees").fetchone() == (1,)
        assert connection.execute("SELECT count(*) FROM bot_code_copies").fetchone() == (1,)


def test_failed_binding_insert_rolls_back_worktree(store: storage.SessionStore) -> None:
    binding = _copy(200, session=1)
    binding["created_at"] = "invalid"
    with pytest.raises(storage.StateError) as failure:
        store.insert_bot_copy(_worktree(200), binding, limit=32)
    assert failure.value.code == "invalid_input"
    assert store.get_worktree(_id(200)) is None


def test_a_former_binding_is_never_inserted_and_frees_its_session(
    store: storage.SessionStore,
) -> None:
    _session(store, 1)
    binding: storage.BotCodeCopyRow = {**_copy(200, session=1), "state": "former"}
    with pytest.raises(storage.StateError) as failure:
        store.insert_bot_copy(_worktree(200), binding, limit=32)
    assert failure.value.code == "invalid_input"
    assert store.get_worktree(_id(200)) is None
    store.insert_bot_copy(_worktree(200), _copy(200, session=1), limit=32)
    store.delete_session_metadata(_scope(VAULT), _id(1), lambda _: None)
    assert store.finish_release(_id(200))
    # The session may be bound to a new copy; the former one keeps its worktree id. Its Bot's
    # later deletion or a thread release touches only bound rows.
    store.insert_bot_copy(_worktree(201), _copy(201, session=1), limit=32)
    assert store.bound_copy(_id(1)) == _copy(201, session=1)
    with store._connection(write=True) as connection:
        storage.SessionStore._release_copies(connection, [_id(200), _id(201)])
    assert store.released_copies() == [{**_copy(201, session=1), "state": "released"}]
    assert store.copy_states([_id(200)])[_id(200)]["state"] == "former"
    with pytest.raises(storage.StateError) as conflict:
        store.insert_bot_copy(_worktree(200), _copy(200, session=1), limit=32)
    assert conflict.value.code == "conflict"


def test_baseline_insert_and_cas_are_atomic_across_connections(store: storage.SessionStore) -> None:
    def insert(candidate: storage.SessionStore, index: int) -> bool:
        return candidate.insert_bot_code_baseline(
            {"workspace_id": CODE, "git_fingerprint": str(index) * 64, "created_at": STAMP}
        )

    assert sorted(_race(store, insert)) == [False, True]
    baseline = store.bot_code_baseline(CODE)
    assert baseline is not None
    fingerprint = baseline["git_fingerprint"]

    def replace(candidate: storage.SessionStore, index: int) -> bool:
        return candidate.replace_bot_code_baseline(CODE, fingerprint, str(index + 2) * 64, STAMP)

    assert sorted(_race(store, replace)) == [False, True]
    after = store.bot_code_baseline(CODE)
    assert after is not None and after["git_fingerprint"] in {"2" * 64, "3" * 64}
    assert not store.replace_bot_code_baseline(VAULT, "a" * 64, "b" * 64, STAMP)
    with pytest.raises(storage.StateError):
        store.replace_bot_code_baseline(CODE, after["git_fingerprint"], "invalid", STAMP)
    assert store.bot_code_baseline(CODE) == after


@pytest.mark.parametrize("origin", ["unknown", "interactive", "routine"])
def test_launch_origin_is_only_the_binding_value_and_dies_only_with_binding(
    store: storage.SessionStore, monkeypatch: pytest.MonkeyPatch, origin: str
) -> None:
    sid = _session(store, 1, origin=origin)
    assert store.profile_launch_origin(sid) == origin
    assert store.profile_launch_origin(_id(999)) is None
    with store._connection(write=True) as connection:
        connection.execute("DELETE FROM bots WHERE bot_id=?", (BOT,))
        connection.execute(
            "UPDATE session_profile_bindings SET native_started=1 WHERE session_id=?", (sid,)
        )
    store.delete_settings(lambda _: None)
    store.delete_scope_rows("drafts", _scope(VAULT), OWNER)
    store.delete_scope_rows("commands", _scope(VAULT), OWNER)
    store.delete_scope_rows("approvals", _scope(VAULT), OWNER)
    store.delete_session_metadata(_scope(VAULT), sid, lambda _: None)
    assert store.profile_launch_origin(sid) == origin
    task_store = tasks.TaskStore(store, _scope(VAULT), OWNER)
    with store._connection(write=True) as connection:
        attempt = task_store.insert_claim(
            connection,
            routine_id=_id(100),
            kind="manual",
            revision=1,
            now=STAMP,
            trigger_id=_id(101),
            request_hash="a" * 64,
        )
        connection.execute(
            "UPDATE task_attempts SET session_id=?,state='done',outcome='expired',finished_at=? "
            "WHERE attempt_id=?",
            (sid, STAMP, attempt.attempt_id),
        )
        monkeypatch.setattr(tasks, "MAX_OCCURRENCES", 0)
        task_store.prune(connection, _id(100))
    with store._connection() as connection:
        assert connection.execute("SELECT count(*) FROM task_attempts").fetchone() == (0,)
    assert store.profile_launch_origin(sid) == origin
    with store._connection(write=True) as connection:
        connection.execute("DELETE FROM session_profile_bindings WHERE session_id=?", (sid,))
    assert store.profile_launch_origin(sid) is None


def test_origin_read_has_no_inference_or_write(
    store: storage.SessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    sid = _session(store, 1, origin="unknown")
    original = store._connection
    statements: list[str] = []

    @contextlib.contextmanager
    def traced(**kwargs: Any):
        assert not kwargs.get("write")
        with original(**kwargs) as connection:
            connection.set_trace_callback(statements.append)
            yield connection

    monkeypatch.setattr(store, "_connection", traced)
    assert store.profile_launch_origin(sid) == "unknown"
    assert statements == [
        f"SELECT launch_origin FROM session_profile_bindings WHERE session_id='{sid}'"
    ]


def test_origin_stays_within_existing_binding_capacity(setup: Any) -> None:
    launch, state, _vault, profile_id, workspace_id = setup
    assert MAX_BINDINGS == 4096
    with state.store._connection(write=True) as connection:
        for number in range(1, MAX_BINDINGS + 1):
            connection.execute(
                "INSERT INTO sessions VALUES (?,'plan','high',NULL,'profile')", (_id(number),)
            )
            _binding(
                connection,
                number,
                profile_id=profile_id,
                workspace_id=workspace_id,
                launch_origin="routine" if number % 2 else "interactive",
            )
    assert state.store.profile_launch_origin(_id(1)) == "routine"
    assert state.store.profile_launch_origin(_id(MAX_BINDINGS)) == "interactive"
    with pytest.raises(ProfileError, match="capacity"):
        launch.prepare(profile_id, _id(50000), _id(50001), 1, workspace_id)
    assert state.store.profile_launch_origin(_id(50001)) is None
    with state.store._connection() as connection:
        assert connection.execute("SELECT count(*) FROM session_profile_bindings").fetchone() == (
            MAX_BINDINGS,
        )


@pytest.mark.parametrize("operation", ["remove", "reset"])
def test_existing_bot_lifecycle_keeps_binding_provenance(
    service: BotService, operation: str
) -> None:
    bot = _bot(service)
    _bind_chat(service, bot)
    sid = bot["chat_session_id"]
    with service.store._connection(write=True) as connection:
        connection.execute(
            "UPDATE session_profile_bindings SET launch_origin='routine' WHERE session_id=?", (sid,)
        )
    if operation == "remove":
        service.remove(bot["bot_id"], bot["state_revision"])
    else:
        service.reset(bot["workspace_id"])
    assert service.store.profile_launch_origin(sid) == "routine"
    assert service.store.get(sid) is not None
