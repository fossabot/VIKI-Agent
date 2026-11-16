"""One fixed Discord owner-DM channel for the shared remote conversation engine."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import aclosing, suppress
from typing import Any, cast

from viki_agent.messaging.discord_http import _MAX_POST_BYTES, DiscordHttp
from viki_agent.messaging.discord_transport import posted
from viki_agent.messaging.mattermost_transport import _failure_callback
from viki_agent.messaging.messaging_channel import RemoteBinding, RemotePost, StopReason
from viki_agent.messaging.messaging_config import DiscordConfig, valid_discord_id
from viki_agent.messaging.messaging_format import render


class DiscordChannel:
    def __init__(self, config: DiscordConfig, http: DiscordHttp | None = None) -> None:
        self.http = DiscordHttp(config) if http is None else http
        self._config = config
        self._binding: RemoteBinding | None = None
        self._cutoff = 0
        self._closed = False
        self._reader: asyncio.Task[Any] | None = None
        self._stream: AsyncGenerator[RemotePost] | None = None
        self._close_task: asyncio.Task[None] | None = None

    def _current(self) -> RemoteBinding | None:
        return self._binding if not self._closed and self.http.binding == self._binding else None

    async def bind(self) -> RemoteBinding:
        if self._closed:
            raise ValueError("discord_channel_closed")
        if self._binding is None:
            binding = await self.http.bind()
            if (
                self._closed
                or self.http.binding != binding
                or binding.provider != "discord"
                or binding.owner_user_id != self._config.owner_id
            ):
                raise ValueError("discord_channel_closed")
            self._binding = binding
        binding = self._current()
        if binding is None:
            raise ValueError("discord_channel_closed")
        return binding

    async def fresh_cutoff(self) -> int:
        binding = self._current()
        if binding is None:
            raise ValueError("discord_channel_closed")
        cutoff = await self.http.fresh_cutoff()
        if self._current() != binding:
            raise ValueError("discord_channel_closed")
        self._cutoff = cutoff
        return cutoff

    def validate(self, candidate: object, cutoff: int) -> RemotePost | None:
        if (
            self._current() is None
            or type(candidate) is not RemotePost
            or type(cutoff) is not int
            or cutoff <= 0
            or cutoff != self._cutoff
            or not valid_discord_id(candidate.id)
            or candidate.root_id != ""
            or type(candidate.created_ms) is not int
            or candidate.created_ms < cutoff
            or candidate.created_ms != (int(candidate.id) >> 22) + 1420070400000
            or type(candidate.size_bytes) is not int
            or not 0 < candidate.size_bytes <= _MAX_POST_BYTES
            or not isinstance(candidate.fingerprint, str)
            or re.fullmatch(r"[0-9a-f]{64}", candidate.fingerprint) is None
            or not isinstance(candidate.message, str)
            or not 0 < len(candidate.message) <= _MAX_POST_BYTES
            or not candidate.message.strip()
        ):
            return None
        size: int | None = None
        with suppress(UnicodeError):
            size = len(candidate.message.encode("utf-8"))
        # Preserve the transport's original microseconds through exact REST fingerprint equality.
        return candidate if size is not None and size <= candidate.size_bytes else None

    async def confirm(self, candidate: RemotePost) -> RemotePost | None:
        binding = self._current()
        if binding is None or self.validate(candidate, self._cutoff) is None:
            return None
        confirmed = await self.http.confirm(candidate)
        return confirmed if self._current() == binding and confirmed == candidate else None

    async def send(self, part: str, reply_to: str = "") -> None:
        if self._current() is None or (reply_to != "" and not valid_discord_id(reply_to)):
            raise ValueError("discord_post_invalid")
        await self.http.send(part, "")

    async def receive(
        self,
        *,
        fresh_cutoff: Callable[[], Awaitable[int]],
        on_failure: Callable[[], None] | None = None,
    ) -> AsyncGenerator[RemotePost]:
        notify = _failure_callback(on_failure)
        self.http.set_failure_handler(notify)
        binding = self._current()
        if binding is None or self._stream is not None:
            notify()
            raise ValueError("discord_channel_closed")
        stream = cast(
            AsyncGenerator[RemotePost],
            posted(
                self.http,
                binding,
                bot_token=self._config.bot_token,
                fresh_cutoff=fresh_cutoff,
                on_failure=notify,
            ),
        )
        self._stream, self._reader = stream, asyncio.current_task()
        try:
            async with aclosing(stream):
                try:
                    async for candidate in stream:
                        if self._current() != binding:
                            raise ValueError("discord_channel_closed")
                        # A yielded post does not give cleanup authority to cancel its consumer.
                        self._reader = None
                        yield candidate
                        self._reader = asyncio.current_task()
                finally:
                    notify()
        finally:
            notify()
            self._stream, self._reader = None, None
            if not self._closed:
                await self.close()

    def render(self, formatted: str) -> tuple[str, ...]:
        return render("discord", formatted)

    def destination_key(self, binding: RemoteBinding) -> str:
        identity = [
            binding.provider,
            binding.scope,
            binding.bot_user_id,
            binding.owner_user_id,
            binding.channel_id,
        ]
        return hashlib.sha256(
            json.dumps(identity, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    def failure(self) -> StopReason | None:
        return self.http.stop_reason

    async def _cleanup(self) -> None:
        reader, stream = self._reader, self._stream
        try:
            if reader is not None and not reader.done():
                reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)
            if stream is not None:
                await stream.aclose()
        finally:
            await self.http.close()

    async def close(self) -> None:
        self._closed, self._binding = True, None
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._cleanup())
        await asyncio.shield(self._close_task)
