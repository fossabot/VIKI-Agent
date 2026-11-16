"""Chats the last window leaves running. A chat the manager's bound predicate retains keeps
its process and its cards: settle neither denies its cards nor waits for its turn,
close_unretained closes only the other chats, and hold keeps the app until a window is back,
no live chat is retained or its limit passes, denying nothing. `waiting` is an open card on the
live driver. Everything runs over test_session's CardDriver; no process starts."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path

import pytest

from tests.conversations.test_session import HANG_GUARD_S, CardDriver, eventually, watch, with_cards
from viki_agent.drivers.base import PermissionAnswer
from viki_agent.drivers.claude.control import Launch
from viki_agent.drivers.claude.driver import DriverConfig
from viki_agent.events import Event, TurnDone, Usage
from viki_agent.session import SessionManager
from viki_agent.state import AppState, Prefs

from replay import SID

TASK = "22222222-2222-4222-8222-222222222222"
CHAT = "33333333-3333-4333-8333-333333333333"
DENIED = PermissionAnswer(allow=False)


def task_and_chat(
    tmp_path: Path, retained: set[str] | None = None
) -> tuple[SessionManager, dict[str, CardDriver], dict[str, Callable[[Event], None]]]:
    """A manager whose every process is a CardDriver of its own, kept by session id with the
    emit callback it was built with. The bound predicate accepts the ids in `retained` (TASK
    when none is given), read at every call, so a test can change it while the manager runs."""
    kept = {TASK} if retained is None else retained
    drivers: dict[str, CardDriver] = {}
    emits: dict[str, Callable[[Event], None]] = {}

    def make(launch: Launch, prefs: Prefs, emit: Callable[[Event], None]) -> CardDriver:
        drivers[launch.session_id] = CardDriver()
        emits[launch.session_id] = emit
        return drivers[launch.session_id]

    config = DriverConfig(cli=("claude.exe",), vault=Path("C:/Vault"), env={})
    state = AppState(tmp_path / "state.json", Prefs(mode="auto", effort="low"))
    (tmp_path / "home" / "sessions").mkdir(parents=True, exist_ok=True)
    manager = SessionManager(
        config=config, state=state, claude_home=tmp_path / "home", make_driver=make
    )
    manager.bind_retain(lambda session_id: session_id in kept)
    return manager, drivers, emits


async def test_waiting_is_an_open_card_on_the_live_driver(tmp_path: Path) -> None:
    manager, drivers, _ = task_and_chat(tmp_path)
    actor = manager.create(CHAT)
    assert actor.waiting is False  # no process yet
    assert await actor.start()
    assert actor.waiting is False  # a process, and no card open
    drivers[CHAT].cards.append("card")
    assert actor.waiting is True
    assert actor.respond("card", PermissionAnswer(allow=True))
    assert actor.waiting is False  # answered
    drivers[CHAT].cards.append("left-open")
    await actor.close()
    assert actor.waiting is False  # the process went with the close


async def test_settle_spares_a_retained_card_and_waits_only_for_the_other_chats(
    tmp_path: Path,
) -> None:
    manager, drivers, emits = task_and_chat(tmp_path)
    task, chat = manager.create(TASK), manager.create(CHAT)
    assert await task.start() and await chat.start()
    for session_id in (TASK, CHAT):
        drivers[session_id].busy = True
        drivers[session_id].cards.append(f"card-{session_id}")
    settling = asyncio.create_task(manager.settle(60.0))
    await eventually(lambda: bool(drivers[CHAT].answers))
    assert drivers[CHAT].answers == [(f"card-{CHAT}", DENIED)]  # nobody is there to choose
    await asyncio.sleep(0.02)
    assert not settling.done()  # the chat's turn still gets its grace
    drivers[CHAT].busy = False
    emits[CHAT](TurnDone(status="success", usage=Usage()))
    # Returns with the chat idle, long before the grace: the task's turn is not waited for.
    await asyncio.wait_for(settling, HANG_GUARD_S)
    assert task.busy and task.waiting and drivers[TASK].answers == []
    assert "close" not in drivers[TASK].calls + drivers[CHAT].calls  # settle only settles


async def test_settle_follows_the_predicate_at_each_poll(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The last window closes: settle starts while the supervisor still reads the task idle, so
    the busy chat is not retained and its card is denied. One tick later the task works and the
    predicate retains it: from the next poll settle denies nothing and no longer waits for its
    turn."""
    monkeypatch.setattr("viki_agent.session.DENY_POLL_S", 0.02)
    retained: set[str] = set()
    manager, drivers, _ = task_and_chat(tmp_path, retained)
    task = manager.create(TASK)
    assert await task.start()
    drivers[TASK].busy = True
    drivers[TASK].cards.append("early")
    settling = asyncio.create_task(manager.settle(60.0))
    await eventually(lambda: bool(drivers[TASK].answers))
    assert drivers[TASK].answers == [("early", DENIED)]  # not retained yet: nobody is there
    retained.add(TASK)  # the supervisor's tick reads the task working
    drivers[TASK].cards.append("ship-question")
    await asyncio.wait_for(settling, HANG_GUARD_S)
    assert task.busy and task.waiting and drivers[TASK].answers == [("early", DENIED)]


