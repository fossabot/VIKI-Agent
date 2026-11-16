"""Authenticated, bounded routes for keep-in-vault, memory records, backup and backup restore.

Each route hands one strict body to its service, which decides policy: a protected vault is refused
with 403 `vault_protected` only where a service raises it, so a memory created there answers as a
proposal (`outcome: "proposed"`) and a backup of it runs. Every refusal answers
`{error: {code, message, ...}}`, whose extra keys are only ids, revisions and grammar reasons; the
client acts on `code` alone. "Backup restore" is the copy of a backup into a new folder,
never the journal's `restore` kind. This module logs nothing.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
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
from viki_agent.automation.routines import stamp
from viki_agent.vaults import memory_record
from viki_agent.vaults.memory import (
    Corrected,
    MemoryService,
    MemoryServiceError,
    Reviewed,
    Written,
)
from viki_agent.vaults.memory_record import Fields, NoteSource, SessionSource, Source
from viki_agent.vaults.vault_attachments import AttachmentError, VaultAttachmentService
from viki_agent.vaults.vault_backup import BackupError, VaultBackupService
from viki_agent.vaults.vault_mutation import MutationError
from viki_agent.vaults.vault_publication import PublicationError
from viki_agent.vaults.vault_restore import BackupRestoreError, BackupRestoreService
from viki_agent.vaults.vault_write_api import _CONTROL, _HASH, _PHYSICAL_ID, _canonical_uuid

# A whole memory record, every character escaped as six bytes, bounds the largest body any route
# here takes (a claim plus its sources); the service enforces the record's exact limits.
MAX_REQUEST_BYTES = 6 * memory_record.MAX_RECORD_BYTES + 4096
MAX_KNOWLEDGE_REQUESTS = 4
MAX_NAME_CHARS = 255
MAX_PATH_CHARS = 4096
MAX_BACKUP_RESPONSE_BYTES = 256 * 1024
MAX_BACKUP_RESPONSE_ROWS = 2_000
_MAX_REVISION = 9_007_199_254_740_991
_MESSAGE = "The vault request did not complete."
_Handler = Callable[[Request], Coroutine[Any, Any, Response]]
# The keys a refusal may carry beyond its code: ids, revisions and grammar reasons, never a ref,
# a path, a claim or manifest text.
_EXTRA_KEYS = ("operation_id", "current_revision", "reason", "replacement_id")
_ADMISSION_KEYS = frozenset({"policy_revision", "root_physical_id"})
_KEEP_KEYS = _ADMISSION_KEYS | {"command_id", "asset_id", "name"}
_DRAFT_KEYS = frozenset({"kind", "scope", "claim", "sources"})
_CREATE_KEYS = _ADMISSION_KEYS | _DRAFT_KEYS | {"command_id"}
_PATCH_KEYS = _ADMISSION_KEYS | {"action", "sha256"}
_ACTION_KEYS = {
    "approve": frozenset(),
    "retire": frozenset({"retired_by"}),
    "correct": frozenset({"claim"}),
    "narrow_scope": frozenset(),
}
_STATUS = {
    code: status
    for status, codes in (
        (
            400,
            (
                "invalid_request",
                "invalid_input",
                "invalid_name",
                "invalid_path",
                "invalid_backup",
                "invalid_ref",
                "excluded_path",
                "invalid_publication",
                "private_path",
                "not_found",
                "not_directory",
                "unsafe_path",
            ),
        ),
        (
            403,
            (
                "vault_protected",
                "vault_read_only",
                "denied_destination",
                "denied_location",
                "protected_overlap",
                "protected_root",
            ),
        ),
        (404, ("unknown_workspace", "asset_unavailable", "missing_note", "source_missing")),
        (
            409,
            (
                "conflict",
                "stale_revision",
                "invalid_transition",
                "invalid_record",
                "vault_locked",
                "vault_recovery_required",
                "ordinary_workspace_required",
                "workspace_changed",
                "unsupported_filesystem",
                "read_only_target",
                "publication_busy",
                "root_unavailable",
                "root_changed",
                "not_active_vault",
                "overlaps_registration",
                "case_collision",
                "not_protected",
                "proposal_conflict",
                "no_change",
            ),
        ),
        (413, ("too_large", "backup_too_large", "note_too_large")),
        (422, ("unsupported_location",)),
        (423, ("target_busy", "parent_busy", "stage_busy")),
        (429, ("publication_limit", "proposals_full")),
    )
    for code in codes
}


class _Refused(Exception):
    """A request this module refuses before any service sees it."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


