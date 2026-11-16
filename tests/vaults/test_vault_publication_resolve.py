"""Resolving a blocked publication row: `forget` and `drop_previous`."""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

import pytest

from viki_agent.vaults.vault_mutation import (
    FileFingerprint,
    MutationCoordinator,
    MutationError,
    MutationLease,
    OrdinaryMutationRoot,
    VaultPublication,
)
from viki_agent.vaults.vault_publication import (
    JOURNAL_SCHEMA,
    PublicationError,
    PublicationJournal,
    PublicationOutcome,
    PublicationRow,
    ResolveOutcome,
    VaultPublicationService,
)
from viki_agent.workspaces import root_key

# A guard on an event that normally fires in milliseconds, never a bound on real work.
HANG_GUARD_S = 5


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


class _DiscardRefuses(VaultPublication):
    """The backup cleanup fails: at save time the row stays `published` with its backup beside."""

    def discard_backup_after_publish(self, *args: Any, **kwargs: Any) -> None:
        raise MutationError("discard_failed")


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
    return PublicationJournal(cast(Any, _Store(tmp_path / "journal.sqlite3")))


def _service(
    journal: PublicationJournal, root: OrdinaryMutationRoot, native: VaultPublication | None = None
) -> VaultPublicationService:
    def resolve_root(workspace_id: str, revision: int, physical_id: str) -> OrdinaryMutationRoot:
        assert (workspace_id, revision, physical_id) == (
            root.workspace_id,
            root.policy_revision,
            root.physical_id,
        )
        return root

    return VaultPublicationService(journal, resolve_root, MutationCoordinator(), native)


def _sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _tree(path: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in path.iterdir() if p.name != "journal.sqlite3"}


async def _save(
    service: VaultPublicationService,
    root: OrdinaryMutationRoot,
    content: bytes,
    expected: str | None,
) -> PublicationOutcome:
    return await service.save(
        root.workspace_id,
        root.policy_revision,
        root.physical_id,
        "note.md",
        content,
        expected_sha256=expected,
    )


async def _published_with_backup(
    tmp_path: Path, root: OrdinaryMutationRoot, journal: PublicationJournal
) -> PublicationRow:
    """A replace that published but could not delete its backup, which leaves its row blocked."""
    (tmp_path / "note.md").write_bytes(b"old")
    outcome = await _save(_service(journal, root, _DiscardRefuses()), root, b"new", _sha(b"old"))
    [row] = journal.list_pending()
    assert outcome == PublicationOutcome(row.operation_id, "saved", True)
    assert row.state == "published"
    return row


def _planted(
    journal: PublicationJournal, root: OrdinaryMutationRoot, state: str, kind: str = "replace"
) -> PublicationRow:
    """A row in `state` with no file behind it: `forget` touches none, so none is needed."""
    native = VaultPublication()
    reservation = native.reserve()
    expected = _sha(b"old") if kind == "replace" else None
    row = journal.reserve(
        root,
        "note.md",
        reservation,
        cast(Any, kind),
        _sha(b"new"),
        3,
        expected,
    )
    if state == "reserved":
        return row
    journal.record_stage(row.operation_id, "1:2")
    if state != "staged":
        if kind == "replace":
            journal.record_prepared(row.operation_id, FileFingerprint("1:3", _sha(b"old"), 3))
        if state in {"backed_up", "published", "conflict"}:
            journal.transition(row.operation_id, ("prepared", "staged"), cast(Any, state))
    row = journal.get(row.operation_id)
    assert row is not None
    return row


