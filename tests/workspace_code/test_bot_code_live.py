"""A Bot code copy stays in use from its driver going live until that driver's close completes.

The real manager and actor run with synthetic gated drivers; no native provider runs."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path

import pytest

from tests.conversations.test_session import HANG_GUARD_S, GatedDriver, eventually
from viki_agent import events
from viki_agent.drivers.base import StartError, StartFacts
from viki_agent.drivers.claude.control import Launch
from viki_agent.drivers.claude.driver import DriverConfig
from viki_agent.session import SessionActor, SessionManager
from viki_agent.state import AppState, Prefs

from replay import SID

OTHER = "22222222-2222-4222-8222-222222222222"
COPY = "33333333-3333-4333-8333-333333333333"
SECOND_COPY = "55555555-5555-4555-8555-555555555555"


class StartRefused(GatedDriver):
    """open() fails the way ClaudeDriver's does: its own process is closed before it raises."""

    async def open(self) -> events.SessionStarted:
        self.opening.set()
        await self.release.wait()
        raise StartError("crashed", "synthetic start failure")


class OpenBroken(GatedDriver):
    """open() fails with an unexpected error, which the actor answers by closing the driver."""

    async def open(self) -> events.SessionStarted:
        self.opening.set()
        await self.release.wait()
        raise RuntimeError("synthetic open failure")


class CloseFails(GatedDriver):
    async def close(self) -> None:
        self.closing.set()
        raise RuntimeError("synthetic close failure")


class Runtime:
    """The real manager, one gated driver per spawn and a Bot copy chosen per session."""

    def __init__(self, tmp_path: Path) -> None:
        self.area = tmp_path / "project-worktrees"
        self.copies = {SID: COPY, OTHER: COPY}
        self.driver_type: type[GatedDriver] = GatedDriver
        self.hold_open = False
        self.drivers: list[GatedDriver] = []
        self.emits: list[Callable[[events.Event], None]] = []
        self.settings = str(tmp_path / "bot-memory-settings" / ("a" * 64 + ".json"))
        self.state = AppState(tmp_path / "state.json", Prefs(mode="auto", effort="low"))
        home = tmp_path / "synthetic-home"
        (home / "sessions").mkdir(parents=True)
        self.manager = SessionManager(
            config=DriverConfig(cli=("claude.exe",), vault=tmp_path / "vault", env={}),
            state=self.state,
            claude_home=home,
            make_driver=self.factory,
        )
        self.manager.bind_profiles(self.guard, self.catalog, initialized=self.initialized)
        self.manager.bind_bot_memory(self.memory)
        self.manager.bind_bot_code(self.copy)

    def factory(
        self, _launch: Launch, _prefs: Prefs, emit: Callable[[events.Event], None]
    ) -> GatedDriver:
        driver = self.driver_type()
        if not self.hold_open:
            driver.release.set()
        self.drivers.append(driver)
        self.emits.append(emit)
        return driver

    async def guard(self, _session_id: str, _prefs: Prefs) -> None:
        pass

    async def catalog(self, _started: events.SessionStarted) -> None:
        pass

    async def initialized(self, _session_id: str, _facts: StartFacts | None) -> None:
        pass

    async def memory(self, _session_id: str) -> str:
        return self.settings

    async def copy(self, session_id: str, _settings_file: str) -> events.CodeCopy:
        path = str(self.area / self.copies[session_id])
        return events.CodeCopy(path=path, folder="project", branch="bot/synthetic")

    def actor(self, session_id: str = SID) -> SessionActor:
        self.state.remember(session_id, mode="auto", effort="low")
        return self.manager.attach_prepared(session_id, profile=True)

    async def running(self, session_id: str = SID) -> tuple[SessionActor, GatedDriver]:
        actor = self.actor(session_id)
        assert await actor.send("synthetic prompt")
        return actor, self.drivers[-1]

    def in_use(self, worktree_id: str = COPY) -> bool:
        return self.manager.code_copy_in_use(worktree_id)

    async def opening(self) -> GatedDriver:
        count = len(self.drivers)
        await eventually(lambda: len(self.drivers) > count)
        driver = self.drivers[-1]
        await asyncio.wait_for(driver.opening.wait(), HANG_GUARD_S)
        return driver

    async def aclose(self) -> None:
        for driver in self.drivers:
            driver.release.set()
            driver.close_gate.set()
        await self.manager.close_all()


