"""The one-line status of a remote turn, built only from fixed phrases.

Nothing here reads a tool name into a string except through ``activity``'s table, so an MCP
or unknown tool name never leaves the computer.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Outcome = Literal["done", "error", "stopped"]
NOTE_CHARS = 280

_ACTIVITY = {
    "Read": "reading files",
    "Glob": "searching files",
    "Grep": "searching files",
    "Edit": "editing files",
    "Write": "editing files",
    "NotebookEdit": "editing files",
    "Bash": "running a command",
    "PowerShell": "running a command",
    "WebFetch": "reading a web page",
    "WebSearch": "searching the web",
    "Agent": "a helper agent is working",
    "Task": "a helper agent is working",
    "Workflow": "a helper agent is working",
    "TodoWrite": "updating its plan",
    "Skill": "using a skill",
}
_HEADS = {"done": "Done", "stopped": "Stopped", "error": "Error"}
_TURN_END: dict[str, Outcome] = {"success": "done", "cancelled": "stopped"}
_STATE_PHRASES = {"compacting": "compacting the conversation", "requesting": "thinking"}


def activity(tool: str) -> str:
    if tool.startswith("mcp__"):
        return "using a connected tool"
    return _ACTIVITY.get(tool, "working")


def elapsed(seconds: float) -> str:
    total = max(0, int(seconds))
    if total < 60:
        return f"{total}s"
    if total < 3600:
        return f"{total // 60}m {total % 60:02d}s"
    return f"{total // 3600}h {total % 3600 // 60:02d}m"


def _clean_note(text: str) -> str:
    text = text.encode("utf-8", "ignore").decode("utf-8")
    text = " ".join(text.split())
    return text if len(text) <= NOTE_CHARS else text[: NOTE_CHARS - 1] + "…"


@dataclass
class TurnStatus:
    started: float | None = None
    ended: float | None = None
    steps: int = 0
    phrase: str = "working"
    note: str = ""
    outcome: Outcome | None = None

    @property
    def running(self) -> bool:
        return self.started is not None and self.ended is None

    def end(self, outcome: Outcome, now: float) -> None:
        if self.ended is None:
            self.ended = now
            self.outcome = outcome

    def observe(self, kind: object, data: object, now: float) -> None:
        try:
            self._observe(kind, data if isinstance(data, dict) else {}, now)
        except Exception:  # noqa: BLE001 - a status line must never break the turn
            return

    def _observe(self, kind: object, data: dict[str, object], now: float) -> None:
        if kind == "turn.started":
            self.started, self.ended, self.outcome = now, None, None
            self.steps, self.phrase, self.note = 0, "working", ""
            return
        if not self.running:
            return
        if kind == "tool.started":
            tool = data.get("tool")
            if isinstance(tool, str):
                self.steps += 1
                self.phrase = activity(tool)
        elif kind == "subagent.started":
            self.phrase = "a helper agent is working"
        elif kind in ("thinking.delta", "thinking.done"):
            self.phrase = "thinking"
        elif kind == "text.delta":
            self.phrase = "writing"
        elif kind == "text.done":
            text = data.get("text")
            if isinstance(text, str) and (note := _clean_note(text)):
                self.note, self.phrase = note, "writing"
        elif kind == "status":
            phrase = _STATE_PHRASES.get(str(data.get("state")))
            if phrase:
                self.phrase = phrase
        elif kind == "rate_limit":
            if data.get("status") == "rejected":
                self.phrase = "waiting for the usage limit"
        elif kind == "turn.done":
            self.end(_TURN_END.get(str(data.get("status")), "error"), now)

    def line(self, now: float, *, waiting: bool = False) -> str:
        if self.outcome is not None:
            head = _HEADS[self.outcome]
        else:
            head = "Waiting for you" if waiting else "Working"
        begin = self.started if self.started is not None else now
        parts = [head, elapsed((self.ended if self.ended is not None else now) - begin)]
        parts.append("1 step" if self.steps == 1 else f"{self.steps} steps")
        if self.running and not waiting:
            parts.append(self.phrase)
        text = " · ".join(parts)
        if self.running and self.note:
            text += "\nClaude: " + self.note
        return text
