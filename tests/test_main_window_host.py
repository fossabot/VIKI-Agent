"""`--window-host=<pid>`: the launcher serves the desktop app's windows over a
`window_host.WindowHost` link instead of Edge — no browser, no redirect page, the token never
touches argv. Covers `_window_host_pid`'s parsing, the open/closed/wake/EOF lifecycle over a link
built on os.pipe pairs, the recovery shell's hosted link, `windows.holds()` naming only the app
pid, and `desktop/policy.mjs`'s trusted origin."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import socket
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO
from urllib.parse import parse_qs, urlsplit

import pytest

from tests.test_main import (
    FakeServer,
    _cfg,
    _forbid,
    _preserve_os_environ,  # noqa: F401 (autouse: restores os.environ main() scrubs)
    _private_instance_names,  # noqa: F401 (autouse: this launch's mutex/signal names are its own)
    _reaching_the_server,
    alerts,  # noqa: F401 (autouse: no real message box may show)
    nofile_raises,  # noqa: F401 (autouse: never raises this pytest process's fd limit for real)
)
from viki_agent import __main__ as launcher
from viki_agent import config
from viki_agent.window import WindowManager

HANG_GUARD_S = 5.0  # a guard on a reader-thread handoff that normally completes in milliseconds
HOST_PID = 4321


@dataclass
class HostedLink:
    """One hosted launch's end of the window-host link: `app` plays the desktop app's write end
    of the server's stdin (closing it says the app is gone), `sent` reads what the server wrote
    to its stdout. `host_reader`/`host_writer` are the server's own ends, kept only to close them
    after the test."""

    host_pid: int
    app: BinaryIO
    sent: BinaryIO
    host_reader: BinaryIO
    host_writer: BinaryIO


@pytest.fixture
def hosted(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[HostedLink]]:
    """Patches `window_host.WindowHost.from_stdio` to link over os.pipe pairs standing in for
    the desktop app's stdio, and forbids `winproc.spawn`: a hosted launch never starts a
    browser. Each hosted launch's link lands in the returned list."""
    monkeypatch.setattr(launcher.winproc, "spawn", _forbid("winproc.spawn"))
    links: list[HostedLink] = []

    def from_stdio(
        host_pid: int, *, wake: Callable[[], None], gone: Callable[[], None]
    ) -> launcher.window_host.WindowHost:
        host_in, app_out = os.pipe()
        app_in, host_out = os.pipe()
        host_reader, host_writer = os.fdopen(host_in, "rb"), os.fdopen(host_out, "wb")
        app, sent = os.fdopen(app_out, "wb"), os.fdopen(app_in, "rb")
        links.append(HostedLink(host_pid, app, sent, host_reader, host_writer))
        return launcher.window_host.WindowHost(
            host_reader, host_writer, host_pid=host_pid, wake=wake, gone=gone
        )

    monkeypatch.setattr(launcher.window_host.WindowHost, "from_stdio", from_stdio)
    yield links
    for link in links:
        for pipe in (link.app, link.sent, link.host_reader, link.host_writer):
            with contextlib.suppress(OSError):
                pipe.close()


async def _line(pipe: BinaryIO, timeout: float = HANG_GUARD_S) -> bytes:
    """The next line `pipe` holds, read off the loop (never a plain blocking call on it): a
    wake's or a close's line is produced by a chain that itself needs the loop free to run
    (the reader thread's call_soon_threadsafe, the watch thread's own task). A missing line
    fails within `timeout` instead of hanging the test."""
    return await asyncio.wait_for(asyncio.to_thread(pipe.readline), timeout)


async def _until(predicate: Callable[[], bool], timeout: float = HANG_GUARD_S) -> None:
    """Poll `predicate` until it holds, bounded by `timeout`: the cross-thread handoff from the
    reader thread to the loop normally lands within milliseconds; only a hang reaches the bound."""

    async def poll() -> None:
        while not predicate():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(poll(), timeout)


def _assert_open_url(url: str, *, token: str, session_id: str | None = None) -> None:
    parsed = urlsplit(url)
    assert (parsed.scheme, parsed.netloc, parsed.path) == ("http", "127.0.0.1:8765", "/")
    query = parse_qs(parsed.query, keep_blank_values=True)
    assert set(query) == {"ui"} and len(query["ui"]) == 1
    assert uuid.UUID(query["ui"][0]).hex == query["ui"][0]
    fragment = f"token={token}" if session_id is None else f"token={token}&session={session_id}"
    assert parsed.fragment == fragment


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        ([], None),
        (["--dev"], None),
        (["--window-host=1"], 1),
        (["--window-host=4321"], 4321),
        (["--dev", "--window-host=7"], 7),
    ],
)
def test_window_host_pid_parses_the_flag(args: list[str], expected: int | None) -> None:
    assert launcher._window_host_pid(args) == expected


