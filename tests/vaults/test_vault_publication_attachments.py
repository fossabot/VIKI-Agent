"""The publication service writes one non-note file: a create-only chat attachment."""

from __future__ import annotations

import asyncio
import hashlib
import os
import sqlite3
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal, cast

import pytest

from viki_agent import winproc
from viki_agent.storage import SessionStore, StateError
from viki_agent.vaults.vault_mutation import (
    MAX_PUBLICATION_BYTES,
    MAX_STAGE_BYTES,
    MutationCoordinator,
    MutationError,
    MutationLease,
    OrdinaryMutationRoot,
    PublishedPublication,
    StagedPublication,
    VaultPublication,
)
from viki_agent.vaults.vault_publication import (
    JOURNAL_SCHEMA,
    FolderOutcome,
    PublicationError,
    PublicationJournal,
    PublicationOutcome,
    PublicationRecoveryStatus,
    PublicationRow,
    ResolveOutcome,
    VaultPublicationService,
    WorkspaceGate,
)
from viki_agent.workspaces import root_key

REF = "attachments/photo.png"
# Only for the one wait that must end by the lease deadline or by the lease's release
# (AGENTS.md's HANG_GUARD_S exception).
HANG_GUARD_S = 5

# Each refused for its shape: only exactly `attachments/<leaf>` with an image suffix is an
# attachment, so any other image-suffixed ref keeps the note grammar and is refused there.
HOSTILE_REFS = (
    "photo.png",
    "other/photo.png",
    "Attachments/photo.png",
    "attachments/sub/photo.png",
    "attachments/.photo.png",
    "attachments//photo.png",
    "attachments/./photo.png",
    "attachments/../photo.png",
    "/attachments/photo.png",
    "attachments\\photo.png",
    "attachments/photo.png:stream",
    "attachments/con.png",
    "attachments/photo.gif",
)

_ATTACHMENT_CRASH = r"""
import asyncio
import hashlib
import os
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from viki_agent.storage import SessionStore
from viki_agent.vaults.vault_mutation import (
    MutationCoordinator, OrdinaryMutationRoot, VaultPublication,
)
from viki_agent.vaults.vault_publication import PublicationJournal
from viki_agent.workspaces import root_key

source, checkpoint, vault_arg, database_arg, workspace_id = sys.argv[1:]
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
ref = "attachments/photo.png"
content = b"\x89PNG" + bytes(3 << 20)

def stop(name):
    if checkpoint == name:
        os._exit(73)

async def run():
    reservation = native.reserve()
    row = journal.reserve(
        root, ref, reservation, "create", hashlib.sha256(content).hexdigest(), len(content), None
    )
    stop("reserved")
    async with MutationCoordinator().lease(root, (ref,)) as lease:
        staged = native.stage(lease, root, ref, reservation, content)
        stop("stage_uncheckpointed")
        row = journal.record_stage(row.operation_id, staged.physical_id)
        stop("staged")
        native.publish_create(lease, staged)
        stop("renamed")
        journal.transition(row.operation_id, "staged", "published")
        stop("published")
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


class _PublishBusy(VaultPublication):
    """A transient refusal of the create's rename, as another process holding the stage."""

    def publish_create(
        self, lease: MutationLease, staged: StagedPublication
    ) -> PublishedPublication:
        raise MutationError("stage_busy", stage_retained=True)


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


def _vault(tmp_path: Path) -> tuple[Path, OrdinaryMutationRoot, PublicationJournal, _Store]:
    vault = tmp_path / "vault"
    (vault / "attachments").mkdir(parents=True)
    store = _Store(tmp_path / "journal.sqlite3")
    return vault, _root(vault), PublicationJournal(cast(Any, store)), store


def _service(
    journal: PublicationJournal,
    root: OrdinaryMutationRoot,
    native: VaultPublication | None = None,
    gate: WorkspaceGate | None = None,
    coordinator: MutationCoordinator | None = None,
) -> VaultPublicationService:
    def resolve(workspace_id: str, revision: int, physical_id: str) -> OrdinaryMutationRoot:
        assert (workspace_id, revision, physical_id) == (
            root.workspace_id,
            root.policy_revision,
            root.physical_id,
        )
        return root

    return VaultPublicationService(
        journal, resolve, coordinator or MutationCoordinator(), native, workspace_gate=gate
    )


