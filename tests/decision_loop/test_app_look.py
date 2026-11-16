"""The app's Look reader: what it reads of the live chats, the send ledger and the bridges, and how
a part that cannot be read answers None with its word. No real actor, store or clock."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest

from viki_agent.conversations.commands import MAX_PAGE_SIZE, CommandError, CommandSummary
from viki_agent.decision_loop.app_look import AppLookReader
from viki_agent.decision_loop.contracts import (
    HeavyServer,
    StoppedBridge,
    UncertainSend,
    WaitingChat,
)

HANG_GUARD_S = 5.0  # bounds a wait that settles in milliseconds; it never fires in a healthy run
WALL = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)
S1 = "00000000-0000-4000-8000-000000000001"
S2 = "00000000-0000-4000-8000-000000000002"
S3 = "00000000-0000-4000-8000-000000000003"


def at(minutes: int) -> str:
    return (WALL + timedelta(minutes=minutes)).isoformat(timespec="milliseconds")[:-6] + "Z"


def moment(minutes: int) -> datetime:
    return WALL + timedelta(minutes=minutes)


def ask(tool_use_id: str) -> dict[str, Any]:
    return {"type": "question.asked", "data": {"tool_use_id": tool_use_id}}


def permission(tool_use_id: str, kind: str = "tool") -> dict[str, Any]:
    return {"type": "permission.requested", "data": {"tool_use_id": tool_use_id, "kind": kind}}


def request(tool_use_id: str, raised_at: object) -> dict[str, object]:
    return {"tool_use_id": tool_use_id, "run": None, "raised_at": raised_at}


class Actor:
    def __init__(
        self,
        session_id: str,
        *,
        requests: list[Any] | None = None,
        cards: list[Any] | None = None,
        context: object = None,
    ) -> None:
        self.session_id = session_id
        self._requests = requests or []
        self._cards = cards or []
        self._context = context
        self.broken = False

    def requests(self) -> list[Any]:
        if self.broken:
            raise RuntimeError("requests")
        return self._requests

    def pending_cards(self) -> list[Any]:
        return self._cards

    async def context(self) -> dict[str, object] | None:
        if self.broken:
            raise RuntimeError("context")
        return cast("dict[str, object] | None", self._context)


class Sessions:
    def __init__(self, *actors: Actor) -> None:
        self.actors = list(actors)
        self.broken = False

    def live(self) -> list[Actor]:
        if self.broken:
            raise RuntimeError("live")
        return self.actors


class Commands:
    def __init__(self, *rows: CommandSummary, fail: int = 0) -> None:
        self.rows = rows
        self.fail = fail
        self.calls: list[tuple[str, int]] = []
        self.threads: list[int] = []

    def uncertain(self, since: str, limit: int) -> tuple[CommandSummary, ...]:
        self.calls.append((since, limit))
        self.threads.append(threading.get_ident())
        if self.fail:
            self.fail -= 1
            raise CommandError("corrupt")
        return self.rows


def summary(command_id: str, session_id: str, updated_at: str) -> CommandSummary:
    return CommandSummary(
        command_id, session_id, "gen", 1, "unknown", "send_failed", updated_at, updated_at, 1
    )


def provider(
    state: str,
    *,
    configured: bool = True,
    restart_required: object = False,
    reason: object = None,
) -> dict[str, object]:
    return {
        "configured": configured,
        "state": state,
        "restart_required": restart_required,
        "reason": reason,
    }


def reader(
    sessions: Sessions | None = None,
    commands: Commands | None = None,
    messaging: Callable[[], object] | None = None,
) -> AppLookReader:
    return AppLookReader(
        sessions or Sessions(),
        cast(Any, commands or Commands()),
        messaging,
        wall=lambda: WALL,
    )


async def read_all(look: AppLookReader) -> Any:
    return await look.read(waiting=True, heavy=True, sends=True, bridges=True)


def report(max_tokens: object, *servers: object) -> dict[str, object]:
    return {"max_tokens": max_tokens, "mcp_servers": list(servers)}


def server(name: object, loaded_tokens: object) -> dict[str, object]:
    return {"server": name, "tools": 1, "loaded": 1, "loaded_tokens": loaded_tokens}


async def test_the_oldest_open_card_of_each_live_chat_with_its_kind() -> None:
    sessions = Sessions(
        Actor(
            S1,
            requests=[request("a1", at(-3)), request("a2", at(-9)), request("a3", at(-6))],
            cards=[ask("a1"), permission("a2"), ask("a3")],
        ),
        Actor(S2, requests=[request("b1", at(-1))], cards=[permission("b1", "plan")]),
        # No card behind the request, a card with no request, an unparseable stamp and a card
        # of a type nobody asks about: none of them is a waiting chat.
        Actor(S3, requests=[request("c1", at(-20)), request("c2", "soon")], cards=[ask("c2")]),
        Actor(S1 + "x", requests=[request("d1", at(-30))], cards=[{"type": "turn.done"}]),
        Actor(S2 + "x"),
    )

    look = await reader(sessions).read(waiting=True, heavy=False, sends=False, bridges=False)

    assert look.waiting == (
        WaitingChat(S1, "a2", "permission", moment(-9)),
        WaitingChat(S2, "b1", "plan", moment(-1)),
    )
    assert look.failures == ()


async def test_a_question_card_reads_as_a_question_and_the_earlier_card_wins_a_tie() -> None:
    sessions = Sessions(
        Actor(
            S1,
            requests=[request("a1", at(-5)), request("a2", at(-5))],
            cards=[ask("a1"), ask("a2")],
        )
    )

    look = await reader(sessions).read(waiting=True, heavy=False, sends=False, bridges=False)

    assert look.waiting == (WaitingChat(S1, "a1", "question", moment(-5)),)


async def test_servers_at_a_tenth_of_the_context() -> None:
    sessions = Sessions(
        Actor(
            S1,
            context=report(
                1000,
                server("big", 100),
                server("small", 99),
                server("huge", 900),
                "not a row",
                {"server": 7, "loaded_tokens": 900},
                server("text", "900"),
                server("flag", True),
            ),
        ),
        Actor(S2, context=report(True, server("a", 5))),  # a bool is no size
        Actor(S3, context=report(0, server("a", 5))),
        Actor(S1 + "a", context=None),
        Actor(S1 + "b", context={"max_tokens": 1000, "mcp_servers": None}),
        Actor(S1 + "c", context={"max_tokens": 1000, "mcp_servers": "none"}),
        Actor(S1 + "d", context=report(100, server("boolean", True))),
    )

    look = await reader(sessions).read(waiting=False, heavy=True, sends=False, bridges=False)

    assert look.heavy == (HeavyServer(S1, "big"), HeavyServer(S1, "huge"))
    assert look.failures == ()


async def test_the_ledger_is_read_once_per_launch() -> None:
    commands = Commands(
        summary("c1", S1, at(-60)),
        summary("c2", S2, at(-90)),
        summary("c3", S3, "yesterday"),  # an unparseable stamp drops its row
        fail=1,
    )
    look = reader(commands=commands)
    main = threading.get_ident()

    first = await look.read(waiting=False, heavy=False, sends=True, bridges=False)
    second = await look.read(waiting=False, heavy=False, sends=True, bridges=False)
    third = await look.read(waiting=False, heavy=False, sends=True, bridges=False)

    assert (first.sends, first.failures) == (None, ("sends_unknown",))
    assert second.sends == (
        UncertainSend("c1", S1, moment(-60)),
        UncertainSend("c2", S2, moment(-90)),
    )
    assert second.failures == ()
    assert third.sends == second.sends
    since = "2026-10-31T09:00:00.000Z"  # a day before the wall clock, in the ledger's stamp form
    assert commands.calls == [(since, MAX_PAGE_SIZE)] * 2  # the failure retried, then cached
    assert main not in commands.threads  # the ledger is read off the event loop


async def test_a_read_that_does_not_ask_for_sends_leaves_the_ledger_alone() -> None:
    commands = Commands(summary("c1", S1, at(-60)))

    look = await reader(commands=commands).read(waiting=True, heavy=True, sends=False, bridges=True)

    assert look.sends is None
    assert commands.calls == []


async def test_stopped_bridges_only() -> None:
    status = {
        "mattermost": provider("stopped", reason="start_failed"),
        "slack": provider("stopped", restart_required=True),
        "discord": provider("connected"),
        "other": provider("disabled"),
        "gone": provider("stopped", configured=False),
        "odd": provider("stopped", restart_required="yes"),
        "oddish": provider("stopped", reason=7),
        "plain": "stopped",
    }

    look = await reader(messaging=lambda: status).read(
        waiting=False, heavy=False, sends=False, bridges=True
    )

    assert look.bridges == (
        StoppedBridge("mattermost", False, "start_failed"),
        StoppedBridge("slack", True, None),
    )
    assert look.failures == ()


async def test_an_app_without_messaging_has_no_stopped_bridge() -> None:
    look = await reader().read(waiting=False, heavy=False, sends=False, bridges=True)

    assert (look.bridges, look.failures) == ((), ())


async def test_parts_follow_their_flags_and_fail_closed() -> None:
    actor = Actor(
        S1,
        requests=[request("a1", at(-9))],
        cards=[ask("a1")],
        context=report(1000, server("big", 500)),
    )
    sessions = Sessions(actor)
    commands = Commands(summary("c1", S1, at(-60)))
    status = {"slack": provider("stopped")}
    look = reader(sessions, commands, lambda: status)

    # Nothing asked: nothing read, nothing failed.
    nothing = await look.read(waiting=False, heavy=False, sends=False, bridges=False)
    assert (nothing.waiting, nothing.heavy, nothing.sends, nothing.bridges) == (None,) * 4
    assert (nothing.failures, commands.calls) == ((), [])

    # Each flag reads its own part alone.
    flags = ("waiting", "heavy", "sends", "bridges")
    for flag in flags:
        one = await look.read(**{name: name == flag for name in flags})  # type: ignore[arg-type]
        parts = {name: getattr(one, name) for name in flags}
        assert [name for name, value in parts.items() if value is not None] == [flag]

    # A broken chat list fails both chat parts; the ledger and bridges still answer.
    sessions.broken = True
    commands.rows = (summary("c1", S1, at(-60)),)
    down = await read_all(reader(sessions, commands, lambda: status))
    assert (down.waiting, down.heavy) == (None, None)
    assert down.sends is not None
    assert down.bridges == (StoppedBridge("slack", False, None),)
    assert down.failures == ("chats_unknown", "context_unknown")

    # A chat that raises while read fails its part, not the others, and nothing escapes.
    sessions.broken = False
    actor.broken = True
    hurt = await read_all(reader(sessions, Commands(fail=1), lambda: status))
    assert (hurt.waiting, hurt.heavy, hurt.sends) == (None, None, None)
    assert hurt.bridges == (StoppedBridge("slack", False, None),)
    assert hurt.failures == ("chats_unknown", "context_unknown", "sends_unknown")

    # A messaging source that raises or answers off-shape fails the bridges part alone.
    def broken() -> object:
        raise RuntimeError("messaging")

    for source in (broken, lambda: None, lambda: [1]):
        gone = await read_all(reader(Sessions(), Commands(), source))
        assert (gone.bridges, gone.failures) == (None, ("bridges_unknown",))


async def test_a_cancelled_read_escapes_and_leaves_the_ledger_cache_empty() -> None:
    """Cancelling a read that awaits the ledger thread raises CancelledError out of `read`, not
    into a failure word, and caches no send rows."""
    release = threading.Event()
    entered = threading.Event()

    class Blocked(Commands):
        def uncertain(self, since: str, limit: int) -> tuple[CommandSummary, ...]:
            entered.set()
            release.wait(HANG_GUARD_S)
            return super().uncertain(since, limit)

    look = reader(commands=Blocked(summary("c1", S1, at(-1))))
    task = asyncio.ensure_future(look.read(waiting=False, heavy=False, sends=True, bridges=False))
    try:
        async with asyncio.timeout(HANG_GUARD_S):
            while not entered.is_set():
                await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
    assert look._sends is None  # pyright: ignore[reportPrivateUsage]
