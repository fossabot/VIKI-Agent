"""Map Claude frames and transcript entries to browser events.

Each live session and history read gets its own mapper instance.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import replace
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import urlsplit

from viki_agent.conversations.artifacts import (
    ArtifactDetailRef,
    ArtifactError,
    ArtifactStart,
    classify_artifact,
    observe_result,
)
from viki_agent.conversations.stream_state import MAX_TEXT_CHARS
from viki_agent.drivers.claude.transport import FRAME_ERROR
from viki_agent.events import (
    Compacted,
    Error,
    Event,
    Hook,
    PermissionDenied,
    RateLimit,
    Status,
    SubagentDone,
    SubagentStarted,
    TextDelta,
    TextDone,
    ThinkingDelta,
    ThinkingDone,
    ToolDone,
    ToolRow,
    ToolStarted,
    TurnDone,
    TurnStatus,
    UnavailableAttachment,
    Usage,
    UserMessage,
)
from viki_agent.turn_input import IMAGE_MEDIA_TYPES, Attachment

FRAME_ERROR_MESSAGE = "Some of the received data could not be read."

_SUBAGENT_TOOLS = ("Agent", "Task", "Workflow")
_PATH_TOOLS = ("Read", "Edit", "Write")
_SUBAGENT_STATUSES = ("completed", "failed", "stopped")

# The model group is anchored on the reason that follows it, not on "]": the user's own models
# are written "opus[1m]" and "claude-opus-5[1m]", so a group that stopped at the first "]" cut
# the name in half and then failed to match the closing bracket at all — the routing rows this
# app exists to show would silently not appear for exactly the models it is run with.
# Each group is bounded by the delimiter that ends it rather than left lazy. Two adjacent lazy
# groups can trade characters across " → " and "（", and the cost of that is cubic: this pattern
# is fullmatch-ed against every completed line of every delta and every line of every finished
# text block, on the event loop, and catalog.py runs the same mapper over history pages and
# search. A 4.6 KB line beginning "▸ routing: " that never closes its bracket took 0.62 s before
# this bound; once such a line is in a transcript it is replayed on every open and every search.
# The agent cannot contain "→" and the model cannot contain "(" or "（", so bounding them changes
# no match this app can produce — it only removes the ambiguity that made the failure possible.
# The model also ends on a non-space. A model that could end in spaces and the `\s*` after it
# matched the same spaces, so every split of a run of spaces was tried: "→ b", 16,000 spaces, "("
# and no closing bracket took 3 s, quadratic in the run. The subagent transcript route maps a
# subagent's own text with this pattern, in a worker thread that holds the GIL, on every poll.
# A model that ends in spaces — a blank model, or spaces before a closing bracket, the only
# malformed cases — does not match, keeps those spaces inside a longer model ("[b ]" reads
# "b ]"), or, after an unclosed "[", reads that bracket as the model ("[ (x)" reads "[").
_ROUTING_RE = re.compile(
    r"▸ routing: (?P<agent>[^→]+) → \[?(?P<model>[^(（]*?[^(（\s])\]?\s*[\(（](?P<reason>.*)[\)）]"
)
_COMMAND_OPENERS = ("<command-name>", "<command-message>")
_WORKFLOW_META_RE = re.compile(r"export\s+const\s+meta\s*=\s*\{[^}]*?name\s*:\s*['\"](.*?)['\"]")
# Transcript user text the user did not type: IDE context blocks and injected notices.
_INJECTED = (
    "<ide_",
    "<system-reminder>",
    "<task-notification>",
    "<local-command-stdout>",
    "[Request interrupted",
)
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_EXIT_CODE_RE = re.compile(r"^Exit code (\d+)")

_HISTORY_IMAGE_LIMIT = 10
_HISTORY_IMAGE_BYTES = 50 << 20
_HISTORY_IMAGE_MAX_BYTES = 5 << 20
_HISTORY_MEDIA_TYPES = frozenset(IMAGE_MEDIA_TYPES)
_HISTORY_EXTENSIONS = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}

HistoryMedia = Callable[[str, str, int, str], Attachment | None]


# --- pure helpers (also usable outside Mapper) -----------------------------------------------


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _vault_relative(path: str, vault: str) -> str:
    """`path` relative to `vault` with `/` separators, when it is under the vault (case- and
    separator-insensitive); otherwise `path` unchanged."""
    norm_path = path.replace("\\", "/")
    norm_vault = vault.replace("\\", "/")
    if norm_path.lower() == norm_vault.lower():
        return ""
    prefix = norm_vault.lower() + "/"
    if norm_path.lower().startswith(prefix):
        return norm_path[len(prefix) :]
    return path


def _first_string_value(data: dict[str, Any]) -> str:
    for value in data.values():
        if isinstance(value, str):
            return value
    return ""


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


# Command and task-notification tags are found with str.find, not the lazy DOTALL regexes that
# tests/test_mapper.py keeps as their oracle: those backtracked for seconds over a few tens of KB
# of tags that never closed, and the subagent transcript route maps model-written prompts and
# messages through `Mapper._history_user`, in a worker thread that holds the GIL, on every poll.
# Each answer is the regex's: when the first opener has no closer after it, no later opener has
# one, and a later closer only starts the search for the next tag later.


def _between(text: str, opener: str, closer: str, start: int = 0) -> tuple[str, int] | None:
    """The text from the first `opener` at or after `start` to the first `closer` after it, and
    the index just past that closer; None when either is missing."""
    begin = text.find(opener, start)
    if begin == -1:
        return None
    begin += len(opener)
    end = text.find(closer, begin)
    return None if end == -1 else (text[begin:end], end + len(closer))


def _parse_command(content: str) -> tuple[str, str | None] | None:
    """A command entry's name, and the args after it (None when missing or unclosed); None when
    the name is missing or unclosed."""
    name = _between(content, "<command-name>", "</command-name>")
    if name is None:
        return None
    args = _between(content, "<command-args>", "</command-args>", name[1])
    return name[0], args[0] if args else None


def _parse_task_notification(content: str) -> tuple[str, str] | None:
    """A task notification's tool-use id and the status after it; None when either is missing or
    unclosed."""
    tool_id = _between(content, "<tool-use-id>", "</tool-use-id>")
    if tool_id is None:
        return None
    status = _between(content, "<status>", "</status>", tool_id[1])
    return (tool_id[0], status[0]) if status else None


# How much of a tool's argument each surface carries. A conversation row is one line in a panel
# that shows three at a time, so 160 characters is generous there. A permission card is not a
# row: it is the sentence the user is being asked to authorise, and a command they cannot read to
# the end is one they cannot judge. Shell cards must fit this larger limit in full; other
# permission summaries use it as their row limit, the same 2 KB the tool preview uses.
ROW_REST_LIMIT = 160
CARD_REST_LIMIT = 2000

# A card for a tool this module has no grammar for (an MCP tool, or one newer than this list)
# shows every argument, so one long value must not push the others off it: string values are cut
# to the first length here that lets the whole text fit, each cut said where it was made.
_ARGUMENT_CUTS = (400, 120, 40, 12)
# Arguments nested deeper than this are written as "…": honest input stops well short of it.
_ARGUMENT_DEPTH = 8
# Opens such a card's arguments when any part of them was left out, so the reader knows before
# the first argument that this is not the whole request.
ARGUMENTS_SHORTENED = "(arguments shortened to fit) "

# The same "nothing client-bound is unbounded" rule, for the event fields whose text this app
# did not shape: a routing row's agent, model and description come straight out of a
# model-authored tool_input, its reason out of a model-written "▸ routing:" line, and a hook's
# message and a permission denial's reason are whatever a hook or the CLI's classifier printed.
# Each goes on the wire, into the session's replay log — kept for the life of the session so a
# reconnecting browser can catch up, which means one oversized field is paid for again on every
# reconnect — and into the DOM as a single row. 2 KB is far past any honest value (the longest
# in any fixture is a 203-character hook message), so the cut only ever fires on text that could
# never have read as a row anyway.
FREE_TEXT_LIMIT = 2000
# Incomplete live routing candidates share this hard budget, independent of history clipping.
MAX_ROUTING_CHARS = 64 << 10


class MapperOverflow(BufferError):
    """Live output exceeded the routing scratch budget; the driver must end the attempt."""


def _argument_text(value: Any, cut: int, depth: int, room: int) -> tuple[str, bool]:
    """`value` as JSON text, and whether that is all of it.

    A string past `cut` characters is closed there and followed by how many more it holds. A
    container nested past `depth` is written as "…", and so is the rest of one whose text has
    outgrown `room`. `room` bounds the work as well as the text: the arguments are the model's,
    and none of them is read further than a card could show.
    """
    if isinstance(value, str):
        if len(value) <= cut:
            return json.dumps(value, ensure_ascii=False), True
        more = len(value) - cut
        return f"{json.dumps(value[:cut], ensure_ascii=False)}… ({more} more characters)", False
    if not isinstance(value, dict | list | tuple):
        return json.dumps(value, ensure_ascii=False, default=str), True
    opener, closer = "{}" if isinstance(value, dict) else "[]"
    if not value:
        return opener + closer, True
    if depth == 0:
        return f"{opener}…{closer}", False
    pairs = value.items() if isinstance(value, dict) else ((None, item) for item in value)
    parts: list[str] = []
    whole = True
    used = 1
    for key, item in pairs:
        if used > room:
            parts.append("…")
            whole = False
            break
        text, entire = _argument_text(item, cut, depth - 1, room - used)
        if key is not None:
            name, named = _argument_text(str(key), cut, 0, 0)
            text, entire = f"{name}: {text}", entire and named
        parts.append(text)
        whole = whole and entire
        used += len(text) + 2
    return opener + ", ".join(parts) + closer, whole


def _arguments(tool_input: dict[str, Any], limit: int) -> str:
    """Every argument of a tool this module has no grammar for, as a permission card shows them.

    One line of JSON in the tool's own key order: exactly `json.dumps(tool_input,
    ensure_ascii=False)` when that fits `limit`, so nothing between the braces is this module's
    wording. Otherwise the text opens with `ARGUMENTS_SHORTENED`, keeps every key it can by
    cutting long values (`_ARGUMENT_CUTS`), and only then stops at `limit`. The same input always
    gives the same text, and a line break inside a value stays an escape inside its quotes.
    """
    if not tool_input:
        return ""
    text, whole = _argument_text(tool_input, limit, _ARGUMENT_DEPTH, limit)
    if whole and len(text) <= limit:
        return text
    room = limit - len(ARGUMENTS_SHORTENED)
    for cut in _ARGUMENT_CUTS:
        text, _ = _argument_text(tool_input, cut, _ARGUMENT_DEPTH, room)
        if len(text) <= room:
            break
    return ARGUMENTS_SHORTENED + _cut(text, room)


def _unlisted_rest(tool_input: dict[str, Any], limit: int) -> str:
    """What follows the name of a tool this module has no grammar for.

    A row is one line and names the tool's first string argument. A permission card, which is
    any `limit` past a row's, is what the user authorises, and one argument out of several is
    not that: it carries them all.
    """
    if limit > ROW_REST_LIMIT:
        return _arguments(tool_input, limit)
    return _first_string_value(tool_input)


def tool_row(
    tool: str, tool_input: dict[str, Any], vault: str, *, limit: int = ROW_REST_LIMIT
) -> ToolRow:
    """Build a tool row's `head` and `rest`; `tail` is filled when the result is known.

    `limit` bounds both fields. `ROW_REST_LIMIT` and `CARD_REST_LIMIT` keep row and card
    truncation independent, and a `limit` past a row's also says the text is for a card
    (`_unlisted_rest`).
    """
    if tool in ("Bash", "PowerShell"):
        tokens = str(tool_input.get("command", "")).split()
        head = tokens[0] if tokens else ""
        rest = " ".join(tokens[1:])
    elif tool in _PATH_TOOLS:
        head = tool
        rest = _vault_relative(str(tool_input.get("file_path", "")), vault)
    elif tool == "NotebookEdit":
        head = "NotebookEdit"
        rest = _vault_relative(str(tool_input.get("notebook_path", "")), vault)
    elif tool in ("Grep", "Glob"):
        head = tool
        parts = [str(tool_input.get("pattern", ""))]
        path = tool_input.get("path")
        if path:
            parts.append(_vault_relative(str(path), vault))
        rest = " ".join(parts)
    elif tool == "WebFetch":
        head = "WebFetch"
        url = str(tool_input.get("url", ""))
        try:
            rest = urlsplit(url).hostname or ""
        except ValueError:
            rest = url  # a host Python can't split (e.g. full-width ／): show the raw URL
    elif tool == "WebSearch":
        head = "WebSearch"
        rest = str(tool_input.get("query", ""))
    elif tool == "Skill":
        head = "Skill"
        skill = tool_input.get("skill")
        rest = skill if isinstance(skill, str) else ""
    elif tool == "Workflow":
        # Not the generic branch: a Workflow's first string argument is its entire script, and
        # `rest` is one line in a panel. Its own name is what a row (and a card) can use.
        head = "Workflow"
        rest = _workflow_description(tool_input)
    elif tool.startswith("mcp__"):
        parts = tool.split("__", 2)
        server = parts[1] if len(parts) > 1 else ""
        name = parts[2] if len(parts) > 2 else ""
        head = f"{server}·{name}"
        rest = _unlisted_rest(tool_input, limit)
    else:
        head = tool
        rest = _unlisted_rest(tool_input, limit)
    # `head` is cut on the same limit: for Bash and PowerShell it is the command's first
    # whitespace-delimited token, which is the model's text and not a program name — a long
    # argv written without spaces puts the whole command in the head.
    return ToolRow(head=_cut(head, limit), rest=_cut(rest, limit), tail="")


# A permission card asks its question in plain words, worded from what the
# tool is about to do — "May I read this web page?" for a fetch, not one generic line
# for everything. The wording lives beside tool_row because it is the same per-tool knowledge, and
# on the wire rather than in the browser for the same reason the row grammar is. Write asks
# "create or overwrite" rather than assuming either: the mapper cannot and must not check the
# filesystem, so it cannot know whether the path already exists (Edit, only ever applied to a
# file that already exists, keeps the plain overwrite phrasing).
_TOOL_QUESTIONS: dict[str, str] = {
    "Bash": "May I run this command?",
    "PowerShell": "May I run this command?",
    "Read": "May I read this file?",
    "Edit": "May I change this file?",
    "Write": "May I create or overwrite this file?",
    "NotebookEdit": "May I change this notebook?",
    "Grep": "May I search files with this pattern?",
    "Glob": "May I search files with this pattern?",
    "WebFetch": "May I read this web page?",
    "WebSearch": "May I search the web for this?",
    "Agent": "May I run a subagent?",
    "Task": "May I run a subagent?",
    # Workflow is a subagent tool everywhere else in this module (_SUBAGENT_TOOLS); without it
    # here the card fell through to the generic sentence AND to the generic row, whose "first
    # string argument" is a Workflow's whole script source.
    "Workflow": "May I run a subagent?",
}

GENERIC_TOOL_QUESTION = "May I do this?"


def tool_question(tool: str) -> str:
    """The permission card's headline for `tool`, in English.

    An MCP tool is named by its server, which the user chose to install, so it gets its own
    sentence rather than the catch-all. Anything genuinely unknown falls back to the generic
    line — the card must still say something true about a tool this app has never seen."""
    if tool.startswith("mcp__"):
        return "May I use this external tool?"
    return _TOOL_QUESTIONS.get(tool, GENERIC_TOOL_QUESTION)


def _result_text(content: Any) -> str:
    """The tool_result content as text: a string as-is, or its text blocks joined with "\n"."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            str(block.get("text", ""))
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        return "\n".join(parts)
    return ""


def _preview(text: str) -> str:
    text = text.replace("\r\n", "\n")
    lines = [line.rstrip() for line in text.split("\n")]
    while lines and lines[-1] == "":
        lines.pop()
    cut = False
    if len(lines) > 40:
        lines = lines[:40]
        cut = True
    joined = "\n".join(lines)
    if len(joined) > 2048:
        joined = joined[:2048]
        cut = True
    return joined + "…" if cut else joined


def _summary(preview: str) -> str:
    for line in preview.split("\n"):
        if line:
            return _cut(line, 120)
    return ""


def _tail(tool: str, text: str, structured: Any) -> str:
    if tool in ("Bash", "PowerShell"):
        match = _EXIT_CODE_RE.match(text)
        return f"→ exit {match.group(1)}" if match else ""
    if not isinstance(structured, dict):
        return ""
    if tool == "Grep" and structured.get("mode") == "content" and "numLines" in structured:
        return f"→ {structured['numLines']} lines"
    if tool in ("Grep", "Glob") and "numFiles" in structured:
        return f"→ {structured['numFiles']} files"
    if tool in ("Edit", "Write"):
        patch = structured.get("structuredPatch")
        if patch:
            added = sum(1 for hunk in patch for ln in hunk.get("lines", []) if ln.startswith("+"))
            removed = sum(1 for hunk in patch for ln in hunk.get("lines", []) if ln.startswith("-"))
            return f"+{added} −{removed}"
        if tool == "Write" and structured.get("type") == "create":
            content = str(structured.get("content", ""))
            return f"+{len(content.splitlines())}"
    return ""


def _turn_status(result: dict[str, Any]) -> TurnStatus:
    if result.get("terminal_reason") in ("aborted_streaming", "aborted_tools"):
        return "cancelled"
    if result.get("subtype") == "success" and not result.get("is_error"):
        return "success"
    return "error"


# --- the mapper -------------------------------------------------------------------------------


class Mapper:
    def __init__(
        self,
        *,
        vault: str,
        mode: str,
        clock: Callable[[], float] = time.monotonic,
        history_media: HistoryMedia | None = None,
        artifacts_enabled: bool = False,
        text_limit: int | None = MAX_TEXT_CHARS,
    ) -> None:
        """`vault`: the vault path, for vault-relative tool paths. `mode`: the permission mode the
        session started in (a later init/status with a different mode emits a `status` event).
        `clock`: seconds, used only for thinking durations."""
        self._vault = vault
        self._mode = mode
        self._clock = clock
        self._history_media = history_media
        self._artifacts_enabled = artifacts_enabled
        self._text_limit = text_limit
        self._artifacts: dict[str, ArtifactStart] = {}
        self._current_message_id = ""
        self._msg_block_count: dict[str, int] = {}
        self._thinking_start: dict[str, float] = {}
        self._tool_names: dict[str, str] = {}
        self._subagent_ids: set[str] = set()
        self._subagent_done: set[str] = set()
        self._subagent_starts: dict[str, SubagentStarted] = {}
        self._task_tools: dict[str, str] = {}
        self._pending_reasons: dict[str, list[str]] = {}
        self._held: dict[str, str] = {}
        self._held_chars = 0
        self._at_line_start: dict[str, bool] = {}
        self._delta_emitted: set[str] = set()
        self._compacting = False

    def note_subagents(self, tool_ids: Iterable[str]) -> None:
        """Tell this Mapper about subagent launches it did not see itself.

        The live path keeps one Mapper for a whole session, so a launch and the notification that
        closes it are always seen by the same instance. History does not: `catalog.history()`
        builds a fresh Mapper per page, so a background agent launched on an older page and
        reported on a newer one would have its `subagent.done` dropped, and the launch's own
        tool_result mapped as an ordinary `tool.done`. Only the launches are noted, never the
        closes — this page's own closes must still be free to fire.
        """
        self._subagent_ids |= set(tool_ids)

    # --- live path ------------------------------------------------------------------------

    def frame(self, frame: dict[str, Any]) -> list[Event]:
        """One live stdout frame (never a control frame) → zero or more events, in order."""
        kind = frame.get("type")
        if kind == "stream_event":
            return self._stream_event(frame)
        if kind == "assistant":
            return self._assistant(frame)
        if kind == "user":
            return self._user(frame)
        if kind == "system":
            return self._system(frame)
        if kind == "rate_limit_event":
            return self._rate_limit(frame)
        if kind == "result":
            return self._result(frame)
        if kind == FRAME_ERROR:
            return [Error(code="frame_error", message=FRAME_ERROR_MESSAGE, recoverable=True)]
        return []

    def _stream_event(self, frame: dict[str, Any]) -> list[Event]:
        event = frame.get("event", {})
        parent = frame.get("parent_tool_use_id")
        if parent:
            # Every subagent stream event maps to nothing (the rule table), so nothing else here
            # may run for one either. The delta branch already checked `parent`, but
            # message_start and content_block_start did not, and both WRITE per-message state:
            # a subagent's message id would become the main thread's block-id prefix, retagging
            # the reply the user is watching mid-stream.
            return []
        etype = event.get("type")
        if etype == "message_start":
            self._current_message_id = str(event.get("message", {}).get("id", ""))
            return []
        if etype == "content_block_start":
            block = event.get("content_block", {})
            if block.get("type") == "thinking":
                block_id = self._wire_block_id(event.get("index", 0))
                self._thinking_start[block_id] = self._clock()
            return []
        if etype == "content_block_delta":
            return self._content_delta(event)
        return []

    def _wire_block_id(self, index: int) -> str:
        return f"{self._current_message_id}:{index}"

    def _content_delta(self, event: dict[str, Any]) -> list[Event]:
        delta = event.get("delta", {})
        dtype = delta.get("type")
        block_id = self._wire_block_id(event.get("index", 0))
        if dtype == "text_delta":
            text = self._route_delta(block_id, str(delta.get("text", "")))
            if not text:
                return []
            self._delta_emitted.add(block_id)
            return [TextDelta(block_id=block_id, text=text)]
        if dtype == "thinking_delta":
            return [ThinkingDelta(block_id=block_id, text=str(delta.get("thinking", "")))]
        return []

    def _assistant(self, frame: dict[str, Any]) -> list[Event]:
        message = frame.get("message", {})
        msg_id = str(message.get("id", ""))
        parent = str(frame.get("parent_tool_use_id") or "")
        content = message.get("content") or []
        if not content:
            return []
        block = content[0]
        index = self._next_index(msg_id)
        block_id = f"{msg_id}:{index}"
        thinking_ms = 0
        if block.get("type") == "thinking":
            start = self._thinking_start.pop(block_id, None)
            thinking_ms = int((self._clock() - start) * 1000) if start is not None else 0
        return self._content_block_event(block, block_id, parent, thinking_ms)

    def _next_index(self, msg_id: str) -> int:
        index = self._msg_block_count.get(msg_id, 0)
        self._msg_block_count[msg_id] = index + 1
        return index

    def _user(self, frame: dict[str, Any]) -> list[Event]:
        content = frame.get("message", {}).get("content")
        if not isinstance(content, list):
            return []
        structured = frame.get("tool_use_result")
        events: list[Event] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                events += self._tool_result(block, structured)
        return events

    def _system(self, frame: dict[str, Any]) -> list[Event]:
        subtype = frame.get("subtype")
        if subtype in ("init", "status"):
            return self._status_frame(frame)
        if subtype == "hook_response":
            return self._hook_response(frame)
        if subtype == "compact_boundary":
            return self._compact(frame)
        if subtype == "permission_denied":
            return self._permission_denied(frame)
        if subtype == "task_notification":
            return self._task_notification(frame)
        if subtype == "task_started":
            return self._task_started(frame)
        if subtype == "task_updated":
            return self._task_updated(frame)
        return []

    def _status_frame(self, frame: dict[str, Any]) -> list[Event]:
        mode_val = frame.get("permissionMode")
        mode_changed = mode_val is not None and mode_val != self._mode
        if mode_changed:
            self._mode = str(mode_val)
        now_compacting = frame.get("status") == "compacting"
        # A compaction that ends without a boundary (e.g. compact_result "failed") still needs
        # to clear the flag, even though its own state is "" like any non-compacting status.
        compaction_ended = self._compacting and not now_compacting
        self._compacting = now_compacting
        if not (mode_changed or now_compacting or compaction_ended):
            return []
        state = "compacting" if now_compacting else ""
        return [Status(state=state, mode=str(mode_val) if mode_changed else "")]

    def _hook_response(self, frame: dict[str, Any]) -> list[Event]:
        # "cancelled" is Stop aborting a running hook, not a failure of the hook itself.
        if frame.get("outcome") in ("success", "cancelled"):
            return []
        outcome: Literal["blocked", "error"] = "blocked" if frame.get("exit_code") == 2 else "error"
        message = frame.get("stderr") or frame.get("output") or ""
        return [
            Hook(
                name=str(frame.get("hook_name", "")),
                outcome=outcome,
                message=_cut(str(message).strip(), FREE_TEXT_LIMIT),
            )
        ]

    def _compact(self, frame: dict[str, Any]) -> list[Event]:
        meta = frame.get("compact_metadata") or {}
        return [
            Compacted(
                trigger=str(meta.get("trigger", "")), pre_tokens=int(meta.get("pre_tokens", 0))
            )
        ]

    def _permission_denied(self, frame: dict[str, Any]) -> list[Event]:
        reason = frame.get("decision_reason") or frame.get("message") or ""
        return [
            PermissionDenied(
                tool=str(frame.get("tool_name", "")),
                reason=_cut(_strip_ansi(str(reason)), FREE_TEXT_LIMIT),
            )
        ]

    def _task_notification(self, frame: dict[str, Any]) -> list[Event]:
        # The CLI's counts for the run pass only as non-negative ints: a negative, fractional,
        # boolean or textual value is dropped, not coerced. The summary is the model's text, so it
        # is cut like every other free-text field.
        usage = frame.get("usage")
        usage = usage if isinstance(usage, dict) else {}
        counts = {
            key: usage[key]
            for key in ("tool_uses", "total_tokens", "duration_ms")
            if type(usage.get(key)) is int and usage[key] >= 0
        }
        summary = frame.get("summary")
        return self._close_subagent(
            str(frame.get("tool_use_id", "")),
            str(frame.get("status", "")),
            summary=_cut(summary.strip(), FREE_TEXT_LIMIT) if isinstance(summary, str) else "",
            **counts,
        )

    def _task_started(self, frame: dict[str, Any]) -> list[Event]:
        """Correlates a top-level task and reopens its row on provider start evidence.

        A resumed process may not repeat the launching tool_use. Admit that lifecycle-only
        start only when its typed metadata identifies a top-level local agent.
        """
        task_id = frame.get("task_id")
        tool_id = frame.get("tool_use_id")
        if (
            not isinstance(task_id, str)
            or not task_id
            or not isinstance(tool_id, str)
            or not tool_id
            or frame.get("parent_tool_use_id")
            or ("task_type" in frame and frame["task_type"] != "local_agent")
            or (
                "spawn_depth" in frame
                and (type(frame["spawn_depth"]) is not int or frame["spawn_depth"] != 1)
            )
        ):
            return []
        started = self._subagent_starts.get(tool_id)
        agent, model, description = (
            frame.get("subagent_type"),
            frame.get("model"),
            frame.get("description"),
        )
        if started is None:
            if (
                tool_id in self._tool_names
                or frame.get("task_type") != "local_agent"
                or frame.get("spawn_depth") != 1
                or not isinstance(agent, str)
                or not agent.strip()
            ):
                if tool_id in self._subagent_ids and tool_id not in self._tool_names:
                    self._task_tools[task_id] = tool_id
                return []
            started = SubagentStarted(
                block_id=tool_id,
                agent=_cut(agent, FREE_TEXT_LIMIT),
                model="inherit",
                description="",
                reason=_cut(self._pop_reason(agent), FREE_TEXT_LIMIT),
            )
        if started is not None:
            started = replace(
                started,
                agent=_cut(agent, FREE_TEXT_LIMIT)
                if isinstance(agent, str) and agent.strip()
                else started.agent,
                model=_cut(model, FREE_TEXT_LIMIT)
                if isinstance(model, str) and model
                else started.model,
                description=_cut(description, FREE_TEXT_LIMIT)
                if isinstance(description, str)
                else started.description,
            )
        self._task_tools[task_id] = tool_id
        return self._start_subagent(started) if started is not None else []

    def _task_updated(self, frame: dict[str, Any]) -> list[Event]:
        """A stopped task may end only this way, its own task_notification suppressed (the SDK's
        docstring on stop_task): `patch.status` killed or stopped closes the row as "stopped",
        with no counts or summary. completed and failed stay silent here so the later
        task_notification remains the one source of the usage counts and summary the recorded
        contracts pin; `_close_subagent`'s own dedupe then drops that notification once this has
        already closed the row. A known task's running update reuses its start metadata to
        open another active episode; an unknown task cannot supply that metadata."""
        patch = frame.get("patch")
        status = patch.get("status") if isinstance(patch, dict) else None
        if status not in ("running", "killed", "stopped"):
            return []
        task_id = frame.get("task_id")
        tool_id = self._task_tools.get(task_id) if isinstance(task_id, str) else None
        if tool_id is None:
            return []
        if status == "running":
            return self._task_started({**frame, "tool_use_id": tool_id})
        return self._close_subagent(tool_id, "stopped")

    def _start_subagent(self, started: SubagentStarted) -> list[Event]:
        tool_id = started.block_id
        if tool_id not in self._subagent_done and self._subagent_starts.get(tool_id) == started:
            return []
        self._subagent_ids.add(tool_id)
        self._subagent_done.discard(tool_id)
        self._subagent_starts[tool_id] = started
        return [started]

    def _close_subagent(self, tool_id: str, status: str, **details: Any) -> list[Event]:
        """Ends a known routing row once per active episode. `details`: a task notification's
        usage counts and summary, passed through to SubagentDone."""
        if tool_id not in self._subagent_ids or tool_id in self._subagent_done:
            return []
        self._subagent_done.add(tool_id)
        raw = status if status in _SUBAGENT_STATUSES else "failed"
        final = cast(Literal["completed", "failed", "stopped"], raw)
        return [SubagentDone(block_id=tool_id, status=final, **details)]

    def _rate_limit(self, frame: dict[str, Any]) -> list[Event]:
        info = frame.get("rate_limit_info") or {}
        status = info.get("status") or "allowed"
        if status == "allowed":
            return []
        return [
            RateLimit(
                status=str(status),
                kind=str(info.get("rateLimitType", "")),
                resets_at=int(info.get("resetsAt", 0)),
            )
        ]

    def _result(self, frame: dict[str, Any]) -> list[Event]:
        self._pending_reasons.clear()  # an unclaimed routing reason dies with the turn
        self._held.clear()
        self._held_chars = 0
        self._at_line_start.clear()
        self._delta_emitted.clear()
        self._thinking_start.clear()
        usage_raw = frame.get("usage") or {}
        usage = Usage(
            input_tokens=int(usage_raw.get("input_tokens", 0)),
            output_tokens=int(usage_raw.get("output_tokens", 0)),
            cache_read_tokens=int(usage_raw.get("cache_read_input_tokens", 0)),
            cache_creation_tokens=int(usage_raw.get("cache_creation_input_tokens", 0)),
        )
        return [
            TurnDone(
                status=_turn_status(frame),
                usage=usage,
                context_percent=None,
                duration_ms=int(frame.get("duration_ms", 0)),
            )
        ]

    # --- history path -----------------------------------------------------------------------

    def entry(self, entry: dict[str, Any], *, source_revision: str = "") -> list[Event]:
        """One transcript entry (the history path) → zero or more events, in order."""
        kind = entry.get("type")
        if kind == "user":
            mapped = self._history_user(entry)
            entry_id = entry.get("uuid")
            try:
                valid_id = isinstance(entry_id, str) and str(uuid.UUID(entry_id)) == entry_id
            except ValueError:
                valid_id = False
            if (
                valid_id
                and isinstance(entry_id, str)
                and re.fullmatch(r"[0-9a-f]{64}", source_revision)
            ):
                mapped = [
                    replace(
                        event,
                        artifact=replace(
                            event.artifact,
                            source_revision=source_revision,
                            detail_ref=ArtifactDetailRef(
                                entry_uuid=entry_id,
                                tool_use_id=event.block_id,
                                source_revision=source_revision,
                            ),
                        ),
                    )
                    if isinstance(event, ToolDone) and event.artifact is not None
                    else event
                    for event in mapped
                ]
            return mapped
        if kind == "assistant":
            return self._history_assistant(entry)
        if kind == "attachment":
            return self._history_attachment(entry)
        if kind == "system" and entry.get("subtype") == "compact_boundary":
            meta = entry.get("compactMetadata") or {}
            trigger, pre_tokens = str(meta.get("trigger", "")), int(meta.get("preTokens", 0))
            return [Compacted(trigger=trigger, pre_tokens=pre_tokens)]
        return []

    def _history_user(self, entry: dict[str, Any]) -> list[Event]:
        # A compaction summary stands in for older turns; its boundary already said so.
        if entry.get("isMeta") or entry.get("isCompactSummary"):
            return []
        content = entry.get("message", {}).get("content")
        if isinstance(content, list):
            blocks = [b for b in content if isinstance(b, dict)]
            if any(b.get("type") == "tool_result" for b in blocks):
                structured = entry.get("toolUseResult")
                events: list[Event] = []
                for block in blocks:
                    if block.get("type") == "tool_result":
                        events += self._tool_result(block, structured)
                return events
            # A prompt sent as blocks (VS Code does this): its typed text, minus IDE context.
            texts = [str(b.get("text", "")) for b in blocks if b.get("type") == "text"]
            content = "\n".join(t for t in texts if not t.startswith(_INJECTED))
            attachments, unavailable = self._history_media_blocks(blocks)
            if (
                not content
                and any(b.get("type") == "image" for b in blocks)
                and not attachments
                and not unavailable
            ):
                content = "[image]"
        else:
            attachments, unavailable = (), ()
        if isinstance(content, str) and content.lstrip().startswith("<task-notification>"):
            # Its text is otherwise dropped, but it still closes a background agent's row.
            match = _parse_task_notification(content)
            return self._close_subagent(*match) if match else []
        has_entries = bool(attachments or unavailable)
        if (
            not isinstance(content, str)
            or (not content and not has_entries)
            or content.lstrip().startswith(_INJECTED)
        ):
            return []
        at = str(entry.get("timestamp", ""))
        self._pending_reasons.clear()  # a new turn: an unclaimed routing reason dies with the old
        # Only an entry that opens with the command tags is a command; the same tags further in
        # (a quoted excerpt, a fenced example) are the user's text.
        starts_command = content.lstrip().startswith(_COMMAND_OPENERS)
        match = _parse_command(content) if starts_command else None
        # Matched against the untouched string so a huge paste after the tags can't hide the
        # command; only the text handed to the event is bounded, same limit as the live path
        # (stream_state._bounded), so a 20-turn history page can't blow past its 2 MiB cap.
        if match:
            name, args = match
            text = args or ""
            return [
                UserMessage(
                    text=text[: self._text_limit],
                    command=name,
                    at=at,
                    attachments=attachments,
                    unavailable_attachments=unavailable,
                )
            ]
        return [
            UserMessage(
                text=content[: self._text_limit],
                command="",
                at=at,
                attachments=attachments,
                unavailable_attachments=unavailable,
            )
        ]

    def _history_attachment(self, entry: dict[str, Any]) -> list[Event]:
        # A steer folded into a running turn, saved next to `queue-operation` entries that
        # entry() routes nowhere (kind not handled above). Anything other than a prompt-mode
        # queued_command maps to nothing, like the frozen "instructions" attachment already
        # pinned in tests/acceptance/test_mapper_accept.py.
        if entry.get("isMeta") is True:
            return []
        attachment = entry.get("attachment")
        if not isinstance(attachment, dict) or attachment.get("type") != "queued_command":
            return []
        if attachment.get("commandMode") != "prompt":
            return []
        # A subagent handback folded into a running turn (CLI 2.1.281, verified live): its own
        # isMeta flag and the sender's origin live inside the attachment, never on the entry.
        # Only a human-originated prompt (origin absent, or kind == "human") is the user's own.
        if attachment.get("isMeta"):
            return []
        origin = attachment.get("origin")
        if "origin" in attachment and not (
            isinstance(origin, dict) and origin.get("kind") == "human"
        ):
            return []
        prompt = attachment.get("prompt")
        if not isinstance(prompt, str | list):
            return []
        # A received cross-session message (between_chats.py's own carrier): already surfaced
        # there, never the user's own words, so it must not also appear as a typed turn.
        if isinstance(prompt, str) and prompt.startswith("<cross-session-message"):
            return []
        if isinstance(prompt, list):
            # Only text/image blocks are the user's typed turn. A tool_result block here would
            # make _history_user read the whole entry as a reported tool result and emit a
            # ToolDone instead (its list branch keys on any block being "tool_result") — a
            # queued prompt is never that.
            prompt = [
                block
                for block in prompt
                if isinstance(block, dict) and block.get("type") in ("text", "image")
            ]
        timestamp = entry.get("timestamp") or attachment.get("timestamp", "")
        return self._history_user(
            {"type": "user", "message": {"role": "user", "content": prompt}, "timestamp": timestamp}
        )

    def _history_media_blocks(
        self, blocks: list[dict[str, Any]]
    ) -> tuple[tuple[Attachment, ...], tuple[UnavailableAttachment, ...]]:
        if self._history_media is None:
            return (), ()
        attachments: list[Attachment] = []
        unavailable: list[UnavailableAttachment] = []
        decoded_total = 0
        image_count = 0
        for block in blocks:
            if block.get("type") != "image":
                continue
            if image_count == _HISTORY_IMAGE_LIMIT:
                if len(unavailable) < _HISTORY_IMAGE_LIMIT:
                    unavailable.append(self._unavailable_image(block, "limit"))
                break
            image_count += 1
            found, missing, decoded = self._history_image(block, decoded_total)
            decoded_total += decoded
            if found is not None:
                if not any(existing.asset_id == found.asset_id for existing in attachments):
                    attachments.append(found)
            elif missing is not None:
                unavailable.append(missing)
        return tuple(attachments), tuple(unavailable)

    def _history_image(
        self, block: dict[str, Any], decoded_total: int
    ) -> tuple[Attachment | None, UnavailableAttachment | None, int]:
        source = block.get("source")
        if not isinstance(source, dict) or source.get("type") != "base64":
            return None, self._unavailable_image(block, "invalid_native_media"), 0
        media_type = source.get("media_type")
        data = source.get("data")
        if (
            media_type not in _HISTORY_MEDIA_TYPES
            or not isinstance(data, str)
            or not data.isascii()
        ):
            return None, self._unavailable_image(block, "invalid_native_media"), 0
        declared = source.get("byte_size", source.get("size"))
        if declared is not None and (type(declared) is not int or declared < 0):
            return None, self._unavailable_image(block, "invalid_native_media"), 0
        if declared is not None and declared > _HISTORY_IMAGE_MAX_BYTES:
            return None, self._unavailable_image(block, "too_large", declared), 0
        encoded_limit = 4 * ((_HISTORY_IMAGE_MAX_BYTES + 2) // 3)
        if len(data) > encoded_limit:
            return None, self._unavailable_image(block, "too_large"), 0
        try:
            decoded = base64.b64decode(data, validate=True)
        except ValueError:
            return None, self._unavailable_image(block, "invalid_native_media"), 0
        if base64.b64encode(decoded).decode("ascii") != data:
            return None, self._unavailable_image(block, "invalid_native_media"), 0
        size = len(decoded)
        if not size:
            return None, self._unavailable_image(block, "invalid_native_media"), 0
        if declared is not None and declared != size:
            return None, self._unavailable_image(block, "invalid_native_media"), 0
        if size > _HISTORY_IMAGE_MAX_BYTES:
            return None, self._unavailable_image(block, "too_large", size), 0
        if decoded_total + size > _HISTORY_IMAGE_BYTES:
            return None, self._unavailable_image(block, "limit", size), 0
        name = f"history-image.{_HISTORY_EXTENSIONS[media_type]}"
        digest = hashlib.sha256(decoded).hexdigest()
        resolver = self._history_media
        try:
            attachment = resolver(digest, media_type, size, name) if resolver is not None else None
        except Exception:
            attachment = None
        if attachment is None:
            return (
                None,
                UnavailableAttachment(
                    name=name,
                    media_type=media_type,
                    byte_size=size,
                    reason="not_in_local_assets",
                ),
                size,
            )
        return attachment, None, size

    @staticmethod
    def _unavailable_image(
        block: dict[str, Any],
        reason: Literal["invalid_native_media", "too_large", "limit"],
        byte_size: int = 0,
    ) -> UnavailableAttachment:
        source = block.get("source")
        media_type = source.get("media_type") if isinstance(source, dict) else ""
        if media_type not in _HISTORY_MEDIA_TYPES:
            return UnavailableAttachment(
                name="history-image",
                media_type="application/octet-stream",
                byte_size=byte_size,
                reason=reason,
            )
        return UnavailableAttachment(
            name=f"history-image.{_HISTORY_EXTENSIONS[media_type]}",
            media_type=media_type,
            byte_size=byte_size,
            reason=reason,
        )

    def _history_assistant(self, entry: dict[str, Any]) -> list[Event]:
        message = entry.get("message", {})
        msg_id = str(message.get("id", ""))
        api_index = entry.get("apiBlockIndex")
        index = api_index if isinstance(api_index, int) else self._next_index(msg_id)
        content = message.get("content") or []
        if not content:
            return []
        return self._content_block_event(content[0], f"{msg_id}:{index}", "", 0)

    # --- shared between the two paths -------------------------------------------------------

    def _content_block_event(
        self, block: dict[str, Any], block_id: str, parent: str, thinking_ms: int
    ) -> list[Event]:
        kind = block.get("type")
        if kind == "tool_use":
            tool_input = block.get("input") or {}
            return self._tool_use_event(str(block["id"]), str(block["name"]), tool_input, parent)
        if parent:
            return []
        if kind == "text":
            text = self._strip_routing_lines(str(block.get("text", "")))
            had_delta = block_id in self._delta_emitted
            self._delta_emitted.discard(block_id)
            self._held_chars -= len(self._held.pop(block_id, ""))
            if not text and not had_delta:
                return []  # a block left empty by the removal, with no delta: no empty reply
            # Shared by both paths: the live path's own clip (stream_state._bounded) already
            # bites here for live frames, so this is a no-op there and the fix for history,
            # which never passes through stream_state at all (catalog.py calls mapper.entry
            # directly).
            return [TextDone(block_id=block_id, text=text[: self._text_limit])]
        if kind == "thinking":
            return [
                ThinkingDone(
                    block_id=block_id, text=str(block.get("thinking", "")), duration_ms=thinking_ms
                )
            ]
        return []

    def _tool_use_event(
        self, tool_id: str, name: str, tool_input: dict[str, Any], parent: str
    ) -> list[Event]:
        self._tool_names[tool_id] = name
        if not parent and name in _SUBAGENT_TOOLS:
            if tool_id in self._subagent_starts and tool_id not in self._subagent_done:
                return []
            resume = tool_input.get("resume")
            previous_id = self._task_tools.get(resume) if isinstance(resume, str) else None
            previous = self._subagent_starts.get(previous_id or tool_id)
            if name == "Workflow":
                agent = "workflow"
                description = _workflow_description(tool_input)
            else:
                agent = str(
                    tool_input.get("subagent_type")
                    or (previous.agent if previous else "general-purpose")
                )
                description = str(
                    tool_input.get("description", previous.description if previous else "")
                )
            model = str(tool_input.get("model") or (previous.model if previous else "inherit"))
            reason = self._pop_reason(agent) or (previous.reason if previous else "")
            # Cut here and not where `agent` is read: `_pop_reason` above still matches on the
            # name the routing line queued, so bounding what goes out cannot change which reason
            # a row claims.
            return self._start_subagent(
                SubagentStarted(
                    block_id=tool_id,
                    agent=_cut(agent, FREE_TEXT_LIMIT),
                    model=_cut(model, FREE_TEXT_LIMIT),
                    description=_cut(description, FREE_TEXT_LIMIT),
                    reason=_cut(reason, FREE_TEXT_LIMIT),
                )
            )
        row = tool_row(name, tool_input, self._vault)
        artifact = (
            classify_artifact(tool_id, name, tool_input, workspace=self._vault)
            if self._artifacts_enabled
            else None
        )
        if artifact is not None:
            # Bound retained correlation for a provider that never returns tool results.
            if len(self._artifacts) >= 512:
                self._artifacts.pop(next(iter(self._artifacts)))
            self._artifacts[tool_id] = artifact
        return [
            ToolStarted(block_id=tool_id, tool=name, row=row, parent_id=parent, artifact=artifact)
        ]

    def _tool_result(self, block: dict[str, Any], structured: Any) -> list[Event]:
        tool_id = str(block.get("tool_use_id", ""))
        is_error = bool(block.get("is_error"))
        if tool_id in self._subagent_ids:
            if isinstance(structured, dict) and structured.get("status") == "async_launched":
                return []  # runs on in the background; its task_notification closes the row
            return self._close_subagent(tool_id, "failed" if is_error else "completed")
        tool = self._tool_names.get(tool_id, "")
        text = _result_text(block.get("content"))
        preview = _preview(text)
        artifact = None
        start = self._artifacts.pop(tool_id, None)
        content = block.get("content")
        valid_content = isinstance(content, str) or (
            isinstance(content, list)
            and all(
                isinstance(part, dict)
                and part.get("type") == "text"
                and isinstance(part.get("text"), str)
                for part in content
            )
        )
        if start is not None and valid_content:
            try:
                flag = block.get("is_error", False)
                artifact = observe_result(
                    start,
                    text.encode("utf-8", errors="replace"),
                    is_error=flag if isinstance(flag, bool) else None,
                )
            except ArtifactError:
                pass  # The ordinary bounded row remains; no complete-payload claim is made.
        return [
            ToolDone(
                block_id=tool_id,
                is_error=is_error,
                summary=_summary(preview),
                preview=preview,
                tail=_tail(tool, text, structured),
                artifact=artifact,
            )
        ]

    # --- routing lines ("▸ routing: <agent> → [<model>]（<reason>）") ------------------------

    def _route_delta(self, block_id: str, chunk: str) -> str:
        """Applies the routing-line filter to one delta chunk of a main-thread text block,
        holding back an unterminated line that might still turn out to be a routing line. Only
        a segment that truly began at a line start is ever tested as a routing line: a chunk
        boundary falling mid-line is not one, even when what follows starts with "▸". A
        matched line is only ever suppressed here, never queued: the block's own assistant text
        frame always follows and `_strip_routing_lines` registers the reason there once (queuing
        from both places double-registered every live routing line)."""
        held = self._held.get(block_id, "")
        at_line_start = bool(held) or self._at_line_start.get(block_id, True)
        buf = chunk
        emitted: list[str] = []
        start = 0
        if held:
            first_newline = chunk.find("\n")
            prefix_length = len(chunk) if first_newline == -1 else first_newline
            # Only the candidate prefix shares the pending budget. Resolve it before
            # scanning the original chunk's tail, which has its own frame admission cap.
            if self._held_chars + prefix_length > MAX_ROUTING_CHARS:
                raise MapperOverflow
            if first_newline == -1:
                buf = held + chunk
            else:
                line = held + chunk[:first_newline]
                if not _ROUTING_RE.fullmatch(line):
                    emitted.append(line + "\n")
                start = first_newline + 1
        self._held.pop(block_id, None)
        self._held_chars -= len(held)
        while (nl := buf.find("\n", start)) != -1:
            line = buf[start:nl]
            is_routing = at_line_start and _ROUTING_RE.fullmatch(line)
            if not is_routing:
                emitted.append(buf[start : nl + 1])
            start = nl + 1
            at_line_start = True  # right after a real newline is always a line start
        remainder = buf[start:]
        if at_line_start and remainder.startswith("▸"):
            if self._held_chars + len(remainder) > MAX_ROUTING_CHARS:
                raise MapperOverflow
            self._held[block_id] = remainder
            self._held_chars += len(remainder)
        else:
            emitted.append(remainder)
            self._at_line_start[block_id] = at_line_start and not remainder
        return "".join(emitted)

    def _strip_routing_lines(self, text: str) -> str:
        """The full text of a finished text block, with routing lines removed; their reasons are
        captured for the next matching subagent.started. The only place a routing line is
        registered: `_route_delta` merely suppresses it from the live delta stream."""
        kept: list[str] = []
        for line in text.split("\n"):
            match = _ROUTING_RE.fullmatch(line)
            if match:
                self._queue_reason(match["agent"], match["reason"])
            else:
                kept.append(line)
        return "\n".join(kept)

    def _queue_reason(self, agent: str, reason: str) -> None:
        self._pending_reasons.setdefault(agent, []).append(reason)

    def _pop_reason(self, agent: str) -> str:
        """The oldest unclaimed routing reason for `agent` (first come, first served)."""
        reasons = self._pending_reasons.get(agent)
        if not reasons:
            return ""
        reason = reasons.pop(0)
        if not reasons:
            del self._pending_reasons[agent]
        return reason


def _workflow_description(tool_input: dict[str, Any]) -> str:
    """A Workflow tool_use has no description: the name in its script's
    `export const meta = { name: '…' }` (first 2,000 characters), else the scriptPath file
    stem, else ""."""
    script = str(tool_input.get("script", ""))[:2000]
    match = _WORKFLOW_META_RE.search(script)
    if match:
        return match[1]
    script_path = tool_input.get("scriptPath")
    return Path(str(script_path)).stem if script_path else ""
