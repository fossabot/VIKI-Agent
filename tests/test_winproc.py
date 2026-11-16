"""winproc: the scrubbed child environment, windowless spawning, and Job reaping."""

from __future__ import annotations

import ast
import asyncio
import io
import os
import re
import shutil
import sys
import time
import tokenize
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from viki_agent import winproc
from viki_agent.voice import voice_worker

REPO = Path(__file__).parents[1]
SRC = REPO / "src" / "viki_agent"
SCRIPTS = REPO / "scripts"
_HAS_GIT = shutil.which("git") is not None

POISONED = {
    "PATH": r"C:\Windows",
    "CLAUDECODE": "1",
    "CLAUDE_CODE_ENTRYPOINT": "claude-vscode",
    "CLAUDE_CODE_SSE_PORT": "1234",
    "claude_code_oauth_token": "leak",  # Windows names are case-insensitive
    "CLAUDE_AGENT_SDK_VERSION": "0.1.0",
    "CLAUDE_PID": "1",
    "CLAUDE_EFFORT": "max",
    "CLAUDE_SESSION_ID": "abc",
    "ANTHROPIC_API_KEY": "sk-placeholder",
    "ANTHROPIC_BASE_URL": "https://proxy.invalid",
    "AI_AGENT": "claude",
    "TRACEPARENT": "00-a-b-01",
    "TRACESTATE": "x",
    "MCP_CONNECTION_NONBLOCKING": "1",
    "ENABLE_IDE_INTEGRATION": "true",
    "VIKI_MATTERMOST_BOT_TOKEN": "synthetic-token",
    "viki_mattermost_url": "https://synthetic.invalid",
    "ViKi_MaTtErMoSt_future": "synthetic-token",
}


def test_child_env_strips_every_leak_and_adds_markers() -> None:
    env = winproc.child_env("sdk-py", POISONED)
    assert env == {
        "PATH": r"C:\Windows",
        "DISABLE_AUTOUPDATER": "1",
        "CLAUDE_AGENT_SDK_CLIENT_APP": "viki-agent",
        "CLAUDE_CODE_ENTRYPOINT": "sdk-py",
    }


def test_child_env_can_leave_updates_on() -> None:
    """For the user's own install the base's DISABLE_AUTOUPDATER stays as it is (absent stays
    absent, "0" stays "0"); by default it is pinned, as for the SDK's copy."""
    markers = {"CLAUDE_AGENT_SDK_CLIENT_APP": "viki-agent", "CLAUDE_CODE_ENTRYPOINT": "sdk-py"}
    for base in ({"PATH": "x"}, {"PATH": "x", "DISABLE_AUTOUPDATER": "0"}):
        assert winproc.child_env("sdk-py", base, pin_updates=False) == {**base, **markers}
        assert winproc.child_env("sdk-py", base) == {**base, **markers, "DISABLE_AUTOUPDATER": "1"}


_HOME = Path("/home/synthetic")
_LOCAL_BIN = str(_HOME / ".local" / "bin")


def _path_after(
    path: str | None, existing: set[str], platform: str = "darwin"
) -> tuple[str | None, dict[str, str]]:
    env = {"HOME_MARKER": "kept"} | ({} if path is None else {"PATH": path})
    winproc.prepend_user_bin(env, platform=platform, home=_HOME, isdir=existing.__contains__)
    return env.get("PATH"), env


def test_a_finder_launch_gets_the_existing_user_folders_before_its_minimal_path() -> None:
    """A Finder or desktop-menu launch has the system's minimal PATH, so saved checks and
    Claude Code's MCP servers and hooks could not find `npm`, `uv` or Homebrew tools."""
    sep = os.pathsep
    existing = {_LOCAL_BIN, "/opt/homebrew/bin", "/usr/local/bin"}  # no /opt/homebrew/sbin
    path, env = _path_after(sep.join(["/usr/bin", "/bin"]), existing)
    assert path == sep.join([_LOCAL_BIN, "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"])
    assert env["HOME_MARKER"] == "kept"