async def test_forget_deletes_the_row_touches_no_file_and_lifts_the_freeze(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    journal = _journal(tmp_path)
    row = _planted(journal, root, "conflict")
    stage, backup = tmp_path / row.stage_name, tmp_path / row.backup_name
    stage.write_bytes(b"stage")
    backup.write_bytes(b"backup")
    (tmp_path / "note.md").write_bytes(b"external")
    before = _tree(tmp_path)

    outcome = await _service(journal, root).resolve(row.operation_id, "forget")

    assert outcome == ResolveOutcome(row.operation_id, "forget", True, True)
    assert journal.list_pending() == []
    assert _tree(tmp_path) == before
    # The registration freeze is lifted: the same ref reserves again.
    journal.reserve(root, "note.md", VaultPublication().reserve(), "create", _sha(b"x"), 1, None)


@pytest.mark.parametrize(
    ("state", "kind", "stage_retained", "backup_retained"),
    [
        ("reserved", "create", True, False),
        ("staged", "create", True, False),
        ("reserved", "replace", True, False),
        ("staged", "replace", True, False),
        ("prepared", "replace", True, True),
        ("backed_up", "replace", True, True),
        ("conflict", "replace", True, True),
        ("published", "replace", False, True),
        ("published", "create", False, False),
    ],
)
async def test_forget_names_the_private_files_the_row_state_leaves(
    tmp_path: Path, state: str, kind: str, stage_retained: bool, backup_retained: bool
) -> None:
    root = _root(tmp_path)
    journal = _journal(tmp_path)
    row = _planted(journal, root, state, kind)
    outcome = await _service(journal, root).resolve(row.operation_id, "forget")
    assert outcome == ResolveOutcome(row.operation_id, "forget", stage_retained, backup_retained)
    assert journal.get(row.operation_id) is None


async def test_forget_works_when_the_workspace_no_longer_resolves(tmp_path: Path) -> None:
    root = _root(tmp_path)
    journal = _journal(tmp_path)
    row = _planted(journal, root, "backed_up")

    def gone(*_args: object) -> OrdinaryMutationRoot:
        raise PublicationError("workspace_changed")

    service = VaultPublicationService(journal, gone, MutationCoordinator())
    assert (await service.resolve(row.operation_id, "forget")).choice == "forget"
    assert journal.list_pending() == []


async def test_resolve_refuses_an_unknown_row_a_bad_id_and_a_bad_choice(tmp_path: Path) -> None:
    root = _root(tmp_path)
    journal = _journal(tmp_path)
    row = _planted(journal, root, "staged")
    service = _service(journal, root)
    for operation_id, choice, code in (
        (str(uuid.uuid4()), "forget", "unknown_publication"),
        ("not-a-uuid", "forget", "invalid_publication"),
        (row.operation_id.upper(), "forget", "invalid_publication"),
        (row.operation_id, "delete", "invalid_publication"),
    ):
        with pytest.raises(PublicationError) as caught:
            await service.resolve(operation_id, cast(Any, choice))
        assert caught.value.code == code
    assert journal.list_pending() == [row]


async def test_forgetting_a_row_twice_is_unknown_the_second_time(tmp_path: Path) -> None:
    root = _root(tmp_path)
    journal = _journal(tmp_path)
    row = _planted(journal, root, "staged")
    service = _service(journal, root)
    await service.resolve(row.operation_id, "forget")
    with pytest.raises(PublicationError) as caught:
        await service.resolve(row.operation_id, "forget")
    assert caught.value.code == "unknown_publication"


@pytest.mark.parametrize(
    ("state", "kind", "resolvable"),
    [
        ("reserved", "create", ("forget",)),
        ("staged", "create", ("forget",)),
        ("published", "create", ("forget",)),
        ("reserved", "replace", ("forget",)),
        ("staged", "replace", ("forget",)),
        ("prepared", "replace", ("forget",)),
        ("backed_up", "replace", ("forget",)),
        ("conflict", "replace", ("forget",)),
        ("published", "replace", ("forget", "drop_previous")),
    ],
)
async def test_recovery_status_lists_what_each_row_can_resolve_to(
    tmp_path: Path, state: str, kind: str, resolvable: tuple[str, ...]
) -> None:
    root = _root(tmp_path)
    journal = _journal(tmp_path)
    row = _planted(journal, root, state, kind)
    service = _service(journal, root)
    [status] = service.recovery_status()
    assert status.resolvable == resolvable
    # The listing and `resolve` share one rule: every listed choice is one the row's state admits.
    for choice in ("forget", "drop_previous"):
        if choice not in status.resolvable:
            with pytest.raises(PublicationError) as caught:
                await service.resolve(row.operation_id, cast(Any, choice))
            assert caught.value.code == "not_resolvable"
    assert journal.list_pending() == [row]


async def test_drop_previous_deletes_only_the_backup_and_then_the_row(tmp_path: Path) -> None:
    root = _root(tmp_path)
    journal = _journal(tmp_path)
    row = await _published_with_backup(tmp_path, root, journal)
    assert (tmp_path / row.backup_name).read_bytes() == b"old"

    outcome = await _service(journal, root).resolve(row.operation_id, "drop_previous")

    assert outcome == ResolveOutcome(row.operation_id, "drop_previous", False, False)
    assert journal.list_pending() == []
    assert not (tmp_path / row.backup_name).exists()
    assert (tmp_path / "note.md").read_bytes() == b"new"


@pytest.mark.parametrize("what", ["note_edited", "backup_changed", "backup_gone"])
async def test_drop_previous_refuses_unless_the_note_and_backup_still_match(
    tmp_path: Path, what: str
) -> None:
    root = _root(tmp_path)
    journal = _journal(tmp_path)
    row = await _published_with_backup(tmp_path, root, journal)
    if what == "note_edited":
        (tmp_path / "note.md").write_bytes(b"edited elsewhere")
    elif what == "backup_changed":
        (tmp_path / row.backup_name).write_bytes(b"not the old note")
    else:
        (tmp_path / row.backup_name).unlink()
    before = _tree(tmp_path)

    with pytest.raises(PublicationError) as caught:
        await _service(journal, root).resolve(row.operation_id, "drop_previous")

    assert (caught.value.code, caught.value.operation_id) == ("not_resolvable", row.operation_id)
    assert journal.list_pending() == [row]
    assert _tree(tmp_path) == before


@pytest.mark.parametrize(("state", "kind"), [("conflict", "replace"), ("published", "create")])
async def test_drop_previous_refuses_a_row_that_is_not_a_published_replace(
    tmp_path: Path, state: str, kind: str
) -> None:
    root = _root(tmp_path)
    journal = _journal(tmp_path)
    row = _planted(journal, root, state, kind)
    with pytest.raises(PublicationError) as caught:
        await _service(journal, root).resolve(row.operation_id, "drop_previous")
    assert (caught.value.code, caught.value.operation_id) == ("not_resolvable", row.operation_id)
    assert journal.list_pending() == [row]


async def test_a_refused_backup_delete_keeps_the_row_and_the_backup(tmp_path: Path) -> None:
    root = _root(tmp_path)
    journal = _journal(tmp_path)
    row = await _published_with_backup(tmp_path, root, journal)
    with pytest.raises(PublicationError) as caught:
        await _service(journal, root, _DiscardRefuses()).resolve(row.operation_id, "drop_previous")
    assert (caught.value.code, caught.value.operation_id) == ("recovery_required", row.operation_id)
    assert journal.list_pending() == [row]
    assert (tmp_path / row.backup_name).read_bytes() == b"old"


async def test_drop_previous_refuses_when_the_workspace_changed(tmp_path: Path) -> None:
    root = _root(tmp_path)
    journal = _journal(tmp_path)
    row = await _published_with_backup(tmp_path, root, journal)
    moved = OrdinaryMutationRoot(
        root.workspace_id,
        root.path,
        root.canonical_key,
        root.physical_id,
        root.policy_revision + 1,
        None,
    )

    def changed(*_args: object) -> OrdinaryMutationRoot:
        return moved

    service = VaultPublicationService(journal, changed, MutationCoordinator())
    with pytest.raises(PublicationError) as caught:
        await service.resolve(row.operation_id, "drop_previous")
    assert caught.value.code == "workspace_changed"
    assert journal.list_pending() == [row]
    assert (tmp_path / row.backup_name).read_bytes() == b"old"


async def test_drop_previous_past_its_deadline_refuses_and_changes_nothing(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    journal = _journal(tmp_path)
    row = await _published_with_backup(tmp_path, root, journal)
    before = _tree(tmp_path)
    coordinator = MutationCoordinator()
    service = VaultPublicationService(journal, lambda *_args: root, coordinator)

    async with coordinator.lease(root, ("note.md",)):
        # A guard on a refusal that answers in milliseconds: without the deadline this waits
        # for a lease that is never released.
        async with asyncio.timeout(HANG_GUARD_S):
            with pytest.raises(MutationError) as caught:
                await service.resolve(row.operation_id, "drop_previous", wait_s=0.02)

    assert caught.value.code == "vault_locked"
    assert journal.list_pending() == [row]
    assert _tree(tmp_path) == before


async def test_drop_previous_takes_the_lease_on_the_ref(tmp_path: Path) -> None:
    root = _root(tmp_path)
    journal = _journal(tmp_path)
    row = await _published_with_backup(tmp_path, root, journal)
    leases: list[MutationLease] = []

    class Spy(VaultPublication):
        def discard_backup_after_publish(
            self,
            lease: MutationLease,
            root: OrdinaryMutationRoot,
            ref: str,
            backup_name: str,
            published: FileFingerprint,
            backup: FileFingerprint,
        ) -> None:
            leases.append(lease)
            super().discard_backup_after_publish(lease, root, ref, backup_name, published, backup)

    await _service(journal, root, Spy()).resolve(row.operation_id, "drop_previous")
    assert len(leases) == 1
    # The lease is closed again: a stale capability cannot reach the vault.
    with pytest.raises(MutationError) as caught:
        leases[0].require_target(root, "note.md")
    assert caught.value.code == "lease_required"