async def _json(request: Request) -> dict[str, Any]:
    if (
        request.scope.get("query_string", b"")
        or request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        != "application/json"
        or any(
            value.strip().lower() != "identity"
            for value in request.headers.getlist("content-encoding")
        )
    ):
        raise _Refused("invalid_request")
    encoded = bytearray()
    with anyio.fail_after(5):
        async for chunk in request.stream():
            if len(chunk) > MAX_REQUEST_BYTES - len(encoded):
                raise _Refused("too_large")
            encoded.extend(chunk)
    try:
        value = json.loads(encoded.decode("utf-8"), object_pairs_hook=unique_object)
    except ValueError, UnicodeError, RecursionError:
        raise _Refused("invalid_request") from None
    if not isinstance(value, dict):
        raise _Refused("invalid_request")
    return cast(dict[str, Any], value)


def _keys(value: dict[str, Any], keys: frozenset[str]) -> dict[str, Any]:
    if set(value) != keys:
        raise _Refused("invalid_request")
    return value


def _require(condition: bool) -> None:
    if not condition:
        raise _Refused("invalid_request")


def _text(value: object, high: int) -> str:
    """A path or folder name: 1..`high` characters and no control code (a NUL would leave the
    services' `OSError` handling as a `ValueError`)."""
    _require(
        isinstance(value, str)
        and 1 <= len(value) <= high
        and not any(ord(character) < 32 or character == "\x7f" for character in value)
    )
    return cast(str, value)


def _absolute(value: object) -> str:
    """A path the server would otherwise resolve against its own working directory is refused:
    on this platform a drive-relative `C:foo` or a rooted `\\foo` is not absolute either."""
    path = _text(value, MAX_PATH_CHARS)
    if not os.path.isabs(path):
        raise _Refused("invalid_path")
    return path


def _path_uuid(request: Request, name: str) -> str:
    value = request.path_params[name]
    _require(_canonical_uuid(value))
    return value


def _admission(value: dict[str, Any]) -> tuple[int, str]:
    revision = value["policy_revision"]
    physical_id = value["root_physical_id"]
    _require(
        type(revision) is int
        and 1 <= revision <= _MAX_REVISION
        and isinstance(physical_id, str)
        and 3 <= len(physical_id) <= 80
        and _PHYSICAL_ID.fullmatch(physical_id) is not None
    )
    return revision, physical_id


def _claim(value: object) -> str:
    # The grammar refuses CR and every byte bound itself (`invalid_input`); this refuses only
    # what is not text at all.
    _require(isinstance(value, str) and _CONTROL.search(value) is None)
    return cast(str, value)


def _sources(value: object) -> tuple[Source, ...]:
    """Each source's shape; its ref and hash rules, and the count, are the grammar's."""
    _require(isinstance(value, list))
    sources: list[Source] = []
    for item in cast(list[object], value):
        _require(isinstance(item, dict))
        entry = cast(dict[str, object], item)
        _require(all(isinstance(field, str) for field in entry.values()))
        if set(entry) == {"note", "sha256"}:
            sources.append(NoteSource(cast(str, entry["note"]), cast(str, entry["sha256"])))
        else:
            _require(set(entry) == {"session"})
            sources.append(SessionSource(cast(str, entry["session"])))
    return tuple(sources)


def _draft(value: dict[str, Any]) -> tuple[str, str, str, tuple[Source, ...]]:
    kind, scope = value["kind"], value["scope"]
    _require(isinstance(kind, str) and isinstance(scope, str))
    return kind, scope, _claim(value["claim"]), _sources(value["sources"])


def _failure(
    error: _Refused
    | AttachmentError
    | MemoryServiceError
    | BackupError
    | BackupRestoreError
    | PublicationError
    | MutationError,
) -> Response:
    code = error.code
    body: dict[str, Any] = {"code": code, "message": _MESSAGE}
    extra = dict(getattr(error, "extra", {}))
    operation_id = getattr(error, "operation_id", None)
    if operation_id is not None:
        extra["operation_id"] = operation_id
    for key in _EXTRA_KEYS:
        if isinstance(extra.get(key), str):
            body[key] = extra[key]
    return json_response({"error": body}, _STATUS.get(code, 503))


