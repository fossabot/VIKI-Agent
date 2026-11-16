"""The one-running-app lock and the open-window signal on POSIX: a flock and a FIFO in a private
runtime directory (real files and a real process)."""

from __future__ import annotations

import asyncio
import errno
import os
import stat
import sys
from pathlib import Path

import pytest

from viki_agent import winproc

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX lock and FIFO")

# Guards a real child's start-up and exit, which normally take milliseconds; never a bound on work.
HANG_GUARD_S = 10

NAME = "Local\\viki-test-instance"
SAFE = "Local_viki-test-instance"  # every character outside [A-Za-z0-9._-] becomes "_"

# Takes the lock named argv[1], says whether it got it, then holds it until killed.
_HOLDER = (
    "import sys, time; from viki_agent import winproc;"
    "print(winproc.single_instance(sys.argv[1]) is not None, flush=True); time.sleep(120)"
)


@pytest.fixture
def runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The runtime directory the code will use, placed under tmp_path."""
    monkeypatch.setenv("TMPDIR" if sys.platform == "darwin" else "XDG_RUNTIME_DIR", str(tmp_path))
    return tmp_path / "viki-agent"


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def test_the_lock_is_exclusive_until_released_in_a_private_directory(
    runtime: Path, desktop_umask: None
) -> None:
    assert sys.platform != "win32"
    first = winproc.single_instance(NAME)
    assert first is not None
    assert winproc.single_instance(NAME) is None
    first.release()
    first.release()  # idempotent
    third = winproc.single_instance(NAME)
    assert third is not None
    third.release()
    assert _mode(runtime) == 0o700
    assert _mode(runtime / f"{SAFE}.lock") == 0o600  # kept: unlinking it races the next locker


def test_the_signal_wakes_once_for_several_sets_and_its_fifo_is_private(
    runtime: Path, desktop_umask: None
) -> None:
    assert sys.platform != "win32"
    assert winproc.Signal.open(NAME) is None
    owner = winproc.Signal.create(NAME)
    fifo = runtime / f"{SAFE}.fifo"
    try:
        assert stat.S_ISFIFO(fifo.lstat().st_mode) and _mode(fifo) == 0o600
        assert owner.wait(0) is False
        other = winproc.Signal.open(NAME)
        assert other is not None
        with pytest.raises(RuntimeError):
            other.wait(0)  # only the creator reads
        for _ in range(3):
            other.set()
        other.close()
        assert owner.wait(50) is True
        assert owner.wait(0) is False  # three sets with no wait between them: one wake
    finally:
        owner.close()
    assert not fifo.exists()
    with pytest.raises(RuntimeError):
        owner.wait(0)


def test_a_set_after_the_creator_closed_is_lost_quietly(runtime: Path) -> None:
    assert sys.platform != "win32"
    owner = winproc.Signal.create(NAME)
    other = winproc.Signal.open(NAME)
    assert other is not None
    owner.close()  # the running app quits while a second launch wakes it
    try:
        other.set()  # as SetEvent on an event nobody waits on: no error
    finally:
        other.close()


@pytest.mark.parametrize("problem", ["group and other bits", "another owner", "symlink"])
def test_a_runtime_directory_that_is_not_private_is_refused(
    runtime: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, problem: str
) -> None:
    assert sys.platform != "win32"
    if problem == "symlink":
        (tmp_path / "elsewhere").mkdir(mode=0o700)
        runtime.symlink_to(tmp_path / "elsewhere")
    else:
        runtime.mkdir(mode=0o700)
    if problem == "group and other bits":
        runtime.chmod(0o755)
    if problem == "another owner":
        uid = os.getuid() + 1
        monkeypatch.setattr(os, "getuid", lambda: uid)
    with pytest.raises(PermissionError):
        winproc.single_instance(NAME)
    with pytest.raises(PermissionError):
        winproc.Signal.create(NAME)


@pytest.mark.parametrize("problem", ["relative", "missing", "unwritable"])
def test_an_unusable_runtime_dir_env_falls_back_to_tmp_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, problem: str
) -> None:
    assert sys.platform != "win32"
    if problem == "unwritable" and os.geteuid() == 0:
        pytest.skip("root bypasses the mode bits")
    # _TMP_BASE points here, not the real /tmp, so the fallback directory this test makes
    # never outlives it.
    monkeypatch.setattr(winproc, "_TMP_BASE", tmp_path)
    var = "TMPDIR" if sys.platform == "darwin" else "XDG_RUNTIME_DIR"
    if problem == "unwritable":
        foreign = tmp_path / "unwritable-runtime-dir"
        foreign.mkdir(mode=0o500)
        value = str(foreign)
    else:
        value = "relative-runtime-dir" if problem == "relative" else str(tmp_path / "no-such-dir")
    monkeypatch.setenv(var, value)
    lock = winproc.single_instance(NAME)
    try:
        assert lock is not None
        assert (tmp_path / f"viki-agent-{os.getuid()}").is_dir()
    finally:
        if problem == "unwritable":
            (tmp_path / "unwritable-runtime-dir").chmod(0o700)
        assert lock is not None
        lock.release()


def test_a_stale_fifo_has_no_reader_and_create_replaces_it(runtime: Path) -> None:
    assert sys.platform != "win32"
    runtime.mkdir(mode=0o700)
    fifo = runtime / f"{SAFE}.fifo"
    os.mkfifo(fifo)
    fifo.chmod(0o644)  # so a reused FIFO would show
    assert winproc.Signal.open(NAME) is None  # left by a crashed app: nobody listens
    owner = winproc.Signal.create(NAME)
    try:
        assert _mode(fifo) == 0o600
        other = winproc.Signal.open(NAME)
        assert other is not None
        other.set()
        other.close()
        assert owner.wait(50) is True
    finally:
        owner.close()
    fifo.write_text("", encoding="utf-8")
    with pytest.raises(FileExistsError):
        winproc.Signal.create(NAME)  # never unlinks what is not a FIFO


def test_a_symlink_at_the_lock_path_is_refused(runtime: Path) -> None:
    assert sys.platform != "win32"
    runtime.mkdir(mode=0o700)
    target = runtime.parent / "elsewhere.lock"
    target.write_text("", encoding="utf-8")
    (runtime / f"{SAFE}.lock").symlink_to(target)
    with pytest.raises(OSError) as caught:
        winproc.single_instance(NAME)
    assert caught.value.errno == errno.ELOOP


def test_a_regular_file_at_the_fifo_path_makes_open_return_none(runtime: Path) -> None:
    assert sys.platform != "win32"
    runtime.mkdir(mode=0o700)
    (runtime / f"{SAFE}.fifo").write_text("", encoding="utf-8")
    assert winproc.Signal.open(NAME) is None


async def test_the_lock_is_released_when_its_holder_dies(runtime: Path) -> None:
    assert sys.platform != "win32"
    child = await winproc.spawn(
        [sys.executable, "-c", _HOLDER, NAME], cwd=Path.cwd(), env=os.environ
    )
    try:
        assert child.process.stdout is not None
        assert await asyncio.wait_for(child.process.stdout.readline(), HANG_GUARD_S) == b"True\n"
        assert winproc.single_instance(NAME) is None
        child.kill_tree()
        await asyncio.wait_for(child.process.wait(), HANG_GUARD_S)
        lock = winproc.single_instance(NAME)
        assert lock is not None
        lock.release()
    finally:
        child.kill_tree()
        child.release()