def test_a_terminal_launch_keeps_its_path_unchanged() -> None:
    """Folders already on PATH are not added again, so a path from a login shell stays as it is."""
    sep = os.pathsep
    start = sep.join(["/opt/homebrew/bin", "/usr/bin", _LOCAL_BIN, "/usr/local/bin"])
    path, _ = _path_after(start, {_LOCAL_BIN, "/opt/homebrew/bin", "/usr/local/bin"}, "linux")
    assert path == start


def test_missing_folders_and_a_missing_path_are_handled() -> None:
    assert _path_after("/usr/bin", set())[0] == "/usr/bin"
    # With no PATH, the default search path stays reachable behind the added folder.
    assert _path_after(None, {"/usr/local/bin"})[0] == os.pathsep.join(
        ["/usr/local/bin", os.defpath]
    )


def test_windows_keeps_its_path() -> None:
    path, _ = _path_after(r"C:\Windows", {"/usr/local/bin"}, "win32")
    assert path == r"C:\Windows"


def test_the_server_prepends_the_user_folders_before_it_reads_its_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The launcher changes os.environ once, so every child (checks, claude) inherits it."""
    from viki_agent import __main__ as launcher

    seen: list[object] = []
    monkeypatch.setattr(launcher.winproc, "prepend_user_bin", seen.append)
    monkeypatch.setattr(launcher.winproc, "scrub_env", lambda env: (_ for _ in ()).throw(KeyError))
    with pytest.raises(KeyError):
        launcher._main([], [])
    assert seen == [os.environ]


def _program(path: Path) -> Path:
    """A stand-in executable at `path` that `resolve_exe` accepts; nothing runs it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"#!/bin/sh\n")
    path.chmod(0o755)
    return path


def test_find_on_path_uses_absolute_entries_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty and relative entries are skipped, so the current folder never answers; the first
    absolute entry that `resolve_exe` accepts does, its surrounding quotes dropped on Windows."""
    for folder in ("relative", "first", "second"):
        _program(tmp_path / folder / "tool")
    _program(tmp_path / "tool")  # in the current folder
    (tmp_path / "refused" / "tool").mkdir(parents=True)  # a folder, not a program
    monkeypatch.chdir(tmp_path)
    skipped = ["", "  ", ".", "relative", str(tmp_path / "missing"), str(tmp_path / "refused")]
    first = f'"{tmp_path / "first"}"' if sys.platform == "win32" else str(tmp_path / "first")
    path = os.pathsep.join([*skipped, f" {first} ", str(tmp_path / "second")])
    assert winproc.find_on_path("tool", path) == tmp_path / "first" / "tool"
    assert winproc.find_on_path("tool", os.pathsep.join(skipped)) is None
    assert winproc.find_on_path("tool", None) is None
    if sys.platform == "win32":  # a batch shim: resolve_exe's ValueError skips it too
        _program(tmp_path / "first" / "tool.cmd")
        assert winproc.find_on_path("tool.cmd", path) is None


def test_detached_leaves_windows_its_own_stdio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On Windows the console keeps its own stdin/stdout/stderr and starts in the caller's own
    session: a slip that gave it the null device (as the POSIX branch below gets) would leave
    Sign in's and Open terminal's PowerShell a blank window with no input that exits at once."""
    started: list[dict[str, Any]] = []

    class Popen:
        def __init__(self, argv: list[str], **kwargs: Any) -> None:
            started.append({"argv": list(argv), **kwargs})

        def wait(self) -> int:
            return 0

    monkeypatch.setattr(winproc.subprocess, "Popen", Popen)
    monkeypatch.setattr(winproc, "sys", SimpleNamespace(platform="win32"))
    env = {"PATH": "x"}
    winproc._detached(
        ["claude", "auth"], cwd=tmp_path, env=env, creationflags=winproc.CREATE_NEW_CONSOLE
    )
    [call] = started
    assert (call["stdin"], call["stdout"], call["stderr"]) == (None, None, None)
    assert call["start_new_session"] is False
    assert (call["argv"], call["cwd"], call["env"], call["creationflags"]) == (
        ["claude", "auth"],
        tmp_path,
        env,
        winproc.CREATE_NEW_CONSOLE,
    )


