"""One Discord Gateway generation against bounded synthetic sockets and timers."""

from __future__ import annotations

import asyncio
import json
import ssl
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import replace
from typing import Any, cast

import pytest
from websockets.exceptions import ConnectionClosedError
from websockets.frames import Close

from viki_agent.messaging import discord_transport
from viki_agent.messaging.discord_http import DiscordHttp, GatewayInfo
from viki_agent.messaging.discord_transport import DiscordTransportError, posted
from viki_agent.messaging.messaging_channel import RemoteBinding, RemotePost

BOT, OWNER, CHANNEL, APPLICATION, OTHER = (str(10**17 + value) for value in range(1, 6))
BINDING = RemoteBinding("discord", (APPLICATION,), BOT, OWNER, CHANNEL)
CUTOFF = 1791853324000
POST = (CUTOFF - 1420070400000) << 22
TOKEN = "synthetic-bot-token"
INFO = GatewayInfo("wss://gateway.discord.gg/", 1, 1000, 999, 14400000, 1)
HELLO = {"op": 10, "d": {"heartbeat_interval": 45000}}
READY = {
    "op": 0,
    "s": 1,
    "t": "READY",
    "d": {
        "v": 10,
        "user": {"id": BOT, "bot": True},
        "application": {"id": APPLICATION},
        "guilds": [],
        "session_id": "synthetic-session",
        "resume_gateway_url": "wss://resume.example.test/not-used",
    },
}


def event(seq: int = 2, *, number: int = 0, **changes: Any) -> dict[str, Any]:
    return {
        "op": 0,
        "s": seq,
        "t": "MESSAGE_CREATE",
        "d": {
            "id": str(POST + number),
            "channel_id": CHANNEL,
            "author": {"id": OWNER},
            "content": f"synthetic message {number}",
            "timestamp": "2026-10-13T01:02:04.000000+00:00",
            "edited_timestamp": None,
            "type": 0,
            "tts": False,
            "mention_everyone": False,
            "mentions": [],
            "mention_roles": [],
            "attachments": [],
            "embeds": [],
            "pinned": False,
        },
        **changes,
    }


