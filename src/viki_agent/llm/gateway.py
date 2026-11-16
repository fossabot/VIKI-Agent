"""A per-process, capability-authenticated loopback gateway with bounded upstream streams."""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Coroutine
from contextlib import AsyncExitStack, aclosing
from dataclasses import dataclass, field
from typing import Any

import anyio
import httpx2
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

from viki_agent.api_json import json_response, unique_object
from viki_agent.llm.connections import Connection, ConnectionError, validate_model
from viki_agent.llm.protocols import ReplyStream, event, request_body

MAX_BODY = 32 * 1024 * 1024
MAX_REPLY = 32 * 1024 * 1024
MAX_EVENT = 2 * 1024 * 1024
DEADLINE_S = 300
INTAKE_S = 10
MAX_REQUESTS = 16

# URLs may contain private endpoint paths. Match the existing HTTP boundary's logging rule.
for _name in ("httpx2", "httpcore2", "httpcore2.connection", "httpcore2.http11"):
    logging.getLogger(_name).disabled = True
    logging.getLogger(_name).addFilter(lambda _: False)


async def read_body(request: Request, limit: int = MAX_BODY) -> dict[str, Any]:
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > limit:
            raise ConnectionError("request_too_large")
    try:
        value = json.loads(data, object_pairs_hook=unique_object)
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except ValueError, TypeError, RecursionError:
        raise ConnectionError("invalid_connection") from None


async def _events(response: httpx2.Response) -> AsyncGenerator[dict[str, Any]]:
    line = bytearray()
    data: list[bytes] = []
    frame_size = 0
    previous_cr = False
    first_line = True
    total = 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > MAX_REPLY:
            raise ConnectionError("response_too_large")
        # Decompression may deliver a large chunk without an await. Bound parser work too.
        for offset in range(0, len(chunk), 16384):
            await asyncio.sleep(0)
            for char in memoryview(chunk)[offset : offset + 16384]:
                if previous_cr and char == 10:
                    previous_cr = False
                    continue
                previous_cr = char == 13
                frame_size += 1
                if frame_size > MAX_EVENT:
                    raise ConnectionError("response_too_large")
                if char not in (10, 13):
                    line.append(char)
                    continue
                raw = bytes(line)
                line.clear()
                if first_line:
                    raw = raw.removeprefix(b"\xef\xbb\xbf")
                    first_line = False
                if raw:
                    if raw.startswith(b"data:"):
                        data.append(raw[5:].removeprefix(b" "))
                    continue
                frame_size = 0
                if not data:
                    continue
                payload = b"\n".join(data)
                data.clear()
                if payload == b"[DONE]":
                    return
                try:
                    value = json.loads(payload)
                    if not isinstance(value, dict):
                        raise ValueError()
                except ValueError, TypeError, RecursionError:
                    raise ConnectionError("invalid_response") from None
                yield value
    if line or data:
        raise ConnectionError("incomplete_response")


async def finish_cleanup(work: Coroutine[Any, Any, None]) -> None:
    """Finish owned cleanup even under repeated asyncio or AnyIO cancellation."""
    closing = asyncio.create_task(work)
    cancelled: asyncio.CancelledError | None = None
    with anyio.CancelScope(shield=True):
        while not closing.done():
            try:
                await asyncio.shield(closing)
            except asyncio.CancelledError as exc:
                cancelled = exc
        closing.result()
    if cancelled is not None:
        raise cancelled


class _ClosingStream(httpx2.AsyncByteStream):
    def __init__(self, stream: httpx2.AsyncByteStream) -> None:
        self.stream = stream

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self.stream.__aiter__()

    async def aclose(self) -> None:
        # httpx marks a response closed before awaiting its stream's close. Protect
        # that first close too; retrying response.aclose after cancellation is a no-op.
        await finish_cleanup(self.stream.aclose())


async def _protect_close(response: httpx2.Response) -> None:
    if not isinstance(response.stream, httpx2.AsyncByteStream):
        raise ConnectionError("invalid_response")
    response.stream = _ClosingStream(response.stream)