def test_detached_gives_posix_the_null_device_and_its_own_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Off Windows the console shares neither the app's process group nor its terminal or
    pipes: null stdio and a session of its own. The closest existing assertion of this shape
    (darwin only) is indirect, through `open_console` in
    tests/app/test_provider_login.py::test_open_console_on_macos_writes_a_private_command_file;
    this is the direct, linux-platform `_detached` twin to the Windows test above."""
    started: list[dict[str, Any]] = []

    class Popen:
        def __init__(self, argv: list[str], **kwargs: Any) -> None:
            started.append({"argv": list(argv), **kwargs})

        def wait(self) -> int:
            return 0

    monkeypatch.setattr(winproc.subprocess, "Popen", Popen)
    monkeypatch.setattr(winproc, "sys", SimpleNamespace(platform="linux"))
    env = {"PATH": "x"}
    winproc._detached(["claude", "auth"], cwd=tmp_path, env=env, creationflags=0)
    [call] = started
    null = winproc.subprocess.DEVNULL
    assert (call["stdin"], call["stdout"], call["stderr"]) == (null, null, null)
    assert call["start_new_session"] is True
    assert (call["argv"], call["cwd"], call["env"], call["creationflags"]) == (
        ["claude", "auth"],
        tmp_path,
        env,
        0,
    )


@pytest.mark.parametrize("platform", ["win32", "linux", "darwin"])
async def test_scrubbed_bridge_configuration_reaches_no_process_sink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
) -> None:
    env = winproc.scrub_env(POISONED)
    env["PATH"] = str(tmp_path)
    seen: list[dict[str, str]] = []
    monkeypatch.setattr(winproc.os, "environ", env)
    monkeypatch.setattr(winproc, "sys", SimpleNamespace(platform=platform))
    monkeypatch.setattr(winproc, "_OPENER", ("synthetic-open",), raising=False)
    monkeypatch.setattr(
        winproc, "Job", lambda: SimpleNamespace(adopt=lambda _: None, close=lambda: None)
    )
    monkeypatch.setattr(
        winproc.threading, "Thread", lambda **_: SimpleNamespace(start=lambda: None)
    )
    monkeypatch.setattr(winproc, "resolve_exe", lambda path: Path(path))
    monkeypatch.setattr(winproc, "system_dir", lambda: tmp_path)
    monkeypatch.setattr(winproc, "powershell_argv", lambda: ["synthetic-powershell"])

    class Process:
        pid, returncode = 1, 0

        def __init__(self, args: Any, **kwargs: Any) -> None:
            self.args = args
            seen.append(dict(kwargs.get("env", winproc.os.environ)))

        def __enter__(self) -> Process:
            return self

        def __exit__(self, *_: Any) -> None:
            pass

        def communicate(self, **_: Any) -> tuple[str, str]:
            return "", ""

        def wait(self) -> int:
            return 0

    async def async_process(*args: Any, **kwargs: Any) -> Process:
        return Process(args, **kwargs)

    monkeypatch.setattr(winproc.subprocess, "Popen", Process)
    monkeypatch.setattr(winproc.asyncio, "create_subprocess_exec", async_process)
    monkeypatch.setattr(
        winproc.os, "startfile", lambda _: seen.append(dict(winproc.os.environ)), raising=False
    )
    child = await winproc.spawn(["synthetic-cli"], cwd=tmp_path, env=winproc.child_env("sdk-py"))
    child.release()
    winproc.run(["synthetic-probe"], cwd=tmp_path)
    winproc.git(tmp_path, "--version")
    winproc.open_external("https://synthetic.invalid")
    if platform == "win32":
        winproc.open_terminal(tmp_path, env)
        winproc.open_console(
            ["synthetic-cli", "auth", "login", "--claudeai"], cwd=tmp_path, env=env
        )
        winproc.start_shell("synthetic-check-or-preview", cwd=tmp_path, env=env).release()
    assert len(seen) == (7 if platform == "win32" else 4)
    assert all(
        not any(name.upper().startswith("VIKI_MATTERMOST_") for name in child) for child in seen
    )


@pytest.mark.skipif(sys.platform != "win32", reason="the batch-shim refusal is Windows-only")
def test_resolve_exe_refuses_batch_shims_and_missing_files(tmp_path: Path) -> None:
    assert sys.platform == "win32"
    shim = tmp_path / "claude.cmd"
    shim.write_text("@echo off", encoding="utf-8")
    with pytest.raises(ValueError):
        winproc.resolve_exe(shim)
    with pytest.raises(FileNotFoundError):
        winproc.resolve_exe(tmp_path / "claude.exe")
    assert winproc.resolve_exe(sys.executable) == Path(sys.executable)


# Starting a process outside winproc is allowed only in winproc itself and in the dev-tooling
# runners that pyproject's `"scripts/*" = ["TID251"]` ignore was written for: check.py drives ruff,
# pytest and npm before the package is even importable, e2e_server.py builds a scratch git repo
# for a screenshot run, compile.py and compiled_smoke_app.py run Nuitka builds of up to an hour
# with inherited output (the smoke starts the compiled app itself through winproc.spawn), and
# linux_check.py drives git and docker for the Linux check (ADR-056) and never starts the app.
# They are listed by path rather than globbed so a *new* script that spawns
# something fails here — scripts/capture.py is the one thing in this repo that starts the real
# claude.exe against a real vault on a real login, and it must keep going through
# winproc's env scrub and kill-on-close Job.
MAY_SPAWN = {
    "src/viki_agent/winproc.py",
    "scripts/check.py",
    "scripts/e2e_server.py",
    "scripts/compile.py",
    "scripts/compiled_smoke_app.py",
    "scripts/linux_check.py",
}


# The voice worker's audit hook refuses process creation by audit-event name. It names
# those events and starts nothing, as test_names_process_events_only_in_strings pins: outside its
# comments and the string literals that are one of its refused events, the scan still applies.
NAMES_PROCESS_EVENTS = {"src/viki_agent/voice/voice_worker.py": voice_worker._REFUSED_EVENTS}

_PROCESS_API = re.compile(
    r"\bsubprocess\b|create_subprocess|subprocess_exec|subprocess_shell|os\.system|os\.popen"
    r"|\bspawn[lv]p?e?\b|\bexec[lv]p?e?\b|startfile|multiprocessing|webbrowser"
)
_PROCESS_MODULES = (
    "subprocess",
    "multiprocessing",
    "webbrowser",
    "_winapi",
    "_posixsubprocess",
    "pty",
    "concurrent.futures.process",
)
_PROCESS_CALLS = ("system", "popen", "startfile", "exec", "spawn", "posix_spawn", "fork")
_EXEMPT = {"subprocess.Popen", "os.system"}


def _blank_exempt(source: str, exempt: frozenset[str] | set[str]) -> str:
    """The source with every comment, and every string literal whose value is an exempt name,
    replaced by spaces, so offsets and lines stay put. Docstrings, f-strings and every other
    string stay in for the pattern."""
    starts = [0]
    # The lines tokenize reads: str.splitlines also splits at form feeds and U+2028, which would
    # shift every offset after one.
    for line in io.StringIO(source).readlines():
        starts.append(starts[-1] + len(line))
    chars = list(source)
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type == tokenize.COMMENT or (
            tok.type == tokenize.STRING and ast.literal_eval(tok.string) in exempt
        ):
            begin = starts[tok.start[0] - 1] + tok.start[1]
            end = starts[tok.end[0] - 1] + tok.end[1]
            chars[begin:end] = [c if c in "\r\n" else " " for c in chars[begin:end]]
    return "".join(chars)


def _process_api_outside_exempt(source: str, exempt: frozenset[str] | set[str]) -> list[str]:
    found = [m.group() for m in _PROCESS_API.finditer(_blank_exempt(source, exempt))]
    for node in ast.walk(ast.parse(source)):
        imported: list[str] = []
        if isinstance(node, ast.Import):
            imported = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            imported = [module, *(f"{module}.{a.name}" for a in node.names)]
            if module in ("os", "nt", "posix"):
                found += [
                    f"from {module} import {a.name}"
                    for a in node.names
                    if a.name.startswith(_PROCESS_CALLS)
                ]
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "os"
            and node.func.attr.startswith(_PROCESS_CALLS)
        ):
            found.append(f"os.{node.func.attr}()")
        found += [
            f"import {name}"
            for name in imported
            if any(name == m or name.startswith(f"{m}.") for m in _PROCESS_MODULES)
        ]
    return found


def test_only_winproc_starts_processes() -> None:
    offenders = [
        rel
        for p in [*SRC.rglob("*.py"), *SCRIPTS.rglob("*.py")]
        if (rel := p.relative_to(REPO).as_posix()) not in {*MAY_SPAWN, *NAMES_PROCESS_EVENTS}
        and _PROCESS_API.search(p.read_text(encoding="utf-8"))
    ]
    assert offenders == []


def test_names_process_events_only_in_strings() -> None:
    for rel, events in NAMES_PROCESS_EVENTS.items():
        source = (REPO / rel).read_text(encoding="utf-8")
        assert _process_api_outside_exempt(source, events) == [], rel


@pytest.mark.parametrize(
    "source",
    [
        "import subprocess",
        "from subprocess import Popen",
        "os.system('x')",
        "import multiprocessing",
        "subprocess = None",
        "import concurrent.futures.process",
        "from concurrent.futures import process",
        "os.execv('x', [])",
        "os.posix_spawn('x', [], {})",
        "print(f'{subprocess}')",
        'importlib.import_module("subprocess")',
        '__import__("subprocess")',
        '"""Uses subprocess."""',
        "from os import system",
        "print(f'os.system {1 + 1}')",
    ],
)
def test_the_names_only_check_flags_real_use(source: str) -> None:
    assert _process_api_outside_exempt(source, _EXEMPT)


@pytest.mark.parametrize(
    "source",
    [
        'EVENTS = ("subprocess.Popen", "os.system")',
        "# multiprocessing is refused too",
        "from concurrent.futures import ThreadPoolExecutor",
        # form feeds stay inside tokenize's line, so they must not shift the offsets
        "x = 1" + "\x0c" * 12 + "\nEVENTS = ('os.system',)",
    ],
)
def test_the_names_only_check_passes_exempt_names_and_comments(source: str) -> None:
    assert _process_api_outside_exempt(source, _EXEMPT) == []


def test_nothing_reads_the_sdk_message_stream() -> None:
    """Hard rule 5: the transport gives Query control frames only, so receive_messages() would
    wait forever; every other frame goes to the mapper."""
    offenders = [
        p.name for p in SRC.rglob("*.py") if ".receive_messages(" in p.read_text(encoding="utf-8")
    ]
    assert offenders == []


@pytest.mark.skipif(sys.platform != "win32", reason="console windows are Windows-only")
async def test_spawned_child_has_no_console() -> None:
    assert sys.platform == "win32"
    probe = "import ctypes; print(ctypes.windll.kernel32.GetConsoleWindow())"
    child = await winproc.spawn([sys.executable, "-c", probe], cwd=Path.cwd(), env=os.environ)
    try:
        out, _ = await asyncio.wait_for(child.process.communicate(), 20)
        assert out.decode().strip() == "0"
    finally:
        child.release()


@pytest.mark.skipif(sys.platform != "win32", reason="only the Windows Job applies a memory ceiling")
async def test_worker_memory_limit_rejects_excess_allocation_without_killing_parent() -> None:
    assert sys.platform == "win32"
    probe = """try:
    value = bytearray(256 * 1024 * 1024)
