"""Translate Messages requests and streaming replies without executing any model tool."""

from __future__ import annotations

import base64
import json
import uuid
from collections.abc import Iterator
from typing import Any

from viki_agent.llm.connections import ApiMode, ConnectionError


def event(kind: str, data: dict[str, Any]) -> bytes:
    return (
        f"event: {kind}\ndata: {json.dumps({'type': kind, **data}, ensure_ascii=True)}\n\n".encode()
    )


def _parts(content: Any, *, responses: bool = False) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"type": "input_text" if responses else "text", "text": content}]
    if not isinstance(content, list):
        raise ConnectionError("unsupported_request")
    result = []
    for part in content:
        if not isinstance(part, dict):
            raise ConnectionError("unsupported_request")
        kind = part.get("type")
        if kind == "text" and isinstance(part.get("text"), str):
            result.append({"type": "input_text" if responses else "text", "text": part["text"]})
        elif kind == "image":
            source = part.get("source", {})
            if source.get("type") == "base64":
                url = f"data:{source.get('media_type')};base64,{source.get('data')}"
            elif source.get("type") == "url":
                url = source.get("url")
            else:
                raise ConnectionError("unsupported_request")
            result.append(
                {"type": "input_image", "image_url": url}
                if responses
                else {"type": "image_url", "image_url": {"url": url}}
            )
        elif kind not in ("tool_use", "tool_result", "thinking", "redacted_thinking"):
            raise ConnectionError("unsupported_request")
    return result


def _text(content: Any) -> str:
    parts = _parts(content)
    if any(part["type"] != "text" for part in parts):
        raise ConnectionError("unsupported_request")
    return "\n".join(part["text"] for part in parts)


def request_body(body: dict[str, Any], mode: ApiMode, model: str) -> dict[str, Any]:
    if mode == "anthropic":
        return {**body, "model": model}
    responses = mode == "responses"
    messages = body.get("messages")
    if not isinstance(messages, list):
        raise ConnectionError("unsupported_request")
    converted: list[dict[str, Any]] = []
    if body.get("system"):
        converted.append({"role": "system", "content": _text(body["system"])})
    for message in messages:
        if not isinstance(message, dict) or message.get("role") not in (
            "user",
            "assistant",
            "system",
        ):
            raise ConnectionError("unsupported_request")
        role, content = message["role"], message.get("content", "")
        parts = _parts(content, responses=responses)
        blocks = content if isinstance(content, list) else []
        calls = [part for part in blocks if part.get("type") == "tool_use"]
        results = [part for part in blocks if part.get("type") == "tool_result"]
        thoughts = [part for part in blocks if part.get("type") == "thinking"]
        images = []
        for result in results:
            content_parts = _parts(result.get("content", ""), responses=responses)
            output = "\n".join(part["text"] for part in content_parts if "text" in part)
            if result.get("is_error"):
                output = "Tool error: " + output
            has_images = any("image_url" in part for part in content_parts)
            if responses and has_images and result.get("is_error"):
                content_parts.insert(0, {"type": "input_text", "text": "Tool error: "})
            converted.append(
                {
                    "type": "function_call_output",
                    "call_id": result["tool_use_id"],
                    "output": content_parts if has_images else output,
                }
                if responses
                else {"role": "tool", "tool_call_id": result["tool_use_id"], "content": output}
            )
            if not responses and has_images:
                images.extend(part for part in content_parts if part["type"] == "image_url")
        if responses:
            # Keep opaque reasoning, text and calls in their native block order.
            row = None
            for block in (
                blocks if isinstance(content, list) else [{"type": "text", "text": content}]
            ):
                kind = block.get("type")
                if kind == "tool_use":
                    row = None
                    converted.append(
                        {
                            "type": "function_call",
                            "call_id": block["id"],
                            "name": block["name"],
                            "arguments": json.dumps(block.get("input", {})),
                        }
                    )
                elif kind == "thinking":
                    signature = block.get("signature", "")
                    if isinstance(signature, str) and signature.startswith("viki-responses:"):
                        try:
                            item = json.loads(
                                base64.b64decode(signature.partition(":")[2], validate=True)
                            )
                            if not isinstance(item, dict) or item.get("type") != "reasoning":
                                raise ValueError()
                        except ValueError, TypeError, RecursionError:
                            raise ConnectionError("unsupported_request") from None
                        row = None
                        converted.append(item)
                elif kind in ("text", "image"):
                    content_parts = _parts([block], responses=True)
                    if role == "assistant":
                        for part in content_parts:
                            if part["type"] != "input_text":
                                raise ConnectionError("unsupported_request")
                            part.update(type="output_text", annotations=[])
                    if row is None:
                        row = {"role": role, "content": []}
                        converted.append(row)
                    row["content"].extend(content_parts)
        elif parts or calls:
            row = {"role": role, "content": parts or None}
            if role == "assistant" and thoughts:
                row["reasoning_content"] = "".join(
                    str(part.get("thinking", "")) for part in thoughts
                )
            if calls:
                row["tool_calls"] = [
                    {
                        "id": call["id"],
                        "type": "function",
                        "function": {
                            "name": call["name"],
                            "arguments": json.dumps(call.get("input", {})),
                        },
                    }
                    for call in calls
                ]
            converted.append(row)
        if images:
            converted.append({"role": "user", "content": images})
    tools = []
    for tool in body.get("tools", []):
        if (
            not isinstance(tool, dict)
            or not isinstance(tool.get("name"), str)
            or "input_schema" not in tool
        ):
            raise ConnectionError("unsupported_request")
        function = {
            "name": tool["name"],
            "description": tool.get("description", ""),
            "parameters": tool["input_schema"],
        }
        tools.append(
            {"type": "function", "strict": False, **function}
            if responses
            else {"type": "function", "function": function}
        )
    result = {"model": model, "input" if responses else "messages": converted, "stream": True}
    if responses:
        result["store"] = False
        result["include"] = ["reasoning.encrypted_content"]
    else:
        result["stream_options"] = {"include_usage": True}
    limit = body.get("max_tokens")
    if type(limit) is int and 0 < limit <= 1_000_000:
        result["max_output_tokens" if responses else "max_completion_tokens"] = limit
    if tools:
        result["tools"] = tools
    effort = (body.get("output_config") or {}).get("effort")
    if effort in ("low", "medium", "high", "xhigh", "max"):
        if responses:
            result["reasoning"] = {"effort": effort}
        else:
            result["reasoning_effort"] = effort
    choice = body.get("tool_choice", {})
    if isinstance(choice, dict):
        kind = choice.get("type")
        if kind in ("auto", "any", "none"):
            result["tool_choice"] = "required" if kind == "any" else kind
        elif kind == "tool" and isinstance(choice.get("name"), str):
            result["tool_choice"] = (
                {"type": "function", "name": choice["name"]}
                if responses
                else {"type": "function", "function": {"name": choice["name"]}}
            )
    return result