class OwnedResponse(Response):
    """Retain request admission until ASGI and all entered iterators have finished."""

    def __init__(self, response: Response, release: Callable[[], None]) -> None:
        super().__init__(status_code=response.status_code)
        self.response = response
        self.release = release

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await self.response(scope, receive, send)
        finally:

            async def close() -> None:
                try:
                    iterator = getattr(self.response, "body_iterator", None)
                    close_body = getattr(iterator, "aclose", None)
                    if close_body is not None:
                        await close_body()
                finally:
                    self.release()

            await finish_cleanup(close())


@dataclass(repr=False)
class RuntimeRoute:
    gateway: Gateway
    connection: Connection
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    token: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    failure_code: str | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def environment(self) -> dict[str, str]:
        model = self.connection.model
        return {
            "CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST": "1",
            "ANTHROPIC_BASE_URL": f"http://127.0.0.1:8765/api/llm/runtime/{self.id}",
            "ANTHROPIC_AUTH_TOKEN": self.token,
            "ANTHROPIC_API_KEY": "",
            "ANTHROPIC_MODEL": model,
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": model,
            "ANTHROPIC_DEFAULT_SONNET_MODEL": model,
            "ANTHROPIC_DEFAULT_OPUS_MODEL": model,
        }

    def revoke(self) -> None:
        self.gateway.revoke(self.id)


