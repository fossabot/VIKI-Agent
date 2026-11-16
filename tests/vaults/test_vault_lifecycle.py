from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import sqlite3
import stat
import sys
import threading
import unicodedata
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from unittest.mock import Mock

import pytest

from viki_agent import osfs, protection
from viki_agent.persistence.feature_schemas import OPERATION_SCHEMAS
from viki_agent.vaults import vault_lifecycle, vault_mutation, vault_operations
from viki_agent.vaults.vault_lifecycle import (
    ForgottenOperation,
    OperationOutcome,
    PreviewStep,
    StuckOperation,
    VaultLifecycleService,
)
from viki_agent.vaults.vault_mutation import (
    MAX_PUBLICATION_BYTES,
    FileFingerprint,
    MutationCoordinator,
    MutationError,
    MutationLease,
    OrdinaryMutationRoot,
    PublicationReservation,
    PublishedPublication,
    StagedPublication,
    VaultPublication,
)
from viki_agent.vaults.vault_operations import OperationJournal, RecoveryEntry, RecoveryStore
from viki_agent.vaults.vault_publication import (
    JOURNAL_SCHEMA,
    PublicationError,
    PublicationJournal,
)
from viki_agent.workspaces import root_key

# A guard on an event that normally fires in milliseconds, never a bound on real work.
HANG_GUARD_S = 5
LEASE_WAIT_S = 0.02


class _Store:
    """The publication and operation tables behind `SessionStore._connection`'s shape."""

    def __init__(self, path: Path) -> None:
        self.path = path
        with sqlite3.connect(path) as connection:
            connection.execute(JOURNAL_SCHEMA)
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
                connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()


class _Crash(BaseException):
    """The process dies here: nothing after it runs, the journal row stays as it was."""


Hook = Callable[[], object]


class _Faulty(VaultPublication):
    """Runs an injected hook just before or just after one native step."""

    def __init__(self, **hooks: Hook) -> None:
        self.hooks = hooks

    def _hook(self, name: str) -> None:
        hook = self.hooks.get(name)
        if hook is not None:
            hook()

    def read_verified(
        self,
        lease: MutationLease,
        root: OrdinaryMutationRoot,
        ref: str,
        expected: FileFingerprint,
    ) -> bytes:
        self._hook("before_read")
        return super().read_verified(lease, root, ref, expected)

    def move_verified(
        self,
        lease: MutationLease,
        root: OrdinaryMutationRoot,
        source_ref: str,
        dest_ref: str,
        expected: FileFingerprint,
    ) -> PublishedPublication:
        self._hook("before_move")
        published = super().move_verified(lease, root, source_ref, dest_ref, expected)
        self._hook("after_move")
        return published

    def remove_verified(
        self,
        lease: MutationLease,
        root: OrdinaryMutationRoot,
        ref: str,
        expected: FileFingerprint,
    ) -> None:
        self._hook("before_remove")
        super().remove_verified(lease, root, ref, expected)
        self._hook("after_remove")

    def stage(
        self,
        lease: MutationLease,
        root: OrdinaryMutationRoot,
        ref: str,
        reservation: PublicationReservation,
        content: bytes,
    ) -> StagedPublication:
        staged = super().stage(lease, root, ref, reservation, content)
        self._hook("after_stage")
        return staged

    def publish_create(
        self, lease: MutationLease, staged: StagedPublication
    ) -> PublishedPublication:
        self._hook("before_publish")
        published = super().publish_create(lease, staged)
        self._hook("after_publish")
        return published


def _raise(exc: BaseException) -> Hook:
    def hook() -> None:
        raise exc

    return hook


def _root(path: Path) -> OrdinaryMutationRoot:
    path.mkdir(exist_ok=True)
    resolved = path.resolve()
    info = resolved.stat()
    return OrdinaryMutationRoot(
        str(uuid.uuid4()),
        resolved,
        root_key(resolved),
        f"{info.st_dev}:{info.st_ino}",
        7,
        None,
    )


@dataclass
class _Env:
    tmp: Path
    vault: Path
    root: OrdinaryMutationRoot
    roots: dict[str, OrdinaryMutationRoot]
    store: _Store
    journal: OperationJournal
    recovery: RecoveryStore
    publications: PublicationJournal
    coordinator: MutationCoordinator
    service: VaultLifecycleService

    def launch(self, native: VaultPublication | None = None) -> VaultLifecycleService:
        """A fresh service over the same store and recovery folder: the next launch."""

        def resolve(workspace_id: str, revision: int, physical_id: str) -> OrdinaryMutationRoot:
            root = self.roots.get(workspace_id)
            if root is None:
                raise MutationError("workspace_changed")
            assert (revision, physical_id) == (root.policy_revision, root.physical_id)
            return root

        self.coordinator = MutationCoordinator()
        self.service = VaultLifecycleService(
            self.journal,
            self.recovery,
            resolve,
            self.coordinator,
            native,
            lease_wait_s=LEASE_WAIT_S,
            publications=self.publications,
        )
        return self.service

    async def apply(
        self,
        kind: str,
        *,
        ref: str | None = None,
        recovery_id: str | None = None,
        to: str | None = None,
        command_id: str | None = None,
        root: OrdinaryMutationRoot | None = None,
        expected: str | None = None,
    ) -> OperationOutcome:
        root = root or self.root
        return await self.service.apply(
            command_id or str(uuid.uuid4()),
            root.workspace_id,
            root.policy_revision,
            root.physical_id,
            kind,
            ref=ref,
            recovery_id=recovery_id,
            to=to,
            expected_sha256=expected or _loaded(root, ref),
        )

    async def preview(
        self,
        kind: str,
        *,
        ref: str | None = None,
        recovery_id: str | None = None,
        to: str | None = None,
        expected: str | None = None,
    ) -> vault_lifecycle.OperationPlan:
        return await self.service.preview(
            self.root.workspace_id,
            self.root.policy_revision,
            self.root.physical_id,
            kind,
            ref=ref,
            recovery_id=recovery_id,
            to=to,
            expected_sha256=expected or _loaded(self.root, ref),
        )

    def entries(self) -> list[RecoveryEntry]:
        return self.recovery.sweep(datetime.now(UTC))

    def rows(self) -> list[tuple[str, str]]:
        return [(row.operation_id, row.state) for row in self.journal.list_pending()]