def _unresolved(
    journal: PublicationJournal, gate: WorkspaceGate | None = None
) -> VaultPublicationService:
    """A service whose refusal must come before it resolves the root, so before any vault I/O."""

    def resolve(*_args: object) -> OrdinaryMutationRoot:
        raise AssertionError("the root resolved")

    return VaultPublicationService(journal, resolve, MutationCoordinator(), workspace_gate=gate)


def _image(size: int) -> bytes:
    return b"\x89PNG" + bytes(size - 4)


def _sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _files(vault: Path) -> dict[str, str]:
    """Every file under the vault by its SHA-256, so a failure never prints 20 MiB."""
    return {
        path.relative_to(vault).as_posix(): _sha(path.read_bytes())
        for path in vault.rglob("*")
        if path.is_file()
    }


def _hashed(files: dict[str, bytes]) -> dict[str, str]:
    return {name: _sha(content) for name, content in files.items()}


async def _save(
    service: VaultPublicationService,
    root: OrdinaryMutationRoot,
    ref: str,
    content: bytes,
    expected: str | None = None,
) -> PublicationOutcome:
    return await service.save(
        root.workspace_id,
        root.policy_revision,
        root.physical_id,
        ref,
        content,
        expected_sha256=expected,
    )


async def _plant(
    journal: PublicationJournal,
    root: OrdinaryMutationRoot,
    content: bytes,
    until: Literal["reserved", "staged", "renamed", "published"],
) -> PublicationRow:
    """The row and files a crash leaves after the checkpoint `until`."""
    native = VaultPublication()
    reservation = native.reserve()
    row = journal.reserve(root, REF, reservation, "create", _sha(content), len(content), None)
    if until == "reserved":
        return row
    async with MutationCoordinator().lease(root, (REF,)) as lease:
        staged = native.stage(lease, root, REF, reservation, content)
        row = journal.record_stage(row.operation_id, staged.physical_id)
        if until in {"renamed", "published"}:
            native.publish_create(lease, staged)
        if until == "published":
            row = journal.transition(row.operation_id, "staged", "published")
    return row


# --- the create path ---------------------------------------------------------------------------


async def test_an_attachment_over_the_note_cap_saves_and_clears_its_row(tmp_path: Path) -> None:
    vault, root, journal, _store = _vault(tmp_path)
    content = _image(MAX_PUBLICATION_BYTES + 1)

    outcome = await _save(_service(journal, root), root, REF, content)

    assert outcome.status == "saved" and not outcome.cleanup_pending
    assert _files(vault) == _hashed({REF: content})
    assert journal.list_pending() == []


async def test_the_attachment_cap_is_exact_and_refuses_before_any_vault_write(
    tmp_path: Path,
) -> None:
    vault, root, journal, _store = _vault(tmp_path)
    service = _service(journal, root)

    with pytest.raises(PublicationError, match="invalid_publication"):
        await _save(_unresolved(journal), root, REF, _image(MAX_STAGE_BYTES + 1))
    assert _files(vault) == _hashed({})
    assert journal.list_pending() == []

    content = _image(MAX_STAGE_BYTES)
    assert (await _save(service, root, REF, content)).status == "saved"
    assert _files(vault) == _hashed({REF: content})


@pytest.mark.parametrize("ref", ["note.md", "attachments/note.md", *HOSTILE_REFS])
async def test_a_ref_that_is_not_an_attachment_keeps_the_note_cap(tmp_path: Path, ref: str) -> None:
    vault, root, journal, _store = _vault(tmp_path)

    with pytest.raises(PublicationError, match="invalid_publication"):
        await _save(_unresolved(journal), root, ref, _image(MAX_PUBLICATION_BYTES + 1))
    assert _files(vault) == _hashed({})
    assert journal.list_pending() == []


@pytest.mark.parametrize("ref", HOSTILE_REFS)
async def test_an_image_ref_that_is_not_exactly_attachments_leaf_writes_nothing(
    tmp_path: Path, ref: str
) -> None:
    vault, root, journal, _store = _vault(tmp_path)

    with pytest.raises(MutationError) as caught:
        await _save(_service(journal, root), root, ref, _image(16))
    assert caught.value.code == "invalid_ref"
    assert _files(vault) == _hashed({})
    assert journal.list_pending() == []


