"""The sign-in console: `control.login_argv`, `winproc.open_console` and the route that opens
Claude Code's own subscription sign-in on the user's click.

A real call opens a window, so only the macOS CI probe makes one, and it runs `touch`, not
`claude`: the other `open_console` tests patch its `_detached` seam (or Popen) and `Job`. Every
route test replaces `winproc.open_console` with a recorder, but one that finds no terminal."""

from __future__ import annotations

import asyncio
import errno
import importlib
import logging
import os
import stat
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from tests.test_server import BASE, BEARER, ORIGIN, _p1_app, _v2_routes, running
from tests.test_winproc import _program
from tests.workspace_code.test_open_terminal import _record
from viki_agent import config, winproc
from viki_agent.app import provider_login
from viki_agent.app.diagnostics import Readiness
from viki_agent.app.provider_login import MAX_LOGIN_REQUESTS, provider_login_routes
from viki_agent.drivers.claude import control

PATH = "/api/v2/provider/login"
CLICK = {**BEARER, "origin": ORIGIN}
# A synthetic install path: PowerShell expands `$b` and the backtick inside "...", not in '...'.
EXE = "C:\\Tools\\a $b`c\\claude.exe"
LEAKED = "CLAUDECODE"
HANG_GUARD_S = 10.0
# What a Linux terminal runs: the argv ($0 and "$@"), then the user's shell, which keeps it open.
KEEP_OPEN = '"$0" "$@"; exec "${SHELL:-/bin/sh}"'
# The contract's refusals: each code's status and fixed message.
REFUSALS = {
    "unauthorized": (401, "Can't authenticate. Open VIKI Agent again."),
    "forbidden_origin": (403, "This window can't do that."),
    "invalid_query": (400, "Check what was sent."),
    "unsupported_platform": (409, "Signing in from VIKI Agent works on Windows, macOS and Linux."),
    "provider_unavailable": (409, "VIKI Agent can't start Claude Code right now."),
    "overloaded": (429, "Busy right now. Wait a moment, then try again."),
    "start_failed": (503, "Couldn't start the sign-in."),
}

Console = tuple[list[str], Path, dict[str, str]]

# Captured here, before the autouse `supported` fixture below ever runs: LOGIN_SUPPORTED is
# computed once, at import time, from sys.platform, so a test that only ever sees it through
# that fixture's override would let a narrowed or broken expression pass unnoticed.
_REAL_LOGIN_SUPPORTED = provider_login.LOGIN_SUPPORTED


def test_login_supported_matches_this_hosts_real_platform() -> None:
    """Every other test in this file sees LOGIN_SUPPORTED patched to True by the `supported`
    fixture below, so a revert to `sys.platform == "win32"` would still pass them all on this
    host. This checks the value captured above, before that fixture ever ran."""
    assert sys.platform in ("win32", "darwin", "linux")
    assert _REAL_LOGIN_SUPPORTED is True


