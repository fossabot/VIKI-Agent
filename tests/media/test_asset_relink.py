"""Relinking brings back an expired sent image only from the very file that was sent."""

from __future__ import annotations

import asyncio
import contextlib
import os
import sqlite3
import threading
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from tests.media.test_assets import PNG
from viki_agent import osfs, protection
from viki_agent.media import assets
from viki_agent.media.assets import AssetError, AssetService, DecodedImage
from viki_agent.media.assets_api import _error, asset_routes
from viki_agent.state import AppState, Prefs

BASE = "http://127.0.0.1:8765"
AUTH = {"authorization": "Bearer relink-token"}
MUTATION = {**AUTH, "origin": BASE}
SCOPE = "d" * 64
REMOVED_AT = "2026-01-01T00:00:00.000Z"
IDENTITY = ("media_type", "byte_size", "sha256", "width", "height")
# Guards waits on cross-thread events that normally fire in milliseconds; only a hang reaches it.
HANG_GUARD_S = 5


@dataclass
class Rig:
    client: TestClient
    service: AssetService
    state: AppState
    decoded: DecodedImage = field(default_factory=lambda: DecodedImage("image/png", 1, 1))
    decodes: int = 0

    def upload(self) -> str:
        response = self.client.post(
            "/api/v2/assets", headers={**MUTATION, "x-file-name": "sent.png"}, content=PNG
        )
        assert response.status_code == 201
        return response.json()["asset_id"]

    def blob(self, asset_id: str) -> Path:
        return self.service.root / "blobs" / SCOPE / asset_id

    def expire(self, asset_id: str) -> None:
        """What the retention pass leaves behind: a tombstoned row and no bytes."""
        with contextlib.closing(sqlite3.connect(self.state.database_path)) as connection:
            connection.execute(
                "UPDATE assets SET local_state='expired',local_removed_at=?,created_at=? "
                "WHERE asset_id=?",
                (REMOVED_AT, REMOVED_AT, asset_id),
            )
            connection.commit()
        self.blob(asset_id).unlink()

    def row(self, asset_id: str) -> dict[str, Any]:
        (row,) = self.state.store.asset_metadata(SCOPE, (asset_id,))
        return row

    def relink(
        self, asset_id: str, content: bytes = PNG, headers: dict[str, str] = MUTATION
    ) -> Any:
        return self.client.post(
            f"/api/v2/assets/{asset_id}/relink", headers=headers, content=content
        )

    def assert_nothing_held(self) -> None:
        assert (self.service._reserved_count, self.service._reserved_bytes) == (0, 0)
        assert list((self.service.root / "incoming").iterdir()) == []


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Rig:
    monkeypatch.setattr(protection, "require_private_directory", lambda path: None)
    monkeypatch.setattr(protection, "require_private_file", lambda path: None)
    monkeypatch.setattr(
        protection, "create_private_directory", lambda path: (path.mkdir(), True)[1]
    )
    monkeypatch.setattr(assets, "require_private_store", lambda store: None)
    state = AppState(tmp_path / "state.json", Prefs())

    async def decoder(_path: Path) -> DecodedImage:
        built.decodes += 1
        return built.decoded

    service = AssetService(tmp_path / "assets", state.store, SCOPE, decoder=decoder)
    routes = asset_routes(
        service,
        authorized=lambda request: request.headers.get("authorization") == AUTH["authorization"],
        origins=frozenset({BASE}),
        workers=set(),
    )
    built = Rig(TestClient(Starlette(routes=routes), base_url=BASE), service, state)
    return built


def test_the_same_file_restores_an_expired_image_and_it_reads_again(rig: Rig) -> None:
    asset_id = rig.upload()
    before = rig.row(asset_id)
    rig.expire(asset_id)
    assert rig.client.get(f"/api/v2/assets/{asset_id}", headers=AUTH).status_code == 410

    response = rig.relink(asset_id)

    assert response.status_code == 204
    after = rig.row(asset_id)
    assert after["local_state"] == "stored" and after["local_removed_at"] is None
    assert after["created_at"] > REMOVED_AT  # the retention clock starts again
    assert [after[key] for key in IDENTITY] == [before[key] for key in IDENTITY]
    read = rig.client.get(f"/api/v2/assets/{asset_id}", headers=AUTH)
    assert read.status_code == 200 and read.content == PNG
    rig.assert_nothing_held()


