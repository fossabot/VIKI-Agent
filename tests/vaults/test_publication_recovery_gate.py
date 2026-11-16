from __future__ import annotations

import asyncio
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Literal
from unittest.mock import Mock

import pytest

from tests.vaults.test_vault_lifecycle import _env, _sha, _tree
from viki_agent.storage import StateError
from viki_agent.vaults.vault_mutation import VaultPublication
from viki_agent.vaults.vault_operations import OperationStep
from viki_agent.vaults.vault_publication import PublicationError, VaultPublicationService


async def test_old_bot_memory_publication_row_completes_recovery(tmp_path: Path) -> None:
    env = _env(tmp_path)
    folder = env.vault / "bot-memory"
    folder.mkdir()
    native = VaultPublication()
    reservation = native.reserve()
    content = b"memory"
    ref = "bot-memory/note.md"
    row = env.publications.reserve(
        env.root, ref, reservation, "create", _sha(content), len(content), None
    )
    async with env.coordinator.lease(env.root, (ref,)) as lease:
        stage = native.stage(lease, env.root, ref, reservation, content)
    env.publications.record_stage(row.operation_id, stage.physical_id)

    service = VaultPublicationService(env.publications, lambda *_args: env.root, env.coordinator)
    [outcome] = await service.recover_pending()

    assert (outcome.operation_id, outcome.status) == (row.operation_id, "recovered")
    assert (folder / "note.md").read_bytes() == content
    assert env.publications.list_pending() == []
    assert not list(folder.glob(".viki-*"))


@pytest.mark.parametrize("applied", [False, True])
async def test_old_bot_memory_operation_row_reconciles(tmp_path: Path, applied: bool) -> None:
    env = _env(tmp_path)
    folder = env.vault / "bot-memory"
    folder.mkdir()
    source = folder / "note.md"
    target = folder / "renamed.md"
    source.write_bytes(b"memory")
    ref, to = "bot-memory/note.md", "bot-memory/renamed.md"
    operation_id = str(uuid.uuid4())
    digest = _sha(b"memory")
    env.journal.plan(
        operation_id,
        env.root,
        "rename",
        [OperationStep(0, "move", ref, to, digest, digest, None)],
        digest,
    )
    env.journal.transition(operation_id, "planned", "applying")
    if applied:
        source.rename(target)

    env.launch()
    [outcome] = await env.service.reconcile()

    assert (outcome.operation_id, outcome.status) == (
        operation_id,
        "done" if applied else "failed",
    )
    assert _tree(env.vault) == [
        ("bot-memory", None),
        (to if applied else ref, b"memory"),
    ]
    assert env.rows() == []


@pytest.mark.parametrize(
    ("ref", "kind", "create_folders", "replacing"),
    [
        ("note.md", "vault", False, False),
        ("note.md", "vault", False, True),
        ("nested/note.md", "vault", True, False),
        ("attachments/image.png", "vault", True, False),
        ("script.py", "code", False, False),
        ("script.py", "code", False, True),
    ],
)
@pytest.mark.parametrize("recovery_required", [False, True])
async def test_a_queued_save_rechecks_recovery_before_reserving_or_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ref: str,
    kind: Literal["vault", "code"],
    create_folders: bool,
    replacing: bool,
    recovery_required: bool,
) -> None:
    env = _env(tmp_path)
    root = replace(env.root, kind=kind)
    (env.vault / "keep.md").write_bytes(b"unchanged")
    if replacing:
        (env.vault / ref).write_bytes(b"old")
    operation_id = str(uuid.uuid4())
    digest = _sha(b"old")
    env.journal.plan(
        operation_id,
        root,
        "rename",
        [OperationStep(0, "move", "old.md", "moved.md", digest, digest, None)],
        digest,
    )
    env.journal.transition(operation_id, "planned", "applying")
    calls: list[str] = []

    def gate(workspace_id: str) -> None:
        calls.append(workspace_id)
        env.service.require_writable(workspace_id)

    service = VaultPublicationService(
        env.publications, lambda *_args: root, env.coordinator, workspace_gate=gate
    )
    reserve = Mock(wraps=env.publications.reserve)
    monkeypatch.setattr(env.publications, "reserve", reserve)
    queued = asyncio.Event()
    real_wait = env.coordinator._condition.wait

    async def observed_wait() -> bool:
        queued.set()
        return await real_wait()

    monkeypatch.setattr(env.coordinator._condition, "wait", observed_wait)
    before = _tree(env.vault)
    async with env.coordinator.lease(root, (ref,)):
        args = (root.workspace_id, root.policy_revision, root.physical_id, ref, b"new")
        expected = digest if replacing else None
        if kind == "code":
            saving = service.save_code(*args, expected_sha256=expected)
        else:
            saving = service.save(*args, expected_sha256=expected, create_folders=create_folders)
        pending = asyncio.create_task(saving)
        await queued.wait()
        assert not pending.done()
        assert calls == [root.workspace_id]
        if recovery_required:
            env.journal.transition(operation_id, "applying", "recovery_required")
            with pytest.raises(PublicationError, match="^vault_recovery_required$"):
                env.service.require_writable(root.workspace_id)
    if recovery_required:
        with pytest.raises(PublicationError) as caught:
            await pending
        assert (caught.value.code, caught.value.operation_id) == ("vault_recovery_required", None)
        assert reserve.call_count == 0
        assert _tree(env.vault) == before
    else:
        assert (await pending).status == "saved"
        assert reserve.call_count == 1
        assert (env.vault / ref).read_bytes() == b"new"
        assert (env.vault / "keep.md").read_bytes() == b"unchanged"
        assert not list(env.vault.rglob(".viki-*"))
    assert calls == [root.workspace_id, root.workspace_id]
    assert env.publications.list_pending() == []


async def test_an_unreadable_gate_under_the_lease_keeps_its_typed_refusal(tmp_path: Path) -> None:
    env = _env(tmp_path)
    root = env.root
    calls = 0

    def gate(workspace_id: str) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise StateError("busy")
        env.service.require_writable(workspace_id)

    service = VaultPublicationService(
        env.publications, lambda *_args: root, env.coordinator, workspace_gate=gate
    )
    with pytest.raises(PublicationError) as caught:
        await service.save(
            root.workspace_id,
            root.policy_revision,
            root.physical_id,
            "note.md",
            b"new",
            expected_sha256=None,
        )
    assert (caught.value.code, caught.value.operation_id) == ("journal_unavailable", None)
    assert env.publications.list_pending() == []
    assert _tree(env.vault) == []