def _env(tmp_path: Path, native: VaultPublication | None = None) -> _Env:
    store = _Store(tmp_path / "journal.sqlite3")
    journal = OperationJournal(cast(Any, store))
    data = tmp_path / "data"
    assert protection.create_private_directory(data)
    vault = tmp_path / "vault"
    root = _root(vault)
    env = _Env(
        tmp_path,
        vault,
        root,
        {root.workspace_id: root},
        store,
        journal,
        RecoveryStore(data / "vault-recovery", journal),
        PublicationJournal(cast(Any, store)),
        MutationCoordinator(),
        cast(Any, None),
    )
    env.launch(native)
    return env


def _tree(root: Path) -> list[tuple[str, bytes | None]]:
    return sorted(
        (path.relative_to(root).as_posix(), None if path.is_dir() else path.read_bytes())
        for path in root.rglob("*")
    )


def _sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _loaded(root: OrdinaryMutationRoot, ref: str | None) -> str | None:
    """The revision the user loaded: the source's current hash, or none for a restore."""
    if ref is None:
        return None
    note = root.path / ref
    return _sha(note.read_bytes()) if note.is_file() else "0" * 64


async def _refused(code: str, call: Any) -> None:
    with pytest.raises(PublicationError) as caught:
        await call
    assert caught.value.code == code


@pytest.mark.parametrize(
    ("kind", "ref", "to"),
    [
        ("rename", "bot-memory/note.md", "renamed.md"),
        ("move", "BoT-MeMoRy/note.md", "moved.md"),
        ("archive", "bot-memory/note.md", "archive/note.md"),
        ("delete", "bot-memory/note.md", None),
        ("rename", "ordinary.md", "bot-memory/renamed.md"),
        ("move", "ordinary.md", "BoT-MeMoRy/moved.md"),
    ],
)
async def test_direct_operation_refuses_bot_memory_before_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    ref: str,
    to: str | None,
) -> None:
    env = _env(tmp_path)
    (env.vault / "bot-memory").mkdir()
    (env.vault / "bot-memory" / "note.md").write_bytes(b"memory")
    (env.vault / "ordinary.md").write_bytes(b"ordinary")
    before = _tree(env.vault)
    plan = Mock(wraps=env.journal.plan)
    monkeypatch.setattr(env.journal, "plan", plan)

    assert (await env.preview(kind, ref=ref, to=to)).refusal == "excluded_path"
    await _refused("excluded_path", env.apply(kind, ref=ref, to=to))

    assert plan.call_count == 0
    assert env.rows() == []
    assert env.entries() == []
    assert _tree(env.vault) == before
    assert not list(env.vault.rglob(".viki-*"))


async def test_direct_restore_refuses_bot_memory_and_keeps_recovery_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _env(tmp_path)
    entry = env.recovery.put(env.root.workspace_id, "deleted", "bot-memory/note.md", b"memory")
    before = _tree(env.vault)
    plan = Mock(wraps=env.journal.plan)
    monkeypatch.setattr(env.journal, "plan", plan)

    assert (
        await env.preview("restore", recovery_id=entry.recovery_id, to=entry.ref)
    ).refusal == "excluded_path"
    await _refused(
        "excluded_path", env.apply("restore", recovery_id=entry.recovery_id, to=entry.ref)
    )

    assert plan.call_count == 0
    assert env.rows() == []
    assert env.entries() == [entry]
    assert env.recovery.read(entry.recovery_id)[1] == b"memory"
    assert _tree(env.vault) == before
    assert not list(env.vault.rglob(".viki-*"))


# Move, rename and archive: only onto an absent name, against the expected hash


async def test_a_move_lands_only_on_an_absent_name_against_the_expected_hash(
    tmp_path: Path,
) -> None:
    env = _env(tmp_path)
    (env.vault / "a").mkdir()
    (env.vault / "a" / "n.md").write_bytes(b"note")

    outcome = await env.apply("move", ref="a/n.md", to="b/c/n.md")

    assert outcome.status == "done"
    assert outcome.created_folders == ("b", "b/c")
    assert outcome.steps_applied == 3
    assert (env.vault / "b" / "c" / "n.md").read_bytes() == b"note"
    assert not (env.vault / "a" / "n.md").exists()
    assert env.rows() == []

    # A taken name refuses before any row or vault change.
    (env.vault / "x.md").write_bytes(b"x")
    (env.vault / "y.md").write_bytes(b"y")
    before = _tree(env.vault)
    await _refused("conflict", env.apply("rename", ref="x.md", to="y.md"))
    assert _tree(env.vault) == before
    assert env.rows() == []


async def test_a_source_changed_before_the_move_is_a_conflict_and_nothing_moves(
    tmp_path: Path,
) -> None:
    env = _env(tmp_path)
    note = env.vault / "n.md"
    note.write_bytes(b"seen")
    env.launch(_Faulty(before_read=lambda: note.write_bytes(b"edited elsewhere")))

    await _refused("conflict", env.apply("rename", ref="n.md", to="m.md"))

    assert _tree(env.vault) == [("n.md", b"edited elsewhere")]
    assert env.rows() == []


