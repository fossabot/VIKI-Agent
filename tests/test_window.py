"""WindowManager behaviour the frozen acceptance file (tests/acceptance/test_window_accept.py)
does not pin: starting a manager that was stopped, and a first window that never opens."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from unittest.mock import Mock

import pytest

from viki_agent.window import Spawn, WindowManager


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
    """winproc.spawn's shape; `fail` makes every spawn raise, as a missing browser would."""

    def __init__(self, *, fail: bool = False) -> None:
        self.windows: list[FakeWindow] = []
        self.fail = fail

    async def __call__(
        self, argv: Sequence[str], *, cwd: str | Path, env: Mapping[str, str], pipes: bool
    ) -> FakeWindow:
        if self.fail:
            raise OSError("no browser here")
        self.windows.append(FakeWindow())
        return self.windows[-1]


def _manager(spawn: Spawn, closed: list[int]) -> WindowManager:
    async def on_last_closed() -> None:
        closed.append(1)

    return WindowManager(
        spawn=spawn,
        argv=lambda: ["msedge.exe"],
        cwd=Path.cwd(),
        env={},
        on_last_closed=on_last_closed,
    )


async def test_a_first_window_that_cannot_spawn_reports_the_app_closed() -> None:
    """Nothing will ever close, so start() must take the last-window path itself."""
    spawn = Spawner(fail=True)
    closed: list[int] = []
    manager = _manager(spawn, closed)
    await manager.start()
    assert manager.count == 0
    assert manager.first_failed is True
    assert closed == [1]  # the app was told to shut down, not left serving blind


async def test_a_failing_on_last_closed_does_not_escape_start() -> None:
    spawn = Spawner(fail=True)

    async def raise_it() -> None:
        raise RuntimeError("shutdown failed")

    manager = WindowManager(
        spawn=spawn,
        argv=lambda: ["msedge.exe"],
        cwd=Path.cwd(),
        env={},
        on_last_closed=raise_it,
    )
    await manager.start()  # logged, not raised: the launcher still returns its own code
    assert manager.first_failed is True


async def test_a_manager_that_was_stopped_can_be_started_again() -> None:
    """stop() sets the flag open() refuses on, so start() must clear it first."""
    spawn = Spawner()
    closed: list[int] = []
    manager = _manager(spawn, closed)
    await manager.start()
    assert manager.count == 1
    await manager.stop()
    assert manager.count == 0
    await manager.start()
    assert manager.count == 1
    assert manager.first_failed is False
    await manager.stop()


@pytest.mark.parametrize("outcome", ["opened", "failed", "stopped", "late", "cancelled"])
async def test_requested_open_acknowledges_only_admission_and_captures_reply(outcome) -> None:
    reply, replacement = Mock(), Mock()
    entered, resume = asyncio.Event(), asyncio.Event()
    child = FakeWindow()

    async def spawn(*args, **kwargs):
        entered.set()
        if outcome == "failed":
            raise OSError("no browser")
        if outcome in {"late", "cancelled"}:
            await resume.wait()
        return child

    manager = _manager(spawn, [])
    manager._reply = lambda: reply
    if outcome == "stopped":
        manager._stop.set()
    manager._open_soon()
    task = next(iter(manager._tasks))
    if outcome in {"late", "cancelled"}:
        await entered.wait()
        manager._reply = lambda: replacement  # A subsequent caller owns a different reply.
        if outcome == "cancelled":
            task.cancel()
        resume.set()
    try:
        if outcome == "cancelled":
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            await task
        assert reply.set.call_count == (1 if outcome in {"opened", "late"} else 0)
        reply.close.assert_called_once()
        replacement.set.assert_not_called()
        replacement.close.assert_not_called()
        assert manager.count == (1 if outcome in {"opened", "late"} else 0)
    finally:
        await manager.stop()


class _Job:
    def __init__(self, pid: int) -> None:
        self.pid = pid

    def holds(self, pid: int) -> bool:
        return pid == self.pid


class JobSpawner(Spawner):
    """Windows whose `job` answers holds() like winproc.Job, one pid per window."""

    async def __call__(
        self, argv: Sequence[str], *, cwd: str | Path, env: Mapping[str, str], pipes: bool
    ) -> FakeWindow:
        window = await super().__call__(argv, cwd=cwd, env=env, pipes=pipes)
        window.job = _Job(100 + len(self.windows))  # type: ignore[attr-defined]
        return window


async def test_keep_icon_gets_each_open_window_s_tree_until_stop() -> None:
    passes: list[list[bool]] = []
    first = asyncio.Event()

    def keep(holds: Sequence[Callable[[int], bool]]) -> None:
        passes.append([hold(101) for hold in holds])
        first.set()
        if len(passes) == 1:
            raise OSError("a hung browser")  # logged; the next pass still runs

    manager = WindowManager(
        spawn=JobSpawner(),
        argv=lambda: ["msedge.exe"],
        cwd=Path.cwd(),
        env={},
        on_last_closed=_closed,
        keep_icon=keep,
        icon_poll_s=0.01,
    )
    await manager.start()
    await asyncio.wait_for(first.wait(), 5)
    while len(passes) < 2:
        await asyncio.sleep(0.01)
    assert passes[0] == [True]  # window 1's tree holds pid 101
    await manager.stop()
    done = len(passes)
    await asyncio.sleep(0.05)
    assert len(passes) == done  # stop() ended it


async def test_holds_gives_each_open_window_s_tree_and_none_after_stop() -> None:
    """What the folder picker hands `folder_dialog.owner_window`: one Job.holds per open
    window; a window with no job (a test double) has none."""
    manager = _manager(JobSpawner(), [])
    await manager.start()
    assert await manager.open()
    holds = manager.holds()
    assert len(holds) == 2
    assert {(hold(101), hold(102)) for hold in holds} == {(True, False), (False, True)}
    await manager.stop()
    assert manager.holds() == []

    jobless = _manager(Spawner(), [])
    await jobless.start()
    assert jobless.count == 1 and jobless.holds() == []
    await jobless.stop()


async def test_no_keep_icon_no_icon_task() -> None:
    manager = _manager(JobSpawner(), [])
    await manager.start()
    assert len(manager._tasks) == 1  # only the window's own exit watcher
    await manager.stop()


async def _closed() -> None:
    return None
