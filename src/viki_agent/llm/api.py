"""Authenticated model-connection setup. Only an explicit Test sends a fixed test prompt."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from dataclasses import asdict
from typing import Any

from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from viki_agent.api_json import failure, json_response
from viki_agent.app.settings import SettingsError
from viki_agent.llm.connections import (
    MAX_CONNECTIONS,
    ConnectionError,
    Connections,
    parse_connection,
)
from viki_agent.llm.gateway import INTAKE_S, Gateway, OwnedResponse, finish_cleanup, read_body


async def _connected[T](request: Request, work: Coroutine[Any, Any, T]) -> T:
    """The body is consumed; the only remaining inbound event of interest is disconnect."""

    async def disconnected() -> None:
        while (await request.receive())["type"] != "http.disconnect":
            pass

    operation = asyncio.create_task(work)
    watcher = asyncio.create_task(disconnected())
    try:
        done, _ = await asyncio.wait((operation, watcher), return_when=asyncio.FIRST_COMPLETED)
        if watcher in done:
            raise ConnectionError("request_cancelled")
        return await operation
    finally:
        operation.cancel()
        watcher.cancel()

        async def close() -> None:
            await asyncio.gather(operation, watcher, return_exceptions=True)

        await finish_cleanup(close())


def connection_routes(
    connections: Connections,
    gateway: Gateway,
    *,
    authorized: Callable[[Request], bool],
    origins: frozenset[str],
) -> list[Route]:
    testing = asyncio.Semaphore(2)

    def listing() -> dict[str, object]:
        revision, rows = connections.read()
        default = connections.settings.read().effective.get("chat.default_model")
        return {
            "revision": revision,
            "connections": [{**row.public(), "is_default": row.ref == default} for row in rows],
            "default_model": default,
        }

    async def execute(request: Request) -> Response:
        if not authorized(request):
            return failure("unauthorized", "Open VIKI Agent again.", 401)
        if request.method != "GET" and request.headers.get("origin") not in origins:
            return failure("forbidden_origin", "This window can't do that.", 403)
        if request.scope.get("query_string"):
            return failure("invalid_connection", "Check the connection settings.", 400)
        try:
            if request.method == "GET":
                return json_response(await asyncio.to_thread(listing))
            async with asyncio.timeout(INTAKE_S):
                body = await read_body(request, 16384)
            if request.method == "POST" and request.url.path.endswith("/models"):
                if set(body) != {"connection"} or not isinstance(body["connection"], dict):
                    raise ConnectionError()
                values = body["connection"]
                connection = parse_connection(
                    {
                        **values,
                        "model": values.get("model") or "discovery",
                        "name": values.get("name") or "Endpoint",
                    }
                )
                if testing.locked():
                    raise ConnectionError("busy")
                async with testing:
                    models = await _connected(request, gateway.models(connection))
                return json_response({"ok": True, "models": models})
            if request.method == "POST" and request.url.path.endswith("/test"):
                if set(body) != {"connection"}:
                    raise ConnectionError()
                connection = parse_connection(body["connection"])
                if testing.locked():
                    raise ConnectionError("busy")
                try:
                    async with asyncio.timeout(25), testing:
                        models = await _connected(request, gateway.test(connection))
                except TimeoutError:
                    raise ConnectionError("endpoint_unreachable") from None
                return json_response({"ok": True, "models": models})
            revision = body.get("expected_revision")
            if type(revision) is not int or revision < 0:
                raise ConnectionError()
            current, rows = await asyncio.to_thread(connections.read)
            if revision != current:
                raise ConnectionError("stale_revision")
            if request.method == "PATCH":
                if set(body) != {"expected_revision", "api_key"}:
                    raise ConnectionError()
                connection_id = request.path_params["connection_id"]
                existing = next((row for row in rows if row.id == connection_id), None)
                if existing is None:
                    raise ConnectionError("connection_missing")
                values = asdict(existing)
                values.pop("id")
                renewed = parse_connection(
                    {**values, "api_key": body["api_key"]}, connection_id=existing.id
                )
                await asyncio.to_thread(
                    connections.write,
                    revision,
                    [renewed if row.id == connection_id else row for row in rows],
                )
            elif request.method == "DELETE":
                if set(body) != {"expected_revision"}:
                    raise ConnectionError()
                connection_id = request.path_params["connection_id"]
                removed = next((row for row in rows if row.id == connection_id), None)
                if removed is None:
                    raise ConnectionError("connection_missing")
                default = connections.settings.read().effective.get("chat.default_model")
                await asyncio.to_thread(
                    connections.write,
                    revision,
                    [row for row in rows if row.id != connection_id],
                    clear_default=default == removed.ref,
                )
            else:
                if set(body) != {"expected_revision", "connection"}:
                    raise ConnectionError()
                if len(rows) >= MAX_CONNECTIONS:
                    raise ConnectionError("connection_limit")
                connection = parse_connection(body["connection"])
                await asyncio.to_thread(connections.write, revision, [*rows, connection])
            return json_response(await asyncio.to_thread(listing))
        except (ConnectionError, SettingsError, TimeoutError) as exc:
            code = "endpoint_unreachable" if isinstance(exc, TimeoutError) else exc.code
            status = 409 if code in ("stale_revision", "read_only", "connection_limit") else 400
            if code in ("connections_unavailable", "unavailable", "endpoint_unreachable"):
                status = 503
            return failure(code, "Couldn't complete the model connection request.", status)

    async def handler(request: Request) -> Response:
        if not authorized(request):
            return failure("unauthorized", "Open VIKI Agent again.", 401)
        if request.method == "GET":
            return await execute(request)
        try:
            release = gateway.admit()
        except ConnectionError as exc:
            return failure(exc.code, "Model connection is busy.", 503)
        transferred = False
        try:
            response = await execute(request)
            transferred = True
            return OwnedResponse(response, release)
        finally:
            if not transferred:
                release()

    return [
        Route("/api/v2/llm/connections", handler, methods=["GET", "POST"]),
        Route("/api/v2/llm/connections/test", handler, methods=["POST"]),
        Route("/api/v2/llm/connections/models", handler, methods=["POST"]),
        Route("/api/v2/llm/connections/{connection_id}", handler, methods=["DELETE", "PATCH"]),
    ]
