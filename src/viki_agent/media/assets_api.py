"""Authenticated raw upload/read routes for app-owned image assets."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import unquote_to_bytes

import anyio
from anyio.to_thread import run_sync
from starlette.requests import ClientDisconnect, Request
from starlette.responses import Response, StreamingResponse
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

from viki_agent.api_json import failure as _failure
from viki_agent.api_json import json_response as _json
from viki_agent.media.assets import AssetDownload, AssetError, AssetService
from viki_agent.media.avatars import AVATAR_SOURCE_BYTES
from viki_agent.turn_input import (
    CLAUDE_MAX_ATTACHMENT_BYTES,
    CLAUDE_MAX_DIMENSION,
    CLAUDE_MAX_TURN_ATTACHMENT_BYTES,
    IMAGE_MEDIA_TYPES,
    MAX_ATTACHMENT_BYTES,
    MAX_ATTACHMENTS,
)

MAX_ASSET_REQUESTS = 2
READ_WAIT_S = 10.0  # below the client's 30 s request timeout
UPLOAD_TIMEOUT_S = 20.0
REMOVE_BODY_TIMEOUT_S = 5.0
MAX_ENCODED_NAME_BYTES = 2048
PURPOSE_HEADER = "x-viki-asset-purpose"
# The wire code stays "too_large" (the client maps only that code); only the message names the
# avatar's own, smaller cap.
_AVATAR_TOO_LARGE_MESSAGE = "File must be 5 MiB or smaller."

logger = logging.getLogger(__name__)

_ERRORS: dict[str, tuple[int, str]] = {
    "invalid_asset": (400, "Check the attachment you selected."),
    "invalid_name": (400, "Check the file name."),
    "invalid_body": (400, "Could not read the attachment."),
    "invalid_purpose": (400, "Check the image's purpose."),
    "empty": (400, "An empty file can't be attached."),
    "incomplete": (400, "The upload ended before it finished. Try again."),
    "too_large": (413, "File must be 20 MiB or smaller."),
    "decode_failed": (415, "Choose a PNG, JPEG, or WebP image."),
    "capacity": (429, "Attachment storage has reached its limit."),
    "in_use": (409, "Can't delete it: a draft or a sent message uses it."),
    "not_found": (404, "Attachment not found."),
    "missing": (404, "Attachment not found."),
    "expired": (410, "The attachment's local storage period has ended."),
    "worker_timeout": (503, "Checking the image took too long. Try again."),
    "worker_unavailable": (503, "Could not check the image. Try again."),
    "decoder_unavailable": (
        503,
        "The image-checking feature is unavailable. Quit VIKI and run `uv sync`.",
    ),
    "asset_changed": (409, "The attachment's details changed. Choose it again."),
    "asset_mismatch": (409, "The file you chose doesn't match the image you sent."),
    "corrupt": (503, "Could not read the attachment."),
    "protection": (503, "Could not safely save the attachment."),
    "unavailable": (503, "Could not save the attachment."),
    "busy": (503, "The save location is busy. Wait a moment, then try again."),
}


def _error(exc: AssetError) -> Response:
    status, message = _ERRORS.get(exc.code, (503, "Could not process the attachment."))
    if status >= 500:
        logger.warning("asset refused: %s", exc.code)
    return _failure(exc.code, message, status)


def _name(request: Request) -> str:
    raw = request.headers.get("x-file-name")
    if raw is None or len(raw.encode("latin-1", errors="ignore")) > MAX_ENCODED_NAME_BYTES:
        raise AssetError("invalid_name")
    try:
        name = unquote_to_bytes(raw).decode("utf-8")
    except UnicodeError, ValueError:
        raise AssetError("invalid_name") from None
    return name


def _length(request: Request, limit: int) -> int | None:
    raw = request.headers.get("content-length")
    if raw is None:
        return None
    if not raw.isascii() or not raw.isdecimal() or len(raw) > 10:
        raise AssetError("invalid_body")
    value = int(raw)
    if value > limit:
        raise AssetError("too_large")
    if value == 0:
        raise AssetError("empty")
    return value


def _overloaded() -> Response:
    return _failure(
        "overloaded", "Another image is being processed. Wait a moment, then try again.", 429
    )


@dataclass(eq=False)
class _MutationPermit:
    transferred: bool = False


class _AssetResponse(StreamingResponse):
    def __init__(self, download: AssetDownload, release: Callable[[], None]) -> None:
        self._release = release
        super().__init__(
            download.body,
            media_type=download.media_type,
            headers={
                "Cache-Control": "no-store",
                "Content-Disposition": "inline",
                "Content-Length": str(download.byte_size),
                "X-Content-Type-Options": "nosniff",
            },
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            # The body may never start if response.start blocks. Closing an entered iterator
            # also belongs to this response, even when the caller is cancelled again meanwhile.
            async def close() -> None:
                try:
                    close_body = getattr(self.body_iterator, "aclose", None)
                    if close_body is not None:
                        await close_body()
                finally:
                    self._release()

            closing = asyncio.create_task(close())
            cancelled: asyncio.CancelledError | None = None
            with anyio.CancelScope(shield=True):
                while not closing.done():
                    try:
                        await asyncio.shield(closing)
                    except asyncio.CancelledError as exc:
                        cancelled = exc
                closing.result()
            if cancelled is not None:
                raise cancelled


def asset_routes(
    service: AssetService,
    *,
    authorized: Callable[[Request], bool],
    origins: frozenset[str],
    workers: set[asyncio.Task[Any]],
    image_input: Callable[[], dict[str, str | None]] | None = None,
) -> list[Route]:
    capacity = anyio.CapacityLimiter(MAX_ASSET_REQUESTS)
    # Reads have their own slots and queue for them: a sent message fetches every preview at
    # once, and the client treats a refused read as a permanent failure.
    read_capacity = anyio.CapacityLimiter(MAX_ASSET_REQUESTS)

    def allowed(request: Request, *, mutate: bool) -> Response | None:
        if not authorized(request):
            return _failure("unauthorized", "Can't authenticate. Open VIKI Agent again.", 401)
        origin = request.headers.get("origin")
        if (origin is not None and origin not in origins) or (mutate and origin not in origins):
            return _failure("forbidden_origin", "This window can't do that.", 403)
        if request.scope.get("query_string", b""):
            return _failure("invalid_request", "Check the attachment you selected.", 400)
        return None

    async def tracked[T](call: Coroutine[Any, Any, T], permit: _MutationPermit) -> T:
        task = asyncio.create_task(call)
        permit.transferred = True
        workers.add(task)

        def done(completed: asyncio.Task[T]) -> None:
            workers.discard(task)
            # Cancellation can leave cleanup running after the request has ended.
            release(permit)
            if not completed.cancelled():
                completed.exception()

        task.add_done_callback(done)
        # A deadline re-delivers its cancel on every loop pass while we wait; awaiting the task
        # directly would forward each one into the service's cleanup and leak its reservation
        # and stage. Cancel it once; it stays in `workers` so shutdown still drains it.
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            task.cancel()
            raise

    def acquire() -> _MutationPermit | Response:
        permit = _MutationPermit()
        try:
            capacity.acquire_on_behalf_of_nowait(permit)
        except anyio.WouldBlock:
            return _overloaded()
        return permit

    def release(permit: _MutationPermit) -> None:
        capacity.release_on_behalf_of(permit)

    async def intake(
        request: Request, work: Callable[[_MutationPermit], Awaitable[Response]]
    ) -> Response:
        """An image body's shared gate: auth, Origin, identity encoding, a permit, the deadline."""
        refusal = allowed(request, mutate=True)
        if refusal is not None:
            return refusal
        if request.headers.get("content-encoding", "identity").lower() != "identity":
            return _failure("invalid_representation", "Send the image uncompressed.", 415)
        permit = acquire()
        if isinstance(permit, Response):
            return permit
        try:
            with anyio.fail_after(UPLOAD_TIMEOUT_S):
                return await work(permit)
        except TimeoutError:
            return _failure("intake_timeout", "The upload didn't finish in time.", 408)
        except ClientDisconnect:
            return _failure("disconnected", "The upload ended before it finished.", 400)
        except AssetError as exc:
            return _error(exc)
        finally:
            if not permit.transferred:
                release(permit)

    async def upload(request: Request) -> Response:
        async def work(permit: _MutationPermit) -> Response:
            # No purpose is a chat attachment; exactly one `avatar` is an avatar's source image.
            purposes = request.headers.getlist(PURPOSE_HEADER)
            if purposes and purposes != ["avatar"]:
                raise AssetError("invalid_purpose")
            # A chat attachment passes None: the service resolves its cap when called.
            max_bytes = AVATAR_SOURCE_BYTES if purposes else None
            name = _name(request)
            try:
                length = _length(request, max_bytes or MAX_ATTACHMENT_BYTES)
                asset = await tracked(
                    service.upload(
                        request.stream(), name=name, declared_bytes=length, max_bytes=max_bytes
                    ),
                    permit,
                )
            except AssetError as exc:
                # The declared and the streamed caps both refuse as "too_large"; the code stays
                # the same, and only the message names the avatar's own, smaller cap.
                if purposes and exc.code == "too_large":
                    return _failure("too_large", _AVATAR_TOO_LARGE_MESSAGE, 413)
                raise
            return _json(asdict(asset), 201)

        return await intake(request, work)

    async def relink(request: Request) -> Response:
        async def work(permit: _MutationPermit) -> Response:
            await tracked(
                service.relink(
                    request.path_params.get("asset_id", ""),
                    request.stream(),
                    declared_bytes=_length(request, MAX_ATTACHMENT_BYTES),
                ),
                permit,
            )
            return Response(status_code=204)

        return await intake(request, work)

    async def read(request: Request) -> Response:
        refusal = allowed(request, mutate=False)
        if refusal is not None:
            return refusal
        permit = object()
        try:
            with anyio.fail_after(READ_WAIT_S):
                await read_capacity.acquire_on_behalf_of(permit)
        except TimeoutError:
            return _overloaded()
        released = False

        def release_read() -> None:
            nonlocal released
            if not released:
                released = True
                read_capacity.release_on_behalf_of(permit)

        task = asyncio.create_task(service.download(request.path_params.get("asset_id", "")))
        workers.add(task)

        def done(completed: asyncio.Task[Any]) -> None:
            workers.discard(completed)
            if not completed.cancelled():
                completed.exception()

        task.add_done_callback(done)

        async def drain() -> None:
            try:
                await task
            except BaseException:
                pass
            finally:
                release_read()

        try:
            download = await asyncio.shield(task)
        except AssetError as exc:
            release_read()
            return _error(exc)
        except BaseException:
            if task.done():
                release_read()
            else:
                asyncio.create_task(drain())
                # `task` remains in `workers` until the service has stopped its read/decryption
                # work. This small waiter only releases the permit after that tracked task drains.
            raise

        return _AssetResponse(download, release_read)

    async def remove(request: Request) -> Response:
        refusal = allowed(request, mutate=True)
        if refusal is not None:
            return refusal
        if request.headers.get("content-encoding", "identity").lower() != "identity":
            return _failure("invalid_representation", "Send an empty request.", 415)
        length = request.headers.get("content-length")
        if length is not None and length != "0":
            return _failure("invalid_body", "Send an empty request.", 400)
        permit = acquire()
        if isinstance(permit, Response):
            return permit
        try:
            with anyio.fail_after(REMOVE_BODY_TIMEOUT_S):
                async for chunk in request.stream():
                    if chunk:
                        return _failure("invalid_body", "Send an empty request.", 400)
            removed = await tracked(service.remove(request.path_params.get("asset_id", "")), permit)
            return Response(status_code=204 if removed else 404)
        except TimeoutError:
            return _failure("intake_timeout", "The delete request didn't finish in time.", 408)
        except ClientDisconnect:
            return _failure("disconnected", "Could not confirm the delete.", 400)
        except AssetError as exc:
            return _error(exc)
        finally:
            if not permit.transferred:
                release(permit)

    async def attachment_capabilities(request: Request) -> Response:
        refusal = allowed(request, mutate=False)
        if refusal is not None:
            return refusal
        # The one caller reads readiness.version, with no SQLite or ACL I/O; keep it off the loop.
        native = (
            await run_sync(image_input)
            if image_input is not None
            else {"status": "unverified", "reason": "evidence_unavailable"}
        )
        return _json(
            {
                "provider": "claude",
                "media_types": sorted(IMAGE_MEDIA_TYPES),
                "max_files": MAX_ATTACHMENTS,
                "max_file_bytes": CLAUDE_MAX_ATTACHMENT_BYTES,
                "max_turn_bytes": CLAUDE_MAX_TURN_ATTACHMENT_BYTES,
                "max_dimension": CLAUDE_MAX_DIMENSION,
                "image_input": native,
            }
        )

    return [
        Route("/api/v2/assets", upload, methods=["POST"]),
        Route("/api/v2/attachment-capabilities", attachment_capabilities, methods=["GET"]),
        Route("/api/v2/assets/{asset_id}", read, methods=["GET"]),
        Route("/api/v2/assets/{asset_id}/remove", remove, methods=["POST"]),
        Route("/api/v2/assets/{asset_id}/relink", relink, methods=["POST"]),
    ]
