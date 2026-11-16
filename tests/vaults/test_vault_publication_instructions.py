"""`VaultPublicationService.save_instruction`: the replace-only save of a vault's root `AGENTS.md`
or `CLAUDE.md` through the journalled publication service, and its recovery (ADR-082)."""

from __future__ import annotations

import hashlib
import os
import stat
import uuid
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from viki_agent.storage import SessionStore
from viki_agent.vaults import vault_mutation
from viki_agent.vaults.vault_mutation import (
    MAX_PUBLICATION_BYTES,
    FileFingerprint,
    MutationCoordinator,
    MutationError,
    MutationLease,
    OrdinaryMutationRoot,
    PreparedReplacement,
)
from viki_agent.vaults.vault_publication import (
    PublicationError,
    PublicationJournal,
    PublicationOutcome,
    PublicationRecoveryStatus,
    VaultPublicationService,
    _ref_cap,
)
from viki_agent.workspaces import root_key

OLD = b"\xef\xbb\xbf# synthetic\r\n"
NEW = b"\xef\xbb\xbf# changed\r\n"


def _sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _vault(tmp_path: Path) -> tuple[Path, OrdinaryMutationRoot]:
    vault = (tmp_path / "vault").resolve()
    vault.mkdir()
    info = vault.stat()
    root = OrdinaryMutationRoot(
        str(uuid.uuid4()), vault, root_key(vault), f"{info.st_dev}:{info.st_ino}", 7, None
    )
    return vault, root


def _journal(tmp_path: Path) -> PublicationJournal:
    return PublicationJournal(SessionStore(tmp_path / "app" / "state.sqlite3", lambda: {}))


def _service(
    journal: PublicationJournal,
    root: OrdinaryMutationRoot,
    resolve: Callable[[str, int, str], OrdinaryMutationRoot] | None = None,
    gate: Callable[[str], None] | None = None,
) -> VaultPublicationService:
    def resolve_root(workspace_id: str, revision: int, physical_id: str) -> OrdinaryMutationRoot:
        assert (workspace_id, revision, physical_id) == (
            root.workspace_id,
            root.policy_revision,
            root.physical_id,
        )
        return root

    return VaultPublicationService(
        journal, resolve or resolve_root, MutationCoordinator(), workspace_gate=gate
    )


async def _save(
    service: VaultPublicationService,
    root: OrdinaryMutationRoot,
    name: str = "AGENTS.md",
    content: bytes = NEW,
    expected: str = _sha(OLD),
) -> PublicationOutcome:
    return await service.save_instruction(
        root.workspace_id,
        root.policy_revision,
        root.physical_id,
        name,
        content,
        expected_sha256=expected,
    )


def _clean(vault: Path, journal: PublicationJournal) -> bool:
    return not list(vault.glob(".viki-*")) and journal.list_pending() == []


def _files(vault: Path) -> dict[str, bytes]:
    return {path.name: path.read_bytes() for path in vault.iterdir()}


@pytest.mark.parametrize("name", ["AGENTS.md", "CLAUDE.md"])
async def test_a_root_instruction_file_is_replaced_byte_for_byte(tmp_path: Path, name: str) -> None:
    vault, root = _vault(tmp_path)
    (vault / name).write_bytes(OLD)
    (vault / "note.md").write_bytes(b"kept")
    journal = _journal(tmp_path)

    outcome = await _save(_service(journal, root), root, name)

    assert (outcome.status, outcome.cleanup_pending) == ("saved", False)
    assert (vault / name).read_bytes() == NEW
    assert sorted(path.name for path in vault.iterdir()) == sorted([name, "note.md"])
    assert (vault / "note.md").read_bytes() == b"kept"
    assert _clean(vault, journal)


async def test_identical_bytes_are_no_change(tmp_path: Path) -> None:
    vault, root = _vault(tmp_path)
    (vault / "CLAUDE.md").write_bytes(OLD)
    journal = _journal(tmp_path)

    outcome = await _save(_service(journal, root), root, "CLAUDE.md", OLD)

    assert outcome.status == "no_change"
    assert (vault / "CLAUDE.md").read_bytes() == OLD
    assert _clean(vault, journal)


async def test_a_missing_file_is_never_created(tmp_path: Path) -> None:
    vault, root = _vault(tmp_path)
    journal = _journal(tmp_path)

    with pytest.raises(MutationError) as caught:
        await _save(_service(journal, root), root)

    assert caught.value.code == "missing_target"
    assert list(vault.iterdir()) == []
    assert journal.list_pending() == []


