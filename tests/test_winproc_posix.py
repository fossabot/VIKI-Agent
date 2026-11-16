"""winproc on POSIX: the process group stands in for the Windows Job (real processes)."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
import time
from pathlib import Path

import pytest

from viki_agent import winproc

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")

# Guards real process starts and exits that normally take milliseconds; never a bound on work.
HANG_GUARD_S = 10

# Starts a grandchild that sleeps argv[2] seconds (ignoring SIGTERM), prints its pid, then lives
# argv[1] more seconds itself.
_PARENT = (
    "import subprocess, sys, time;"
    "code = 'import signal, sys, time; signal.signal(signal.SIGTERM, signal.SIG_IGN);"
    " time.sleep(float(sys.argv[1]))';"
    "p = subprocess.Popen([sys.executable, '-c', code, sys.argv[2]],"
    " stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL);"
    "print(p.pid, flush=True);"
    "time.sleep(float(sys.argv[1]))"
)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def _until_gone(pid: int) -> None:
    async with asyncio.timeout(HANG_GUARD_S):
        while _alive(pid):
            await asyncio.sleep(0.02)  # polls real OS state


async def _spawn_parent(linger_s: float, grandchild_s: float) -> tuple[winproc.Child, int]:
    argv = [sys.executable, "-c", _PARENT, str(linger_s), str(grandchild_s)]
    child = await winproc.spawn(argv, cwd=Path.cwd(), env=os.environ)
    assert child.process.stdout is not None
    line = await asyncio.wait_for(child.process.stdout.readline(), HANG_GUARD_S)
    return child, int(line)


# Starts a grandchild that ignores SIGTERM, then prints its own pid on the stdout it inherits
# from the launched process (so the test reads the pid only once the ignore is installed), then
# lingers. The launched process itself just lingers alongside it.
_SIGTERM_PARENT = (
    "import subprocess, sys, time;"
    "code = ("
    "'import os, signal, time;'"
    "'signal.signal(signal.SIGTERM, signal.SIG_IGN);'"
    "'print(os.getpid(), flush=True);'"
    "'time.sleep(120)'"
    ");"
    "p = subprocess.Popen([sys.executable, '-c', code],"
    " stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL);"
    "time.sleep(120)"
)


async def _spawn_sigterm_ignoring_grandchild() -> tuple[winproc.Child, int]:
    argv = [sys.executable, "-c", _SIGTERM_PARENT]
    child = await winproc.spawn(argv, cwd=Path.cwd(), env=os.environ)
    assert child.process.stdout is not None
    line = await asyncio.wait_for(child.process.stdout.readline(), HANG_GUARD_S)
    return child, int(line)


async def test_spawned_child_leads_its_own_session() -> None:
    assert sys.platform != "win32"
    child, grandchild = await _spawn_parent(120, 120)
    try:
        assert os.getsid(child.pid) == child.pid != os.getsid(0)
        assert os.getpgid(grandchild) == child.pid
    finally:
        child.kill_tree()
        await asyncio.wait_for(child.process.wait(), HANG_GUARD_S)
        child.release()


async def test_job_holds_only_members_of_its_live_group() -> None:
    child, grandchild = await _spawn_parent(120, 120)
    try:
        assert child.job.holds(child.pid)
        assert child.job.holds(grandchild)
        assert not child.job.holds(os.getpid())
        for invalid in (True, 0, -1, 1 << 63):
            assert not child.job.holds(invalid)
    finally:
        child.kill_tree()
        await asyncio.wait_for(child.process.wait(), HANG_GUARD_S)
        child.release()
    assert not child.job.holds(grandchild)


async def test_kill_tree_ends_a_grandchild_that_ignores_sigterm() -> None:
    # The pid is read only after the grandchild installs SIGTERM=SIG_IGN, so a Job.terminate
    # that sent SIGTERM instead of SIGKILL would leave it running past the guard.
    child, grandchild = await _spawn_sigterm_ignoring_grandchild()
    try:
        child.kill_tree()
        await asyncio.wait_for(child.process.wait(), HANG_GUARD_S)
        await _until_gone(grandchild)
    finally:
        child.release()


async def test_wait_tree_waits_for_a_grandchild_that_outlives_the_launched_pid() -> None:
    child, grandchild = await _spawn_parent(0, 1)
    try:
        assert await asyncio.wait_for(child.process.wait(), HANG_GUARD_S) == 0
        assert _alive(grandchild)  # the pid alone is not the tree's lifetime
        await asyncio.wait_for(child.wait_tree(poll_s=0.02), HANG_GUARD_S)
        assert not _alive(grandchild)
        assert child.job.active_processes() == 0
    finally:
        child.release()


async def test_release_ends_a_grandchild_orphaned_by_the_launched_pid() -> None:
    child, grandchild = await _spawn_parent(0, 120)
    try:
        await asyncio.wait_for(child.process.wait(), HANG_GUARD_S)
        assert _alive(grandchild)  # orphaned, still alive
    finally:
        child.release()
    await _until_gone(grandchild)


def test_run_timeout_kills_a_grandchild_that_holds_stdout() -> None:
    # The grandchild inherits the stdout pipe; killing only the launched pid would leave
    # communicate() waiting for its 30 s sleep. The timeout also bounds this child's own
    # Python start-up, so it is well above that, and still far below the grandchild's 30 s
    # sleep and the HANG_GUARD_S below.
    code = (
        "import subprocess, sys;"
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']);"
        "print(p.pid, flush=True); p.wait()"
    )
    started = time.monotonic()
    with pytest.raises(winproc.subprocess.TimeoutExpired) as caught:
        winproc.run([sys.executable, "-c", code], cwd=Path.cwd(), timeout=2.0)
    assert time.monotonic() - started < HANG_GUARD_S
    grandchild = int(caught.value.output)
    deadline = time.monotonic() + HANG_GUARD_S
    while _alive(grandchild) and time.monotonic() < deadline:
        time.sleep(0.02)  # the orphan is reaped by init; poll the real OS state
    assert not _alive(grandchild)


def test_run_kills_what_it_leaves_behind_in_the_group(tmp_path: Path) -> None:
    # A child with null stdio, left in the group when the launched command returns normally.
    code = (
        "import subprocess, sys;"
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'],"
        " stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL);"
        "print(p.pid, flush=True)"
    )
    done = winproc.run([sys.executable, "-c", code], cwd=tmp_path, timeout=HANG_GUARD_S)
    leftover = int(done.stdout)
    deadline = time.monotonic() + HANG_GUARD_S
    while _alive(leftover) and time.monotonic() < deadline:
        time.sleep(0.02)  # poll the real OS state
    assert not _alive(leftover)


async def test_release_twice_is_safe() -> None:
    child = await winproc.spawn([sys.executable, "-c", "pass"], cwd=Path.cwd(), env=os.environ)
    await asyncio.wait_for(child.process.wait(), HANG_GUARD_S)
    child.release()
    child.release()
    assert child.job.active_processes() == 0


def test_job_rejects_a_zero_memory_limit() -> None:
    with pytest.raises(ValueError, match="invalid job memory limit"):
        winproc.Job(memory_limit_bytes=0)


def test_eperm_counts_as_alive_only_for_the_grace(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    assert sys.platform != "win32"
    now = [100.0]
    killed: list[int] = []

    def refuse(pgid: int, sig: int) -> None:
        killed.append(sig)
        raise PermissionError

    monkeypatch.setattr(os, "killpg", refuse)
    monkeypatch.setattr(winproc, "_monotonic", lambda: now[0])
    job = winproc.Job()
    job.adopt(4242)
    with caplog.at_level(logging.WARNING, logger=winproc.__name__):
        assert job.active_processes() == 1
        now[0] += winproc._EPERM_GRACE_S - 0.5
        assert job.active_processes() == 1
        now[0] += 0.5
        assert job.active_processes() == 0
        assert job.active_processes() == 0
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert [r.getMessage() for r in warnings] == ["process group unreachable; treated as ended"]
    job.close()  # seen gone: never signalled again
    assert killed == [0, 0, 0]


def test_eperm_clock_resets_on_a_later_success(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [100.0]
    succeeding = [False]

    def killpg(pgid: int, sig: int) -> None:
        if not succeeding[0]:
            raise PermissionError

    monkeypatch.setattr(os, "killpg", killpg)
    monkeypatch.setattr(winproc, "_monotonic", lambda: now[0])
    job = winproc.Job()
    job.adopt(4242)
    assert job.active_processes() == 1  # EPERM at t
    now[0] += 4.0
    succeeding[0] = True
    assert job.active_processes() == 1  # success at t+4 resets the clock
    succeeding[0] = False
    now[0] += 0.5
    assert job.active_processes() == 1  # EPERM at t+4.5
    now[0] += 3.5  # t+8, only 3.5 s since the reset: still inside the grace
    assert job.active_processes() == 1


def test_terminate_suppresses_permission_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(pgid: int, sig: int) -> None:
        raise PermissionError

    monkeypatch.setattr(os, "killpg", refuse)
    job = winproc.Job()
    job.adopt(4242)
    job.terminate()  # returns quietly; does not raise


_HANDLER = """import os, sys, time
out = sys.argv[1]
with open(out + ".tmp", "w", encoding="utf-8") as f:
    f.write(f"{os.getpid()} {os.getsid(0)}")
