"""Asset and avatar publishes on POSIX: private stages, and a rename that never replaces.

Tests that check a mode set umask 022.
Synthetic bytes only; the decoder and the transform worker are fakes.
"""

from __future__ import annotations

import hashlib
import os
import stat
import sys
import uuid
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import cast

import pytest

from tests.persistence.test_storage_posix import _fsync_spy
from viki_agent import protection, storage
from viki_agent.media import assets, avatars
from viki_agent.media.assets import AssetDownload, AssetError, AssetService, DecodedImage
from viki_agent.media.avatars import VARIANT_SIZES, AvatarError, AvatarService, Crop
from viki_agent.storage import AssetRow, SessionStore

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX publishes only")

SCOPE = "b" * 64
PNG = b"\x89PNG\r\n\x1a\nsynthetic-publish"


def _private_parent(tmp_path: Path) -> Path:
    parent = tmp_path / "data"
    parent.mkdir(0o700)
    return parent


class _Rows:
    """The two store calls an upload makes."""

    def __init__(self) -> None:
        self.rows: dict[str, AssetRow] = {}

    def asset_usage(self, scope: str) -> tuple[int, int]:
        return len(self.rows), sum(row["byte_size"] for row in self.rows.values())

    def put_asset(self, row: AssetRow) -> None:
        self.rows[row["asset_id"]] = row


def _asset_service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[AssetService, _Rows]:
    monkeypatch.setattr(assets, "require_private_store", lambda _store: None)
    rows = _Rows()

    async def decoder(_path: Path) -> DecodedImage:
        return DecodedImage("image/png", 1, 1)

    service = AssetService(
        _private_parent(tmp_path) / "assets", cast(SessionStore, rows), SCOPE, decoder=decoder
    )
    return service, rows


async def _body() -> AsyncIterator[bytes]:
    yield PNG


@pytest.mark.usefixtures("desktop_umask")
async def test_an_uploaded_stage_and_blob_are_private(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert sys.platform != "win32"
    service, rows = _asset_service(tmp_path, monkeypatch)

    uploaded = await service.upload(_body(), name="sample.png", declared_bytes=len(PNG))
    # The embedded-image check stages its bytes the same way and must pass the same check.
    assert await service.inspect_bytes(PNG) == DecodedImage("image/png", 1, 1)

    blob = service.root / "blobs" / SCOPE / uploaded.asset_id
    assert set(rows.rows) == {uploaded.asset_id}
    assert stat.S_IMODE(os.lstat(blob).st_mode) == 0o600
    protection.require_private_file(blob)
    assert list((service.root / "incoming").iterdir()) == []


@pytest.mark.usefixtures("desktop_umask")
async def test_a_publish_over_an_existing_blob_is_refused_and_keeps_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert sys.platform != "win32"
    service, rows = _asset_service(tmp_path, monkeypatch)
    real_check_file = storage.check_file
    raced: list[Path] = []

    def check_then_race(path: Path) -> bool:
        # Another writer creates the blob right after the pre-rename check found none.
        exists = real_check_file(path)
        if path.parent.name == SCOPE and path.name != ".viki-asset-boundary":
            path.write_bytes(b"external")
            raced.append(path)
        return exists

    monkeypatch.setattr(assets.storage, "check_file", check_then_race)

    with pytest.raises(AssetError, match="unavailable"):
        await service.upload(_body(), name="sample.png", declared_bytes=len(PNG))

    assert len(raced) == 1
    assert raced[0].read_bytes() == b"external"
    assert rows.rows == {}
    assert list((service.root / "incoming").iterdir()) == []


class _Source:
    async def download(self, asset_id: str) -> AssetDownload:
        async def chunks() -> AsyncIterator[bytes]:
            yield b"synthetic source"

        return AssetDownload("image/png", len(b"synthetic source"), chunks())


async def _private_outputs(stage: Path, crop: Crop, rotation: int) -> Mapping[str, object]:
    del crop, rotation
    files: list[dict[str, object]] = []
    for name, size in (("master.png", 64), *((f"{n}.png", n) for n in VARIANT_SIZES)):
        data = PNG + name.encode("ascii")
        with open(os.open(stage / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as out:
            out.write(data)
        files.append(
            {
                "name": name,
                "size": size,
                "byte_size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
    return {"ok": True, "master": files[0], "variants": files[1:]}


async def test_an_avatar_publish_over_an_existing_directory_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert sys.platform != "win32"
    store = SessionStore(tmp_path / "state" / "state.sqlite3", lambda: {})
    monkeypatch.setattr(avatars, "require_private_store", lambda _store: None)
    avatar_id = str(uuid.uuid4())
    service = AvatarService(
        _private_parent(tmp_path) / "avatars",
        store,
        cast(AssetService, _Source()),
        worker=_private_outputs,
    )
    target = service.root / "blobs" / avatar_id
    real_lstat = Path.lstat

    def lstat_then_race(self: Path) -> os.stat_result:
        # Another writer makes an empty directory right after the pre-rename check found none.
        try:
            return real_lstat(self)
        except FileNotFoundError:
            if self == target:
                self.mkdir(0o700)
            raise

    monkeypatch.setattr(Path, "lstat", lstat_then_race)
    previous = os.umask(0o022)
    try:
        with pytest.raises(AvatarError, match="command_conflict"):
            await service.transform(
                avatar_id, str(uuid.uuid4()), {"x": 0, "y": 0, "width": 64, "height": 64}, 0
            )
    finally:
        os.umask(previous)

    assert target.is_dir()
    assert list(target.iterdir()) == []
    assert list((service.root / "incoming").iterdir()) == []
    with store._connection() as connection:
        assert connection.execute("SELECT count(*) FROM avatars").fetchone() == (0,)


async def test_an_upload_syncs_the_scope_blob_directory_after_its_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert sys.platform != "win32"
    service, _rows = _asset_service(tmp_path, monkeypatch)
    calls = _fsync_spy(monkeypatch)

    uploaded = await service.upload(_body(), name="sample.png", declared_bytes=len(PNG))

    blob = service.root / "blobs" / SCOPE / uploaded.asset_id
    assert calls == [("rename", blob), ("fsync", blob.parent)]


async def test_an_avatar_publish_syncs_the_blobs_directory_after_its_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert sys.platform != "win32"
    store = SessionStore(tmp_path / "state" / "state.sqlite3", lambda: {})
    monkeypatch.setattr(avatars, "require_private_store", lambda _store: None)
    avatar_id = str(uuid.uuid4())
    service = AvatarService(
        _private_parent(tmp_path) / "avatars",
        store,
        cast(AssetService, _Source()),
        worker=_private_outputs,
    )
    calls = _fsync_spy(monkeypatch)

    await service.transform(
        avatar_id, str(uuid.uuid4()), {"x": 0, "y": 0, "width": 64, "height": 64}, 0
    )

    final = service.root / "blobs" / avatar_id
    assert calls == [("rename", final), ("fsync", final.parent)]
