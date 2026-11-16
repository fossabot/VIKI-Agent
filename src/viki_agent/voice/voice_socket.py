"""The /ws/voice session: one dictation capture per connection, one connection app-wide.

SecurityMiddleware refuses a foreign Host or Origin before the app. Eight connections at most wait
unauthenticated; a ninth closes 1013 before accept. The endpoint accepts, runs
the main socket's first-message authentication and only then takes the one slot, so a connection
that has not authenticated never holds the slot and never learns that a capture is running. A
busy slot closes 1013 after accept, so the browser sees the code. The client sends `begin`, PCM
s16le frames, then `end` or `cancel`; the server streams the engine's results and closes 1000
after the capture's done, cancelled or error.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from typing import Any

import anyio
from starlette.routing import WebSocketRoute
from starlette.types import Message
from starlette.websockets import WebSocket, WebSocketDisconnect

from viki_agent.api_json import unique_object
from viki_agent.voice import voice_engine
from viki_agent.voice.voice_engine import VoiceError, VoiceResult, _is_canonical_uuid
from viki_agent.voice.voice_service import VoiceService

logger = logging.getLogger(__name__)

MAX_TEXT_BYTES = 1024
MAX_AUDIO_BYTES = 16_384
RECEIVE_GAP_S = 10.0
SOCKET_WRITE_TIMEOUT_S = 5.0
NORMAL = 1000
BUSY = 1013
MAX_UNAUTHENTICATED = 8  # the main socket's limit applies before authentication too
BAD_MESSAGE = 4400
IDLE = 4408

_GONE = 0  # the client disconnected: there is nothing to close
_KEYS = frozenset({"type", "capture_id"})
_TYPES = frozenset({"begin", "end", "cancel"})
_TERMINAL = frozenset({"done", "cancelled", "error"})
# What a send or close raises once the client has gone; the next receive reports the disconnect.
_SEND_ERRORS = (WebSocketDisconnect, RuntimeError, OSError)


def _parse(text: str) -> tuple[str, str] | None:
    """(type, capture id) of a well-formed client message, else None."""
    if len(text.encode("utf-8")) > MAX_TEXT_BYTES:
        return None
    try:
        data = json.loads(text, object_pairs_hook=unique_object)
    except ValueError, RecursionError:
        return None
    if not isinstance(data, dict) or set(data) != _KEYS:
        return None
    kind, capture_id = data["type"], data["capture_id"]
    # A str check comes first: `in` raises TypeError for an unhashable array or object.
    if not isinstance(kind, str) or kind not in _TYPES or not isinstance(capture_id, str):
        return None
    return (kind, capture_id) if _is_canonical_uuid(capture_id) else None


def _message(capture_id: str, result: VoiceResult) -> dict[str, object]:
    if result.kind == "partial":
        return {
            "type": "partial",
            "capture_id": capture_id,
            "segment": result.segment,
            "revision": result.revision,
            "text": result.text,
        }
    if result.kind == "final":
        return {
            "type": "final",
            "capture_id": capture_id,
            "segment": result.segment,
            "text": result.text,
        }
    if result.kind == "error":
        return {"type": "error", "capture_id": capture_id, "code": result.code}
    return {"type": result.kind, "capture_id": capture_id}


async def _cancel(task: asyncio.Task[Any] | None) -> None:
    if task is not None:
        task.cancel()
        await asyncio.wait([task])


class _Session:
    """One connection's capture, run by one task: it is the socket's only reader and writer."""

    def __init__(
        self,
        service: VoiceService,
        websocket: WebSocket,
        receive_gap_s: float,
        final_timeout_s: float,
    ) -> None:
        self._service = service
        self._websocket = websocket
        self._receive_gap_s = receive_gap_s
        self._final_timeout_s = final_timeout_s
        self._loop = asyncio.get_running_loop()
        self._capture: str | None = None  # the id `begin` named
        self._open = False  # the service holds the capture: no done, cancelled or error yet
        self._accepting = False  # audio is taken: no end, cancel or stopping yet
        self._heard = self._loop.time()
        self._ending_since = 0.0
        self._receiving: asyncio.Task[Message] | None = None
        self._next_result: asyncio.Task[VoiceResult] | None = None

    async def run(self) -> int:
        """Serve the capture; the close code to send, or _GONE."""
        try:
            return await self._serve()
        except TimeoutError:
            return IDLE
        finally:
            # Shielded against one cancel: that handler still cancels its capture, so the next begin
            # is free. A second native task.cancel(), as at shutdown, is not stopped by the shield.
            with anyio.CancelScope(shield=True):
                await self._teardown()

    async def _serve(self) -> int:
        while True:
            if self._receiving is None:
                self._receiving = asyncio.ensure_future(self._websocket.receive())
            receiving = self._receiving
            waits: list[asyncio.Task[Any]] = [receiving]
            if self._open:
                if self._next_result is None:
                    self._next_result = asyncio.ensure_future(self._service.results().get())
                waits.append(self._next_result)
            timeout = max(0.0, self._deadline() - self._loop.time())
            await asyncio.wait(waits, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
            next_result = self._next_result
            if next_result is not None and next_result.done():
                self._next_result = None
                code = await self._forward(next_result.result())
            elif receiving.done():
                self._receiving = None
                self._heard = self._loop.time()
                code = await self._handle(receiving.result())
            else:
                code = await self._expired()
            if code is not None:
                return code

    def _deadline(self) -> float:
        if self._capture is None or self._accepting:
            return self._heard + self._receive_gap_s
        return self._ending_since + self._final_timeout_s

    async def _expired(self) -> int:
        if self._capture is None or self._accepting:
            return IDLE
        logger.warning("voice socket closed: final_timeout")
        await self._service.abort()
        await self._send({"type": "error", "capture_id": self._capture, "code": "final_timeout"})
        self._finish()
        return NORMAL

    async def _forward(self, result: VoiceResult) -> int | None:
        assert self._capture is not None
        await self._send(_message(self._capture, result))
        if result.kind in _TERMINAL:
            self._finish()
            return NORMAL
        return None

    async def _handle(self, message: Message) -> int | None:
        if message["type"] == "websocket.disconnect":
            return _GONE
        text, pcm = message.get("text"), message.get("bytes")
        if text is not None:
            return await self._text(text)
        if pcm is not None:
            return await self._audio(pcm)
        return BAD_MESSAGE

    async def _text(self, text: str) -> int | None:
        parsed = _parse(text)
        if parsed is None:
            return BAD_MESSAGE
        kind, capture_id = parsed
        if self._capture is None:
            return await self._begin(capture_id) if kind == "begin" else BAD_MESSAGE
        if kind == "begin" or capture_id != self._capture:
            return BAD_MESSAGE
        if self._accepting:  # a repeat after end, cancel or stopping is ignored
            self._stop_accepting()
            # A VoiceError here means the worker ended: its error is already in the queue.
            with contextlib.suppress(VoiceError):
                if kind == "end":
                    await self._service.end(capture_id)
                else:
                    await self._service.cancel(capture_id)
        return None

    async def _audio(self, pcm: bytes) -> int | None:
        if self._capture is None or not 2 <= len(pcm) <= MAX_AUDIO_BYTES or len(pcm) % 2:
            return BAD_MESSAGE
        if not self._accepting:
            return None  # after end, cancel or stopping
        try:
            await self._service.audio(self._capture, pcm)
        except VoiceError as exc:
            if exc.code == "invalid_audio":
                return BAD_MESSAGE
            if exc.code in ("capture_cap", "overloaded"):
                self._stop_accepting()
                await self._send(
                    {"type": "stopping", "capture_id": self._capture, "reason": exc.code}
                )
                with contextlib.suppress(VoiceError):
                    await self._service.end(self._capture)
            # unknown_capture or not_running: the worker ended, and its error is in the queue
        return None

    async def _begin(self, capture_id: str) -> int | None:
        self._capture = capture_id

        async def starting() -> None:
            await self._send({"type": "starting", "capture_id": capture_id})

        try:
            capturing = await self._service.begin(capture_id, starting=starting)
        except VoiceError as exc:
            await self._send({"type": "error", "capture_id": capture_id, "code": exc.code})
            return NORMAL
        self._open = self._accepting = True
        self._heard = self._loop.time()
        await self._send({"type": "capturing", "capture_id": capture_id, **asdict(capturing)})
        return None

    def _stop_accepting(self) -> None:
        self._accepting = False
        self._ending_since = self._loop.time()

    def _finish(self) -> None:
        self._open = False
        self._service.finished()

    async def _send(self, message: dict[str, object]) -> None:
        text = json.dumps(message, ensure_ascii=True, separators=(",", ":"))
        # Expiry must escape the send errors: TimeoutError is an OSError.
        async with asyncio.timeout(SOCKET_WRITE_TIMEOUT_S):
            with contextlib.suppress(*_SEND_ERRORS):
                await self._websocket.send_text(text)

    async def _teardown(self) -> None:
        await _cancel(self._receiving)
        if self._open and self._capture is not None:
            with contextlib.suppress(VoiceError):  # already ending, or the worker ended
                await self._service.cancel(self._capture)
            if not await self._ended():
                await self._service.abort()
            self._finish()
        await _cancel(self._next_result)

    async def _ended(self) -> bool:
        """Whether the capture's done, cancelled or error arrives within final_timeout_s."""
        try:
            async with asyncio.timeout(self._final_timeout_s):
                while True:
                    if self._next_result is None:
                        self._next_result = asyncio.ensure_future(self._service.results().get())
                    result = await self._next_result
                    self._next_result = None
                    if result.kind in _TERMINAL:
                        return True
        except TimeoutError:
            return False


