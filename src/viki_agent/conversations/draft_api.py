"""Bounded, authenticated editor-draft routes. None of these operations executes a command."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
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
from viki_agent.conversations.drafts import Draft, DraftConflict, DraftError, DraftService
from viki_agent.conversations.turn_parts import (
    EXCERPTS_PER_TURN,
    ExcerptPart,
    parse_excerpt_part,
    part_data,
)
from viki_agent.turn_input import Attachment, TurnInputError

MAX_DRAFT_REQUESTS = 4
MAX_DRAFT_BODY_BYTES = (6 << 20) + 1024
DRAFT_BODY_TIMEOUT_S = 5.0

logger = logging.getLogger(__name__)

_STATUS = {
    "invalid_key": 400,
    "invalid_revision": 400,
    "invalid_text": 400,
    "invalid_attachment": 400,
    "invalid_page": 400,
    "too_large": 413,
    "capacity": 429,
    "conflict": 409,
    "attachment_unavailable": 409,
}


def _draft_json(draft: Draft) -> Response:
    """Keep the text-only v1 response byte-for-byte shaped for older windows."""
    body = asdict(draft)
    for name in ("attachments", "parts"):
        if not body[name]:
            del body[name]
    if "parts" in body:
        # The wire shape a client can send back to PUT, `kind` included.
        body["parts"] = [part_data(part) for part in draft.parts]
    return _json(body)


def draft_routes(
    service: DraftService,
    *,
    authorized: Callable[[Request], bool],
    origins: frozenset[str],
    workers: set[asyncio.Task[Any]],
) -> list[Route]:
    # Admission covers intake AND blocking work, with no queue of waiting requests. Once
    # storage starts, the shielded worker owns the permit. Cancelling the HTTP coroutine must
    # not free a slot while its thread is still running. The app drains workers at shutdown.
    capacity = anyio.CapacityLimiter(MAX_DRAFT_REQUESTS)

    async def endpoint(request: Request) -> Response:
        if not authorized(request):
            return _failure("unauthorized", "Can't authenticate. Open VIKI Agent again.", 401)
        origin = request.headers.get("origin")
        if (origin is not None and origin not in origins) or (
            request.method == "PUT" and origin not in origins
        ):
            return _failure("forbidden_origin", "This window can't do that.", 403)
        permit = object()
        delegated = False
        try:
            capacity.acquire_on_behalf_of_nowait(permit)
        except anyio.WouldBlock:
            return _failure(
                "overloaded", "Busy processing the draft. Wait a moment, then try again.", 429
            )

        async def run(call: Callable[[], Any]) -> Any:
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
        except DraftConflict as exc:
            return _json(
                {
                    "error": {
                        "code": exc.code,
                        "message": exc.message,
                        "current_revision": exc.current_revision,
                    }
                },
                409,
            )
        except DraftError as exc:
            status = _STATUS.get(exc.code, 503)
            if status >= 500:
                # Storage and DPAPI failures arrive typed, so this is where the log learns of
                # them. Hard rule 8: the code is the whole record, never the draft text.
                logger.warning("draft refused: %s", exc.code)
            return _failure(exc.code, exc.message, status)
        except ClientDisconnect:
            return _failure("disconnected", "Receiving the draft was interrupted.", 400)
        finally:
            if not delegated:
                capacity.release_on_behalf_of(permit)

    async def perform(
        request: Request, run: Callable[[Callable[[], Any]], Awaitable[Any]]
    ) -> Response:
        key = request.path_params.get("key")
        if len(request.scope.get("query_string", b"")) > 256:
            return _failure("invalid_page", "The draft list query is too long.", 400)
        query = request.query_params
        if key is None:
            if set(query) - {"after", "limit"} or any(
                len(query.getlist(name)) != 1 for name in query
            ):
                return _failure("invalid_page", "Check the draft list query.", 400)
            raw_limit = query.get("limit", "50")
            if not raw_limit.isascii() or not raw_limit.isdecimal() or len(raw_limit) > 3:
                return _failure("invalid_page", "Check the draft list query.", 400)
            # Keep query parsing bounded even before it reaches the service's validation.
            after = query.get("after")
            if after is not None and len(after) > 36:
                return _failure("invalid_page", "Check the draft list query.", 400)
            page = await run(lambda: service.list(after=after, limit=int(raw_limit)))
            return _json(asdict(page))
        if query:
            return _failure("invalid_page", "Check the draft request.", 400)
        # Starlette adds HEAD to GET routes. It must never enter body intake or a save.
        if request.method in {"GET", "HEAD"}:
            draft = await run(lambda: service.get(key))
            return _draft_json(draft)
        if (
            request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            != "application/json"
        ):
            return _failure("invalid_representation", "Send the draft as JSON.", 415)
        if request.headers.get("content-encoding", "identity").lower() != "identity":
            return _failure("invalid_representation", "Send the draft uncompressed.", 415)
        raw_length = request.headers.get("content-length")
        if raw_length is not None:
            if not raw_length.isascii() or not raw_length.isdecimal() or len(raw_length) > 10:
                return _failure("invalid_body", "Check the submitted draft.", 400)
            if int(raw_length) > MAX_DRAFT_BODY_BYTES:
                return _failure("too_large", "The draft sent is too large.", 413)
        body = bytearray()
        try:
            with anyio.fail_after(DRAFT_BODY_TIMEOUT_S):
                async for chunk in request.stream():
                    if len(chunk) > MAX_DRAFT_BODY_BYTES - len(body):
                        return _failure("too_large", "The draft sent is too large.", 413)
                    body.extend(chunk)
        except TimeoutError:
            return _failure("intake_timeout", "Receiving the draft took too long.", 408)
        try:
            raw = json.loads(body.decode("utf-8"), object_pairs_hook=_unique)
        except ValueError, RecursionError:
            return _failure("invalid_body", "Check the submitted draft.", 400)
        if (
            not isinstance(raw, dict)
            or not {"expected_revision", "text"} <= set(raw)
            or set(raw) - {"expected_revision", "text", "attachments", "parts"}
        ):
            return _failure("invalid_body", "Check the submitted draft.", 400)
        attachments: tuple[Attachment, ...] | None = None
        if "attachments" in raw:
            values = raw["attachments"]
            if (
                not isinstance(values, list)
                or len(values) > 10
                or not all(isinstance(value, dict) for value in values)
            ):
                return _failure("invalid_body", "Check the submitted draft.", 400)
            try:
                attachments = tuple(Attachment(**value) for value in values)
            except TypeError, TurnInputError:
                return _failure("invalid_body", "Check the submitted draft.", 400)
        # An omitted `parts` stays None so the legacy text-only save can still clear excerpts;
        # a sent `[]` is a typed save. A stale excerpt is refused at acceptance, not here.
        parts: tuple[ExcerptPart, ...] | None = None
        if "parts" in raw:
            values = raw["parts"]
            if not isinstance(values, list) or len(values) > EXCERPTS_PER_TURN:
                return _failure("invalid_body", "Check the submitted draft.", 400)
            try:
                parts = tuple(parse_excerpt_part(value) for value in values)
            except TurnInputError:
                return _failure("invalid_body", "Check the submitted draft.", 400)
        draft = await run(
            lambda: (
                service.save(key, raw["expected_revision"], raw["text"])
                if attachments is None and parts is None
                else service.save(key, raw["expected_revision"], raw["text"], attachments, parts)
            )
        )
        return _draft_json(draft)

    return [
        Route("/api/v2/drafts", endpoint, methods=["GET"]),
        Route("/api/v2/drafts/{key}", endpoint, methods=["GET", "PUT"]),
    ]
