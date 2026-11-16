"""Authenticated, admission-bounded routes for a code folder: files, Git and saves."""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from collections.abc import Callable
from dataclasses import asdict
from typing import Any

import anyio
from anyio.to_thread import run_sync
from starlette.requests import ClientDisconnect, Request
from starlette.responses import Response
from starlette.routing import Route

from viki_agent.api_json import failure, json_response, unique_object
from viki_agent.code.code_diff import diff
from viki_agent.code.code_files import CodeError, CodeFiles
from viki_agent.code.code_git import CodeGit
from viki_agent.code.code_save import CodeSaver
from viki_agent.code.code_search import search
from viki_agent.vaults.vault import VaultError
from viki_agent.vaults.vault_api import _decode_query_value
from viki_agent.vaults.vault_mutation import MAX_PUBLICATION_BYTES, MutationError
from viki_agent.vaults.vault_publication import PublicationError

MAX_CODE_REQUESTS = 4
# JSON may escape one Unicode code point as six ASCII bytes; bound the envelope while still
# accepting every valid 2 MiB string.
MAX_SAVE_BYTES = 6 * MAX_PUBLICATION_BYTES + 4096
_HASH = re.compile(r"[0-9a-f]{64}")
_PHYSICAL_ID = re.compile(r"[0-9]+:[0-9]+")
_ENCODINGS = ("utf-8", "utf-8-bom", "utf-16-le", "utf-16-be")
_NEWLINES = ("lf", "crlf", "cr", "none")
_SAVE_FIELDS = {
    "policy_revision",
    "root_physical_id",
    "content",
    "expected_sha256",
    "encoding",
    "newline",
}
# A ref is at most 1,024 characters and a percent-encoded CJK one is 9 bytes each.
MAX_QUERY_BYTES = 16_384

_STATUS = {
    "invalid_path": 400,
    "invalid_query": 400,
    "not_a_file": 400,
    "not_a_directory": 400,
    "private_path": 403,
    "unknown_workspace": 404,
    "not_found": 404,
    "trust_required": 409,
    "root_unavailable": 409,
    "not_repository": 409,
    "not_repository_root": 409,
    "git_settings_changed": 409,
    "too_large": 413,
}
# The one refusal every code-folder route family answers when the folder's Git settings left
# the baseline a Bot copy was made with.
GIT_SETTINGS_CHANGED = (
    "This folder's Git settings or hooks changed after a Bot copy was made, so VIKI Agent "
    "stopped its own Git actions here. Undo the change, or confirm the current settings in the "
    "Worktrees tab."
)
_MESSAGES = {
    "invalid_path": "Check the file's location.",
    "invalid_query": "Check your search term.",
    "not_a_file": "This isn't a file.",
    "not_a_directory": "This isn't a folder.",
    "private_path": "This location can't be shown.",
    "unknown_workspace": "Can't find the code folder.",
    "not_found": "Can't find the file.",
    "trust_required": "Trust this folder, then try again.",
    "root_unavailable": "Can't read the code folder. Check the connection.",
    "not_repository": "This isn't a Git repository.",
    "not_repository_root": "This isn't the Git repository's root.",
    "too_large": "The search scope is too large.",
    "git_unavailable": "Git isn't available.",
    "git_settings_changed": GIT_SETTINGS_CHANGED,
}


def _error(exc: CodeError) -> Response:
    return failure(
        exc.code,
        _MESSAGES.get(exc.code, "Couldn't read the code folder."),
        _STATUS.get(exc.code, 503),
    )


def _query_value(request: Request, key: bytes | None, *, required: bool) -> str:
    """The one value of `key`, `""` when an optional key is absent. Nothing but `key=value` is
    allowed: no other key, no repeat and no bare `key`."""
    raw = request.scope.get("query_string", b"")
    if len(raw) > MAX_QUERY_BYTES:
        raise CodeError("invalid_query")
    if not raw:
        if required:
            raise CodeError("invalid_query")
        return ""
    name, equals, value = raw.partition(b"=")
    if key is None or name != key or not equals or b"&" in value:
        raise CodeError("invalid_query")
    try:
        return _decode_query_value(value)
    except VaultError:
        raise CodeError("invalid_query") from None


def _canonical(workspace_id: str) -> bool:
    try:
        return str(uuid.UUID(workspace_id)) == workspace_id
    except ValueError:
        return False