def test_login_supported_recomputes_per_platform_without_reimporting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reload the module under each platform to re-evaluate `sys.platform in (...)` directly, so
    a narrowing mutant (`== "win32"`) fails here on any single host, not only on a non-Windows CI
    run. `importlib.reload` re-executes the module in place rather than importing it again."""
    try:
        for platform in ("win32", "darwin", "linux"):
            monkeypatch.setattr(sys, "platform", platform)
            importlib.reload(provider_login)
            assert provider_login.LOGIN_SUPPORTED is True
        monkeypatch.setattr(sys, "platform", "cygwin")
        importlib.reload(provider_login)
        assert provider_login.LOGIN_SUPPORTED is False
    finally:
        monkeypatch.undo()
        importlib.reload(provider_login)  # back to this host's real value before the next test


@pytest.fixture(autouse=True)
def supported(monkeypatch: pytest.MonkeyPatch) -> None:
    """The route reaches its launch on every platform."""
    monkeypatch.setattr(provider_login, "LOGIN_SUPPORTED", True)


@pytest.fixture
def consoles(monkeypatch: pytest.MonkeyPatch) -> list[Console]:
    """Every console the route would open (it looks `open_console` up at call time)."""
    recorded: list[Console] = []

    def record(argv: Sequence[str], *, cwd: str | os.PathLike[str], env: Mapping[str, str]) -> None:
        recorded.append((list(argv), Path(cwd), dict(env)))

    monkeypatch.setattr(winproc, "open_console", record)
    return recorded


def _held(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[threading.Semaphore, threading.Event, list[list[str]]]:
    """A console start that waits for `gate`: `entered` counts the starts that began, `opened`
    holds the argv of each one that finished."""
    entered, gate = threading.Semaphore(0), threading.Event()
    opened: list[list[str]] = []

    def held(argv: Sequence[str], *, cwd: str | os.PathLike[str], env: Mapping[str, str]) -> None:
        entered.release()
        gate.wait(HANG_GUARD_S)
        opened.append(list(argv))

    monkeypatch.setattr(winproc, "open_console", held)
    return entered, gate, opened


def _app(
    cli: Callable[[], Path | None], workers: set[asyncio.Task[Any]] | None = None
) -> Starlette:
    return Starlette(
        routes=provider_login_routes(
            cli=cli,
            authorized=lambda request: (
                request.headers.get("authorization") == BEARER["authorization"]
            ),
            origins=frozenset({ORIGIN}),
            workers=set() if workers is None else workers,
        )
    )


def _refused(response: Any) -> str:
    """The refusal's code, once its status, fixed message and no-store are checked. `Any`:
    Starlette's TestClient answers with httpx2's Response when httpx2 is installed."""
    assert response.headers["cache-control"] == "no-store"
    error = response.json()["error"]
    assert (response.status_code, error["message"]) == REFUSALS[error["code"]], response.text
    return error["code"]


def _console_argv(exe: str) -> list[str]:
    """The argv a click starts for `exe`: the route makes the path absolute first."""
    return control.login_argv([str(Path(exe).absolute())])


# --- the argv and the console ------------------------------------------------------------------


def test_login_argv_is_the_subscription_login_only() -> None:
    argv = control.login_argv(["claude.exe"])
    assert argv == ["claude.exe", "auth", "login", "--claudeai"]
    assert not {"--console", "--sso", "--email"} & set(argv)


@pytest.mark.skipif(sys.platform != "win32", reason="open_console is Windows-only")
def test_open_console_runs_the_argv_in_powershell_outside_any_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _record(monkeypatch)
    env = {"PATH": "x"}
    winproc.open_console(control.login_argv([EXE]), cwd=tmp_path, env=env)
    shell = str(winproc.system_dir() / "WindowsPowerShell" / "v1.0" / "powershell.exe")
    command = "& 'C:\\Tools\\a $b`c\\claude.exe' 'auth' 'login' '--claudeai'"
    assert calls == [
        {
            "argv": [shell, "-NoLogo", "-NoExit", "-Command", command],
            "cwd": tmp_path,
            "env": env,
            "creationflags": winproc.CREATE_NEW_CONSOLE,
        }
    ]
    assert calls[0]["env"] is not env  # a copy


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["C:\\Tools\\it's\\claude.exe", "auth"],
        ["claude.exe", "\u2018"],
        ["claude.exe", "\u2019"],
        ["claude.exe", "\u201a"],
        ["claude.exe", "\u201b"],
        # Wildcards to `&`: a[b]\claude.exe would start a sibling ab\claude.exe if one exists.
        ["C:\\Tools\\a[b]\\claude.exe", "auth"],
        ["C:\\Tools\\a]\\claude.exe", "auth"],
        ["claude.exe", "*"],
        ["claude.exe", "?"],
        # Doubled backtick: `&`'s command lookup unescapes "a``b" into "a`b", a different file.
        ["C:\\Tools\\a``b\\claude.exe", "auth"],
        ["claude.exe", "a\nb"],
        ["claude.exe", "a\tb"],
        ["claude.exe", "\x00"],
        ["claude.exe", "\x1f"],
    ],
    ids=[
        "empty",
        "quote",
        "u2018",
        "u2019",
        "u201a",
        "u201b",
        "brackets",
        "close-bracket",
        "star",
        "question",
        "double-backtick",
        "newline",
        "tab",
        "nul",
        "x1f",
    ],
)
def test_open_console_refuses_what_it_cannot_quote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, argv: list[str]
) -> None:
    calls = _record(monkeypatch)
    monkeypatch.setattr(sys, "platform", "win32")  # the same string check on every host
    with pytest.raises(ValueError):
        winproc.open_console(argv, cwd=tmp_path, env={})
    assert calls == []


@pytest.mark.parametrize("platform", ["win32", "darwin", "linux"])
@pytest.mark.parametrize(
    "argv",
    [[], ["claude", "a\nb"], ["claude", "a\tb"], ["claude\x00", "auth"], ["claude", "\x1f"]],
    ids=["empty", "newline", "tab", "nul", "x1f"],
)
def test_open_console_refuses_control_characters_on_every_os(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform: str, argv: list[str]
) -> None:
    calls = _record(monkeypatch)
    # A terminal Linux would start and a temp folder macOS would write in, were it not refused.
    terminal = _program(tmp_path / "bin" / "xdg-terminal-exec")
    temp = tmp_path / "temp"
    temp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temp))
    monkeypatch.setattr(sys, "platform", platform)
    with pytest.raises(ValueError):
        winproc.open_console(argv, cwd=tmp_path, env={"PATH": str(terminal.parent)})
    assert calls == [] and not any(temp.iterdir())


@pytest.mark.skipif(sys.platform == "win32", reason="the file's POSIX mode")
def test_open_console_on_macos_writes_a_private_command_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Terminal runs a 0700 `.command` in the user's temp folder that removes itself, enters
    the folder, runs the quoted argv and then a login shell; `open` starts detached: a session
    of its own, the null device for stdio, no Job."""
    started: list[dict[str, Any]] = []

    class Popen:
        def __init__(self, argv: list[str], **kwargs: Any) -> None:
            started.append({"argv": argv, **kwargs})

        def wait(self) -> int:
            return 0

    monkeypatch.setattr(winproc.subprocess, "Popen", Popen)
    monkeypatch.setattr(winproc, "Job", lambda *_, **__: pytest.fail("open_console made a Job"))
    temp = tmp_path / "temp"
    temp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temp))
    monkeypatch.setattr(sys, "platform", "darwin")
    home, env = tmp_path / "a home", {"PATH": "x"}
    winproc.open_console(control.login_argv(["/Apps/it's $b`c/claude"]), cwd=home, env=env)
    [script] = temp.iterdir()
    null = winproc.subprocess.DEVNULL
    assert started == [
        {
            "argv": ["/usr/bin/open", "-a", "Terminal", str(script)],
            "cwd": home,
            "env": env,
            "creationflags": 0,
            "close_fds": True,
            "stdin": null,
            "stdout": null,
            "stderr": null,
            "start_new_session": True,
        }
    ]
    assert started[0]["env"] is not env  # a copy
    assert script.name.startswith("viki-sign-in-") and script.suffix == ".command"
    assert stat.S_IMODE(script.stat().st_mode) == 0o700
    lines = [
        "#!/bin/sh",
        'rm -f -- "$0"',
        f"cd -- '{home}' || exit",
        "'/Apps/it'\"'\"'s $b`c/claude' auth login --claudeai",
        'exec "${SHELL:-/bin/sh}" -l',
    ]
    assert script.read_bytes() == "".join(f"{line}\n" for line in lines).encode()


