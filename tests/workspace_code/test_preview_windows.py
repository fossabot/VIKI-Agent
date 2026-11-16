"""Preview windows: the argv, a fresh profile per window, the slots, the refusing proxy and the
sweep. A recording spawn stands for Edge; no test starts a browser."""

from __future__ import annotations

import asyncio
import logging
import socket
import sys
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest

import viki_agent.__main__ as launcher
from viki_agent import winproc
from viki_agent.code import preview_windows
from viki_agent.code.code_checks import CodeRunError
from viki_agent.code.preview_windows import MAX_WINDOWS, PreviewWindows, preview_argv

HANG_GUARD_S = 5  # the tracker's profile deletion runs in a thread and takes milliseconds
EDGE = Path("C:/Edge/msedge.exe")
URL = "http://localhost:5173/"
ALLOW = ("localhost:5173", "127.0.0.1:5173", "[::1]:5173")
windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows sockets and junctions")


class FakeWindow:
    """winproc.Child's shape: the tree ends when `ended` is set, which kill_tree does unless
    the window is stubborn."""

    def __init__(self, *, stubborn: bool = False) -> None:
        self.ended = asyncio.Event()
        self.stubborn = stubborn
        self.kills = 0
        self.releases = 0

    async def wait_tree(self) -> None:
        await self.ended.wait()

    def kill_tree(self) -> None:
        self.kills += 1
        if not self.stubborn:
            self.ended.set()

    def release(self) -> None:
        self.releases += 1


@dataclass
class Call:
    argv: list[str]
    cwd: Path
    env: dict[str, str]
    pipes: bool
    cwd_existed: bool


class Spawner:
    """winproc.spawn's shape, recording each call; `fail` raises as a missing browser would."""

    def __init__(self, *, fail: bool = False, stubborn: bool = False) -> None:
        self.calls: list[Call] = []
        self.windows: list[FakeWindow] = []
        self.fail = fail
        self.stubborn = stubborn

    async def __call__(
        self, argv: Sequence[str], *, cwd: str | Path, env: Mapping[str, str], pipes: bool
    ) -> FakeWindow:
        self.calls.append(Call(list(argv), Path(cwd), dict(env), pipes, Path(cwd).is_dir()))
        if self.fail:
            raise OSError("no browser here")
        self.windows.append(FakeWindow(stubborn=self.stubborn))
        return self.windows[-1]


@pytest.fixture
def spawn() -> Spawner:
    return Spawner()


@pytest.fixture
async def windows(tmp_path: Path, spawn: Spawner) -> AsyncIterator[PreviewWindows]:
    opened = PreviewWindows(EDGE, tmp_path / "data", spawn=spawn)
    yield opened
    await opened.close_all()


async def _open(
    windows: PreviewWindows, owner: str = "p1", closed: asyncio.Event | None = None
) -> None:
    await windows.open(
        URL,
        allow=ALLOW,
        owner=owner,
        on_closed=None if closed is None else closed.set,
    )


def _junction(target: Path, link: Path) -> None:
    assert sys.platform == "win32"
    import _winapi

    _winapi.CreateJunction(str(target), str(link))


def test_preview_argv_is_the_app_windows_argv_with_the_preview_switches() -> None:
    profile = Path("C:/Data/preview-edge/abc")
    argv = preview_argv(EDGE, profile, URL, allow=ALLOW, proxy="127.0.0.1:9")
    app = launcher.edge_argv(EDGE, profile, URL)
    assert argv[:7] == app[:7]
    assert argv[-1] == app[-1] == f"--app={URL}"
    assert argv[7:-1] == [
        "--proxy-server=127.0.0.1:9",
        "--proxy-bypass-list=<-loopback>;localhost:5173;127.0.0.1:5173;[::1]:5173",
        "--host-resolver-rules=MAP * ~NOTFOUND , EXCLUDE localhost , EXCLUDE 127.0.0.1",
        "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
    ]