class Gateway:
    def __init__(
        self,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
        before_request: Callable[[Connection], Awaitable[None]] | None = None,
    ) -> None:
        self._routes: dict[str, RuntimeRoute] = {}
        self._requests: dict[str, set[asyncio.Task[Any]]] = {}
        self._capacity = asyncio.Semaphore(8)
        self._admitted = 0
        self.transport = transport
        self.before_request = before_request

    def issue(self, connection: Connection) -> RuntimeRoute:
        if len(self._routes) >= 128:
            raise ConnectionError("connection_limit")
        route = RuntimeRoute(self, connection)
        self._routes[route.id] = route
        return route

    def revoke(self, runtime_id: str) -> None:
        self._routes.pop(runtime_id, None)
        for task in self._requests.pop(runtime_id, set()):
            task.cancel()

    def admit(self, runtime_id: str | None = None) -> Callable[[], None]:
        if self._admitted >= MAX_REQUESTS:
            raise ConnectionError("busy")
        task = asyncio.current_task()
        self._admitted += 1
        if runtime_id is not None and task is not None:
            self._requests.setdefault(runtime_id, set()).add(task)

        def release() -> None:
            self._admitted -= 1
            active = self._requests.get(runtime_id or "")
            if active is not None:
                active.discard(task)
                if not active:
                    self._requests.pop(runtime_id or "", None)

        return release

    def _client(self, connection: Connection) -> httpx2.AsyncClient:
        headers = {"Accept-Encoding": "identity", "Content-Type": "application/json"}
        if connection.api_key:
            headers["Authorization"] = f"Bearer {connection.api_key}"
            if connection.api_mode == "anthropic":
                headers["x-api-key"] = connection.api_key
        if connection.api_mode == "anthropic":
            headers["anthropic-version"] = "2023-06-01"
        return httpx2.AsyncClient(
            transport=self.transport,
            headers=headers,
            timeout=180,
            follow_redirects=False,
            trust_env=False,
            limits=httpx2.Limits(max_connections=2),
            event_hooks={"response": [_protect_close]},
        )

    async def generate(
        self,
        connection: Connection,
        body: dict[str, Any],
        *,
        beta: str = "",
        on_start: Callable[[], None] | None = None,
    ) -> AsyncGenerator[bytes]:
        mode = connection.api_mode
        try:
            if self.before_request is not None:
                await self.before_request(connection)
            outgoing = request_body(body, mode, connection.model)
            outgoing["stream"] = True
            path = {
                "anthropic": "messages",
                "responses": "responses",
                "chat_completions": "chat/completions",
            }[mode]
            headers = {"anthropic-beta": beta} if beta and mode == "anthropic" else {}
            async with asyncio.timeout(DEADLINE_S), self._capacity:
                stack = AsyncExitStack()
                try:
                    client = await stack.enter_async_context(self._client(connection))
                    if on_start is not None:
                        on_start()
                    response = await stack.enter_async_context(
                        client.stream("POST", connection.url(path), json=outgoing, headers=headers)
                    )
                    rows = await stack.enter_async_context(aclosing(_events(response)))
                    if response.status_code != 200:
                        raise ConnectionError(
                            "authentication_failed"
                            if response.status_code in (401, 403)
                            else "rate_limited"
                            if response.status_code == 429
                            else "upstream_failed"
                        )
                    if (
                        not response.headers.get("content-type", "")
                        .lower()
                        .startswith("text/event-stream")
                    ):
                        raise ConnectionError("invalid_response")
                    if mode == "anthropic":
                        started = delta = complete = stopped = False
                        blocks: set[int] = set()
                        next_block = 0
                        async for row in rows:
                            kind = row.pop("type", "")
                            index = row.get("index")
                            if kind == "error":
                                raise ConnectionError("upstream_failed")
                            if complete:
                                raise ConnectionError("invalid_response")
                            if kind == "ping":
                                continue
                            if kind == "message_start":
                                message = row.get("message")
                                if (
                                    started
                                    or not isinstance(message, dict)
                                    or not isinstance(message.get("id"), str)
                                    or not message["id"]
                                    or message.get("type") != "message"
                                    or message.get("role") != "assistant"
                                    or not isinstance(message.get("content"), list)
                                ):
                                    raise ConnectionError("invalid_response")
                                started = True
                            elif not started:
                                raise ConnectionError("invalid_response")
                            elif kind == "content_block_start":
                                if delta or type(index) is not int or index != next_block:
                                    raise ConnectionError("invalid_response")
                                if not isinstance(row.get("content_block"), dict):
                                    raise ConnectionError("invalid_response")
                                blocks.add(index)
                                next_block += 1
                            elif kind in ("content_block_delta", "content_block_stop"):
                                if type(index) is not int or index not in blocks or delta:
                                    raise ConnectionError("invalid_response")
                                if kind == "content_block_stop":
                                    blocks.remove(index)
                                elif not isinstance(row.get("delta"), dict):
                                    raise ConnectionError("invalid_response")
                            elif kind == "message_delta":
                                if blocks or not isinstance(row.get("delta"), dict):
                                    raise ConnectionError("invalid_response")
                                reason = row["delta"].get("stop_reason")
                                if reason is not None:
                                    if not isinstance(reason, str) or not reason:
                                        raise ConnectionError("invalid_response")
                                    stopped = True
                                delta = True
                            elif kind == "message_stop":
                                if blocks or not delta or not stopped:
                                    raise ConnectionError("invalid_response")
                                complete = True
                            else:
                                raise ConnectionError("invalid_response")
                            yield event(kind, row)
                        if not complete:
                            raise ConnectionError("incomplete_response")
                    else:
                        adapter = ReplyStream(connection.model, mode)
                        yield adapter.start()
                        async for row in rows:
                            for part in adapter.accept(row):
                                yield part
                        for part in adapter.finish():
                            yield part
                finally:
                    await finish_cleanup(stack.aclose())
        except ConnectionError:
            raise
        except TimeoutError, httpx2.HTTPError, httpx2.InvalidURL:
            raise ConnectionError("endpoint_unreachable") from None
        except ValueError, TypeError, KeyError, IndexError, AttributeError, RecursionError:
            raise ConnectionError("invalid_response") from None

    async def test(self, connection: Connection) -> list[str]:
        body = {"messages": [{"role": "user", "content": "Reply OK."}], "max_tokens": 16}
        async with aclosing(self.generate(connection, body)) as rows:
            async for _ in rows:
                pass
        try:
            return list(dict.fromkeys([connection.model, *await self.models(connection)]))
        except ConnectionError:
            return [connection.model]

    async def models(self, connection: Connection) -> list[str]:
        models: list[str] = []
        try:
            if self.before_request is not None:
                await self.before_request(connection)
            async with asyncio.timeout(10):
                stack = AsyncExitStack()
                try:
                    client = await stack.enter_async_context(self._client(connection))
                    response = await stack.enter_async_context(
                        client.stream("GET", connection.url("models"))
                    )
                    if response.status_code != 200:
                        raise ConnectionError("discovery_failed")
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        data.extend(chunk)
                        if len(data) > 1024 * 1024:
                            raise ConnectionError("response_too_large")
                    value = json.loads(data)
                    for row in value.get("data", [])[:256]:
                        name = row.get("id")
                        try:
                            valid = validate_model(name)
                        except ConnectionError:
                            continue
                        if valid not in models:
                            models.append(valid)
                finally:
                    await finish_cleanup(stack.aclose())
        except (
            TimeoutError,
            httpx2.HTTPError,
            httpx2.InvalidURL,
            ValueError,
            TypeError,
            AttributeError,
        ):
            raise ConnectionError("discovery_failed") from None
        return models

    def routes(self) -> list[Route]:
        async def handler(request: Request) -> Response:
            route = self._routes.get(request.path_params["runtime_id"])
            authorization = request.headers.get("authorization", "")
            bearer = authorization[7:] if authorization.startswith("Bearer ") else ""
            credential = bearer or request.headers.get("x-api-key", "")
            if route is None or not secrets.compare_digest(
                credential.encode(), route.token.encode()
            ):
                return json_response(
                    {
                        "error": {
                            "type": "authentication_error",
                            "message": "Unavailable model connection.",
                        }
                    },
                    401,
                )
            if request.headers.get("origin"):
                return json_response(
                    {
                        "error": {
                            "type": "permission_error",
                            "message": "Unavailable model connection.",
                        }
                    },
                    403,
                )
            path = request.path_params["operation"]
            if path == "models" and request.method == "GET":
                return json_response(
                    {
                        "data": [
                            {
                                "id": route.connection.model,
                                "type": "model",
                                "display_name": route.connection.name,
                            }
                        ]
                    }
                )
            if request.method != "POST" or path not in ("messages", "messages/count_tokens"):
                return json_response(
                    {"error": {"type": "not_found_error", "message": "Unknown operation."}}, 404
                )
            release: Callable[[], None] | None = None
            transferred = False
            try:
                release = self.admit(route.id)
                async with asyncio.timeout(INTAKE_S):
                    body = await read_body(request)
                if self._routes.get(route.id) is not route:
                    raise ConnectionError("connection_closed")
                if path == "messages/count_tokens":
                    # Estimate locally rather than sending another context copy upstream.
                    response = json_response({"input_tokens": max(1, len(json.dumps(body)) // 4)})
                else:

                    async def stream() -> AsyncGenerator[bytes]:
                        # Claude Code may retry errors itself. Once inference fails, only a
                        # newly issued capability can start another upstream request.
                        async with route.lock:
                            started = complete = False

                            def inference_started() -> None:
                                nonlocal started
                                started = True

                            try:
                                if self._routes.get(route.id) is not route:
                                    raise ConnectionError("connection_closed")
                                if route.failure_code is not None:
                                    raise ConnectionError(route.failure_code)
                                beta = request.headers.get("anthropic-beta", "")
                                if len(beta) > 8192:
                                    raise ConnectionError("invalid_connection")
                                async with aclosing(
                                    self.generate(
                                        route.connection,
                                        body,
                                        beta=beta,
                                        on_start=inference_started,
                                    )
                                ) as rows:
                                    async for chunk in rows:
                                        if self._routes.get(route.id) is not route:
                                            raise ConnectionError("connection_closed")
                                        yield chunk
                                complete = True
                            except ConnectionError as exc:
                                if started:
                                    route.failure_code = exc.code
                                yield event(
                                    "error",
                                    {
                                        "error": {
                                            "type": "api_error",
                                            "message": f"Model connection failed ({exc.code}). "
                                            "Close and reopen the chat to retry. "
                                            "Check Providers in Settings.",
                                        }
                                    },
                                )
                            finally:
                                if started and not complete and route.failure_code is None:
                                    route.failure_code = "incomplete_response"

                    response = StreamingResponse(stream(), media_type="text/event-stream")
                transferred = True
                return OwnedResponse(response, release)
            except (ConnectionError, TimeoutError) as exc:
                busy = isinstance(exc, ConnectionError) and exc.code == "busy"
                return json_response(
                    {
                        "error": {
                            "type": "overloaded_error" if busy else "invalid_request_error",
                            "message": "Model connection is busy."
                            if busy
                            else "Invalid model request.",
                        }
                    },
                    503 if busy else 400,
                )
            finally:
                if release is not None and not transferred:
                    release()

        return [
            Route(
                "/api/llm/runtime/{runtime_id}/v1/{operation:path}",
                handler,
                methods=["POST", "GET"],
            )
        ]
