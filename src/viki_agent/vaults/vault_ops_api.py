"""Authenticated, bounded routes for note operations and their recovery (hard rules 1 and 7).

Preview and apply a rename, move, archive, delete or restore; list a workspace's recovery
entries and stuck operation rows; forget a stuck operation; resolve a blocked publication row.
Every refusal answers `{error: {code, message}}`; the client acts on `code` alone.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Coroutine
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any, cast

import anyio
from anyio.to_thread import run_sync
from starlette.requests import ClientDisconnect, Request
from starlette.responses import Response
from starlette.routing import Route

from viki_agent.api_json import failure, json_response, unique_object
from viki_agent.vaults.vault_lifecycle import VaultLifecycleService
from viki_agent.vaults.vault_mutation import MutationError
from viki_agent.vaults.vault_operations import RecoveryStore
from viki_agent.vaults.vault_publication import (
    PublicationError,
    ResolveChoice,
    VaultPublicationService,
)
from viki_agent.vaults.vault_write_api import _HASH, _PHYSICAL_ID, _canonical_uuid, _valid_ref

# Two 512-character refs with every character escaped as six bytes, and the other fields.
MAX_REQUEST_BYTES = 16 * 1024
MAX_OPERATION_REQUESTS = 4
_Handler = Callable[[Request], Coroutine[Any, Any, Response]]
_MAX_REVISION = 9_007_199_254_740_991
_COMMON_KEYS = frozenset({"policy_revision", "root_physical_id", "kind"})
# Exactly the keys each kind takes: a restore names its recovery entry and carries no hash,
# every other kind names a note and its expected revision, and a delete has no destination.
_MOVE_KEYS = frozenset({"ref", "to", "expected_sha256"})
_KIND_KEYS = {
    "rename": _MOVE_KEYS,
    "move": _MOVE_KEYS,
    "archive": _MOVE_KEYS,
    "delete": frozenset({"ref", "expected_sha256"}),
    "restore": frozenset({"recovery_id", "to"}),
}
_STATUS = {
    code: status
    for status, codes in (
        (400, ("invalid_operation", "invalid_publication", "invalid_ref", "excluded_path")),
        (404, ("unknown_operation", "unknown_publication", "recovery_missing")),
        (
            409,
            (
                "conflict",
                "command_conflict",
                "op_in_progress",
                "vault_locked",
                "vault_recovery_required",
                "unsupported_filesystem",
                "ordinary_workspace_required",
                "workspace_changed",
                "missing_target",
                "read_only_target",
                "not_resolvable",
            ),
        ),
        (413, ("too_large",)),
        (
            423,
            ("target_busy", "parent_busy", "stage_busy", "operation_busy", "publication_busy"),
        ),
        (429, ("overloaded", "operation_limit", "recovery_full")),
    )
    for code in codes
}


async def _json(request: Request, invalid: str) -> dict[str, Any]:
    if (
        request.scope.get("query_string", b"")
        or request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        != "application/json"
        or any(
            value.strip().lower() != "identity"
            for value in request.headers.getlist("content-encoding")
        )
    ):
        raise PublicationError(invalid)
    encoded = bytearray()
    with anyio.fail_after(5):
        async for chunk in request.stream():
            if len(chunk) > MAX_REQUEST_BYTES - len(encoded):
                raise PublicationError("too_large")
            encoded.extend(chunk)
    try:
        value = json.loads(encoded.decode("utf-8"), object_pairs_hook=unique_object)
    except ValueError, UnicodeError, RecursionError:
        raise PublicationError(invalid) from None
    if not isinstance(value, dict):
        raise PublicationError(invalid)
    return cast(dict[str, Any], value)


def _operation(value: dict[str, Any], *, apply: bool) -> tuple[int, str, str, dict[str, str]]:
    """The request's revision, root id, kind and kind-specific fields, or a 400 refusal."""
    kind = value.get("kind")
    if not isinstance(kind, str) or kind not in _KIND_KEYS:
        raise PublicationError("invalid_operation")
    keys = _KIND_KEYS[kind]
    if set(value) != _COMMON_KEYS | keys | ({"command_id"} if apply else frozenset()):
        raise PublicationError("invalid_operation")
    revision = value["policy_revision"]
    physical_id = value["root_physical_id"]
    fields = {key: value[key] for key in keys}
    if (
        type(revision) is not int
        or not 1 <= revision <= _MAX_REVISION
        or not isinstance(physical_id, str)
        or not 3 <= len(physical_id) <= 80
        or _PHYSICAL_ID.fullmatch(physical_id) is None
        or (apply and not _canonical_uuid(value["command_id"]))
        or not all(isinstance(field, str) for field in fields.values())
        or ("expected_sha256" in fields and _HASH.fullmatch(fields["expected_sha256"]) is None)
        or ("recovery_id" in fields and not _canonical_uuid(fields["recovery_id"]))
    ):
        raise PublicationError("invalid_operation")
    # The writer's own rule, as a save's ref meets it.
    if not all(_valid_ref(fields[key]) for key in ("ref", "to") if key in fields):
        raise PublicationError("invalid_ref")
    return revision, physical_id, kind, fields


def _choice(value: dict[str, Any], allowed: tuple[str, ...], invalid: str) -> str:
    if set(value) != {"choice"} or value["choice"] not in allowed:
        raise PublicationError(invalid)
    return value["choice"]


def _path_uuid(request: Request, name: str, invalid: str) -> str:
    value = request.path_params[name]
    if not _canonical_uuid(value):
        raise PublicationError(invalid)
    return value


