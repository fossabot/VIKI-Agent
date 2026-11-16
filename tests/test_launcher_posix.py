"""The launcher's POSIX voice and port: alert argv per OS, the alert fallback, the port rebind,
the descriptor limit, the private redirect page that keeps the token out of the browser's
argv, the stop signals, and the browser an earlier launch left running."""

from __future__ import annotations

import asyncio
import contextlib
import errno
import html
import io
import logging
import os
import signal
import socket
import stat
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit

import pytest
import uvicorn

from viki_agent import __main__ as launcher
from viki_agent import config, protection, winproc
from viki_agent.window import WindowManager

# Guards a real launcher's start-up and stop (a second or two of imports and teardown), and an
# orphan's reaping; never a bound on work.
HANG_GUARD_S = 30

_TEXT = 'a "quoted" line; $(id) `x`\nsecond line'


def test_macos_alert_passes_the_text_as_one_argument_to_a_fixed_script() -> None:
    (argv,) = launcher._alert_argvs("darwin", _TEXT)
    assert argv == [
        "/usr/bin/osascript",
        "-e",
        "on run argv",
        "-e",
        'display alert "VIKI Agent" message (item 1 of argv) as critical',
        "-e",
        "end run",
        _TEXT,
    ]


def test_linux_alert_tries_zenity_then_kdialog_with_the_text_as_one_argument() -> None:
    assert launcher._alert_argvs("linux", _TEXT) == [
        ["zenity", "--error", "--no-markup", "--title=VIKI Agent", "--text=" + _TEXT],
        ["kdialog", "--title", "VIKI Agent", "--error", _TEXT],
    ]
    for argv in launcher._alert_argvs("linux", _TEXT) + launcher._alert_argvs("darwin", _TEXT):
        assert argv[0] not in ("sh", "/bin/sh", "bash")
        assert sum(_TEXT in arg for arg in argv) == 1


def test_windows_alert_has_no_argv_because_it_is_a_message_box() -> None:
    assert launcher._alert_argvs("win32", _TEXT) == []


def test_the_window_argv_adds_speech_dispatcher_before_app_on_linux_only() -> None:
    edge, profile, url = Path("edge"), Path("profile"), "http://127.0.0.1:8765/"
    base = launcher.edge_argv(edge, profile, url)
    linux = launcher.window_argv(edge, profile, url, "linux")
    assert linux == [*base[:-1], "--enable-speech-dispatcher", base[-1]]
    assert launcher.window_argv(edge, profile, url, "darwin") == base
    assert launcher.window_argv(edge, profile, url, "win32") == base


def _linux_argvs(platform: str, text: str) -> list[list[str]]:
    del platform
    return [
        ["zenity", "--error", "--no-markup", "--title=VIKI Agent", "--text=" + text],
        ["kdialog", "--title", "VIKI Agent", "--error", text],
    ]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX alert")
