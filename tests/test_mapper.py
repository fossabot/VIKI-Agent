"""Extra tests for the mapper, beside the frozen acceptance goldens
(tests/acceptance/test_mapper_accept.py):

* `tool_row` exercised directly — it is public because the driver reuses it for permission-card
  summaries.
* A few Mapper rules the acceptance goldens do not happen to exercise: a nested Agent/Task call
  (on a subagent thread) is a plain tool row rather than a routing row; a routing line's reason
  only ever attaches to the agent it named; `task_notification` fires `subagent.done` at most
  once per active episode.
* One test per further rule (async-launched tool results, routing lines, workflow descriptions,
  hook and permission frames, history text), each failing without its corresponding change.
"""

from __future__ import annotations

import json
import re
import sys
from itertools import product
from typing import Any

import pytest

from viki_agent.conversations.stream_state import MAX_TEXT_CHARS
from viki_agent.drivers.claude.mapper import (
    _ROUTING_RE,
    ARGUMENTS_SHORTENED,
    CARD_REST_LIMIT,
    FREE_TEXT_LIMIT,
    GENERIC_TOOL_QUESTION,
    ROW_REST_LIMIT,
    Mapper,
    _parse_command,
    _parse_task_notification,
    tool_question,
    tool_row,
)
from viki_agent.events import (
    Compacted,
    Event,
    Hook,
    PermissionDenied,
    Status,
    SubagentDone,
    SubagentStarted,
    TextDelta,
    TextDone,
    ToolDone,
    ToolRow,
    ToolStarted,
    UserMessage,
)

VAULT = "C:\\Vault"


def _only[T: Event](events: list[Event], cls: type[T]) -> T:
    """The single event `events` must contain, narrowed to `cls`."""
    (event,) = events
    assert isinstance(event, cls)
    return event


def make_mapper(mode: str = "auto") -> Mapper:
    return Mapper(vault=VAULT, mode=mode, clock=lambda: 0.0)


def assistant(msg: str, block: dict[str, Any], parent: str | None = None) -> dict[str, Any]:
    message = {"id": msg, "role": "assistant", "content": [block]}
    return {"type": "assistant", "message": message, "parent_tool_use_id": parent}