async def test_open_wait_and_running_hold_copy_until_close_completes(tmp_path: Path) -> None:
    rt = Runtime(tmp_path)
    rt.hold_open = True
    actor = rt.actor()
    sending = asyncio.create_task(actor.send("synthetic prompt"))
    try:
        driver = await rt.opening()
        # The started event and its copy do not exist yet while open() awaits.
        assert actor.started is None
        assert rt.in_use()
        driver.release.set()
        assert await sending
        assert rt.in_use()
        driver.close_gate.clear()
        closing = asyncio.create_task(actor.close())
        await asyncio.wait_for(driver.closing.wait(), HANG_GUARD_S)
        # No longer live anywhere, but the process has not finished closing.
        assert not actor.live and not rt.manager.live()
        assert rt.in_use()
        driver.close_gate.set()
        await closing
        assert not rt.in_use()
    finally:
        await rt.aclose()


async def test_start_refused_by_open_releases_after_its_own_close(tmp_path: Path) -> None:
    rt = Runtime(tmp_path)
    rt.hold_open, rt.driver_type = True, StartRefused
    actor = rt.actor()
    sending = asyncio.create_task(actor.send("synthetic prompt"))
    try:
        driver = await rt.opening()
        assert rt.in_use()
        driver.release.set()
        assert await sending is False
        assert not actor.live and not rt.in_use()
    finally:
        await rt.aclose()


async def test_broken_open_holds_copy_until_the_actor_close_completes(tmp_path: Path) -> None:
    rt = Runtime(tmp_path)
    rt.hold_open, rt.driver_type = True, OpenBroken
    actor = rt.actor()
    sending = asyncio.create_task(actor.send("synthetic prompt"))
    try:
        driver = await rt.opening()
        driver.close_gate.clear()
        driver.release.set()
        await asyncio.wait_for(driver.closing.wait(), HANG_GUARD_S)
        assert not actor.live and rt.in_use()
        driver.close_gate.set()
        with pytest.raises(RuntimeError, match="synthetic open failure"):
            await sending
        assert not rt.in_use()
    finally:
        await rt.aclose()


async def test_cancelled_start_holds_copy_until_its_close_completes(tmp_path: Path) -> None:
    rt = Runtime(tmp_path)
    rt.hold_open = True
    actor = rt.actor()
    sending = asyncio.create_task(actor.send("synthetic prompt"))
    try:
        driver = await rt.opening()
        driver.close_gate.clear()
        sending.cancel()
        await asyncio.wait_for(driver.closing.wait(), HANG_GUARD_S)
        assert not actor.live and rt.in_use()
        # A second cancellation lands on the shielded close, which still completes.
        sending.cancel()
        await asyncio.sleep(0)
        assert rt.in_use()
        driver.close_gate.set()
        with pytest.raises(asyncio.CancelledError):
            await sending
        assert not rt.in_use()
    finally:
        await rt.aclose()


async def test_exited_driver_holds_copy_and_its_close_spares_the_replacement(
    tmp_path: Path,
) -> None:
    rt = Runtime(tmp_path)
    try:
        actor, old = await rt.running()
        old.close_gate.clear()
        old.alive = False
        # The process ended on its own: the actor forgets the driver at once.
        rt.emits[0](events.Error(code="crashed", message="synthetic exit", recoverable=True))
        assert not actor.live
        await asyncio.wait_for(old.closing.wait(), HANG_GUARD_S)
        assert rt.in_use()
        # A replacement on the same copy starts while the old driver is still closing.
        assert await actor.send("synthetic follow-up")
        new = rt.drivers[1]
        assert new is not old and actor.live
        old.close_gate.set()
        # The release runs in the closing task's same step as close() returning.
        await eventually(lambda: "close" in old.calls)
        assert rt.in_use()
        new.close_gate.clear()
        closing = asyncio.create_task(actor.close())
        await asyncio.wait_for(new.closing.wait(), HANG_GUARD_S)
        assert rt.in_use()
        new.close_gate.set()
        await closing
        assert not rt.in_use()
    finally:
        await rt.aclose()


