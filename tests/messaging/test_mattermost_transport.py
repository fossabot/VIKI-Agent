"""Authenticated event intake using synthetic frames and temporary-port handshakes."""

from __future__ import annotations

import asyncio
import json
import logging
import logging.config
import ssl
from collections.abc import AsyncGenerator, Awaitable, Callable
from typing import Any, cast

import pytest
from websockets.asyncio.client import connect
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosedOK, InvalidStatus
from websockets.frames import Close
from websockets.http11 import Response

from viki_agent.messaging import mattermost_transport
from viki_agent.messaging.mattermost_config import MattermostConfig
from viki_agent.messaging.mattermost_http import Binding, MattermostHttpError
from viki_agent.messaging.mattermost_transport import MattermostTransportError, posted

BOT, OWNER, CHANNEL, POST, OTHER = (char * 26 for char in "bocpx")
TOKEN = "synthetic-secret-token"
CONFIG = MattermostConfig("https://chat.example.test/deployment", OWNER, TOKEN)
BINDING = Binding(BOT, OWNER, CHANNEL)
CUTOFF = 1791853324000
TLS = ssl.create_default_context()
OK = {"status": "OK", "seq_reply": 1, "data": {}}
HELLO = {"event": "hello", "data": {}, "broadcast": {"user_id": BOT}, "seq": 0}


def post(**changes: Any) -> dict[str, Any]:
    return {
        "id": POST,
        "user_id": OWNER,
        "channel_id": CHANNEL,
        "root_id": "",
        "original_id": "",
        "type": "",
        "props": {},
        "file_ids": [],
        "message": "literal synthetic message",
        "create_at": CUTOFF,
        "edit_at": 0,
        "delete_at": 0,
        **changes,
    }


def event(value: dict[str, Any] | None = None, **changes: Any) -> dict[str, Any]:
    return {
        "event": "posted",
        "data": {"post": json.dumps(value if value is not None else post())},
        "broadcast": {"channel_id": CHANNEL, "user_id": ""},
        "seq": 1,
        **changes,
    }


class Socket:
    def __init__(self, frames: list[Any]) -> None:
        self.frames: asyncio.Queue[str | bytes | Exception] = asyncio.Queue()
        for frame in frames:
            self.put(frame)
        self.waiting = asyncio.Event()
        self.send_started = asyncio.Event()
        self.send_gate: asyncio.Event | None = None
        self.sent: list[str] = []
        self.received: list[str | bytes] = []
        self.closed = False

    def put(self, frame: Any) -> None:
        self.frames.put_nowait(json.dumps(frame) if isinstance(frame, dict) else frame)

    async def send(self, value: str) -> None:
        self.send_started.set()
        if self.send_gate is not None:
            await self.send_gate.wait()
        self.sent.append(value)

    async def recv(self) -> str | bytes:
        if self.frames.empty():
            self.waiting.set()
        frame = await self.frames.get()
        if isinstance(frame, Exception):
            raise frame
        self.received.append(frame)
        return frame


class Connector:
    def __init__(self, socket: Socket) -> None:
        self.socket = socket
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, uri: str, **kwargs: Any) -> Connector:
        self.calls.append((uri, kwargs))
        return self

    async def __aenter__(self) -> Socket:
        return self.socket

    async def __aexit__(self, *_: Any) -> None:
        self.socket.closed = True
        self.socket.waiting.set()


class HeldExitConnector(Connector):
    def __init__(self, socket: Socket) -> None:
        super().__init__(socket)
        self.closing, self.release = asyncio.Event(), asyncio.Event()
        self.exit_error: BaseException | None = None

    async def __aexit__(self, *_: Any) -> None:
        self.closing.set()
        await self.release.wait()
        self.socket.closed = True
        if self.exit_error is not None:
            raise self.exit_error


