"""The authenticated route that opens Claude Code's own sign-in, `claude auth login --claudeai`,
in a terminal window of its own on the user's click: Windows PowerShell on Windows, Terminal on
macOS, the desktop's terminal on Linux (`winproc.open_console`).

`--claudeai` keeps the sign-in on the subscription route, and the CLI writes its own credential
store: VIKI reads no output, stores nothing and logs no path or text. `winproc.open_console`
starts the console outside any Job, since the CLI may read a pasted code from it and it outlives
VIKI. Its environment is the server's, scrubbed, with `DISABLE_AUTOUPDATER=1` only for the SDK's
copy of the CLI (`config.BUNDLED_CLI`); the user's own install keeps its own update setting.
On macOS Terminal's own environment applies instead. Refusals come first, in a fixed order, and
none starts anything; the start then runs in a shielded task of its own, so a client that leaves
does not cancel it midway. Every answer carries `Cache-Control: no-store` and a fixed message,
and the request body is not read.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import anyio
from anyio.to_thread import run_sync
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from viki_agent import config, winproc
from viki_agent.api_json import failure, json_response
from viki_agent.drivers.claude import control

# Read at call time, so tests can patch it.
LOGIN_SUPPORTED = sys.platform in ("win32", "darwin", "linux")
MAX_LOGIN_REQUESTS = 2
_LOGGER = logging.getLogger(__name__)

_REFUSALS = {
    "unauthorized": (401, "Can't authenticate. Open VIKI Agent again."),
    "forbidden_origin": (403, "This window can't do that."),
    "invalid_query": (400, "Check what was sent."),
    "unsupported_platform": (409, "Signing in from VIKI Agent works on Windows, macOS and Linux."),
    "provider_unavailable": (409, "VIKI Agent can't start Claude Code right now."),
    "overloaded": (429, "Busy right now. Wait a moment, then try again."),
    "start_failed": (503, "Couldn't start the sign-in."),
}


def _refusal(code: str) -> Response:
    status, message = _REFUSALS[code]
    return failure(code, message, status)


def provider_login_routes(
    *,
    cli: Callable[[], Path | None],
    authorized: Callable[[Request], bool],
    origins: frozenset[str],
    workers: set[asyncio.Task[Any]],
) -> list[Route]:
    """`cli` answers the executable the console may start, or None while readiness says no
    claude may start."""
    capacity = anyio.CapacityLimiter(MAX_LOGIN_REQUESTS)

    async def open_sign_in(exe: Path) -> Response:
        # Not child_env: its SDK markers are for SDK sessions only. Only the SDK's copy is
        # pinned against auto-update; the user's own install keeps its own setting.
        env = winproc.scrub_env(os.environ)
        if exe == config.BUNDLED_CLI:
            env["DISABLE_AUTOUPDATER"] = "1"
        # Looked up at call time, so tests can patch them.
        start = functools.partial(
            winproc.open_console,
            # Absolute: readiness checked it from the server's cwd, not the console's home folder.
            control.login_argv((str(exe.absolute()),)),
            cwd=Path.home(),
            env=env,
        )
        try:
            await run_sync(start)
        except OSError, ValueError:
            return _refusal("start_failed")
        return json_response({"opened": True})

    async def admitted(request: Request) -> Response:
        if request.headers.get("origin") not in origins:
            return _refusal("forbidden_origin")
        if not LOGIN_SUPPORTED:
            return _refusal("unsupported_platform")
        if request.scope.get("query_string"):
            return _refusal("invalid_query")
        exe = cli()
        if exe is None:
            return _refusal("provider_unavailable")
        permit = object()
        try:
            capacity.acquire_on_behalf_of_nowait(permit)
        except anyio.WouldBlock:
            return _refusal("overloaded")
        worker = asyncio.create_task(open_sign_in(exe))
        workers.add(worker)

        def done(task: asyncio.Task[Response]) -> None:
            workers.discard(task)
            capacity.release_on_behalf_of(permit)
            if not task.cancelled():
                task.exception()

        worker.add_done_callback(done)
        try:
            return await asyncio.shield(worker)
        except Exception as error:
            _LOGGER.warning("sign-in console failed (%s)", type(error).__name__)
            return _refusal("start_failed")

    async def handler(request: Request) -> Response:
        response = await admitted(request) if authorized(request) else _refusal("unauthorized")
        response.headers["Cache-Control"] = "no-store"
        return response

    return [Route("/api/v2/provider/login", handler, methods=["POST"])]