class Scheduler:
    def __init__(self) -> None:
        self.now = 100.0
        self.sleepers: dict[asyncio.Future[None], float] = {}
        self.scheduled = asyncio.Event()

    def clock(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        future = asyncio.get_running_loop().create_future()
        self.sleepers[future] = self.now + delay
        self.scheduled.set()
        try:
            await future
        finally:
            self.sleepers.pop(future, None)

    def advance(self, seconds: float) -> None:
        self.now += seconds
        self.scheduled.clear()
        for future, deadline in list(self.sleepers.items()):
            if deadline <= self.now and not future.done():
                future.set_result(None)


class Socket:
    def __init__(self, frames: list[Any]) -> None:
        self.frames: asyncio.Queue[str | bytes | Exception] = asyncio.Queue()
        self.waiting = asyncio.Event()
        self.closed = asyncio.Event()
        self.sent_event = asyncio.Event()
        self.send_started = asyncio.Event()
        self.sent: list[dict[str, Any]] = []
        self.received: list[str | bytes] = []
        self.receive_count = 0
        self.send_gate: asyncio.Event | None = None
        for frame in frames:
            self.put(frame)

    def put(self, frame: Any) -> None:
        self.waiting.clear()
        self.frames.put_nowait(json.dumps(frame) if isinstance(frame, dict) else frame)

    async def recv(self) -> str | bytes:
        if self.frames.empty():
            self.waiting.set()
        self.receive_count += 1
        try:
            frame = await self.frames.get()
            if isinstance(frame, Exception):
                raise frame
            self.received.append(frame)
            return frame
        finally:
            self.receive_count -= 1

    async def send(self, raw: str) -> None:
        assert len(raw.encode("utf-8")) <= 4096
        self.send_started.set()
        if self.send_gate is not None:
            await self.send_gate.wait()
        self.sent.append(json.loads(raw))
        self.sent_event.set()


class Connector:
    def __init__(self, socket: Socket) -> None:
        self.socket = socket
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.enter_gate: asyncio.Event | None = None

    def __call__(self, url: str, **options: Any) -> Connector:
        self.calls.append((url, options))
        return self

    async def __aenter__(self) -> Socket:
        if self.enter_gate is not None:
            await self.enter_gate.wait()
        return self.socket

    async def __aexit__(self, *_: Any) -> None:
        self.socket.closed.set()


class Http:
    def __init__(self) -> None:
        self.binding: RemoteBinding | None = BINDING
        self.ssl_context = ssl.create_default_context()
        self.info = INFO
        self.gateway_calls = 0
        self.identify_calls = 0
        self.stop_reason: str | None = None

    async def gateway(self) -> GatewayInfo:
        self.gateway_calls += 1
        return self.info

    def reserve_identify(self) -> None:
        self.identify_calls += 1

    def record_failure(self, reason: str) -> None:
        if self.stop_reason is None:
            self.stop_reason = reason


class Rig:
    def __init__(self, frames: list[Any] | None = None) -> None:
        self.http = Http()
        self.socket = Socket([HELLO, READY] if frames is None else frames)
        self.connector = Connector(self.socket)
        self.timer = Scheduler()
        self.fresh_called = asyncio.Event()
        self.fresh_calls = 0

    async def fresh(self) -> int:
        self.fresh_calls += 1
        self.fresh_called.set()
        return CUTOFF

    def stream(
        self,
        *,
        fresh: Callable[[], Awaitable[int]] | None = None,
        token: str = TOKEN,
        jitter: Callable[[], float] = lambda: 0.5,
    ) -> AsyncGenerator[RemotePost]:
        return cast(
            AsyncGenerator[RemotePost],
            posted(
                self.http,  # type: ignore[arg-type]
                BINDING,
                bot_token=token,
                fresh_cutoff=fresh or self.fresh,
                connect=self.connector,
                clock=self.timer.clock,
                sleep=self.timer.sleep,
                jitter=jitter,
            ),
        )


async def waiting(rig: Rig) -> None:
    await asyncio.wait_for(rig.fresh_called.wait(), 0.05)
    await asyncio.wait_for(rig.socket.waiting.wait(), 0.05)
    # Let the callback settlement, cancelled public recv and final buffered drain finish.
    # These are event-loop turns, with no elapsed-time sleep or provider work.
    for _ in range(12):
        turn = asyncio.get_running_loop().create_future()
        asyncio.get_running_loop().call_soon(turn.set_result, None)
        await turn


class HeldExitConnector(Connector):
    def __init__(self, socket: Socket) -> None:
        super().__init__(socket)
        self.closing, self.release = asyncio.Event(), asyncio.Event()

    async def __aexit__(self, *_: Any) -> None:
        self.closing.set()
        await self.release.wait()
        self.socket.closed.set()


@pytest.mark.parametrize("callback_error", [None, RuntimeError, asyncio.CancelledError])
async def test_terminal_notification_precedes_held_socket_exit(
    callback_error: type[BaseException] | None,
) -> None:
    rig = Rig([{**HELLO, "d": {"heartbeat_interval": 0}}])
    rig.connector = HeldExitConnector(rig.socket)
    notices: list[str] = []

    def failed() -> None:
        notices.append("failed")
        if callback_error is not None:
            raise callback_error("synthetic private callback")

    pending = asyncio.ensure_future(
        anext(
            posted(
                cast(DiscordHttp, rig.http),
                BINDING,
                bot_token=TOKEN,
                fresh_cutoff=rig.fresh,
                connect=rig.connector,
                clock=rig.timer.clock,
                sleep=rig.timer.sleep,
                jitter=lambda: 0.5,
                on_failure=failed,
            )
        )
    )
    try:
        await asyncio.wait_for(rig.connector.closing.wait(), 0.05)
        assert notices == ["failed"] and not pending.done() and not rig.socket.closed.is_set()
    finally:
        rig.connector.release.set()
    with pytest.raises(DiscordTransportError):
        await pending
    assert notices == ["failed"] and rig.socket.closed.is_set() and not rig.timer.sleepers


@pytest.mark.parametrize(
    "terminal",
    ["reconnect", "invalid_session", "malformed", "eof", "ack", "cancel", "ready_identity"],
)
async def test_receiver_terminal_and_cancel_notification_precedes_held_exit(terminal: str) -> None:
    rig = Rig()
    if terminal == "ready_identity":
        rig.socket = Socket([HELLO, {**READY, "d": {**READY["d"], "application": {"id": OTHER}}}])
    connector = HeldExitConnector(rig.socket)
    notices: list[str] = []
    iterator = posted(
        cast(DiscordHttp, rig.http),
        BINDING,
        bot_token=TOKEN,
        fresh_cutoff=rig.fresh,
        connect=connector,
        clock=rig.timer.clock,
        sleep=rig.timer.sleep,
        jitter=lambda: 0.5,
        on_failure=lambda: notices.append("failed"),
    )
    pending = asyncio.ensure_future(anext(iterator))
    try:
        if terminal != "ready_identity":
            await waiting(rig)
            assert notices == []
            if terminal == "cancel":
                pending.cancel()
            else:
                frames = {
                    "reconnect": {"op": 7},
                    "invalid_session": {"op": 9, "d": False},
                    "malformed": "{bad",
                    "eof": RuntimeError("private EOF"),
                    "ack": {"op": 11},
                }
                rig.socket.put(frames[terminal])
        await asyncio.wait_for(connector.closing.wait(), 0.05)
        assert notices == ["failed"] and not pending.done() and not rig.socket.closed.is_set()
    finally:
        connector.release.set()
    with pytest.raises(asyncio.CancelledError if terminal == "cancel" else DiscordTransportError):
        await pending
    assert notices == ["failed"] and rig.socket.closed.is_set() and not rig.timer.sleepers


@pytest.mark.parametrize("blocked", ["heartbeat", "receiver", "cutoff"])
async def test_background_detection_notifies_before_child_task_drain(blocked: str) -> None:
    rig = Rig()
    connector = HeldExitConnector(rig.socket)
    drain_started, drain_release = asyncio.Event(), asyncio.Event()
    notices: list[str] = []

    async def sleep(delay: float) -> None:
        try:
            await rig.timer.sleep(delay)
        finally:
            if blocked == "heartbeat":
                drain_started.set()
                await drain_release.wait()

    async def fresh() -> int:
        rig.fresh_called.set()
        if blocked != "cutoff":
            return CUTOFF
        try:
            await asyncio.Event().wait()
        finally:
            drain_started.set()
            await drain_release.wait()
        return CUTOFF

    iterator = posted(
        cast(DiscordHttp, rig.http),
        BINDING,
        bot_token=TOKEN,
        fresh_cutoff=fresh,
        connect=connector,
        clock=rig.timer.clock,
        sleep=sleep,
        jitter=lambda: 0.5,
        on_failure=lambda: notices.append("failed"),
    )
    pending = asyncio.ensure_future(anext(iterator))
    try:
        await waiting(rig)
        if blocked == "receiver":
            recv = rig.socket.recv

            async def held_recv() -> str | bytes:
                try:
                    return await recv()
                finally:
                    drain_started.set()
                    await drain_release.wait()

            rig.socket.recv = held_recv
            rig.socket.put(event(2))
            assert await asyncio.wait_for(pending, 0.05)
            rig.socket.sent_event.clear()
            rig.timer.advance(22.5)
            await asyncio.wait_for(rig.socket.sent_event.wait(), 0.05)
            await asyncio.wait_for(rig.timer.scheduled.wait(), 0.05)
            rig.timer.advance(45)
        else:
            if blocked == "heartbeat":
                rig.socket.put(event(2))
                assert await asyncio.wait_for(pending, 0.05)
            rig.socket.put({"op": 7})
        await asyncio.wait_for(drain_started.wait(), 0.05)
        assert notices == ["failed"] and not connector.closing.is_set()
        assert not rig.socket.closed.is_set()
    finally:
        drain_release.set()
        connector.release.set()
    await asyncio.wait_for(rig.socket.closed.wait(), 0.05)
    if not pending.done():
        with pytest.raises(DiscordTransportError):
            await pending
    else:
        with pytest.raises(DiscordTransportError):
            await anext(iterator)
    assert notices == ["failed"] and not rig.timer.sleepers
    await iterator.aclose()  # type: ignore[attr-defined]


async def test_invalid_preconnect_authority_notifies_once() -> None:
    rig, notices = Rig(), []
    rig.http.binding = None
    with pytest.raises(DiscordTransportError):
        await anext(
            posted(
                cast(DiscordHttp, rig.http),
                BINDING,
                bot_token=TOKEN,
                fresh_cutoff=rig.fresh,
                connect=rig.connector,
                on_failure=lambda: notices.append("failed"),
            )
        )
    assert notices == ["failed"] and rig.connector.calls == []


async def test_ready_bot_and_application_gate_identify_and_startup_drain() -> None:
    rig = Rig([HELLO, event(1), {**READY, "s": 2}, event(3, number=1)])
    iterator = rig.stream()
    pending = asyncio.create_task(anext(iterator))
    await waiting(rig)
    assert not pending.done()
    assert rig.fresh_calls == 1
    identify = rig.socket.sent[0]
    assert identify["op"] == 2
    assert identify["d"]["token"] == TOKEN
    assert identify["d"]["intents"] == 4096
    assert rig.http.identify_calls == 1
    assert identify["d"]["compress"] is False
    assert identify["d"]["properties"]["browser"] == "viki-agent"
    assert "presence" not in identify["d"] and "shard" not in identify["d"]
    rig.socket.put(event(4, number=1))
    rig.socket.put(event(5, number=2))
    result = await asyncio.wait_for(pending, 0.05)
    assert result.id == str(POST + 2)
    uri, options = rig.connector.calls[0]
    assert uri == "wss://gateway.discord.gg/?v=10&encoding=json"
    assert options["ssl"] is rig.http.ssl_context
    assert options["proxy"] is None and options["compression"] is None
    assert options["ping_interval"] is None
    assert options["max_size"] == 128 * 1024 and options["max_queue"] == 4
    assert options["open_timeout"] == 5 and options["close_timeout"] <= 5
    assert options["user_agent_header"] is None and "additional_headers" not in options
    assert options["logger"].disabled and not options["logger"].propagate
    await iterator.aclose()
    assert rig.socket.closed.is_set() and not rig.timer.sleepers and rig.socket.receive_count == 0


@pytest.mark.parametrize("cancel_send", [False, True])
async def test_identify_start_window_survives_closed_socket_and_fresh_runtime_generation(
    cancel_send: bool,
) -> None:
    import httpx2

    from tests.messaging.test_discord_http import (
        CONFIG,
        Clock,
        Server,
        runtime_reenable_after_public_deadline,
    )
    from viki_agent.messaging.discord_http import DiscordHttp

    clock, server = Clock(), Server()

    def make_http() -> DiscordHttp:
        return DiscordHttp(
            CONFIG, transport=httpx2.MockTransport(server), clock=clock, sleep=clock.sleep
        )

    async def establish(client: DiscordHttp) -> float:
        rig = Rig()
        rig.http = cast(Any, client)

        async def fresh() -> int:
            value = await client.fresh_cutoff()
            rig.fresh_called.set()
            return value

        iterator = cast(
            AsyncGenerator[RemotePost],
            posted(
                client,
                BINDING,
                bot_token=CONFIG.bot_token,
                fresh_cutoff=fresh,
                connect=rig.connector,
                clock=clock,
                sleep=rig.timer.sleep,
                jitter=lambda: 0.5,
            ),
        )
        if cancel_send:
            rig.socket.send_gate = asyncio.Event()
        pending = asyncio.create_task(anext(iterator))
        try:
            if cancel_send:
                await asyncio.wait_for(rig.socket.send_started.wait(), 0.05)
                pending.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await pending
            else:
                await waiting(rig)
                rig.socket.put(event(2))
                assert (await asyncio.wait_for(pending, 0.05)).id == str(POST)
                assert rig.socket.sent[0]["op"] == 2
            deadline = client.cooldown_until
            assert deadline > clock.now + 4.9
        finally:
            await iterator.aclose()
        assert rig.socket.closed.is_set() and not rig.timer.sleepers
        return deadline

    await runtime_reenable_after_public_deadline("discord", make_http, clock, establish)


@pytest.mark.parametrize(
    "change",
    [
        {"v": 9},
        {"v": True},
        {"user": {"id": OTHER, "bot": True}},
        {"user": {"id": BOT}},
        {"user": {"id": BOT, "bot": 1}},
        {"application": {"id": BOT}},
        {"application": {"id": OTHER}},
        {"application": None},
        {"shard": [0, 2]},
        {"shard": [False, 1]},
    ],
)
async def test_ready_mismatches_never_admit_owner_message(change: dict[str, Any]) -> None:
    rig = Rig([HELLO, {**READY, "d": {**READY["d"], **change}}, event()])
    with pytest.raises(DiscordTransportError, match="^discord_transport_failed$"):
        await anext(rig.stream())
    assert rig.fresh_calls == 0 and rig.socket.closed.is_set()


@pytest.mark.parametrize(
    "hello",
    [
        {"op": 10, "d": {}},
        {"op": 10, "d": {"heartbeat_interval": True}},
        {"op": 10, "d": {"heartbeat_interval": 0}},
        {"op": 10, "d": {"heartbeat_interval": 999999999}},
        READY,
        {"op": 7, "d": None},
    ],
)
async def test_hello_must_precede_identify(hello: dict[str, Any]) -> None:
    rig = Rig([hello])
    with pytest.raises(DiscordTransportError):
        await anext(rig.stream())
    assert not rig.socket.sent and rig.fresh_calls == 0 and rig.socket.closed.is_set()


@pytest.mark.parametrize(
    "info",
    [
        replace(INFO, url="wss://evil.example.test/"),
        replace(INFO, shards=2),
        replace(INFO, remaining=0),
        replace(INFO, max_concurrency=0),
        replace(INFO, remaining=1001),
    ],
)
async def test_exhausted_or_sharded_gateway_never_opens(info: GatewayInfo) -> None:
    rig = Rig()
    rig.http.info = info
    with pytest.raises(DiscordTransportError):
        await anext(rig.stream())
    assert not rig.connector.calls


@pytest.mark.parametrize("token", ["", "bad\r\nsecret", "x" * 4096, "x" * 4097])
async def test_identify_bytes_and_token_shape_are_checked_before_connect(token: str) -> None:
    rig = Rig()
    with pytest.raises(DiscordTransportError):
        await anext(rig.stream(token=token))
    assert not rig.connector.calls


@pytest.mark.parametrize("sequence", [1, 0, -1, True, 1.5, "2", 2**53])
async def test_dispatch_sequence_is_strictly_monotonic(sequence: Any) -> None:
    rig = Rig()
    iterator = rig.stream()
    pending = asyncio.create_task(anext(iterator))
    await waiting(rig)
    rig.socket.put(event(sequence))
    with pytest.raises(DiscordTransportError):
        await asyncio.wait_for(pending, 0.05)
    assert rig.socket.closed.is_set()


async def test_ignored_dispatches_advance_sequence_and_only_original_creates_enter() -> None:
    rig = Rig()
    iterator = rig.stream()
    pending = asyncio.create_task(anext(iterator))
    await waiting(rig)
    rig.socket.put(event(2, t="MESSAGE_UPDATE"))
    rig.socket.put(event(3, d={**event()["d"], "guild_id": OTHER}))
    rig.socket.put(event(4, d={**event()["d"], "author": {"id": OTHER}}))
    rig.socket.put(event(5, number=1))
    result = await asyncio.wait_for(pending, 0.05)
    assert result.id == str(POST + 1)
    await iterator.aclose()


async def test_heartbeat_jitter_ack_and_latest_dispatch_sequence() -> None:
    rig = Rig()
    iterator = rig.stream()
    pending = asyncio.create_task(anext(iterator))
    await waiting(rig)
    await asyncio.wait_for(rig.timer.scheduled.wait(), 0.05)
    assert rig.socket.sent == [rig.socket.sent[0]]
    rig.socket.sent_event.clear()
    rig.timer.advance(22.5)
    await asyncio.wait_for(rig.socket.sent_event.wait(), 0.05)
    assert rig.socket.sent[-1] == {"op": 1, "d": 1}
    rig.socket.put({"op": 11})
    rig.socket.put(event(2, number=1))
    assert (await asyncio.wait_for(pending, 0.05)).id == str(POST + 1)
    rig.socket.sent_event.clear()
    rig.timer.advance(45)
    await asyncio.wait_for(rig.socket.sent_event.wait(), 0.05)
    assert rig.socket.sent[-1] == {"op": 1, "d": 2}
    await iterator.aclose()
    assert not rig.timer.sleepers


async def test_heartbeat_request_and_ack_continue_while_cutoff_rest_waits() -> None:
    rig = Rig()
    entered, release = asyncio.Event(), asyncio.Event()

    async def fresh() -> int:
        entered.set()
        await release.wait()
        rig.fresh_called.set()
        return CUTOFF

    iterator = rig.stream(fresh=fresh)
    pending = asyncio.create_task(anext(iterator))
    await asyncio.wait_for(entered.wait(), 0.05)
    rig.socket.sent_event.clear()
    rig.socket.put({"op": 1, "d": None})
    await asyncio.wait_for(rig.socket.sent_event.wait(), 0.05)
    assert rig.socket.sent[-1] == {"op": 1, "d": 1}
    rig.socket.put({"op": 11})
    rig.socket.put(event(2, number=1))
    await asyncio.wait_for(rig.socket.waiting.wait(), 0.05)
    release.set()
    await waiting(rig)
    rig.socket.put(event(3, number=1))
    rig.socket.put(event(4, number=2))
    assert (await asyncio.wait_for(pending, 0.05)).id == str(POST + 2)
    await iterator.aclose()


async def test_ack_loss_stops_even_when_the_consumer_is_paused() -> None:
    rig = Rig()
    iterator = rig.stream()
    pending = asyncio.create_task(anext(iterator))
    await waiting(rig)
    rig.socket.put(event(2))
    await asyncio.wait_for(pending, 0.05)
    await asyncio.wait_for(rig.timer.scheduled.wait(), 0.05)
    rig.socket.sent_event.clear()
    rig.timer.advance(22.5)
    await asyncio.wait_for(rig.socket.sent_event.wait(), 0.05)
    await asyncio.wait_for(rig.timer.scheduled.wait(), 0.05)
    rig.timer.advance(45)
    await asyncio.wait_for(rig.socket.closed.wait(), 0.05)
    with pytest.raises(DiscordTransportError):
        await anext(iterator)
    assert len(rig.connector.calls) == 1 and not rig.timer.sleepers


async def test_acks_are_processed_while_the_consumer_pauses_and_overflow_closes() -> None:
    rig = Rig()
    iterator = rig.stream()
    pending = asyncio.create_task(anext(iterator))
    await waiting(rig)
    rig.socket.put(event(2))
    await asyncio.wait_for(pending, 0.05)
    rig.socket.sent_event.clear()
    rig.socket.put({"op": 1, "d": None})
    await asyncio.wait_for(rig.socket.sent_event.wait(), 0.05)
    rig.socket.put({"op": 11})
    await asyncio.wait_for(rig.socket.waiting.wait(), 0.05)
    assert not rig.socket.closed.is_set()
    for number in range(1, 6):
        rig.socket.put(event(number + 2, number=number))
    await asyncio.wait_for(rig.socket.closed.wait(), 0.05)
    with pytest.raises(DiscordTransportError):
        await anext(iterator)
    assert rig.socket.receive_count == 0 and not rig.timer.sleepers


@pytest.mark.parametrize(
    "frame",
    [
        {"op": 7, "d": None},
        {"op": 9, "d": False},
        {"op": 9, "d": True},
        {"op": 10, "d": {"heartbeat_interval": 45000}},
        {"op": 11},
        {"op": 99},
        {"op": 0, "t": "RESUMED", "s": 2, "d": {}},
        {"op": 0, "t": "READY", "s": 2, "d": READY["d"]},
        RuntimeError("synthetic private close reason"),
    ],
)
async def test_terminal_frames_and_eof_stop_without_resume_or_reconnect(frame: Any) -> None:
    rig = Rig()
    pending = asyncio.create_task(anext(rig.stream()))
    await waiting(rig)
    rig.socket.put(frame)
    with pytest.raises(DiscordTransportError, match="^discord_transport_failed$"):
        await asyncio.wait_for(pending, 0.05)
    assert rig.socket.closed.is_set() and len(rig.connector.calls) == 1
    assert all(payload["op"] in (1, 2) for payload in rig.socket.sent)


@pytest.mark.parametrize("cutoff", [0, -1000, 1, True, "1791853324000", None])
async def test_cutoff_is_required_after_ready_before_intake(cutoff: Any) -> None:
    rig = Rig()

    async def fresh() -> int:
        return cutoff

    with pytest.raises(DiscordTransportError):
        await anext(rig.stream(fresh=fresh))
    assert rig.socket.closed.is_set()


@pytest.mark.parametrize(
    "raw",
    [
        b"{}",
        "[]",
        '{"op":10,"op":10}',
        '{"op":NaN}',
        '{"op":1e999}',
        "\ud800",
        '{"d":' + "[" * 33 + "0" + "]" * 33 + "}",
        '{"d":"' + "x" * (128 * 1024) + '"}',
        '{"s":' + "9" * 10000 + "}",
    ],
    ids=(
        "binary",
        "array",
        "duplicate",
        "nan",
        "infinite",
        "surrogate",
        "depth",
        "raw-size",
        "integer-size",
    ),
)
async def test_raw_json_size_depth_and_numbers_fail_before_authentication(raw: Any) -> None:
    rig = Rig([raw])
    with pytest.raises(DiscordTransportError):
        await anext(rig.stream())
    assert not rig.socket.sent and rig.socket.closed.is_set()


async def test_invalid_tls_and_changed_http_binding_never_connect() -> None:
    rig = Rig()
    rig.http.ssl_context.check_hostname = False
    with pytest.raises(DiscordTransportError):
        await anext(rig.stream())
    assert not rig.connector.calls
    rig = Rig()
    rig.http.binding = replace(BINDING, bot_user_id=OTHER)
    with pytest.raises(DiscordTransportError):
        await anext(rig.stream())
    assert not rig.connector.calls


async def test_cutoff_callback_cannot_adopt_a_revoked_binding() -> None:
    rig = Rig()

    async def fresh() -> int:
        rig.http.binding = None
        return CUTOFF

    with pytest.raises(DiscordTransportError):
        await anext(rig.stream(fresh=fresh))
    assert rig.socket.closed.is_set()


@pytest.mark.parametrize("point", ["open", "hello", "ready", "send"])
async def test_open_authentication_and_send_have_absolute_deadlines(
    monkeypatch: pytest.MonkeyPatch, point: str
) -> None:
    monkeypatch.setattr(discord_transport, "OPEN_DEADLINE_S", 0.01)
    monkeypatch.setattr(discord_transport, "AUTH_DEADLINE_S", 0.01)
    monkeypatch.setattr(discord_transport, "SEND_DEADLINE_S", 0.01)
    rig = Rig([] if point == "hello" else [HELLO] if point == "ready" else None)
    if point == "open":
        rig.connector.enter_gate = asyncio.Event()
    if point == "send":
        rig.socket.send_gate = asyncio.Event()
    with pytest.raises(DiscordTransportError):
        await asyncio.wait_for(anext(rig.stream()), 0.05)
    assert not rig.timer.sleepers and rig.socket.receive_count == 0


async def test_cancellation_joins_receiver_heartbeat_and_cutoff_tasks() -> None:
    rig = Rig()
    blocked = asyncio.Event()

    async def fresh() -> int:
        rig.fresh_called.set()
        await blocked.wait()
        return CUTOFF

    iterator = rig.stream(fresh=fresh)
    pending = asyncio.create_task(anext(iterator))
    await waiting(rig)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert rig.socket.closed.is_set() and not rig.timer.sleepers and rig.socket.receive_count == 0


def test_default_connector_cannot_redirect_to_server_authority() -> None:
    connector = discord_transport._NoRedirectConnect("wss://gateway.discord.gg/?v=10&encoding=json")
    error = connector.process_redirect(RuntimeError("synthetic private redirect"))
    assert "redirect" not in str(error) and TOKEN not in str(error)


@pytest.mark.parametrize(("code", "reason"), [(4004, "token_rejected"), (4000, None)])
async def test_gateway_4004_reads_token_rejected(code: int, reason: str | None) -> None:
    rig = Rig([HELLO, ConnectionClosedError(Close(code, "synthetic private close reason"), None)])
    seen: list[str | None] = []
    with pytest.raises(DiscordTransportError, match="^discord_transport_failed$"):
        await anext(
            posted(
                cast(DiscordHttp, rig.http),
                BINDING,
                bot_token=TOKEN,
                fresh_cutoff=rig.fresh,
                connect=rig.connector,
                clock=rig.timer.clock,
                sleep=rig.timer.sleep,
                jitter=lambda: 0.5,
                on_failure=lambda: seen.append(rig.http.stop_reason),
            )
        )
    assert seen and seen[0] == reason
    assert rig.http.stop_reason == reason
