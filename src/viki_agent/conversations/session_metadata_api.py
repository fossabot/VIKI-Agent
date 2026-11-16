"""Small private API for conversation labels; no native process or transcript mutations."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

import anyio
from anyio.to_thread import run_sync
from starlette.requests import ClientDisconnect, Request
from starlette.responses import Response
from starlette.routing import Route

from viki_agent.api_json import failure, json_response, unique_object
from viki_agent.conversations.session_metadata import SessionMetadataError, SessionMetadataService
from viki_agent.protection import ProtectionError
from viki_agent.storage import StateError


def session_metadata_routes(
    service: SessionMetadataService,
    *,
    authorized: Callable[[Request], bool],
    origins: frozenset[str],
    workers: set[asyncio.Task[Any]],
    copies_released: Callable[[], None] | None = None,
) -> list[Route]:
    """`copies_released` runs on the loop after every committed delete, label row or not:
    the delete releases the chat's and its Bot threads' code copies in its own transaction
    (`SessionStore.delete_session_metadata`), and they are settled outside it."""
    capacity = anyio.CapacityLimiter(4)

    async def perform(request: Request) -> Response:
        if request.url.query:
            return failure("invalid_input", "This request doesn't take a query string.", 400)
        if request.method in {"GET", "HEAD"}:
            return json_response({"sessions": await run_sync(service.list)})
        if request.headers.get("content-type", "").split(";", 1)[0].strip() != "application/json":
            return failure("invalid_representation", "Send it as JSON.", 415)
        if request.headers.get("content-encoding", "identity") != "identity":
            return failure("invalid_representation", "Send it uncompressed.", 415)
        body = bytearray()
        with anyio.fail_after(5):
            async for chunk in request.stream():
                if len(chunk) > 8192 - len(body):
                    return failure("too_large", "The name is too long.", 413)
                body.extend(chunk)
        try:
            raw = json.loads(body.decode("utf-8"), object_pairs_hook=unique_object)
        except ValueError, RecursionError:
            raise SessionMetadataError("invalid_input") from None
        if request.method == "DELETE":
            if not isinstance(raw, dict) or set(raw) != {"expected_revision"}:
                raise SessionMetadataError("invalid_input")
            deleted = await run_sync(
                lambda: service.delete(request.path_params["session_id"], raw["expected_revision"])
            )
            if copies_released is not None:
                copies_released()
            return Response(status_code=204 if deleted else 404)
        if not isinstance(raw, dict) or set(raw) != {"expected_revision", "patch"}:
            raise SessionMetadataError("invalid_input")
        return json_response(
            await run_sync(
                lambda: service.patch(
                    request.path_params["session_id"], raw["expected_revision"], raw["patch"]
                )
            )
        )

    async def endpoint(request: Request) -> Response:
        if not authorized(request):
            return failure("unauthorized", "Can't authenticate. Open VIKI Agent again.", 401)
        origin = request.headers.get("origin")
        if (origin is not None and origin not in origins) or (
            request.method in {"PATCH", "DELETE"} and origin not in origins
        ):
            return failure("forbidden_origin", "This window can't do that.", 403)
        permit = object()
        try:
            capacity.acquire_on_behalf_of_nowait(permit)
        except anyio.WouldBlock:
            return failure(
                "overloaded", "Busy processing conversation settings. Wait a moment.", 429
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
        except SessionMetadataError as exc:
            status = 409 if exc.code == "conflict" else 400
            return json_response(
                {
                    "error": {
                        "code": exc.code,
                        "message": "Check what you're saving, then try again.",
                        "current_revision": exc.revision,
                    }
                },
                status,
            )
        except StateError as exc:
            if exc.code == "capacity":
                return failure(
                    "capacity", "Saved conversation settings have reached their limit.", 429
                )
            return failure("unavailable", "Couldn't save the conversation settings.", 503)
        except ProtectionError:
            return failure("unavailable", "Couldn't save the conversation settings.", 503)
        except TimeoutError:
            return failure("intake_timeout", "Receiving the update took too long.", 408)
        except ClientDisconnect:
            return failure("disconnected", "Receiving the update was interrupted.", 400)

    return [
        Route("/api/v2/session-metadata", endpoint, methods=["GET"]),
        Route("/api/v2/session-metadata/{session_id}", endpoint, methods=["PATCH", "DELETE"]),
    ]
