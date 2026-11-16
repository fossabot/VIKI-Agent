"""Acceptance (frozen): the single-instance lock, the open-window signal, and the window
manager's lifecycle over a fake spawner."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from viki_agent import winproc
from viki_agent.window import WindowManager


def _name(prefix: str) -> str:
    return f"Local\\viki-test-{prefix}-{uuid.uuid4().hex}"


def test_the_single_instance_lock_is_exclusive_until_released() -> None:
    name = _name("mutex")
    first = winproc.single_instance(name)
    assert first is not None
    assert winproc.single_instance(name) is None
    first.release()
    first.release()  # idempotent
    third = winproc.single_instance(name)
    assert third is not None
    third.release()


def test_the_open_signal_wakes_once_per_set_and_only_when_it_exists() -> None:
    name = _name("event")
    assert winproc.Signal.open(name) is None
    owner = winproc.Signal.create(name)
    try:
        assert owner.wait(0) is False
        other = winproc.Signal.open(name)
        assert other is not None
        other.set()
        other.close()
        assert owner.wait(50) is True
        assert owner.wait(0) is False  # auto-reset: one wake per set
    finally:
        owner.close()
    with pytest.raises(RuntimeError):
        owner.wait(0)  # closed: a waiter must not spin on it


class FakeWindow:
    """A window whose whole process tree ends when the test sets `exited`."""

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
    def __init__(self, *, fail: bool = False) -> None:
        self.windows: list[FakeWindow] = []
        self.calls: list[tuple[list[str], Path, dict[str, str], bool]] = []
        self.fail = fail

    async def __call__(
        self, argv: Sequence[str], *, cwd: str | Path, env: Mapping[str, str], pipes: bool
    ) -> FakeWindow:
        self.calls.append((list(argv), Path(cwd), dict(env), pipes))
        if self.fail:
            raise OSError("no edge here")
        window = FakeWindow()
        self.windows.append(window)
        return window


async def _settle(turns: int = 10) -> None:
    for _ in range(turns):
        await asyncio.sleep(0)


def _manager(
    spawner: Spawner,
    tmp_path: Path,
    closed: list[bool],
    *,
    signal: winproc.Signal | None = None,
    poll_ms: int = 500,
) -> WindowManager:
    async def on_last_closed() -> None:
        closed.append(True)

    return WindowManager(
        spawn=spawner,
        argv=lambda: ["edge", "--app=http://127.0.0.1:8765/#token=t"],
        cwd=tmp_path,
        env={"A": "1"},
        on_last_closed=on_last_closed,
        signal=signal,
        poll_ms=poll_ms,
    )


async def test_windows_open_in_the_loop_and_the_last_exit_reports_once(tmp_path: Path) -> None:
    spawner, closed = Spawner(), []
    windows = _manager(spawner, tmp_path, closed)
    await windows.start()
    assert windows.count == 1
    assert spawner.calls == [
        (["edge", "--app=http://127.0.0.1:8765/#token=t"], tmp_path, {"A": "1"}, False)
    ]
    await windows.open()
    assert windows.count == 2
    spawner.windows[0].exited.set()
    await _settle()
    assert windows.count == 1 and closed == [] and spawner.windows[0].released
    spawner.windows[1].exited.set()
    await _settle()
    assert windows.count == 0 and closed == [True]
    await windows.open()  # a new round: the next last exit reports again
    spawner.windows[2].exited.set()
    await _settle()
    assert closed == [True, True]
    await windows.stop()


async def test_stop_kills_every_window_without_reporting_a_last_close(tmp_path: Path) -> None:
    spawner, closed = Spawner(), []
    windows = _manager(spawner, tmp_path, closed)
    await windows.start()
    await windows.open()
    await windows.stop()
    assert windows.count == 0
    assert all(w.killed and w.released for w in spawner.windows)
    spawner.windows[0].exited.set()  # a late exit changes nothing
    await _settle()
    assert closed == []


async def test_a_failing_last_closed_callback_is_logged_and_the_manager_goes_on(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    spawner = Spawner()

    async def boom() -> None:
        raise RuntimeError("close_all blew up")

    windows = WindowManager(
        spawn=spawner, argv=lambda: ["edge"], cwd=tmp_path, env={}, on_last_closed=boom
    )
    with caplog.at_level(logging.ERROR):
        await windows.start()
        spawner.windows[0].exited.set()
        await _settle()
    assert windows.count == 0
    assert any("last window" in r.getMessage() for r in caplog.records)
    await windows.open()  # still usable afterwards
    assert windows.count == 1
    await windows.stop()


async def test_open_after_stop_spawns_nothing(tmp_path: Path) -> None:
    spawner, closed = Spawner(), []
    windows = _manager(spawner, tmp_path, closed)
    await windows.start()
    await windows.stop()
    await windows.open()  # the watcher's callback landing after stop(): a closing app
    assert windows.count == 0 and len(spawner.calls) == 1


async def test_a_window_that_fails_to_spawn_is_logged_without_paths_and_ends_the_app(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Amended: a failed first window used to leave the app serving with no window, no UI and
    no way to stop it, and this test pinned that. It now takes the last-window path instead,
    so the launcher shuts down and says why. The log rules are unchanged: the exception type
    only, never a path."""
    spawner, closed = Spawner(fail=True), []
    windows = _manager(spawner, tmp_path, closed)
    with caplog.at_level(logging.WARNING):
        await windows.start()
    assert windows.count == 0 and closed == [True] and windows.first_failed is True
    assert any("window" in r.getMessage() and "OSError" in r.getMessage() for r in caplog.records)
    assert not any(str(tmp_path) in r.getMessage() for r in caplog.records)
    await windows.stop()


async def test_the_signal_from_another_launch_opens_a_fresh_window(tmp_path: Path) -> None:
    name = _name("open")
    signal = winproc.Signal.create(name)
    spawner, closed = Spawner(), []
    windows = _manager(spawner, tmp_path, closed, signal=signal, poll_ms=20)
    try:
        await windows.start()
        assert windows.count == 1
        other = winproc.Signal.open(name)
        assert other is not None
        other.set()
        other.close()
        for _ in range(100):  # the watcher polls every 20 ms; give it up to 1 s
            await asyncio.sleep(0.01)
            if windows.count == 2:
                break
        assert windows.count == 2
    finally:
        await windows.stop()
        signal.close()
