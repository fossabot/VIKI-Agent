from __future__ import annotations

import base64
import json
from collections.abc import Iterator
from typing import Any

import pytest

from viki_agent.llm.connections import ApiMode, ConnectionError
from viki_agent.llm.protocols import ReplyStream, request_body

IMAGE = {
    "type": "image",
    "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"},
}


def blocks(events: list[bytes]) -> list[dict[str, Any]]:
    result: dict[int, dict[str, Any]] = {}
    for event in events:
        row = json.loads(event.split(b"data: ", 1)[1])
        if row["type"] == "content_block_start":
            result[row["index"]] = dict(row["content_block"])
        elif row["type"] == "content_block_delta":
            block = result[row["index"]]
            delta = row["delta"]
            for key in ("text", "thinking", "signature", "partial_json"):
                if key in delta:
                    block[key] = block.get(key, "") + delta[key]
    for block in result.values():
        if block["type"] == "tool_use":
            block["input"] = json.loads(block.pop("partial_json"))
    return list(result.values())


def tool(index: int, *, arguments: str = "", name: str = "Read") -> dict[str, Any]:
    return {
        "type": "response.output_item.added",
        "output_index": index,
        "item": {
            "type": "function_call",
            "call_id": f"call_{index}",
            "name": name,
            "arguments": arguments,
        },
    }


def thought(index: int) -> dict[str, Any]:
    return {
        "type": "reasoning",
        "id": f"rs_{index}",
        "encrypted_content": f"opaque-{index}",
        "summary": [{"type": "summary_text", "text": f"thought-{index}"}],
    }


@pytest.mark.parametrize("mode", ["chat_completions", "responses"])
def test_mixed_parallel_results_precede_ordinary_user_content(mode: ApiMode) -> None:
    converted = request_body(
        {
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "tool_use", "id": call, "name": "Read", "input": {}}
                        for call in ("a", "b")
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Continue"},
                        {"type": "tool_result", "tool_use_id": "b", "content": [IMAGE]},
                        IMAGE,
                        {"type": "tool_result", "tool_use_id": "a", "content": "result-a"},
                    ],
                },
            ]
        },
        mode,
        "synthetic",
    )
    rows = converted["input" if mode == "responses" else "messages"]
    if mode == "responses":
        assert [row.get("type", row.get("role")) for row in rows] == [
            "function_call",
            "function_call",
            "function_call_output",
            "function_call_output",
            "user",
        ]
        assert [row["call_id"] for row in rows[:4]] == ["a", "b", "b", "a"]
        assert rows[2]["output"] == [
            {"type": "input_image", "image_url": "data:image/png;base64,AAAA"}
        ]
    else:
        assert [row["role"] for row in rows] == ["assistant", "tool", "tool", "user", "user"]
        assert [row["tool_call_id"] for row in rows[1:3]] == ["b", "a"]
        assert rows[-1]["content"] == [
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}
        ]
    assert rows[-1 if mode == "responses" else -2]["content"][:1] == [
        {"type": "input_text" if mode == "responses" else "text", "text": "Continue"}
    ]


def test_responses_image_tool_error_retains_explicit_marker() -> None:
    converted = request_body(
        {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "a",
                            "is_error": True,
                            "content": [{"type": "text", "text": "failed"}, IMAGE],
                        }
                    ],
                }
            ]
        },
        "responses",
        "synthetic",
    )
    assert converted["input"] == [
        {
            "type": "function_call_output",
            "call_id": "a",
            "output": [
                {"type": "input_text", "text": "Tool error: "},
                {"type": "input_text", "text": "failed"},
                {"type": "input_image", "image_url": "data:image/png;base64,AAAA"},
            ],
        }
    ]


@pytest.mark.parametrize("mode", ["chat_completions", "responses", "anthropic"])
@pytest.mark.parametrize("effort", ["low", "medium", "high", "xhigh", "max"])
def test_native_effort_is_forwarded_without_mapping(mode: ApiMode, effort: str) -> None:
    body = {"messages": [], "output_config": {"effort": effort}}
    converted = request_body(body, mode, "synthetic")
    assert (
        converted["reasoning"]["effort"]
        if mode == "responses"
        else converted["reasoning_effort"]
        if mode == "chat_completions"
        else converted["output_config"]["effort"]
    ) == effort


class Counted(str):
    copied = 0

    def __add__(self, other: str) -> Counted:
        Counted.copied += len(self) + len(other)
        return Counted(str(self) + str(other))

    def __radd__(self, other: str) -> Counted:
        Counted.copied += len(self) + len(other)
        return Counted(str(other) + str(self))


