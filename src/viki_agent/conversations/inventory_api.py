"""The live tool inventory's route and its last filter before the browser.

`sanitize` trusts nothing in the driver's document: it keeps only the known keys, drops every
string that looks like a secret or runs past 256 UTF-16 units (the browser's measure), and caps
every list; the caps bound the answer. Past a 256 KiB budget it drops the servers' tool names
first.

`inventory_routes` serves one running chat's inventory behind the Bearer token and the Origin
check, two requests at a time; an answer past the browser's 1 MiB reads `too_large`. It logs
nothing: never the token, a session id or inventory text.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Callable, Mapping
from functools import partial
from itertools import islice
from typing import Any, Protocol, cast

import anyio
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from viki_agent.api_json import failure, json_response
from viki_agent.app.diagnostics import _looks_secret
from viki_agent.drivers.claude.driver import MCP_STATUSES

MAX_INVENTORY_REQUESTS = 2
MAX_STRING_CHARS = 256  # UTF-16 units, as the browser counts them
TOOLS_BUDGET_BYTES = 256 * 1024
MAX_ANSWER_BYTES = 1 << 20  # the browser refuses a larger body
MAX_SERVERS = 64
MAX_ENTRIES = 256  # tool names per server, and each other part's entries

REASONS = frozenset({"not_observed", "absent", "unrecognized", "timeout", "unavailable"})

# A server row's fields that become null, rather than dropping the row, when not fit to show.
_SERVER_OPTIONAL = ("scope", "source", "server_name", "server_version", "egress")
# Each other object part's fields and their kinds: "str" or "bool".
_TOOL_FIELDS = {"name": "str", "server": "str", "loaded": "bool"}
_MEMORY_FIELDS = {"path": "str", "type": "str"}
_AGENT_FIELDS = {"type": "str", "source": "str"}


def _text(value: object) -> str | None:
    """`value` when it is a string the browser may see, else None."""
    if (
        isinstance(value, str)
        # A lone surrogate survives JSON decoding, so it is counted rather than refused.
        and len(value.encode("utf-16-le", "surrogatepass")) // 2 <= MAX_STRING_CHARS
        and not _looks_secret(value)
    ):
        return value
    return None


def _fields(spec: Mapping[str, str], item: object) -> dict[str, object] | None:
    """A new object with `spec`'s fields of `item`, or None when `item` is not an object or any
    field is off its kind or holds a string the browser may not see: such an object goes whole."""
    if not isinstance(item, Mapping):
        return None
    kept: dict[str, object] = {}
    for key, kind in spec.items():
        value = item.get(key)
        fits = isinstance(value, bool) if kind == "bool" else _text(value) is not None
        if not fits:
            return None
        kept[key] = value
    return kept


def _server(item: object) -> dict[str, object] | None:
    """A new server row, or None when its name, status or tools are off. A row names a live
    server, so one other field not fit to show becomes null instead of hiding the server."""
    if not isinstance(item, Mapping):
        return None
    name, status, tools = _text(item.get("name")), _text(item.get("status")), item.get("tools")
    if name is None or status not in MCP_STATUSES:
        return None
    if tools is not None and not isinstance(tools, list):
        return None
    kept: dict[str, object] = {"name": name, "status": status}
    kept |= {key: _text(item.get(key)) for key in _SERVER_OPTIONAL}
    kept["tools"] = None if tools is None else _capped(tools, _text, MAX_ENTRIES)
    kept["tools_omitted"] = False
    return kept


def _capped(items: list[object], keep: Callable[[object], object | None], cap: int) -> list[object]:
    """The first `cap` items `keep` admits, as `keep` rebuilt them."""
    return list(islice((kept for kept in map(keep, items) if kept is not None), cap))


_PARTS: dict[str, tuple[Callable[[object], object | None], int]] = {
    "mcp_servers": (_server, MAX_SERVERS),
    "mcp_tools": (partial(_fields, _TOOL_FIELDS), MAX_ENTRIES),
    "skills": (_text, MAX_ENTRIES),
    "memory_files": (partial(_fields, _MEMORY_FIELDS), MAX_ENTRIES),
    "system_tools": (_text, MAX_ENTRIES),
    "agents": (partial(_fields, _AGENT_FIELDS), MAX_ENTRIES),
}


def _size(document: dict[str, object]) -> int:
    # The encoding `api_json.json_response` sends: ASCII, so characters are bytes.
    return len(json.dumps(document, ensure_ascii=True, separators=(",", ":")))


def sanitize(document: Mapping[str, object]) -> dict[str, object]:
    """A new inventory document holding only what the browser may see; `document` is untouched.

    A part that is not a list becomes null. Every null part keeps its reason when that is a known
    code, else reads `unrecognized`, so the browser always has one; a listed part carries none."""
    given = document.get("reasons")
    reasons = given if isinstance(given, Mapping) else {}
    result: dict[str, object] = {"observed_at": _text(document.get("observed_at"))}
    kept_reasons: dict[str, str] = {}
    for part, (keep, cap) in _PARTS.items():
        value = document.get(part)
        result[part] = _capped(value, keep, cap) if isinstance(value, list) else None
        if result[part] is None:
            reason = reasons.get(part)
            kept_reasons[part] = (
                reason if isinstance(reason, str) and reason in REASONS else "unrecognized"
            )
    result["reasons"] = kept_reasons
    servers = cast("list[dict[str, object]] | None", result["mcp_servers"])
    if servers and _size(result) > TOOLS_BUDGET_BYTES:
        result["mcp_servers"] = [{**s, "tools": [], "tools_omitted": True} for s in servers]
    return result


class InventoryActor(Protocol):
    async def inventory(self) -> dict[str, object] | None: ...


class InventorySessions(Protocol):
    def get(self, session_id: str) -> InventoryActor | None: ...


def _canonical_uuid(value: str) -> bool:
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False


def inventory_routes(
    sessions: InventorySessions,
    *,
    authorized: Callable[[Request], bool],
    origins: frozenset[str],
    workers: set[asyncio.Task[Any]],
) -> list[Route]:
    slots = anyio.CapacityLimiter(MAX_INVENTORY_REQUESTS)

    async def perform(session_id: str) -> Response:
        actor = sessions.get(session_id)
        if actor is None:
            return failure("not_found", "This chat is not open.", 404)
        document = await actor.inventory()
        if document is None:
            return failure("not_running", "This chat is not running.", 409)
        response = json_response(sanitize(document))
        if len(response.body) > MAX_ANSWER_BYTES:
            return failure("too_large", "The tool inventory is too large.", 413)
        return response

    async def admitted(session_id: str) -> Response:
        permit = object()
        try:
            slots.acquire_on_behalf_of_nowait(permit)
        except anyio.WouldBlock:
            return failure("overloaded", "The tool inventory is busy.", 429)
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
            response = failure("invalid_input", "The inventory request is invalid.", 400)
        else:
            response = await admitted(session_id)
        response.headers["Cache-Control"] = "no-store"
        return response

    return [Route("/api/v2/sessions/{session_id}/inventory", endpoint, methods=["GET"])]
