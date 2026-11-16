"""Authenticated, admission-bounded routes for session roots and worktrees.

The POST that records a session's root only records: the spawn check decides. The Git-settings
GET answers only the stored baseline's fingerprint; its POST replaces exactly the baseline the
user reviewed with the current settings' fingerprint, and never creates one. Every answer
carries `Cache-Control: no-store`, and a refusal holds a fixed message, never a path, a branch name,
an id or an exception's text.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable, Coroutine, Mapping, Sequence
from dataclasses import asdict
from typing import Any

import anyio
from anyio.to_thread import run_sync
from starlette.requests import ClientDisconnect, Request
from starlette.responses import Response
from starlette.routing import Route

from viki_agent.api_json import json_response, unique_object
from viki_agent.code.code_api import _HASH, _PHYSICAL_ID, GIT_SETTINGS_CHANGED, _canonical
from viki_agent.code.code_checkout import GitSettings
from viki_agent.code.code_files import CodeError
from viki_agent.code.session_roots import MAX_ROOT_ROWS, RootError, SessionRoots
from viki_agent.code.worktrees import WorktreeError, WorktreeService
from viki_agent.storage import StateError
from viki_agent.vaults.vault_mutation import MutationError

MAX_ROOT_REQUESTS = 4
MAX_BODY_BYTES = 4096
MAX_WORKTREE_ROWS = 256
_LOGGER = logging.getLogger(__name__)

_RENAMED = {
    "ordinary_workspace_required": "trust_required",
    "stale_root": "root_unavailable",
    "git_common_unsafe": "git_common_unavailable",
}
_STATUS = {
    code: status
    for status, codes in {
        400: "invalid_body invalid_query invalid_branch disconnected",
        401: "unauthorized",
        403: "forbidden_origin",
        404: "unknown_workspace unknown_worktree",
        408: "intake_timeout",
        409: "trust_required workspace_changed root_unavailable not_repository "
        "not_repository_root no_commits branch_exists worktree_limit worktree_not_ready "
        "worktree_busy worktree_in_use session_exists unsupported_filesystem "
        "git_common_unavailable no_archive branch_moved branch_in_use bot_copy_root "
        "git_settings_changed baseline_changed",
        413: "too_large",
        423: "vault_locked",
        429: "overloaded",
        503: "git_unavailable git_failed git_settings_unavailable unavailable",
    }.items()
    for code in codes.split()
}
_GIT = "Git couldn't complete this. Try again in a moment."
_NOT_READY = "This worktree isn't available right now."
_MESSAGES = {
    "invalid_body": "Check what you sent.",
    "invalid_query": "Check your request.",
    "invalid_branch": "Branch names can use letters, numbers, and . _ - / (up to 100 characters).",
    "disconnected": "The connection was interrupted. Try again.",
    "unauthorized": "Can't authenticate. Open VIKI Agent again.",
    "forbidden_origin": "This window can't do that.",
    "unknown_workspace": "Can't find the code folder.",
    "unknown_worktree": "Can't find the worktree.",
    "intake_timeout": "The connection timed out. Try again.",
    "trust_required": "Trust this folder, then try again.",
    "workspace_changed": "The folder's state changed. Reopen it, then try again.",
    "root_unavailable": "Can't read the folder. Check its location.",
    "not_repository": "This isn't a Git repository.",
    "not_repository_root": "This isn't the repository's top-level folder.",
    "no_commits": "This folder has no commits yet.",
    "branch_exists": "A branch with this name already exists. Choose a different name.",
    "worktree_limit": "You've reached the worktree limit. Remove one first.",
    "worktree_not_ready": _NOT_READY,
    "worktree_busy": _NOT_READY,
    "worktree_in_use": "A session is running in this worktree. Close it first.",
    "session_exists": "This session's folder is already set.",
    "bot_copy_root": "This worktree belongs to a Bot chat. Open that chat instead.",
    "unsupported_filesystem": "Worktrees aren't supported in this environment.",
    "git_common_unavailable": _GIT,
    "no_archive": "This worktree has no archive to restore.",
    "branch_moved": "Its branch has moved since the archive. Restore it with Git by hand.",
    "branch_in_use": "Its branch is checked out in another worktree.",
    "git_unavailable": _GIT,
    "git_failed": _GIT,
    "git_settings_changed": GIT_SETTINGS_CHANGED,
    "baseline_changed": "These settings changed in another window. Review them again.",
    "git_settings_unavailable": "VIKI Agent can't read this folder's Git settings safely.",
    "too_large": "What you sent is too large.",
    "vault_locked": "The folder is in use. Wait a moment, then try again.",
    "overloaded": "Busy right now. Wait a moment, then try again.",
    "unavailable": "Couldn't complete the operation. Try again.",
}


def _refusal(code: str) -> Response:
    code = _RENAMED.get(code, code)
    if code not in _STATUS:
        code = "unavailable"
    return json_response({"error": {"code": code, "message": _MESSAGES[code]}}, _STATUS[code])


def _uuid(value: object) -> bool:
    return isinstance(value, str) and _canonical(value)


def _uuid_or_null(value: object) -> bool:
    return value is None or _uuid(value)


def _revision(value: object) -> bool:
    return type(value) is int and 1 <= value <= 9_007_199_254_740_991


def _physical_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and 3 <= len(value) <= 80
        and _PHYSICAL_ID.fullmatch(value) is not None
    )


def _branch(value: object) -> bool:
    return isinstance(value, str) and 1 <= len(value) <= 100


def _fingerprint(value: object) -> bool:
    return isinstance(value, str) and _HASH.fullmatch(value) is not None


_ROOT_BODY = {"session_id": _uuid, "workspace_id": _uuid, "worktree_id": _uuid_or_null}
_CREATE_BODY = {"policy_revision": _revision, "root_physical_id": _physical_id, "branch": _branch}
_REMOVE_BODY = {"policy_revision": _revision, "root_physical_id": _physical_id}
_CONFIRM_BODY = {**_REMOVE_BODY, "expected_fingerprint": _fingerprint}


async def _body(
    request: Request, shape: dict[str, Callable[[Any], bool]], *, limit: int = MAX_BODY_BYTES
) -> dict[str, Any]:
    """The request's one JSON object, at most `limit` bytes, with the required keys of `shape`.
    Only root requests may also carry a valid additional-workspace list."""
    if request.headers.get("content-type", "").split(";", 1)[
        0
    ].strip().lower() != "application/json" or any(
        value.strip().lower() != "identity" for value in request.headers.getlist("content-encoding")
    ):
        raise CodeError("invalid_body")
    encoded = bytearray()
    try:
        with anyio.fail_after(5):
            async for chunk in request.stream():
                if len(chunk) > limit - len(encoded):
                    raise CodeError("too_large")
                encoded.extend(chunk)
    except TimeoutError:
        raise CodeError("intake_timeout") from None
    except ClientDisconnect:
        raise CodeError("disconnected") from None
    try:
        value = json.loads(encoded.decode("utf-8"), object_pairs_hook=unique_object)
    except ValueError, RecursionError:
        raise CodeError("invalid_body") from None
    if (
        not isinstance(value, dict)
        or (value.keys() - {"additional_workspace_ids"} if shape is _ROOT_BODY else value.keys())
        != shape.keys()
        or not all(valid(value[key]) for key, valid in shape.items())
    ):
        raise CodeError("invalid_body")
    if shape is _ROOT_BODY and "additional_workspace_ids" in value:
        additional = value["additional_workspace_ids"]
        if (
            not isinstance(additional, list)
            or len(additional) > 7
            or not all(_uuid(workspace_id) for workspace_id in additional)
            or len(set(additional)) != len(additional)
            or value["workspace_id"] in additional
            or (additional and value["worktree_id"] is not None)
        ):
            raise CodeError("invalid_body")
    return value


def root_routes(
    roots: SessionRoots,
    worktrees: WorktreeService,
    *,
    authorized: Callable[[Request], bool],
    origins: frozenset[str],
    workers: set[asyncio.Task[Any]],
    describe: Callable[[Sequence[str]], Mapping[str, Mapping[str, object]]] | None = None,
    git_settings: GitSettings | None = None,
) -> list[Route]:
    """`git_settings`, when given, adds the folder's Git-settings baseline read and confirm."""
    capacity = anyio.CapacityLimiter(MAX_ROOT_REQUESTS)

    async def list_roots(_: Request) -> Response:
        found, truncated = await run_sync(roots.list, MAX_ROOT_ROWS)
        projected = [
            {
                key: value
                for key, value in asdict(root).items()
                if key != "additional_workspace_ids" or value
            }
            for root in found
        ]
        return json_response({"roots": projected, "truncated": truncated})

    async def record_root(request: Request) -> Response:
        body = await _body(request, _ROOT_BODY)
        args = (body["session_id"], body["workspace_id"], body["worktree_id"])
        additional = body.get("additional_workspace_ids", [])
        if additional:
            root, created = await roots.prepare(*args, tuple(additional))
        else:
            root, created = await roots.prepare(*args)
        projected = {
            key: value
            for key, value in asdict(root).items()
            if key != "additional_workspace_ids" or value
        }
        return json_response({"root": projected}, 201 if created else 200)

    def project_worktrees() -> dict[str, Any]:
        found, truncated = worktrees.list(MAX_WORKTREE_ROWS)
        descriptions = describe([worktree.worktree_id for worktree in found]) if describe else {}
        projected = []
        for worktree in found:
            row = asdict(worktree)
            if (description := descriptions.get(worktree.worktree_id)) is not None:
                row["bot_copy"] = dict(description)
            projected.append(row)
        return {"worktrees": projected, "truncated": truncated}

    async def list_worktrees(_: Request) -> Response:
        return json_response(await run_sync(project_worktrees))

    async def create_worktree(request: Request) -> Response:
        body = await _body(request, _CREATE_BODY)
        worktree = await worktrees.create(
            request.path_params["workspace_id"],
            body["policy_revision"],
            body["root_physical_id"],
            body["branch"],
        )
        return json_response({"worktree": asdict(worktree)}, 201)

    async def remove_worktree(request: Request) -> Response:
        body = await _body(request, _REMOVE_BODY)
        removal = await worktrees.remove(
            request.path_params["worktree_id"], body["policy_revision"], body["root_physical_id"]
        )
        return json_response(
            {
                "worktree": asdict(removal.worktree),
                "outcome": removal.outcome,
                "reason": removal.reason,
                "changes": removal.changes,
            }
        )

    async def list_cleanup(request: Request) -> Response:
        decisions = await worktrees.plan(request.path_params["workspace_id"])
        return json_response({"decisions": [asdict(decision) for decision in decisions]})

    async def list_archives(request: Request) -> Response:
        workspace_id = request.path_params["workspace_id"]
        enabled = await run_sync(worktrees.cleanup_enabled)
        found = await run_sync(worktrees.archives, workspace_id)
        return json_response(
            {"enabled": enabled, "archives": [asdict(archive) for archive in found]}
        )

    async def restore_worktree(request: Request) -> Response:
        body = await _body(request, _REMOVE_BODY)
        worktree = await worktrees.restore(
            request.path_params["worktree_id"], body["policy_revision"], body["root_physical_id"]
        )
        return json_response({"worktree": asdict(worktree)})

    async def read_git_settings(request: Request) -> Response:
        assert git_settings is not None
        saved = await run_sync(git_settings.saved, request.path_params["workspace_id"])
        return json_response({"fingerprint": saved})

    async def confirm_git_settings(request: Request) -> Response:
        assert git_settings is not None
        body = await _body(request, _CONFIRM_BODY)
        confirmed = await git_settings.confirm(
            request.path_params["workspace_id"],
            body["policy_revision"],
            body["root_physical_id"],
            body["expected_fingerprint"],
        )
        return json_response({"fingerprint": confirmed})

    def endpoint(
        perform: Callable[[Request], Coroutine[Any, Any, Response]],
        *,
        post: bool,
        path_id: tuple[str, str] | None = None,
    ) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        """`path_id` names the path parameter that must be a canonical UUID, and the code
        answered when it is not."""

        async def admitted(request: Request) -> Response:
            origin = request.headers.get("origin")
            if origin not in origins and (post or origin is not None):
                return _refusal("forbidden_origin")
            if path_id is not None and not _canonical(request.path_params[path_id[0]]):
                return _refusal(path_id[1])
            if request.scope.get("query_string"):
                return _refusal("invalid_query")
            permit = object()
            try:
                capacity.acquire_on_behalf_of_nowait(permit)
            except anyio.WouldBlock:
                return _refusal("overloaded")
            # A task of its own, shielded: a client that leaves does not cancel a create or a
            # remove partway.
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
            except (RootError, WorktreeError, CodeError, MutationError) as error:
                return _refusal(error.code)
            except StateError:
                return _refusal("unavailable")
            except Exception as error:
                _LOGGER.warning("roots route failed (%s)", type(error).__name__)
                return _refusal("unavailable")

        async def handler(request: Request) -> Response:
            response = await admitted(request) if authorized(request) else _refusal("unauthorized")
            response.headers["Cache-Control"] = "no-store"
            return response

        return handler

    workspace = ("workspace_id", "unknown_workspace")
    settings_routes = (
        []
        if git_settings is None
        else [
            Route(
                "/api/v2/code/{workspace_id}/git-settings",
                endpoint(read_git_settings, post=False, path_id=workspace),
                methods=["GET"],
            ),
            Route(
                "/api/v2/code/{workspace_id}/git-settings",
                endpoint(confirm_git_settings, post=True, path_id=workspace),
                methods=["POST"],
            ),
        ]
    )
    return [
        *settings_routes,
        Route("/api/v2/session-roots", endpoint(list_roots, post=False), methods=["GET"]),
        Route("/api/v2/session-roots", endpoint(record_root, post=True), methods=["POST"]),
        Route("/api/v2/worktrees", endpoint(list_worktrees, post=False), methods=["GET"]),
        Route(
            "/api/v2/code/{workspace_id}/worktrees",
            endpoint(create_worktree, post=True, path_id=("workspace_id", "unknown_workspace")),
            methods=["POST"],
        ),
        Route(
            "/api/v2/code/{workspace_id}/worktree-cleanup",
            endpoint(list_cleanup, post=False, path_id=("workspace_id", "unknown_workspace")),
            methods=["GET"],
        ),
        Route(
            "/api/v2/code/{workspace_id}/worktree-archives",
            endpoint(list_archives, post=False, path_id=("workspace_id", "unknown_workspace")),
            methods=["GET"],
        ),
        Route(
            "/api/v2/worktrees/{worktree_id}/remove",
            endpoint(remove_worktree, post=True, path_id=("worktree_id", "unknown_worktree")),
            methods=["POST"],
        ),
        Route(
            "/api/v2/worktrees/{worktree_id}/restore",
            endpoint(restore_worktree, post=True, path_id=("worktree_id", "unknown_worktree")),
            methods=["POST"],
        ),
    ]
