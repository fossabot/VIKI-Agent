"""Every check in one command.

    uv run --directory <repo> python scripts/check.py            # all checks
    uv run --directory <repo> python scripts/check.py --parallel # overlap Python and frontend
    uv run --directory <repo> python scripts/check.py --py       # Python only
    uv run --directory <repo> python scripts/check.py --fe       # frontend only

The frontend lane (`--fe`, and the frontend half of `--parallel`) also runs desktop's own `lint`
and `test` npm scripts. Exit code 0 only when everything passed.

A stage still running after 30 minutes is killed with its whole process tree and fails.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import shutil
import signal
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from time import perf_counter

REPO = Path(__file__).resolve().parents[1]
FRONTEND = REPO / "frontend"
DESKTOP = REPO / "desktop"
PY_CHECKS = [
    ["ruff", "check", "src", "tests", "scripts"],
    ["ruff", "format", "--check", "src", "tests", "scripts"],
    ["pyright", "--pythonplatform", "Windows"],
    # pyright-posix.json ignores tests/acceptance/test_vault_write_accept.py: that frozen
    # writer acceptance test skips itself off Windows and calls Win32 APIs, which pyright
    # otherwise reports for Linux and Darwin.
    ["pyright", "-p", "pyright-posix.json", "--pythonplatform", "Linux"],
    ["pyright", "-p", "pyright-posix.json", "--pythonplatform", "Darwin"],
    # Up to 12 xdist workers, fewer on a smaller machine; worksteal hung at the end of a run on
    # 2026-10-20.
    ["pytest", "-q", "-n", "auto", "--maxprocesses", "12", "--dist", "load"],
]
FE_SCRIPTS = ["lint", "typecheck", "test", "build", "e2e"]  # npm scripts in frontend/package.json
DESKTOP_SCRIPTS = ["lint", "test"]  # npm scripts in desktop/package.json

# A hung stage (a wedged subprocess, a frozen test run) must not block a person or CI forever.
STAGE_LIMIT_S = 30 * 60
_running: set[subprocess.Popen[bytes]] = set()


def _kill_tree(process: subprocess.Popen[bytes]) -> None:
    # `taskkill /T` misses children of an exited parent and setsid escapes killpg; a Job
    # object or cgroup would not.
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(process.pid)], capture_output=True, check=False
        )
    else:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
    process.kill()  # ends the stage if taskkill failed; a no-op once it has already exited
    process.wait()


def _run(cmd: list[str], cwd: Path) -> bool:
    print(f"\n$ {' '.join(cmd)}", flush=True)
    started = perf_counter()
    process = subprocess.Popen(cmd, cwd=cwd, start_new_session=sys.platform != "win32")
    _running.add(process)
    try:
        passed = process.wait(timeout=STAGE_LIMIT_S) == 0
    except subprocess.TimeoutExpired:
        print(
            f"[TIMEOUT] {' '.join(cmd)} ran past {STAGE_LIMIT_S}s: killed its process tree",
            flush=True,
        )
        _kill_tree(process)
        passed = False
    except BaseException:
        _kill_tree(process)
        raise
    finally:
        _running.discard(process)
    elapsed = perf_counter() - started
    print(f"[{'PASS' if passed else 'FAIL'}] {' '.join(cmd)} ({elapsed:.2f}s)", flush=True)
    return passed


def _run_checks(
    commands: list[tuple[list[str], str]], cwd: Path, stopped: Event | None = None
) -> list[str]:
    failed = []
    try:
        for cmd, label in commands:
            if stopped is not None and stopped.is_set():
                break
            if not _run(cmd, cwd):
                failed.append(label)
    except BaseException:
        if stopped is not None:
            stopped.set()
        raise
    return failed


def _frontend_checks(
    npm: str | None, scripts: list[str], stopped: Event | None = None
) -> list[str]:
    if npm is None:
        return ["npm not found"]
    commands = [([npm, "run", stage], f"npm run {stage}") for stage in scripts]
    return _run_checks(commands, FRONTEND, stopped)


def _desktop_checks(npm: str | None, stopped: Event | None = None) -> list[str]:
    if npm is None:
        return []  # _frontend_checks already reports "npm not found" once; don't duplicate it
    commands = [([npm, "run", stage], f"desktop npm run {stage}") for stage in DESKTOP_SCRIPTS]
    return _run_checks(commands, DESKTOP, stopped)


def _frontend_lane(npm: str | None, stopped: Event | None = None) -> list[str]:
    """Everything the e2e join must wait for: frontend lint through build, then desktop."""
    failed = _frontend_checks(npm, FE_SCRIPTS[:-1], stopped)
    failed += _desktop_checks(npm, stopped)
    return failed


def _route_stop_signals() -> None:
    # Each POSIX stage now runs in its own session, so a SIGTERM or SIGHUP to check.py's group no
    # longer reaches it; raising KeyboardInterrupt instead runs the same tree cleanup.
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    if sys.platform != "win32":  # SIGHUP doesn't exist on win32; same condition as hasattr here
        signal.signal(signal.SIGHUP, signal.default_int_handler)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run every VIKI Agent check.")
    parser.add_argument("--py", action="store_true", help="Python checks only")
    parser.add_argument("--fe", action="store_true", help="frontend checks only")
    parser.add_argument(
        "--parallel", action="store_true", help="overlap Python and frontend checks; e2e runs last"
    )
    args = parser.parse_args()
    both = args.py == args.fe
    python_commands = [(cmd, " ".join(cmd)) for cmd in PY_CHECKS]
    failed: list[str] = []
    if both and args.parallel:
        npm = shutil.which("npm")
        stopped = Event()
        with ThreadPoolExecutor(max_workers=2) as pool:
            try:
                python = pool.submit(_run_checks, python_commands, REPO, stopped)
                frontend = pool.submit(_frontend_lane, npm, stopped)
                # Join both lanes before e2e: Python lifecycle tests also bind port 8765.
                failed += python.result()
                failed += frontend.result()
            except BaseException:
                stopped.set()
                for process in _running.copy():
                    _kill_tree(process)
                raise
        if npm is not None:
            failed += _frontend_checks(npm, FE_SCRIPTS[-1:])
    else:
        if both or args.py:
            failed += _run_checks(python_commands, REPO)
        if both or args.fe:
            npm = shutil.which("npm")
            failed += _frontend_checks(npm, FE_SCRIPTS)
            failed += _desktop_checks(npm)
    print("\n" + ("FAILED: " + ", ".join(failed) if failed else "All checks passed."))
    return 1 if failed else 0


if __name__ == "__main__":
    _route_stop_signals()
    sys.exit(main())
