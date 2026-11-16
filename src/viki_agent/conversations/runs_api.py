"""The run requests route and the run-stop route: a chat's open tool-use requests, each joined to
the run that raised it, and stopping one of the chat's live runs.

`GET /api/v2/sessions/{session_id}/requests` answers the session's actor's pending requests. Every
item is re-checked against the browser's shape: a `tool_use_id` matching
`subagents_api.TOOL_USE_ID`, a `run` that is a run id or null, and a `raised_at` stamp of the form
`_context_percent` writes. An off-shape item is dropped, and at most MAX_ITEMS are kept. No actor
for the session answers not_found.

`POST /api/v2/sessions/{session_id}/runs/{tool_use_id}/stop`, with no body, asks the actor to stop
that run, answering `{"status": "sent"}` once the control message is sent. A `RunStopError` from
the actor answers its own code: unknown_run, not_running or refused as 409, timeout as 503. No
actor answers not_found, and at most MAX_STOPS stops run at a time; a later one answers overloaded.

Both sit behind the Bearer token and the Origin check, a POST naming its Origin. Every answer sends
`Cache-Control: no-store`, and the route logs nothing.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Protocol

import anyio
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from viki_agent.api_json import failure, json_response
from viki_agent.conversations.subagents_api import TOOL_USE_ID
from viki_agent.drivers.base import RunStopCode, RunStopError

MAX_ITEMS = 64
MAX_STOPS = 4
RAISED_AT = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", re.ASCII)  # matched whole
INVALID = "The run request is invalid."
NOT_FOUND = "This chat is not open."
OVERLOADED = "Too many stop requests at once."

_STOP_OUTCOMES: dict[RunStopCode, tuple[int, str]] = {
    "not_running": (409, "Claude Code is not running for this chat."),
    "unknown_run": (409, "This run is not running in this chat's Claude Code process."),
    "refused": (409, "Claude Code did not stop this run."),
    "timeout": (503, "Claude Code did not answer in time."),
}


class _Actor(Protocol):
    def requests(self) -> list[dict[str, object]]: ...

    async def stop_run(self, block_id: str) -> None: ...


class _Sessions(Protocol):
    def get(self, session_id: str) -> _Actor | None: ...


def _canonical_uuid(value: str) -> bool:
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False


def _item(raw: object) -> dict[str, object] | None:
    """`raw` re-shaped to exactly `{"tool_use_id", "run", "raised_at"}`, or None off-shape."""
    if not isinstance(raw, dict):
        return None
    tool_use_id = raw.get("tool_use_id")
    run = raw.get("run")
    raised_at = raw.get("raised_at")
    if (
        not isinstance(tool_use_id, str)
        or not TOOL_USE_ID.fullmatch(tool_use_id)
        or not (run is None or (isinstance(run, str) and TOOL_USE_ID.fullmatch(run)))
        or not isinstance(raised_at, str)
        or not RAISED_AT.fullmatch(raised_at)
    ):
        return None
    try:  # the regex checks shape only; refuse a stamp that is not a real calendar date/time
        datetime.strptime(raised_at, "%Y-%m-%dT%H:%M:%S.%fZ")
    except ValueError:
        return None
    return {"tool_use_id": tool_use_id, "run": run, "raised_at": raised_at}


def _filtered(raw_items: list[dict[str, object]]) -> list[dict[str, object]]:
    """The items of `raw_items` that fit the browser's shape, in order, at most MAX_ITEMS."""
    kept: list[dict[str, object]] = []
    for raw in raw_items:
        if len(kept) == MAX_ITEMS:
            break
        item = _item(raw)
        if item is not None:
            kept.append(item)
    return kept


def run_routes(
    sessions: _Sessions,
    *,
    authorized: Callable[[Request], bool],
    origins: frozenset[str],
) -> list[Route]:
    slots = anyio.CapacityLimiter(MAX_STOPS)

    async def perform(session_id: str, tool_use_id: str) -> Response:
        actor = sessions.get(session_id)
        if actor is None:
            return failure("not_found", NOT_FOUND, 404)
        try:
            await actor.stop_run(tool_use_id)
        except RunStopError as exc:
            status, message = _STOP_OUTCOMES[exc.code]
            return failure(exc.code, message, status)
        return json_response({"status": "sent"})

    async def admitted(session_id: str, tool_use_id: str) -> Response:
        try:
            slots.acquire_nowait()
        except anyio.WouldBlock:
            return failure("overloaded", OVERLOADED, 429)
        try:
            return await perform(session_id, tool_use_id)
        finally:
            slots.release()

    async def requests_endpoint(request: Request) -> Response:
        session_id = request.path_params["session_id"]
        if not authorized(request):
            response = failure("unauthorized", "Authentication is required.", 401)
        elif request.headers.get("origin") not in {None, *origins}:
            response = failure("forbidden_origin", "This origin is not allowed.", 403)
        elif request.scope.get("query_string") or not _canonical_uuid(session_id):
            response = failure("invalid_input", INVALID, 400)
        else:
            actor = sessions.get(session_id)
            if actor is None:
                response = failure("not_found", NOT_FOUND, 404)
            else:
                response = json_response({"requests": _filtered(actor.requests())})
        response.headers["Cache-Control"] = "no-store"
        return response

    async def stop_endpoint(request: Request) -> Response:
        session_id = request.path_params["session_id"]
        tool_use_id = request.path_params["tool_use_id"]
        if not authorized(request):
            response = failure("unauthorized", "Authentication is required.", 401)
        elif request.headers.get("origin") not in origins:
            response = failure("forbidden_origin", "This origin is not allowed.", 403)
        elif (
            request.scope.get("query_string")
            or not _canonical_uuid(session_id)
            or not TOOL_USE_ID.fullmatch(tool_use_id)
            or request.headers.get("content-length") not in (None, "0")
            or request.headers.get("transfer-encoding") is not None
        ):
            response = failure("invalid_input", INVALID, 400)
        else:
            response = await admitted(session_id, tool_use_id)
        response.headers["Cache-Control"] = "no-store"
        return response

    return [
        Route("/api/v2/sessions/{session_id}/requests", requests_endpoint, methods=["GET"]),
        Route(
            "/api/v2/sessions/{session_id}/runs/{tool_use_id}/stop",
            stop_endpoint,
            methods=["POST"],
        ),
    ]
