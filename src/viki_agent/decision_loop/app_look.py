"""The app's Look facts: which live chats wait on the user, which MCP servers fill a chat's
context, which sends an earlier launch left unconfirmed and which bridges have stopped.

`AppLookReader` implements `contracts.LookHost`. It runs on the event loop beside live chats, so it
only reads state the app already holds: a chat's open cards and requests, its last context report
(`SessionActor.context` makes no request of Claude Code) and the messaging status. It sends nothing
to a chat and never raises. A part nobody asked for answers None; a part that cannot be read
answers None and names itself in `failures`; a row of the wrong shape is skipped.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Protocol, cast

from viki_agent.automation.routines import parse_stamp, stamp
from viki_agent.conversations.commands import MAX_PAGE_SIZE, CommandService
from viki_agent.decision_loop.contracts import (
    MCP_CONTEXT_SHARE,
    UNCERTAIN_SEND_WINDOW,
    AppLook,
    HeavyServer,
    StoppedBridge,
    UncertainSend,
    WaitingChat,
)


class _Chat(Protocol):
    session_id: str

    def requests(self) -> list[dict[str, object]]: ...

    def pending_cards(self) -> list[dict[str, Any]]: ...

    async def context(self) -> dict[str, object] | None: ...


class _Sessions(Protocol):
    def live(self) -> Sequence[_Chat]: ...


def _kind(card: object) -> str | None:
    """The kind a projected card shows the user, or None for anything but a question or a
    permission or plan request."""
    if not isinstance(card, Mapping):
        return None
    card = cast(Mapping[str, object], card)
    data = card.get("data")
    if card.get("type") == "question.asked":
        return "question"
    if card.get("type") == "permission.requested" and isinstance(data, Mapping):
        return "plan" if cast(Mapping[str, object], data).get("kind") == "plan" else "permission"
    return None


def _card_id(card: object) -> str | None:
    data = cast(Mapping[str, object], card).get("data") if isinstance(card, Mapping) else None
    if not isinstance(data, Mapping):
        return None
    tool_use_id = cast(Mapping[str, object], data).get("tool_use_id")
    return tool_use_id if isinstance(tool_use_id, str) else None


def _oldest_card(chat: _Chat) -> WaitingChat | None:
    """The chat's oldest card that has both a request (for its raise time) and a displayed item
    (for its kind), the earlier in arrival order winning a tie."""
    kinds = {
        card_id: kind
        for card in chat.pending_cards()
        if (card_id := _card_id(card)) is not None and (kind := _kind(card)) is not None
    }
    oldest: WaitingChat | None = None
    for row in chat.requests():
        if not isinstance(row, Mapping):
            continue
        row = cast(Mapping[str, object], row)
        tool_use_id = row.get("tool_use_id")
        if not isinstance(tool_use_id, str) or tool_use_id not in kinds:
            continue
        try:
            since = parse_stamp(row.get("raised_at"))
        except ValueError:
            continue
        if oldest is None or since < oldest.since:
            oldest = WaitingChat(chat.session_id, tool_use_id, kinds[tool_use_id], since)
    return oldest


def _size(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _heavy_servers(chat: _Chat, report: object) -> list[HeavyServer]:
    """The chat's servers whose loaded tokens reach MCP_CONTEXT_SHARE of its `max_tokens`."""
    if not isinstance(report, Mapping):
        return []
    report = cast(Mapping[str, object], report)
    max_tokens, servers = _size(report.get("max_tokens")), report.get("mcp_servers")
    if max_tokens is None or max_tokens <= 0 or not isinstance(servers, list):
        return []
    found: list[HeavyServer] = []
    for row in cast(list[object], servers):
        if not isinstance(row, Mapping):
            continue
        row = cast(Mapping[str, object], row)
        name, loaded = row.get("server"), _size(row.get("loaded_tokens"))
        if (
            isinstance(name, str)
            and loaded is not None
            and loaded >= MCP_CONTEXT_SHARE * max_tokens
        ):
            found.append(HeavyServer(chat.session_id, name))
    return found


def _uncertain(rows: tuple[Any, ...]) -> tuple[UncertainSend, ...]:
    sends: list[UncertainSend] = []
    for row in rows:
        try:
            sends.append(UncertainSend(row.command_id, row.session_id, parse_stamp(row.updated_at)))
        except AttributeError, ValueError:
            continue
    return tuple(sends)


def _stopped(status: object) -> tuple[StoppedBridge, ...]:
    if not isinstance(status, Mapping):
        raise TypeError("messaging status")
    bridges: list[StoppedBridge] = []
    for name, row in cast(Mapping[object, object], status).items():
        if not isinstance(name, str) or not isinstance(row, Mapping):
            continue
        row = cast(Mapping[str, object], row)
        restart, reason = row.get("restart_required"), row.get("reason")
        if (
            row.get("configured") is True
            and row.get("state") == "stopped"
            and isinstance(restart, bool)
            and (reason is None or isinstance(reason, str))
        ):
            bridges.append(StoppedBridge(name, restart, reason))
    return tuple(bridges)


class AppLookReader:
    def __init__(
        self,
        sessions: _Sessions,
        commands: CommandService,
        messaging: Callable[[], object] | None = None,
        *,
        wall: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._sessions = sessions
        self._commands = commands
        self._messaging = messaging
        self._wall = wall
        # The ledger holds earlier launches' rows only, so they cannot change while the app runs:
        # one successful read answers every later cycle.
        self._sends: tuple[UncertainSend, ...] | None = None

    async def read(self, *, waiting: bool, heavy: bool, sends: bool, bridges: bool) -> AppLook:
        failures: list[str] = []
        parts: list[Any] = []
        for asked, word, part in (
            (waiting, "chats_unknown", self._waiting),
            (heavy, "context_unknown", self._heavy),
            (sends, "sends_unknown", self._sent),
            (bridges, "bridges_unknown", self._bridges),
        ):
            if not asked:
                parts.append(None)
                continue
            try:
                parts.append(await part())
            except Exception:  # a part fails alone; cancellation still propagates
                parts.append(None)
                failures.append(word)
        return AppLook(*parts, failures=tuple(failures))

    async def _waiting(self) -> tuple[WaitingChat, ...]:
        found = (_oldest_card(chat) for chat in self._sessions.live())
        return tuple(card for card in found if card is not None)

    async def _heavy(self) -> tuple[HeavyServer, ...]:
        found: list[HeavyServer] = []
        for chat in self._sessions.live():
            found.extend(_heavy_servers(chat, await chat.context()))
        return tuple(found)

    async def _sent(self) -> tuple[UncertainSend, ...]:
        if self._sends is None:
            since = stamp(self._wall() - UNCERTAIN_SEND_WINDOW)
            rows = await asyncio.to_thread(self._commands.uncertain, since, MAX_PAGE_SIZE)
            self._sends = _uncertain(rows)
        return self._sends

    async def _bridges(self) -> tuple[StoppedBridge, ...]:
        return () if self._messaging is None else _stopped(self._messaging())