async def test_a_window_gets_a_fresh_profile_a_scrubbed_env_and_no_pipes(
    windows: PreviewWindows, spawn: Spawner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://example.com")
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "cli")
    await _open(windows)
    await _open(windows)
    first, second = spawn.calls
    assert windows.proxy is not None
    for call in spawn.calls:
        assert call.cwd_existed
        assert call.cwd.parent == windows.area
        assert call.pipes is False
        assert call.argv == preview_argv(EDGE, call.cwd, URL, allow=ALLOW, proxy=windows.proxy)
        assert not [k for k in call.env if k.upper().startswith(("ANTHROPIC_", "CLAUDE_CODE_"))]
    assert first.cwd != second.cwd
    assert windows.count == 2


async def test_a_fifth_window_is_refused_without_a_spawn(
    windows: PreviewWindows, spawn: Spawner
) -> None:
    for _ in range(MAX_WINDOWS):
        await _open(windows)
    with pytest.raises(CodeRunError) as refused:
        await _open(windows)
    assert refused.value.code == "window_limit"
    assert len(spawn.calls) == MAX_WINDOWS


async def test_no_browser_is_browser_missing(tmp_path: Path) -> None:
    spawn = Spawner()
    windows = PreviewWindows(None, tmp_path, spawn=spawn)
    with pytest.raises(CodeRunError) as refused:
        await _open(windows)
    assert refused.value.code == "browser_missing"
    assert spawn.calls == []