async def test_close_unretained_closes_only_the_chats_nothing_retains(tmp_path: Path) -> None:
    manager, drivers, _ = task_and_chat(tmp_path)
    task, chat = manager.create(TASK), manager.create(CHAT)
    task_seen, chat_seen = watch(task), watch(chat)
    assert await task.start() and await chat.start()
    drivers[TASK].cards.append("task-card")
    drivers[CHAT].cards.append("chat-card")
    await asyncio.wait_for(manager.close_unretained(), HANG_GUARD_S)
    assert drivers[CHAT].answers == [("chat-card", DENIED)]  # denied first, as close_all does
    assert drivers[CHAT].calls[-1] == "close"
    assert (await chat_seen.until("session.closed"))["data"]["reason"] == "browser_gone"
    assert manager.live() == [task] and task.waiting  # its process and its card are kept
    assert drivers[TASK].answers == [] and "close" not in drivers[TASK].calls
    assert "session.closed" not in task_seen.types


async def test_close_unretained_gives_up_on_a_chat_that_will_not_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("viki_agent.session.CLOSE_TIMEOUT_S", 0.05)
    manager, drivers, _ = task_and_chat(tmp_path)
    chat = manager.create(CHAT)
    assert await chat.start()
    drivers[CHAT].close_gate.clear()  # close() never returns
    await asyncio.wait_for(manager.close_unretained(), HANG_GUARD_S)
    assert drivers[CHAT].closing.is_set() and manager.live() == []


async def test_hold_returns_once_a_window_is_back(tmp_path: Path) -> None:
    manager, drivers, _ = task_and_chat(tmp_path)
    task = manager.create(TASK)
    assert await task.start()
    drivers[TASK].busy = True
    drivers[TASK].cards.append("card")
    windows = 0
    holding = asyncio.create_task(manager.hold(60.0, until=lambda: windows == 0, poll_s=0.01))
    await asyncio.sleep(0.05)  # several polls: retained, no window, the limit far off
    assert not holding.done()
    windows = 1  # a second launch opened a window
    await asyncio.wait_for(holding, HANG_GUARD_S)
    assert task.live and task.waiting and drivers[TASK].answers == []  # it denied nothing


async def test_hold_returns_once_nothing_is_retained(tmp_path: Path) -> None:
    retained = {TASK}
    manager, drivers, _ = task_and_chat(tmp_path, retained)
    task = manager.create(TASK)
    assert await task.start()
    drivers[TASK].busy = True
    holding = asyncio.create_task(manager.hold(60.0, until=lambda: True, poll_s=0.01))
    await asyncio.sleep(0.05)
    assert not holding.done()
    retained.clear()  # the predicate lets the task's chat go
    await asyncio.wait_for(holding, HANG_GUARD_S)
    assert task.live and "close" not in drivers[TASK].calls  # hold closes nothing itself


async def test_hold_returns_when_its_limit_passes(tmp_path: Path) -> None:
    manager, drivers, _ = task_and_chat(tmp_path)
    task = manager.create(TASK)
    assert await task.start()
    drivers[TASK].busy = True
    drivers[TASK].cards.append("card")
    clock = asyncio.get_running_loop().time
    began = clock()
    await asyncio.wait_for(manager.hold(0.1, until=lambda: True, poll_s=0.01), HANG_GUARD_S)
    assert clock() - began >= 0.1  # the whole limit, the task still retained throughout
    assert task.waiting and drivers[TASK].answers == []  # it denied nothing


async def test_retaining_counts_the_live_chats_the_predicate_accepts(tmp_path: Path) -> None:
    manager, _, _ = task_and_chat(tmp_path)
    chat, task = manager.create(CHAT), manager.create(TASK)
    assert await chat.start()
    assert manager.retaining() is False  # the live chat is not retained; the task has no process
    assert await task.start()
    assert manager.retaining() is True
    await task.close()
    assert manager.retaining() is False  # still accepted, but its process is gone
    await asyncio.wait_for(manager.hold(60.0, until=lambda: True), HANG_GUARD_S)  # at once


async def test_no_chat_is_retained_until_a_predicate_is_bound(tmp_path: Path) -> None:
    manager, driver, _ = with_cards(tmp_path)
    actor = manager.create(SID)
    assert await actor.start()
    driver.busy = True
    driver.cards.append("card")
    assert manager.retaining() is False
    await asyncio.wait_for(manager.hold(60.0, until=lambda: True), HANG_GUARD_S)  # at once
    await asyncio.wait_for(manager.close_unretained(), HANG_GUARD_S)
    assert driver.answers == [("card", DENIED)] and manager.live() == []  # as close_all would
