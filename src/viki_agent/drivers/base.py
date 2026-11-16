"""What a session needs from an agent driver. v1 has one implementation: drivers.claude.

A driver runs one agent process for one chat session and reports everything through the
`emit` callback it was built with, as `events.py` events in order. There is no registry:
the session layer imports the drivers it knows.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

from viki_agent.events import ErrorCode, SessionStarted
from viki_agent.turn_input import TurnInput


@dataclass(frozen=True)
class PermissionAnswer:
    """The user's reply to a permission or plan card: Allow once / Approve (True) or Deny /
    Keep planning (False)."""

    allow: bool
    stop_at_draft: bool = False  # set only by TaskRunner, for a routine that stops at a draft


@dataclass(frozen=True)
class QuestionAnswer:
    """The user's reply to a question card: question text → chosen label(s) or Other text;
    `response` is free text when the user answers without choosing."""

    answers: dict[str, str] = field(default_factory=dict)
    response: str = ""


Answer = PermissionAnswer | QuestionAnswer


class StartError(Exception):
    """The agent could not start; `code` is an events.ErrorCode, `message` is for the user."""

    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code: ErrorCode = code
        self.message = message


class SendError(Exception):
    """A terminal send failure with a safe message. `unsent` is True only when the driver
    refused before writing anything to the provider; otherwise native receipt is unknown."""

    def __init__(self, code: ErrorCode, message: str, *, unsent: bool = False) -> None:
        super().__init__(message)
        self.code: ErrorCode = code
        self.message = message
        self.unsent = unsent


class AgentDriver(Protocol):
    alive: bool  # the process has started and not exited

    @property
    def pid(self) -> int | None: ...
    @property
    def busy(self) -> bool: ...

    async def open(self) -> SessionStarted: ...
    async def send(self, text: str, /) -> None: ...
    async def interrupt(self) -> None: ...
    def respond(self, tool_use_id: str, answer: Answer) -> bool: ...
    def pending(self) -> list[str]: ...  # tool_use_ids of cards still waiting for an answer
    async def set_model(self, model: str) -> None: ...
    async def close(self) -> None: ...


@runtime_checkable
class TurnInputDriver(Protocol):
    """Explicit attachment capability; legacy text-only adapters remain type-safe.

    send_turn raises AttachmentUnavailable only before any provider write, so a caller that
    catches it knows nothing of the turn reached the provider.
    """

    async def send_turn(self, turn: TurnInput, /) -> None: ...


@runtime_checkable
class SteerDriver(Protocol):
    """Explicit mid-turn injection capability; not every driver reports one.

    steer writes `turn` into the already-running turn as a CLI steer. It raises exactly as
    send_turn does, and never changes `busy`: the turn already running holds that flag.

    settle_steers waits until every steer this driver wrote has ended (started or dropped) and
    no turn a steer started is still running, at most `timeout_s`; while it waits, it interrupts
    each turn a steer starts, so it ends cancelled instead of running into a close. It returns at
    once when no steer is pending and no turn a steer started is running.
    """

    async def steer(self, turn: TurnInput, steer_id: str, /) -> None: ...
    async def settle_steers(self, timeout_s: float, /) -> None: ...


@runtime_checkable
class InventoryDriver(Protocol):
    """Explicit live-inventory capability; not every driver reports one."""

    async def inventory(self) -> dict[str, object]: ...


@runtime_checkable
class ContextDriver(Protocol):
    """Explicit live-context-report capability; not every driver reports one."""

    async def context(self) -> dict[str, object]: ...


@runtime_checkable
class ModeDriver(Protocol):
    """Explicit live permission-mode switch; not every driver reports one."""

    async def set_permission_mode(self, mode: str) -> None: ...


@dataclass(frozen=True)
class StartFacts:
    """What the provider itself reported for one process, before any text was sent."""

    version: str | None  # the provider's own validated version report; None when it sent none
    mode: str | None  # the permission mode it says it is in; None when it sent none
    models: tuple[str, ...]  # its model menu's values
    mcp_servers: tuple[str, ...] | None  # app MCP servers it lists; None if unasked or unreadable


@runtime_checkable
class StartFactsDriver(Protocol):
    """Explicit start-report capability; not every driver reports one.

    Never raises except CancelledError: an unreadable part reads None.
    """

    async def start_facts(self) -> StartFacts: ...


@runtime_checkable
class PermissionDetailsDriver(Protocol):
    """Complete live card details, or None when unavailable within 8 KiB ASCII JSON."""

    def permission_details(self, tool_use_id: str) -> dict[str, Any] | None: ...


RunStopCode = Literal["unknown_run", "not_running", "refused", "timeout"]


class RunStopError(Exception):
    """`stop_run` could not stop the run; `.code` says why, one of `RunStopCode`."""

    def __init__(self, code: RunStopCode) -> None:
        super().__init__(code)
        self.code: RunStopCode = code


@runtime_checkable
class RunsDriver(Protocol):
    """Explicit live-requests/stop capability; not every driver reports one."""

    def requests(self) -> list[dict[str, object]]: ...
    async def stop_run(self, block_id: str) -> None: ...


McpControlCode = Literal[
    "not_running",
    "busy",
    "unknown_server",
    "managed",
    "not_approved",
    "blocked",
    "refused",
    "not_connected",
    "timeout",
]


class McpControlError(Exception):
    """`set_mcp_server` or `reconnect_mcp_server` did not succeed; `.code` says why, one of
    `McpControlCode`."""

    def __init__(self, code: McpControlCode) -> None:
        super().__init__(code)
        self.code: McpControlCode = code


@runtime_checkable
class McpControlDriver(Protocol):
    """Explicit MCP server switch/reconnect capability; not every driver reports one."""

    async def set_mcp_server(self, name: str, enabled: bool) -> None: ...
    async def reconnect_mcp_server(self, name: str) -> None: ...
