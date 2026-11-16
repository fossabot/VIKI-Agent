"""Fixed Mattermost API operations with bounded authenticated HTTPS responses."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import ssl
import time
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from email.utils import format_datetime, parsedate_to_datetime
from typing import Any, Literal, NoReturn

import httpx2

from viki_agent.api_json import unique_object
from viki_agent.messaging.mattermost_config import MattermostConfig, valid_id

# The pinned clients log request URLs and transport details at INFO/DEBUG.
# Keep rejecting records if logging configuration re-enables these loggers.
for _logger_name in (
    "httpx2",
    "httpcore2",
    "httpcore2.connection",
    "httpcore2.http11",
    "httpcore2.http2",
    "httpcore2.proxy",
    "httpcore2.socks",
):
    _logger = logging.getLogger(_logger_name)
    _logger.disabled = True
    _logger.addFilter(lambda _: False)

REQUEST_DEADLINE_S = 5.0
_BODY_LIMIT = 128 * 1024
_MESSAGE_LIMIT = 12 * 1024
_CODES = (
    "mattermost_http_failed",
    "mattermost_binding_failed",
    "mattermost_freshness_failed",
    "mattermost_post_invalid",
    "mattermost_token_rejected",
    "mattermost_unreachable",
)
ErrorCode = Literal[
    "mattermost_http_failed",
    "mattermost_binding_failed",
    "mattermost_freshness_failed",
    "mattermost_post_invalid",
    "mattermost_token_rejected",
    "mattermost_unreachable",
]


class MattermostHttpError(RuntimeError):
    """Only fixed codes cross the API boundary."""

    def __init__(self, code: ErrorCode) -> None:
        self.code = code if code in _CODES else "mattermost_http_failed"
        super().__init__(self.code)


class FixedHttpError(RuntimeError):
    """A fixed refusal with bounded status headers, without request or response content."""

    code = "messaging_http_failed"
    unanswered = False

    def __init__(
        self,
        status_code: int | None = None,
        headers: dict[str, str] | None = None,
        *,
        unanswered: bool = False,
    ) -> None:
        self.status_code = status_code
        self.headers = headers or {}
        if unanswered:
            self.unanswered = True
        super().__init__(self.code)


@dataclass(frozen=True)
class Binding:
    bot_user_id: str
    owner_user_id: str
    channel_id: str


def _constant(_: str) -> NoReturn:
    raise ValueError("invalid_json_constant")


def _float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("invalid_json_number")
    return number


def _safe_headers(headers: httpx2.Headers) -> dict[str, str]:
    safe = {}
    for name in (
        "date",
        "retry-after",
        "x-ratelimit-limit",
        "x-ratelimit-remaining",
        "x-ratelimit-reset",
        "x-ratelimit-reset-after",
        "x-ratelimit-bucket",
        "x-ratelimit-global",
        "x-ratelimit-scope",
    ):
        values = headers.get_list(name)
        if (
            len(values) == 1
            and len(values[0]) <= (64 if name == "date" else 128)
            and all(32 <= ord(char) <= 126 for char in values[0])
        ):
            safe[name] = values[0]
    return safe


def server_date(headers: Mapping[str, str]) -> int | None:
    date = headers.get("date")
    if date is None or len(date) > 64:
        return None
    try:
        value = parsedate_to_datetime(date)
        if format_datetime(value, usegmt=True) != date or value.timestamp() < 0:
            return None
        return int(value.timestamp()) * 1000
    except ValueError, TypeError, OverflowError, OSError:
        return None


def _retry_after_seconds(value: str) -> int:
    return int(value) if re.fullmatch(r"[0-9]{1,5}", value) and 1 <= int(value) <= 86400 else 86400


def _user(body: Any, expected: str | None, *, bot: bool) -> str | None:
    if (
        not isinstance(body, dict)
        or not valid_id(body.get("id"))
        or (expected is not None and body["id"] != expected)
        or type(body.get("delete_at")) is not int
        or body["delete_at"] != 0
        or body.get("is_bot", False) is not bot
    ):
        return None
    if bot and (not isinstance(body.get("roles"), str) or "system_admin" in body["roles"].split()):
        return None
    return body["id"]


class FixedJsonHttp:
    """Bounded JSON requests admitted by each provider's closed method and path predicate.

    A reply that sets a cookie is refused unless `set_cookie` is "drop", which ignores the
    header. The cookie jar is cleared around every request, and a request that would carry a
    cookie is refused in either mode.
    """

    def __init__(
        self,
        base_url: str,
        headers: Mapping[str, str],
        *,
        allowed: Callable[[str, str], bool],
        transport: httpx2.AsyncBaseTransport | None = None,
        set_cookie: Literal["refuse", "drop"] = "refuse",
    ) -> None:
        self._base_url = base_url
        self._headers = {**headers, "Accept": "application/json", "Accept-Encoding": "identity"}
        self._admitted = allowed
        self._set_cookie = set_cookie
        self._failure_callback: Callable[[FixedHttpError], None] | None = None
        self._ssl_context = httpx2.create_ssl_context(verify=True, trust_env=False)
        limits = httpx2.Limits(max_connections=2, max_keepalive_connections=2)
        if transport is None:
            transport = httpx2.AsyncHTTPTransport(
                verify=self._ssl_context,
                trust_env=False,
                proxy=None,
                retries=0,
                http1=True,
                http2=False,
                limits=limits,
            )
        self._client = httpx2.AsyncClient(
            transport=transport,
            verify=self._ssl_context,
            auth=None,
            proxy=None,
            trust_env=False,
            follow_redirects=False,
            http1=True,
            http2=False,
            limits=limits,
            timeout=REQUEST_DEADLINE_S,
            event_hooks={"response": [self._clear_cookies]},
        )

    @property
    def ssl_context(self) -> ssl.SSLContext:
        return self._ssl_context

    async def _clear_cookies(self, _: httpx2.Response) -> None:
        self._client.cookies.clear()

    def set_failure_callback(self, handler: Callable[[FixedHttpError], None] | None) -> None:
        """Replace the one synchronous failure callback; pass only fresh safe metadata."""
        self._failure_callback = handler

    async def request(
        self,
        method: str,
        path: str,
        body: Any = None,
        *,
        status_codes: tuple[int, ...] = (200,),
        authorization: str | None = None,
    ) -> tuple[Any, dict[str, str]]:
        status_code, headers = None, {}
        notified = False
        unanswered = False

        def notify_failure() -> None:
            nonlocal notified
            if notified:
                return
            notified = True
            if self._failure_callback is not None:
                with suppress(Exception):
                    self._failure_callback(
                        FixedHttpError(status_code, dict(headers), unanswered=unanswered)
                    )

        if not self._admitted(method, path) or (
            authorization is not None
            and (
                not isinstance(authorization, str)
                or re.fullmatch(r"(?:Bearer|Bot) [\x21-\x7e]{1,4096}", authorization) is None
            )
        ):
            notify_failure()
            raise FixedHttpError
        try:
            async with asyncio.timeout(REQUEST_DEADLINE_S):
                self._client.cookies.clear()
                raw_body = (
                    json.dumps(
                        body, ensure_ascii=False, allow_nan=False, separators=(",", ":")
                    ).encode("utf-8")
                    if body is not None
                    else None
                )
                if raw_body is not None and len(raw_body) > _BODY_LIMIT:
                    raise ValueError("invalid_json_size")
                request = self._client.build_request(
                    method,
                    self._base_url + path,
                    content=raw_body,
                    headers={
                        **self._headers,
                        **({"Content-Type": "application/json"} if raw_body is not None else {}),
                    },
                )
                if authorization is not None:
                    request.headers["Authorization"] = authorization
                if "cookie" in request.headers:
                    raise ValueError("invalid_cookie")
                response = await self._client.send(request, stream=True, follow_redirects=False)
                try:
                    self._client.cookies.clear()
                    status_code = response.status_code
                    headers = _safe_headers(response.headers)
                    encodings = response.headers.get_list("content-encoding")
                    if (
                        not 200 <= status_code < 300
                        or status_code not in status_codes
                        or ("set-cookie" in response.headers and self._set_cookie != "drop")
                        or (encodings and encodings != ["identity"])
                    ):
                        raise ValueError("invalid_response")
                    if (
                        response.is_stream_consumed
                        or response.is_closed
                        or not isinstance(response.stream, httpx2.AsyncByteStream)
                    ):
                        raise ValueError("invalid_stream")
                    response.is_stream_consumed = True
                    raw = bytearray()
                    # aiter_raw closes at EOF before JSON can be validated. Own this raw
                    # stream's cleanup so a refusal revokes authority before close can wait.
                    async for chunk in response.stream:
                        if len(raw) + len(chunk) > _BODY_LIMIT:
                            raise ValueError("invalid_json_size")
                        raw.extend(chunk)
                    value = json.loads(
                        raw.decode("utf-8"),
                        object_pairs_hook=unique_object,
                        parse_constant=_constant,
                        parse_float=_float,
                    )
                    return value, headers
                except BaseException:
                    # Revocation cannot wait behind response cleanup or its cancellation.
                    notify_failure()
                    raise
                finally:
                    await response.aclose()
        except asyncio.CancelledError:
            notify_failure()
            raise
        except httpx2.TransportError, TimeoutError:
            unanswered = status_code is None
            notify_failure()
        except httpx2.HTTPError, ValueError, TypeError, RecursionError, RuntimeError:
            notify_failure()
        raise FixedHttpError(status_code, headers, unanswered=unanswered)

    async def close(self) -> None:
        await self._client.aclose()


class MattermostHttp(FixedJsonHttp):
    def __init__(
        self,
        config: MattermostConfig,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config = config
        self._transport = transport
        self._binding: Binding | None = None
        self._server_date: int | None = None
        self._clock = clock
        self._cooldown_until = 0.0
        super().__init__(
            config.url,
            {"Authorization": "Bearer " + config.bot_token},
            allowed=self._allowed,
            transport=transport,
        )
        self._failure_handler: Callable[[], None] | None = None
        self.set_failure_callback(self._http_failed)

    def set_failure_handler(self, handler: Callable[[], None] | None) -> None:
        self._failure_handler = handler

    @property
    def cooldown_until(self) -> float:
        return self._cooldown_until

    def _http_failed(self, error: FixedHttpError) -> None:
        if error.status_code == 429:
            self._cooldown_until = max(
                self._cooldown_until,
                self._clock() + _retry_after_seconds(error.headers.get("retry-after", "")),
            )
        if self._failure_handler is not None:
            self._failure_handler()

    def _allowed(self, method: str, path: str) -> bool:
        if method == "GET":
            return (
                path in ("/api/v4/users/me", "/api/v4/users/" + self._config.owner_user_id)
                or re.fullmatch(r"/api/v4/posts/[a-z0-9]{26}(?:/reactions)?", path) is not None
                or re.fullmatch(r"/api/v4/channels/[a-z0-9]{26}/members\?per_page=3&page=0", path)
                is not None
            )
        return method == "POST" and path in ("/api/v4/channels/direct", "/api/v4/posts")

    async def _request(
        self, method: str, path: str, body: Any = None, *, account: bool = False
    ) -> tuple[Any, dict[str, str]]:
        if self._clock() < self._cooldown_until:
            raise MattermostHttpError("mattermost_http_failed")
        try:
            return await self.request(
                method, path, body, status_codes=(201 if method == "POST" else 200,)
            )
        except FixedHttpError as error:
            unanswered, status_code = error.unanswered, error.status_code
        if unanswered:
            raise MattermostHttpError("mattermost_unreachable")
        if path == "/api/v4/users/me" and status_code in (401, 403):
            raise MattermostHttpError("mattermost_token_rejected")
        if account and status_code is not None and 400 <= status_code < 500 and status_code != 429:
            raise MattermostHttpError("mattermost_binding_failed")
        raise MattermostHttpError("mattermost_http_failed")

    async def bind(self) -> Binding:
        self._binding = None
        self._server_date = None
        me, headers = await self._request("GET", "/api/v4/users/me")
        bot_id = _user(me, None, bot=True)
        if bot_id is None or bot_id == self._config.owner_user_id:
            raise MattermostHttpError("mattermost_binding_failed")
        date = server_date(headers)
        if date is None:
            raise MattermostHttpError("mattermost_freshness_failed")
        owner, _ = await self._request(
            "GET", "/api/v4/users/" + self._config.owner_user_id, account=True
        )
        if _user(owner, self._config.owner_user_id, bot=False) is None:
            raise MattermostHttpError("mattermost_binding_failed")
        channel, _ = await self._request(
            "POST", "/api/v4/channels/direct", [bot_id, self._config.owner_user_id], account=True
        )
        if (
            not isinstance(channel, dict)
            or not valid_id(channel.get("id"))
            or channel.get("type") != "D"
            or type(channel.get("delete_at")) is not int
            or channel["delete_at"] != 0
        ):
            raise MattermostHttpError("mattermost_binding_failed")
        channel_id = channel["id"]
        members, _ = await self._request(
            "GET", f"/api/v4/channels/{channel_id}/members?per_page=3&page=0", account=True
        )
        if (
            not isinstance(members, list)
            or len(members) != 2
            or any(
                not isinstance(member, dict)
                or member.get("channel_id") != channel_id
                or not valid_id(member.get("user_id"))
                for member in members
            )
            or {member["user_id"] for member in members} != {bot_id, self._config.owner_user_id}
        ):
            raise MattermostHttpError("mattermost_binding_failed")
        self._binding = Binding(bot_id, self._config.owner_user_id, channel_id)
        self._server_date = date
        return self._binding

    async def fresh_cutoff(self) -> int:
        binding = self._binding
        if binding is None or self._server_date is None:
            raise MattermostHttpError("mattermost_freshness_failed")
        me, headers = await self._request("GET", "/api/v4/users/me")
        date = server_date(headers)
        if (
            _user(me, binding.bot_user_id, bot=True) is None
            or date is None
            or date < self._server_date
        ):
            raise MattermostHttpError("mattermost_freshness_failed")
        self._server_date = date
        return date + 1000

    async def post(self, post_id: str) -> dict[str, Any]:
        binding = self._binding
        if binding is None or not valid_id(post_id):
            raise MattermostHttpError("mattermost_post_invalid")
        post, _ = await self._request("GET", "/api/v4/posts/" + post_id)
        if (
            not isinstance(post, dict)
            or post.get("id") != post_id
            or post.get("channel_id") != binding.channel_id
            or post.get("user_id") != binding.owner_user_id
            or type(post.get("create_at")) is not int
            or post["create_at"] <= 0
            or any(
                type(post.get(key)) is not int or post[key] != 0 for key in ("edit_at", "delete_at")
            )
        ):
            raise MattermostHttpError("mattermost_post_invalid")
        return post

    async def reactions(self, post_id: str) -> list[dict[str, Any]]:
        if self._binding is None or not valid_id(post_id):
            raise MattermostHttpError("mattermost_post_invalid")
        found, _ = await self._request("GET", f"/api/v4/posts/{post_id}/reactions")
        if found is None:
            return []
        if not isinstance(found, list):
            raise MattermostHttpError("mattermost_post_invalid")
        return [item for item in found if isinstance(item, dict)]

    def progress_client(self) -> ProgressHttp:
        """A separate client whose failures never reach this one's failure handler."""
        return ProgressHttp(self._config, transport=self._transport, clock=self._clock)

    async def send(self, binding: Binding, message: str, root_id: str = "") -> str:
        size: int | None = None
        try:
            if isinstance(message, str):
                size = len(message.encode("utf-8"))
        except UnicodeError:
            pass
        if (
            self._binding is None
            or binding != self._binding
            or size is None
            or size > _MESSAGE_LIMIT
            or (root_id != "" and not valid_id(root_id))
        ):
            raise MattermostHttpError("mattermost_post_invalid")
        body = {"channel_id": binding.channel_id, "message": message, "root_id": root_id}
        post, _ = await self._request("POST", "/api/v4/posts", body)
        if (
            not isinstance(post, dict)
            or not valid_id(post.get("id"))
            or post.get("user_id") != binding.bot_user_id
            or any(post.get(key) != value for key, value in body.items())
        ):
            raise MattermostHttpError("mattermost_post_invalid")
        return post["id"]


