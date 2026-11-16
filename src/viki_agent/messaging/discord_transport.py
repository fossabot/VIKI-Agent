"""One fixed Discord Gateway generation with bounded intake and independent heartbeats."""

from __future__ import annotations

import asyncio
import json
import math
import random
import re
import ssl
import sys
import time
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from websockets.exceptions import ConnectionClosed

from viki_agent.api_json import unique_object
from viki_agent.messaging.discord_http import (
    DiscordHttp,
    DiscordHttpError,
    GatewayInfo,
    normalize_post,
)
from viki_agent.messaging.mattermost_http import _constant, _float
from viki_agent.messaging.mattermost_transport import (
    _QUIET_LOGGER,
    _failure_callback,
    _NoRedirectConnect,
)
from viki_agent.messaging.messaging_channel import RemoteBinding, RemotePost
from viki_agent.messaging.messaging_config import valid_discord_id

OPEN_DEADLINE_S = AUTH_DEADLINE_S = SEND_DEADLINE_S = 5.0
CLOSE_DEADLINE_S = 1.0
_FRAME_LIMIT = 128 * 1024
_DEPTH_LIMIT = 32
_SEQUENCE_LIMIT = 2**53 - 1
_DEDUP_LIMIT = 4096
_URL = "wss://gateway.discord.gg/?v=10&encoding=json"


class DiscordTransportError(RuntimeError):
    """A fixed refusal without tokens, frame contents or server close reasons."""

    code = "discord_transport_failed"

    def __init__(self) -> None:
        super().__init__(self.code)


def _integer(value: str) -> int:
    if len(value) > 17:
        raise ValueError("invalid_number")
    result = int(value)
    if abs(result) > _SEQUENCE_LIMIT:
        raise ValueError("invalid_number")
    return result


def _object(raw: object) -> dict[str, Any]:
    if (
        not isinstance(raw, str)
        or len(raw) > _FRAME_LIMIT
        or len(raw.encode("utf-8")) > _FRAME_LIMIT
    ):
        raise ValueError("invalid_frame")
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
    result = json.loads(
        raw,
        object_pairs_hook=unique_object,
        parse_constant=_constant,
        parse_float=_float,
        parse_int=_integer,
    )
    if not isinstance(result, dict):
        raise ValueError("invalid_frame")
    return result


def _encoded(value: dict[str, Any]) -> str:
    raw = json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
    if len(raw.encode("utf-8")) > 4096:
        raise ValueError("invalid_outbound")
    return raw


@dataclass
class _State:
    sequence: int | None = None
    interval: float = 0
    started: float = 0
    cutoff: int | None = None
    pending_ack: bool = False
    hello: asyncio.Event = field(default_factory=asyncio.Event)
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    sent_at: deque[float] = field(default_factory=deque)
    seen: set[str] = field(default_factory=set)


def _frame(raw: object, state: _State) -> dict[str, Any]:
    value = _object(raw)
    op = value.get("op")
    if type(op) is not int or op not in (0, 1, 7, 9, 10, 11):
        raise ValueError("invalid_opcode")
    if op == 0:
        seq, name = value.get("s"), value.get("t")
        if (
            type(seq) is not int
            or not 0 <= seq <= _SEQUENCE_LIMIT
            or (state.sequence is not None and seq <= state.sequence)
            or not isinstance(name, str)
            or re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", name) is None
            or not isinstance(value.get("d"), dict)
        ):
            raise ValueError("invalid_dispatch")
        state.sequence = seq
    elif value.get("s") is not None or value.get("t") is not None:
        raise ValueError("invalid_control")
    return value


async def _send(socket: Any, state: _State, raw: str, clock: Callable[[], float]) -> None:
    async with state.send_lock:
        now = clock()
        while state.sent_at and state.sent_at[0] <= now - 60:
            state.sent_at.popleft()
        if len(state.sent_at) >= 120:
            raise ValueError("outbound_rate_exhausted")
        state.sent_at.append(now)
        async with asyncio.timeout(SEND_DEADLINE_S):
            await socket.send(raw)


async def _heartbeat_frame(socket: Any, state: _State, clock: Callable[[], float]) -> None:
    state.pending_ack = True
    await _send(socket, state, _encoded({"op": 1, "d": state.sequence}), clock)


async def _heartbeat(
    socket: Any,
    state: _State,
    clock: Callable[[], float],
    sleep: Callable[[float], Awaitable[None]],
    jitter: Callable[[], float],
    notify: Callable[[], None],
) -> None:
    try:
        await state.hello.wait()
        fraction = jitter()
        if (
            type(fraction) not in (int, float)
            or not math.isfinite(fraction)
            or not 0 <= fraction <= 1
        ):
            raise ValueError("invalid_jitter")
        deadline = state.started + state.interval * fraction
        while True:
            while clock() < deadline:
                await sleep(deadline - clock())
            if state.pending_ack:
                raise ValueError("heartbeat_ack_missing")
            await _heartbeat_frame(socket, state, clock)
            deadline = clock() + state.interval
    finally:
        notify()