class ReplyStream:
    """Stream ordered content, holding tools until the completed turn validates every call."""

    def __init__(self, model: str, mode: ApiMode) -> None:
        self.model, self.mode = model, mode
        self.text_open = False
        self.block_index = 0
        self.calls: dict[int, dict[str, Any]] = {}
        self.thinking: list[str] = []
        self.response_items: dict[int, dict[str, Any]] = {}
        self.response_cursor = 0
        self.input_tokens = self.output_tokens = 0
        self.complete = False
        self.reason = "end_turn"

    def start(self) -> bytes:
        return event(
            "message_start",
            {
                "message": {
                    "id": "msg_" + uuid.uuid4().hex,
                    "type": "message",
                    "role": "assistant",
                    "model": self.model,
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                }
            },
        )

    def _text(self, value: Any) -> Iterator[bytes]:
        if not isinstance(value, str) or not value:
            return
        if not self.text_open:
            self.text_open = True
            yield event(
                "content_block_start",
                {"index": self.block_index, "content_block": {"type": "text", "text": ""}},
            )
        yield event(
            "content_block_delta",
            {"index": self.block_index, "delta": {"type": "text_delta", "text": value}},
        )

    def _close_text(self) -> Iterator[bytes]:
        if self.text_open:
            yield event("content_block_stop", {"index": self.block_index})
            self.text_open = False
            self.block_index += 1

    def _thought(self, text: str, signature: str) -> Iterator[bytes]:
        yield from self._close_text()
        yield event(
            "content_block_start",
            {"index": self.block_index, "content_block": {"type": "thinking", "thinking": ""}},
        )
        if text:
            yield event(
                "content_block_delta",
                {"index": self.block_index, "delta": {"type": "thinking_delta", "thinking": text}},
            )
        yield event(
            "content_block_delta",
            {
                "index": self.block_index,
                "delta": {"type": "signature_delta", "signature": signature},
            },
        )
        yield event("content_block_stop", {"index": self.block_index})
        self.block_index += 1

    def _tool(self, call: dict[str, Any]) -> Iterator[bytes]:
        yield from self._close_text()
        yield event(
            "content_block_start",
            {
                "index": self.block_index,
                "content_block": {
                    "type": "tool_use",
                    "id": call["id"],
                    "name": call["name"],
                    "input": {},
                },
            },
        )
        yield event(
            "content_block_delta",
            {
                "index": self.block_index,
                "delta": {
                    "type": "input_json_delta",
                    "partial_json": json.dumps(call["input"]),
                },
            },
        )
        yield event("content_block_stop", {"index": self.block_index})
        self.block_index += 1

    def _flush_responses(self, calls: dict[int, dict[str, Any]] | None = None) -> Iterator[bytes]:
        indexes = (
            sorted(self.response_items)
            if calls is not None
            else range(self.response_cursor, self.response_cursor + len(self.response_items))
        )
        for index in indexes:
            if index not in self.response_items:
                return
            item = self.response_items[index]
            kind = item["type"]
            if kind == "function_call":
                # No tool or later item can escape until the whole turn is validated.
                if calls is None:
                    return
                yield from self._tool(calls[index])
            elif kind == "reasoning":
                if "item" not in item:
                    if calls is not None:
                        raise ConnectionError("invalid_response")
                    return
                thought = item["item"]
                signature = "viki-responses:" + base64.b64encode(
                    json.dumps(thought).encode()
                ).decode("ascii")
                text = "".join(part.get("text", "") for part in thought.get("summary", []))
                yield from self._thought(text, signature)
            else:
                yield from self._text("".join(item["text"]))
                item["text"].clear()
                if calls is None and not item.get("done"):
                    return
                yield from self._close_text()
            del self.response_items[index]
            self.response_cursor = index + 1

    def accept(self, row: dict[str, Any]) -> Iterator[bytes]:
        if self.mode == "responses":
            kind = row.get("type")
            index = row.get("output_index", self.response_cursor)
            if kind in ("response.output_text.delta", "response.refusal.delta"):
                item = self.response_items.setdefault(index, {"type": "message", "text": []})
                item["text"].append(row.get("delta") or "")
            elif kind in ("response.output_item.added", "response.output_item.done"):
                item = row.get("item", {})
                item_kind = item.get("type")
                if item_kind == "function_call":
                    call = self.calls.setdefault(
                        index,
                        {
                            "id": item.get("call_id", ""),
                            "name": [item.get("name", "")],
                            "arguments": [],
                        },
                    )
                    if "arguments" in item:
                        call["arguments"] = [item["arguments"]]
                    self.response_items[index] = {"type": "function_call"}
                elif item_kind == "reasoning":
                    state = self.response_items.setdefault(index, {"type": "reasoning"})
                    if kind == "response.output_item.done":
                        state["item"] = item
                elif item_kind == "message":
                    state = self.response_items.setdefault(index, {"type": "message", "text": []})
                    state["done"] = kind == "response.output_item.done"
            elif kind == "response.function_call_arguments.delta":
                self.calls[index]["arguments"].append(row["delta"])
            elif kind == "response.function_call_arguments.done":
                self.calls[index]["arguments"] = [row["arguments"]]
            elif kind in ("response.completed", "response.incomplete"):
                response = row.get("response", {})
                usage = response.get("usage", {})
                self.input_tokens = _tokens(usage.get("input_tokens"))
                self.output_tokens = _tokens(usage.get("output_tokens"))
                self.complete = True
                if kind == "response.incomplete":
                    if response.get("incomplete_details", {}).get("reason") != "max_output_tokens":
                        raise ConnectionError("upstream_failed")
                    self.reason = "max_tokens"
            elif kind in ("error", "response.failed"):
                raise ConnectionError("upstream_failed")
            yield from self._flush_responses()
            return
        usage = row.get("usage") or {}
        self.input_tokens = max(self.input_tokens, _tokens(usage.get("prompt_tokens")))
        self.output_tokens = max(self.output_tokens, _tokens(usage.get("completion_tokens")))
        choices = row.get("choices", [])
        if not choices:
            return
        choice = choices[0]
        delta = choice.get("delta") or {}
        if delta.get("reasoning_content"):
            self.thinking.append(delta["reasoning_content"])
        yield from self._text(delta.get("content") or delta.get("refusal"))
        for item in delta.get("tool_calls", []):
            index = item["index"]
            call = self.calls.setdefault(index, {"id": "", "name": [], "arguments": []})
            if item.get("id"):
                call["id"] = item["id"]
            function = item.get("function") or {}
            if function.get("name"):
                call["name"].append(function["name"])
            if function.get("arguments"):
                call["arguments"].append(function["arguments"])
        reason = choice.get("finish_reason")
        if reason is not None:
            self.complete = True
            if reason == "length":
                self.reason = "max_tokens"
            elif reason not in ("stop", "tool_calls", "function_call"):
                raise ConnectionError("upstream_failed")

    def finish(self) -> Iterator[bytes]:
        if not self.complete or self.reason == "max_tokens" and self.calls:
            raise ConnectionError("incomplete_response")
        validated = {}
        for index, call in self.calls.items():
            if not isinstance(call["id"], str) or not call["id"]:
                raise ConnectionError("invalid_response")
            try:
                name = "".join(call["name"])
                arguments = json.loads("".join(call["arguments"]) or "{}")
            except ValueError, TypeError:
                raise ConnectionError("invalid_response") from None
            if not name or not isinstance(arguments, dict):
                raise ConnectionError("invalid_response")
            validated[index] = {"id": call["id"], "name": name, "input": arguments}
        if self.mode == "responses":
            yield from self._flush_responses(validated)
        else:
            yield from self._close_text()
            if self.thinking:
                yield from self._thought("".join(self.thinking), "viki-chat")
            for call in validated.values():
                yield from self._tool(call)
        yield event(
            "message_delta",
            {
                "delta": {
                    "stop_reason": "tool_use" if self.calls else self.reason,
                    "stop_sequence": None,
                },
                "usage": {"input_tokens": self.input_tokens, "output_tokens": self.output_tokens},
            },
        )
        yield event("message_stop", {})


def _tokens(value: Any) -> int:
    return value if type(value) is int and 0 <= value <= 1_000_000_000 else 0
