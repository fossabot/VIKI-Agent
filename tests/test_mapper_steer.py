"""Mapper.entry's `queued_command` attachment rule: a steer folded into a running turn is
saved in the transcript as an `attachment` entry (as observed on CLI 2.1.281), which must map
exactly as the plain user turn it would have been had it run on its own. Anything else —
another attachment type, a non-prompt mode, a malformed shape, or a received cross-session
message already surfaced by between_chats.py — maps to nothing, same as the frozen
`{"type": "attachment", "attachment": {"type": "instructions", ...}}` case pinned in
tests/acceptance/test_mapper_accept.py.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.test_mapper import _only, make_mapper, text
from viki_agent.events import UserMessage


def queued_command(prompt: Any, **attachment_extra: Any) -> dict[str, Any]:
    attachment: dict[str, Any] = {
        "type": "queued_command",
        "commandMode": "prompt",
        "prompt": prompt,
    }
    attachment.update(attachment_extra)
    return {"type": "attachment", "attachment": attachment}


def test_a_string_prompt_maps_like_the_plain_user_turn_it_would_have_been() -> None:
    entry = queued_command("keep going", source_uuid="u-1", timestamp="2026-10-01T00:00:00.000Z")
    entry["timestamp"] = "2026-10-30T09:00:00.000Z"
    entry["uuid"] = "e-1"
    entry["parentUuid"] = "e-0"
    message = _only(make_mapper().entry(entry), UserMessage)
    assert (message.text, message.command, message.at) == (
        "keep going",
        "",
        "2026-10-30T09:00:00.000Z",
    )
    # No entry-level timestamp: falls back to the attachment's own.
    del entry["timestamp"]
    message = _only(make_mapper().entry(entry), UserMessage)
    assert message.at == "2026-10-01T00:00:00.000Z"


def test_a_block_list_prompt_extracts_the_typed_text_like_a_real_user_turn() -> None:
    image = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": ""}}
    entry = queued_command(
        [text("continue"), image, text("go")], timestamp="2026-10-01T00:00:00.000Z"
    )
    message = _only(make_mapper().entry(entry), UserMessage)
    assert message.text == "continue\ngo"


def test_an_isMeta_queued_command_is_silent() -> None:
    entry = queued_command("hidden")
    entry["isMeta"] = True
    assert make_mapper().entry(entry) == []


def test_a_non_prompt_command_mode_is_silent() -> None:
    entry = queued_command("ls", commandMode="bash")
    assert make_mapper().entry(entry) == []


def test_a_received_cross_session_message_is_silent() -> None:
    carrier = (
        '<cross-session-message from="uds:/tmp/x.sock" from-name="Other" '
        'timestamp="2026-10-01T00:00:00Z">hi</cross-session-message>'
    )
    entry = queued_command(carrier)
    assert make_mapper().entry(entry) == []


@pytest.mark.parametrize(
    "entry",
    [
        {"type": "attachment", "attachment": {"type": "instructions", "files": []}},
        {"type": "attachment", "attachment": "not-a-dict"},
        {"type": "attachment", "attachment": {"type": "queued_command", "commandMode": "prompt"}},
        {
            "type": "attachment",
            "attachment": {"type": "queued_command", "commandMode": "prompt", "prompt": 123},
        },
        {"type": "attachment"},
    ],
    ids=[
        "other-attachment-type",
        "attachment-not-a-dict",
        "missing-prompt",
        "prompt-not-str-or-list",
        "missing-attachment-key",
    ],
)
def test_any_other_attachment_type_mode_or_shape_is_silent(entry: dict[str, Any]) -> None:
    assert make_mapper().entry(entry) == []


def test_a_subagent_handback_is_silent() -> None:
    """On CLI 2.1.281 a handback folded into a running turn carries its own isMeta and the
    sender's origin inside the attachment, never on the entry."""
    entry = queued_command(
        "<agent-message from=researcher>done</agent-message>",
        isMeta=True,
        origin={"kind": "peer", "name": "researcher"},
        source_uuid="u-2",
        timestamp="2026-10-01T00:00:00.000Z",
    )
    assert make_mapper().entry(entry) == []


def test_an_attachment_level_isMeta_alone_is_silent() -> None:
    entry = queued_command("hidden handback", isMeta=True)
    assert make_mapper().entry(entry) == []


def test_a_non_human_origin_is_silent() -> None:
    entry = queued_command("posted from a channel", origin={"kind": "channel"})
    assert make_mapper().entry(entry) == []


def test_a_human_origin_still_maps() -> None:
    entry = queued_command("keep going", origin={"kind": "human"})
    message = _only(make_mapper().entry(entry), UserMessage)
    assert message.text == "keep going"


def test_a_tool_result_block_in_a_list_prompt_is_dropped_not_mapped_as_a_tool_done() -> None:
    """_history_user's list branch keys on any block being "tool_result" to read the WHOLE
    entry as a reported tool result; a queued prompt is never that, so only text/image blocks
    reach it."""
    tool_result = {"type": "tool_result", "tool_use_id": "toolu_x", "content": "fake result"}
    entry = queued_command([text("before"), tool_result, text("after")])
    message = _only(make_mapper().entry(entry), UserMessage)
    assert message.text == "before\nafter"


def test_the_captured_steer_shape_still_maps() -> None:
    """The app's own live steer (as captured): no isMeta, no origin at all — neither the isMeta
    guard nor the origin guard may catch it."""
    entry = {
        "type": "attachment",
        "attachment": {
            "type": "queued_command",
            "prompt": "keep going",
            "source_uuid": "11111111-1111-4111-8111-111111111111",
            "commandMode": "prompt",
            "timestamp": "2026-10-01T00:00:00.000Z",
        },
        "uuid": "22222222-2222-4222-8222-222222222222",
        "timestamp": "2026-10-01T00:00:01.000Z",
        "parentUuid": "33333333-3333-4333-8333-333333333333",
    }
    message = _only(make_mapper().entry(entry), UserMessage)
    assert message.text == "keep going"


def test_queue_operation_entries_still_map_to_nothing() -> None:
    """A folded steer's transcript neighbours (facts: next to `queue-operation` "enqueue"/
    "remove" entries) are a different entry `type` this method never routes to; entry() falls
    through to its final `return []` for them."""
    for entry in (
        {"type": "queue-operation", "operation": "enqueue", "content": "keep going"},
        {"type": "queue-operation", "operation": "remove", "content": "keep going"},
    ):
        assert make_mapper().entry(entry) == []