def test_alert_writes_stderr_and_falls_back_from_a_missing_zenity_to_kdialog(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert sys.platform != "win32"
    ran: list[tuple[list[str], object, float]] = []

    def fake_run(argv: Sequence[str], *, cwd: object, timeout: float = 15.0) -> None:
        ran.append((list(argv), cwd, timeout))
        if argv[0] == "zenity":
            raise FileNotFoundError(argv[0])

    monkeypatch.setattr(launcher, "_alert_argvs", _linux_argvs)
    monkeypatch.setattr(launcher.winproc, "run", fake_run)

    launcher._alert(_TEXT)

    assert [argv[0] for argv, _, _ in ran] == ["zenity", "kdialog"]
    assert all(cwd == Path.home() and t == launcher.ALERT_TIMEOUT_S for _, cwd, t in ran)
    assert capsys.readouterr().err == _TEXT + "\n"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX alert")
def test_alert_stops_at_an_error_that_is_not_a_missing_program(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hung dialog (a timeout) must not open a second one after it."""
    assert sys.platform != "win32"
    ran: list[str] = []

    def hung(argv: Sequence[str], *, cwd: object, timeout: float = 15.0) -> None:
        ran.append(argv[0])
        raise RuntimeError("timed out")

    monkeypatch.setattr(launcher, "_alert_argvs", _linux_argvs)
    monkeypatch.setattr(launcher.winproc, "run", hung)

    launcher._alert(_TEXT)

    assert ran == ["zenity"]


class _ClosedTerminal(io.StringIO):
    def write(self, s: str) -> int:
        raise OSError(errno.EIO, "the terminal is gone")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX alert")
def test_a_stderr_that_cannot_be_written_still_gets_the_dialog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A launch started from a terminal that has since closed: its stderr raises on write."""
    assert sys.platform != "win32"
    ran: list[str] = []
    monkeypatch.setattr(sys, "stderr", _ClosedTerminal())
    monkeypatch.setattr(launcher, "_alert_argvs", _linux_argvs)
    monkeypatch.setattr(launcher.winproc, "run", lambda argv, **_: ran.append(argv[0]))

    launcher._alert(_TEXT)

    assert ran == ["zenity"]


def test_the_left_open_message_names_the_quit_key_only_on_macos() -> None:
    mac = launcher._msg_browser_left_open("darwin")
    linux = launcher._msg_browser_left_open("linux")
    assert "⌘Q" in mac and "⌘Q" not in linux
    assert "browser" in mac and "browser" in linux


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX rebind rule")
def test_listen_rebinds_over_time_wait_but_not_over_a_live_listener() -> None:
    """The server side closes first, so its end of the connection sits in TIME_WAIT on the
    port; a relaunch must still bind it. A live listener must still refuse it."""
    assert sys.platform != "win32"
    first = launcher._listen(config.HOST, 0)
    assert first is not None
    port = first.getsockname()[1]
    with socket.create_connection((config.HOST, port), timeout=1.0) as client:
        served, _ = first.accept()
        served.close()  # the side that closes first keeps TIME_WAIT
        assert client.recv(1) == b""
    first.close()

    again = launcher._listen(config.HOST, port)
    assert again is not None
    try:
        assert launcher._listen(config.HOST, port) is None
    finally:
        again.close()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX RLIMIT_NOFILE")
def test_nofile_limit_is_raised_to_4096_under_an_infinite_hard_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import resource

    assert sys.platform != "win32"
    calls: list[tuple[int, tuple[int, int]]] = []
    monkeypatch.setattr(resource, "getrlimit", lambda which: (256, resource.RLIM_INFINITY))
    monkeypatch.setattr(resource, "setrlimit", lambda which, limits: calls.append((which, limits)))

    launcher._raise_nofile_limit()

    assert calls == [(resource.RLIMIT_NOFILE, (4096, resource.RLIM_INFINITY))]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX RLIMIT_NOFILE")
def test_nofile_limit_is_capped_at_a_lower_hard_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    import resource

    assert sys.platform != "win32"
    calls: list[tuple[int, tuple[int, int]]] = []
    monkeypatch.setattr(resource, "getrlimit", lambda which: (256, 1024))
    monkeypatch.setattr(resource, "setrlimit", lambda which, limits: calls.append((which, limits)))

    launcher._raise_nofile_limit()

    assert calls == [(resource.RLIMIT_NOFILE, (1024, 1024))]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX RLIMIT_NOFILE")
def test_nofile_limit_already_above_4096_is_left_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    import resource

    assert sys.platform != "win32"
    calls: list[tuple[int, tuple[int, int]]] = []
    monkeypatch.setattr(resource, "getrlimit", lambda which: (8192, resource.RLIM_INFINITY))
    monkeypatch.setattr(resource, "setrlimit", lambda which, limits: calls.append((which, limits)))

    launcher._raise_nofile_limit()

    assert calls == []


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX RLIMIT_NOFILE")
def test_an_unlimited_soft_nofile_limit_is_left_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    """CPython reports RLIM_INFINITY as -1 on Linux, which compares below 4096."""
    import resource

    assert sys.platform != "win32"
    calls: list[tuple[int, tuple[int, int]]] = []
    unlimited = (resource.RLIM_INFINITY, resource.RLIM_INFINITY)
    monkeypatch.setattr(resource, "getrlimit", lambda which: unlimited)
    monkeypatch.setattr(resource, "setrlimit", lambda which, limits: calls.append((which, limits)))

    launcher._raise_nofile_limit()

    assert calls == []


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX RLIMIT_NOFILE")
def test_a_setrlimit_error_does_not_stop_the_launch_path(monkeypatch: pytest.MonkeyPatch) -> None:
    import resource

    assert sys.platform != "win32"
    monkeypatch.setattr(resource, "getrlimit", lambda which: (256, resource.RLIM_INFINITY))

    def raising(which: int, limits: tuple[int, int]) -> None:
        raise OSError("nope")

    monkeypatch.setattr(resource, "setrlimit", raising)

    launcher._raise_nofile_limit()  # must not raise


_TARGET = "http://127.0.0.1:8765/#token="


def _cfg(tmp_path: Path) -> config.Config:
    data = tmp_path / "data"
    data.mkdir()
    return config.Config(
        vault=tmp_path / "vault",
        claude_home=tmp_path / "home",
        cli=tmp_path / "claude",
        edge=tmp_path / "browser",
        data_dir=data,
    )


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX redirect page")
def test_the_redirect_page_is_private_and_holds_the_escaped_target(tmp_path: Path) -> None:
    assert sys.platform != "win32"
    run = tmp_path / "run"
    target = _TARGET + 'a&b"<c>'
    old = os.umask(0o022)  # a desktop session's umask; this test session runs under 077
    try:
        uri = launcher._redirect_url(run, target)
    finally:
        os.umask(old)

    (page,) = run.iterdir()
    assert uri == page.as_uri()
    assert page.name.startswith("open-") and page.suffix == ".html"
    assert stat.S_IMODE(run.stat().st_mode) == 0o700
    assert stat.S_IMODE(page.stat().st_mode) == 0o600
    text = page.read_text(encoding="utf-8")
    assert text == (
        '<!doctype html><meta charset="utf-8"><meta name="referrer" content="no-referrer">'
        f'<meta http-equiv="refresh" content="0;url={html.escape(target)}">'
    )
    assert 'content="0;url=http://127.0.0.1:8765/#token=a&amp;b&quot;&lt;c&gt;"' in text


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX redirect page")
def test_a_run_folder_others_can_read_gets_no_page(tmp_path: Path) -> None:
    assert sys.platform != "win32"
    run = tmp_path / "run"
    run.mkdir()
    run.chmod(0o755)

    with pytest.raises(protection.ProtectionError) as refused:
        launcher._redirect_url(run, _TARGET + "canary-7f3a")

    # Not the word "secret": the Linux message names the secret-tool program.
    assert "canary-7f3a" not in str(refused.value)
    assert list(run.iterdir()) == []


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX redirect page")
def test_a_relative_run_folder_still_gets_an_absolute_page_uri(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A relative VIKI_DATA_DIR (config.data_dir keeps it relative) must not turn every launch
    into MSG_NO_WINDOW: Path.as_uri() raises for a relative path, so the page is written
    relative and only its URI is made absolute, against the process's cwd, last."""
    assert sys.platform != "win32"
    monkeypatch.chdir(tmp_path)

    uri = launcher._redirect_url(Path("run"), _TARGET + "abs")

    (page,) = (tmp_path / "run").iterdir()
    assert uri == page.absolute().as_uri()
    assert uri.startswith("file:///") and page.name in uri


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX redirect page")
def test_an_unprivate_run_folder_is_logged_by_name_with_no_token(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    assert sys.platform != "win32"
    run = tmp_path / "run"
    run.mkdir()
    run.chmod(0o755)

    with caplog.at_level(logging.WARNING), pytest.raises(protection.ProtectionError):
        launcher._redirect_url(run, _TARGET + "secret")

    messages = [r.getMessage() for r in caplog.records]
    assert any(str(run) in m for m in messages)
    assert all("secret" not in m for m in messages)


class _Window:
    async def wait_tree(self) -> None:
        await asyncio.Event().wait()  # open until stop() cancels the tracker

    def kill_tree(self) -> None:
        pass

    def release(self) -> None:
        pass


class _KilledWindow(_Window):
    def __init__(self) -> None:
        self.kills = 0

    def kill_tree(self) -> None:
        self.kills += 1


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX redirect page")
def test_each_window_gets_its_own_page_and_no_argv_element_carries_the_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert sys.platform != "win32"
    cfg = _cfg(tmp_path)
    assert cfg.edge is not None
    token = launcher.token_from({})
    argvs: list[list[str]] = []

    async def spawn(
        argv: Sequence[str], *, cwd: object, env: Mapping[str, str], pipes: bool
    ) -> _Window:
        argvs.append(list(argv))
        return _Window()

    async def nothing() -> None:
        pass

    monkeypatch.setattr(launcher.winproc, "spawn", spawn)
    windows = launcher._windows(
        cfg,
        cfg.edge,
        token,
        dev=False,
        env={},
        signal=cast(Any, None),  # open() never reads it; only start()'s watcher does
        on_last_closed=nothing,
    )

    async def open_two() -> None:
        assert await windows.open()
        assert await windows.open()
        await windows.stop()

    asyncio.run(open_two())

    pages = list((cfg.data_dir / "run").glob("open-*.html"))
    assert len(pages) == 2
    apps = sorted(arg for argv in argvs for arg in argv if arg.startswith("--app="))
    assert apps == sorted(f"--app={page.as_uri()}" for page in pages)
    assert not [arg for argv in argvs for arg in argv if token in arg]
    ui_values = []
    for page in pages:
        target = html.unescape(
            page.read_text(encoding="utf-8").partition('content="0;url=')[2].partition('"')[0]
        )
        parsed = urlsplit(target)
        assert (parsed.scheme, parsed.netloc, parsed.path) == ("http", "127.0.0.1:8765", "/")
        assert parsed.fragment == f"token={token}"
        query = parse_qs(parsed.query, keep_blank_values=True)
        assert set(query) == {"ui"} and len(query["ui"]) == 1
        ui_value = query["ui"][0]
        assert uuid.UUID(ui_value).hex == ui_value
        assert token not in parsed.query
        ui_values.append(ui_value)
    assert len(set(ui_values)) == 2


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX redirect page")
@pytest.mark.parametrize("crashes", [False, True])
def test_serve_removes_stale_pages_first_and_its_own_when_it_ends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, crashes: bool
) -> None:
    assert sys.platform != "win32"
    cfg = _cfg(tmp_path)
    run = cfg.data_dir / "run"
    launcher._redirect_url(run, _TARGET + "stale")
    outside = tmp_path / "outside.html"
    outside.write_text("not the app's", encoding="utf-8")
    (run / "open-link.html").symlink_to(outside)
    (run / "keep.txt").write_text("not a page", encoding="utf-8")
    monkeypatch.setattr(launcher.config, "PORT", 0)
    monkeypatch.setattr(launcher, "OPEN_EVENT", f"Local\\viki-test-{uuid.uuid4().hex}-open")
    seen: list[list[str]] = []

    def serve_on(*_args: Any, **_kwargs: Any) -> int:
        seen.append(sorted(p.name for p in run.iterdir()))
        launcher._redirect_url(run, _TARGET + "live")
        if crashes:
            raise RuntimeError("the server crashed")
        return 0

    monkeypatch.setattr(launcher, "_serve_on", serve_on)

    if crashes:
        with pytest.raises(RuntimeError):
            launcher._serve(cfg, {}, args=[], later=[])
    else:
        assert launcher._serve(cfg, {}, args=[], later=[]) == 0

    assert seen == [["keep.txt"]]
    assert sorted(p.name for p in run.iterdir()) == ["keep.txt"]
    assert outside.read_text(encoding="utf-8") == "not the app's"  # the link, not its target


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX redirect page")
def test_clearing_does_not_follow_a_run_folder_that_is_a_link(tmp_path: Path) -> None:
    assert sys.platform != "win32"
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "open-theirs.html").write_text("not the app's", encoding="utf-8")
    (tmp_path / "run").symlink_to(elsewhere, target_is_directory=True)

    launcher._clear_redirects(tmp_path / "run")

    assert [p.name for p in elsewhere.iterdir()] == ["open-theirs.html"]


def _stop_signals() -> tuple[signal.Signals, ...]:
    """Written out, not read from the launcher: a test must not iterate the list it checks."""
    assert sys.platform != "win32"
    return (signal.SIGHUP, signal.SIGINT, signal.SIGQUIT, signal.SIGTERM)


@pytest.fixture
def fallen() -> Iterator[list[int]]:
    """Every stop signal lands here unless the code under test installed its own handler, so a
    missing handler fails the test instead of ending the test process; restored afterwards."""
    assert sys.platform != "win32"
    landed: list[int] = []
    found = {signum: signal.getsignal(signum) for signum in _stop_signals()}
    for signum in found:
        signal.signal(signum, lambda signum, frame: landed.append(signum))
    try:
        yield landed
    finally:
        for signum, handler in found.items():
            signal.signal(signum, handler)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX stop signals")
@pytest.mark.parametrize(
    "names",
    [
        ["SIGHUP"],
        ["SIGINT"],
        ["SIGQUIT"],
        ["SIGTERM"],
        ["SIGTERM", "SIGINT"],
        ["SIGINT", "SIGINT"],
        ["SIGHUP", "SIGQUIT", "SIGTERM"],
    ],
)
def test_every_stop_signal_asks_for_one_graceful_stop_and_none_is_raised_again(
    fallen: list[int], names: list[str]
) -> None:
    """uvicorn's own handling forces the exit on a second SIGINT (skipping the lifespan that
    kills the windows), leaves SIGHUP and SIGQUIT to end the process, and re-raises the signal
    after serving, before the launcher's own cleanup has run."""
    assert sys.platform != "win32"
    stops: list[None] = []
    uvi = launcher._GracefulServer(
        uvicorn.Config("unused:app"), before_exit=lambda: stops.append(None)
    )

    with uvi.capture_signals():
        for name in names:
            signal.raise_signal(getattr(signal, name))

    assert uvi.should_exit is True and uvi.force_exit is False
    assert stops == []  # a handler runs no hook: the shutdown runs it, on the loop
    assert fallen == []
    signal.raise_signal(signal.SIGHUP)  # still the server's until _serve restores its own
    assert fallen == []


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX stop signals")
def test_a_stop_signal_the_launch_inherited_ignored_stays_ignored(fallen: list[int]) -> None:
    """nohup hands the launch SIGHUP ignored so that a closed terminal leaves it running; an
    ignored signal cannot end the process, so it cannot leave a browser behind either."""
    assert sys.platform != "win32"
    signal.signal(signal.SIGHUP, signal.SIG_IGN)  # the fixture puts the handler back
    uvi = launcher._GracefulServer(uvicorn.Config("unused:app"), before_exit=lambda: None)

    with uvi.capture_signals():
        signal.raise_signal(signal.SIGHUP)
        assert signal.getsignal(signal.SIGHUP) == signal.SIG_IGN

    assert uvi.should_exit is False and fallen == []


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX stop signals")
@pytest.mark.parametrize("should_exit", [False, True])
def test_any_stop_runs_the_hook_on_the_loop_before_uvicorns_own_shutdown(
    monkeypatch: pytest.MonkeyPatch, should_exit: bool
) -> None:
    """A signal's stop, or the last window's (should_exit already set), alike."""
    assert sys.platform != "win32"
    order: list[str] = []

    async def uvicorn_shutdown(self: uvicorn.Server, sockets: Any = None) -> None:
        order.append("uvicorn")

    monkeypatch.setattr(uvicorn.Server, "shutdown", uvicorn_shutdown)
    uvi = launcher._GracefulServer(
        uvicorn.Config("unused:app"), before_exit=lambda: order.append("hook")
    )
    uvi.should_exit = should_exit

    asyncio.run(uvi.shutdown())

    assert order == ["hook", "uvicorn"]


class _Hung(Exception):
    pass


@contextlib.contextmanager
def _hang_guard() -> Iterator[None]:
    """Turns a handler blocked for good on a lock into a failure: SIGALRM interrupts the blocked
    acquire, and its handler's exception ends it."""
    assert sys.platform != "win32"

    def hung(signum: int, frame: object) -> None:
        raise _Hung

    previous = signal.signal(signal.SIGALRM, hung)
    signal.setitimer(signal.ITIMER_REAL, HANG_GUARD_S)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


class _SecondSignalInsideSet(threading.Event):
    """An Event whose set() meets a second stop signal while it holds its own lock: under
    `uv run`, a closed terminal or `pkill` delivers each stop signal twice, microseconds apart."""

    def set(self) -> None:
        with cast(Any, self)._cond:
            signal.raise_signal(signal.SIGINT)
        super().set()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX stop signals")
@pytest.mark.parametrize("lands", ["inside_start", "inside_itself"])
def test_a_stop_signal_returns_at_once_wherever_it_lands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fallen: list[int], lands: str
) -> None:
    """A handler that stopped window opening itself took the window manager's Event lock, which
    is not reentrant: a signal landing while start()'s clear() held it, or a second one landing
    inside the first handler's set(), waited on it for good, and only SIGKILL ended the app."""
    assert sys.platform != "win32"

    async def nothing() -> None:
        pass

    async def uvicorn_shutdown(self: uvicorn.Server, sockets: Any = None) -> None:
        pass

    monkeypatch.setattr(uvicorn.Server, "shutdown", uvicorn_shutdown)
    windows = WindowManager(
        spawn=cast(Any, None), argv=lambda: [], cwd=tmp_path, env={}, on_last_closed=nothing
    )
    if lands == "inside_itself":
        windows._stop = _SecondSignalInsideSet()
    uvi = launcher._GracefulServer(
        uvicorn.Config("unused:app"), before_exit=lambda: launcher._stop_opening(windows)
    )

    with _hang_guard(), uvi.capture_signals():
        if lands == "inside_start":
            with cast(Any, windows._stop)._cond:  # what start()'s clear() holds
                signal.raise_signal(signal.SIGTERM)
        else:
            signal.raise_signal(signal.SIGTERM)
        assert uvi.should_exit is True
        asyncio.run(uvi.shutdown())  # the hook, on the loop, with a signal inside its set()

    assert windows._stop.is_set() and fallen == []


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX stop signals")
def test_a_stop_signal_during_the_first_windows_spawn_still_ends_the_launch_as_a_stop(
    tmp_path: Path, fallen: list[int]
) -> None:
    """A Ctrl+C or kill while the browser starts: the window opens, the server stops without
    serving, and the lifespan kills the window. When the handler itself stopped window opening,
    open() killed the new window, start() read that as a failed first window, and the launch
    ended with MSG_NO_WINDOW (exit 6) and its dialog."""
    assert sys.platform != "win32"
    window = _KilledWindow()

    async def spawn(
        argv: Sequence[str], *, cwd: object, env: Mapping[str, str], pipes: bool
    ) -> _Window:
        signal.raise_signal(signal.SIGTERM)
        return window

    async def nothing() -> None:
        pass

    windows = WindowManager(
        spawn=spawn, argv=lambda: ["browser"], cwd=tmp_path, env={}, on_last_closed=nothing
    )

    async def app(scope: Any, receive: Any, send: Any) -> None:
        assert scope["type"] == "lifespan"
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await windows.start()
                await send({"type": "lifespan.startup.complete"})
            else:
                await windows.stop()
                await send({"type": "lifespan.shutdown.complete"})
                return

    uvi = launcher._GracefulServer(
        uvicorn.Config(app, lifespan="on", log_config=None),
        before_exit=lambda: launcher._stop_opening(windows),
    )
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind((config.HOST, 0))
    listener.listen()

    uvi.run(sockets=[listener])  # uvicorn closes the listener

    assert windows.first_failed is False
    assert window.kills == 1 and windows.count == 0
    assert fallen == []


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX stop signals")
def test_serve_puts_back_the_handlers_it_found_as_its_last_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fallen: list[int], runtime: Path
) -> None:
    assert sys.platform != "win32"
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(launcher.config, "PORT", 0)
    monkeypatch.setattr(launcher, "OPEN_EVENT", f"Local\\viki-test-{uuid.uuid4().hex}-open")
    found = {signum: signal.getsignal(signum) for signum in _stop_signals()}
    servers: list[launcher._GracefulServer] = []
    while_clearing: list[object] = []
    clear_redirects = launcher._clear_redirects

    def serve_on(*_args: Any, **_kwargs: Any) -> int:
        servers.append(
            launcher._GracefulServer(uvicorn.Config("unused:app"), before_exit=lambda: None)
        )
        with servers[0].capture_signals():
            pass
        return 0

    def clearing(run_dir: Path) -> None:
        while_clearing.append(signal.getsignal(signal.SIGHUP))
        clear_redirects(run_dir)

    monkeypatch.setattr(launcher, "_serve_on", serve_on)
    monkeypatch.setattr(launcher, "_clear_redirects", clearing)

    assert launcher._serve(cfg, {}, args=[], later=[]) == 0

    # The final cleanup still ran under the server's handler: only then are the found ones back.
    assert while_clearing == [found[signal.SIGHUP], servers[0]._stop]
    assert {signum: signal.getsignal(signum) for signum in _stop_signals()} == found
    signal.raise_signal(signal.SIGHUP)
    assert fallen == [signal.SIGHUP]


