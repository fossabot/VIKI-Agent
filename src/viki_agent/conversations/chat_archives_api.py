"""Authenticated routes for encrypted, read-only conversation archives."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import asdict
from typing import Any

import anyio
from anyio.to_thread import run_sync
from starlette.requests import ClientDisconnect, Request
from starlette.responses import Response
from starlette.routing import Route

from viki_agent.api_json import failure as _failure
from viki_agent.api_json import json_response as _json
from viki_agent.api_json import unique_object as _unique
from viki_agent.conversations.chat_archives import (
    MAX_ARCHIVE_BYTES,
    ArchiveConflict,
    ArchiveError,
    ConversationArchiveService,
)

MAX_ARCHIVE_REQUEST_BYTES = MAX_ARCHIVE_BYTES + (64 << 10)
ARCHIVE_BODY_TIMEOUT_S = 10.0
MAX_ARCHIVE_REQUESTS = 2

_STATUS = {
    "invalid_archive": 400,
    "invalid_media": 415,
    "too_large": 413,
    "conflict": 409,
    "not_found": 404,
    "capacity": 429,
}
_MESSAGES = {
    "invalid_archive": "Check the conversation archive's contents.",
    "invalid_media": "Couldn't load an image in the conversation archive.",
    "too_large": "Keep the conversation archive to 8 MiB or less.",
    "conflict": "A different conversation archive with the same ID is already saved.",
    "not_found": "Conversation archive not found.",
    "capacity": "The conversation archive storage limit has been reached.",
}


def conversation_archive_routes(
    service: ConversationArchiveService,
    *,
    authorized: Callable[[Request], bool],
    origins: frozenset[str],
    workers: set[asyncio.Task[Any]],
) -> list[Route]:
    capacity = anyio.CapacityLimiter(MAX_ARCHIVE_REQUESTS)

    def refusal(request: Request, *, mutate: bool) -> Response | None:
        if not authorized(request):
            return _failure("unauthorized", "Can't authenticate. Open VIKI Agent again.", 401)
        origin = request.headers.get("origin")
        if (origin is not None and origin not in origins) or (mutate and origin not in origins):
            return _failure("forbidden_origin", "This window can't do that.", 403)
        if request.scope.get("query_string", b""):
            return _failure("invalid_archive", _MESSAGES["invalid_archive"], 400)
        return None

    async def endpoint(request: Request) -> Response:
        denied = refusal(request, mutate=request.method in {"POST", "DELETE"})
        if denied is not None:
            return denied
        permit = object()
        try:
            capacity.acquire_on_behalf_of_nowait(permit)
        except anyio.WouldBlock:
            return _failure(
                "overloaded", "Busy processing the conversation archive. Wait a moment.", 429
            )
        task = asyncio.create_task(perform(request))
        workers.add(task)

        def done(completed: asyncio.Task[Response]) -> None:
            workers.discard(completed)
            capacity.release_on_behalf_of(permit)
            if not completed.cancelled():
                completed.exception()

        task.add_done_callback(done)
        try:
            return await asyncio.shield(task)
        except ArchiveConflict as exc:
            return _failure(exc.code, _MESSAGES[exc.code], 409)
        except ArchiveError as exc:
            return _failure(
                exc.code,
                _MESSAGES.get(exc.code, "Couldn't process the conversation archive."),
                _STATUS.get(exc.code, 503),
            )
        except ClientDisconnect:
            return _failure(
                "disconnected", "Receiving the conversation archive was interrupted.", 400
            )

    async def perform(request: Request) -> Response:
        archive_id = request.path_params.get("archive_id")
        if request.method in {"GET", "HEAD"}:
            if archive_id is None:
                return _json({"archives": [asdict(item) for item in await service.list()]})
            record = await service.get(archive_id)
            return await run_sync(lambda: _json(asdict(record)))
        if request.method == "DELETE":
            try:
                with anyio.fail_after(5):
                    async for chunk in request.stream():
                        if chunk:
                            return _failure("invalid_archive", _MESSAGES["invalid_archive"], 400)
            except TimeoutError:
                return _failure(
                    "intake_timeout", "Receiving the conversation archive took too long.", 408
                )
            return Response(status_code=204 if await service.remove(archive_id or "") else 404)
        if (
            request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            != "application/json"
            or request.headers.get("content-encoding", "identity").lower() != "identity"
        ):
            return _failure("invalid_representation", "Send the request as JSON.", 415)
        body = bytearray()
        try:
            with anyio.fail_after(ARCHIVE_BODY_TIMEOUT_S):
                async for chunk in request.stream():
                    if len(chunk) > MAX_ARCHIVE_REQUEST_BYTES - len(body):
                        return _failure("too_large", _MESSAGES["too_large"], 413)
                    body.extend(chunk)
        except TimeoutError:
            return _failure(
                "intake_timeout", "Receiving the conversation archive took too long.", 408
            )
        try:
            value = await run_sync(
                lambda: json.loads(body.decode("utf-8"), object_pairs_hook=_unique)
            )
        except UnicodeError, ValueError, RecursionError:
            raise ArchiveError("invalid_archive") from None
        if not isinstance(value, dict) or set(value) != {"archive_id", "archive"}:
            raise ArchiveError("invalid_archive")
        record, created = await service.create(value["archive_id"], value["archive"])
        return await run_sync(lambda: _json(asdict(record), 201 if created else 200))

    return [
        Route("/api/v2/conversation-archives", endpoint, methods=["GET", "POST"]),
        Route("/api/v2/conversation-archives/{archive_id}", endpoint, methods=["GET", "DELETE"]),
    ]
