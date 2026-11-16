"""Asset service tests use only synthetic bytes under the test directory."""

from __future__ import annotations

import asyncio
import base64
import io
import logging
import os
import sys
import threading
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from viki_agent import protection, winproc
from viki_agent.media import assets, media_worker
from viki_agent.media.assets import AssetError, AssetService, DecodedImage
from viki_agent.media.avatars import AVATAR_SOURCE_BYTES
from viki_agent.storage import AssetRow, SessionStore, StateError
from viki_agent.turn_input import MAX_ATTACHMENT_BYTES, Attachment

SCOPE = "a" * 64
# Only for the one wait that outlasts a spawn (AGENTS.md's HANG_GUARD_S exception); every other
# wait in this file is a real coroutine result, not a timed sleep.
HANG_GUARD_S = 5
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class FakeStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.rows: dict[str, AssetRow] = {}
        self.refs: set[str] = set()
        self.ref_rows: dict[str, list[tuple[str, str]]] = {}

    def put_asset(self, row: AssetRow) -> None:
        if row["asset_id"] in self.rows:
            raise StateError("invalid_input")
        self.rows[row["asset_id"]] = {
            **row,
            "local_state": row.get("local_state", "stored"),
            "local_removed_at": row.get("local_removed_at"),
        }

    def asset_metadata(self, scope: str, asset_ids: tuple[str, ...]) -> list[AssetRow]:
        try:
            rows = [self.rows[asset_id] for asset_id in asset_ids]
        except KeyError:
            raise StateError("not_found") from None
        if any(row["scope"] != scope for row in rows):
            raise StateError("not_found")
        return [dict(row) for row in rows]

    def asset_usage(self, scope: str) -> tuple[int, int]:
        rows = [
            row
            for row in self.rows.values()
            if row["scope"] == scope and row["local_state"] == "stored"
        ]
        return len(rows), sum(row["byte_size"] for row in rows)

    def delete_asset_if_unreferenced(
        self, scope: str, asset_id: str, *, created_before: str | None = None
    ) -> bool:
        row = self.rows.get(asset_id)
        if (
            row is None
            or row["scope"] != scope
            or asset_id in self.refs
            or self.ref_rows.get(asset_id)
            or (created_before is not None and row["created_at"] >= created_before)
        ):
            return False
        del self.rows[asset_id]
        return True

    def list_unreferenced_assets(self, scope: str, before: str, limit: int) -> list[AssetRow]:
        return [
            dict(row)
            for row in sorted(self.rows.values(), key=lambda value: value["created_at"])
            if row["scope"] == scope
            and row["created_at"] < before
            and row["asset_id"] not in self.refs
            and not self.ref_rows.get(row["asset_id"])
        ][:limit]

    def list_stored_command_assets(self, scope: str, before: str, limit: int) -> list[AssetRow]:
        result: list[AssetRow] = []
        for row in sorted(self.rows.values(), key=lambda value: value["created_at"]):
            refs = self.ref_rows.get(row["asset_id"], [])
            if (
                row["scope"] == scope
                and row["local_state"] == "stored"
                and row["created_at"] < before
                and refs
                and all(kind == "command" and created_at < before for kind, created_at in refs)
                and row["asset_id"] not in self.refs
            ):
                result.append(dict(row))
        return result[:limit]

    def expire_asset_if_due(
        self, scope: str, asset_id: str, command_before: str, removed_at: str
    ) -> bool:
        row = self.rows.get(asset_id)
        refs = self.ref_rows.get(asset_id, [])
        if (
            row is None
            or row["scope"] != scope
            or row["local_state"] != "stored"
            or not refs
            or asset_id in self.refs
            or any(kind != "command" or created_at >= command_before for kind, created_at in refs)
        ):
            return False
        row["local_state"] = "expired"
        row["local_removed_at"] = removed_at
        return True

    def asset_scopes(self, after: str, limit: int) -> list[str]:
        return sorted({row["scope"] for row in self.rows.values() if row["scope"] > after})[:limit]


@pytest.fixture
def service_parts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[AssetService, FakeStore]:
    monkeypatch.setattr(protection, "require_private_directory", lambda path: None)
    monkeypatch.setattr(protection, "require_private_file", lambda path: None)
    monkeypatch.setattr(
        protection,
        "create_private_directory",
        lambda path: (path.mkdir(), True)[1],
    )
    monkeypatch.setattr(assets, "require_private_store", lambda store: None)
    store = FakeStore(tmp_path / "state.sqlite3")

    async def decoder(_path: Path) -> DecodedImage:
        return DecodedImage("image/png", 1, 1)

    return AssetService(tmp_path / "assets", store, SCOPE, decoder=decoder), store  # type: ignore[arg-type]


async def chunks(*values: bytes) -> AsyncIterator[bytes]:
    for value in values:
        yield value


@pytest.mark.asyncio
async def test_upload_resolve_and_stream_keep_the_blob_behind_an_opaque_id(
    service_parts: tuple[AssetService, FakeStore],
) -> None:
    service, store = service_parts
    uploaded = await service.upload(
        chunks(PNG[:20], PNG[20:]), name="sample.png", declared_bytes=len(PNG)
    )
    assert uploaded.media_type == "image/png"
    assert uploaded.byte_size == len(PNG)
    assert uploaded.width == uploaded.height == 1
    assert uploaded.name == "sample.png"
    assert set(store.rows) == {uploaded.asset_id}
    assert not any(path.name == "sample.png" for path in service.root.rglob("*"))

    attachment = Attachment(
        asset_id=uploaded.asset_id,
        media_type=uploaded.media_type,
        byte_size=uploaded.byte_size,
        width=uploaded.width,
        height=uploaded.height,
        name=uploaded.name,
    )
    assert await service.resolve(attachment) == PNG
    download = await service.download(uploaded.asset_id)
    assert download.media_type == "image/png"
    assert b"".join([part async for part in download.body]) == PNG


