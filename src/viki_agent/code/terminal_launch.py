"""The authenticated route that opens a terminal window in a
trusted code folder or its ready worktree. It is a launcher, not an in-app terminal.

The root gate (`code_checks.trusted_root`) and the removal guard (`SessionRoots.blocked`) come
before `winproc.open_terminal`, which starts the shell outside any Job: the user's window
outlives VIKI. The start runs in a shielded task of its own, so a client that leaves does not
cancel it midway. Answers and refusals are `checks_api`'s: `Cache-Control: no-store` and fixed
messages, never a path, an id or an exception's text.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from collections.abc import Callable
from typing import Any

import anyio
from anyio.to_thread import run_sync
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from viki_agent import winproc
from viki_agent.api_json import json_response
from viki_agent.code import code_checks
from viki_agent.code.checks_api import _START_BODY, MAX_BODY_BYTES, _refusal
from viki_agent.code.code_api import _canonical
from viki_agent.code.code_checks import CodeRunError
from viki_agent.code.code_files import CodeError
from viki_agent.code.roots_api import _body
from viki_agent.code.session_roots import SessionRoots
from viki_agent.storage import SessionStore, StateError
from viki_agent.workspaces import WorkspaceService

TERMINAL_SUPPORTED = sys.platform in {"win32", "darwin", "linux"}  # read at call time
MAX_TERMINAL_REQUESTS = 8
_LOGGER = logging.getLogger(__name__)


def terminal_launch_routes(
    store: SessionStore,
    workspaces: WorkspaceService,
    roots: SessionRoots,
    *,
    authorized: Callable[[Request], bool],
    origins: frozenset[str],
    workers: set[asyncio.Task[Any]],
    pending: dict[str, int] | None = None,
) -> list[Route]:
    capacity = anyio.CapacityLimiter(MAX_TERMINAL_REQUESTS)
    pending = {} if pending is None else pending

    async def open_here(request: Request) -> Response:
        body = await _body(request, _START_BODY, limit=MAX_BODY_BYTES)
        workspace_id, worktree_id = request.path_params["workspace_id"], body["worktree_id"]
        cwd = await run_sync(code_checks.trusted_root, store, workspaces, workspace_id, worktree_id)
        if worktree_id is not None and roots.blocked(worktree_id):
            raise CodeRunError("worktree_not_ready")
        # No await between the removal guard and this reservation. Count concurrent opens of
        # one worktree so the first completion cannot release another launch's hold.
        if worktree_id is not None:
            pending[worktree_id] = pending.get(worktree_id, 0) + 1
        try:
            # Looked up at call time, so tests can patch it.
            await run_sync(winproc.open_terminal, cwd, winproc.scrub_env(os.environ))
        except OSError:
            raise CodeRunError("start_failed") from None
        finally:
            if worktree_id is not None:
                pending[worktree_id] -= 1
                if pending[worktree_id] == 0:
                    del pending[worktree_id]
        return json_response({"opened": True})

    async def admitted(request: Request) -> Response:
        if request.headers.get("origin") not in origins:
            return _refusal("forbidden_origin")
        if not TERMINAL_SUPPORTED:
            return _refusal("unsupported_platform")
        if not _canonical(request.path_params["workspace_id"]):
            return _refusal("unknown_workspace")
        if request.scope.get("query_string"):
            return _refusal("invalid_query")
        permit = object()
        try:
            capacity.acquire_on_behalf_of_nowait(permit)
        except anyio.WouldBlock:
            return _refusal("overloaded")
        worker = asyncio.create_task(open_here(request))
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
            _LOGGER.warning("terminal launch failed (%s)", type(error).__name__)
            return _refusal("unavailable")

    async def handler(request: Request) -> Response:
        response = await admitted(request) if authorized(request) else _refusal("unauthorized")
        response.headers["Cache-Control"] = "no-store"
        return response

    return [Route("/api/v2/code/{workspace_id}/terminal", handler, methods=["POST"])]
