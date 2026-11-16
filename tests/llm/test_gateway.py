from __future__ import annotations

import json
from typing import Any

import httpx2
import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from tests.llm.test_connections import BASE, FORM, TOKEN
from viki_agent.llm.connections import ApiMode, Connection, ConnectionError, parse_connection
from viki_agent.llm.gateway import Gateway
from viki_agent.llm.protocols import ReplyStream, event, request_body


def connection(mode: str = "chat_completions") -> Connection:
    return parse_connection({**FORM, "api_mode": mode, "base_url": "https://models.example/v1"})


def sse(*rows: dict[str, Any]) -> bytes:
    return b"".join(b"data: " + json.dumps(row).encode() + b"\n\n" for row in rows)


def decoded(data: bytes) -> list[dict[str, Any]]:
    return [json.loads(line[6:]) for line in data.splitlines() if line.startswith(b"data: ")]


@pytest.mark.parametrize(
    "mode,path",
    [
        ("chat_completions", "/v1/chat/completions"),
        ("responses", "/v1/responses"),
        ("anthropic", "/v1/messages"),
    ],
)
async def test_protocol_streams_text_and_tools_to_claude(mode: str, path: str) -> None:
    seen: list[httpx2.Request] = []
    chat = [
        {"choices": [{"delta": {"content": "Hello"}}]},
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_a",
                                "function": {"name": "Read", "arguments": '{"file_path":'},
                            }
                        ]
                    }
                }
            ]
        },
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [{"index": 0, "function": {"arguments": '"example.md"}'}}]
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        },
        {"choices": [], "usage": {"prompt_tokens": 12, "completion_tokens": 6}},
    ]
    responses = [
        {"type": "response.output_text.delta", "delta": "Hello"},
        {
            "type": "response.output_item.added",
            "output_index": 1,
            "item": {"type": "function_call", "call_id": "call_a", "name": "Read"},
        },
        {
            "type": "response.function_call_arguments.delta",
            "output_index": 1,
            "delta": '{"file_path":"example.md"}',
        },
        {
            "type": "response.completed",
            "response": {"usage": {"input_tokens": 12, "output_tokens": 6}},
        },
    ]
    native = (
        event(
            "message_start",
            {
                "message": {
                    "id": "msg_a",
                    "type": "message",
                    "role": "assistant",
                    "content": [],
                    "model": "example/model",
                    "stop_reason": None,
                    "usage": {"input_tokens": 1, "output_tokens": 0},
                }
            },
        )
        + event("message_delta", {"delta": {"stop_reason": "end_turn"}})
        + event("message_stop", {})
    )

    def respond(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        body = native if mode == "anthropic" else sse(*(responses if mode == "responses" else chat))
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=body)

    gateway = Gateway(transport=httpx2.MockTransport(respond))
    body = {
        "messages": [{"role": "user", "content": "synthetic"}],
        "tools": [{"name": "Read", "input_schema": {"type": "object"}}],
        "max_tokens": 128,
    }
    output = decoded(b"".join([part async for part in gateway.generate(connection(mode), body)]))
    assert seen[0].url.path == path and len(seen) == 1
    assert seen[0].headers["authorization"] == f"Bearer {FORM['api_key']}"
    assert json.loads(seen[0].content)["model"] == "example/model"
    assert (
        json.loads(seen[0].content)[
            "max_tokens"
            if mode == "anthropic"
            else "max_output_tokens"
            if mode == "responses"
            else "max_completion_tokens"
        ]
        == 128
    )
    assert output[-1]["type"] == "message_stop"
    if mode != "anthropic":
        assert any(row.get("delta", {}).get("text") == "Hello" for row in output)
        tool = next(
            row["content_block"]
            for row in output
            if row.get("content_block", {}).get("type") == "tool_use"
        )
        assert tool["name"] == "Read" and tool["id"] == "call_a"
        assert output[-2]["delta"]["stop_reason"] == "tool_use"
        assert output[-2]["usage"] == {"input_tokens": 12, "output_tokens": 6}


@pytest.mark.parametrize("mode", ["chat_completions", "responses"])
def test_tool_results_images_and_ids_survive_request_conversion(mode: ApiMode) -> None:
    body = {
        "system": [{"type": "text", "text": "Instructions"}],
        "messages": [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "call_original",
                        "name": "Read",
                        "input": {"file_path": "example.md"},
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "call_original",
                        "content": "synthetic result",
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"},
                    }
                ],
            },
        ],
    }
    result = request_body(body, mode, "local-model")
    assert "call_original" in json.dumps(result) and "synthetic result" in json.dumps(result)
    assert "data:image/png;base64,AAAA" in json.dumps(result)
    if mode == "responses":
        assert result["store"] is False


@pytest.mark.parametrize("status", [302, 401, 429, 500])
async def test_failure_never_redirects_retries_or_exposes_upstream_body(status: int) -> None:
    calls = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        calls.append(request)
        return httpx2.Response(
            status,
            headers={"location": "https://elsewhere.example/secret"},
            text="echo synthetic-key-only",
        )

    gateway = Gateway(transport=httpx2.MockTransport(respond))
    with pytest.raises(ConnectionError) as error:
        _ = [part async for part in gateway.generate(connection(), {"messages": []})]
    assert "synthetic-key-only" not in str(error.value)
    assert len(calls) == 1


