from __future__ import annotations

import ctypes
import errno
import hashlib
import os
import shutil
import sqlite3
import stat
import sys
import uuid
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any, Literal, cast
from unittest.mock import Mock

import pytest

from viki_agent import winproc
from viki_agent.storage import SessionStore, StateError
from viki_agent.vaults import vault_mutation
from viki_agent.vaults.vault import (
    BACKUP_PREFIX,
    EXCLUDED_DIRS,
    PRIVATE_NAMES,
    STAGE_PREFIX,
    WRITE_ONLY_NAMES,
)
from viki_agent.vaults.vault_mutation import (
    FileFingerprint,
    MutationCoordinator,
    MutationError,
    MutationLease,
    OrdinaryMutationRoot,
    PreparedReplacement,
    PublicationReservation,
    PublishedPublication,
    StagedPublication,
    VaultPublication,
)
from viki_agent.vaults.vault_publication import (
    JOURNAL_SCHEMA,
    PublicationError,
    PublicationJournal,
    PublicationOutcome,
    PublicationRow,
    VaultPublicationService,
)
from viki_agent.workspaces import root_key

_WIN32_HOLD = pytest.mark.skipif(sys.platform != "win32", reason="needs a Win32 sharing-mode hold")
_WIN32_SHORT_NAME = pytest.mark.skipif(
    sys.platform != "win32", reason="needs a Windows 8.3 short name"
)


_PUBLICATION_CRASH = r"""
import asyncio
import hashlib
import os
import sys
import uuid
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from viki_agent.storage import SessionStore
from viki_agent.vaults.vault_mutation import (
    MutationCoordinator, OrdinaryMutationRoot, VaultPublication,
)
from viki_agent.vaults.vault_publication import PublicationJournal
from viki_agent.workspaces import root_key

source, checkpoint, kind, vault_arg, database_arg, workspace_id = sys.argv[1:]
vault = Path(vault_arg).resolve()
info = vault.stat()
root = OrdinaryMutationRoot(
    workspace_id,
    vault,
    root_key(vault),
    f"{info.st_dev}:{info.st_ino}",
    7,
    None,
)
journal = PublicationJournal(SessionStore(Path(database_arg), lambda: {}))
native = VaultPublication()
content = b"new"
expected = hashlib.sha256(b"old").hexdigest() if kind == "replace" else None

def stop(name):
    if checkpoint == name:
        os._exit(73)

async def run():
    reservation = native.reserve()
    row = journal.reserve(
        root,
        "note.md",
        reservation,
        kind,
        hashlib.sha256(content).hexdigest(),
        len(content),
        expected,
    )
    stop("reserved")
    async with MutationCoordinator().lease(root, ("note.md",)) as lease:
        staged = native.stage(lease, root, "note.md", reservation, content)
        stop("stage_uncheckpointed")
        row = journal.record_stage(row.operation_id, staged.physical_id)
        stop("staged")
        if kind == "create":
            native.publish_create(lease, staged)
            stop("renamed")
            journal.transition(row.operation_id, "staged", "published")
            stop("published")
            journal.delete(row.operation_id, "published")
            stop("deleted")
            return
        with native.prepare_replace(lease, staged, expected, staged.backup_name) as prepared:
            journal.record_prepared(row.operation_id, prepared.old)
            stop("prepared")
            prepared.move_target_to_backup()
            stop("backup_renamed")
            journal.transition(row.operation_id, "prepared", "backed_up")
            stop("backed_up")
            prepared.publish_stage_no_replace()
            stop("stage_renamed")
            prepared.verify_published()
            stop("verified")
            journal.transition(row.operation_id, "backed_up", "published")
            stop("published")
        native.discard_backup_after_publish(
            lease,
            root,
            row.ref,
            row.backup_name,
            staged.fingerprint,
            prepared.old,
        )
        stop("backup_deleted")
        journal.delete(row.operation_id, "published")
        stop("deleted")

asyncio.run(run())
os._exit(74)
"""


class _Store:
    def __init__(self, path: Path) -> None:
        self.path = path
        with sqlite3.connect(path) as connection:
            connection.execute(JOURNAL_SCHEMA)

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


class _RaceOnMove(VaultPublication):
    def __init__(self, source_name: str) -> None:
        self._source_name = source_name
        self._raced = False

    def move_verified_no_replace(
        self,
        lease: MutationLease,
        root: OrdinaryMutationRoot,
        ref: str,
        source_name: str,
        expected: FileFingerprint,
    ) -> PublishedPublication:
        if source_name == self._source_name and not self._raced:
            self._raced = True
            root.path.joinpath(*ref.split("/")).write_bytes(b"external")
        return super().move_verified_no_replace(lease, root, ref, source_name, expected)


class _CreateOSError(VaultPublication):
    def publish_create(
        self, lease: MutationLease, staged: StagedPublication
    ) -> PublishedPublication:
        raise OSError("synthetic")


class _CreateMutationAfterRename(VaultPublication):
    def publish_create(
        self, lease: MutationLease, staged: StagedPublication
    ) -> PublishedPublication:
        super().publish_create(lease, staged)
        raise MutationError("missing_target", stage_retained=True)


class _ProbeOSError(VaultPublication):
    def probe_verified(
        self,
        lease: MutationLease,
        root: OrdinaryMutationRoot,
        ref: str,
        name: str | None,
        expected: FileFingerprint,
    ) -> Literal["absent", "match", "unexpected"]:
        if ref == "bad.md":
            raise OSError("synthetic")
        return super().probe_verified(lease, root, ref, name, expected)


def _root(path: Path) -> OrdinaryMutationRoot:
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


def _journal(tmp_path: Path) -> PublicationJournal:
    store = _Store(tmp_path / "journal.sqlite3")
    return PublicationJournal(cast(Any, store))


def _service(
    journal: PublicationJournal,
    root: OrdinaryMutationRoot,
    native: VaultPublication | None = None,
) -> VaultPublicationService:
    def resolve(workspace_id: str, revision: int, physical_id: str) -> OrdinaryMutationRoot:
        assert (workspace_id, revision, physical_id) == (
            root.workspace_id,
            root.policy_revision,
            root.physical_id,
        )
        return root

    return VaultPublicationService(journal, resolve, MutationCoordinator(), native)


def test_journal_enforces_busy_and_compare_and_set(tmp_path: Path) -> None:
    root = _root(tmp_path)
    journal = _journal(tmp_path)
    native = VaultPublication()
    reservation = native.reserve()
    row = journal.reserve(
        root,
        "note.md",
        reservation,
        "replace",
        hashlib.sha256(b"new").hexdigest(),
        3,
        hashlib.sha256(b"old").hexdigest(),
    )
    with pytest.raises(PublicationError, match="publication_busy"):
        journal.reserve(
            root,
            "note.md",
            native.reserve(),
            "create",
            hashlib.sha256(b"other").hexdigest(),
            5,
            None,
        )
    staged = journal.record_stage(row.operation_id, "volume:file")
    assert staged.state == "staged"
    with pytest.raises(PublicationError, match="recovery_required"):
        journal.record_stage(row.operation_id, "volume:file")


@pytest.mark.parametrize("ref", ["../note.md", ".git/config", "CLAUDE.md", "a\\b.md"])
def test_journal_rejects_invalid_or_excluded_ref_before_commit(tmp_path: Path, ref: str) -> None:
    root = _root(tmp_path)
    journal = _journal(tmp_path)
    with pytest.raises(PublicationError, match="invalid_publication"):
        journal.reserve(
            root,
            ref,
            VaultPublication().reserve(),
            "create",
            hashlib.sha256(b"new").hexdigest(),
            3,
            None,
        )
    assert journal.list_pending() == []


def _mixed(name: str) -> str:
    return "".join(c.upper() if i % 2 else c for i, c in enumerate(name))


def test_journal_refuses_every_excluded_name_and_non_markdown_leaf(tmp_path: Path) -> None:
    names = (
        *EXCLUDED_DIRS,
        *PRIVATE_NAMES,
        ".env.synthetic",
        *WRITE_ONLY_NAMES,
        f"{STAGE_PREFIX}x.md",
        f"{BACKUP_PREFIX}x.md",
    )
    refused = [
        *(
            f"{parent}{_mixed(name)}{leaf}"
            for name in names
            for parent in ("", "sub/", "a/b/")
            for leaf in ("/n.md", "")
        ),
        ".md",
        "..md",
        "notes/.MD",
        "x.markdown",
    ]
    root = _root(tmp_path)
    journal = _journal(tmp_path)
    for ref in refused:
        with pytest.raises(PublicationError, match="invalid_publication"):
            journal.reserve(
                root,
                ref,
                VaultPublication().reserve(),
                "create",
                hashlib.sha256(b"new").hexdigest(),
                3,
                None,
            )
    assert journal.list_pending() == []


def test_journal_keeps_reading_rows_whose_names_ntfs_refuses(tmp_path: Path) -> None:
    """The writer refuses these names; the journal still reads a row written before it did."""
    root = _root(tmp_path)
    journal = _journal(tmp_path)
    row = journal.reserve(
        root,
        "what?.md",
        VaultPublication().reserve(),
        "create",
        hashlib.sha256(b"new").hexdigest(),
        3,
        None,
    )
    assert journal.list_pending() == [row]


