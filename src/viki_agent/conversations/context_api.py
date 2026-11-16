"""The context route and its last filter before the browser.

`sanitize_context` trusts nothing in the driver's context report
(`drivers/claude/context_report.py`): every count and string is rechecked from scratch, the
same discipline `inventory_api.sanitize` already applies to the tool inventory. A row that
is not an object, is off its kind, holds a string unfit for the browser, or arrives past its
part's cap is dropped and counted in `hidden[part]`, rather than spoiling the rest of that
part the way one bad row spoils the whole part when the driver first builds the report. A
part that is not its expected shape at all -- a list for most parts, an object with its own
required fields for `skills` -- reads null instead, keeping `document`'s own reason for it
when that reason is known, else `unrecognized`.

`context_routes` serves one running chat's context report behind the Bearer token and the Origin
check, two requests at a time, with the same gate order as `inventory_routes`; an answer past the
browser's 1 MiB bound reads `too_large`. It logs nothing: never a token, a session id or report
text.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Callable, Mapping
from typing import Any, Protocol

import anyio
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from viki_agent.api_json import failure, json_response
from viki_agent.conversations.inventory_api import (
    MAX_ANSWER_BYTES,
    REASONS,
    _canonical_uuid,
    _text,
)
from viki_agent.drivers.claude.context_report import CATEGORY_KINDS, MAX_COUNT, REPORT_PARTS

MAX_CONTEXT_REQUESTS = 2
MAX_CATEGORIES = 32
MAX_MCP_SERVERS = 64
MAX_ENTRIES = 256  # memory_files, skills.items and agents share this cap


def _count(value: object) -> int | None:
    """A whole number in 0..MAX_COUNT. A `bool` is an `int` subclass and is never one."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 0 <= value <= MAX_COUNT else None


def _percentage(value: object) -> int | float | None:
    """An int or a finite float in 0..100, else None. Never a `bool`."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value if 0 <= value <= 100 else None


def _category_row(item: object) -> dict[str, object] | None:
    if not isinstance(item, Mapping):
        return None
    name, tokens, kind = _text(item.get("name")), _count(item.get("tokens")), item.get("kind")
    if name is None or tokens is None or not (isinstance(kind, str) and kind in CATEGORY_KINDS):
        return None
    return {"name": name, "tokens": tokens, "kind": kind}


def _memory_file_row(item: object) -> dict[str, object] | None:
    if not isinstance(item, Mapping):
        return None
    name, kind = _text(item.get("name")), _text(item.get("type"))
    tokens = _count(item.get("tokens"))
    if name is None or kind is None or tokens is None:
        return None
    return {"name": name, "type": kind, "tokens": tokens}


def _mcp_server_row(item: object) -> dict[str, object] | None:
    if not isinstance(item, Mapping):
        return None
    server = _text(item.get("server"))
    tools, loaded = _count(item.get("tools")), _count(item.get("loaded"))
    loaded_tokens = _count(item.get("loaded_tokens"))
    if server is None or tools is None or loaded is None or loaded_tokens is None:
        return None
    return {"server": server, "tools": tools, "loaded": loaded, "loaded_tokens": loaded_tokens}


def _agent_row(item: object) -> dict[str, object] | None:
    if not isinstance(item, Mapping):
        return None
    kind, source = _text(item.get("type")), _text(item.get("source"))
    tokens = _count(item.get("tokens"))
    if kind is None or source is None or tokens is None:
        return None
    return {"type": kind, "source": source, "tokens": tokens}


def _skill_item_row(item: object) -> dict[str, object] | None:
    """`plugin` is optional, but a present, unfit value still drops the whole row rather than
    nulling just that field -- every other field here is required, so a row is whole or gone."""
    if not isinstance(item, Mapping):
        return None
    name, source = _text(item.get("name")), _text(item.get("source"))
    tokens = _count(item.get("tokens"))
    if name is None or source is None or tokens is None:
        return None
    plugin = item.get("plugin")
    if plugin is not None:
        plugin = _text(plugin)
        if plugin is None:
            return None
    return {"name": name, "source": source, "plugin": plugin, "tokens": tokens}


# part -> (its row reader, its cap). `skills` is handled on its own below: it is an object with
# its own required counts, not a bare list of rows.
_LIST_PARTS: dict[str, tuple[Callable[[object], dict[str, object] | None], int]] = {
    "categories": (_category_row, MAX_CATEGORIES),
    "memory_files": (_memory_file_row, MAX_ENTRIES),
    "mcp_servers": (_mcp_server_row, MAX_MCP_SERVERS),
    "agents": (_agent_row, MAX_ENTRIES),
}


def _capped_rows(
    items: list[object], read_row: Callable[[object], dict[str, object] | None], cap: int
) -> tuple[list[dict[str, object]], int]:
    """The first `cap` rows `read_row` admits from `items`, and how many were dropped: unfit
    (`read_row` refused them) or past the cap. The caller passes only an `items` it already knows
    is a list; a part that is not a list at all is handled before this is ever called."""
    kept: list[dict[str, object]] = []
    hidden = 0
    for item in items:
        row = read_row(item)
        if row is None:
            hidden += 1
        elif len(kept) < cap:
            kept.append(row)
        else:
            hidden += 1
    return kept, hidden


def _skills(value: object) -> tuple[dict[str, object] | None, int]:
    """None when `value` is not an object, or its `total`/`included` counts or `items` list are
    off -- these make the object unrecognisable as `skills` at all, the same way a non-list part
    is unrecognisable below. `tokens` alone degrades to None without spoiling the rest, as
    `context_report._skills` already treats it; `items`' own rows are capped like any other
    part's."""
    if not isinstance(value, Mapping):
        return None, 0
    total, included = _count(value.get("total")), _count(value.get("included"))
    raw_items = value.get("items")
    if total is None or included is None or not isinstance(raw_items, list):
        return None, 0
    items, hidden = _capped_rows(raw_items, _skill_item_row, MAX_ENTRIES)
    skills = {
        "total": total,
        "included": included,
        "tokens": _count(value.get("tokens")),
        "items": items,
    }
    return skills, hidden


