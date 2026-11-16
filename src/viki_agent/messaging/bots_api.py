"""Private, bounded Bot routes; chat attachment uses the server's native source adapter."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import anyio
from starlette.requests import ClientDisconnect, Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from viki_agent.api_json import failure, unique_object
from viki_agent.conversations.command_runtime import storage_call
from viki_agent.messaging.bot_threads import BotThreadService
from viki_agent.messaging.bots import BotError, BotService, _id, _revision
from viki_agent.protection import ProtectionError

MAX_BODY = 4 << 10
MAX_RESPONSE = 256 << 10
MAX_LIST_RESPONSE = 2 << 20
MAX_CHAT_RESPONSE = 2 << 20
MAX_REQUESTS = 4
INTAKE_TIMEOUT_S = 5
BASE = "/api/v2/bots"
THREAD_BASE = "/api/v2/bot-threads"
THREAD_BODY = 65536
logger = logging.getLogger(__name__)

_STATUS = {
    "invalid_input": 400,
    "unauthorized": 401,
    "forbidden_origin": 403,
    "not_found": 404,
    "transcript_missing": 404,
    **dict.fromkeys(
        ("conflict", "command_conflict", "busy", "session_exists", "workspace_restart_required"),
        409,
    ),
    **dict.fromkeys(
        (
            "profile_missing",
            "profile_archived",
            "profile_changed",
            "workspace_missing",
            "workspace_unavailable",
            "delivery_unavailable",
            "profile_blocked",
            "code_folder_needs_ordinary",
            "code_folder_untrusted",
            "code_folder_missing",
            "code_folder_not_repository",
            "code_folder_no_commits",
        ),
        422,
    ),
    "git_settings_changed": 409,
    "capacity": 429,
    "overloaded": 429,
    **dict.fromkeys(
        (
            "code_folder_git_unavailable",
            "unavailable",
            "corrupt",
            "protection",
            "newer_version",
            "provider_unavailable",
            "settings_unavailable",
            "app_closing",
        ),
        503,
    ),
    "intake_timeout": 408,
}
_CHAT_STATES = {
    "unprepared",
    "prepared",
    "reopenable",
    "archived",
    "transcript_missing",
    "blocked",
    "unavailable",
}


def _keys(value: object, expected: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        raise BotError("invalid_input")
    return value


def _response(value: object, status: int = 200, *, budget: int = MAX_RESPONSE) -> Response:
    response = JSONResponse(value, status_code=status)
    if len(response.body) > budget:
        raise BotError("unavailable")
    return response


def _headers(request: Request) -> bool:
    if request.scope.get("query_string") or len(request.headers.getlist("content-encoding")) > 1:
        return False
    if request.headers.get("content-encoding", "identity").strip().lower() != "identity":
        return False
    if request.headers.get("transfer-encoding") is not None:
        return False
    lengths = request.headers.getlist("content-length")
    if len(lengths) > 1 or (
        lengths
        and (
            not lengths[0].isascii()
            or not lengths[0].isdecimal()
            or len(lengths[0]) > 8
            or int(lengths[0])
            > (THREAD_BODY if request.url.path.startswith(THREAD_BASE + "/") else MAX_BODY)
        )
    ):
        return False
    if request.method in {"GET", "HEAD"}:
        return not lengths or int(lengths[0]) == 0
    types = request.headers.getlist("content-type")
    if len(types) != 1:
        return False
    parts = [part.strip().lower() for part in types[0].split(";")]
    return parts[0] == "application/json" and (
        len(parts) == 1 or len(parts) == 2 and parts[1] in {"charset=utf-8", 'charset="utf-8"'}
    )


async def _body(request: Request, maximum: int = MAX_BODY) -> object:
    body = bytearray()
    try:
        with anyio.fail_after(INTAKE_TIMEOUT_S):
            async for chunk in request.stream():
                if len(chunk) > maximum - len(body):
                    raise BotError("invalid_input")
                body.extend(chunk)
    except TimeoutError:
        raise BotError("intake_timeout") from None
    try:
        return json.loads(body.decode("utf-8"), object_pairs_hook=unique_object)
    except ValueError, RecursionError:
        raise BotError("invalid_input") from None


def _preview(value: object, bot: dict[str, Any]) -> tuple[dict[str, Any], int]:
    if not isinstance(value, dict) or value.get("profile_id") != bot["profile_id"]:
        raise BotError("unavailable")
    if type(value.get("launchable")) is not bool or not isinstance(value.get("blockers"), list):
        raise BotError("unavailable")
    if value["launchable"] != (len(value["blockers"]) == 0):
        raise BotError("unavailable")
    if "session_id" not in value:
        if value["launchable"] or set(value) != {
            "launchable",
            "fields",
            "blockers",
            "profile_id",
            "profile_revision",
        }:
            raise BotError("unavailable")
        return value, 422
    if (
        value["session_id"] != bot["chat_session_id"]
        or value.get("workspace_id") != bot["workspace_id"]
    ):
        raise BotError("unavailable")
    try:
        _id(value.get("generation"))
        revision = _revision(value.get("profile_revision"))
    except BotError:
        raise BotError("unavailable") from None
    expected = bot["chat"]["profile_revision"]
    if expected is None and bot["profile"] is not None:
        expected = bot["profile"]["revision"]
    if revision != expected:
        raise BotError("unavailable")
    return value, 200


def bot_routes(
    service: BotService,
    *,
    authorized: Callable[[Request], bool],
    origins: frozenset[str],
    workers: set[asyncio.Task[Any]],
    admission_lock: asyncio.Lock,
    prepare_chat: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]],
    chat_status: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]],
    busy: Callable[[str], bool],
    scheduler_status: Callable[[], dict[str, Any]],
    delivery_status: Callable[[dict[str, Any]], dict[str, Any]],
    set_delivery: Callable[[str, int, bool], Awaitable[dict[str, Any]]],
    revoke: Callable[[str], None],
    wake: Callable[[], None],
    copies_released: Callable[[], None] | None = None,
) -> list[Route]:
    """`copies_released` runs on the loop after a committed Bot or Bot-thread deletion, so
    the copies it released can be cleaned up outside the transaction."""
    capacity = anyio.CapacityLimiter(MAX_REQUESTS)
    threads = BotThreadService(service.store, service.workspaces)

    def decorate(bot: dict[str, Any]) -> dict[str, Any]:
        return {**bot, "delivery": delivery_status(bot)}

    def released() -> None:
        if copies_released is not None:
            copies_released()

    async def perform(request: Request) -> Response:
        if request.url.path.startswith(THREAD_BASE + "/"):
            workspace_id = _id(request.path_params["workspace_id"])
            thread_id = request.path_params.get("thread_id")
            if request.method in {"GET", "HEAD"} and thread_id is None:
                return _response(
                    await storage_call(lambda: threads.list(workspace_id)), budget=2 << 20
                )
            if thread_id is None:
                raise BotError("invalid_input")
            value = await _body(request, THREAD_BODY)
            if request.method == "DELETE":
                raw = _keys(value, {"expected_revision"})
                result = await storage_call(
                    lambda: threads.remove(workspace_id, thread_id, raw["expected_revision"])
                )
                released()
                return _response(result)
            raw = _keys(value, {"expected_revision", "state"})
            return _response(
                await storage_call(
                    lambda: threads.save(
                        workspace_id, thread_id, raw["expected_revision"], raw["state"]
                    )
                )
            )
        bot_id = request.path_params.get("bot_id")
        action = request.path_params.get("action")
        if bot_id is not None:
            bot_id = _id(bot_id)
        if request.method in {"GET", "HEAD"}:
            if bot_id is not None and request.url.path.endswith("/threads"):
                return _response(await storage_call(lambda: service.threads(bot_id)))
            if bot_id is None:
                rows = await storage_call(service.list)
                scheduler = await storage_call(scheduler_status)
                return _response(
                    {"bots": [decorate(row) for row in rows], "scheduler": scheduler},
                    budget=MAX_LIST_RESPONSE,
                )
            bot = await storage_call(lambda: service.get(bot_id))
            status = await chat_status(bot)
            if (
                not isinstance(status, dict)
                or set(status) != {"state", "blockers"}
                or status["state"] not in _CHAT_STATES
                or not isinstance(status["blockers"], list)
            ):
                raise BotError("unavailable")
            return _response({"bot": decorate(bot), "chat_status": status}, budget=MAX_RESPONSE)
        value = await _body(request)
        async with admission_lock:
            if request.url.path == BASE + "/reset":
                raw = _keys(value, {"workspace_id"})
                workspace_id = _id(raw["workspace_id"])
                rows = await storage_call(service.list)
                for row in rows:
                    if row["workspace_id"] == workspace_id:
                        revoke(row["bot_id"])
                result = await storage_call(lambda: service.reset(workspace_id))
                wake()
                released()
                return _response(result, budget=MAX_RESPONSE)
            if bot_id is None:
                raw = _keys(
                    value, {"bot_id", "profile_id", "expected_profile_revision", "workspace_id"}
                )
                bot, replayed = await storage_call(lambda: service.create(**raw))
                return _response(
                    {"bot": decorate(bot), "replayed": replayed},
                    200 if replayed else 201,
                    budget=MAX_RESPONSE,
                )
            if action == "delivery":
                raw = _keys(value, {"expected_state_revision", "enabled"})
                expected = _revision(raw["expected_state_revision"])
                if type(raw["enabled"]) is not bool:
                    raise BotError("invalid_input")
                # Runtime revokes first, including a stale CAS; the failed toggle remains off.
                bot = await set_delivery(bot_id, expected, raw["enabled"])
                return _response({"bot": decorate(bot)}, budget=MAX_RESPONSE)
            if request.method == "DELETE" and action is None:
                raw = _keys(value, {"expected_state_revision"})
                expected = _revision(raw["expected_state_revision"])
                revoke(bot_id)
                result = await storage_call(lambda: service.remove(bot_id, expected))
                wake()
                released()
                return _response(result, budget=MAX_RESPONSE)
            if action == "code-folder":
                raw = _keys(value, {"expected_state_revision", "workspace_id"})
                bot = await storage_call(
                    lambda: service.set_code_folder(
                        bot_id, raw["expected_state_revision"], raw["workspace_id"]
                    )
                )
                return _response({"bot": decorate(bot)}, budget=MAX_RESPONSE)
            if action not in {"chat", "new-chat"}:
                raise BotError("not_found")
            raw = _keys(value, {"expected_state_revision"})
            expected = _revision(raw["expected_state_revision"])
            bot = await storage_call(lambda: service.get(bot_id))
            if bot["state_revision"] != expected:
                raise BotError("conflict")
            if action == "new-chat":
                if busy(bot["chat_session_id"]):
                    raise BotError("busy")
                revoke(bot_id)
                bot = await storage_call(lambda: service.new_chat(bot_id, expected))
                return _response({"bot": decorate(bot)}, budget=MAX_RESPONSE)
            preview, status = _preview(await prepare_chat(bot), bot)
            return _response(
                {"bot_id": bot_id, "state_revision": expected, "preview": preview},
                status,
                budget=MAX_CHAT_RESPONSE,
            )

    async def safe_perform(request: Request) -> Response:
        try:
            response = await perform(request)
        except ClientDisconnect:
            response = failure("invalid_input", "The Bot request was interrupted.", 400)
        except Exception as error:
            code = (
                "protection" if isinstance(error, ProtectionError) else getattr(error, "code", None)
            )
            if not isinstance(code, str) or code not in _STATUS:
                logger.warning("Bot operation failed: %s", type(error).__name__)
                code = "unavailable"
            response = failure(code, "The Bot operation could not be completed.", _STATUS[code])
        response.headers["Cache-Control"] = "no-store"
        return response

    async def admitted(request: Request) -> Response:
        permit = object()
        try:
            capacity.acquire_on_behalf_of_nowait(permit)
        except anyio.WouldBlock:
            return failure("overloaded", "Bot operations are busy.", 429)
        task = asyncio.create_task(safe_perform(request))
        workers.add(task)

        def done(future: asyncio.Task[Response]) -> None:
            workers.discard(future)
            capacity.release_on_behalf_of(permit)
            if not future.cancelled():
                future.exception()

        task.add_done_callback(done)
        return await asyncio.shield(task)

    async def endpoint(request: Request) -> Response:
        origins_given = request.headers.getlist("origin")
        if len(request.headers.getlist("authorization")) != 1 or not authorized(request):
            response = failure("unauthorized", "Authentication is required.", 401)
        elif (
            len(origins_given) > 1
            or (origins_given and origins_given[0] not in origins)
            or (request.method not in {"GET", "HEAD"} and not origins_given)
        ):
            response = failure("forbidden_origin", "This origin is not allowed.", 403)
        elif not _headers(request):
            response = failure("invalid_input", "The Bot request is invalid.", 400)
        else:
            response = await admitted(request)
        response.headers["Cache-Control"] = "no-store"
        return response

    return [
        Route(THREAD_BASE + "/{workspace_id}", endpoint, methods=["GET"]),
        Route(THREAD_BASE + "/{workspace_id}/{thread_id}", endpoint, methods=["PUT", "DELETE"]),
        Route(BASE, endpoint, methods=["GET", "POST"]),
        Route(BASE + "/reset", endpoint, methods=["POST"]),
        Route(BASE + "/{bot_id}", endpoint, methods=["GET", "DELETE"]),
        Route(BASE + "/{bot_id}/threads", endpoint, methods=["GET"]),
        Route(BASE + "/{bot_id}/{action}", endpoint, methods=["POST"]),
    ]