def test_journal_uses_the_integrated_session_store_schema(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    root = _root(vault)
    store = SessionStore(tmp_path / "app" / "state.sqlite3", lambda: {})
    journal = PublicationJournal(store)
    reservation = VaultPublication().reserve()
    row = journal.reserve(
        root,
        "note.md",
        reservation,
        "create",
        hashlib.sha256(b"new").hexdigest(),
        3,
        None,
    )
    assert row.state == "reserved"
    assert journal.list_pending() == [row]


@pytest.mark.parametrize(
    ("kind", "checkpoint", "expected_status", "expected_bytes"),
    [
        ("create", "reserved", "recovered", None),
        ("create", "stage_uncheckpointed", "blocked", None),
        ("create", "staged", "recovered", b"new"),
        ("create", "renamed", "recovered", b"new"),
        ("create", "published", "recovered", b"new"),
        ("create", "deleted", None, b"new"),
        ("replace", "reserved", "recovered", b"old"),
        ("replace", "stage_uncheckpointed", "blocked", b"old"),
        ("replace", "staged", "saved", b"new"),
        ("replace", "prepared", "recovered", b"new"),
        ("replace", "backup_renamed", "recovered", b"new"),
        ("replace", "backed_up", "recovered", b"new"),
        ("replace", "stage_renamed", "recovered", b"new"),
        ("replace", "verified", "recovered", b"new"),
        ("replace", "published", "recovered", b"new"),
        ("replace", "backup_deleted", "recovered", b"new"),
        ("replace", "deleted", None, b"new"),
    ],
)
async def test_real_process_exit_recovery_matrix(
    tmp_path: Path,
    kind: Literal["create", "replace"],
    checkpoint: str,
    expected_status: str | None,
    expected_bytes: bytes | None,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    root = _root(vault)
    target = vault / "note.md"
    if kind == "replace":
        target.write_bytes(b"old")
    database = tmp_path / "state.sqlite3"
    result = winproc.run(
        [
            sys.executable,
            "-I",
            "-c",
            _PUBLICATION_CRASH,
            str(Path(__file__).parents[2] / "src"),
            checkpoint,
            kind,
            str(vault),
            str(database),
            root.workspace_id,
        ],
        cwd=tmp_path,
    )
    assert result.returncode == 73, result.stderr

    journal = PublicationJournal(SessionStore(database, lambda: {}))
    service = _service(journal, root)
    first = await service.recover_pending()
    second = await service.recover_pending()
    if expected_status is None:
        assert first == second == []
    else:
        assert [outcome.status for outcome in first] == [expected_status]
        if expected_status == "blocked":
            assert [outcome.status for outcome in second] == ["blocked"]
        else:
            assert second == []
    assert (target.read_bytes() if target.exists() else None) == expected_bytes


def test_forged_journal_names_fail_before_any_file_is_touched(tmp_path: Path) -> None:
    root = _root(tmp_path)
    store = _Store(tmp_path / "journal.sqlite3")
    journal = PublicationJournal(cast(Any, store))
    native = VaultPublication()
    reservation = native.reserve()
    row = journal.reserve(
        root,
        "note.md",
        reservation,
        "create",
        hashlib.sha256(b"new").hexdigest(),
        3,
        None,
    )
    forged = f".viki-stage-{uuid.uuid4()}.tmp"
    marker = tmp_path / forged
    marker.write_bytes(b"external")
    with store._connection(write=True) as connection:
        connection.execute(
            "UPDATE vault_publications SET stage_name=? WHERE operation_id=?",
            (forged, row.operation_id),
        )
    with pytest.raises(PublicationError, match="journal_corrupt"):
        journal.list_pending()
    assert marker.read_bytes() == b"external"


def test_journal_caps_live_rows_at_128(tmp_path: Path) -> None:
    root = _root(tmp_path)
    journal = _journal(tmp_path)
    native = VaultPublication()
    digest = hashlib.sha256(b"").hexdigest()
    for index in range(128):
        journal.reserve(root, f"{index}.md", native.reserve(), "create", digest, 0, None)
    with pytest.raises(PublicationError, match="publication_limit"):
        journal.reserve(root, "full.md", native.reserve(), "create", digest, 0, None)


async def test_create_and_replace_complete_and_clear_the_journal(tmp_path: Path) -> None:
    root = _root(tmp_path)
    journal = _journal(tmp_path)
    service = _service(journal, root)
    created = await service.save(
        root.workspace_id,
        root.policy_revision,
        root.physical_id,
        "note.md",
        b"first",
        expected_sha256=None,
    )
    assert created.status == "saved"
    assert (tmp_path / "note.md").read_bytes() == b"first"
    assert journal.list_pending() == []

    replaced = await service.save(
        root.workspace_id,
        root.policy_revision,
        root.physical_id,
        "note.md",
        b"second",
        expected_sha256=hashlib.sha256(b"first").hexdigest(),
    )
    assert replaced.status == "saved" and not replaced.cleanup_pending
    assert (tmp_path / "note.md").read_bytes() == b"second"
    assert journal.list_pending() == []
    assert not list(tmp_path.glob(".viki-*"))


@pytest.mark.parametrize(
    ("ref", "expected", "create_folders"),
    [
        ("bot-memory/note.md", None, True),
        ("BoT-MeMoRy/note.md", None, True),
        ("bot-memory/note.md", hashlib.sha256(b"old").hexdigest(), False),
    ],
)
async def test_direct_vault_save_refuses_bot_memory_before_reservation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ref: str,
    expected: str | None,
    create_folders: bool,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    root = _root(vault)
    journal = _journal(tmp_path)
    if expected is not None:
        target = vault / ref
        target.parent.mkdir()
        target.write_bytes(b"old")
    before = sorted(
        (path.relative_to(vault).as_posix(), path.read_bytes()) for path in vault.rglob("*.md")
    )
    before_paths = sorted(path.relative_to(vault).as_posix() for path in vault.rglob("*"))
    reserve = Mock(wraps=journal.reserve)
    monkeypatch.setattr(journal, "reserve", reserve)

    with pytest.raises(PublicationError) as caught:
        await _service(journal, root).save(
            root.workspace_id,
            root.policy_revision,
            root.physical_id,
            ref,
            b"new",
            expected_sha256=expected,
            create_folders=create_folders,
        )

    assert caught.value.code == "excluded_path"
    assert reserve.call_count == 0
    assert journal.list_pending() == []
    assert (
        sorted(
            (path.relative_to(vault).as_posix(), path.read_bytes()) for path in vault.rglob("*.md")
        )
        == before
    )
    assert sorted(path.relative_to(vault).as_posix() for path in vault.rglob("*")) == before_paths
    assert not list(vault.rglob(".viki-*"))


async def test_code_folder_save_can_use_bot_memory_name(tmp_path: Path) -> None:
    code = tmp_path / "code"
    code.mkdir()
    root = _root(code)
    root = replace(root, kind="code")
    (code / "bot-memory").mkdir()
    journal = _journal(tmp_path)
    outcome = await _service(journal, root).save_code(
        root.workspace_id,
        root.policy_revision,
        root.physical_id,
        "bot-memory/note.md",
        b"code",
        expected_sha256=None,
    )
    assert outcome.status == "saved"
    assert (code / "bot-memory" / "note.md").read_bytes() == b"code"
    assert journal.list_pending() == []


async def test_same_content_is_a_no_change_and_stale_hash_discards_stage(tmp_path: Path) -> None:
    root = _root(tmp_path)
    target = tmp_path / "note.md"
    target.write_bytes(b"old")
    journal = _journal(tmp_path)
    service = _service(journal, root)
    unchanged = await service.save(
        root.workspace_id,
        root.policy_revision,
        root.physical_id,
        "note.md",
        b"old",
        expected_sha256=hashlib.sha256(b"old").hexdigest(),
    )
    assert unchanged.status == "no_change"
    assert journal.list_pending() == []

    conflict = await service.save(
        root.workspace_id,
        root.policy_revision,
        root.physical_id,
        "note.md",
        b"new",
        expected_sha256="0" * 64,
    )
    # A refusal before `prepared` leaves no row and no stage behind.
    assert conflict.status == "conflict" and not conflict.cleanup_pending
    assert journal.list_pending() == []
    assert target.read_bytes() == b"old"
    assert not list(tmp_path.glob(".viki-*"))


async def test_create_collision_preserves_external_target_and_leaves_no_row(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    journal = _journal(tmp_path)
    service = _service(journal, root)
    (tmp_path / "note.md").write_bytes(b"external")
    outcome = await service.save(
        root.workspace_id,
        root.policy_revision,
        root.physical_id,
        "note.md",
        b"new",
        expected_sha256=None,
    )
    assert outcome.status == "conflict" and not outcome.cleanup_pending
    assert journal.list_pending() == []
    assert (tmp_path / "note.md").read_bytes() == b"external"
    assert not list(tmp_path.glob(".viki-*"))


async def test_a_raw_oserror_before_the_create_rename_discards(tmp_path: Path) -> None:
    """Our stage still at its name proves the rename did not happen, so the save is discarded and
    leaves no row or stage."""
    root = _root(tmp_path)
    journal = _journal(tmp_path)
    service = _service(journal, root, _CreateOSError())
    with pytest.raises(OSError, match="synthetic"):
        await _save_new(service, root, "note.md")
    assert journal.list_pending() == []
    assert not list(tmp_path.glob(".viki-*"))
    assert not (tmp_path / "note.md").exists()


async def test_post_rename_mutation_error_is_recovery_required(tmp_path: Path) -> None:
    root = _root(tmp_path)
    journal = _journal(tmp_path)
    service = _service(journal, root, _CreateMutationAfterRename())
    with pytest.raises(PublicationError) as caught:
        await service.save(
            root.workspace_id,
            root.policy_revision,
            root.physical_id,
            "note.md",
            b"new",
            expected_sha256=None,
        )
    [row] = journal.list_pending()
    # The stage is gone, so the rename may have happened; the row keeps its ID.
    assert (caught.value.code, caught.value.operation_id) == ("recovery_required", row.operation_id)
    assert (tmp_path / "note.md").read_bytes() == b"new"
    assert row.state == "staged"
    assert await service.recover_pending() == [PublicationOutcome(row.operation_id, "recovered")]
    assert journal.list_pending() == []
    assert (tmp_path / "note.md").read_bytes() == b"new"


async def test_checkpoint_failure_after_create_rename_is_typed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _root(tmp_path)
    journal = _journal(tmp_path)

    def fail_transition(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("synthetic")

    monkeypatch.setattr(journal, "transition", fail_transition)
    with pytest.raises(PublicationError) as caught:
        await _service(journal, root).save(
            root.workspace_id,
            root.policy_revision,
            root.physical_id,
            "note.md",
            b"new",
            expected_sha256=None,
        )
    assert caught.value.code == "recovery_required"
    assert (tmp_path / "note.md").read_bytes() == b"new"
    [row] = journal.list_pending()
    assert row.state == "staged"


async def test_an_uncertain_stage_checkpoint_deletes_the_stage_and_keeps_the_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`staged` may have committed, so the row stays; with its stage gone, the next
    launch deletes it."""
    root = _root(tmp_path)
    journal = _journal(tmp_path)

    def fail_record(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("synthetic")

    monkeypatch.setattr(journal, "record_stage", fail_record)
    with pytest.raises(PublicationError) as caught:
        await _service(journal, root).save(
            root.workspace_id,
            root.policy_revision,
            root.physical_id,
            "note.md",
            b"new",
            expected_sha256=None,
        )
    [row] = journal.list_pending()
    assert (caught.value.code, caught.value.operation_id) == ("recovery_required", row.operation_id)
    assert row.state == "reserved"
    assert not list(tmp_path.glob(".viki-*"))
    assert not (tmp_path / "note.md").exists()

    assert await _service(journal, root).recover_pending() == [
        PublicationOutcome(row.operation_id, "recovered")
    ]
    assert journal.list_pending() == []
    assert not (tmp_path / "note.md").exists()


@pytest.mark.parametrize("failure", [StateError("busy"), RuntimeError("synthetic")])
async def test_a_failed_backed_up_checkpoint_still_publishes_the_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    """The note has moved to its backup, so the save goes on to publish and commits `published`
    from `prepared`; the note is never missing once the save returns."""
    root = _root(tmp_path)
    target = tmp_path / "note.md"
    target.write_bytes(b"old")
    journal = _journal(tmp_path)
    real_transition = journal.transition

    def fail_backed_up(operation_id: str, expected: Any, state: Any) -> object:
        if state == "backed_up":
            raise failure
        return real_transition(operation_id, expected, state)

    monkeypatch.setattr(journal, "transition", fail_backed_up)
    outcome = await _save(_service(journal, root), root, b"new", _sha(b"old"))
    assert outcome == PublicationOutcome(outcome.operation_id, "saved")
    assert target.read_bytes() == b"new"
    assert journal.list_pending() == []
    assert not list(tmp_path.glob(".viki-*"))


class _RacerAfterBackup(VaultPublication):
    """Another process creates the note's name in the gap between the two renames."""

    @contextmanager
    def prepare_replace(
        self,
        lease: MutationLease,
        staged: StagedPublication,
        expected_sha256: str,
        backup_name: str,
    ) -> Iterator[PreparedReplacement]:
        with super().prepare_replace(lease, staged, expected_sha256, backup_name) as prepared:
            move = prepared.move_target_to_backup

            def move_then_race() -> None:
                move()
                staged.root.path.joinpath(*staged.ref.split("/")).write_bytes(b"external")

            prepared.move_target_to_backup = move_then_race  # type: ignore[method-assign]
            yield prepared


async def test_a_gap_collision_after_a_busy_backed_up_checkpoint_commits_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """As `published` is, `conflict` is committed from `prepared`, and every file stays."""
    root = _root(tmp_path)
    target = tmp_path / "note.md"
    target.write_bytes(b"old")
    journal = _journal(tmp_path)
    real_transition = journal.transition

    def busy_backed_up(operation_id: str, expected: Any, state: Any) -> object:
        if state == "backed_up":
            raise StateError("busy")
        return real_transition(operation_id, expected, state)

    monkeypatch.setattr(journal, "transition", busy_backed_up)
    outcome = await _save(_service(journal, root, _RacerAfterBackup()), root, b"new", _sha(b"old"))
    [row] = journal.list_pending()
    assert outcome == PublicationOutcome(row.operation_id, "conflict", True)
    assert row.state == "conflict"
    assert target.read_bytes() == b"external"
    assert (tmp_path / row.backup_name).read_bytes() == b"old"
    assert (tmp_path / row.stage_name).read_bytes() == b"new"


def _refuse_backup_rename(
    monkeypatch: pytest.MonkeyPatch, code: str, *, after_move: bool = False
) -> list[str]:
    """The backup rename refuses `code`. `after_move` runs the real rename first, as an undo
    that failed leaves the note at its backup name. Returns the refusals made."""
    real_move = PreparedReplacement.move_target_to_backup
    refused: list[str] = []

    def refuse(prepared: PreparedReplacement) -> None:
        if after_move:
            real_move(prepared)
        refused.append(code)
        raise MutationError(code, stage_retained=True)

    monkeypatch.setattr(PreparedReplacement, "move_target_to_backup", refuse)
    return refused


async def test_a_conflict_before_the_backup_rename_discards_the_stage_and_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Off Windows the backup rename re-checks the note and answers `conflict` having moved
    nothing, so the save is refused like any other refusal before `prepared`: its stage and row
    are discarded."""
    root = _root(tmp_path)
    note = tmp_path / "note.md"
    note.write_bytes(b"old")
    journal = _journal(tmp_path)
    refused = _refuse_backup_rename(monkeypatch, "conflict")
    outcome = await _save(_service(journal, root), root, b"new", _sha(b"old"))
    assert refused == ["conflict"]
    assert outcome == PublicationOutcome(outcome.operation_id, "conflict")
    assert journal.list_pending() == []
    assert not list(tmp_path.glob(".viki-*"))
    assert note.read_bytes() == b"old"


async def test_a_read_only_refusal_before_the_backup_rename_moves_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A backup rename the kernel refuses (off Windows: a folder without write permission or a
    Linux immutable note) moved nothing: the stage and row go, and the typed refusal stands."""
    root = _root(tmp_path)
    note = tmp_path / "note.md"
    note.write_bytes(b"old")
    journal = _journal(tmp_path)
    refused = _refuse_backup_rename(monkeypatch, "read_only_target")
    with pytest.raises(MutationError) as caught:
        await _save(_service(journal, root), root, b"new", _sha(b"old"))
    assert refused == ["read_only_target"]
    assert caught.value.code == "read_only_target"
    assert journal.list_pending() == []
    assert not list(tmp_path.glob(".viki-*"))
    assert note.read_bytes() == b"old"


async def test_a_failed_undo_of_the_backup_rename_keeps_its_row_for_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only `conflict` and `read_only_target` mean that nothing moved: an undo that failed
    (`unsafe_target`) can leave the note at its backup name, so the row stays and the next
    launch completes the save."""
    root = _root(tmp_path)
    note = tmp_path / "note.md"
    note.write_bytes(b"old")
    journal = _journal(tmp_path)
    _refuse_backup_rename(monkeypatch, "unsafe_target", after_move=True)
    with pytest.raises(PublicationError) as caught:
        await _save(_service(journal, root), root, b"new", _sha(b"old"))
    [row] = journal.list_pending()
    assert (caught.value.code, caught.value.operation_id) == ("recovery_required", row.operation_id)
    assert row.state == "prepared"
    assert (tmp_path / row.backup_name).read_bytes() == b"old"
    assert (tmp_path / row.stage_name).read_bytes() == b"new"

    monkeypatch.undo()
    assert await _service(journal, root).recover_pending() == [
        PublicationOutcome(row.operation_id, "recovered")
    ]
    assert note.read_bytes() == b"new"
    assert journal.list_pending() == []
    assert not list(tmp_path.glob(".viki-*"))


async def test_a_raw_publish_failure_after_the_backup_keeps_the_stage_and_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a refusal of the backup rename itself moved nothing: a full disk at the publish
    rename, after a good backup rename, keeps the stage with the new bytes and the `backed_up`
    row, and the next launch completes the save."""
    root = _root(tmp_path)
    note = tmp_path / "note.md"
    note.write_bytes(b"old")
    journal = _journal(tmp_path)

    def disk_full(prepared: PreparedReplacement) -> None:
        raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))

    monkeypatch.setattr(PreparedReplacement, "publish_stage_no_replace", disk_full)
    with pytest.raises(PublicationError) as caught:
        await _save(_service(journal, root), root, b"new", _sha(b"old"))
    [row] = journal.list_pending()
    assert (caught.value.code, caught.value.operation_id) == ("recovery_required", row.operation_id)
    assert row.state == "backed_up"
    assert (tmp_path / row.stage_name).read_bytes() == b"new"
    assert (tmp_path / row.backup_name).read_bytes() == b"old"

    monkeypatch.undo()
    assert await _service(journal, root).recover_pending() == [
        PublicationOutcome(row.operation_id, "recovered")
    ]
    assert note.read_bytes() == b"new"
    assert journal.list_pending() == []
    assert not list(tmp_path.glob(".viki-*"))


@pytest.mark.skipif(
    sys.platform == "win32", reason="Windows sharing modes keep an editor off the held note"
)
@pytest.mark.parametrize("in_place", [False, True])
async def test_a_save_racing_an_editor_answers_conflict_and_keeps_the_editors_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, in_place: bool
) -> None:
    """An editor saves the note just before the backup rename, by replacing it as an
    atomic save does or by writing into it. The rename's checks see it, so the note keeps the
    editor's bytes and the save answers `conflict`, leaving no stage, backup or row."""
    root = _root(tmp_path)
    note = tmp_path / "note.md"
    note.write_bytes(b"old")
    journal = _journal(tmp_path)
    real_move = PreparedReplacement.move_target_to_backup

    def edit_then_move(prepared: PreparedReplacement) -> None:
        if in_place:
            note.write_bytes(b"editor")
        else:
            swap = tmp_path / "editor-save.tmp"
            swap.write_bytes(b"editor")
            swap.replace(note)
        real_move(prepared)

    monkeypatch.setattr(PreparedReplacement, "move_target_to_backup", edit_then_move)
    outcome = await _save(_service(journal, root), root, b"new", _sha(b"old"))
    assert outcome == PublicationOutcome(outcome.operation_id, "conflict")
    assert note.read_bytes() == b"editor"
    assert journal.list_pending() == []
    assert not list(tmp_path.glob(".viki-*"))
    assert await _service(journal, root).recover_pending() == []


def _saved_over_after_the_rename(monkeypatch: pytest.MonkeyPatch, note: Path) -> None:
    """Just after the stage's no-replace rename onto `note`, an editor saves `note` once, as an
    atomic save does: a new file of its own, renamed over the name."""
    assert sys.platform != "win32"
    real_rename = vault_mutation.osfs.rename_at_no_replace

    def racing(src_dir_fd: int, src: str, dst_dir_fd: int, dst: str) -> None:
        real_rename(src_dir_fd, src, dst_dir_fd, dst)
        if dst == note.name:
            monkeypatch.setattr(vault_mutation.osfs, "rename_at_no_replace", real_rename)
            swap = note.with_name(f"{note.name}.editor-swap")
            swap.write_bytes(b"editor")
            swap.replace(note)

    monkeypatch.setattr(vault_mutation.osfs, "rename_at_no_replace", racing)


@pytest.mark.skipif(
    sys.platform == "win32", reason="Windows sharing modes keep an editor off the held stage"
)
async def test_an_editor_save_just_after_the_publish_rename_stays_and_the_row_ends_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An editor saves the note just after the stage took its name. Its file stays at the
    note's name, the old note in the backup, and the row ends `conflict` for the user to
    resolve; nothing is hidden under the stage's name."""
    root = _root(tmp_path)
    note = tmp_path / "note.md"
    note.write_bytes(b"old")
    journal = _journal(tmp_path)
    _saved_over_after_the_rename(monkeypatch, note)
    outcome = await _save(_service(journal, root), root, b"new", _sha(b"old"))
    [row] = journal.list_pending()
    assert outcome == PublicationOutcome(row.operation_id, "conflict", True)
    assert row.state == "conflict"
    assert note.read_bytes() == b"editor"
    assert (tmp_path / row.backup_name).read_bytes() == b"old"
    assert not (tmp_path / row.stage_name).exists()
    assert await _service(journal, root).recover_pending() == [
        PublicationOutcome(row.operation_id, "conflict", True)
    ]
    assert note.read_bytes() == b"editor"


@pytest.mark.skipif(
    sys.platform == "win32", reason="Windows sharing modes keep an editor off the held stage"
)
async def test_an_editor_save_just_after_a_create_rename_answers_conflict_and_stays(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A create meets the same save: the editor's file stays at the name, and the create
    answers `conflict` with no row or stage left, as a create onto a taken name does."""
    root = _root(tmp_path)
    note = tmp_path / "note.md"
    journal = _journal(tmp_path)
    _saved_over_after_the_rename(monkeypatch, note)
    outcome = await _save(_service(journal, root), root, b"new", None)
    assert outcome == PublicationOutcome(outcome.operation_id, "conflict")
    assert journal.list_pending() == []
    assert note.read_bytes() == b"editor"
    assert not list(tmp_path.glob(".viki-*"))
    assert await _service(journal, root).recover_pending() == []


async def test_one_recovery_oserror_does_not_abort_other_rows(tmp_path: Path) -> None:
    root = _root(tmp_path)
    journal = _journal(tmp_path)
    native = VaultPublication()
    digest = hashlib.sha256(b"new").hexdigest()
    bad_reservation = native.reserve()
    bad = journal.reserve(root, "bad.md", bad_reservation, "create", digest, 3, None)
    async with MutationCoordinator().lease(root, ("bad.md",)) as lease:
        staged = native.stage(lease, root, "bad.md", bad_reservation, b"new")
    journal.record_stage(bad.operation_id, staged.physical_id)
    good = journal.reserve(root, "good.md", native.reserve(), "create", digest, 3, None)

    outcomes = await _service(journal, root, _ProbeOSError()).recover_pending()
    by_id = {outcome.operation_id: outcome for outcome in outcomes}
    assert by_id[bad.operation_id].status == "blocked"
    assert by_id[good.operation_id].status == "recovered"
    assert journal.get(bad.operation_id) is not None
    assert journal.get(good.operation_id) is None


async def test_workspace_change_blocks_one_recovery_without_escaping(tmp_path: Path) -> None:
    root = _root(tmp_path)
    journal = _journal(tmp_path)
    row = journal.reserve(
        root,
        "note.md",
        VaultPublication().reserve(),
        "create",
        hashlib.sha256(b"new").hexdigest(),
        3,
        None,
    )

    def changed(*_args: object) -> OrdinaryMutationRoot:
        raise PublicationError("workspace_changed")

    service = VaultPublicationService(journal, changed, MutationCoordinator())
    [outcome] = await service.recover_pending()
    assert outcome.operation_id == row.operation_id
    assert outcome.status == "blocked" and outcome.cleanup_pending
    assert journal.get(row.operation_id) == row


async def test_recovery_publishes_a_staged_create_and_clears_its_row(tmp_path: Path) -> None:
    root = _root(tmp_path)
    journal = _journal(tmp_path)
    native = VaultPublication()
    reservation = native.reserve()
    digest = hashlib.sha256(b"new").hexdigest()
    row = journal.reserve(root, "note.md", reservation, "create", digest, 3, None)
    async with MutationCoordinator().lease(root, ("note.md",)) as lease:
        staged = native.stage(lease, root, "note.md", reservation, b"new")
    journal.record_stage(row.operation_id, staged.physical_id)

    outcomes = await _service(journal, root).recover_pending()
    assert outcomes == [type(outcomes[0])(row.operation_id, "recovered")]
    assert (tmp_path / "note.md").read_bytes() == b"new"
    assert journal.list_pending() == []


async def test_create_recovery_collision_reprobes_and_preserves_racer(tmp_path: Path) -> None:
    root = _root(tmp_path)
    journal = _journal(tmp_path)
    native = VaultPublication()
    reservation = native.reserve()
    digest = hashlib.sha256(b"new").hexdigest()
    row = journal.reserve(root, "note.md", reservation, "create", digest, 3, None)
    async with MutationCoordinator().lease(root, ("note.md",)) as lease:
        staged = native.stage(lease, root, "note.md", reservation, b"new")
    journal.record_stage(row.operation_id, staged.physical_id)

    [outcome] = await _service(journal, root, _RaceOnMove(reservation.stage_name)).recover_pending()
    # The racer keeps the name; the abandoned stage and its row are discarded.
    assert outcome.status == "conflict" and not outcome.cleanup_pending
    assert (tmp_path / "note.md").read_bytes() == b"external"
    assert not list(tmp_path.glob(".viki-*"))
    assert journal.get(row.operation_id) is None


async def test_recovery_recognizes_create_rename_before_published_commit(tmp_path: Path) -> None:
    root = _root(tmp_path)
    journal = _journal(tmp_path)
    native = VaultPublication()
    reservation = native.reserve()
    digest = hashlib.sha256(b"new").hexdigest()
    row = journal.reserve(root, "note.md", reservation, "create", digest, 3, None)
    async with MutationCoordinator().lease(root, ("note.md",)) as lease:
        staged = native.stage(lease, root, "note.md", reservation, b"new")
        journal.record_stage(row.operation_id, staged.physical_id)
        native.publish_create(lease, staged)

    [outcome] = await _service(journal, root).recover_pending()
    assert outcome.status == "recovered"
    assert (tmp_path / "note.md").read_bytes() == b"new"
    assert journal.list_pending() == []


async def test_unexpected_create_backup_blocks_without_touching_files(tmp_path: Path) -> None:
    root = _root(tmp_path)
    journal = _journal(tmp_path)
    native = VaultPublication()
    reservation = native.reserve()
    digest = hashlib.sha256(b"new").hexdigest()
    row = journal.reserve(root, "note.md", reservation, "create", digest, 3, None)
    (tmp_path / reservation.backup_name).write_bytes(b"unexpected")

    [outcome] = await _service(journal, root).recover_pending()
    assert outcome.status == "blocked"
    assert (tmp_path / reservation.backup_name).read_bytes() == b"unexpected"
    assert journal.get(row.operation_id) is not None


async def test_replace_recovery_continues_after_backup_rename_before_checkpoint(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    target = tmp_path / "note.md"
    target.write_bytes(b"old")
    journal = _journal(tmp_path)
    native = VaultPublication()
    reservation = native.reserve()
    new_hash = hashlib.sha256(b"new").hexdigest()
    old_hash = hashlib.sha256(b"old").hexdigest()
    row = journal.reserve(root, "note.md", reservation, "replace", new_hash, 3, old_hash)
    async with MutationCoordinator().lease(root, ("note.md",)) as lease:
        staged = native.stage(lease, root, "note.md", reservation, b"new")
        journal.record_stage(row.operation_id, staged.physical_id)
        with native.prepare_replace(lease, staged, old_hash, staged.backup_name) as prepared:
            journal.record_prepared(row.operation_id, prepared.old)
            prepared.move_target_to_backup()

    [outcome] = await _service(journal, root).recover_pending()
    assert outcome.status == "recovered"
    assert target.read_bytes() == b"new"
    assert journal.list_pending() == []
    assert not (tmp_path / reservation.backup_name).exists()


async def test_backed_up_checkpoint_with_old_canonical_blocks_untouched(tmp_path: Path) -> None:
    root = _root(tmp_path)
    target = tmp_path / "note.md"
    target.write_bytes(b"old")
    journal = _journal(tmp_path)
    native = VaultPublication()
    reservation = native.reserve()
    new_hash = hashlib.sha256(b"new").hexdigest()
    old_hash = hashlib.sha256(b"old").hexdigest()
    row = journal.reserve(root, "note.md", reservation, "replace", new_hash, 3, old_hash)
    async with MutationCoordinator().lease(root, ("note.md",)) as lease:
        staged = native.stage(lease, root, "note.md", reservation, b"new")
        journal.record_stage(row.operation_id, staged.physical_id)
        with native.prepare_replace(lease, staged, old_hash, staged.backup_name) as prepared:
            journal.record_prepared(row.operation_id, prepared.old)
        journal.transition(row.operation_id, "prepared", "backed_up")

    [outcome] = await _service(journal, root).recover_pending()
    assert outcome.status == "blocked"
    assert target.read_bytes() == b"old"
    assert (tmp_path / reservation.stage_name).read_bytes() == b"new"
    assert not (tmp_path / reservation.backup_name).exists()


async def test_replace_recovery_preserves_external_gap_racer(tmp_path: Path) -> None:
    root = _root(tmp_path)
    target = tmp_path / "note.md"
    target.write_bytes(b"old")
    journal = _journal(tmp_path)
    native = VaultPublication()
    reservation = native.reserve()
    new_hash = hashlib.sha256(b"new").hexdigest()
    old_hash = hashlib.sha256(b"old").hexdigest()
    row = journal.reserve(root, "note.md", reservation, "replace", new_hash, 3, old_hash)
    async with MutationCoordinator().lease(root, ("note.md",)) as lease:
        staged = native.stage(lease, root, "note.md", reservation, b"new")
        journal.record_stage(row.operation_id, staged.physical_id)
        with native.prepare_replace(lease, staged, old_hash, staged.backup_name) as prepared:
            journal.record_prepared(row.operation_id, prepared.old)
            prepared.move_target_to_backup()
            journal.transition(row.operation_id, "prepared", "backed_up")
        target.write_bytes(b"external")

    [outcome] = await _service(journal, root).recover_pending()
    assert outcome.status == "conflict"
    assert target.read_bytes() == b"external"
    assert (tmp_path / reservation.backup_name).read_bytes() == b"old"
    assert (tmp_path / reservation.stage_name).read_bytes() == b"new"
    assert journal.get(row.operation_id).state == "conflict"  # type: ignore[union-attr]


async def test_replace_recovery_restores_old_when_stage_is_missing(tmp_path: Path) -> None:
    root = _root(tmp_path)
    target = tmp_path / "note.md"
    target.write_bytes(b"old")
    journal = _journal(tmp_path)
    native = VaultPublication()
    reservation = native.reserve()
    new_hash = hashlib.sha256(b"new").hexdigest()
    old_hash = hashlib.sha256(b"old").hexdigest()
    row = journal.reserve(root, "note.md", reservation, "replace", new_hash, 3, old_hash)
    async with MutationCoordinator().lease(root, ("note.md",)) as lease:
        staged = native.stage(lease, root, "note.md", reservation, b"new")
        journal.record_stage(row.operation_id, staged.physical_id)
        with native.prepare_replace(lease, staged, old_hash, staged.backup_name) as prepared:
            journal.record_prepared(row.operation_id, prepared.old)
            prepared.move_target_to_backup()
            journal.transition(row.operation_id, "prepared", "backed_up")
        assert native.discard(lease, staged)

    [outcome] = await _service(journal, root).recover_pending()
    assert outcome.status == "blocked" and outcome.cleanup_pending
    assert target.read_bytes() == b"old"
    assert journal.get(row.operation_id) is not None


@pytest.mark.parametrize("missing_stage", [False, True])
async def test_recovery_reprobes_a_racer_before_publish_or_restore(
    tmp_path: Path, missing_stage: bool
) -> None:
    root = _root(tmp_path)
    target = tmp_path / "note.md"
    target.write_bytes(b"old")
    journal = _journal(tmp_path)
    native = VaultPublication()
    reservation = native.reserve()
    new_hash = hashlib.sha256(b"new").hexdigest()
    old_hash = hashlib.sha256(b"old").hexdigest()
    row = journal.reserve(root, "note.md", reservation, "replace", new_hash, 3, old_hash)
    async with MutationCoordinator().lease(root, ("note.md",)) as lease:
        staged = native.stage(lease, root, "note.md", reservation, b"new")
        journal.record_stage(row.operation_id, staged.physical_id)
        with native.prepare_replace(lease, staged, old_hash, staged.backup_name) as prepared:
            journal.record_prepared(row.operation_id, prepared.old)
            prepared.move_target_to_backup()
            journal.transition(row.operation_id, "prepared", "backed_up")
        if missing_stage:
            assert native.discard(lease, staged)

    raced_source = reservation.backup_name if missing_stage else reservation.stage_name
    [outcome] = await _service(journal, root, _RaceOnMove(raced_source)).recover_pending()
    assert outcome.status == "conflict"
    assert target.read_bytes() == b"external"
    assert (tmp_path / reservation.backup_name).read_bytes() == b"old"
    if not missing_stage:
        assert (tmp_path / reservation.stage_name).read_bytes() == b"new"
    pending = journal.get(row.operation_id)
    assert pending is not None and pending.state == "conflict"


async def test_published_replace_recovery_deletes_only_verified_backup(tmp_path: Path) -> None:
    root = _root(tmp_path)
    target = tmp_path / "note.md"
    target.write_bytes(b"old")
    journal = _journal(tmp_path)
    native = VaultPublication()
    reservation = native.reserve()
    new_hash = hashlib.sha256(b"new").hexdigest()
    old_hash = hashlib.sha256(b"old").hexdigest()
    row = journal.reserve(root, "note.md", reservation, "replace", new_hash, 3, old_hash)
    async with MutationCoordinator().lease(root, ("note.md",)) as lease:
        staged = native.stage(lease, root, "note.md", reservation, b"new")
        journal.record_stage(row.operation_id, staged.physical_id)
        with native.prepare_replace(lease, staged, old_hash, staged.backup_name) as prepared:
            journal.record_prepared(row.operation_id, prepared.old)
            prepared.move_target_to_backup()
            journal.transition(row.operation_id, "prepared", "backed_up")
            prepared.publish_stage_no_replace()
            prepared.verify_published()
            journal.transition(row.operation_id, "backed_up", "published")

    [outcome] = await _service(journal, root).recover_pending()
    assert outcome.status == "recovered"
    assert target.read_bytes() == b"new"
    assert not (tmp_path / reservation.backup_name).exists()
    assert journal.list_pending() == []


async def test_policy_is_revalidated_after_waiting_for_the_lease(tmp_path: Path) -> None:
    root = _root(tmp_path)
    journal = _journal(tmp_path)
    calls = 0

    def resolve(_workspace_id: str, _revision: int, _physical_id: str) -> OrdinaryMutationRoot:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise PublicationError("workspace_changed")
        return root

    service = VaultPublicationService(journal, resolve, MutationCoordinator())
    with pytest.raises(PublicationError, match="workspace_changed"):
        await service.save(
            root.workspace_id,
            root.policy_revision,
            root.physical_id,
            "note.md",
            b"new",
            expected_sha256=None,
        )
    assert calls == 2
    assert journal.list_pending() == []
    assert not (tmp_path / "note.md").exists()


async def test_policy_is_revalidated_after_journal_reservation(tmp_path: Path) -> None:
    root = _root(tmp_path)
    journal = _journal(tmp_path)
    calls = 0

    def resolve(_workspace_id: str, _revision: int, _physical_id: str) -> OrdinaryMutationRoot:
        nonlocal calls
        calls += 1
        if calls == 3:
            raise PublicationError("workspace_changed")
        return root

    service = VaultPublicationService(journal, resolve, MutationCoordinator())
    with pytest.raises(PublicationError, match="workspace_changed"):
        await service.save(
            root.workspace_id,
            root.policy_revision,
            root.physical_id,
            "note.md",
            b"new",
            expected_sha256=None,
        )
    assert calls == 3
    # No stage exists yet, so the refusal releases its reservation.
    assert journal.list_pending() == []
    assert not list(tmp_path.glob(".viki-*"))
    assert not (tmp_path / "note.md").exists()


def test_journal_busy_check_folds_case(tmp_path: Path) -> None:
    root = _root(tmp_path)
    journal = _journal(tmp_path)
    digest = hashlib.sha256(b"new").hexdigest()
    journal.reserve(root, "Notes/Note.md", VaultPublication().reserve(), "create", digest, 3, None)
    with pytest.raises(PublicationError, match="publication_busy"):
        journal.reserve(
            root, "notes/NOTE.md", VaultPublication().reserve(), "create", digest, 3, None
        )
    other = _root(tmp_path)  # another workspace may hold the same ref
    journal.reserve(other, "notes/note.md", VaultPublication().reserve(), "create", digest, 3, None)
    assert len(journal.list_pending()) == 2


async def test_save_of_a_case_variant_of_a_pending_row_is_busy(tmp_path: Path) -> None:
    root = _root(tmp_path)
    journal = _journal(tmp_path)
    digest = hashlib.sha256(b"new").hexdigest()
    pending = journal.reserve(
        root, "Note.md", VaultPublication().reserve(), "create", digest, 3, None
    )
    with pytest.raises(PublicationError, match="publication_busy") as caught:
        await _service(journal, root).save(
            root.workspace_id,
            root.policy_revision,
            root.physical_id,
            "note.md",
            b"new",
            expected_sha256=None,
        )
    # The refused reservation names no row, so the refusal carries no ID.
    assert caught.value.operation_id is None
    assert journal.list_pending() == [pending]
    assert not list(tmp_path.glob(".viki-*"))
    assert not (tmp_path / "note.md").exists()


class _StageOSError(VaultPublication):
    def stage(
        self,
        lease: MutationLease,
        root: OrdinaryMutationRoot,
        ref: str,
        reservation: PublicationReservation,
        content: bytes,
    ) -> StagedPublication:
        raise OSError("synthetic")


class _StageRetainedFailure(VaultPublication):
    def stage(
        self,
        lease: MutationLease,
        root: OrdinaryMutationRoot,
        ref: str,
        reservation: PublicationReservation,
        content: bytes,
    ) -> StagedPublication:
        super().stage(lease, root, ref, reservation, content)
        raise MutationError("short_write", stage_retained=True)


async def _save_new(service: VaultPublicationService, root: OrdinaryMutationRoot, ref: str) -> Any:
    return await service.save(
        root.workspace_id,
        root.policy_revision,
        root.physical_id,
        ref,
        b"new",
        expected_sha256=None,
    )


async def _refused_twice_without_a_row(
    tmp_path: Path, ref: str, error: type[Exception], code: str
) -> None:
    vault = tmp_path / "vault"
    root = _root(vault)
    journal = _journal(tmp_path)
    service = _service(journal, root)
    for _attempt in range(2):  # the retry sees the same refusal, not publication_busy
        with pytest.raises(error) as caught:
            await _save_new(service, root, ref)
        assert getattr(caught.value, "code", None) == code
        assert journal.list_pending() == []
    assert not list(vault.rglob(".viki-*"))


def _link_folder(link: Path, target: Path) -> None:
    """Link `link` to the folder `target`: a junction on Windows, a symlink elsewhere."""
    if sys.platform != "win32":
        link.symlink_to(target, target_is_directory=True)
        return
    done = winproc.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)], cwd=str(link.parent), timeout=10.0
    )
    if done.returncode != 0:
        pytest.skip("this volume refuses junctions")


async def test_a_junction_parent_is_refused_without_a_journal_row(tmp_path: Path) -> None:
    """Off Windows the link is a symlink."""
    vault = tmp_path / "vault"
    vault.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    _link_folder(vault / "link", outside)
    await _refused_twice_without_a_row(tmp_path, "link/n.md", MutationError, "unsafe_parent")
    assert not list(outside.iterdir())


async def test_a_missing_parent_is_refused_without_a_journal_row(tmp_path: Path) -> None:
    (tmp_path / "vault").mkdir()
    await _refused_twice_without_a_row(tmp_path, "missing/n.md", MutationError, "unsafe_parent")


async def test_a_raw_oserror_before_the_stage_releases_the_reservation(tmp_path: Path) -> None:
    root = _root(tmp_path)
    journal = _journal(tmp_path)
    service = _service(journal, root, _StageOSError())
    with pytest.raises(OSError, match="synthetic"):
        await _save_new(service, root, "note.md")
    assert journal.list_pending() == []


async def test_a_failure_after_the_stage_exists_keeps_its_row(tmp_path: Path) -> None:
    """Only a stage the writer could not delete (`stage_retained`) keeps its row."""
    root = _root(tmp_path)
    journal = _journal(tmp_path)
    service = _service(journal, root, _StageRetainedFailure())
    with pytest.raises(MutationError, match="short_write"):
        await _save_new(service, root, "note.md")
    [row] = journal.list_pending()
    assert row.state == "reserved"
    assert (tmp_path / row.stage_name).read_bytes() == b"new"


async def test_a_blocked_recovery_row_logs_only_its_id_and_code(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    root = _root(tmp_path)
    journal = _journal(tmp_path)
    row = journal.reserve(
        root,
        "secret-name.md",
        VaultPublication().reserve(),
        "create",
        hashlib.sha256(b"new").hexdigest(),
        3,
        None,
    )

    def changed(*_args: object) -> OrdinaryMutationRoot:
        raise PublicationError("workspace_changed")

    service = VaultPublicationService(journal, changed, MutationCoordinator())
    with caplog.at_level("WARNING", logger="viki_agent.vaults.vault_publication"):
        [outcome] = await service.recover_pending()
    assert outcome.status == "blocked"
    assert caplog.messages == [f"vault publication {row.operation_id} blocked: workspace_changed"]


def _sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


async def _save(
    service: VaultPublicationService,
    root: OrdinaryMutationRoot,
    content: bytes,
    expected_sha256: str | None,
) -> PublicationOutcome:
    return await service.save(
        root.workspace_id,
        root.policy_revision,
        root.physical_id,
        "note.md",
        content,
        expected_sha256=expected_sha256,
    )


async def test_a_save_conflict_leaves_no_row_or_stage_and_the_retry_is_not_busy(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    target = tmp_path / "note.md"
    target.write_bytes(b"changed")
    journal = _journal(tmp_path)
    service = _service(journal, root)
    stale = await _save(service, root, b"draft", _sha(b"old"))
    assert stale == PublicationOutcome(stale.operation_id, "conflict")
    assert journal.list_pending() == []
    assert not list(tmp_path.glob(".viki-*"))
    retry = await _save(service, root, b"draft", _sha(b"changed"))
    assert retry.status == "saved"
    assert target.read_bytes() == b"draft"


async def test_an_edit_conflict_is_not_published_after_the_note_is_restored(
    tmp_path: Path,
) -> None:
    """An abandoned draft never wins after the user restores the note."""
    root = _root(tmp_path)
    target = tmp_path / "note.md"
    target.write_bytes(b"edited elsewhere")
    journal = _journal(tmp_path)
    outcome = await _save(_service(journal, root), root, b"abandoned draft", _sha(b"old"))
    assert outcome.status == "conflict"
    target.write_bytes(b"old")
    assert await _service(journal, root).recover_pending() == []
    assert target.read_bytes() == b"old"
    assert not list(tmp_path.glob(".viki-*"))


async def test_a_create_collision_is_not_published_after_the_old_note_is_deleted(
    tmp_path: Path,
) -> None:
    """A refused New note never appears once the name frees up."""
    root = _root(tmp_path)
    target = tmp_path / "note.md"
    target.write_bytes(b"existing")
    journal = _journal(tmp_path)
    outcome = await _save(_service(journal, root), root, b"abandoned note", None)
    assert outcome.status == "conflict"
    target.unlink()
    assert await _service(journal, root).recover_pending() == []
    assert not target.exists()
    assert not list(tmp_path.glob(".viki-*"))


@pytest.mark.parametrize(
    "code", ["missing_target", "unsafe_target", pytest.param("target_busy", marks=_WIN32_HOLD)]
)
async def test_other_refusals_before_prepared_discard_the_stage_and_row(
    tmp_path: Path, code: str
) -> None:
    root = _root(tmp_path)
    target = tmp_path / "note.md"
    if code != "missing_target":
        target.write_bytes(b"old")
    if code == "unsafe_target":
        os.link(target, tmp_path / "second-link.md")
    journal = _journal(tmp_path)
    service = _service(journal, root)
    with ExitStack() as held:
        if code == "target_busy":
            held.enter_context(target.open("r+b"))
        with pytest.raises(MutationError) as caught:
            await _save(service, root, b"new", _sha(b"old"))
    assert caught.value.code == code
    assert journal.list_pending() == []
    assert not list(tmp_path.glob(".viki-*"))
    assert (target.read_bytes() if target.exists() else None) == (
        None if code == "missing_target" else b"old"
    )


class _DiscardFails(VaultPublication):
    def discard(self, lease: MutationLease, staged: StagedPublication) -> bool:
        raise MutationError("discard_failed", stage_retained=True)


@pytest.mark.parametrize("kind", ["create", "replace"])
async def test_a_failed_discard_keeps_its_row_and_recovery_discards_it(
    tmp_path: Path, kind: str
) -> None:
    root = _root(tmp_path)
    target = tmp_path / "note.md"
    target.write_bytes(b"external")
    journal = _journal(tmp_path)
    expected = _sha(b"old") if kind == "replace" else None
    with pytest.raises(PublicationError) as caught:
        await _save(_service(journal, root, _DiscardFails()), root, b"new", expected)
    [row] = journal.list_pending()
    assert (caught.value.code, caught.value.operation_id) == ("recovery_required", row.operation_id)
    assert row.state == "staged"
    assert (tmp_path / row.stage_name).read_bytes() == b"new"
    # The refusal and the recovery label name the same thing.
    [status] = _service(journal, root).recovery_status()
    assert status.reason == "recovery_required"

    # A staged row whose target no longer matches is discarded, never completed.
    [outcome] = await _service(journal, root).recover_pending()
    assert outcome == PublicationOutcome(row.operation_id, "conflict")
    assert journal.list_pending() == []
    assert target.read_bytes() == b"external"
    assert not list(tmp_path.glob(".viki-*"))


async def test_a_legacy_excluded_ref_row_blocks_alone(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    root = _root(tmp_path)
    store = _Store(tmp_path / "journal.sqlite3")
    journal = PublicationJournal(cast(Any, store))
    native = VaultPublication()
    reservation = native.reserve()
    row = journal.reserve(root, "note.md", reservation, "create", _sha(b"new"), 3, None)
    async with MutationCoordinator().lease(root, ("note.md",)) as lease:
        staged = native.stage(lease, root, "note.md", reservation, b"new")
    journal.record_stage(row.operation_id, staged.physical_id)
    legacy = native.reserve()  # a row written before the ref joined the excluded set
    recorded = "2026-01-01T00:00:00.000Z"
    with store._connection(write=True) as connection:
        connection.execute(
            "INSERT INTO vault_publications (operation_id,workspace_id,root_physical_id,"
            "policy_revision,ref,kind,stage_name,backup_name,stage_sha256,stage_bytes,state,"
            "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                legacy.operation_id,
                root.workspace_id,
                root.physical_id,
                root.policy_revision,
                "GEMINI.md",
                "create",
                legacy.stage_name,
                legacy.backup_name,
                _sha(b"new"),
                3,
                "reserved",
                recorded,
                recorded,
            ),
        )

    with caplog.at_level("WARNING", logger="viki_agent.vaults.vault_publication"):
        outcomes = await _service(journal, root).recover_pending()
    assert [(outcome.operation_id, outcome.status) for outcome in outcomes] == [
        (legacy.operation_id, "blocked"),
        (row.operation_id, "recovered"),
    ]
    assert (tmp_path / "note.md").read_bytes() == b"new"
    assert [pending.operation_id for pending in journal.list_pending()] == [legacy.operation_id]
    assert caplog.messages == [f"vault publication {legacy.operation_id} blocked: invalid_ref"]


def _hold_exclusively(path: Path) -> int:
    """A real Win32 handle that shares nothing, as an indexer, scanner or sync client can hold."""
    assert sys.platform == "win32"
    return vault_mutation._open_file(path, vault_mutation._GENERIC_READ, share=0)


class _StageHeldWhilePreparing(VaultPublication):
    """Another process holds the fresh stage from `stage()` until the prepare has refused."""

    @contextmanager
    def prepare_replace(
        self,
        lease: MutationLease,
        staged: StagedPublication,
        expected_sha256: str,
        backup_name: str,
    ) -> Iterator[PreparedReplacement]:
        assert sys.platform == "win32"
        held = _hold_exclusively(staged.root.path / staged.stage_name)
        try:
            with super().prepare_replace(lease, staged, expected_sha256, backup_name) as prepared:
                yield prepared
        finally:
            vault_mutation._CloseHandle(held)


class _ParentHeldWhilePreparing(VaultPublication):
    """Another process holds the target's folder only until the prepare has refused.

    Like a held stage, and unlike a held note, a held parent also blocks `discard`'s own open, so
    the hold must clear before `discard` runs to tell a transient refusal from a definite one."""

    @contextmanager
    def prepare_replace(
        self,
        lease: MutationLease,
        staged: StagedPublication,
        expected_sha256: str,
        backup_name: str,
    ) -> Iterator[PreparedReplacement]:
        assert sys.platform == "win32"
        parent = staged.root.path.joinpath(*staged.ref.split("/")[:-1])
        held = vault_mutation._open_file(
            parent, vault_mutation._GENERIC_READ, directory=True, share=0
        )
        try:
            with super().prepare_replace(lease, staged, expected_sha256, backup_name) as prepared:
                yield prepared
        finally:
            vault_mutation._CloseHandle(held)


@_WIN32_HOLD
async def test_a_held_stage_before_prepared_is_discarded_and_the_retry_saves(
    tmp_path: Path,
) -> None:
    """Every refusal before `prepared` discards the save, not only the target ones."""
    root = _root(tmp_path)
    target = tmp_path / "note.md"
    target.write_bytes(b"old")
    journal = _journal(tmp_path)
    with pytest.raises(MutationError) as caught:
        await _save(_service(journal, root, _StageHeldWhilePreparing()), root, b"new", _sha(b"old"))
    assert caught.value.code == "stage_busy"
    assert journal.list_pending() == []
    assert not list(tmp_path.glob(".viki-*"))
    assert await _service(journal, root).recover_pending() == []
    assert target.read_bytes() == b"old"

    retry = await _save(_service(journal, root), root, b"new", _sha(b"old"))
    assert retry.status == "saved"
    assert target.read_bytes() == b"new"


def _short_name(path: Path) -> str:
    """The 8.3 name Windows gave the last component of `path`, or the long name if it gave none."""
    assert sys.platform == "win32"
    get_short = ctypes.WinDLL("kernel32", use_last_error=True).GetShortPathNameW
    buffer = ctypes.create_unicode_buffer(1024)
    assert 0 < get_short(str(path), buffer, len(buffer)) < len(buffer), ctypes.get_last_error()
    return Path(buffer.value).name


@_WIN32_SHORT_NAME
async def test_a_short_name_leaf_is_refused_after_its_stage_and_leaves_nothing(
    tmp_path: Path,
) -> None:
    """The alias check runs once the stage exists; the refusal still discards."""
    (tmp_path / "notes").mkdir()
    note = tmp_path / "notes" / "a long note name.md"
    note.write_bytes(b"old")
    alias = _short_name(note)
    if alias == note.name:
        pytest.skip("this volume makes no 8.3 short names, so the note has no alias to send")
    root = _root(tmp_path)
    journal = _journal(tmp_path)
    with pytest.raises(MutationError) as caught:
        await _service(journal, root).save(
            root.workspace_id,
            root.policy_revision,
            root.physical_id,
            f"notes/{alias}",
            b"new",
            expected_sha256=_sha(b"old"),
        )
    assert caught.value.code == "excluded_path"
    assert journal.list_pending() == []
    assert not list(tmp_path.rglob(".viki-*"))
    assert await _service(journal, root).recover_pending() == []
    assert note.read_bytes() == b"old"


@pytest.mark.skipif(
    sys.platform != "win32", reason="the read-only attribute is Windows'; the mode twin follows"
)
async def test_a_read_only_note_is_refused_and_keeps_its_bytes_and_attribute(
    tmp_path: Path,
) -> None:
    """Windows renames a read-only file, so a replace would strip the protection."""
    assert sys.platform == "win32"
    root = _root(tmp_path)
    target = tmp_path / "note.md"
    target.write_bytes(b"old")
    os.chmod(target, stat.S_IREAD)
    try:
        journal = _journal(tmp_path)
        with pytest.raises(MutationError) as caught:
            await _save(_service(journal, root), root, b"new", _sha(b"old"))
        assert caught.value.code == "read_only_target"
        assert target.read_bytes() == b"old"
        assert target.stat().st_file_attributes & stat.FILE_ATTRIBUTE_READONLY
        assert journal.list_pending() == []
        assert not list(tmp_path.glob(".viki-*"))
    finally:
        os.chmod(target, stat.S_IREAD | stat.S_IWRITE)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits; Windows: the test above")
async def test_a_note_without_owner_write_is_refused_and_keeps_its_bytes_and_mode(
    tmp_path: Path,
) -> None:
    """The POSIX twin: a note whose mode grants its owner no write refuses `read_only_target`."""
    root = _root(tmp_path)
    target = tmp_path / "note.md"
    target.write_bytes(b"old")
    os.chmod(target, 0o444)
    try:
        journal = _journal(tmp_path)
        with pytest.raises(MutationError) as caught:
            await _save(_service(journal, root), root, b"new", _sha(b"old"))
        assert caught.value.code == "read_only_target"
        assert target.read_bytes() == b"old"
        assert stat.S_IMODE(target.stat().st_mode) == 0o444
        assert journal.list_pending() == []
        assert not list(tmp_path.glob(".viki-*"))
    finally:
        os.chmod(target, 0o644)


@pytest.mark.skipif(
    sys.platform != "win32", reason="Windows refuses to delete a read-only file; POSIX unlinks it"
)
async def test_a_read_only_backup_leaves_a_typed_pending_cleanup(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The backup delete's OSError is a pending cleanup, not an untyped block."""
    root = _root(tmp_path)
    target = tmp_path / "note.md"
    target.write_bytes(b"old")
    journal = _journal(tmp_path)
    native = VaultPublication()
    reservation = native.reserve()
    row = journal.reserve(root, "note.md", reservation, "replace", _sha(b"new"), 3, _sha(b"old"))
    async with MutationCoordinator().lease(root, ("note.md",)) as lease:
        staged = native.stage(lease, root, "note.md", reservation, b"new")
        journal.record_stage(row.operation_id, staged.physical_id)
        with native.prepare_replace(lease, staged, _sha(b"old"), staged.backup_name) as prepared:
            journal.record_prepared(row.operation_id, prepared.old)
            prepared.move_target_to_backup()
            journal.transition(row.operation_id, "prepared", "backed_up")
            prepared.publish_stage_no_replace()
            prepared.verify_published()
            journal.transition(row.operation_id, "backed_up", "published")
    backup = tmp_path / reservation.backup_name
    os.chmod(backup, stat.S_IREAD)
    try:
        with caplog.at_level("WARNING", logger="viki_agent.vaults.vault_publication"):
            outcomes = await _service(journal, root).recover_pending()
        assert outcomes == [PublicationOutcome(row.operation_id, "saved", True)]
        assert caplog.messages == []
        assert target.read_bytes() == b"new"
        assert backup.read_bytes() == b"old"
    finally:
        os.chmod(backup, stat.S_IREAD | stat.S_IWRITE)


async def test_a_staged_replace_whose_stage_is_gone_is_discarded_not_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_discard` deleted the stage, then its row delete failed once: the next launch clears the
    row and the note stays."""
    root = _root(tmp_path)
    target = tmp_path / "note.md"
    target.write_bytes(b"changed")
    journal = _journal(tmp_path)
    real_delete = journal.delete
    failures = [RuntimeError("synthetic")]

    def delete_fails_once(operation_id: str, expected: Any) -> None:
        if failures:
            raise failures.pop()
        real_delete(operation_id, expected)

    monkeypatch.setattr(journal, "delete", delete_fails_once)
    with pytest.raises(PublicationError) as caught:
        await _save(_service(journal, root), root, b"draft", _sha(b"old"))
    [row] = journal.list_pending()
    assert (caught.value.code, caught.value.operation_id) == ("recovery_required", row.operation_id)
    assert row.state == "staged"
    assert not list(tmp_path.glob(".viki-*"))

    # Nothing was published before `prepared`, and no file of the save is left: the row goes
    # and the note stays.
    [outcome] = await _service(journal, root).recover_pending()
    assert outcome == PublicationOutcome(row.operation_id, "recovered")
    assert journal.list_pending() == []
    assert await _service(journal, root).recover_pending() == []
    assert target.read_bytes() == b"changed"


class _StageHeldWhileDiscarding(VaultPublication):
    """Another process holds the stage while the save deletes it."""

    def discard(self, lease: MutationLease, staged: StagedPublication) -> bool:
        assert sys.platform == "win32"
        held = _hold_exclusively(staged.root.path / staged.stage_name)
        try:
            return super().discard(lease, staged)
        finally:
            vault_mutation._CloseHandle(held)


@_WIN32_HOLD
async def test_a_failed_no_change_cleanup_is_recovery_required_with_its_id(
    tmp_path: Path,
) -> None:
    """The no-op save's cleanup fails the way `_discard` does."""
    root = _root(tmp_path)
    target = tmp_path / "note.md"
    target.write_bytes(b"same")
    journal = _journal(tmp_path)
    with pytest.raises(PublicationError) as caught:
        await _save(
            _service(journal, root, _StageHeldWhileDiscarding()), root, b"same", _sha(b"same")
        )
    [row] = journal.list_pending()
    assert (caught.value.code, caught.value.operation_id) == ("recovery_required", row.operation_id)
    assert (tmp_path / row.stage_name).read_bytes() == b"same"

    [outcome] = await _service(journal, root).recover_pending()
    assert outcome == PublicationOutcome(row.operation_id, "no_change")
    assert journal.list_pending() == []
    assert not list(tmp_path.glob(".viki-*"))
    assert target.read_bytes() == b"same"


class _CreateStageHeld(VaultPublication):
    """Another process holds the fresh stage while `publish_create` opens it, then lets go."""

    def publish_create(
        self, lease: MutationLease, staged: StagedPublication
    ) -> PublishedPublication:
        assert sys.platform == "win32"
        held = _hold_exclusively(staged.root.path / staged.stage_name)
        try:
            return super().publish_create(lease, staged)
        finally:
            vault_mutation._CloseHandle(held)


@_WIN32_HOLD
async def test_a_create_refused_before_its_rename_is_discarded_and_publishes_nothing(
    tmp_path: Path,
) -> None:
    """A create whose stage proves that no rename happened is discarded like any refusal before
    `prepared`."""
    root = _root(tmp_path)
    note = tmp_path / "note.md"
    journal = _journal(tmp_path)
    with pytest.raises(MutationError) as caught:
        await _save(_service(journal, root, _CreateStageHeld()), root, b"draft", None)
    assert caught.value.code == "stage_busy"
    assert journal.list_pending() == []
    assert not list(tmp_path.glob(".viki-*"))
    assert await _service(journal, root).recover_pending() == []
    assert not note.exists()

    retry = await _save(_service(journal, root), root, b"draft", None)
    assert retry.status == "saved"
    assert note.read_bytes() == b"draft"


async def _plant_staged(
    journal: PublicationJournal,
    root: OrdinaryMutationRoot,
    ref: str,
    content: bytes,
    expected_sha256: str | None,
) -> str:
    """The row and stage a crash leaves after the `staged` commit."""
    native = VaultPublication()
    reservation = native.reserve()
    kind = "create" if expected_sha256 is None else "replace"
    row = journal.reserve(
        root, ref, reservation, kind, _sha(content), len(content), expected_sha256
    )
    async with MutationCoordinator().lease(root, (ref,)) as lease:
        staged = native.stage(lease, root, ref, reservation, content)
    journal.record_stage(row.operation_id, staged.physical_id)
    return row.operation_id


async def test_a_deleted_folder_clears_its_staged_rows_and_a_junction_still_blocks(
    tmp_path: Path,
) -> None:
    """A missing folder holds no stage, so its `staged` rows read as a missing stage."""
    vault = tmp_path / "vault"
    (vault / "sub").mkdir(parents=True)
    (vault / "sub" / "note.md").write_bytes(b"old")
    root = _root(vault)
    journal = _journal(tmp_path)
    replace = await _plant_staged(journal, root, "sub/note.md", b"new", _sha(b"old"))
    create = await _plant_staged(journal, root, "sub/idea.md", b"idea", None)
    shutil.rmtree(vault / "sub")

    outcomes = await _service(journal, root).recover_pending()
    assert {outcome.operation_id: outcome for outcome in outcomes} == {
        replace: PublicationOutcome(replace, "recovered"),
        create: PublicationOutcome(create, "recovered"),
    }
    assert journal.list_pending() == []
    assert await _service(journal, root).recover_pending() == []
    assert not (vault / "sub").exists()

    # A junction (off Windows, a symlink) at the folder's name is not a missing folder: its row
    # blocks and stays.
    (vault / "sub").mkdir()
    (vault / "sub" / "note.md").write_bytes(b"old")
    blocked = await _plant_staged(journal, root, "sub/note.md", b"new", _sha(b"old"))
    shutil.rmtree(vault / "sub")
    outside = tmp_path / "outside"
    outside.mkdir()
    _link_folder(vault / "sub", outside)
    assert await _service(journal, root).recover_pending() == [
        PublicationOutcome(blocked, "blocked", True)
    ]
    assert [row.operation_id for row in journal.list_pending()] == [blocked]
    assert not list(outside.iterdir())


class _PrepareOSErrorOnce(VaultPublication):
    """An I/O error while recovery prepares, which the next launch no longer meets."""

    def __init__(self) -> None:
        self.failures = [OSError("synthetic")]

    @contextmanager
    def prepare_replace(
        self,
        lease: MutationLease,
        staged: StagedPublication,
        expected_sha256: str,
        backup_name: str,
    ) -> Iterator[PreparedReplacement]:
        if self.failures:
            raise self.failures.pop()
        with super().prepare_replace(lease, staged, expected_sha256, backup_name) as prepared:
            yield prepared


@pytest.mark.parametrize(
    "cause",
    [
        pytest.param("note_busy", marks=_WIN32_HOLD),
        pytest.param("stage_busy", marks=_WIN32_HOLD),
        "oserror",
        "stale_root",
        "unsupported_filesystem",
        pytest.param("parent_busy", marks=_WIN32_HOLD),
        "store_busy",
    ],
)
async def test_recovery_keeps_a_crash_interrupted_save_on_a_transient_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cause: str
) -> None:
    """A busy note, stage, folder or store, a momentary root check, or an I/O error,
    keeps the row for the next launch.

    A definite refusal still discards in recovery; for a conflict, see
    `test_a_failed_discard_keeps_its_row_and_recovery_discards_it`, and for the rest of the
    definite refusals, `test_recovery_discards_a_definite_target_refusal`."""
    root = _root(tmp_path)
    ref = "sub/note.md" if cause == "parent_busy" else "note.md"
    note = tmp_path / ref
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_bytes(b"old")
    journal = _journal(tmp_path)
    operation_id = await _plant_staged(journal, root, ref, b"new", _sha(b"old"))
    [row] = journal.list_pending()
    natives: dict[str, type[VaultPublication]] = {
        "note_busy": VaultPublication,
        "stage_busy": _StageHeldWhilePreparing,
        "oserror": _PrepareOSErrorOnce,
        "stale_root": VaultPublication,
        "unsupported_filesystem": VaultPublication,
        "parent_busy": _ParentHeldWhilePreparing,
        "store_busy": VaultPublication,
    }
    native = natives[cause]()
    if cause == "store_busy":
        monkeypatch.setattr(journal, "record_prepared", _store_busy)
    if cause in {"stale_root", "unsupported_filesystem"}:
        # The lease's own check (`MutationCoordinator.lease`) and the stage and backup probes
        # are the first three calls and pass; the fourth, inside `prepare_replace`'s
        # parent guard, is the momentary failure recovery must retry.
        real_revalidate = vault_mutation._revalidate_root
        calls = [0]

        def revalidate_once_bad(revalidated_root: OrdinaryMutationRoot) -> None:
            calls[0] += 1
            if calls[0] == 4:
                raise MutationError(cause)
            real_revalidate(revalidated_root)

        monkeypatch.setattr(vault_mutation, "_revalidate_root", revalidate_once_bad)
    with ExitStack() as held:
        if cause == "note_busy":
            held.callback(vault_mutation._CloseHandle, _hold_exclusively(note))
        outcomes = await _service(journal, root, native).recover_pending()
    assert outcomes == [PublicationOutcome(operation_id, "blocked", True)]
    assert journal.list_pending() == [row]
    assert (note.parent / row.stage_name).read_bytes() == b"new"
    assert note.read_bytes() == b"old"

    # The following launch no longer meets the refusal.
    monkeypatch.undo()
    assert await _service(journal, root).recover_pending() == [
        PublicationOutcome(operation_id, "saved")
    ]
    assert journal.list_pending() == []
    assert note.read_bytes() == b"new"
    assert not list(tmp_path.rglob(".viki-*"))


@pytest.mark.parametrize(
    "code",
    [
        "missing_target",
        "unsafe_target",
        "read_only_target",
        pytest.param("excluded_path", marks=_WIN32_SHORT_NAME),
    ],
)
async def test_recovery_discards_a_definite_target_refusal(tmp_path: Path, code: str) -> None:
    """A definite target refusal still discards in recovery, a short-name alias included.

    A momentary root or volume failure is transient instead:
    `test_recovery_keeps_a_crash_interrupted_save_on_a_transient_refusal`."""
    root = _root(tmp_path)
    journal = _journal(tmp_path)
    ref = "note.md"
    note = tmp_path / "note.md"
    if code == "excluded_path":
        (tmp_path / "notes").mkdir()
        note = tmp_path / "notes" / "a long note name.md"
        note.write_bytes(b"old")
        alias = _short_name(note)
        if alias == note.name:
            pytest.skip("this volume makes no 8.3 short names, so the note has no alias to send")
        ref = f"notes/{alias}"
    else:
        note.write_bytes(b"old")
    operation_id = await _plant_staged(journal, root, ref, b"new", _sha(b"old"))
    [row] = journal.list_pending()
    if code == "missing_target":
        note.unlink()
    elif code == "unsafe_target":
        os.link(note, tmp_path / "second-link.md")
    elif code == "read_only_target":
        os.chmod(note, stat.S_IREAD)
    try:
        assert await _service(journal, root).recover_pending() == [
            PublicationOutcome(operation_id, "conflict")
        ]
        assert journal.list_pending() == []
        assert not list(tmp_path.rglob(".viki-*"))
        assert (note.read_bytes() if note.exists() else None) == (
            None if code == "missing_target" else b"old"
        )
    finally:
        if code == "read_only_target":
            os.chmod(note, stat.S_IREAD | stat.S_IWRITE)


@pytest.mark.skipif(
    sys.platform != "win32", reason="Win32's open refuses a folder raw; the POSIX twin follows"
)
async def test_a_raw_oserror_before_prepared_is_discarded_without_an_id(tmp_path: Path) -> None:
    """A folder at the note's name fails the target open with a raw OSError; the save is discarded
    and the error carries no operation ID."""
    root = _root(tmp_path)
    (tmp_path / "note.md").mkdir()
    journal = _journal(tmp_path)
    with pytest.raises(OSError) as caught:
        await _save(_service(journal, root), root, b"new", _sha(b"old"))
    assert not isinstance(caught.value, (MutationError, PublicationError))
    assert journal.list_pending() == []
    assert not list(tmp_path.glob(".viki-*"))
    assert (tmp_path / "note.md").is_dir()


@pytest.mark.skipif(sys.platform == "win32", reason="Windows: the raw OSError test above")
async def test_a_folder_at_the_note_name_is_refused_unsafe_target(tmp_path: Path) -> None:
    """The POSIX twin: a folder at the note's name is no regular file, so its open refuses it."""
    root = _root(tmp_path)
    (tmp_path / "note.md").mkdir()
    journal = _journal(tmp_path)
    with pytest.raises(MutationError) as caught:
        await _save(_service(journal, root), root, b"new", _sha(b"old"))
    assert caught.value.code == "unsafe_target"
    assert journal.list_pending() == []
    assert not list(tmp_path.glob(".viki-*"))
    assert (tmp_path / "note.md").is_dir()


async def test_a_journal_failure_before_prepared_keeps_its_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed `record_prepared` may have committed, so its stage must stay."""
    root = _root(tmp_path)
    note = tmp_path / "note.md"
    note.write_bytes(b"old")
    journal = _journal(tmp_path)

    def fail_record(*_args: object, **_kwargs: object) -> None:
        raise StateError("capacity")

    monkeypatch.setattr(journal, "record_prepared", fail_record)
    with pytest.raises(PublicationError) as caught:
        await _save(_service(journal, root), root, b"new", _sha(b"old"))
    [row] = journal.list_pending()
    assert (caught.value.code, caught.value.operation_id) == ("recovery_required", row.operation_id)
    assert row.state == "staged"
    assert (tmp_path / row.stage_name).read_bytes() == b"new"
    assert note.read_bytes() == b"old"


def test_reserve_refusals_carry_no_operation_id(tmp_path: Path) -> None:
    """A refused reservation names no row the recovery status lists."""
    root = _root(tmp_path)
    journal = _journal(tmp_path)
    native = VaultPublication()
    digest = _sha(b"")
    with pytest.raises(PublicationError) as invalid:
        journal.reserve(root, "note.txt", native.reserve(), "create", digest, 0, None)
    reservation = native.reserve()
    journal.reserve(root, "first.md", reservation, "create", digest, 0, None)
    with pytest.raises(PublicationError) as duplicate:  # the same operation ID twice
        journal.reserve(root, "second.md", reservation, "create", digest, 0, None)
    for index in range(127):
        journal.reserve(root, f"{index}.md", native.reserve(), "create", digest, 0, None)
    with pytest.raises(PublicationError) as full:
        journal.reserve(root, "full.md", native.reserve(), "create", digest, 0, None)
    assert [
        (caught.value.code, caught.value.operation_id) for caught in (invalid, duplicate, full)
    ] == [
        ("invalid_publication", None),
        ("invalid_publication", None),
        ("publication_limit", None),
    ]


def _store_busy(*_args: object, **_kwargs: object) -> None:
    raise StateError("busy")  # what `_connection` raises once `BUSY_TIMEOUT_S` runs out


@pytest.mark.parametrize(
    ("kind", "checkpoint"),
    [("create", "record_stage"), ("replace", "record_stage"), ("replace", "record_prepared")],
)
async def test_a_busy_checkpoint_before_prepared_leaves_nothing_to_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, checkpoint: str
) -> None:
    """A store `busy` certainly did not commit, so the save is refused like any other refusal
    before `prepared`, with a retryable code and no operation ID. No row or stage is left, the
    note is untouched, and a relaunch publishes nothing."""
    root = _root(tmp_path)
    note = tmp_path / "note.md"
    expected = None
    if kind == "replace":
        note.write_bytes(b"old")
        expected = _sha(b"old")
    journal = _journal(tmp_path)
    monkeypatch.setattr(journal, checkpoint, _store_busy)
    with pytest.raises(PublicationError) as caught:
        await _save(_service(journal, root), root, b"abandoned", expected)
    assert (caught.value.code, caught.value.operation_id) == ("journal_unavailable", None)
    assert journal.list_pending() == []
    assert not list(tmp_path.glob(".viki-*"))

    monkeypatch.undo()
    assert await _service(journal, root).recover_pending() == []
    assert (note.read_bytes() if note.exists() else None) == (b"old" if expected else None)
    retry = await _save(_service(journal, root), root, b"retry", expected)
    assert retry.status == "saved"
    assert note.read_bytes() == b"retry"


@pytest.mark.parametrize("edited", [False, True])
async def test_a_busy_final_delete_of_a_create_is_saved_with_cleanup_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, edited: bool
) -> None:
    """The note is published and verified, so only the journal cleanup is pending. The next
    launch deletes the row even after the note changed elsewhere."""
    root = _root(tmp_path)
    note = tmp_path / "note.md"
    journal = _journal(tmp_path)
    real_delete = journal.delete

    def busy_published(operation_id: str, expected: Any) -> None:
        if expected == "published":
            raise StateError("busy")
        real_delete(operation_id, expected)

    monkeypatch.setattr(journal, "delete", busy_published)
    outcome = await _save(_service(journal, root), root, b"new", None)
    [row] = journal.list_pending()
    assert outcome == PublicationOutcome(row.operation_id, "saved", True)
    assert row.state == "published"
    assert note.read_bytes() == b"new"

    monkeypatch.undo()
    if edited:
        note.write_bytes(b"edited elsewhere")
    assert await _service(journal, root).recover_pending() == [
        PublicationOutcome(row.operation_id, "recovered")
    ]
    assert journal.list_pending() == []
    assert note.read_bytes() == (b"edited elsewhere" if edited else b"new")


async def test_a_failed_stage_write_leaves_no_stage_and_no_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A disk that fills part-way through the write. The save deletes its own stage
    through the handle that created it, so nothing blocks the note or registration changes."""
    root = _root(tmp_path)
    journal = _journal(tmp_path)
    real_write = vault_mutation._write_handle

    def disk_full(handle: vault_mutation.FileHandle, content: bytes) -> None:
        real_write(handle, content[: len(content) // 2])
        raise OSError(errno.ENOSPC, "WriteFile")

    monkeypatch.setattr(vault_mutation, "_write_handle", disk_full)
    with pytest.raises(MutationError) as caught:
        await _save(_service(journal, root), root, b"draft body", None)
    assert (caught.value.code, caught.value.stage_retained) == ("stage_failed", False)
    assert journal.list_pending() == []
    assert not list(tmp_path.glob(".viki-*"))

    monkeypatch.undo()
    assert await _service(journal, root).recover_pending() == []
    assert (await _save(_service(journal, root), root, b"again", None)).status == "saved"


async def _plant_moved(
    journal: PublicationJournal, root: OrdinaryMutationRoot, state: str, ref: str = "note.md"
) -> PublicationRow:
    """A replace interrupted once the note moved to its backup, at `prepared` or `backed_up`."""
    native = VaultPublication()
    reservation = native.reserve()
    row = journal.reserve(root, ref, reservation, "replace", _sha(b"new"), 3, _sha(b"old"))
    async with MutationCoordinator().lease(root, (ref,)) as lease:
        staged = native.stage(lease, root, ref, reservation, b"new")
        journal.record_stage(row.operation_id, staged.physical_id)
        with native.prepare_replace(lease, staged, _sha(b"old"), staged.backup_name) as prepared:
            journal.record_prepared(row.operation_id, prepared.old)
            prepared.move_target_to_backup()
            if state == "backed_up":
                journal.transition(row.operation_id, "prepared", "backed_up")
    return row


async def test_a_conflict_row_whose_files_are_gone_clears_at_the_next_launch(
    tmp_path: Path,
) -> None:
    """The user kept the external note and deleted the stage and backup."""
    root = _root(tmp_path)
    note = tmp_path / "note.md"
    note.write_bytes(b"old")
    journal = _journal(tmp_path)
    row = await _plant_moved(journal, root, "backed_up")
    note.write_bytes(b"external")  # another editor recreates the note while the app is down
    assert await _service(journal, root).recover_pending() == [
        PublicationOutcome(row.operation_id, "conflict", True)
    ]
    (tmp_path / row.stage_name).unlink()
    (tmp_path / row.backup_name).unlink()

    assert await _service(journal, root).recover_pending() == [
        PublicationOutcome(row.operation_id, "recovered")
    ]
    assert journal.list_pending() == []
    assert note.read_bytes() == b"external"
    later = await _save(_service(journal, root), root, b"later", _sha(b"external"))
    assert later.status == "saved"


@pytest.mark.parametrize("state", ["prepared", "backed_up"])
async def test_a_restored_note_whose_stage_is_gone_clears_at_the_next_launch(
    tmp_path: Path, state: str
) -> None:
    """The stage was deleted, so recovery put the old note back and kept the row; the launch
    after that deletes the row, and the note keeps its old bytes."""
    root = _root(tmp_path)
    note = tmp_path / "note.md"
    note.write_bytes(b"old")
    journal = _journal(tmp_path)
    row = await _plant_moved(journal, root, state)
    (tmp_path / row.stage_name).unlink()
    assert await _service(journal, root).recover_pending() == [
        PublicationOutcome(row.operation_id, "blocked", True)
    ]
    assert note.read_bytes() == b"old"

    assert await _service(journal, root).recover_pending() == [
        PublicationOutcome(row.operation_id, "recovered")
    ]
    assert journal.list_pending() == []
    assert note.read_bytes() == b"old"
    assert not list(tmp_path.glob(".viki-*"))


class _BackupHeldWhileCleaning(VaultPublication):
    """A scanner holds the old backup while the save deletes it, so the cleanup stays pending."""

    def discard_backup_after_publish(
        self,
        lease: MutationLease,
        root: OrdinaryMutationRoot,
        ref: str,
        backup_name: str,
        published: FileFingerprint,
        backup: FileFingerprint,
    ) -> None:
        assert sys.platform == "win32"
        held = _hold_exclusively(root.path / backup_name)
        try:
            super().discard_backup_after_publish(lease, root, ref, backup_name, published, backup)
        finally:
            vault_mutation._CloseHandle(held)


@_WIN32_HOLD
async def test_a_published_replace_whose_backup_remains_stays_blocked(tmp_path: Path) -> None:
    """A backup beside a note changed elsewhere is a file of the save, so its row and
    the backup stay, blocked, until they are resolved with `forget` or `drop_previous`."""
    root = _root(tmp_path)
    note = tmp_path / "note.md"
    note.write_bytes(b"old")
    journal = _journal(tmp_path)
    outcome = await _save(
        _service(journal, root, _BackupHeldWhileCleaning()), root, b"new", _sha(b"old")
    )
    [row] = journal.list_pending()
    assert outcome == PublicationOutcome(row.operation_id, "saved", True)
    assert row.state == "published"
    note.write_bytes(b"edited elsewhere")
    for _launch in range(2):
        assert await _service(journal, root).recover_pending() == [
            PublicationOutcome(row.operation_id, "blocked", True)
        ]
    assert journal.list_pending() == [row]
    assert note.read_bytes() == b"edited elsewhere"
    assert (tmp_path / row.backup_name).read_bytes() == b"old"


def _reads_busy(monkeypatch: pytest.MonkeyPatch, store: _Store) -> None:
    """Every store read meets a busy store; write transactions still commit."""
    real_connection = store._connection

    @contextmanager
    def writes_only(*, write: bool = False) -> Iterator[sqlite3.Connection]:
        if not write:
            raise StateError("busy")  # a read after the commit would meet a busy store
        with real_connection(write=True) as connection:
            yield connection

    monkeypatch.setattr(store, "_connection", writes_only)


def test_a_checkpoint_reads_its_row_back_before_it_commits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A checkpoint's only store access is its write transaction, so a `busy` it raises
    always means that it did not commit. `reserve` returns the row it inserted."""
    root = _root(tmp_path)
    store = _Store(tmp_path / "journal.sqlite3")
    journal = PublicationJournal(cast(Any, store))
    _reads_busy(monkeypatch, store)
    reservation = VaultPublication().reserve()
    row = journal.reserve(root, "note.md", reservation, "replace", _sha(b"new"), 3, _sha(b"old"))
    staged = journal.record_stage(row.operation_id, "1:2")
    prepared = journal.record_prepared(row.operation_id, FileFingerprint("1:3", _sha(b"old"), 3))
    moved = journal.transition(row.operation_id, "prepared", "backed_up")
    assert [row.state, staged.state, prepared.state, moved.state] == [
        "reserved",
        "staged",
        "prepared",
        "backed_up",
    ]


async def test_a_busy_read_after_the_reserve_commit_strands_no_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No journal read follows the `reserved` commit, so a busy store there cannot answer
    `journal_unavailable` while a `reserved` row stays behind."""
    root = _root(tmp_path)
    store = _Store(tmp_path / "journal.sqlite3")
    journal = PublicationJournal(cast(Any, store))
    _reads_busy(monkeypatch, store)
    outcome = await _save(_service(journal, root), root, b"new", None)
    monkeypatch.undo()
    assert outcome == PublicationOutcome(outcome.operation_id, "saved")
    assert journal.list_pending() == []
    assert (tmp_path / "note.md").read_bytes() == b"new"


async def test_recovery_publishes_the_note_after_a_busy_backed_up_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """In recovery, a `prepared` row whose note has not moved yet completes even when its
    `backed_up` commit meets a busy store, so the note is never left at its backup."""
    root = _root(tmp_path)
    note = tmp_path / "note.md"
    note.write_bytes(b"old")
    journal = _journal(tmp_path)
    native = VaultPublication()
    reservation = native.reserve()
    row = journal.reserve(root, "note.md", reservation, "replace", _sha(b"new"), 3, _sha(b"old"))
    async with MutationCoordinator().lease(root, ("note.md",)) as lease:
        staged = native.stage(lease, root, "note.md", reservation, b"new")
        journal.record_stage(row.operation_id, staged.physical_id)
        with native.prepare_replace(lease, staged, _sha(b"old"), staged.backup_name) as ready:
            journal.record_prepared(row.operation_id, ready.old)
    real_transition = journal.transition

    def busy_backed_up(operation_id: str, expected: Any, state: Any) -> object:
        if state == "backed_up":
            raise StateError("busy")
        return real_transition(operation_id, expected, state)

    monkeypatch.setattr(journal, "transition", busy_backed_up)
    assert await _service(journal, root).recover_pending() == [
        PublicationOutcome(row.operation_id, "recovered")
    ]
    assert note.read_bytes() == b"new"
    assert journal.list_pending() == []
    assert not list(tmp_path.glob(".viki-*"))


async def test_a_recovery_gap_collision_after_a_busy_backed_up_checkpoint_commits_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Recovery's twin of the save's gap collision: `conflict` is committed from `prepared`
    too, and every file stays."""
    root = _root(tmp_path)
    note = tmp_path / "note.md"
    note.write_bytes(b"old")
    journal = _journal(tmp_path)
    native = VaultPublication()
    reservation = native.reserve()
    row = journal.reserve(root, "note.md", reservation, "replace", _sha(b"new"), 3, _sha(b"old"))
    async with MutationCoordinator().lease(root, ("note.md",)) as lease:
        staged = native.stage(lease, root, "note.md", reservation, b"new")
        journal.record_stage(row.operation_id, staged.physical_id)
        with native.prepare_replace(lease, staged, _sha(b"old"), staged.backup_name) as ready:
            journal.record_prepared(row.operation_id, ready.old)
    real_transition = journal.transition

    def busy_backed_up(operation_id: str, expected: Any, state: Any) -> object:
        if state == "backed_up":
            raise StateError("busy")
        return real_transition(operation_id, expected, state)

    monkeypatch.setattr(journal, "transition", busy_backed_up)
    assert await _service(journal, root, _RacerAfterBackup()).recover_pending() == [
        PublicationOutcome(row.operation_id, "conflict", True)
    ]
    [kept] = journal.list_pending()
    assert kept.state == "conflict"
    assert note.read_bytes() == b"external"
    assert (tmp_path / row.backup_name).read_bytes() == b"old"
    assert (tmp_path / row.stage_name).read_bytes() == b"new"


async def test_a_cleared_row_logs_only_its_id_and_state(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A folder renamed during an unfinished save takes the save's files with it, so the next
    launch clears the row. One warning keeps that visible, and the files stay in the moved
    folder."""
    vault = tmp_path / "vault"
    (vault / "secret-folder").mkdir(parents=True)
    (vault / "secret-folder" / "secret-name.md").write_bytes(b"old")
    root = _root(vault)
    journal = _journal(tmp_path)
    row = await _plant_moved(journal, root, "backed_up", "secret-folder/secret-name.md")
    moved = vault / "moved"
    (vault / "secret-folder").rename(moved)
    with caplog.at_level("WARNING", logger="viki_agent.vaults.vault_publication"):
        outcomes = await _service(journal, root).recover_pending()
    assert outcomes == [PublicationOutcome(row.operation_id, "recovered")]
    assert journal.list_pending() == []
    [record] = caplog.records
    message = record.getMessage()
    assert message == f"vault publication {row.operation_id} cleared: backed_up"
    for private in ("secret", str(tmp_path), STAGE_PREFIX, BACKUP_PREFIX):
        assert private not in message
    assert (moved / row.backup_name).read_bytes() == b"old"
    assert (moved / row.stage_name).read_bytes() == b"new"
    assert not (moved / "secret-name.md").exists()


async def test_a_busy_stage_checkpoint_whose_discard_fails_keeps_its_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The row is released only once the stage is gone; a stage that stays keeps its row,
    so no unrecorded stage is ever left without one."""
    root = _root(tmp_path)
    journal = _journal(tmp_path)
    monkeypatch.setattr(journal, "record_stage", _store_busy)
    with pytest.raises(PublicationError) as caught:
        await _save(_service(journal, root, _DiscardFails()), root, b"new", None)
    [row] = journal.list_pending()
    assert (caught.value.code, caught.value.operation_id) == ("recovery_required", row.operation_id)
    assert row.state == "reserved"
    assert (tmp_path / row.stage_name).read_bytes() == b"new"
