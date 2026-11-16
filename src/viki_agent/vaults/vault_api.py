"""Authenticated, admission-bounded read routes for the vault service."""

from __future__ import annotations

import asyncio
import json
import re
import unicodedata
from collections.abc import Callable
from dataclasses import asdict
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import unquote_to_bytes

import anyio
from anyio.to_thread import run_sync
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from viki_agent.api_json import failure, json_response, unique_object
from viki_agent.vaults.vault import (
    NOTE_SUFFIXES,
    MetadataBatch,
    Overview,
    VaultError,
    VaultService,
    safe_ref,
)

MAX_QUERY_BYTES = 1_024
MAX_VAULT_REQUESTS = 4
MAX_EXCERPT_REQUESTS = 2
MAX_METADATA_BODY = 128 * 1024
MAX_METADATA_RESPONSE = 512 * 1024
MAX_OVERVIEW_RESPONSE = 2 * 1024 * 1024
_METADATA_REASONS = (
    "deadline",
    "byte_limit",
    "response_limit",
    "unsupported",
    "truncated",
    "changed",
    "unreadable",
)
_OVERVIEW_REASONS = (
    "deadline",
    "entry_limit",
    "item_limit",
    "byte_limit",
    "response_limit",
    "depth_limit",
    "unsafe_entry",
    "unreadable",
    "changed",
)
EXCERPT_KEYS = frozenset({b"ref", b"start", b"end"})
_LINE_NUMBER = re.compile(rb"[0-9]{1,7}")

_MESSAGES = {
    "invalid_query": "Check the search term.",
    "invalid_ref": "Check the note's location.",
    "private_path": "This location can't be shown.",
    "unknown_workspace": "Vault not found.",
    "missing_note": "Note not found.",
    "unsupported_note": "This file type can't be shown.",
    "note_too_large": "The note is too large to show.",
    "unsafe_path": "This location can't be safely read.",
    "target_busy": "Another app is using it. Close it, then try again.",
    "root_changed": "The vault's location has changed. Open it again.",
    "root_unavailable": "Can't read the vault. Check the connection.",
    "invalid_range": "Check the line range.",
    "excerpt_too_large": "The selected range is too large.",
    "not_active_vault": "Not the open vault.",
}


def _strict_query(request: Request) -> str | None:
    raw = request.scope.get("query_string", b"")
    if not raw:
        return None
    if len(raw) > MAX_QUERY_BYTES or not raw.startswith(b"q=") or b"&" in raw:
        raise VaultError("invalid_query")
    return _decode_query_value(raw[2:])


def _decode_query_value(value: bytes) -> str:
    for index, byte in enumerate(value):
        if byte == 0x25 and (
            index + 2 >= len(value)
            or any(
                character not in b"0123456789abcdefABCDEF"
                for character in value[index + 1 : index + 3]
            )
        ):
            raise VaultError("invalid_query")
    try:
        return unquote_to_bytes(value.replace(b"+", b" ")).decode("utf-8")
    except UnicodeError:
        raise VaultError("invalid_query") from None


def _excerpt_query(request: Request) -> tuple[str, int, int]:
    """`ref`, `start` and `end`, each once and nothing else. A line number is plain ASCII digits,
    1 to 7 of them, never percent-decoded, so no sign, space or exponent reaches `int`."""
    raw = request.scope.get("query_string", b"")
    if len(raw) > MAX_QUERY_BYTES:
        raise VaultError("invalid_query")
    fields: dict[bytes, bytes] = {}
    for pair in raw.split(b"&"):
        key, _, value = pair.partition(b"=")
        if key not in EXCERPT_KEYS or key in fields:
            raise VaultError("invalid_query")
        fields[key] = value
    if b"ref" not in fields:
        raise VaultError("invalid_query")
    start, end = fields.get(b"start", b""), fields.get(b"end", b"")
    if not _LINE_NUMBER.fullmatch(start) or not _LINE_NUMBER.fullmatch(end):
        raise VaultError("invalid_range")
    return _decode_query_value(fields[b"ref"]), int(start), int(end)


