"""One authenticated Slack Socket Mode stream with bounded ACKs and no reconnect."""

from __future__ import annotations

import asyncio
import json
import re
import ssl
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from viki_agent.api_json import unique_object
from viki_agent.messaging.mattermost_http import _constant, _float
from viki_agent.messaging.mattermost_transport import (
    _QUIET_LOGGER,
    _failure_callback,
    _NoRedirectConnect,
)
from viki_agent.messaging.messaging_channel import RemoteBinding, RemotePost
from viki_agent.messaging.messaging_config import valid_slack_id
from viki_agent.messaging.slack_http import SlackHttp, _socket_url, normalize_post

OPEN_DEADLINE_S = AUTH_DEADLINE_S = ACK_DEADLINE_S = 5.0
CLOSE_DEADLINE_S = 1.0
_FRAME_LIMIT = 128 * 1024
_DEPTH_LIMIT = 32
_DEDUP_LIMIT = 4096


class SlackTransportError(RuntimeError):
    """A fixed refusal without socket tickets, tokens, payloads or server reasons."""

    code = "slack_transport_failed"

    def __init__(self) -> None:
        super().__init__(self.code)


def _object(raw: object) -> dict[str, Any]:
    if (
        not isinstance(raw, str)
        or len(raw) > _FRAME_LIMIT
        or len(raw.encode("utf-8")) > _FRAME_LIMIT
    ):
        raise ValueError("invalid_frame")
    # Reject depth before json.loads allocates nested containers; quoted braces are data.
    depth, quoted, escaped = 0, False, False
    for char in raw:
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in "[{":
            depth += 1
            if depth > _DEPTH_LIMIT:
                raise ValueError("invalid_frame")
        elif char in "]}":
            depth -= 1
    value = json.loads(
        raw, object_pairs_hook=unique_object, parse_constant=_constant, parse_float=_float
    )
    if not isinstance(value, dict):
        raise ValueError("invalid_frame")
    return value


def _envelope_id(frame: dict[str, Any]) -> str:
    identity = frame.get("envelope_id")
    if (
        not isinstance(identity, str)
        or len(identity) != 36
        or str(uuid.UUID(identity)) != identity
        or frame.get("type") not in ("events_api", "interactive", "slash_commands")
        or type(frame.get("accepts_response_payload")) is not bool
        or not isinstance(frame.get("payload"), dict)
    ):
        raise ValueError("invalid_envelope")
    return identity


def _remember(seen: set[str], key: str) -> bool:
    if key in seen:
        return False
    if len(seen) >= _DEDUP_LIMIT:
        raise ValueError("dedup_exhausted")
    seen.add(key)
    return True


async def _ack(socket: Any, identity: str) -> None:
    async with asyncio.timeout(ACK_DEADLINE_S):
        await socket.send(json.dumps({"envelope_id": identity}, separators=(",", ":")))


async def _envelope(
    socket: Any,
    frame: dict[str, Any],
    binding: RemoteBinding,
    seen: set[str],
    cutoff: int | None,
) -> RemotePost | None:
    identity = _envelope_id(frame)
    await _ack(socket, identity)
    if not _remember(seen, "envelope:" + identity):
        return None
    payload = frame["payload"]
    event = payload.get("event")
    if payload.get("type") == "app_rate_limited" or (
        isinstance(event, dict) and event.get("type") == "app_rate_limited"
    ):
        raise ValueError("rate_limited")
    if (
        frame["type"] != "events_api"
        or payload.get("type") != "event_callback"
        or payload.get("team_id") != binding.scope[0]
        or payload.get("api_app_id") != binding.scope[1]
        or payload.get("is_ext_shared_channel", False) is not False
    ):
        return None
    authorizations = payload.get("authorizations")
    if "authorizations" in payload and (
        not isinstance(authorizations, list)
        or not authorizations
        or any(
            not isinstance(item, dict)
            or item.get("team_id") != binding.scope[0]
            or item.get("user_id") != binding.bot_user_id
            or item.get("is_bot") is not True
            or item.get("is_enterprise_install", False) is not False
            or item.get("app_id", binding.scope[1]) != binding.scope[1]
            for item in authorizations
        )
    ):
        return None
    event_id = payload.get("event_id")
    if not isinstance(event_id, str) or re.fullmatch(r"Ev[A-Z0-9]{7,31}", event_id) is None:
        return None
    if not _remember(seen, "event:" + event_id):
        return None
    post = normalize_post(event, binding, 1)
    if post is None or not _remember(seen, "message:" + post.id):
        return None
    return post if cutoff is not None and post.created_ms >= cutoff else None


async def _authenticate(socket: Any, binding: RemoteBinding, seen: set[str]) -> None:
    while True:
        frame = _object(await socket.recv())
        if frame.get("type") == "hello":
            info = frame.get("connection_info")
            if not isinstance(info, dict) or info.get("app_id") != binding.scope[1]:
                raise ValueError("invalid_hello")
            return
        await _envelope(socket, frame, binding, seen, None)


async def _discard_buffered(socket: Any, binding: RemoteBinding, seen: set[str]) -> None:
    while True:
        try:
            # Safe public recv cancellation consumes buffered frames without waiting for new ones.
            async with asyncio.timeout(0):
                raw = await socket.recv()
        except TimeoutError:
            return
        await _envelope(socket, _object(raw), binding, seen, None)


async def posted(
    http: SlackHttp,
    binding: RemoteBinding,
    *,
    fresh_cutoff: Callable[[], Awaitable[int]],
    connect: Callable[..., Any] | None = None,
    on_failure: Callable[[], None] | None = None,
) -> AsyncIterator[RemotePost]:
    """ACK first, then yield original owner text for the caller's queued REST confirmation."""
    notify = _failure_callback(on_failure)
    try:
        context = http.ssl_context
        if (
            http.binding != binding
            or binding.provider != "slack"
            or len(binding.scope) != 2
            or re.fullmatch(r"T[A-Z0-9]{7,31}", binding.scope[0]) is None
            or re.fullmatch(r"A[A-Z0-9]{7,31}", binding.scope[1]) is None
            or re.fullmatch(r"D[A-Z0-9]{7,31}", binding.channel_id) is None
            or not valid_slack_id(binding.bot_user_id)
            or not valid_slack_id(binding.owner_user_id)
            or binding.bot_user_id == binding.owner_user_id
            or not isinstance(context, ssl.SSLContext)
            or not context.check_hostname
            or context.verify_mode != ssl.CERT_REQUIRED
        ):
            raise ValueError("invalid_binding")
        url = _socket_url(await http.socket_url())
        if url is None or http.binding != binding:
            raise ValueError("invalid_socket")
        connector = _NoRedirectConnect if connect is None else connect
        async with connector(
            url,
            ssl=context,
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
                seen: set[str] = set()
                async with asyncio.timeout(AUTH_DEADLINE_S):
                    await _authenticate(socket, binding, seen)
                    cutoff = await fresh_cutoff()
                    if (
                        type(cutoff) is not int
                        or cutoff <= 0
                        or cutoff % 1000
                        or http.binding != binding
                    ):
                        raise ValueError("invalid_cutoff")
                    await _discard_buffered(socket, binding, seen)
                while True:
                    if http.binding != binding:
                        raise ValueError("binding_revoked")
                    candidate = await _envelope(
                        socket, _object(await socket.recv()), binding, seen, cutoff
                    )
                    if candidate is not None:
                        yield candidate
            finally:
                notify()
    except Exception:
        pass
    finally:
        notify()
    raise SlackTransportError
