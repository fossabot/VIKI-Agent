"""Authenticated bounded note and instruction-file publication and redacted recovery-status
routes."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import sys
import uuid
from collections.abc import Callable, Coroutine
from dataclasses import asdict
from typing import Any

import anyio
from anyio.to_thread import run_sync
from starlette.requests import ClientDisconnect, Request
from starlette.responses import Response
from starlette.routing import Route

from viki_agent.api_json import failure, json_response, unique_object
from viki_agent.vaults.vault import is_bot_memory_ref
from viki_agent.vaults.vault_mutation import (
    INSTRUCTION_REFS,
    MAX_PUBLICATION_BYTES,
    MutationError,
    _safe_ref,
)
from viki_agent.vaults.vault_publication import (
    REQUEST_LEASE_WAIT_S,
    PublicationError,
    VaultPublicationService,
)

logger = logging.getLogger(__name__)

# JSON may escape one Unicode code point as six ASCII bytes; bound the envelope while still
# accepting every valid 2 MiB UTF-8 content string.
MAX_REQUEST_BYTES = 6 * MAX_PUBLICATION_BYTES + 4096
MAX_WRITE_REQUESTS = 4
_HASH = re.compile(r"[0-9a-f]{64}")
_PHYSICAL_ID = re.compile(r"[0-9]+:[0-9]+")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")  # every C0 control but tab, LF and CR
_Action = Callable[[Request], Coroutine[Any, Any, Response]]


def _canonical_uuid(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False


def _valid_ref(value: object) -> bool:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 512
        or not value.casefold().endswith(".md")
        or value.startswith("/")
        or is_bot_memory_ref(value)
        or "\\" in value
        or ":" in value
        or any(ord(character) < 32 for character in value)
    ):
        return False
    if any(part in {"", ".", ".."} for part in value.split("/")):
        return False
    # The writer's own check: excluded names at any depth, NTFS names, the Markdown suffix.
    try:
        _safe_ref(value)
    except MutationError:
        return False
    return True


async def _body(request: Request) -> tuple[int, str, bytes, str | None]:
    if (
        request.scope.get("query_string", b"")
        or request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        != "application/json"
        or any(
            value.strip().lower() != "identity"
            for value in request.headers.getlist("content-encoding")
        )
    ):
        raise PublicationError("invalid_publication")
    encoded = bytearray()
    with anyio.fail_after(5):
        async for chunk in request.stream():
            if len(chunk) > MAX_REQUEST_BYTES - len(encoded):
                raise PublicationError("publication_too_large")
            encoded.extend(chunk)
    try:
        value = json.loads(encoded.decode("utf-8"), object_pairs_hook=unique_object)
    except ValueError, UnicodeError, RecursionError:
        raise PublicationError("invalid_publication") from None
    if not isinstance(value, dict) or set(value) != {
        "policy_revision",
        "root_physical_id",
        "content",
        "expected_sha256",
    }:
        raise PublicationError("invalid_publication")
    revision = value["policy_revision"]
    physical_id = value["root_physical_id"]
    content = value["content"]
    expected = value["expected_sha256"]
    if (
        type(revision) is not int
        or not 1 <= revision <= 9_007_199_254_740_991
        or not isinstance(physical_id, str)
        or not 3 <= len(physical_id) <= 80
        or _PHYSICAL_ID.fullmatch(physical_id) is None
        or not isinstance(content, str)
        or _CONTROL.search(content) is not None
        or expected is not None
        and (not isinstance(expected, str) or _HASH.fullmatch(expected) is None)
    ):
        raise PublicationError("invalid_publication")
    try:
        body = content.encode("utf-8")
    except UnicodeError:
        raise PublicationError("invalid_publication") from None
    if len(body) > MAX_PUBLICATION_BYTES:
        raise PublicationError("publication_too_large")
    return revision, physical_id, body, expected


def _status(code: str) -> int:
    if code in {"invalid_publication", "invalid_ref", "excluded_path", "invalid_content"}:
        return 400
    if code == "publication_too_large":
        return 413
    if code in {"publication_limit"}:
        return 429
    if code in {
        "publication_busy",
        "ordinary_workspace_required",
        "workspace_changed",
        "unsupported_filesystem",
        "conflict",
        "read_only_target",
        "vault_recovery_required",
        "protected_instructions",
    }:
        return 409
    # Another holder has the file, its folder or the whole vault (`vault_locked`, as a backup
    # holds it): the same save can be sent again once it lets go.
    if code in {"parent_busy", "target_busy", "stage_busy", "vault_locked"}:
        return 423
    return 503


def _described(error: BaseException | None) -> str:
    """An exception's type with its typed code or OS error number, never its message: an
    OSError's message names the file."""
    if error is None:
        return "-"
    name = type(error).__name__
    if isinstance(error, PublicationError | MutationError):
        return f"{name} {error.code}"
    if isinstance(error, OSError):
        number = getattr(error, "winerror", None) or error.errno
        return name if number is None else f"{name} {number}"
    return name


