"""Memory drift: an active memory whose cited note changed after this device's review.

The vault is a scripted double whose `read` counts calls, so the bounds show as numbers; the
reviews are rows in a real store.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import pytest

from viki_agent.automation.routines import parse_stamp
from viki_agent.decision_loop.memory_drift import MAX_MEMORY_READS, Seen, memory_drift
from viki_agent.state import AppState, Prefs
from viki_agent.storage import SessionStore
from viki_agent.vaults.memory_record import (
    MAX_RECORD_BYTES,
    Fields,
    NoteSource,
    Record,
    SessionSource,
    Source,
    compose,
    parse,
)
from viki_agent.vaults.vault import NoteEntry, ScanResult, VaultError, VaultService

WORKSPACE = "5b8f2c1e-3d4a-4e6b-9c7d-0a1b2c3d4e5f"
CREATED = "2026-10-01T09:00:00.000Z"
REVIEWED = "2026-10-20T09:00:00.000Z"
REVIEWED_EPOCH = parse_stamp(REVIEWED).timestamp()
OLDER = REVIEWED_EPOCH - 3600
NEWER = REVIEWED_EPOCH + 3600
CITED = "notes/cited.md"
CLAIM = "tea in the morning"


@dataclass
class Vault:
    """The scan and the reads of one vault: `files` maps a ref to its text, `modified` a ref to
    its change time; a ref in `broken` fails to read."""

    files: dict[str, str] = field(default_factory=dict)
    modified: dict[str, float] = field(default_factory=dict)
    sizes: dict[str, int] = field(default_factory=dict)
    broken: set[str] = field(default_factory=set)
    reads: list[str] = field(default_factory=list)

    def note(self, ref: str, modified: float) -> None:
        self.modified[ref] = modified

    def scan(self, *, partial: bool = False) -> ScanResult:
        entries = tuple(
            NoteEntry(
                ref,
                ref,
                self.sizes.get(ref, len(self.files.get(ref, "x").encode())),
                modified,
            )
            for ref, modified in self.modified.items()
        )
        reasons = ("entry_limit",) if partial else ()
        return ScanResult(entries, partial, reasons, 0, len(entries), 0)

    def read(self, workspace_id: str, ref: str) -> object:
        assert workspace_id == WORKSPACE
        self.reads.append(ref)
        if ref in self.broken:
            raise VaultError("read_failed")
        return type("Note", (), {"source": self.files[ref]})()

    @property
    def reader(self) -> VaultService:
        return cast(VaultService, self)


@pytest.fixture
def store(tmp_path: Path) -> SessionStore:
    return AppState(tmp_path / "state.json", Prefs()).store


@pytest.fixture
def vault() -> Vault:
    return Vault()


def put(
    vault: Vault,
    store: SessionStore,
    *,
    sources: tuple[Source, ...] = (NoteSource(CITED, "a" * 64),),
    status: str = "approved",
    kind: str = "owner",
    reviewed_at: str | None = REVIEWED,
    approved: str | None = None,
    modified: float = OLDER,
    memory_id: str | None = None,
) -> str:
    """One record in `memory/` and, unless `reviewed_at` is None, this device's review of it."""
    memory_id = memory_id or str(uuid.uuid4())
    fields = Fields(memory_id, kind, "vault", status, CREATED, sources=sources)
    text = compose(fields, CLAIM)
    ref = f"memory/{memory_id}.md"
    vault.files[ref] = text
    vault.note(ref, modified)
    if reviewed_at is not None:
        record = parse(text)
        assert isinstance(record, Record)
        review(store, memory_id, approved or record.sha256, reviewed_at)
    return memory_id


def review(store: SessionStore, memory_id: str, digest: str, reviewed_at: str) -> None:
    with store._connection(write=True) as connection:
        connection.execute(
            "INSERT OR REPLACE INTO memory_reviews (workspace_id,memory_id,sha256,reviewed_at) "
            "VALUES (?,?,?,?)",
            (WORKSPACE, memory_id, digest, reviewed_at),
        )