def _failure(error: PublicationError | MutationError) -> Response:
    # The code and an operation id only: a `MutationError`'s `stage_retained` describes a save's
    # stage, and no ref, path or hash ever rides on a refusal.
    body: dict[str, Any] = {
        "error": {"code": error.code, "message": "The vault operation did not run."}
    }
    if isinstance(error, PublicationError) and error.operation_id is not None:
        body["error"]["operation_id"] = error.operation_id
    return json_response(body, _STATUS.get(error.code, 503))


def vault_ops_routes(
    lifecycle: VaultLifecycleService,
    recovery: RecoveryStore,
    publications: VaultPublicationService,
    *,
    authorized: Callable[[Request], bool],
    origins: frozenset[str],
    workers: set[asyncio.Task[Any]],
    lease_wait_s: float,
) -> list[Route]:
    capacity = anyio.CapacityLimiter(MAX_OPERATION_REQUESTS)

    async def preview(request: Request) -> Response:
        workspace_id = _path_uuid(request, "workspace_id", "invalid_operation")
        value = await _json(request, "invalid_operation")
        revision, physical_id, kind, fields = _operation(value, apply=False)
        plan = await lifecycle.preview(workspace_id, revision, physical_id, kind, **fields)
        body = asdict(plan)
        if plan.refusal is None:
            del body["refusal"]
        return json_response(body)

    async def apply(request: Request) -> Response:
        workspace_id = _path_uuid(request, "workspace_id", "invalid_operation")
        value = await _json(request, "invalid_operation")
        revision, physical_id, kind, fields = _operation(value, apply=True)
        outcome = await lifecycle.apply(
            value["command_id"], workspace_id, revision, physical_id, kind, **fields
        )
        # Every outcome is an answer, `done` or not; a refusal before the step raised instead.
        return json_response(asdict(outcome))

    async def recovery_list(request: Request) -> Response:
        workspace_id = _path_uuid(request, "workspace_id", "invalid_operation")
        if request.scope.get("query_string", b""):
            raise PublicationError("invalid_operation")
        # The sweep drops expired entries first; an entry a pending row names stays past its date.
        entries = await run_sync(recovery.sweep, datetime.now(UTC))
        stuck = await lifecycle.stuck(workspace_id)
        return json_response(
            {
                "entries": [
                    {
                        "recovery_id": entry.recovery_id,
                        "kind": entry.kind,
                        "ref": entry.ref,
                        "byte_size": entry.byte_size,
                        "created_at": entry.created_at,
                        "expires_at": entry.expires_at,
                    }
                    for entry in entries
                    if entry.workspace_id == workspace_id
                ],
                "stuck": [asdict(row) for row in stuck],
            }
        )

    async def forget_operation(request: Request) -> Response:
        operation_id = _path_uuid(request, "operation_id", "invalid_operation")
        _choice(await _json(request, "invalid_operation"), ("forget",), "invalid_operation")
        return json_response(asdict(await lifecycle.forget(operation_id)))

    async def resolve_publication(request: Request) -> Response:
        operation_id = _path_uuid(request, "operation_id", "invalid_publication")
        value = await _json(request, "invalid_publication")
        choice = _choice(value, ("forget", "drop_previous"), "invalid_publication")
        # Bounded: behind a long operation or backup an unbounded wait would hold a slot.
        outcome = await publications.resolve(
            operation_id, cast(ResolveChoice, choice), wait_s=lease_wait_s
        )
        return json_response(asdict(outcome))

    async def admitted(handler: _Handler, request: Request) -> Response:
        permit = object()
        try:
            capacity.acquire_on_behalf_of_nowait(permit)
        except anyio.WouldBlock:
            return failure("overloaded", "Vault operations are busy.", 429)
        task = asyncio.create_task(handler(request))
        workers.add(task)

        def done(future: asyncio.Task[Response]) -> None:
            workers.discard(future)
            capacity.release_on_behalf_of(permit)
            if not future.cancelled():
                future.exception()

        task.add_done_callback(done)
        try:
            return await asyncio.shield(task)
        except PublicationError as error:
            return _failure(error)
        except MutationError as error:
            return _failure(error)
        except TimeoutError:
            return failure("intake_timeout", "The request timed out.", 408)
        except ClientDisconnect:
            return failure("disconnected", "The request was interrupted.", 400)
        except OSError, RuntimeError:
            return failure("unavailable", "The vault operation did not run.", 503)

    def guarded(handler: _Handler) -> _Handler:
        async def endpoint(request: Request) -> Response:
            # Bearer, then Origin, before the path, the body or the capacity is looked at.
            if not authorized(request):
                response = failure("unauthorized", "Authentication is required.", 401)
            else:
                origin = request.headers.get("origin")
                read = request.method in {"GET", "HEAD"}
                if origin not in origins and not (read and origin is None):
                    response = failure("forbidden_origin", "This origin is not allowed.", 403)
                else:
                    response = await admitted(handler, request)
            response.headers["Cache-Control"] = "no-store"
            return response

        return endpoint

    return [
        Route(
            "/api/v2/vaults/{workspace_id}/operations/preview",
            guarded(preview),
            methods=["POST"],
        ),
        Route("/api/v2/vaults/{workspace_id}/operations", guarded(apply), methods=["POST"]),
        Route("/api/v2/vaults/{workspace_id}/recovery", guarded(recovery_list), methods=["GET"]),
        Route(
            "/api/v2/vault-operations/{operation_id}/resolve",
            guarded(forget_operation),
            methods=["POST"],
        ),
        Route(
            "/api/v2/vault-publications/{operation_id}/resolve",
            guarded(resolve_publication),
            methods=["POST"],
        ),
    ]
