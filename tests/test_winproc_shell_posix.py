"""Native POSIX check commands, safe PATH lookup and bounded output after release."""

from __future__ import annotations

import os
import shlex
import signal
import sys
import threading
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path

import pytest

from viki_agent import winproc

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="native POSIX shell processes")
HANG_GUARD_S = 10.0  # a guard on real process start, output and cancellation


def _start(command: str, cwd: Path, **env: str) -> winproc.ShellRun:
    return winproc.start_shell(command, cwd=cwd, env={**winproc.scrub_env(os.environ), **env})


def _all(run: winproc.ShellRun, size: int = 65536) -> bytes:
    parts: list[bytes] = []
    while chunk := run.read(size):
        parts.append(chunk)
    return b"".join(parts)


def _bounded(call: Callable[[], bytes]) -> bytes:
    result: list[bytes] = []
    worker = threading.Thread(target=lambda: result.append(call()), daemon=True)
    worker.start()
    worker.join(HANG_GUARD_S)
    assert not worker.is_alive(), "the output reader did not finish"
    return result[0]


def test_native_shell_expands_env_uses_cwd_and_merges_stderr(tmp_path: Path) -> None:
    run = _start(
        'pwd -P; printf "%s\\n" "$VIKI_CASE"; printf err >&2; read ignored',
        tmp_path,
        VIKI_CASE="a $b",
    )
    try:
        assert _bounded(lambda: _all(run)) == f"{tmp_path.resolve()}\na $b\nerr".encode()
        assert run.wait(HANG_GUARD_S) == 1  # stdin is the null device
    finally:
        run.release()


def test_path_skips_empty_and_relative_entries_but_explicit_path_runs(tmp_path: Path) -> None:
    program = tmp_path / "viki-shell-probe"
    program.write_text("#!/bin/sh\nprintf planted", encoding="utf-8")
    program.chmod(0o755)
    safe = tmp_path / "safe"
    safe.mkdir()
    installed = safe / program.name
    installed.write_text("#!/bin/sh\nprintf installed", encoding="utf-8")
    installed.chmod(0o755)
    given = {"PATH": f":.:relative:{safe}:"}
    run = winproc.start_shell(
        f"{program.name}; printf /; ./{program.name}", cwd=tmp_path, env=given
    )
    try:
        assert _bounded(lambda: _all(run)) == b"installed/planted"
        assert given == {"PATH": f":.:relative:{safe}:"}
    finally:
        run.release()


@pytest.mark.parametrize("path", ["", ":.:relative:", "relative"])
def test_path_without_absolute_entries_does_not_search_the_working_folder(
    tmp_path: Path, path: str
) -> None:
    program = tmp_path / "viki-shell-probe"
    program.write_text("#!/bin/sh\nprintf planted", encoding="utf-8")
    program.chmod(0o755)
    run = winproc.start_shell(program.name, cwd=tmp_path, env={"PATH": path})
    try:
        assert b"planted" not in _bounded(lambda: _all(run))
        assert run.wait(HANG_GUARD_S) == 127
    finally:
        run.release()


def test_release_drains_the_available_tail_in_bounded_chunks(tmp_path: Path) -> None:
    run = _start("printf 0123456789", tmp_path)
    try:
        assert run.wait(HANG_GUARD_S) == 0
        run.release()
        assert _bounded(lambda: _all(run, 3)) == b"0123456789"
        assert run.read() == b""
    finally:
        run.release()


def test_command_leads_its_own_session_and_kill_reaps_it(tmp_path: Path) -> None:
    assert sys.platform != "win32"
    run = _start("/bin/sleep 120", tmp_path)
    try:
        assert os.getsid(run.pid) == run.pid != os.getsid(0)
        assert run.wait(0.01) is None
        run.kill_tree()
        assert run.wait(HANG_GUARD_S) is not None
        run.release()
        assert _bounded(lambda: _all(run)) == b""
    finally:
        run.release()


@pytest.mark.parametrize("busy", [False, True], ids=["idle-inherited-pipe", "continuous-output"])
def test_release_finishes_reading_even_when_a_descendant_leaves_the_group(
    tmp_path: Path, busy: bool
) -> None:
    assert sys.platform != "win32"
    escaped = "import os,time; print(os.getpid(),flush=True)\n" + (
        "while True: os.write(1, b'x' * 4096)" if busy else "time.sleep(120)"
    )
    parent = (
        "import subprocess,sys; subprocess.Popen([sys.executable, '-c', "
        f"{escaped!r}], start_new_session=True)"
    )
    run = _start(f"{shlex.quote(sys.executable)} -c {shlex.quote(parent)}", tmp_path)
    pid: int | None = None
    try:
        pid = int(_bounded(run.read).split(b"\n", 1)[0])
        assert os.getpgid(pid) == pid != run.pid
        assert run.wait(HANG_GUARD_S) == 0
        run.release()
        tail = _bounded(lambda: _all(run))
        if busy:
            assert tail == b"x" * len(tail)
        else:
            assert not tail
        assert run.read() == b""
    finally:
        run.release()
        if pid is not None:
            with suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)


@pytest.mark.parametrize("bad", ["echo a\rb", "echo a\nb", "echo a\0b"])
def test_start_shell_refuses_line_break_or_nul(tmp_path: Path, bad: str) -> None:
    with pytest.raises(ValueError):
        _start(bad, tmp_path)