def tool_use(tool_id: str, name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
    return {"type": "tool_use", "id": tool_id, "name": name, "input": tool_input}


def text(value: str) -> dict[str, Any]:
    return {"type": "text", "text": value}


# --- tool_row: public, direct tests (the driver reuses it for permission-card summaries) -----


def test_tool_row_bash_collapses_whitespace() -> None:
    assert tool_row("Bash", {"command": "  rg   -n  foo   bar  "}, VAULT) == ToolRow(
        head="rg", rest="-n foo bar", tail=""
    )


def test_tool_row_bash_with_no_command_is_empty() -> None:
    assert tool_row("Bash", {}, VAULT) == ToolRow(head="", rest="", tail="")


def test_tool_row_read_is_vault_relative_case_insensitively() -> None:
    assert tool_row("Read", {"file_path": "c:\\VAULT\\30-knowledge\\x.md"}, VAULT) == ToolRow(
        head="Read", rest="30-knowledge/x.md", tail=""
    )


def test_tool_row_read_at_the_vault_root_is_empty() -> None:
    assert tool_row("Read", {"file_path": "C:\\Vault"}, VAULT) == ToolRow(
        head="Read", rest="", tail=""
    )


def test_tool_row_read_outside_vault_is_unchanged() -> None:
    assert tool_row("Read", {"file_path": "D:\\elsewhere\\a.md"}, VAULT) == ToolRow(
        head="Read", rest="D:\\elsewhere\\a.md", tail=""
    )


def test_tool_row_grep_path_outside_vault_is_unchanged() -> None:
    assert tool_row("Grep", {"pattern": "x", "path": "D:\\elsewhere"}, VAULT) == ToolRow(
        head="Grep", rest="x D:\\elsewhere", tail=""
    )


def test_skill_row_reads_only_the_named_skill_even_when_args_comes_first() -> None:
    tool_input = {"args": "wrong label", "skill": "capture", "other": "also wrong"}
    assert tool_row("Skill", tool_input, VAULT) == ToolRow(head="Skill", rest="capture", tail="")
    started = _only(
        make_mapper().frame(assistant("m1", tool_use("toolu_1", "Skill", tool_input))),
        ToolStarted,
    )
    assert started.row == ToolRow(head="Skill", rest="capture", tail="")


def test_skill_row_rejects_missing_or_non_string_names_and_keeps_the_row_cap() -> None:
    for value in (None, 42, ["capture"], {"name": "capture"}):
        assert tool_row("Skill", {"args": "not a name", "skill": value}, VAULT) == ToolRow(
            head="Skill", rest="", tail=""
        )
    assert tool_row("Skill", {"args": "not a name"}, VAULT).rest == ""
    long = "s" * (ROW_REST_LIMIT + 10)
    assert tool_row("Skill", {"skill": long}, VAULT) == ToolRow(
        head="Skill", rest="s" * (ROW_REST_LIMIT - 1) + "…", tail=""
    )


# --- tool_row for a permission card: a tool with no grammar here shows every argument --------


def _card_rest(tool: str, tool_input: dict[str, Any]) -> str:
    """`rest` as the driver asks for it when it builds a permission card."""
    return tool_row(tool, tool_input, VAULT, limit=CARD_REST_LIMIT).rest


def test_card_for_an_mcp_tool_carries_every_argument_not_only_the_first_string() -> None:
    tool_input = {
        "owner": "acme",
        "repo": "site",
        "title": "Remove the staging database",
        "confirm": True,
        "labels": ["ops", "urgent"],
        "retries": 3,
        "parent": None,
    }
    row = tool_row("mcp__tracker__create_issue", tool_input, VAULT, limit=CARD_REST_LIMIT)
    assert row.head == "tracker·create_issue"
    # Exactly the JSON of the input: nothing between the braces is this app's own wording.
    assert row.rest == json.dumps(tool_input, ensure_ascii=False)
    assert row.rest == (
        '{"owner": "acme", "repo": "site", "title": "Remove the staging database", '
        '"confirm": true, "labels": ["ops", "urgent"], "retries": 3, "parent": null}'
    )


def test_card_for_an_unknown_tool_carries_every_argument() -> None:
    agent = {"description": "Tidy notes", "prompt": "Delete every draft.", "subagent_type": "x"}
    assert _card_rest("Agent", agent) == json.dumps(agent)
    todos = {"todos": [{"content": "x", "status": "pending"}]}
    assert _card_rest("TodoWrite", todos) == '{"todos": [{"content": "x", "status": "pending"}]}'
    # No argument at all leaves the card with the tool's name alone, as before.
    assert _card_rest("MadeUpTool", {}) == ""
    assert _card_rest("mcp__s__t", {}) == ""


def test_row_for_an_mcp_or_unknown_tool_is_still_its_first_string_argument() -> None:
    # A conversation row is one line; only a card's limit asks for every argument.
    tool_input = {"count": 2, "query": "from:x", "body": "second"}
    assert tool_row("mcp__mail__search", tool_input, VAULT) == ToolRow(
        head="mail·search", rest="from:x", tail=""
    )
    assert tool_row("MadeUpTool", tool_input, VAULT, limit=ROW_REST_LIMIT) == ToolRow(
        head="MadeUpTool", rest="from:x", tail=""
    )
    assert tool_row("TodoWrite", {"todos": [{"content": "x"}]}, VAULT).rest == ""


def test_card_arguments_keep_text_in_other_scripts_and_escape_what_would_break_the_line() -> None:
    tool_input = {"title": "修正テスト", "body": 'line one\nline two\t"quoted" \\ \x1b[31m'}
    rest = _card_rest("mcp__notes__save", tool_input)
    assert rest == json.dumps(tool_input, ensure_ascii=False)
    assert "修正テスト" in rest
    # A value cannot start a line of its own, or end its quotes early, to pass for an argument.
    assert "\n" not in rest and "\x1b" not in rest
    assert '"line one\\nline two\\t\\"quoted\\" \\\\ \\u001b[31m"' in rest


def test_card_arguments_that_do_not_fit_say_so_first_and_keep_every_key() -> None:
    tool_input = {"path": "notes/a.md", "content": "c" * 10_000, "overwrite": True, "mode": "x"}
    rest = _card_rest("mcp__files__write", tool_input)
    assert rest.startswith(ARGUMENTS_SHORTENED)
    assert len(rest) <= CARD_REST_LIMIT
    # The long value is cut where it sits, with how much of it is missing; the arguments after
    # it, which the first-string rendering never showed and a plain cut would lose, are whole.
    assert rest == (
        ARGUMENTS_SHORTENED
        + '{"path": "notes/a.md", "content": "'
        + "c" * 400
        + '"… (9600 more characters), "overwrite": true, "mode": "x"}'
    )


def test_card_arguments_are_cut_shorter_until_every_key_fits() -> None:
    tool_input = {f"field{index:02}": chr(97 + index % 26) * 300 for index in range(30)}
    rest = _card_rest("MadeUpTool", tool_input)
    assert rest.startswith(ARGUMENTS_SHORTENED)
    assert len(rest) <= CARD_REST_LIMIT
    for index in range(30):
        letter = chr(97 + index % 26)
        assert f'"field{index:02}": "{letter * 12}"… (288 more characters)' in rest
    assert rest.endswith("}")


def test_card_arguments_past_every_cut_stop_at_the_limit_and_still_say_so_first() -> None:
    tool_input = {f"key{index}": index for index in range(2_000)}
    rest = _card_rest("MadeUpTool", tool_input)
    assert rest.startswith(ARGUMENTS_SHORTENED + '{"key0": 0, "key1": 1, ')
    assert len(rest) == CARD_REST_LIMIT
    assert rest.endswith("…")


def test_card_arguments_one_character_over_the_limit_are_marked_and_at_it_are_not() -> None:
    frame = len(json.dumps({"value": ""}))
    fits = {"value": "v" * (CARD_REST_LIMIT - frame)}
    assert _card_rest("MadeUpTool", fits) == json.dumps(fits)
    assert len(_card_rest("MadeUpTool", fits)) == CARD_REST_LIMIT
    over = {"value": "v" * (CARD_REST_LIMIT - frame + 1)}
    assert _card_rest("MadeUpTool", over).startswith(ARGUMENTS_SHORTENED)


def test_card_arguments_nested_past_the_depth_limit_are_written_as_an_ellipsis() -> None:
    deep: dict[str, Any] = {"leaf": "x"}
    for _ in range(12):
        deep = {"next": deep, "items": [deep]}
    rest = _card_rest("MadeUpTool", {"tree": deep, "after": 1})
    assert rest.startswith(ARGUMENTS_SHORTENED)
    assert "{…}" in rest and "[…]" in rest
    assert len(rest) <= CARD_REST_LIMIT


class _CountedList(list[Any]):
    """A list that counts how many of its items were read."""

    def __init__(self, items: Any) -> None:
        super().__init__(items)
        self.read = 0

    def __iter__(self) -> Any:
        for item in super().__iter__():
            self.read += 1
            yield item


def test_card_arguments_are_read_no_further_than_a_card_could_show() -> None:
    # The arguments are the model's: a million items must not cost a million steps.
    items = _CountedList(range(1_000_000))
    rest = _card_rest("MadeUpTool", {"items": items, "note": "n" * 5_000_000})
    assert rest.startswith(ARGUMENTS_SHORTENED + '{"items": [0, 1, 2, ')
    assert len(rest) <= CARD_REST_LIMIT
    assert 0 < items.read <= 5 * CARD_REST_LIMIT


def test_card_arguments_are_the_same_text_for_the_same_input_in_the_inputs_own_order() -> None:
    forward = {"b": "2", "a": "1", "c": {"z": [1, 2], "y": "y" * 3_000}}
    backward = dict(reversed(list(forward.items())))
    first = _card_rest("mcp__s__t", forward)
    assert first == _card_rest("mcp__s__t", dict(forward))
    assert first.index('"b"') < first.index('"a"') < first.index('"c"')
    assert _card_rest("mcp__s__t", backward).index('"c"') < first.index('"c"')


def test_card_for_a_tool_with_its_own_grammar_is_unchanged_by_the_card_limit() -> None:
    assert tool_row(
        "Read", {"file_path": "C:\\Vault\\a.md", "limit": 5}, VAULT, limit=CARD_REST_LIMIT
    ) == ToolRow(head="Read", rest="a.md", tail="")
    workflow = {"script": "export const meta = { name: 'nightly' }\n" + "x" * 5_000}
    assert tool_row("Workflow", workflow, VAULT, limit=CARD_REST_LIMIT) == ToolRow(
        head="Workflow", rest="nightly", tail=""
    )


# --- nested subagent calls: Agent/Task/Workflow on a subagent thread are plain tool rows ------


def test_nested_agent_call_is_a_tool_row_not_a_subagent() -> None:
    mapper = make_mapper()
    tool_input = {"subagent_type": "nested", "description": "d"}
    events = mapper.frame(
        assistant("s1", tool_use("toolu_inner", "Agent", tool_input), parent="toolu_outer")
    )
    started = _only(events, ToolStarted)
    assert started == ToolStarted(
        block_id="toolu_inner",
        tool="Agent",
        row=tool_row("Agent", tool_input, VAULT),
        parent_id="toolu_outer",
    )


def test_nested_agent_result_is_a_tool_done_not_a_subagent_done() -> None:
    mapper = make_mapper()
    mapper.frame(
        assistant(
            "s1",
            tool_use("toolu_inner", "Agent", {"subagent_type": "nested"}),
            parent="toolu_outer",
        )
    )
    block = {
        "type": "tool_result",
        "tool_use_id": "toolu_inner",
        "content": "done",
        "is_error": False,
    }
    frame = {
        "type": "user",
        "message": {"role": "user", "content": [block]},
        "parent_tool_use_id": "toolu_outer",
    }
    done = _only(mapper.frame(frame), ToolDone)
    assert done.block_id == "toolu_inner"


def _stream(event: dict, parent: str | None = None) -> dict:
    return {"type": "stream_event", "event": event, "parent_tool_use_id": parent}


def test_a_bracketed_model_name_is_read_whole() -> None:
    """The user's own models are written "opus[1m]". A model group that stopped at the first
    "]" cut the name in half and then failed to match the closing bracket, so the routing rows
    this app exists to show did not appear at all - for exactly the models it is run with."""
    for line, model in (
        ("\u25b8 routing: reviewer \u2192 [opus[1m]]\uff08a\uff09", "opus[1m]"),
        ("\u25b8 routing: reviewer \u2192 [opus]\uff08a\uff09", "opus"),
        ("\u25b8 routing: reviewer \u2192 inherit (why not)", "inherit"),
        ("\u25b8 routing: a \u2192 [claude-opus-5[1m]] (deep review)", "claude-opus-5[1m]"),
    ):
        found = _ROUTING_RE.search(line)
        assert found is not None, line
        assert found["model"] == model


def test_a_routing_model_ends_on_a_non_space() -> None:
    """A model group that could end in spaces matched the same spaces as the `\\s*` after it, so
    a line with a long run of spaces and no closing bracket took time quadratic in the run: about
    3 s at 32,000 characters, for every read of a subagent transcript that held such a line. With
    the model ending on a non-space, such a line maps in about a millisecond. A blank model, which
    the old group read as " ", no longer makes a line a routing line. More generally, wherever the
    pattern matches at all, the model it captures ends on a non-space."""
    assert _ROUTING_RE.fullmatch("▸ routing: a →  (x)") is None  # the arrow, then two spaces
    # Wherever the pattern matches, its model ends on a non-space, so the spaces before the
    # bracket belong to `\s*` alone; a model group that could end in spaces — the old one, or
    # one that only starts on a non-space — reads `b ` from the first line below.
    for line in (
        "▸ routing: a → [b ] (x)",
        "▸ routing: a → [b  ]（x）",
        "▸ routing: a → b \t(x)",
        "▸ routing: a → [opus[1m]] (x)",
    ):
        match = _ROUTING_RE.fullmatch(line)
        assert match is not None, line
        model = match["model"]
        assert model == model.rstrip(), line
    crafted = [
        "▸ routing: a → b" + " " * 16_000 + "(" + "x" * 16_000,
        "▸ routing: a → b" + " " * 32_000 + ")",
    ]
    mapper = make_mapper()
    done = [
        _only(mapper.entry(assistant(f"m{n}", text(line))), TextDone)
        for n, line in enumerate(crafted)
    ]
    # Neither is a routing line: each stays the reply's own text.
    assert [event.text for event in done] == [line[:MAX_TEXT_CHARS] for line in crafted]
    found = _ROUTING_RE.fullmatch("▸ routing: reviewer → [opus[1m]]（a）")
    assert found is not None and found["model"] == "opus[1m]"


def test_a_subagents_stream_event_never_retags_the_main_threads_blocks() -> None:
    """Every subagent stream event maps to nothing, and the two that WRITE per-message state
    have to obey that too: message_start sets the prefix every main-thread block id is built
    from, so a subagent's id would retag the reply the user is watching, mid-stream."""
    mapper = make_mapper()
    mapper.frame(_stream({"type": "message_start", "message": {"id": "msg_main"}}))
    before = mapper._current_message_id
    sub = _stream({"type": "message_start", "message": {"id": "msg_sub"}}, parent="tu_1")
    assert mapper.frame(sub) == []
    assert mapper._current_message_id == before

    thinking = {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking"}}
    assert mapper.frame(_stream(thinking, parent="tu_1")) == []
    assert mapper._thinking_start == {}


# --- routing reasons: only attach to the agent they named, and never leak to another ----------


def test_routing_reason_does_not_leak_to_a_different_agent() -> None:
    mapper = make_mapper()
    line = "▸ routing: researcher → [opus]（primary sources）"
    mapper.frame(assistant("m1", text(line)))
    events = mapper.frame(
        assistant("m2", tool_use("toolu_a", "Agent", {"subagent_type": "librarian"}))
    )
    started = _only(events, SubagentStarted)
    assert started.reason == ""


def test_multiple_routing_lines_in_one_text_each_reach_their_own_agent() -> None:
    mapper = make_mapper()
    body = "\n".join(
        [
            "▸ routing: a → [opus]（first）",
            "▸ routing: b → [opus]（second）",
            "done",
        ]
    )
    done = _only(mapper.frame(assistant("m1", text(body))), TextDone)
    assert done.text == "done"
    started_a = _only(
        mapper.frame(assistant("m1", tool_use("toolu_a", "Task", {"subagent_type": "a"}))),
        SubagentStarted,
    )
    started_b = _only(
        mapper.frame(assistant("m1", tool_use("toolu_b", "Task", {"subagent_type": "b"}))),
        SubagentStarted,
    )
    assert (started_a.reason, started_b.reason) == ("first", "second")


# --- task_notification fires subagent.done at most once per active episode ------------------


def test_task_notification_is_idempotent() -> None:
    mapper = make_mapper()
    mapper.frame(assistant("m1", tool_use("toolu_a", "Agent", {"subagent_type": "Explore"})))
    notification = {
        "type": "system",
        "subtype": "task_notification",
        "tool_use_id": "toolu_a",
        "status": "completed",
    }
    first = _only(mapper.frame(notification), SubagentDone)
    second = mapper.frame(notification)
    assert first == SubagentDone(block_id="toolu_a", status="completed")
    assert second == []


# --- task_notification carries the CLI's usage and summary, only when they are valid ----------


def _notification(**fields: Any) -> dict[str, Any]:
    frame = {"type": "system", "subtype": "task_notification", "tool_use_id": "toolu_a"}
    return {**frame, "status": "completed", **fields}


def _done_after(notification: dict[str, Any]) -> SubagentDone:
    mapper = make_mapper()
    mapper.frame(assistant("m1", tool_use("toolu_a", "Agent", {"subagent_type": "Explore"})))
    return _only(mapper.frame(notification), SubagentDone)


def test_task_notification_carries_the_clis_usage_and_summary() -> None:
    usage = {"total_tokens": 19744, "tool_uses": 0, "duration_ms": 9380}
    done = _done_after(_notification(usage=usage, summary="  daily.md\n"))
    assert done.data() == {
        "block_id": "toolu_a",
        "status": "completed",
        "tool_uses": 0,
        "total_tokens": 19744,
        "duration_ms": 9380,
        "summary": "daily.md",
    }


def test_a_usage_value_that_is_not_a_count_is_dropped() -> None:
    for bad in (-1, 1.5, True, "7"):
        usage = {"tool_uses": bad, "total_tokens": 19744, "duration_ms": bad}
        done = _done_after(_notification(usage=usage))
        assert done.data() == {
            "block_id": "toolu_a",
            "status": "completed",
            "total_tokens": 19744,
        }, bad


def test_a_long_summary_is_stripped_then_cut_to_the_free_text_limit() -> None:
    done = _done_after(_notification(summary=" " * 10 + "s" * 50_000))
    assert len(done.summary) == FREE_TEXT_LIMIT
    assert done.summary == "s" * (FREE_TEXT_LIMIT - 1) + "…"


def test_a_notification_without_usage_or_summary_reads_block_id_and_status_only() -> None:
    for extra in ({}, {"usage": [2], "summary": 42}, {"usage": {}, "summary": " \n"}):
        done = _done_after(_notification(**extra))
        assert done.data() == {"block_id": "toolu_a", "status": "completed"}, extra


# --- task_started/task_updated: a killed or stopped task closes the row once -------------


def _task_started_frame(task_id: str, tool_id: str) -> dict[str, Any]:
    return {"type": "system", "subtype": "task_started", "task_id": task_id, "tool_use_id": tool_id}


def _task_updated_frame(task_id: str, status: str) -> dict[str, Any]:
    return {
        "type": "system",
        "subtype": "task_updated",
        "task_id": task_id,
        "patch": {"status": status},
    }


def test_task_updated_killed_closes_a_background_launch_once() -> None:
    mapper = make_mapper()
    mapper.frame(assistant("m1", tool_use("toolu_a", "Agent", {"subagent_type": "Explore"})))
    launched = {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "toolu_a", "content": "launched"}],
        },
        "parent_tool_use_id": None,
        "tool_use_result": {"status": "async_launched", "isAsync": True},
    }
    assert mapper.frame(launched) == []  # still running: a background launch
    assert mapper.frame(_task_started_frame("t1", "toolu_a")) == []
    assert mapper.frame(_task_updated_frame("t1", "killed")) == [
        SubagentDone(block_id="toolu_a", status="stopped")
    ]
    notification = {
        "type": "system",
        "subtype": "task_notification",
        "tool_use_id": "toolu_a",
        "status": "completed",
    }
    assert mapper.frame(notification) == []  # already closed: deduplicated