@pytest.mark.asyncio
async def test_resolve_rejects_changed_metadata_and_blob_bytes(
    service_parts: tuple[AssetService, FakeStore],
) -> None:
    service, store = service_parts
    uploaded = await service.upload(chunks(PNG), name="sample.png", declared_bytes=len(PNG))
    changed = Attachment(
        asset_id=uploaded.asset_id,
        media_type="image/png",
        byte_size=uploaded.byte_size,
        width=2,
        height=1,
        name="sample.png",
    )
    with pytest.raises(AssetError, match="asset_changed"):
        await service.resolve(changed)
    path = service.root / "blobs" / SCOPE / uploaded.asset_id
    path.write_bytes(PNG[:-1] + b"x")
    valid = Attachment(
        asset_id=uploaded.asset_id,
        media_type="image/png",
        byte_size=uploaded.byte_size,
        width=1,
        height=1,
        name="sample.png",
    )
    with pytest.raises(AssetError, match="corrupt"):
        await service.resolve(valid)
    assert uploaded.asset_id in store.rows


@pytest.mark.asyncio
async def test_upload_maps_an_unsafe_path_escape_to_unavailable_and_cleans_up(
    service_parts: tuple[AssetService, FakeStore], monkeypatch: pytest.MonkeyPatch
) -> None:
    service, store = service_parts
    real_check_file = assets.storage.check_file

    def check_final_asset_path(path: Path) -> bool:
        # Only the published-asset check (the boundary probe in `_prepare` must still pass).
        if path.name == ".viki-asset-boundary":
            return real_check_file(path)
        raise StateError("unsafe_path")

    monkeypatch.setattr(assets.storage, "check_file", check_final_asset_path)

    with pytest.raises(AssetError, match="unavailable"):
        await service.upload(chunks(PNG), name="sample.png", declared_bytes=len(PNG))

    assert store.rows == {}
    assert list(service._incoming.iterdir()) == []
    assert (service._reserved_count, service._reserved_bytes) == (0, 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("failing", ["open", "read"])
async def test_download_body_maps_an_open_or_read_failure_to_unavailable_with_no_path_in_the_log(
    service_parts: tuple[AssetService, FakeStore],
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    failing: str,
) -> None:
    service, _store = service_parts
    uploaded = await service.upload(chunks(PNG), name="sample.png", declared_bytes=len(PNG))
    blob = service.root / "blobs" / SCOPE / uploaded.asset_id
    real_open = Path.open
    opens = {"count": 0}

    class FailingRead:
        def __init__(self, handle: Any) -> None:
            self._handle = handle

        def read(self, *args: Any, **kwargs: Any) -> bytes:
            raise OSError(f"read failed: {blob}")

        def close(self) -> None:
            self._handle.close()

    def flaky_open(self: Path, *args: Any, **kwargs: Any) -> Any:
        # `download` opens the blob twice: once for its own digest check, once more for the
        # stream. The first open succeeds; the stream's open fails, or its read does.
        opens["count"] += 1
        if opens["count"] > 1 and failing == "open":
            raise OSError(f"open failed: {blob}")
        handle = real_open(self, *args, **kwargs)
        return handle if opens["count"] == 1 else FailingRead(handle)

    monkeypatch.setattr(Path, "open", flaky_open)
    download = await service.download(uploaded.asset_id)

    with caplog.at_level(logging.WARNING), pytest.raises(AssetError, match="unavailable") as info:
        async for _chunk in download.body:
            pass

    assert info.value.__cause__ is None and info.value.__suppress_context__
    assert not any(str(blob) in record.getMessage() for record in caplog.records)


@pytest.mark.asyncio
async def test_concurrent_upload_reservations_enforce_scope_capacity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(protection, "require_private_directory", lambda path: None)
    monkeypatch.setattr(protection, "require_private_file", lambda path: None)
    monkeypatch.setattr(
        protection, "create_private_directory", lambda path: (path.mkdir(), True)[1]
    )
    monkeypatch.setattr(assets, "require_private_store", lambda store: None)
    monkeypatch.setattr(assets, "MAX_ATTACHMENT_BYTES", 6)
    monkeypatch.setattr(assets, "MAX_SCOPE_BYTES", 10)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def decoder(_path: Path) -> DecodedImage:
        entered.set()
        await release.wait()
        return DecodedImage("image/png", 1, 1)

    service = AssetService(
        tmp_path / "assets",
        cast(SessionStore, FakeStore(tmp_path / "state.sqlite3")),
        SCOPE,
        decoder=decoder,
    )
    first = asyncio.create_task(service.upload(chunks(b"123456"), name="one.png", declared_bytes=6))
    await entered.wait()
    with pytest.raises(AssetError, match="capacity"):
        await service.upload(chunks(b"abcdef"), name="two.png", declared_bytes=6)
    release.set()
    assert (await first).byte_size == 6


@pytest.mark.asyncio
async def test_reservation_does_not_trust_declared_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(protection, "require_private_directory", lambda path: None)
    monkeypatch.setattr(protection, "require_private_file", lambda path: None)
    monkeypatch.setattr(
        protection, "create_private_directory", lambda path: (path.mkdir(), True)[1]
    )
    monkeypatch.setattr(assets, "require_private_store", lambda store: None)
    monkeypatch.setattr(assets, "MAX_ATTACHMENT_BYTES", 10)
    monkeypatch.setattr(assets, "MAX_SCOPE_BYTES", 10)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def decoder(_path: Path) -> DecodedImage:
        entered.set()
        await release.wait()
        return DecodedImage("image/png", 1, 1)

    service = AssetService(
        tmp_path / "assets",
        cast(SessionStore, FakeStore(tmp_path / "state.sqlite3")),
        SCOPE,
        decoder=decoder,
    )
    first = asyncio.create_task(service.upload(chunks(b"x"), name="one.png", declared_bytes=1))
    await entered.wait()
    with pytest.raises(AssetError, match="capacity"):
        await service.upload(chunks(b"y"), name="two.png", declared_bytes=1)
    release.set()
    assert (await first).byte_size == 1


@pytest.mark.asyncio
async def test_reference_safe_remove_and_collect_due(
    service_parts: tuple[AssetService, FakeStore],
) -> None:
    service, store = service_parts
    first = await service.upload(chunks(PNG), name="one.png", declared_bytes=len(PNG))
    second = await service.upload(chunks(PNG), name="two.png", declared_bytes=len(PNG))
    store.refs.add(first.asset_id)
    with pytest.raises(AssetError, match="in_use"):
        await service.remove(first.asset_id)
    assert first.asset_id in store.rows
    report = await service.collect_due(now=datetime(9999, 12, 31, 23, 59, 59, tzinfo=UTC))
    assert report.orphan_rows == 1
    assert first.asset_id in store.rows and second.asset_id not in store.rows
    store.refs.clear()
    assert await service.remove(first.asset_id) is True
    assert await service.remove(first.asset_id) is False


@pytest.mark.asyncio
async def test_collect_due_orphan_delete_keeps_a_row_a_concurrent_restore_made_fresh(
    service_parts: tuple[AssetService, FakeStore], monkeypatch: pytest.MonkeyPatch
) -> None:
    service, store = service_parts
    uploaded = await service.upload(chunks(PNG), name="orphan.png", declared_bytes=len(PNG))
    store.rows[uploaded.asset_id]["created_at"] = "2026-07-01T00:00:00.000Z"
    real_list = store.list_unreferenced_assets

    def list_then_restore(scope: str, before: str, limit: int) -> list[AssetRow]:
        # A relink's restore lands between the pass's listing and its delete.
        rows = real_list(scope, before, limit)
        for row in rows:
            store.rows[row["asset_id"]]["created_at"] = "2026-09-19T00:00:00.000Z"
        return rows

    monkeypatch.setattr(store, "list_unreferenced_assets", list_then_restore)

    report = await service.collect_due(now=datetime(2026, 9, 19, tzinfo=UTC))

    assert report.orphan_rows == 0
    assert uploaded.asset_id in store.rows
    assert (service.root / "blobs" / SCOPE / uploaded.asset_id).exists()


@pytest.mark.asyncio
async def test_retention_expires_only_old_command_copies_and_keeps_tombstones(
    service_parts: tuple[AssetService, FakeStore],
) -> None:
    service, store = service_parts
    orphan = await service.upload(chunks(PNG), name="orphan.png", declared_bytes=len(PNG))
    sent = await service.upload(chunks(PNG), name="sent.png", declared_bytes=len(PNG))
    draft = await service.upload(chunks(PNG), name="draft.png", declared_bytes=len(PNG))
    recent = await service.upload(chunks(PNG), name="recent.png", declared_bytes=len(PNG))
    for uploaded in (orphan, sent, draft, recent):
        store.rows[uploaded.asset_id]["created_at"] = "2026-07-01T00:00:00.000Z"
    store.ref_rows[sent.asset_id] = [("command", "2026-08-01T00:00:00.000Z")]
    store.ref_rows[draft.asset_id] = [
        ("command", "2026-08-01T00:00:00.000Z"),
        ("draft", "2026-08-01T00:00:00.000Z"),
    ]
    store.ref_rows[recent.asset_id] = [
        ("command", "2026-08-01T00:00:00.000Z"),
        ("command", "2026-09-10T00:00:00.000Z"),
    ]

    report = await service.collect_due(now=datetime(2026, 9, 19, tzinfo=UTC))
    assert report.orphan_rows == 1 and report.expired_rows == 1
    assert orphan.asset_id not in store.rows
    assert store.rows[sent.asset_id]["local_state"] == "expired"
    assert store.rows[sent.asset_id]["local_removed_at"] == "2026-09-19T00:00:00.000Z"
    assert store.rows[draft.asset_id]["local_state"] == "stored"
    assert store.rows[recent.asset_id]["local_state"] == "stored"
    blob = service.root / "blobs" / SCOPE
    assert not (blob / orphan.asset_id).exists() and not (blob / sent.asset_id).exists()
    assert (blob / draft.asset_id).exists() and (blob / recent.asset_id).exists()
    assert store.asset_usage(SCOPE) == (2, 2 * len(PNG))
    with pytest.raises(AssetError, match="expired"):
        await service.resolve(
            Attachment(
                sent.asset_id,
                sent.media_type,
                sent.byte_size,
                sent.width,
                sent.height,
                sent.name,
            )
        )


@pytest.mark.asyncio
async def test_retention_restart_retries_expired_blob_cleanup(
    service_parts: tuple[AssetService, FakeStore],
) -> None:
    service, store = service_parts
    uploaded = await service.upload(chunks(PNG), name="sent.png", declared_bytes=len(PNG))
    store.rows[uploaded.asset_id]["local_state"] = "expired"
    store.rows[uploaded.asset_id]["local_removed_at"] = "2026-09-01T00:00:00.000Z"
    store.ref_rows[uploaded.asset_id] = [("command", "2026-08-01T00:00:00.000Z")]
    blob = service.root / "blobs" / SCOPE / uploaded.asset_id
    old = datetime(2026, 8, 1, tzinfo=UTC).timestamp()
    os.utime(blob, (old, old))
    assert blob.exists()
    report = await service.collect_due(now=datetime(2026, 9, 19, tzinfo=UTC))
    assert report == assets.AssetCollection(orphan_rows=0, expired_rows=0, files_removed=1)
    assert not blob.exists() and uploaded.asset_id in store.rows


@pytest.mark.asyncio
async def test_retention_restart_removes_old_blob_after_row_delete_crash(
    service_parts: tuple[AssetService, FakeStore],
) -> None:
    service, store = service_parts
    uploaded = await service.upload(chunks(PNG), name="orphan.png", declared_bytes=len(PNG))
    del store.rows[uploaded.asset_id]
    blob = service.root / "blobs" / SCOPE / uploaded.asset_id
    old = datetime(2026, 9, 1, tzinfo=UTC).timestamp()
    blob.touch()
    os.utime(blob, (old, old))
    candidates = service._stale_untracked_ids(datetime(2026, 9, 18, tzinfo=UTC), 100)
    assert candidates == [uploaded.asset_id]
    report = await service.collect_due(now=datetime(2026, 9, 19, tzinfo=UTC))
    assert report == assets.AssetCollection(orphan_rows=0, expired_rows=0, files_removed=1)
    assert not blob.exists()


async def _upload_old_command_asset(service: AssetService, store: FakeStore, index: int) -> str:
    uploaded = await service.upload(chunks(PNG), name=f"a{index}.png", declared_bytes=len(PNG))
    store.rows[uploaded.asset_id]["created_at"] = "2026-07-01T00:00:00.000Z"
    store.ref_rows[uploaded.asset_id] = [("command", "2026-08-01T00:00:00.000Z")]
    old = datetime(2026, 7, 1, tzinfo=UTC).timestamp()
    os.utime(service.root / "blobs" / SCOPE / uploaded.asset_id, (old, old))
    return uploaded.asset_id


@pytest.mark.asyncio
async def test_retention_unlinks_every_expired_blob_past_one_pass_limit(
    service_parts: tuple[AssetService, FakeStore],
) -> None:
    service, store = service_parts
    ids = [await _upload_old_command_asset(service, store, index) for index in range(150)]

    first = await service.collect_due(now=datetime(2026, 9, 19, tzinfo=UTC))
    assert first.expired_rows == 100 and first.files_removed == 100
    second = await service.collect_due(now=datetime(2026, 9, 19, tzinfo=UTC))
    assert second.expired_rows == 50 and second.files_removed == 50

    assert list((service.root / "blobs" / SCOPE).iterdir()) == []
    assert all(store.rows[asset_id]["local_state"] == "expired" for asset_id in ids)


@pytest.mark.asyncio
async def test_retention_recovers_a_blob_left_by_a_crashed_unlink_after_the_101st_tombstone(
    service_parts: tuple[AssetService, FakeStore],
) -> None:
    service, store = service_parts
    await service._ready()
    # 100 tombstones that already have no blob on disk, sorted (by local_removed_at) ahead of
    # the 101st, so a fix that re-lists only the oldest 100 tombstones would never reach it.
    for index in range(100):
        asset_id = str(uuid.uuid4())
        store.rows[asset_id] = {
            "scope": SCOPE,
            "asset_id": asset_id,
            "media_type": "image/png",
            "byte_size": len(PNG),
            "sha256": "0" * 64,
            "width": 1,
            "height": 1,
            "created_at": "2026-07-01T00:00:00.000Z",
            "local_state": "expired",
            "local_removed_at": f"2026-09-01T00:00:{index:02d}.000Z",
        }
        store.ref_rows[asset_id] = [("command", "2026-08-01T00:00:00.000Z")]

    # The 101st tombstone: its commit landed, but a simulated crash left the blob behind.
    crashed_id = await _upload_old_command_asset(service, store, 100)
    store.rows[crashed_id]["local_state"] = "expired"
    store.rows[crashed_id]["local_removed_at"] = "2026-09-02T00:00:00.000Z"
    crashed_blob = service.root / "blobs" / SCOPE / crashed_id

    report = await service.collect_due(now=datetime(2026, 9, 19, tzinfo=UTC))
    assert report.files_removed == 1
    assert not crashed_blob.exists()


@pytest.mark.asyncio
async def test_untracked_blob_sweep_rotates_past_a_full_page_of_stored_blobs(
    service_parts: tuple[AssetService, FakeStore],
) -> None:
    service, store = service_parts
    await service._ready()
    blobs = service.root / "blobs" / SCOPE
    old = datetime(2026, 7, 1, tzinfo=UTC).timestamp()

    def plant(asset_id: str, local_state: str, removed_at: str | None) -> Path:
        store.rows[asset_id] = {
            "scope": SCOPE,
            "asset_id": asset_id,
            "media_type": "image/png",
            "byte_size": len(PNG),
            "sha256": "0" * 64,
            "width": 1,
            "height": 1,
            "created_at": "2026-07-01T00:00:00.000Z",
            "local_state": local_state,
            "local_removed_at": removed_at,
        }
        blob = blobs / asset_id
        blob.write_bytes(PNG)
        os.utime(blob, (old, old))
        return blob

    # 150 old blobs that stay: a draft still holds each one, so no pass may remove them.
    for _index in range(150):
        asset_id = str(uuid.uuid4())
        plant(asset_id, "stored", None)
        store.refs.add(asset_id)
    # A tombstone whose commit landed but whose unlink was lost. It sorts after every stored
    # blob, so a sweep that always examines the first 100 names never reaches it.
    crashed_id = "ffffffff-ffff-4fff-bfff-ffffffffffff"
    crashed = plant(crashed_id, "expired", "2026-09-01T00:00:00.000Z")
    store.ref_rows[crashed_id] = [("command", "2026-08-01T00:00:00.000Z")]

    now = datetime(2026, 9, 19, tzinfo=UTC)
    first = await service.collect_due(now=now)
    second = await service.collect_due(now=now)

    assert first.files_removed + second.files_removed == 1
    assert not crashed.exists()
    assert sum(1 for _entry in blobs.iterdir()) == 150


@pytest.mark.asyncio
async def test_untracked_blob_sweep_seeds_the_cursor_at_a_random_name_per_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A launch shorter than an hour runs only one pass. If every launch's cursor started at
    # "", that one pass would always re-examine the same first 100 old blobs, and a blob whose
    # unlink was lost after its tombstone committed could stay on disk for good.
    monkeypatch.setattr(protection, "require_private_directory", lambda path: None)
    monkeypatch.setattr(protection, "require_private_file", lambda path: None)
    monkeypatch.setattr(
        protection,
        "create_private_directory",
        lambda path: (path.mkdir(), True)[1],
    )
    monkeypatch.setattr(assets, "require_private_store", lambda store: None)
    store = FakeStore(tmp_path / "state.sqlite3")

    async def decoder(_path: Path) -> DecodedImage:
        return DecodedImage("image/png", 1, 1)

    root = tmp_path / "assets"
    blobs = root / "blobs" / SCOPE
    blobs.mkdir(parents=True)
    old = datetime(2026, 7, 1, tzinfo=UTC).timestamp()

    def plant(asset_id: str, local_state: str, removed_at: str | None) -> Path:
        store.rows[asset_id] = {
            "scope": SCOPE,
            "asset_id": asset_id,
            "media_type": "image/png",
            "byte_size": len(PNG),
            "sha256": "0" * 64,
            "width": 1,
            "height": 1,
            "created_at": "2026-07-01T00:00:00.000Z",
            "local_state": local_state,
            "local_removed_at": removed_at,
        }
        blob = blobs / asset_id
        blob.write_bytes(PNG)
        os.utime(blob, (old, old))
        return blob

    # 150 old blobs that stay: a draft still holds each one, so no pass may remove them.
    ids = [str(uuid.uuid4()) for _ in range(150)]
    for asset_id in ids:
        plant(asset_id, "stored", None)
        store.refs.add(asset_id)
    crashed_id = "ffffffff-ffff-4fff-bfff-ffffffffffff"
    crashed = plant(crashed_id, "expired", "2026-09-01T00:00:00.000Z")
    store.ref_rows[crashed_id] = [("command", "2026-08-01T00:00:00.000Z")]

    # A seed that sorts after the first 100 names, so this one launch's single pass reaches it.
    seed = sorted([*ids, crashed_id])[100]
    monkeypatch.setattr(assets.uuid, "uuid4", lambda: uuid.UUID(seed))

    service = AssetService(root, store, SCOPE, decoder=decoder)  # type: ignore[arg-type]
    report = await service.collect_due(now=datetime(2026, 9, 19, tzinfo=UTC))

    assert report.files_removed == 1
    assert not crashed.exists()


@pytest.mark.asyncio
async def test_untracked_blob_sweep_skips_a_busy_metadata_read_within_the_same_pass(
    service_parts: tuple[AssetService, FakeStore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, store = service_parts
    await service._ready()
    blobs = service.root / "blobs" / SCOPE
    old = datetime(2026, 7, 1, tzinfo=UTC).timestamp()

    def plant(asset_id: str, local_state: str, removed_at: str | None) -> Path:
        store.rows[asset_id] = {
            "scope": SCOPE,
            "asset_id": asset_id,
            "media_type": "image/png",
            "byte_size": len(PNG),
            "sha256": "0" * 64,
            "width": 1,
            "height": 1,
            "created_at": "2026-07-01T00:00:00.000Z",
            "local_state": local_state,
            "local_removed_at": removed_at,
        }
        blob = blobs / asset_id
        blob.write_bytes(PNG)
        os.utime(blob, (old, old))
        return blob

    # Fixed-width int-backed UUIDs sort by index, so the tombstone lands at sorted position 50.
    ids = [str(uuid.UUID(int=index)) for index in range(100)]
    for index, asset_id in enumerate(ids):
        if index == 50:
            plant(asset_id, "expired", "2026-09-01T00:00:00.000Z")
            store.ref_rows[asset_id] = [("command", "2026-08-01T00:00:00.000Z")]
        else:
            plant(asset_id, "stored", None)
            store.refs.add(asset_id)
    tombstone = blobs / ids[50]
    # Fix the examine order to ids[0], ids[1], ... so the busy read lands well before the
    # tombstone at position 50, regardless of the fixture's random per-launch cursor.
    service._sweep_cursor = ids[-1]

    # A stage sweep the pass must still run after a busy read is skipped.
    incoming = service.root / "incoming"
    stage = incoming / str(uuid.uuid4())
    stage.write_bytes(b"x")
    os.utime(stage, (old, old))

    reads = 0
    real_asset_metadata = store.asset_metadata

    def flaky_asset_metadata(scope: str, asset_ids: tuple[str, ...]) -> list[AssetRow]:
        nonlocal reads
        reads += 1
        if reads == 10:
            raise StateError("busy")
        return real_asset_metadata(scope, asset_ids)

    monkeypatch.setattr(store, "asset_metadata", flaky_asset_metadata)

    report = await service.collect_due(now=datetime(2026, 9, 19, tzinfo=UTC), limit=100)

    assert not tombstone.exists()
    assert report.files_removed == 2
    assert not stage.exists()


@pytest.mark.asyncio
async def test_upload_cleanup_releases_the_reservation_under_a_cancel(
    service_parts: tuple[AssetService, FakeStore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, store = service_parts
    await service._ready()
    incoming = service.root / "incoming"
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = threading.Event()
    real_exists = Path.exists

    def exists(path: Path, *, follow_symlinks: bool = True) -> bool:
        # Only the success path's cleanup asks whether the renamed stage still exists.
        if path.parent == incoming:
            loop.call_soon_threadsafe(entered.set)
            release.wait(5)
        return real_exists(path, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(Path, "exists", exists)
    task = asyncio.create_task(service.upload(chunks(PNG), name="a.png", declared_bytes=len(PNG)))
    await entered.wait()
    task.cancel()  # the route deadline's one cancel, landing inside the cleanup
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert (service._reserved_count, service._reserved_bytes) == (0, 0)
    [asset_id] = store.rows
    assert (service.root / "blobs" / SCOPE / asset_id).exists()


@pytest.mark.asyncio
async def test_an_avatar_upload_reserves_the_avatar_limit_not_the_attachment_limit(
    service_parts: tuple[AssetService, FakeStore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _store = service_parts
    reserved: list[int] = []
    reserve = service._reserve

    async def spy(requested: int | None, limit: int) -> int:
        amount = await reserve(requested, limit)
        reserved.append(amount)
        return amount

    monkeypatch.setattr(service, "_reserve", spy)
    await service.upload(
        chunks(PNG), name="a.png", declared_bytes=len(PNG), max_bytes=AVATAR_SOURCE_BYTES
    )
    await service.upload(chunks(PNG), name="a.png", declared_bytes=len(PNG))
    assert reserved == [AVATAR_SOURCE_BYTES, MAX_ATTACHMENT_BYTES]
    assert (service._reserved_count, service._reserved_bytes) == (0, 0)


@pytest.mark.asyncio
async def test_incoming_stage_sweep_stops_at_its_scan_budget(
    service_parts: tuple[AssetService, FakeStore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _store = service_parts
    await service._ready()
    monkeypatch.setattr(assets, "MAX_SCOPE_ASSETS", 0)
    incoming = service.root / "incoming"
    old = datetime(2026, 7, 1, tzinfo=UTC).timestamp()
    real_iterdir = Path.iterdir

    # This test pins name order because the scan order differs per filesystem: the two
    # non-UUID entries use up the budget of 0 + limit first.
    monkeypatch.setattr(Path, "iterdir", lambda self: iter(sorted(real_iterdir(self))))
    stage = incoming / str(uuid.uuid4())
    for entry in (incoming / "!a", incoming / "!b", stage):
        entry.write_bytes(b"x")
        os.utime(entry, (old, old))

    report = await service.collect_due(now=datetime(2026, 9, 19, tzinfo=UTC), limit=2)

    assert report.files_removed == 0
    assert stage.exists()


@pytest.mark.asyncio
async def test_collect_due_sweeps_stale_incoming_stages_and_keeps_unsafe_shapes(
    service_parts: tuple[AssetService, FakeStore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _store = service_parts
    await service._ready()
    incoming = service.root / "incoming"
    old = datetime(2026, 7, 1, tzinfo=UTC).timestamp()

    old_stage = incoming / str(uuid.uuid4())
    old_stage.write_bytes(b"x")
    os.utime(old_stage, (old, old))

    young_stage = incoming / str(uuid.uuid4())
    young_stage.write_bytes(b"x")

    a_directory = incoming / str(uuid.uuid4())
    a_directory.mkdir()
    os.utime(a_directory, (old, old))

    non_uuid = incoming / "not-a-uuid"
    non_uuid.write_bytes(b"x")
    os.utime(non_uuid, (old, old))

    hardlinked = incoming / str(uuid.uuid4())
    hardlinked.write_bytes(b"x")
    hardlinked_alias = incoming / "alias-of-hardlinked"
    os.link(hardlinked, hardlinked_alias)
    os.utime(hardlinked, (old, old))

    thrice_linked = incoming / str(uuid.uuid4())
    thrice_linked.write_bytes(b"x")
    os.link(thrice_linked, incoming / "alias-one-of-thrice")
    os.link(thrice_linked, incoming / "alias-two-of-thrice")
    os.utime(thrice_linked, (old, old))

    unprivate = incoming / str(uuid.uuid4())
    unprivate.write_bytes(b"x")
    os.utime(unprivate, (old, old))

    def guard(path: Path) -> None:
        if path == unprivate:
            raise protection.ProtectionError()

    monkeypatch.setattr(protection, "require_private_file", guard)

    report = await service.collect_due(now=datetime(2026, 9, 19, tzinfo=UTC))
    assert report.files_removed == 2
    assert not old_stage.exists()
    assert not hardlinked.exists()
    assert hardlinked_alias.exists()
    for kept in (young_stage, a_directory, non_uuid, thrice_linked, unprivate):
        assert kept.exists()


@pytest.mark.asyncio
async def test_collect_due_bounds_incoming_stage_removals_to_limit(
    service_parts: tuple[AssetService, FakeStore],
) -> None:
    service, _store = service_parts
    await service._ready()
    incoming = service.root / "incoming"
    old = datetime(2026, 7, 1, tzinfo=UTC).timestamp()
    stages = []
    for _index in range(5):
        stage = incoming / str(uuid.uuid4())
        stage.write_bytes(b"x")
        os.utime(stage, (old, old))
        stages.append(stage)

    report = await service.collect_due(now=datetime(2026, 9, 19, tzinfo=UTC), limit=3)
    assert report.files_removed == 3
    assert sum(1 for stage in stages if stage.exists()) == 2


async def _old_orphan(root: Path, store: FakeStore, scope: str) -> str:
    async def decoder(_path: Path) -> DecodedImage:
        return DecodedImage("image/png", 1, 1)

    service = AssetService(root, cast(SessionStore, store), scope, decoder=decoder)
    uploaded = await service.upload(chunks(PNG), name="old.png", declared_bytes=len(PNG))
    store.rows[uploaded.asset_id]["created_at"] = "2026-07-01T00:00:00.000Z"
    return uploaded.asset_id


@pytest.mark.asyncio
async def test_collect_scopes_collects_every_other_scope_and_reuses_services(
    service_parts: tuple[AssetService, FakeStore],
) -> None:
    launch, store = service_parts
    launch_orphan = await _old_orphan(launch.root, store, SCOPE)
    other, another = "b" * 64, "c" * 64
    orphans = [await _old_orphan(launch.root, store, scope) for scope in (other, another)]
    services = {SCOPE: launch}

    cursor = await assets.collect_scopes(
        launch.root, cast(SessionStore, store), services, "", skip=SCOPE
    )

    assert cursor == ""
    assert not any(orphan in store.rows for orphan in orphans)
    assert not any((launch.root / "blobs" / other).iterdir())
    assert launch_orphan in store.rows  # the launch scope is collected by its own pass
    assert services[SCOPE] is launch and set(services) == {SCOPE, other, another}
    reused = services[other]

    # A fresh, unexpired row keeps `other` in the next page, so a second pass reuses the
    # cached service for it instead of building a new one.
    async def decoder(_path: Path) -> DecodedImage:
        return DecodedImage("image/png", 1, 1)

    fresh = AssetService(launch.root, cast(SessionStore, store), other, decoder=decoder)  # type: ignore[arg-type]
    await fresh.upload(chunks(PNG), name="fresh.png", declared_bytes=len(PNG))

    await assets.collect_scopes(launch.root, cast(SessionStore, store), services, "", skip=SCOPE)

    assert services[other] is reused


@pytest.mark.asyncio
async def test_collect_scopes_skips_a_malformed_scope_and_survives_a_failing_one(
    service_parts: tuple[AssetService, FakeStore], caplog: pytest.LogCaptureFixture
) -> None:
    service, store = service_parts
    broken, healthy = "b" * 64, "c" * 64
    orphan = await _old_orphan(service.root, store, healthy)
    for scope in (broken, "bz"):
        store.rows[scope] = {**store.rows[orphan], "asset_id": scope, "scope": scope}

    class Broken:
        async def collect_due(self) -> None:
            raise RuntimeError(str(service.root))

    services = {broken: cast(AssetService, Broken())}
    with caplog.at_level(logging.WARNING, logger="viki_agent.media.assets"):
        cursor = await assets.collect_scopes(service.root, cast(SessionStore, store), services, "")

    assert cursor == ""
    assert orphan not in store.rows and "bz" not in services
    assert [record.getMessage() for record in caplog.records] == [
        "asset retention pass unavailable"
    ]
    assert broken not in caplog.text and str(service.root) not in caplog.text


@pytest.mark.asyncio
async def test_collect_scopes_pages_through_more_scopes_than_one_pass_takes(
    service_parts: tuple[AssetService, FakeStore], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(assets, "MAX_SCOPES_PER_PASS", 2)
    service, store = service_parts
    scopes = ["b" * 64, "c" * 64, "d" * 64]
    orphans = [await _old_orphan(service.root, store, scope) for scope in scopes]
    services: dict[str, AssetService] = {}

    first = await assets.collect_scopes(service.root, cast(SessionStore, store), services, "")
    assert first == scopes[1]
    assert [orphan in store.rows for orphan in orphans] == [False, False, True]

    second = await assets.collect_scopes(service.root, cast(SessionStore, store), services, first)
    assert second == ""
    assert not any(orphan in store.rows for orphan in orphans)


@pytest.mark.asyncio
async def test_real_worker_decodes_a_forty_megapixel_lossy_webp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from PIL import Image

    monkeypatch.setattr(protection, "require_private_directory", lambda path: None)
    monkeypatch.setattr(protection, "require_private_file", lambda path: None)
    monkeypatch.setattr(
        protection, "create_private_directory", lambda path: (path.mkdir(), True)[1]
    )
    monkeypatch.setattr(assets, "require_private_store", lambda store: None)
    service = AssetService(
        tmp_path / "assets",
        cast(SessionStore, FakeStore(tmp_path / "state.sqlite3")),
        SCOPE,
    )
    image = Image.new("RGB", (8000, 5000), color=(120, 60, 200))
    buffer = io.BytesIO()
    image.save(buffer, format="WEBP", lossless=False, quality=80)
    data = buffer.getvalue()
    assert len(data) <= 20 << 20

    uploaded = await service.upload(chunks(data), name="big.webp", declared_bytes=len(data))
    assert (uploaded.media_type, uploaded.width, uploaded.height) == ("image/webp", 8000, 5000)


@pytest.mark.asyncio
async def test_a_decoder_reporting_over_40mp_is_refused_before_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(protection, "require_private_directory", lambda path: None)
    monkeypatch.setattr(protection, "require_private_file", lambda path: None)
    monkeypatch.setattr(
        protection, "create_private_directory", lambda path: (path.mkdir(), True)[1]
    )
    monkeypatch.setattr(assets, "require_private_store", lambda store: None)
    store = FakeStore(tmp_path / "state.sqlite3")

    async def decoder(_path: Path) -> DecodedImage:
        return DecodedImage("image/png", 8001, 5000)

    service = AssetService(tmp_path / "assets", store, SCOPE, decoder=decoder)  # type: ignore[arg-type]

    with pytest.raises(AssetError, match="decode_failed"):
        await service.upload(chunks(PNG), name="huge.png", declared_bytes=len(PNG))

    assert store.rows == {}
    assert list(service._incoming.iterdir()) == []
    assert (service._reserved_count, service._reserved_bytes) == (0, 0)


def test_display_name_rejects_unpaired_surrogate_as_typed_error() -> None:
    with pytest.raises(AssetError, match="invalid_name"):
        assets._display_name("bad\ud800name.png")


@pytest.mark.asyncio
async def test_real_worker_accepts_png_and_rejects_non_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(protection, "require_private_directory", lambda path: None)
    monkeypatch.setattr(protection, "require_private_file", lambda path: None)
    monkeypatch.setattr(
        protection, "create_private_directory", lambda path: (path.mkdir(), True)[1]
    )
    monkeypatch.setattr(assets, "require_private_store", lambda store: None)
    service = AssetService(
        tmp_path / "assets",
        cast(SessionStore, FakeStore(tmp_path / "state.sqlite3")),
        SCOPE,
    )
    uploaded = await service.upload(chunks(PNG), name="valid.png", declared_bytes=len(PNG))
    assert (uploaded.media_type, uploaded.width, uploaded.height) == ("image/png", 1, 1)
    with pytest.raises(AssetError, match="decode_failed"):
        await service.upload(chunks(b"<svg></svg>"), name="spoof.png", declared_bytes=11)
    assert not list((service.root / "incoming").iterdir())


@pytest.mark.asyncio
async def test_cancelled_upload_kills_reaps_the_worker_and_frees_the_stage_and_reservation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cancel reap, proven through `upload` (not just the decoder call it wraps): a
    cancelled upload must kill and reap the worker, and still clean up its own stage file and
    reservation, the same as any other refused upload."""
    monkeypatch.setattr(protection, "require_private_directory", lambda path: None)
    monkeypatch.setattr(protection, "require_private_file", lambda path: None)
    monkeypatch.setattr(
        protection, "create_private_directory", lambda path: (path.mkdir(), True)[1]
    )
    monkeypatch.setattr(assets, "require_private_store", lambda store: None)

    class Process:
        def __init__(self) -> None:
            self.returncode: int | None = None
            self.started = asyncio.Event()
            self.killed = asyncio.Event()
            self.reaped = False
            self.stdin = SimpleNamespace(close=lambda: None)
            self.stdout = SimpleNamespace(read=self._silent)
            self.stderr = SimpleNamespace(read=self._silent)

        async def _silent(self, _size: int = -1) -> bytes:
            # A pipe that stays silent until the worker is killed, then reads EOF.
            self.started.set()
            await self.killed.wait()
            return b""

        async def wait(self) -> int:
            await self.killed.wait()
            self.returncode = 1
            self.reaped = True
            return 1

    class Child:
        def __init__(self) -> None:
            self.process = Process()
            self.released = False
            self.kill_calls = 0

        def kill_tree(self) -> None:
            self.kill_calls += 1
            self.process.killed.set()

        def release(self) -> None:
            self.released = True

    child = Child()

    async def spawn(*_args: object, **_kwargs: object) -> Child:
        return child

    monkeypatch.setattr(assets.winproc, "spawn", spawn)
    store = FakeStore(tmp_path / "state.sqlite3")
    service = AssetService(tmp_path / "assets", cast(SessionStore, store), SCOPE)

    task = asyncio.create_task(
        service.upload(chunks(PNG), name="sample.png", declared_bytes=len(PNG))
    )
    await asyncio.wait_for(child.process.started.wait(), HANG_GUARD_S)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert child.kill_calls == 1
    assert child.process.reaped and child.released
    assert list(service._incoming.iterdir()) == []
    assert store.rows == {}
    assert (service._reserved_count, service._reserved_bytes) == (0, 0)


@pytest.mark.asyncio
async def test_worker_output_past_the_bound_kills_the_worker_and_reads_one_byte_more(
    tmp_path: Path,
) -> None:
    limit = 2048
    script = (
        f"import sys, time; sys.stdout.buffer.write(b'x' * {limit + 100}); "
        "sys.stdout.flush(); time.sleep(30)"
    )
    child = await winproc.spawn([sys.executable, "-c", script], cwd=tmp_path, env=os.environ)
    try:
        async with asyncio.timeout(HANG_GUARD_S):
            output = await assets.read_worker_output(child, limit)
        assert output == b"x" * (limit + 1)
        assert child.process.returncode is not None
    finally:
        child.release()


@pytest.mark.asyncio
async def test_a_worker_flooding_stdout_past_the_pipe_buffers_is_still_reaped(
    tmp_path: Path,
) -> None:
    # Far more than the stdout reader buffers before it pauses its transport, which then never
    # sees EOF unless the rest is drained after the kill.
    limit = 2048
    script = (
        "import sys, time; sys.stdout.buffer.write(b'x' * (1 << 20)); "
        "sys.stdout.flush(); time.sleep(30)"
    )
    child = await winproc.spawn([sys.executable, "-c", script], cwd=tmp_path, env=os.environ)
    try:
        async with asyncio.timeout(HANG_GUARD_S):
            output = await assets.read_worker_output(child, limit)
        assert output == b"x" * (limit + 1)
        assert child.process.returncode is not None
    finally:
        child.release()


@pytest.mark.asyncio
async def test_worker_stderr_is_drained_so_a_noisy_worker_never_hangs(tmp_path: Path) -> None:
    script = (
        "import sys; sys.stderr.buffer.write(b'e' * (1 << 20)); sys.stderr.flush(); "
        "sys.stdout.buffer.write(b'short')"
    )
    child = await winproc.spawn([sys.executable, "-c", script], cwd=tmp_path, env=os.environ)
    try:
        async with asyncio.timeout(HANG_GUARD_S):
            output = await assets.read_worker_output(child, 2048)
        assert output == b"short"
        assert child.process.returncode == 0
    finally:
        child.release()


def _fake_child(returncode: int, stdout: bytes) -> Any:
    class Process:
        def __init__(self) -> None:
            self.returncode = returncode
            self.stdin = SimpleNamespace(close=lambda: None)
            self.stdout = SimpleNamespace(read=self._read(stdout))
            self.stderr = SimpleNamespace(read=self._read(b""))

        @staticmethod
        def _read(data: bytes) -> Callable[[int], Awaitable[bytes]]:
            buffer = io.BytesIO(data)

            async def read(size: int = -1) -> bytes:
                return buffer.read(size)

            return read

        async def wait(self) -> int:
            return returncode

    class Child:
        def __init__(self) -> None:
            self.process = Process()
            self.released = False

        def kill_tree(self) -> None:  # pragma: no cover - none of these cases time out
            raise AssertionError("unexpected kill")

        def release(self) -> None:
            self.released = True

    return Child()


def _service_over_fake_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, child: Any
) -> AssetService:
    async def spawn(*_args: object, **_kwargs: object) -> Any:
        return child

    monkeypatch.setattr(assets.winproc, "spawn", spawn)
    return AssetService(
        tmp_path / "assets", cast(SessionStore, FakeStore(tmp_path / "state.sqlite3")), SCOPE
    )


@pytest.mark.asyncio
async def test_decoder_unavailable_exit_code_maps_to_decoder_unavailable_and_logs_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    child = _fake_child(
        media_worker.DECODER_UNAVAILABLE_EXIT_CODE, b'{"ok":false,"code":"decoder_unavailable"}'
    )
    service = _service_over_fake_worker(tmp_path, monkeypatch, child)

    with caplog.at_level(logging.WARNING), pytest.raises(AssetError, match="decoder_unavailable"):
        await service._decode_in_worker(tmp_path / "opaque")

    assert child.released
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert str(media_worker.DECODER_UNAVAILABLE_EXIT_CODE) in message
    for leak in ("opaque", str(tmp_path), "decoder_unavailable"):
        assert leak not in message


@pytest.mark.asyncio
async def test_killed_worker_maps_to_unavailable_and_logs_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # An external kill (antivirus, Task Manager) or a native crash never runs the worker's own
    # return statement, so it leaves no valid JSON behind even though the exit code (here, the
    # Job's own default TerminateJobObject code) collides with a genuine decode_failed exit.
    child = _fake_child(1, b"")
    service = _service_over_fake_worker(tmp_path, monkeypatch, child)

    with caplog.at_level(logging.WARNING), pytest.raises(AssetError, match="unavailable"):
        await service._decode_in_worker(tmp_path / "opaque")

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1
    assert warnings[0].getMessage() == "asset worker exited: 1"


@pytest.mark.asyncio
async def test_unexpected_exit_code_with_no_valid_json_maps_to_unavailable_and_logs_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    child = _fake_child(99, b"not json")
    service = _service_over_fake_worker(tmp_path, monkeypatch, child)

    with caplog.at_level(logging.WARNING), pytest.raises(AssetError, match="unavailable"):
        await service._decode_in_worker(tmp_path / "opaque")

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1
    assert warnings[0].getMessage() == "asset worker exited: 99"


@pytest.mark.asyncio
async def test_worker_reported_decode_failure_stays_decode_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    child = _fake_child(1, b'{"ok":false,"code":"decode_failed"}')
    service = _service_over_fake_worker(tmp_path, monkeypatch, child)

    with caplog.at_level(logging.WARNING), pytest.raises(AssetError, match="decode_failed"):
        await service._decode_in_worker(tmp_path / "opaque")

    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


@pytest.mark.asyncio
async def test_read_for_vault_returns_the_verified_bytes_with_the_rows_digest_and_type(
    service_parts: tuple[AssetService, FakeStore],
) -> None:
    service, store = service_parts
    uploaded = await service.upload(chunks(PNG), name="sample.png", declared_bytes=len(PNG))

    data, sha256, media_type = await service.read_for_vault(uploaded.asset_id)

    assert data == PNG
    assert sha256 == store.rows[uploaded.asset_id]["sha256"]
    assert media_type == "image/png"


@pytest.mark.asyncio
async def test_read_for_vault_refuses_an_unknown_expired_or_changed_asset_as_unavailable(
    service_parts: tuple[AssetService, FakeStore],
) -> None:
    service, store = service_parts
    with pytest.raises(AssetError, match="^unavailable$"):
        await service.read_for_vault(str(uuid.uuid4()))
    with pytest.raises(AssetError, match="^unavailable$"):
        await service.read_for_vault("not-an-asset-id")

    tampered = await service.upload(chunks(PNG), name="a.png", declared_bytes=len(PNG))
    (service.root / "blobs" / SCOPE / tampered.asset_id).write_bytes(PNG[:-1] + b"x")
    with pytest.raises(AssetError, match="^unavailable$"):
        await service.read_for_vault(tampered.asset_id)

    expired = await service.upload(chunks(PNG), name="b.png", declared_bytes=len(PNG))
    store.rows[expired.asset_id]["local_state"] = "expired"
    store.rows[expired.asset_id]["local_removed_at"] = "2026-01-01T00:00:00.000Z"
    with pytest.raises(AssetError, match="^unavailable$"):
        await service.read_for_vault(expired.asset_id)