@pytest.fixture
def runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The single-instance runtime directory, placed under tmp_path."""
    monkeypatch.setenv("TMPDIR" if sys.platform == "darwin" else "XDG_RUNTIME_DIR", str(tmp_path))
    return tmp_path / "viki-agent"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX open-window FIFO")
async def test_a_signal_stop_opens_no_window_for_a_relaunch_during_the_drain(
    tmp_path: Path, runtime: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A relaunch that sets the open-window signal while the app drains would otherwise get a
    window from an app on its way out. The signal stays open under the watcher, which ends by
    itself, so nothing reads a descriptor closed under it."""
    assert sys.platform != "win32"
    name = f"Local\\viki-test-{uuid.uuid4().hex}-open"
    wake = winproc.Signal.create(name)
    spawned: list[Sequence[str]] = []

    async def spawn(
        argv: Sequence[str], *, cwd: object, env: Mapping[str, str], pipes: bool
    ) -> _Window:
        spawned.append(argv)
        return _Window()

    async def nothing() -> None:
        pass

    windows = WindowManager(
        spawn=spawn,
        argv=lambda: ["browser"],
        cwd=tmp_path,
        env={},
        on_last_closed=nothing,
        signal=wake,
        poll_ms=20,
    )
    try:
        await windows.start()  # the first window, and the watcher
        launcher._stop_opening(windows)
        relaunch = winproc.Signal.open(name)
        assert relaunch is not None
        relaunch.set()
        relaunch.close()

        watcher = windows._thread
        assert watcher is not None
        await asyncio.wait_for(asyncio.to_thread(watcher.join), HANG_GUARD_S)
        for _ in range(5):
            await asyncio.sleep(0)  # an open() the watcher scheduled has run
        assert len(spawned) == 1
        assert "signal is gone" not in caplog.text
    finally:
        await windows.stop()
        wake.close()