def test_task_updated_stopped_also_closes_the_row() -> None:
    mapper = make_mapper()
    mapper.frame(assistant("m1", tool_use("toolu_a", "Agent", {"subagent_type": "Explore"})))
    mapper.frame(_task_started_frame("t1", "toolu_a"))
    assert mapper.frame(_task_updated_frame("t1", "stopped")) == [
        SubagentDone(block_id="toolu_a", status="stopped")
    ]


def test_task_updated_completed_or_failed_emits_nothing_and_keeps_the_row_open() -> None:
    mapper = make_mapper()
    mapper.frame(assistant("m1", tool_use("toolu_a", "Agent", {"subagent_type": "Explore"})))
    mapper.frame(_task_started_frame("t1", "toolu_a"))
    for status in ("completed", "failed"):
        assert mapper.frame(_task_updated_frame("t1", status)) == [], status
    # The row is still open: the later task_notification is the one that closes it, with counts.
    notification = {
        "type": "system",
        "subtype": "task_notification",
        "tool_use_id": "toolu_a",
        "status": "completed",
        "usage": {"total_tokens": 1, "tool_uses": 0, "duration_ms": 1},
    }
    done = _only(mapper.frame(notification), SubagentDone)
    assert done.data() == {
        "block_id": "toolu_a",
        "status": "completed",
        "tool_uses": 0,
        "total_tokens": 1,
        "duration_ms": 1,
    }


def test_task_updated_for_an_unknown_task_id_emits_nothing() -> None:
    mapper = make_mapper()
    mapper.frame(assistant("m1", tool_use("toolu_a", "Agent", {"subagent_type": "Explore"})))
    mapper.frame(_task_started_frame("t1", "toolu_a"))
    assert mapper.frame(_task_updated_frame("unknown", "killed")) == []
    # t1's own row was not touched by the unknown id above, and still closes normally.
    assert mapper.frame(_task_updated_frame("t1", "killed")) == [
        SubagentDone(block_id="toolu_a", status="stopped")
    ]


