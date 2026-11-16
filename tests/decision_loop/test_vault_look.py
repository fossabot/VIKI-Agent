"""The vault Look reader: this vault's stuck rows and deleted notes, and
memory drift only when asked. Journals, recovery index and store are real; `memory_drift` is
spied, as its own tests (`test_memory_drift.py`) cover its reading, except in the write-nothing
test, which runs it over a real vault."""

from __future__ import annotations

import dataclasses
import hashlib
import os
import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from viki_agent import protection
from viki_agent.decision_loop import vault_look
from viki_agent.decision_loop.contracts import DeletedNote, SaveRow, VaultLook
from viki_agent.decision_loop.memory_drift import Seen, memory_drift
from viki_agent.decision_loop.vault_look import VaultLookReader
from viki_agent.domain import CanonicalRoot
from viki_agent.state import AppState, Prefs
from viki_agent.storage import SessionStore
from viki_agent.vaults.memory_record import Fields, NoteSource, Record, compose, parse
from viki_agent.vaults.vault import ScanResult, VaultService
from viki_agent.vaults.vault_mutation import OrdinaryMutationRoot, VaultPublication
from viki_agent.vaults.vault_operations import (
    OperationJournal,
    OperationStep,
    RecoveryStore,
    _stamp,
)
from viki_agent.vaults.vault_publication import PublicationJournal
from viki_agent.workspaces import WorkspaceService, root_key

WORKSPACE = "5b8f2c1e-3d4a-4e6b-9c7d-0a1b2c3d4e5f"
OTHER = "7c9e6679-7425-40de-944b-e07fc1f90ae7"
HASH = "a" * 64
BAD_STAMP = "x" * 24  # the store only checks the length
SCAN = ScanResult((), False, (), 0, 0, 0)


