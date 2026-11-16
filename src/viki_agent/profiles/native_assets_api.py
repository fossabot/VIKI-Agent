"""Private bounded metadata inspection; no caller paths and no native process launch."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from dataclasses import asdict
from typing import Any

import anyio
from anyio.to_thread import run_sync
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from viki_agent.api_json import failure, json_response
from viki_agent.profiles.native_assets import NativeAssetError, NativeAssetInspector
from viki_agent.storage import StateError
from viki_agent.workspaces import WorkspaceError


def _sources(value: object) -> list[dict[str, str]]:
    if not isinstance(value, list) or len(value) > 2:
        raise NativeAssetError("invalid_sources")
    seen = set()
    for source in value:
        if not isinstance(source, dict) or set(source) != {"source_id", "provider", "scope"}:
            raise NativeAssetError("invalid_sources")
        source_id = source["source_id"]
        if (
            not isinstance(source_id, str)
            or len(source_id) > 80
            or source_id in seen
            or source["provider"] != "claude"
        ):
            raise NativeAssetError("invalid_sources")
        if source["scope"] == "user":
            valid = source_id == "claude:user"
        elif source["scope"] == "workspace" and source_id.startswith("claude:workspace:"):
            identity = source_id.removeprefix("claude:workspace:")
            try:
                valid = str(uuid.UUID(identity)) == identity
            except ValueError:
                valid = False
        else:
            valid = False
        if not valid:
            raise NativeAssetError("invalid_sources")
        seen.add(source_id)
    return value


def native_asset_routes(
    inspector: NativeAssetInspector,
    sources: Callable[[], list[dict[str, str]]],
    *,
    authorized: Callable[[Request], bool],
    origins: frozenset[str],
    workers: set[asyncio.Task[Any]],
) -> list[Route]:
    slots = anyio.CapacityLimiter(4)

    async def perform(request: Request) -> Response:
        query = request.query_params
        if (
            len(request.scope.get("query_string", b"")) > 512
            or set(query) - {"source_id", "kind"}
            or any(len(query.getlist(key)) != 1 for key in query)
            or query.get("kind", "all") not in {"agent", "skill", "all"}
        ):
            return failure("invalid_input", "The native asset query is invalid.", 400)
        if "source_id" not in query:
            if query:
                return failure("invalid_input", "Choose a native asset source.", 400)
            response = json_response({"sources": _sources(await run_sync(sources))})
            return (
                response
                if len(response.body) <= 2 << 20
                else failure("too_large", "The catalog is too large.", 413)
            )
        result = asdict(await run_sync(lambda: inspector.inspect(query["source_id"])))
        if query.get("kind", "all") != "all":
            result["assets"] = [item for item in result["assets"] if item["kind"] == query["kind"]]
        response = json_response(result)
        return (
            response
            if len(response.body) <= 2 << 20
            else failure("too_large", "The catalog is too large.", 413)
        )

    async def endpoint(request: Request) -> Response:
        async def admitted() -> Response:
            permit = object()
            try:
                slots.acquire_on_behalf_of_nowait(permit)
            except anyio.WouldBlock:
                return failure("overloaded", "Native asset inspection is busy.", 429)
            task = asyncio.create_task(perform(request))
            workers.add(task)

            def done(future: asyncio.Task[Response]) -> None:
                workers.discard(future)
                slots.release_on_behalf_of(permit)
                if not future.cancelled():
                    future.exception()

            task.add_done_callback(done)
            try:
                return await asyncio.shield(task)
            except NativeAssetError as error:
                return failure(
                    error.code,
                    "This native asset source is unavailable.",
                    400 if error.code == "invalid_source" else 409,
                )
            except WorkspaceError, StateError, OSError:
                return failure("unavailable", "Native assets are unavailable.", 503)

        if not authorized(request):
            response = failure("unauthorized", "Authentication is required.", 401)
        elif request.headers.get("origin") not in {None, *origins}:
            response = failure("forbidden_origin", "This origin is not allowed.", 403)
        else:
            response = await admitted()
        response.headers["Cache-Control"] = "no-store"
        return response

    return [Route("/api/v2/native-assets", endpoint, methods=["GET"])]