def test_a_kill_closes_only_its_own_run_leaving_a_concurrent_sibling_open() -> None:
    """Two runs launched together: a kill for one must close only that one row. A mutant that
    closes every known run on one task_updated (siblings included) passes every other test
    here, because none of them launches two runs at once."""
    mapper = make_mapper()
    mapper.frame(assistant("m1", tool_use("toolu_a", "Agent", {"subagent_type": "Explore"})))
    mapper.frame(assistant("m2", tool_use("toolu_b", "Agent", {"subagent_type": "Explore"})))
    mapper.frame(_task_started_frame("t1", "toolu_a"))
    mapper.frame(_task_started_frame("t2", "toolu_b"))
    assert mapper.frame(_task_updated_frame("t2", "killed")) == [
        SubagentDone(block_id="toolu_b", status="stopped")
    ]
    # t1's own row is untouched: its later task_notification still closes it normally.
    notification = {
        "type": "system",
        "subtype": "task_notification",
        "tool_use_id": "toolu_a",
        "status": "completed",
    }
    assert mapper.frame(notification) == [
        SubagentDone(block_id="toolu_a", status="completed", summary="")
    ]


# --- resumed agents: lifecycle evidence opens another active episode -------------------------


@pytest.mark.parametrize("signal", ["task_started", "task_updated"])
def test_resumed_task_reopens_once_then_reports_its_new_completion(signal: str) -> None:
    mapper = make_mapper()
    mapper.frame(assistant("reason", text("▸ routing: Explore → [opus] (Find the source)")))
    launch = assistant(
        "m1",
        tool_use(
            "toolu_a", "Agent", {"subagent_type": "Explore", "model": "opus", "description": "Find"}
        ),
    )
    started = _only(mapper.frame(launch), SubagentStarted)
    mapper.frame(assistant("m2", tool_use("toolu_b", "Agent", {"subagent_type": "reviewer"})))
    assert mapper.frame(_task_started_frame("t1", "toolu_a")) == []
    assert mapper.frame(_task_started_frame("t2", "toolu_b")) == []
    assert mapper.frame(_task_updated_frame("t1", "stopped")) == [
        SubagentDone(block_id="toolu_a", status="stopped")
    ]
    assert mapper.frame(_notification(status="stopped")) == []
    message = assistant("m3", tool_use("message", "SendMessage", {"to": "t1", "message": "Resume"}))
    assert isinstance(_only(mapper.frame(message), ToolStarted), ToolStarted)
    assert mapper.frame(_notification(status="stopped")) == []  # sending is not running evidence

    restart = (
        _task_started_frame("t1", "toolu_a")
        if signal == "task_started"
        else _task_updated_frame("t1", "running")
    )
    assert mapper.frame(restart) == [started]
    assert mapper.frame(restart) == []
    assert mapper.frame(_task_started_frame("t1", "toolu_a")) == []
    assert mapper.frame(_task_updated_frame("t1", "running")) == []
    assert mapper.frame(_task_updated_frame("t2", "running")) == []  # sibling stayed active
    completed = _notification(usage={"total_tokens": 7}, summary="Finished resumed work")
    assert mapper.frame(completed) == [
        SubagentDone(
            block_id="toolu_a", status="completed", total_tokens=7, summary="Finished resumed work"
        )
    ]
    assert mapper.frame(completed) == []
    assert mapper.frame(_notification(tool_use_id="toolu_b")) == [
        SubagentDone(block_id="toolu_b", status="completed")
    ]


def _local_agent_start(**fields: Any) -> dict[str, Any]:
    return {
        **_task_started_frame("t1", "toolu_a"),
        "task_type": "local_agent",
        "spawn_depth": 1,
        "subagent_type": "Explore",
        "description": "Find the source",
        **fields,
    }


def test_a_fresh_mapper_admits_an_explicit_top_level_local_agent_start() -> None:
    mapper = make_mapper()
    frame = _local_agent_start(subagent_type="a" * 5000, description="d" * 5000, model="m" * 5000)
    started = _only(mapper.frame(frame), SubagentStarted)
    assert started.block_id == "toolu_a" and started.reason == ""
    for value in (started.agent, started.description, started.model):
        assert len(value) == FREE_TEXT_LIMIT and value.endswith("…")
    assert mapper.frame(frame) == []
    assert mapper.frame(_task_updated_frame("t1", "stopped")) == [
        SubagentDone(block_id="toolu_a", status="stopped")
    ]
    assert mapper.frame(_task_updated_frame("t1", "running")) == [started]


@pytest.mark.parametrize(
    "fields",
    [
        {"task_id": ""},
        {"task_id": 1},
        {"tool_use_id": ""},
        {"tool_use_id": []},
        {"task_type": None},
        {"task_type": "local_bash"},
        {"spawn_depth": None},
        {"spawn_depth": 2},
        {"spawn_depth": "1"},
        {"spawn_depth": True},
        {"subagent_type": ""},
        {"subagent_type": "  "},
        {"subagent_type": 1},
        {"parent_tool_use_id": "outer"},
    ],
)
def test_lifecycle_only_start_requires_typed_top_level_agent_identity(
    fields: dict[str, Any],
) -> None:
    mapper = make_mapper()
    assert mapper.frame(_local_agent_start(**fields)) == []
    assert mapper.frame(_task_updated_frame("t1", "running")) == []
    assert mapper.frame(_task_updated_frame("t1", "stopped")) == []
    assert mapper.frame(_notification()) == []


@pytest.mark.parametrize("nested", [False, True])
def test_lifecycle_start_does_not_promote_a_known_ordinary_or_nested_tool(nested: bool) -> None:
    mapper = make_mapper()
    mapper.frame(
        assistant(
            "m1",
            tool_use("toolu_a", "Agent" if nested else "Read", {}),
            parent="outer" if nested else None,
        )
    )
    assert mapper.frame(_local_agent_start()) == []
    assert mapper.frame(_task_updated_frame("t1", "running")) == []
    assert mapper.frame(_notification()) == []


@pytest.mark.parametrize(
    "fields", [{"parent_tool_use_id": "outer"}, {"spawn_depth": 2}, {"task_type": "local_bash"}]
)
def test_nested_or_unrelated_lifecycle_evidence_cannot_reopen_a_known_agent(
    fields: dict[str, Any],
) -> None:
    mapper = make_mapper()
    mapper.frame(assistant("m1", tool_use("toolu_a", "Agent", {"subagent_type": "Explore"})))
    mapper.frame(_task_started_frame("t1", "toolu_a"))
    mapper.frame(_task_updated_frame("t1", "stopped"))
    assert mapper.frame({**_task_started_frame("t1", "toolu_a"), **fields}) == []
    assert mapper.frame({**_task_updated_frame("t1", "running"), **fields}) == []
    assert mapper.frame(_notification()) == []
    assert mapper.frame(_task_updated_frame("unknown", "running")) == []
    assert mapper.frame(_task_updated_frame("t1", "unknown")) == []


@pytest.mark.parametrize("explicit", [None, "general-purpose"])
def test_resumed_agent_tool_retains_known_identity_only_when_omitted(explicit: str | None) -> None:
    mapper = make_mapper()
    mapper.frame(assistant("m1", tool_use("toolu_a", "Agent", {"subagent_type": "reviewer"})))
    mapper.frame(_task_started_frame("t1", "toolu_a"))
    mapper.frame(_task_updated_frame("t1", "stopped"))
    tool_input = {"resume": "t1", "description": "Review again"}
    if explicit is not None:
        tool_input["subagent_type"] = explicit
    resumed = _only(
        mapper.frame(assistant("m2", tool_use("toolu_resume", "Agent", tool_input))),
        SubagentStarted,
    )
    assert resumed.agent == (explicit or "reviewer")
    assert resumed.description == "Review again" and resumed.block_id == "toolu_resume"
    unknown = _only(
        mapper.frame(assistant("m3", tool_use("unknown", "Agent", {"resume": "missing"}))),
        SubagentStarted,
    )
    assert unknown.agent == "general-purpose"