async def test_a_stale_hash_is_a_conflict_and_keeps_the_file(tmp_path: Path) -> None:
    vault, root = _vault(tmp_path)
    (vault / "AGENTS.md").write_bytes(b"edited elsewhere")
    journal = _journal(tmp_path)

    outcome = await _save(_service(journal, root), root)

    assert (outcome.status, outcome.cleanup_pending) == ("conflict", False)
    assert (vault / "AGENTS.md").read_bytes() == b"edited elsewhere"
    assert _clean(vault, journal)


@pytest.mark.parametrize(
    ("name", "content", "expected"),
    [
        ("agents.md", NEW, _sha(OLD)),
        ("Agents.md", NEW, _sha(OLD)),
        ("CLAUDE.MD", NEW, _sha(OLD)),
        ("AGENTS.override.md", NEW, _sha(OLD)),
        ("CLAUDE.local.md", NEW, _sha(OLD)),
        ("GEMINI.md", NEW, _sha(OLD)),
        ("AGENTS.md.", NEW, _sha(OLD)),
        ("sub/AGENTS.md", NEW, _sha(OLD)),
        ("note.md", NEW, _sha(OLD)),
        (b"AGENTS.md", NEW, _sha(OLD)),
        ("AGENTS.md", NEW.decode(), _sha(OLD)),
        ("AGENTS.md", b"x" * (MAX_PUBLICATION_BYTES + 1), _sha(OLD)),
        ("AGENTS.md", NEW, _sha(OLD).upper()),
        ("AGENTS.md", NEW, "0" * 63),
        ("AGENTS.md", NEW, None),
    ],
    ids=[
        "lower",
        "title",
        "upper-suffix",
        "override",
        "local",
        "gemini",
        "trailing-dot",
        "subfolder",
        "note",
        "bytes-name",
        "str-content",
        "over-cap",
        "upper-hash",
        "short-hash",
        "no-hash",
    ],
)
async def test_a_bad_name_content_or_hash_is_refused_before_any_row(
    tmp_path: Path, name: object, content: object, expected: object
) -> None:
    vault, root = _vault(tmp_path)
    for leaf in ("AGENTS.md", "note.md", "GEMINI.md"):
        (vault / leaf).write_bytes(OLD)
    journal = _journal(tmp_path)

    def resolve(*_args: object) -> OrdinaryMutationRoot:
        raise AssertionError("refused before the resolver")

    with pytest.raises(PublicationError) as caught:
        await _save(
            _service(journal, root, resolve),
            root,
            name,  # type: ignore[arg-type]
            content,  # type: ignore[arg-type]
            expected,  # type: ignore[arg-type]
        )

    assert caught.value.code == "invalid_publication"
    assert all((vault / leaf).read_bytes() == OLD for leaf in ("AGENTS.md", "note.md", "GEMINI.md"))
    assert _clean(vault, journal)


async def test_the_workspace_gate_refuses_first(tmp_path: Path) -> None:
    vault, root = _vault(tmp_path)
    (vault / "AGENTS.md").write_bytes(OLD)
    journal = _journal(tmp_path)

    def gate(_workspace_id: str) -> None:
        raise PublicationError("vault_recovery_required")

    with pytest.raises(PublicationError) as caught:
        await _save(_service(journal, root, gate=gate), root)

    assert caught.value.code == "vault_recovery_required"
    assert (vault / "AGENTS.md").read_bytes() == OLD
    assert _clean(vault, journal)


async def test_a_code_folder_is_refused(tmp_path: Path) -> None:
    vault, root = _vault(tmp_path)
    (vault / "AGENTS.md").write_bytes(OLD)
    journal = _journal(tmp_path)
    code = replace(root, kind="code")

    with pytest.raises(MutationError) as caught:
        await _save(_service(journal, code), code)

    assert caught.value.code == "ordinary_workspace_required"
    assert (vault / "AGENTS.md").read_bytes() == OLD
    assert _clean(vault, journal)


async def test_a_vault_the_resolver_refuses_keeps_no_row(tmp_path: Path) -> None:
    vault, root = _vault(tmp_path)
    (vault / "AGENTS.md").write_bytes(OLD)
    journal = _journal(tmp_path)

    def resolve(*_args: object) -> OrdinaryMutationRoot:
        raise MutationError("ordinary_workspace_required")

    with pytest.raises(MutationError) as caught:
        await _save(_service(journal, root, resolve), root)

    assert caught.value.code == "ordinary_workspace_required"
    assert (vault / "AGENTS.md").read_bytes() == OLD
    assert _clean(vault, journal)


