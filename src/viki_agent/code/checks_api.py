"""Authenticated, admission-bounded routes for user-written checks and their runs.

List, create and start pass the root gate in their service before any Git call or process;
delete, runs, cancel and output act by id and start nothing. A start spawns a process in a
thread, so each service call runs in a shielded task of its own: a client that leaves does not
leave a process without its supervisor. Every answer carries `Cache-Control: no-store`, and a
refusal holds a fixed message, never a path, a name, a command, an id or an exception's text.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable, Coroutine
from dataclasses import asdict
from typing import Any

import anyio
from anyio.to_thread import run_sync
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from viki_agent.api_json import json_response
from viki_agent.code import code_checks, roots_api
from viki_agent.code.check_runs import CheckRunner
from viki_agent.code.code_api import _canonical
from viki_agent.code.code_checks import CheckService, CheckView, CodeRunError
from viki_agent.code.code_files import CodeError
from viki_agent.code.integration_api import _query
from viki_agent.code.roots_api import _body, _uuid_or_null
from viki_agent.storage import CheckRunRow, StateError

MAX_CHECK_REQUESTS = 8
MAX_BODY_BYTES = 65_536
MAX_OFFSET = 9_007_199_254_740_991
_OFFSET = re.compile(r"[0-9]{1,16}")
_LOGGER = logging.getLogger(__name__)

_STATUS = {
    code: status
    for status, codes in {
        400: "invalid_body invalid_query invalid_name invalid_command invalid_timeout disconnected",
        401: "unauthorized",
        403: "forbidden_origin",
        404: "unknown_workspace unknown_worktree unknown_check unknown_run",
        408: "intake_timeout",
        409: "trust_required root_unavailable worktree_not_ready check_limit check_running "
        "run_limit check_unreadable unsupported_platform",
        413: "too_large",
        429: "overloaded",
        503: "start_failed unavailable",
    }.items()
    for code in codes.split()
}
_MESSAGES = {
    **{code: roots_api._MESSAGES[code] for code in _STATUS if code in roots_api._MESSAGES},
    "invalid_name": "Use 1 to 100 characters on one line.",
    "invalid_command": "Write the command on one line, up to 8,000 characters.",
    "invalid_timeout": "Set a time limit of 1 to 60 minutes.",
    "unknown_check": "This check no longer exists. Refresh the list.",
    "unknown_run": "This check no longer exists. Refresh the list.",
    "check_limit": "A folder keeps at most 32 checks. Delete one first.",
    "check_running": "This check is running. Cancel it or wait until it ends.",
    "run_limit": "Four checks are running. Wait until one ends.",
    "check_unreadable": "This check can't be read on this computer. Delete it and add it again.",
    "unsupported_platform": "Checks are unavailable on this platform.",
    "start_failed": "The command could not start. Check that the folder still exists.",
}


def _refusal(code: str) -> Response:
    if code not in _STATUS:
        code = "unavailable"
    return json_response({"error": {"code": code, "message": _MESSAGES[code]}}, _STATUS[code])


def _string(value: object) -> bool:
    return isinstance(value, str)


def _integer(value: object) -> bool:
    return type(value) is int


_CREATE_BODY = {"name": _string, "command": _string, "timeout_s": _integer}
_START_BODY = {"worktree_id": _uuid_or_null}
_EMPTY_BODY: dict[str, Callable[[Any], bool]] = {}
_LIST_KEYS = frozenset({b"worktree"})
_OUTPUT_KEYS = frozenset({b"after"})


def _run(row: CheckRunRow) -> dict[str, Any]:
    """R: the run's columns but its sealed tail, with `dirty` as a boolean."""
    return {
        "run_id": row["run_id"],
        "check_id": row["check_id"],
        "workspace_id": row["workspace_id"],
        "worktree_id": row["worktree_id"],
        "head_oid": row["head_oid"],
        "dirty": None if row["dirty"] is None else bool(row["dirty"]),
        "outcome": row["outcome"],
        "exit_code": row["exit_code"],
        "duration_ms": row["duration_ms"],
        "output_bytes": row["output_bytes"],
        "started_at": row["started_at"],
        "ended_at": row["ended_at"],
    }


def _check(view: CheckView, last: CheckRunRow | None) -> dict[str, Any]:
    return {**asdict(view), "last_run": None if last is None else _run(last)}


def _offset(query: dict[str, str]) -> int:
    raw = query.get("after", "0")
    if _OFFSET.fullmatch(raw) is None or int(raw) > MAX_OFFSET:
        raise CodeError("invalid_query")
    return int(raw)