async def test_an_attachment_replace_is_refused_before_any_vault_write(tmp_path: Path) -> None:
    vault, root, journal, _store = _vault(tmp_path)
    old = _image(16)
    (vault / REF).write_bytes(old)

    with pytest.raises(PublicationError, match="invalid_publication"):
        await _save(_unresolved(journal), root, REF, _image(32), _sha(old))
    assert _files(vault) == _hashed({REF: old})
    assert journal.list_pending() == []


async def test_an_attachment_create_never_replaces_an_existing_file(tmp_path: Path) -> None:
    vault, root, journal, _store = _vault(tmp_path)
    existing = b"existing"
    (vault / REF).write_bytes(existing)

    outcome = await _save(_service(journal, root), root, REF, _image(MAX_PUBLICATION_BYTES + 1))

    assert outcome.status == "conflict" and not outcome.cleanup_pending
    assert _files(vault) == _hashed({REF: existing})
    assert journal.list_pending() == []


def test_the_journal_admits_an_attachment_create_only_at_its_cap(tmp_path: Path) -> None:
    _vault_path, root, journal, _store = _vault(tmp_path)
    native = VaultPublication()
    digest = _sha(b"")

    def reserve(ref: str, size: int, expected: str | None = None) -> PublicationRow:
        kind = "create" if expected is None else "replace"
        return journal.reserve(root, ref, native.reserve(), kind, digest, size, expected)

    refused = [
        (REF, MAX_STAGE_BYTES + 1, None),
        (REF, 3, digest),
        ("note.md", MAX_PUBLICATION_BYTES + 1, None),
        ("attachments/note.md", MAX_PUBLICATION_BYTES + 1, None),
        *((ref, 3, None) for ref in HOSTILE_REFS),
    ]
    for ref, size, expected in refused:
        with pytest.raises(PublicationError, match="invalid_publication"):
            reserve(ref, size, expected)
    assert journal.list_pending() == []

    row = reserve(REF, MAX_STAGE_BYTES)
    assert journal.list_pending() == [row]


def test_a_note_row_over_the_note_cap_is_still_journal_corrupt(tmp_path: Path) -> None:
    _vault_path, root, journal, store = _vault(tmp_path)
    native = VaultPublication()
    row = journal.reserve(root, "note.md", native.reserve(), "create", _sha(b"new"), 3, None)
    with store._connection(write=True) as connection:
        connection.execute(
            "UPDATE vault_publications SET stage_bytes=? WHERE operation_id=?",
            (MAX_PUBLICATION_BYTES + 1, row.operation_id),
        )
    with pytest.raises(PublicationError, match="journal_corrupt"):
        journal.list_pending()


