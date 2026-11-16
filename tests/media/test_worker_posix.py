"""The image worker's process environment and its Linux-only address-space ceiling.

Synthetic images only; the worker itself is the real one, run as a real process.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path
from typing import cast

import pytest

from viki_agent import winproc
from viki_agent.media import assets, media_worker
from viki_agent.media.assets import AssetError, AssetService
from viki_agent.storage import SessionStore

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX worker behaviour only")


class _NoStore:
    """AssetService's decoder never touches the store; only its type is asked for."""


async def test_a_png_decodes(tmp_path: Path) -> None:
    assert sys.platform != "win32"
    from PIL import Image

    Image.new("RGB", (4, 4), (10, 20, 30)).save(tmp_path / "source", format="PNG")
    worker = Path(media_worker.__file__)

    child = await winproc.spawn(
        [sys.executable, "-I", "-B", str(worker), "source"],
        cwd=tmp_path,
        env=media_worker._worker_env(sys.platform, os.environ),
    )
    try:
        stdout, _stderr = await child.process.communicate()
    finally:
        child.release()

    answer = json.loads(stdout)
    assert answer == {"ok": True, "media_type": "image/png", "width": 4, "height": 4}


async def test_worker_environment_holds_exactly_the_posix_names(tmp_path: Path) -> None:
    assert sys.platform != "win32"
    expected = sorted(
        {name for name in ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL") if name in os.environ}
        | {"PYTHONUTF8", "PYTHONNOUSERSITE"}
    )
    env = media_worker._worker_env(sys.platform, os.environ)
    assert sorted(env) == expected

    # /usr/bin/env, not a python -c script: CPython's own locale coercion (PEP 538) adds
    # LC_CTYPE to its own os.environ at startup, which would make a python dump lie about
    # what was actually execed.
    child = await winproc.spawn(["/usr/bin/env"], cwd=tmp_path, env=env)
    try:
        stdout, _stderr = await child.process.communicate()
    finally:
        child.release()

    names = sorted(line.split(b"=", 1)[0].decode() for line in stdout.splitlines() if line)
    assert names == expected


@pytest.mark.skipif(sys.platform != "linux", reason="the address-space ceiling is Linux only")
async def test_address_space_ceiling_fails_a_large_decode_that_succeeds_without_it(
    tmp_path: Path,
) -> None:
    assert sys.platform == "linux"
    from PIL import Image

    # 36,000,000 pixels: under _inspect's 40,000,000-pixel cap, but its RGBA buffer alone
    # (144 MB) cannot fit inside a 64 MiB address space.
    Image.new("RGBA", (6000, 6000), (10, 20, 30, 255)).save(tmp_path / "big", format="PNG")
    worker = Path(media_worker.__file__)
    env = media_worker._worker_env(sys.platform, os.environ)

    limited = await winproc.spawn(
        [sys.executable, "-I", "-B", str(worker), "--address-space", str(64 << 20), "big"],
        cwd=tmp_path,
        env=env,
    )
    try:
        limited_stdout, _stderr = await limited.process.communicate()
    finally:
        limited.release()
    unlimited = await winproc.spawn(
        [sys.executable, "-I", "-B", str(worker), "big"], cwd=tmp_path, env=env
    )
    try:
        unlimited_stdout, _stderr = await unlimited.process.communicate()
    finally:
        unlimited.release()

    assert json.loads(limited_stdout) == {"ok": False, "code": "decode_failed"}
    assert json.loads(unlimited_stdout)["ok"] is True


async def test_avatar_outputs_are_0600_under_umask_022(tmp_path: Path, desktop_umask: None) -> None:
    assert sys.platform != "win32"
    from PIL import Image

    Image.new("RGB", (8, 8), (10, 20, 30)).save(tmp_path / "source", format="PNG")
    worker = Path(media_worker.__file__)

    child = await winproc.spawn(
        [
            sys.executable,
            "-I",
            "-B",
            str(worker),
            "source",
            "--avatar",
            "0",
            "0",
            "8",
            "0",
        ],
        cwd=tmp_path,
        env=media_worker._worker_env(sys.platform, os.environ),
    )
    try:
        stdout, _stderr = await child.process.communicate()
    finally:
        child.release()

    answer = json.loads(stdout)
    assert answer["ok"] is True
    assert stat.S_IMODE(os.lstat(tmp_path / "master.png").st_mode) == 0o600
    assert stat.S_IMODE(os.lstat(tmp_path / "24.png").st_mode) == 0o600


async def test_asset_worker_argv_carries_the_linux_address_space_pair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert sys.platform != "win32"
    monkeypatch.setattr(assets, "require_private_store", lambda _store: None)
    service = AssetService(tmp_path / "assets", cast(SessionStore, _NoStore()), "a" * 64)
    captured: list[str] = []

    async def refuse(argv: list[str], **_options: object) -> None:
        captured.extend(argv)
        raise OSError

    monkeypatch.setattr(winproc, "spawn", refuse)
    stage = tmp_path / "stage-name"

    with pytest.raises(AssetError, match="worker_unavailable"):
        await service._decode_in_worker(stage)

    assert assets.WORKER_MEMORY_BYTES == 768 << 20
    worker = str(Path(assets.__file__).with_name("media_worker.py"))
    pair = ["--address-space", str(768 << 20)] if sys.platform == "linux" else []
    assert captured == [
        sys.executable,
        "-I",
        "-B",
        worker,
        *pair,
        stage.name,
    ]
