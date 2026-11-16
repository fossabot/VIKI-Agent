"""Authenticated status, explicit actions and saved settings for three fixed messaging providers."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any

import anyio
from starlette.requests import ClientDisconnect, Request
from starlette.responses import Response
from starlette.routing import Route

from viki_agent.api_json import failure, json_response, unique_object
from viki_agent.messaging.mattermost_config import valid_id
from viki_agent.messaging.messaging_channel import Provider
from viki_agent.messaging.messaging_runtime import BridgeFactory, MessagingError, MessagingRuntime
from viki_agent.messaging.messaging_store import (
    FIELDS,
    SECRETS,
    Config,
    MessagingConfigStore,
    MessagingStoreError,
    build,
    fields,
    public_values,
)

MAX_ACTION_REQUESTS = 4
MAX_DISABLE_REQUESTS = MAX_ACTION_REQUESTS
MAX_CONFIG_BODY = 16 << 10
MAX_CONFIG_VALUE = 4096
INTAKE_TIMEOUT_S = 5
INVALID = "The messaging request is invalid."
UNAVAILABLE = "Messaging is unavailable."
_FAILURES = {
    "invalid_input": (INVALID, 400),
    "invalid_config": ("Check the messaging settings and try again.", 400),
    "invalid_token": ("Check the bot token and try again.", 400),
    "invalid_app_token": ("Check the app token and try again.", 400),
    "not_configured": ("Set up this messaging provider before connecting.", 409),
    "restart_required": ("Restart VIKI Agent before connecting.", 409),
    "unavailable": (UNAVAILABLE, 503),
}
_PROVIDERS: tuple[Provider, ...] = ("mattermost", "slack", "discord")
# Fields that decide where a secret is sent (Mattermost's server URL). Discord's client URL
# only names the bot in its user agent, so it is not one.
_DESTINATIONS = frozenset({"url"})
# A typed (non-empty, non-kept) token that is obviously the wrong shape, checked before build.
_TOKEN_CHECKS: tuple[tuple[Provider, str, str, Callable[[str], bool]], ...] = (
    ("mattermost", "bot_token", "invalid_token", valid_id),
    (
        "slack",
        "bot_token",
        "invalid_token",
        lambda value: value.startswith("xoxb-") and len(value) > 5,
    ),
    (
        "slack",
        "app_token",
        "invalid_app_token",
        lambda value: value.startswith("xapp-") and len(value) > 5,
    ),
)
logger = logging.getLogger(__name__)


def _keeps(saved: Config, config: Config, kept: list[str]) -> bool:
    """Whether config reuses saved's kept secrets unchanged and for the same canonical
    destination: a token left out is never sent anywhere it was not saved for."""
    old, new = fields(saved), fields(config)
    return all(old[name] == new[name] for name in new if name in kept or name in _DESTINATIONS)


def _json_declared(request: Request) -> bool:
    """One JSON media type and one decimal length within the settings cap."""
    lengths = request.headers.getlist("content-length")
    types = request.headers.getlist("content-type")
    if len(lengths) != 1 or len(types) != 1:
        return False
    length = lengths[0]
    parts = [part.strip().lower() for part in types[0].split(";")]
    return (
        length.isascii()
        and length.isdecimal()
        and len(length) <= 8
        and int(length) <= MAX_CONFIG_BODY
        and parts[0] == "application/json"
        and (
            len(parts) == 1 or len(parts) == 2 and parts[1] in {"charset=utf-8", 'charset="utf-8"'}
        )
    )


def _refused(
    request: Request,
    authorized: Callable[[Request], bool],
    origins: frozenset[str],
    *,
    json_body: bool = False,
) -> Response | None:
    """Bearer, an allowed Origin (required to change anything), no query, no transfer coding,
    identity content coding, and either no declared body or one bounded JSON body."""
    allowed = {None, *origins} if request.method in ("GET", "HEAD") else origins
    if not authorized(request):
        return failure("unauthorized", "Authentication is required.", 401)
    if request.headers.get("origin") not in allowed:
        return failure("forbidden_origin", "This origin is not allowed.", 403)
    if (
        request.scope.get("query_string")
        or request.headers.get("transfer-encoding") is not None
        or request.headers.get("content-encoding", "identity") != "identity"
        or not (
            _json_declared(request)
            if json_body
            else request.headers.get("content-length") in (None, "0")
        )
    ):
        return failure("invalid_input", INVALID, 400)
    return None


async def _drained(request: Request) -> Response | None:
    """Refuse a body that arrives although none was declared."""
    try:
        with anyio.fail_after(INTAKE_TIMEOUT_S):
            async for chunk in request.stream():
                if chunk:
                    return failure("invalid_input", INVALID, 400)
    except TimeoutError:
        return failure("intake_timeout", "The request did not arrive in time.", 408)
    except ClientDisconnect:
        return failure("disconnected", "The request was interrupted.", 400)
    return None


def _answer(error: MessagingError) -> Response:
    if error.code == "cooldown":
        seconds = error.retry_after_seconds
        if type(seconds) is not int or not 1 <= seconds <= 86400:
            return failure("unavailable", UNAVAILABLE, 503)
        return json_response(
            {
                "error": {
                    "code": "cooldown",
                    "message": "Wait before connecting again.",
                    "retry_after_seconds": seconds,
                }
            },
            429,
        )
    if error.code not in _FAILURES:
        return failure("unavailable", UNAVAILABLE, 503)
    message, code = _FAILURES[error.code]
    return failure(error.code, message, code)


async def _admitted(
    slots: anyio.CapacityLimiter,
    workers: set[asyncio.Task[Any]],
    work: Callable[[], Coroutine[Any, Any, Response]],
) -> Response:
    """Run work in a shielded worker that holds a slot until it ends, even if the caller leaves."""
    permit = object()
    try:
        slots.acquire_on_behalf_of_nowait(permit)
    except anyio.WouldBlock:
        return failure("overloaded", "Too many messaging requests at once.", 429)
    worker = asyncio.create_task(work())
    workers.add(worker)

    def done(task: asyncio.Task[Response]) -> None:
        workers.discard(task)
        slots.release_on_behalf_of(permit)
        if not task.cancelled():
            task.exception()

    worker.add_done_callback(done)
    return await asyncio.shield(worker)


def messaging_routes(
    *,
    runtime: MessagingRuntime,
    authorized: Callable[[Request], bool],
    origins: frozenset[str],
    workers: set[asyncio.Task[Any]],
) -> list[Route]:
    slots = anyio.CapacityLimiter(MAX_ACTION_REQUESTS)

    def status() -> Response:
        return json_response(runtime.status())

    async def perform(request: Request, provider: Provider | None, enabled: bool) -> Response:
        refused = await _drained(request)
        if refused is not None:
            return refused
        if provider is None:
            return status()
        try:
            # Runtime Disable revokes authority before its action lock or cleanup can await.
            await runtime.set_enabled(provider, enabled)
        except MessagingError as error:
            return _answer(error)
        except Exception:
            return failure("unavailable", UNAVAILABLE, 503)
        return status()

    async def endpoint(
        request: Request, provider: Provider | None = None, enabled: bool = False
    ) -> Response:
        response = _refused(request, authorized, origins)
        if response is None:
            response = (
                await _admitted(slots, workers, lambda: perform(request, provider, enabled))
                if provider is not None
                else await perform(request, None, False)
            )
        response.headers["Cache-Control"] = "no-store"
        return response

    def action(provider: Provider, enabled: bool) -> Callable[[Request], Awaitable[Response]]:
        async def bound(request: Request) -> Response:
            return await endpoint(request, provider, enabled)

        return bound

    return [
        Route("/api/v2/messaging", endpoint, methods=["GET"]),
        Route("/api/v2/messaging/slack/enable", action("slack", True), methods=["POST"]),
        Route("/api/v2/messaging/slack/disable", action("slack", False), methods=["POST"]),
        Route("/api/v2/messaging/discord/enable", action("discord", True), methods=["POST"]),
        Route("/api/v2/messaging/discord/disable", action("discord", False), methods=["POST"]),
        Route("/api/v2/messaging/mattermost/enable", action("mattermost", True), methods=["POST"]),
        Route(
            "/api/v2/messaging/mattermost/disable", action("mattermost", False), methods=["POST"]
        ),
    ]


def messaging_settings_routes(
    *,
    runtime: MessagingRuntime,
    authorized: Callable[[Request], bool],
    origins: frozenset[str],
    workers: set[asyncio.Task[Any]],
    settings: MessagingConfigStore,
    factory_for: Callable[[Config], BridgeFactory],
) -> list[Route]:
    """Saved settings: read the non-secret values, save, remove; no response carries a token."""
    slots = anyio.CapacityLimiter(MAX_ACTION_REQUESTS)

    def invalid_config() -> Response:
        return failure("invalid_config", *_FAILURES["invalid_config"])

    async def source(provider: Provider) -> dict[str, Any]:
        try:
            saved = await asyncio.to_thread(settings.load, provider)
        except MessagingStoreError as error:
            logger.warning("messaging settings unavailable (%s)", error.code)
            return {"source": "unreadable", "values": {}}
        if saved is None:
            return {"source": "none", "values": {}}
        return {"source": "saved", "values": public_values(saved.config)}

    async def show(request: Request) -> Response:
        response = _refused(request, authorized, origins)
        if response is None:
            response = await _drained(request)
        if response is None:
            try:
                response = json_response(
                    {provider: await source(provider) for provider in _PROVIDERS}
                )
            except Exception:
                response = failure("unavailable", UNAVAILABLE, 503)
        response.headers["Cache-Control"] = "no-store"
        return response

    async def apply(
        provider: Provider, factory: BridgeFactory | None, commit: Callable[[], Awaitable[None]]
    ) -> Response:
        try:
            # The runtime stops a running bridge before it waits, then commits under its lock.
            await runtime.configure(provider, factory, commit)
        except MessagingError as error:
            return _answer(error)
        except Exception:
            return failure("unavailable", UNAVAILABLE, 503)
        return json_response(runtime.status())

    async def save(request: Request, provider: Provider) -> Response:
        body = bytearray()
        try:
            with anyio.fail_after(INTAKE_TIMEOUT_S):
                async for chunk in request.stream():
                    if len(chunk) > MAX_CONFIG_BODY - len(body):
                        return failure("invalid_input", INVALID, 400)
                    body.extend(chunk)
        except TimeoutError:
            return failure("intake_timeout", "The request did not arrive in time.", 408)
        except ClientDisconnect:
            return failure("disconnected", "The request was interrupted.", 400)
        try:
            value = json.loads(body.decode("utf-8"), object_pairs_hook=unique_object)
        except ValueError, RecursionError:
            return failure("invalid_input", INVALID, 400)
        names = FIELDS[provider]
        if (
            not isinstance(value, dict)
            or not set(value) <= set(names)
            or any(name not in value for name in names if name not in SECRETS)
            or any(type(item) is not str or len(item) > MAX_CONFIG_VALUE for item in value.values())
        ):
            return failure("invalid_input", INVALID, 400)
        values: dict[str, str] = dict(value)
        kept = [name for name in names if name not in values]  # Only a secret may be omitted.
        saved = None
        try:
            if kept:
                try:
                    saved = await asyncio.to_thread(settings.load, provider)
                except MessagingStoreError:
                    saved = None
                if saved is None:
                    return invalid_config()
                values |= {name: fields(saved.config)[name] for name in kept}
            for owner, name, code, valid in _TOKEN_CHECKS:
                if (
                    provider == owner
                    and name not in kept
                    and values[name]
                    and not valid(values[name])
                ):
                    return failure(code, *_FAILURES[code])
            try:
                config = build(provider, values)
            except ValueError:
                return invalid_config()
            if saved is not None and not _keeps(saved.config, config, kept):
                return invalid_config()
            factory = factory_for(config)
        except Exception:
            return failure("unavailable", UNAVAILABLE, 503)

        def write() -> None:
            if kept:
                # Runs under the provider lock: a Save or Remove that committed after this
                # request read the kept token wins, and this one refuses rather than restore it
                # or send it to another server.
                current = settings.load(provider)
                if current is None or not _keeps(current.config, config, kept):
                    raise RuntimeError("settings_changed")
            settings.save(provider, config)

        async def commit() -> None:
            await asyncio.to_thread(write)

        return await apply(provider, factory, commit)

    async def remove(request: Request, provider: Provider) -> Response:
        refused = await _drained(request)
        if refused is not None:
            return refused

        async def commit() -> None:
            await asyncio.to_thread(settings.remove, provider)

        return await apply(provider, None, commit)

    def edit(provider: Provider) -> Callable[[Request], Awaitable[Response]]:
        async def bound(request: Request) -> Response:
            saving = request.method == "PUT"
            response = _refused(request, authorized, origins, json_body=saving)
            if response is None:
                response = await _admitted(
                    slots,
                    workers,
                    lambda: save(request, provider) if saving else remove(request, provider),
                )
            response.headers["Cache-Control"] = "no-store"
            return response

        return bound

    return [
        Route("/api/v2/messaging/config", show, methods=["GET"]),
        *(
            Route(f"/api/v2/messaging/{provider}/config", edit(provider), methods=["PUT", "DELETE"])
            for provider in _PROVIDERS
        ),
    ]