def _is_excerpt(request: Request) -> bool:
    workspace_id = request.path_params["workspace_id"]
    return request.scope["path"] == f"/api/v2/vaults/{workspace_id}/excerpt"


def _error(exc: VaultError) -> Response:
    if exc.code in {"unknown_workspace", "missing_note", "private_path"}:
        status = 404
    elif exc.code in {"note_too_large", "excerpt_too_large", "metadata_request_too_large"}:
        status = 413
    elif exc.code == "not_active_vault":
        status = 409
    elif exc.code == "target_busy":
        status = 423
    elif exc.code.startswith("invalid") or exc.code in {"unsupported_note", "unsafe_path"}:
        status = 400
    else:
        status = 503
    return failure(exc.code, _MESSAGES.get(exc.code, "Couldn't read the vault."), status)


def _is_metadata(request: Request) -> bool:
    return request.scope["path"] == f"/api/v2/vaults/{request.path_params['workspace_id']}/metadata"


def _is_overview(request: Request) -> bool:
    return request.scope["path"] == f"/api/v2/vaults/{request.path_params['workspace_id']}/overview"


async def _metadata_refs(request: Request) -> list[str]:
    if request.scope.get("query_string", b""):
        raise VaultError("invalid_metadata_request")
    encoded = bytearray()
    async for chunk in request.stream():
        if len(chunk) > MAX_METADATA_BODY - len(encoded):
            raise VaultError("metadata_request_too_large")
        encoded.extend(chunk)
    try:
        body = json.loads(encoded.decode("utf-8"), object_pairs_hook=unique_object)
    except ValueError, UnicodeError, RecursionError:
        raise VaultError("invalid_metadata_request") from None
    if not isinstance(body, dict) or set(body) != {"refs"}:
        raise VaultError("invalid_metadata_request")
    refs = body["refs"]
    if not isinstance(refs, list) or not 1 <= len(refs) <= 32:
        raise VaultError("invalid_metadata_request")
    seen: set[str] = set()
    for ref in refs:
        if (
            not isinstance(ref, str)
            or ref in seen
            or any(unicodedata.category(char) in {"Cc", "Cs"} for char in ref)
            or PurePosixPath(ref).suffix.casefold() not in NOTE_SUFFIXES
        ):
            raise VaultError("invalid_metadata_request")
        try:
            safe_ref(ref)
        except VaultError as exc:
            if exc.code != "private_path":
                raise VaultError("invalid_metadata_request") from None
        seen.add(ref)
    return refs


def _metadata_size(value: object) -> int:
    # ASCII escaping makes this character count the exact encoded byte count used by api_json.
    return len(json.dumps(value, ensure_ascii=True, separators=(",", ":")))


def _metadata_response(batch: MetadataBatch) -> Response:
    items: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    sizes: list[int] = []
    for item in batch.items:
        value = (
            asdict(item)
            if item.display_metadata is None
            else {
                "ref": item.ref,
                "state": "unknown",
                "reason": "response_limit",
                "read_bytes": item.read_bytes,
                "display_metadata": None,
            }
        )
        items.append(value)
        sizes.append(_metadata_size(value))
        key = value["reason"] if value["state"] == "unknown" else value["state"]
        if key not in {"ready", "absent"}:
            counts[key] = counts.get(key, 0) + 1
    omitted = sum(item["state"] == "unknown" for item in items)
    base = _metadata_size(
        {
            "items": [],
            "partial": False,
            "partial_reasons": [],
            "read_bytes": batch.read_bytes,
            "omitted": 0,
        }
    )

    def overhead(current: dict[str, int], unknown: int) -> int:
        reasons = [reason for reason in _METADATA_REASONS if current.get(reason, 0)]
        return (
            base
            - bool(reasons)
            + len(str(unknown))
            - 1
            + sum(len(reason) + 2 for reason in reasons)
            + max(0, len(reasons) - 1)
        )

    size = sum(sizes) + max(0, len(items) - 1) + overhead(counts, omitted)
    for index, item in enumerate(batch.items):
        if item.display_metadata is None:
            continue
        candidate = asdict(item)
        changed = dict(counts)
        changed["response_limit"] -= 1
        if item.state not in {"ready", "absent"}:
            changed[item.state] = changed.get(item.state, 0) + 1
        wanted = (
            size
            + _metadata_size(candidate)
            - sizes[index]
            + overhead(changed, omitted - 1)
            - overhead(counts, omitted)
        )
        if wanted <= MAX_METADATA_RESPONSE:
            items[index], counts, omitted, size = candidate, changed, omitted - 1, wanted
    reasons = [reason for reason in _METADATA_REASONS if counts.get(reason, 0)]
    response = json_response(
        {
            "items": items,
            "partial": bool(reasons),
            "partial_reasons": reasons,
            "read_bytes": batch.read_bytes,
            "omitted": omitted,
        }
    )
    if len(response.body) > MAX_METADATA_RESPONSE:
        raise VaultError("read_failed")
    return response


