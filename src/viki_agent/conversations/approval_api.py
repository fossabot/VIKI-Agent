"""Private REST recording, lookup and recovery routes for durable user choices.

Authorization and Origin are decided before a single body byte is read. Once `record` has
committed, a POST answers with the receipt whatever happens afterwards, so a 4xx or 5xx from
this route always means nothing was recorded under that ID.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Callable
from dataclasses import asdict
from typing import Any

import anyio
from starlette.requests import ClientDisconnect, Request
from starlette.responses import Response
from starlette.routing import Route

from viki_agent.api_json import failure as _failure
from viki_agent.api_json import json_response as _json
from viki_agent.api_json import unique_object as _unique
from viki_agent.conversations.approval_runtime import ApprovalDispatchRejected, ApprovalRunner
from viki_agent.conversations.approvals import (
    ApprovalError,
    ChoiceInput,
    NativeRequest,
    choice_from_json,
    choice_to_json,
)

MAX_APPROVAL_REQUESTS = 4
MAX_APPROVAL_BODY_BYTES = 65_536 + 1_024  # the choice JSON's own limit, plus its framing
APPROVAL_BODY_TIMEOUT_S = 5.0

logger = logging.getLogger(__name__)

_FAILURES = {
    "no_target": (404, "This session isn't open. Open the conversation, then choose."),
    "stale_generation": (
        409,
        "The session has been updated. Check the conversation, then choose.",
    ),
    "stale_request": (409, "This confirmation has already ended."),
    "closed": (409, "This session is closed."),
    "closing": (503, "Can't record the choice while shutting down."),
    "overloaded": (429, "Busy processing choice records. Wait a moment, then try again."),
}
# ApprovalService._access passes StateError codes through, so `busy`, `incompatible` and
# `recovery_required` reach this table as well as the service's own codes. `busy` is a
# retryable conflict; everything unlisted answers 503 with the service's fixed safe message.
_STATUS = {
    "invalid_input": 400,
    "invalid_choice": 400,
    "invalid_id": 400,
    "invalid_page": 400,
    "not_found": 404,
    "conflict": 409,
    "already_decided": 409,
    "busy": 409,
    "too_large": 413,
    "capacity": 429,
}
_BODY_KEYS = {"choice_id", "session_id", "generation", "tool_use_id", "native_request", "choice"}


def _uuid(value: Any) -> bool:
    if not isinstance(value, str) or len(value) != 36:
        return False
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False


def approval_routes(
    runner: ApprovalRunner,
    *,
    authorized: Callable[[Request], bool],
    origins: frozenset[str],
    workers: set[asyncio.Task[Any]],
) -> list[Route]:
    capacity = anyio.CapacityLimiter(MAX_APPROVAL_REQUESTS)

    async def endpoint(request: Request) -> Response:
        if not authorized(request):
            return _failure("unauthorized", "Can't authenticate. Open VIKI Agent again.", 401)
        origin = request.headers.get("origin")
        if (origin is not None and origin not in origins) or (
            request.method == "POST" and origin not in origins
        ):
            return _failure("forbidden_origin", "This window can't do that.", 403)
        permit = object()
        try:
            capacity.acquire_on_behalf_of_nowait(permit)
        except anyio.WouldBlock:
            return _failure("overloaded", "Busy processing choice records. Wait a moment.", 429)
        # This worker owns the intake/read slot until completion, even after HTTP disconnect.
        # The delivery attempt has its own bounded lifetime inside ApprovalRunner.
        worker = asyncio.create_task(perform(request))
        workers.add(worker)

        def done(task: asyncio.Task[Any]) -> None:
            workers.discard(task)
            capacity.release_on_behalf_of(permit)
            if not task.cancelled():
                task.exception()

        worker.add_done_callback(done)
        try:
            return await asyncio.shield(worker)
        except ApprovalDispatchRejected as exc:
            status, message = _FAILURES.get(
                exc.code, (503, "Couldn't finish recording the choice.")
            )
            return _failure(exc.code, message, status)
        except ApprovalError as exc:
            status = _STATUS.get(exc.code, 503)
            if status >= 500:
                # Every real storage or DPAPI failure arrives typed, so this is where the log
                # learns of it. Hard rule 8: the code is the whole record. A 4xx is the user's
                # own answer being refused and the browser already names it.
                logger.warning("approval refused: %s", exc.code)
            return _failure(exc.code, exc.message, status)
        except ClientDisconnect:
            return _failure("disconnected", "Receiving the choice was interrupted.", 400)
        except Exception as exc:
            # Hard rule 8: the exception's type name is the whole record. Never the choice, the
            # card, a storage message or a token.
            logger.warning("approval request failed: %s", type(exc).__name__)
            return _failure("unavailable", "Couldn't read or write the choice record.", 503)

    async def perform(request: Request) -> Response:
        raw_query = request.scope.get("query_string", b"")
        # A POST never carries a query string at all (checked below with the same code for a
        # short one); only a read route answers the length bound with `invalid_page`.
        if len(raw_query) > 256 and request.method != "POST":
            return _failure("invalid_page", "Check the paging for the choice list.", 400)
        params = request.path_params
        if "session_id" in params:
            # The browser's lookup before it allocates a choice ID: metadata only, and it needs
            # no managed actor, so a reloaded page can ask about a card it has not opened yet.
            session_id = params["session_id"]
            query = request.query_params
            runtime_id, request_id = query.get("runtime_id"), query.get("request_id")
            if (
                not _uuid(session_id)
                or set(query) != {"runtime_id", "request_id"}
                or any(len(query.getlist(key)) != 1 for key in query)
                or not _uuid(runtime_id)
                or not _uuid(request_id)
            ):
                return _failure("invalid_input", "Check the choice record's details.", 400)
            summary = await runner.find_request(
                session_id, query["runtime_id"], query["request_id"]
            )
            return _json(asdict(summary))
        if "choice_id" in params:
            choice_id = params["choice_id"]
            if raw_query or not _uuid(choice_id):
                return _failure("invalid_id", "Check the choice record's details.", 400)
            if not request.url.path.endswith("/choice"):
                # The metadata receipt never decrypts, so an unreadable body still has a state.
                return _json(asdict(await runner.summary(choice_id)))
            record = await runner.get(choice_id)
            body = asdict(record)
            body["choice"] = choice_to_json(record.choice)  # asdict drops the choice's kind
            return _json(body)
        if request.method in {"GET", "HEAD"}:
            query = request.query_params
            limit = query.get("limit", "50")
            after = query.get("after")
            if (
                set(query) - {"after", "limit"}
                or any(len(query.getlist(key)) != 1 for key in query)
                or not limit.isascii()
                or not limit.isdecimal()
                or len(limit) > 3
                or (after is not None and not _uuid(after))
            ):
                return _failure("invalid_page", "Check the paging for the choice list.", 400)
            return _json(asdict(await runner.list(after=after, limit=int(limit))))
        if raw_query:
            return _failure("invalid_input", "Check the choice.", 400)
        if (
            request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            != "application/json"
            or request.headers.get("content-encoding", "identity").lower() != "identity"
        ):
            return _failure("invalid_representation", "Send the choice as uncompressed JSON.", 415)
        length = request.headers.get("content-length")
        if length is not None:
            if not length.isascii() or not length.isdecimal() or len(length) > 10:
                return _failure("invalid_input", "Check the submitted choice.", 400)
            if int(length) > MAX_APPROVAL_BODY_BYTES:
                return _failure("too_large", "The choice data sent is too large.", 413)
        body = bytearray()
        try:
            with anyio.fail_after(APPROVAL_BODY_TIMEOUT_S):
                async for chunk in request.stream():
                    if len(chunk) > MAX_APPROVAL_BODY_BYTES - len(body):
                        return _failure("too_large", "The choice data sent is too large.", 413)
                    body.extend(chunk)
        except TimeoutError:
            return _failure("intake_timeout", "Receiving the choice took too long.", 408)
        try:
            value = json.loads(body.decode("utf-8"), object_pairs_hook=_unique)
        except ValueError, RecursionError:
            value = None
        native = value.get("native_request") if isinstance(value, dict) else None
        if (
            not isinstance(value, dict)
            or set(value) != _BODY_KEYS
            or not all(_uuid(value[key]) for key in ("choice_id", "session_id", "generation"))
            or not isinstance(value["tool_use_id"], str)
            or not isinstance(native, dict)
            or set(native) != {"runtime_id", "request_id", "turn_id"}
            or not all(_uuid(part) for part in native.values())
        ):
            return _failure("invalid_input", "Check the submitted choice.", 400)
        summary, created = await runner.decide(
            ChoiceInput(
                choice_id=value["choice_id"],
                session_id=value["session_id"],
                generation=value["generation"],
                tool_use_id=value["tool_use_id"],
                native=NativeRequest(**native),
                choice=choice_from_json(value["choice"]),
            )
        )
        return _json(asdict(summary), 202 if created else 200)

    return [
        Route("/api/v2/sessions/{session_id}/approval", endpoint, methods=["GET"]),
        Route("/api/v2/approvals", endpoint, methods=["GET", "POST"]),
        Route("/api/v2/approvals/{choice_id}", endpoint, methods=["GET"]),
        Route("/api/v2/approvals/{choice_id}/choice", endpoint, methods=["GET"]),
    ]