def voice_socket_route(
    service: VoiceService,
    *,
    authenticate: Callable[[WebSocket], Awaitable[bool]],
    receive_gap_s: float = RECEIVE_GAP_S,
    final_timeout_s: float = voice_engine.FINAL_TIMEOUT_S,
) -> WebSocketRoute:
    slot = anyio.CapacityLimiter(1)
    unauthenticated = anyio.CapacityLimiter(MAX_UNAUTHENTICATED)

    async def endpoint(websocket: WebSocket) -> None:
        try:
            unauthenticated.acquire_nowait()
        except anyio.WouldBlock:
            await websocket.close(code=BUSY)
            return
        try:
            await websocket.accept()
            authenticated = await authenticate(websocket)
        finally:
            unauthenticated.release()
        if not authenticated:
            return
        try:
            slot.acquire_nowait()
        except anyio.WouldBlock:
            await websocket.close(code=BUSY)
            return
        try:
            code = await _Session(service, websocket, receive_gap_s, final_timeout_s).run()
        finally:
            slot.release()
        if code in (BAD_MESSAGE, IDLE):
            logger.warning("voice socket closed: %d", code)
        if code != _GONE:
            with contextlib.suppress(*_SEND_ERRORS):
                async with asyncio.timeout(SOCKET_WRITE_TIMEOUT_S):
                    await websocket.close(code=code)

    return WebSocketRoute("/ws/voice", endpoint)
