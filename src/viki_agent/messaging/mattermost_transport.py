"""A single bounded, authenticated Mattermost WebSocket; no reconnect or backlog."""

from __future__ import annotations

import asyncio
import json
import logging
import ssl
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import suppress
from typing import Any, Literal, NoReturn

from websockets.asyncio.client import ClientConnection, connect

from viki_agent.api_json import unique_object
from viki_agent.messaging.mattermost_config import MattermostConfig, valid_id
from viki_agent.messaging.mattermost_http import Binding, MattermostHttpError

OPEN_DEADLINE_S = 5.0
AUTH_DEADLINE_S = 5.0
CLOSE_DEADLINE_S = 1.0
_FRAME_LIMIT = 128 * 1024
_MESSAGE_LIMIT = 8 * 1024
_DEPTH_LIMIT = 32
_REACTION_LIMIT = 4096
REACTION_CHOICES: dict[str, Literal["allow", "deny"]] = {
    "+1": "allow",
    "thumbsup": "allow",
    "-1": "deny",
    "thumbsdown": "deny",
}
# An unregistered logger keeps logging configuration away from wire records.
_QUIET_LOGGER = logging.Logger("viki_agent.mattermost_transport.wire")
_QUIET_LOGGER.disabled = True
_QUIET_LOGGER.propagate = False
_QUIET_LOGGER.addHandler(logging.NullHandler())
_FORBIDDEN_PROPS = frozenset(
    {
        "from_webhook",
        "from_bot",
        "from_oauth_app",
        "from_plugin",
        "webhook_display_name",
        "override_username",
        "override_icon_url",
        "override_icon_emoji",
        "attachments",
        "mm_blocks",
        "blocks",
        "cards",
        "mm_blocks_actions",
    }
)


class MattermostTransportError(RuntimeError):
    """A fixed refusal code without a URL, token, server reason or frame."""

    code = "mattermost_transport_failed"

    def __init__(self) -> None:
        super().__init__(self.code)


class _NoRedirectConnect(connect):
    def process_redirect(self, exc: Exception) -> Exception:
        return MattermostTransportError()


def _constant(_: str) -> NoReturn:
    raise ValueError("invalid_json_constant")


def _object(raw: str | bytes) -> dict[str, Any]:
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > _FRAME_LIMIT:
        raise ValueError("invalid_frame")
    value = json.loads(raw, object_pairs_hook=unique_object, parse_constant=_constant)
    if not isinstance(value, dict):
        raise ValueError("invalid_frame")
    pending: list[tuple[Any, int]] = [(value, 0)]
    while pending:
        item, depth = pending.pop()
        if isinstance(item, (dict, list)):
            if depth > _DEPTH_LIMIT:
                raise ValueError("invalid_frame")
            children = item.values() if isinstance(item, dict) else item
            pending.extend((child, depth + 1) for child in children)
    return value


def _failure_callback(on_failure: Callable[[], None] | None) -> Callable[[], None]:
    notified = False

    def notify() -> None:
        nonlocal notified
        if notified:
            return
        notified = True
        if on_failure is not None:
            # Notification cannot replace the original failure or prevent cleanup.
            with suppress(BaseException):
                on_failure()

    return notify


async def _authenticate(
    socket: ClientConnection, config: MattermostConfig, binding: Binding
) -> None:
    async with asyncio.timeout(AUTH_DEADLINE_S):
        await socket.send(
            json.dumps(
                {
                    "seq": 1,
                    "action": "authentication_challenge",
                    "data": {"token": config.bot_token},
                }
            )
        )
        hello = ok = False
        while not (hello and ok):
            frame = _object(await socket.recv())
            if "status" in frame:
                if (
                    frame["status"] != "OK"
                    or type(frame.get("seq_reply")) is not int
                    or frame["seq_reply"] != 1
                    or frame.get("error") is not None
                ):
                    raise ValueError("invalid_auth")
                ok = True
            elif frame.get("event") == "hello":
                broadcast = frame.get("broadcast")
                if (
                    not isinstance(frame.get("data"), dict)
                    or not isinstance(broadcast, dict)
                    or broadcast.get("user_id") != binding.bot_user_id
                ):
                    raise ValueError("invalid_auth")
                hello = True


async def _discard_buffered(socket: ClientConnection) -> None:
    # recv cancellation is public and safe; a zero deadline consumes only buffered messages.
    while True:
        try:
            async with asyncio.timeout(0):
                _object(await socket.recv())
        except TimeoutError:
            return