def _log_failure(error: BaseException | None) -> None:
    """One WARNING for a request that failed on the server's side, so a save that stopped
    partway can be traced to its operation and its cause; a refusal the user can act on logs
    nothing. Codes, operation ids and exception types only (hard rule 8): never a ref, a path or
    a note's text."""
    if isinstance(error, PublicationError | MutationError):
        if _status(error.code) >= 500:
            operation_id = error.operation_id if isinstance(error, PublicationError) else None
            logger.warning(
                "vault publication failed: %s %s (%s)",
                error.code,
                operation_id or "-",
                _described(error.__cause__),
            )
    elif isinstance(error, OSError | RuntimeError) and not isinstance(error, TimeoutError):
        logger.warning("vault publication failed: %s", _described(error))


def _failure(error: PublicationError | MutationError) -> Response:
    code = error.code
    status = _status(code)
    if code == "read_only_target":
        if sys.platform == "win32":
            message = "This note is read-only. Clear Read-only in its Properties, then save again."
        else:
            message = "This note is read-only or locked. Make it writable, then save again."
    elif code == "vault_recovery_required":
        message = "Another operation in this vault needs recovery first."
    else:
        message = "The note could not be saved."
    body: dict[str, Any] = {"error": {"code": code, "message": message}}
    if isinstance(error, PublicationError) and error.operation_id is not None:
        body["error"]["operation_id"] = error.operation_id
    return json_response(body, status)


def vault_write_routes(
    service: VaultPublicationService,
    *,
    authorized: Callable[[Request], bool],
    origins: frozenset[str],
    workers: set[asyncio.Task[Any]],
    lease_wait_s: float = REQUEST_LEASE_WAIT_S,
) -> list[Route]:
    """`lease_wait_s` bounds a note save's wait for the vault lease below its client's 15 s
    abort, for a note save and an instruction save alike: past it the save refuses
    `vault_locked` before any vault write, so a save the client has reported failed never lands
    later."""
    capacity = anyio.CapacityLimiter(MAX_WRITE_REQUESTS)

    async def perform(request: Request) -> Response:
        if request.method in {"GET", "HEAD"}:
            if request.scope.get("query_string", b""):
                raise PublicationError("invalid_publication")
            rows = await run_sync(service.recovery_status)
            return json_response({"publications": [asdict(row) for row in rows]})
        workspace_id = request.path_params["workspace_id"]
        ref = request.path_params["ref"]
        if not _canonical_uuid(workspace_id) or not _valid_ref(ref):
            raise PublicationError("invalid_ref")
        revision, physical_id, content, expected = await _body(request)
        outcome = await service.save(
            workspace_id,
            revision,
            physical_id,
            ref,
            content,
            expected_sha256=expected,
            wait_s=lease_wait_s,
        )
        return json_response(asdict(outcome), 409 if outcome.status == "conflict" else 200)

    async def perform_instruction(request: Request) -> Response:
        """The Settings instruction editor's Save: replace the vault's root `AGENTS.md` or
        `CLAUDE.md` in place (hard rule 1's one exception, ADR-082)."""
        workspace_id = request.path_params["workspace_id"]
        name = request.path_params["name"]
        if not _canonical_uuid(workspace_id) or name not in INSTRUCTION_REFS:
            raise PublicationError("invalid_ref")
        revision, physical_id, content, expected = await _body(request)
        if expected is None:  # replace only: never creates either file
            raise PublicationError("invalid_publication")
        outcome = await service.save_instruction(
            workspace_id,
            revision,
            physical_id,
            name,
            content,
            expected_sha256=expected,
            wait_s=lease_wait_s,
        )
        conflict = outcome.status == "conflict"
        sha256 = None if conflict else hashlib.sha256(content).hexdigest()
        return json_response({**asdict(outcome), "sha256": sha256}, 409 if conflict else 200)

    async def admitted(request: Request, action: _Action) -> Response:
        permit = object()
        try:
            capacity.acquire_on_behalf_of_nowait(permit)
        except anyio.WouldBlock:
            return failure("overloaded", "Vault writes are busy.", 429)
        task = asyncio.create_task(action(request))
        workers.add(task)

        def done(future: asyncio.Task[Response]) -> None:
            workers.discard(future)
            capacity.release_on_behalf_of(permit)
            # Logged here rather than in the handler: the save runs on after its client has gone.
            if not future.cancelled():
                _log_failure(future.exception())

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
            return failure("unavailable", "The note could not be saved.", 503)

    def gated(action: _Action) -> _Action:
        async def endpoint(request: Request) -> Response:
            if not authorized(request):
                response = failure("unauthorized", "Authentication is required.", 401)
            else:
                origin = request.headers.get("origin")
                if (request.method == "PUT" and origin not in origins) or (
                    request.method in {"GET", "HEAD"}
                    and origin is not None
                    and origin not in origins
                ):
                    response = failure("forbidden_origin", "This origin is not allowed.", 403)
                else:
                    response = await admitted(request, action)
            response.headers["Cache-Control"] = "no-store"
            return response

        return endpoint

    return [
        Route("/api/v2/vault-publications/recovery", gated(perform), methods=["GET"]),
        Route("/api/v2/vaults/{workspace_id}/notes/{ref:path}", gated(perform), methods=["PUT"]),
        Route(
            "/api/v2/vaults/{workspace_id}/instructions/{name}",
            gated(perform_instruction),
            methods=["PUT"],
        ),
    ]