def test_lifecycle_identity_corrects_a_fresh_process_resume_fallback_once() -> None:
    mapper = make_mapper()
    initial = _only(
        mapper.frame(assistant("m1", tool_use("toolu_a", "Agent", {"resume": "old-task"}))),
        SubagentStarted,
    )
    assert initial.agent == "general-purpose"
    frame = _local_agent_start(subagent_type="researcher")
    confirmed = _only(mapper.frame(frame), SubagentStarted)
    assert confirmed == SubagentStarted(
        block_id="toolu_a", agent="researcher", model="inherit", description="Find the source"
    )
    assert mapper.frame(frame) == []
    assert mapper.frame(_task_updated_frame("t1", "stopped")) == [
        SubagentDone(block_id="toolu_a", status="stopped")
    ]
    assert mapper.frame(_task_updated_frame("t1", "running")) == [confirmed]


@pytest.mark.parametrize("lifecycle", [False, True])
def test_seeded_history_id_does_not_suppress_its_first_observed_start(lifecycle: bool) -> None:
    mapper = make_mapper()
    mapper.note_subagents(["toolu_a"])
    frame = (
        _local_agent_start()
        if lifecycle
        else assistant("m1", tool_use("toolu_a", "Agent", {"subagent_type": "Explore"}))
    )
    started = _only(mapper.frame(frame), SubagentStarted)
    assert started.agent == "Explore"
    assert mapper.frame(frame) == []
    assert mapper.frame(_notification()) == [SubagentDone(block_id="toolu_a", status="completed")]


def test_duplicate_agent_tool_start_is_silent_until_its_active_episode_ends() -> None:
    mapper = make_mapper()
    frame = assistant("m1", tool_use("toolu_a", "Agent", {"subagent_type": "Explore"}))
    started = _only(mapper.frame(frame), SubagentStarted)
    assert mapper.frame(frame) == []
    assert mapper.frame(_notification()) == [SubagentDone(block_id="toolu_a", status="completed")]
    assert mapper.frame(frame) == [started]
    assert mapper.frame(_notification()) == [SubagentDone(block_id="toolu_a", status="completed")]


# --- history: what the user typed, and compaction (transcript shapes surveyed 2026-09-11) ------


def history_user(content: Any, **extra: Any) -> dict[str, Any]:
    entry = {"type": "user", "message": {"role": "user", "content": content}}
    return {**entry, "timestamp": "2026-09-11T08:00:00.000Z", **extra}


def test_a_prompt_sent_as_blocks_is_the_typed_text_without_ide_context() -> None:
    blocks = [text("<ide_opened_file>The user opened a.md</ide_opened_file>"), text("Go on")]
    message = _only(make_mapper().entry(history_user(blocks)), UserMessage)
    assert (message.text, message.command) == ("Go on", "")
    image = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": ""}}
    assert _only(make_mapper().entry(history_user([image])), UserMessage).text == "[image]"


def test_injected_user_entries_are_not_the_owners_turns() -> None:
    mapper = make_mapper()
    for entry in (
        history_user("<task-notification><task-id>t1</task-id></task-notification>"),
        history_user("<local-command-stdout>ok</local-command-stdout>"),
        history_user("This session is being continued...", isCompactSummary=True),
        history_user("caveat", isMeta=True),
        history_user([text("<system-reminder>x</system-reminder>")]),
    ):
        assert mapper.entry(entry) == [], entry


def test_a_compact_boundary_in_history_is_a_compacted_notice() -> None:
    boundary = {
        "type": "system",
        "subtype": "compact_boundary",
        "compactMetadata": {"trigger": "auto", "preTokens": 182000},
        "logicalParentUuid": "u-9",
    }
    assert make_mapper().entry(boundary) == [Compacted(trigger="auto", pre_tokens=182000)]


# --- history command and task-notification tags: str.find gives the old regexes' answers ---------

# The regexes the parsers replaced, kept as the oracle they must agree with on every input: a
# subagent's prompt and its later messages are model-written, so any arrangement of tags can come.
_COMMAND_ORACLE = re.compile(
    r"<command-name>(?P<name>.*?)</command-name>(?:.*?<command-args>(?P<args>.*?)</command-args>)?",
    re.DOTALL,
)
_NOTIFICATION_ORACLE = re.compile(
    r"<tool-use-id>(?P<tool_id>.*?)</tool-use-id>.*?<status>(?P<status>.*?)</status>", re.DOTALL
)


def _command_oracle(content: str) -> tuple[str, str | None] | None:
    match = _COMMAND_ORACLE.search(content)
    return (match["name"], match["args"]) if match else None


def _notification_oracle(content: str) -> tuple[str, str] | None:
    match = _NOTIFICATION_ORACLE.search(content)
    return (match["tool_id"], match["status"]) if match else None


def _tag_texts(*tags: str) -> list[str]:
    """Every text of at most six parts, each one of `tags` or "x": every order, repeat and
    nesting of the tags, closed or not."""
    parts = (*tags, "x")
    return ["".join(seq) for n in range(7) for seq in product(parts, repeat=n)]


# Each content with the old regex's answer, (name, args), or None where it found no command.
_COMMANDS: list[tuple[str, tuple[str, str | None] | None]] = [
    (
        "<command-name>/retro</command-name>\n<command-message>retro</command-message>\n"
        "<command-args>a b</command-args>",
        ("/retro", "a b"),
    ),
    (
        "<command-message>Compact the conversation</command-message>\n"
        "<command-name>/compact</command-name>",
        ("/compact", None),
    ),
    ("<command-name>/retro\n<command-args>x</command-args>", None),  # the name never closes
    ("<command-name>/retro</command-name>\n<command-args>x", ("/retro", None)),  # the args never do
    ("<command-args>x</command-args>\n<command-name>/retro</command-name>", ("/retro", None)),
    (
        "<command-args>x</command-args><command-name>/r</command-name><command-args>y</command-args>",
        ("/r", "y"),
    ),
    ("</command-name><command-name>/r</command-name>", ("/r", None)),
    ("<command-name><command-name>/r</command-name>", ("<command-name>/r", None)),
    ("<command-name>/r</command-name>x</command-name>", ("/r", None)),
    (
        "<command-name>/r</command-name><command-args>x<command-args>y</command-args>",
        ("/r", "x<command-args>y"),
    ),
    ("<command-name>/r</command-name><command-args>x</command-args>y</command-args>", ("/r", "x")),
    ("<command-name>/r</command-name></command-args><command-args>x", ("/r", None)),
    (
        "<command-name>/r<command-args>x</command-args></command-name>",
        ("/r<command-args>x</command-args>", None),
    ),
    (
        "<command-name>/a</command-name><command-args>1</command-args>"
        "<command-name>/b</command-name><command-args>2</command-args>",
        ("/a", "1"),
    ),
    ("<command-name></command-name><command-args></command-args>", ("", "")),
    ("<command-name>/r</command-name><command-args >y</command-args>", ("/r", None)),
    ("x <command-name>/r</command-name>", ("/r", None)),
    (
        "<command-name>/メモ\n</command-name>\n<command-args>今日\nの</command-args>",
        ("/メモ\n", "今日\nの"),
    ),
    ("<command-name>x</command-name>" + "<command-args>" * 3, ("x", None)),
    ("<command-name>" * 3, None),
    ("", None),
]