def _candidate(frame: dict[str, Any], binding: Binding, cutoff: int) -> dict[str, Any] | None:
    data, broadcast = frame.get("data"), frame.get("broadcast")
    if (
        frame.get("event") != "posted"
        or not isinstance(data, dict)
        or not isinstance(broadcast, dict)
        or broadcast.get("channel_id") != binding.channel_id
        or not isinstance(data.get("post"), str)
    ):
        return None
    post = _object(data["post"])
    message, props, metadata = post.get("message"), post.get("props"), post.get("metadata")
    if (
        not valid_id(post.get("id"))
        or post.get("user_id") != binding.owner_user_id
        or post.get("channel_id") != binding.channel_id
        or type(post.get("create_at")) is not int
        or post["create_at"] < cutoff
        or any(type(post.get(key)) is not int or post[key] != 0 for key in ("edit_at", "delete_at"))
        or post.get("type") != ""
        or post.get("original_id", "") != ""
        or (post.get("root_id") != "" and not valid_id(post.get("root_id")))
        or not isinstance(message, str)
        or len(message.encode("utf-8")) > _MESSAGE_LIMIT
        or not isinstance(props, dict)
        or not _FORBIDDEN_PROPS.isdisjoint(props)
        or post.get("file_ids") not in (None, [])
        or (metadata is not None and (not isinstance(metadata, dict) or metadata.get("files")))
    ):
        return None
    post["_fresh_cutoff_ms"] = cutoff
    return post


def reaction_candidate(reaction: object, binding: Binding, cutoff: int) -> dict[str, Any] | None:
    if type(reaction) is not dict:
        return None
    emoji, created, deleted = (
        reaction.get("emoji_name"),
        reaction.get("create_at"),
        reaction.get("delete_at"),
    )
    if (
        reaction.get("user_id") != binding.owner_user_id
        or reaction.get("channel_id") != binding.channel_id
        or not valid_id(reaction.get("post_id"))
        or type(emoji) is not str
        or emoji not in REACTION_CHOICES
        or type(created) is not int
        or not created >= cutoff > 0
        or type(deleted) is not int
        or deleted != 0
    ):
        return None
    return {
        "_reaction": True,
        "post_id": reaction["post_id"],
        "emoji_name": emoji,
        "create_at": created,
        "choice": REACTION_CHOICES[emoji],
        "_fresh_cutoff_ms": cutoff,
    }


def _reaction(frame: dict[str, Any], binding: Binding, cutoff: int) -> dict[str, Any] | None:
    data, broadcast = frame.get("data"), frame.get("broadcast")
    if (
        frame.get("event") != "reaction_added"
        or not isinstance(data, dict)
        or not isinstance(broadcast, dict)
        or broadcast.get("channel_id") != binding.channel_id
        or not isinstance(data.get("reaction"), str)
        or len(data["reaction"].encode("utf-8")) > _REACTION_LIMIT
    ):
        return None
    return reaction_candidate(_object(data["reaction"]), binding, cutoff)


async def posted(
    config: MattermostConfig,
    binding: Binding,
    *,
    fresh_cutoff: Callable[[], Awaitable[int]],
    ssl_context: ssl.SSLContext,
    on_failure: Callable[[], None] | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Yield fresh owner post and thumbs-reaction candidates for the caller's REST confirmation."""
    notify = _failure_callback(on_failure)
    if (
        not all(
            valid_id(value)
            for value in (binding.bot_user_id, binding.owner_user_id, binding.channel_id)
        )
        or binding.owner_user_id != config.owner_user_id
        or binding.bot_user_id == binding.owner_user_id
        or not isinstance(ssl_context, ssl.SSLContext)
        or not ssl_context.check_hostname
        or ssl_context.verify_mode != ssl.CERT_REQUIRED
    ):
        notify()
        raise MattermostTransportError
    try:
        async with _NoRedirectConnect(
            "wss://" + config.url.removeprefix("https://") + "/api/v4/websocket",
            ssl=ssl_context,
            proxy=None,
            compression=None,
            user_agent_header=None,
            max_size=_FRAME_LIMIT,
            max_queue=4,
            open_timeout=OPEN_DEADLINE_S,
            close_timeout=CLOSE_DEADLINE_S,
            logger=_QUIET_LOGGER,
        ) as socket:
            try:
                await _authenticate(socket, config, binding)
                async with asyncio.timeout(AUTH_DEADLINE_S):
                    cutoff = await fresh_cutoff()
                if type(cutoff) is not int or cutoff <= 0 or cutoff % 1000:
                    raise ValueError("invalid_cutoff")
                await _discard_buffered(socket)
                while True:
                    frame = _object(await socket.recv())
                    candidate = _candidate(frame, binding, cutoff)
                    if candidate is None:
                        candidate = _reaction(frame, binding, cutoff)
                    if candidate is not None:
                        yield candidate
            finally:
                notify()
    except MattermostHttpError:
        raise
    except Exception:
        pass
    finally:
        notify()
    raise MattermostTransportError
