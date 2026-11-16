"""Code saves through the publication service, and their journal rows across a crash."""

from __future__ import annotations

import hashlib
import os
import sys
import uuid
from dataclasses import replace
from pathlib import Path

import pytest

from viki_agent import winproc
from viki_agent.storage import SessionStore
from viki_agent.vaults.vault_mutation import (
    MAX_PUBLICATION_BYTES,
    MutationCoordinator,
    MutationError,
    OrdinaryMutationRoot,
    VaultPublication,
)
from viki_agent.vaults.vault_publication import (
    PublicationError,
    PublicationJournal,
    VaultPublicationService,
)
from viki_agent.workspaces import RowKind

# `test_vault_publication._PUBLICATION_CRASH` with the ref and the whole root as arguments, so a
# code root and a root from a real resolver (any revision, identity or Git dir) both run it.
_CODE_SAVE_CRASH = r"""
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

(
    source,
    checkpoint,
    kind,
    ref,
    root_kind,
    path,
    database_arg,
    workspace_id,
    canonical_key,
    physical_id,
    revision,
    git_common_dir,
) = sys.argv[1:]
root = OrdinaryMutationRoot(
    workspace_id,
    Path(path),
    canonical_key,
    physical_id,
    int(revision),
    git_common_dir or None,
    root_kind,
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
        ref,
        reservation,
        kind,
        hashlib.sha256(content).hexdigest(),
        len(content),
        expected,
    )
    stop("reserved")
    async with MutationCoordinator().lease(root, (ref,)) as lease:
        staged = native.stage(lease, root, ref, reservation, content)
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


def crash_save(
    tmp_path: Path,
    root: OrdinaryMutationRoot,
    ref: str,
    kind: str,
    checkpoint: str,
    database: Path,
) -> None:
    """Save `b"new"` to `ref` in a child process that exits at `checkpoint`, as a crash would.

    `kind` is `create` or `replace`; for a replace the caller first writes `b"old"` at `ref`.
    """
    result = winproc.run(
        [
            sys.executable,
            "-I",
            "-c",
            _CODE_SAVE_CRASH,
            str(Path(__file__).parents[2] / "src"),
            checkpoint,
            kind,
            ref,
            root.kind,
            str(root.path),
            str(database),
            root.workspace_id,
            root.canonical_key,
            root.physical_id,
            str(root.policy_revision),
            root.git_common_dir or "",
        ],
        cwd=tmp_path,
    )
    assert result.returncode == 73, result.stderr


def _root(path: Path, kind: RowKind = "code") -> OrdinaryMutationRoot:
    resolved = path.resolve()
    info = resolved.stat()
    return OrdinaryMutationRoot(
        str(uuid.uuid4()),
        resolved,
        os.path.normcase(str(resolved)),
        f"{info.st_dev}:{info.st_ino}",
        7,
        None,
        kind,
    )


def _service(journal: PublicationJournal, root: OrdinaryMutationRoot) -> VaultPublicationService:
    def resolve(workspace_id: str, revision: int, physical_id: str) -> OrdinaryMutationRoot:
        assert (workspace_id, revision, physical_id) == (
            root.workspace_id,
            root.policy_revision,
            root.physical_id,
        )
        return root

    return VaultPublicationService(journal, resolve, MutationCoordinator())


def _journal(tmp_path: Path) -> PublicationJournal:
    return PublicationJournal(SessionStore(tmp_path / "state.sqlite3", lambda: {}))


def _sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _files(folder: Path) -> set[str]:
    """Every file under the folder, so a stage or backup left behind shows by name."""
    return {path.relative_to(folder).as_posix() for path in folder.rglob("*") if path.is_file()}


def _folder(tmp_path: Path) -> Path:
    folder = tmp_path / "code"
    for parent in ("src", "attachments"):
        (folder / parent).mkdir(parents=True)
    return folder


async def _recovers_to_new_bytes(
    tmp_path: Path, folder: Path, ref: str, kind: str, checkpoint: str
) -> None:
    root = _root(folder)
    target = folder.joinpath(*ref.split("/"))
    if kind == "replace":
        target.write_bytes(b"old")
    database = tmp_path / "state.sqlite3"
    crash_save(tmp_path, root, ref, kind, checkpoint, database)

    journal = PublicationJournal(SessionStore(database, lambda: {}))
    service = _service(journal, root)
    assert [outcome.status for outcome in await service.recover_pending()] == ["recovered"]
    assert await service.recover_pending() == []
    assert journal.list_pending() == []
    assert target.read_bytes() == b"new"
    assert _files(folder) == {ref}


@pytest.mark.parametrize("ref", ["src/app.py", "CLAUDE.md"])
@pytest.mark.parametrize(
    ("kind", "checkpoint"),
    [
        ("create", "staged"),
        ("replace", "prepared"),
        ("replace", "backed_up"),
        ("replace", "published"),
    ],
)
async def test_a_code_row_recovers_at_the_next_launch(
    tmp_path: Path, ref: str, kind: str, checkpoint: str
) -> None:
    await _recovers_to_new_bytes(tmp_path, _folder(tmp_path), ref, kind, checkpoint)


@pytest.mark.parametrize(("kind", "checkpoint"), [("create", "staged"), ("replace", "backed_up")])
async def test_a_code_row_under_attachments_recovers_as_a_file(
    tmp_path: Path, kind: str, checkpoint: str
) -> None:
    # In a vault this ref takes the attachment path, which a code root must not take.
    await _recovers_to_new_bytes(tmp_path, _folder(tmp_path), "attachments/x.png", kind, checkpoint)


async def test_a_vault_row_holding_a_code_only_ref_stays_blocked(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    root = _root(vault, "vault")
    store = SessionStore(tmp_path / "state.sqlite3", lambda: {})
    journal = PublicationJournal(store)
    native = VaultPublication()
    reservation = native.reserve()
    with pytest.raises(PublicationError, match="invalid_publication"):
        journal.reserve(root, "a.py", reservation, "create", _sha(b"new"), 3, None)
    # A real stage of the row's bytes, so a recovery that took the ref would publish it.
    as_code = replace(root, kind="code")
    async with MutationCoordinator().lease(as_code, ("a.py",)) as lease:
        staged = native.stage(lease, as_code, "a.py", reservation, b"new")
    recorded = "2026-01-01T00:00:00.000Z"
    with store._connection(write=True) as connection:
        connection.execute(
            "INSERT INTO vault_publications (operation_id,workspace_id,root_physical_id,"
            "policy_revision,ref,kind,stage_name,backup_name,stage_physical_id,stage_sha256,"
            "stage_bytes,state,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                reservation.operation_id,
                root.workspace_id,
                root.physical_id,
                root.policy_revision,
                "a.py",
                "create",
                reservation.stage_name,
                reservation.backup_name,
                staged.physical_id,
                _sha(b"new"),
                3,
                "staged",
                recorded,
                recorded,
            ),
        )

    service = _service(journal, root)
    for _ in range(2):
        assert [outcome.status for outcome in await service.recover_pending()] == ["blocked"]
    assert [row.operation_id for row in journal.list_pending()] == [reservation.operation_id]
    assert _files(vault) == {reservation.stage_name}
    assert (vault / reservation.stage_name).read_bytes() == b"new"


async def test_save_code_saves_and_a_changed_file_is_a_conflict_that_leaves_nothing(
    tmp_path: Path,
) -> None:
    folder = _folder(tmp_path)
    root = _root(folder)
    journal = _journal(tmp_path)
    service = _service(journal, root)
    where = (root.workspace_id, root.policy_revision, root.physical_id)
    target = folder / "src" / "app.py"

    created = await service.save_code(*where, "src/app.py", b"one", expected_sha256=None)
    replaced = await service.save_code(*where, "src/app.py", b"two", expected_sha256=_sha(b"one"))
    instructions = await service.save_code(*where, "CLAUDE.md", b"rules", expected_sha256=None)
    assert [created.status, replaced.status, instructions.status] == ["saved"] * 3
    assert target.read_bytes() == b"two"

    target.write_bytes(b"elsewhere")
    outcome = await service.save_code(*where, "src/app.py", b"three", expected_sha256=_sha(b"two"))
    assert (outcome.status, outcome.cleanup_pending) == ("conflict", False)
    assert target.read_bytes() == b"elsewhere"
    assert journal.list_pending() == []
    assert _files(folder) == {"src/app.py", "CLAUDE.md"}


async def test_save_code_refuses_a_vault_root_and_save_refuses_a_code_root(
    tmp_path: Path,
) -> None:
    kinds: tuple[RowKind, ...] = ("vault", "code")
    for kind in kinds:
        folder = tmp_path / kind
        folder.mkdir()
        root = _root(folder, kind)
        journal = PublicationJournal(SessionStore(tmp_path / f"{kind}.sqlite3", lambda: {}))
        service = _service(journal, root)
        where = (root.workspace_id, root.policy_revision, root.physical_id)
        with pytest.raises(MutationError, match="ordinary_workspace_required"):
            if kind == "vault":
                await service.save_code(*where, "a.py", b"x", expected_sha256=None)
            else:
                await service.save(*where, "note.md", b"x", expected_sha256=None)
        assert journal.list_pending() == []
        assert _files(folder) == set()


@pytest.mark.parametrize(
    ("content", "expected_sha256"),
    [
        (bytes(MAX_PUBLICATION_BYTES + 1), None),
        (bytearray(b"x"), None),
        (b"x", "A" * 64),
        (b"x", "abc"),
    ],
    ids=["over_cap", "not_bytes", "upper_hex", "short_hash"],
)
async def test_save_code_refuses_bad_content_or_hash_before_resolving(
    tmp_path: Path, content: bytes, expected_sha256: str | None
) -> None:
    def resolve(*_args: object) -> OrdinaryMutationRoot:
        raise AssertionError("the root resolved")

    service = VaultPublicationService(_journal(tmp_path), resolve, MutationCoordinator())
    with pytest.raises(PublicationError, match="invalid_publication"):
        await service.save_code(
            str(uuid.uuid4()), 7, "1:2", "a.py", content, expected_sha256=expected_sha256
        )


async def test_save_code_asks_the_workspace_gate_first(tmp_path: Path) -> None:
    def resolve(*_args: object) -> OrdinaryMutationRoot:
        raise AssertionError("the root resolved")

    def gate(_workspace_id: str) -> None:
        raise PublicationError("recovery_required")

    service = VaultPublicationService(
        _journal(tmp_path), resolve, MutationCoordinator(), workspace_gate=gate
    )
    with pytest.raises(PublicationError, match="recovery_required"):
        await service.save_code(str(uuid.uuid4()), 7, "1:2", "a.py", b"x", expected_sha256=None)