# --- recovery ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("checkpoint", "expected_status", "published"),
    [
        ("reserved", "recovered", False),
        ("stage_uncheckpointed", "blocked", False),
        ("staged", "recovered", True),
        ("renamed", "recovered", True),
        ("published", "recovered", True),
        ("deleted", None, True),
    ],
)
async def test_real_process_exit_recovery_matrix_for_an_attachment(
    tmp_path: Path, checkpoint: str, expected_status: str | None, published: bool
) -> None:
    vault = tmp_path / "vault"
    (vault / "attachments").mkdir(parents=True)
    root = _root(vault)
    database = tmp_path / "state.sqlite3"
    result = winproc.run(
        [
            sys.executable,
            "-I",
            "-c",
            _ATTACHMENT_CRASH,
            str(Path(__file__).parents[2] / "src"),
            checkpoint,
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
            # A stage whose identity was never recorded is not provably ours: it stays.
            assert [path.name for path in (vault / "attachments").iterdir()] == [
                journal.list_pending()[0].stage_name
            ]
        else:
            assert second == []
    assert _files(vault).get(REF) == (_sha(_image((3 << 20) + 4)) if published else None)


@pytest.mark.parametrize(
    "external", [b"external", _image(MAX_PUBLICATION_BYTES + 1)], ids=["different", "identical"]
)
async def test_a_staged_attachment_beside_another_file_discards_only_its_stage(
    tmp_path: Path, external: bytes
) -> None:
    """Any file at the name, even one with the staged bytes, is not ours to replace."""
    vault, root, journal, _store = _vault(tmp_path)
    row = await _plant(journal, root, _image(MAX_PUBLICATION_BYTES + 1), "staged")
    (vault / REF).write_bytes(external)

    outcomes = await _service(journal, root).recover_pending()

    assert outcomes == [PublicationOutcome(row.operation_id, "conflict")]
    assert _files(vault) == _hashed({REF: external})
    assert journal.list_pending() == []


@pytest.mark.parametrize("until", ["reserved", "staged", "published"])
async def test_a_foreign_file_at_the_stage_name_blocks_untouched(
    tmp_path: Path, until: Literal["reserved", "staged", "published"]
) -> None:
    vault, root, journal, _store = _vault(tmp_path)
    content = _image(64)
    row = await _plant(journal, root, content, until)
    stage = vault / "attachments" / row.stage_name
    # The same bytes under another identity: written beside the stage and renamed over it, so
    # the new file cannot take the stage's freed inode number (ext4 reuses it at once).
    other = stage.with_name(f"{row.stage_name}.other")
    other.write_bytes(content)
    os.replace(other, stage)
    before = _files(vault)

    outcomes = await _service(journal, root).recover_pending()

    assert outcomes == [PublicationOutcome(row.operation_id, "blocked", True)]
    assert _files(vault) == before
    assert journal.list_pending() == [row]


async def test_a_published_row_whose_file_moved_back_to_its_stage_blocks_untouched(
    tmp_path: Path,
) -> None:
    """Only a `staged` row completes, as for a note: a published one is never published again."""
    vault, root, journal, _store = _vault(tmp_path)
    content = _image(64)
    row = await _plant(journal, root, content, "published")
    (vault / REF).rename(vault / "attachments" / row.stage_name)  # its own identity

    assert await _service(journal, root).recover_pending() == [
        PublicationOutcome(row.operation_id, "blocked", True)
    ]
    assert _files(vault) == _hashed({f"attachments/{row.stage_name}": content})
    assert journal.list_pending() == [row]


async def test_a_transient_refusal_keeps_the_row_for_the_next_launch(tmp_path: Path) -> None:
    vault, root, journal, _store = _vault(tmp_path)
    content = _image(MAX_PUBLICATION_BYTES + 1)
    row = await _plant(journal, root, content, "staged")

    assert await _service(journal, root, _PublishBusy()).recover_pending() == [
        PublicationOutcome(row.operation_id, "blocked", True)
    ]
    assert journal.list_pending() == [row]
    assert _files(vault) == _hashed({f"attachments/{row.stage_name}": content})

    assert await _service(journal, root).recover_pending() == [
        PublicationOutcome(row.operation_id, "recovered")
    ]
    assert _files(vault) == _hashed({REF: content})
    assert journal.list_pending() == []


async def test_a_deleted_attachments_folder_clears_its_rows(tmp_path: Path) -> None:
    vault, root, journal, _store = _vault(tmp_path)
    row = await _plant(journal, root, _image(64), "staged")
    for path in (vault / "attachments").iterdir():
        path.unlink()
    (vault / "attachments").rmdir()

    assert await _service(journal, root).recover_pending() == [
        PublicationOutcome(row.operation_id, "recovered")
    ]
    assert journal.list_pending() == []
    assert _files(vault) == _hashed({})


async def test_attachment_and_note_rows_recover_side_by_side(tmp_path: Path) -> None:
    vault, root, journal, _store = _vault(tmp_path)
    content = _image(MAX_PUBLICATION_BYTES + 1)
    attachment = await _plant(journal, root, content, "staged")
    native = VaultPublication()
    reservation = native.reserve()
    note = journal.reserve(root, "note.md", reservation, "create", _sha(b"new"), 3, None)
    async with MutationCoordinator().lease(root, ("note.md",)) as lease:
        staged = native.stage(lease, root, "note.md", reservation, b"new")
    journal.record_stage(note.operation_id, staged.physical_id)

    outcomes = await _service(journal, root).recover_pending()

    assert {outcome.operation_id: outcome.status for outcome in outcomes} == {
        attachment.operation_id: "recovered",
        note.operation_id: "recovered",
    }
    assert _files(vault) == _hashed({REF: content, "note.md": b"new"})
    assert journal.list_pending() == []


async def test_a_replace_row_on_an_attachment_ref_blocks_alone(tmp_path: Path) -> None:
    """A row the journal never admits, as a forged or corrupted store would hold it."""
    vault, root, journal, store = _vault(tmp_path)
    old = _image(16)
    (vault / REF).write_bytes(old)
    forged = VaultPublication().reserve()
    recorded = "2026-01-01T00:00:00.000Z"
    with store._connection(write=True) as connection:
        connection.execute(
            "INSERT INTO vault_publications (operation_id,workspace_id,root_physical_id,"
            "policy_revision,ref,kind,stage_name,backup_name,stage_sha256,stage_bytes,"
            "expected_sha256,state,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                forged.operation_id,
                root.workspace_id,
                root.physical_id,
                root.policy_revision,
                REF,
                "replace",
                forged.stage_name,
                forged.backup_name,
                _sha(b"new"),
                3,
                _sha(old),
                "reserved",
                recorded,
                recorded,
            ),
        )

    assert await _service(journal, root).recover_pending() == [
        PublicationOutcome(forged.operation_id, "blocked", True)
    ]
    assert _files(vault) == _hashed({REF: old})
    assert [row.operation_id for row in journal.list_pending()] == [forged.operation_id]


# --- listing and resolve -----------------------------------------------------------------------


@pytest.mark.parametrize("ref", ["GEMINI.md", "sub/CLAUDE.md", REF])
async def test_a_published_replace_whose_ref_is_no_longer_admitted_offers_only_forget(
    tmp_path: Path, ref: str
) -> None:
    """The offer and `resolve` share one admission rule, so neither offers what the
    other refuses."""
    vault, root, journal, store = _vault(tmp_path)
    forged = VaultPublication().reserve()
    recorded = "2026-01-01T00:00:00.000Z"
    with store._connection(write=True) as connection:
        connection.execute(
            "INSERT INTO vault_publications (operation_id,workspace_id,root_physical_id,"
            "policy_revision,ref,kind,stage_name,backup_name,stage_physical_id,stage_sha256,"
            "stage_bytes,target_physical_id,expected_sha256,target_bytes,state,created_at,"
            "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                forged.operation_id,
                root.workspace_id,
                root.physical_id,
                root.policy_revision,
                ref,
                "replace",
                forged.stage_name,
                forged.backup_name,
                "1:2",
                _sha(b"new"),
                3,
                "1:3",
                _sha(b"old"),
                3,
                "published",
                recorded,
                recorded,
            ),
        )
    service = _service(journal, root)
    before = _tree(vault)

    [status] = service.recovery_status()
    assert status.resolvable == ("forget",)
    with pytest.raises(PublicationError, match="not_resolvable"):
        await service.resolve(forged.operation_id, "drop_previous")
    assert await service.resolve(forged.operation_id, "forget") == ResolveOutcome(
        forged.operation_id, "forget", stage_retained=False, backup_retained=True
    )
    assert journal.list_pending() == []
    assert _tree(vault) == before


