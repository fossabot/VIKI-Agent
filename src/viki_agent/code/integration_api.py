"""Authenticated, admission-bounded routes for review, commit, integrate and push.

Every route resolves trust in its service before any Git call. Commit, integrate and push hold the
mutation lease while their Git work runs in a thread, so each service call runs in a shielded task
of its own: a client that leaves does not release the lease under a running merge or push. Every
answer carries `Cache-Control: no-store`, and a refusal holds a fixed message, never a path, a
branch, a URL, a token, an id or an exception's text.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from dataclasses import asdict
from functools import partial
from typing import Any

import anyio
from anyio.to_thread import run_sync
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from viki_agent.api_json import json_response
from viki_agent.code import roots_api
from viki_agent.code.code_api import _canonical
from viki_agent.code.code_commit import MAX_COMMIT_PATHS, MAX_MESSAGE_CHARS, CodeCommitter
from viki_agent.code.code_files import MAX_REF_CHARS, CodeError
from viki_agent.code.code_integration import CodeIntegration
from viki_agent.code.code_range import OID
from viki_agent.code.code_remote import REMOTE_NAME
from viki_agent.code.code_review import CodeReview
from viki_agent.code.roots_api import _body, _branch, _physical_id, _revision, _uuid_or_null
from viki_agent.storage import StateError
from viki_agent.vaults.vault import VaultError
from viki_agent.vaults.vault_api import _decode_query_value

MAX_INTEGRATION_REQUESTS = 4
MAX_BODY_BYTES = 262_144
MAX_QUERY_BYTES = 16_384
_LOGGER = logging.getLogger(__name__)

_STATUS = {
    code: status
    for status, codes in {
        400: "invalid_body invalid_query invalid_path invalid_message too_many_paths not_a_file "
        "disconnected",
        401: "unauthorized",
        403: "forbidden_origin private_path",
        404: "unknown_workspace unknown_worktree not_found",
        408: "intake_timeout",
        409: "unsupported_branch trust_required workspace_changed root_unavailable "
        "not_repository not_repository_root no_commits worktree_not_ready worktree_moved "
        "unsupported_filesystem git_common_unavailable stale_review stale_base unrelated_staged "
        "in_progress detached_head detached_target identity_missing unsupported_path "
        "unsupported_change commit_refused checkout_busy dirty_target not_fast_forward "
        "up_to_date merge_refused merge_failed no_remote remote_unsupported push_rejected "
        "git_settings_changed",
        413: "too_large",
        423: "vault_locked",
        429: "overloaded",
        503: "git_unavailable git_failed push_failed unavailable",
    }.items()
    for code in codes.split()
}
_MESSAGES = {
    **{code: roots_api._MESSAGES[code] for code in _STATUS if code in roots_api._MESSAGES},
    "invalid_path": "Check the file's location.",
    "invalid_message": "Write a commit message of 1 to 4,000 characters.",
    "too_many_paths": "You can commit at most 200 files at a time.",
    "not_a_file": "This isn't a file.",
    "not_found": "Can't find the file.",
    "private_path": "This file can't be handled.",
    "unsupported_branch": "VIKI can't use this branch name.",
    "worktree_moved": "This worktree's state changed.",
    "unsupported_filesystem": "Commit, integrate and push aren't supported in this environment.",
    "stale_review": "This changed since you reviewed it. Review it again.",
    "stale_base": "The folder's branch moved since you reviewed it. Review it again.",
    "unrelated_staged": "Other files are already staged.",
    "in_progress": "An operation such as a merge is in progress.",
    "detached_head": "No branch is checked out.",
    "detached_target": "No branch is checked out in the folder.",
    "identity_missing": "Git has no name and email address set.",
    "unsupported_path": "This includes a file VIKI can't commit.",
    "unsupported_change": "This includes a file change VIKI can't review.",
    "commit_refused": "Git or a hook refused the commit.",
    "checkout_busy": "A session is working in this folder.",
    "dirty_target": "The folder has changes that aren't committed.",
    "not_fast_forward": "Can't integrate by fast-forward.",
    "up_to_date": "There's nothing to integrate.",
    "merge_refused": "Git refused the fast-forward. Nothing changed.",
    "merge_failed": "The fast-forward stopped partway.",
    "no_remote": "There's no remote.",
    "remote_unsupported": "VIKI can't push to this remote.",
    "push_rejected": "The remote rejected the push.",
    "push_failed": "Couldn't connect to the remote.",
}


def _refusal(code: str) -> Response:
    if code not in _STATUS:
        code = "unavailable"
    return json_response({"error": {"code": code, "message": _MESSAGES[code]}}, _STATUS[code])


def _query(
    request: Request, allowed: frozenset[bytes], required: frozenset[bytes]
) -> dict[str, str]:
    """Each listed key at most once as `key=value`, the required ones present, nothing else."""
    raw: bytes = request.scope.get("query_string", b"")
    if len(raw) > MAX_QUERY_BYTES:
        raise CodeError("invalid_query")
    fields: dict[str, str] = {}
    for pair in raw.split(b"&") if raw else ():
        key, equals, value = pair.partition(b"=")
        if key not in allowed or not equals or key.decode() in fields:
            raise CodeError("invalid_query")
        try:
            fields[key.decode()] = _decode_query_value(value)
        except VaultError:
            raise CodeError("invalid_query") from None
    if not required <= {key.encode() for key in fields}:
        raise CodeError("invalid_query")
    if "worktree" in fields and not _canonical(fields["worktree"]):
        raise CodeError("invalid_query")
    if "range" in fields and (fields["range"] != "integration" or "worktree" not in fields):
        raise CodeError("invalid_query")
    return fields


def _oid(value: object) -> bool:
    return isinstance(value, str) and OID.fullmatch(value) is not None


def _message(value: object) -> bool:
    return isinstance(value, str) and 1 <= len(value) <= MAX_MESSAGE_CHARS


def _digest(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and OID.fullmatch(value) is not None


def _approved(value: object) -> bool:
    return (
        isinstance(value, dict)
        and value.keys() == {"path", "digest"}
        and isinstance(path := value["path"], str)
        and 1 <= len(path) <= MAX_REF_CHARS
        and _digest(value["digest"])
    )


def _paths(value: object) -> bool:
    return (
        isinstance(value, list)
        and 1 <= len(value) <= MAX_COMMIT_PATHS
        and all(_approved(item) for item in value)
        and len({item["path"] for item in value}) == len(value)
    )


def _remote(value: object) -> bool:
    return isinstance(value, str) and REMOTE_NAME.fullmatch(value) is not None


_COMMIT_BODY = {
    "policy_revision": _revision,
    "root_physical_id": _physical_id,
    "worktree_id": _uuid_or_null,
    "branch": _branch,
    "head_oid": _oid,
    "message": _message,
    "paths": _paths,
}
_INTEGRATE_BODY = {
    "policy_revision": _revision,
    "root_physical_id": _physical_id,
    "target_branch": _branch,
    "target_oid": _oid,
    "head_oid": _oid,
}
_PUSH_BODY = {
    "policy_revision": _revision,
    "root_physical_id": _physical_id,
    "remote": _remote,
    "branch": _branch,
    "head_oid": _oid,
}
_CHECK_BODY = {"remote": _remote, "branch": _branch, "head_oid": _oid}
_REVIEW_KEYS = frozenset({b"worktree"})
_VIEW_KEYS = frozenset({b"path", b"worktree", b"range"})


def integration_routes(
    review: CodeReview,
    committer: CodeCommitter,
    integration: CodeIntegration,
    *,
    authorized: Callable[[Request], bool],
    origins: frozenset[str],
    workers: set[asyncio.Task[Any]],
) -> list[Route]:
    capacity = anyio.CapacityLimiter(MAX_INTEGRATION_REQUESTS)

    async def get_review(request: Request) -> Response:
        query = _query(request, _REVIEW_KEYS, frozenset())
        found = await run_sync(
            review.review, request.path_params["workspace_id"], query.get("worktree")
        )
        return json_response(asdict(found))

    async def get_view(request: Request) -> Response:
        query = _query(request, _VIEW_KEYS, frozenset({b"path"}))
        shown = await run_sync(
            partial(
                review.view,
                request.path_params["workspace_id"],
                query["path"],
                query.get("worktree"),
                integration="range" in query,
            )
        )
        return json_response(asdict(shown))

    async def post_commit(request: Request) -> Response:
        body = await _body(request, _COMMIT_BODY, limit=MAX_BODY_BYTES)
        result = await committer.commit(
            request.path_params["workspace_id"],
            body["worktree_id"],
            body["policy_revision"],
            body["root_physical_id"],
            body["branch"],
            body["head_oid"],
            body["message"],
            [(item["path"], item["digest"]) for item in body["paths"]],
        )
        return json_response({"outcome": "committed", **asdict(result)})

    async def post_integrate(request: Request) -> Response:
        body = await _body(request, _INTEGRATE_BODY, limit=MAX_BODY_BYTES)
        head_oid = await integration.integrate(
            request.path_params["worktree_id"],
            body["policy_revision"],
            body["root_physical_id"],
            body["target_branch"],
            body["target_oid"],
            body["head_oid"],
        )
        return json_response({"outcome": "integrated", "head_oid": head_oid})

    async def post_push(request: Request) -> Response:
        body = await _body(request, _PUSH_BODY, limit=MAX_BODY_BYTES)
        pushed = await integration.push(
            request.path_params["workspace_id"],
            body["policy_revision"],
            body["root_physical_id"],
            body["remote"],
            body["branch"],
            body["head_oid"],
        )
        return json_response(asdict(pushed))

    async def post_check(request: Request) -> Response:
        body = await _body(request, _CHECK_BODY, limit=MAX_BODY_BYTES)
        checked = await integration.check(
            request.path_params["workspace_id"], body["remote"], body["branch"], body["head_oid"]
        )
        return json_response(asdict(checked))

    def endpoint(
        perform: Callable[[Request], Coroutine[Any, Any, Response]],
        *,
        post: bool,
        path_id: tuple[str, str],
    ) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        """`path_id` names the path parameter that must be a canonical UUID, and the code
        answered when it is not. A GET reads its own query; a POST takes none."""

        async def admitted(request: Request) -> Response:
            origin = request.headers.get("origin")
            if origin not in origins and (post or origin is not None):
                return _refusal("forbidden_origin")
            if not _canonical(request.path_params[path_id[0]]):
                return _refusal(path_id[1])
            if post and request.scope.get("query_string"):
                return _refusal("invalid_query")
            permit = object()
            try:
                capacity.acquire_on_behalf_of_nowait(permit)
            except anyio.WouldBlock:
                return _refusal("overloaded")
            # A task of its own, shielded: a client that leaves does not cancel a commit, a merge
            # or a push partway, nor release the lease while its Git work still runs.
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
            except CodeError as error:
                return _refusal(error.code)
            except StateError:
                return _refusal("unavailable")
            except ValueError:
                # The routes validate every value first, so this is a malformed one the
                # service caught: a POST's body or a GET's query.
                return _refusal("invalid_body" if post else "invalid_query")
            except Exception as error:
                _LOGGER.warning("integration route failed (%s)", type(error).__name__)
                return _refusal("unavailable")

        async def handler(request: Request) -> Response:
            response = await admitted(request) if authorized(request) else _refusal("unauthorized")
            response.headers["Cache-Control"] = "no-store"
            return response

        return handler

    workspace = ("workspace_id", "unknown_workspace")
    return [
        Route(
            "/api/v2/code/{workspace_id}/review",
            endpoint(get_review, post=False, path_id=workspace),
            methods=["GET"],
        ),
        Route(
            "/api/v2/code/{workspace_id}/review/file",
            endpoint(get_view, post=False, path_id=workspace),
            methods=["GET"],
        ),
        Route(
            "/api/v2/code/{workspace_id}/commit",
            endpoint(post_commit, post=True, path_id=workspace),
            methods=["POST"],
        ),
        Route(
            "/api/v2/worktrees/{worktree_id}/integrate",
            endpoint(post_integrate, post=True, path_id=("worktree_id", "unknown_worktree")),
            methods=["POST"],
        ),
        Route(
            "/api/v2/code/{workspace_id}/push",
            endpoint(post_push, post=True, path_id=workspace),
            methods=["POST"],
        ),
        Route(
            "/api/v2/code/{workspace_id}/push/check",
            endpoint(post_check, post=True, path_id=workspace),
            methods=["POST"],
        ),
    ]
