"""Fixed Slack owner-DM binding, original-message confirmation and bounded sends."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import ssl
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Any, NoReturn, TypeIs
from urllib.parse import urlsplit

from viki_agent.messaging.mattermost_http import (
    FixedHttpError,
    FixedJsonHttp,
    _retry_after_seconds,
    server_date,
)
from viki_agent.messaging.messaging_channel import RemoteBinding, RemotePost, StopReason
from viki_agent.messaging.messaging_config import SlackConfig, valid_slack_id

_ID = r"[A-Z0-9]{7,31}"
_TS = r"[1-9][0-9]{0,10}\.[0-9]{6}"
_POST_LIMIT = 128 * 1024
_ADORNMENTS = (
    "subtype",
    "bot_id",
    "bot_profile",
    "app_id",
    "username",
    "icons",
    "edited",
    "hidden",
    "deleted",
    "files",
    "attachments",
    "thread_ts",
    "reply_count",
    "reply_users",
    "latest_reply",
    "replies",
    "metadata",
    "file_id",
    "upload",
)
_CODES = (
    "slack_http_failed",
    "slack_binding_failed",
    "slack_freshness_failed",
    "slack_post_invalid",
    "slack_socket_invalid",
    "slack_rate_limited",
)


class SlackHttpError(RuntimeError):
    """Only fixed codes cross this boundary."""

    def __init__(self, code: str = "slack_http_failed") -> None:
        self.code = code if code in _CODES else "slack_http_failed"
        super().__init__(self.code)


def _id(value: object, prefix: str) -> TypeIs[str]:
    return isinstance(value, str) and re.fullmatch(prefix + _ID, value) is not None


def timestamp_ms(value: object) -> int | None:
    """Preserve Slack's six-digit timestamp identity while flooring to milliseconds."""
    if not isinstance(value, str) or re.fullmatch(_TS, value) is None:
        return None
    seconds, fraction = value.split(".")
    return int(seconds) * 1000 + int(fraction[:3])


def _plain_blocks(value: object) -> bool:
    return isinstance(value, list) and all(
        isinstance(block, dict) and block.get("type") == "rich_text" for block in value
    )


