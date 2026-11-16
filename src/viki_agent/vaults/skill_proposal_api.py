"""Authenticated skill proposal routes: a proposed `SKILL.md` text, kept sealed in the store.

The proposed text is the user's skill content. It is read from the request body, handed to
`SkillProposalService` and sealed there; it is never echoed in a create answer, an error body or a
log line, and this module logs nothing.
"""

from __future__ import annotations

import asyncio
import functools
import json
import uuid
from collections.abc import Callable
from typing import Any

import anyio
from anyio.to_thread import run_sync
from starlette.requests import ClientDisconnect, Request
from starlette.responses import Response
from starlette.routing import Route

from viki_agent.api_json import failure, json_response, unique_object
from viki_agent.storage import StateError
from viki_agent.vaults.skill_proposals import (
    MAX_SKILL_BYTES,
    SkillProposalError,
    SkillProposalService,
)

MAX_SKILL_REQUESTS = 4
# JSON may escape one code point as six ASCII bytes, so the envelope is bounded at six times the
# text limit; the service enforces the exact byte limit on the decoded text.
MAX_REQUEST_BYTES = 6 * MAX_SKILL_BYTES + 4096
_CREATE_KEYS = {"text", "trigger", "sessions"}
_ACT_KEYS = {"action", "revision"}

_MESSAGES = {
    "invalid_request": "Check what you're proposing and try again.",
    "invalid_skill": "This is not a skill VIKI Agent can propose.",
    "unknown_proposal": "This proposal is gone.",
    "no_vault": "Open a vault to propose skills.",
    "not_active": "Open this proposal's vault to act on it.",
    "catalog_partial": "VIKI Agent could not read this vault's skills in full. Try again.",
    "no_change": "The vault already has this exact skill.",
    "proposal_conflict": (
        "Approvals already holds a proposal for this skill. Open it to edit it, or withdraw it "
        "first."
    ),
    "proposals_full": (
        "Approvals already keeps 100 skill proposals for this vault. Withdraw some first."
    ),
    "stale_revision": "This proposal changed since you opened it. Open it again.",
    "not_approvable": "This proposal cannot be approved in its current state.",
    "already_applied": "The skill file already matches this proposal.",
    "name_changed": "An edit keeps the skill's name. Propose a new skill for a new name.",
    "too_large": "This skill is too large.",
    "unauthorized": "Could not authenticate. Reopen the window and try again.",
    "forbidden_origin": "This window can't perform that action.",
    "overloaded": "Busy right now. Wait a moment and try again.",
    "intake_timeout": "The request timed out.",
    "disconnected": "The request was interrupted.",
    "invalid_representation": "This request is not in a form this route accepts.",
    "unavailable": "That did not work. Try again.",
}
_STATUS = {
    "invalid_request": 400,
    "invalid_skill": 400,
    "unknown_proposal": 404,
    "no_vault": 422,
    "not_active": 409,
    "catalog_partial": 409,
    "no_change": 409,
    "proposal_conflict": 409,
    "proposals_full": 409,
    "stale_revision": 409,
    "not_approvable": 409,
    "already_applied": 409,
    "name_changed": 409,
    "too_large": 413,
}


class _BadRepresentation(Exception):
    """The body is not JSON the route accepts, or a GET carries one: answered 415."""


def _error(exc: SkillProposalError) -> Response:
    """The code, a fixed message and `extra`, which holds ids, hashes and reason codes only."""
    body: dict[str, Any] = {
        "code": exc.code,
        "message": _MESSAGES.get(exc.code, _MESSAGES["unavailable"]),
    }
    if exc.extra:
        body["details"] = exc.extra
    return json_response({"error": body}, _STATUS.get(exc.code, 503))


def _canonical_uuid(value: str) -> bool:
    """Whether `value` is a UUID in its one canonical (lowercase, hyphenated) spelling.

    A path param is whatever the router matched, so a malformed or surrogate-laden id must be
    refused here, before it ever reaches a store call that could choke on it.
    """
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False


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
                raise SkillProposalError("too_large", limit=MAX_SKILL_BYTES)
            encoded.extend(chunk)
    try:
        value = json.loads(encoded.decode("utf-8"), object_pairs_hook=unique_object)
    except ValueError, RecursionError:
        raise SkillProposalError("invalid_request") from None
    if not isinstance(value, dict):
        raise SkillProposalError("invalid_request")
    return value