def _ready(value: dict[str, Any], binding: RemoteBinding) -> None:
    data = value["d"]
    user, application = data.get("user"), data.get("application")
    shard = data.get("shard", [0, 1])
    if (
        type(data.get("v")) is not int
        or data["v"] != 10
        or not isinstance(user, dict)
        or user.get("id") != binding.bot_user_id
        or user.get("bot") is not True
        or user.get("system", False) is not False
        or not isinstance(application, dict)
        or application.get("id") != binding.scope[0]
        or not isinstance(data.get("guilds"), list)
        or not isinstance(shard, list)
        or len(shard) != 2
        or any(type(item) is not int for item in shard)
        or shard != [0, 1]
    ):
        raise ValueError("invalid_ready")


async def _consume(
    socket: Any,
    state: _State,
    value: dict[str, Any],
    binding: RemoteBinding,
    clock: Callable[[], float],
) -> RemotePost | None:
    op = value["op"]
    if op == 1:
        requested = value.get("d")
        if requested is not None and (
            type(requested) is not int or not 0 <= requested <= _SEQUENCE_LIMIT
        ):
            raise ValueError("invalid_heartbeat_request")
        await _heartbeat_frame(socket, state, clock)
    elif op == 11:
        if not state.pending_ack or value.get("d") is not None:
            raise ValueError("invalid_heartbeat_ack")
        state.pending_ack = False
    elif op == 0:
        if value["t"] in ("READY", "RESUMED"):
            raise ValueError("unexpected_session")
        if value["t"] == "MESSAGE_CREATE":
            post = normalize_post(value["d"], binding, 0)
            if post is not None:
                if post.id in state.seen:
                    return None
                if len(state.seen) >= _DEDUP_LIMIT:
                    raise ValueError("dedup_exhausted")
                state.seen.add(post.id)
                if state.cutoff is not None and post.created_ms >= state.cutoff:
                    return post
    else:
        # Hello is accepted once; Reconnect and Invalid Session end this generation.
        raise ValueError("terminal_control")
    return None


async def _discard_buffered(
    socket: Any, state: _State, binding: RemoteBinding, clock: Callable[[], float]
) -> None:
    while True:
        try:
            # Public recv cancellation safely distinguishes buffered data from a new wait.
            async with asyncio.timeout(0):
                raw = await socket.recv()
        except TimeoutError:
            return
        await _consume(socket, state, _frame(raw, state), binding, clock)


async def _fresh(
    socket: Any,
    state: _State,
    http: DiscordHttp,
    binding: RemoteBinding,
    fresh_cutoff: Callable[[], Awaitable[int]],
    clock: Callable[[], float],
    notify: Callable[[], None],
) -> None:
    callback = asyncio.ensure_future(fresh_cutoff())
    receive: asyncio.Task[Any] | None = None
    try:
        while not callback.done():
            receive = asyncio.create_task(socket.recv())
            await asyncio.wait((callback, receive), return_when=asyncio.FIRST_COMPLETED)
            if receive.done():
                await _consume(socket, state, _frame(receive.result(), state), binding, clock)
                receive = None
            else:
                receive.cancel()
                await asyncio.gather(receive, return_exceptions=True)
                receive = None
        cutoff = callback.result()
        await _discard_buffered(socket, state, binding, clock)
        if (
            type(cutoff) is not int
            or not 0 < cutoff <= 2**63 - 1
            or cutoff % 1000
            or http.binding != binding
        ):
            raise ValueError("invalid_cutoff")
        state.cutoff = cutoff
    except BaseException:
        notify()
        raise
    finally:
        callback.cancel()
        if receive is not None:
            receive.cancel()
        await asyncio.gather(
            callback, *((receive,) if receive is not None else ()), return_exceptions=True
        )


async def _receive(
    socket: Any,
    state: _State,
    http: DiscordHttp,
    binding: RemoteBinding,
    identify: str,
    fresh_cutoff: Callable[[], Awaitable[int]],
    clock: Callable[[], float],
    messages: asyncio.Queue[RemotePost],
    notify: Callable[[], None],
) -> None:
    try:
        async with asyncio.timeout(AUTH_DEADLINE_S):
            hello = _frame(await socket.recv(), state)
            data = hello.get("d")
            if (
                hello["op"] != 10
                or not isinstance(data, dict)
                or type(data.get("heartbeat_interval")) is not int
                or not 1 <= data["heartbeat_interval"] <= 120000
            ):
                raise ValueError("invalid_hello")
            state.interval, state.started = data["heartbeat_interval"] / 1000, clock()
            state.hello.set()
            http.reserve_identify()
            await _send(socket, state, identify, clock)
            while True:
                value = _frame(await socket.recv(), state)
                if value["op"] == 0 and value["t"] == "READY":
                    _ready(value, binding)
                    break
                await _consume(socket, state, value, binding, clock)
            await _discard_buffered(socket, state, binding, clock)
            await _fresh(socket, state, http, binding, fresh_cutoff, clock, notify)
        while True:
            value = _frame(await socket.recv(), state)
            if http.binding != binding:
                raise ValueError("binding_revoked")
            post = await _consume(socket, state, value, binding, clock)
            if post is not None:
                # Overflow ends the stream rather than blocking control-frame and ACK intake.
                messages.put_nowait(post)
    except ConnectionClosed as error:
        # Discord's Gateway closes with 4004 on a rejected token; never read rcvd.reason.
        if error.rcvd is not None and error.rcvd.code == 4004:
            http.record_failure("token_rejected")
        raise
    finally:
        notify()


