"""The subagent transcript route: one subagent run's own transcript, for the right rail's Peek.

`GET /api/v2/sessions/{session_id}/subagents/{tool_use_id}` answers the run a chat's routing
block launched, `tool_use_id` being that block's Agent tool_use id. Claude Code writes each run
beside the chat's transcript, under `<session_id>/subagents/`, as `agent-<id>.jsonl` with an
`agent-<id>.meta.json` whose `toolUseId` names the launch. The lookup walks the chat's folder,
then its history source's (a fork's source) when that differs, for the meta file naming the id:
the id is compared with each `toolUseId` and builds no path. The transcript is the meta file's
own sibling, and both must be regular files that resolve inside the folder. A found transcript
is remembered in memory, at most RESOLVED_CACHE of them, while it is still a file, and every
folder's remembered run is tried before any folder is walked, so a run's polls walk once, a
fork's polls for a run of its source included.

Bounds: MAX_REQUESTS reads at a time, each in a worker thread that a caller going away does not
cancel. A walk stops past MAX_WALK_ENTRIES entries and skips a meta file over MAX_META_BYTES. A
transcript over TAIL_BYTES is read as the whole lines of its first HEAD_BYTES and the later lines
that begin in its last TAIL_BYTES, and reads `partial`. The answer keeps the newest MAX_LINES lines
that fit MAX_ANSWER_BYTES and counts the rest in `omitted`. One history mapper reads the head and
then the body, so a tool or nested agent that starts in the head and ends in the body still
closes. It makes the lines: text, tools, nested agents and later messages; the first message is
the run's `prompt`.

Every value is one the browser's shape check takes: one value past it fails the whole answer.
Strings are counted as the browser counts them, in UTF-16 units. A tool's tail is cut to MAX_STR
and the prompt to MAX_FREE_TEXT; a line holding a longer string (a text past MAX_FREE_TEXT, any
other string past MAX_STR) is dropped and counted in `omitted`. A start past MAX_STARTED_AT reads
"", and a usage count or total past MAX_COUNT is no count.

The route sits behind the Bearer token and the Origin check and answers `no-store`. It writes
nothing and logs nothing: an off-shape entry costs its own line, silently.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import threading
import uuid
from collections import OrderedDict
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

import anyio
from anyio.to_thread import run_sync
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from viki_agent.api_json import failure, json_response
from viki_agent.catalog import _iter_lines
from viki_agent.drivers.claude.mapper import Mapper
from viki_agent.events import (
    SubagentDone,
    SubagentStarted,
    TextDone,
    ToolDone,
    ToolStarted,
    UserMessage,
)

MAX_REQUESTS = 4
MAX_WALK_ENTRIES = 4096
MAX_META_BYTES = 16 * 1024
HEAD_BYTES = 256 * 1024
TAIL_BYTES = 4 * 1024 * 1024
MAX_LINES = 1000
MAX_ANSWER_BYTES = 1 << 20  # the browser refuses a larger body
RESOLVED_CACHE = 256
TOOL_USE_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")  # matched whole
# The browser's own bounds (frontend/src/clients/conversations/subagentsApi.ts), in UTF-16
# units: the prompt and a line's text, every other string, and the start.
MAX_FREE_TEXT = 65_536
MAX_STR = 4_096
MAX_STARTED_AT = 64
MAX_COUNT = 2**53 - 1  # the largest integer a browser's number holds exactly

_COUNTS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)


class _Actor(Protocol):
    history_source: str


class _Sessions(Protocol):
    def get(self, session_id: str) -> _Actor | None: ...


class _Transcripts(Protocol):
    def transcript_path(self, session_id: str) -> Path | None: ...

    def root_for(self, session_id: str) -> Path: ...


def _canonical_uuid(value: str) -> bool:
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False


def _inside(path: Path, top: Path) -> bool:
    """Whether `path` is a regular file that resolves inside `top`."""
    try:
        return path.resolve().is_relative_to(top) and path.is_file()
    except OSError:
        return False


def _names(meta: Path, tool_use_id: str) -> bool:
    """Whether `meta` is a JSON object of at most MAX_META_BYTES whose `toolUseId` is the string
    `tool_use_id`. A meta file that cannot be read is skipped like one that does not match."""
    try:
        with meta.open("rb") as fh:
            raw = fh.read(MAX_META_BYTES + 1)
        document = json.loads(raw) if len(raw) <= MAX_META_BYTES else None
    except OSError, ValueError, RecursionError:
        return False
    # Only a string can equal the string `tool_use_id`.
    return isinstance(document, dict) and document.get("toolUseId") == tool_use_id


def _find(folder: Path, tool_use_id: str) -> Path | None:
    """The transcript of the run whose meta file under `folder` names `tool_use_id`, or None."""
    entries = 0
    # Count-bounded, checked after each directory's listing, and not timed; the cache
    # serves a run's polls, so each run costs one walk. A folder past the bound needs an index.
    for directory, dirnames, filenames in folder.walk():  # top-down, links not followed
        entries += len(dirnames) + len(filenames)
        if entries > MAX_WALK_ENTRIES:
            return None
        for name in filenames:
            meta = directory / name
            if not (name.startswith("agent-") and name.endswith(".meta.json")):
                continue
            if _inside(meta, folder) and _names(meta, tool_use_id):
                jsonl = meta.with_name(name.removesuffix(".meta.json") + ".jsonl")
                return jsonl if _inside(jsonl, folder) else None
    return None


def _windows(path: Path) -> list[bytes]:
    """The bytes of `path` to map: the whole file when it is at most TAIL_BYTES, else its head,
    the whole lines of its first HEAD_BYTES, and its body, the lines that begin in its last
    TAIL_BYTES and after the head (catalog._tail_window's cut). One handle: open, read, close."""
    with path.open("rb") as fh:
        size = os.fstat(fh.fileno()).st_size
        if size <= TAIL_BYTES:
            return [fh.read(size)]
        head = fh.read(HEAD_BYTES)
        head = head[: head.rfind(b"\n") + 1]
        begin = max(size - TAIL_BYTES, len(head))
        # One byte early, so a body that begins at a line start keeps that line.
        fh.seek(begin - 1)
        body = fh.read(size - begin + 1)
    cut = body.find(b"\n")
    return [head, body[cut + 1 :] if cut != -1 else b""]


def _entries(raw: bytes) -> Iterator[dict[str, Any]]:
    """The JSON objects among the lines of `raw`, in order; every other line is skipped."""
    for line in _iter_lines(raw):
        try:
            entry = json.loads(line.decode("utf-8", errors="replace"))
        except ValueError, RecursionError:
            continue
        if isinstance(entry, dict):
            yield entry


def _units(value: str) -> int:
    """`value`'s length as the browser counts it, in UTF-16 units."""
    # A lone surrogate survives JSON decoding, so it is counted rather than refused.
    return len(value.encode("utf-16-le", "surrogatepass")) // 2


def _clipped(value: str, limit: int) -> str:
    """`value`, or its longest prefix within `limit` UTF-16 units when it is longer."""
    if _units(value) <= limit:
        return value
    units = 0
    for end, char in enumerate(value):
        units += 2 if ord(char) > 0xFFFF else 1
        if units > limit:
            return value[:end]
    return value


def _fits(line: dict[str, Any]) -> bool:
    """Whether the browser takes `line`'s strings: its `text` within MAX_FREE_TEXT UTF-16 units,
    every other within MAX_STR."""
    return all(
        _units(value) <= (MAX_FREE_TEXT if key == "text" else MAX_STR)
        for key, value in line.items()
        if isinstance(value, str)
    )


def _timestamp(entry: dict[str, Any]) -> str:
    """The entry's `timestamp` when it is an ISO 8601 string within MAX_STARTED_AT UTF-16 units,
    else ""."""
    value = entry.get("timestamp")
    if not isinstance(value, str) or _units(value) > MAX_STARTED_AT:
        return ""
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return ""
    return value


def _count(value: object) -> bool:
    return type(value) is int and 0 <= value <= MAX_COUNT


def _tokens(entry: dict[str, Any]) -> int | None:
    """The total of the entry's `message.usage`, or None unless its input and output counts are
    ints from 0 to MAX_COUNT and the total stays within MAX_COUNT; a cache count joins the total
    only when it is one too. A larger int reaches the browser as an inexact or infinite number,
    and one past 4,300 digits makes `json.dumps` raise."""
    message = entry.get("message")
    usage = message.get("usage") if isinstance(message, dict) else None
    if not isinstance(usage, dict) or not all(_count(usage.get(key)) for key in _COUNTS[:2]):
        return None
    total = sum(usage[key] for key in _COUNTS if _count(usage.get(key)))
    return total if total <= MAX_COUNT else None


def _size(value: object) -> int:
    # The encoding `api_json.json_response` sends: ASCII, so characters are bytes.
    return len(json.dumps(value, ensure_ascii=True, separators=(",", ":")))


def _fitted(answer: dict[str, Any], lines: list[dict[str, Any]]) -> dict[str, Any]:
    """`answer` with the newest of `lines` the browser takes, at most MAX_LINES, that keep its
    encoding within MAX_ANSWER_BYTES, and `omitted` counting the rest."""
    # The answer with no lines, less the "0" of `omitted`, whose own digits are counted as each
    # line is taken.
    base = _size({**answer, "lines": [], "omitted": 0}) - 1
    kept: list[dict[str, Any]] = []
    used = 0
    for line in reversed(lines):
        if len(kept) == MAX_LINES:
            break
        if not _fits(line):
            continue  # it costs only its own line, not the whole answer
        grown = used + _size(line) + (1 if kept else 0)  # a comma before all lines but the first
        if base + len(str(len(lines) - len(kept) - 1)) + grown > MAX_ANSWER_BYTES:
            break
        kept.append(line)
        used = grown
    return {**answer, "lines": kept[::-1], "omitted": len(lines) - len(kept)}


def _answer(path: Path, root: Path) -> Response:
    """The run's prompt, lines, start and tokens. The prompt and the start come from the head.
    One mapper reads both windows, which come in order and never overlap, so what the head
    launched is known when the body closes it."""
    windows = _windows(path)
    prompt: str | None = None
    started_at: str | None = None
    tokens: int | None = None
    messages = 0
    lines: list[dict[str, Any]] = []
    tools: dict[str, dict[str, Any]] = {}
    agents: dict[str, dict[str, Any]] = {}
    mapper = Mapper(vault=str(root), mode="default")
    for index, window in enumerate(windows):
        for entry in _entries(window):
            if index == 0 and started_at is None:
                started_at = _timestamp(entry)
            if (count := _tokens(entry)) is not None:
                tokens = count
            try:
                events = mapper.entry(entry)
            except Exception:
                continue  # an off-shape entry costs its own line, unlogged
            for event in events:
                if isinstance(event, UserMessage):
                    if index == 0 and prompt is None:
                        prompt = event.text
                    else:
                        messages += 1
                        lines.append({"kind": "message", "id": f"m{messages}", "text": event.text})
                elif isinstance(event, TextDone):
                    lines.append({"kind": "text", "id": event.block_id, "text": event.text})
                elif isinstance(event, ToolStarted):
                    tools[event.block_id] = {
                        "kind": "tool",
                        "id": event.block_id,
                        "head": event.row.head,
                        "rest": event.row.rest,
                        "tail": "",
                        "done": False,
                        "error": False,
                    }
                    lines.append(tools[event.block_id])
                elif isinstance(event, ToolDone) and event.block_id in tools:
                    # The tail can carry the tool's own output (a Bash "Exit code <digits>").
                    tools[event.block_id] |= {
                        "tail": _clipped(event.tail, MAX_STR),
                        "done": True,
                        "error": event.is_error,
                    }
                elif isinstance(event, SubagentStarted):
                    agents[event.block_id] = {
                        "kind": "agent",
                        "id": event.block_id,
                        "agent": event.agent,
                        "description": event.description,
                        "status": "running",
                    }
                    lines.append(agents[event.block_id])
                elif isinstance(event, SubagentDone) and event.block_id in agents:
                    agents[event.block_id]["status"] = event.status
    answer = {
        "prompt": _clipped(prompt or "", MAX_FREE_TEXT),
        "lines": [],
        "omitted": 0,
        "partial": len(windows) > 1,
        "started_at": started_at or "",
        "tokens": tokens,
    }
    return json_response(_fitted(answer, lines))


def subagent_routes(
    transcripts: _Transcripts,
    sessions: _Sessions,
    *,
    authorized: Callable[[Request], bool],
    origins: frozenset[str],
    workers: set[asyncio.Task[Any]],
) -> list[Route]:
    slots = anyio.CapacityLimiter(MAX_REQUESTS)
    # (the folder's session id, tool_use id) → the run's transcript, least recently used first.
    # The reads run in worker threads, so the lock guards it.
    resolved: OrderedDict[tuple[str, str], Path] = OrderedDict()
    lock = threading.Lock()

    def remembered(key: tuple[str, str], folder: Path) -> Path | None:
        with lock:
            jsonl = resolved.get(key)
            if jsonl is not None:
                resolved.move_to_end(key)
        return jsonl if jsonl is not None and _inside(jsonl, folder) else None

    def walked(key: tuple[str, str], folder: Path) -> Path | None:
        jsonl = _find(folder, key[1])
        with lock:
            resolved.pop(key, None)
            if jsonl is not None:
                resolved[key] = jsonl
                while len(resolved) > RESOLVED_CACHE:
                    resolved.popitem(last=False)
        return jsonl

    def read(session_ids: list[str], tool_use_id: str) -> Response:
        try:
            folders: list[tuple[str, Path]] = []
            for session_id in session_ids:
                main = transcripts.transcript_path(session_id)
                if main is not None:
                    # Parent aliases are part of the catalog's admitted provider project.
                    # Keep the session suffix anchored there, rather than rebasing it to a link.
                    folder = main.parent.resolve() / session_id / "subagents"
                    if folder.resolve().is_relative_to(folder):
                        folders.append((session_id, folder))
            # Every folder's remembered run before any walk: a fork's polls for a run of its
            # source would otherwise walk the fork's own folder, and miss, every time.
            for session_id, folder in folders:
                jsonl = remembered((session_id, tool_use_id), folder)
                if jsonl is not None:
                    return _answer(jsonl, transcripts.root_for(session_id))
            for session_id, folder in folders:
                jsonl = walked((session_id, tool_use_id), folder)
                if jsonl is not None:
                    return _answer(jsonl, transcripts.root_for(session_id))
        except FileNotFoundError:
            pass
        except OSError:
            return failure("unavailable", "The subagent transcript is unavailable.", 503)
        return failure("not_found", "This subagent run has no transcript.", 404)

    async def perform(session_id: str, tool_use_id: str) -> Response:
        actor = sessions.get(session_id)
        session_ids = [session_id]
        if actor is not None and actor.history_source != session_id:
            session_ids.append(actor.history_source)
        return await run_sync(read, session_ids, tool_use_id)

    async def admitted(session_id: str, tool_use_id: str) -> Response:
        permit = object()
        try:
            slots.acquire_on_behalf_of_nowait(permit)
        except anyio.WouldBlock:
            return failure("overloaded", "The subagent transcripts are busy.", 429)
        task = asyncio.create_task(perform(session_id, tool_use_id))
        workers.add(task)

        def done(future: asyncio.Task[Response]) -> None:
            workers.discard(future)
            slots.release_on_behalf_of(permit)
            if not future.cancelled():
                future.exception()

        task.add_done_callback(done)
        # A caller that goes away does not cancel the read or free its permit early.
        return await asyncio.shield(task)

    async def endpoint(request: Request) -> Response:
        session_id = request.path_params["session_id"]
        tool_use_id = request.path_params["tool_use_id"]
        if not authorized(request):
            response = failure("unauthorized", "Authentication is required.", 401)
        elif request.headers.get("origin") not in {None, *origins}:
            response = failure("forbidden_origin", "This origin is not allowed.", 403)
        elif (
            request.scope.get("query_string")
            or not _canonical_uuid(session_id)
            or not TOOL_USE_ID.fullmatch(tool_use_id)
        ):
            response = failure("invalid_input", "The subagent request is invalid.", 400)
        else:
            response = await admitted(session_id, tool_use_id)
        response.headers["Cache-Control"] = "no-store"
        return response

    return [
        Route("/api/v2/sessions/{session_id}/subagents/{tool_use_id}", endpoint, methods=["GET"])
    ]
