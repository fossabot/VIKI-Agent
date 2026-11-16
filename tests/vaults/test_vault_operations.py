from __future__ import annotations

import dataclasses
import hashlib
import logging
import os
import shutil
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from viki_agent import protection
from viki_agent.persistence.feature_schemas import OPERATION_SCHEMAS
from viki_agent.storage import StateError
from viki_agent.vaults.vault_mutation import MAX_PUBLICATION_BYTES, OrdinaryMutationRoot
from viki_agent.vaults.vault_operations import (
    MAX_LIVE_OPERATIONS,
    MAX_RECOVERY_BYTES,
    MAX_RECOVERY_ENTRIES,
    RECOVERY_DAYS,
    OperationJournal,
    OperationStep,
    RecoveryEntry,
    RecoveryStore,
    _stamp,
)
from viki_agent.vaults.vault_publication import PublicationError

WS = "00000000-0000-4000-8000-0000000000a1"
OTHER_WS = "00000000-0000-4000-8000-0000000000a2"
H = "a" * 64
REQUEST = "b" * 64
OTHER_REQUEST = "c" * 64


def _id(n: int) -> str:
    return f"00000000-0000-4000-8000-{n:012x}"


class _Store:
    """The two frozen schema-12 tables behind `SessionStore._connection`'s shape."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.fail_commit = False
        with sqlite3.connect(path) as connection:
            for ddl in OPERATION_SCHEMAS.values():
                connection.execute(ddl)

    @contextmanager
    def _connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, isolation_level=None)
        try:
            if write:
                connection.execute("BEGIN IMMEDIATE")
            yield connection
            if write:
                if self.fail_commit:
                    # The store's busy wait ran out at the commit, which rolls the write back.
                    raise StateError("busy")
                connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()


def _journal(tmp_path: Path) -> OperationJournal:
    return OperationJournal(cast(Any, _Store(tmp_path / "journal.sqlite3")))


def _recovery(tmp_path: Path) -> tuple[RecoveryStore, Path]:
    tmp_path.mkdir(exist_ok=True)
    data = tmp_path / "data"
    assert protection.create_private_directory(data)
    root = data / "vault-recovery"
    return RecoveryStore(root, _journal(tmp_path)), root


def _rows(store: RecoveryStore) -> list[tuple[object, ...]]:
    with cast(Any, store)._store._connection() as connection:
        return connection.execute("SELECT * FROM vault_recovery ORDER BY recovery_id").fetchall()


def _root(workspace_id: str = WS) -> OrdinaryMutationRoot:
    # The journal records the root's identity only; it never touches the path.
    return OrdinaryMutationRoot(workspace_id, Path("vault"), "vault", "1:2", 7, None)


def _remove(ref: str, n: int = 0) -> OperationStep:
    return OperationStep(n, "remove", ref, None, H, None, None)


def _mkdir(ref: str, n: int = 0) -> OperationStep:
    return OperationStep(n, "mkdir", ref, None, None, None, None)


def _move(ref: str, to: str, n: int = 0) -> OperationStep:
    return OperationStep(n, "move", ref, to, H, H, None)


def _refusal(code: str) -> Any:
    return pytest.raises(PublicationError, match=f"^{code}$")


# ---------------------------------------------------------------- the operation journal


def test_one_live_row_holds_every_path_its_steps_name_case_folded(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    row = journal.plan(
        _id(1), _root(), "move", (_mkdir("b"), _move("a/n.md", "b/n.md", 1)), REQUEST
    )
    assert row.state == "planned" and row.kind == "move" and row.workspace_id == WS
    assert row.steps == (_mkdir("b"), _move("a/n.md", "b/n.md", 1))
    assert journal.get(_id(1)) == row

    # Either end of the move, and the folder it creates, is held, in any letter case.
    for n, step in enumerate((_remove("A/N.md"), _remove("b/N.MD"), _mkdir("B")), start=2):
        with _refusal("operation_busy"):
            journal.plan(_id(n), _root(), "delete", (step,), REQUEST)

    # Another workspace's same path, and an unrelated path here, are free.
    journal.plan(_id(8), _root(OTHER_WS), "delete", (_remove("a/n.md"),), REQUEST)
    journal.plan(_id(9), _root(), "delete", (_remove("a/other.md"),), REQUEST)
    # A case-only rename names one file twice inside its own step, and that is not a clash.
    journal.plan(_id(10), _root(), "rename", (_move("c/n.md", "c/N.md"),), REQUEST)
    assert [r.operation_id for r in journal.list_pending()] == [_id(1), _id(8), _id(9), _id(10)]

    # The path is free again once its row is gone.
    journal.transition(_id(1), "planned", "failed")
    journal.delete(_id(1), "failed")
    journal.plan(_id(11), _root(), "delete", (_remove("a/N.md"),), REQUEST)


def test_the_same_command_replays_its_row_and_a_rewrite_conflicts(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    steps = (_remove("n.md"),)
    row = journal.plan(_id(1), _root(), "delete", steps, REQUEST)
    assert journal.plan(_id(1), _root(), "delete", steps, REQUEST) == row
    applying = journal.transition(_id(1), "planned", "applying")
    # The replay answers the row as it stands now, not a second plan.
    assert journal.plan(_id(1), _root(), "delete", steps, REQUEST) == applying

    for root, request in ((_root(), OTHER_REQUEST), (_root(OTHER_WS), REQUEST)):
        with pytest.raises(PublicationError) as failure:
            journal.plan(_id(1), root, "delete", steps, request)
        assert failure.value.code == "command_conflict"
        assert failure.value.operation_id == _id(1)
    assert journal.list_pending() == [applying]


def test_the_live_rows_are_capped_across_every_workspace(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    for n in range(MAX_LIVE_OPERATIONS):
        root = _root(WS if n % 2 else OTHER_WS)
        journal.plan(_id(n + 1), root, "delete", (_remove(f"n{n}.md"),), REQUEST)
    with _refusal("operation_limit"):
        journal.plan(_id(100), _root(), "delete", (_remove("last.md"),), REQUEST)
    assert len(journal.list_pending()) == MAX_LIVE_OPERATIONS


@pytest.mark.parametrize(
    "steps",
    [
        (),
        (_remove("n.md", n=1),),
        (OperationStep(0, cast(Any, "copy"), "n.md", None, H, None, None),),
        (OperationStep(0, "move", "n.md", None, H, H, None),),
        (OperationStep(0, "remove", "n.md", "m.md", H, None, None),),
        (OperationStep(cast(Any, True), "remove", "n.md", None, H, None, None),),
        (_remove("n.txt"),),
        (_remove("a//n.md"),),
        (_remove("a/./n.md"),),
        (_remove("/n.md"),),
        (_remove("a\\n.md"),),
        (_remove("c:n.md"),),
        (_remove("../n.md"),),
        (_remove("a/n.md/"),),
        (_remove(".git/n.md"),),
        (_remove("Claude.MD"),),
        (_remove("a/.viki-stage-x.md"),),
        (_mkdir("a/.git"),),
        (_move("n.md", "b/../n.md"),),
        (OperationStep(0, "remove", "n.md", None, "A" * 64, None, None),),
        (OperationStep(0, "remove", "n.md", None, H[:-1], None, None),),
        (OperationStep(0, "create", "n.md", None, None, H, "not-a-uuid"),),
        (OperationStep(0, "create", "n.md", None, None, H, _id(0xAB).upper()),),
        (_mkdir("a"), _mkdir("A", 1)),
        (_mkdir("a"), _move("b/n.md", "A", 1)),
        tuple(_mkdir(f"{n:02d}" + "x" * 240, n) for n in range(17)),
    ],
)
def test_an_invalid_step_list_is_refused_and_records_nothing(
    tmp_path: Path, steps: tuple[OperationStep, ...]
) -> None:
    journal = _journal(tmp_path)
    with _refusal("invalid_operation"):
        journal.plan(_id(1), _root(), "delete", steps, REQUEST)
    assert journal.list_pending() == []


@pytest.mark.parametrize(
    ("operation_id", "workspace_id", "kind", "request_sha256"),
    [
        (_id(0xAB).upper(), WS, "delete", REQUEST),
        (_id(1), "not-a-workspace", "delete", REQUEST),
        (_id(1), WS, "copy", REQUEST),
        (_id(1), WS, "delete", REQUEST.upper()),
    ],
)
def test_an_invalid_request_is_refused_and_records_nothing(
    tmp_path: Path, operation_id: str, workspace_id: str, kind: str, request_sha256: str
) -> None:
    journal = _journal(tmp_path)
    with _refusal("invalid_operation"):
        journal.plan(
            operation_id,
            _root(workspace_id),
            cast(Any, kind),
            (_remove("n.md"),),
            request_sha256,
        )
    assert journal.list_pending() == []


def test_transition_and_delete_are_compare_and_set(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    row = journal.plan(_id(1), _root(), "delete", (_remove("n.md"),), REQUEST)
    kept = dataclasses.replace(_remove("n.md"), preimage=_id(50))
    preserved = journal.transition(_id(1), "planned", "preserved", steps=(kept,))
    assert preserved.state == "preserved" and preserved.steps == (kept,)
    assert preserved.created_at == row.created_at
    assert journal.get(_id(1)) == preserved

    with _refusal("recovery_required"):
        journal.transition(_id(1), "planned", "applying")
    # A transition may record hashes and preimages, never a different path or action.
    for changed in (_remove("m.md"), _mkdir("n.md"), _move("n.md", "m.md")):
        with _refusal("invalid_operation"):
            journal.transition(_id(1), "preserved", "applying", steps=(changed,))
    with _refusal("invalid_operation"):
        journal.transition(_id(1), "preserved", cast(Any, "paused"))
    with _refusal("invalid_operation"):
        journal.transition(_id(1), (), "applying")
    with _refusal("recovery_required"):
        journal.transition(_id(9), "planned", "applying")
    assert journal.get(_id(1)) == preserved

    journal.transition(_id(1), ("planned", "preserved"), "applying")
    with _refusal("recovery_required"):
        journal.delete(_id(1), "done")
    journal.transition(_id(1), "applying", "done")
    journal.delete(_id(1), "done")
    assert journal.get(_id(1)) is None and journal.list_pending() == []


def test_a_corrupt_row_is_refused_and_holds_its_workspace(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    store = cast(Any, journal)._store
    with store._connection(write=True) as connection:
        connection.execute(
            "INSERT INTO vault_operations VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                _id(1),
                WS,
                "1:2",
                7,
                "delete",
                REQUEST,
                '[{"n":0,"action":"remove","ref":"a//n.md","to":null,'
                '"pre":null,"post":null,"preimage":null}]',
                "applying",
                "2026-10-02T00:00:00.000Z",
                "2026-10-02T00:00:00.000Z",
            ),
        )
    for call in (
        lambda: journal.get(_id(1)),
        journal.list_pending,
        lambda: journal.plan(_id(2), _root(), "delete", (_remove("z.md"),), REQUEST),
    ):
        with pytest.raises(PublicationError) as failure:
            call()
        assert (failure.value.code, failure.value.operation_id) == ("journal_corrupt", _id(1))
    # Another workspace does not read that row's steps.
    journal.plan(_id(3), _root(OTHER_WS), "delete", (_remove("z.md"),), REQUEST)


# ---------------------------------------------------------------- the private recovery store


def test_a_put_keeps_a_private_verified_copy_and_then_its_row(tmp_path: Path) -> None:
    store, root = _recovery(tmp_path)
    data = b"---\r\ntitle: n\r\n---\r\nbody\x00\xff"
    entry = store.put(WS, "deleted", "a/n.md", data)
    path = root / f"{entry.recovery_id}.bin"
    assert path.read_bytes() == data
    protection.require_private_directory(root)
    protection.require_private_file(path)
    assert (entry.workspace_id, entry.kind, entry.ref) == (WS, "deleted", "a/n.md")
    assert (entry.sha256, entry.byte_size) == (hashlib.sha256(data).hexdigest(), len(data))
    created = datetime.fromisoformat(entry.created_at)
    assert datetime.fromisoformat(entry.expires_at) - created == timedelta(days=30)
    assert _rows(store) == [dataclasses.astuple(entry)]
    assert store.read(entry.recovery_id) == (entry, data)
    assert store.usage() == (1, len(data))
    assert store.sweep(created) == [entry]

    # A note at the cap is kept exactly; one byte more is not a note.
    full = bytes(range(256)) * (MAX_PUBLICATION_BYTES // 256)
    at_cap = store.put(WS, "preimage", "b.md", full)
    assert (root / f"{at_cap.recovery_id}.bin").read_bytes() == full
    assert store.read(at_cap.recovery_id) == (at_cap, full)
    with _refusal("invalid_recovery"):
        store.put(WS, "deleted", "c.md", full + b"x")
    for workspace_id, kind, ref in (
        ("not-a-workspace", "deleted", "n.md"),
        (WS, "kept", "n.md"),
        (WS, "deleted", "CLAUDE.md"),
        (WS, "deleted", "a//n.md"),
    ):
        with _refusal("invalid_recovery"):
            store.put(workspace_id, cast(Any, kind), ref, b"x")
    assert sorted(os.listdir(root)) == sorted(f"{e.recovery_id}.bin" for e in (entry, at_cap))
    assert store.usage() == (2, len(data) + len(full))


def test_the_store_bounds_refuse_before_any_vault_change(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    note = vault / "n.md"
    note.write_bytes(b"keep me")

    def delete_note(store: RecoveryStore) -> None:
        # The order a delete follows: the verified private copy first, then the removal.
        store.put(WS, "deleted", "n.md", note.read_bytes())
        note.unlink()

    by_count, count_root = _recovery(tmp_path / "count")
    for n in range(MAX_RECOVERY_ENTRIES):
        by_count.put(WS, "preimage", f"n{n}.md", b"x")
    files, rows = sorted(os.listdir(count_root)), _rows(by_count)
    with _refusal("recovery_full"):
        delete_note(by_count)
    assert note.read_bytes() == b"keep me"
    assert sorted(os.listdir(count_root)) == files and _rows(by_count) == rows
    assert by_count.usage() == (MAX_RECOVERY_ENTRIES, MAX_RECOVERY_ENTRIES)

    by_bytes, bytes_root = _recovery(tmp_path / "bytes")
    block = b"y" * MAX_PUBLICATION_BYTES
    for n in range(MAX_RECOVERY_BYTES // MAX_PUBLICATION_BYTES):
        by_bytes.put(WS, "deleted", f"n{n}.md", block)
    files, rows = sorted(os.listdir(bytes_root)), _rows(by_bytes)
    with _refusal("recovery_full"):
        delete_note(by_bytes)
    assert note.read_bytes() == b"keep me"
    assert sorted(os.listdir(bytes_root)) == files and _rows(by_bytes) == rows
    assert by_bytes.usage() == (len(rows), MAX_RECOVERY_BYTES)


def test_a_failed_write_leaves_no_row_and_no_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    store, root = _recovery(tmp_path)

    def fail(*args: object) -> None:
        raise OSError("synthetic")

    with monkeypatch.context() as patched:
        patched.setattr(os, "fsync", fail)
        with _refusal("recovery_unavailable"):
            store.put(WS, "deleted", "secret/n.md", b"secret bytes")
    assert os.listdir(root) == [] and _rows(store) == []

    cast(Any, store)._store.fail_commit = True
    with _refusal("recovery_unavailable"):
        store.put(WS, "deleted", "secret/n.md", b"secret bytes")
    cast(Any, store)._store.fail_commit = False
    assert os.listdir(root) == [] and _rows(store) == []

    def not_private(path: Path) -> None:
        raise protection.ProtectionError()

    other, other_root = _recovery(tmp_path / "other")
    with monkeypatch.context() as patched:
        patched.setattr(protection, "require_private_directory", not_private)
        with _refusal("recovery_unavailable"):
            other.put(WS, "deleted", "secret/n.md", b"secret bytes")
    assert not other_root.exists()
    assert "secret" not in caplog.text and str(tmp_path) not in caplog.text


def test_a_read_rehashes_the_copy(tmp_path: Path) -> None:
    store, root = _recovery(tmp_path)
    entry = store.put(WS, "deleted", "n.md", b"original")
    path = root / f"{entry.recovery_id}.bin"
    for changed in (b"0riginal", b"origina", b"original!"):
        path.write_bytes(changed)
        with _refusal("recovery_corrupt"):
            store.read(entry.recovery_id)
    path.unlink()
    with _refusal("recovery_missing"):
        store.read(entry.recovery_id)
    with _refusal("recovery_missing"):
        store.read(_id(99))
    for bad in ("../journal", _id(0xAB).upper()):
        with _refusal("invalid_recovery"):
            store.read(bad)
        with _refusal("invalid_recovery"):
            store.drop(bad)


def test_drop_removes_the_file_and_then_the_row(tmp_path: Path) -> None:
    store, root = _recovery(tmp_path)
    entry = store.put(WS, "deleted", "n.md", b"x")
    kept = store.put(WS, "deleted", "m.md", b"y")
    store.drop(entry.recovery_id)
    store.drop(entry.recovery_id)
    assert os.listdir(root) == [f"{kept.recovery_id}.bin"]
    assert _rows(store) == [dataclasses.astuple(kept)]


def test_a_sweep_drops_expired_entries_and_every_orphan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    store, root = _recovery(tmp_path)
    expired = store.put(WS, "deleted", "old.md", b"old")
    fileless = store.put(WS, "deleted", "gone.md", b"gone")
    kept = store.put(WS, "deleted", "kept.md", b"kept")
    with cast(Any, store)._store._connection(write=True) as connection:
        connection.execute(
            "UPDATE vault_recovery SET expires_at=? WHERE recovery_id=?",
            ("2026-01-01T00:00:00.000Z", expired.recovery_id),
        )
    (root / f"{fileless.recovery_id}.bin").unlink()
    (root / f"{_id(77)}.bin").write_bytes(b"no row")
    (root / "stray.tmp").write_bytes(b"no row")
    expired_file = root / f"{expired.recovery_id}.bin"
    now = datetime.now(UTC)

    # The file goes first: while it cannot be removed, its row stays and it is not listed.
    real_unlink = os.unlink

    def refuse_expired(path: Any, *args: Any, **kwargs: Any) -> None:
        if Path(path) == expired_file:
            raise PermissionError("synthetic")
        real_unlink(path, *args, **kwargs)

    with monkeypatch.context() as patched:
        patched.setattr(os, "unlink", refuse_expired)
        assert store.sweep(now) == [kept]
    assert sorted(os.listdir(root)) == sorted(f"{e.recovery_id}.bin" for e in (expired, kept))
    assert [row[0] for row in _rows(store)] == sorted(e.recovery_id for e in (expired, kept))
    assert expired.recovery_id in caplog.text
    assert "old.md" not in caplog.text and str(tmp_path) not in caplog.text

    assert store.sweep(now) == [kept]
    assert os.listdir(root) == [f"{kept.recovery_id}.bin"]
    assert _rows(store) == [dataclasses.astuple(kept)]
    assert store.usage() == (1, 4)


def test_entries_list_one_vaults_notes_without_sweeping(tmp_path: Path) -> None:
    store, root = _recovery(tmp_path)
    first = store.put(WS, "deleted", "a.md", b"a")
    expired = store.put(WS, "preimage", "b.md", b"b")
    store.put(OTHER_WS, "deleted", "c.md", b"c")
    with cast(Any, store)._store._connection(write=True) as connection:
        connection.execute(
            "UPDATE vault_recovery SET expires_at=? WHERE recovery_id=?",
            ("2026-01-01T00:00:00.000Z", expired.recovery_id),
        )
    shutil.rmtree(root)  # no folder to look in: a listing must not make or sweep one

    listed = store.entries(WS)

    assert {e.recovery_id for e in listed} == {first.recovery_id, expired.recovery_id}
    assert [(e.created_at, e.recovery_id) for e in listed] == sorted(
        (e.created_at, e.recovery_id) for e in listed
    )
    assert next(e for e in listed if e.recovery_id == expired.recovery_id).expires_at < "2026-02"
    assert not root.exists()
    assert len(_rows(store)) == 3


def test_entries_refuse_past_the_entry_bound(tmp_path: Path) -> None:
    store, _root = _recovery(tmp_path)
    rows = [
        (
            _id(n),
            WS,
            "deleted",
            f"n{n}.md",
            H,
            1,
            "2026-01-01T00:00:00.000Z",
            "2026-02-01T00:00:00.000Z",
        )
        for n in range(MAX_RECOVERY_ENTRIES + 1)
    ]
    insert = (
        "INSERT INTO vault_recovery "
        "(recovery_id,workspace_id,kind,ref,sha256,byte_size,created_at,expires_at) "
        "VALUES (?,?,?,?,?,?,?,?)"
    )
    with cast(Any, store)._store._connection(write=True) as connection:
        connection.executemany(insert, rows[:MAX_RECOVERY_ENTRIES])
    assert len(store.entries(WS)) == MAX_RECOVERY_ENTRIES  # at the bound is still listed
    with cast(Any, store)._store._connection(write=True) as connection:
        connection.execute(insert, rows[MAX_RECOVERY_ENTRIES])
    with _refusal("recovery_unavailable"):
        store.entries(WS)


def test_a_sweep_keeps_every_expired_entry_a_live_row_names(tmp_path: Path) -> None:
    store, root = _recovery(tmp_path)
    journal = OperationJournal(cast(Any, store)._store)
    stuck = store.put(WS, "deleted", "stuck.md", b"stuck")
    blocked = store.put(WS, "deleted", "blocked.md", b"blocked")
    source = store.put(WS, "deleted", "source.md", b"source")
    loose = store.put(WS, "deleted", "loose.md", b"loose")

    # A delete keeps its copy as the remove step's preimage: one reconcile left at
    # `recovery_required`, one it blocked (left at `applying`).
    for n, entry, state in ((1, stuck, "recovery_required"), (2, blocked, "applying")):
        journal.plan(_id(n), _root(), "delete", (_remove(entry.ref),), REQUEST)
        kept = dataclasses.replace(_remove(entry.ref), preimage=entry.recovery_id)
        journal.transition(_id(n), "planned", "preserved", steps=(kept,))
        journal.transition(_id(n), "preserved", "applying")
        if state != "applying":
            journal.transition(_id(n), "applying", cast(Any, state))
    # A restore's create step names its source entry the same way.
    restore = OperationStep(0, "create", "back.md", None, None, H, source.recovery_id)
    journal.plan(_id(3), _root(), "restore", (restore,), REQUEST)

    later = datetime.now(UTC) + timedelta(days=31)
    # Puts in one millisecond order by their random IDs, so compare as sets.
    assert set(store.sweep(later)) == {stuck, blocked, source}
    assert sorted(os.listdir(root)) == sorted(
        f"{e.recovery_id}.bin" for e in (stuck, blocked, source)
    )
    assert loose.recovery_id not in {row[0] for row in _rows(store)}

    # Once its row is forgotten, the entry meets the usual expiry.
    journal.delete(_id(1), "recovery_required")
    assert set(store.sweep(later)) == {blocked, source}
    assert not (root / f"{stuck.recovery_id}.bin").exists()
    assert [row[0] for row in _rows(store)] == sorted(e.recovery_id for e in (blocked, source))


def _stuck_delete(
    store: RecoveryStore, n: int, ref: str, state: str, *, age_days: int
) -> tuple[OperationJournal, RecoveryEntry]:
    """A delete row left at `state` whose kept copy was put `age_days` ago."""
    journal = OperationJournal(cast(Any, store)._store)
    entry = store.put(WS, "deleted", ref, ref.encode())
    created = datetime.now(UTC) - timedelta(days=age_days)
    entry = dataclasses.replace(
        entry,
        created_at=_stamp(created),
        expires_at=_stamp(created + timedelta(days=RECOVERY_DAYS)),
    )
    with cast(Any, store)._store._connection(write=True) as connection:
        connection.execute(
            "UPDATE vault_recovery SET created_at=?,expires_at=? WHERE recovery_id=?",
            (entry.created_at, entry.expires_at, entry.recovery_id),
        )
    journal.plan(_id(n), _root(), "delete", (_remove(ref),), REQUEST)
    kept = dataclasses.replace(_remove(ref), preimage=entry.recovery_id)
    journal.transition(_id(n), "planned", "preserved", steps=(kept,))
    journal.transition(_id(n), "preserved", "applying")
    if state != "applying":
        journal.transition(_id(n), "applying", cast(Any, state))
    return journal, entry


def test_forgetting_a_row_re_arms_the_expired_entry_it_kept(tmp_path: Path) -> None:
    store, root = _recovery(tmp_path)
    journal, entry = _stuck_delete(store, 1, "stuck.md", "recovery_required", age_days=31)
    now = datetime.now(UTC)
    assert entry.expires_at < _stamp(now)

    # `forget` deletes the row in its own state, the call below; the copy gets 30 days more in
    # the same transaction, so the panel's next listing still offers it for a restore.
    journal.delete(_id(1), "recovery_required", now=now)

    rearmed = dataclasses.replace(entry, expires_at=_stamp(now + timedelta(days=RECOVERY_DAYS)))
    assert journal.get(_id(1)) is None
    assert store.sweep(now) == [rearmed]
    assert store.read(entry.recovery_id) == (rearmed, b"stuck.md")
    assert store.sweep(now + timedelta(days=29)) == [rearmed]
    # From there it meets the usual expiry.
    assert store.sweep(now + timedelta(days=31)) == []
    assert not (root / f"{entry.recovery_id}.bin").exists()


def test_a_reconciled_row_re_arms_the_expired_entry_it_kept(tmp_path: Path) -> None:
    store, _ = _recovery(tmp_path)
    # Reconcile left the row blocked at `applying` past the entry's date, then decided it.
    journal, entry = _stuck_delete(store, 1, "blocked.md", "applying", age_days=40)
    before = datetime.now(UTC)

    journal.delete(_id(1), "applying")

    after = datetime.now(UTC)
    [kept] = store.sweep(after)
    assert dataclasses.replace(kept, expires_at=entry.expires_at) == entry
    window = timedelta(days=RECOVERY_DAYS)
    assert _stamp(before + window) <= kept.expires_at <= _stamp(after + window)


def test_a_row_leaves_an_entry_still_in_date_as_it_was(tmp_path: Path) -> None:
    store, _ = _recovery(tmp_path)
    journal, recent = _stuck_delete(store, 1, "recent.md", "recovery_required", age_days=0)
    _, later = _stuck_delete(store, 2, "later.md", "recovery_required", age_days=-10)
    now = datetime.now(UTC) + timedelta(days=1)

    for n in (1, 2):
        journal.delete(_id(n), "recovery_required", now=now)

    assert set(store.sweep(now)) == {recent, later}
    assert {row[0]: row[7] for row in _rows(store)} == {
        recent.recovery_id: recent.expires_at,
        later.recovery_id: later.expires_at,
    }


def test_a_sweep_keeps_an_entry_re_armed_after_its_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, root = _recovery(tmp_path)
    journal, entry = _stuck_delete(store, 1, "stuck.md", "recovery_required", age_days=31)
    shared = cast(Any, store)._store
    real = shared._connection
    forgotten = False

    @contextmanager
    def forget_first(*, write: bool = False) -> Iterator[sqlite3.Connection]:
        # The sweep has read its snapshot and is about to open the entry's write transaction:
        # `forget` deletes the row there and commits the re-arm first.
        nonlocal forgotten
        if write and not forgotten:
            forgotten = True
            journal.delete(_id(1), "recovery_required")
        with real(write=write) as connection:
            yield connection

    monkeypatch.setattr(shared, "_connection", forget_first)
    kept = store.sweep(datetime.now(UTC))

    assert forgotten and journal.get(_id(1)) is None
    rearmed, data = store.read(entry.recovery_id)
    assert rearmed.expires_at > entry.expires_at and data == b"stuck.md"
    assert kept == [rearmed]


def test_a_row_naming_a_dropped_entry_still_deletes(tmp_path: Path) -> None:
    store, _ = _recovery(tmp_path)
    journal, entry = _stuck_delete(store, 1, "gone.md", "recovery_required", age_days=31)
    store.drop(entry.recovery_id)

    journal.delete(_id(1), "recovery_required")

    assert journal.list_pending() == [] and _rows(store) == []
    assert store.sweep(datetime.now(UTC)) == []