@pytest.fixture
def install(monkeypatch: pytest.MonkeyPatch) -> Callable[[list[Any]], tuple[Socket, Connector]]:
    def factory(frames: list[Any]) -> tuple[Socket, Connector]:
        socket = Socket(frames)
        connector = Connector(socket)
        monkeypatch.setattr(mattermost_transport, "_NoRedirectConnect", connector)
        return socket, connector

    return factory


async def cutoff() -> int:
    return CUTOFF


def stream(
    callback: Callable[[], Awaitable[int]] = cutoff,
) -> AsyncGenerator[dict[str, Any]]:
    return cast(
        AsyncGenerator[dict[str, Any]],
        posted(CONFIG, BINDING, fresh_cutoff=callback, ssl_context=TLS),
    )


@pytest.mark.parametrize("auth", [[OK, HELLO], [HELLO, OK]])
async def test_auth_before_events_and_startup_queue_discard(install: Any, auth: list[Any]) -> None:
    socket, connector = install([event(), auth[0], event(), auth[1], event()])
    calls = 0

    async def fresh() -> int:
        nonlocal calls
        assert len(socket.sent) == 1
        assert all(json.dumps(frame) in socket.received for frame in auth)
        calls += 1
        return CUTOFF

    iterator = stream(fresh)
    task = asyncio.create_task(anext(iterator))
    await asyncio.wait_for(socket.waiting.wait(), 0.05)
    assert not task.done(), task.exception()
    socket.put(event(post(create_at=CUTOFF - 1)))
    socket.put(event(post(message="accepted after startup")))
    result = await asyncio.wait_for(task, 0.05)
    assert result == {**post(message="accepted after startup"), "_fresh_cutoff_ms": CUTOFF}
    assert calls == 1
    assert json.loads(socket.sent[0]) == {
        "seq": 1,
        "action": "authentication_challenge",
        "data": {"token": TOKEN},
    }
    uri, options = connector.calls[0]
    assert uri == "wss://chat.example.test/deployment/api/v4/websocket"
    assert options["ssl"] is TLS
    assert options["proxy"] is None
    assert options["compression"] is None
    assert options["max_size"] == 128 * 1024
    assert options["max_queue"] == 4
    assert 0 < options["open_timeout"] <= 5
    assert 0 < options["close_timeout"] <= 5
    assert options["logger"].disabled and not options["logger"].propagate
    assert "additional_headers" not in options
    await iterator.aclose()
    assert socket.closed


async def test_events_queued_during_cutoff_are_discarded(install: Any) -> None:
    socket, _ = install([HELLO, OK])
    started, release = asyncio.Event(), asyncio.Event()

    async def fresh() -> int:
        started.set()
        await release.wait()
        return CUTOFF

    iterator = stream(fresh)
    task = asyncio.create_task(anext(iterator))
    await asyncio.wait_for(started.wait(), 0.05)
    socket.put(event(post(message="queued future timestamp", create_at=CUTOFF + 999999)))
    release.set()
    await asyncio.wait_for(socket.waiting.wait(), 0.05)
    socket.put(event(post(message="live")))
    assert (await asyncio.wait_for(task, 0.05))["message"] == "live"
    await iterator.aclose()
    assert socket.closed


@pytest.mark.parametrize(
    "bad",
    [
        {"status": "FAIL", "seq_reply": 1},
        {"status": "OK", "seq_reply": 2},
        {"status": "OK", "seq_reply": True},
        {"event": "hello", "data": {}, "broadcast": {"user_id": OTHER}},
        {"event": "hello", "data": {}, "broadcast": {}},
    ],
    ids=["bad-status", "wrong-reply", "boolean-reply", "wrong-bot", "missing-bot"],
)
async def test_auth_mismatch_closes_without_cutoff(install: Any, bad: Any) -> None:
    socket, _ = install([bad])

    async def forbidden() -> int:
        pytest.fail("cutoff called before authenticated bot hello and matching OK")

    with pytest.raises(MattermostTransportError, match="^mattermost_transport_failed$"):
        await anext(stream(forbidden))
    assert socket.closed


