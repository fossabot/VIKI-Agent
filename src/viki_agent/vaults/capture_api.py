"""Authenticated capture routes: the inbox listing, a suggestion run's status, and starting one.

A capture's `preview` is the user's vault content, read only and never written; it reaches the
listing response as designed, but never an error body, and this module logs nothing.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

import anyio
from anyio.to_thread import run_sync
from starlette.requests import ClientDisconnect, Request
from starlette.responses import Response
from starlette.routing import Route

from viki_agent.api_json import failure, json_response, unique_object
from viki_agent.storage import StateError
from viki_agent.vaults.capture_reply import Suggestion
from viki_agent.vaults.capture_suggest import CaptureSuggester, SuggestionRun
from viki_agent.vaults.captures import Capture, CaptureError, CaptureService
from viki_agent.vaults.vault_api import MAX_VAULT_REQUESTS

# The start body must be exactly `{}`; 1,024 bytes is a generous fixed cap for it.
MAX_REQUEST_BYTES = 1024

_MESSAGES = {
    "invalid_request": "Check the request body.",
    "unauthorized": "Not authorized. Reload the window.",
    "forbidden_origin": "This window cannot do that.",
    "unknown_workspace": "Vault not found.",
    "intake_timeout": "The request timed out.",
    "not_active_vault": "This is not the active vault.",
    "not_a_vault": "This workspace has no captures.",
    "no_captures": "There is nothing in the inbox to suggest for.",
    "suggestion_running": "A suggestion run is already in progress.",
    "too_large": "The request is too large.",
    "invalid_representation": "This request's format is not supported.",
    "overloaded": "Busy right now. Try again shortly.",
    "unavailable": "Captures are not available right now.",
}
_STATUS = {
    "invalid_request": 400,
    "unknown_workspace": 404,
    "not_active_vault": 409,
    "not_a_vault": 409,
    "no_captures": 409,
    "suggestion_running": 409,
    "too_large": 413,
}


class _BadRepresentation(Exception):
    """The body is not JSON the route accepts, or a GET carries one: answered 415."""


def _error(exc: CaptureError) -> Response:
    """The code, a fixed English message and `extra`, which holds ids and limits only."""
    body: dict[str, Any] = {
        "code": exc.code,
        "message": _MESSAGES.get(exc.code, _MESSAGES["unavailable"]),
    }
    if exc.extra:
        body["details"] = exc.extra
    return json_response({"error": body}, _STATUS.get(exc.code, 503))


def _capture_dict(capture: Capture, suggestion: Suggestion | None) -> dict[str, Any]:
    # An unread capture (too_large or budget) has no revision: its suggestion is bound to the
    # scan's modified stamp instead, so it keeps showing while unchanged and drops once the
    # capture changes, even if it stays unread.
    if suggestion is None or (
        suggestion.modified != capture.modified
        if suggestion.revision is None
        else suggestion.revision != capture.revision
    ):
        shown = None
    else:
        shown = {
            "to": suggestion.to,
            "project": suggestion.project,
            "why": suggestion.why,
            "at": suggestion.at,
        }
    return {
        "ref": capture.ref,
        "revision": capture.revision,
        "modified": capture.modified,
        "captured": capture.captured,
        "preview": capture.preview or None,
        "cut": capture.cut,
        "unread": capture.unread,
        "suggestion": shown,
    }


def _run_dict(run: SuggestionRun | None) -> dict[str, Any] | None:
    if run is None:
        return None
    return {
        "run_id": run.run_id,
        "session_id": run.session_id,
        "state": run.state,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "reason": run.reason,
        "suggested": run.suggested,
        "rejected": run.rejected,
    }


def _json_representation(request: Request) -> bool:
    headers = request.headers
    if headers.get("content-encoding", "identity").strip().lower() != "identity":
        return False
    content_type = headers.get("content-type")
    return content_type is not None and (
        content_type.split(";")[0].strip().lower() == "application/json"
    )


def _bodiless(request: Request) -> bool:
    headers = request.headers
    return "transfer-encoding" not in headers and headers.get("content-length", "0") == "0"


async def _json_object(request: Request) -> dict[str, Any]:
    if not _json_representation(request):
        raise _BadRepresentation
    encoded = bytearray()
    with anyio.fail_after(5):
        async for chunk in request.stream():
            if len(chunk) > MAX_REQUEST_BYTES - len(encoded):
                raise CaptureError("too_large", limit=MAX_REQUEST_BYTES)
            encoded.extend(chunk)
    try:
        value = json.loads(encoded.decode("utf-8"), object_pairs_hook=unique_object)
    except ValueError, RecursionError:
        raise CaptureError("invalid_request") from None
    if not isinstance(value, dict):
        raise CaptureError("invalid_request")
    return value


def capture_routes(
    captures: CaptureService,
    suggester: CaptureSuggester,
    *,
    authorized: Callable[[Request], bool],
    origins: frozenset[str],
    workers: set[asyncio.Task[Any]],
) -> list[Route]:
    capacity = anyio.CapacityLimiter(MAX_VAULT_REQUESTS)

    async def listing(request: Request) -> Response:
        if not _bodiless(request):
            raise _BadRepresentation
        workspace_id = request.path_params["workspace_id"]
        result = await run_sync(captures.list, workspace_id)
        table = suggester.suggestions(workspace_id)
        return json_response(
            {
                "captures": [_capture_dict(item, table.get(item.ref)) for item in result.captures],
                "projects": list(result.projects),
                "partial": result.partial,
                "omitted": result.omitted,
                "run": _run_dict(suggester.run_for(workspace_id)),
            }
        )

    async def suggest(request: Request) -> Response:
        workspace_id = request.path_params["workspace_id"]
        if request.method == "GET":
            if not _bodiless(request):
                raise _BadRepresentation
            captures.check_active(workspace_id)
            return json_response({"run": _run_dict(suggester.run_for(workspace_id))})
        body = await _json_object(request)
        if body:
            raise CaptureError("invalid_request")
        return json_response({"run": _run_dict(await suggester.start(workspace_id))}, 202)

    def admitted(
        handler: Callable[[Request], Awaitable[Response]],
    ) -> Callable[[Request], Awaitable[Response]]:
        async def respond(request: Request) -> Response:
            if request.scope.get("query_string"):
                raise CaptureError("invalid_request")
            return await handler(request)

        async def admit(request: Request) -> Response:
            permit = object()
            try:
                capacity.acquire_on_behalf_of_nowait(permit)
            except anyio.WouldBlock:
                return failure("overloaded", _MESSAGES["overloaded"], 429)
            worker = asyncio.create_task(respond(request))
            workers.add(worker)

            def done(task: asyncio.Task[Response]) -> None:
                workers.discard(task)
                capacity.release_on_behalf_of(permit)
                if not task.cancelled():
                    task.exception()

            worker.add_done_callback(done)
            try:
                return await asyncio.shield(worker)
            except CaptureError as exc:
                return _error(exc)
            except _BadRepresentation:
                return failure("invalid_representation", _MESSAGES["invalid_representation"], 415)
            except StateError:
                return failure("unavailable", _MESSAGES["unavailable"], 503)
            except TimeoutError:
                return failure("intake_timeout", _MESSAGES["intake_timeout"], 408)
            except ClientDisconnect:
                return failure("unavailable", _MESSAGES["unavailable"], 503)

        async def endpoint(request: Request) -> Response:
            if not authorized(request):
                response = failure("unauthorized", _MESSAGES["unauthorized"], 401)
            else:
                origin = request.headers.get("origin")
                # A state-changing POST needs a listed Origin; only a GET may omit it.
                if origin not in origins and (origin is not None or request.method != "GET"):
                    response = failure("forbidden_origin", _MESSAGES["forbidden_origin"], 403)
                else:
                    response = await admit(request)
            response.headers["Cache-Control"] = "no-store"
            return response

        return endpoint

    return [
        Route("/api/v2/vaults/{workspace_id}/captures", admitted(listing), methods=["GET"]),
        Route(
            "/api/v2/vaults/{workspace_id}/captures/suggest",
            admitted(suggest),
            methods=["GET", "POST"],
        ),
    ]