def code_routes(
    files: CodeFiles,
    git: CodeGit,
    *,
    authorized: Callable[[Request], bool],
    origins: frozenset[str],
    workers: set[asyncio.Task[Any]],
) -> list[Route]:
    capacity = anyio.CapacityLimiter(MAX_CODE_REQUESTS)

    def handler(
        key: bytes | None, *, required: bool, call: Callable[[str, str], Any]
    ) -> Callable[[Request], Any]:
        async def perform(workspace_id: str, value: str) -> Response:
            return json_response(asdict(await run_sync(call, workspace_id, value)))

        async def endpoint(request: Request) -> Response:
            if not authorized(request):
                return failure("unauthorized", "Can't authenticate. Open VIKI Agent again.", 401)
            origin = request.headers.get("origin")
            if origin is not None and origin not in origins:
                return failure("forbidden_origin", "This window can't show that.", 403)
            workspace_id = request.path_params["workspace_id"]
            if not _canonical(workspace_id):
                return _error(CodeError("unknown_workspace"))
            try:
                value = _query_value(request, key, required=required)
            except CodeError as exc:
                return _error(exc)
            permit = object()
            try:
                capacity.acquire_on_behalf_of_nowait(permit)
            except anyio.WouldBlock:
                return failure("overloaded", "Busy right now. Wait a moment, then try again.", 429)
            worker = asyncio.create_task(perform(workspace_id, value))
            workers.add(worker)

            def done(task: asyncio.Task[Response]) -> None:
                workers.discard(task)
                capacity.release_on_behalf_of(permit)
                if not task.cancelled():
                    task.exception()

            worker.add_done_callback(done)
            try:
                return await asyncio.shield(worker)
            except CodeError as exc:
                return _error(exc)

        return endpoint

    base = "/api/v2/code/{workspace_id}"
    return [
        Route(
            f"{base}/tree",
            handler(b"path", required=False, call=files.tree),
            methods=["GET"],
        ),
        Route(
            f"{base}/file",
            handler(b"path", required=True, call=files.read),
            methods=["GET"],
        ),
        Route(
            f"{base}/search",
            handler(b"q", required=True, call=lambda id_, q: search(git, id_, q)),
            methods=["GET"],
        ),
        Route(
            f"{base}/changes",
            handler(None, required=False, call=lambda id_, _: git.changes(id_)),
            methods=["GET"],
        ),
        Route(
            f"{base}/diff",
            handler(b"path", required=True, call=lambda id_, ref: diff(git, files, id_, ref)),
            methods=["GET"],
        ),
    ]


# --- the save route -----------------------------------------------------------------------------

# The saver passes through the vault writer's codes; a code folder names four of them its own way.
_SAVE_RENAMES = {
    "invalid_ref": "invalid_path",
    "excluded_path": "private_path",
    "ordinary_workspace_required": "trust_required",
    "stale_root": "root_unavailable",
}
_SAVE_STATUS = {
    code: status
    for status, codes in {
        400: "invalid_path invalid_query invalid_body invalid_content disconnected",
        403: "forbidden_origin private_path",
        404: "unknown_workspace",
        408: "intake_timeout",
        409: "trust_required workspace_changed unsupported_filesystem read_only_target "
        "unsafe_target unsafe_parent missing_target publication_busy vault_recovery_required "
        "root_unavailable",
        413: "too_large",
        423: "target_busy parent_busy stage_busy vault_locked",
        429: "overloaded publication_limit",
    }.items()
    for code in codes.split()
}
_BUSY = "Another save is in progress. Wait a moment, then try again."
_NOT_SAVABLE = "Can't save to this location."
_SAVE_MESSAGES = {
    "invalid_path": "Check the file's location.",
    "invalid_query": "Check the file's location.",
    "invalid_body": "Check what you're saving.",
    "invalid_content": "This content can't be saved.",
    "disconnected": "The connection was interrupted. Try again.",
    "forbidden_origin": "This window can't save that.",
    "private_path": _NOT_SAVABLE,
    "unsafe_target": _NOT_SAVABLE,
    "unsafe_parent": _NOT_SAVABLE,
    "unknown_workspace": "Can't find the code folder.",
    "intake_timeout": "The connection timed out. Try again.",
    "trust_required": "Trust this folder, then try again.",
    "workspace_changed": "The code folder's settings changed. Reopen it, then try again.",
    "unsupported_filesystem": "Code can't be saved in this environment.",
    "read_only_target": "This file is read-only. Clear that attribute, then try again.",
    "missing_target": "Can't find the file to save to.",
    "root_unavailable": "Can't read the code folder. Check the connection.",
    "publication_busy": _BUSY,
    "target_busy": _BUSY,
    "parent_busy": _BUSY,
    "stage_busy": _BUSY,
    "vault_locked": _BUSY,
    "vault_recovery_required": "Another operation needs recovery first.",
    "recovery_required": "The last save needs recovery first.",
    "too_large": "The file is too large.",
    "overloaded": "Busy right now. Wait a moment, then try again.",
    "publication_limit": "Busy right now. Wait a moment, then try again.",
}