# Each content with the old regex's answer, (tool id, status), or None where it found none.
_NOTIFICATIONS: list[tuple[str, tuple[str, str] | None]] = [
    (
        "<task-notification>\n<task-id>t1</task-id>\n<tool-use-id>toolu_a</tool-use-id>\n"
        "<output-file>o</output-file>\n<status>completed</status>\n<summary>done</summary>\n"
        "</task-notification>",
        ("toolu_a", "completed"),
    ),
    (
        "see <task-notification><tool-use-id>toolu_a</tool-use-id> then <status>failed</status>"
        "</task-notification>\nRead the output file.",
        ("toolu_a", "failed"),
    ),
    ("<task-notification><tool-use-id>toolu_a</tool-use-id></task-notification>", None),
    ("<tool-use-id>toolu_a</tool-use-id><status>completed", None),  # the status never closes
    ("<status>completed</status><tool-use-id>toolu_a</tool-use-id>", None),  # it comes first
    ("<status>a</status><tool-use-id>t</tool-use-id><status>b</status>", ("t", "b")),
    ("<tool-use-id>toolu_a<status>completed</status>", None),  # the tool id never closes
    ("<tool-use-id>t<status>s</status></tool-use-id>", None),
    (
        "<tool-use-id>t<status>s</status></tool-use-id><status>u</status>",
        ("t<status>s</status>", "u"),
    ),
    ("</tool-use-id><tool-use-id>a</tool-use-id><status>s</status>", ("a", "s")),
    ("<tool-use-id>a</tool-use-id><tool-use-id>b</tool-use-id><status>s</status>", ("a", "s")),
    ("<tool-use-id>a</tool-use-id>b</tool-use-id><status>s</status>", ("a", "s")),
    (
        "<tool-use-id><tool-use-id>a</tool-use-id></tool-use-id><status>s</status>",
        ("<tool-use-id>a", "s"),
    ),
    ("<tool-use-id>a</tool-use-id><status>s<status>t</status>", ("a", "s<status>t")),
    ("<tool-use-id>a</tool-use-id><status>s</status><status>t</status>", ("a", "s")),
    (
        "<tool-use-id>a</tool-use-id><status>x</tool-use-id><status>s</status>",
        ("a", "x</tool-use-id><status>s"),
    ),
    ("<tool-use-id>a</tool-use-id><status>x</tool-use-id>", None),  # a later id closer: no status
    ("<tool-use-id>a</tool-use-id></status><status>s", None),
    ("<tool-use-id></tool-use-id><status></status>", ("", "")),
    ("<tool-use-id>a\n</tool-use-id>\n<status>\ncompleted\n</status>", ("a\n", "\ncompleted\n")),
    ("<task-notification>" + "<tool-use-id>x</tool-use-id>" * 3, None),
    ("<tool-use-id>t</tool-use-id>" + "<status>" * 3, None),
    ("", None),
]


def test_a_history_command_is_read_exactly_as_its_old_regex_read_it() -> None:
    """The name runs from the first opener to the first closer after it, and the args from the
    first args opener after that to the first args closer after it; this is the regex's own
    answer, on these cases and on every short arrangement of the four tags."""
    for content, expected in _COMMANDS:
        assert _command_oracle(content) == expected, content
        assert _parse_command(content) == expected, content
    tags = ("<command-name>", "</command-name>", "<command-args>", "</command-args>")
    for content in _tag_texts(*tags):
        assert _parse_command(content) == _command_oracle(content), content


def test_a_task_notification_is_read_exactly_as_its_old_regex_read_it() -> None:
    """The tool id runs from the first opener to the first closer after it, and the status from
    the first status opener after that to the first status closer after it; this is the regex's
    own answer, on these cases and on every short arrangement of the four tags."""
    for content, expected in _NOTIFICATIONS:
        assert _notification_oracle(content) == expected, content
        assert _parse_task_notification(content) == expected, content
    for content in _tag_texts("<tool-use-id>", "</tool-use-id>", "<status>", "</status>"):
        assert _parse_task_notification(content) == _notification_oracle(content), content


def test_tags_that_never_close_in_a_long_model_written_prompt_map_as_before() -> None:
    """A parent model can write tags that never close into a subagent's prompt. Over each of these
    texts, about 84 KB, the old regexes took seconds. They map as the regexes had them: the
    command with no args, the unclosed name as the user's text, and neither notification
    closing the run."""
    mapper = make_mapper()
    unclosed_args = "<command-name>x</command-name>" + "<command-args>" * 6_000
    message = _only(mapper.entry(history_user(unclosed_args)), UserMessage)
    assert (message.command, message.text) == ("x", "")
    unclosed_name = "<command-name>" * 6_000
    message = _only(mapper.entry(history_user(unclosed_name)), UserMessage)
    assert (message.command, message.text) == ("", unclosed_name[:MAX_TEXT_CHARS])
    mapper.note_subagents(["toolu_a"])
    unclosed_id = "<task-notification>" + "<tool-use-id>" * 6_500
    unclosed_status = "<task-notification><tool-use-id>toolu_a</tool-use-id>" + "<status>" * 10_500
    assert mapper.entry(history_user(unclosed_id)) == []
    assert mapper.entry(history_user(unclosed_status)) == []
    # The run is still open: a notification that does close names it.
    closed = "<task-notification><tool-use-id>toolu_a</tool-use-id><status>completed</status>"
    assert mapper.entry(history_user(closed)) == [
        SubagentDone(block_id="toolu_a", status="completed")
    ]


# --- further mapper rules, one test per rule -------------------------------------------------


def test_async_launched_tool_result_keeps_the_routing_row_open() -> None:
    mapper = make_mapper()
    mapper.frame(assistant("m1", tool_use("toolu_a", "Agent", {"subagent_type": "Explore"})))
    result = {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "toolu_a", "content": "launched"}],
        },
        "parent_tool_use_id": None,
        "tool_use_result": {"status": "async_launched", "isAsync": True},
    }
    assert mapper.frame(result) == []  # still running: no subagent.done yet
    notification = {
        "type": "system",
        "subtype": "task_notification",
        "tool_use_id": "toolu_a",
        "status": "completed",
    }
    assert mapper.frame(notification) == [SubagentDone(block_id="toolu_a", status="completed")]


def test_history_async_launched_result_is_closed_by_the_task_notification() -> None:
    mapper = make_mapper()
    started = mapper.entry(
        {
            "type": "assistant",
            "apiBlockIndex": 0,
            "message": {
                "id": "m1",
                "content": [
                    {"type": "tool_use", "id": "toolu_a", "name": "Agent", "input": {}},
                ],
            },
        }
    )
    assert started == [
        SubagentStarted(
            block_id="toolu_a", agent="general-purpose", model="inherit", description=""
        )
    ]
    result = history_user(
        [{"type": "tool_result", "tool_use_id": "toolu_a", "content": "launched"}],
        toolUseResult={"status": "async_launched", "isAsync": True},
    )
    assert mapper.entry(result) == []
    notification = history_user(
        "<task-notification>\n<tool-use-id>toolu_a</tool-use-id>\n<status>completed</status>\n"
        "</task-notification>"
    )
    assert mapper.entry(notification) == [SubagentDone(block_id="toolu_a", status="completed")]


def test_history_missing_api_block_index_falls_back_to_the_live_rule() -> None:
    mapper = make_mapper()
    entries = [
        {
            "type": "assistant",
            "message": {"id": "m1", "content": [{"type": "thinking", "thinking": "t"}]},
        },
        {
            "type": "assistant",
            "message": {"id": "m1", "content": [text("reply")]},
        },
    ]
    events = [e for entry in entries for e in mapper.entry(entry)]
    assert [e.data()["block_id"] for e in events] == ["m1:0", "m1:1"]  # not both "m1:0"


def test_tool_row_webfetch_falls_back_to_the_raw_url_when_unsplittable() -> None:
    url = "https://zenn.dev／articles/x"  # full-width ／: urlsplit raises ValueError
    assert tool_row("WebFetch", {"url": url}, VAULT) == ToolRow(head="WebFetch", rest=url, tail="")
    assert tool_row("WebFetch", {"url": "https://example.com/a"}, VAULT) == ToolRow(
        head="WebFetch", rest="example.com", tail=""
    )


def test_permission_denied_falls_back_to_message_when_no_decision_reason() -> None:
    frame = {
        "type": "system",
        "subtype": "permission_denied",
        "tool_name": "WebFetch",
        "decision_reason": "",
        "message": "Permission denied by classifier",
    }
    denied = _only(make_mapper().frame(frame), PermissionDenied)
    assert denied.reason == "Permission denied by classifier"


def test_compaction_without_a_boundary_clears_the_compacting_flag() -> None:
    mapper = make_mapper()
    started = mapper.frame({"type": "system", "subtype": "status", "status": "compacting"})
    ended = mapper.frame(
        {
            "type": "system",
            "subtype": "status",
            "status": None,
            "compact_result": "failed",
            "compact_error": "Not enough messages to compact.",
        }
    )
    assert started == [Status(state="compacting", mode="")]
    assert ended == [Status(state="", mode="")]


def test_grep_content_mode_counts_lines_not_files() -> None:
    mapper = make_mapper()
    mapper.frame(assistant("m1", tool_use("toolu_1", "Grep", {"pattern": "x"})))
    result = {
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_1",
                    "content": "a.md:1:x\nb.md:2:x",
                    "is_error": False,
                }
            ],
        },
        "parent_tool_use_id": None,
        "tool_use_result": {"mode": "content", "numFiles": 0, "numLines": 2},
    }
    done = _only(mapper.frame(result), ToolDone)
    assert done.tail == "→ 2 lines"