def run(
    vault: Vault,
    store: SessionStore,
    *,
    partial: bool = False,
    seen: dict[str, Seen] | None = None,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    return memory_drift(store, vault.reader, WORKSPACE, vault.scan(partial=partial), seen=seen)


def test_a_cited_note_changed_after_the_review_drifts(vault: Vault, store: SessionStore) -> None:
    vault.note(CITED, NEWER)
    second_cited = "notes/second.md"
    vault.note(second_cited, OLDER)
    first = put(vault, store)
    # One changed source among several is enough; the unchanged one does not hide it.
    second = put(
        vault, store, sources=(NoteSource(second_cited, "b" * 64), NoteSource(CITED, "c" * 64))
    )
    # The exact review instant is not a change after it.
    vault.note("notes/edge.md", REVIEWED_EPOCH)
    edge = put(vault, store, sources=(NoteSource("notes/edge.md", "d" * 64),))

    found = run(vault, store)

    assert found == (tuple(sorted((first, second))), ())
    assert edge not in found[0]
    # Change times come from the scan: only the records are read, never a cited note.
    assert CITED not in vault.reads
    assert sorted(vault.reads) == sorted(f"memory/{i}.md" for i in (first, second, edge))


def test_unchanged_session_or_missing_sources_do_not_drift(
    vault: Vault, store: SessionStore
) -> None:
    vault.note(CITED, OLDER)
    vault.note("notes/new.md", NEWER)  # a newer note elsewhere makes each memory due
    unchanged = put(vault, store)
    session = put(vault, store, sources=(SessionSource(str(uuid.uuid4())),))
    missing = put(vault, store, sources=(NoteSource("notes/gone.md", "e" * 64),))
    no_sources = put(vault, store, sources=())

    found = run(vault, store)

    assert found == ((), ())
    assert sorted(vault.reads) == sorted(
        f"memory/{i}.md" for i in (unchanged, session, missing, no_sources)
    )


def test_only_active_memories_drift(vault: Vault, store: SessionStore) -> None:
    vault.note(CITED, NEWER)
    active = put(vault, store)
    pending = put(vault, store, approved="f" * 64)  # approved bytes are not these bytes
    retired = put(vault, store, status="retired")
    candidate = put(vault, store, status="candidate")
    inference = put(vault, store, kind="inference")
    unreviewed = put(vault, store, reviewed_at=None)
    # An ordinary note, and a record filed under another memory's name, are not memories.
    plain_id = str(uuid.uuid4())
    vault.files[f"memory/{plain_id}.md"] = "# an ordinary note\n"
    vault.note(f"memory/{plain_id}.md", OLDER)
    review(store, plain_id, "0" * 64, REVIEWED)
    filed_as = str(uuid.uuid4())
    stray = put(vault, store, reviewed_at=None)
    vault.files[f"memory/{filed_as}.md"] = vault.files.pop(f"memory/{stray}.md")
    vault.modified[f"memory/{filed_as}.md"] = vault.modified.pop(f"memory/{stray}.md")
    stray_record = parse(vault.files[f"memory/{filed_as}.md"])
    assert isinstance(stray_record, Record)
    review(store, filed_as, stray_record.sha256, REVIEWED)
    del pending, retired, candidate, inference, unreviewed

    found, failures = run(vault, store)

    assert found == (active,)
    assert failures == ()


def test_a_new_review_or_retire_clears_the_drift(vault: Vault, store: SessionStore) -> None:
    vault.note(CITED, NEWER)
    memory_id = put(vault, store)
    assert run(vault, store)[0] == (memory_id,)

    # Correct: the user reviews again after the change.
    later = "2026-10-21T09:00:00.000Z"
    put(vault, store, memory_id=memory_id, reviewed_at=later)
    assert run(vault, store) == ((), ())

    # Retire: the record is saved retired, whatever the review says.
    put(vault, store, memory_id=memory_id, reviewed_at=REVIEWED, status="retired")
    vault.note(CITED, NEWER + 86400 * 5)
    assert run(vault, store) == ((), ())


def test_a_failed_read_or_partial_scan_names_memory_partial(
    vault: Vault, store: SessionStore
) -> None:
    vault.note(CITED, NEWER)
    broken = put(vault, store)
    working = put(vault, store)
    vault.broken.add(f"memory/{broken}.md")

    assert run(vault, store) == ((working,), ("memory_partial",))
    assert run(vault, store, partial=True) == ((working,), ("memory_partial",))

    vault.broken.clear()
    assert run(vault, store) == (tuple(sorted((broken, working))), ())
    assert run(vault, store, partial=True)[1] == ("memory_partial",)


def test_reads_are_capped_and_skipped_without_a_newer_note(
    vault: Vault, store: SessionStore
) -> None:
    vault.note(CITED, NEWER)
    ids = sorted(put(vault, store) for _ in range(MAX_MEMORY_READS + 8))

    found, failures = run(vault, store)

    assert len(vault.reads) == MAX_MEMORY_READS
    assert vault.reads == [f"memory/{i}.md" for i in ids[:MAX_MEMORY_READS]]
    assert found == tuple(ids[:MAX_MEMORY_READS])
    assert failures == ("memory_partial",)

    # Nothing outside memory/ changed after the reviews: no record is read. The records' own
    # change times and times a datetime cannot hold do not count.
    vault.reads.clear()
    vault.note(CITED, OLDER)
    for ref in list(vault.modified):
        if ref.startswith("memory/"):
            vault.modified[ref] = NEWER
    vault.note("notes/huge.md", 1e30)
    vault.note("notes/nan.md", float("nan"))
    assert run(vault, store) == ((), ())
    assert vault.reads == []

    # A record over 32 KiB is not read, even with a newer note.
    vault.note("notes/new.md", NEWER)
    for ref in list(vault.modified):
        if ref.startswith("memory/"):
            vault.sizes[ref] = MAX_RECORD_BYTES + 1
    vault.sizes[f"memory/{ids[0]}.md"] = MAX_RECORD_BYTES
    assert run(vault, store) == ((), ())
    assert vault.reads == [f"memory/{ids[0]}.md"]

    # A record the scan did not list is not read, though its file exists and a newer note is there.
    vault.reads.clear()
    vault.sizes.clear()
    unlisted = put(vault, store)
    del vault.modified[f"memory/{unlisted}.md"]
    run(vault, store)
    assert f"memory/{unlisted}.md" not in vault.reads


def test_the_drift_read_writes_nothing_to_the_store(
    vault: Vault, store: SessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault.note(CITED, NEWER)
    memory_id = put(vault, store)
    before = store.path.read_bytes()
    real = store._connection
    writes: list[bool] = []

    def watch(*, write: bool = False, **kwargs: Any) -> Any:
        writes.append(write)
        return real(write=write, **kwargs)

    monkeypatch.setattr(store, "_connection", watch)

    assert run(vault, store) == ((memory_id,), ())

    assert vault.reads == [f"memory/{memory_id}.md"]  # the record reads were reached
    assert writes and not any(writes)  # a no-op UPDATE leaves the bytes alone, so watch opens too
    assert store.path.read_bytes() == before


def test_retired_memories_do_not_starve_a_later_active_one(
    vault: Vault, store: SessionStore
) -> None:
    for i in range(MAX_MEMORY_READS):
        put(vault, store, status="retired", memory_id=f"00000000-0000-4000-8000-{i:012d}")
    live = put(vault, store, memory_id="ffffffff-0000-4000-8000-000000000000")
    vault.note(CITED, NEWER)
    seen: dict[str, Seen] = {}

    # The first cycle fills its 32 reads with the retired records and says it is partial.
    assert run(vault, store, seen=seen) == ((), ("memory_partial",))
    assert len(vault.reads) == MAX_MEMORY_READS
    assert f"memory/{live}.md" not in vault.reads

    # The next one reads only the record the first never reached, and finds the drift.
    vault.reads.clear()
    assert run(vault, store, seen=seen) == ((live,), ())
    assert vault.reads == [f"memory/{live}.md"]

    # Every record is remembered now: a cycle reads none and still reports the drift.
    vault.reads.clear()
    assert run(vault, store, seen=seen) == ((live,), ())
    assert vault.reads == []


def test_a_remembered_record_is_read_again_when_its_file_or_approval_changes(
    vault: Vault, store: SessionStore
) -> None:
    vault.note(CITED, NEWER)
    memory_id = put(vault, store)
    ref = f"memory/{memory_id}.md"
    seen: dict[str, Seen] = {}
    assert run(vault, store, seen=seen) == ((memory_id,), ())
    assert run(vault, store, seen=seen) == ((memory_id,), ())
    assert vault.reads == [ref]

    # A new change time, a new size and a new approval each cost one read.
    vault.note(ref, OLDER + 1)
    run(vault, store, seen=seen)
    assert vault.reads == [ref] * 2
    vault.sizes[ref] = len(vault.files[ref].encode()) + 1
    run(vault, store, seen=seen)
    assert vault.reads == [ref] * 3
    review(store, memory_id, "f" * 64, REVIEWED)  # not the record's bytes: pending, not active
    assert run(vault, store, seen=seen) == ((), ())
    assert vault.reads == [ref] * 4
    assert run(vault, store, seen=seen) == ((), ())
    assert vault.reads == [ref] * 4


def test_a_record_that_is_not_a_memory_is_remembered_as_not_active(
    vault: Vault, store: SessionStore
) -> None:
    vault.note(CITED, NEWER)
    plain_id = str(uuid.uuid4())
    vault.files[f"memory/{plain_id}.md"] = "# an ordinary note\n"
    vault.note(f"memory/{plain_id}.md", OLDER)
    review(store, plain_id, "0" * 64, REVIEWED)
    seen: dict[str, Seen] = {}

    assert run(vault, store, seen=seen) == ((), ())
    assert run(vault, store, seen=seen) == ((), ())

    assert vault.reads == [f"memory/{plain_id}.md"]
    assert seen[plain_id].active is False and seen[plain_id].cites == ()


def test_a_failed_read_is_not_remembered(vault: Vault, store: SessionStore) -> None:
    vault.note(CITED, NEWER)
    memory_id = put(vault, store)
    vault.broken.add(f"memory/{memory_id}.md")
    seen: dict[str, Seen] = {}

    assert run(vault, store, seen=seen) == ((), ("memory_partial",))
    assert seen == {}

    vault.broken.clear()
    assert run(vault, store, seen=seen) == ((memory_id,), ())
    assert memory_id in seen


def test_the_map_keeps_only_memories_that_still_have_a_review(
    vault: Vault, store: SessionStore
) -> None:
    vault.note(CITED, NEWER)
    kept = put(vault, store)
    dropped = put(vault, store)
    seen: dict[str, Seen] = {}
    run(vault, store, seen=seen)
    assert set(seen) == {kept, dropped}

    with store._connection(write=True) as connection:
        connection.execute("DELETE FROM memory_reviews WHERE memory_id=?", (dropped,))

    run(vault, store, seen=seen)
    assert set(seen) == {kept}


def test_without_a_map_every_call_reads_again(vault: Vault, store: SessionStore) -> None:
    vault.note(CITED, NEWER)
    memory_id = put(vault, store)

    assert run(vault, store) == ((memory_id,), ())
    assert run(vault, store) == ((memory_id,), ())

    assert vault.reads == [f"memory/{memory_id}.md"] * 2
