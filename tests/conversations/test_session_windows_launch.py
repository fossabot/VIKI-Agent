"""Synthetic session-target window admission and launch URLs."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from pathlib import Path
from unittest.mock import Mock

import pytest

from viki_agent import __main__ as launcher
from viki_agent.config import Config
from viki_agent.window import WindowManager

SESSION_ID = "a0000000-0000-4000-8000-000000000001"


class FakeWindow:
    def __init__(self) -> None:
        self.exited = asyncio.Event()
        self.killed = False
        self.released = False

    async def wait_tree(self) -> None:
        await self.exited.wait()

    def kill_tree(self) -> None:
        self.killed = True

    def release(self) -> None:
        self.released = True


class Spawner:
    def __init__(self) -> None:
        self.windows: list[FakeWindow] = []
        self.calls: list[list[str]] = []
        self.entered = asyncio.Event()
        self.resume = asyncio.Event()
        self.defer = False
        self.fail = False

    async def __call__(
        self, argv: Sequence[str], *, cwd: str | Path, env: Mapping[str, str], pipes: bool
    ) -> FakeWindow:
        assert pipes is False
        self.calls.append(list(argv))
        self.entered.set()
        if self.defer:
            await self.resume.wait()
        if self.fail:
            raise OSError("synthetic launch-token must not enter logs")
        window = FakeWindow()
        self.windows.append(window)
        return window


def manager_for(spawn: Spawner, closed: list[bool]) -> WindowManager:
    async def on_last_closed() -> None:
        closed.append(True)

    return WindowManager(
        spawn=spawn,
        argv=lambda: ["browser", "ordinary"],
        target_argv=lambda session_id: ["browser", session_id],
        cwd=Path.cwd(),
        env={},
        on_last_closed=on_last_closed,
    )


async def settle() -> None:
    for _ in range(10):
        await asyncio.sleep(0)


async def test_targeted_open_preserves_ordinary_argv_and_tracks_same_session() -> None:
    spawn, closed = Spawner(), []
    manager = manager_for(spawn, closed)
    try:
        await manager.start()
        assert await manager.open(SESSION_ID)
        assert spawn.calls == [["browser", "ordinary"], ["browser", SESSION_ID]]
        assert manager.count == manager.admitted_count == 2
    finally:
        await manager.stop()
    assert closed == []


@pytest.mark.parametrize("bad", ["", "not-a-session", SESSION_ID.upper(), SESSION_ID + "&token=x"])
async def test_invalid_target_spawns_nothing(bad: str) -> None:
    spawn, closed = Spawner(), []
    manager = manager_for(spawn, closed)
    assert not await manager.open(bad)
    assert spawn.calls == []
    await manager.stop()


async def test_no_target_callback_refuses_without_using_ordinary_argv() -> None:
    spawn = Spawner()
    manager = WindowManager(
        spawn=spawn,
        argv=lambda: ["browser"],
        cwd=Path.cwd(),
        env={},
        on_last_closed=Mock(),
    )
    assert not await manager.open(SESSION_ID)
    assert spawn.calls == []
    await manager.stop()


async def test_target_cap_counts_pending_and_keeps_ordinary_open_behavior() -> None:
    spawn, closed = Spawner(), []
    manager = manager_for(spawn, closed)
    pending: list[asyncio.Task[bool]] = []
    try:
        await manager.start()
        spawn.defer = True
        pending = [asyncio.create_task(manager.open(SESSION_ID)) for _ in range(7)]
        await settle()
        assert manager.count == 1 and manager.admitted_count == 8
        assert not await manager.open(SESSION_ID)
        assert len(spawn.calls) == 8
        spawn.resume.set()
        assert all(await asyncio.gather(*pending))
        assert await manager.open()  # the single-instance signal retains its existing behavior
        assert manager.count == manager.admitted_count == 9
        assert not await manager.open(SESSION_ID)
    finally:
        await manager.stop()
        await asyncio.gather(*pending, return_exceptions=True)


@pytest.mark.parametrize("outcome", ["opened", "failed", "cancelled"])
async def test_pending_spawn_defers_last_close_and_releases_its_reservation(
    outcome: str, caplog: pytest.LogCaptureFixture
) -> None:
    spawn, closed = Spawner(), []
    manager = manager_for(spawn, closed)
    await manager.start()
    first = spawn.windows[0]
    spawn.defer = True
    spawn.entered.clear()
    opening = asyncio.create_task(manager.open(SESSION_ID))
    try:
        await spawn.entered.wait()
        first.exited.set()
        await settle()
        assert manager.count == 0 and manager.admitted_count == 1 and closed == []
        if outcome == "cancelled":
            opening.cancel()
            with pytest.raises(asyncio.CancelledError):
                await opening
        else:
            spawn.fail = outcome == "failed"
            spawn.resume.set()
            assert await opening is (outcome == "opened")
        assert manager.admitted_count == (1 if outcome == "opened" else 0)
        if outcome == "opened":
            assert closed == []
            spawn.windows[-1].exited.set()
            await settle()
        assert closed == [True]
        assert "launch-token" not in caplog.text
        assert SESSION_ID not in caplog.text
    finally:
        await manager.stop()
        await asyncio.gather(opening, return_exceptions=True)


async def test_cancellation_at_registration_reaps_the_process_and_pending_admission() -> None:
    spawn, closed = Spawner(), []
    manager = manager_for(spawn, closed)
    await manager.start()
    spawn.defer = True
    spawn.entered.clear()
    opening = asyncio.create_task(manager.open(SESSION_ID))
    await spawn.entered.wait()
    await manager.admission_lock.acquire()
    try:
        spawn.resume.set()
        await settle()
        assert len(spawn.windows) == 2
        opening.cancel()
    finally:
        manager.admission_lock.release()
    with pytest.raises(asyncio.CancelledError):
        await opening
    assert spawn.windows[1].killed and spawn.windows[1].released
    assert manager.count == manager.admitted_count == 1
    await manager.stop()


async def test_stop_cancels_pending_admission_without_last_close() -> None:
    spawn, closed = Spawner(), []
    manager = manager_for(spawn, closed)
    await manager.start()
    spawn.defer = True
    spawn.entered.clear()
    opening = asyncio.create_task(manager.open(SESSION_ID))
    await spawn.entered.wait()
    await manager.stop()
    assert opening.cancelled()
    assert manager.count == manager.admitted_count == 0
    assert closed == []
    assert not await manager.open(SESSION_ID)


async def test_repeated_cancellation_while_lease_is_held_releases_pending_admission() -> None:
    spawn, closed = Spawner(), []
    manager = manager_for(spawn, closed)
    await manager.start()
    spawn.defer = True
    spawn.entered.clear()
    opening = asyncio.create_task(manager.open(SESSION_ID))
    await spawn.entered.wait()
    await manager.admission_lock.acquire()
    try:
        opening.cancel()
        await settle()
        opening.cancel()  # cancellation while cleanup is waiting for the admission lease
        await settle()
    finally:
        manager.admission_lock.release()
    with pytest.raises(asyncio.CancelledError):
        await opening
    assert manager.count == manager.admitted_count == 1
    await manager.stop()


async def test_cancelled_ordinary_pending_open_reports_last_close_after_lease_is_released() -> None:
    spawn, closed = Spawner(), []
    manager = manager_for(spawn, closed)
    await manager.start()
    first = spawn.windows[0]
    spawn.defer = True
    spawn.entered.clear()
    opening = asyncio.create_task(manager.open())
    await spawn.entered.wait()
    first.exited.set()
    await settle()
    assert manager.count == 0 and manager.admitted_count == 1 and closed == []
    async with manager.admission_lock:
        opening.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(opening, 0.05)
        assert manager.admitted_count == 0 and closed == []
    await settle()
    assert closed == [True]
    await manager.stop()


async def test_initial_spawn_failure_reports_last_close_once() -> None:
    spawn, closed = Spawner(), []
    spawn.fail = True
    manager = manager_for(spawn, closed)
    await manager.start()
    assert manager.first_failed and closed == [True]
    assert manager.count == manager.admitted_count == 0
    await manager.stop()


@pytest.mark.parametrize("dev", [False, True])
def test_target_url_uses_only_fragment_and_preserves_ordinary_url(dev: bool) -> None:
    ordinary = launcher.app_url("synthetic-token", dev=dev)
    assert launcher.app_url("synthetic-token", dev=dev, session_id=SESSION_ID) == (
        ordinary + "&session=" + SESSION_ID
    )
    with pytest.raises(ValueError):
        launcher.app_url("synthetic-token", dev=dev, session_id="x&token=y")


@pytest.mark.parametrize("platform", ["win32", "linux", "darwin"])
async def test_launcher_target_argv_keeps_posix_redirect_private(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    cfg = Config(
        vault=None,
        claude_home=tmp_path / "synthetic-provider",
        cli=None,
        edge=tmp_path / "browser",
        data_dir=tmp_path / "data",
    )
    spawn, closed = Spawner(), []
    redirects: list[tuple[Path, str]] = []

    def redirect(run_dir: Path, target: str) -> str:
        redirects.append((run_dir, target))
        return "file:///synthetic-private/open.html"

    async def on_last_closed() -> None:
        closed.append(True)

    monkeypatch.setattr(launcher.sys, "platform", platform)
    monkeypatch.setattr(launcher.winproc, "spawn", spawn)
    monkeypatch.setattr(launcher, "_redirect_url", redirect)
    manager = launcher._windows(
        cfg,
        cfg.edge,  # type: ignore[arg-type]
        "synthetic-token",
        dev=True,
        env={},
        signal=Mock(),
        on_last_closed=on_last_closed,
    )
    try:
        assert await manager.open(SESSION_ID)
        argv = spawn.calls[0]
        if platform == "win32":
            assert argv[-1] == (
                "--app=" + launcher.app_url("synthetic-token", dev=True, session_id=SESSION_ID)
            )
            assert redirects == []
        else:
            assert argv[-1] == "--app=file:///synthetic-private/open.html"
            assert not any("synthetic-token" in arg or SESSION_ID in arg for arg in argv)
            assert redirects == [
                (
                    cfg.data_dir / "run",
                    launcher.app_url("synthetic-token", dev=True, session_id=SESSION_ID),
                )
            ]
    finally:
        await manager.stop()
