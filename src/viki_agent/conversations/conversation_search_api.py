"""Private bounded HTTP boundary for revisioned conversation search."""

from __future__ import annotations

import asyncio
import contextlib
import json
import threading
import unicodedata
import uuid
from collections.abc import Callable
from typing import Any

import anyio
from anyio.to_thread import run_sync
from starlette.requests import ClientDisconnect, Request
from starlette.responses import Response
from starlette.routing import Route

from viki_agent.api_json import failure, json_response, unique_object
from viki_agent.conversations.artifact_details import DETAIL_BYTES, ArtifactDetails
from viki_agent.conversations.conversation_search import ConversationSearch
from viki_agent.conversations.tool_sources import ToolSources

MAX_BODY = 2048
MAX_QUERY = 256
MAX_REQUESTS = 2


def _uuid(value: object) -> str:
    if not isinstance(value, str) or len(value) != 36:
        raise ValueError
    try:
        if str(uuid.UUID(value)) == value:
            return value
    except ValueError:
        pass
    raise ValueError


def _anchor(value: object) -> dict[str, Any]:
    if (
        not isinstance(value, dict)
        or set(value) != {"kind", "entry_uuid", "mapped_index", "source_revision"}
        or value["kind"] != "native_entry"
        or type(value["mapped_index"]) is not int
        or not 0 <= value["mapped_index"] <= 63
        or not isinstance(value["source_revision"], str)
        or len(value["source_revision"]) != 64
        or any(c not in "0123456789abcdef" for c in value["source_revision"])
    ):
        raise ValueError
    _uuid(value["entry_uuid"])
    return value


def _constant(name: str) -> Any:
    raise ValueError(name)


