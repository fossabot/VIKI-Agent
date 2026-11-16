"""A backup's admitted destination stays outside registrations for the entire copy."""

from __future__ import annotations

import asyncio
import threading
import uuid
from collections.abc import AsyncIterator
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from tests.vaults.test_vault_backup import snapshot
from tests.vaults.test_vault_restore import harness
from viki_agent import osfs
from viki_agent.domain import Workspace
from viki_agent.vaults.vault_backup import MANIFEST_NAME, BackupError, VaultBackupService


async def test_a_destination_registered_during_the_source_lease_wait_gets_no_backup_bytes(
    tmp_path: Path,
) -> None:
    kit = await harness(tmp_path, protected=False)
    destination = tmp_path / "later-registered"
    destination.mkdir()
    before = snapshot(kit.vault)

    @asynccontextmanager
    async def register_then_lease(_workspace: Workspace) -> AsyncIterator[None]:
        workspace, _ = kit.workspaces.register_existing(str(uuid.uuid4()), str(destination))
        assert workspace.policy == "existing_readonly"
        yield None

    service = VaultBackupService(
        kit.workspaces,
        lease=register_then_lease,
        denied_roots=lambda: [Path(w.root.display) for w in kit.workspaces.list(kind=None)],
        app_version="test",
    )

    with pytest.raises(BackupError) as refused:
        await service.run(str(uuid.uuid4()), kit.source_id, str(destination))
    assert refused.value.code == "denied_destination"
    assert list(destination.iterdir()) == []
    assert snapshot(kit.vault) == before


async def test_a_registration_attempt_mid_copy_waits_until_the_backup_is_published(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kit = await harness(tmp_path, protected=False)
    destination = tmp_path / "concurrent-destination"
    destination.mkdir()
    before = snapshot(kit.vault)
    attempted, committed, published = threading.Event(), threading.Event(), threading.Event()
    register = kit.workspaces.register_existing

    def register_destination() -> tuple[Workspace, bool, bool]:
        attempted.set()
        workspace, created = register(str(uuid.uuid4()), str(destination))
        saw_publication = published.is_set()
        committed.set()
        return workspace, created, saw_publication

    @asynccontextmanager
    async def no_lease(_workspace: Workspace) -> AsyncIterator[None]:
        yield None

    service = VaultBackupService(
        kit.workspaces,
        lease=no_lease,
        denied_roots=lambda: [Path(w.root.display) for w in kit.workspaces.list(kind=None)],
        app_version="test",
    )
    copy, rename = service._copy, osfs.rename_no_replace
    registration: Future[tuple[Workspace, bool, bool]] | None = None

    def publish(source: Path, target: Path) -> None:
        assert not committed.is_set()
        rename(source, target)
        published.set()

    with ThreadPoolExecutor(max_workers=1) as workers:

        def copy_while_registering(ref: str, source: Path, target: Path) -> Any:
            nonlocal registration
            if registration is None:
                registration = workers.submit(register_destination)
                assert attempted.wait(0.05)
                assert not committed.wait(0.05)
                assert all(Path(w.root.display) != destination for w in kit.workspaces.list())
            return copy(ref, source, target)

        monkeypatch.setattr(service, "_copy", copy_while_registering)
        monkeypatch.setattr(osfs, "rename_no_replace", publish)
        made, _ = await service.run(str(uuid.uuid4()), kit.source_id, str(destination))
        assert registration is not None
        workspace, created, saw_publication = await asyncio.wrap_future(registration)

    assert (workspace.policy, created, saw_publication) == ("existing_readonly", True, True)
    assert (destination / made.folder / MANIFEST_NAME).is_file()
    assert snapshot(kit.vault) == before
