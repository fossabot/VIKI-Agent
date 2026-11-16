"""Shared fixtures. This directory is on sys.path, so tests import `replay` directly."""

from __future__ import annotations

import json
import os
import secrets
import sys
import tempfile
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path

import pytest

from viki_agent import protection, workspaces

from replay import Entry

TESTS = Path(__file__).parent
FAKE_CLAUDE = TESTS / "fake_claude.py"


def pytest_configure(config: pytest.Config) -> None:
    if sys.platform != "win32":
        # Directories a test makes with mkdir() must be private, as Windows' per-user temp is.
        # Production creates its own private files and directories explicitly; some POSIX tests
        # of those sites use the desktop_umask fixture, and the rest set umask 022 themselves.
        os.umask(0o077)
        # pytest resolves its own base temp, so tmp_path never holds a link, but tests that call
        # tempfile.mkdtemp() directly (test_history_media_app) do not get that. On macOS $TMPDIR
        # lies under /var, a link to /private/var, so resolve tempfile's own directory too.
        tempfile.tempdir = os.path.realpath(tempfile.gettempdir())


@pytest.fixture
def desktop_umask() -> Iterator[None]:
    """A desktop's usual umask, under which a missing explicit mode shows up as 0755 or 0644."""
    previous = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(previous)


@pytest.fixture(scope="session", autouse=True)
def posix_key_source() -> Iterator[None]:
    """On POSIX, seal with a random per-session key; production reads it from the OS keychain.

    Tests never touch a real keychain. Windows ignores the source: DPAPI needs no key.
    """
    if sys.platform == "win32":
        yield
        return
    key = secrets.token_bytes(32)
    protection.set_key_source(lambda: key)
    try:
        yield
    finally:
        protection.set_key_source(None)


@pytest.fixture(autouse=True)
def posix_keychain_stub(posix_key_source: None) -> Iterator[None]:
    """On POSIX, a launcher run in a test re-installs the session key, and any source a test
    installs is undone after it.

    A generator fixture that saves and restores by hand, not monkeypatch. An autouse fixture
    that requests monkeypatch sets it up before a test's own fixtures, so monkeypatch, one
    object shared with the test, is torn down after them: every patch the test itself made would
    then stay active through its other fixtures' teardown.
    """
    if sys.platform == "win32":
        yield
        return
    # Tests never reach a real keychain; the opt-in keychain-marked test builds its source
    # through protection._keychain_source directly, so this stub does not affect it.
    installed = protection._key_source
    saved_keychain_source = protection.keychain_source
    protection.keychain_source = lambda data_dir: installed
    try:
        yield
    finally:
        protection.keychain_source = saved_keychain_source
        protection._key_source = installed


@pytest.fixture(scope="session", autouse=True)
def posix_system_deny(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    """On POSIX, let tests register folders in pytest's temp directory.

    Its base lies under /tmp (or /private/var on macOS), which the fixed deny set refuses; the
    Windows tests keep TEMP out of their hand-built environments the same way. Only the fixed
    entries holding the base are dropped, and tests of the full list restore it themselves.
    """
    if sys.platform == "win32":
        yield
        return
    base = Path(os.path.realpath(tmp_path_factory.getbasetemp()))
    full = workspaces.SYSTEM_DENY
    workspaces.SYSTEM_DENY = tuple(
        spec for spec in full if not base.is_relative_to(os.path.realpath(spec[0]))
    )
    try:
        yield
    finally:
        workspaces.SYSTEM_DENY = full


@pytest.fixture
def fake_cli() -> list[str]:
    """The `cli` prefix for build_argv that runs fake_claude.py instead of claude.exe."""
    return [sys.executable, str(FAKE_CLAUDE)]


@pytest.fixture
def write_script(tmp_path: Path) -> Callable[[Sequence[Entry]], Path]:
    def write(entries: Sequence[Entry]) -> Path:
        path = tmp_path / "script.jsonl"
        with path.open("w", encoding="utf-8") as fh:
            for entry in entries:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return path

    return write