@contextlib.contextmanager
def _listening_singleton(profile: Path) -> Iterator[socket.socket]:
    """A stand-in for a browser holding `profile`: a listener on a short path in the temp folder,
    with the profile's SingletonSocket a link to it, as Chromium lays it out."""
    assert sys.platform != "win32"
    folder = Path(tempfile.mkdtemp(prefix="viki-singleton-"))
    holder = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        holder.bind(str(folder / "SingletonSocket"))
        holder.listen()
        profile.mkdir(parents=True, exist_ok=True)
        (profile / "SingletonSocket").symlink_to(folder / "SingletonSocket")
        yield holder
    finally:
        holder.close()
        with contextlib.suppress(FileNotFoundError):
            (folder / "SingletonSocket").unlink()
        folder.rmdir()


def _forbid_serving(*_args: Any, **_kwargs: Any) -> int:
    raise AssertionError("_serve_on must not be called")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX browser singleton")
def test_a_browser_left_running_on_the_profile_stops_the_launch_with_a_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """It would take this launch's window and leave the launch to end at once, silently."""
    assert sys.platform != "win32"
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(launcher.config, "PORT", 0)
    monkeypatch.setattr(launcher, "_serve_on", _forbid_serving)
    monkeypatch.setattr(launcher, "BROWSER_RECHECK_S", 0.0)
    later: list[str] = []
    with _listening_singleton(cfg.edge_profile), caplog.at_level(logging.ERROR):
        assert launcher._serve(cfg, {}, args=[], later=later) == 9

    assert later == [launcher.MSG_BROWSER_LEFT_OPEN]
    assert launcher.MSG_BROWSER_LEFT_OPEN in caplog.text
    assert not (cfg.data_dir / "run").exists()  # stopped before any page or port


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX browser singleton")
def test_a_singleton_nobody_listens_on_does_not_stop_the_launch(tmp_path: Path) -> None:
    """A crash or a restart leaves the link and the socket file behind; nothing answers."""
    assert sys.platform != "win32"
    profile = tmp_path / "edge"
    pauses: list[float] = []
    assert launcher._browser_left_open(profile, pauses.append) is False  # no profile yet

    with _listening_singleton(profile) as holder:
        assert launcher._browser_left_open(profile, pauses.append) is True
        holder.close()
        assert launcher._browser_left_open(profile, pauses.append) is False
    assert launcher._browser_left_open(profile, pauses.append) is False  # a dangling link
    assert pauses == [launcher.BROWSER_RECHECK_S]  # only one that answered gets a second look


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX browser singleton")
def test_a_browser_that_stops_answering_during_the_second_look_is_not_left_open(
    tmp_path: Path,
) -> None:
    """A launch stopped by a signal kills its browser without waiting for it and releases the
    lock a few milliseconds before the browser stops answering: a relaunch right behind it must
    not stop on a browser that is already dying."""
    assert sys.platform != "win32"
    profile = tmp_path / "edge"
    with _listening_singleton(profile) as holder:
        assert launcher._browser_left_open(profile, lambda _s: holder.close()) is False


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX browser singleton")
def test_a_profile_path_longer_than_a_socket_path_still_reaches_the_browser(
    tmp_path: Path,
) -> None:
    """A Unix socket path is limited to about 104 bytes; the profile's link names a short one."""
    assert sys.platform != "win32"
    profile = tmp_path / ("p" * 120) / "edge"
    assert len(os.fsencode(profile / "SingletonSocket")) > 108
    with _listening_singleton(profile):
        assert launcher._browser_left_open(profile, lambda _s: None) is True


