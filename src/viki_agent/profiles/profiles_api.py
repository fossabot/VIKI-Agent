"""Private, bounded profile definition routes; none starts a provider process."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

import anyio
from anyio.to_thread import run_sync
from starlette.requests import ClientDisconnect, Request
from starlette.responses import Response
from starlette.routing import Route

from viki_agent.api_json import failure, json_response, unique_object
from viki_agent.app.settings import SettingsError
from viki_agent.profiles.mcp_config import McpConfigError, import_configuration
from viki_agent.profiles.profile_document import ProfileDocumentError
from viki_agent.profiles.service import ProfileError, ProfileService
from viki_agent.protection import ProtectionError
from viki_agent.storage import StateError

MAX_BODY = 2 << 20


def _keys(value: object, keys: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ProfileError("invalid_input")
    return value


def _integer(value: str) -> int:
    if not value.isascii() or not value.isdecimal() or len(value) > 16:
        raise ProfileError("invalid_revision")
    return int(value)


def profile_routes(
    service: ProfileService,
    *,
    authorized: Callable[[Request], bool],
    origins: frozenset[str],
    workers: set[asyncio.Task[Any]],
    start_preview: Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]] | None = None,
    provider_view: Callable[[str, str], dict[str, Any]] | None = None,
    inspect_session: Callable[[str], dict[str, Any]] | None = None,
) -> list[Route]:
    capacity = anyio.CapacityLimiter(4)

    async def perform(request: Request) -> Response:
        status = 200
        if len(request.scope.get("query_string", b"")) > 512:
            raise ProfileError("invalid_input")
        query = request.query_params
        if any(len(query.getlist(key)) != 1 for key in query):
            raise ProfileError("invalid_input")
        profile_id = request.path_params.get("profile_id")
        action = request.path_params.get("action")
        if "session_id" in request.path_params:
            if query or inspect_session is None:
                raise ProfileError("invalid_input")
            inspect_profile = inspect_session
            return json_response(
                await run_sync(lambda: inspect_profile(request.path_params["session_id"]))
            )
        if "provider" in request.path_params:
            if query or provider_view is None:
                raise ProfileError("invalid_input")
            get_provider = provider_view
            result = await run_sync(
                lambda: get_provider(
                    request.path_params["provider"], request.path_params["provider_action"]
                )
            )
            return json_response(result)
        if request.method in {"GET", "HEAD"}:
            if profile_id is None:
                if set(query) - {"archived"} or query.get("archived", "false") not in {
                    "false",
                    "true",
                }:
                    raise ProfileError("invalid_input")
                result: Any = {
                    "profiles": await run_sync(
                        lambda: service.list(query.get("archived") == "true")
                    )
                }
            elif action == "compare":
                if set(query) != {"left", "right"}:
                    raise ProfileError("invalid_input")
                left, right = _integer(query["left"]), _integer(query["right"])
                result = await run_sync(lambda: service.compare(profile_id, left, right))
            elif action is None:
                if set(query) - {"revision"}:
                    raise ProfileError("invalid_input")
                revision = _integer(query["revision"]) if "revision" in query else None
                result = await run_sync(lambda: service.get(profile_id, revision))
            else:
                return failure("not_found", "Profile action is unavailable.", 404)
        else:
            if (
                query
                or request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                != "application/json"
            ):
                raise ProfileError("invalid_input")
            if any(
                item.strip().lower() != "identity"
                for item in request.headers.getlist("content-encoding")
            ):
                raise ProfileError("invalid_input")
            body = bytearray()
            with anyio.fail_after(5):
                async for chunk in request.stream():
                    if len(chunk) > MAX_BODY - len(body):
                        return failure("too_large", "Profile request is too large.", 413)
                    body.extend(chunk)
            try:
                value = json.loads(body.decode("utf-8"), object_pairs_hook=unique_object)
            except ValueError, RecursionError:
                raise ProfileError("invalid_input") from None
            if profile_id == "mcp" and action == "import" and request.method == "POST":
                raw = _keys(value, {"json"})
                try:
                    result = await run_sync(lambda: import_configuration(raw["json"]))
                except McpConfigError as error:
                    raise ProfileDocumentError(error.code, error.field) from None
            elif profile_id is None and request.method == "POST":
                raw = _keys(value, {"profile_id", "command_id", "document"})
                result = await run_sync(
                    lambda: service.create(raw["profile_id"], raw["command_id"], raw["document"])
                )
            elif profile_id is not None and action is None and request.method == "PATCH":
                raw = _keys(value, {"command_id", "expected_revision", "document"})
                result = await run_sync(
                    lambda: service.revise(
                        profile_id, raw["command_id"], raw["expected_revision"], raw["document"]
                    )
                )
            elif profile_id is not None and action == "archive" and request.method == "POST":
                raw = _keys(value, {"command_id", "expected_state_revision", "archived"})
                result = await run_sync(
                    lambda: service.archive(
                        profile_id,
                        raw["command_id"],
                        raw["expected_state_revision"],
                        raw["archived"],
                    )
                )
            elif profile_id is not None and action == "duplicate" and request.method == "POST":
                raw = _keys(
                    value,
                    {"command_id", "expected_revision", "target_profile_id", "target_runtime"},
                )
                result = await run_sync(
                    lambda: service.duplicate(
                        profile_id,
                        raw["command_id"],
                        raw["expected_revision"],
                        raw["target_profile_id"],
                        raw["target_runtime"],
                    )
                )
            elif (
                profile_id is not None
                and action == "start-preview"
                and request.method == "POST"
                and start_preview is not None
            ):
                raw = _keys(
                    value,
                    {"command_id", "session_id", "expected_profile_revision", "workspace_id"}
                    | ({"bot_id"} if isinstance(value, dict) and "bot_id" in value else set()),
                )
                result = await start_preview(profile_id, raw)
                if not result["launchable"] and "session_id" not in result:
                    status = 422
            else:
                return failure("not_found", "Profile action is unavailable.", 404)
        response = json_response(result, status=status)
        if len(response.body) > MAX_BODY:
            return failure("too_large", "Profile response is too large.", 413)
        return response

    async def endpoint(request: Request) -> Response:
        if not authorized(request):
            response = failure("unauthorized", "Authentication is required.", 401)
        else:
            origin = request.headers.get("origin")
            if (origin is not None and origin not in origins) or (
                request.method not in {"GET", "HEAD"} and origin not in origins
            ):
                response = failure("forbidden_origin", "This origin is not allowed.", 403)
            else:
                response = await admitted(request)
        response.headers["Cache-Control"] = "no-store"
        return response

    async def admitted(request: Request) -> Response:
        permit = object()
        try:
            capacity.acquire_on_behalf_of_nowait(permit)
        except anyio.WouldBlock:
            return failure("overloaded", "Profile operations are busy.", 429)
        task = asyncio.create_task(perform(request))
        workers.add(task)

        def done(future: asyncio.Task[Response]) -> None:
            workers.discard(future)
            capacity.release_on_behalf_of(permit)
            if not future.cancelled():
                future.exception()

        task.add_done_callback(done)
        try:
            return await asyncio.shield(task)
        except ProfileError as error:
            if error.code in {"corrupt", "protection"}:
                return failure("unavailable", "Profile storage is unavailable.", 503)
            status = (
                404
                if error.code == "not_found"
                else 409
                if error.code
                in {
                    "conflict",
                    "command_conflict",
                    "newer_version",
                    "workspace_restart_required",
                    "session_exists",
                    "transcript_missing",
                }
                else 429
                if error.code == "capacity"
                else 503
                if error.code
                in {
                    "provider_unavailable",
                    "settings_unavailable",
                    "workspace_unavailable",
                    "capability_unverified",
                    "invalid_catalog",
                }
                else 400
            )
            return failure(error.code, "The profile operation could not be completed.", status)
        except ProfileDocumentError as error:
            return json_response(
                {
                    "error": {
                        "code": "invalid_document",
                        "message": "The profile document is invalid.",
                        "field": error.field,
                        "reason": error.code,
                    }
                },
                status=400,
            )
        except StateError, ProtectionError, SettingsError:
            return failure("unavailable", "Profile storage is unavailable.", 503)
        except TimeoutError:
            return failure("intake_timeout", "The request timed out.", 408)
        except ClientDisconnect:
            return failure("disconnected", "The request was interrupted.", 400)

    return [
        Route("/api/v2/profiles", endpoint, methods=["GET", "POST"]),
        Route("/api/v2/profiles/{profile_id}", endpoint, methods=["GET", "PATCH"]),
        Route("/api/v2/profiles/{profile_id}/{action}", endpoint, methods=["GET", "POST"]),
        *(
            [Route("/api/v2/profile-sessions/{session_id}", endpoint, methods=["GET"])]
            if inspect_session is not None
            else []
        ),
        *(
            [Route("/api/v2/providers/{provider}/{provider_action}", endpoint, methods=["GET"])]
            if provider_view is not None
            else []
        ),
    ]