@pytest.mark.parametrize("until", ["reserved", "staged", "published"])
async def test_an_attachment_row_lists_and_resolves_honestly(
    tmp_path: Path, until: Literal["reserved", "staged", "published"]
) -> None:
    vault, root, journal, _store = _vault(tmp_path)
    row = await _plant(journal, root, _image(MAX_PUBLICATION_BYTES + 1), until)
    service = _service(journal, root)
    before = _files(vault)

    assert service.recovery_status() == [
        PublicationRecoveryStatus(
            row.operation_id, root.workspace_id, REF, until, "recovery_required", ("forget",)
        )
    ]
    with pytest.raises(PublicationError, match="not_resolvable"):
        await service.resolve(row.operation_id, "drop_previous")
    assert await service.resolve(row.operation_id, "forget") == ResolveOutcome(
        row.operation_id, "forget", stage_retained=until != "published", backup_retained=False
    )
    assert journal.list_pending() == []
    assert _files(vault) == before


# --- the opt-in that creates a create's missing parent folders ---------------------------------


def _tree(vault: Path) -> list[str]:
    """Every entry under the vault, folders included."""
    return sorted(path.relative_to(vault).as_posix() for path in vault.rglob("*"))


async def _create(
    service: VaultPublicationService, root: OrdinaryMutationRoot, ref: str, content: bytes
) -> FolderOutcome:
    return await service.save(
        root.workspace_id,
        root.policy_revision,
        root.physical_id,
        ref,
        content,
        expected_sha256=None,
        create_folders=True,
    )


