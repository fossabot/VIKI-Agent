"""Acceptance (frozen): the mapper against the goldens. Deltas are compared merged per
block, so the tests do not depend on how text is chunked."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from viki_agent.drivers.claude.mapper import Mapper
from viki_agent.drivers.claude.transport import CONTROL_TYPES, FRAME_ERROR
from viki_agent.events import history_item

from replay import load_script

TESTS = Path(__file__).parents[1]
GOLDENS = sorted((TESTS / "contracts" / "events").glob("*.json"))
VAULT = "C:\\Vault"
Item = dict[str, Any]


class Clock:
    now = 0.0

    def __call__(self) -> float:
        return self.now


def merged(items: list[Item]) -> list[Item]:
    """Adjacent deltas of the same block become one delta carrying the joined text."""
    out: list[Item] = []
    for item in items:
        prev = out[-1] if out else None
        if (
            prev
            and item["type"] in ("text.delta", "thinking.delta")
            and prev["type"] == item["type"]
            and prev["data"]["block_id"] == item["data"]["block_id"]
        ):
            prev["data"] = {**prev["data"], "text": prev["data"]["text"] + item["data"]["text"]}
        else:
            out.append({"type": item["type"], "data": dict(item["data"])})
    return out


def run(frames: list[dict[str, Any]], mode: str = "auto") -> list[Item]:
    mapper = Mapper(vault=VAULT, mode=mode, clock=Clock())
    return merged([history_item(e) for f in frames for e in mapper.frame(f)])


def run_history(entries: list[dict[str, Any]]) -> list[Item]:
    mapper = Mapper(vault=VAULT, mode="auto", clock=Clock())
    return [history_item(e) for x in entries for e in mapper.entry(x)]


# --- goldens --------------------------------------------------------------------------------------


@pytest.mark.parametrize("golden", GOLDENS, ids=lambda p: p.stem)
def test_golden(golden: Path) -> None:
    spec = json.loads(golden.read_text(encoding="utf-8"))
    clock = Clock()
    mapper = Mapper(vault=spec["vault"], mode=spec["mode"], clock=clock)
    events = []
    if "fixture" in spec:
        for line in load_script(TESTS / "fixtures" / spec["fixture"]):
            frame = line["frame"]
            if line["dir"] != "out" or frame.get("type") in CONTROL_TYPES:
                continue
            clock.now = line["t_ms"] / 1000
            events += mapper.frame(frame)
    else:
        for entry in load_script(TESTS / "fixtures" / spec["transcript"]):
            events += mapper.entry(entry)
    assert merged([history_item(e) for e in events]) == spec["events"]


# --- synthetic frames -----------------------------------------------------------------------------


def msg_start(msg: str, parent: str | None = None) -> dict[str, Any]:
    event = {"type": "message_start", "message": {"id": msg}}
    return {"type": "stream_event", "event": event, "parent_tool_use_id": parent}


def block_start(index: int, kind: str, parent: str | None = None) -> dict[str, Any]:
    event = {"type": "content_block_start", "index": index, "content_block": {"type": kind}}
    return {"type": "stream_event", "event": event, "parent_tool_use_id": parent}


def delta(index: int, text: str, kind: str = "text_delta", parent: str | None = None) -> dict:
    key = "thinking" if kind == "thinking_delta" else "text"
    event = {"type": "content_block_delta", "index": index, "delta": {"type": kind, key: text}}
    return {"type": "stream_event", "event": event, "parent_tool_use_id": parent}


def assistant(msg: str, block: dict[str, Any], parent: str | None = None) -> dict[str, Any]:
    message = {"id": msg, "role": "assistant", "content": [block]}
    return {"type": "assistant", "message": message, "parent_tool_use_id": parent}


def text(value: str) -> dict[str, Any]:
    return {"type": "text", "text": value}


def tool_use(tool_id: str, name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
    return {"type": "tool_use", "id": tool_id, "name": name, "input": tool_input}


def result_frame_for(
    tool_id: str,
    content: Any,
    *,
    is_error: bool | None = False,
    structured: Any = None,
    parent: str | None = None,
) -> dict[str, Any]:
    block = {
        "type": "tool_result",
        "tool_use_id": tool_id,
        "content": content,
        "is_error": is_error,
    }
    return {
        "type": "user",
        "message": {"role": "user", "content": [block]},
        "parent_tool_use_id": parent,
        "tool_use_result": structured,
    }


def system(subtype: str, **fields: Any) -> dict[str, Any]:
    return {"type": "system", "subtype": subtype, **fields}


# --- routing lines --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "line",
    [
        "▸ routing: researcher → [opus]（Web横断＋一次資料確認）",
        "▸ routing: researcher → opus（Web横断＋一次資料確認）",
        "▸ routing: researcher → [opus](Web横断＋一次資料確認)",
    ],
)
def test_a_routing_line_leaves_the_reply_and_its_reason_joins_the_subagent(line: str) -> None:
    body = f"{line}\nLooking into it."
    frames = [
        msg_start("m1"),
        block_start(0, "text"),
        delta(0, line[:9]),
        delta(0, line[9:] + "\nLooking"),
        delta(0, " into it."),
        assistant("m1", text(body)),
        assistant(
            "m1",
            tool_use(
                "toolu_a",
                "Agent",
                {"subagent_type": "researcher", "model": "opus", "description": "Survey"},
            ),
        ),
    ]
    assert run(frames) == [
        {"type": "text.delta", "data": {"block_id": "m1:0", "text": "Looking into it."}},
        {"type": "text.done", "data": {"block_id": "m1:0", "text": "Looking into it."}},
        {
            "type": "subagent.started",
            "data": {
                "block_id": "toolu_a",
                "agent": "researcher",
                "model": "opus",
                "description": "Survey",
                "reason": "Web横断＋一次資料確認",
            },
        },
    ]


def test_other_marker_lines_are_held_then_released_intact() -> None:
    frames = [
        msg_start("m1"),
        block_start(0, "text"),
        delta(0, "▸ step one"),
        delta(0, "\nnext"),
        assistant("m1", text("▸ step one\nnext")),
    ]
    assert run(frames) == [
        {"type": "text.delta", "data": {"block_id": "m1:0", "text": "▸ step one\nnext"}},
        {"type": "text.done", "data": {"block_id": "m1:0", "text": "▸ step one\nnext"}},
    ]


def test_subagent_defaults_and_workflow() -> None:
    frames = [
        assistant("m1", tool_use("toolu_a", "Task", {"description": "Look around"})),
        assistant("m1", tool_use("toolu_w", "Workflow", {"description": "Review"})),
    ]
    assert [(i["type"], i["data"]["agent"], i["data"]["model"]) for i in run(frames)] == [
        ("subagent.started", "general-purpose", "inherit"),
        ("subagent.started", "workflow", "inherit"),
    ]


def test_subagent_text_and_deltas_stay_silent() -> None:
    frames = [
        msg_start("s1", parent="toolu_a"),
        block_start(0, "text", parent="toolu_a"),
        delta(0, "inner", parent="toolu_a"),
        assistant("s1", text("inner"), parent="toolu_a"),
    ]
    assert run(frames) == []


# --- tool rows ------------------------------------------------------------------------------------

ROWS = [
    ("Bash", {"command": "rg -il   linter  40-sources"}, "rg", "-il linter 40-sources"),
    ("PowerShell", {"command": "Get-ChildItem _templates"}, "Get-ChildItem", "_templates"),
    ("Read", {"file_path": "C:\\Vault\\40-sources\\Note.md"}, "Read", "40-sources/Note.md"),
    ("Read", {"file_path": "c:/vault/30-knowledge/x.md"}, "Read", "30-knowledge/x.md"),
    ("Read", {"file_path": "D:\\elsewhere\\a.md"}, "Read", "D:\\elsewhere\\a.md"),
    (
        "Edit",
        {"file_path": "C:\\Vault\\Home.md", "old_string": "a", "new_string": "b"},
        "Edit",
        "Home.md",
    ),
    ("Write", {"file_path": "C:\\Vault\\00-inbox\\n.md", "content": "x"}, "Write", "00-inbox/n.md"),
    (
        "NotebookEdit",
        {"notebook_path": "C:\\Vault\\n.ipynb", "new_source": "x"},
        "NotebookEdit",
        "n.ipynb",
    ),
    ("Grep", {"pattern": "linter", "path": "C:\\Vault\\40-sources"}, "Grep", "linter 40-sources"),
    ("Glob", {"pattern": "**/*.md"}, "Glob", "**/*.md"),
    (
        "WebFetch",
        {"url": "https://example.com/a/b?c=1", "prompt": "title"},
        "WebFetch",
        "example.com",
    ),
    ("WebSearch", {"query": "markdown linting"}, "WebSearch", "markdown linting"),
    (
        "mcp__claude_ai_Example__search_threads",
        {"query": "from:x"},
        "claude_ai_Example·search_threads",
        "from:x",
    ),
    ("TodoWrite", {"todos": [{"content": "x"}]}, "TodoWrite", ""),
    ("Skill", {"skill": "capture", "args": "note"}, "Skill", "capture"),
]


@pytest.mark.parametrize(
    ("tool", "tool_input", "head", "rest"), ROWS, ids=[f"{r[0]}-{i}" for i, r in enumerate(ROWS)]
)
def test_tool_rows(tool: str, tool_input: dict[str, Any], head: str, rest: str) -> None:
    (item,) = run([assistant("m1", tool_use("toolu_1", tool, tool_input))])
    assert item == {
        "type": "tool.started",
        "data": {
            "block_id": "toolu_1",
            "tool": tool,
            "row": {"head": head, "rest": rest, "tail": ""},
            "parent_id": "",
        },
    }


def test_long_rows_are_cut_to_160_characters() -> None:
    command = "echo " + "x" * 300
    (item,) = run([assistant("m1", tool_use("toolu_1", "Bash", {"command": command}))])
    assert item["data"]["row"]["rest"] == ("x" * 300)[:159] + "…"


def test_subagent_tools_carry_their_parent() -> None:
    (item,) = run(
        [assistant("s1", tool_use("toolu_2", "Read", {"file_path": "a.md"}), parent="toolu_a")]
    )
    assert item["data"]["parent_id"] == "toolu_a"


# --- tool results ---------------------------------------------------------------------------------


def done_for(tool: str, tool_input: dict[str, Any], content: Any, **result: Any) -> Item:
    items = run(
        [
            assistant("m1", tool_use("toolu_1", tool, tool_input)),
            result_frame_for("toolu_1", content, **result),
        ]
    )
    assert items[0]["type"] == "tool.started"
    assert items[1]["type"] == "tool.done"
    return items[1]["data"]


def test_bash_exit_code_becomes_the_tail() -> None:
    data = done_for("Bash", {"command": "false"}, "Exit code 1\nboom\r\n", is_error=True)
    assert data == {
        "block_id": "toolu_1",
        "is_error": True,
        "summary": "Exit code 1",
        "preview": "Exit code 1\nboom",
        "tail": "→ exit 1",
    }


@pytest.mark.parametrize("tool", ["Grep", "Glob"])
def test_search_tools_count_files(tool: str) -> None:
    data = done_for(
        tool, {"pattern": "x"}, "a.md\nb.md", structured={"numFiles": 7, "filenames": []}
    )
    assert data["tail"] == "→ 7 files"


def test_edit_and_write_count_lines() -> None:
    patch = [
        {
            "oldStart": 1,
            "oldLines": 2,
            "newStart": 1,
            "newLines": 3,
            "lines": [" a", "-b", "+c", "+d"],
        }
    ]
    edit = done_for("Edit", {"file_path": "x.md"}, "ok", structured={"structuredPatch": patch})
    assert edit["tail"] == "+2 −1"
    create = done_for(
        "Write",
        {"file_path": "n.md"},
        "ok",
        structured={"type": "create", "content": "l1\nl2\n", "structuredPatch": []},
    )
    assert create["tail"] == "+2"


def test_previews_and_summaries_are_bounded() -> None:
    many = "\n".join(f"line {i}" for i in range(50))
    data = done_for("Bash", {"command": "seq"}, many)
    assert data["preview"] == "\n".join(f"line {i}" for i in range(40)) + "…"
    wide = "y" * 3000
    data = done_for("Bash", {"command": "cat"}, wide)
    assert data["preview"] == "y" * 2048 + "…"
    assert data["summary"] == "y" * 119 + "…"


def test_text_blocks_in_a_result_are_joined() -> None:
    content = [
        {"type": "text", "text": "a"},
        {"type": "image", "source": {}},
        {"type": "text", "text": "b"},
    ]
    assert done_for("Read", {"file_path": "x.md"}, content)["preview"] == "a\nb"


def test_subagent_ends_once_whichever_signal_comes_first() -> None:
    start = assistant("m1", tool_use("toolu_a", "Agent", {"subagent_type": "Explore"}))
    notified = run(
        [
            start,
            system("task_notification", task_id="t1", tool_use_id="toolu_a", status="stopped"),
            result_frame_for("toolu_a", "partial"),
        ]
    )
    assert [(i["type"], i["data"].get("status")) for i in notified] == [
        ("subagent.started", None),
        ("subagent.done", "stopped"),
    ]
    failed = run([start, result_frame_for("toolu_a", "boom", is_error=True)])
    assert failed[-1] == {
        "type": "subagent.done",
        "data": {"block_id": "toolu_a", "status": "failed"},
    }


# --- system frames and results --------------------------------------------------------------------


def test_mode_changes_and_compaction_become_status_events() -> None:
    frames = [
        system("init", permissionMode="auto"),
        system("status", status="requesting"),
        system("status", status=None, permissionMode="plan"),
        system("status", status=None, permissionMode="plan"),
        system("status", status="compacting", permissionMode="default"),
    ]
    assert run(frames) == [
        {"type": "status", "data": {"state": "", "mode": "plan"}},
        {"type": "status", "data": {"state": "compacting", "mode": "default"}},
    ]


def test_hooks_surface_only_when_they_block_or_fail() -> None:
    frames = [
        system(
            "hook_response", hook_name="Stop", exit_code=0, outcome="success", stderr="", output=""
        ),
        system(
            "hook_response",
            hook_name="PreToolUse:Bash",
            exit_code=2,
            outcome="error",
            stderr="denied\r\n",
            output="denied\r\n",
        ),
        system(
            "hook_response",
            hook_name="PostToolUse",
            exit_code=1,
            outcome="error",
            stderr="",
            output="crashed\n",
        ),
    ]
    assert run(frames) == [
        {
            "type": "hook",
            "data": {"name": "PreToolUse:Bash", "outcome": "blocked", "message": "denied"},
        },
        {"type": "hook", "data": {"name": "PostToolUse", "outcome": "error", "message": "crashed"}},
    ]


def test_other_system_frames() -> None:
    frames = [
        system("compact_boundary", compact_metadata={"trigger": "manual", "pre_tokens": 12345}),
        # The CLI's keys (2.1.259): decision_reason, decision_reason_type, message; no "reason".
        system(
            "permission_denied",
            tool_name="Bash",
            tool_use_id="toolu_d",
            decision_reason_type="classifier",
            decision_reason="\x1b[31mblocked\x1b[0m by auto mode",
            message="Permission denied",
        ),
        {
            "type": "rate_limit_event",
            "rate_limit_info": {"status": "allowed", "rateLimitType": "five_hour", "resetsAt": 1},
        },
        {
            "type": "rate_limit_event",
            "rate_limit_info": {
                "status": "allowed_warning",
                "rateLimitType": "five_hour",
                "resetsAt": 1789128000,
            },
        },
        {"type": FRAME_ERROR, "reason": "too_large"},
        {"type": "viki.exit", "code": 0, "stderr": ""},
        system("thinking_tokens", estimated_tokens=5),
        {"type": "something_new", "x": 1},
    ]
    assert run(frames) == [
        {"type": "compacted", "data": {"trigger": "manual", "pre_tokens": 12345}},
        {"type": "permission.denied", "data": {"tool": "Bash", "reason": "blocked by auto mode"}},
        {
            "type": "rate_limit",
            "data": {"status": "allowed_warning", "kind": "five_hour", "resets_at": 1789128000},
        },
        {
            "type": "error",
            "data": {
                "code": "frame_error",
                "message": "Some of the received data could not be read.",
                "recoverable": True,
            },
        },
    ]


@pytest.mark.parametrize(
    ("result", "status"),
    [
        ({"subtype": "success", "is_error": False, "terminal_reason": "completed"}, "success"),
        (
            {
                "subtype": "error_during_execution",
                "is_error": True,
                "terminal_reason": "aborted_streaming",
            },
            "cancelled",
        ),
        (
            {"subtype": "success", "is_error": False, "terminal_reason": "aborted_tools"},
            "cancelled",
        ),
        ({"subtype": "success", "is_error": True, "terminal_reason": "completed"}, "error"),
        ({"subtype": "error_max_turns", "is_error": True}, "error"),
    ],
)
def test_turn_status(result: dict[str, Any], status: str) -> None:
    (item,) = run([{"type": "result", **result}])
    assert item == {
        "type": "turn.done",
        "data": {
            "status": status,
            "usage": {
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_tokens": 0,
                "cache_creation_tokens": 0,
            },
            "context_percent": None,
            "duration_ms": 0,
        },
    }


def test_thinking_duration_uses_the_clock() -> None:
    clock = Clock()
    mapper = Mapper(vault=VAULT, mode="auto", clock=clock)
    clock.now = 10.0
    mapper.frame(msg_start("m1"))
    mapper.frame(block_start(0, "thinking"))
    clock.now = 16.5
    (done,) = mapper.frame(assistant("m1", {"type": "thinking", "thinking": "", "signature": "s"}))
    assert history_item(done) == {
        "type": "thinking.done",
        "data": {"block_id": "m1:0", "text": "", "duration_ms": 6500},
    }


# --- history entries ------------------------------------------------------------------------------


def user_entry(content: Any, **extra: Any) -> dict[str, Any]:
    return {
        "type": "user",
        "message": {"role": "user", "content": content},
        "timestamp": "2026-09-11T08:00:00.000Z",
        **extra,
    }


def assistant_entry(msg: str, index: int, block: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "assistant",
        "apiBlockIndex": index,
        "message": {"id": msg, "role": "assistant", "content": [block]},
    }


def test_history_entries() -> None:
    command = (
        "<command-name>/proofread</command-name>\n"
        "<command-message>proofread</command-message>\n"
        "<command-args>draft notes</command-args>"
    )
    entries = [
        user_entry("Hello"),
        user_entry("expanded skill body", isMeta=True),
        user_entry(command),
        user_entry("<local-command-stdout>output</local-command-stdout>"),
        {"type": "attachment", "attachment": {"type": "instructions", "files": []}},
        assistant_entry("m1", 0, {"type": "thinking", "thinking": "hmm", "signature": "s"}),
        assistant_entry("m1", 2, text("Answer")),
        assistant_entry("m1", 3, tool_use("toolu_g", "Grep", {"pattern": "x"})),
        user_entry(
            [{"type": "tool_result", "tool_use_id": "toolu_g", "content": "a.md\nb.md"}],
            toolUseResult={"numFiles": 2},
        ),
        {"type": "summary", "summary": "x"},
    ]
    at = "2026-09-11T08:00:00.000Z"
    assert run_history(entries) == [
        {"type": "user.message", "data": {"text": "Hello", "command": "", "at": at}},
        {
            "type": "user.message",
            "data": {"text": "draft notes", "command": "/proofread", "at": at},
        },
        {"type": "thinking.done", "data": {"block_id": "m1:0", "text": "hmm", "duration_ms": 0}},
        {"type": "text.done", "data": {"block_id": "m1:2", "text": "Answer"}},
        {
            "type": "tool.started",
            "data": {
                "block_id": "toolu_g",
                "tool": "Grep",
                "row": {"head": "Grep", "rest": "x", "tail": ""},
                "parent_id": "",
            },
        },
        {
            "type": "tool.done",
            "data": {
                "block_id": "toolu_g",
                "is_error": False,
                "summary": "a.md",
                "preview": "a.md\nb.md",
                "tail": "→ 2 files",
            },
        },
    ]