@pytest.mark.parametrize("name", ["AGENTS.md", "CLAUDE.md"])
async def test_bytes_a_protected_workspace_records_for_the_name_are_refused_before_any_row(
    tmp_path: Path, name: str
) -> None:
    """Saved, they would make the resolver refuse every later write to this vault, the save that
    reverts them included."""
    vault, root = _vault(tmp_path)
    (vault / name).write_bytes(OLD)
    journal = _journal(tmp_path)
    guarded = replace(root, protected_instructions=frozenset({(name, _sha(NEW))}))

    with pytest.raises(PublicationError) as caught:
        await _save(_service(journal, guarded), guarded, name)

    assert caught.value.code == "protected_instructions"
    assert _files(vault) == {name: OLD}
    assert _clean(vault, journal)


async def test_a_protected_workspaces_bytes_under_the_other_name_still_save(
    tmp_path: Path,
) -> None:
    """The resolver compares a file's hash only with a protected file of the same name."""
    vault, root = _vault(tmp_path)
    (vault / "AGENTS.md").write_bytes(OLD)
    journal = _journal(tmp_path)
    guarded = replace(root, protected_instructions=frozenset({("CLAUDE.md", _sha(NEW))}))

    outcome = await _save(_service(journal, guarded), guarded)

    assert outcome.status == "saved"
    assert _files(vault) == {"AGENTS.md": NEW}
    assert _clean(vault, journal)


async def test_a_read_only_file_is_refused(tmp_path: Path) -> None:
    vault, root = _vault(tmp_path)
    target = vault / "AGENTS.md"
    target.write_bytes(OLD)
    os.chmod(target, stat.S_IREAD)
    try:
        journal = _journal(tmp_path)
        with pytest.raises(MutationError) as caught:
            await _save(_service(journal, root), root)
        assert caught.value.code == "read_only_target"
        assert target.read_bytes() == OLD
        assert _clean(vault, journal)
    finally:
        os.chmod(target, stat.S_IREAD | stat.S_IWRITE)


async def test_an_unsupported_filesystem_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, root = _vault(tmp_path)
    (vault / "AGENTS.md").write_bytes(OLD)
    journal = _journal(tmp_path)
    monkeypatch.setattr(vault_mutation, "_supported_volume", lambda _path: False)

    with pytest.raises(MutationError) as caught:
        await _save(_service(journal, root), root)

    assert caught.value.code == "unsupported_filesystem"
    assert [path.name for path in vault.iterdir()] == ["AGENTS.md"]
    assert (vault / "AGENTS.md").read_bytes() == OLD
    assert journal.list_pending() == []


async def test_a_differently_spelled_file_on_disk_is_refused(tmp_path: Path) -> None:
    vault, root = _vault(tmp_path)
    (vault / "Agents.md").write_bytes(OLD)
    if not (vault / "AGENTS.md").exists():
        pytest.skip("needs a case-insensitive volume")
    journal = _journal(tmp_path)

    with pytest.raises(MutationError) as caught:
        await _save(_service(journal, root), root)

    assert caught.value.code == "excluded_path"
    assert [path.name for path in vault.iterdir()] == ["Agents.md"]
    assert (vault / "Agents.md").read_bytes() == OLD
    assert _clean(vault, journal)