def _auto_compact(value: object) -> dict[str, object] | None:
    """None unless `enabled` is a bool; `threshold` and `source` degrade alone, as
    `context_report._auto_compact` already treats them."""
    if not isinstance(value, Mapping):
        return None
    enabled = value.get("enabled")
    if not isinstance(enabled, bool):
        return None
    return {
        "enabled": enabled,
        "threshold": _count(value.get("threshold")),
        "source": _text(value.get("source")),
    }


def sanitize_context(document: Mapping[str, object]) -> dict[str, object]:
    """A new document holding only what the browser may see; `document` is untouched.

    Every count and string is rechecked from scratch. A part that is not its expected container
    shape -- a list for `categories`/`memory_files`/`mcp_servers`/`agents`, an object with its own
    required fields for `skills` -- reads null, keeping `document`'s own reason for it when that
    is a known reason, else `unrecognized`; `reasons` holds exactly these null parts. A part that
    *is* its expected shape never goes null just because some of its rows are bad: each bad row
    (not an object, off its kind, or holding a string unfit for the browser) is simply dropped and
    counted in `hidden[part]`, along with any row past that part's cap.
    """
    given = document.get("reasons")
    given_reasons = given if isinstance(given, Mapping) else {}
    result: dict[str, object] = {
        "observed_at": _text(document.get("observed_at")),
        "total_tokens": _count(document.get("total_tokens")),
        "max_tokens": _count(document.get("max_tokens")),
        "raw_max_tokens": _count(document.get("raw_max_tokens")),
        "percentage": _percentage(document.get("percentage")),
        "auto_compact": _auto_compact(document.get("auto_compact")),
    }
    hidden: dict[str, int] = {}
    reasons: dict[str, str] = {}
    for part in REPORT_PARTS:
        raw = document.get(part)
        if part == "skills":
            value, dropped = _skills(raw)
        elif isinstance(raw, list):
            read_row, cap = _LIST_PARTS[part]
            value, dropped = _capped_rows(raw, read_row, cap)
        else:
            value, dropped = None, 0
        result[part] = value
        hidden[part] = dropped
        if value is None:
            reason = given_reasons.get(part)
            reasons[part] = (
                reason if isinstance(reason, str) and reason in REASONS else "unrecognized"
            )
    result["hidden"] = hidden
    result["reasons"] = reasons
    return result


class ContextActor(Protocol):
    async def context(self) -> dict[str, object] | None: ...


class ContextSessions(Protocol):
    def get(self, session_id: str) -> ContextActor | None: ...


def context_routes(
    sessions: ContextSessions,
    *,
    authorized: Callable[[Request], bool],
    origins: frozenset[str],
    workers: set[asyncio.Task[Any]],
) -> list[Route]:
    slots = anyio.CapacityLimiter(MAX_CONTEXT_REQUESTS)

    async def perform(session_id: str) -> Response:
        actor = sessions.get(session_id)
        if actor is None:
            return failure("not_found", "This chat is not open.", 404)
        document = await actor.context()
        if document is None:
            return failure("not_running", "This chat is not running.", 409)
        response = json_response(sanitize_context(document))
        if len(response.body) > MAX_ANSWER_BYTES:
            return failure("too_large", "The context report is too large.", 413)
        return response

    async def admitted(session_id: str) -> Response:
        permit = object()
        try:
            slots.acquire_on_behalf_of_nowait(permit)
        except anyio.WouldBlock:
            return failure("overloaded", "The context report is busy.", 429)
        task = asyncio.create_task(perform(session_id))
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
        if not authorized(request):
            response = failure("unauthorized", "Authentication is required.", 401)
        elif request.headers.get("origin") not in {None, *origins}:
            response = failure("forbidden_origin", "This origin is not allowed.", 403)
        elif request.scope.get("query_string") or not _canonical_uuid(session_id):
            response = failure("invalid_input", "The context request is invalid.", 400)
        else:
            response = await admitted(session_id)
        response.headers["Cache-Control"] = "no-store"
        return response

    return [Route("/api/v2/sessions/{session_id}/context", endpoint, methods=["GET"])]