os.replace(out + ".tmp", out)
while not os.path.exists(out + ".go"):
    time.sleep(0.01)
"""


async def test_open_external_detaches_the_handler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert sys.platform != "win32"
    script = tmp_path / "handler.py"
    script.write_text(_HANDLER, encoding="utf-8")
    monkeypatch.setattr(winproc, "_OPENER", (sys.executable, str(script)))
    out = tmp_path / "seen"
    winproc.open_external(str(out))
    try:
        async with asyncio.timeout(HANG_GUARD_S):
            while not out.exists():
                await asyncio.sleep(0.02)  # polls real OS state
        pid, sid = map(int, out.read_text(encoding="utf-8").split())
        assert sid == pid != os.getsid(0)  # a session of its own
        assert _alive(pid)  # it outlives the call
    finally:
        # Always release the handler, so a failed assertion above never leaves it polling.
        (tmp_path / "seen.go").touch()
    await _until_gone(pid)  # and the opener thread reaps it


def test_resolve_exe_refuses_a_file_without_the_execute_bit(tmp_path: Path) -> None:
    exe = tmp_path / "claude"
    exe.write_text("#!/bin/sh\n", encoding="utf-8")
    exe.chmod(0o644)
    with pytest.raises(FileNotFoundError):
        winproc.resolve_exe(exe)
    exe.chmod(0o755)
    assert winproc.resolve_exe(exe) == exe


_LSTART_RE = re.compile(r"^[A-Z][a-z]{2} [A-Z][a-z]{2} [ 0-9]\d \d{2}:\d{2}:\d{2} \d{4}$")
_darwin_only = pytest.mark.skipif(sys.platform != "darwin", reason="the macOS CLI's ps lstart")
_linux_only = pytest.mark.skipif(sys.platform != "linux", reason="the Linux CLI's /proc starttime")


def _cli_proc_start(stat: bytes) -> str:
    """procStart the way the Linux CLI writes it: field 22 of /proc/<pid>/stat, counted after
    the last ")" (the command name may hold one), kept as the digit string."""
    return stat.rsplit(b")", 1)[1].split()[19].decode("ascii")


@_linux_only
def test_process_start_time_is_the_proc_starttime_the_linux_cli_writes() -> None:
    pid = os.getpid()
    expected = _cli_proc_start(Path("/proc/self/stat").read_bytes())
    assert expected.isdigit()
    assert winproc.process_start_time(pid) == expected
    assert winproc.process_start_times([pid]) == {pid: expected}


# Records its own starttime, then renames itself so the command name in /proc/<pid>/stat holds
# a ")" and spaces, which a first-")" or a plain whitespace split would misread.
_RENAMED = (
    "import time;"
    "stat = open('/proc/self/stat', 'rb').read();"
    "start = stat.rsplit(b')', 1)[1].split()[19].decode();"
    "open('/proc/self/comm', 'w', encoding='ascii').write('x) 1 2 3');"
    "print(start, flush=True);"
    "time.sleep(120)"
)


@_linux_only
async def test_process_start_time_reads_past_a_command_name_holding_a_parenthesis() -> None:
    child = await winproc.spawn([sys.executable, "-c", _RENAMED], cwd=Path.cwd(), env=os.environ)
    try:
        assert child.process.stdout is not None
        line = await asyncio.wait_for(child.process.stdout.readline(), HANG_GUARD_S)
        assert b"x) 1 2 3" in Path(f"/proc/{child.pid}/stat").read_bytes()
        assert winproc.process_start_time(child.pid) == line.decode().strip()
        assert winproc.process_start_times([child.pid]) == {child.pid: line.decode().strip()}
    finally:
        child.kill_tree()
        await asyncio.wait_for(child.process.wait(), HANG_GUARD_S)
        child.release()


@_darwin_only
def test_process_start_time_matches_the_batch_call_for_the_current_process() -> None:
    pid = os.getpid()
    started = winproc.process_start_time(pid)
    assert isinstance(started, str)
    assert _LSTART_RE.match(started)
    assert winproc.process_start_times([pid])[pid] == started


@_darwin_only
def test_process_start_time_is_stable_across_timezone_and_locale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # `ps` renders lstart in the caller's TZ and locale; the env prefix in the ps argv
    # (LC_ALL=C, TZ=UTC) is what fixes both, so the result equals the CLI's own procStart
    # regardless of the app's environment.
    assert sys.platform != "win32"
    pid = os.getpid()
    monkeypatch.setenv("TZ", "UTC")
    utc = winproc.process_start_time(pid)
    monkeypatch.setenv("TZ", "JST-9")  # a POSIX TZ string; needs no tzdata
    monkeypatch.setenv("LC_ALL", "ja_JP.UTF-8")
    assert winproc.process_start_time(pid) == utc
    assert winproc.process_start_times([pid])[pid] == utc


async def test_process_start_time_is_none_for_a_reaped_child() -> None:
    child = await winproc.spawn([sys.executable, "-c", "pass"], cwd=Path.cwd(), env=os.environ)
    pid = child.pid
    await asyncio.wait_for(child.process.wait(), HANG_GUARD_S)
    child.release()
    assert winproc.process_start_time(pid) is None
    assert pid not in winproc.process_start_times([pid])


@_darwin_only
def test_process_start_times_keeps_a_two_pid_line_with_a_single_digit_day(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(argv: list[str], **kwargs: object) -> winproc.subprocess.CompletedProcess[str]:
        return winproc.subprocess.CompletedProcess(
            argv,
            0,
            "  12 Sat Sep  6 07:42:00 2026\n"
            "  34 Fri Sep  5 01:02:03 2026\n"
            "  56 Thu Sep  4 00:00:00 2026\n",  # not requested: ps can list more than asked
            "",
        )

    monkeypatch.setattr(winproc, "run", fake_run)
    result = winproc.process_start_times([12, 34])
    assert result == {12: "Sat Sep  6 07:42:00 2026", 34: "Fri Sep  5 01:02:03 2026"}


def test_process_start_times_returns_both_the_current_process_and_its_parent() -> None:
    result = winproc.process_start_times([os.getpid(), os.getppid()])
    assert set(result) == {os.getpid(), os.getppid()}