async def _connection(
    connector: Callable[..., Any],
    http: DiscordHttp,
    binding: RemoteBinding,
    identify: str,
    fresh_cutoff: Callable[[], Awaitable[int]],
    clock: Callable[[], float],
    sleep: Callable[[float], Awaitable[None]],
    jitter: Callable[[], float],
    messages: asyncio.Queue[RemotePost],
    notify: Callable[[], None],
) -> None:
    connection = connector(
        _URL,
        ssl=http.ssl_context,
        proxy=None,
        compression=None,
        ping_interval=None,
        user_agent_header=None,
        max_size=_FRAME_LIMIT,
        max_queue=4,
        open_timeout=OPEN_DEADLINE_S,
        close_timeout=CLOSE_DEADLINE_S,
        logger=_QUIET_LOGGER,
    )
    async with asyncio.timeout(OPEN_DEADLINE_S):
        socket = await connection.__aenter__()
    state = _State()
    receiver = asyncio.create_task(
        _receive(socket, state, http, binding, identify, fresh_cutoff, clock, messages, notify)
    )
    heartbeat = asyncio.create_task(_heartbeat(socket, state, clock, sleep, jitter, notify))
    try:
        done, _ = await asyncio.wait((receiver, heartbeat), return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
        raise ValueError("stream_ended")
    finally:
        notify()
        receiver.cancel()
        heartbeat.cancel()
        await asyncio.gather(receiver, heartbeat, return_exceptions=True)
        async with asyncio.timeout(CLOSE_DEADLINE_S):
            await connection.__aexit__(None, None, None)


def _gateway(info: GatewayInfo) -> None:
    if (
        info.url != "wss://gateway.discord.gg/"
        or type(info.shards) is not int
        or info.shards != 1
        or any(
            type(value) is not int or not minimum <= value <= maximum
            for value, minimum, maximum in (
                (info.total, 1, 1_000_000),
                (info.remaining, 1, info.total),
                (info.reset_after, 1, 86400000),
                (info.max_concurrency, 1, 1000),
            )
        )
    ):
        raise ValueError("invalid_gateway")


async def posted(
    http: DiscordHttp,
    binding: RemoteBinding,
    *,
    bot_token: str,
    fresh_cutoff: Callable[[], Awaitable[int]],
    connect: Callable[..., Any] | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    jitter: Callable[[], float] = random.random,
    on_failure: Callable[[], None] | None = None,
) -> AsyncIterator[RemotePost]:
    """Yield fresh original owner posts; confirmation remains the fixed REST adapter's job."""
    notify = _failure_callback(on_failure)
    running: asyncio.Task[None] | None = None
    try:
        context = http.ssl_context
        if (
            http.binding != binding
            or binding.provider != "discord"
            or len(binding.scope) != 1
            or not all(
                valid_discord_id(value)
                for value in (
                    *binding.scope,
                    binding.bot_user_id,
                    binding.owner_user_id,
                    binding.channel_id,
                )
            )
            or binding.bot_user_id == binding.owner_user_id
            or not isinstance(context, ssl.SSLContext)
            or not context.check_hostname
            or context.verify_mode != ssl.CERT_REQUIRED
            or not isinstance(bot_token, str)
            or not 1 <= len(bot_token) <= 4096
            or any(not 33 <= ord(char) <= 126 for char in bot_token)
        ):
            raise ValueError("invalid_binding")
        identify = _encoded(
            {
                "op": 2,
                "d": {
                    "token": bot_token,
                    "intents": 4096,
                    "compress": False,
                    "properties": {
                        "os": sys.platform,
                        "browser": "viki-agent",
                        "device": "viki-agent",
                    },
                },
            }
        )
        _gateway(await http.gateway())
        if http.binding != binding:
            raise ValueError("binding_revoked")
        messages: asyncio.Queue[RemotePost] = asyncio.Queue(maxsize=4)
        running = asyncio.create_task(
            _connection(
                _NoRedirectConnect if connect is None else connect,
                http,
                binding,
                identify,
                fresh_cutoff,
                clock,
                sleep,
                jitter,
                messages,
                notify,
            )
        )
        while True:
            receive = asyncio.create_task(messages.get())
            try:
                await asyncio.wait((running, receive), return_when=asyncio.FIRST_COMPLETED)
                if running.done():
                    running.result()
                    raise ValueError("stream_ended")
                if http.binding != binding:
                    raise ValueError("binding_revoked")
                yield receive.result()
            except BaseException:
                notify()
                raise
            finally:
                receive.cancel()
                await asyncio.gather(receive, return_exceptions=True)
    except DiscordHttpError:
        raise
    except Exception:
        pass
    finally:
        notify()
        if running is not None:
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
    raise DiscordTransportError
