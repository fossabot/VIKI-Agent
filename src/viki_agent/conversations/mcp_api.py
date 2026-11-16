"""The MCP server control routes: turning one server on or off, and reconnecting it, for one
chat's live Claude Code process.

`POST /api/v2/sessions/{session_id}/mcp/toggle` takes exactly `{"name": str, "enabled": bool}`;
`POST .../mcp/reconnect` takes exactly `{"name": str}`. Each asks the session's actor
(`SessionActor.set_mcp_server` / `.reconnect_mcp_server`) to change that server's live state and,
on success, answers 200 `{"status": "done"}`. Both sit behind the Bearer token and a required
Origin, then a canonical session id with no query string, a body that is `application/json`, at
most MAX_BODY bytes read within INTAKE_TIMEOUT_S, with no duplicate or extra keys, `enabled` a
real bool and `name` fit for the browser (`inventory_api._text`). An `McpControlError` from the
actor answers its own code -- every one 409 except `timeout` (503); no actor for the session
answers not_found. At most MAX_REQUESTS of either call run at a time, shielded in `workers` like
`inventory_api.inventory_routes`; a later one answers overloaded. Every answer sends
Cache-Control: no-store, and the route logs nothing.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

import anyio
from starlette.requests import ClientDisconnect, Request
from starlette.responses import Response
from starlette.routing import Route

from viki_agent.api_json import failure, json_response, unique_object
from viki_agent.conversations.inventory_api import _canonical_uuid, _text
from viki_agent.drivers.base import McpControlCode, McpControlError

MAX_BODY = 1024  # 1 KiB
INTAKE_TIMEOUT_S = 5
MAX_REQUESTS = 2  # concurrent toggle/reconnect calls, shared across both routes
INVALID = "The MCP request is invalid."
NOT_FOUND = "This chat is not open."
OVERLOADED = "Too many MCP requests at once."

_OUTCOMES: dict[McpControlCode, tuple[int, str]] = {
    "not_running": (409, "Claude Code is not running for this chat."),
    "busy": (409, "Available when this reply finishes."),
    "unknown_server": (409, "This chat's Claude Code does not list that server."),
    "managed": (409, "This server comes from a profile. Change it there."),
    "not_approved": (409, "Approve this server with /mcp in a terminal first."),
    "blocked": (409, "Your organization's policy blocks this server."),
    "refused": (409, "Claude Code did not change this server."),
    "not_connected": (409, "The server is on but did not connect. Try Retry."),
    "timeout": (503, "Claude Code did not answer in time. The change may still apply."),
}


class _Actor(Protocol):
    async def set_mcp_server(self, name: str, enabled: bool) -> None: ...
    async def reconnect_mcp_server(self, name: str) -> None: ...


class _Sessions(Protocol):
    def get(self, session_id: str) -> _Actor | None: ...


def _is_json_content_type(request: Request) -> bool:
    parts = [p.strip().lower() for p in request.headers.get("content-type", "").split(";")]
    return parts[0] == "application/json" and (
        len(parts) == 1 or (len(parts) == 2 and parts[1] in {"charset=utf-8", 'charset="utf-8"'})
    )


async def _body(request: Request) -> object | None:
    """The request's parsed JSON body, or None when it is over MAX_BODY, not valid JSON (this
    also catches a duplicate key, via `unique_object`), not read within INTAKE_TIMEOUT_S -- a
    slow or stalled client must not hold a capacity slot -- or the client disconnects mid-read."""
    body = bytearray()
    try:
        with anyio.fail_after(INTAKE_TIMEOUT_S):
            async for chunk in request.stream():
                if len(chunk) > MAX_BODY - len(body):
                    return None
                body.extend(chunk)
    except TimeoutError, ClientDisconnect:
        return None
    try:
        return json.loads(body.decode("utf-8"), object_pairs_hook=unique_object)
    except ValueError, RecursionError:
        return None


def _toggle_input(value: object) -> tuple[str, bool] | None:
    if not isinstance(value, dict) or set(value) != {"name", "enabled"}:
        return None
    name, enabled = _text(value.get("name")), value.get("enabled")
    return None if name is None or type(enabled) is not bool else (name, enabled)


def _reconnect_input(value: object) -> str | None:
    if not isinstance(value, dict) or set(value) != {"name"}:
        return None
    return _text(value.get("name"))


def mcp_routes(
    sessions: _Sessions,
    *,
    authorized: Callable[[Request], bool],
    origins: frozenset[str],
    workers: set[asyncio.Task[Any]],
) -> list[Route]:
    slots = anyio.CapacityLimiter(MAX_REQUESTS)

    async def call(session_id: str, run: Callable[[_Actor], Awaitable[None]]) -> Response:
        actor = sessions.get(session_id)
        if actor is None:
            return failure("not_found", NOT_FOUND, 404)
        try:
            await run(actor)
        except McpControlError as exc:
            status, message = _OUTCOMES[exc.code]
            return failure(exc.code, message, status)
        return json_response({"status": "done"})

    async def admitted(session_id: str, run: Callable[[_Actor], Awaitable[None]]) -> Response:
        permit = object()
        try:
            slots.acquire_on_behalf_of_nowait(permit)
        except anyio.WouldBlock:
            return failure("overloaded", OVERLOADED, 429)
        task = asyncio.create_task(call(session_id, run))
        workers.add(task)

        def done(future: asyncio.Task[Response]) -> None:
            workers.discard(future)
            slots.release_on_behalf_of(permit)
            if not future.cancelled():
                future.exception()

        task.add_done_callback(done)
        # A caller that goes away does not cancel the call or free its permit early.
        return await asyncio.shield(task)

    async def handle(
        request: Request,
        parse: Callable[[object], Any | None],
        run: Callable[[_Actor, Any], Awaitable[None]],
    ) -> Response:
        session_id = request.path_params["session_id"]
        if not authorized(request):
            response = failure("unauthorized", "Authentication is required.", 401)
        elif request.headers.get("origin") not in origins:
            response = failure("forbidden_origin", "This origin is not allowed.", 403)
        elif (
            request.scope.get("query_string")
            or not _canonical_uuid(session_id)
            or not _is_json_content_type(request)
        ):
            response = failure("invalid_input", INVALID, 400)
        else:
            body = await _body(request)
            parsed = None if body is None else parse(body)
            if parsed is None:
                response = failure("invalid_input", INVALID, 400)
            else:
                response = await admitted(session_id, lambda actor: run(actor, parsed))
        response.headers["Cache-Control"] = "no-store"
        return response

    async def toggle_endpoint(request: Request) -> Response:
        return await handle(
            request, _toggle_input, lambda actor, parsed: actor.set_mcp_server(*parsed)
        )

    async def reconnect_endpoint(request: Request) -> Response:
        return await handle(
            request, _reconnect_input, lambda actor, parsed: actor.reconnect_mcp_server(parsed)
        )

    return [
        Route("/api/v2/sessions/{session_id}/mcp/toggle", toggle_endpoint, methods=["POST"]),
        Route("/api/v2/sessions/{session_id}/mcp/reconnect", reconnect_endpoint, methods=["POST"]),
    ]
