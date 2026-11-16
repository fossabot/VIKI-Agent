"""The chat preferences route: the model and effort a chat's next spawn uses.

GET answers `AppState.prefs` for a chat id: its stored row, else the defaults a chat with no row
inherits. It writes nothing. PATCH stores a model, an effort or both for a new chat only: a chat
with an open actor, a transcript or a profile binding answers `session_exists`. It merges into
the row an earlier PATCH wrote, and a `mode` key is refused. Neither starts a process.

Both sit behind the Bearer token and the Origin check, and a PATCH must name its Origin; four
requests run at a time. A refusal never echoes the body, and the route logs nothing.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Callable
from typing import Any, Protocol

import anyio
from anyio.to_thread import run_sync
from starlette.requests import ClientDisconnect, Request
from starlette.responses import Response
from starlette.routing import Route

from viki_agent.api_json import failure, json_response, unique_object
from viki_agent.state import AppState, Prefs
from viki_agent.storage import StateError

MAX_PREFS_REQUESTS = 4
MAX_BODY_BYTES = 4096
INTAKE_TIMEOUT_S = 5
FIELDS = frozenset({"model", "effort"})  # a `mode` key is refused
INVALID = "The chat preferences request is invalid."
EXISTS = "This chat already exists."


class _Sessions(Protocol):
    def get(self, session_id: str) -> object | None: ...


def _canonical_uuid(value: str) -> bool:
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False


def _answer(prefs: Prefs) -> Response:
    return json_response({"model": prefs.model, "effort": prefs.effort})


def session_prefs_routes(
    state: AppState,
    sessions: _Sessions,
    exists: Callable[[str], bool],
    *,
    authorized: Callable[[Request], bool],
    origins: frozenset[str],
    workers: set[asyncio.Task[Any]],
) -> list[Route]:
    slots = anyio.CapacityLimiter(MAX_PREFS_REQUESTS)

    async def patch(request: Request, session_id: str) -> Response:
        if request.headers.get("content-type", "").split(";", 1)[0].strip() != "application/json":
            return failure("invalid_representation", "Send the preferences as JSON.", 415)
        if request.headers.get("content-encoding", "identity") != "identity":
            return failure("invalid_representation", "Send the preferences uncompressed.", 415)
        body = bytearray()
        try:
            with anyio.fail_after(INTAKE_TIMEOUT_S):
                async for chunk in request.stream():
                    if len(chunk) > MAX_BODY_BYTES - len(body):
                        return failure("too_large", "The preferences are too large.", 413)
                    body.extend(chunk)
        except TimeoutError:
            return failure("intake_timeout", "The preferences did not arrive in time.", 408)
        except ClientDisconnect:
            return failure("disconnected", "The request was interrupted.", 400)
        try:
            changes = json.loads(body.decode("utf-8"), object_pairs_hook=unique_object)
        except ValueError, RecursionError:
            changes = None
        if (
            not isinstance(changes, dict)
            or not changes
            or not set(changes) <= FIELDS
            or not all(isinstance(value, str) for value in changes.values())
        ):
            return failure("invalid_input", INVALID, 400)

        def taken() -> bool:
            return exists(session_id) or state.store.session_origin(session_id) == "profile"

        if await run_sync(taken):
            return failure("session_exists", EXISTS, 409)
        # No await from the actor check through the commit, as in _actor_for's re-check before
        # manager.create(): a session.open either made its actor first (409) or creates it after
        # this commit and its register() merges into the row the start then reads.
        if sessions.get(session_id) is not None:
            return failure("session_exists", EXISTS, 409)
        return _answer(state.remember(session_id, new_session=True, **changes))

    async def perform(request: Request, session_id: str) -> Response:
        try:
            if request.method == "PATCH":
                return await patch(request, session_id)
            return _answer(await run_sync(state.prefs, session_id))
        except StateError as exc:
            if exc.code == "invalid_preferences":
                return failure("invalid_input", INVALID, 400)
            return failure("unavailable", "The chat preferences are unavailable.", 503)

    async def admitted(request: Request, session_id: str) -> Response:
        permit = object()
        try:
            slots.acquire_on_behalf_of_nowait(permit)
        except anyio.WouldBlock:
            return failure("overloaded", "The chat preferences are busy.", 429)
        task = asyncio.create_task(perform(request, session_id))
        workers.add(task)

        def done(future: asyncio.Task[Response]) -> None:
            workers.discard(future)
            slots.release_on_behalf_of(permit)
            if not future.cancelled():
                future.exception()

        task.add_done_callback(done)
        # A caller that goes away does not cancel the request's work or free its permit early.
        return await asyncio.shield(task)

    async def endpoint(request: Request) -> Response:
        session_id = request.path_params["session_id"]
        allowed = origins if request.method == "PATCH" else {None, *origins}
        if not authorized(request):
            response = failure("unauthorized", "Authentication is required.", 401)
        elif request.headers.get("origin") not in allowed:
            response = failure("forbidden_origin", "This origin is not allowed.", 403)
        elif request.scope.get("query_string") or not _canonical_uuid(session_id):
            response = failure("invalid_input", INVALID, 400)
        else:
            response = await admitted(request, session_id)
        response.headers["Cache-Control"] = "no-store"
        return response

    return [Route("/api/v2/sessions/{session_id}/prefs", endpoint, methods=["GET", "PATCH"])]