async def test_an_interrupted_save_is_finished_by_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After the backup rename the name is absent; the next launch's recovery publishes the
    new bytes under the instructions grammar, which the vault grammar alone refuses."""
    vault, root = _vault(tmp_path)
    (vault / "AGENTS.md").write_bytes(OLD)
    journal = _journal(tmp_path)

    def interrupted(_prepared: PreparedReplacement) -> None:
        raise OSError("synthetic")

    monkeypatch.setattr(PreparedReplacement, "publish_stage_no_replace", interrupted)
    with pytest.raises(PublicationError) as caught:
        await _save(_service(journal, root), root)
    [row] = journal.list_pending()
    assert (caught.value.code, caught.value.operation_id) == ("recovery_required", row.operation_id)
    assert row.state == "backed_up"
    assert not (vault / "AGENTS.md").exists()
    assert (vault / row.stage_name).read_bytes() == NEW
    assert (vault / row.backup_name).read_bytes() == OLD

    monkeypatch.undo()
    assert await _service(journal, root).recover_pending() == [
        PublicationOutcome(row.operation_id, "recovered")
    ]
    assert [path.name for path in vault.iterdir()] == ["AGENTS.md"]
    assert (vault / "AGENTS.md").read_bytes() == NEW
    assert journal.list_pending() == []


def _keep_backup_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """The save's last step, deleting its backup, fails once (another program has the backup
    open): the save stands, and its row stays `published` beside the backup."""
    real = vault_mutation.VaultPublication.discard_backup_after_publish
    calls = 0

    def backup_kept(
        self: vault_mutation.VaultPublication,
        lease: MutationLease,
        root: OrdinaryMutationRoot,
        ref: str,
        backup_name: str,
        published: FileFingerprint,
        backup: FileFingerprint,
    ) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("synthetic")
        real(self, lease, root, ref, backup_name, published, backup)

    monkeypatch.setattr(
        vault_mutation.VaultPublication, "discard_backup_after_publish", backup_kept
    )


async def test_a_published_row_whose_backup_stayed_is_resolvable_and_recovery_clears_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, root = _vault(tmp_path)
    (vault / "CLAUDE.md").write_bytes(OLD)
    journal = _journal(tmp_path)
    _keep_backup_once(monkeypatch)

    service = _service(journal, root)
    outcome = await _save(service, root, "CLAUDE.md")
    [row] = journal.list_pending()
    assert (outcome.status, outcome.cleanup_pending) == ("saved", True)
    assert service.recovery_status() == [
        PublicationRecoveryStatus(
            row.operation_id,
            root.workspace_id,
            "CLAUDE.md",
            "published",
            "recovery_required",
            ("forget", "drop_previous"),
        )
    ]

    assert await _service(journal, root).recover_pending() == [
        PublicationOutcome(row.operation_id, "recovered")
    ]
    assert [path.name for path in vault.iterdir()] == ["CLAUDE.md"]
    assert (vault / "CLAUDE.md").read_bytes() == NEW
    assert journal.list_pending() == []


@pytest.mark.parametrize("name", ["AGENTS.md", "CLAUDE.md"])
async def test_drop_previous_deletes_the_kept_backup_and_frees_the_file_for_its_next_save(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    """Without it the file could not be saved again until the next launch, and discarding the
    record would leave the previous text in the vault's root as a `.viki-backup-*` file."""
    vault, root = _vault(tmp_path)
    (vault / name).write_bytes(OLD)
    journal = _journal(tmp_path)
    _keep_backup_once(monkeypatch)
    service = _service(journal, root)
    await _save(service, root, name)
    [row] = journal.list_pending()
    with pytest.raises(PublicationError) as busy:
        await _save(service, root, name, b"# newer\n", _sha(NEW))
    assert busy.value.code == "publication_busy"

    resolved = await service.resolve(row.operation_id, "drop_previous", wait_s=0.05)

    assert (resolved.choice, resolved.backup_retained) == ("drop_previous", False)
    assert _files(vault) == {name: NEW}
    assert journal.list_pending() == []
    outcome = await _save(service, root, name, b"# newer\n", _sha(NEW))
    assert (outcome.status, outcome.cleanup_pending) == ("saved", False)
    assert _files(vault) == {name: b"# newer\n"}
    assert _clean(vault, journal)


async def test_the_note_save_still_refuses_an_instruction_file(tmp_path: Path) -> None:
    vault, root = _vault(tmp_path)
    (vault / "AGENTS.md").write_bytes(OLD)
    journal = _journal(tmp_path)

    with pytest.raises(MutationError) as caught:
        await _service(journal, root).save(
            root.workspace_id,
            root.policy_revision,
            root.physical_id,
            "AGENTS.md",
            NEW,
            expected_sha256=_sha(OLD),
        )

    assert caught.value.code == "excluded_path"
    assert (vault / "AGENTS.md").read_bytes() == OLD
    assert _clean(vault, journal)


def test_the_instructions_grammar_admits_only_a_replace_of_a_root_instruction_file() -> None:
    assert _ref_cap("AGENTS.md", "replace", "instructions") == MAX_PUBLICATION_BYTES
    assert _ref_cap("CLAUDE.md", "replace", "instructions") == MAX_PUBLICATION_BYTES
    for ref, kind in [
        ("AGENTS.md", "create"),
        ("note.md", "replace"),
        ("Agents.md", "replace"),
        ("sub/CLAUDE.md", "replace"),
    ]:
        assert _ref_cap(ref, kind, "instructions") is None, ref  # type: ignore[arg-type]
    assert _ref_cap("AGENTS.md", "replace") is None
