"""winproc.start_shell on Windows: a check's command through cmd.exe inside a Job; and the
system directory and Windows PowerShell's argv it and the terminal use."""

from __future__ import annotations

import errno
import os
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

from viki_agent import winproc

# A guard on events that normally take milliseconds (a short command's output, a kill).
HANG_GUARD_S = 10.0

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="the Windows command shell")

# A grandchild that reports its pid and lingers, started with `start /b` so it outlives cmd.exe.
_SLEEPER = "import os,time;print(os.getpid(),flush=True);time.sleep(60)"


def _start(command: str, cwd: Path, **extra: str) -> winproc.ShellRun:
    return winproc.start_shell(command, cwd=cwd, env={**winproc.scrub_env(os.environ), **extra})


def _read_until(run: winproc.ShellRun, done: bytes | None) -> bytes:
    """Read until `done` appears (None: to EOF). A watchdog kills the tree past the guard, so a
    hung command fails the test instead of blocking it."""
    watchdog = threading.Timer(HANG_GUARD_S, run.kill_tree)
    watchdog.start()
    out = b""
    try:
        while (chunk := run.read()) and (done is None or done not in out + chunk):
            out += chunk
        return out + chunk
    finally:
        watchdog.cancel()


def _lines(run: winproc.ShellRun) -> list[str]:
    return [line.strip() for line in _read_until(run, None).decode().splitlines()]


def _until_dead(pid: int) -> None:
    deadline = time.monotonic() + HANG_GUARD_S
    while winproc.process_start_time(pid) is not None:
        assert time.monotonic() < deadline, f"pid {pid} still alive"
        time.sleep(0.05)  # kill-on-close is asynchronous; poll the real OS state


@windows_only
def test_start_shell_passes_the_command_line_verbatim(tmp_path: Path) -> None:
    run = _start('echo "a b" && echo %VIKI_E03%', tmp_path, VIKI_E03="x")
    try:
        assert _lines(run) == ['"a b"', "x"]
        assert run.wait(HANG_GUARD_S) == 0
    finally:
        run.release()


@windows_only
def test_start_shell_keeps_cmd_escapes(tmp_path: Path) -> None:
    run = _start("echo a^&b", tmp_path)
    try:
        assert _lines(run) == ["a&b"]
    finally:
        run.release()


@windows_only
def test_start_shell_runs_in_cwd(tmp_path: Path) -> None:
    run = _start("cd", tmp_path)
    try:
        assert _lines(run) == [str(tmp_path)]
    finally:
        run.release()


@windows_only
def test_start_shell_opens_no_console_window(tmp_path: Path) -> None:
    probe = "import ctypes;print(ctypes.windll.kernel32.GetConsoleWindow())"
    run = _start(f'"{sys.executable}" -c "{probe}"', tmp_path)
    try:
        assert _lines(run) == ["0"]
    finally:
        run.release()


@windows_only
def test_start_shell_merges_stderr_into_the_one_stream(tmp_path: Path) -> None:
    run = _start("echo out& echo err 1>&2", tmp_path)
    try:
        assert _lines(run) == ["out", "err"]
    finally:
        run.release()


@windows_only
def test_wait_gives_the_exit_code(tmp_path: Path) -> None:
    run = _start("exit /b 5", tmp_path)
    try:
        assert run.wait(HANG_GUARD_S) == 5
    finally:
        run.release()


@windows_only
def test_wait_times_out_and_kill_tree_ends_the_grandchild(tmp_path: Path) -> None:
    command = f'start /b "" "{sys.executable}" -c "{_SLEEPER}" & ping -n 30 127.0.0.1 >nul'
    run = _start(command, tmp_path)
    try:
        grandchild = int(_read_until(run, b"\n"))
        assert run.wait(0.05) is None
        run.kill_tree()
        assert run.wait(HANG_GUARD_S) is not None
        _until_dead(grandchild)
    finally:
        run.release()


@windows_only
def test_release_ends_what_a_finished_command_left(tmp_path: Path) -> None:
    run = _start(f'start /b "" "{sys.executable}" -c "{_SLEEPER}"', tmp_path)
    try:
        grandchild = int(_read_until(run, b"\n"))
        assert run.wait(HANG_GUARD_S) == 0
        assert winproc.process_start_time(grandchild) is not None  # left running
    finally:
        run.release()
    run.release()  # idempotent
    _until_dead(grandchild)


_NO_CWD_SEARCH = "NODEFAULTCURRENTDIRECTORYINEXEPATH"


def _owner_env() -> dict[str, str]:
    """The user's environment, which does not set NoDefaultCurrentDirectoryInExePath."""
    env = winproc.scrub_env(os.environ)
    return {k: v for k, v in env.items() if k.upper() != _NO_CWD_SEARCH}


@windows_only
def test_a_bare_name_skips_the_folders_own_program(tmp_path: Path) -> None:
    (tmp_path / "hostname.bat").write_text("@echo PLANTED\n", encoding="ascii")
    run = winproc.start_shell("hostname", cwd=tmp_path, env=_owner_env())
    try:
        assert [line.lower() for line in _lines(run)] == [socket.gethostname().lower()]
    finally:
        run.release()


@windows_only
def test_a_program_in_the_folder_runs_by_its_path(tmp_path: Path) -> None:
    (tmp_path / "hostname.bat").write_text("@echo PLANTED\n", encoding="ascii")
    run = winproc.start_shell(r".\hostname.bat", cwd=tmp_path, env=_owner_env())
    try:
        assert _lines(run) == ["PLANTED"]
    finally:
        run.release()


@windows_only
def test_the_child_env_turns_off_the_current_directory_search(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, str] = {}

    def fake_popen(*_args: object, env: dict[str, str], **_kwargs: object) -> None:
        seen.update(env)
        raise OSError("recorded")

    monkeypatch.setattr(winproc.subprocess, "Popen", fake_popen)
    given = {_NO_CWD_SEARCH: "", "Path": r"C:\bin", "VIKI_E03": "x"}
    with pytest.raises(OSError, match="recorded"):
        winproc.start_shell("echo a", cwd=tmp_path, env=given)
    assert seen == {"Path": r"C:\bin", "VIKI_E03": "x", "NoDefaultCurrentDirectoryInExePath": "1"}


@windows_only
@pytest.mark.parametrize("bad", ["echo a\rb", "echo a\nb", "echo a\0b"])
def test_start_shell_refuses_a_line_break_or_nul(tmp_path: Path, bad: str) -> None:
    with pytest.raises(ValueError):
        _start(bad, tmp_path)


@windows_only
def test_powershell_argv_names_windows_powershell() -> None:
    argv = winproc.powershell_argv()
    assert Path(argv[0]).is_file()
    assert Path(argv[0]).parent == winproc.system_dir() / "WindowsPowerShell" / "v1.0"
    assert argv[1:] == ["-NoLogo"]


@pytest.mark.skipif(sys.platform == "win32", reason="the POSIX refusal")
def test_windows_shell_entry_points_refuse_off_windows() -> None:
    for call in (
        winproc.system_dir,
        winproc.powershell_argv,
    ):
        with pytest.raises(OSError) as refused:
            call()
        assert refused.value.errno == errno.ENOSYS