def skill_proposal_routes(
    service: SkillProposalService,
    *,
    authorized: Callable[[Request], bool],
    origins: frozenset[str],
    workers: set[asyncio.Task[Any]],
) -> list[Route]:
    capacity = anyio.CapacityLimiter(MAX_SKILL_REQUESTS)

    async def create(request: Request) -> Response:
        body = await _json_object(request)
        if (
            set(body) != _CREATE_KEYS
            or not isinstance(body["text"], str)
            or not isinstance(body["trigger"], str)
            or not isinstance(body["sessions"], list)
        ):
            raise SkillProposalError("invalid_request")
        try:
            item = await run_sync(
                service.create,
                request.path_params["workspace_id"],
                body["text"],
                body["trigger"],
                body["sessions"],
            )
        except SkillProposalError as exc:
            if exc.code != "proposal_exists":
                raise
            return json_response(
                {"proposal": {"id": exc.extra["proposal_id"]}, "replayed": True}, 200
            )
        return json_response({"proposal": item}, 201)

    async def act(request: Request) -> Response:
        proposal_id = request.path_params["proposal_id"]
        if not _canonical_uuid(proposal_id):
            raise SkillProposalError("invalid_request")
        body = await _json_object(request)
        keys = set(body)
        if keys not in (_ACT_KEYS, _ACT_KEYS | {"text"}, _ACT_KEYS | {"reason"}):
            raise SkillProposalError("invalid_request")
        action, revision = body["action"], body["revision"]
        text, reason = body.get("text"), body.get("reason")
        if (
            not isinstance(action, str)
            or not isinstance(revision, str)
            or not isinstance(text, str | None)
            or not isinstance(reason, str | None)
        ):
            raise SkillProposalError("invalid_request")
        result = await run_sync(
            functools.partial(service.act, proposal_id, action, revision, text=text, reason=reason)
        )
        if result is None:
            return json_response({"withdrawn": True})
        return json_response({"proposal": result})

    async def perform(request: Request) -> Response:
        if request.method == "GET":
            if not _bodiless(request):
                raise _BadRepresentation
            proposal_id = request.path_params["proposal_id"]
            if not _canonical_uuid(proposal_id):
                raise SkillProposalError("invalid_request")
            found = await run_sync(service.detail, proposal_id)
            return json_response({"proposal": found})
        if "proposal_id" in request.path_params:
            return await act(request)
        return await create(request)

    async def admitted(request: Request) -> Response:
        permit = object()
        try:
            capacity.acquire_on_behalf_of_nowait(permit)
        except anyio.WouldBlock:
            return failure("overloaded", _MESSAGES["overloaded"], 429)
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
        except SkillProposalError as exc:
            return _error(exc)
        except _BadRepresentation:
            return failure("invalid_representation", _MESSAGES["invalid_representation"], 415)
        except StateError:
            return failure("unavailable", _MESSAGES["unavailable"], 503)
        except TimeoutError:
            return failure("intake_timeout", _MESSAGES["intake_timeout"], 408)
        except ClientDisconnect:
            return failure("disconnected", _MESSAGES["disconnected"], 400)

    async def endpoint(request: Request) -> Response:
        if not authorized(request):
            response = failure("unauthorized", _MESSAGES["unauthorized"], 401)
        else:
            origin = request.headers.get("origin")
            # A state-changing POST needs a listed Origin; only the GET may omit it.
            if origin not in origins and (origin is not None or request.method != "GET"):
                response = failure("forbidden_origin", _MESSAGES["forbidden_origin"], 403)
            else:
                response = await admitted(request)
        response.headers["Cache-Control"] = "no-store"
        return response

    return [
        Route("/api/v2/vaults/{workspace_id}/skill-proposals", endpoint, methods=["POST"]),
        Route("/api/v2/decisions/skills/{proposal_id}", endpoint, methods=["GET", "POST"]),
    ]