# The launcher itself, on a spare port, with an in-memory key, no dialog program and no git.
_LAUNCHER = """\
import secrets, sys
from viki_agent import config, protection
import viki_agent.__main__ as launcher
config.PORT = int(sys.argv[1])
key = secrets.token_bytes(32)
protection.keychain_source = lambda data_dir: (lambda: key)
launcher.git_version = lambda: ""
launcher._alert_argvs = lambda platform, text: []
raise SystemExit(launcher.main(["--dev"]))
"""

# A browser the way Chromium is one: the launched process hands the window to a process in its
# own group and exits. That process holds a lock on stand-in.lock for as long as it lives, and
# writes its pid once it holds it.
_STAND_IN = """\
#!{python}
import fcntl, os, sys, time
profile = next(a.split("=", 1)[1] for a in sys.argv if a.startswith("--user-data-dir="))
if os.fork() == 0:
    held = os.open(os.path.join(profile, "stand-in.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(held, fcntl.LOCK_EX)
    with open(os.path.join(profile, "stand-in.tmp"), "w", encoding="utf-8") as out:
        out.write(str(os.getpid()))
    os.replace(os.path.join(profile, "stand-in.tmp"), os.path.join(profile, "stand-in.pid"))
    time.sleep(600)
"""


async def _poll(ready: Any, what: str) -> None:
    deadline = time.monotonic() + HANG_GUARD_S
    while not ready():
        assert time.monotonic() < deadline, what
        await asyncio.sleep(0.02)