PROGRESS_EMOJI = frozenset({"eyes", "white_check_mark", "warning", "black_square_for_stop"})


class ProgressError(RuntimeError):
    """A progress call that was refused, failed or answered unexpectedly."""

    code = "mattermost_progress_failed"

    def __init__(self) -> None:
        super().__init__(self.code)


def _fits_message(value: object) -> bool:
    try:
        return isinstance(value, str) and len(value.encode("utf-8")) <= _MESSAGE_LIMIT
    except UnicodeError:
        return False


class ProgressHttp(FixedJsonHttp):
    """Best-effort status posts, reactions and typing for the bound bot and channel.

    Its failures stay local: only a 429 is recorded, as a cooldown for this client.
    """

    def __init__(
        self,
        config: MattermostConfig,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ids: tuple[str, str] | None = None
        self._created: set[str] = set()
        self._clock = clock
        self._cooldown_until = 0.0
        super().__init__(
            config.url,
            {"Authorization": "Bearer " + config.bot_token},
            allowed=self._allowed,
            transport=transport,
        )
        self.set_failure_callback(self._http_failed)

    def bind(self, bot_user_id: str, channel_id: str) -> None:
        if not valid_id(bot_user_id) or not valid_id(channel_id):
            raise ValueError("mattermost_progress_invalid")
        if self._ids not in (None, (bot_user_id, channel_id)):
            raise ValueError("mattermost_progress_bound")
        self._ids = (bot_user_id, channel_id)

    def _http_failed(self, error: FixedHttpError) -> None:
        if error.status_code == 429:
            self._cooldown_until = max(
                self._cooldown_until,
                self._clock() + _retry_after_seconds(error.headers.get("retry-after", "")),
            )

    def _allowed(self, method: str, path: str) -> bool:
        if self._ids is None:
            return False
        if method == "POST":
            return path in (
                "/api/v4/reactions",
                "/api/v4/posts",
                f"/api/v4/users/{self._ids[0]}/typing",
            )
        if method == "DELETE":
            return (
                re.fullmatch(
                    rf"/api/v4/users/{self._ids[0]}/posts/[a-z0-9]{{26}}/reactions/eyes", path
                )
                is not None
            )
        return (
            method == "PUT" and re.fullmatch(r"/api/v4/posts/[a-z0-9]{26}/patch", path) is not None
        )

    async def _call(self, method: str, path: str, body: Any = None) -> Any:
        if self._clock() < self._cooldown_until:
            raise ProgressError
        try:
            value, _ = await self.request(method, path, body, status_codes=(200, 201))
        except FixedHttpError:
            raise ProgressError from None
        return value

    def _bound(self) -> tuple[str, str]:
        if self._ids is None:
            raise ProgressError
        return self._ids

    async def react(self, post_id: str, emoji: str) -> None:
        bot, _ = self._bound()
        if not valid_id(post_id) or emoji not in PROGRESS_EMOJI:
            raise ProgressError
        body = {"user_id": bot, "post_id": post_id, "emoji_name": emoji}
        reaction = await self._call("POST", "/api/v4/reactions", body)
        if not isinstance(reaction, dict) or any(reaction.get(k) != v for k, v in body.items()):
            raise ProgressError

    async def unreact(self, post_id: str) -> None:
        bot, _ = self._bound()
        if not valid_id(post_id):
            raise ProgressError
        await self._call("DELETE", f"/api/v4/users/{bot}/posts/{post_id}/reactions/eyes")

    async def typing(self, parent_id: str) -> None:
        bot, channel = self._bound()
        if parent_id != "" and not valid_id(parent_id):
            raise ProgressError
        body = {"channel_id": channel, "parent_id": parent_id}
        await self._call("POST", f"/api/v4/users/{bot}/typing", body)

    async def create(self, message: str, root_id: str) -> str:
        bot, channel = self._bound()
        if not _fits_message(message) or (root_id != "" and not valid_id(root_id)):
            raise ProgressError
        body = {"channel_id": channel, "message": message, "root_id": root_id}
        post = await self._call("POST", "/api/v4/posts", body)
        if (
            not isinstance(post, dict)
            or not valid_id(post.get("id"))
            or post.get("user_id") != bot
            or any(post.get(key) != value for key, value in body.items())
        ):
            raise ProgressError
        self._created.add(post["id"])
        return post["id"]

    async def edit(self, post_id: str, message: str) -> None:
        bot, _ = self._bound()
        if post_id not in self._created or not _fits_message(message):
            raise ProgressError
        post = await self._call("PUT", f"/api/v4/posts/{post_id}/patch", {"message": message})
        if not isinstance(post, dict) or post.get("id") != post_id or post.get("user_id") != bot:
            raise ProgressError