def _refusal(code: str, operation_id: str | None = None) -> Response:
    code = _SAVE_RENAMES.get(code, code)
    error = {"code": code, "message": _SAVE_MESSAGES.get(code, "Couldn't save.")}
    if operation_id is not None:
        error["operation_id"] = operation_id
    return json_response({"error": error}, _SAVE_STATUS.get(code, 503))


async def _save_body(request: Request) -> dict[str, Any]:
    """The request's one JSON object, with every field checked."""
    if request.headers.get("content-type", "").split(";", 1)[
        0
    ].strip().lower() != "application/json" or any(
        value.strip().lower() != "identity" for value in request.headers.getlist("content-encoding")
    ):
        raise CodeError("invalid_body")
    encoded = bytearray()
    with anyio.fail_after(5):
        async for chunk in request.stream():
            if len(chunk) > MAX_SAVE_BYTES - len(encoded):
                raise CodeError("too_large")
            encoded.extend(chunk)
    try:
        value = json.loads(encoded.decode("utf-8"), object_pairs_hook=unique_object)
    except ValueError, UnicodeError, RecursionError:
        raise CodeError("invalid_body") from None
    if not isinstance(value, dict) or set(value) != _SAVE_FIELDS:
        raise CodeError("invalid_body")
    revision, physical_id, expected = (
        value["policy_revision"],
        value["root_physical_id"],
        value["expected_sha256"],
    )
    if (
        type(revision) is not int
        or not 1 <= revision <= 9_007_199_254_740_991
        or not isinstance(physical_id, str)
        or not 3 <= len(physical_id) <= 80
        or _PHYSICAL_ID.fullmatch(physical_id) is None
        or not isinstance(value["content"], str)
        or expected is not None
        and (not isinstance(expected, str) or _HASH.fullmatch(expected) is None)
        or value["encoding"] not in _ENCODINGS
        or value["newline"] not in _NEWLINES
    ):
        raise CodeError("invalid_body")
    return value


def code_save_routes(
    saver: CodeSaver,
    *,
    authorized: Callable[[Request], bool],
    origins: frozenset[str],
    workers: set[asyncio.Task[Any]],
) -> list[Route]:
    capacity = anyio.CapacityLimiter(MAX_CODE_REQUESTS)

    async def perform(request: Request, workspace_id: str, ref: str) -> Response:
        body = await _save_body(request)
        outcome = await saver.save(
            workspace_id,
            body["policy_revision"],
            body["root_physical_id"],
            ref,
            body["content"],
            encoding=body["encoding"],
            newline=body["newline"],
            expected_sha256=body["expected_sha256"],
        )
        return json_response(asdict(outcome), 409 if outcome.status == "conflict" else 200)

    async def admitted(request: Request) -> Response:
        origin = request.headers.get("origin")
        if origin not in origins:
            return _refusal("forbidden_origin")
        workspace_id = request.path_params["workspace_id"]
        if not _canonical(workspace_id):
            return _refusal("unknown_workspace")
        try:
            ref = _query_value(request, b"path", required=True)
        except CodeError as error:
            return _refusal(error.code)
        permit = object()
        try:
            capacity.acquire_on_behalf_of_nowait(permit)
        except anyio.WouldBlock:
            return _refusal("overloaded")
        worker = asyncio.create_task(perform(request, workspace_id, ref))
        workers.add(worker)

        def done(task: asyncio.Task[Response]) -> None:
            workers.discard(task)
            capacity.release_on_behalf_of(permit)
            if not task.cancelled():
                task.exception()

        worker.add_done_callback(done)
        try:
            return await asyncio.shield(worker)
        except PublicationError as error:
            return _refusal(error.code, error.operation_id)
        except (CodeError, MutationError) as error:
            return _refusal(error.code)
        except TimeoutError:
            return _refusal("intake_timeout")
        except ClientDisconnect:
            return _refusal("disconnected")
        except OSError, RuntimeError:
            return _refusal("unavailable")

    async def endpoint(request: Request) -> Response:
        if authorized(request):
            response = await admitted(request)
        else:
            response = failure("unauthorized", "Can't authenticate. Open VIKI Agent again.", 401)
        response.headers["Cache-Control"] = "no-store"
        return response

    return [Route("/api/v2/code/{workspace_id}/file", endpoint, methods=["PUT"])]