async def test_a_name_taken_by_another_writer_mid_move_is_a_conflict_outcome(
    tmp_path: Path,
) -> None:
    env = _env(tmp_path)
    (env.vault / "n.md").write_bytes(b"note")
    env.launch(_Faulty(before_move=lambda: (env.vault / "m.md").write_bytes(b"racer")))

    outcome = await env.apply("rename", ref="n.md", to="m.md")

    assert (outcome.status, outcome.reason, outcome.steps_applied) == ("conflict", "conflict", 0)
    assert _tree(env.vault) == [("m.md", b"racer"), ("n.md", b"note")]
    assert env.rows() == []


def _fold_like_apfs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Observe notes at the vault's top as APFS does by default, on any OS: a ref finds the entry
    its name spells in any letter case or Unicode normalization. The native moves stay real."""
    observe = VaultLifecycleService._observe

    def key(name: str) -> str:
        return unicodedata.normalize("NFC", name).casefold()

    def folded(root: OrdinaryMutationRoot, ref: str) -> FileFingerprint | None:
        spelled = (name for name in os.listdir(root.path) if key(name) == key(ref))
        return observe(root, next(spelled, ref))

    monkeypatch.setattr(VaultLifecycleService, "_observe", staticmethod(folded))


@pytest.mark.parametrize("folding", [False, True], ids=["real_volume", "faked_apfs"])
async def test_a_case_only_rename_lands_and_reconciles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, folding: bool
) -> None:
    # A volume that folds case (NTFS and APFS by default, or the fake) finds the note under both
    # spellings and reconciles by the name its folder holds; a case-sensitive one sees two names.
    if folding:
        _fold_like_apfs(monkeypatch)
    env = _env(tmp_path)
    (env.vault / "note.md").write_bytes(b"note")

    assert (await env.apply("rename", ref="note.md", to="Note.md")).status == "done"
    assert [path.name for path in env.vault.iterdir()] == ["Note.md"]

    env.launch(_Faulty(after_move=_raise(_Crash())))
    with pytest.raises(_Crash):
        await env.apply("rename", ref="Note.md", to="NOTE.md")
    env.launch()
    [outcome] = await env.service.reconcile()
    assert outcome.status == "done"
    assert [path.name for path in env.vault.iterdir()] == ["NOTE.md"]


@pytest.mark.skipif(
    sys.platform != "linux", reason="two names differing only in case need a case-sensitive volume"
)
@pytest.mark.parametrize("other", [b"other", b"note"], ids=["other_bytes", "same_bytes"])
async def test_a_case_only_rename_onto_a_distinct_file_is_a_conflict(
    tmp_path: Path, other: bytes
) -> None:
    env = _env(tmp_path)
    (env.vault / "note.md").write_bytes(b"note")
    (env.vault / "Note.md").write_bytes(other)
    both = [("Note.md", other), ("note.md", b"note")]

    # Two files are not one note under two spellings, even with the same bytes.
    assert (await env.preview("rename", ref="note.md", to="Note.md")).refusal == "conflict"
    await _refused("conflict", env.apply("rename", ref="note.md", to="Note.md"))
    assert _tree(env.vault) == both
    assert env.rows() == []

    # A file another writer puts at the new spelling mid-rename is kept as well.
    (env.vault / "Note.md").unlink()
    env.launch(_Faulty(before_move=lambda: (env.vault / "Note.md").write_bytes(other)))
    outcome = await env.apply("rename", ref="note.md", to="Note.md")

    assert (outcome.status, outcome.reason, outcome.steps_applied) == ("conflict", "conflict", 0)
    assert _tree(env.vault) == both
    assert env.rows() == []


async def test_a_normalization_only_rename_on_apfs_is_a_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A normalization-only rename is no case-only one, so the note APFS finds under the new
    # spelling is a taken name and nothing moves.
    _fold_like_apfs(monkeypatch)
    env = _env(tmp_path)
    nfc, nfd = (unicodedata.normalize(form, "café.md") for form in ("NFC", "NFD"))
    (env.vault / nfc).write_bytes(b"note")

    assert (await env.preview("rename", ref=nfc, to=nfd)).refusal == "conflict"
    await _refused("conflict", env.apply("rename", ref=nfc, to=nfd))
    assert _tree(env.vault) == [(nfc, b"note")]
    assert env.rows() == []


async def test_an_archive_lands_only_under_the_archive_folder(tmp_path: Path) -> None:
    env = _env(tmp_path)
    (env.vault / "n.md").write_bytes(b"note")

    await _refused("invalid_ref", env.apply("archive", ref="n.md", to="notes/n.md"))
    assert (await env.preview("archive", ref="n.md", to="n2.md")).refusal == "invalid_ref"
    outcome = await env.apply("archive", ref="n.md", to="Archive/2026/n.md")

    assert (outcome.status, outcome.created_folders) == ("done", ("Archive", "Archive/2026"))
    assert _tree(env.vault) == [
        ("Archive", None),
        ("Archive/2026", None),
        ("Archive/2026/n.md", b"note"),
    ]


# Delete: a verified copy first


async def test_a_delete_keeps_a_verified_copy_before_it_removes_the_note(tmp_path: Path) -> None:
    content = b"delete me\n"
    seen: list[bytes] = []

    def copy_is_kept() -> None:
        [entry] = env.entries()
        seen.append(env.recovery.read(entry.recovery_id)[1])
        assert (env.vault / "n.md").read_bytes() == content

    env = _env(tmp_path)
    env.launch(_Faulty(before_remove=copy_is_kept))
    (env.vault / "n.md").write_bytes(content)

    outcome = await env.apply("delete", ref="n.md")

    assert (outcome.status, outcome.steps_applied) == ("done", 1)
    assert seen == [content]
    assert not (env.vault / "n.md").exists()
    [entry] = env.entries()
    assert (entry.workspace_id, entry.kind, entry.ref) == (env.root.workspace_id, "deleted", "n.md")
    assert (entry.sha256, entry.byte_size) == (_sha(content), len(content))
    created = datetime.fromisoformat(entry.created_at)
    assert datetime.fromisoformat(entry.expires_at) - created == timedelta(days=30)
    assert (env.recovery.root / f"{entry.recovery_id}.bin").read_bytes() == content
    assert env.rows() == []


# Restore: create-only, from an entry of the note's own vault


async def test_a_restore_publishes_create_only_and_never_overwrites(tmp_path: Path) -> None:
    env = _env(tmp_path)
    note = env.vault / "a" / "n.md"
    note.parent.mkdir()
    note.write_bytes(b"kept")
    await env.apply("delete", ref="a/n.md")
    [entry] = env.entries()

    # A taken name refuses, and the recovered bytes stay in the store.
    note.write_bytes(b"other")
    await _refused("conflict", env.apply("restore", recovery_id=entry.recovery_id, to="a/n.md"))
    assert note.read_bytes() == b"other"
    assert env.entries() == [entry]

    # Another writer takes the name between the check and the publish: nothing is overwritten.
    note.unlink()
    env.launch(_Faulty(before_publish=lambda: note.write_bytes(b"racer")))
    outcome = await env.apply("restore", recovery_id=entry.recovery_id, to="a/n.md")
    assert (outcome.status, outcome.reason) == ("conflict", "conflict")
    assert _tree(env.vault) == [("a", None), ("a/n.md", b"racer")]
    assert env.entries() == [entry]
    assert env.rows() == []

    # An absent name takes the bytes, into a folder it creates, and the entry is spent.
    env.launch()
    outcome = await env.apply("restore", recovery_id=entry.recovery_id, to="back/n.md")
    assert (outcome.status, outcome.created_folders) == ("done", ("back",))
    assert (env.vault / "back" / "n.md").read_bytes() == b"kept"
    assert env.entries() == []
    assert env.rows() == []


async def test_a_restore_names_only_an_entry_of_its_own_vault(tmp_path: Path) -> None:
    env = _env(tmp_path)
    other = _root(tmp_path / "other")
    env.roots[other.workspace_id] = other
    (other.path / "n.md").write_bytes(b"theirs")
    await env.apply("delete", ref="n.md", root=other)
    [entry] = env.entries()

    await _refused(
        "recovery_missing", env.apply("restore", recovery_id=entry.recovery_id, to="n.md")
    )
    await _refused(
        "recovery_missing", env.apply("restore", recovery_id=str(uuid.uuid4()), to="n.md")
    )
    assert _tree(env.vault) == []


# Notes an operation refuses (over the cap, read-only, hard-linked) and a note at the cap


@pytest.mark.parametrize("kind", ["rename", "move", "archive", "delete"])
@pytest.mark.parametrize(
    ("condition", "code"),
    [
        ("over_cap", "unsafe_target"),
        ("read_only", "read_only_target"),
        ("hard_link", "unsafe_target"),
    ],
)
async def test_a_note_over_the_cap_refuses_every_operation(
    tmp_path: Path, kind: str, condition: str, code: str
) -> None:
    env = _env(tmp_path)
    note = env.vault / "n.md"
    note.write_bytes(b"x" * (MAX_PUBLICATION_BYTES + 1) if condition == "over_cap" else b"note")
    if condition == "hard_link":
        os.link(note, tmp_path / "elsewhere.md")
    if condition == "read_only":
        os.chmod(note, stat.S_IREAD)
    to = {"rename": "m.md", "move": "sub/n.md", "archive": "archive/n.md", "delete": None}[kind]
    before = _tree(env.vault)
    try:
        await _refused(code, env.apply(kind, ref="n.md", to=to))
        assert _tree(env.vault) == before
        assert env.rows() == []
        assert env.recovery.usage() == (0, 0)
    finally:
        os.chmod(note, stat.S_IREAD | stat.S_IWRITE)


async def test_a_note_at_the_cap_is_kept_byte_for_byte(tmp_path: Path) -> None:
    env = _env(tmp_path)
    content = os.urandom(MAX_PUBLICATION_BYTES)
    (env.vault / "n.md").write_bytes(content)

    assert (await env.apply("delete", ref="n.md")).status == "done"

    [entry] = env.entries()
    assert (env.recovery.root / f"{entry.recovery_id}.bin").read_bytes() == content
    assert entry.byte_size == MAX_PUBLICATION_BYTES


# Replays, a command still applying and bounded receipts


async def test_a_replay_answers_the_recorded_outcome_and_a_rewrite_conflicts(
    tmp_path: Path,
) -> None:
    env = _env(tmp_path)
    (env.vault / "n.md").write_bytes(b"note")
    command, loaded = str(uuid.uuid4()), _sha(b"note")

    first = await env.apply("rename", ref="n.md", to="m.md", command_id=command, expected=loaded)
    again = await env.apply("rename", ref="n.md", to="m.md", command_id=command, expected=loaded)

    assert first.replayed is False
    assert again == OperationOutcome(command, "done", True, 1)
    assert _tree(env.vault) == [("m.md", b"note")]
    await _refused(
        "command_conflict",
        env.apply("rename", ref="n.md", to="k.md", command_id=command, expected=loaded),
    )
    # The revision the user loaded is part of the request: another one is another request.
    await _refused(
        "command_conflict",
        env.apply("rename", ref="n.md", to="m.md", command_id=command, expected="f" * 64),
    )


async def test_a_command_still_applying_is_op_in_progress(tmp_path: Path) -> None:
    entered, release = threading.Event(), threading.Event()

    def hold() -> None:
        entered.set()
        assert release.wait(HANG_GUARD_S)

    env = _env(tmp_path, _Faulty(before_move=hold))
    (env.vault / "n.md").write_bytes(b"note")
    command = str(uuid.uuid4())
    running = asyncio.create_task(env.apply("rename", ref="n.md", to="m.md", command_id=command))
    try:
        assert await asyncio.to_thread(entered.wait, HANG_GUARD_S)
        await _refused(
            "op_in_progress", env.apply("rename", ref="n.md", to="m.md", command_id=command)
        )
    finally:
        release.set()
    assert (await running).status == "done"


async def test_receipts_are_bounded_oldest_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(vault_lifecycle, "MAX_RECEIPTS", 1)
    env = _env(tmp_path)
    (env.vault / "a.md").write_bytes(b"a")
    (env.vault / "b.md").write_bytes(b"b")
    first, second = str(uuid.uuid4()), str(uuid.uuid4())
    a, b = _sha(b"a"), _sha(b"b")
    await env.apply("rename", ref="a.md", to="a2.md", command_id=first, expected=a)
    await env.apply("rename", ref="b.md", to="b2.md", command_id=second, expected=b)

    replay = await env.apply("rename", ref="b.md", to="b2.md", command_id=second, expected=b)
    assert replay.replayed
    # The evicted receipt plans afresh, and its source is gone.
    await _refused(
        "missing_target",
        env.apply("rename", ref="a.md", to="a2.md", command_id=first, expected=a),
    )


# Admission, the lease and the two blocking journals


async def test_a_refused_admission_touches_nothing(tmp_path: Path) -> None:
    env = _env(tmp_path)
    (env.vault / "n.md").write_bytes(b"note")
    del env.roots[env.root.workspace_id]
    before = _tree(env.vault)

    await _refused("workspace_changed", env.apply("delete", ref="n.md"))
    assert (await env.preview("delete", ref="n.md")).refusal == "workspace_changed"
    assert _tree(env.vault) == before
    assert env.rows() == []
    assert env.recovery.usage() == (0, 0)


async def test_a_lease_held_past_the_deadline_is_vault_locked(tmp_path: Path) -> None:
    env = _env(tmp_path)
    (env.vault / "n.md").write_bytes(b"note")

    async with env.coordinator.lease(env.root, ("n.md",)):
        await _refused("vault_locked", env.apply("rename", ref="n.md", to="m.md"))

    assert _tree(env.vault) == [("n.md", b"note")]
    assert env.rows() == []


@pytest.mark.parametrize("held", ["x.md", "Y.md"])
async def test_a_live_publication_row_on_either_end_refuses(tmp_path: Path, held: str) -> None:
    env = _env(tmp_path)
    (env.vault / "x.md").write_bytes(b"x")
    env.publications.reserve(
        env.root, held, VaultPublication().reserve(), "create", "a" * 64, 1, None
    )

    await _refused("publication_busy", env.apply("rename", ref="x.md", to="y.md"))
    assert (await env.preview("rename", ref="x.md", to="y.md")).refusal == "publication_busy"
    assert _tree(env.vault) == [("x.md", b"x")]
    assert env.rows() == []


ENTRY = "00000000-0000-4000-8000-000000000001"


@pytest.mark.parametrize(
    ("kind", "fields"),
    [
        ("delete", {"ref": "n.md", "to": "m.md", "expected_sha256": "a" * 64}),
        ("rename", {"ref": "n.md", "expected_sha256": "a" * 64}),
        ("restore", {"ref": "n.md", "to": "n.md"}),
        ("move", {"recovery_id": ENTRY, "to": "m.md", "expected_sha256": "a" * 64}),
        ("copy", {"ref": "n.md", "to": "m.md", "expected_sha256": "a" * 64}),
        # A note's operation names the revision the user loaded; a restore names none.
        ("rename", {"ref": "n.md", "to": "m.md"}),
        ("delete", {"ref": "n.md", "expected_sha256": "A" * 64}),
        ("archive", {"ref": "n.md", "to": "archive/n.md", "expected_sha256": "a" * 63}),
        ("restore", {"recovery_id": ENTRY, "to": "n.md", "expected_sha256": "a" * 64}),
    ],
)
async def test_a_malformed_request_is_invalid_operation(
    tmp_path: Path, kind: str, fields: dict[str, str]
) -> None:
    env = _env(tmp_path)
    root = env.root
    ids = (root.workspace_id, root.policy_revision, root.physical_id, kind)
    await _refused("invalid_operation", env.service.apply(str(uuid.uuid4()), *ids, **fields))
    await _refused("invalid_operation", env.service.preview(*ids, **fields))


@pytest.mark.parametrize(("to", "code"), [("CLAUDE.md", "excluded_path"), ("a.png", "invalid_ref")])
async def test_an_excluded_or_non_note_destination_is_refused(
    tmp_path: Path, to: str, code: str
) -> None:
    env = _env(tmp_path)
    (env.vault / "n.md").write_bytes(b"note")

    await _refused(code, env.apply("rename", ref="n.md", to=to))
    assert (await env.preview("rename", ref="n.md", to=to)).refusal == code
    assert _tree(env.vault) == [("n.md", b"note")]


async def test_preview_shows_steps_and_folders_and_changes_nothing(tmp_path: Path) -> None:
    env = _env(tmp_path)
    (env.vault / "n.md").write_bytes(b"note")

    plan = await env.preview("move", ref="n.md", to="a/b/n.md")

    assert plan.steps == (
        PreviewStep(0, "mkdir", "a", None),
        PreviewStep(1, "mkdir", "a/b", None),
        PreviewStep(2, "move", "n.md", "a/b/n.md"),
    )
    assert (plan.creates_folders, plan.recovery_days, plan.refusal) == (("a", "a/b"), 30, None)
    assert _tree(env.vault) == [("n.md", b"note")]
    assert env.rows() == []
    assert (await env.preview("move", ref="gone.md", to="m.md")).refusal == "missing_target"


async def test_preview_refuses_an_unsupported_volume_as_apply_does(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A plan apply would refuse is no plan: the preview asks the volume as apply's lease does.
    env = _env(tmp_path)
    (env.vault / "n.md").write_bytes(b"note")
    monkeypatch.setattr(vault_mutation, "_supported_volume", lambda _path: False)

    plan = await env.preview("rename", ref="n.md", to="m.md")

    assert (plan.steps, plan.refusal) == ((), "unsupported_filesystem")
    await _refused("unsupported_filesystem", env.apply("rename", ref="n.md", to="m.md"))
    assert _tree(env.vault) == [("n.md", b"note")]
    assert env.rows() == []


# The undo matrix: what a failed step leaves decides the status


async def test_a_step_that_fails_before_it_changes_anything_is_failed_and_keeps_its_folder(
    tmp_path: Path,
) -> None:
    env = _env(tmp_path, _Faulty(before_move=_raise(MutationError("target_busy"))))
    (env.vault / "n.md").write_bytes(b"note")

    outcome = await env.apply("move", ref="n.md", to="a/n.md")

    assert (outcome.status, outcome.reason) == ("failed", "target_busy")
    assert (outcome.created_folders, outcome.steps_applied) == (("a",), 1)
    assert _tree(env.vault) == [("a", None), ("n.md", b"note")]
    assert env.rows() == []


async def test_a_step_refused_after_it_landed_is_done(tmp_path: Path) -> None:
    env = _env(tmp_path, _Faulty(after_move=_raise(MutationError("missing_target"))))
    (env.vault / "n.md").write_bytes(b"note")

    outcome = await env.apply("rename", ref="n.md", to="m.md")

    assert (outcome.status, outcome.reason) == ("done", None)
    assert _tree(env.vault) == [("m.md", b"note")]
    assert env.rows() == []


async def test_a_diverged_step_is_recovery_required_and_blocks_the_vault(tmp_path: Path) -> None:
    def overwrite_then_fail() -> None:
        (env.vault / "m.md").write_bytes(b"changed elsewhere")
        raise MutationError("missing_target")

    env = _env(tmp_path, _Faulty(after_move=overwrite_then_fail))
    (env.vault / "n.md").write_bytes(b"note")
    (env.vault / "other.md").write_bytes(b"other")
    command, loaded = str(uuid.uuid4()), _sha(b"note")

    outcome = await env.apply("rename", ref="n.md", to="m.md", command_id=command, expected=loaded)

    assert (outcome.status, outcome.reason) == ("recovery_required", "missing_target")
    assert env.rows() == [(command, "recovery_required")]
    env.launch()
    await _refused("vault_recovery_required", env.apply("delete", ref="other.md"))
    assert (await env.preview("delete", ref="other.md")).refusal == "vault_recovery_required"
    with pytest.raises(PublicationError) as caught:
        await asyncio.to_thread(env.service.require_writable, env.root.workspace_id)
    assert caught.value.code == "vault_recovery_required"
    # A replay after the receipts are gone still answers from the row.
    replay = await env.apply("rename", ref="n.md", to="m.md", command_id=command, expected=loaded)
    assert (replay.status, replay.replayed) == ("recovery_required", True)
    assert (env.vault / "other.md").read_bytes() == b"other"

    # The user forgets the row: no vault file changes, and the vault takes writes again.
    before = _tree(env.vault)
    assert await env.service.stuck(env.root.workspace_id) == [
        StuckOperation(command, "rename", "recovery_required", ("n.md", "m.md"))
    ]
    assert await env.service.forget(command) == ForgottenOperation(command, (), False)
    assert _tree(env.vault) == before
    assert env.rows() == []
    assert await env.service.stuck(env.root.workspace_id) == []
    assert (await env.apply("delete", ref="other.md")).status == "done"
    # The forgotten command now plans afresh instead of answering its old outcome.
    await _refused(
        "missing_target",
        env.apply("rename", ref="n.md", to="m.md", command_id=command, expected=loaded),
    )
    await _refused("unknown_operation", env.service.forget(command))
    await _refused("invalid_operation", env.service.forget("not-an-id"))


@pytest.mark.parametrize(
    ("kind", "to"),
    [("rename", "m.md"), ("move", "sub/n.md"), ("archive", "archive/n.md"), ("delete", None)],
)
async def test_a_stale_expected_hash_refuses_every_kind_before_any_change(
    tmp_path: Path, kind: str, to: str | None
) -> None:
    env = _env(tmp_path)
    (env.vault / "n.md").write_bytes(b"edited elsewhere")
    before = _tree(env.vault)
    stale, command = _sha(b"as the owner loaded it"), str(uuid.uuid4())

    assert (await env.preview(kind, ref="n.md", to=to, expected=stale)).refusal == "conflict"
    await _refused(
        "conflict", env.apply(kind, ref="n.md", to=to, command_id=command, expected=stale)
    )
    assert _tree(env.vault) == before
    assert env.rows() == []
    assert env.recovery.usage() == (0, 0)

    # The receipt replays the refusal for the same request, even once the note matches again.
    (env.vault / "n.md").write_bytes(b"as the owner loaded it")
    await _refused(
        "conflict", env.apply(kind, ref="n.md", to=to, command_id=command, expected=stale)
    )
    assert env.rows() == []
    # The current revision proceeds.
    assert (await env.apply(kind, ref="n.md", to=to, expected=stale)).status == "done"


async def test_a_transient_refusal_is_not_replayed(tmp_path: Path) -> None:
    env = _env(tmp_path)
    (env.vault / "n.md").write_bytes(b"note")
    command, loaded = str(uuid.uuid4()), _sha(b"note")

    async with env.coordinator.lease(env.root, ("n.md",)):
        await _refused(
            "vault_locked",
            env.apply("rename", ref="n.md", to="m.md", command_id=command, expected=loaded),
        )
    outcome = await env.apply("rename", ref="n.md", to="m.md", command_id=command, expected=loaded)

    assert (outcome.status, outcome.replayed) == ("done", False)


async def test_forget_clears_a_blocked_row_and_keeps_its_copy(tmp_path: Path) -> None:
    env = _env(tmp_path, _Faulty(before_remove=_raise(_Crash())))
    (env.vault / "n.md").write_bytes(b"note")
    command = str(uuid.uuid4())
    with pytest.raises(_Crash):
        await env.apply("delete", ref="n.md", command_id=command)
    [entry] = env.entries()
    del env.roots[env.root.workspace_id]
    env.launch()
    assert await env.service.reconcile() == []

    assert await env.service.stuck(env.root.workspace_id) == [
        StuckOperation(command, "delete", "applying", ("n.md",))
    ]
    assert await env.service.stuck(str(uuid.uuid4())) == []
    assert await env.service.forget(command) == ForgottenOperation(
        command, (entry.recovery_id,), False
    )

    assert env.rows() == []
    assert _tree(env.vault) == [("n.md", b"note")]
    assert env.entries() == [entry]


async def test_forget_drops_the_receipt_of_the_row_it_clears(tmp_path: Path) -> None:
    def diverge() -> None:
        (env.vault / "m.md").write_bytes(b"changed elsewhere")
        raise MutationError("missing_target")

    env = _env(tmp_path, _Faulty(after_move=diverge))
    (env.vault / "n.md").write_bytes(b"note")
    command, loaded = str(uuid.uuid4()), _sha(b"note")
    outcome = await env.apply("rename", ref="n.md", to="m.md", command_id=command, expected=loaded)
    assert outcome.status == "recovery_required"

    await env.service.forget(command)

    # Same launch, same command: it plans afresh rather than answering the cleared row.
    await _refused(
        "missing_target",
        env.apply("rename", ref="n.md", to="m.md", command_id=command, expected=loaded),
    )


async def test_forget_refuses_a_row_still_applying(tmp_path: Path) -> None:
    entered, release = threading.Event(), threading.Event()

    def hold() -> None:
        entered.set()
        assert release.wait(HANG_GUARD_S)

    env = _env(tmp_path, _Faulty(before_move=hold))
    (env.vault / "n.md").write_bytes(b"note")
    command = str(uuid.uuid4())
    running = asyncio.create_task(env.apply("rename", ref="n.md", to="m.md", command_id=command))
    try:
        assert await asyncio.to_thread(entered.wait, HANG_GUARD_S)
        await _refused("op_in_progress", env.service.forget(command))
        assert await env.service.stuck(env.root.workspace_id) == []
    finally:
        release.set()
    assert (await running).status == "done"


async def test_a_delete_whose_remove_fails_keeps_the_note_and_drops_its_copy(
    tmp_path: Path,
) -> None:
    env = _env(tmp_path, _Faulty(before_remove=_raise(MutationError("target_busy"))))
    (env.vault / "n.md").write_bytes(b"note")

    outcome = await env.apply("delete", ref="n.md")

    assert (outcome.status, outcome.reason) == ("failed", "target_busy")
    assert _tree(env.vault) == [("n.md", b"note")]
    assert env.entries() == []
    assert env.rows() == []


async def test_a_delete_whose_copy_lands_wrong_keeps_the_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hard rule 1: the copy is verified before the name goes. A write that silently lands one
    byte short refuses the delete, and neither the row nor the copy stays."""
    opened = osfs.open_new_private

    class _Short:
        def __init__(self, path: Path) -> None:
            self._handle = opened(path)

        def write(self, data: bytes) -> int:
            return self._handle.write(data[:-1])

        def __getattr__(self, name: str) -> Any:
            return getattr(self._handle, name)

        def __enter__(self) -> _Short:
            self._handle.__enter__()
            return self

        def __exit__(self, *exc: Any) -> None:
            self._handle.__exit__(*exc)

    monkeypatch.setattr(vault_operations.osfs, "open_new_private", _Short)
    env = _env(tmp_path)
    (env.vault / "n.md").write_bytes(b"keep me\n")

    await _refused("recovery_corrupt", env.apply("delete", ref="n.md"))

    assert _tree(env.vault) == [("n.md", b"keep me\n")]
    assert env.entries() == []
    assert list(env.recovery.root.iterdir()) == []
    assert env.rows() == []


