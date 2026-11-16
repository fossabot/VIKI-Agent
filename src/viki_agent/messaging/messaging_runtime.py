"""Default-off generations for three fixed, user-controlled messaging bridges."""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal, TypedDict

from viki_agent.messaging.mattermost_bot import RemoteBot
from viki_agent.messaging.messaging_channel import Provider, StopCause, StopReason
from viki_agent.session import SessionActor

BridgeFactory = Callable[[Callable[[], None]], RemoteBot]
ProviderState = Literal["not_configured", "disabled", "connecting", "connected", "stopped"]
ErrorCode = Literal[
    "invalid_input", "not_configured", "cooldown", "restart_required", "unavailable"
]
_PROVIDERS: tuple[Provider, ...] = ("mattermost", "slack", "discord")


class ProviderStatus(TypedDict):
    configured: bool
    state: ProviderState
    restart_required: bool
    reason: StopReason | None


class MessagingStatus(TypedDict):
    mattermost: ProviderStatus
    slack: ProviderStatus
    discord: ProviderStatus


class MessagingError(RuntimeError):
    def __init__(self, code: ErrorCode, *, retry_after_seconds: int | None = None) -> None:
        self.code = code
        self.retry_after_seconds = retry_after_seconds if code == "cooldown" else None
        super().__init__(code)


@dataclass
class _Entry:
    factory: BridgeFactory | None
    desired: bool = False
    generation: int = 0
    bot_generation: int = 0
    bot: RemoteBot | None = None
    run: asyncio.Task[None] | None = None
    cleanup: asyncio.Task[None] | None = None
    failed: bool = False
    reason: StopReason | None = None
    restart_required: bool = False
    cooldown_until: float = 0.0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


def _observe(task: asyncio.Task[None]) -> None:
    if not task.cancelled():
        task.exception()


async def _remember_nothing(_provider: Provider, _on: bool) -> None:
    return None