def test_interrupt_marker_text_is_dropped_not_a_user_message() -> None:
    entry = history_user("[Request interrupted by user for tool use]")
    assert make_mapper().entry(entry) == []


def test_routing_line_with_a_multi_word_agent_name() -> None:
    mapper = make_mapper()
    body = "▸ routing: Explore ×3 → [haiku]（example）\nNext."
    done = _only(mapper.frame(assistant("m1", text(body))), TextDone)
    assert done.text == "Next."
    started = _only(
        mapper.frame(assistant("m1", tool_use("toolu_a", "Task", {"subagent_type": "Explore ×3"}))),
        SubagentStarted,
    )
    assert started.reason == "example"


def test_workflow_description_from_script_meta() -> None:
    script = "import x from 'y'\nexport const meta = { name: 'Nightly Sync' }\nrun()"
    started = _only(
        make_mapper().frame(assistant("m1", tool_use("toolu_w", "Workflow", {"script": script}))),
        SubagentStarted,
    )
    assert started.description == "Nightly Sync"


def test_workflow_description_falls_back_to_the_script_path_stem() -> None:
    # The CLI sends the script path in the host's native form: backslashes on Windows,
    # POSIX slashes everywhere else.
    script_path = "C:\\wf\\nightly-sync.ts" if sys.platform == "win32" else "/wf/nightly-sync.ts"
    started = _only(
        make_mapper().frame(
            assistant("m1", tool_use("toolu_w", "Workflow", {"scriptPath": script_path}))
        ),
        SubagentStarted,
    )
    assert started.description == "nightly-sync"


def test_workflow_description_defaults_to_empty() -> None:
    started = _only(
        make_mapper().frame(assistant("m1", tool_use("toolu_w", "Workflow", {}))), SubagentStarted
    )
    assert started.description == ""


def test_pending_routing_reasons_are_fifo_and_expire_with_the_turn() -> None:
    mapper = make_mapper()
    mapper.frame(assistant("m1", text("▸ routing: a → [opus]（first）")))
    mapper.frame(assistant("m2", text("▸ routing: a → [opus]（second）")))
    first = _only(
        mapper.frame(assistant("m3", tool_use("toolu_1", "Task", {"subagent_type": "a"}))),
        SubagentStarted,
    )
    second = _only(
        mapper.frame(assistant("m3", tool_use("toolu_2", "Task", {"subagent_type": "a"}))),
        SubagentStarted,
    )
    assert (first.reason, second.reason) == ("first", "second")

    # A reason left unclaimed when the turn ends must not leak into a later, unrelated call.
    mapper.frame(assistant("m4", text("▸ routing: a → [opus]（stale）")))
    mapper.frame({"type": "result", "subtype": "success", "is_error": False})
    third = _only(
        mapper.frame(assistant("m3", tool_use("toolu_3", "Task", {"subagent_type": "a"}))),
        SubagentStarted,
    )
    assert third.reason == ""


def _message_start(msg_id: str) -> dict[str, Any]:
    return {
        "type": "stream_event",
        "event": {"type": "message_start", "message": {"id": msg_id}},
        "parent_tool_use_id": None,
    }


def _text_delta(index: int, chunk: str) -> dict[str, Any]:
    return {
        "type": "stream_event",
        "event": {
            "type": "content_block_delta",
            "index": index,
            "delta": {"type": "text_delta", "text": chunk},
        },
        "parent_tool_use_id": None,
    }


def test_routing_line_reason_is_queued_once_not_twice_by_delta_and_assistant() -> None:
    """A live turn always runs a routing line's deltas (which complete the line) before the
    block's own assistant text frame arrives. Registering the reason from both — as `_route_delta`
    used to — left a stale duplicate in the FIFO queue every time, so a second subagent of the
    same agent got the first one's reason instead of its own, or a leftover reason instead of
    none. Feeding only the final assistant frame (as the FIFO test above does) cannot see this:
    the delta path has to run too."""
    mapper = make_mapper()

    def routing_line_via_delta(msg_id: str, line: str) -> None:
        mapper.frame(_message_start(msg_id))
        mapper.frame(_text_delta(0, line + "\n"))
        mapper.frame(assistant(msg_id, text(line)))

    # (a) two routing lines for the same agent, a launch right after each: FIFO, not a repeat.
    routing_line_via_delta("m1", "▸ routing: reviewer → [opus]（M1 review）")
    first = _only(
        mapper.frame(assistant("m3", tool_use("toolu_1", "Task", {"subagent_type": "reviewer"}))),
        SubagentStarted,
    )
    routing_line_via_delta("m2", "▸ routing: reviewer → [opus]（a second look）")
    second = _only(
        mapper.frame(assistant("m3", tool_use("toolu_2", "Task", {"subagent_type": "reviewer"}))),
        SubagentStarted,
    )
    assert (first.reason, second.reason) == ("M1 review", "a second look")

    # (b) one routing line, two subagents of that agent: the second gets no reason, not a repeat.
    routing_line_via_delta("m4", "▸ routing: reviewer → [opus]（recheck）")
    third = _only(
        mapper.frame(assistant("m3", tool_use("toolu_3", "Task", {"subagent_type": "reviewer"}))),
        SubagentStarted,
    )
    fourth = _only(
        mapper.frame(assistant("m3", tool_use("toolu_4", "Task", {"subagent_type": "reviewer"}))),
        SubagentStarted,
    )
    assert (third.reason, fourth.reason) == ("recheck", "")


def test_task_notification_for_an_id_without_a_routing_row_is_ignored() -> None:
    notification = {
        "type": "system",
        "subtype": "task_notification",
        "tool_use_id": "toolu_shell",
        "status": "completed",
    }
    assert make_mapper().frame(notification) == []


def test_hook_cancelled_by_stop_produces_no_event() -> None:
    frame = {
        "type": "system",
        "subtype": "hook_response",
        "hook_name": "PreToolUse:Bash",
        "outcome": "cancelled",
        "exit_code": 130,
        "stderr": "",
        "output": "",
    }
    assert make_mapper().frame(frame) == []


def test_rate_limit_without_a_status_is_silent() -> None:
    frame = {"type": "rate_limit_event", "rate_limit_info": {"rateLimitType": "five_hour"}}
    assert make_mapper().frame(frame) == []


def test_history_routing_reason_does_not_outlive_its_turn() -> None:
    """Live, the result frame drops an unclaimed reason; in history the next user entry does."""
    mapper = make_mapper()
    routing = text("▸ routing: Explore → [haiku]（turn-one reason）")
    launch = tool_use("toolu_1", "Agent", {"subagent_type": "Explore", "description": "d"})
    entries = [
        history_user("turn one"),
        {"type": "assistant", "message": {"id": "m1", "content": [routing]}},
        history_user("turn two"),
        {"type": "assistant", "message": {"id": "m2", "content": [launch]}},
    ]
    events = [e for entry in entries for e in mapper.entry(entry)]
    started = [e for e in events if isinstance(e, SubagentStarted)]
    assert len(started) == 1 and started[0].reason == ""


def test_command_without_args_is_not_shown_as_raw_xml() -> None:
    content = (
        "<command-message>Compact the conversation</command-message>\n"
        "<command-name>/compact</command-name>"
    )
    message = _only(make_mapper().entry(history_user(content)), UserMessage)
    assert (message.command, message.text) == ("/compact", "")


def test_a_command_tag_after_other_text_is_ordinary_history_text() -> None:
    content = (
        "[vault excerpt] notes/a.md lines 1-3\n"
        "```\n<command-name>/retro</command-name>\n<command-args>x</command-args>\n```"
    )
    message = _only(make_mapper().entry(history_user(content)), UserMessage)
    assert (message.command, message.text) == ("", content)


def test_a_command_after_leading_whitespace_and_a_long_paste_is_still_the_command() -> None:
    content = (
        "  \n<command-name>/retro</command-name>\n<command-args>x</command-args>" + "y" * 20_000
    )
    message = _only(make_mapper().entry(history_user(content)), UserMessage)
    assert (message.command, message.text) == ("/retro", "x")


