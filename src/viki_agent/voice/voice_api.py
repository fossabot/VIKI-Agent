"""Authenticated, admission-bounded REST routes for dictation, and the `/ws/voice` route.

`GET /api/v2/voice` reads the status view and `POST /api/v2/voice/fallback/clear` clears a GPU
fallback. `POST /api/v2/voice/handsfree/report` logs a fixed hands-free event/code pair at
WARNING and answers `{"ok": true}`. With a `Voicebox`, `GET /api/v2/voice/voicebox` reads
its status and `POST /api/v2/voice/voicebox/speech` synthesizes speech (ADR-047). Each service
call runs in a shielded task of its own: a client that leaves does not cancel an engine close
halfway. Every answer carries `Cache-Control: no-store`, and a refusal holds a fixed message,
never an exception's text.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import unicodedata
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any

import anyio
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import BaseRoute, Route
from starlette.websockets import WebSocket

from viki_agent.api_json import json_response
from viki_agent.code import roots_api
from viki_agent.code.checks_api import _EMPTY_BODY
from viki_agent.code.code_files import CodeError
from viki_agent.code.integration_api import _query
from viki_agent.code.roots_api import _body
from viki_agent.voice.voice_pack_install import PackRefused
from viki_agent.voice.voice_pack_service import VoicePack
from viki_agent.voice.voice_service import VoiceService
from viki_agent.voice.voice_socket import voice_socket_route
from viki_agent.voice.voicebox import Voicebox
from viki_agent.voice.voicebox_http import CODES, LANGUAGES, MAX_TEXT_CHARS, VoiceboxError

MAX_VOICE_REQUESTS = 4
MAX_BODY_BYTES = 1024
MAX_SPEECH_BODY_BYTES = 4096
_LOGGER = logging.getLogger(__name__)

_STATUS = {
    "unauthorized": 401,
    "forbidden_origin": 403,
    "invalid_query": 400,
    "invalid_body": 400,
    "overloaded": 429,
    "unavailable": 503,
}
_MESSAGES = {code: roots_api._MESSAGES[code] for code in _STATUS}
# Added only after the roots_api comprehension above runs: a Voicebox code inside it would raise
# KeyError at import, since roots_api's own messages know nothing about them.
_STATUS.update({code: 502 for code in CODES})
_STATUS["voicebox_off"] = 409
_STATUS["voicebox_busy"] = 429
_STATUS["voicebox_pack_missing"] = 409
_STATUS["voicebox_pack_failed"] = 502
_SPEECH_FAILED = "Voicebox could not make the speech."
_MESSAGES.update({code: _SPEECH_FAILED for code in CODES})
_MESSAGES["voicebox_off"] = "Choose a Voicebox voice in Settings first."
_MESSAGES["voicebox_busy"] = "Voicebox is busy. Try again in a moment."
_MESSAGES["voicebox_pack_missing"] = "Natural voices aren't installed."
_MESSAGES["voicebox_pack_failed"] = "Natural voices stopped working."
# The voice-pack facade's own refusals (voice_pack_install.PackRefused) and the speech-settings
# opener's platform refusal: neither is a Voicebox code, so neither lives in CODES.
_STATUS["pack_unsupported"] = 409
_STATUS["pack_busy"] = 409
_STATUS["unsupported_platform"] = 409
_MESSAGES["pack_unsupported"] = "Natural voices can't run on this computer."
_MESSAGES["pack_busy"] = "The voice pack is busy. Try again when it finishes."
_MESSAGES["unsupported_platform"] = "Windows speech settings exist only on Windows."

# The hands-free report route's own closed vocabulary: the frontend's `HandsFreeReportEvent`
# values, and every code it may pair with one of them (a Voicebox code, any other refusal code
# this module answers, or one of the frontend's own hands-free/read-aloud reason strings).
_REPORT_EVENTS = frozenset({"dropped", "failed", "fallback", "silent"})
_HANDSFREE_REASONS = frozenset(
    {
        "audio_failed",
        "synth_failed",
        "playback_failed",
        "nothing",
        "unreadable",
        "no_voice",
        "esc",
        "toggle",
        "hidden",
        "session",
        "pagehide",
        "read",
        "view",
        "mic",
    }
)
_REPORT_CODES = frozenset(CODES) | frozenset(_STATUS) | _HANDSFREE_REASONS

Perform = Callable[[Request], Coroutine[Any, Any, Response]]


def _refusal(code: str) -> Response:
    return json_response({"error": {"code": code, "message": _MESSAGES[code]}}, _STATUS[code])


def _text(value: object) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= MAX_TEXT_CHARS
        and value.strip() != ""
        and not any(unicodedata.category(ch) == "Cc" for ch in value)
    )


def _language(value: object) -> bool:
    return isinstance(value, str) and value in LANGUAGES


def _report_event(value: object) -> bool:
    return isinstance(value, str) and value in _REPORT_EVENTS


def _report_code(value: object) -> bool:
    return isinstance(value, str) and value in _REPORT_CODES


_SPEECH_BODY = {"text": _text, "language": _language}
_REPORT_BODY = {"event": _report_event, "code": _report_code}
# The VoicePack actions that take an empty body and answer {"pack": status}; "warm" is its own
# route below, since it does not call a same-named method on the pack itself.
_PACK_ACTIONS = ("check", "install", "repair", "cancel", "remove", "retry")
# The pack states voicebox.warm() is allowed to run in (VoicePack.status()'s "state", once
# installed): a server that is stopped, mid-start or already ready. Everything else (not yet
# installed, installing, removing, unsupported, failed) is left alone.
_WARM_STATES = frozenset({"stopped", "starting", "ready"})


def voice_routes(
    service: VoiceService,
    *,
    authorized: Callable[[Request], bool],
    origins: frozenset[str],
    workers: set[asyncio.Task[Any]],
    authenticate: Callable[[WebSocket], Awaitable[bool]],
    voicebox: Voicebox | None = None,
    pack: VoicePack | None = None,
    opener: Callable[[str], None] | None = None,
    platform: str = sys.platform,
) -> list[BaseRoute]:
    capacity = anyio.CapacityLimiter(MAX_VOICE_REQUESTS)

    async def read_status(request: Request) -> Response:
        _query(request, frozenset(), frozenset())
        return json_response({"voice": await service.status()})

    async def clear_fallback(request: Request) -> Response:
        _query(request, frozenset(), frozenset())
        await _body(request, _EMPTY_BODY, limit=MAX_BODY_BYTES)
        return json_response({"voice": await service.clear_fallback()})

    handsfree_capacity = anyio.CapacityLimiter(MAX_VOICE_REQUESTS)

    async def handsfree_report(request: Request) -> Response:
        _query(request, frozenset(), frozenset())
        body = await _body(request, _REPORT_BODY, limit=MAX_BODY_BYTES)
        _LOGGER.warning("hands-free %s: %s", body["event"], body["code"])
        if pack is not None and body["event"] == "fallback":
            pack.note_fallback(body["code"])
        return json_response({"ok": True})

    async def admitted(
        request: Request, perform: Perform, post: bool, limiter: anyio.CapacityLimiter
    ) -> Response:
        origin = request.headers.get("origin")
        if origin not in origins and (post or origin is not None):
            return _refusal("forbidden_origin")
        permit = object()
        try:
            limiter.acquire_on_behalf_of_nowait(permit)
        except anyio.WouldBlock:
            return _refusal("overloaded")
        worker = asyncio.create_task(perform(request))
        workers.add(worker)

        def done(task: asyncio.Task[Response]) -> None:
            workers.discard(task)
            limiter.release_on_behalf_of(permit)
            if not task.cancelled():
                task.exception()  # a failure is answered below; this only marks it retrieved

        worker.add_done_callback(done)
        try:
            return await asyncio.shield(worker)
        except CodeError as error:
            # Only the query and body checks raise it here, and both fault the client's request.
            return _refusal(error.code if error.code in _STATUS else "invalid_body")
        except Exception as error:
            _LOGGER.warning("voice route failed (%s)", type(error).__name__)
            return _refusal("unavailable")

    def route(
        path: str, perform: Perform, method: str, *, limiter: anyio.CapacityLimiter = capacity
    ) -> Route:
        async def handler(request: Request) -> Response:
            if authorized(request):
                response = await admitted(request, perform, method == "POST", limiter)
            else:
                response = _refusal("unauthorized")
            response.headers["Cache-Control"] = "no-store"
            return response

        return Route(path, handler, methods=[method])

    routes: list[BaseRoute] = [
        route("/api/v2/voice", read_status, "GET"),
        route("/api/v2/voice/fallback/clear", clear_fallback, "POST"),
        route(
            "/api/v2/voice/handsfree/report",
            handsfree_report,
            "POST",
            limiter=handsfree_capacity,
        ),
        voice_socket_route(service, authenticate=authenticate),
    ]
    if voicebox is not None:
        box = voicebox  # narrows Voicebox | None for the two closures below
        # Its own limiter: a speech request can wait on Voicebox's one-at-a-time lock for up to
        # SPEECH_TIMEOUT_S, and sharing `capacity` with it would let four such waits turn every
        # dictation route overloaded too, though dictation never touches Voicebox at all.
        voicebox_capacity = anyio.CapacityLimiter(MAX_VOICE_REQUESTS)

        async def read_voicebox_status(request: Request) -> Response:
            _query(request, frozenset(), frozenset())
            return json_response({"voicebox": await box.status()})

        async def voicebox_speech(request: Request) -> Response:
            _query(request, frozenset(), frozenset())
            body = await _body(request, _SPEECH_BODY, limit=MAX_SPEECH_BODY_BYTES)
            try:
                wav = await box.speech(body["text"], body["language"])
            except VoiceboxError as error:
                _LOGGER.warning("voice speech refused: %s", error.code)
                return _refusal(error.code)
            return Response(wav, media_type="audio/wav")

        routes.append(
            route("/api/v2/voice/voicebox", read_voicebox_status, "GET", limiter=voicebox_capacity)
        )
        routes.append(
            route(
                "/api/v2/voice/voicebox/speech",
                voicebox_speech,
                "POST",
                limiter=voicebox_capacity,
            )
        )
    if pack is not None:
        natural = pack  # narrows VoicePack | None for the closures below
        # Its own limiter, like Voicebox's: an install/repair can sit under this for a long time,
        # and sharing `capacity` with it would starve dictation of its four slots too.
        pack_capacity = anyio.CapacityLimiter(MAX_VOICE_REQUESTS)
        warm_task: asyncio.Task[None] | None = None

        async def read_pack_status(request: Request) -> Response:
            _query(request, frozenset(), frozenset())
            return json_response({"pack": await natural.status()})

        def pack_action(name: str) -> Perform:
            async def perform(request: Request) -> Response:
                _query(request, frozenset(), frozenset())
                await _body(request, _EMPTY_BODY, limit=MAX_BODY_BYTES)
                try:
                    status = await getattr(natural, name)()
                except PackRefused as error:
                    return _refusal(error.code)
                return json_response({"pack": status})

            return perform

        async def pack_warm(request: Request) -> Response:
            nonlocal warm_task
            _query(request, frozenset(), frozenset())
            await _body(request, _EMPTY_BODY, limit=MAX_BODY_BYTES)
            live = warm_task is not None and not warm_task.done()
            if voicebox is not None and not live and voicebox.engine() == "pack":
                status = await natural.status()
                if status["state"] in _WARM_STATES:

                    async def run_warm() -> None:
                        assert voicebox is not None
                        try:
                            await voicebox.warm()
                        except VoiceboxError as error:
                            _LOGGER.warning("voice pack warm failed: %s", error.code)

                    task = asyncio.create_task(run_warm())
                    warm_task = task
                    workers.add(task)

                    def warm_done(task: asyncio.Task[None]) -> None:
                        workers.discard(task)
                        if not task.cancelled():
                            task.exception()  # only marks it retrieved; nothing answers it

                    task.add_done_callback(warm_done)
            return json_response({"pack": await natural.status()})

        routes.append(route("/api/v2/voice/pack", read_pack_status, "GET", limiter=pack_capacity))
        for name in _PACK_ACTIONS:
            routes.append(
                route(
                    f"/api/v2/voice/pack/{name}", pack_action(name), "POST", limiter=pack_capacity
                )
            )
        routes.append(route("/api/v2/voice/pack/warm", pack_warm, "POST", limiter=pack_capacity))
    if opener is not None:

        async def speech_settings(request: Request) -> Response:
            _query(request, frozenset(), frozenset())
            await _body(request, _EMPTY_BODY, limit=MAX_BODY_BYTES)
            if platform != "win32":
                return _refusal("unsupported_platform")
            await asyncio.to_thread(opener, "ms-settings:speech")
            return json_response({"opened": True})

        routes.append(route("/api/v2/voice/speech-settings", speech_settings, "POST"))
    return routes