def routes(
    service: ConversationSearch,
    *,
    authorized: Callable[[Request], bool],
    origins: frozenset[str],
    workers: set[asyncio.Task[Any]],
    artifacts: ArtifactDetails | None = None,
    tool_sources: ToolSources | None = None,
) -> list[Route]:
    capacity = anyio.CapacityLimiter(MAX_REQUESTS)

    async def read_work(
        request: Request, call: Callable[[Callable[[], bool]], dict[str, Any]]
    ) -> dict[str, Any]:
        cancelled = threading.Event()

        async def watch_disconnect() -> None:
            while True:
                if (await request.receive())["type"] == "http.disconnect":
                    cancelled.set()
                    return

        watcher = asyncio.create_task(watch_disconnect())
        try:
            return await run_sync(lambda: call(cancelled.is_set))
        finally:
            cancelled.set()
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watcher

    async def work(request: Request) -> Response:
        sid = _uuid(request.path_params["session_id"])
        if request.method == "GET":
            query = request.query_params
            if set(query) != {"q", "request_id"} or any(len(query.getlist(k)) != 1 for k in query):
                raise ValueError
            q = query.get("q")
            request_id = _uuid(query.get("request_id"))
            if q is None or len(q) > MAX_QUERY:
                raise ValueError
            q = unicodedata.normalize("NFKC", q).strip()
            if not q:
                raise ValueError
            result = await read_work(request, lambda cancelled: service.search(sid, q, cancelled))
            result["request_id"] = request_id
        else:
            if (
                request.scope.get("query_string", b"")
                or request.headers.get("content-type", "").split(";", 1)[0].lower()
                != "application/json"
            ):
                raise ValueError
            if any(
                encoding.strip().lower() != "identity"
                for encoding in request.headers.getlist("content-encoding")
            ):
                raise ValueError
            length = request.headers.get("content-length")
            if length is not None and (
                not length.isascii() or not length.isdecimal() or int(length) > MAX_BODY
            ):
                raise ValueError
            body = bytearray()
            try:
                with anyio.fail_after(5):
                    async for chunk in request.stream():
                        if len(chunk) > MAX_BODY - len(body):
                            return failure("too_large", "Request is too large.", 413)
                        body.extend(chunk)
            except TimeoutError:
                return failure("intake_timeout", "Request timed out.", 408)
            try:
                raw = json.loads(
                    bytes(body).decode("utf-8"),
                    object_pairs_hook=unique_object,
                    parse_constant=_constant,
                )
            except RecursionError:
                raise ValueError from None
            detail = request.url.path.endswith("/artifact-detail")
            sources = request.url.path.endswith("/tool-sources")
            keys = (
                {"request_id", "ref", "cursor"}
                if detail
                else {"request_id", "tool_use_ids"}
                if sources
                else {"request_id", "anchor"}
            )
            if not isinstance(raw, dict) or set(raw) != keys:
                raise ValueError
            request_id = _uuid(raw["request_id"])
            if detail:
                ref, cursor = raw["ref"], raw["cursor"]
                if (
                    not isinstance(ref, dict)
                    or set(ref) != {"entry_uuid", "tool_use_id", "source_revision"}
                    or not isinstance(ref["tool_use_id"], str)
                    or not 1 <= len(ref["tool_use_id"]) <= 160
                    or len(ref["tool_use_id"].encode("utf-8")) > 512
                    or type(cursor) is not int
                    or not 0 <= cursor <= DETAIL_BYTES
                    or artifacts is None
                ):
                    raise ValueError
                _anchor(
                    {
                        "kind": "native_entry",
                        "entry_uuid": ref["entry_uuid"],
                        "mapped_index": 0,
                        "source_revision": ref["source_revision"],
                    }
                )
                detail_service = artifacts
                result = await read_work(
                    request, lambda cancelled: detail_service.page(sid, ref, cursor, cancelled)
                )
            elif sources:
                ids = raw["tool_use_ids"]
                if (
                    not isinstance(ids, list)
                    or not 1 <= len(ids) <= 16
                    or any(
                        not isinstance(item, str)
                        or not 1 <= len(item) <= 160
                        or len(item.encode("utf-8")) > 512
                        for item in ids
                    )
                    or len(set(ids)) != len(ids)
                    or tool_sources is None
                ):
                    raise ValueError
                sources_service = tool_sources
                result = await read_work(
                    request, lambda cancelled: sources_service.lookup(sid, ids, cancelled)
                )
            else:
                anchor = _anchor(raw["anchor"])
                result = await read_work(
                    request, lambda cancelled: service.anchor(sid, anchor, cancelled)
                )
            result["request_id"] = request_id
        return json_response(result)

    async def _endpoint(request: Request) -> Response:
        if not authorized(request):
            return failure("unauthorized", "Authentication is required.", 401)
        origin = request.headers.get("origin")
        if (origin is not None and origin not in origins) or (
            request.method == "POST" and origin not in origins
        ):
            return failure("forbidden_origin", "This origin is not allowed.", 403)
        token = object()
        try:
            capacity.acquire_on_behalf_of_nowait(token)
        except anyio.WouldBlock:
            return failure("overloaded", "Conversation search is busy.", 429)
        task = asyncio.create_task(work(request))
        workers.add(task)

        def done(future: asyncio.Task[Response]) -> None:
            workers.discard(future)
            capacity.release_on_behalf_of(token)
            if not future.cancelled():
                future.exception()

        task.add_done_callback(done)
        try:
            return await asyncio.shield(task)
        except ValueError, UnicodeError, json.JSONDecodeError:
            return failure("invalid_input", "The request is invalid.", 400)
        except ClientDisconnect:
            return failure("disconnected", "The request was interrupted.", 400)
        except Exception:
            return failure("unavailable", "Conversation search is unavailable.", 503)

    async def endpoint(request: Request) -> Response:
        response = await _endpoint(request)
        response.headers["Cache-Control"] = "no-store"
        return response

    result_routes = [
        Route("/api/v2/sessions/{session_id}/conversation-search", endpoint, methods=["GET"]),
        Route("/api/v2/sessions/{session_id}/history-anchor", endpoint, methods=["POST"]),
    ]
    if artifacts is not None:
        result_routes.append(
            Route("/api/v2/sessions/{session_id}/artifact-detail", endpoint, methods=["POST"])
        )
    if tool_sources is not None:
        result_routes.append(
            Route("/api/v2/sessions/{session_id}/tool-sources", endpoint, methods=["POST"])
        )
    return result_routes
