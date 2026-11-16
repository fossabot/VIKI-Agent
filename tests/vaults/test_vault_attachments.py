"""Keep in vault copies one verified chat attachment to `attachments/<leaf>`."""

from __future__ import annotations

import asyncio
import hashlib
import tracemalloc
import uuid
from collections.abc import AsyncIterator, Iterable
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from tests.media.test_assets import PNG, FakeStore
from viki_agent import protection
from viki_agent.media import assets
from viki_agent.media.assets import AssetService, DecodedImage
from viki_agent.storage import SessionStore
from viki_agent.turn_input import MAX_ATTACHMENT_BYTES
from viki_agent.vaults.vault_attachments import AttachmentError, VaultAttachmentService
from viki_agent.vaults.vault_mutation import (
    MutationCoordinator,
    MutationLease,
    OrdinaryMutationRoot,
    PublicationReservation,
    PublishedPublication,
    StagedPublication,
    VaultPublication,
)
from viki_agent.vaults.vault_publication import PublicationJournal, VaultPublicationService
from viki_agent.workspaces import WorkspaceError, WorkspaceService, root_key

SCOPE = "b" * 64
JPEG = b"\xff\xd8\xff\xe0" + bytes(60)
LEASE_WAIT_S = 0.02
# Only for the one wait that must end by the lease deadline (AGENTS.md's HANG_GUARD_S exception).
HANG_GUARD_S = 5


class _MeasuredNative(VaultPublication):
    """The real native writer, measuring each stage file on disk just before it is published."""

    def __init__(self) -> None:
        self.stages = 0
        self.staged_sizes: list[int] = []

    def stage(
        self,
        lease: MutationLease,
        root: OrdinaryMutationRoot,
        ref: str,
        reservation: PublicationReservation,
        content: bytes,
    ) -> StagedPublication:
        self.stages += 1
        return super().stage(lease, root, ref, reservation, content)

    def publish_create(
        self, lease: MutationLease, staged: StagedPublication
    ) -> PublishedPublication:
        stage_file = (staged.root.path / staged.ref).parent / staged.stage_name
        self.staged_sizes.append(stage_file.stat().st_size)
        return super().publish_create(lease, staged)


class _Reader:
    """An asset reader for what the real service cannot hold: more than the cap, or bytes that
    do not match the digest it answers. It records whether anything asked it."""

    def __init__(self, data: bytes, sha256: str, media_type: str = "image/png") -> None:
        self.answer = (data, sha256, media_type)
        self.reads = 0

    async def read_for_vault(self, asset_id: str) -> tuple[bytes, str, str]:
        self.reads += 1
        return self.answer


@dataclass
class Rig:
    vault: Path
    root: OrdinaryMutationRoot
    assets: AssetService
    journal: PublicationJournal
    coordinator: MutationCoordinator
    native: _MeasuredNative
    policies: dict[str, str] = field(default_factory=dict)

    def service(self, reader: Any = None) -> VaultAttachmentService:
        def get(workspace_id: str) -> SimpleNamespace:
            if workspace_id not in self.policies:
                raise WorkspaceError("unknown_workspace")
            return SimpleNamespace(policy=self.policies[workspace_id])

        def resolve(workspace_id: str, revision: int, physical_id: str) -> OrdinaryMutationRoot:
            assert (workspace_id, revision, physical_id) == (
                self.root.workspace_id,
                self.root.policy_revision,
                self.root.physical_id,
            )
            return self.root

        publications = VaultPublicationService(self.journal, resolve, self.coordinator, self.native)
        return VaultAttachmentService(
            cast(WorkspaceService, SimpleNamespace(get=get)),
            reader or self.assets,
            publications,
            resolve,
            self.coordinator,
            lease_wait_s=LEASE_WAIT_S,
            native=self.native,
        )

    async def keep(self, asset_id: str, name: str = "photo.png", reader: Any = None) -> Any:
        return await self.service(reader).keep(
            self.root.workspace_id,
            self.root.policy_revision,
            self.root.physical_id,
            asset_id,
            name,
        )

    async def upload(self, data: bytes) -> str:
        async def body() -> AsyncIterator[bytes]:
            for start in range(0, len(data), 1 << 20):
                yield data[start : start + (1 << 20)]

        uploaded = await self.assets.upload(body(), name="upload.png", declared_bytes=len(data))
        return uploaded.asset_id

    def tree(self) -> list[str]:
        return sorted(path.relative_to(self.vault).as_posix() for path in self.vault.rglob("*"))


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Rig:
    monkeypatch.setattr(protection, "require_private_directory", lambda path: None)
    monkeypatch.setattr(protection, "require_private_file", lambda path: None)
    monkeypatch.setattr(
        protection, "create_private_directory", lambda path: (path.mkdir(), True)[1]
    )
    monkeypatch.setattr(assets, "require_private_store", lambda store: None)

    async def decoder(path: Path) -> DecodedImage:
        sniffed = "image/jpeg" if path.read_bytes()[:2] == b"\xff\xd8" else "image/png"
        return DecodedImage(sniffed, 1, 1)

    asset_service = AssetService(
        tmp_path / "assets",
        cast(SessionStore, FakeStore(tmp_path / "assets.sqlite3")),
        SCOPE,
        decoder=decoder,
    )
    vault = tmp_path / "vault"
    vault.mkdir()
    resolved = vault.resolve()
    info = resolved.stat()
    root = OrdinaryMutationRoot(
        str(uuid.uuid4()),
        resolved,
        root_key(resolved),
        f"{info.st_dev}:{info.st_ino}",
        3,
        None,
    )
    journal = PublicationJournal(SessionStore(tmp_path / "state.sqlite3", lambda: {}))
    built = Rig(vault, root, asset_service, journal, MutationCoordinator(), _MeasuredNative())
    built.policies[root.workspace_id] = "ordinary"
    return built


