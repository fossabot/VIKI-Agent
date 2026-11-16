"""Private REST routes for readiness: capabilities, the diagnostics document, and the two
non-spawning checks. Bearer + Origin like every other v2 module (draft_api.py's shape); no
route here ever reads a credential file or logs a token, prompt or frame body.

`capabilities` and `document` are injected callables rather than a workspace/settings/storage
service list: this module owns readiness and the two checks only, never workspace or storage
state, so the caller (root's server wiring) builds each payload from whatever real state it
holds and hands this module a zero-argument getter.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping
from typing import Any

import anyio
from anyio.to_thread import run_sync
from starlette.requests import ClientDisconnect, Request
from starlette.responses import Response
from starlette.routing import Route

from viki_agent.api_json import failure as _failure
from viki_agent.api_json import json_response as _json
from viki_agent.api_json import unique_object as _unique
from viki_agent.app.diagnostics import (
    CLI_TESTED_MAX,
    CLI_TESTED_MIN,
    CheckBusy,
    CheckResult,
    CheckUnsupported,
    ProviderChecks,
)
from viki_agent.domain import Capabilities

MAX_CHECK_REQUESTS = 4
MAX_CHECK_BODY_BYTES = 1024
CHECK_BODY_TIMEOUT_S = 5.0

_UNAUTHORIZED = ("unauthorized", "Can't authenticate. Open VIKI Agent again.", 401)


def _capabilities_json(caps: Capabilities) -> Mapping[str, object]:
    provider = {
        name: {
            "cli": p.cli,
            "version": p.version,
            "login": p.login,
            "billing": p.billing,
            "last_checked_at": p.last_checked_at,
            "can_start": p.can_start,
            "problems": list(p.problems),
            "source": p.source,
            "version_status": p.version_status,
            "tested": [CLI_TESTED_MIN, CLI_TESTED_MAX],
        }
        for name, p in caps.provider.items()
    }
    return {
        "app": caps.app,
        "workspace": caps.workspace,
        "provider": provider,
        "git": {"state": caps.git.state, "version": caps.git.version},
        "storage": {
            "state": caps.storage.state,
            "reason": caps.storage.reason,
            "private": caps.storage.private,
        },
        "features": caps.features,
        "launch": caps.launch,
    }


def _result_json(result: CheckResult) -> Mapping[str, object]:
    return {
        "check": result.check,
        "status": result.status,
        "result": dict(result.result),
        "checked_at": result.checked_at,
        "duration_ms": result.duration_ms,
    }


def diagnostics_routes(
    checks: ProviderChecks,
    *,
    capabilities: Callable[[], Capabilities],
    document: Callable[[], Mapping[str, object]],
    authorized: Callable[[Request], bool],
    origins: frozenset[str],
    workers: set[asyncio.Task[Any]],
) -> list[Route]:
    # `checks` already holds the Readiness it probes; `capabilities`/`document` are zero-arg
    # getters the caller builds from whatever real workspace/storage/settings state it owns --
    # this module never needs a Readiness (or workspace/storage service) of its own.
    capacity = anyio.CapacityLimiter(MAX_CHECK_REQUESTS)

    async def capabilities_endpoint(request: Request) -> Response:
        if not authorized(request):
            return _failure(*_UNAUTHORIZED)
        # capabilities() does SQLite/ACL/canonical-root I/O; keep it off the event loop while
        # frames are streaming (measured 1.7ms/request). The getter itself stays synchronous --
        # only this call site changes.
        return _json(_capabilities_json(await run_sync(capabilities)))

    async def diagnostics_endpoint(request: Request) -> Response:
        if not authorized(request):
            return _failure(*_UNAUTHORIZED)
        # Same reasoning as capabilities_endpoint above (measured 3.2ms/request).
        return _json(await run_sync(document))

    async def checks_endpoint(request: Request) -> Response:
        if not authorized(request):
            return _failure(*_UNAUTHORIZED)
        # This route is POST-only (a mutation), so a valid Origin is required outright, unlike
        # a GET route where a same-origin request may omit the header: a missing Origin must
        # refuse exactly like a wrong one, not fall through as if none had been sent.
        origin = request.headers.get("origin")
        if origin not in origins:
            return _failure("forbidden_origin", "This window can't do that.", 403)
        permit = object()
        delegated = False
        try:
            capacity.acquire_on_behalf_of_nowait(permit)
        except anyio.WouldBlock:
            return _failure("overloaded", "Busy processing the check. Wait a moment.", 429)

        async def run_check(check: str) -> CheckResult:
            nonlocal delegated
            worker = asyncio.create_task(checks.run(check))
            workers.add(worker)
            delegated = True

            def done(task: asyncio.Task[Any]) -> None:
                workers.discard(task)
                capacity.release_on_behalf_of(permit)
                if not task.cancelled():
                    task.exception()  # retrieve it without logging content

            worker.add_done_callback(done)
            return await asyncio.shield(worker)

        try:
            if (
                request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                != "application/json"
            ):
                return _failure("invalid_representation", "Send it as JSON.", 415)
            raw_length = request.headers.get("content-length")
            if raw_length is not None:
                if not raw_length.isascii() or not raw_length.isdecimal() or len(raw_length) > 6:
                    return _failure("invalid_body", "The request was malformed.", 400)
                if int(raw_length) > MAX_CHECK_BODY_BYTES:
                    return _failure("too_large", "The request is too large.", 413)
            body = bytearray()
            try:
                with anyio.fail_after(CHECK_BODY_TIMEOUT_S):
                    async for chunk in request.stream():
                        if len(chunk) > MAX_CHECK_BODY_BYTES - len(body):
                            return _failure("too_large", "The request is too large.", 413)
                        body.extend(chunk)
            except TimeoutError:
                return _failure("intake_timeout", "Receiving the check took too long.", 408)
            try:
                raw = json.loads(body.decode("utf-8"), object_pairs_hook=_unique)
            except ValueError, RecursionError:
                return _failure("invalid_body", "The request was malformed.", 400)
            if (
                not isinstance(raw, dict)
                or set(raw) != {"command_id", "check"}
                or not isinstance(raw["command_id"], str)
                or not raw["command_id"]
                or len(raw["command_id"]) > 64
                or not isinstance(raw["check"], str)
            ):
                return _failure("invalid_body", "The request was malformed.", 400)
            try:
                result = await run_check(raw["check"])
            except CheckUnsupported:
                return _failure("unsupported", "This check cannot run right now.", 422)
            except CheckBusy:
                return _failure("check_in_progress", "Another check is running.", 409)
            return _json(_result_json(result))
        except ClientDisconnect:
            return _failure("disconnected", "Receiving the check was interrupted.", 400)
        finally:
            if not delegated:
                capacity.release_on_behalf_of(permit)

    return [
        Route("/api/v2/capabilities", capabilities_endpoint, methods=["GET"]),
        Route("/api/v2/diagnostics", diagnostics_endpoint, methods=["GET"]),
        Route("/api/v2/diagnostics/checks", checks_endpoint, methods=["POST"]),
    ]