def test_a_command_file_that_cannot_be_written_is_removed(tmp_path: Path) -> None:
    # A lone surrogate has no UTF-8 form, so the write fails once the file exists.
    with pytest.raises(UnicodeEncodeError):
        winproc._command_file(["claude", "\udc80"], tmp_path, str(tmp_path))
    assert not any(tmp_path.iterdir())


@pytest.mark.skipif(sys.platform == "win32", reason="runs the private POSIX script without a GUI")
def test_command_file_does_not_run_in_another_folder_when_cd_fails(tmp_path: Path) -> None:
    marker = tmp_path / "should-not-exist"
    script = winproc._command_file(
        ["/usr/bin/touch", str(marker)], tmp_path / "missing", str(tmp_path)
    )
    done = winproc.run(["/bin/sh", str(script)], cwd=tmp_path, env={"SHELL": "/bin/true"})
    assert done.returncode != 0
    assert not marker.exists()
    assert not script.exists()


@pytest.mark.skipif(
    sys.platform != "darwin" or not os.environ.get("CI"),
    reason="opens a real Terminal window: macOS CI only",
)
def test_terminal_runs_the_command_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    marker = tmp_path / "marker"
    winproc.open_console(["/usr/bin/touch", str(marker)], cwd=tmp_path, env=os.environ)
    deadline = time.monotonic() + HANG_GUARD_S
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.05)  # Terminal runs the file on its own time: poll the real file
    assert marker.exists()
    assert not list(tmp_path.glob("*.command"))  # it removed itself before the argv ran