@pytest.mark.parametrize(
    "bad",
    [
        "not JSON",
        "[]",
        '{"event":"hello","event":"posted"}',
        '{"value":NaN}',
        '{"value":Infinity}',
        b'{"event":"hello"}',
        '{"nested":' + "[" * 33 + "0" + "]" * 33 + "}",
        "[" * 1200 + "0" + "]" * 1200,
        '"' + "x" * (128 * 1024) + '"',
        '"' + "\ud800" + '"',
    ],
    ids=[
        "invalid",
        "array",
        "duplicate",
        "nan",
        "infinity",
        "binary",
        "depth",
        "recursion",
        "oversize",
        "surrogate",
    ],
)
async def test_malformed_duplicate_deep_binary_and_oversize_close(install: Any, bad: Any) -> None:
    socket, _ = install([bad])
    with pytest.raises(MattermostTransportError) as error:
        await anext(stream())
    assert str(error.value) == "mattermost_transport_failed"
    assert error.value.code == "mattermost_transport_failed"
    assert error.value.__context__ is None
    assert socket.closed


@pytest.mark.parametrize(
    "changes",
    [
        {"id": "invalid"},
        {"user_id": BOT},
        {"user_id": OTHER},
        {"channel_id": OTHER},
        {"create_at": CUTOFF - 1},
        {"create_at": True},
        {"create_at": float(CUTOFF)},
        {"edit_at": 1},
        {"edit_at": False},
        {"delete_at": 1},
        {"delete_at": False},
        {"root_id": "invalid"},
        {"original_id": OTHER},
        {"type": "system_generic"},
        {"type": None},
        {"props": None},
        {"file_ids": [OTHER]},
        {"file_ids": ""},
        {"message": "x" * (8 * 1024 + 1)},
        {"message": "\u2603" * 2731},
        {"message": 1},
        {"props": {"attachments": []}},
        {"props": {"from_webhook": "false"}},
        {"props": {"override_username": "literal"}},
        {"props": {"override_icon_url": "literal"}},
        {"props": {"override_icon_emoji": "literal"}},
        {"props": {"webhook_display_name": "literal"}},
        {"props": {"from_bot": True}},
        {"metadata": {"files": [{"id": OTHER}]}},
    ],
)
async def test_invalid_or_unbound_posts_are_ignored(install: Any, changes: Any) -> None:
    socket, _ = install([HELLO, OK])
    iterator = stream()
    task = asyncio.create_task(anext(iterator))
    await asyncio.wait_for(socket.waiting.wait(), 0.05)
    socket.put(event(post(**changes)))
    socket.put(event(post(message="accepted")))
    assert (await asyncio.wait_for(task, 0.05))["message"] == "accepted"
    await iterator.aclose()
    assert socket.closed


def reaction(**changes: Any) -> dict[str, Any]:
    return {
        "user_id": OWNER,
        "post_id": POST,
        "emoji_name": "+1",
        "create_at": CUTOFF,
        "update_at": CUTOFF,
        "delete_at": 0,
        "remote_id": "",
        "channel_id": CHANNEL,
        **changes,
    }


def reaction_event(value: dict[str, Any] | None = None, **changes: Any) -> dict[str, Any]:
    raw = json.dumps(value if value is not None else reaction())
    return {**event(event="reaction_added", data={"reaction": raw}), **changes}