class CountedItems(dict[int, dict[str, Any]]):
    visited = 0

    def __iter__(self) -> Iterator[int]:
        for index in super().__iter__():
            self.visited += 1
            yield index


@pytest.mark.parametrize("stage", ["prefix", "finish"])
def test_buffered_responses_items_are_not_repeatedly_scanned(stage: str) -> None:
    adapter = ReplyStream("synthetic", "responses")
    items = CountedItems()
    adapter.response_items = items
    assert list(adapter.accept(tool(0))) == []
    count = 64
    for index in range(count, 0, -1):
        assert (
            list(
                adapter.accept(
                    {
                        "type": "response.output_text.delta",
                        "output_index": index,
                        "delta": f"item-{index}",
                    }
                )
            )
            == []
        )
    prefix_visits = items.visited
    list(adapter.accept({"type": "response.completed", "response": {}}))
    items.visited = 0
    native = blocks(list(adapter.finish()))
    assert native[0]["id"] == "call_0"
    assert [block["text"] for block in native[1:]] == [
        f"item-{index}" for index in range(1, count + 1)
    ]
    visits = prefix_visits if stage == "prefix" else items.visited
    assert visits <= 2 * (count + 1)


def test_chat_tool_name_fragments_do_not_recopy_growing_prefixes() -> None:
    adapter = ReplyStream("synthetic", "chat_completions")
    name = "a" * 4096
    Counted.copied = 0
    for start in range(0, len(name), 32):
        list(
            adapter.accept(
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call_0",
                                        "function": {"name": Counted(name[start : start + 32])},
                                    }
                                ]
                            }
                        }
                    ]
                }
            )
        )
    list(adapter.accept({"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}))
    assert blocks(list(adapter.finish()))[0]["name"] == name
    assert Counted.copied <= 2 * len(name)


@pytest.mark.parametrize("mode", ["chat_completions", "responses"])
def test_fragment_assembly_does_not_recopy_growing_prefixes(mode: ApiMode) -> None:
    adapter = ReplyStream("synthetic", mode)
    arguments = '{"value":"' + "a" * 4096 + '"}'
    Counted.copied = 0
    if mode == "responses":
        list(adapter.accept(tool(0)))
    for start in range(0, len(arguments), 32):
        fragment = Counted(arguments[start : start + 32])
        row = (
            {"type": "response.function_call_arguments.delta", "output_index": 0, "delta": fragment}
            if mode == "responses"
            else {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_0",
                                    "function": {
                                        "name": "Read" if start == 0 else "",
                                        "arguments": fragment,
                                    },
                                }
                            ],
                            "reasoning_content": Counted("thinking"),
                        }
                    }
                ]
            }
        )
        list(adapter.accept(row))
    list(
        adapter.accept(
            {"type": "response.completed", "response": {}}
            if mode == "responses"
            else {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}
        )
    )
    assert next(block for block in blocks(list(adapter.finish())) if block["type"] == "tool_use")[
        "input"
    ] == {"value": "a" * 4096}
    assert Counted.copied <= 2 * (len(arguments) + 129 * len("thinking"))


def test_responses_done_arguments_replace_deltas() -> None:
    adapter = ReplyStream("synthetic", "responses")
    for row in [
        tool(0),
        {"type": "response.function_call_arguments.delta", "output_index": 0, "delta": "bad"},
        {
            "type": "response.function_call_arguments.done",
            "output_index": 0,
            "arguments": '{"authoritative":true}',
        },
        {"type": "response.completed", "response": {}},
    ]:
        list(adapter.accept(row))
    assert blocks(list(adapter.finish()))[0]["input"] == {"authoritative": True}


def test_responses_opaque_items_round_trip_in_output_index_order() -> None:
    adapter = ReplyStream("synthetic", "responses")
    events = []
    for row in [
        {"type": "response.output_item.done", "output_index": 0, "item": thought(0)},
        tool(1),
        {"type": "response.output_item.done", "output_index": 2, "item": thought(2)},
        tool(3),
        {"type": "response.completed", "response": {}},
    ]:
        events.extend(adapter.accept(row))
    events.extend(adapter.finish())
    native = blocks(events)
    assert [block["type"] for block in native] == ["thinking", "tool_use", "thinking", "tool_use"]
    converted = request_body(
        {"messages": [{"role": "assistant", "content": native}]}, "responses", "synthetic"
    )
    assert converted["input"] == [
        thought(0),
        {"type": "function_call", "call_id": "call_1", "name": "Read", "arguments": "{}"},
        thought(2),
        {"type": "function_call", "call_id": "call_3", "name": "Read", "arguments": "{}"},
    ]


