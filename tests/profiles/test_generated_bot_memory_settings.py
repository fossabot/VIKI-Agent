"""Generated native settings must be private files with one name and exact bytes."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import pytest

from viki_agent import protection
from viki_agent.messaging.bot_memory import SETTINGS_FOLDER, settings_bytes, write_settings
from viki_agent.profiles import mcp_config

OWNER = "00000000-0000-4000-8000-000000000001"


def files(tmp_path: Path) -> tuple[Path, Path, Path, bytes]:
    folder, vault = tmp_path / SETTINGS_FOLDER, tmp_path / "vault"
    protection.create_private_directory(folder)
    vault.mkdir()
    data = settings_bytes(vault, OWNER)
    return folder, vault, mcp_config.config_path(folder, data), data


def test_equal_content_public_settings_file_is_refused(tmp_path: Path) -> None:
    folder, vault, path, data = files(tmp_path)
    path.write_bytes(data)
    if sys.platform == "win32":
        from tests.test_protection import _set_acl

        _set_acl(path, "D:P(A;;FA;;;WD)")
    else:
        path.chmod(0o666)
    with pytest.raises(protection.ProtectionError):
        protection.require_private_file(path)
    before = path.stat()
    with pytest.raises((protection.ProtectionError, OSError)):
        write_settings(folder, vault, OWNER)
    with pytest.raises(protection.ProtectionError):
        protection.require_private_file(path)
    assert path.read_bytes() == data
    assert path.stat().st_mode == before.st_mode


def test_equal_content_private_hardlink_is_refused_without_unlinking(tmp_path: Path) -> None:
    folder, vault, path, data = files(tmp_path)
    assert write_settings(folder, vault, OWNER) == str(path)
    protection.require_private_file(path)
    outside = tmp_path / "external.json"
    os.link(path, outside)
    with pytest.raises((protection.ProtectionError, OSError)):
        write_settings(folder, vault, OWNER)
    assert path.read_bytes() == outside.read_bytes() == data
    assert path.stat().st_nlink == outside.stat().st_nlink == 2


def test_equal_content_final_symlink_is_refused(tmp_path: Path) -> None:
    folder, vault, path, data = files(tmp_path)
    outside = tmp_path / "external.json"
    outside.write_bytes(data)
    try:
        path.symlink_to(outside)
    except OSError:
        pytest.skip("Creating symlinks requires host privileges")
    with pytest.raises((protection.ProtectionError, OSError)):
        write_settings(folder, vault, OWNER)
    assert path.is_symlink() and outside.read_bytes() == data


def test_opened_settings_file_must_match_the_checked_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder, vault, path, data = files(tmp_path)
    write_settings(folder, vault, OWNER)
    replacement = folder / "replacement.json"
    replacement.write_bytes(data)
    original = mcp_config.osfs.read_regular

    def swap(*args: Any, **kwargs: Any) -> bytes:
        replacement.replace(path)
        return original(*args, **kwargs)

    monkeypatch.setattr(mcp_config.osfs, "read_regular", swap)
    with pytest.raises(OSError):
        write_settings(folder, vault, OWNER)
    assert path.read_bytes() == data
