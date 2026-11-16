"""Authenticated bookmark routes: app-owned state, apart from the read-only vault routes."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import asdict
from typing import Any

import anyio
from anyio.to_thread import run_sync
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from viki_agent.api_json import failure, json_response
from viki_agent.storage import StateError
from viki_agent.vaults.vault_api import MAX_VAULT_REQUESTS
from viki_agent.vaults.vault_bookmarks import BookmarkError, BookmarkService

_MESSAGES = {
    "invalid_ref": "Check the note's location.",
    "private_path": "This location can't be bookmarked.",
    "unknown_workspace": "Vault not found.",
    "bookmarks_full": "Bookmarks are full. Remove one you no longer need, then try again.",
    "invalid_representation": "This request can't include content.",
}


def _error(exc: BookmarkError) -> Response:
    if exc.code in {"unknown_workspace", "private_path"}:
        status = 404
    elif exc.code == "bookmarks_full":
        status = 409
    else:
        status = 400
    return failure(exc.code, _MESSAGES.get(exc.code, "Couldn't process the bookmark."), status)


def _bodiless(request: Request) -> bool:
    headers = request.headers
    if "transfer-encoding" in headers or headers.get("content-length", "0") != "0":
        return False
    if headers.get("content-encoding", "identity").strip().lower() != "identity":
        return False
    content_type = headers.get("content-type")
    return content_type is None or content_type.split(";")[0].strip().lower() == "application/json"


def bookmark_routes(
    service: BookmarkService,
    *,
    authorized: Callable[[Request], bool],
    origins: frozenset[str],
    workers: set[asyncio.Task[Any]],
) -> list[Route]:
    capacity = anyio.CapacityLimiter(MAX_VAULT_REQUESTS)

    async def perform(request: Request) -> Response:
        workspace_id = request.path_params["workspace_id"]
        ref = request.path_params.get("ref")
        if ref is None:
            marks = await run_sync(service.list, workspace_id)
            return json_response({"bookmarks": [asdict(mark) for mark in marks]})
        if not _bodiless(request) or await request.body():
            return failure("invalid_representation", _MESSAGES["invalid_representation"], 415)
        if request.method == "PUT":
            mark = await run_sync(service.add, workspace_id, ref)
            return json_response({"bookmark": asdict(mark)})
        return json_response({"removed": await run_sync(service.remove, workspace_id, ref)})

    async def endpoint(request: Request) -> Response:
        if not authorized(request):
            return failure("unauthorized", "Can't authenticate. Open VIKI Agent again.", 401)
        origin = request.headers.get("origin")
        # A state-changing PUT or DELETE needs a listed Origin; only the GET may omit it.
        if origin not in origins and (origin is not None or request.method != "GET"):
            return failure("forbidden_origin", "This window can't do that.", 403)
        permit = object()
        try:
            capacity.acquire_on_behalf_of_nowait(permit)
        except anyio.WouldBlock:
            return failure(
                "overloaded",
                "Busy processing bookmarks. Wait a moment, then try again.",
                429,
            )
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
        except BookmarkError as exc:
            return _error(exc)
        except StateError:
            return failure("unavailable", "Can't save the bookmark.", 503)

    return [
        Route("/api/v2/vaults/{workspace_id}/bookmarks", endpoint, methods=["GET"]),
        Route(
            "/api/v2/vaults/{workspace_id}/bookmarks/{ref:path}",
            endpoint,
            methods=["PUT", "DELETE"],
        ),
    ]
