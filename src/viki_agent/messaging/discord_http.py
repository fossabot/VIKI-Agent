"""Fixed Discord v10 owner-DM requests, identity checks and cancelable request pacing."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import ssl
import time
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from importlib import metadata
from typing import Any, Literal

from viki_agent.messaging.mattermost_http import FixedHttpError, FixedJsonHttp, server_date
from viki_agent.messaging.messaging_channel import RemoteBinding, RemotePost, StopReason
from viki_agent.messaging.messaging_config import (
    DiscordConfig,
    MessagingConfigError,
    load_discord,
    valid_discord_id,
)

_BASE = "https://discord.com/api/v10"
_EPOCH_MS = 1420070400000
_MAX_COOLDOWN = 86400
_MAX_POST_BYTES = 12 * 1024
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)")
_FORBIDDEN = (
    "guild_id",
    "member",
    "webhook_id",
    "message_reference",
    "referenced_message",
    "message_snapshots",
    "poll",
    "activity",
    "application",
    "application_id",
    "interaction",
    "interaction_metadata",
    "thread",
    "role_subscription_data",
    "resolved",
    "call",
    "shared_client_theme",
)
ErrorCode = Literal[
    "discord_http_failed",
    "discord_binding_failed",
    "discord_freshness_failed",
    "discord_post_invalid",
    "discord_gateway_failed",
    "discord_cooldown",
]


class DiscordHttpError(RuntimeError):
    """Only fixed codes leave the transport boundary."""

    def __init__(self, code: ErrorCode) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class GatewayInfo:
    url: str
    shards: int
    total: int
    remaining: int
    reset_after: int
    max_concurrency: int


def _user(value: object, expected: str | None, *, bot: bool) -> str | None:
    if (
        not isinstance(value, dict)
        or not valid_discord_id(value.get("id"))
        or (expected is not None and value["id"] != expected)
        or value.get("bot", False) is not bot
        or value.get("system", False) is not False
    ):
        return None
    return value["id"]


def _created_us(value: object) -> int | None:
    if not isinstance(value, str) or _TIMESTAMP.fullmatch(value) is None:
        return None
    try:
        delta = datetime.fromisoformat(value) - datetime(1970, 1, 1, tzinfo=UTC)
    except ValueError:
        return None
    return (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds


def _message(
    value: object, channel_id: str, author_id: str, *, bot: bool, flags: int
) -> dict[str, Any] | None:
    if (
        not isinstance(value, dict)
        or not valid_discord_id(value.get("id"))
        or value.get("channel_id") != channel_id
        or _user(value.get("author"), author_id, bot=bot) is None
        or type(value.get("type")) is not int
        or value["type"] != 0
        or "edited_timestamp" not in value
        or value["edited_timestamp"] is not None
        or value.get("tts") is not False
        or value.get("mention_everyone") is not False
        or value.get("pinned") is not False
        or type(value.get("flags", 0)) is not int
        or value.get("flags", 0) != flags
        or any(key in value for key in _FORBIDDEN)
        or any(
            value.get(key) != [] for key in ("attachments", "embeds", "mentions", "mention_roles")
        )
        or any(
            value.get(key, []) != []
            for key in ("components", "reactions", "mention_channels", "sticker_items", "stickers")
        )
    ):
        return None
    content = value.get("content")
    created = _created_us(value.get("timestamp"))
    if (
        not isinstance(content, str)
        or not content.strip()
        or len(content) > _MAX_POST_BYTES
        or created is None
        or created // 1000 != (int(value["id"]) >> 22) + _EPOCH_MS
    ):
        return None
    try:
        content.encode("utf-8")
    except UnicodeError:
        return None
    return {
        "id": value["id"],
        "channel_id": channel_id,
        "author_id": author_id,
        "content": content,
        "created_us": created,
        "edited_timestamp": None,
        "type": 0,
        "flags": flags,
    }


def normalize_post(candidate: object, binding: RemoteBinding, cutoff: int) -> RemotePost | None:
    """Normalize an original MESSAGE_CREATE data object; Gateway gates its opcode/event name."""
    if (
        binding.provider != "discord"
        or len(binding.scope) != 1
        or not valid_discord_id(binding.scope[0])
        or not all(
            valid_discord_id(value)
            for value in (binding.bot_user_id, binding.owner_user_id, binding.channel_id)
        )
        or binding.bot_user_id == binding.owner_user_id
        or type(cutoff) is not int
        or cutoff < 0
    ):
        return None
    admitted = _message(candidate, binding.channel_id, binding.owner_user_id, bot=False, flags=0)
    if admitted is None or admitted["created_us"] // 1000 < cutoff:
        return None
    encoded = json.dumps(
        admitted, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    if len(encoded) > _MAX_POST_BYTES:
        return None
    identity = json.dumps(
        [binding.provider, binding.scope, binding.bot_user_id, admitted],
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return RemotePost(
        admitted["id"],
        admitted["content"],
        "",
        admitted["created_us"] // 1000,
        hashlib.sha256(identity).hexdigest(),
        len(encoded),
    )


def _seconds(value: object, *, epoch: bool = False) -> Decimal | None:
    if not isinstance(value, str) or re.fullmatch(r"[0-9]{1,12}(?:\.[0-9]{1,6})?", value) is None:
        return None
    result = Decimal(value)
    return result if result <= (253402300799 if epoch else _MAX_COOLDOWN) else None


def _count(value: object, maximum: int = 1_000_000) -> int | None:
    if not isinstance(value, str) or re.fullmatch(r"[0-9]{1,7}", value) is None:
        return None
    result = int(value)
    return result if result <= maximum else None


class DiscordHttp:
    def __init__(
        self,
        config: DiscordConfig,
        *,
        transport: Any = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        try:
            admitted = load_discord(
                {
                    "VIKI_DISCORD_BOT_TOKEN": config.bot_token,
                    "VIKI_DISCORD_OWNER_ID": config.owner_id,
                    "VIKI_DISCORD_CLIENT_URL": config.client_url,
                }
            )
            version = metadata.version("viki-agent")
            if admitted != config or re.fullmatch(r"[0-9][A-Za-z0-9.!+_-]{0,63}", version) is None:
                raise MessagingConfigError
        except MessagingConfigError, metadata.PackageNotFoundError:
            raise DiscordHttpError("discord_binding_failed") from None
        self._config = config
        self._clock, self._sleep = clock, sleep
        self._binding: RemoteBinding | None = None
        self._channel_id: str | None = None
        self._server_date: int | None = None
        self._cutoff = 0
        self._cooldown_until = 0.0
        self._next_request = 0.0
        self._identify_until = 0.0
        self._stopped = False
        self._closed = False
        self.stop_reason: StopReason | None = None
        self._path = ""
        self._lock = asyncio.Lock()
        self._http = FixedJsonHttp(
            _BASE,
            {
                "Authorization": "Bot " + config.bot_token,
                "User-Agent": f"DiscordBot ({config.client_url}, {version})",
            },
            allowed=self._allowed,
            transport=transport,
        )
        self._failure_handler: Callable[[], None] | None = None
        self._failure_notified = False
        self._http.set_failure_callback(self._http_failed)

    @property
    def ssl_context(self) -> ssl.SSLContext:
        return self._http.ssl_context

    @property
    def binding(self) -> RemoteBinding | None:
        return self._binding

    @property
    def cooldown_until(self) -> float:
        return max(self._cooldown_until, self._next_request, self._identify_until)

    def set_failure_handler(self, handler: Callable[[], None] | None) -> None:
        self._failure_handler = handler
        self._failure_notified = False

    def record_failure(self, reason: StopReason) -> None:
        """Keep only the first fixed reason recorded for this bind generation."""
        if self.stop_reason is None:
            self.stop_reason = reason

    def _http_failed(self, error: FixedHttpError) -> None:
        # FixedJsonHttp's failure callback fires inside request(), before _request's own
        # except clause sees the error, so the reason must be recorded here to beat disarm.
        if error.status_code == 401:
            self.record_failure("token_rejected")
        elif error.unanswered:
            self.record_failure("server_unreachable")
        elif (
            error.status_code is not None
            and 400 <= error.status_code < 500
            and error.status_code != 429
            and self._path not in ("/users/@me", "/applications/@me", "/gateway/bot")
        ):
            self.record_failure("account_check_failed")
        if not self._stopped and error.status_code == 429:
            delays = [
                _seconds(error.headers.get(name))
                for name in ("retry-after", "x-ratelimit-reset-after")
            ]
            delay = max(
                (value for value in delays if value is not None),
                default=Decimal(_MAX_COOLDOWN),
            )
            if any(
                name in error.headers and _seconds(error.headers[name]) is None
                for name in ("retry-after", "x-ratelimit-reset-after")
            ):
                delay = Decimal(_MAX_COOLDOWN)
            self._cooldown_until = max(self._cooldown_until, self._clock() + float(delay))
        self._stopped = True
        if not self._failure_notified:
            self._failure_notified = True
            if self._failure_handler is not None:
                with suppress(Exception):
                    self._failure_handler()

    def reserve_identify(self) -> None:
        """Reserve the single-shard start window before the Gateway sends Identify."""
        if self._binding is None or self._closed:
            raise DiscordHttpError("discord_gateway_failed")
        if self._stopped or self._clock() < self._identify_until:
            raise DiscordHttpError("discord_cooldown")
        self._identify_until = self._clock() + 5

    def _allowed(self, method: str, path: str) -> bool:
        if method == "GET":
            if path in (
                "/users/@me",
                "/users/" + self._config.owner_id,
                "/applications/@me",
                "/gateway/bot",
            ):
                return True
            if self._channel_id is not None and path == "/channels/" + self._channel_id:
                return True
            if self._binding is not None:
                prefix = f"/channels/{self._binding.channel_id}/messages/"
                return path.startswith(prefix) and valid_discord_id(path[len(prefix) :])
        return method == "POST" and (
            path == "/users/@me/channels"
            or (
                self._binding is not None
                and path == f"/channels/{self._binding.channel_id}/messages"
            )
        )

    def _rate_delay(self, headers: Mapping[str, str]) -> float:
        counts: dict[str, int] = {}
        for name in ("x-ratelimit-limit", "x-ratelimit-remaining"):
            if name in headers:
                count = _count(headers[name])
                if count is None:
                    raise DiscordHttpError("discord_http_failed")
                counts[name] = count
        if counts.get("x-ratelimit-remaining", 0) > counts.get("x-ratelimit-limit", 1_000_000):
            raise DiscordHttpError("discord_http_failed")
        durations: dict[str, Decimal] = {}
        for name in ("x-ratelimit-reset-after", "retry-after", "x-ratelimit-reset"):
            if name in headers:
                seconds = _seconds(headers[name], epoch=name == "x-ratelimit-reset")
                if seconds is None:
                    raise DiscordHttpError("discord_http_failed")
                durations[name] = seconds
        if (
            "x-ratelimit-bucket" in headers
            and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", headers["x-ratelimit-bucket"]) is None
        ):
            raise DiscordHttpError("discord_http_failed")
        delay = Decimal(0)
        if counts.get("x-ratelimit-remaining") == 0:
            if "x-ratelimit-reset-after" in durations:
                delay = durations["x-ratelimit-reset-after"]
            elif "x-ratelimit-reset" in durations:
                date = server_date(headers)
                if date is None:
                    raise DiscordHttpError("discord_http_failed")
                # HTTP Date has only second precision; include its unknown final millisecond.
                delay = max(Decimal(0), durations["x-ratelimit-reset"] - Decimal(date) / 1000 + 1)
            else:
                raise DiscordHttpError("discord_http_failed")
        if delay > _MAX_COOLDOWN:
            raise DiscordHttpError("discord_http_failed")
        return float(max(delay, durations.get("retry-after", Decimal(0))))

    async def _request(
        self, method: str, path: str, body: Any = None
    ) -> tuple[Any, dict[str, str]]:
        async with self._lock:
            if self._closed:
                raise DiscordHttpError("discord_http_failed")
            if self._stopped or self._clock() < self._cooldown_until:
                raise DiscordHttpError("discord_cooldown")
            while self._clock() < self._next_request:
                await self._sleep(self._next_request - self._clock())
            if self._closed:
                raise DiscordHttpError("discord_http_failed")
            try:
                self._path = path
                result, headers = await self._http.request(method, path, body)
            except FixedHttpError as error:
                self._http_failed(error)
                if error.status_code == 429:
                    raise DiscordHttpError("discord_cooldown") from None
                raise DiscordHttpError("discord_http_failed") from None
            try:
                delay = self._rate_delay(headers)
                date = server_date(headers)
                if date is None or (self._server_date is not None and date < self._server_date):
                    raise DiscordHttpError("discord_freshness_failed")
            except DiscordHttpError:
                self._stopped = True
                raise
            self._server_date = date
            # Serialize this client's requests and stay below Discord's global 50/s ceiling.
            self._next_request = self._clock() + max(delay, 0.02)
            return result, headers

    def _dm(self, value: object, expected: str | None = None) -> str | None:
        if (
            not isinstance(value, dict)
            or not valid_discord_id(value.get("id"))
            or (expected is not None and value["id"] != expected)
            or type(value.get("type")) is not int
            or value["type"] != 1
            or "guild_id" in value
            or not isinstance(value.get("recipients"), list)
            or len(value["recipients"]) != 1
            or _user(value["recipients"][0], self._config.owner_id, bot=False) is None
        ):
            return None
        return value["id"]

    async def bind(self) -> RemoteBinding:
        if self._clock() < self._cooldown_until:
            raise DiscordHttpError("discord_cooldown")
        self._binding = None
        self._channel_id = None
        self._server_date = None
        self._cutoff = 0
        self._stopped = False
        self.stop_reason = None
        me, _ = await self._request("GET", "/users/@me")
        bot_id = _user(me, None, bot=True)
        if bot_id is None or bot_id == self._config.owner_id:
            self.record_failure("account_check_failed")
            raise DiscordHttpError("discord_binding_failed")
        application, _ = await self._request("GET", "/applications/@me")
        application_bot = application.get("bot") if isinstance(application, dict) else None
        if (
            not isinstance(application, dict)
            or not valid_discord_id(application.get("id"))
            or (
                "bot" in application
                and (
                    not isinstance(application_bot, dict)
                    or application_bot.get("id") != bot_id
                    or application_bot.get("bot", True) is not True
                    or application_bot.get("system", False) is not False
                )
            )
        ):
            self.record_failure("account_check_failed")
            raise DiscordHttpError("discord_binding_failed")
        owner, _ = await self._request("GET", "/users/" + self._config.owner_id)
        if _user(owner, self._config.owner_id, bot=False) is None:
            self.record_failure("account_check_failed")
            raise DiscordHttpError("discord_binding_failed")
        channel, _ = await self._request(
            "POST", "/users/@me/channels", {"recipient_id": self._config.owner_id}
        )
        channel_id = self._dm(channel)
        if channel_id is None:
            self.record_failure("account_check_failed")
            raise DiscordHttpError("discord_binding_failed")
        self._channel_id = channel_id
        confirmed, _ = await self._request("GET", "/channels/" + channel_id)
        if self._dm(confirmed, channel_id) is None:
            self._channel_id = None
            self.record_failure("account_check_failed")
            raise DiscordHttpError("discord_binding_failed")
        self._binding = RemoteBinding(
            "discord", (application["id"],), bot_id, self._config.owner_id, channel_id
        )
        return self._binding

    async def fresh_cutoff(self) -> int:
        binding = self._binding
        if binding is None:
            raise DiscordHttpError("discord_freshness_failed")
        me, headers = await self._request("GET", "/users/@me")
        if _user(me, binding.bot_user_id, bot=True) is None:
            raise DiscordHttpError("discord_freshness_failed")
        date = server_date(headers)
        if date is None:
            raise DiscordHttpError("discord_freshness_failed")
        self._cutoff = date + 1000
        return self._cutoff

    async def gateway(self) -> GatewayInfo:
        if self._binding is None:
            raise DiscordHttpError("discord_gateway_failed")
        value, _ = await self._request("GET", "/gateway/bot")
        if (
            not isinstance(value, dict)
            or value.get("url") != "wss://gateway.discord.gg/"
            or type(value.get("shards")) is not int
            or value["shards"] != 1
            or not isinstance(value.get("session_start_limit"), dict)
        ):
            raise DiscordHttpError("discord_gateway_failed")
        limits = value["session_start_limit"]
        for key, minimum, maximum in (
            ("total", 1, 1_000_000),
            ("remaining", 0, 1_000_000),
            ("reset_after", 1, _MAX_COOLDOWN * 1000),
            ("max_concurrency", 1, 1000),
        ):
            if type(limits.get(key)) is not int or not minimum <= limits[key] <= maximum:
                raise DiscordHttpError("discord_gateway_failed")
        if limits["remaining"] > limits["total"]:
            raise DiscordHttpError("discord_gateway_failed")
        if limits["remaining"] == 0:
            self._stopped = True
            self._cooldown_until = max(
                self._cooldown_until, self._clock() + limits["reset_after"] / 1000
            )
            raise DiscordHttpError("discord_cooldown")
        return GatewayInfo(
            value["url"],
            1,
            limits["total"],
            limits["remaining"],
            limits["reset_after"],
            limits["max_concurrency"],
        )

    def validate(self, candidate: object, cutoff: int) -> RemotePost | None:
        if self._binding is None or self._closed or self._stopped:
            return None
        post = normalize_post(candidate, self._binding, cutoff)
        if post is not None:
            self._cutoff = cutoff
        return post

    async def confirm(self, candidate: RemotePost) -> RemotePost | None:
        binding = self._binding
        if (
            binding is None
            or not isinstance(candidate, RemotePost)
            or not valid_discord_id(candidate.id)
            or candidate.root_id != ""
            or type(candidate.created_ms) is not int
            or candidate.created_ms < self._cutoff
            or type(candidate.size_bytes) is not int
            or not 0 < candidate.size_bytes <= _MAX_POST_BYTES
            or not isinstance(candidate.fingerprint, str)
            or re.fullmatch(r"[0-9a-f]{64}", candidate.fingerprint) is None
        ):
            return None
        value, _ = await self._request(
            "GET", f"/channels/{binding.channel_id}/messages/{candidate.id}"
        )
        confirmed = normalize_post(value, binding, self._cutoff)
        return confirmed if confirmed == candidate else None

    async def send(self, part: str, reply_to: str = "") -> None:
        binding = self._binding
        size: int | None = None
        if isinstance(part, str) and 0 < len(part) <= 2000:
            with suppress(UnicodeError):
                size = len(part.encode("utf-16-le")) // 2
        if (
            binding is None
            or size is None
            or not 1 <= size <= 2000
            or not part.strip()
            or (reply_to != "" and not valid_discord_id(reply_to))
        ):
            raise DiscordHttpError("discord_post_invalid")
        value, _ = await self._request(
            "POST",
            f"/channels/{binding.channel_id}/messages",
            {
                "content": part,
                "allowed_mentions": {"parse": [], "replied_user": False},
                "flags": 4,
                "tts": False,
            },
        )
        confirmed = _message(value, binding.channel_id, binding.bot_user_id, bot=True, flags=4)
        if confirmed is None or confirmed["content"] != part:
            raise DiscordHttpError("discord_post_invalid")

    async def close(self) -> None:
        self._closed = True
        self._binding = None
        await self._http.close()