@pytest.mark.parametrize(
    ("emoji", "choice"),
    [("+1", "allow"), ("thumbsup", "allow"), ("-1", "deny"), ("thumbsdown", "deny")],
)
async def test_reaction_frames_yield_only_owner_thumbs(
    install: Any, emoji: str, choice: str
) -> None:
    socket, _ = install([HELLO, OK])
    iterator = stream()
    task = asyncio.create_task(anext(iterator))
    await asyncio.wait_for(socket.waiting.wait(), 0.05)
    ignored = [
        reaction_event(reaction(user_id=OTHER)),
        reaction_event(broadcast={"channel_id": OTHER}),
        reaction_event(reaction(channel_id=OTHER)),
        reaction_event(reaction(post_id="short")),
        reaction_event(reaction(post_id=None)),
        reaction_event(reaction(emoji_name="eyes")),
        reaction_event(reaction(emoji_name="THUMBSUP")),
        reaction_event(reaction(emoji_name=["+1"])),
        reaction_event(reaction(create_at=CUTOFF - 1)),
        reaction_event(reaction(create_at=True)),
        reaction_event(reaction(delete_at=1)),
        reaction_event(reaction(delete_at=False)),
        reaction_event(reaction(padding="x" * 4097)),
        reaction_event(event="reaction_removed"),
        event(data={"reaction": reaction()}, event="reaction_added"),
    ]
    for frame in ignored:
        socket.put(frame)
    socket.put(reaction_event(reaction(emoji_name=emoji, create_at=CUTOFF + 7)))
    result = await asyncio.wait_for(task, 0.05)
    assert result == {
        "_reaction": True,
        "post_id": POST,
        "emoji_name": emoji,
        "create_at": CUTOFF + 7,
        "choice": choice,
        "_fresh_cutoff_ms": CUTOFF,
    }
    await iterator.aclose()


async def test_malformed_reaction_json_ends_the_stream(install: Any) -> None:
    socket, _ = install([HELLO, OK])
    iterator = stream()
    task = asyncio.create_task(anext(iterator))
    await asyncio.wait_for(socket.waiting.wait(), 0.05)
    socket.put(event(event="reaction_added", data={"reaction": "{"}))
    with pytest.raises(MattermostTransportError):
        await asyncio.wait_for(task, 0.05)
    assert socket.closed


@pytest.mark.parametrize("message", ["x" * 8192, "\u2603" * 2730 + "xx"], ids=["ascii", "utf8"])
async def test_exact_input_byte_limit_and_thread_are_admitted(install: Any, message: str) -> None:
    socket, _ = install([HELLO, OK])
    iterator = stream()
    task = asyncio.create_task(anext(iterator))
    await asyncio.wait_for(socket.waiting.wait(), 0.05)
    socket.put(event(post(message=message, root_id=OTHER, file_ids=None)))
    result = await asyncio.wait_for(task, 0.05)
    assert result["message"] == message and result["root_id"] == OTHER
    await iterator.aclose()


@pytest.mark.parametrize(
    "bad",
    [
        event(event="post_edited"),
        event(event="post_deleted"),
        event(event="typing"),
        event(broadcast={"channel_id": OTHER}),
        event(data={}),
        event(data={"post": {}}),
    ],
)
async def test_nonposted_or_invalid_event_shapes_are_ignored(install: Any, bad: Any) -> None:
    socket, _ = install([HELLO, OK])
    iterator = stream()
    task = asyncio.create_task(anext(iterator))
    await asyncio.wait_for(socket.waiting.wait(), 0.05)
    socket.put(bad)
    socket.put(event(post(message="accepted")))
    assert (await asyncio.wait_for(task, 0.05))["message"] == "accepted"
    await iterator.aclose()


@pytest.mark.parametrize(
    "raw",
    [
        '{"id":"' + POST + '","id":"' + OTHER + '"}',
        '{"nested":' + "[" * 33 + "0" + "]" * 33 + "}",
        '{"create_at":NaN}',
        '"' + "x" * (128 * 1024) + '"',
    ],
    ids=["duplicate", "depth", "nan", "oversize"],
)
async def test_embedded_post_json_is_bounded_and_unique(install: Any, raw: str) -> None:
    socket, _ = install([HELLO, OK])
    iterator = stream()
    task = asyncio.create_task(anext(iterator))
    await asyncio.wait_for(socket.waiting.wait(), 0.05)
    socket.put(event(data={"post": raw}))
    with pytest.raises(MattermostTransportError):
        await asyncio.wait_for(task, 0.05)
    assert socket.closed


