"""Authenticated proposal routes: the user's note text for a protected vault, kept in the store.

The proposed text is the user's note content. It is read from the request body, handed to
`ProposalService` and sealed there; it is never echoed in a create response, an error body or a log
line, and this module logs nothing.
"""

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
from viki_agent.storage import StateError
from viki_agent.vaults.proposals import MAX_PROPOSAL_BYTES, ProposalError, ProposalService
from viki_agent.vaults.vault_api import MAX_VAULT_REQUESTS

# JSON may escape one code point as six ASCII bytes, so the envelope is bounded at six times the
# text limit; the service enforces the exact byte limit on the decoded text.
MAX_REQUEST_BYTES = 6 * MAX_PROPOSAL_BYTES + 4096
_CREATE_KEYS = {"ref", "text", "base_revision"}
_ACTIONS = {"handoff", "withdraw"}

_MESSAGES = {
    "invalid_request": "Check the proposal's content.",
    "invalid_ref": "Check the note's location.",
    "private_path": "This location can't accept a proposal.",
    "unknown_workspace": "Vault not found.",
    "unknown_proposal": "Proposal not found.",
    "not_protected": "This vault doesn't accept proposals.",
    "stale_revision": "The note has changed. Reopen it, then try again.",
    "proposal_conflict": "This note already has another proposal. Withdraw it first.",
    "no_change": "This is the same as the note's current content.",
    "proposals_full": "Proposals are full. Withdraw one you no longer need, then try again.",
    "too_large": "The proposal is too large.",
    "source_missing": "The original note can't be found.",
    "stale_proposal": "The original note has changed, so this proposal can no longer be used.",
    "already_applied": "This proposal has already been applied.",
    "unavailable": "Can't process the proposal.",
    "invalid_representation": "The content of this request is in the wrong format.",
}
_STATUS = {
    "invalid_request": 400,
    "invalid_ref": 400,
    "private_path": 400,
    "unknown_workspace": 404,
    "unknown_proposal": 404,
    "source_missing": 404,
    "not_protected": 409,
    "stale_revision": 409,
    "proposal_conflict": 409,
    "no_change": 409,
    "proposals_full": 409,
    "stale_proposal": 409,
    "already_applied": 409,
    "too_large": 413,
}


class _BadRepresentation(Exception):
    """The body is not JSON the route accepts, or a GET carries one: answered 415."""


def _error(exc: ProposalError) -> Response:
    """The code, a fixed message and `extra`, which holds ids, revisions and limits only."""
    body: dict[str, Any] = {
        "code": exc.code,
        "message": _MESSAGES.get(exc.code, _MESSAGES["unavailable"]),
    }
    if exc.extra:
        body["details"] = exc.extra
    return json_response({"error": body}, _STATUS.get(exc.code, 503))


def _json_representation(request: Request) -> bool:
    headers = request.headers
    if headers.get("content-encoding", "identity").strip().lower() != "identity":
        return False
    content_type = headers.get("content-type")
    return content_type is not None and (
        content_type.split(";")[0].strip().lower() == "application/json"
    )


def _bodiless(request: Request) -> bool:
    headers = request.headers
    return "transfer-encoding" not in headers and headers.get("content-length", "0") == "0"


async def _json_object(request: Request) -> dict[str, Any]:
    if not _json_representation(request):
        raise _BadRepresentation
    encoded = bytearray()
    with anyio.fail_after(5):
        async for chunk in request.stream():
            if len(chunk) > MAX_REQUEST_BYTES - len(encoded):
                raise ProposalError("too_large", limit=MAX_PROPOSAL_BYTES)
            encoded.extend(chunk)
    try:
        value = json.loads(encoded.decode("utf-8"), object_pairs_hook=unique_object)
    except ValueError, RecursionError:
        raise ProposalError("invalid_request") from None
    if not isinstance(value, dict):
        raise ProposalError("invalid_request")
    return value


def proposal_routes(
    service: ProposalService,
    *,
    authorized: Callable[[Request], bool],
    origins: frozenset[str],
    workers: set[asyncio.Task[Any]],
) -> list[Route]:
    capacity = anyio.CapacityLimiter(MAX_VAULT_REQUESTS)

    async def create(request: Request) -> Response:
        body = await _json_object(request)
        if set(body) != _CREATE_KEYS or not all(isinstance(v, str) for v in body.values()):
            raise ProposalError("invalid_request")
        try:
            item = await run_sync(
                service.create,
                request.path_params["workspace_id"],
                body["ref"],
                body["text"],
                body["base_revision"],
            )
        except ProposalError as exc:
            if exc.code != "proposal_exists":
                raise
            return json_response(
                {"proposal": {"id": exc.extra["proposal_id"]}, "replayed": True}, 200
            )
        return json_response({"proposal": item}, 201)

    async def act(request: Request) -> Response:
        proposal_id = request.path_params["proposal_id"]
        body = await _json_object(request)
        action = body.get("action")
        if set(body) != {"action"} or action not in _ACTIONS:
            raise ProposalError("invalid_request")
        if action == "withdraw":
            await run_sync(service.withdraw, proposal_id)
            return json_response({"withdrawn": True})
        return json_response({"proposal": await run_sync(service.handoff, proposal_id)})

    async def perform(request: Request) -> Response:
        if request.method == "GET":
            if not _bodiless(request):
                raise _BadRepresentation
            detail = await run_sync(service.detail, request.path_params["proposal_id"])
            return json_response({"proposal": detail})
        if "proposal_id" in request.path_params:
            return await act(request)
        return await create(request)

    async def admitted(request: Request) -> Response:
        permit = object()
        try:
            capacity.acquire_on_behalf_of_nowait(permit)
        except anyio.WouldBlock:
            return failure(
                "overloaded",
                "Busy processing proposals. Wait a moment, then try again.",
                429,
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
        except ProposalError as exc:
            return _error(exc)
        except _BadRepresentation:
            return failure("invalid_representation", _MESSAGES["invalid_representation"], 415)
        except StateError:
            return failure("unavailable", _MESSAGES["unavailable"], 503)
        except TimeoutError:
            return failure("intake_timeout", "Receiving the proposal took too long.", 408)
        except ClientDisconnect:
            return failure("disconnected", "Receiving the proposal was interrupted.", 400)

    async def endpoint(request: Request) -> Response:
        if not authorized(request):
            response = failure("unauthorized", "Can't authenticate. Open VIKI Agent again.", 401)
        else:
            origin = request.headers.get("origin")
            # A state-changing POST needs a listed Origin; only the GET may omit it.
            if origin not in origins and (origin is not None or request.method != "GET"):
                response = failure("forbidden_origin", "This window can't do that.", 403)
            else:
                response = await admitted(request)
        response.headers["Cache-Control"] = "no-store"
        return response

    return [
        Route("/api/v2/vaults/{workspace_id}/proposals", endpoint, methods=["POST"]),
        Route("/api/v2/decisions/proposals/{proposal_id}", endpoint, methods=["GET", "POST"]),
    ]