@dataclasses.dataclass
class Rig:
    store: SessionStore
    publications: PublicationJournal
    operations: OperationJournal
    recovery: RecoveryStore
    data: Path
    drift_calls: list[tuple[str, ScanResult]]
    seen_maps: list[dict[str, Seen]] = dataclasses.field(default_factory=list)
    drift_result: tuple[tuple[str, ...], tuple[str, ...]] = (("memory-1",), ())

    def reader(self) -> VaultLookReader:
        return VaultLookReader(
            self.publications,
            self.operations,
            self.recovery,
            self.store,
            cast(VaultService, object()),
        )

    def sql(self, statement: str, *values: object) -> None:
        with self.store._connection(write=True) as connection:
            connection.execute(statement, values)

    def save(self, workspace_id: str, ref: str) -> str:
        root = OrdinaryMutationRoot(workspace_id, Path("vault"), "vault", "1:2", 7, None)
        row = self.publications.reserve(
            root, ref, VaultPublication().reserve(), "create", HASH, 3, None
        )
        return row.operation_id

    def operation(self, workspace_id: str, ref: str, state: str = "planned") -> str:
        root = OrdinaryMutationRoot(workspace_id, Path("vault"), "vault", "1:2", 7, None)
        operation_id = str(uuid.uuid4())
        step = OperationStep(0, "remove", ref, None, HASH, None, None)
        self.operations.plan(operation_id, root, "delete", (step,), HASH)
        self.sql("UPDATE vault_operations SET state=? WHERE operation_id=?", state, operation_id)
        return operation_id

    def note(self, workspace_id: str, kind: str, ref: str) -> str:
        entry = self.recovery.put(workspace_id, cast(Any, kind), ref, ref.encode())
        return entry.recovery_id


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Rig:
    store = AppState(tmp_path / "state.json", Prefs()).store
    data = tmp_path / "data"
    assert protection.create_private_directory(data)
    operations = OperationJournal(store)
    made = Rig(
        store,
        PublicationJournal(store),
        operations,
        RecoveryStore(data / "vault-recovery", operations),
        data,
        [],
    )

    def spy(
        _store: SessionStore,
        _reader: VaultService,
        workspace_id: str,
        scan: ScanResult,
        *,
        seen: dict[str, Seen],
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        made.drift_calls.append((workspace_id, scan))
        made.seen_maps.append(seen)
        return made.drift_result

    monkeypatch.setattr(vault_look, "memory_drift", spy)
    return made


def fill(rig: Rig) -> dict[str, str]:
    """Rows of this vault and of another, one of every shape the reader tells apart."""
    ids = {
        "save": rig.save(WORKSPACE, "a.md"),
        "idle": rig.operation(WORKSPACE, "b.md"),
        "blocked": rig.operation(WORKSPACE, "c.md", "recovery_required"),
        "deleted": rig.note(WORKSPACE, "deleted", "d.md"),
    }
    rig.save(OTHER, "e.md")
    rig.operation(OTHER, "f.md", "recovery_required")
    rig.note(OTHER, "deleted", "g.md")
    rig.note(WORKSPACE, "preimage", "h.md")  # a replaced note's copy is not a deleted note
    return ids


def test_each_part_reads_this_vaults_rows(rig: Rig) -> None:
    ids = fill(rig)
    stamp = "2026-10-20T09:00:00.000Z"
    expires = _stamp(datetime(2026, 11, 3, tzinfo=UTC))
    rig.sql("UPDATE vault_publications SET updated_at=? WHERE operation_id=?", stamp, ids["save"])
    rig.sql("UPDATE vault_operations SET updated_at=? WHERE operation_id=?", stamp, ids["idle"])
    rig.sql(
        "UPDATE vault_operations SET updated_at=? WHERE operation_id=?", BAD_STAMP, ids["blocked"]
    )
    rig.sql("UPDATE vault_recovery SET expires_at=? WHERE recovery_id=?", expires, ids["deleted"])
    moment = datetime(2026, 10, 20, 9, tzinfo=UTC)

    look = rig.reader().read(WORKSPACE, SCAN, False)

    assert look.saves == (
        SaveRow(ids["save"], False, False, moment),
        # Operation rows follow the saves; only `recovery_required` blocks; a bad stamp is None.
        SaveRow(ids["idle"], True, False, moment),
        SaveRow(ids["blocked"], True, True, None),
    )
    assert look.deleted == (DeletedNote(ids["deleted"], datetime(2026, 11, 3, tzinfo=UTC)),)
    # The usage counts every vault's entries, the bound a put refuses past.
    assert look.usage == (3, 3 * len("x.md"))
    assert look.drift is None and look.failures == ()
    assert rig.drift_calls == []

    rig.sql("UPDATE vault_recovery SET expires_at=?", BAD_STAMP)
    assert rig.reader().read(WORKSPACE, SCAN, False).deleted == (DeletedNote(ids["deleted"], None),)
    other = rig.reader().read(OTHER, SCAN, False)
    assert [row.operation for row in other.saves or ()] == [False, True]
    assert other.saves is not None and other.saves[1].blocked
    assert len(other.deleted or ()) == 1


@pytest.mark.parametrize(
    ("word", "failing", "cleared"),
    [
        ("saves_unknown", "publications.list_pending", {"saves"}),
        ("saves_unknown", "operations.list_pending", {"saves"}),
        ("recovery_unknown", "recovery.entries", {"deleted"}),
        ("recovery_unknown", "recovery.usage", {"usage"}),
        ("memory_unknown", "vault_look.memory_drift", {"drift"}),
    ],
)
def test_a_failed_part_reads_none_and_names_its_failure(
    rig: Rig,
    monkeypatch: pytest.MonkeyPatch,
    word: str,
    failing: str,
    cleared: set[str],
) -> None:
    fill(rig)
    whole = rig.reader().read(WORKSPACE, SCAN, True)
    assert whole.failures == () and None not in dataclasses.astuple(whole)[:4]

    owner, name = failing.split(".")
    target = vault_look if owner == "vault_look" else getattr(rig, owner)

    def boom(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("boom")

    monkeypatch.setattr(target, name, boom)
    look = rig.reader().read(WORKSPACE, SCAN, True)

    # Only the failed part is cleared: every other part keeps what it read.
    expected = dataclasses.replace(whole, failures=(word,), **{part: None for part in cleared})
    assert look == expected


def test_every_part_failing_still_answers(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_args: object, **_kwargs: object) -> object:
        raise OSError("boom")

    for target, name in (
        (rig.publications, "list_pending"),
        (rig.recovery, "entries"),
        (rig.recovery, "usage"),
        (vault_look, "memory_drift"),
    ):
        monkeypatch.setattr(target, name, boom)

    assert rig.reader().read(WORKSPACE, SCAN, True) == VaultLook(
        None, None, None, None, ("saves_unknown", "recovery_unknown", "memory_unknown")
    )


def test_memory_is_read_only_when_asked(rig: Rig) -> None:
    rig.drift_result = (("m1", "m2"), ("memory_partial",))

    off = rig.reader().read(WORKSPACE, SCAN, False)
    assert (off.drift, off.failures) == (None, ())
    assert rig.drift_calls == []

    # Without a whole scan to compare against there is nothing to read: unknown, not none.
    unscanned = rig.reader().read(WORKSPACE, None, True)
    assert (unscanned.drift, unscanned.failures) == (None, ("memory_unknown",))
    assert rig.drift_calls == []

    on = rig.reader().read(WORKSPACE, SCAN, True)
    assert (on.drift, on.failures) == (("m1", "m2"), ("memory_partial",))
    assert rig.drift_calls == [(WORKSPACE, SCAN)]

    # The vault parts do not need a scan.
    assert unscanned.saves == () and unscanned.deleted == () and unscanned.usage == (0, 0)


def test_the_reader_keeps_one_memory_map_per_workspace(rig: Rig) -> None:
    reader = rig.reader()

    reader.read(WORKSPACE, SCAN, True)
    reader.read(OTHER, SCAN, True)
    reader.read(WORKSPACE, SCAN, True)

    first, other, again = rig.seen_maps
    assert first is again and first is not other
    first["memory-1"] = Seen(1.0, 2, HASH, True, ())
    assert other == {}

    # A new reader starts cold: the map is not stored anywhere else.
    rig.reader().read(WORKSPACE, SCAN, True)
    assert rig.seen_maps[-1] == {}


def test_the_read_writes_nothing(rig: Rig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fill(rig)
    shutil.rmtree(rig.recovery.root)  # a read must not make the folder again
    # The real memory read over a real vault: one approved memory whose cited note changed after
    # its review, so the read really opens memory/<id>.md.
    monkeypatch.setattr(vault_look, "memory_drift", memory_drift)
    vault = tmp_path / "vault"
    (vault / "notes").mkdir(parents=True)
    (vault / "memory").mkdir()
    (vault / "notes" / "n.md").write_text("note\n", encoding="utf-8")
    memory_id = str(uuid.uuid4())
    text = compose(
        Fields(
            memory_id,
            "owner",
            "vault",
            "approved",
            "2026-10-01T09:00:00.000Z",
            sources=(NoteSource("notes/n.md", HASH),),
        ),
        "tea in the morning",
    )
    # The exact bytes: write_text would end Windows lines in CRLF, so the record read back would
    # not hash to the approved sha256 below.
    (vault / "memory" / f"{memory_id}.md").write_bytes(text.encode("utf-8"))
    record = parse(text)
    assert isinstance(record, Record)
    rig.sql(
        "INSERT INTO memory_reviews (workspace_id,memory_id,sha256,reviewed_at) VALUES (?,?,?,?)",
        WORKSPACE,
        memory_id,
        record.sha256,
        "2026-10-20T09:00:00.000Z",
    )
    info = os.stat(vault)
    root = CanonicalRoot(
        key=root_key(vault.resolve()),
        display=str(vault.resolve()),
        physical_id=f"{info.st_dev}:{info.st_ino}",
        aliased=False,
    )
    workspace = SimpleNamespace(root=root)
    owner = SimpleNamespace(get=lambda _id: workspace, current_root=lambda _workspace: root)
    reader = VaultService(cast(WorkspaceService, owner))
    look_reader = VaultLookReader(rig.publications, rig.operations, rig.recovery, rig.store, reader)
    scan = reader.scan(WORKSPACE)

    def tree() -> dict[str, tuple[bytes, int]]:
        return {
            str(path): (path.read_bytes() if path.is_file() else b"", path.stat().st_mtime_ns)
            for path in sorted(vault.rglob("*"))
        }

    def digest() -> str:
        return hashlib.sha256(rig.store.path.read_bytes()).hexdigest()

    before = (digest(), tree(), sorted(p.name for p in rig.data.iterdir()))

    looks = [look_reader.read(WORKSPACE, scan, memory) for memory in (False, True)]

    assert (looks[0].drift, looks[1].drift) == (None, (memory_id,))  # the record was read
    assert (digest(), tree(), sorted(p.name for p in rig.data.iterdir())) == before
    assert not rig.recovery.root.exists()