def test_routing_text_starting_mid_line_at_a_delta_boundary_stays_plain() -> None:
    mapper = make_mapper()
    mapper.frame(
        {
            "type": "stream_event",
            "event": {"type": "message_start", "message": {"id": "m1"}},
            "parent_tool_use_id": None,
        }
    )

    def text_delta(chunk: str) -> dict[str, Any]:
        return {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": chunk},
            },
            "parent_tool_use_id": None,
        }

    full_line = "The format is ▸ routing: Explore → [haiku]（example）\nThat is all."
    first = _only(mapper.frame(text_delta("The format is ")), TextDelta)
    second = _only(mapper.frame(text_delta(full_line[len("The format is ") :])), TextDelta)
    assert first.text + second.text == full_line
    done = _only(mapper.frame(assistant("m1", text(full_line))), TextDone)
    assert done.text == full_line  # not stripped: "▸" was mid-line, not a line start


def test_a_text_block_that_is_only_a_routing_line_produces_no_text_done() -> None:
    events = make_mapper().frame(
        assistant("m1", text("▸ routing: researcher → [opus]（primary sources）"))
    )
    assert events == []


def test_held_routing_prefix_is_cleared_when_the_block_ends() -> None:
    mapper = make_mapper()
    mapper.frame(
        {
            "type": "stream_event",
            "event": {"type": "message_start", "message": {"id": "m1"}},
            "parent_tool_use_id": None,
        }
    )
    mapper.frame(
        {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "▸ routing: a"},
            },
            "parent_tool_use_id": None,
        }
    )
    assert mapper._held  # holding an incomplete candidate routing line, no newline yet
    mapper.frame(assistant("m1", text("▸ routing: a")))
    assert mapper._held == {}


# --- crash guards: odd but real shapes that map to nothing instead of raising -----------------


def test_result_text_of_an_unrecognized_content_shape_is_empty() -> None:
    from viki_agent.drivers.claude.mapper import _result_text

    assert _result_text(123) == ""
    assert _result_text(None) == ""


def test_assistant_frame_with_empty_content_is_silent() -> None:
    frame = {
        "type": "assistant",
        "message": {"id": "m1", "role": "assistant", "content": []},
        "parent_tool_use_id": None,
    }
    assert make_mapper().frame(frame) == []


def test_history_assistant_entry_with_empty_content_is_silent() -> None:
    entry = {"type": "assistant", "apiBlockIndex": 0, "message": {"id": "m1", "content": []}}
    assert make_mapper().entry(entry) == []


def test_user_frame_with_non_list_content_is_silent() -> None:
    frame = {
        "type": "user",
        "message": {"role": "user", "content": "plain string, not blocks"},
        "parent_tool_use_id": None,
    }
    assert make_mapper().frame(frame) == []


def test_unknown_content_block_kind_is_silent() -> None:
    block = {"type": "redacted_thinking", "data": "opaque"}
    assert make_mapper().frame(assistant("m1", block)) == []


# --- history text is bounded like the live path -----------------------------------------------


def test_a_huge_history_user_entry_is_clipped_to_the_live_paths_limit() -> None:
    huge = "x" * 20_000
    message = _only(make_mapper().entry(history_user(huge)), UserMessage)
    assert message.text == huge[:MAX_TEXT_CHARS]
    assert len(message.text) == MAX_TEXT_CHARS


def test_a_huge_history_text_done_is_clipped_to_the_live_paths_limit() -> None:
    huge = "x" * 20_000
    entry = {
        "type": "assistant",
        "apiBlockIndex": 0,
        "message": {"id": "m1", "content": [text(huge)]},
    }
    done = _only(make_mapper().entry(entry), TextDone)
    assert done.text == huge[:MAX_TEXT_CHARS]
    assert len(done.text) == MAX_TEXT_CHARS


# --- tool_question: the permission card's headline ------------------


def test_each_tool_family_asks_its_own_question_not_one_generic_line() -> None:
    """Each tool family gets a sentence of its own. A card that says the same thing for a web
    fetch and a file rewrite tells the user nothing they can decide on."""
    assert tool_question("WebFetch") == "May I read this web page?"
    assert tool_question("Bash") == "May I run this command?"
    assert tool_question("Read") == "May I read this file?"
    assert tool_question("Write") == "May I create or overwrite this file?"
    assert tool_question("WebSearch") == "May I search the web for this?"
    asked = {tool_question(t) for t in ("WebFetch", "Bash", "Read", "Write", "WebSearch")}
    assert len(asked) == 5


def test_a_tool_that_writes_and_one_that_only_reads_never_read_the_same() -> None:
    assert tool_question("Read") != tool_question("Write")
    assert tool_question("Grep") != tool_question("Edit")
    # Write does not assume the path exists: its own sentence, distinct from Edit's.
    assert tool_question("Edit") != tool_question("Write")


def test_an_mcp_tool_is_named_as_external_rather_than_falling_through() -> None:
    assert tool_question("mcp__github__create_issue") == "May I use this external tool?"
    assert tool_question("mcp__") == "May I use this external tool?"


def test_an_unknown_tool_still_asks_something_true() -> None:
    assert tool_question("SomeToolShippedNextYear") == GENERIC_TOOL_QUESTION
    assert tool_question("") == GENERIC_TOOL_QUESTION


def test_every_question_is_a_question_the_owner_can_answer_yes_or_no() -> None:
    tools = ("Bash", "Read", "Write", "Grep", "WebFetch", "WebSearch", "Agent", "mcp__s__t", "?")
    for tool in tools:
        q = tool_question(tool)
        assert q.startswith("May I "), tool
        assert q.endswith("?"), tool


# --- nothing client-bound is unbounded: the fields the model and the hooks write --------------
#
# The limits comment states the rule; these are the fields that used to break it. Each one is
# text this app never wrote, and each one travels the same three-hop path: the WebSocket, the
# session's replay log (kept for the life of the session, so an oversized value is resent on
# every reconnect), and the DOM, where all of them render as a single row.


def test_a_long_hook_message_is_bounded_and_an_honest_one_is_untouched() -> None:
    def hook_frame(stderr: str) -> dict[str, Any]:
        return {
            "type": "system",
            "subtype": "hook_response",
            "hook_name": "PreToolUse:Bash",
            "outcome": "error",
            "exit_code": 2,
            "stderr": stderr,
            "output": "",
        }

    blocked = _only(make_mapper().frame(hook_frame("x" * 50_000)), Hook)
    assert len(blocked.message) == FREE_TEXT_LIMIT and blocked.message.endswith("…")
    # The vault guard's real block message (tests/contracts/events/bash-hook-block.json) is 203
    # characters: the cap has to sit far enough above an honest hook that it never fires on one.
    honest = "Example guard: " + "y" * 188
    assert _only(make_mapper().frame(hook_frame(honest)), Hook).message == honest


def test_a_long_permission_denial_reason_is_bounded_and_still_ansi_stripped() -> None:
    frame = {
        "type": "system",
        "subtype": "permission_denied",
        "tool_name": "Bash",
        "decision_reason": "[31m" + "z" * 50_000,
    }
    denied = _only(make_mapper().frame(frame), PermissionDenied)
    assert len(denied.reason) == FREE_TEXT_LIMIT and denied.reason.endswith("…")
    assert "" not in denied.reason


def test_every_field_of_a_routing_row_is_bounded_without_losing_its_reason() -> None:
    """agent, model and description come out of a model-authored tool_input and the reason out of
    a model-written routing line, so all four are the model's text. The reason must still reach
    the row it belongs to: the cut is applied on the way out, after the agent-name lookup."""
    agent = "a" * 5_000
    mapper = make_mapper()
    mapper.frame(assistant("m1", text(f"▸ routing: {agent} → [{'m' * 5_000}]（{'r' * 5_000}）")))
    started = _only(
        mapper.frame(
            assistant(
                "m2",
                tool_use(
                    "toolu_a",
                    "Task",
                    {"subagent_type": agent, "model": "M" * 5_000, "description": "d" * 5_000},
                ),
            )
        ),
        SubagentStarted,
    )
    for value in (started.agent, started.model, started.description, started.reason):
        assert len(value) == FREE_TEXT_LIMIT and value.endswith("…")


def test_a_bash_head_is_bounded_like_the_rest_of_the_row() -> None:
    """`head` for Bash is the command's first whitespace-delimited token — the model's text, not
    a program name — so a long argv written without spaces is a long head."""
    row = tool_row("Bash", {"command": "x" * 5_000}, VAULT)
    assert len(row.head) == ROW_REST_LIMIT and row.head.endswith("…")
    assert tool_row("Bash", {"command": "x" * 5_000}, VAULT, limit=FREE_TEXT_LIMIT).head.endswith(
        "…"
    )