@pytest.mark.parametrize(
    "content,decoded",
    [
        (PNG + b"x", DecodedImage("image/png", 1, 1)),
        # Same length, type and dimensions: only the digest tells this file apart.
        (PNG[:-1] + bytes([PNG[-1] ^ 1]), DecodedImage("image/png", 1, 1)),
        (PNG, DecodedImage("image/png", 2, 1)),
        (PNG, DecodedImage("image/webp", 1, 1)),
    ],
    ids=["other bytes", "same length other bytes", "other dimensions", "another media type"],
)
def test_a_different_file_is_refused_and_the_image_stays_expired(
    rig: Rig, content: bytes, decoded: DecodedImage
) -> None:
    asset_id = rig.upload()
    rig.expire(asset_id)
    rig.decoded = decoded

    response = rig.relink(asset_id, content)

    assert response.status_code == 409
    assert response.json()["error"] == {
        "code": "asset_mismatch",
        "message": "The file you chose doesn't match the image you sent.",
    }
    assert rig.row(asset_id)["local_state"] == "expired"
    assert rig.row(asset_id)["created_at"] == REMOVED_AT
    assert not rig.blob(asset_id).exists()
    rig.assert_nothing_held()


def test_a_stored_image_answers_at_once_without_reading_or_reserving(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    asset_id = rig.upload()
    monkeypatch.setattr(assets, "MAX_SCOPE_BYTES", 0)  # any reservation would be refused
    decodes = rig.decodes

    async def unread() -> AsyncIterator[bytes]:
        raise AssertionError("the body was read")
        yield b""

    assert asyncio.run(rig.service.relink(asset_id, unread(), declared_bytes=None)) is None
    response = rig.relink(asset_id)

    assert response.status_code == 204
    assert rig.decodes == decodes
    assert rig.row(asset_id)["local_state"] == "stored"
    rig.assert_nothing_held()


@pytest.mark.parametrize(
    "asset_id,status,code",
    [
        (str(uuid.uuid4()), 404, "not_found"),
        (str(uuid.uuid4()).upper(), 400, "invalid_asset"),
        ("not-an-asset", 400, "invalid_asset"),
        (str(uuid.uuid4())[:35], 400, "invalid_asset"),
    ],
    ids=["unknown", "uppercase", "not-an-asset", "35-characters"],
)
def test_an_unknown_or_malformed_id_is_refused_before_any_intake(
    rig: Rig, asset_id: str, status: int, code: str
) -> None:
    response = rig.relink(asset_id)

    assert response.status_code == status
    assert response.json()["error"]["code"] == code
    assert rig.decodes == 0
    assert not (rig.service.root / "incoming").exists()
    assert (rig.service._reserved_count, rig.service._reserved_bytes) == (0, 0)


def test_capacity_is_reserved_as_for_an_upload(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    asset_id = rig.upload()
    rig.expire(asset_id)
    monkeypatch.setattr(assets, "MAX_SCOPE_BYTES", assets.MAX_ATTACHMENT_BYTES - 1)

    response = rig.relink(asset_id)

    assert response.status_code == 429
    assert response.json()["error"]["code"] == "capacity"
    assert rig.decodes == 1  # the seeding upload only
    assert rig.row(asset_id)["local_state"] == "expired"
    rig.assert_nothing_held()


@pytest.mark.parametrize(
    "headers,status,code",
    [
        ({"origin": BASE}, 401, "unauthorized"),
        (AUTH, 403, "forbidden_origin"),
        ({**AUTH, "origin": "http://evil.invalid"}, 403, "forbidden_origin"),
    ],
)
def test_relink_requires_the_bearer_token_and_the_app_origin(
    rig: Rig, headers: dict[str, str], status: int, code: str
) -> None:
    asset_id = rig.upload()
    rig.expire(asset_id)

    response = rig.relink(asset_id, headers=headers)

    assert response.status_code == status
    assert response.json()["error"]["code"] == code
    assert rig.row(asset_id)["local_state"] == "expired"
    assert rig.decodes == 1


def test_a_file_already_at_the_blob_path_is_left_alone(rig: Rig) -> None:
    asset_id = rig.upload()
    rig.expire(asset_id)
    rig.blob(asset_id).write_bytes(b"not ours")

    response = rig.relink(asset_id)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "unavailable"
    assert rig.blob(asset_id).read_bytes() == b"not ours"
    assert rig.row(asset_id)["local_state"] == "expired"
    rig.assert_nothing_held()


def test_a_cancel_during_the_restore_write_keeps_the_bytes_the_row_now_owns(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    asset_id = rig.upload()
    rig.expire(asset_id)
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    restore = rig.state.store.restore_expired_asset

    def slow_restore(row: Any, created_at: str) -> bool:
        # A slow COMMIT: the write is under way when the route deadline's cancel lands.
        entered.set()
        try:
            assert release.wait(HANG_GUARD_S)
            return restore(row, created_at)
        finally:
            finished.set()

    monkeypatch.setattr(rig.state.store, "restore_expired_asset", slow_restore)

    async def body() -> AsyncIterator[bytes]:
        yield PNG

    async def scenario() -> bytes:
        task = asyncio.ensure_future(rig.service.relink(asset_id, body(), declared_bytes=len(PNG)))
        assert await asyncio.to_thread(entered.wait, HANG_GUARD_S)
        task.cancel()
        release.set()
        done, _ = await asyncio.wait({task}, timeout=HANG_GUARD_S)
        assert task in done and task.cancelled()
        assert await asyncio.to_thread(finished.wait, HANG_GUARD_S)
        download = await rig.service.download(asset_id)
        return b"".join([chunk async for chunk in download.body])

    # The write committed, so the row is stored and must keep its bytes.
    assert asyncio.run(scenario()) == PNG
    assert rig.row(asset_id)["local_state"] == "stored"
    assert rig.blob(asset_id).read_bytes() == PNG
    rig.assert_nothing_held()


def test_a_cancel_during_the_rename_still_restores_and_keeps_the_blob(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    asset_id = rig.upload()
    rig.expire(asset_id)
    entered, release, renamed = threading.Event(), threading.Event(), threading.Event()
    incoming = rig.service.root / "incoming"
    real_rename, real_exists, real_run_sync = (
        osfs.rename_no_replace,
        Path.exists,
        assets.to_thread.run_sync,
    )
    renaming_tasks: list[asyncio.Task[Any] | None] = []

    def held_rename(source: Any, target: Any) -> None:
        entered.set()
        assert release.wait(HANG_GUARD_S)
        real_rename(source, target)
        renamed.set()

    def exists(self: Path, *args: Any, **kwargs: Any) -> bool:
        result = real_exists(self, *args, **kwargs)
        if self.parent == incoming and entered.is_set() and not release.is_set():
            # A look at the stage while the rename is held: the rename finishes only after the
            # look has seen the stage still in place.
            release.set()
            assert renamed.wait(HANG_GUARD_S)
        return result

    async def run_sync(func: Any, *args: Any, **kwargs: Any) -> Any:
        if func is held_rename:
            renaming_tasks.append(asyncio.current_task())
        return await real_run_sync(func, *args, **kwargs)

    monkeypatch.setattr(osfs, "rename_no_replace", held_rename)
    monkeypatch.setattr(Path, "exists", exists)
    monkeypatch.setattr(assets.to_thread, "run_sync", run_sync)

    async def body() -> AsyncIterator[bytes]:
        yield PNG

    async def scenario() -> None:
        task = asyncio.ensure_future(rig.service.relink(asset_id, body(), declared_bytes=len(PNG)))
        assert await asyncio.to_thread(entered.wait, HANG_GUARD_S)
        task.cancel()
        if renaming_tasks != [task]:
            # The rename runs outside the cancelled task, and the cleanup waits for it, so no
            # look at the stage can come first: nothing else will release it.
            release.set()
        done, _ = await asyncio.wait({task}, timeout=HANG_GUARD_S)
        assert task in done and task.cancelled()

    asyncio.run(scenario())
    assert rig.row(asset_id)["local_state"] == "stored"
    assert rig.blob(asset_id).read_bytes() == PNG
    rig.assert_nothing_held()
    assert rig.relink(asset_id).status_code == 204


def test_a_restore_refused_after_a_cancel_reaches_no_loop_exception_handler(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    asset_id = rig.upload()
    rig.expire(asset_id)
    entered, release = threading.Event(), threading.Event()
    reported: list[dict[str, Any]] = []

    def refused_restore(row: Any, created_at: str) -> bool:
        # The row changed while the write waited: the store refuses it after the cancel landed.
        entered.set()
        assert release.wait(HANG_GUARD_S)
        return False

    monkeypatch.setattr(rig.state.store, "restore_expired_asset", refused_restore)

    async def body() -> AsyncIterator[bytes]:
        yield PNG

    async def scenario() -> None:
        asyncio.get_running_loop().set_exception_handler(
            lambda _loop, context: reported.append(context)
        )
        task = asyncio.ensure_future(rig.service.relink(asset_id, body(), declared_bytes=len(PNG)))
        assert await asyncio.to_thread(entered.wait, HANG_GUARD_S)
        task.cancel()
        release.set()
        done, _ = await asyncio.wait({task}, timeout=HANG_GUARD_S)
        assert task in done and task.cancelled()

    asyncio.run(scenario())
    # The refusal is the service's to handle; the loop's handler would log it as an ERROR.
    assert reported == []
    assert rig.row(asset_id)["local_state"] == "expired"
    assert not rig.blob(asset_id).exists()
    rig.assert_nothing_held()


def test_a_row_that_changed_before_the_restore_removes_the_moved_blob(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    asset_id = rig.upload()
    rig.expire(asset_id)
    monkeypatch.setattr(rig.state.store, "restore_expired_asset", lambda row, created_at: False)

    response = rig.relink(asset_id)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "asset_changed"
    assert not rig.blob(asset_id).exists()
    assert rig.row(asset_id)["local_state"] == "expired"
    rig.assert_nothing_held()


@pytest.mark.parametrize(
    "content",
    [PNG + b"x", PNG[:-1] + bytes([PNG[-1] ^ 1])],
    ids=["other size", "same size other digest"],
)
def test_a_different_file_is_refused_before_the_decoder_runs(rig: Rig, content: bytes) -> None:
    asset_id = rig.upload()
    rig.expire(asset_id)
    decodes = rig.decodes

    response = rig.relink(asset_id, content)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "asset_mismatch"
    assert rig.decodes == decodes
    assert rig.row(asset_id)["local_state"] == "expired"
    rig.assert_nothing_held()


def test_the_sent_bytes_an_interrupted_relink_left_at_the_blob_path_are_adopted(rig: Rig) -> None:
    asset_id = rig.upload()
    rig.expire(asset_id)
    # A relink that moved its blob into place, then died before the restore committed.
    rig.blob(asset_id).write_bytes(PNG)
    before = rig.blob(asset_id).stat()

    response = rig.relink(asset_id)

    assert response.status_code == 204
    assert rig.row(asset_id)["local_state"] == "stored"
    after = rig.blob(asset_id).stat()
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)
    read = rig.client.get(f"/api/v2/assets/{asset_id}", headers=AUTH)
    assert read.status_code == 200 and read.content == PNG
    rig.assert_nothing_held()


def test_an_adopted_blob_stays_when_the_restore_is_refused(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    asset_id = rig.upload()
    rig.expire(asset_id)
    rig.blob(asset_id).write_bytes(PNG)
    monkeypatch.setattr(rig.state.store, "restore_expired_asset", lambda row, created_at: False)

    response = rig.relink(asset_id)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "asset_changed"
    # This call never moved that file, so it is never this call's to remove.
    assert rig.blob(asset_id).read_bytes() == PNG
    assert rig.row(asset_id)["local_state"] == "expired"
    rig.assert_nothing_held()


def test_a_retention_pass_during_an_adoption_keeps_the_adopted_blob(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    asset_id = rig.upload()
    rig.expire(asset_id)
    with contextlib.closing(sqlite3.connect(rig.state.database_path)) as connection:
        connection.execute(
            "INSERT INTO asset_refs VALUES (?,?,?,?,?,?)",
            (SCOPE, asset_id, "command", "command-1", "attachment:0", REMOVED_AT),
        )
        connection.commit()
    # A leftover from a relink that died two days ago: old enough for the stale-blob pass.
    blob = rig.blob(asset_id)
    blob.write_bytes(PNG)
    two_days_ago = time.time() - 2 * 86400
    os.utime(blob, (two_days_ago, two_days_ago))
    checked, release = threading.Event(), threading.Event()
    read_checked = rig.service._read_checked

    def gated(row: Any) -> bytes:
        # The relink has verified the leftover; hold it there while the pass runs.
        data = read_checked(row)
        if not checked.is_set():
            checked.set()
            assert release.wait(HANG_GUARD_S)
        return data

    monkeypatch.setattr(rig.service, "_read_checked", gated)

    async def body() -> AsyncIterator[bytes]:
        yield PNG

    async def scenario() -> assets.AssetCollection:
        relinking = asyncio.ensure_future(
            rig.service.relink(asset_id, body(), declared_bytes=len(PNG))
        )
        assert await asyncio.to_thread(checked.wait, HANG_GUARD_S)
        try:
            return await rig.service.collect_due()
        finally:
            release.set()
            await asyncio.wait_for(relinking, HANG_GUARD_S)

    collected = asyncio.run(scenario())

    assert collected.files_removed == 0
    assert rig.row(asset_id)["local_state"] == "stored"
    assert blob.read_bytes() == PNG
    read = rig.client.get(f"/api/v2/assets/{asset_id}", headers=AUTH)
    assert read.status_code == 200 and read.content == PNG
    rig.assert_nothing_held()


def test_a_retention_pass_after_a_cancel_during_an_adoption_keeps_the_adopted_blob(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    asset_id = rig.upload()
    rig.expire(asset_id)
    with contextlib.closing(sqlite3.connect(rig.state.database_path)) as connection:
        connection.execute(
            "INSERT INTO asset_refs VALUES (?,?,?,?,?,?)",
            (SCOPE, asset_id, "command", "command-1", "attachment:0", REMOVED_AT),
        )
        connection.commit()
    # The sent bytes, two days old (an expiry whose unlink failed, or a relink that died): old
    # enough for the stale-blob pass.
    blob = rig.blob(asset_id)
    blob.write_bytes(PNG)
    two_days_ago = time.time() - 2 * 86400
    os.utime(blob, (two_days_ago, two_days_ago))
    entered, release = threading.Event(), threading.Event()
    restore = rig.state.store.restore_expired_asset

    def held_restore(row: Any, created_at: str) -> bool:
        # The adoption's restore write is under way when the route deadline's cancel lands.
        entered.set()
        assert release.wait(HANG_GUARD_S)
        return restore(row, created_at)

    monkeypatch.setattr(rig.state.store, "restore_expired_asset", held_restore)

    async def body() -> AsyncIterator[bytes]:
        yield PNG

    async def scenario() -> assets.AssetCollection:
        task = asyncio.ensure_future(rig.service.relink(asset_id, body(), declared_bytes=len(PNG)))
        assert await asyncio.to_thread(entered.wait, HANG_GUARD_S)
        task.cancel()
        try:
            # The hourly pass, while the cancelled relink's restore write still runs.
            return await rig.service.collect_due()
        finally:
            release.set()
            done, _ = await asyncio.wait({task}, timeout=HANG_GUARD_S)
            assert task in done and task.cancelled()

    collected = asyncio.run(scenario())

    # The write committed, so the row is stored and must keep the bytes it adopted.
    assert collected.files_removed == 0
    assert rig.row(asset_id)["local_state"] == "stored"
    assert blob.read_bytes() == PNG
    read = rig.client.get(f"/api/v2/assets/{asset_id}", headers=AUTH)
    assert read.status_code == 200 and read.content == PNG
    assert rig.service._relinking == set()
    rig.assert_nothing_held()


def test_a_second_relink_of_the_same_image_is_refused_while_the_first_runs(rig: Rig) -> None:
    asset_id = rig.upload()
    rig.expire(asset_id)

    async def body() -> AsyncIterator[bytes]:
        yield PNG

    async def unread() -> AsyncIterator[bytes]:
        raise AssertionError("the body was read")
        yield b""

    async def scenario() -> str:
        entered, release = asyncio.Event(), asyncio.Event()

        async def held_decoder(_path: Path) -> DecodedImage:
            entered.set()
            await release.wait()
            return DecodedImage("image/png", 1, 1)

        rig.service._decoder = held_decoder
        first = asyncio.ensure_future(rig.service.relink(asset_id, body(), declared_bytes=len(PNG)))
        await asyncio.wait_for(entered.wait(), HANG_GUARD_S)
        with pytest.raises(AssetError) as refused:
            await rig.service.relink(asset_id, unread(), declared_bytes=None)
        assert rig.service._reserved_count == 1  # the first relink's reservation only
        release.set()
        await asyncio.wait_for(first, HANG_GUARD_S)
        return refused.value.code

    assert asyncio.run(scenario()) == "busy"
    assert _error(AssetError("busy")).status_code == 503
    assert rig.row(asset_id)["local_state"] == "stored"
    assert rig.blob(asset_id).read_bytes() == PNG
    assert rig.service._relinking == set()
    rig.assert_nothing_held()


def _stale_leftover(rig: Rig) -> tuple[str, Path]:
    """An expired, still-referenced image whose sent bytes a relink that died left two days ago."""
    asset_id = rig.upload()
    rig.expire(asset_id)
    with contextlib.closing(sqlite3.connect(rig.state.database_path)) as connection:
        connection.execute(
            "INSERT INTO asset_refs VALUES (?,?,?,?,?,?)",
            (SCOPE, asset_id, "command", "command-1", "attachment:0", REMOVED_AT),
        )
        connection.commit()
    blob = rig.blob(asset_id)
    blob.write_bytes(PNG)
    two_days_ago = time.time() - 2 * 86400
    os.utime(blob, (two_days_ago, two_days_ago))
    return asset_id, blob


def test_a_relink_that_commits_during_the_stale_blob_check_keeps_the_adopted_blob(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    asset_id, blob = _stale_leftover(rig)
    metadata = rig.service._metadata

    async def body() -> AsyncIterator[bytes]:
        yield PNG

    async def scenario() -> assets.AssetCollection:
        entered, release = asyncio.Event(), asyncio.Event()

        async def held_decoder(_path: Path) -> DecodedImage:
            entered.set()
            await release.wait()
            return DecodedImage("image/png", 1, 1)

        rig.service._decoder = held_decoder
        relinking = asyncio.ensure_future(
            rig.service.relink(asset_id, body(), declared_bytes=len(PNG))
        )
        await asyncio.wait_for(entered.wait(), HANG_GUARD_S)

        async def read_then_let_the_relink_commit(checked_id: str) -> Any:
            row = await metadata(checked_id)
            if checked_id == asset_id:
                # The pass has read the row as expired; the relink now adopts the blob and ends
                # before the pass decides whether to unlink it.
                release.set()
                await asyncio.wait_for(relinking, HANG_GUARD_S)
            return row

        monkeypatch.setattr(rig.service, "_metadata", read_then_let_the_relink_commit)
        try:
            return await rig.service.collect_due()
        finally:
            release.set()
            await asyncio.wait_for(relinking, HANG_GUARD_S)

    collected = asyncio.run(scenario())

    assert collected.files_removed == 0
    assert rig.row(asset_id)["local_state"] == "stored"
    assert blob.read_bytes() == PNG
    read = rig.client.get(f"/api/v2/assets/{asset_id}", headers=AUTH)
    assert read.status_code == 200 and read.content == PNG
    assert rig.service._relinking == set()
    rig.assert_nothing_held()


def test_a_relink_that_starts_while_the_stale_blob_check_holds_the_image_is_busy(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    asset_id, blob = _stale_leftover(rig)
    metadata = rig.service._metadata
    attempts: list[str] = []
    refusals: list[str] = []

    async def unread() -> AsyncIterator[bytes]:
        raise AssertionError("the body was read")
        yield b""

    async def read_then_relink(checked_id: str) -> Any:
        row = await metadata(checked_id)
        if checked_id == asset_id and not attempts:
            attempts.append(checked_id)
            with pytest.raises(AssetError) as refused:
                await rig.service.relink(asset_id, unread(), declared_bytes=None)
            refusals.append(refused.value.code)
            # The refused relink reserved nothing and left the blob to the pass's decision.
            assert rig.service._reserved_count == 0
            assert blob.read_bytes() == PNG
        return row

    monkeypatch.setattr(rig.service, "_metadata", read_then_relink)
    collected = asyncio.run(rig.service.collect_due())

    assert refusals == ["busy"]
    # The row is still expired, so the pass removes the stale leftover itself.
    assert collected.files_removed == 1
    assert rig.row(asset_id)["local_state"] == "expired"
    assert not blob.exists()
    assert rig.service._relinking == set()
    rig.assert_nothing_held()