def normalize_post(event: object, binding: RemoteBinding, cutoff: int) -> RemotePost | None:
    """Keep admitted owner text and identity; ordinary rich-text blocks are unused."""
    if (
        not isinstance(event, dict)
        or binding.provider != "slack"
        or len(binding.scope) != 2
        or not _id(binding.scope[0], "T")
        or not _id(binding.scope[1], "A")
        or not valid_slack_id(binding.bot_user_id)
        or not valid_slack_id(binding.owner_user_id)
        or binding.bot_user_id == binding.owner_user_id
        or not _id(binding.channel_id, "D")
        or type(cutoff) is not int
        or cutoff <= 0
        or event.get("type") != "message"
        or event.get("channel_type") != "im"
        or event.get("channel") != binding.channel_id
        or event.get("user") != binding.owner_user_id
        or ("team" in event and event["team"] != binding.scope[0])
        or any(key in event for key in _ADORNMENTS)
        or ("blocks" in event and not _plain_blocks(event["blocks"]))
        or not isinstance(event.get("text"), str)
    ):
        return None
    created = timestamp_ms(event.get("ts"))
    if created is None or created < cutoff:
        return None
    admitted = {
        "provider": "slack",
        "scope": binding.scope,
        "bot": binding.bot_user_id,
        "owner": binding.owner_user_id,
        "channel": binding.channel_id,
        "id": event["ts"],
        "text": event["text"],
        "created_ms": created,
        "root_id": "",
        "edited": None,
        "adornments": [],
    }
    try:
        raw = json.dumps(
            admitted, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    except UnicodeError:
        return None
    if len(raw) > _POST_LIMIT:
        return None
    return RemotePost(
        event["ts"], event["text"], "", created, hashlib.sha256(raw).hexdigest(), len(raw)
    )


def _socket_url(value: object) -> str | None:
    if not isinstance(value, str) or not 1 <= len(value) <= 4608:
        return None
    if any(not 33 <= ord(char) <= 126 for char in value) or "\\" in value:
        return None
    try:
        url = urlsplit(value)
        if (
            url.scheme == "wss"
            and url.username is None
            and url.password is None
            and url.port in (None, 443)
            and url.path == "/link/"
            and not url.fragment
            and re.fullmatch(
                r"wss(?:-[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)?\.slack\.com", url.hostname or ""
            )
            and re.fullmatch(
                r"(?:[A-Za-z0-9._~!$&'()*+,;=:/?-]|%[0-9A-Fa-f]{2}){1,4096}", url.query
            )
            and url.query.startswith("ticket=")
            and len(url.query) > 7
        ):
            return value
    except ValueError:
        pass
    return None


def _allowed(method: str, path: str) -> bool:
    if method == "POST":
        return path in (
            "/auth.test",
            "/conversations.open",
            "/apps.connections.open",
            "/chat.postMessage",
        )
    return method == "GET" and any(
        re.fullmatch(pattern, path) is not None
        for pattern in (
            r"/bots\.info\?bot=B" + _ID,
            r"/users\.info\?user=[UW]" + _ID,
            r"/conversations\.info\?channel=D" + _ID,
            r"/conversations\.history\?channel=D"
            + _ID
            + r"&oldest="
            + _TS
            + r"&latest="
            + _TS
            + r"&inclusive=true&limit=1",
        )
    )


def _user(value: Any, identity: str, team: str, *, bot: bool) -> TypeIs[dict[str, Any]]:
    return (
        isinstance(value, dict)
        and value.get("id") == identity
        and value.get("team_id") == team
        and value.get("deleted") is False
        and value.get("is_bot") is bot
        and (bot or value.get("is_app_user") is False)
    )


def _dm(value: Any, owner: str, team: str, expected: str | None = None) -> str | None:
    if (
        not isinstance(value, dict)
        or not _id(value.get("id"), "D")
        or (expected is not None and value["id"] != expected)
        or value.get("user") != owner
        or value.get("is_im") is not True
        or value.get("is_org_shared") is not False
        or value.get("is_private", True) is not True
        or any(
            value.get(key, False) is not False
            for key in (
                "is_mpim",
                "is_shared",
                "is_ext_shared",
                "is_pending_ext_shared",
                "is_channel",
                "is_group",
                "is_archived",
            )
        )
        or (
            "num_members" in value
            and (type(value["num_members"]) is not int or value["num_members"] != 2)
        )
        or ("context_team_id" in value and value["context_team_id"] != team)
        or ("shared_team_ids" in value and value["shared_team_ids"] not in ([], [team]))
        or any(value.get(key, []) != [] for key in ("pending_shared", "pending_connected_team_ids"))
    ):
        return None
    return value["id"]


# Slack's documented authentication-failure errors (shared across methods).
_TOKEN_ERRORS = frozenset(
    {
        "invalid_auth",
        "not_authed",
        "account_inactive",
        "token_revoked",
        "token_expired",
        "not_allowed_token_type",
    }
)
_SERVER_ERRORS = frozenset(
    {"internal_error", "fatal_error", "service_unavailable", "ratelimited", "request_timeout"}
)


def _refusal(path: str, result: Any) -> StopReason | None:
    """Classify an answered `ok: false` body into a stop reason, or None to record nothing."""
    error = result.get("error") if isinstance(result, dict) else None
    if not isinstance(error, str):
        error = None
    if error in _TOKEN_ERRORS or (path == "/apps.connections.open" and error == "missing_scope"):
        return "app_token_rejected" if path == "/apps.connections.open" else "token_rejected"
    if (
        path in ("/apps.connections.open", "/auth.test")
        or path.startswith("/conversations.history")
        or error in _SERVER_ERRORS
    ):
        return None
    return "account_check_failed"


class SlackHttp:
    def __init__(
        self,
        config: SlackConfig,
        *,
        transport: Any = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._config = config
        self._http = FixedJsonHttp(
            "https://slack.com/api",
            {"Authorization": "Bearer " + config.bot_token, "User-Agent": "VIKI Agent"},
            allowed=_allowed,
            transport=transport,
        )
        self._clock, self._sleep = clock, sleep
        self._binding: RemoteBinding | None = None
        self._bot_id: str | None = None
        self._date: int | None = None
        self._cutoff = 0
        self._cooldown_until = self._next_send = 0.0
        self._failed = self._closed = False
        self.stop_reason: StopReason | None = None
        self._send_lock = asyncio.Lock()
        self._failure_handler: Callable[[], None] | None = None
        self._failure_notified = False
        self._http.set_failure_callback(self._http_failed)

    @property
    def binding(self) -> RemoteBinding | None:
        return self._binding

    @property
    def cooldown_until(self) -> float:
        return max(self._cooldown_until, self._next_send)

    @property
    def ssl_context(self) -> ssl.SSLContext:
        return self._http.ssl_context

    def set_failure_handler(self, handler: Callable[[], None] | None) -> None:
        self._failure_handler = handler
        self._failure_notified = False

    def _notify_failure(self) -> None:
        if not self._failure_notified:
            self._failure_notified = True
            if self._failure_handler is not None:
                with suppress(Exception):
                    self._failure_handler()

    def _http_failed(self, error: FixedHttpError) -> None:
        if error.unanswered and self.stop_reason is None:
            self.stop_reason = "server_unreachable"
        if error.status_code == 429:
            seconds = _retry_after_seconds(error.headers.get("retry-after", ""))
            self._cooldown_until = max(self._cooldown_until, self._clock() + seconds)
        self._failed = True
        self._binding = None
        self._notify_failure()

    def _fail(self, code: str, reason: StopReason | None = None) -> NoReturn:
        if self.stop_reason is None:
            # The handler disarms the bot and the runtime reads the reason then.
            self.stop_reason = reason or (
                "account_check_failed" if code == "slack_binding_failed" else None
            )
        self._failed = True
        self._binding = None
        self._notify_failure()
        raise SlackHttpError(code)

    def _usable(self) -> None:
        if self._failed or self._closed or self._clock() < self._cooldown_until:
            raise SlackHttpError

    async def _request(
        self, method: str, path: str, body: Any = None
    ) -> tuple[dict[str, Any], dict[str, str]]:
        self._usable()
        code = "slack_http_failed"
        reason: StopReason | None = None
        try:
            result, headers = await self._http.request(
                method,
                path,
                body,
                authorization="Bearer " + self._config.app_token
                if path == "/apps.connections.open"
                else None,
            )
        except FixedHttpError as error:
            if error.status_code == 429:
                code = "slack_rate_limited"
            elif error.unanswered:
                reason = "server_unreachable"
        else:
            self._usable()
            if isinstance(result, dict) and result.get("ok") is True:
                return result, headers
            reason = _refusal(path, result)
        self._fail(code, reason)

    async def bind(self) -> RemoteBinding:
        self._usable()
        self._binding = None
        auth, headers = await self._request("POST", "/auth.test")
        team, user, bot = auth.get("team_id"), auth.get("user_id"), auth.get("bot_id")
        if (
            not _id(team, "T")
            or not isinstance(user, str)
            or not valid_slack_id(user)
            or not _id(bot, "B")
            or user == self._config.owner_id
        ):
            self._fail("slack_binding_failed")
        date = server_date(headers)
        if date is None:
            self._fail("slack_freshness_failed")
        info, _ = await self._request("GET", "/bots.info?bot=" + bot)
        info = info.get("bot")
        if (
            not isinstance(info, dict)
            or info.get("id") != bot
            or info.get("user_id") != user
            or not _id(info.get("app_id"), "A")
            or info.get("deleted") is not False
            or ("app_id" in auth and auth["app_id"] != info["app_id"])
        ):
            self._fail("slack_binding_failed")
        app = info["app_id"]
        bot_user, _ = await self._request("GET", "/users.info?user=" + user)
        bot_user = bot_user.get("user")
        if not _user(bot_user, user, team, bot=True):
            self._fail("slack_binding_failed")
        profile = bot_user.get("profile", {})
        if (
            not isinstance(profile, dict)
            or ("bot_id" in profile and profile["bot_id"] != bot)
            or ("api_app_id" in profile and profile["api_app_id"] != app)
        ):
            self._fail("slack_binding_failed")
        owner, _ = await self._request("GET", "/users.info?user=" + self._config.owner_id)
        if not _user(owner.get("user"), self._config.owner_id, team, bot=False):
            self._fail("slack_binding_failed")
        opened, _ = await self._request(
            "POST", "/conversations.open", {"users": self._config.owner_id, "return_im": True}
        )
        channel = _dm(opened.get("channel"), self._config.owner_id, team)
        if channel is None:
            self._fail("slack_binding_failed")
        info, _ = await self._request("GET", "/conversations.info?channel=" + channel)
        if _dm(info.get("channel"), self._config.owner_id, team, channel) is None:
            self._fail("slack_binding_failed")
        self._binding = RemoteBinding("slack", (team, app), user, self._config.owner_id, channel)
        self._bot_id, self._date = bot, date
        return self._binding

    async def fresh_cutoff(self) -> int:
        self._usable()
        binding = self._binding
        if binding is None or self._date is None:
            self._fail("slack_freshness_failed")
        auth, headers = await self._request("POST", "/auth.test")
        date = server_date(headers)
        if (
            auth.get("team_id") != binding.scope[0]
            or auth.get("user_id") != binding.bot_user_id
            or auth.get("bot_id") != self._bot_id
            or ("app_id" in auth and auth["app_id"] != binding.scope[1])
            or date is None
            or date < self._date
        ):
            self._fail("slack_freshness_failed")
        self._date = date
        self._cutoff = date + 1000
        return self._cutoff

    async def socket_url(self) -> str:
        if self._binding is None:
            self._fail("slack_binding_failed")
        response, _ = await self._request("POST", "/apps.connections.open")
        url = _socket_url(response.get("url"))
        if url is None:
            self._fail("slack_socket_invalid")
        return url

    def validate(self, candidate: object, cutoff: int) -> RemotePost | None:
        if self._binding is None or self._failed or self._closed:
            return None
        return normalize_post(candidate, self._binding, cutoff)

    async def confirm(self, candidate: RemotePost) -> RemotePost | None:
        self._usable()
        binding = self._binding
        if binding is None or not isinstance(candidate, RemotePost):
            return None
        original = {
            "type": "message",
            "channel_type": "im",
            "channel": binding.channel_id,
            "user": binding.owner_user_id,
            "ts": candidate.id,
            "text": candidate.message,
        }
        if normalize_post(original, binding, self._cutoff) != candidate:
            return None
        body, _ = await self._request(
            "GET",
            "/conversations.history?channel="
            + binding.channel_id
            + "&oldest="
            + candidate.id
            + "&latest="
            + candidate.id
            + "&inclusive=true&limit=1",
        )
        messages = body.get("messages")
        if (
            not isinstance(messages, list)
            or len(messages) != 1
            or not isinstance(messages[0], dict)
        ):
            return None
        event = {"channel": binding.channel_id, "channel_type": "im", **messages[0]}
        confirmed = normalize_post(event, binding, self._cutoff)
        return confirmed if confirmed == candidate else None

    async def send(self, part: str, reply_to: str = "") -> None:
        self._usable()
        if (
            self._binding is None
            or reply_to != ""
            or not isinstance(part, str)
            or not 1 <= len(part) <= 4000
        ):
            self._fail("slack_post_invalid")
        encoded = True
        try:
            part.encode("utf-8")
        except UnicodeError:
            encoded = False
        if not encoded:
            self._fail("slack_post_invalid")
        async with self._send_lock:
            while self._clock() < self._next_send:
                await self._sleep(self._next_send - self._clock())
                self._usable()
            self._usable()
            binding = self._binding
            if binding is None:
                self._fail("slack_post_invalid")
            self._next_send = self._clock() + 1.0
            response, _ = await self._request(
                "POST",
                "/chat.postMessage",
                {
                    "channel": binding.channel_id,
                    "text": part,
                    "mrkdwn": True,
                    "parse": "none",
                    "unfurl_links": False,
                    "unfurl_media": False,
                },
            )
            message = response.get("message")
            if (
                response.get("channel") != binding.channel_id
                or timestamp_ms(response.get("ts")) is None
                or not isinstance(message, dict)
                or message.get("text") != part
                or message.get("bot_id") != self._bot_id
                or message.get("ts") != response["ts"]
                or message.get("user", binding.bot_user_id) != binding.bot_user_id
                or message.get("app_id", binding.scope[1]) != binding.scope[1]
                or message.get("team", binding.scope[0]) != binding.scope[0]
                or message.get("type", "message") != "message"
                or message.get("subtype", "bot_message") != "bot_message"
                or any(key in message for key in ("thread_ts", "edited", "files", "attachments"))
            ):
                self._fail("slack_post_invalid")
            if "bot_profile" in message:
                profile = message["bot_profile"]
                if (
                    not isinstance(profile, dict)
                    or profile.get("id") != self._bot_id
                    or profile.get("app_id") != binding.scope[1]
                ):
                    self._fail("slack_post_invalid")

    async def close(self) -> None:
        self._closed = True
        self._binding = None
        await self._http.close()