def _lock_is_free(path: Path) -> bool:
    assert sys.platform != "win32"
    import fcntl

    fd = os.open(path, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    finally:
        os.close(fd)
    return True


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX stop signals")
@pytest.mark.parametrize("names", [["SIGHUP"], ["SIGQUIT"], ["SIGTERM"], ["SIGTERM", "SIGINT"]])
async def test_a_signalled_launcher_ends_its_browser_and_leaves_no_page_or_fifo(
    tmp_path: Path, names: list[str]
) -> None:
    """A closed terminal (SIGHUP), Ctrl+backslash (SIGQUIT), kill (SIGTERM), and a Ctrl+C
    during the drain: each used to leave the browser running, and with it every later launch
    handed its window to that browser and exited at once."""
    assert sys.platform != "win32"
    data, home = tmp_path / "data", tmp_path / "home"
    home.mkdir()
    stand_in = tmp_path / "browser"
    stand_in.write_text(_STAND_IN.format(python=sys.executable), encoding="utf-8")
    stand_in.chmod(0o700)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((config.HOST, 0))
        port = probe.getsockname()[1]
    env = {
        **os.environ,
        "HOME": str(home),
        "VIKI_DATA_DIR": str(data),
        "VIKI_EDGE": str(stand_in),
        "VIKI_CLAUDE_CLI": str(tmp_path / "no-such-claude"),
        "TMPDIR" if sys.platform == "darwin" else "XDG_RUNTIME_DIR": str(tmp_path),
    }
    fifo = tmp_path / "viki-agent" / "Local_viki-agent-open.fifo"
    pid_file = data / "edge" / "stand-in.pid"
    child = await winproc.spawn(
        [sys.executable, "-c", _LAUNCHER, str(port)], cwd=tmp_path, env=env, pipes=True
    )
    browser: int | None = None
    try:
        await _poll(pid_file.exists, "the launcher never opened its window")
        browser = int(pid_file.read_text(encoding="utf-8"))
        assert fifo.exists() and list((data / "run").glob("open-*.html"))

        for name in names:
            child.process.send_signal(getattr(signal, name))
        _, err = await asyncio.wait_for(child.process.communicate(), HANG_GUARD_S)

        assert child.process.returncode == 0, err.decode(errors="replace")[-2000:]
        await _poll(lambda: _lock_is_free(data / "edge" / "stand-in.lock"), "the browser survived")
        assert list((data / "run").glob("open-*.html")) == []
        assert not fifo.exists()
    finally:
        child.kill_tree()
        child.release()
        if browser is not None:
            with contextlib.suppress(ProcessLookupError):
                os.kill(browser, signal.SIGKILL)
