"""Private REST target for the app-wide settings document. GET/PATCH only: reset is
a data operation (`data_api.py`) that shares the same read-only guard, and there is no DELETE."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import anyio
from anyio.to_thread import run_sync
from starlette.requests import ClientDisconnect, Request
from starlette.responses import Response
from starlette.routing import Route

from viki_agent.api_json import failure as _failure
from viki_agent.api_json import json_response as _json
from viki_agent.api_json import unique_object as _unique
from viki_agent.app.settings import SettingsConflict, SettingsError, SettingsService, to_view

MAX_SETTINGS_REQUESTS = 4
MAX_SETTINGS_BODY_BYTES = 16 << 10
SETTINGS_BODY_TIMEOUT_S = 5.0

logger = logging.getLogger(__name__)

_STATUS = {
    "invalid_settings": 400,
    "invalid_input": 400,
    "settings_too_large": 413,
    "read_only": 409,
    "stale_revision": 409,
    "command_conflict": 409,
    "busy": 503,
    "unavailable": 503,
    "corrupt": 503,
    "recovery_required": 503,
    "incompatible": 503,
}


def settings_routes(
    service: SettingsService,
    *,
    authorized: Callable[[Request], bool],
    origins: frozenset[str],
    workers: set[asyncio.Task[Any]],
) -> list[Route]:
    capacity = anyio.CapacityLimiter(MAX_SETTINGS_REQUESTS)

    async def endpoint(request: Request) -> Response:
        if not authorized(request):
            return _failure("unauthorized", "Can't authenticate. Open VIKI Agent again.", 401)
        origin = request.headers.get("origin")
        if (origin is not None and origin not in origins) or (
            request.method == "PATCH" and origin not in origins
        ):
            return _failure("forbidden_origin", "This window can't do that.", 403)
        permit = object()
        delegated = False
        try:
            capacity.acquire_on_behalf_of_nowait(permit)
        except anyio.WouldBlock:
            return _failure("overloaded", "Busy; try again in a moment.", 429)

        async def run(call: Callable[[], Any]) -> Any:
            # The blocking service call (validation, JSON (de)serialisation, the SQLite
            # transaction) never runs on the event loop: a competing writer must not stall
            # every other connection's frame pump while this request holds the write lock.
            nonlocal delegated
            worker = asyncio.create_task(run_sync(call))
            workers.add(worker)
            delegated = True

            def done(task: asyncio.Task[Any]) -> None:
                workers.discard(task)
                capacity.release_on_behalf_of(permit)
                # A disconnected caller cannot observe an exception. Retrieve it without
                # logging content; a later authenticated GET can recover the committed value.
                if not task.cancelled():
                    task.exception()

            worker.add_done_callback(done)
            return await asyncio.shield(worker)

        try:
            return await perform(request, run)
        except SettingsConflict as exc:
            return _json(
                {
                    "error": {
                        "code": exc.code,
                        "message": exc.message,
                        "current_revision": exc.current_revision,
                        "current": to_view(exc.current),
                    }
                },
                409,
            )
        except SettingsError as exc:
            status = _STATUS.get(exc.code, 503)
            body: dict[str, Any] = {"code": exc.code, "message": exc.message}
            if exc.fields:
                body["fields"] = exc.fields
            body.update(exc.extra)
            if status >= 500:
                # Storage failures arrive typed, so this is where the log learns of them.
                # Hard rule 8: the code is the whole record, never the settings content.
                logger.warning("settings refused: %s", exc.code)
            return _json({"error": body}, status)
        except ClientDisconnect:
            return _failure("disconnected", "The request was interrupted.", 400)
        finally:
            if not delegated:
                capacity.release_on_behalf_of(permit)

    async def perform(
        request: Request, run: Callable[[Callable[[], Any]], Awaitable[Any]]
    ) -> Response:
        if request.scope.get("query_string"):
            return _failure("invalid_input", "Check the settings request.", 400)
        if request.method in {"GET", "HEAD"}:
            return _json(to_view(await run(service.read)))
        if (
            request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            != "application/json"
        ):
            return _failure("invalid_representation", "Send the settings as JSON.", 415)
        if request.headers.get("content-encoding", "identity").lower() != "identity":
            return _failure("invalid_representation", "Send the settings uncompressed.", 415)
        length = request.headers.get("content-length")
        if length is not None:
            if not length.isascii() or not length.isdecimal() or len(length) > 10:
                return _failure("invalid_body", "The request was malformed.", 400)
            if int(length) > MAX_SETTINGS_BODY_BYTES:
                return _failure("too_large", "The request is too large.", 413)
        body = bytearray()
        try:
            with anyio.fail_after(SETTINGS_BODY_TIMEOUT_S):
                async for chunk in request.stream():
                    if len(chunk) > MAX_SETTINGS_BODY_BYTES - len(body):
                        return _failure("too_large", "The request is too large.", 413)
                    body.extend(chunk)
        except TimeoutError:
            return _failure("intake_timeout", "The request did not arrive in time.", 408)
        try:
            raw = json.loads(body.decode("utf-8"), object_pairs_hook=_unique)
        except ValueError, RecursionError:
            return _failure("invalid_body", "The request was malformed.", 400)
        if (
            not isinstance(raw, dict)
            or set(raw) != {"command_id", "expected_revision", "changes"}
            or not isinstance(raw["changes"], dict)
        ):
            return _failure("invalid_body", "The request was malformed.", 400)
        result, replayed = await run(
            lambda: service.patch(raw["command_id"], raw["expected_revision"], raw["changes"])
        )
        view = to_view(result)
        view["command_id"] = raw["command_id"]
        view["replayed"] = replayed
        return _json(view)

    return [Route("/api/v2/settings", endpoint, methods=["GET", "PATCH"])]