def test_open_console_runs_the_argv_in_the_linux_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """xdg-terminal-exec wins wherever it is on PATH; x-terminal-emulator takes the command
    after -e. PowerShell's refusals do not apply: sh gets each element as one argument."""
    calls = _record(monkeypatch)
    monkeypatch.setattr(sys, "platform", "linux")
    emulator = _program(tmp_path / "first" / "x-terminal-emulator")
    xdg = _program(tmp_path / "second" / "xdg-terminal-exec")
    env = {"PATH": os.pathsep.join([str(emulator.parent), str(xdg.parent)])}
    argv = control.login_argv(["/opt/it's [a]*?``b/claude"])
    winproc.open_console(argv, cwd=tmp_path, env=env)
    xdg.unlink()
    winproc.open_console(argv, cwd=tmp_path, env=env)
    shell = ["/bin/sh", "-c", KEEP_OPEN, *argv]
    assert calls == [
        {"argv": [str(xdg), *shell], "cwd": tmp_path, "env": env, "creationflags": 0},
        {"argv": [str(emulator), "-e", *shell], "cwd": tmp_path, "env": env, "creationflags": 0},
    ]
    assert all(call["env"] is not env for call in calls)  # copies


def test_open_console_without_a_terminal_starts_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Neither launcher on PATH, or no PATH: OSError ENOENT before anything starts, and the
    route answers start_failed."""
    calls = _record(monkeypatch)
    folder = tmp_path / "bin"
    _program(folder / "xterm")  # a terminal, but not a launcher open_console looks for
    (folder / "xdg-terminal-exec").mkdir()  # a folder of that name is not a program
    # Only winproc sees Linux, so the route's event loop keeps the real platform.
    monkeypatch.setattr(winproc, "sys", SimpleNamespace(platform="linux"))
    for env in ({"PATH": str(folder)}, {}):
        with pytest.raises(OSError) as refused:
            winproc.open_console(control.login_argv(["claude"]), cwd=tmp_path, env=env)
        assert refused.value.errno == errno.ENOENT
    monkeypatch.setenv("PATH", str(folder))
    with TestClient(_app(lambda: Path(EXE)), base_url=BASE) as client:
        assert _refused(client.post(PATH, headers=CLICK)) == "start_failed"
    assert calls == []


# --- the route ---------------------------------------------------------------------------------


@pytest.mark.parametrize("updates", [None, "0"], ids=["unset", "zero"])
def test_a_click_opens_one_sign_in_console(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, consoles: list[Console], updates: str | None
) -> None:
    monkeypatch.setenv(LEAKED, "1")
    # Not the SDK's copy: the user's own update setting stays, set or not.
    monkeypatch.delenv("DISABLE_AUTOUPDATER", raising=False)
    if updates is not None:
        monkeypatch.setenv("DISABLE_AUTOUPDATER", updates)
    with TestClient(_p1_app(tmp_path), base_url=BASE) as client:
        response = client.post(PATH, headers=CLICK)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"opened": True}
    # `_p1_app` configures the CLI as "fake", a relative path its readiness lets start.
    [(argv, cwd, env)] = consoles
    assert argv == _console_argv("fake") and Path(argv[0]).is_absolute()
    assert cwd == Path.home()
    assert env == winproc.scrub_env(os.environ)
    assert env.get("DISABLE_AUTOUPDATER") == updates
    assert LEAKED not in env


def test_the_sdk_copy_signs_in_with_updates_pinned(
    monkeypatch: pytest.MonkeyPatch, consoles: list[Console]
) -> None:
    monkeypatch.setattr(config, "BUNDLED_CLI", Path(EXE))
    monkeypatch.setenv("DISABLE_AUTOUPDATER", "0")  # the pin replaces the user's value
    with TestClient(_app(lambda: Path(EXE)), base_url=BASE) as client:
        assert client.post(PATH, headers=CLICK).status_code == 200
    [(argv, _, env)] = consoles
    assert argv == _console_argv(EXE)
    assert env == {**winproc.scrub_env(os.environ), "DISABLE_AUTOUPDATER": "1"}


def test_the_app_refuses_while_readiness_may_not_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, consoles: list[Console]
) -> None:
    """CLAUDE_CONFIG_DIR set, or a missing CLI or a shim: the one spawn predicate says no."""
    monkeypatch.setattr(Readiness, "may_spawn", lambda _self: False)
    with TestClient(_p1_app(tmp_path), base_url=BASE) as client:
        response = client.post(PATH, headers=CLICK)
    assert _refused(response) == "provider_unavailable"
    assert consoles == []


async def test_refusals_open_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every refusal, in the contract's order: with all their faults at once a click gets the
    first, and each fix uncovers the next. Only the two clicks that fill the limiter open a
    console, and a third once they finish: each gives its permit back."""
    entered, gate, opened = _held(monkeypatch)
    exe: list[Path | None] = [Path(EXE)]
    workers: set[asyncio.Task[Any]] = set()
    app = _app(lambda: exe[0], workers)
    seen: list[str] = []
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE) as client:
        busy = [
            asyncio.create_task(client.post(PATH, headers=CLICK)) for _ in range(MAX_LOGIN_REQUESTS)
        ]
        for _ in busy:
            assert await asyncio.to_thread(entered.acquire, timeout=HANG_GUARD_S)
        assert len(workers) == MAX_LOGIN_REQUESTS
        monkeypatch.setattr(provider_login, "LOGIN_SUPPORTED", False)
        exe[0] = None
        query = PATH + "?x=1"
        for headers in ({"origin": ORIGIN}, BEARER, {**BEARER, "origin": "http://evil.example"}):
            seen.append(_refused(await client.post(query, headers=headers)))
        seen.append(_refused(await client.post(query, headers=CLICK)))
        monkeypatch.setattr(provider_login, "LOGIN_SUPPORTED", True)
        seen.append(_refused(await client.post(query, headers=CLICK)))
        seen.append(_refused(await client.post(PATH, headers=CLICK)))
        exe[0] = Path(EXE)
        seen.append(_refused(await client.post(PATH, headers=CLICK)))
        gate.set()
        assert [answer.status_code for answer in await asyncio.gather(*busy)] == [200, 200]
        assert (await client.post(PATH, headers=CLICK)).status_code == 200
    assert seen == [
        "unauthorized",
        "forbidden_origin",
        "forbidden_origin",
        "unsupported_platform",
        "invalid_query",
        "provider_unavailable",
        "overloaded",
    ]
    assert opened == [_console_argv(EXE)] * (MAX_LOGIN_REQUESTS + 1) and not workers


