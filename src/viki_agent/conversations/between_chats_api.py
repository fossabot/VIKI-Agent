"""Serves GET /api/v2/between-chats: refusals in a fixed order, at most MAX_REQUESTS reads at a
time, each a `scan` call run in a worker thread inside a shielded task that a caller going away
does not cancel, and the scan's own body passed straight through as the 200 answer.

`scan` is `between_chats.between_chats` bound to the catalog (viki_agent.server). Its messages
are untrusted text written by other processes, so this route never inspects or logs them: a
failed scan answers 503, and nothing else about the request or the result is logged.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import anyio
from anyio.to_thread import run_sync
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from viki_agent.api_json import failure, json_response

MAX_REQUESTS = 2


def between_chats_routes(
    scan: Callable[[], dict[str, object]],
    *,
    authorized: Callable[[Request], bool],
    origins: frozenset[str],
    workers: set[asyncio.Task[Any]],
) -> list[Route]:
    slots = anyio.CapacityLimiter(MAX_REQUESTS)

    async def perform() -> dict[str, object]:
        return await run_sync(scan)

    async def admitted() -> Response:
        permit = object()
        try:
            slots.acquire_on_behalf_of_nowait(permit)
        except anyio.WouldBlock:
            return failure("overloaded", "Between chats is busy.", 429)
        task = asyncio.create_task(perform())
        workers.add(task)

        def done(future: asyncio.Task[dict[str, object]]) -> None:
            workers.discard(future)
            slots.release_on_behalf_of(permit)
            if not future.cancelled():
                future.exception()

        task.add_done_callback(done)
        try:
            # A caller that goes away does not cancel the scan or free its permit early.
            result = await asyncio.shield(task)
        except OSError:
            return failure("unavailable", "Between chats is unavailable.", 503)
        return json_response(result)

    async def endpoint(request: Request) -> Response:
        if not authorized(request):
            response = failure("unauthorized", "Authentication is required.", 401)
        elif request.headers.get("origin") not in {None, *origins}:
            response = failure("forbidden_origin", "This origin is not allowed.", 403)
        elif request.scope.get("query_string"):
            response = failure("invalid_input", "The between-chats request is invalid.", 400)
        else:
            response = await admitted()
        response.headers["Cache-Control"] = "no-store"
        return response

    return [Route("/api/v2/between-chats", endpoint, methods=["GET"])]