class MessagingRuntime:
    def __init__(
        self,
        *,
        mattermost: BridgeFactory | None = None,
        slack: BridgeFactory | None = None,
        discord: BridgeFactory | None = None,
        revoke_mattermost: Callable[[], None] = lambda: None,
        cooldown_until: Callable[[Provider, RemoteBot], float] = lambda _provider, _bot: 0.0,
        clock: Callable[[], float] = time.monotonic,
        remember: Callable[[Provider, bool], Awaitable[None]] = _remember_nothing,
    ) -> None:
        self._entries: dict[Provider, _Entry] = {
            "mattermost": _Entry(mattermost),
            "slack": _Entry(slack),
            "discord": _Entry(discord),
        }
        self._revoke_mattermost, self._cooldown_until, self._clock = (
            revoke_mattermost,
            cooldown_until,
            clock,
        )
        self._remember = remember
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None

    def _entry(self, provider: Provider) -> _Entry:
        if provider not in _PROVIDERS:
            raise MessagingError("invalid_input")
        return self._entries[provider]

    def _status(self, entry: _Entry) -> ProviderStatus:
        if entry.factory is None:
            return {
                "configured": False,
                "state": "not_configured",
                "restart_required": False,
                "reason": None,
            }
        state: ProviderState = "disabled"
        if entry.restart_required or entry.failed:
            state = "stopped"
        elif entry.desired:
            state = (
                entry.bot.state
                if entry.bot is not None and entry.bot_generation == entry.generation
                else "connecting"
            )
        reason = entry.reason if entry.failed and not entry.restart_required else None
        return {
            "configured": True,
            "state": state,
            "restart_required": entry.restart_required,
            "reason": reason,
        }

    def status(self) -> MessagingStatus:
        return {
            "mattermost": self._status(self._entries["mattermost"]),
            "slack": self._status(self._entries["slack"]),
            "discord": self._status(self._entries["discord"]),
        }

    def bridge(self, provider: Provider) -> RemoteBot | None:
        entry = self._entry(provider)
        return entry.bot if entry.desired and entry.bot_generation == entry.generation else None

    def observe(self, actor: SessionActor, envelope: dict[str, Any]) -> None:
        """Fan one session's question event out to every connected bridge: the manager's single
        observer (SessionManager.bind_observer), installed on `create_app`'s manager so each
        bridge can relay a chat's question to its own desktop-question opt-in. A bridge whose
        `observe` raises is disarmed alone; the others still see the envelope."""
        if envelope.get("type") != "question.asked":
            return
        for provider in _PROVIDERS:
            bot = self.bridge(provider)
            if bot is None:
                continue
            try:
                bot.observe(actor, envelope)
            except Exception:
                bot.disarm(reason="internal_error")

    def _remember_cooldown(self, provider: Provider, entry: _Entry) -> None:
        if entry.bot is None:
            return
        value = 0.0
        try:
            value = self._cooldown_until(provider, entry.bot)
            valid = (
                type(value) in (int, float)
                and math.isfinite(value)
                and 0 <= value <= self._clock() + 86400
            )
        except Exception:
            valid = False
        if not valid:
            entry.restart_required = True
        else:
            entry.cooldown_until = max(entry.cooldown_until, value)

    def _revoke_consent(self, provider: Provider, entry: _Entry) -> None:
        if provider == "mattermost":
            try:
                self._revoke_mattermost()
            except Exception:
                entry.restart_required = True

    def _revoke(
        self,
        provider: Provider,
        entry: _Entry,
        *,
        failed: bool = False,
        reason: StopReason | None = None,
        cause: StopCause | None = None,
    ) -> None:
        entry.desired = False
        entry.generation += 1
        entry.failed, entry.reason = failed, reason
        self._remember_cooldown(provider, entry)
        self._revoke_consent(provider, entry)
        if entry.bot is not None:
            entry.bot.disarm(reason=cause)

    def _failed(self, provider: Provider, entry: _Entry, generation: int) -> None:
        if entry.generation == generation and entry.desired:
            bot = entry.bot if entry.bot_generation == generation else None
            reason = bot.stop_reason if bot is not None else "start_failed"
            self._revoke(provider, entry, failed=True, reason=reason)

    def disarm_all(self) -> None:
        for provider in _PROVIDERS:
            entry = self._entries[provider]
            # A shutdown sweep, not a new failure: keep whatever stop reason was already recorded.
            self._revoke(
                provider, entry, failed=entry.failed, reason=entry.reason, cause="app_closing"
            )

    def invalidate(self, provider: Provider) -> None:
        entry = self._entry(provider)
        entry.restart_required = True
        self._revoke(provider, entry, failed=True, cause="restart_required")

    async def _run(
        self, provider: Provider, entry: _Entry, generation: int, bot: RemoteBot
    ) -> None:
        try:
            await bot.run()
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        finally:
            self._failed(provider, entry, generation)

    async def _retire(self, provider: Provider, entry: _Entry) -> None:
        if entry.cleanup is None and entry.bot is not None:
            bot, run = entry.bot, entry.run

            async def cleanup() -> None:
                if run is not None and not run.done():
                    run.cancel()
                try:
                    await bot.close()
                finally:
                    if run is not None:
                        await asyncio.gather(run, return_exceptions=True)
                    self._remember_cooldown(provider, entry)
                    if entry.bot is bot:
                        entry.bot, entry.run = None, None

            entry.cleanup = asyncio.create_task(cleanup())
            entry.cleanup.add_done_callback(_observe)
        cleanup_task = entry.cleanup
        if cleanup_task is not None:
            await asyncio.shield(cleanup_task)
            if entry.cleanup is cleanup_task:
                entry.cleanup = None

    async def set_enabled(self, provider: Provider, enabled: bool) -> ProviderStatus:
        entry = self._entry(provider)
        if type(enabled) is not bool:
            raise MessagingError("invalid_input")
        if enabled and self._closed:
            raise MessagingError("unavailable")
        if enabled and entry.factory is None:
            raise MessagingError("not_configured")
        if not enabled:
            # Revoke before waiting for another action's lock.
            self._revoke(provider, entry, cause="turned_off")
        requested = entry.generation
        async with entry.lock:
            if enabled:
                if requested != entry.generation:
                    return self._status(entry)
                if entry.factory is None:  # Removed while this Enable waited for the lock.
                    raise MessagingError("not_configured")
                if entry.restart_required:
                    raise MessagingError("restart_required")
                if entry.desired:
                    return self._status(entry)
                entry.generation += 1
                entry.desired, entry.failed = True, False
            generation = entry.generation
            # The lock orders flag writes, so the stored flag follows the last explicit action.
            remembered = True
            try:
                await self._remember(provider, enabled)
            except asyncio.CancelledError:
                if enabled and entry.generation == generation:
                    self._revoke(provider, entry)
                raise
            except Exception:
                # The stored flag may disagree with this action, so the state is not a clean Off;
                # a later action owns the state once it has revoked this generation.
                if entry.generation == generation:
                    if enabled:
                        self._revoke(provider, entry, failed=True)
                    else:
                        entry.failed = True
                if enabled:
                    raise MessagingError("unavailable") from None
                remembered = False
            try:
                await self._retire(provider, entry)
            except asyncio.CancelledError:
                if enabled and entry.generation == generation:
                    self._revoke(provider, entry)
                raise
            except Exception:
                entry.restart_required = True
                self._revoke(provider, entry, failed=True)
                raise MessagingError("unavailable") from None
            if not remembered:
                raise MessagingError("unavailable")
            if not enabled or not entry.desired or entry.generation != generation:
                return self._status(entry)
            if entry.restart_required:
                self._revoke(provider, entry, failed=True)
                raise MessagingError("restart_required")
            remaining = entry.cooldown_until - self._clock()
            if remaining > 0:
                self._revoke(provider, entry, failed=True)
                raise MessagingError(
                    "cooldown", retry_after_seconds=max(1, min(86400, math.ceil(remaining)))
                )
            factory = entry.factory
            assert factory is not None
            try:
                bot = factory(lambda: self._failed(provider, entry, generation))
            except Exception:
                self._revoke(provider, entry, failed=True)
                raise MessagingError("unavailable") from None
            entry.bot, entry.bot_generation = bot, generation
            if not entry.desired or entry.generation != generation:
                bot.disarm()
                await self._retire(provider, entry)
                return self._status(entry)
            entry.run = asyncio.create_task(self._run(provider, entry, generation, bot))
            entry.run.add_done_callback(_observe)
            return self._status(entry)

    async def configure(
        self,
        provider: Provider,
        factory: BridgeFactory | None,
        commit: Callable[[], Awaitable[None]],
    ) -> ProviderStatus:
        entry = self._entry(provider)
        if self._closed:
            raise MessagingError("unavailable")
        # Revoke before waiting for another action's lock.
        self._revoke(provider, entry, cause="turned_off")
        generation = entry.generation
        async with entry.lock:
            retired = True
            try:
                await self._retire(provider, entry)
            except Exception:
                entry.restart_required = True
                self._revoke(provider, entry, failed=True)
                retired = False  # The bridge is disarmed; still commit so a Remove is not lost.
            try:
                await commit()
            except Exception:
                # As for a failed flag write: the stored flag may still read on, so not a clean Off.
                if entry.generation == generation:
                    entry.failed = True
                raise MessagingError("unavailable") from None
            entry.factory, entry.failed = factory, False
            if not retired:
                raise MessagingError("unavailable")
            return self._status(entry)

    async def _close_all(self) -> None:
        async def close_one(provider: Provider) -> None:
            entry = self._entries[provider]
            async with entry.lock:
                await self._retire(provider, entry)

        results = await asyncio.gather(
            *(close_one(provider) for provider in _PROVIDERS), return_exceptions=True
        )
        if any(isinstance(result, BaseException) for result in results):
            raise MessagingError("unavailable")

    async def close(self) -> None:
        if self._close_task is None:
            self._closed = True
            self.disarm_all()
            self._close_task = asyncio.create_task(self._close_all())
            self._close_task.add_done_callback(_observe)
        await asyncio.shield(self._close_task)