def _backup_response(body: dict[str, Any], status: int = 200) -> Response:
    """Bound public diagnostic prefixes, leaving complete service results and receipts intact."""
    names = tuple(name for name in ("problems", "instruction_files", "skipped") if name in body)
    projected = dict(body)
    for name in names:
        projected[name] = []
        projected[f"{name}_total"] = len(body[name])
    # Include every scalar, list delimiter and total before assigning the shared byte budget.
    remaining = MAX_BACKUP_RESPONSE_BYTES - len(json_response(projected).body)
    for name in names:
        kept: list[Any] = []
        for row in body[name][:MAX_BACKUP_RESPONSE_ROWS]:
            ref = row if isinstance(row, str) else row["ref"]
            if not 1 <= len(ref) <= 512:
                break  # skipped names can precede the manifest's ref admission
            cost = len(json.dumps(row, ensure_ascii=True, separators=(",", ":"))) + bool(kept)
            if cost > remaining:
                break
            kept.append(row)
            remaining -= cost
        projected[name] = kept
    return json_response(projected, status)


def vault_knowledge_routes(
    attachments: VaultAttachmentService,
    memories: MemoryService,
    backups: VaultBackupService,
    restores: BackupRestoreService,
    *,
    authorized: Callable[[Request], bool],
    origins: frozenset[str],
    workers: set[asyncio.Task[Any]],
) -> list[Route]:
    capacity = anyio.CapacityLimiter(MAX_KNOWLEDGE_REQUESTS)

    async def keep(request: Request) -> Response:
        workspace_id = _path_uuid(request, "workspace_id")
        value = _keys(await _json(request), _KEEP_KEYS)
        revision, physical_id = _admission(value)
        # A retry finds the identical file and answers `reused`, so the id has no receipt
        # behind it: checked, then dropped.
        _require(_canonical_uuid(value["command_id"]) and _canonical_uuid(value["asset_id"]))
        _require(isinstance(value["name"], str) and len(value["name"]) <= MAX_NAME_CHARS)
        kept = await attachments.keep(
            workspace_id, revision, physical_id, value["asset_id"], value["name"]
        )
        return json_response(asdict(kept), 200 if kept.reused else 201)

    async def memory_list(request: Request) -> Response:
        workspace_id = _path_uuid(request, "workspace_id")
        _require(not request.scope.get("query_string", b""))
        listing = await run_sync(memories.list, workspace_id)
        return json_response(asdict(listing))

    async def memory_create(request: Request) -> Response:
        workspace_id = _path_uuid(request, "workspace_id")
        value = _keys(await _json(request), _CREATE_KEYS)
        revision, physical_id = _admission(value)
        _require(_canonical_uuid(value["command_id"]))
        kind, scope, claim, sources = _draft(value)
        # A protected vault answers `Proposed` (outcome "proposed"): sealed, nothing written.
        # The command id is the new record's id, so a retry replays: 200, not 201.
        created, replayed = await memories.promote(
            workspace_id,
            revision,
            physical_id,
            command_id=value["command_id"],
            kind=kind,
            scope=scope,
            claim=claim,
            sources=sources,
        )
        return json_response({**asdict(created), "replayed": replayed}, 200 if replayed else 201)

    async def memory_compose(request: Request) -> Response:
        _path_uuid(request, "workspace_id")
        kind, scope, claim, sources = _draft(_keys(await _json(request), _DRAFT_KEYS))
        # The candidate a create would write, with its own id and time; nothing is stored.
        fields = Fields(
            str(uuid.uuid4()), kind, scope, "candidate", stamp(datetime.now(UTC)), sources=sources
        )
        try:
            text = memory_record.compose(fields, claim)
        except ValueError as exc:
            raise MemoryServiceError("invalid_input", reason=str(exc)) from None
        return json_response({"text": text})

    async def memory_patch(request: Request) -> Response:
        workspace_id = _path_uuid(request, "workspace_id")
        memory_id = _path_uuid(request, "memory_id")
        value = await _json(request)
        action = value.get("action")
        if not isinstance(action, str) or action not in _ACTION_KEYS:
            raise _Refused("invalid_request")
        _keys(value, _PATCH_KEYS | _ACTION_KEYS[action])
        revision, physical_id = _admission(value)
        sha256 = value["sha256"]
        _require(isinstance(sha256, str) and _HASH.fullmatch(sha256) is not None)
        if action == "approve":
            outcome: Reviewed | Written | Corrected = await memories.approve(
                workspace_id, memory_id, sha256, revision, physical_id
            )
        elif action == "retire":
            retired_by = value["retired_by"]
            _require(retired_by is None or _canonical_uuid(retired_by))
            outcome = await memories.retire(
                workspace_id, memory_id, sha256, revision, physical_id, retired_by=retired_by
            )
        elif action == "correct":
            claim = _claim(value["claim"])
            outcome = await memories.correct(
                workspace_id, memory_id, sha256, claim, revision, physical_id
            )
        else:
            outcome = await memories.narrow_scope(
                workspace_id, memory_id, sha256, revision, physical_id
            )
        return json_response(asdict(outcome))

    async def backup_preview(request: Request) -> Response:
        workspace_id = _path_uuid(request, "workspace_id")
        value = _keys(await _json(request), frozenset({"destination"}))
        preview = await backups.preview(workspace_id, _absolute(value["destination"]))
        return _backup_response(asdict(preview))

    async def backup_run(request: Request) -> Response:
        workspace_id = _path_uuid(request, "workspace_id")
        value = _keys(await _json(request), frozenset({"command_id", "destination"}))
        _require(_canonical_uuid(value["command_id"]))
        result, replayed = await backups.run(
            value["command_id"], workspace_id, _absolute(value["destination"])
        )
        return _backup_response({**asdict(result), "replayed": replayed}, 200 if replayed else 201)

    async def restore_preview(request: Request) -> Response:
        value = _keys(await _json(request), frozenset({"backup"}))
        preview = await restores.preview(_absolute(value["backup"]))
        return _backup_response(asdict(preview))

    async def restore_apply(request: Request) -> Response:
        value = _keys(await _json(request), frozenset({"command_id", "backup", "parent", "folder"}))
        _require(_canonical_uuid(value["command_id"]))
        result, replayed = await restores.apply(
            value["command_id"],
            _absolute(value["backup"]),
            _absolute(value["parent"]),
            _text(value["folder"], MAX_NAME_CHARS),
        )
        # The registration by id, name and policy only: its root is a path the request named
        # only in parts, so it stays out of the answer.
        workspace = result.workspace
        return _backup_response(
            {
                "workspace": {
                    "workspace_id": workspace.workspace_id,
                    "name": workspace.name,
                    "policy": workspace.policy,
                },
                "files": result.files,
                "bytes": result.bytes,
                "verified": result.verified,
                "problems": [asdict(problem) for problem in result.problems],
                "replayed": replayed,
            },
            200 if replayed else 201,
        )

    async def admitted(handler: _Handler, request: Request) -> Response:
        permit = object()
        try:
            capacity.acquire_on_behalf_of_nowait(permit)
        except anyio.WouldBlock:
            return failure("overloaded", "Vault requests are busy.", 429)
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
        except (
            _Refused,
            AttachmentError,
            MemoryServiceError,
            BackupError,
            BackupRestoreError,
            PublicationError,
            MutationError,
        ) as error:
            return _failure(error)
        except TimeoutError:
            return failure("intake_timeout", "The request timed out.", 408)
        except ClientDisconnect:
            return failure("disconnected", "The request was interrupted.", 400)
        except OSError, RuntimeError:
            return failure("unavailable", _MESSAGE, 503)

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

    vault = "/api/v2/vaults/{workspace_id}"
    return [
        Route(f"{vault}/attachments", guarded(keep), methods=["POST"]),
        Route(f"{vault}/memories", guarded(memory_list), methods=["GET"]),
        Route(f"{vault}/memories", guarded(memory_create), methods=["POST"]),
        Route(f"{vault}/memories/compose", guarded(memory_compose), methods=["POST"]),
        Route(f"{vault}/memories/{{memory_id}}", guarded(memory_patch), methods=["PATCH"]),
        Route(f"{vault}/backup/preview", guarded(backup_preview), methods=["POST"]),
        Route(f"{vault}/backup", guarded(backup_run), methods=["POST"]),
        Route("/api/v2/backup-restore/preview", guarded(restore_preview), methods=["POST"]),
        Route("/api/v2/backup-restore", guarded(restore_apply), methods=["POST"]),
    ]
