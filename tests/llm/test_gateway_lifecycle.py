from __future__ import annotations

import asyncio
import base64
import gzip
import json
from collections.abc import AsyncIterator
from contextlib import aclosing
from typing import Any

import httpx2
import pytest
from anyio.lowlevel import checkpoint
from starlette.applications import Starlette
from starlette.requests import Request

from tests.llm.test_connections import FORM
from tests.llm.test_gateway import connection, sse
from viki_agent.llm import gateway as gateway_module
from viki_agent.llm.connections import ConnectionError, parse_connection
from viki_agent.llm.gateway import MAX_BODY, Gateway, _events, read_body
from viki_agent.llm.protocols import event


class Stream(httpx2.AsyncByteStream):
    def __init__(self, chunks: list[bytes], *, block: bool = False) -> None:
        self.chunks = chunks
        self.block = block
        self.entered = asyncio.Event()
        self.closed = asyncio.Event()

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            yield chunk
        self.entered.set()
        if self.block:
            await asyncio.Event().wait()

    async def aclose(self) -> None:
        await checkpoint()
        self.closed.set()


def scope(runtime: Any, operation: str = "messages", *, spec: str = "2.3") -> dict[str, Any]:
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": spec},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": f"/api/llm/runtime/{runtime.id}/v1/{operation}",
        "query_string": b"",
        "root_path": "",
        "server": ("127.0.0.1", 8765),
        "client": ("127.0.0.1", 1),
        "headers": [(b"authorization", f"Bearer {runtime.token}".encode())],
    }


@pytest.mark.parametrize("operation", ["messages", "messages/count_tokens"])
async def test_revoke_owns_partial_body_and_releases_admission(operation: str) -> None:
    gateway = Gateway()
    runtime = gateway.issue(connection())
    entered = asyncio.Event()

    async def receive() -> dict[str, Any]:
        entered.set()
        await asyncio.Event().wait()
        return {}

    async def send(message: Any) -> None:
        pass

    task = asyncio.create_task(
        Starlette(routes=gateway.routes())(scope(runtime, operation), receive, send)
    )
    await entered.wait()
    assert gateway._requests
    runtime.revoke()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 0.05)
    assert not gateway._requests


@pytest.mark.parametrize("spec", ["2.3", "2.4"])
async def test_response_closes_paused_upstream_before_return(spec: str) -> None:
    upstream = Stream([sse({"choices": [{"delta": {"content": "hello"}}]})], block=True)
    gateway = Gateway(
        transport=httpx2.MockTransport(
            lambda _: httpx2.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=upstream,
            )
        )
    )
    runtime = gateway.issue(connection())
    blocked = asyncio.Event()
    disconnected = asyncio.Event()
    first = True

    async def receive() -> dict[str, Any]:
        nonlocal first
        if first:
            first = False
            return {"type": "http.request", "body": b'{"messages":[]}', "more_body": False}
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(message: Any) -> None:
        if message["type"] == "http.response.body" and b"content_block_delta" in message.get(
            "body", b""
        ):
            blocked.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(
        Starlette(routes=gateway.routes())(scope(runtime, spec=spec), receive, send)
    )
    await blocked.wait()
    if spec == "2.3":
        disconnected.set()
        await asyncio.wait_for(task, 0.05)
    else:
        runtime.revoke()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 0.05)
    assert upstream.closed.is_set()
    assert not gateway._requests and gateway._capacity._value == 8
    assert runtime.failure_code is not None


