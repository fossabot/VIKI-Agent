"""The store on POSIX: SQLite's locks survive a nested read, publishes never replace, and the
store files stay private. Synthetic scratch stores only."""

from __future__ import annotations

import os
import shutil
import stat
import sys
from pathlib import Path

import pytest

from tests.persistence.test_storage import SID, _version_one
from viki_agent import osfs, storage, winproc
from viki_agent.state import AppState, Prefs
from viki_agent.storage import StateError

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX locks and modes only")

# Another process asks for the writer reservation and waits at most 50 ms for it.
_RESERVE = """
import sqlite3
import sys
connection = sqlite3.connect(sys.argv[1], timeout=0.05, isolation_level=None)
try:
    connection.execute('BEGIN IMMEDIATE')
except sqlite3.OperationalError as exc:
    print(exc)
else:
    print('reserved')
"""


def test_a_nested_read_keeps_the_writer_reservation_from_another_process(tmp_path: Path) -> None:
    assert sys.platform != "win32"
    store = AppState(tmp_path / "state.json", Prefs()).store
    with store._connection(write=True):
        assert store.get(SID) is None
        other = winproc.run([sys.executable, "-I", "-c", _RESERVE, str(store.path)], cwd=tmp_path)
    assert other.returncode == 0, other.stderr
    assert other.stdout.strip() == "database is locked"


def test_the_shared_descriptor_closes_after_the_last_connection(tmp_path: Path) -> None:
    assert sys.platform != "win32"
    store = AppState(tmp_path / "state.json", Prefs()).store
    assert str(store.path) not in storage._header_fds
    with store._connection():
        fd, users = storage._header_fds[str(store.path)]
        assert users == 1
        with store._connection(write=True):
            assert storage._header_fds == {str(store.path): [fd, 2]}
        assert storage._header_fds == {str(store.path): [fd, 1]}
    assert str(store.path) not in storage._header_fds
    with pytest.raises(OSError):
        os.fstat(fd)


def test_a_file_swapped_under_an_open_connection_is_refused(tmp_path: Path) -> None:
    assert sys.platform != "win32"
    store = AppState(tmp_path / "state.json", Prefs()).store
    with store._connection():
        shutil.copyfile(store.path, tmp_path / "copy.sqlite3")
        store.path.rename(tmp_path / "moved.sqlite3")
        (tmp_path / "copy.sqlite3").rename(store.path)
        with pytest.raises(StateError) as failure:
            store.get(SID)
        assert failure.value.code == "unavailable"
    assert str(store.path) not in storage._header_fds
    assert store.get(SID) is None  # With the old file released, the new one opens.


def test_a_backup_never_replaces_an_existing_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert sys.platform != "win32"
    database = tmp_path / "app.sqlite3"
    _version_one(database)
    original = database.read_bytes()
    publish = osfs.rename_no_replace

    def competing(source: str | os.PathLike[str], destination: str | os.PathLike[str]) -> None:
        Path(destination).write_bytes(b"synthetic competing backup")
        publish(source, destination)

    monkeypatch.setattr(storage.osfs, "rename_no_replace", competing)
    with pytest.raises(StateError) as failure:
        AppState(tmp_path / "state.json", Prefs())
    assert failure.value.code == "unavailable"
    (backup,) = tmp_path.glob("app.sqlite3.v1-backup-*.sqlite3")
    assert backup.read_bytes() == b"synthetic competing backup"
    assert database.read_bytes() == original


def _fsync_spy(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, Path]]:
    """Wraps osfs.rename_no_replace and osfs.fsync_dir, keeping the real behaviour and
    recording each call, in order, so a test can check a sync follows its rename."""
    calls: list[tuple[str, Path]] = []
    real_rename, real_fsync = osfs.rename_no_replace, osfs.fsync_dir

    def rename(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        real_rename(src, dst)
        calls.append(("rename", Path(dst)))

    def fsync(path: str | os.PathLike[str]) -> None:
        real_fsync(path)
        calls.append(("fsync", Path(path)))

    monkeypatch.setattr(osfs, "rename_no_replace", rename)
    monkeypatch.setattr(osfs, "fsync_dir", fsync)
    return calls


def test_create_syncs_the_stores_parent_after_its_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert sys.platform != "win32"
    calls = _fsync_spy(monkeypatch)
    store = AppState(tmp_path / "state.json", Prefs()).store
    assert calls == [("rename", store.path), ("fsync", store.path.parent)]


def test_backup_syncs_the_stores_parent_after_its_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert sys.platform != "win32"
    _version_one(tmp_path / "app.sqlite3")
    calls = _fsync_spy(monkeypatch)
    AppState(tmp_path / "state.json", Prefs())  # Migrates, backing up once per step.
    backups = set(tmp_path.glob("app.sqlite3.v*-backup-*.sqlite3"))
    assert backups
    assert len(calls) == 2 * len(backups)
    for index in range(0, len(calls), 2):
        (rename_kind, rename_path), (fsync_kind, fsync_path) = calls[index], calls[index + 1]
        assert rename_kind == "rename" and fsync_kind == "fsync"
        assert fsync_path == rename_path.parent
    assert {path for kind, path in calls if kind == "rename"} == backups


def test_created_and_backed_up_stores_are_mode_0600_under_umask_022(tmp_path: Path) -> None:
    assert sys.platform != "win32"
    _version_one(tmp_path / "app.sqlite3")
    previous = os.umask(0o022)
    try:
        created = AppState(tmp_path / "new" / "state.json", Prefs()).database_path
        AppState(tmp_path / "state.json", Prefs())  # Migrates, leaving a version 1 backup.
    finally:
        os.umask(previous)
    (backup,) = tmp_path.glob("app.sqlite3.v1-backup-*.sqlite3")
    for path in (created, backup, tmp_path / "app.sqlite3"):
        assert stat.S_IMODE(os.lstat(path).st_mode) == 0o600
