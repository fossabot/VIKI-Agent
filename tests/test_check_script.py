"""The check runner keeps every stage and joins shared-port work before e2e."""

from __future__ import annotations

import contextlib
import os
import signal
import sys
import threading
import time
from concurrent.futures import Future
from pathlib import Path
from unittest.mock import Mock

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import check  # noqa: E402

HANG_GUARD_S = 30  # a process starts slowly under a parallel run's load
NPM = "test-npm"
PY_COMMANDS = [
    ["ruff", "check", "src", "tests", "scripts"],
    ["ruff", "format", "--check", "src", "tests", "scripts"],
    ["pyright", "--pythonplatform", "Windows"],
    ["pyright", "-p", "pyright-posix.json", "--pythonplatform", "Linux"],
    ["pyright", "-p", "pyright-posix.json", "--pythonplatform", "Darwin"],
    ["pytest", "-q", "-n", "auto", "--maxprocesses", "12", "--dist", "load"],
]
FE_COMMANDS = [[NPM, "run", stage] for stage in ("lint", "typecheck", "test", "build", "e2e")]
DESKTOP_COMMANDS = [[NPM, "run", stage] for stage in ("lint", "test")]


@pytest.mark.parametrize(
    ("args", "python", "frontend"),
    [
        ([], True, True),
        (["--py"], True, False),
        (["--fe"], False, True),
        (["--py", "--fe"], True, True),
        (["--parallel", "--py"], True, False),
        (["--parallel", "--fe"], False, True),
    ],
)
def test_serial_selection_preserves_exact_commands_and_order(
    args: list[str], python: bool, frontend: bool, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    calls: list[tuple[list[str], Path]] = []
    monkeypatch.setattr(sys, "argv", ["check.py", *args])
    npm = Mock(return_value=NPM)
    monkeypatch.setattr(check.shutil, "which", npm)
    executor = Mock(side_effect=AssertionError("serial mode started a worker"))
    monkeypatch.setattr(check, "ThreadPoolExecutor", executor, raising=False)

    def run(cmd: list[str], cwd: Path) -> bool:
        calls.append((cmd, cwd))
        return True

    monkeypatch.setattr(check, "_run", run)
    assert check.main() == 0
    assert calls == [(cmd, check.REPO) for cmd in PY_COMMANDS if python] + [
        (cmd, check.FRONTEND) for cmd in FE_COMMANDS if frontend
    ] + [(cmd, check.DESKTOP) for cmd in DESKTOP_COMMANDS if frontend]
    assert capsys.readouterr().out.endswith("All checks passed.\n")
    executor.assert_not_called()
    if frontend:
        npm.assert_called_once_with("npm")
    else:
        npm.assert_not_called()


@pytest.mark.parametrize("parallel", [False, True])
def test_stage_failures_aggregate_without_skipping_checks(
    parallel: bool, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    calls: list[tuple[list[str], Path]] = []
    monkeypatch.setattr(sys, "argv", ["check.py", *(["--parallel"] if parallel else [])])
    monkeypatch.setattr(check.shutil, "which", Mock(return_value=NPM))

    def run(cmd: list[str], cwd: Path) -> bool:
        calls.append((cmd, cwd))
        return False

    monkeypatch.setattr(check, "_run", run)
    assert check.main() == 1
    assert [cmd for cmd, cwd in calls if cwd == check.REPO] == PY_COMMANDS
    assert [cmd for cmd, cwd in calls if cwd == check.FRONTEND] == FE_COMMANDS
    assert [cmd for cmd, cwd in calls if cwd == check.DESKTOP] == DESKTOP_COMMANDS
    fe_labels = [f"npm run {cmd[-1]}" for cmd in FE_COMMANDS]
    desktop_labels = [f"desktop npm run {cmd[-1]}" for cmd in DESKTOP_COMMANDS]
    # Serial runs desktop after all of FE_COMMANDS (e2e included). Parallel runs desktop inside
    # the frontend worker, after FE_COMMANDS[:-1] but before the e2e join, so e2e is last there.
    ordered_labels = (
        fe_labels[:-1] + desktop_labels + fe_labels[-1:] if parallel else fe_labels + desktop_labels
    )
    failures = [" ".join(cmd) for cmd in PY_COMMANDS] + ordered_labels
    assert capsys.readouterr().out.splitlines() == ["", "FAILED: " + ", ".join(failures)]


@pytest.mark.parametrize("args", [[], ["--parallel"], ["--fe"], ["--parallel", "--fe"]])
def test_missing_npm_is_one_failure_and_python_still_runs(
    args: list[str], monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    calls: list[tuple[list[str], Path]] = []
    monkeypatch.setattr(sys, "argv", ["check.py", *args])
    npm = Mock(return_value=None)
    monkeypatch.setattr(check.shutil, "which", npm)

    def run(cmd: list[str], cwd: Path) -> bool:
        calls.append((cmd, cwd))
        return True

    monkeypatch.setattr(check, "_run", run)
    assert check.main() == 1
    assert calls == ([] if "--fe" in args else [(cmd, check.REPO) for cmd in PY_COMMANDS])
    assert capsys.readouterr().out.endswith("FAILED: npm not found\n")
    npm.assert_called_once_with("npm")


@pytest.mark.parametrize("args", [["--parallel"], ["--parallel", "--py", "--fe"]])
def test_parallel_lanes_overlap_and_join_before_e2e(
    args: list[str], monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.setattr(sys, "argv", ["check.py", *args])
    monkeypatch.setattr(check.shutil, "which", Mock(return_value=NPM))
    overlap = threading.Barrier(2)
    joining = threading.Event()
    python_done = threading.Event()
    frontend_done = threading.Event()
    calls: list[tuple[list[str], Path]] = []
    original_result = Future.result

    def result(self, timeout=None):
        joining.set()
        return original_result(self, timeout)

    monkeypatch.setattr(Future, "result", result)

    # FE_COMMANDS and DESKTOP_COMMANDS share stage names ("lint", "test" both run via npm in each),
    # so their command lists are value-equal; every comparison below pairs cmd with cwd to tell
    # a frontend stage from a desktop one.
    def run(cmd: list[str], cwd: Path) -> bool:
        calls.append((cmd, cwd))
        if (cmd, cwd) in ((PY_COMMANDS[0], check.REPO), (FE_COMMANDS[0], check.FRONTEND)):
            overlap.wait(HANG_GUARD_S)
        if (cmd, cwd) == (PY_COMMANDS[-1], check.REPO):
            assert frontend_done.wait(HANG_GUARD_S)
            # Hold Python until the runner joins; starting e2e before that must fail.
            assert joining.wait(HANG_GUARD_S)
            python_done.set()
        elif (cmd, cwd) == (DESKTOP_COMMANDS[-1], check.DESKTOP):
            # The frontend worker's last step: FE_COMMANDS[:-1] then desktop, before the e2e join.
            frontend_done.set()
        elif (cmd, cwd) == (FE_COMMANDS[-1], check.FRONTEND):
            try:
                assert python_done.is_set() and frontend_done.is_set()
            finally:
                joining.set()  # release workers even if the barrier is broken by a mutant
        return True

    monkeypatch.setattr(check, "_run", run)
    assert check.main() == 0
    assert [cmd for cmd, cwd in calls if cwd == check.REPO] == PY_COMMANDS
    assert [cmd for cmd, cwd in calls if cwd == check.FRONTEND] == FE_COMMANDS
    assert [cmd for cmd, cwd in calls if cwd == check.DESKTOP] == DESKTOP_COMMANDS
    assert calls[-1] == (FE_COMMANDS[-1], check.FRONTEND)
    # Desktop runs inside the frontend worker: after FE_COMMANDS[:-1], before the e2e join.
    assert (
        calls.index((FE_COMMANDS[-2], check.FRONTEND))
        < calls.index((DESKTOP_COMMANDS[0], check.DESKTOP))
        < calls.index((FE_COMMANDS[-1], check.FRONTEND))
    )
    assert capsys.readouterr().out.endswith("All checks passed.\n")


@pytest.mark.parametrize("failure", ["interrupt", "launch"])
def test_abnormal_exit_stops_new_parallel_stages(
    failure: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(check, "_running", set())
    monkeypatch.setattr(sys, "argv", ["check.py", "--parallel"])
    monkeypatch.setattr(check.shutil, "which", Mock(return_value=NPM))
    python_started = threading.Event()
    frontend_started = threading.Event()
    release = threading.Event()
    calls: list[tuple[list[str], Path]] = []
    registered: list[Mock] = []
    killed: list[object] = []
    monkeypatch.setattr(check, "_kill_tree", killed.append)
    original_shutdown = check.ThreadPoolExecutor.shutdown

    def shutdown(self, *args, **kwargs):
        release.set()  # let current stages end only after the runner begins cleanup
        return original_shutdown(self, *args, **kwargs)

    monkeypatch.setattr(check.ThreadPoolExecutor, "shutdown", shutdown)

    def run(cmd: list[str], cwd: Path) -> bool:
        calls.append((cmd, cwd))
        # Stands in for a real check._run, which would still have this stage in _running: main's
        # abnormal-exit cleanup must find and kill it before the pool joins.
        stage = Mock() if cmd in (PY_COMMANDS[0], FE_COMMANDS[0]) else None
        if stage is not None:
            registered.append(stage)
            check._running.add(stage)
        if cmd == PY_COMMANDS[0]:
            python_started.set()
            assert frontend_started.wait(HANG_GUARD_S)
            if failure == "launch":
                raise OSError("test launch failure")
        elif cmd == FE_COMMANDS[0]:
            frontend_started.set()
        assert release.wait(HANG_GUARD_S)
        if stage is not None:
            check._running.discard(stage)
        return True

    def interrupt(self, timeout=None):
        assert python_started.wait(HANG_GUARD_S)
        assert frontend_started.wait(HANG_GUARD_S)
        raise KeyboardInterrupt

    monkeypatch.setattr(check, "_run", run)
    if failure == "interrupt":
        monkeypatch.setattr(Future, "result", interrupt)
    with pytest.raises(KeyboardInterrupt if failure == "interrupt" else OSError):
        check.main()
    assert [cmd for cmd, cwd in calls if cwd == check.REPO] == PY_COMMANDS[:1]
    assert [cmd for cmd, cwd in calls if cwd == check.FRONTEND] == FE_COMMANDS[:1]
    assert len(registered) == 2
    assert set(killed) == set(registered)


@pytest.mark.parametrize(("returncode", "passed", "label"), [(0, True, "PASS"), (3, False, "FAIL")])
def test_run_reports_result_and_elapsed_time_without_capturing_output(
    returncode: int, passed: bool, label: str, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.setattr(check, "_running", set())
    cmd = ["test-command", "argument"]
    clock = Mock(side_effect=[12.0, 14.25])
    stage = Mock()

    def wait(timeout: float) -> int:
        # Pins that _run keeps the stage in _running while it waits: a mutant that deletes
        # _running.add(process) would otherwise pass every test here, and main's --parallel
        # cleanup would then kill nothing on an abnormal exit.
        assert check._running == {stage}
        return returncode

    stage.wait = Mock(side_effect=wait)
    popen = Mock(return_value=stage)
    monkeypatch.setattr(check, "perf_counter", clock, raising=False)
    monkeypatch.setattr(check.subprocess, "Popen", popen)
    assert check._run(cmd, check.REPO) is passed
    popen.assert_called_once_with(cmd, cwd=check.REPO, start_new_session=sys.platform != "win32")
    stage.wait.assert_called_once_with(timeout=check.STAGE_LIMIT_S)
    assert (
        capsys.readouterr().out
        == f"\n$ test-command argument\n[{label}] test-command argument (2.25s)\n"
    )
    assert clock.call_count == 2
    assert check._running == set()


def test_a_stage_past_the_limit_is_killed_and_fails(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.setattr(check, "_running", set())
    cmd = ["test-command", "argument"]
    clock = Mock(side_effect=[12.0, 14.25])
    stage = Mock()
    stage.wait = Mock(side_effect=check.subprocess.TimeoutExpired(cmd, check.STAGE_LIMIT_S))
    monkeypatch.setattr(check, "perf_counter", clock, raising=False)
    monkeypatch.setattr(check.subprocess, "Popen", Mock(return_value=stage))
    killed: list[object] = []
    monkeypatch.setattr(check, "_kill_tree", killed.append)
    assert check._run(cmd, check.REPO) is False
    assert killed == [stage]
    assert capsys.readouterr().out == (
        "\n$ test-command argument\n"
        f"[TIMEOUT] test-command argument ran past {check.STAGE_LIMIT_S}s:"
        " killed its process tree\n"
        "[FAIL] test-command argument (2.25s)\n"
    )


def test_an_interrupt_kills_the_running_stage_and_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(check, "_running", set())
    cmd = ["test-command"]
    stage = Mock()
    stage.wait = Mock(side_effect=KeyboardInterrupt)
    monkeypatch.setattr(check.subprocess, "Popen", Mock(return_value=stage))
    killed: list[object] = []
    monkeypatch.setattr(check, "_kill_tree", killed.append)
    with pytest.raises(KeyboardInterrupt):
        check._run(cmd, check.REPO)
    assert killed == [stage]
    assert check._running == set()


# The grandchild locks a byte of a shared file, signals ready by writing its own pid, then sleeps:
# only a kill that reaches the whole tree (not just the stage) frees the lock.
_GRANDCHILD = (
    "import os, sys\n"
    "fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT)\n"
    "os.lseek(fd, 0, os.SEEK_SET)\n"
    "if sys.platform == 'win32':\n"
    "    import msvcrt\n"
    "    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)\n"
    "else:\n"
    "    import fcntl\n"
    "    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
    "tmp_ready = sys.argv[2] + '.tmp'\n"
    "with open(tmp_ready, 'w') as ready:\n"
    "    ready.write(str(os.getpid()))\n"
    "os.replace(tmp_ready, sys.argv[2])\n"  # the ready file appears only with its content
    "import time\n"
    "time.sleep(60)\n"
)

# The stage outlives its own launch of the grandchild: taskkill /T can only walk to a live
# parent's children, and a parent still in its own POSIX session is what killpg reaches.
_STAGE = (
    "import subprocess, sys, time\n"
    "subprocess.Popen([sys.executable, *sys.argv[1:]])\n"
    "time.sleep(60)\n"
)

_TRY_LOCK = (
    "import os, sys\n"
    "fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT)\n"
    "os.lseek(fd, 0, os.SEEK_SET)\n"
    "try:\n"
    "    if sys.platform == 'win32':\n"
    "        import msvcrt\n"
    "        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)\n"
    "    else:\n"
    "        import fcntl\n"
    "        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
    "except OSError:\n"
    "    sys.exit(1)\n"
)


def test_kill_tree_ends_the_stage_and_its_grandchild(tmp_path: Path) -> None:
    grandchild_py = tmp_path / "grandchild.py"
    grandchild_py.write_text(_GRANDCHILD, encoding="utf-8")
    lock_path = tmp_path / "lock"
    ready_path = tmp_path / "ready"
    stage = check.subprocess.Popen(
        [sys.executable, "-c", _STAGE, str(grandchild_py), str(lock_path), str(ready_path)],
        start_new_session=sys.platform != "win32",
    )
    pid = None
    try:
        deadline = time.monotonic() + HANG_GUARD_S
        while not ready_path.exists():
            assert time.monotonic() < deadline, "the grandchild never signaled ready"
            time.sleep(0.05)
        pid = int(ready_path.read_text(encoding="utf-8"))

        check._kill_tree(stage)

        deadline = time.monotonic() + HANG_GUARD_S
        released = False
        while time.monotonic() < deadline:
            probe = check.subprocess.run([sys.executable, "-c", _TRY_LOCK, str(lock_path)])
            if probe.returncode == 0:
                released = True
                break
            time.sleep(0.05)
        assert released, "the grandchild's lock outlived _kill_tree"
    finally:
        # A failure before check._kill_tree runs must still leak no stage.
        with contextlib.suppress(OSError):
            stage.kill()
        with contextlib.suppress(OSError):
            stage.wait()
        if pid is not None:
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGTERM)


def test_stop_signals_raise_keyboard_interrupt() -> None:
    # SIGHUP doesn't exist on win32 (same condition as hasattr(signal, "SIGHUP") there); pyright
    # only resolves it typed this way, inline, per platform.
    saved_sigterm = signal.getsignal(signal.SIGTERM)
    saved_sighup = signal.getsignal(signal.SIGHUP) if sys.platform != "win32" else None
    try:
        check._route_stop_signals()
        assert signal.getsignal(signal.SIGTERM) is signal.default_int_handler
        if sys.platform != "win32":
            assert signal.getsignal(signal.SIGHUP) is signal.default_int_handler
    finally:
        signal.signal(signal.SIGTERM, saved_sigterm)
        if sys.platform != "win32":
            signal.signal(signal.SIGHUP, saved_sighup)