except MemoryError:
    print('limited')
else:
    print('unlimited')
"""
    child = await winproc.spawn(
        [sys.executable, "-I", "-c", probe],
        cwd=Path.cwd(),
        env=os.environ,
        memory_limit_bytes=128 << 20,
    )
    try:
        out, _ = await asyncio.wait_for(child.process.communicate(), 20)
        assert child.process.returncode == 0
        assert out.strip() == b"limited"
    finally:
        child.release()


@pytest.mark.parametrize("limit", [True, 0, -1, 1 << 64])
def test_job_rejects_invalid_memory_limit(limit: int) -> None:
    with pytest.raises(ValueError, match="invalid job memory limit"):
        winproc.Job(memory_limit_bytes=limit)


async def test_spawn_without_pipes_uses_the_null_device_and_still_reports_the_exit_code() -> None:
    argv = [sys.executable, "-c", "import sys; print('quiet'); sys.exit(7)"]
    child = await winproc.spawn(argv, cwd=Path.cwd(), env=os.environ, pipes=False)
    try:
        assert child.process.stdin is None and child.process.stdout is None
        assert await asyncio.wait_for(child.process.wait(), 20) == 7
    finally:
        child.release()


_HANDOFF = (
    "import subprocess, sys;"
    "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(0.6)'],"
    " creationflags=0x08000000)"
)


@pytest.mark.skipif(sys.platform != "win32", reason="counts the Windows Job's processes")
async def test_wait_tree_ends_only_when_the_whole_job_is_empty() -> None:
    """A browser hands off to its own process tree and the launched pid exits at once:
    the tree, not the pid, is the lifetime."""
    assert sys.platform == "win32"
    child = await winproc.spawn([sys.executable, "-c", _HANDOFF], cwd=Path.cwd(), env=os.environ)
    try:
        assert await asyncio.wait_for(child.process.wait(), 20) == 0
        assert child.job.active_processes() >= 1  # the grandchild lives on inside the job
        await asyncio.wait_for(child.wait_tree(poll_s=0.05), 20)
        assert child.job.active_processes() == 0
    finally:
        child.release()


_PARENT = (
    "import subprocess, sys, time;"
    "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'],"
    " creationflags=0x08000000);"
    "print(p.pid, flush=True);"
    "time.sleep(float(sys.argv[1]))"
)


async def _until_dead(pid: int) -> None:
    async with asyncio.timeout(10):
        while winproc.process_start_time(pid) is not None:
            await asyncio.sleep(0.05)


async def _spawn_parent(linger_s: float) -> tuple[winproc.Child, int]:
    argv = [sys.executable, "-c", _PARENT, str(linger_s)]
    child = await winproc.spawn(argv, cwd=Path.cwd(), env=os.environ)
    assert child.process.stdout is not None
    line = await asyncio.wait_for(child.process.stdout.readline(), 20)
    grandchild = int(line)
    assert winproc.process_start_time(grandchild) is not None
    return child, grandchild


@pytest.mark.skipif(sys.platform != "win32", reason="the Windows Job and FILETIME start times")
async def test_kill_tree_reaps_grandchildren() -> None:
    assert sys.platform == "win32"
    child, grandchild = await _spawn_parent(120)
    try:
        child.kill_tree()
        await asyncio.wait_for(child.process.wait(), 10)
        await _until_dead(grandchild)
    finally:
        child.release()


@pytest.mark.skipif(sys.platform != "win32", reason="the Windows Job and FILETIME start times")
async def test_release_reaps_orphans_of_a_finished_child() -> None:
    assert sys.platform == "win32"
    child, grandchild = await _spawn_parent(0)
    await asyncio.wait_for(child.process.wait(), 20)
    assert winproc.process_start_time(grandchild) is not None  # orphaned, still alive
    child.release()
    await _until_dead(grandchild)


@pytest.mark.skipif(sys.platform != "win32", reason="FILETIME start times are Windows-only")
def test_process_start_time_is_stable_and_none_when_gone() -> None:
    assert sys.platform == "win32"
    mine = winproc.process_start_time(os.getpid())
    assert isinstance(mine, int) and mine > 0
    assert winproc.process_start_time(os.getpid()) == mine
    assert winproc.process_start_time(0x7FFFFFF0) is None


def test_git_always_skips_optional_locks(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen: list[tuple[list[str], dict[str, Any]]] = []
    exe = tmp_path / ("git.exe" if sys.platform == "win32" else "git")
    exe.touch()
    exe.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path))

    def fake_run(argv: list[str], **kwargs: Any) -> object:
        seen.append((list(argv), dict(kwargs)))
        return type("Done", (), {"returncode": 0, "stdout": "ok\n", "stderr": ""})()

    monkeypatch.setattr(winproc, "run", fake_run)
    assert winproc.git(Path.cwd(), "status", "--porcelain") == "ok\n"

    index = tmp_path / "idx" / "index"
    assert winproc.git(Path.cwd(), "status", "--porcelain", index=index) == "ok\n"

    (argv_plain, kwargs_plain), (argv_indexed, kwargs_indexed) = seen
    assert argv_plain == [
        str(exe),
        "--no-optional-locks",
        "-c",
        "core.quotepath=off",
        "status",
        "--porcelain",
    ]
    assert kwargs_plain.get("env") is None
    assert argv_indexed == [
        str(exe),
        "--no-optional-locks",
        "-c",
        "core.quotepath=off",
        "-c",
        "core.sparseCheckout=false",
        "-c",
        f"core.hooksPath={index.parent}",
        "status",
        "--porcelain",
    ]
    assert kwargs_indexed["env"] == {**os.environ, "GIT_INDEX_FILE": os.fspath(index)}


def test_git_rejects_a_relative_index_before_any_process_starts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fail_run(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("run() must not be called for a relative index")

    monkeypatch.setattr(winproc, "run", fail_run)
    with pytest.raises(ValueError):
        winproc.git(tmp_path, "status", index=Path("relative") / "index")


@pytest.mark.skipif(not _HAS_GIT, reason="git not installed")
def test_git_index_path_follows_the_override_or_defaults_to_dot_git(tmp_path: Path) -> None:
    """`rev-parse --git-path index` is git's own documented way of reporting `GIT_INDEX_FILE`:
    the override comes back unchanged, and the real os.environ never carries it either way."""
    repo = tmp_path / "repo"
    repo.mkdir()
    assert winproc.run(["git", "init", "-q", "-b", "main"], cwd=repo).returncode == 0

    index = tmp_path / "private" / "index"
    index.parent.mkdir()
    out = winproc.git(repo, "rev-parse", "--git-path", "index", index=index)
    assert Path(out.strip()) == index
    assert "GIT_INDEX_FILE" not in os.environ

    out = winproc.git(repo, "rev-parse", "--git-path", "index")
    assert out.strip() == ".git/index"
    assert "GIT_INDEX_FILE" not in os.environ


@pytest.mark.skipif(not _HAS_GIT, reason="git not installed")
def test_git_with_index_leaves_no_post_index_change_marker(tmp_path: Path) -> None:
    """The hardened call must not trip a repo hook. A control run with the same
    `GIT_INDEX_FILE` override but no `core.hooksPath` override proves the hook fires at all."""
    repo = tmp_path / "repo"
    repo.mkdir()
    assert winproc.run(["git", "init", "-q", "-b", "main"], cwd=repo).returncode == 0
    (repo / "f.txt").write_text("x", encoding="utf-8")
    assert winproc.run(["git", "add", "f.txt"], cwd=repo).returncode == 0
    commit = winproc.run(
        [
            "git",
            "-c",
            "user.email=viki@example.com",
            "-c",
            "user.name=viki",
            "commit",
            "-q",
            "-m",
            "x",
        ],
        cwd=repo,
    )
    assert commit.returncode == 0, commit.stderr

    marker = tmp_path / "marker.txt"
    hook = repo / ".git" / "hooks" / "post-index-change"
    hook.write_text(f'#!/bin/sh\necho hit >> "{marker.as_posix()}"\n', encoding="utf-8")
    if sys.platform != "win32":
        hook.chmod(0o755)

    index_a = tmp_path / "idx_a" / "index"
    index_a.parent.mkdir()
    control = winproc.run(
        ["git", "read-tree", "HEAD"],
        cwd=repo,
        env={**os.environ, "GIT_INDEX_FILE": os.fspath(index_a)},
    )
    assert control.returncode == 0, control.stderr
    assert marker.read_text(encoding="utf-8") == "hit\n"  # control: the hook does fire

    index_b = tmp_path / "idx_b" / "index"
    index_b.parent.mkdir()
    winproc.git(repo, "read-tree", "HEAD", index=index_b)
    assert marker.read_text(encoding="utf-8") == "hit\n"  # unchanged: the hardened call ran none


def test_run_env_reaches_a_python_child(tmp_path: Path) -> None:
    argv = [sys.executable, "-c", "import os; print(os.environ.get('VIKI_WINPROC_PROBE', ''))"]
    done = winproc.run(argv, cwd=tmp_path, env={**os.environ, "VIKI_WINPROC_PROBE": "hello"})
    assert done.returncode == 0
    assert done.stdout.strip() == "hello"


def test_run_feeds_input_to_stdin(tmp_path: Path) -> None:
    argv = [sys.executable, "-c", "import sys; print(sys.stdin.read().upper())"]
    done = winproc.run(argv, cwd=tmp_path, input="abc")
    assert (done.returncode, done.stdout.strip()) == (0, "ABC")


@pytest.mark.skipif(sys.platform != "win32", reason="the Windows Job ends git.exe's tree")
def test_run_timeout_is_a_real_bound_for_a_git_wrapper() -> None:
    """git.exe hands the work to a grandchild that inherits the pipes; killing git alone left
    a 1 s timeout waiting 8 s for ping to finish. The Job ends the whole tree at once."""
    assert sys.platform == "win32"
    argv = ["git", "-c", "alias.x=!ping -n 9 127.0.0.1", "x"]
    started = time.monotonic()
    with pytest.raises(Exception, match="timed out"):
        winproc.run(argv, cwd=Path.cwd(), timeout=1.0)
    assert time.monotonic() - started < 3


# The alias starts a detached great-grandchild and exits at once, like a browser handing off.
_ORPHAN = (
    "import subprocess, sys\n"
    "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(20)'],"
    " stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,"
    " creationflags=0x08000000)\n"
    "print(p.pid)\n"
)


@pytest.mark.skipif(sys.platform != "win32", reason="the Windows Job and FILETIME start times")
def test_run_reaps_what_the_command_left_behind(tmp_path: Path) -> None:
    assert sys.platform == "win32"
    script = tmp_path / "orphan.py"
    script.write_bytes(_ORPHAN.encode())
    alias = f"alias.x=!{Path(sys.executable).as_posix()} {script.as_posix()}"
    done = winproc.run(["git", "-c", alias, "x"], cwd=tmp_path, timeout=15.0)
    assert done.returncode == 0, done.stderr
    orphan = int(done.stdout)
    deadline = time.monotonic() + 5
    while winproc.process_start_time(orphan) is not None and time.monotonic() < deadline:
        time.sleep(0.05)  # kill-on-close is asynchronous; poll the real OS state
    assert winproc.process_start_time(orphan) is None