async def test_a_restore_whose_publish_fails_discards_its_stage_and_keeps_the_entry(
    tmp_path: Path,
) -> None:
    env = _env(tmp_path)
    (env.vault / "n.md").write_bytes(b"note")
    await env.apply("delete", ref="n.md")
    [entry] = env.entries()
    env.launch(_Faulty(before_publish=_raise(OSError("synthetic"))))

    outcome = await env.apply("restore", recovery_id=entry.recovery_id, to="n.md")

    assert (outcome.status, outcome.reason) == ("failed", "unavailable")
    assert _tree(env.vault) == []
    assert env.entries() == [entry]
    assert env.rows() == []


# Reconcile: a crash at each boundary, decided at the next launch


@pytest.mark.parametrize(
    ("hook", "status", "tree"),
    [
        ("before_move", "failed", [("a", None), ("n.md", b"note")]),
        ("after_move", "done", [("a", None), ("a/m.md", b"note")]),
    ],
)
async def test_reconcile_decides_a_crashed_move_by_its_paths(
    tmp_path: Path, hook: str, status: str, tree: list[tuple[str, bytes | None]]
) -> None:
    env = _env(tmp_path, _Faulty(**{hook: _raise(_Crash())}))
    (env.vault / "n.md").write_bytes(b"note")
    command, loaded = str(uuid.uuid4()), _sha(b"note")
    with pytest.raises(_Crash):
        await env.apply("move", ref="n.md", to="a/m.md", command_id=command, expected=loaded)
    assert env.rows() == [(command, "applying")]

    env.launch()
    [outcome] = await env.service.reconcile()

    assert (outcome.operation_id, outcome.status) == (command, status)
    assert _tree(env.vault) == tree
    assert env.rows() == []
    replay = await env.apply("move", ref="n.md", to="a/m.md", command_id=command, expected=loaded)
    assert (replay.status, replay.replayed) == (status, True)