@pytest.mark.parametrize("operation", ["messages", "messages/count_tokens"])
async def test_body_intake_has_deadline_and_bounded_admission(
    monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    gateway = Gateway()
    runtime = gateway.issue(connection())
    release = gateway.admit(runtime.id)
    monkeypatch.setattr(gateway_module, "MAX_REQUESTS", 1)
    reads = 0
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        nonlocal reads
        reads += 1
        await asyncio.Event().wait()
        return {}

    async def send(message: Any) -> None:
        sent.append(message)

    app = Starlette(routes=gateway.routes())
    await app(scope(runtime, operation), receive, send)
    assert reads == 0 and sent[0]["status"] == 503
    release()
    sent.clear()
    monkeypatch.setattr(gateway_module, "INTAKE_S", 0.001)
    await app(scope(runtime, operation), receive, send)
    assert reads == 1 and sent[0]["status"] == 400
    assert not gateway._requests and gateway._admitted == 0


@pytest.mark.parametrize("reply", [b"", event("message_stop", {}), 500])
async def test_failed_runtime_latches_across_sequential_and_queued_requests(
    reply: bytes | int,
) -> None:
    calls = 0
    entered = asyncio.Event()
    release = asyncio.Event()

    async def respond(_: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return httpx2.Response(
            reply if isinstance(reply, int) else 200,
            headers={"content-type": "text/event-stream"},
            content=b"" if isinstance(reply, int) else reply,
        )

    gateway = Gateway(transport=httpx2.MockTransport(respond))
    runtime = gateway.issue(connection("anthropic"))
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=Starlette(routes=gateway.routes())),
        base_url="http://test",
    ) as client:
        path = f"/api/llm/runtime/{runtime.id}/v1/messages"
        headers = {"authorization": f"Bearer {runtime.token}"}
        one = asyncio.create_task(client.post(path, headers=headers, json={"messages": []}))
        await entered.wait()
        two = asyncio.create_task(client.post(path, headers=headers, json={"messages": []}))
        release.set()
        await asyncio.gather(one, two)
        await client.get(path.replace("messages", "models"), headers=headers)
        await client.post(path + "/count_tokens", headers=headers, json={})
        await client.post(path, headers=headers, json={"messages": []})
    assert calls == 1 and runtime.failure_code is not None


async def test_successful_tool_continuations_remain_available_but_warmup_failure_latches() -> None:
    calls = 0

    def respond(_: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        calls += 1
        if calls >= 3:
            return httpx2.Response(500)
        return httpx2.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=sse(
                {
                    "choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}],
                }
            ),
        )

    gateway = Gateway(transport=httpx2.MockTransport(respond))
    runtime = gateway.issue(connection())
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=Starlette(routes=gateway.routes())),
        base_url="http://test",
    ) as client:
        path = f"/api/llm/runtime/{runtime.id}/v1/messages"
        headers = {"authorization": f"Bearer {runtime.token}"}
        for _ in range(2):
            response = await client.post(path, headers=headers, json={"messages": []})
            assert "message_stop" in response.text and runtime.failure_code is None
        await client.post(path, headers=headers, json={"messages": []})
        await client.post(path, headers=headers, json={"messages": []})
        assert calls == 3 and runtime.failure_code == "upstream_failed"
        fresh = gateway.issue(connection())
        await client.post(
            f"/api/llm/runtime/{fresh.id}/v1/messages",
            headers={"authorization": f"Bearer {fresh.token}"},
            json={"messages": []},
        )
        assert calls == 4


async def test_cancelled_inference_latches_before_queued_request_enters_upstream() -> None:
    upstream = Stream([], block=True)
    calls = 0

    def respond(_: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        calls += 1
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, stream=upstream)

    gateway = Gateway(transport=httpx2.MockTransport(respond))
    runtime = gateway.issue(connection())
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=Starlette(routes=gateway.routes())),
        base_url="http://test",
    ) as client:
        path = f"/api/llm/runtime/{runtime.id}/v1/messages"
        headers = {"authorization": f"Bearer {runtime.token}"}
        one = asyncio.create_task(client.post(path, headers=headers, json={"messages": []}))
        await upstream.entered.wait()
        two = asyncio.create_task(client.post(path, headers=headers, json={"messages": []}))
        one.cancel()
        with pytest.raises(asyncio.CancelledError):
            await one
        await two
    assert calls == 1 and runtime.failure_code == "incomplete_response"
    assert upstream.closed.is_set() and gateway._admitted == 0


async def test_sse_handles_split_line_endings_bom_and_multidata() -> None:
    stream = Stream(
        [b"\xef\xbb\xbf: comment\r", b'\ndata: {\rdata: "ok": true}\r', b"\rdata: [DONE]\n\n"]
    )
    response = httpx2.Response(200, stream=stream)
    assert [row async for row in _events(response)] == [{"ok": True}]


