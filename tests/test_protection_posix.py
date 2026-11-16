"""POSIX private directories and files, and the AES-GCM seal; synthetic keys and paths only."""

from __future__ import annotations

import os
import re
import secrets
import stat
import sys
from pathlib import Path

import pytest

from viki_agent import protection
from viki_agent.protection import (
    ProtectionError,
    create_private_directory,
    protect,
    require_private_directory,
    require_private_file,
    set_key_source,
    unprotect,
)

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes, owners and keys")


def _mode(path: Path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


def _private_file(path: Path) -> Path:
    path.write_bytes(b"synthetic metadata")
    path.chmod(0o600)
    return path


@posix_only
@pytest.mark.usefixtures("desktop_umask")
def test_create_private_directory_makes_a_0700_leaf_owned_by_the_user(tmp_path: Path) -> None:
    assert sys.platform != "win32"
    leaf = tmp_path / "app-data"
    assert create_private_directory(leaf) is True
    info = os.lstat(leaf)
    assert stat.S_ISDIR(info.st_mode)
    assert _mode(leaf) == 0o700
    assert info.st_uid == os.geteuid()
    require_private_directory(leaf)


@posix_only
def test_create_private_directory_leaves_an_existing_leaf_alone(tmp_path: Path) -> None:
    leaf = tmp_path / "existing"
    leaf.mkdir()
    leaf.chmod(0o755)
    assert create_private_directory(leaf) is False
    assert _mode(leaf) == 0o755
    with pytest.raises(ProtectionError):
        require_private_directory(leaf)


@posix_only
def test_create_private_directory_does_not_create_missing_parents(tmp_path: Path) -> None:
    parent = tmp_path / "missing"
    with pytest.raises(ProtectionError):
        create_private_directory(parent / "leaf")
    assert not parent.exists()


@posix_only
def test_private_directory_and_file_pass(tmp_path: Path) -> None:
    tmp_path.chmod(0o700)
    require_private_directory(tmp_path)
    require_private_file(_private_file(tmp_path / "synthetic.db"))


@posix_only
@pytest.mark.parametrize("mode", [0o750, 0o701, 0o707])
def test_directory_with_group_or_other_bits_is_refused(tmp_path: Path, mode: int) -> None:
    leaf = tmp_path / "broad"
    leaf.mkdir()
    leaf.chmod(mode)
    with pytest.raises(ProtectionError, match=re.escape(protection._MESSAGE)):
        require_private_directory(leaf)


@posix_only
def test_directory_check_refuses_a_symlink_a_file_and_an_absent_path(tmp_path: Path) -> None:
    target = tmp_path / "private"
    assert create_private_directory(target) is True
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    for path in (link, _private_file(tmp_path / "file.db"), tmp_path / "absent"):
        with pytest.raises(ProtectionError):
            require_private_directory(path)


@posix_only
@pytest.mark.parametrize("mode", [0o640, 0o604, 0o660])
def test_file_with_group_or_other_bits_is_refused(tmp_path: Path, mode: int) -> None:
    path = _private_file(tmp_path / "synthetic.db")
    path.chmod(mode)
    with pytest.raises(ProtectionError):
        require_private_file(path)


@posix_only
def test_file_check_refuses_a_symlink_a_directory_and_an_absent_path(tmp_path: Path) -> None:
    link = tmp_path / "link.db"
    link.symlink_to(_private_file(tmp_path / "synthetic.db"))
    for path in (link, tmp_path, tmp_path / "absent.db"):
        with pytest.raises(ProtectionError):
            require_private_file(path)


@posix_only
def test_a_path_owned_by_another_uid_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert sys.platform != "win32"
    tmp_path.chmod(0o700)
    path = _private_file(tmp_path / "synthetic.db")
    monkeypatch.setattr(os, "geteuid", lambda: os.lstat(tmp_path).st_uid + 1)
    with pytest.raises(ProtectionError):
        require_private_directory(tmp_path)
    with pytest.raises(ProtectionError):
        require_private_file(path)


@pytest.mark.parametrize("size", [0, 1, 1 << 20])
def test_seal_round_trips(size: int) -> None:
    key, data = secrets.token_bytes(32), secrets.token_bytes(size)
    blob = protection._seal(key, data, b"synthetic scope")
    assert protection._unseal(key, blob, b"synthetic scope") == data


def test_two_seals_differ_and_hold_no_plaintext() -> None:
    key, data = secrets.token_bytes(32), b"synthetic draft body " * 8
    first, second = (protection._seal(key, data, b"scope") for _ in range(2))
    assert first != second
    assert b"synthetic draft" not in first
    assert first.startswith(protection._SEAL_MAGIC)


def test_unseal_refuses_any_tampering() -> None:
    key = secrets.token_bytes(32)
    blob = protection._seal(key, b"synthetic draft", b"scope")
    refused = [
        (key, blob, b"other scope"),
        (secrets.token_bytes(32), blob, b"scope"),
        (key, blob[:-1], b"scope"),
        (key, blob[:30], b"scope"),
        (key, b"", b"scope"),
    ]
    # Every byte counts: magic, key id, nonce, ciphertext and tag.
    for index in range(len(blob)):
        flipped = blob[:index] + bytes([blob[index] ^ 0x01]) + blob[index + 1 :]
        refused.append((key, flipped, b"scope"))
    for other_key, other_blob, context in refused:
        with pytest.raises(ProtectionError):
            protection._unseal(other_key, other_blob, context)


@posix_only
def test_protect_round_trips_through_the_installed_key_source() -> None:
    blob = protect(b"synthetic draft", context=b"synthetic scope")
    assert b"synthetic draft" not in blob
    assert unprotect(blob, context=b"synthetic scope") == b"synthetic draft"
    with pytest.raises(ProtectionError):
        unprotect(blob, context=b"other scope")


@posix_only
def test_protect_refuses_a_missing_failing_or_short_key(monkeypatch: pytest.MonkeyPatch) -> None:
    blob = protect(b"synthetic draft", context=b"scope")
    # Snapshot the session's source so teardown reinstalls it.
    monkeypatch.setattr(protection, "_key_source", protection._key_source)

    def failing() -> bytes:
        raise OSError("synthetic keychain failure")

    for source in (None, failing, lambda: secrets.token_bytes(16)):
        set_key_source(source)
        with pytest.raises(ProtectionError):
            protect(b"synthetic draft", context=b"scope")
        with pytest.raises(ProtectionError):
            unprotect(blob, context=b"scope")


@posix_only
def test_visit_key_source_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """The launcher calls this once, holding the lock and before serving; a missing or a
    failing source must never surface as an exception for main() to handle — a later seal
    answers ProtectionError on its own."""
    assert sys.platform != "win32"
    # Snapshot the session's source so teardown reinstalls it (as test_protect_refuses_a_missing…
    # does above).
    monkeypatch.setattr(protection, "_key_source", protection._key_source)

    def failing() -> bytes:
        raise OSError("synthetic keychain failure")

    for source in (None, failing):
        set_key_source(source)
        protection.visit_key_source()  # must not raise


def test_no_environment_variable_selects_the_key_store() -> None:
    text = Path(protection.__file__).read_text(encoding="utf-8")
    assert "os.environ" not in text
    assert "getenv" not in text


@pytest.mark.skipif(sys.platform != "darwin", reason="extended ACLs are read on macOS only")
def test_extended_acl_entry_is_refused_on_macos(tmp_path: Path) -> None:
    assert sys.platform == "darwin"
    from viki_agent import winproc  # Imported here so collection never depends on it.

    directory = tmp_path / "acl-dir"
    assert create_private_directory(directory) is True
    path = _private_file(tmp_path / "synthetic.db")
    # macOS names the read grant "list" on a directory and "read" on a file.
    for grant, target in (("list", directory), ("read", path)):
        argv = ["/bin/chmod", "+a", f"everyone allow {grant}", str(target)]
        assert winproc.run(argv, cwd=tmp_path).returncode == 0
    with pytest.raises(ProtectionError):
        require_private_directory(directory)
    with pytest.raises(ProtectionError):
        require_private_file(path)