@pytest.mark.parametrize(
    ("hook", "status", "kept"), [("before_remove", "failed", True), ("after_remove", "done", False)]
)
async def test_reconcile_decides_a_crashed_delete_and_its_copy(
    tmp_path: Path, hook: str, status: str, kept: bool
) -> None:
    env = _env(tmp_path, _Faulty(**{hook: _raise(_Crash())}))
    (env.vault / "n.md").write_bytes(b"note")
    with pytest.raises(_Crash):
        await env.apply("delete", ref="n.md")

    env.launch()
    [outcome] = await env.service.reconcile()

    assert outcome.status == status
    assert (env.vault / "n.md").exists() is kept
    # A note still there drops the copy; a removed note keeps it as its recovery entry.
    assert [entry.ref for entry in env.entries()] == ([] if kept else ["n.md"])
    assert env.rows() == []


async def test_reconcile_keeps_the_copy_of_a_deleted_name_that_now_holds_other_bytes(
    tmp_path: Path,
) -> None:
    env = _env(tmp_path, _Faulty(after_remove=_raise(_Crash())))
    (env.vault / "n.md").write_bytes(b"the owner's note")
    with pytest.raises(_Crash):
        await env.apply("delete", ref="n.md")
    assert not (env.vault / "n.md").exists()
    # A sync client or another tool fills the deleted name before the next launch.
    (env.vault / "n.md").write_bytes(b"someone else's file")

    env.launch()
    [outcome] = await env.service.reconcile()

    # The name is diverged, so the row blocks the vault and the only copy of the note stays.
    assert outcome.status == "recovery_required"
    [entry] = env.entries()
    assert env.recovery.read(entry.recovery_id)[1] == b"the owner's note"
    assert (env.vault / "n.md").read_bytes() == b"someone else's file"
    with pytest.raises(PublicationError) as caught:
        await asyncio.to_thread(env.service.require_writable, env.root.workspace_id)
    assert caught.value.code == "vault_recovery_required"