async def _refused(rig: Rig, code: str, asset_id: str, **kwargs: Any) -> None:
    before = rig.tree()
    with pytest.raises(AttachmentError) as refused:
        await rig.keep(asset_id, **kwargs)
    assert refused.value.code == code
    assert rig.tree() == before
    assert rig.journal.list_pending() == []


async def test_keep_in_vault_copies_only_the_verified_asset(rig: Rig) -> None:
    asset_id = await rig.upload(PNG)

    kept = await rig.keep(asset_id)

    assert (kept.ref, kept.reused, kept.byte_size) == ("attachments/photo.png", False, len(PNG))
    written = (rig.vault / "attachments" / "photo.png").read_bytes()
    assert written == PNG
    assert hashlib.sha256(written).hexdigest() == hashlib.sha256(PNG).hexdigest()
    # The folder was made for it; no stage or journal row is left behind.
    assert rig.tree() == ["attachments", "attachments/photo.png"]
    assert rig.journal.list_pending() == []

    await _refused(rig, "asset_unavailable", str(uuid.uuid4()), name="unknown.png")
    tampered = await rig.upload(PNG[:-1] + b"\x01")
    (rig.assets.root / "blobs" / SCOPE / tampered).write_bytes(PNG[:-1] + b"\x02")
    await _refused(rig, "asset_unavailable", tampered, name="tampered.png")


async def test_keep_re_hashes_the_bytes_itself_and_refuses_a_mismatch(rig: Rig) -> None:
    lying = _Reader(PNG, hashlib.sha256(PNG + b"other").hexdigest())

    await _refused(rig, "asset_unavailable", str(uuid.uuid4()), reader=lying)

    assert lying.reads == 1
    assert rig.native.stages == 0


async def test_the_attachment_cap_refuses_before_any_vault_write(rig: Rig) -> None:
    at_cap = bytes(MAX_ATTACHMENT_BYTES)
    asset_id = await rig.upload(at_cap)

    tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        kept = await rig.keep(asset_id)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    # One copy of the asset is held, never two: the read, the hash and the stage share it.
    assert MAX_ATTACHMENT_BYTES <= peak < MAX_ATTACHMENT_BYTES * 3 // 2
    assert rig.native.staged_sizes == [MAX_ATTACHMENT_BYTES]
    assert kept.byte_size == MAX_ATTACHMENT_BYTES
    assert (rig.vault / kept.ref).stat().st_size == MAX_ATTACHMENT_BYTES
    del at_cap

    over = bytes(MAX_ATTACHMENT_BYTES + 1)
    reader = _Reader(over, hashlib.sha256(over).hexdigest())
    await _refused(rig, "too_large", str(uuid.uuid4()), name="over.png", reader=reader)
    assert reader.reads == 1
    assert rig.native.stages == 1  # only the 20 MiB keep staged
    assert not (rig.vault / "attachments" / "over.png").exists()


async def test_an_identical_existing_file_is_reused_and_not_written_again(rig: Rig) -> None:
    first = await rig.keep(await rig.upload(PNG))
    again = await rig.keep(await rig.upload(PNG))

    assert (again.ref, again.reused, again.byte_size) == (first.ref, True, len(PNG))
    assert rig.native.stages == 1
    assert rig.tree() == ["attachments", "attachments/photo.png"]


