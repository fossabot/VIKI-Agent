from __future__ import annotations

import pytest

from viki_agent.messaging.remote_status import TurnStatus, activity, elapsed


def test_elapsed_formats() -> None:
    assert [elapsed(x) for x in (45.9, 185, 3720, -4)] == ["45s", "3m 05s", "1h 02m", "0s"]


def test_line_shows_elapsed_steps_activity_and_note() -> None:
    s = TurnStatus()
    s.observe("turn.started", {}, 100)
    s.observe("tool.started", {"tool": "Grep"}, 101)
    s.observe("text.done", {"text": "  found \n it\ud800 "}, 102)
    assert s.line(145) == "Working · 45s · 1 step · writing\nClaude: found it"
    assert s.line(145, waiting=True) == "Waiting for you · 45s · 1 step\nClaude: found it"
    s.observe("tool.started", {"tool": "Bash"}, 103)
    assert s.phrase == "running a command"
    s.observe("turn.done", {"status": "success"}, 160)
    s.observe("turn.done", {"status": "error"}, 170)
    assert s.line(999) == "Done · 1m 00s · 2 steps"


@pytest.mark.parametrize("tool", ["mcp__secret__leak", "MadeUpTool"])
def test_unknown_and_mcp_tools_never_show_their_names(tool: str) -> None:
    s = TurnStatus()
    s.observe("turn.started", {}, 0)
    s.observe("tool.started", {"tool": tool}, 1)
    assert "secret" not in s.line(2) and "MadeUpTool" not in s.line(2)
    assert activity(tool) in ("using a connected tool", "working")


def test_bad_input_never_raises_and_idle_ignored() -> None:
    s = TurnStatus()
    s.observe("tool.started", {"tool": "Read"}, 1)
    assert s.steps == 0
    s.observe("turn.started", {}, 1)
    s.observe("tool.started", {"tool": 5}, 2)
    s.observe("text.done", None, 2)
    s.observe(None, "x", 2)
    s.observe("text.done", {"text": "x" * 400}, 2)
    assert len(s.note) == 280 and s.note.endswith("…")
    s.observe("status", {"state": "compacting"}, 3)
    assert s.phrase == "compacting the conversation"
    s.observe("rate_limit", {"status": "rejected"}, 3)
    assert s.phrase == "waiting for the usage limit"
    s.observe("turn.done", {"status": "cancelled"}, 4)
    assert s.outcome == "stopped"