@pytest.mark.parametrize(
    "error",
    [OSError("no console"), ValueError("no quote"), RuntimeError("odd")],
    ids=["os-error", "value-error", "other"],
)
def test_a_console_that_cannot_start_answers_start_failed(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, error: Exception
) -> None:
    def fail(argv: Sequence[str], *, cwd: str | os.PathLike[str], env: Mapping[str, str]) -> None:
        raise error

    monkeypatch.setattr(winproc, "open_console", fail)
    app = _app(lambda: Path(EXE))
    clicks = MAX_LOGIN_REQUESTS + 1  # one more than the limiter holds: each gives its permit back
    with caplog.at_level(logging.WARNING), TestClient(app, base_url=BASE) as client:
        responses = [client.post(PATH, headers=CLICK) for _ in range(clicks)]
    assert [_refused(response) for response in responses] == ["start_failed"] * clicks
    logged = [r.getMessage() for r in caplog.records if r.name == provider_login.__name__]
    unexpected = not isinstance(error, OSError | ValueError)
    warning = f"sign-in console failed ({type(error).__name__})"
    assert logged == ([warning] * clicks if unexpected else [])
    texts = "".join(response.text for response in responses) + caplog.text
    assert str(error) not in texts and EXE not in caplog.text


async def test_a_client_that_leaves_does_not_cancel_the_sign_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered, gate, opened = _held(monkeypatch)
    workers: set[asyncio.Task[Any]] = set()
    app = _app(lambda: Path(EXE), workers)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE) as client:
        request = asyncio.create_task(client.post(PATH, headers=CLICK))
        assert await asyncio.to_thread(entered.acquire, timeout=HANG_GUARD_S)
        [worker] = workers
        request.cancel()
        gate.set()
        with pytest.raises(asyncio.CancelledError):
            await request
        # The client is gone; the console still starts and the click is answered.
        await asyncio.wait([worker], timeout=HANG_GUARD_S)
    assert not worker.cancelled() and worker.result().status_code == 200
    assert opened == [_console_argv(EXE)] and not workers


def test_the_route_registers_with_readiness_only(tmp_path: Path) -> None:
    login = (PATH, "POST")
    assert login in _v2_routes(_p1_app(tmp_path / "p1"))
    with running(tmp_path / "bare") as app:
        assert login not in _v2_routes(cast(Starlette, app.client.app))