async def test_a_different_existing_file_gets_a_hash_suffixed_name(rig: Rig) -> None:
    (rig.vault / "attachments").mkdir()
    other = rig.vault / "attachments" / "photo.png"
    other.write_bytes(b"someone else's picture")
    asset_id = await rig.upload(PNG)
    suffixed = f"attachments/photo-{hashlib.sha256(PNG).hexdigest()[:12]}.png"

    kept = await rig.keep(asset_id)

    assert (kept.ref, kept.reused) == (suffixed, False)
    assert (rig.vault / suffixed).read_bytes() == PNG
    assert other.read_bytes() == b"someone else's picture"
    assert (await rig.keep(asset_id)).reused is True
    assert rig.native.stages == 1

    (rig.vault / suffixed).write_bytes(b"also someone else's")
    await _refused(rig, "conflict", asset_id)


async def test_the_suffix_comes_from_the_sniffed_type_and_an_empty_name_is_accepted(
    rig: Rig,
) -> None:
    jpeg = await rig.upload(JPEG)

    assert (await rig.keep(jpeg, name="holiday.PNG")).ref == "attachments/holiday.jpg"
    assert (await rig.keep(jpeg, name="report.final")).ref == "attachments/report.final.jpg"
    assert (await rig.keep(jpeg, name="  ")).ref == "attachments/attachment.jpg"
    assert (await rig.keep(await rig.upload(PNG), name="")).ref == "attachments/attachment.png"


@pytest.mark.parametrize(
    "name", ["a/b", "a\\b", "con", "con.png", ".hidden", "x:y", "what?", "tab\there", "x" * 260]
)
async def test_a_name_no_attachment_ref_admits_refuses_before_any_write(
    rig: Rig, name: str
) -> None:
    await _refused(rig, "invalid_name", await rig.upload(PNG), name=name)
    assert rig.native.stages == 0


@pytest.mark.parametrize(
    ("policy", "code"),
    [("protected", "vault_protected"), ("existing_readonly", "vault_read_only")],
)
async def test_a_protected_or_read_only_vault_refuses_before_any_asset_read(
    rig: Rig, policy: str, code: str
) -> None:
    rig.policies[rig.root.workspace_id] = policy
    reader = _Reader(PNG, hashlib.sha256(PNG).hexdigest())

    await _refused(rig, code, str(uuid.uuid4()), reader=reader)

    assert reader.reads == 0
    assert rig.native.stages == 0


async def test_an_unknown_workspace_refuses_before_any_asset_read(rig: Rig) -> None:
    del rig.policies[rig.root.workspace_id]
    reader = _Reader(PNG, hashlib.sha256(PNG).hexdigest())

    await _refused(rig, "unknown_workspace", str(uuid.uuid4()), reader=reader)

    assert reader.reads == 0


async def test_a_held_lease_refuses_vault_locked_once_the_deadline_passes(rig: Rig) -> None:
    asset_id = await rig.upload(PNG)
    async with rig.coordinator.lease(rig.root, ("attachments/photo.png",)):
        async with asyncio.timeout(HANG_GUARD_S):
            await _refused(rig, "vault_locked", asset_id)
    assert rig.native.stages == 0


class _HeldAtSave(MutationCoordinator):
    """Takes a conflicting lease just before the second one is asked for, so the reuse probe
    passes and the save finds its ref held, as it does behind a long backup."""

    def __init__(self) -> None:
        super().__init__()
        self.asked = 0

    @asynccontextmanager
    async def lease(
        self,
        root: OrdinaryMutationRoot,
        refs: Iterable[str],
        *,
        include_git_common: bool = False,
        wait_s: float | None = None,
    ) -> AsyncIterator[MutationLease]:
        self.asked += 1
        refs = tuple(refs)
        async with AsyncExitStack() as stack:
            if self.asked == 2:
                await stack.enter_async_context(MutationCoordinator.lease(self, root, refs))
            yield await stack.enter_async_context(
                MutationCoordinator.lease(
                    self, root, refs, include_git_common=include_git_common, wait_s=wait_s
                )
            )


async def test_a_lease_held_at_the_save_refuses_vault_locked_once_the_deadline_passes(
    rig: Rig,
) -> None:
    asset_id = await rig.upload(PNG)
    held = _HeldAtSave()
    rig.coordinator = held

    async with asyncio.timeout(HANG_GUARD_S):
        await _refused(rig, "vault_locked", asset_id)

    assert held.asked == 2  # the probe took its lease, then the save asked for its own
    assert rig.native.stages == 0
