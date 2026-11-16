"""Manage browser app windows and the second-launch signal.

Launch URLs carry the per-launch token only on Windows; other platforms use a private redirect
page. Window tracking follows process trees and coordinates shutdown with routine admissions.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import Protocol, cast

from viki_agent import winproc

log = logging.getLogger(__name__)


class Window(Protocol):
    """The part of winproc.Child a window needs."""

    async def wait_tree(self) -> None: ...
    def kill_tree(self) -> None: ...
    def release(self) -> None: ...


class Spawn(Protocol):
    """winproc.spawn's shape."""

    def __call__(
        self,
        argv: Sequence[str],
        *,
        cwd: str | Path,
        env: Mapping[str, str],
        pipes: bool,
    ) -> Awaitable[Window]: ...


class WindowManager:
    def __init__(
        self,
        *,
        spawn: Spawn,
        argv: Callable[[], Sequence[str]],
        cwd: Path,
        env: Mapping[str, str],
        on_last_closed: Callable[[], Awaitable[None]],
        target_argv: Callable[[str], Sequence[str]] | None = None,
        signal: winproc.Signal | None = None,
        reply: Callable[[], winproc.Signal | None] | None = None,
        poll_ms: int = 500,
        keep_icon: Callable[[Sequence[Callable[[int], bool]]], object] | None = None,
        icon_poll_s: float = 1.0,
    ) -> None:
        self._spawn = spawn
        self._argv = argv
        self._target_argv = target_argv
        self._cwd = cwd
        self._env = env
        self._on_last_closed = on_last_closed
        self._signal = signal
        self._reply = reply
        self._poll_ms = poll_ms
        self._keep_icon = keep_icon
        self._icon_poll_s = icon_poll_s
        self.admission_lock = asyncio.Lock()
        self._windows: set[Window] = set()
        self._pending: set[asyncio.Task[object]] = set()
        self._close_pending = False
        self._tasks: set[asyncio.Task[object]] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._stopping: asyncio.Task[None] | None = None
        self.first_failed = False  # start() could not open the first window

    @property
    def count(self) -> int:
        return len(self._windows)

    @property
    def admitted_count(self) -> int:
        """Open windows plus admitted spawns that have not finished yet."""
        return self.count + len(self._pending)

    async def start(self) -> None:
        if self._stopping is not None:
            await self.stop()
            self._stopping = None
        self._loop = asyncio.get_running_loop()
        self._stop.clear()  # before open(): a manager that was stopped can be started again
        self._close_pending = True  # a failed initial spawn also ends the app
        if not await self.open():
            # No window ever appeared, so nothing will ever close and end the app: take the
            # last-window path now, or the user is left with a server they cannot see or stop.
            self.first_failed = True
            log.error("the first app window could not be opened")
            return
        if self._signal is not None and self._thread is None:
            self._thread = threading.Thread(target=self._watch, name="viki-open", daemon=True)
            self._thread.start()
        if self._keep_icon is not None:
            self._keep(asyncio.create_task(self._keep_icons(self._keep_icon)))

    async def stop(self) -> None:
        self._stop.set()
        if self._stopping is None:
            self._stopping = asyncio.create_task(self._finish_stop())
        stopping = self._stopping
        cancelled = False
        while True:
            try:
                await asyncio.shield(stopping)
                break
            except asyncio.CancelledError:
                if stopping.cancelled():
                    raise
                cancelled = True
        if cancelled:
            raise asyncio.CancelledError

    async def _finish_stop(self) -> None:
        # Every caller joins this cleanup; cancellation must not leave a live counted window.
        if self._thread is not None:
            await asyncio.to_thread(self._thread.join)
            self._thread = None
        async with self.admission_lock:
            for task in list(self._tasks):
                task.cancel()
            pending = list(self._pending)
            for task in pending:
                task.cancel()
            windows = list(self._windows)
            self._windows.clear()
        for window in windows:
            window.kill_tree()
            window.release()
        await asyncio.gather(*pending, return_exceptions=True)

    async def open(self, session_id: str | None = None) -> bool:
        """Open one window; False when none was opened (the app is closing, or the spawn
        failed). Targeted opens admit at most eight open or pending windows; ordinary launches
        retain their existing behavior."""
        if session_id is not None:
            try:
                if str(uuid.UUID(session_id)) != session_id or self._target_argv is None:
                    return False
            except ValueError, AttributeError, TypeError:
                return False
        opening = asyncio.current_task()
        assert opening is not None
        if session_id is None:
            if self._stop.is_set():
                return False
            # Ordinary launches spawn before the registration lease, as second launches did.
            self._pending.add(opening)
        else:
            async with self.admission_lock:
                if self._stop.is_set() or self.admitted_count >= 8:
                    return False
                self._pending.add(opening)
        window: Window | None = None
        opened = False
        try:
            try:
                argv = (
                    self._target_argv(session_id)
                    if session_id is not None and self._target_argv is not None
                    else self._argv()
                )
                window = await self._spawn(list(argv), cwd=self._cwd, env=self._env, pipes=False)
            except Exception as exc:
                log.warning("could not open the app window (%s)", type(exc).__name__)
                return False
            async with self.admission_lock:
                if not self._stop.is_set():  # stop may have begun during spawn or the lock wait
                    self._windows.add(window)
                    self._close_pending = True
                    self._keep(asyncio.create_task(self._track(window)))
                    opened = True
            return opened
        finally:
            if window is not None and not opened:
                window.kill_tree()
                window.release()
            if session_id is None:
                # Cancellation must release an ordinary spawn even while registration is held.
                self._pending.discard(opening)
                if self._close_pending and not self.admitted_count and not self._stop.is_set():
                    cleanup = asyncio.create_task(self._release_pending(opening))
                    self._keep(cleanup)
                    if not opening.cancelling():
                        await asyncio.shield(cleanup)
            else:
                cleanup = asyncio.create_task(self._release_pending(opening))
                cancelled = False
                while True:
                    try:
                        await asyncio.shield(cleanup)
                        break
                    except asyncio.CancelledError:
                        if cleanup.cancelled():
                            raise
                        cancelled = True
                if cancelled:
                    raise asyncio.CancelledError

    async def _release_pending(self, opening: asyncio.Task[object]) -> None:
        # Repeated request cancellation must not strand a reservation while the lease is held.
        async with self.admission_lock:
            self._pending.discard(opening)
            last = self._last_closed()
        if last:
            await self._report_last_closed()

    def _last_closed(self) -> bool:
        """Claim a last-close transition while holding admission_lock."""
        if not self._close_pending or self.admitted_count or self._stop.is_set():
            return False
        self._close_pending = False
        return True

    async def _report_last_closed(self) -> None:
        try:
            await self._on_last_closed()
        except Exception:
            log.exception("closing after the last window failed")

    async def _track(self, window: Window) -> None:
        await window.wait_tree()  # cancelled by stop(): then nothing below runs
        async with self.admission_lock:
            self._windows.discard(window)
            last = self._last_closed()
        window.release()
        if last:
            await self._report_last_closed()

    def holds(self) -> list[Callable[[int], bool]]:
        """One membership test per open window's process tree: each winproc.Child's Job.holds;
        a Window without one (a test double) is skipped. The folder picker calls this from a
        worker thread; the open windows are copied in one `list()` call."""
        return [
            cast("Callable[[int], bool]", hold)
            for window in list(self._windows)
            if callable(hold := getattr(getattr(window, "job", None), "holds", None))
        ]

    async def _keep_icons(self, keep: Callable[[Sequence[Callable[[int], bool]]], object]) -> None:
        """Every icon_poll_s until stop(): hand `keep` (window_icon.apply) one membership test
        per open window's process tree, off the loop. Edge resets an app window's icons on its
        own schedule, so this keeps going rather than setting them once."""
        while not self._stop.is_set():
            holds = self.holds()
            if holds:
                try:
                    await asyncio.to_thread(keep, holds)
                except Exception as exc:
                    log.warning("could not set the window icon (%s)", type(exc).__name__)
            await asyncio.sleep(self._icon_poll_s)

    def _keep(self, task: asyncio.Task[object]) -> None:
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _watch(self) -> None:
        signal, loop = self._signal, self._loop
        assert signal is not None and loop is not None
        while not self._stop.is_set():
            try:
                woken = signal.wait(self._poll_ms)
            except Exception as exc:  # closed or broken: nothing more will ever arrive
                log.warning("the open-window signal is gone (%s)", type(exc).__name__)
                return
            if woken:
                loop.call_soon_threadsafe(self._open_soon)

    def _open_soon(self) -> None:
        self._keep(asyncio.create_task(self._open_requested()))

    async def _open_requested(self) -> None:
        reply = self._reply() if self._reply is not None else None
        try:
            if await self.open() and not self._stop.is_set() and self.count and reply is not None:
                # Capture before spawn: a late reply cannot target a subsequent POSIX FIFO.
                reply.set()
        finally:
            if reply is not None:
                reply.close()
