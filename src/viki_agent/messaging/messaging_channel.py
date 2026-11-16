"""Closed contracts for the three fixed messaging adapters."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal, Protocol

Provider = Literal["mattermost", "slack", "discord"]
StopReason = Literal[
    "token_rejected",
    "app_token_rejected",
    "server_unreachable",
    "account_check_failed",
    "start_failed",
    "connection_lost",
    "limit_reached",
    "conversation_failed",
    "safety_check",
    "internal_error",
]
StopCause = StopReason | Literal["turned_off", "app_closing", "restart_required"]


@dataclass(frozen=True)
class RemoteBinding:
    provider: Provider
    scope: tuple[str, ...]
    bot_user_id: str
    owner_user_id: str
    channel_id: str


@dataclass(frozen=True)
class RemotePost:
    """Admitted content with SHA-256 fingerprint and normalized UTF-8 JSON byte count."""

    id: str
    message: str
    root_id: str
    created_ms: int
    fingerprint: str
    size_bytes: int


@dataclass(frozen=True)
class RemoteReaction:
    """An admitted thumbs reaction on a post, read as a permission choice, with its normalized
    UTF-8 JSON byte count."""

    post_id: str
    choice: Literal["allow", "deny"]
    emoji_name: str
    created_ms: int
    size_bytes: int = field(compare=False)

    @property
    def id(self) -> str:
        return f"{self.post_id}:{self.emoji_name}:{self.created_ms}"


class RemoteChannel(Protocol):
    async def bind(self) -> RemoteBinding: ...

    async def fresh_cutoff(self) -> int: ...

    async def confirm(self, candidate: RemotePost) -> RemotePost | None: ...

    async def send(self, part: str, reply_to: str) -> str | None: ...

    async def close(self) -> None: ...

    def receive(
        self,
        *,
        fresh_cutoff: Callable[[], Awaitable[int]],
        on_failure: Callable[[], None] | None = None,
    ) -> AsyncIterator[object]: ...

    def validate(self, candidate: object, cutoff: int) -> RemotePost | RemoteReaction | None: ...

    def render(self, formatted: str) -> tuple[str, ...]: ...

    def destination_key(self, binding: RemoteBinding) -> str: ...

    def failure(self) -> StopReason | None:
        """The fixed reason this channel recorded for its own failure, if any.

        A plain read that never raises or awaits: RemoteBot.stop_reason calls it from
        inside disarm, before the runtime revokes the channel.
        """
        ...