@pytest.mark.parametrize(("hook", "status"), [("after_stage", "failed"), ("after_publish", "done")])
async def test_reconcile_decides_a_crashed_restore_and_clears_its_stage(
    tmp_path: Path, hook: str, status: str
) -> None:
    env = _env(tmp_path)
    (env.vault / "n.md").write_bytes(b"note")
    await env.apply("delete", ref="n.md")
    [entry] = env.entries()
    env.launch(_Faulty(**{hook: _raise(_Crash())}))
    with pytest.raises(_Crash):
        await env.apply("restore", recovery_id=entry.recovery_id, to="n.md")
    assert any(path.name.startswith(".viki-stage-") for path in env.vault.iterdir()) is (
        hook == "after_stage"
    )

    env.launch()
    [outcome] = await env.service.reconcile()

    assert outcome.status == status
    assert _tree(env.vault) == ([("n.md", b"note")] if status == "done" else [])
    assert env.entries() == ([] if status == "done" else [entry])
    assert env.rows() == []


async def test_reconcile_leaves_a_diverged_row_and_blocks_an_unadmitted_one_alone(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    env = _env(tmp_path, _Faulty(after_move=_raise(_Crash())))
    other = _root(tmp_path / "other")
    env.roots[other.workspace_id] = other
    (env.vault / "n.md").write_bytes(b"note")
    (other.path / "secret-name.md").write_bytes(b"theirs")
    diverged, blocked = str(uuid.uuid4()), str(uuid.uuid4())
    with pytest.raises(_Crash):
        await env.apply("rename", ref="n.md", to="m.md", command_id=diverged)
    with pytest.raises(_Crash):
        await env.apply(
            "rename", ref="secret-name.md", to="secret-new.md", command_id=blocked, root=other
        )
    (env.vault / "m.md").write_bytes(b"changed elsewhere")
    del env.roots[other.workspace_id]

    env.launch()
    with caplog.at_level(logging.WARNING, logger="viki_agent.vaults.vault_lifecycle"):
        outcomes = await env.service.reconcile()

    assert [(item.operation_id, item.status) for item in outcomes] == [
        (diverged, "recovery_required")
    ]
    assert sorted(env.rows()) == sorted([(diverged, "recovery_required"), (blocked, "applying")])
    assert [record.getMessage() for record in caplog.records] == [
        f"vault operation {blocked} blocked: rename applying"
    ]
    assert "secret" not in caplog.text
    assert (env.vault / "m.md").read_bytes() == b"changed elsewhere"
