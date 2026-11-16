"""Authenticated decision routes: the user's decision inbox.

This module logs nothing.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import anyio
from anyio.to_thread import run_sync
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from viki_agent.api_json import failure, json_response
from viki_agent.storage import StateError
from viki_agent.vaults.decisions import DecisionError, DecisionService

MAX_DECISION_REQUESTS = 4

_MESSAGES = {
    "invalid_request": "Check the decision's content.",
    "no_vault": "No vault is selected.",
    "unavailable": "Can't process the decision.",
}
_STATUS = {
    "invalid_request": 400,
    "no_vault": 422,
}


def _error(exc: DecisionError) -> Response:
    """The code, a fixed message and `extra`, which holds ids and limits only."""
    body: dict[str, Any] = {
        "code": exc.code,
        "message": _MESSAGES.get(exc.code, _MESSAGES["unavailable"]),
    }
    if exc.extra:
        body["details"] = exc.extra
    return json_response({"error": body}, _STATUS.get(exc.code, 503))


def decision_routes(
    service: DecisionService,
    *,
    authorized: Callable[[Request], bool],
    origins: frozenset[str],
    workers: set[asyncio.Task[Any]],
) -> list[Route]:
    capacity = anyio.CapacityLimiter(MAX_DECISION_REQUESTS)

    async def inbox(_request: Request) -> Response:
        return json_response(await run_sync(service.inbox))

    def admitted(
        handler: Callable[[Request], Awaitable[Response]],
    ) -> Callable[[Request], Awaitable[Response]]:
        async def respond(request: Request) -> Response:
            if request.scope.get("query_string"):
                raise DecisionError("invalid_request")
            return await handler(request)

        async def admit(request: Request) -> Response:
            permit = object()
            try:
                capacity.acquire_on_behalf_of_nowait(permit)
            except anyio.WouldBlock:
                return failure(
                    "overloaded",
                    "Busy processing decisions. Wait a moment, then try again.",
                    429,
                )
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
            except DecisionError as exc:
                return _error(exc)
            except StateError:
                return failure("unavailable", _MESSAGES["unavailable"], 503)

        async def endpoint(request: Request) -> Response:
            if not authorized(request):
                response = failure(
                    "unauthorized", "Can't authenticate. Open VIKI Agent again.", 401
                )
            else:
                origin = request.headers.get("origin")
                # A state-changing POST needs a listed Origin; only a GET may omit it.
                if origin not in origins and (origin is not None or request.method != "GET"):
                    response = failure("forbidden_origin", "This window can't do that.", 403)
                else:
                    response = await admit(request)
            response.headers["Cache-Control"] = "no-store"
            return response

        return endpoint

    return [
        Route("/api/v2/decisions", admitted(inbox), methods=["GET"]),
    ]