@pytest.mark.parametrize("ending", [b"\n", b"\r\n", b"\r"])
async def test_sse_large_fragmented_events_remain_bounded(
    monkeypatch: pytest.MonkeyPatch, ending: bytes
) -> None:
    payload = b"data: " + json.dumps({"text": "x" * 65536}).encode() + ending * 2
    response = httpx2.Response(
        200,
        stream=Stream([payload[offset : offset + 257] for offset in range(0, len(payload), 257)]),
    )
    assert [row async for row in _events(response)] == [{"text": "x" * 65536}]
    monkeypatch.setattr(gateway_module, "MAX_EVENT", 16)
    with pytest.raises(ConnectionError, match="response_too_large"):
        _ = [row async for row in _events(httpx2.Response(200, content=payload))]
    monkeypatch.setattr(gateway_module, "MAX_EVENT", 65536 * 2)
    monkeypatch.setattr(gateway_module, "MAX_REPLY", 16)
    with pytest.raises(ConnectionError, match="response_too_large"):
        _ = [row async for row in _events(httpx2.Response(200, content=payload))]


@pytest.mark.parametrize(
    "frames",
    [
        [("message_stop", {})],
        [
            ("message_start", {"message": {}}),
            ("message_delta", {"delta": {"stop_reason": "end_turn"}}),
            ("message_stop", {}),
        ],
        [("message_delta", {"delta": {"stop_reason": "end_turn"}}), ("message_stop", {})],
        [("content_block_delta", {"index": 0, "delta": {"type": "text_delta", "text": "bad"}})],
    ],
)
async def test_native_messages_reject_invalid_lifecycle(
    frames: list[tuple[str, dict[str, Any]]],
) -> None:
    gateway = Gateway(
        transport=httpx2.MockTransport(
            lambda _: httpx2.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=b"".join(event(kind, data) for kind, data in frames),
            )
        )
    )
    with pytest.raises(ConnectionError):
        await gateway.test(connection("anthropic"))


async def test_compressed_sse_parser_yields_to_other_tasks() -> None:
    payload = b":x\n\n" * (128 * 1024) + sse({"ok": True})
    stream = Stream([gzip.compress(payload)])
    response = httpx2.Response(200, headers={"content-encoding": "gzip"}, stream=stream)
    ran = asyncio.Event()
    asyncio.get_running_loop().call_soon(ran.set)
    async with aclosing(_events(response)) as rows:
        assert await anext(rows) == {"ok": True}
        assert ran.is_set()


async def test_body_allows_twenty_mib_attachment_and_rejects_one_byte_over_limit() -> None:
    data = json.dumps(
        {
            "messages": [{"content": base64.b64encode(b"x" * (20 * 1024 * 1024)).decode()}],
            "system": "context" * 1000,
        }
    ).encode()

    async def read(data: bytes) -> dict[str, Any]:
        async def receive() -> dict[str, Any]:
            return {"type": "http.request", "body": data, "more_body": False}

        return await read_body(Request({"type": "http"}, receive))

    assert await read(data)
    with pytest.raises(ConnectionError, match="request_too_large"):
        await read(b" " * (MAX_BODY + 1))


async def test_discovery_rejects_models_that_cannot_be_saved() -> None:
    invalid = ["viki-llm/reserved", "bad\x7f", "\u3042" * 400, "\ud800"]
    gateway = Gateway(
        transport=httpx2.MockTransport(
            lambda _: httpx2.Response(
                200,
                content=json.dumps(
                    {"data": [{"id": name} for name in ["valid/model", *invalid]]}
                ).encode(),
            )
        )
    )
    assert await gateway.models(connection()) == ["valid/model"]


@pytest.mark.parametrize("codepoint", [127, 128, 159])
@pytest.mark.parametrize("operation", ["save", "discovery"])
async def test_model_control_boundaries_match_saved_and_discovered_models(
    codepoint: int, operation: str
) -> None:
    model = "model-" + chr(codepoint)
    if operation == "save":
        with pytest.raises(ConnectionError):
            parse_connection({**FORM, "model": model})
    else:
        gateway = Gateway(
            transport=httpx2.MockTransport(
                lambda _: httpx2.Response(
                    200,
                    json={"data": [{"id": "valid/model"}, {"id": model}]},
                )
            )
        )
        assert await gateway.models(connection()) == ["valid/model"]


@pytest.mark.parametrize("suffix", ["~", "\u00a1", "\u200d", "e\u0301"])
async def test_model_control_boundary_preserves_valid_opaque_identifiers(suffix: str) -> None:
    model = "model-" + suffix
    assert parse_connection({**FORM, "model": model}).model == model
    gateway = Gateway(
        transport=httpx2.MockTransport(
            lambda _: httpx2.Response(
                200,
                json={"data": [{"id": model}]},
            )
        )
    )
    assert await gateway.models(connection()) == [model]