def _overview_response(overview: Overview) -> Response:
    """The newest rows whose encoded body fits `MAX_OVERVIEW_RESPONSE`; the rest are omitted."""
    rows = [asdict(note) for note in overview.notes]

    def envelope(dropped: int) -> dict[str, Any]:
        reasons = [
            reason
            for reason in _OVERVIEW_REASONS
            if reason in overview.partial_reasons or (reason == "response_limit" and dropped)
        ]
        return {
            "notes": [],
            "partial": bool(reasons),
            "partial_reasons": reasons,
            "omitted": overview.omitted + dropped,
            "read_bytes": overview.read_bytes,
        }

    kept = size = 0
    for row in rows:
        size += _metadata_size(row) + (kept > 0)  # every row after the first adds a comma
        if size + _metadata_size(envelope(len(rows) - kept - 1)) > MAX_OVERVIEW_RESPONSE:
            break
        kept += 1
    response = json_response({**envelope(len(rows) - kept), "notes": rows[:kept]})
    if len(response.body) > MAX_OVERVIEW_RESPONSE:
        raise VaultError("read_failed")
    return response


def vault_routes(
    service: VaultService,
    *,
    authorized: Callable[[Request], bool],
    origins: frozenset[str],
    workers: set[asyncio.Task[Any]],
) -> list[Route]:
    capacity = anyio.CapacityLimiter(MAX_VAULT_REQUESTS)
    # A Git read can take seconds (GIT_TIMEOUT_S); one at a time keeps it from filling `capacity`.
    history_capacity = anyio.CapacityLimiter(1)
    # Eight excerpt chips must not fill `capacity` and starve `entries`.
    excerpt_capacity = anyio.CapacityLimiter(MAX_EXCERPT_REQUESTS)
    metadata_capacity = anyio.CapacityLimiter(1)
    # The overview's reads run up to OVERVIEW_DEADLINE_SECONDS, so it takes none of `capacity`.
    overview_capacity = anyio.CapacityLimiter(1)

    async def perform(request: Request) -> Response:
        workspace_id = request.path_params["workspace_id"]
        if _is_metadata(request):
            refs = await _metadata_refs(request)
            return _metadata_response(await run_sync(service.metadata, workspace_id, refs))
        if _is_overview(request):
            if request.scope.get("query_string", b""):
                raise VaultError("invalid_query")
            return _overview_response(await run_sync(service.overview, workspace_id))
        if _is_excerpt(request):
            ref, start, end = _excerpt_query(request)
            return json_response(
                asdict(await run_sync(service.excerpt, workspace_id, ref, start, end))
            )
        query = _strict_query(request)
        if request.path_params.get("history_ref") is not None:
            if query is not None:
                raise VaultError("invalid_query")
            result = await run_sync(
                service.history, workspace_id, request.path_params["history_ref"]
            )
            return json_response(
                {"commits": [asdict(commit) for commit in result.commits], "state": result.state}
            )
        if request.path_params.get("backlink_ref") is not None:
            if query is not None:
                raise VaultError("invalid_query")
            result = await run_sync(
                service.backlinks, workspace_id, request.path_params["backlink_ref"]
            )
            return json_response(
                {
                    "backlinks": [asdict(item) for item in result.backlinks],
                    "partial": result.partial,
                    "partial_reasons": result.partial_reasons,
                    "scanned_bytes": result.scanned_bytes,
                    "omitted": result.omitted,
                }
            )
        if request.path_params.get("ref") is not None:
            if query is not None:
                raise VaultError("invalid_query")
            result = await run_sync(service.inspect, workspace_id, request.path_params["ref"])
            return json_response(
                {
                    "note": asdict(result.note),
                    "resolved_links": [asdict(link) for link in result.links],
                    "partial": result.partial,
                    "partial_reasons": result.partial_reasons,
                }
            )
        if query is not None:
            result = await run_sync(service.search, workspace_id, query)
            return json_response(
                {
                    "hits": [asdict(hit) for hit in result.hits],
                    "partial": result.partial,
                    "partial_reasons": result.partial_reasons,
                    "scanned_bytes": result.scanned_bytes,
                    "omitted": result.omitted,
                }
            )
        result = await run_sync(service.scan, workspace_id)
        return json_response(
            {
                "entries": [asdict(entry) for entry in result.entries],
                "partial": result.partial,
                "partial_reasons": result.partial_reasons,
                "scanned_bytes": result.scanned_bytes,
                "visited_items": result.visited_items,
                "omitted": result.omitted,
            }
        )

    async def endpoint(request: Request) -> Response:
        if not authorized(request):
            return failure("unauthorized", "Can't authenticate. Open VIKI Agent again.", 401)
        origin = request.headers.get("origin")
        if origin is not None and origin not in origins:
            return failure("forbidden_origin", "This window can't show that.", 403)
        permit = object()
        # A route's own limiter comes first, so its refused request never touches `capacity`.
        if _is_metadata(request):
            wanted = (metadata_capacity, capacity)
        elif _is_overview(request):
            wanted = (overview_capacity,)
        elif request.path_params.get("history_ref") is not None:
            wanted = (history_capacity, capacity)
        elif _is_excerpt(request):
            wanted = (excerpt_capacity, capacity)
        else:
            wanted = (capacity,)
        held: list[anyio.CapacityLimiter] = []
        for limiter in wanted:
            try:
                limiter.acquire_on_behalf_of_nowait(permit)
            except anyio.WouldBlock:
                for taken in held:
                    taken.release_on_behalf_of(permit)
                return failure(
                    "overloaded",
                    "Busy processing the vault. Wait a moment, then try again.",
                    429,
                )
            held.append(limiter)
        worker = asyncio.create_task(perform(request))
        workers.add(worker)

        def done(task: asyncio.Task[Response]) -> None:
            workers.discard(task)
            for taken in held:
                taken.release_on_behalf_of(permit)
            if not task.cancelled():
                task.exception()

        worker.add_done_callback(done)
        try:
            return await asyncio.shield(worker)
        except VaultError as exc:
            return _error(exc)
        except TimeoutError:
            return failure("timeout", "The read didn't finish in time.", 408)

    return [
        Route("/api/v2/vaults/{workspace_id}/metadata", endpoint, methods=["POST"]),
        Route("/api/v2/vaults/{workspace_id}/overview", endpoint, methods=["GET"]),
        Route(
            "/api/v2/vaults/{workspace_id}/backlinks/{backlink_ref:path}",
            endpoint,
            methods=["GET"],
        ),
        Route(
            "/api/v2/vaults/{workspace_id}/history/{history_ref:path}",
            endpoint,
            methods=["GET"],
        ),
        Route("/api/v2/vaults/{workspace_id}/excerpt", endpoint, methods=["GET"]),
        Route("/api/v2/vaults/{workspace_id}/entries", endpoint, methods=["GET"]),
        Route("/api/v2/vaults/{workspace_id}/notes/{ref:path}", endpoint, methods=["GET"]),
    ]