def test_responses_request_preserves_native_reasoning_text_and_call_order() -> None:
    signature = "viki-responses:" + base64.b64encode(json.dumps(thought(1)).encode()).decode()
    content = [
        {"type": "text", "text": "before"},
        {"type": "thinking", "thinking": "", "signature": signature},
        {"type": "tool_use", "id": "a", "name": "Read", "input": {}},
        {"type": "text", "text": "after"},
    ]
    rows = request_body(
        {"messages": [{"role": "assistant", "content": content}]}, "responses", "synthetic"
    )["input"]
    assert [row.get("type", row.get("role")) for row in rows] == [
        "assistant",
        "reasoning",
        "function_call",
        "assistant",
    ]
    assert rows[0]["content"][0]["text"] == "before"
    assert rows[3]["content"][0]["text"] == "after"


def test_responses_streams_ordered_prefix_and_buffers_items_after_a_tool() -> None:
    adapter = ReplyStream("synthetic", "responses")
    events = list(
        adapter.accept(
            {
                "type": "response.output_item.done",
                "output_index": 0,
                "item": thought(0),
            }
        )
    )
    events.extend(
        adapter.accept(
            {
                "type": "response.output_text.delta",
                "output_index": 1,
                "delta": "before",
            }
        )
    )
    assert [block["type"] for block in blocks(events)] == ["thinking", "text"]
    assert blocks(events)[1]["text"] == "before"
    events.extend(
        adapter.accept(
            {"type": "response.output_item.done", "output_index": 1, "item": {"type": "message"}}
        )
    )
    assert list(adapter.accept(tool(2))) == []
    assert (
        list(
            adapter.accept(
                {"type": "response.output_text.delta", "output_index": 3, "delta": "after"}
            )
        )
        == []
    )
    assert (
        list(
            adapter.accept(
                {"type": "response.output_item.done", "output_index": 2, "item": tool(2)["item"]}
            )
        )
        == []
    )
    list(adapter.accept({"type": "response.completed", "response": {}}))
    events.extend(adapter.finish())
    assert [(block["type"], block.get("text", block.get("id"))) for block in blocks(events)] == [
        ("thinking", None),
        ("text", "before"),
        ("tool_use", "call_2"),
        ("text", "after"),
    ]


@pytest.mark.parametrize("mode", ["chat_completions", "responses"])
@pytest.mark.parametrize("bad", ["identity", "name", "json", "array"])
def test_all_calls_are_validated_before_any_tool_is_emitted(mode: ApiMode, bad: str) -> None:
    adapter = ReplyStream("synthetic", mode)
    calls = [tool(0)["item"], tool(1)["item"]]
    calls[1][{"identity": "call_id", "name": "name"}.get(bad, "arguments")] = {
        "identity": "",
        "name": "",
        "json": "broken",
        "array": "[]",
    }[bad]
    events = []
    for index, call in enumerate(calls):
        row = (
            {"type": "response.output_item.added", "output_index": index, "item": call}
            if mode == "responses"
            else {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": index,
                                    "id": call["call_id"],
                                    "function": {
                                        "name": call["name"],
                                        "arguments": call["arguments"],
                                    },
                                }
                            ]
                        }
                    }
                ]
            }
        )
        events.extend(adapter.accept(row))
    list(
        adapter.accept(
            {"type": "response.completed", "response": {}}
            if mode == "responses"
            else {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}
        )
    )
    with pytest.raises(ConnectionError, match="invalid_response"):
        events.extend(adapter.finish())
    assert not any(block["type"] == "tool_use" for block in blocks(events))


@pytest.mark.parametrize("termination", ["truncated", "max_tokens"])
def test_responses_cannot_emit_tools_from_an_incomplete_turn(termination: str) -> None:
    adapter = ReplyStream("synthetic", "responses")
    events = list(adapter.accept(tool(0)))
    events.extend(
        adapter.accept(
            {"type": "response.output_item.done", "output_index": 0, "item": tool(0)["item"]}
        )
    )
    if termination == "max_tokens":
        events.extend(
            adapter.accept(
                {
                    "type": "response.incomplete",
                    "response": {
                        "incomplete_details": {"reason": "max_output_tokens"},
                    },
                }
            )
        )
    with pytest.raises(ConnectionError, match="incomplete_response"):
        events.extend(adapter.finish())
    assert events == []
