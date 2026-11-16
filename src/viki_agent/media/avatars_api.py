"""Authenticated avatar transform and variant read routes over the private avatar service."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable, Coroutine
from typing import Any

import anyio
from starlette.requests import ClientDisconnect, Request
from starlette.responses import Response
from starlette.routing import Route

from viki_agent.api_json import failure, json_response, unique_object
from viki_agent.media.avatars import VARIANT_SIZES, Avatar, AvatarError, AvatarService

MAX_TRANSFORMS = 2
MAX_READS = 4
MAX_BODY_BYTES = 4096
TRANSFORM_TIMEOUT_S = 30.0  # the client waits 35 s, so this deadline's typed 503 reaches it
TRANSFORM_BODY_TIMEOUT_S = 5.0
READ_WAIT_S = 10.0  # below the client's 30 s request timeout
_FIELDS = frozenset({"avatar_id", "source_asset_id", "crop", "rotation"})
# Exact spellings only: "0256" and "master" are not variants.
_SIZES = frozenset(str(size) for size in VARIANT_SIZES)

logger = logging.getLogger(__name__)

_ERRORS: dict[str, tuple[int, str]] = {
    "invalid_request": (400, "Check the avatar details."),
    "invalid_avatar": (400, "Check the avatar details."),
    "invalid_asset": (400, "Check the source image."),
    "invalid_crop": (400, "Check the crop area."),
    "invalid_rotation": (400, "Check the rotation angle."),
    "disconnected": (400, "The request ended before it finished."),
    "intake_timeout": (408, "The avatar creation request didn't arrive in time."),
    "unauthorized": (401, "Can't authenticate. Open VIKI Agent again."),
    "forbidden_origin": (403, "This window can't do that."),
    "not_found": (404, "Image not found. Choose it again."),
    "command_conflict": (409, "This avatar already exists with different content."),
    "source_changed": (409, "The source image changed. Choose it again."),
    "asset_changed": (409, "The source image changed. Choose it again."),
    "too_large": (413, "The size exceeds the limit."),
    "invalid_representation": (415, "Send it uncompressed."),
    "transform_failed": (422, "Could not create the avatar. Try a different image."),
    "capacity": (429, "Avatar storage has reached its limit."),
    "overloaded": (429, "Another avatar is being processed. Wait a moment, then try again."),
    "worker_timeout": (503, "Creating the avatar took too long. Try again."),
    "decoder_unavailable": (
        503,
        "The image-checking feature is unavailable. Quit VIKI and run `uv sync`.",
    ),
    "unavailable": (503, "Could not save the avatar."),
}
# A read saves nothing, so its failure must not say "could not save".
_READ_UNAVAILABLE = "The avatar is unavailable."
# A source the asset store no longer serves is, to the client, simply not found.
_ALIASES = {"expired": "not_found", "missing": "not_found"}


def _refusal(code: str, *, reading: bool = False) -> Response:
    answered = _ALIASES.get(code, code)
    if answered not in _ERRORS:
        answered = "unavailable"
    status, message = _ERRORS[answered]
    if reading and answered == "unavailable":
        message = _READ_UNAVAILABLE
    if status >= 500:
        logger.warning("avatar refused: %s", code)
    return failure(answered, message, status)


def _manifest(avatar: Avatar) -> dict[str, object]:
    return {
        "avatar_id": avatar.avatar_id,
        "master": {
            "size": avatar.master.size,
            "byte_size": avatar.master.byte_size,
            "sha256": avatar.master.sha256,
        },
        "variants": [
            {"size": item.size, "byte_size": item.byte_size, "sha256": item.sha256}
            for item in avatar.variants
        ],
        "total_bytes": avatar.total_bytes,
        "created_at": avatar.created_at,
    }


def avatar_routes(
    service: AvatarService,
    *,
    authorized: Callable[[Request], bool],
    origins: frozenset[str],
    workers: set[asyncio.Task[Any]],
) -> list[Route]:
    capacity = anyio.CapacityLimiter(MAX_TRANSFORMS)
    read_capacity = anyio.CapacityLimiter(MAX_READS)

    async def tracked[T](
        call: Coroutine[Any, Any, T], limiter: anyio.CapacityLimiter, permit: object
    ) -> T:
        task = asyncio.create_task(call)
        workers.add(task)

        def done(completed: asyncio.Task[T]) -> None:
            # The permit bounds running work, so it returns only when the service call ends.
            workers.discard(completed)
            limiter.release_on_behalf_of(permit)
            if not completed.cancelled():
                completed.exception()

        task.add_done_callback(done)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # Cancel the call once; it stays in `workers` so shutdown drains it.
            task.cancel()
            raise

    async def admitted(request: Request) -> Response:
        if not authorized(request):
            return _refusal("unauthorized")
        if request.headers.get("origin") not in origins:
            return _refusal("forbidden_origin")
        if request.scope.get("query_string", b""):
            return _refusal("invalid_request")
        content_type = request.headers.get("content-type", "").split(";", 1)[0]
        if content_type.strip().lower() != "application/json":
            return _refusal("invalid_request")
        if any(
            item.strip().lower() != "identity"
            for item in request.headers.getlist("content-encoding")
        ):
            return _refusal("invalid_representation")
        body = bytearray()
        try:
            with anyio.fail_after(TRANSFORM_BODY_TIMEOUT_S):
                async for chunk in request.stream():
                    if len(chunk) > MAX_BODY_BYTES - len(body):
                        return _refusal("too_large")
                    body.extend(chunk)
        except TimeoutError:
            return _refusal("intake_timeout")
        except ClientDisconnect:
            return _refusal("disconnected")
        try:
            value = json.loads(body.decode("utf-8"), object_pairs_hook=unique_object)
        except ValueError, RecursionError:
            return _refusal("invalid_request")
        if not isinstance(value, dict) or set(value) != _FIELDS:
            return _refusal("invalid_request")
        permit = object()
        try:
            capacity.acquire_on_behalf_of_nowait(permit)
        except anyio.WouldBlock:
            return _refusal("overloaded")
        call = service.transform(
            value["avatar_id"], value["source_asset_id"], value["crop"], value["rotation"]
        )
        try:
            async with asyncio.timeout(TRANSFORM_TIMEOUT_S):
                avatar = await tracked(call, capacity, permit)
        except TimeoutError:
            return _refusal("worker_timeout")
        except AvatarError as exc:
            return _refusal(exc.code)
        return json_response(_manifest(avatar), 201)

    async def fetched(request: Request) -> Response:
        if not authorized(request):
            return _refusal("unauthorized")
        origin = request.headers.get("origin")
        if origin is not None and origin not in origins:
            return _refusal("forbidden_origin")
        if request.scope.get("query_string", b""):
            return _refusal("invalid_request")
        size = request.path_params["size"]
        if size not in _SIZES:
            return _refusal("not_found")
        permit = object()
        try:
            with anyio.fail_after(READ_WAIT_S):
                await read_capacity.acquire_on_behalf_of(permit)
        except TimeoutError:
            return _refusal("overloaded")
        call = service.download(request.path_params["avatar_id"], int(size))
        try:
            download = await tracked(call, read_capacity, permit)
        except AvatarError as exc:
            return _refusal(exc.code, reading=True)
        return Response(
            download.body, media_type="image/png", headers={"X-Content-Type-Options": "nosniff"}
        )

    async def transform(request: Request) -> Response:
        response = await admitted(request)
        response.headers["Cache-Control"] = "no-store"
        return response

    async def read(request: Request) -> Response:
        response = await fetched(request)
        response.headers["Cache-Control"] = "no-store"
        return response

    return [
        Route("/api/v2/avatars", transform, methods=["POST"]),
        Route("/api/v2/avatars/{avatar_id}/{size}", read, methods=["GET"]),
    ]