@pytest.mark.parametrize("stage", ["send", "auth", "cutoff", "live"])
async def test_cancel_closes_socket_and_leaves_no_worker(install: Any, stage: str) -> None:
    socket, _ = install([] if stage in ("send", "auth") else [HELLO, OK])
    started = asyncio.Event()
    if stage == "send":
        socket.send_gate = asyncio.Event()

    async def fresh() -> int:
        started.set()
        if stage == "cutoff":
            await asyncio.Event().wait()
        return CUTOFF

    iterator = stream(fresh)
    task = asyncio.create_task(anext(iterator))
    milestone = (
        socket.send_started
        if stage == "send"
        else (started if stage == "cutoff" else socket.waiting)
    )
    await asyncio.wait_for(milestone.wait(), 0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert socket.closed
    await iterator.aclose()


@pytest.mark.parametrize("frames", [[], [HELLO], [OK]])
async def test_auth_deadline_closes_partial_auth(
    install: Any, monkeypatch: pytest.MonkeyPatch, frames: list[Any]
) -> None:
    socket, _ = install(frames)
    monkeypatch.setattr(mattermost_transport, "AUTH_DEADLINE_S", 0.01)
    with pytest.raises(MattermostTransportError):
        await anext(stream())
    assert socket.closed


async def test_freshness_failure_preserves_fixed_code_and_closes(install: Any) -> None:
    socket, _ = install([HELLO, OK])

    async def invalid() -> int:
        raise MattermostHttpError("mattermost_freshness_failed")

    with pytest.raises(MattermostHttpError, match="^mattermost_freshness_failed$"):
        await anext(stream(invalid))
    assert socket.closed


@pytest.mark.parametrize("value", [True, 0, -1, 1.5, CUTOFF + 1])
async def test_invalid_cutoff_fails_closed(install: Any, value: Any) -> None:
    socket, _ = install([HELLO, OK])

    async def invalid() -> int:
        return value

    with pytest.raises(MattermostTransportError):
        await anext(stream(invalid))
    assert socket.closed


@pytest.mark.parametrize(
    "failure",
    [
        RuntimeError("literal private frame " + TOKEN + CONFIG.url),
        ConnectionClosedOK(Close(1000, TOKEN), Close(1000, TOKEN), True),
    ],
    ids=["failure", "closed"],
)
async def test_closed_and_failure_are_fixed_and_quiet(
    install: Any, caplog: Any, failure: Exception
) -> None:
    socket, _ = install([failure])
    caplog.set_level(logging.DEBUG)
    with pytest.raises(MattermostTransportError) as error:
        await anext(stream())
    assert str(error.value) == "mattermost_transport_failed"
    assert error.value.__context__ is None
    assert socket.closed
    assert TOKEN not in caplog.text and CONFIG.url not in caplog.text


async def test_wss_logging_survives_reconfiguration_after_connection_creation(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    socket = Socket([HELLO, OK])
    connector = Connector(socket)
    logger = mattermost_transport._QUIET_LOGGER
    send = socket.send

    def reconfigure() -> None:
        logging.config.dictConfig({"version": 1, "disable_existing_loggers": False})

    def connection(uri: str, **options: Any) -> Connector:
        assert options["logger"] is logger
        result = connector(uri, **options)
        reconfigure()
        logger.info("opening %s", uri)
        return result

    async def log_send(value: str) -> None:
        logger.debug("outgoing %s", value)
        await send(value)

    monkeypatch.setattr(mattermost_transport, "_NoRedirectConnect", connection)
    monkeypatch.setattr(socket, "send", log_send)
    caplog.set_level(logging.DEBUG)
    logger.addHandler(caplog.handler)
    iterator = stream()
    task = asyncio.create_task(anext(iterator))
    try:
        await asyncio.wait_for(socket.waiting.wait(), 0.05)
        reconfigure()
        for level in (
            logging.DEBUG,
            logging.INFO,
            logging.WARNING,
            logging.ERROR,
            logging.CRITICAL,
        ):
            logger.log(level, "%s %s private body", CONFIG.url, TOKEN)
        socket.put(event(post(message="accepted")))
        assert (await asyncio.wait_for(task, 0.05))["message"] == "accepted"
        logging.getLogger("viki_agent.mattermost_bot").warning("mattermost_transport_failed")
        assert [(record.name, record.getMessage()) for record in caplog.records] == [
            ("viki_agent.mattermost_bot", "mattermost_transport_failed")
        ]
    finally:
        logger.removeHandler(caplog.handler)
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        await iterator.aclose()
    assert socket.closed


@pytest.mark.parametrize("stage", ["send", "cutoff"])
async def test_send_and_cutoff_deadlines_close(
    install: Any, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    socket, _ = install([HELLO, OK])
    monkeypatch.setattr(mattermost_transport, "AUTH_DEADLINE_S", 0.01)
    if stage == "send":
        socket.send_gate = asyncio.Event()

    async def blocked() -> int:
        await asyncio.Event().wait()
        return CUTOFF

    with pytest.raises(MattermostTransportError):
        await anext(stream(blocked if stage == "cutoff" else cutoff))
    assert socket.closed


@pytest.mark.parametrize(
    "binding",
    [
        Binding("invalid", OWNER, CHANNEL),
        Binding(BOT, OWNER, "invalid"),
        Binding(BOT, OTHER, CHANNEL),
        Binding(OWNER, OWNER, CHANNEL),
    ],
)
async def test_invalid_binding_refuses_before_connect(install: Any, binding: Binding) -> None:
    _, connector = install([])
    with pytest.raises(MattermostTransportError):
        await anext(posted(CONFIG, binding, fresh_cutoff=cutoff, ssl_context=TLS))
    assert connector.calls == []


async def test_unverified_tls_context_refuses_before_connect(install: Any) -> None:
    _, connector = install([])
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    with pytest.raises(MattermostTransportError):
        await anext(posted(CONFIG, BINDING, fresh_cutoff=cutoff, ssl_context=context))
    assert connector.calls == []


async def test_transport_never_follows_redirects_or_proxies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts: list[Any] = []
    captured: list[Any] = []
    original_init = mattermost_transport._NoRedirectConnect.__init__

    def capture(self: Any, uri: str, **kwargs: Any) -> None:
        original_init(self, uri, **kwargs)
        captured.append((self, kwargs))

    class Handshake:
        transport: Any

        def __init__(self) -> None:
            self.transport = self

        def abort(self) -> None:
            pass

        async def handshake(self, *_: Any) -> None:
            raise InvalidStatus(
                Response(302, "Found", Headers({"Location": "wss://redirect.example.test/private"}))
            )

    async def tcp(self: Any) -> Any:
        attempts.append(self.uri)
        return Handshake()

    monkeypatch.setattr(mattermost_transport._NoRedirectConnect, "__init__", capture)
    monkeypatch.setattr(connect, "open_tcp_connection", tcp)
    with pytest.raises(MattermostTransportError):
        await anext(stream())
    assert attempts == ["wss://chat.example.test/deployment/api/v4/websocket"]
    assert len(captured) == 1
    connector, options = captured[0]
    assert connector.proxy is None and options["ssl"] is TLS
    assert options["logger"].disabled


async def test_temporary_port_redirect_is_not_followed() -> None:
    requests: list[bytes] = []

    async def redirect(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        request = await reader.readuntil(b"\r\n\r\n")
        requests.append(request)
        writer.write(b"HTTP/1.1 302 Found\r\nLocation: /redirected\r\nContent-Length: 0\r\n\r\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(redirect, "127.0.0.1", 0)
    async with server:
        port = server.sockets[0].getsockname()[1]
        with pytest.raises(MattermostTransportError):
            await mattermost_transport._NoRedirectConnect(
                f"ws://127.0.0.1:{port}/original",
                proxy=None,
                open_timeout=0.05,
                logger=mattermost_transport._QUIET_LOGGER,
            )
    assert len(requests) == 1
    assert requests[0].startswith(b"GET /original HTTP/1.1\r\n")


@pytest.mark.parametrize("callback_error", [None, RuntimeError, asyncio.CancelledError])
async def test_terminal_notification_precedes_held_socket_exit(
    monkeypatch: pytest.MonkeyPatch,
    callback_error: type[BaseException] | None,
) -> None:
    socket = Socket([OK, {**HELLO, "broadcast": {"user_id": OTHER}}])
    connector = HeldExitConnector(socket)
    monkeypatch.setattr(mattermost_transport, "_NoRedirectConnect", connector)
    notices: list[str] = []

    def failed() -> None:
        notices.append("failed")
        if callback_error is not None:
            raise callback_error("synthetic private callback")

    pending = asyncio.ensure_future(
        anext(
            posted(
                CONFIG,
                BINDING,
                fresh_cutoff=cutoff,
                ssl_context=TLS,
                on_failure=failed,
            )
        )
    )
    try:
        await asyncio.wait_for(connector.closing.wait(), 0.05)
        assert notices == ["failed"] and not pending.done() and not socket.closed
    finally:
        connector.release.set()
    with pytest.raises(MattermostTransportError):
        await pending
    assert notices == ["failed"] and socket.closed


@pytest.mark.parametrize("terminal", ["malformed", "eof", "cancel", "cutoff", "http", "exit"])
async def test_live_cutoff_and_cancel_notification_precedes_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    terminal: str,
) -> None:
    socket, ready = Socket([OK, HELLO]), asyncio.Event()
    connector = HeldExitConnector(socket)
    monkeypatch.setattr(mattermost_transport, "_NoRedirectConnect", connector)
    notices: list[str] = []

    async def fresh() -> int:
        ready.set()
        if terminal == "http":
            raise MattermostHttpError("mattermost_http_failed")
        return 0 if terminal == "cutoff" else CUTOFF

    iterator = posted(
        CONFIG,
        BINDING,
        fresh_cutoff=fresh,
        ssl_context=TLS,
        on_failure=lambda: notices.append("failed"),
    )
    pending = asyncio.ensure_future(anext(iterator))
    try:
        await asyncio.wait_for(ready.wait(), 0.05)
        if terminal not in ("cutoff", "http"):
            await asyncio.wait_for(socket.waiting.wait(), 0.05)
            assert notices == []
            if terminal == "cancel":
                pending.cancel()
            elif terminal == "exit":
                socket.put(event())
                assert await asyncio.wait_for(pending, 0.05)
                assert notices == []
                connector.exit_error = RuntimeError("synthetic private socket exit")
                pending = asyncio.create_task(iterator.aclose())  # type: ignore[attr-defined]
            else:
                socket.put("{bad" if terminal == "malformed" else RuntimeError("private EOF"))
        await asyncio.wait_for(connector.closing.wait(), 0.05)
        assert notices == ["failed"] and not pending.done() and not socket.closed
    finally:
        connector.release.set()
    error = (
        asyncio.CancelledError
        if terminal == "cancel"
        else MattermostHttpError
        if terminal == "http"
        else MattermostTransportError
    )
    with pytest.raises(error):
        await pending
    assert notices == ["failed"] and socket.closed


async def test_invalid_preconnect_authority_notifies_once(install: Any) -> None:
    _, connector = install([])
    notices: list[str] = []
    with pytest.raises(MattermostTransportError):
        await anext(
            posted(
                CONFIG,
                Binding(BOT, OTHER, CHANNEL),
                fresh_cutoff=cutoff,
                ssl_context=TLS,
                on_failure=lambda: notices.append("failed"),
            )
        )
    assert notices == ["failed"] and connector.calls == []