async def test_truncated_stream_and_malformed_tool_input_are_errors() -> None:
    for reply in [
        sse({"choices": [{"delta": {"content": "unfinished"}}]}),
        sse(
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "a",
                                    "function": {"name": "Read", "arguments": "bad-json"},
                                }
                            ]
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            }
        ),
    ]:
        gateway = Gateway(
            transport=httpx2.MockTransport(
                lambda request, reply=reply: httpx2.Response(
                    200, headers={"content-type": "text/event-stream"}, content=reply
                )
            )
        )
        with pytest.raises(ConnectionError):
            _ = [part async for part in gateway.generate(connection(), {"messages": []})]


def test_runtime_capability_is_separate_from_window_token_and_revoked() -> None:
    calls = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        calls.append(request)
        return httpx2.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=sse({"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}),
        )

    gateway = Gateway(transport=httpx2.MockTransport(respond))
    route = gateway.issue(connection())
    assert FORM["api_key"] not in json.dumps(route.environment())
    assert (
        route.token not in repr(route)
        and route.token not in route.environment()["ANTHROPIC_BASE_URL"]
    )
    app = Starlette(routes=gateway.routes())
    path = f"/api/llm/runtime/{route.id}/v1/messages"
    with TestClient(app, base_url=BASE) as client:
        body = {"messages": [{"role": "user", "content": "synthetic"}]}
        assert client.post(path, json=body).status_code == 401
        assert (
            client.post(path, headers={"authorization": f"Bearer {TOKEN}"}, json=body).status_code
            == 401
        )
        headers = {"authorization": f"Bearer {route.token}"}
        assert client.post(path, headers={**headers, "origin": BASE}, json=body).status_code == 403
        assert not calls
        response = client.post(path, headers=headers, json=body)
        assert response.status_code == 200 and "message_stop" in response.text
        route.revoke()
        assert client.post(path, headers=headers, json=body).status_code == 401
        assert len(calls) == 1


def test_responses_encrypted_reasoning_survives_a_tool_round_trip() -> None:
    adapter = ReplyStream("example/model", "responses")
    reasoning = {
        "type": "reasoning",
        "id": "rs_synthetic",
        "encrypted_content": "synthetic-opaque-data",
        "summary": [],
    }
    output = list(adapter.accept({"type": "response.output_item.done", "item": reasoning}))
    output.extend(adapter.accept({"type": "response.completed", "response": {}}))
    output.extend(adapter.finish())
    rows = decoded(b"".join(output))
    signature = next(
        row["delta"]["signature"]
        for row in rows
        if row.get("delta", {}).get("type") == "signature_delta"
    )
    converted = request_body(
        {
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "", "signature": signature},
                        {"type": "tool_use", "id": "call_a", "name": "Read", "input": {}},
                    ],
                }
            ]
        },
        "responses",
        "example/model",
    )
    assert converted["input"][0] == reasoning
    assert converted["include"] == ["reasoning.encrypted_content"]


@pytest.mark.parametrize("mode", ["chat_completions", "responses"])
def test_image_tool_results_are_forwarded_without_losing_the_call_id(mode: ApiMode) -> None:
    body = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "call_image",
                        "content": [
                            {"type": "text", "text": "A tool image"},
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/png",
                                    "data": "AAAA",
                                },
                            },
                        ],
                    }
                ],
            }
        ]
    }
    converted = request_body(body, mode, "example/model")
    assert "call_image" in json.dumps(converted)
    assert "data:image/png;base64,AAAA" in json.dumps(converted)


async def test_model_discovery_sends_no_inference_and_no_cookies() -> None:
    calls = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        calls.append(request)
        assert request.method == "GET" and request.url.path == "/v1/models"
        assert "cookie" not in request.headers
        return httpx2.Response(
            200,
            json={"data": [{"id": "example/model"}, {"id": "other-model"}]},
            headers={"set-cookie": "ignored=value"},
        )

    gateway = Gateway(transport=httpx2.MockTransport(respond))
    assert await gateway.models(connection()) == ["example/model", "other-model"]
    assert await gateway.models(connection()) == ["example/model", "other-model"]
    assert len(calls) == 2


def test_a_truncated_tool_turn_cannot_execute_an_assembled_call() -> None:
    adapter = ReplyStream("example/model", "chat_completions")
    list(
        adapter.accept(
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_incomplete",
                                    "function": {"name": "Read", "arguments": "{}"},
                                }
                            ]
                        },
                        "finish_reason": "length",
                    }
                ]
            }
        )
    )
    with pytest.raises(ConnectionError, match="incomplete_response"):
        list(adapter.finish())


async def test_invalid_url_errors_do_not_expose_endpoint_details() -> None:
    def invalid(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.InvalidURL("synthetic-private-path")

    gateway = Gateway(transport=httpx2.MockTransport(invalid))
    for operation in ("models", "generate"):
        with pytest.raises(ConnectionError) as error:
            if operation == "models":
                await gateway.models(connection())
            else:
                _ = [part async for part in gateway.generate(connection(), {"messages": []})]
        assert "synthetic-private-path" not in str(error.value)
