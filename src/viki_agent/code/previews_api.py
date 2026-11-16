"""Authenticated, admission-bounded routes for previews.

Start and Open pass the root gate in `PreviewService` before any process or window; stop,
remove, output and the list act by id or folder and start nothing. A start spawns a process, so
each service call runs in a shielded task of its own: a client that leaves does not leave a
process without its supervisor. Every answer carries `Cache-Control: no-store`, and a refusal
holds a fixed message, never a command, a port, a path, an id or an exception's text.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from dataclasses import asdict
from typing import Any

import anyio
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from viki_agent.api_json import json_response
from viki_agent.code import checks_api, previews, roots_api
from viki_agent.code.checks_api import _EMPTY_BODY, _OUTPUT_KEYS, _integer, _offset, _string
from viki_agent.code.code_api import _canonical
from viki_agent.code.code_checks import CodeRunError
from viki_agent.code.code_files import CodeError
from viki_agent.code.integration_api import _query
from viki_agent.code.previews import PreviewService
from viki_agent.code.roots_api import _body, _uuid_or_null
from viki_agent.storage import StateError

MAX_PREVIEW_REQUESTS = 8
MAX_BODY_BYTES = 65_536
_LOGGER = logging.getLogger(__name__)

# The inert set's file codes are left out: no route here raises them.
_STATUS = {
    code: status
    for status, codes in {
        400: "invalid_body invalid_query invalid_command invalid_port disconnected",
        401: "unauthorized",
        403: "forbidden_origin",
        404: "unknown_workspace unknown_worktree unknown_preview",
        408: "intake_timeout",
        409: "trust_required root_unavailable worktree_not_ready port_reserved port_in_use "
        "port_taken preview_limit window_limit not_listening preview_running "
        "unsupported_platform",
        413: "too_large",
        429: "overloaded",
        503: "start_failed browser_missing unavailable",
    }.items()
    for code in codes.split()
}
_MESSAGES = {
    **{code: roots_api._MESSAGES[code] for code in _STATUS if code in roots_api._MESSAGES},
    "invalid_command": checks_api._MESSAGES["invalid_command"],
    "invalid_port": "Use a port from 1024 to 65535.",
    "port_reserved": "Port 8765 is VIKI's own. Choose another.",
    "port_in_use": "Another preview uses this port.",
    "port_taken": "Another program uses this port, so VIKI does not open it.",
    "preview_limit": "Two previews are running. Stop one first.",
    "window_limit": "Four preview windows are open. Close one first.",
    "not_listening": "Nothing listens on this port yet. Wait for the server, then try again.",
    "preview_running": "Stop the preview first.",
    "unknown_preview": "This preview no longer exists. Refresh the list.",
    "unsupported_platform": "Previews work on Windows in this version.",
    "browser_missing": "No browser for preview windows was found.",
    "start_failed": "The command could not start. Check that the folder still exists.",
}

# The port's range and the command's rules are the service's.
_START_BODY = {"worktree_id": _uuid_or_null, "command": _string, "port": _integer}

Perform = Callable[[Request], Coroutine[Any, Any, Response]]


def _refusal(code: str) -> Response:
    if code not in _STATUS:
        code = "unavailable"
    return json_response({"error": {"code": code, "message": _MESSAGES[code]}}, _STATUS[code])


def endpoint(
    perform: Perform,
    *,
    capacity: anyio.CapacityLimiter,
    post: bool,
    path_id: tuple[str, str],
    authorized: Callable[[Request], bool],
    origins: frozenset[str],
    workers: set[asyncio.Task[Any]],
) -> Perform:
    """`path_id` names the path parameter that must be a canonical UUID, and the code answered
    when it is not. A GET reads its own query; a POST takes none."""

    async def admitted(request: Request) -> Response:
        origin = request.headers.get("origin")
        if origin not in origins and (post or origin is not None):
            return _refusal("forbidden_origin")
        if not previews.PREVIEWS_SUPPORTED:  # read at call time, so tests can patch it
            return _refusal("unsupported_platform")
        if not _canonical(request.path_params[path_id[0]]):
            return _refusal(path_id[1])
        if post and request.scope.get("query_string"):
            return _refusal("invalid_query")
        permit = object()
        try:
            capacity.acquire_on_behalf_of_nowait(permit)
        except anyio.WouldBlock:
            return _refusal("overloaded")
        # A task of its own, shielded: a client that leaves does not cancel a start between its
        # spawn and its supervisor, nor a window's start partway.
        worker = asyncio.create_task(perform(request))
        workers.add(worker)

        def done(task: asyncio.Task[Response]) -> None:
            workers.discard(task)
            capacity.release_on_behalf_of(permit)
            if not task.cancelled():
                task.exception()

        worker.add_done_callback(done)
        try:
            return await asyncio.shield(worker)
        except (CodeRunError, CodeError) as error:
            return _refusal(error.code)
        except StateError:
            return _refusal("unavailable")
        except Exception as error:
            _LOGGER.warning("previews route failed (%s)", type(error).__name__)
            return _refusal("unavailable")

    async def handler(request: Request) -> Response:
        response = await admitted(request) if authorized(request) else _refusal("unauthorized")
        response.headers["Cache-Control"] = "no-store"
        return response

    return handler


def preview_routes(
    service: PreviewService,
    *,
    authorized: Callable[[Request], bool],
    origins: frozenset[str],
    workers: set[asyncio.Task[Any]],
) -> list[Route]:
    capacity = anyio.CapacityLimiter(MAX_PREVIEW_REQUESTS)

    async def list_previews(request: Request) -> Response:
        _query(request, frozenset(), frozenset())
        views = await service.list(request.path_params["workspace_id"])
        return json_response({"previews": [asdict(view) for view in views]})

    async def start_preview(request: Request) -> Response:
        body = await _body(request, _START_BODY, limit=MAX_BODY_BYTES)
        view = await service.start(
            request.path_params["workspace_id"], body["worktree_id"], body["command"], body["port"]
        )
        return json_response({"preview": asdict(view)}, 201)

    async def stop_preview(request: Request) -> Response:
        await _body(request, _EMPTY_BODY, limit=MAX_BODY_BYTES)
        view = await service.stop(request.path_params["preview_id"])
        return json_response({"preview": asdict(view)})

    async def open_preview(request: Request) -> Response:
        await _body(request, _EMPTY_BODY, limit=MAX_BODY_BYTES)
        await service.open(request.path_params["preview_id"])
        return json_response({"opened": True})

    async def remove_preview(request: Request) -> Response:
        await _body(request, _EMPTY_BODY, limit=MAX_BODY_BYTES)
        await service.remove(request.path_params["preview_id"])
        return json_response({"removed": True})

    async def preview_output(request: Request) -> Response:
        after = _offset(_query(request, _OUTPUT_KEYS, frozenset()))
        view, part, done = await service.output(request.path_params["preview_id"], after)
        return json_response(
            {
                "preview": asdict(view),
                "start": part.start,
                "next": part.next,
                "text": part.text,
                "dropped": part.dropped,
                "done": done,
            }
        )

    def route(path: str, perform: Perform, method: str, path_id: tuple[str, str]) -> Route:
        handler = endpoint(
            perform,
            capacity=capacity,
            post=method == "POST",
            path_id=path_id,
            authorized=authorized,
            origins=origins,
            workers=workers,
        )
        return Route(path, handler, methods=[method])

    workspace = ("workspace_id", "unknown_workspace")
    preview = ("preview_id", "unknown_preview")
    return [
        route("/api/v2/code/{workspace_id}/previews", list_previews, "GET", workspace),
        route("/api/v2/code/{workspace_id}/previews", start_preview, "POST", workspace),
        route("/api/v2/previews/{preview_id}/stop", stop_preview, "POST", preview),
        route("/api/v2/previews/{preview_id}/open", open_preview, "POST", preview),
        route("/api/v2/previews/{preview_id}/remove", remove_preview, "POST", preview),
        route("/api/v2/previews/{preview_id}/output", preview_output, "GET", preview),
    ]