async def test_exited_driver_releases_once_its_close_completes(tmp_path: Path) -> None:
    rt = Runtime(tmp_path)
    try:
        actor, driver = await rt.running()
        driver.close_gate.clear()
        driver.alive = False
        rt.emits[0](events.Error(code="crashed", message="synthetic exit", recoverable=True))
        await asyncio.wait_for(driver.closing.wait(), HANG_GUARD_S)
        assert not actor.live and rt.in_use()
        driver.close_gate.set()
        await eventually(lambda: not rt.in_use())
    finally:
        await rt.aclose()


async def test_failed_close_keeps_copy_in_use(tmp_path: Path) -> None:
    rt = Runtime(tmp_path)
    rt.driver_type = CloseFails
    try:
        actor, _driver = await rt.running()
        with pytest.raises(RuntimeError, match="synthetic close failure"):
            await actor.close()
        assert not actor.live and not rt.manager.live()
        assert rt.in_use()
    finally:
        await rt.aclose()
    assert rt.in_use()  # no later close of that driver ever completed


async def test_use_survives_manager_removal_until_close_completes(tmp_path: Path) -> None:
    rt = Runtime(tmp_path)
    try:
        _actor, driver = await rt.running()
        driver.close_gate.clear()
        closing = asyncio.create_task(rt.manager.close(SID))
        await asyncio.wait_for(driver.closing.wait(), HANG_GUARD_S)
        assert rt.in_use()
        driver.close_gate.set()
        await closing
        assert rt.manager.get(SID) is None and not rt.in_use()
    finally:
        await rt.aclose()


async def test_cancelled_manager_close_keeps_use_after_actor_removal(tmp_path: Path) -> None:
    rt = Runtime(tmp_path)
    try:
        _actor, driver = await rt.running()
        driver.close_gate.clear()
        closing = asyncio.create_task(rt.manager.close(SID))
        await asyncio.wait_for(driver.closing.wait(), HANG_GUARD_S)
        closing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await closing
        assert "close" not in driver.calls and rt.in_use()
        # A later close finds no driver, finishes and removes the actor from the manager.
        await rt.manager.close(SID)
        assert rt.manager.get(SID) is None and not rt.manager.live()
        assert rt.in_use()
    finally:
        await rt.aclose()


async def test_two_chats_sharing_one_copy_release_independently(tmp_path: Path) -> None:
    rt = Runtime(tmp_path)
    try:
        first, _ = await rt.running(SID)
        second, _ = await rt.running(OTHER)
        await first.close()
        assert rt.in_use()
        await second.close()
        assert not rt.in_use()
    finally:
        await rt.aclose()


async def test_two_copies_are_tracked_apart(tmp_path: Path) -> None:
    rt = Runtime(tmp_path)
    rt.copies[OTHER] = SECOND_COPY
    try:
        first, _ = await rt.running(SID)
        await rt.running(OTHER)
        assert rt.in_use(COPY) and rt.in_use(SECOND_COPY)
        await first.close()
        assert not rt.in_use(COPY) and rt.in_use(SECOND_COPY)
        assert not rt.in_use(OTHER)  # a session id is not a copy identity
    finally:
        await rt.aclose()
    assert not rt.in_use(SECOND_COPY)


async def test_ordinary_and_copyless_sessions_record_nothing(tmp_path: Path) -> None:
    rt = Runtime(tmp_path)
    try:
        ordinary = rt.manager.create(OTHER)
        assert await ordinary.send("synthetic prompt")
        rt.manager.bind_bot_code(lambda *_: _no_copy())
        await rt.running(SID)
        assert len(rt.manager.live()) == 2
        assert rt.manager._copy_uses == {}
        assert not rt.in_use()
    finally:
        await rt.aclose()


async def _no_copy() -> events.CodeCopy | None:
    return None