@pytest.mark.parametrize(
    "args",
    [
        ["--window-host="],
        ["--window-host=0"],
        ["--window-host=-1"],
        ["--window-host=1.5"],
        ["--window-host=abc"],
        ["--window-host=+1"],
        ["--window-host=0x1"],
        ["--window-host= 1"],
        ["--window-host=1 "],
        ["--window-host=1", "--window-host=1"],
        ["--window-host=1", "--window-host=2"],
    ],
)
def test_window_host_pid_rejects_malformed_values(args: list[str]) -> None:
    with pytest.raises(ValueError):
        launcher._window_host_pid(args)


def test_an_ordinary_launch_never_calls_from_stdio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _reaching_the_server(monkeypatch, _cfg(tmp_path))
    monkeypatch.setattr(
        launcher.window_host.WindowHost, "from_stdio", _forbid("WindowHost.from_stdio")
    )

    assert launcher.main([]) == 0
    assert isinstance(calls["create_app"]["windows"], WindowManager)


def test_a_hosted_launch_needs_no_browser(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    alerts: list[str],  # noqa: F811 (the autouse fixture's own value: no real message box showed)
    hosted: list[HostedLink],
) -> None:
    _reaching_the_server(monkeypatch, _cfg(tmp_path, edge=None))

    assert launcher.main([f"{launcher.WINDOW_HOST_FLAG}{HOST_PID}"]) == 0

    assert alerts == []
    (link,) = hosted
    assert link.host_pid == HOST_PID


def test_hosted_windows_open_over_the_link_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hosted: list[HostedLink]
) -> None:
    """Driven inside a patched `FakeServer.run`, as the other scenarios below are: `_serve`
    closes the launch's signal the instant `uvi.run()` returns, which the fake does at once, so
    anything exercising the real window lifecycle has to run while that call is still on the
    stack — never after `main()` has already returned."""
    cfg = _cfg(tmp_path)
    calls = _reaching_the_server(monkeypatch, cfg)
    session_id = str(uuid.uuid4())
    lines: list[bytes] = []

    def run(self: FakeServer, sockets: list[socket.socket] | None = None) -> None:
        self.ran = True
        windows = calls["create_app"]["windows"]
        (link,) = hosted

        async def scenario() -> None:
            await windows.start()
            lines.append(await _line(link.sent))
            assert await windows.open(session_id=session_id)
            lines.append(await _line(link.sent))
            await windows.stop()

        asyncio.run(scenario())

    monkeypatch.setattr(FakeServer, "run", run)

    assert launcher.main([f"{launcher.WINDOW_HOST_FLAG}{HOST_PID}"]) == 0
    token = calls["create_app"]["token"]

    opened = json.loads(lines[0])
    assert opened["id"] == 1
    _assert_open_url(opened["url"], token=token)

    reopened = json.loads(lines[1])
    assert reopened["id"] == 2
    _assert_open_url(reopened["url"], token=token, session_id=session_id)

    assert not cfg.edge_profile.exists()
    assert list((cfg.data_dir / "run").glob("open-*.html")) == []


def test_the_last_hosted_window_closing_settles_then_stops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hosted: list[HostedLink]
) -> None:
    calls = _reaching_the_server(monkeypatch, _cfg(tmp_path))
    graces: list[float] = []
    closed: list[bool] = []

    class RecordingManager(launcher.SessionManager):
        async def settle(self, grace_s: float, until: Callable[[], bool] | None = None) -> None:
            graces.append(grace_s)
            await super().settle(grace_s, until)

        async def close_all(self) -> None:
            closed.append(True)
            await super().close_all()

    monkeypatch.setattr(launcher, "SessionManager", RecordingManager)
    exited: list[bool] = []

    def run(self: FakeServer, sockets: list[socket.socket] | None = None) -> None:
        self.ran = True
        windows = calls["create_app"]["windows"]
        (link,) = hosted

        async def scenario() -> None:
            await windows.start()
            await _line(link.sent)  # the first open line
            link.app.write(b'{"type":"closed","id":1}\n')
            link.app.flush()
            await _until(lambda: self.should_exit)
            exited.append(self.should_exit)
            await windows.stop()

        asyncio.run(scenario())

    monkeypatch.setattr(FakeServer, "run", run)

    assert launcher.main([f"{launcher.WINDOW_HOST_FLAG}{HOST_PID}"]) == 0
    assert graces == [launcher.WINDOW_GRACE_S]
    assert closed == [True]
    assert exited == [True]


