"""A paused transport cannot retain the shared dictation capture slot."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any, cast

import pytest
from starlette.types import Message, Receive, Scope, Send
from starlette.websockets import WebSocket
from uvicorn.config import Config
from uvicorn.protocols.websockets.websockets_sansio_impl import WebSocketsSansIOProtocol
from uvicorn.server import ServerState

from viki_agent.voice import voice_socket
from viki_agent.voice.voice_engine import VoiceResult
from viki_agent.voice.voice_service import Capturing, VoiceService

CAPTURE = "12345678-1234-5678-1234-567812345678"
FAST = 0.001
HANG_GUARD_S = 5  # event-driven endpoint completion normally takes two short injected deadlines


class Service:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[VoiceResult] = asyncio.Queue()
        self.begins = 0
        self.cancels = 0
        self.finishes = 0
        self.open = False

    async def begin(self, capture_id: str, *, starting: Callable[[], Awaitable[None]]) -> Capturing:
        assert not self.open
        self.open = True
        self.begins += 1
        return Capturing("fake", "cpu", None, "auto", False, None)

    def results(self) -> asyncio.Queue[VoiceResult]:
        return self.queue

    async def cancel(self, capture_id: str) -> None:
        self.cancels += 1
        self.queue.put_nowait(VoiceResult("cancelled", capture_id))

    async def abort(self) -> None:
        pytest.fail("the queued cancellation should complete capture teardown")

    def finished(self) -> None:
        self.open = False
        self.finishes += 1


async def unused_app(_scope: Scope, _receive: Receive, _send: Send) -> None:
    pass


class Wire:
    def __init__(self, *, blocked: bool = False, block_close: bool = False) -> None:
        incoming: asyncio.Queue[Message] = asyncio.Queue()
        incoming.put_nowait({"type": "websocket.connect"})
        incoming.put_nowait({"type": "websocket.receive", "text": '{"type":"auth"}'})
        for kind in ("begin", "cancel"):
            incoming.put_nowait(
                {
                    "type": "websocket.receive",
                    "text": json.dumps({"type": kind, "capture_id": CAPTURE}),
                }
            )
        self.socket = WebSocket({"type": "websocket"}, incoming.get, self.send)
        self.attempts: list[Message] = []
        self.interrupted: list[str] = []
        self.blocked = blocked
        self.block_close = block_close
        # Use the installed protocol's real writable gate, without any socket or worker process.
        self.protocol = WebSocketsSansIOProtocol(
            Config(unused_app, log_config=None), ServerState(), {}
        )
        self.protocol.pause_writing()

    async def send(self, message: Message) -> None:
        self.attempts.append(message)
        if self.blocked and (
            message["type"] == "websocket.send"
            or (self.block_close and message["type"] == "websocket.close")
        ):
            try:
                await self.protocol.send(cast(Any, message))
            except asyncio.CancelledError:
                self.interrupted.append(message["type"])
                raise


async def authenticate(websocket: WebSocket) -> bool:
    return (await websocket.receive()).get("text") == '{"type":"auth"}'


@pytest.mark.parametrize("block_close", [False, True])
def test_a_blocked_write_cleans_up_and_admits_the_next_capture(
    monkeypatch: pytest.MonkeyPatch, block_close: bool, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(voice_socket, "SOCKET_WRITE_TIMEOUT_S", FAST, raising=False)

    async def check() -> None:
        service = Service()
        route = voice_socket.voice_socket_route(
            cast(VoiceService, service),
            authenticate=authenticate,
            receive_gap_s=FAST,
            final_timeout_s=FAST,
        )
        first = Wire(blocked=True, block_close=block_close)
        task = asyncio.create_task(route.endpoint(first.socket))
        try:
            done, pending = await asyncio.wait([task], timeout=0.05)
            assert done and not pending, "a backpressured voice endpoint exceeded its deadlines"
            assert task.result() is None  # timeout is a normal close outcome, not a raw exception
            assert service.cancels == service.finishes == 1 and not service.open
            assert first.attempts[-1]["type"] == "websocket.close"
            assert first.attempts[-1]["code"] == 4408
            assert first.interrupted == ["websocket.send"] + (
                ["websocket.close"] if block_close else []
            )
            assert not first.protocol.writable.is_set()
            second = Wire()
            async with asyncio.timeout(HANG_GUARD_S):
                await route.endpoint(second.socket)
            assert service.begins == service.cancels == service.finishes == 2
            assert [json.loads(message["text"])["type"] for message in second.attempts[1:-1]] == [
                "capturing",
                "cancelled",
            ]
            assert second.attempts[-1]["code"] == 1000
        finally:
            if not task.done():
                task.cancel()
                async with asyncio.timeout(HANG_GUARD_S):
                    await asyncio.gather(task, return_exceptions=True)

    asyncio.run(check())
    assert "voice socket closed: 4408" in caplog.text
