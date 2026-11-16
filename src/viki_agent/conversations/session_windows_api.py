"""Authenticated, bounded requests for another native view of an existing session."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import anyio
from starlette.requests import ClientDisconnect, Request
from starlette.responses import Response
from starlette.routing import Route

from viki_agent.api_json import failure, json_response, unique_object
from viki_agent.protection import ProtectionError
from viki_agent.storage import StateError
from viki_agent.window import WindowManager

MAX_WINDOW_REQUESTS = 2
MAX_BODY_BYTES = 1024
INTAKE_TIMEOUT_S = 5.0


def session_window_routes(
    windows: WindowManager | None,
    *,
    available: Callable[[str], Awaitable[bool]],
    authorized: Callable[[Request], bool],
    origins: frozenset[str],
    workers: set[asyncio.Task[Any]],
) -> list[Route]:
    capacity = anyio.CapacityLimiter(MAX_WINDOW_REQUESTS)

    async def perform(request: Request) -> Response:
        body = bytearray()
        try:
            with anyio.fail_after(INTAKE_TIMEOUT_S):
                async for chunk in request.stream():
                    if len(chunk) > MAX_BODY_BYTES - len(body):
                        return failure("too_large", "The window request is too large.", 413)
                    body.extend(chunk)
        except TimeoutError:
            return failure("intake_timeout", "Receiving the window request took too long.", 408)
        except ClientDisconnect:
            return failure("disconnected", "The window request was interrupted.", 400)
        try:
            raw = json.loads(body.decode("utf-8"), object_pairs_hook=unique_object)
            if not isinstance(raw, dict) or set(raw) != {"session_id"}:
                raise ValueError
            session_id = raw["session_id"]
            if not isinstance(session_id, str) or str(uuid.UUID(session_id)) != session_id:
                raise ValueError
        except ValueError, RecursionError:
            return failure("invalid_input", "Check the conversation to open.", 400)
        if windows is None:
            return failure("unavailable", "Native app windows are unavailable.", 503)
        try:
            if not await available(session_id):
                return failure("unavailable_target", "This conversation is unavailable.", 404)
            async with windows.admission_lock:
                if windows.admitted_count >= 8:
                    return failure(
                        "window_limit", "Close an app window before opening another.", 429
                    )
            if not await windows.open(session_id):
                if windows.admitted_count >= 8:
                    return failure(
                        "window_limit", "Close an app window before opening another.", 429
                    )
                return failure("start_failed", "The app window could not be opened.", 503)
        except StateError, ProtectionError:
            return failure("storage_unavailable", "Conversation storage is unavailable.", 503)
        except Exception:
            return failure("unavailable", "The app window could not be opened.", 503)
        return json_response({"opened": True})

    async def admitted(request: Request) -> Response:
        if request.headers.get("origin") not in origins:
            return failure("forbidden_origin", "This window can't open another app window.", 403)
        if request.scope.get("query_string"):
            return failure("invalid_input", "The window request doesn't take a query string.", 400)
        if request.headers.get("content-type", "").split(";", 1)[
            0
        ].strip().lower() != "application/json" or any(
            value.strip().lower() != "identity"
            for value in request.headers.getlist("content-encoding")
        ):
            return failure("invalid_representation", "Send the window request as plain JSON.", 415)
        permit = object()
        try:
            capacity.acquire_on_behalf_of_nowait(permit)
        except anyio.WouldBlock:
            return failure("overloaded", "Busy opening app windows. Wait a moment.", 429)
        worker = asyncio.create_task(perform(request))
        workers.add(worker)

        def done(task: asyncio.Task[Response]) -> None:
            workers.discard(task)
            capacity.release_on_behalf_of(permit)
            if not task.cancelled():
                task.exception()

        worker.add_done_callback(done)
        # Ownership remains with the app when a requesting window goes away during spawn.
        return await asyncio.shield(worker)

    async def endpoint(request: Request) -> Response:
        response = (
            await admitted(request)
            if authorized(request)
            else failure("unauthorized", "Can't authenticate. Open VIKI Agent again.", 401)
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    return [Route("/api/v2/session-windows", endpoint, methods=["POST"])]