def check_routes(
    service: CheckService,
    runner: CheckRunner,
    *,
    authorized: Callable[[Request], bool],
    origins: frozenset[str],
    workers: set[asyncio.Task[Any]],
) -> list[Route]:
    capacity = anyio.CapacityLimiter(MAX_CHECK_REQUESTS)

    async def list_checks(request: Request) -> Response:
        query = _query(request, _LIST_KEYS, frozenset())
        found = await run_sync(
            service.list, request.path_params["workspace_id"], query.get("worktree")
        )
        return json_response({"checks": [_check(view, last) for view, last in found]})

    async def create_check(request: Request) -> Response:
        body = await _body(request, _CREATE_BODY, limit=MAX_BODY_BYTES)
        view = await run_sync(
            service.add_check,
            request.path_params["workspace_id"],
            body["name"],
            body["command"],
            body["timeout_s"],
        )
        return json_response({"check": _check(view, None)}, 201)

    async def delete_check(request: Request) -> Response:
        await _body(request, _EMPTY_BODY, limit=MAX_BODY_BYTES)
        await runner.delete(request.path_params["check_id"])
        return json_response({"deleted": True})

    async def start_run(request: Request) -> Response:
        body = await _body(request, _START_BODY, limit=MAX_BODY_BYTES)
        row = await runner.start(request.path_params["check_id"], body["worktree_id"])
        return json_response({"run": _run(row)}, 201)

    async def list_runs(request: Request) -> Response:
        _query(request, frozenset(), frozenset())
        rows = await runner.runs(request.path_params["check_id"])
        return json_response({"runs": [_run(row) for row in rows]})

    async def cancel_run(request: Request) -> Response:
        await _body(request, _EMPTY_BODY, limit=MAX_BODY_BYTES)
        row = await runner.cancel(request.path_params["run_id"])
        return json_response({"run": _run(row)})

    async def run_output(request: Request) -> Response:
        after = _offset(_query(request, _OUTPUT_KEYS, frozenset()))
        row, part, done = await runner.output(request.path_params["run_id"], after)
        return json_response(
            {
                "run": _run(row),
                "start": part.start,
                "next": part.next,
                "text": part.text,
                "dropped": part.dropped,
                "done": done,
            }
        )

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
            if not code_checks.CHECKS_SUPPORTED:  # read at call time, so tests can patch it
                return _refusal("unsupported_platform")
            if not _canonical(request.path_params[path_id[0]]):
                return _refusal(path_id[1])
            if post and request.scope.get("query_string"):
                return _refusal("invalid_query")
            permit = object()
            try:
                capacity.acquire_on_behalf_of_nowait(permit)
            except anyio.WouldBlock:
                return _refusal("overloaded")
            # A task of its own, shielded: a client that leaves does not cancel a start between
            # its spawn and its supervisor, nor a delete partway.
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
            except (CodeRunError, CodeError) as error:
                return _refusal(error.code)
            except StateError:
                return _refusal("unavailable")
            except Exception as error:
                _LOGGER.warning("checks route failed (%s)", type(error).__name__)
                return _refusal("unavailable")

        async def handler(request: Request) -> Response:
            response = await admitted(request) if authorized(request) else _refusal("unauthorized")
            response.headers["Cache-Control"] = "no-store"
            return response

        return handler

    workspace = ("workspace_id", "unknown_workspace")
    check = ("check_id", "unknown_check")
    run = ("run_id", "unknown_run")
    return [
        Route(
            "/api/v2/code/{workspace_id}/checks",
            endpoint(list_checks, post=False, path_id=workspace),
            methods=["GET"],
        ),
        Route(
            "/api/v2/code/{workspace_id}/checks",
            endpoint(create_check, post=True, path_id=workspace),
            methods=["POST"],
        ),
        Route(
            "/api/v2/checks/{check_id}/delete",
            endpoint(delete_check, post=True, path_id=check),
            methods=["POST"],
        ),
        Route(
            "/api/v2/checks/{check_id}/runs",
            endpoint(start_run, post=True, path_id=check),
            methods=["POST"],
        ),
        Route(
            "/api/v2/checks/{check_id}/runs",
            endpoint(list_runs, post=False, path_id=check),
            methods=["GET"],
        ),
        Route(
            "/api/v2/check-runs/{run_id}/cancel",
            endpoint(cancel_run, post=True, path_id=run),
            methods=["POST"],
        ),
        Route(
            "/api/v2/check-runs/{run_id}/output",
            endpoint(run_output, post=False, path_id=run),
            methods=["GET"],
        ),
    ]