async def test_an_opted_in_create_makes_a_missing_memory_folder_and_reports_it(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    root = _root(vault)
    journal = PublicationJournal(cast(Any, _Store(tmp_path / "journal.sqlite3")))

    outcome = await _create(_service(journal, root), root, "memory/m.md", b"record")

    assert outcome == FolderOutcome(outcome.operation_id, "saved", False, ("memory",))
    assert _tree(vault) == ["memory", "memory/m.md"]
    assert (vault / "memory" / "m.md").read_bytes() == b"record"
    assert journal.list_pending() == []


async def test_without_the_opt_in_a_missing_folder_still_refuses_and_creates_nothing(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    root = _root(vault)
    journal = PublicationJournal(cast(Any, _Store(tmp_path / "journal.sqlite3")))

    with pytest.raises(MutationError) as caught:
        await _save(_service(journal, root), root, "memory/m.md", b"record")
    assert caught.value.code == "unsafe_parent"
    assert _tree(vault) == []
    assert journal.list_pending() == []


async def test_an_opted_in_create_reports_only_the_folders_it_made(tmp_path: Path) -> None:
    vault, root, journal, _store = _vault(tmp_path)
    service = _service(journal, root)

    first = await _create(service, root, "attachments/a/b/n.md", b"one")
    second = await _create(service, root, "attachments/a/b/m.md", b"two")

    assert first.created_folders == ("attachments/a", "attachments/a/b")
    assert second == FolderOutcome(second.operation_id, "saved", False, ())
    assert _files(vault) == _hashed(
        {"attachments/a/b/n.md": b"one", "attachments/a/b/m.md": b"two"}
    )


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


@pytest.mark.parametrize("ref", ["memory/m.md", "memory/sub/m.md"])
async def test_an_opted_in_create_never_creates_around_a_reparse_point(
    tmp_path: Path, ref: str
) -> None:
    """Off Windows the link is a symlink."""
    vault = tmp_path / "vault"
    vault.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    _link_folder(vault / "memory", outside)
    root = _root(vault)
    journal = PublicationJournal(cast(Any, _Store(tmp_path / "journal.sqlite3")))

    with pytest.raises(MutationError) as caught:
        await _create(_service(journal, root), root, ref, b"record")
    assert caught.value.code == "unsafe_parent"
    assert not list(outside.iterdir())
    assert _tree(vault) == ["memory"]
    assert journal.list_pending() == []


async def test_an_opted_in_attachment_create_makes_its_folder(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    root = _root(vault)
    journal = PublicationJournal(cast(Any, _Store(tmp_path / "journal.sqlite3")))
    content = _image(MAX_PUBLICATION_BYTES + 1)

    outcome = await _create(_service(journal, root), root, REF, content)

    assert outcome == FolderOutcome(outcome.operation_id, "saved", False, ("attachments",))
    assert _files(vault) == _hashed({REF: content})
    assert journal.list_pending() == []


async def test_the_opt_in_is_create_only(tmp_path: Path) -> None:
    vault, root, journal, _store = _vault(tmp_path)
    (vault / "note.md").write_bytes(b"old")

    with pytest.raises(PublicationError, match="invalid_publication"):
        await _unresolved(journal).save(
            root.workspace_id,
            root.policy_revision,
            root.physical_id,
            "sub/note.md",
            b"new",
            expected_sha256=_sha(b"old"),
            create_folders=True,
        )
    assert _tree(vault) == ["attachments", "note.md"]
    assert journal.list_pending() == []


# --- the injected workspace gate ---------------------------------------------------------------


def _refusing(calls: list[str]) -> WorkspaceGate:
    def gate(workspace_id: str) -> None:
        calls.append(workspace_id)
        raise PublicationError("vault_recovery_required")

    return gate


async def test_a_refusing_gate_stops_every_write_before_any_vault_io(tmp_path: Path) -> None:
    vault, root, journal, _store = _vault(tmp_path)
    (vault / "note.md").write_bytes(b"old")
    before = _files(vault)
    calls: list[str] = []
    service = _unresolved(journal, _refusing(calls))
    writes = [
        ("new.md", b"new", None, False),
        ("note.md", b"new", _sha(b"old"), False),
        (REF, _image(MAX_PUBLICATION_BYTES + 1), None, False),
        ("memory/m.md", b"record", None, True),
        (REF, _image(64), None, True),
    ]

    for ref, content, expected, create_folders in writes:
        with pytest.raises(PublicationError, match="vault_recovery_required"):
            await service.save(
                root.workspace_id,
                root.policy_revision,
                root.physical_id,
                ref,
                content,
                expected_sha256=expected,
                create_folders=create_folders,
            )
    assert calls == [root.workspace_id] * len(writes)
    assert _files(vault) == before
    assert _tree(vault) == ["attachments", "note.md"]
    assert journal.list_pending() == []


async def test_a_gate_that_cannot_read_its_journal_refuses_typed(tmp_path: Path) -> None:
    _vault_path, root, journal, _store = _vault(tmp_path)

    def busy(_workspace_id: str) -> None:
        raise StateError("busy")

    with pytest.raises(PublicationError, match="journal_unavailable"):
        await _save(_unresolved(journal, busy), root, REF, _image(64))


async def test_recovery_listing_and_resolve_run_past_a_refusing_gate(tmp_path: Path) -> None:
    vault, root, journal, _store = _vault(tmp_path)
    content = _image(64)
    recovered = await _plant(journal, root, content, "staged")
    calls: list[str] = []
    service = _service(journal, root, gate=_refusing(calls))

    assert await service.recover_pending() == [
        PublicationOutcome(recovered.operation_id, "recovered")
    ]
    assert _files(vault) == _hashed({REF: content})

    blocked = await _plant(journal, root, content, "reserved")
    (vault / "attachments" / blocked.stage_name).write_bytes(b"not ours")
    assert [status.operation_id for status in service.recovery_status()] == [blocked.operation_id]
    assert await service.resolve(blocked.operation_id, "forget") == ResolveOutcome(
        blocked.operation_id, "forget", stage_retained=True, backup_retained=False
    )
    assert journal.list_pending() == []
    assert calls == []


# --- the lease deadline `save` takes -----------------------------------------------------------


async def _save_waiting(
    service: VaultPublicationService, root: OrdinaryMutationRoot, content: bytes, **kwargs: Any
) -> PublicationOutcome:
    return await service.save(
        root.workspace_id,
        root.policy_revision,
        root.physical_id,
        REF,
        content,
        expected_sha256=None,
        **kwargs,
    )


async def test_a_held_lease_refuses_vault_locked_once_wait_s_passes(tmp_path: Path) -> None:
    vault, root, journal, _store = _vault(tmp_path)
    coordinator = MutationCoordinator()
    service = _service(journal, root, coordinator=coordinator)
    before = _files(vault)

    async with coordinator.lease(root, (REF,)):
        async with asyncio.timeout(HANG_GUARD_S):
            with pytest.raises(MutationError) as refused:
                await _save_waiting(service, root, _image(64), wait_s=0.02)

    assert refused.value.code == "vault_locked"
    assert _files(vault) == before == {}  # no stage file either
    assert journal.list_pending() == []


async def test_a_save_without_wait_s_still_waits_for_the_lease(tmp_path: Path) -> None:
    vault, root, journal, _store = _vault(tmp_path)
    coordinator = MutationCoordinator()
    service = _service(journal, root, coordinator=coordinator)
    content = _image(64)

    async with coordinator.lease(root, (REF,)):
        saving = asyncio.ensure_future(_save_waiting(service, root, content))
        # A refusing save has failed by now; one that waits is still pending.
        done, _pending = await asyncio.wait({saving}, timeout=0.05)
        assert not done
        assert _files(vault) == {}

    async with asyncio.timeout(HANG_GUARD_S):
        outcome = await saving
    assert outcome.status == "saved"
    assert _files(vault) == _hashed({REF: content})
    assert journal.list_pending() == []
