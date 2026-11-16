"""Avatar service tests use only synthetic files below pytest's temporary root."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import sys
import threading
import uuid
from collections.abc import AsyncIterator, Iterator, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from viki_agent import compiled, protection, winproc
from viki_agent.app.settings import SettingsService
from viki_agent.media import avatars, media_worker
from viki_agent.media.assets import AssetDownload, AssetError, AssetService
from viki_agent.media.avatars import VARIANT_SIZES, Avatar, AvatarError, AvatarService, Crop
from viki_agent.state import Prefs
from viki_agent.storage import SessionStore, StateError

PNG = b"\x89PNG\r\n\x1a\nsynthetic-avatar"
OUTPUT_NAMES = ("master.png", "24.png", "32.png", "64.png", "128.png", "256.png")
OLD = datetime(2026, 9, 19, tzinfo=UTC)
# Bounds a wait on an event that normally fires within milliseconds; never real work.
HANG_GUARD_S = 5


class FakeAssets:
    def __init__(self, body: bytes = b"source") -> None:
        self.body = body
        self.downloads = 0
        self.removed: list[str] = []

    async def download(self, asset_id: str) -> AssetDownload:
        self.downloads += 1
        if asset_id in self.removed:
            raise AssetError("not_found")

        async def chunks() -> AsyncIterator[bytes]:
            yield self.body

        return AssetDownload("image/png", len(self.body), chunks())

    async def remove(self, asset_id: str) -> bool:
        self.removed.append(asset_id)
        return True


class FakeWorker:
    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, stage: Path, crop: Crop, rotation: int) -> Mapping[str, object]:
        del crop, rotation
        self.calls += 1
        files: list[dict[str, object]] = []
        for name, size in (("master.png", 64), *((f"{n}.png", n) for n in VARIANT_SIZES)):
            data = PNG + name.encode("ascii")
            (stage / name).write_bytes(data)
            files.append(
                {
                    "name": name,
                    "size": size,
                    "byte_size": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                }
            )
        return {"ok": True, "master": files[0], "variants": files[1:]}


def _off_loop(_path: Path) -> None:
    # A protection check is a Win32 ACL read; it must never block the event loop's thread.
    assert threading.current_thread() is not threading.main_thread()


@pytest.fixture
def service_parts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[AvatarService, SessionStore, FakeAssets, FakeWorker]:
    store = SessionStore(tmp_path / "state.sqlite3", lambda: {})
    monkeypatch.setattr(protection, "require_private_directory", _off_loop)
    monkeypatch.setattr(protection, "require_private_file", _off_loop)
    monkeypatch.setattr(
        protection,
        "create_private_directory",
        lambda path: (path.mkdir(), True)[1] if not path.exists() else False,
    )
    monkeypatch.setattr(avatars, "require_private_store", lambda _store: None)
    parent = tmp_path / "assets"
    parent.mkdir()
    source = FakeAssets()
    worker = FakeWorker()
    service = AvatarService(parent / "avatars", store, cast(AssetService, source), worker=worker)
    return service, store, source, worker


def _id() -> str:
    return str(uuid.uuid4())


def _crop() -> dict[str, object]:
    return {"x": 0, "y": 0, "width": 64, "height": 64}


def _count(store: SessionStore, table: str) -> int:
    with store._connection() as connection:
        return connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def _orphan(parent: Path, name: str | None = None) -> Path:
    directory = parent / (name or _id())
    directory.mkdir()
    for name in OUTPUT_NAMES:
        (directory / name).write_bytes(PNG)
    os.utime(directory, (OLD.timestamp(), OLD.timestamp()))
    return directory


async def test_transform_publishes_six_verified_files_and_downloads_variant(
    service_parts: tuple[AvatarService, SessionStore, FakeAssets, FakeWorker],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, store, source, worker = service_parts
    avatar_id, source_id = _id(), _id()

    result = await service.transform(avatar_id, source_id, _crop(), 90)

    assert result.avatar_id == avatar_id
    assert [item.size for item in result.variants] == list(VARIANT_SIZES)
    assert source.removed == []
    assert worker.calls == 1
    directory = service.root / "blobs" / avatar_id
    assert {path.name for path in directory.iterdir()} == set(OUTPUT_NAMES)
    assert _count(store, "avatars") == 1
    assert _count(store, "avatar_variants") == 5

    def second_read(_path: Path) -> bytes:
        raise AssertionError("the served bytes are the verified bytes, never a second read")

    monkeypatch.setattr(Path, "read_bytes", second_read)
    downloaded = await service.download(avatar_id, 64)
    assert downloaded.body == PNG + b"64.png"
    assert downloaded.byte_size == len(downloaded.body)


async def test_exact_retry_reuses_commit_and_changed_request_conflicts(
    service_parts: tuple[AvatarService, SessionStore, FakeAssets, FakeWorker],
) -> None:
    service, _store, source, worker = service_parts
    avatar_id, source_id = _id(), _id()
    first = await service.transform(avatar_id, source_id, _crop(), 0)

    assert await service.transform(avatar_id, source_id, _crop(), 0) == first
    assert worker.calls == 1
    assert source.downloads == 1
    with pytest.raises(AvatarError, match="command_conflict"):
        await service.transform(
            avatar_id, source_id, {"x": 1, "y": 0, "width": 64, "height": 64}, 0
        )


async def test_invalid_requests_and_large_source_never_start_worker(
    service_parts: tuple[AvatarService, SessionStore, FakeAssets, FakeWorker],
) -> None:
    service, _store, source, worker = service_parts
    with pytest.raises(AvatarError, match="invalid_crop"):
        await service.transform(_id(), _id(), {"x": 0, "y": 0, "width": True, "height": 1}, 0)
    with pytest.raises(AvatarError, match="invalid_rotation"):
        await service.transform(_id(), _id(), _crop(), 45)
    # media_worker's argv bound is nine decimal digits.
    with pytest.raises(AvatarError, match="invalid_crop"):
        await service.transform(
            _id(), _id(), {"x": 1_000_000_000, "y": 0, "width": 64, "height": 64}, 0
        )
    assert source.downloads == 0
    source.body = b"x" * (avatars.AVATAR_SOURCE_BYTES + 1)
    with pytest.raises(AvatarError, match="too_large"):
        await service.transform(_id(), _id(), _crop(), 0)
    assert worker.calls == 0
    assert source.removed == []


async def test_manifest_corruption_cleans_only_owned_stage(
    service_parts: tuple[AvatarService, SessionStore, FakeAssets, FakeWorker],
) -> None:
    service, store, source, _worker = service_parts

    async def corrupt(stage: Path, _crop: Crop, _rotation: int) -> Mapping[str, object]:
        (stage / "unexpected").write_bytes(b"keep")
        return {"ok": True, "master": {}, "variants": []}

    service._worker = corrupt
    with pytest.raises(AvatarError, match="transform_failed"):
        await service.transform(_id(), _id(), _crop(), 0)
    assert _count(store, "avatars") == 0
    assert source.removed == []
    stages = list((service.root / "incoming").iterdir())
    assert len(stages) == 1
    assert (stages[0] / "unexpected").read_bytes() == b"keep"


async def test_raced_target_is_preserved_as_conflict(
    service_parts: tuple[AvatarService, SessionStore, FakeAssets, FakeWorker],
) -> None:
    service, store, _source, worker = service_parts
    avatar_id = _id()

    async def race(stage: Path, crop: Crop, rotation: int) -> Mapping[str, object]:
        answer = await worker(stage, crop, rotation)
        target = service.root / "blobs" / avatar_id
        target.mkdir()
        (target / "marker").write_bytes(b"external")
        return answer

    service._worker = race
    with pytest.raises(AvatarError, match="command_conflict"):
        await service.transform(avatar_id, _id(), _crop(), 0)
    assert (service.root / "blobs" / avatar_id / "marker").read_bytes() == b"external"
    assert _count(store, "avatars") == 0


async def test_target_raced_into_the_rename_is_preserved_as_conflict(
    service_parts: tuple[AvatarService, SessionStore, FakeAssets, FakeWorker],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, store, _source, _worker = service_parts
    avatar_id = _id()
    target = service.root / "blobs" / avatar_id
    rename = avatars.osfs.rename_no_replace

    def raced(source: Path, destination: Path) -> None:
        target.mkdir()
        (target / "marker").write_bytes(b"external")
        rename(source, destination)

    monkeypatch.setattr(avatars.osfs, "rename_no_replace", raced)
    with pytest.raises(AvatarError, match="command_conflict"):
        await service.transform(avatar_id, _id(), _crop(), 0)
    assert [path.name for path in target.iterdir()] == ["marker"]
    assert list((service.root / "incoming").iterdir()) == []
    assert _count(store, "avatars") == 0


@pytest.mark.parametrize("tamper", ["extra_file", "changed_bytes"])
async def test_published_directory_is_verified_before_the_row_commit(
    service_parts: tuple[AvatarService, SessionStore, FakeAssets, FakeWorker],
    tamper: str,
) -> None:
    service, store, _source, _worker = service_parts
    avatar_id = _id()
    publish = AvatarService._publish_directory

    def publish_then_tamper(stage: Path, final: Path) -> None:
        publish(stage, final)
        if tamper == "extra_file":
            (final / "extra").write_bytes(b"x")
        else:
            (final / "64.png").write_bytes(PNG + b"64.pnX")

    service._publish_directory = publish_then_tamper
    with pytest.raises(AvatarError, match="transform_failed"):
        await service.transform(avatar_id, _id(), _crop(), 0)
    # The cleanup removes only this call's own files: a foreign name keeps the directory.
    assert (service.root / "blobs" / avatar_id).is_dir() is (tamper == "extra_file")
    assert _count(store, "avatars") == 0
    with pytest.raises(AvatarError, match="not_found"):
        await service.download(avatar_id, 64)


async def test_cancellation_reaps_operation_and_cleans_stage(
    service_parts: tuple[AvatarService, SessionStore, FakeAssets, FakeWorker],
) -> None:
    service, _store, source, _worker = service_parts
    entered = asyncio.Event()

    async def blocked(_stage: Path, _crop: Crop, _rotation: int) -> Mapping[str, object]:
        entered.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    service._worker = blocked
    task = asyncio.create_task(service.transform(_id(), _id(), _crop(), 0))
    waiter = asyncio.create_task(entered.wait())
    # Whichever finishes first: a transform that fails before the worker fails fast here.
    await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
    waiter.cancel()
    assert entered.is_set(), task
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert list((service.root / "incoming").iterdir()) == []
    assert source.removed == []


async def test_cancel_during_stage_creation_still_removes_the_stage(
    service_parts: tuple[AvatarService, SessionStore, FakeAssets, FakeWorker],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A route deadline or a disconnect can cancel while the thread is creating the stage;
    # the directory it goes on to create must not wait for the orphan pass.
    service, _store, _source, worker = service_parts
    await service._ready()
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    gate = threading.Event()

    def gated_create(path: Path) -> bool:
        path.mkdir()
        loop.call_soon_threadsafe(entered.set)
        gate.wait(HANG_GUARD_S)
        return True

    monkeypatch.setattr(protection, "create_private_directory", gated_create)
    task = asyncio.create_task(service.transform(_id(), _id(), _crop(), 0))
    async with asyncio.timeout(HANG_GUARD_S):
        await entered.wait()
    task.cancel()
    gate.set()
    with pytest.raises(asyncio.CancelledError):
        async with asyncio.timeout(HANG_GUARD_S):
            await task
    assert list((service.root / "incoming").iterdir()) == []
    assert worker.calls == 0


async def test_cancel_during_stage_creation_keeps_a_directory_it_did_not_create(
    service_parts: tuple[AvatarService, SessionStore, FakeAssets, FakeWorker],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _store, _source, _worker = service_parts
    await service._ready()
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    gate = threading.Event()

    def refused_create(path: Path) -> bool:
        path.mkdir()  # stands in for a directory some other writer raced in
        loop.call_soon_threadsafe(entered.set)
        gate.wait(HANG_GUARD_S)
        return False

    monkeypatch.setattr(protection, "create_private_directory", refused_create)
    task = asyncio.create_task(service.transform(_id(), _id(), _crop(), 0))
    async with asyncio.timeout(HANG_GUARD_S):
        await entered.wait()
    task.cancel()
    gate.set()
    with pytest.raises(asyncio.CancelledError):
        async with asyncio.timeout(HANG_GUARD_S):
            await task
    assert len(list((service.root / "incoming").iterdir())) == 1


async def test_cancel_during_a_stage_step_waits_for_the_step_then_removes_the_stage(
    service_parts: tuple[AvatarService, SessionStore, FakeAssets, FakeWorker],
) -> None:
    # A thread cannot be stopped, so the cleanup must never run under a step still using the
    # stage: it would race the step's own reads and writes.
    service, store, _source, worker = service_parts
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    gate = threading.Event()
    ended = threading.Event()
    verify = AvatarService._verify_output

    def gated_verify(path: Path, expected: avatars.AvatarFile) -> bytes:
        loop.call_soon_threadsafe(entered.set)
        gate.wait(HANG_GUARD_S)
        try:
            return verify(path, expected)
        finally:
            ended.set()

    service._verify_output = gated_verify
    task = asyncio.create_task(service.transform(_id(), _id(), _crop(), 0))
    async with asyncio.timeout(HANG_GUARD_S):
        await entered.wait()
    task.cancel()
    done, _pending = await asyncio.wait({task}, timeout=0.05)
    assert not done
    gate.set()
    with pytest.raises(asyncio.CancelledError):
        async with asyncio.timeout(HANG_GUARD_S):
            await task
    assert ended.is_set()
    assert list((service.root / "incoming").iterdir()) == []
    assert _count(store, "avatars") == 0
    assert worker.calls == 1


async def test_cancel_during_an_insert_that_commits_keeps_the_directory_and_the_row(
    service_parts: tuple[AvatarService, SessionStore, FakeAssets, FakeWorker],
) -> None:
    service, store, _source, _worker = service_parts
    avatar_id = _id()
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    gate = threading.Event()
    insert = service._insert

    def insert_then_block(avatar: Avatar, request_hash: str) -> None:
        insert(avatar, request_hash)
        loop.call_soon_threadsafe(entered.set)
        gate.wait(HANG_GUARD_S)

    service._insert = insert_then_block
    task = asyncio.create_task(service.transform(avatar_id, _id(), _crop(), 0))
    async with asyncio.timeout(HANG_GUARD_S):
        await entered.wait()
    task.cancel()
    gate.set()
    with pytest.raises(asyncio.CancelledError):
        async with asyncio.timeout(HANG_GUARD_S):
            await task
    # The row committed, so it names the directory: removing it would strand the row.
    assert (service.root / "blobs" / avatar_id).is_dir()
    assert _count(store, "avatars") == 1
    assert (await service.download(avatar_id, 64)).body == PNG + b"64.png"


async def test_collection_preserves_referenced_rows_and_removes_safe_orphans(
    service_parts: tuple[AvatarService, SessionStore, FakeAssets, FakeWorker],
) -> None:
    service, store, _source, _worker = service_parts
    unreferenced, referenced = _id(), _id()
    await service.transform(unreferenced, _id(), _crop(), 0)
    await service.transform(referenced, _id(), _crop(), 0)
    old = "2026-09-19T00:00:00.000Z"
    with store._connection(write=True) as connection:
        connection.execute("UPDATE avatars SET created_at=?", (old,))
        connection.execute(
            "INSERT INTO profile_avatar_refs VALUES (?,?,?,?)", (_id(), 1, referenced, old)
        )
    orphan = _orphan(service.root / "blobs")

    result = await service.collect_due(now=datetime(2026, 9, 21, tzinfo=UTC))

    assert result.rows_removed == 1
    assert result.directories_removed == 2
    assert not (service.root / "blobs" / unreferenced).exists()
    assert (service.root / "blobs" / referenced).is_dir()
    assert not orphan.exists()
    assert _count(store, "avatars") == 1
    assert _count(store, "avatar_variants") == 5


async def test_collection_stops_at_its_removal_limit(
    service_parts: tuple[AvatarService, SessionStore, FakeAssets, FakeWorker],
) -> None:
    service, _store, _source, _worker = service_parts
    await service._ready()
    orphans = [
        _orphan(service.root / "blobs"),
        _orphan(service.root / "blobs"),
        _orphan(service.root / "incoming"),
    ]

    result = await service.collect_due(now=datetime(2026, 9, 21, tzinfo=UTC), limit=2)

    assert result == avatars.AvatarCollection(0, 2)
    assert sum(path.exists() for path in orphans) == 1


async def test_database_failure_after_rename_removes_the_directory_for_an_exact_retry(
    service_parts: tuple[AvatarService, SessionStore, FakeAssets, FakeWorker],
) -> None:
    service, store, _source, _worker = service_parts
    avatar_id, source_id = _id(), _id()
    insert = service._insert

    def fail(avatar: Avatar, request_hash: str) -> None:
        del avatar, request_hash
        raise StateError("unavailable")

    service._insert = fail
    with pytest.raises(AvatarError, match="unavailable"):
        await service.transform(avatar_id, source_id, _crop(), 0)
    assert not (service.root / "blobs" / avatar_id).exists()
    assert list((service.root / "incoming").iterdir()) == []
    assert _count(store, "avatars") == 0
    with pytest.raises(AvatarError, match="not_found"):
        await service.download(avatar_id, 64)

    service._insert = insert
    assert (await service.transform(avatar_id, source_id, _crop(), 0)).avatar_id == avatar_id
    assert _count(store, "avatars") == 1


async def test_worker_argv_is_the_shape_media_worker_parses(
    service_parts: tuple[AvatarService, SessionStore, FakeAssets, FakeWorker],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _store, _source, _worker = service_parts
    captured: list[str] = []

    async def refuse(argv: list[str], **_options: object) -> None:
        captured.extend(argv)
        raise OSError

    monkeypatch.setattr(winproc, "spawn", refuse)
    with pytest.raises(AvatarError, match="worker_unavailable"):
        await service._transform_in_worker(tmp_path, Crop(3, 5, 64, 64), 90)
    worker = str(Path(avatars.__file__).with_name("media_worker.py"))
    assert captured == [
        sys.executable,
        "-I",
        "-B",
        worker,
        *media_worker._worker_argv_prefix(sys.platform, avatars.WORKER_ADDRESS_SPACE_BYTES),
        "source",
        "--avatar",
        "3",
        "5",
        "64",
        "90",
    ]

    parsed: list[tuple[object, ...]] = []

    def record(*arguments: object) -> dict[str, object]:
        parsed.append(arguments)
        return {"ok": True}

    (tmp_path / "source").write_bytes(b"x")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(media_worker, "_avatar", record)
    monkeypatch.setattr(media_worker, "_answer", lambda _body: None)
    limits: list[tuple[object, ...]] = []
    if sys.platform == "linux":
        import resource

        def record_limit(*arguments: object) -> None:
            limits.append(arguments)

        monkeypatch.setattr(resource, "setrlimit", record_limit)
    monkeypatch.setattr(sys, "argv", captured[3:])
    assert media_worker.main() == 0
    assert parsed == [(Path("source"), 3, 5, 64, 90)]
    if sys.platform == "linux":
        import resource

        assert limits == [
            (
                resource.RLIMIT_AS,
                (avatars.WORKER_ADDRESS_SPACE_BYTES, avatars.WORKER_ADDRESS_SPACE_BYTES),
            )
        ]
    else:
        assert limits == []


async def test_linux_worker_gets_768_mib_address_space_and_the_job_keeps_384_mib(
    service_parts: tuple[AvatarService, SessionStore, FakeAssets, FakeWorker],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _store, _source, _worker = service_parts
    captured: list[str] = []
    options: dict[str, object] = {}

    async def refuse(argv: list[str], **spawn_options: object) -> None:
        captured.extend(argv)
        options.update(spawn_options)
        raise OSError

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(winproc, "spawn", refuse)
    with pytest.raises(AvatarError, match="worker_unavailable"):
        await service._transform_in_worker(tmp_path, Crop(3, 5, 64, 64), 90)
    worker_at = captured.index(str(Path(avatars.__file__).with_name("media_worker.py")))
    assert captured[worker_at + 1 : worker_at + 3] == ["--address-space", str(768 << 20)]
    assert options["memory_limit_bytes"] == 384 << 20


async def test_transform_in_worker_spawns_the_compiled_binarys_subcommand(
    service_parts: tuple[AvatarService, SessionStore, FakeAssets, FakeWorker],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = service_parts[0]
    captured: list[str] = []

    async def refuse(argv: list[str], **_options: object) -> None:
        captured.extend(argv)
        raise OSError

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(compiled, "COMPILED", True)
    install_dir = tmp_path / "install"
    monkeypatch.setattr(compiled, "INSTALL_DIR", install_dir)
    monkeypatch.setattr(winproc, "spawn", refuse)

    with pytest.raises(AvatarError, match="worker_unavailable"):
        await service._transform_in_worker(tmp_path, Crop(3, 5, 64, 64), 90)

    assert captured == [
        str(install_dir / compiled.APP_BINARY),
        "--viki-worker",
        "media",
        "--address-space",
        str(avatars.WORKER_ADDRESS_SPACE_BYTES),
        "source",
        "--avatar",
        "3",
        "5",
        "64",
        "90",
    ]


async def test_pre_rename_failure_keeps_the_source_for_the_exact_retry(
    service_parts: tuple[AvatarService, SessionStore, FakeAssets, FakeWorker],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, store, source, _worker = service_parts
    avatar_id, source_id = _id(), _id()
    rename = avatars.osfs.rename_no_replace
    refusals = [PermissionError()]

    def refuse_once(stage: Path, final: Path) -> None:
        if refusals:
            raise refusals.pop()
        rename(stage, final)

    monkeypatch.setattr(avatars.osfs, "rename_no_replace", refuse_once)
    with pytest.raises(AvatarError, match="unavailable"):
        await service.transform(avatar_id, source_id, _crop(), 0)
    assert list((service.root / "incoming").iterdir()) == []

    result = await service.transform(avatar_id, source_id, _crop(), 0)
    assert result.avatar_id == avatar_id
    assert source.removed == []
    assert _count(store, "avatars") == 1


async def test_post_rename_failure_frees_its_id_so_the_exact_retry_reuses_the_source(
    service_parts: tuple[AvatarService, SessionStore, FakeAssets, FakeWorker],
) -> None:
    service, store, source, _worker = service_parts
    avatar_id, source_id = _id(), _id()
    insert = service._insert
    refusals = [StateError("busy")]

    def busy_once(avatar: Avatar, request_hash: str) -> None:
        if refusals:
            raise refusals.pop()
        insert(avatar, request_hash)

    service._insert = busy_once
    with pytest.raises(AvatarError, match="busy"):
        await service.transform(avatar_id, source_id, _crop(), 0)
    # No row names the renamed directory, so the failure removed it and the ID is free again.
    assert not (service.root / "blobs" / avatar_id).exists()
    assert (await service.transform(avatar_id, source_id, _crop(), 0)).avatar_id == avatar_id
    assert source.removed == []
    assert _count(store, "avatars") == 1


async def test_missing_or_corrupt_stored_variant_reads_as_unavailable_and_keeps_the_row(
    service_parts: tuple[AvatarService, SessionStore, FakeAssets, FakeWorker],
) -> None:
    service, store, _source, _worker = service_parts
    avatar_id, source_id = _id(), _id()
    await service.transform(avatar_id, source_id, _crop(), 0)
    directory = service.root / "blobs" / avatar_id
    (directory / "32.png").unlink()
    (directory / "24.png").write_bytes(PNG + b"24.pnX")

    for size in (32, 24):
        with pytest.raises(AvatarError, match="unavailable"):
            await service.download(avatar_id, size)
    with pytest.raises(AvatarError, match="unavailable"):
        await service.transform(avatar_id, source_id, _crop(), 0)
    assert _count(store, "avatars") == 1


class FakeProcess:
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


class FakeChild:
    def __init__(self) -> None:
        self.process = FakeProcess()
        self.released = False

    def kill_tree(self) -> None:
        self.process.killed.set()

    def release(self) -> None:
        self.released = True


@pytest.fixture
def fake_child(monkeypatch: pytest.MonkeyPatch) -> FakeChild:
    child = FakeChild()

    async def spawn(*_args: object, **_kwargs: object) -> FakeChild:
        return child

    monkeypatch.setattr(winproc, "spawn", spawn)
    return child


async def test_cancelled_worker_is_killed_reaped_and_released(
    service_parts: tuple[AvatarService, SessionStore, FakeAssets, FakeWorker],
    fake_child: FakeChild,
    tmp_path: Path,
) -> None:
    service = service_parts[0]
    task = asyncio.create_task(service._transform_in_worker(tmp_path, Crop(0, 0, 64, 64), 0))
    async with asyncio.timeout(HANG_GUARD_S):
        await fake_child.process.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        async with asyncio.timeout(HANG_GUARD_S):
            await task
    assert fake_child.process.killed.is_set()
    assert fake_child.process.reaped and fake_child.released


class ExitedProcess:
    """A worker that has already finished with ``returncode`` and printed nothing."""

    def __init__(self, returncode: int) -> None:
        self.returncode: int | None = returncode
        self.stdin = SimpleNamespace(close=lambda: None)
        self.stdout = SimpleNamespace(read=self._eof)
        self.stderr = SimpleNamespace(read=self._eof)

    async def _eof(self, _size: int = -1) -> bytes:
        return b""

    async def wait(self) -> int:
        assert self.returncode is not None
        return self.returncode


@pytest.mark.parametrize(
    "returncode, code", [(3, "decoder_unavailable"), (4, "invalid_crop"), (1, "transform_failed")]
)
async def test_worker_exit_code_maps_to_its_error_and_the_log_holds_the_integer_only(
    service_parts: tuple[AvatarService, SessionStore, FakeAssets, FakeWorker],
    fake_child: FakeChild,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    returncode: int,
    code: str,
) -> None:
    service = service_parts[0]
    fake_child.process = cast(FakeProcess, ExitedProcess(returncode))
    with (
        caplog.at_level("WARNING", logger="viki_agent.media.avatars"),
        pytest.raises(AvatarError) as raised,
    ):
        await service._transform_in_worker(tmp_path, Crop(7, 11, 64, 64), 90)
    assert raised.value.code == code
    assert [record.getMessage() for record in caplog.records] == [
        f"avatar worker exited: {returncode}"
    ]
    assert fake_child.released


async def test_worker_timeout_kills_reaps_and_releases(
    service_parts: tuple[AvatarService, SessionStore, FakeAssets, FakeWorker],
    fake_child: FakeChild,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = service_parts[0]
    monkeypatch.setattr(avatars, "WORKER_TIMEOUT_SECONDS", 0.01)
    with pytest.raises(AvatarError, match="worker_timeout"):
        async with asyncio.timeout(HANG_GUARD_S):
            await service._transform_in_worker(tmp_path, Crop(0, 0, 64, 64), 0)
    assert fake_child.process.killed.is_set()
    assert fake_child.process.reaped and fake_child.released


async def test_collection_scan_budget_bounds_a_pass(
    service_parts: tuple[AvatarService, SessionStore, FakeAssets, FakeWorker],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _store, _source, _worker = service_parts
    await service._ready()
    blobs = service.root / "blobs"
    real_scandir = os.scandir

    def sorted_scandir(path: Path) -> contextlib.AbstractContextManager[Iterator[os.DirEntry]]:
        with real_scandir(path) as entries:
            listing = list(entries)
        return contextlib.nullcontext(iter(sorted(listing, key=lambda e: e.name)))

    monkeypatch.setattr(os, "scandir", sorted_scandir)
    # This test pins name order because the scan order differs per filesystem: this foreign
    # entry, which no pass removes, comes first.
    (blobs / "0-foreign").write_bytes(b"keep")
    orphan = _orphan(blobs, "ffffffff-ffff-4fff-bfff-ffffffffffff")
    monkeypatch.setattr(avatars, "MAX_AVATARS", 0)

    result = await service.collect_due(now=datetime(2026, 9, 21, tzinfo=UTC), limit=1)

    assert result == avatars.AvatarCollection(0, 0)
    assert orphan.is_dir()


def _settings(store: SessionStore) -> SettingsService:
    return SettingsService(store, provider_defaults=Prefs, on_chat_defaults=lambda _prefs: None)


def _age(store: SessionStore, stamps: Mapping[str, str]) -> None:
    with store._connection(write=True) as connection:
        connection.executemany(
            "UPDATE avatars SET created_at=? WHERE avatar_id=?",
            [(stamp, avatar_id) for avatar_id, stamp in stamps.items()],
        )


async def test_collection_keeps_identity_and_profile_revision_avatars(
    service_parts: tuple[AvatarService, SessionStore, FakeAssets, FakeWorker],
) -> None:
    service, store, _source, _worker = service_parts
    user, assistant, current, older, archived, unreferenced = (_id() for _ in range(6))
    created = [
        await service.transform(avatar_id, _id(), _crop(), 0)
        for avatar_id in (user, assistant, current, older, archived, unreferenced)
    ]
    _settings(store).patch(
        _id(), 0, {"identity.user_avatar_id": user, "identity.assistant_avatar_id": assistant}
    )
    profile, archived_profile = _id(), _id()
    with store._connection(write=True) as connection:
        connection.executemany(
            "INSERT INTO profile_avatar_refs VALUES (?,?,?,?)",
            [
                (profile, 2, current, created[2].created_at),
                (profile, 1, older, created[3].created_at),
                (archived_profile, 1, archived, created[4].created_at),
            ],
        )
    later = max(datetime.fromisoformat(avatar.created_at) for avatar in created)

    result = await service.collect_due(now=later + timedelta(hours=25))

    assert result == avatars.AvatarCollection(1, 1)
    assert not (service.root / "blobs" / unreferenced).exists()
    for kept in (user, assistant, current, older, archived):
        assert (service.root / "blobs" / kept).is_dir()
    assert _count(store, "avatars") == 5
    assert _count(store, "avatar_variants") == 25


async def test_identity_avatars_neither_block_nor_widen_the_removal_limit(
    service_parts: tuple[AvatarService, SessionStore, FakeAssets, FakeWorker],
) -> None:
    service, store, _source, _worker = service_parts
    user, first, second, assistant = (_id() for _ in range(4))
    for avatar_id in (user, first, second, assistant):
        await service.transform(avatar_id, _id(), _crop(), 0)
    _settings(store).patch(
        _id(), 0, {"identity.user_avatar_id": user, "identity.assistant_avatar_id": assistant}
    )
    # The oldest candidate is kept, so the pass must look past it; the next two are both
    # removable, so the pass must still stop at its limit of one.
    _age(
        store,
        {
            user: "2026-09-19T00:00:00.000Z",
            first: "2026-09-19T00:00:01.000Z",
            second: "2026-09-19T00:00:02.000Z",
            assistant: "2026-09-19T00:00:03.000Z",
        },
    )

    result = await service.collect_due(now=datetime(2026, 9, 21, tzinfo=UTC), limit=1)

    assert result == avatars.AvatarCollection(1, 1)
    assert not (service.root / "blobs" / first).exists()
    for kept in (user, second, assistant):
        assert (service.root / "blobs" / kept).is_dir()


async def test_unreadable_settings_document_removes_no_avatar_row(
    service_parts: tuple[AvatarService, SessionStore, FakeAssets, FakeWorker],
) -> None:
    service, store, _source, _worker = service_parts
    avatar_id = _id()
    await service.transform(avatar_id, _id(), _crop(), 0)
    old = "2026-09-19T00:00:00.000Z"
    _age(store, {avatar_id: old})
    with store._connection(write=True) as connection:
        connection.execute(
            "INSERT INTO settings VALUES ('app',1,1,?,?)", ('{"identity.user_avatar_id":', old)
        )

    result = await service.collect_due(now=datetime(2026, 9, 21, tzinfo=UTC))

    assert result == avatars.AvatarCollection(0, 0)
    assert (service.root / "blobs" / avatar_id).is_dir()
    assert _count(store, "avatars") == 1


def test_retention_candidates_select_has_no_correlated_subquery(
    service_parts: tuple[AvatarService, SessionStore, FakeAssets, FakeWorker],
) -> None:
    # A correlated subquery re-runs once per outer row; the plain one below is a single list
    # scan, so a full store never turns retention into a per-row cost.
    _service, store, _source, _worker = service_parts
    with store._connection() as connection:
        plan = connection.execute(
            f"EXPLAIN QUERY PLAN {avatars._RETENTION_CANDIDATES}", ("2026-09-19T00:00:00.000Z", 1)
        ).fetchall()
    assert not any("CORRELATED" in str(row) for row in plan)


def _fill(store: SessionStore, count: int, total_bytes: int = 6) -> list[str]:
    ids = [_id() for _ in range(count)]
    with store._connection(write=True) as connection:
        connection.executemany(
            "INSERT INTO avatars VALUES (?,?,?,?,?,?,?)",
            [
                (avatar_id, "0" * 64, 1, 1, "0" * 64, total_bytes, "2026-09-27T00:00:00.000Z")
                for avatar_id in ids
            ],
        )
    return ids


@pytest.mark.parametrize("cap", ["count", "bytes"])
async def test_a_full_store_refuses_capacity_before_anything_is_published(
    service_parts: tuple[AvatarService, SessionStore, FakeAssets, FakeWorker],
    cap: str,
) -> None:
    service, store, _source, _worker = service_parts
    if cap == "count":
        _fill(store, avatars.MAX_AVATARS)
    else:
        # Full-size rows fill the byte cap exactly while the count stays far below its own.
        _fill(store, avatars.MAX_STORED_BYTES // avatars.TOTAL_BYTES, avatars.TOTAL_BYTES)
    published: list[Path] = []
    publish = AvatarService._publish_directory

    def recorded(stage: Path, final: Path) -> None:
        published.append(final)
        publish(stage, final)

    service._publish_directory = recorded
    avatar_id = _id()
    with pytest.raises(AvatarError, match="capacity"):
        await service.transform(avatar_id, _id(), _crop(), 0)
    assert published == []
    assert not (service.root / "blobs" / avatar_id).exists()
    assert list((service.root / "incoming").iterdir()) == []


async def test_the_count_cap_is_refused_before_any_download_or_worker_call(
    service_parts: tuple[AvatarService, SessionStore, FakeAssets, FakeWorker],
) -> None:
    service, store, source, worker = service_parts
    _fill(store, avatars.MAX_AVATARS)
    with pytest.raises(AvatarError, match="capacity"):
        await service.transform(_id(), _id(), _crop(), 0)
    assert source.downloads == 0
    assert worker.calls == 0


async def test_a_store_filled_after_the_check_removes_the_published_directory(
    service_parts: tuple[AvatarService, SessionStore, FakeAssets, FakeWorker],
) -> None:
    service, store, _source, _worker = service_parts
    _fill(store, avatars.MAX_AVATARS - 1)
    avatar_id, source_id = _id(), _id()
    publish = AvatarService._publish_directory
    raced: list[str] = []

    def publish_then_fill(stage: Path, final: Path) -> None:
        publish(stage, final)
        if not raced:
            raced.extend(_fill(store, 1))

    service._publish_directory = publish_then_fill
    with pytest.raises(AvatarError, match="capacity"):
        await service.transform(avatar_id, source_id, _crop(), 0)
    assert not (service.root / "blobs" / avatar_id).exists()
    assert list((service.root / "incoming").iterdir()) == []

    with store._connection(write=True) as connection:
        connection.execute("DELETE FROM avatars WHERE avatar_id=?", (raced[0],))
    result = await service.transform(avatar_id, source_id, _crop(), 0)
    assert result.avatar_id == avatar_id
    assert (service.root / "blobs" / avatar_id).is_dir()
    assert _count(store, "avatars") == avatars.MAX_AVATARS