def test_a_wake_from_the_app_opens_another_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hosted: list[HostedLink]
) -> None:
    calls = _reaching_the_server(monkeypatch, _cfg(tmp_path))
    ids: list[int] = []

    def run(self: FakeServer, sockets: list[socket.socket] | None = None) -> None:
        self.ran = True
        windows = calls["create_app"]["windows"]
        (link,) = hosted

        async def scenario() -> None:
            await windows.start()
            ids.append(json.loads(await _line(link.sent))["id"])
            link.app.write(b'{"type":"wake"}\n')
            link.app.flush()
            ids.append(json.loads(await _line(link.sent))["id"])
            await windows.stop()

        asyncio.run(scenario())

    monkeypatch.setattr(FakeServer, "run", run)

    assert launcher.main([f"{launcher.WINDOW_HOST_FLAG}{HOST_PID}"]) == 0
    assert ids == [1, 2]


def test_the_app_leaving_stops_the_launch_without_the_grace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hosted: list[HostedLink]
) -> None:
    calls = _reaching_the_server(monkeypatch, _cfg(tmp_path))
    graces: list[float] = []

    class RecordingManager(launcher.SessionManager):
        async def settle(self, grace_s: float, until: Callable[[], bool] | None = None) -> None:
            graces.append(grace_s)
            await super().settle(grace_s, until)

    monkeypatch.setattr(launcher, "SessionManager", RecordingManager)
    results: dict[str, bool] = {}

    def run(self: FakeServer, sockets: list[socket.socket] | None = None) -> None:
        self.ran = True
        windows = calls["create_app"]["windows"]
        (link,) = hosted

        async def scenario() -> None:
            await windows.start()
            await _line(link.sent)  # the first open line
            link.app.close()  # the app's end of the link: it is gone
            await _until(lambda: windows._stop.is_set())
            results["stop_set"] = windows._stop.is_set()
            results["should_exit"] = self.should_exit
            await windows.stop()

        asyncio.run(scenario())

    monkeypatch.setattr(FakeServer, "run", run)

    assert launcher.main([f"{launcher.WINDOW_HOST_FLAG}{HOST_PID}"]) == 0
    assert results == {"stop_set": True, "should_exit": True}
    assert graces == []


def test_a_hosted_recovery_shell_opens_over_the_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hosted: list[HostedLink]
) -> None:
    cfg = _cfg(tmp_path)
    calls = _reaching_the_server(monkeypatch, cfg)
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    (cfg.data_dir / "app.sqlite3").write_bytes(b"not a database")
    lines: list[bytes] = []

    def run(self: FakeServer, sockets: list[socket.socket] | None = None) -> None:
        self.ran = True
        windows = calls["create_recovery_app"]["windows"]
        (link,) = hosted

        async def scenario() -> None:
            await windows.start()
            lines.append(await _line(link.sent))
            await windows.stop()

        asyncio.run(scenario())

    monkeypatch.setattr(FakeServer, "run", run)

    assert launcher.main([f"{launcher.WINDOW_HOST_FLAG}{HOST_PID}"]) == 0
    assert "create_app" not in calls
    token = calls["create_recovery_app"]["token"]
    opened = json.loads(lines[0])
    assert opened["id"] == 1
    _assert_open_url(opened["url"], token=token)
    assert not cfg.edge_profile.exists()


def test_hosted_windows_hold_the_app_pid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hosted: list[HostedLink]
) -> None:
    calls = _reaching_the_server(monkeypatch, _cfg(tmp_path))
    claims: list[bool] = []

    def run(self: FakeServer, sockets: list[socket.socket] | None = None) -> None:
        self.ran = True
        windows = calls["create_app"]["windows"]

        async def scenario() -> None:
            await windows.start()
            claims.extend(hold(pid) for hold in windows.holds() for pid in (HOST_PID, HOST_PID + 1))
            await windows.stop()

        asyncio.run(scenario())

    monkeypatch.setattr(FakeServer, "run", run)

    assert launcher.main([f"{launcher.WINDOW_HOST_FLAG}{HOST_PID}"]) == 0
    assert claims == [True, False]


def test_the_desktop_app_trusts_exactly_the_server_origin() -> None:
    policy = (launcher._REPO / "desktop" / "policy.mjs").read_text(encoding="utf-8")
    assert f'APP_ORIGIN = "http://{config.HOST}:{config.PORT}"' in policy