async def test_a_spawn_that_raises_frees_its_slot_and_profile(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    spawn = Spawner(fail=True)
    windows = PreviewWindows(EDGE, tmp_path, spawn=spawn)
    with caplog.at_level(logging.WARNING), pytest.raises(CodeRunError) as refused:
        await _open(windows)
    await windows.close_all()
    assert refused.value.code == "start_failed"
    assert spawn.calls[0].cwd_existed
    assert list(windows.area.iterdir()) == []
    assert windows.count == 0
    assert str(tmp_path) not in caplog.text and URL not in caplog.text


async def test_a_window_profile_is_deleted_when_its_tree_ends(
    windows: PreviewWindows, spawn: Spawner
) -> None:
    closed = asyncio.Event()
    await _open(windows, closed=closed)
    profile = spawn.calls[0].cwd
    (profile / "Default").mkdir()
    (profile / "Default" / "Preferences").write_text("{}", encoding="utf-8")
    spawn.windows[0].ended.set()  # the user closed the window
    await asyncio.wait_for(closed.wait(), HANG_GUARD_S)
    assert spawn.windows[0].releases == 1
    assert not profile.exists()
    assert windows.count == 0


async def test_on_closed_runs_once_even_when_it_raises(
    windows: PreviewWindows, spawn: Spawner, caplog: pytest.LogCaptureFixture
) -> None:
    calls: list[int] = []
    done = asyncio.Event()

    def on_closed() -> None:
        calls.append(1)
        done.set()
        raise RuntimeError("secret detail")

    await windows.open(URL, allow=ALLOW, owner="p1", on_closed=on_closed)
    with caplog.at_level(logging.WARNING):
        spawn.windows[0].ended.set()
        await asyncio.wait_for(done.wait(), HANG_GUARD_S)
    assert calls == [1]
    assert "RuntimeError" in caplog.text and "secret detail" not in caplog.text
    assert windows.count == 0


async def test_close_owner_kills_only_that_owners_windows(
    windows: PreviewWindows, spawn: Spawner
) -> None:
    closed = asyncio.Event()
    await _open(windows, owner="p1", closed=closed)
    await _open(windows, owner="p2")
    windows.close_owner("p1")
    await asyncio.wait_for(closed.wait(), HANG_GUARD_S)
    assert [w.kills for w in spawn.windows] == [1, 0]
    assert windows.count == 1


async def test_close_all_kills_waits_closes_the_proxy_and_sweeps(
    windows: PreviewWindows, spawn: Spawner
) -> None:
    await _open(windows, owner="p1")
    await _open(windows, owner="p2")
    (windows.area / "stale-profile").mkdir()
    (windows.area / "stray.tmp").write_text("x", encoding="utf-8")
    assert windows.proxy is not None
    port = int(windows.proxy.rsplit(":", 1)[1])
    await windows.close_all()
    assert [(w.kills, w.releases) for w in spawn.windows] == [(1, 1), (1, 1)]
    assert windows.count == 0
    assert list(windows.area.iterdir()) == []
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as again:
        again.bind(("127.0.0.1", port))  # the proxy socket was closed, so its port is free
    with pytest.raises(CodeRunError) as refused:
        await _open(windows)
    assert refused.value.code == "unavailable"


async def test_close_all_releases_a_window_that_outlives_the_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(preview_windows, "CLOSE_WAIT_S", 0.01)
    spawn = Spawner(stubborn=True)
    windows = PreviewWindows(EDGE, tmp_path, spawn=spawn)
    await _open(windows)
    await windows.close_all()
    assert (spawn.windows[0].kills, spawn.windows[0].releases) == (1, 1)


def test_sweep_does_nothing_when_the_area_is_absent(tmp_path: Path) -> None:
    windows = PreviewWindows(EDGE, tmp_path, spawn=Spawner())
    windows.sweep()
    assert not windows.area.exists()


@windows_only
def test_sweep_deletes_profiles_but_never_follows_a_junction(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    windows = PreviewWindows(EDGE, tmp_path / "data", spawn=Spawner())
    (windows.area / "old" / "Default").mkdir(parents=True)
    (windows.area / "old" / "Local State").write_text("{}", encoding="utf-8")
    (windows.area / "stray.tmp").write_text("x", encoding="utf-8")
    target = tmp_path / "elsewhere"
    target.mkdir()
    (target / "keep.txt").write_text("keep", encoding="utf-8")
    _junction(target, windows.area / "link")
    with caplog.at_level(logging.WARNING):
        windows.sweep()
    assert [p.name for p in windows.area.iterdir()] == ["link"]
    assert (target / "keep.txt").read_text(encoding="utf-8") == "keep"
    assert not caplog.records  # a junction is skipped, not a failed deletion


@windows_only
def test_sweep_leaves_an_area_that_is_a_junction(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    target = tmp_path / "elsewhere"
    (target / "keep").mkdir(parents=True)
    (target / "keep.txt").write_text("keep", encoding="utf-8")
    windows = PreviewWindows(EDGE, data, spawn=Spawner())
    _junction(target, windows.area)
    windows.sweep()
    assert sorted(p.name for p in target.iterdir()) == ["keep", "keep.txt"]


@windows_only
async def test_the_proxy_port_cannot_be_taken_and_never_listens(
    windows: PreviewWindows, spawn: Spawner
) -> None:
    await _open(windows)
    assert windows.proxy is not None
    host, text = windows.proxy.rsplit(":", 1)
    port = int(text)
    assert host == "127.0.0.1"
    # A listener on any of these would receive the window's requests to 127.0.0.1:<port>.
    for family, address, reuse, v6only in (
        (socket.AF_INET, "127.0.0.1", False, None),
        (socket.AF_INET, "0.0.0.0", False, None),
        (socket.AF_INET, "0.0.0.0", True, None),
        (socket.AF_INET6, "::", False, 0),
        (socket.AF_INET6, "::1", False, None),
    ):
        with socket.socket(family, socket.SOCK_STREAM) as other:
            if reuse:
                other.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if v6only is not None:
                other.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, v6only)
            with pytest.raises(OSError):
                other.bind((address, port))
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
        client.settimeout(0.05)
        # Windows retries a refused loopback SYN for about 2 s, so within 50 ms the refusal can
        # read as a timeout; either way nothing accepted the connection.
        with pytest.raises((ConnectionRefusedError, TimeoutError)):
            client.connect(("127.0.0.1", port))
    assert winproc.tcp_listeners(port) == frozenset()


@windows_only
async def test_an_area_that_is_a_junction_gets_no_profile(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    target = tmp_path / "elsewhere"
    target.mkdir()
    spawn = Spawner()
    windows = PreviewWindows(EDGE, data, spawn=spawn)
    _junction(target, windows.area)
    with pytest.raises(CodeRunError) as refused:
        await _open(windows)
    await windows.close_all()
    assert refused.value.code == "start_failed"
    assert list(target.iterdir()) == [] and spawn.calls == []
    assert windows.count == 0
