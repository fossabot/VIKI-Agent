"""Private REST target, submission and recovery routes for durable turn commands."""

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
from viki_agent.conversations.command_runtime import CommandRunner
from viki_agent.conversations.commands import CommandError, CommandInput
from viki_agent.conversations.turn_parts import part_data
from viki_agent.session import CommandDispatchRejected

MAX_COMMAND_REQUESTS = 4
MAX_COMMAND_BODY_BYTES = 2048
COMMAND_BODY_TIMEOUT_S = 5.0

logger = logging.getLogger(__name__)

_FAILURES = {
    "no_target": (404, "This session isn't open. Open the conversation, then send."),
    "stale_generation": (
        409,
        "The session has been updated. Check the conversation, then send.",
    ),
    "closed": (409, "This session is closed."),
    "turn_running": (
        409,
        "A response is in progress. Wait for it to finish, or stop it, then send.",
    ),
    "closing": (503, "Can't send while shutting down."),
    "overloaded": (429, "Sending is busy right now. Wait a moment, then try again."),
}


def _uuid(value: Any) -> bool:
    if not isinstance(value, str) or len(value) != 36:
        return False
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False


def command_routes(
    runner: CommandRunner,
    *,
    authorized: Callable[[Request], bool],
    origins: frozenset[str],
    workers: set[asyncio.Task[Any]],
) -> list[Route]:
    capacity = anyio.CapacityLimiter(MAX_COMMAND_REQUESTS)

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
            return _failure("overloaded", "Busy processing the send history. Wait a moment.", 429)
        # This worker owns the intake/read slot until completion, even after HTTP disconnect.
        # Dispatch has its own bounded lifetime in CommandRunner after the acceptance receipt.
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
        except CommandDispatchRejected as exc:
            status, message = _FAILURES.get(
                exc.code, (503, "Couldn't finish processing the submission.")
            )
            return _failure(exc.code, message, status)
        except CommandError as exc:
            status = {
                "conflict": 409,
                "draft_conflict": 409,
                "draft_already_submitted": 409,
                "not_found": 404,
                "capacity": 429,
                "too_large": 413,
                "invalid_input": 400,
                "invalid_command": 400,
                "invalid_id": 400,
                "invalid_page": 400,
                "empty_draft": 400,
                "unsupported_attachment": 400,
                "attachment_unavailable": 409,
            }.get(exc.code, 503)
            if status >= 500:
                # Storage and DPAPI failures arrive typed, so this is where the log learns of
                # them. Hard rule 8: the code is the whole record, never the submitted text.
                logger.warning("command refused: %s", exc.code)
            return _failure(exc.code, exc.message, status)
        except ClientDisconnect:
            return _failure("disconnected", "Receiving the submission was interrupted.", 400)
        except Exception as exc:
            logger.warning("command request failed: %s", type(exc).__name__)
            return _failure("unavailable", "Couldn't read or write the send history.", 503)

    async def perform(request: Request) -> Response:
        raw_query = request.scope.get("query_string", b"")
        if len(raw_query) > 256:
            return _failure("invalid_page", "Check the paging for the send history.", 400)
        params = request.path_params
        if "session_id" in params:
            sid = params["session_id"]
            if not _uuid(sid):
                return _failure("invalid_input", "Check the session ID.", 400)
            if request.url.path.endswith("/draft-command"):
                query = request.query_params
                raw_revision = query.get("revision")
                if (
                    set(query) != {"revision"}
                    or len(query.getlist("revision")) != 1
                    or raw_revision is None
                    or not raw_revision.isascii()
                    or not raw_revision.isdecimal()
                    or len(raw_revision) > 16
                    or not 1 <= int(raw_revision) < (1 << 53)
                    or str(int(raw_revision)) != raw_revision
                ):
                    return _failure("invalid_input", "Check the send history entry.", 400)
                return _json(asdict(await runner.find_draft_command(sid, int(raw_revision))))
            if raw_query:
                return _failure("invalid_input", "Check the session ID.", 400)
            actor = runner.manager.get(sid)
            if actor is None:
                raise CommandDispatchRejected("no_target")
            return _json(actor.command_context())
        if "command_id" in params:
            command_id = params["command_id"]
            if raw_query or not _uuid(command_id):
                return _failure("invalid_id", "Check the send history entry.", 400)
            record = await runner.get(command_id)
            body = asdict(record)
            if not request.url.path.endswith("/submission"):
                body.pop("text")
                body.pop("attachments", None)
                body.pop("parts", None)
            else:
                body["parts"] = [part_data(part) for part in record.parts]
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
                return _failure("invalid_page", "Check the paging for the send history.", 400)
            return _json(asdict(await runner.list(after=after, limit=int(limit))))
        if raw_query:
            return _failure("invalid_input", "Check the submission.", 400)
        if (
            request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            != "application/json"
            or request.headers.get("content-encoding", "identity").lower() != "identity"
        ):
            return _failure(
                "invalid_representation", "Send the submission as uncompressed JSON.", 415
            )
        length = request.headers.get("content-length")
        if length is not None:
            if not length.isascii() or not length.isdecimal() or len(length) > 10:
                return _failure("invalid_body", "Check the submission.", 400)
            if int(length) > MAX_COMMAND_BODY_BYTES:
                return _failure("too_large", "The submitted data is too large.", 413)
        body = bytearray()
        try:
            with anyio.fail_after(COMMAND_BODY_TIMEOUT_S):
                async for chunk in request.stream():
                    if len(chunk) > MAX_COMMAND_BODY_BYTES - len(body):
                        return _failure("too_large", "The submitted data is too large.", 413)
                    body.extend(chunk)
        except TimeoutError:
            return _failure("intake_timeout", "Receiving the submission took too long.", 408)
        try:
            value = json.loads(body.decode("utf-8"), object_pairs_hook=_unique)
        except ValueError, RecursionError:
            value = None
        if (
            not isinstance(value, dict)
            or set(value) != {"command_id", "session_id", "generation", "draft_revision"}
            or not all(_uuid(value[key]) for key in ("command_id", "session_id", "generation"))
            or type(value["draft_revision"]) is not int
            or not 1 <= value["draft_revision"] < (1 << 53)
        ):
            return _failure("invalid_input", "Check the submission.", 400)
        record, created = await runner.submit(CommandInput(**value))
        receipt = asdict(record)
        receipt.pop("text")
        receipt.pop("attachments", None)
        receipt.pop("parts", None)
        return _json(receipt, 202 if created else 200)

    return [
        Route("/api/v2/sessions/{session_id}/target", endpoint, methods=["GET"]),
        Route("/api/v2/sessions/{session_id}/draft-command", endpoint, methods=["GET"]),
        Route("/api/v2/commands", endpoint, methods=["GET", "POST"]),
        Route("/api/v2/commands/{command_id}", endpoint, methods=["GET"]),
        Route("/api/v2/commands/{command_id}/submission", endpoint, methods=["GET"]),
    ]
