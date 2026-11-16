"""A restore excludes parent registration until its copied folder is registered."""

from __future__ import annotations

import asyncio
import threading
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

import pytest

from tests.vaults.test_vault_backup import snapshot
from tests.vaults.test_vault_restore import NOTES, harness, tree
from viki_agent.domain import Workspace
from viki_agent.vaults import vault_restore
from viki_agent.vaults.vault_restore import BackupRestoreError
from viki_agent.workspaces import WorkspaceError

HANG_GUARD_S = 5  # Guard on events normally raised in milliseconds, not a work budget.


async def test_parent_registration_winning_admission_gets_no_restore_bytes(tmp_path: Path) -> None:
    kit = await harness(tmp_path, protected=False)
    started, finished = threading.Event(), threading.Event()

    def apply():
        started.set()
        try:
            return kit.service._apply(
                str(uuid.uuid4()), str(kit.backup), str(kit.restores), "Restored"
            )
        finally:
            finished.set()

    with ThreadPoolExecutor(max_workers=1) as workers:
        with kit.workspaces.registration_lock:
            pending = workers.submit(apply)
            assert started.wait(0.05)
            assert not finished.wait(0.05)
            workspace, created = kit.workspaces.register_existing(
                str(uuid.uuid4()), str(kit.restores)
            )
            assert created and workspace.policy == "existing_readonly"
        with pytest.raises(BackupRestoreError) as refused:
            pending.result(timeout=HANG_GUARD_S)
    assert refused.value.code == "overlaps_registration"
    assert list(kit.restores.iterdir()) == []
    assert len(kit.workspaces.list()) == 2


async def test_parent_registration_mid_copy_waits_until_restore_registration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kit = await harness(tmp_path, protected=False)
    before = snapshot(kit.vault), snapshot(kit.backup)
    attempted, finished, registered = threading.Event(), threading.Event(), threading.Event()
    registration: Future[tuple[str, bool]] | None = None
    write, register = vault_restore._write_verified, kit.service._register

    def register_parent() -> tuple[str, bool]:
        attempted.set()
        try:
            kit.workspaces.register_existing(str(uuid.uuid4()), str(kit.restores))
            code = "registered"
        except WorkspaceError as error:
            code = error.code
        saw_registration = registered.is_set()
        finished.set()
        return code, saw_registration

    def register_restore(command_id: str, target: Path, protect: bool) -> Workspace:
        workspace = register(command_id, target, protect)
        registered.set()
        return workspace

    with ThreadPoolExecutor(max_workers=1) as workers:

        def copy_while_registering(folder, entry, data):
            nonlocal registration
            if registration is None:
                registration = workers.submit(register_parent)
                assert attempted.wait(0.05)
                assert not finished.wait(0.05)
                assert all(
                    Path(workspace.root.display) != kit.restores
                    for workspace in kit.workspaces.list()
                )
            return write(folder, entry, data)

        monkeypatch.setattr(vault_restore, "_write_verified", copy_while_registering)
        monkeypatch.setattr(kit.service, "_register", register_restore)
        result, replayed = await kit.service.apply(
            str(uuid.uuid4()), str(kit.backup), str(kit.restores), "Restored"
        )
        assert registration is not None
        assert await asyncio.wrap_future(registration) == ("overlaps_registration", True)

    target = kit.restores / "Restored"
    assert not replayed and result.verified
    assert result.workspace.policy == "existing_readonly"
    assert tree(target) == {ref: text.encode("utf-8") for ref, text in NOTES.items()}
    assert (snapshot(kit.vault), snapshot(kit.backup)) == before
    assert all(Path(workspace.root.display) != kit.restores for workspace in kit.workspaces.list())
    assert len(kit.workspaces.list()) == 2
