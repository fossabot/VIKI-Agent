"""Read-only routes over the Bot memory Claude Code keeps in VIKI Agent's data folder.

`<data>/bot-memory/<workspace id>/` holds the memory folders of a read-only or protected vault's
Bots. The routes answer in the vault API's wire shapes (entries, notes, metadata), with refs
`<bot>/<file>`, through `VaultService`'s bounded, handle-checked reads over that area as the root.
They create and write nothing.
"""

from __future__ import annotations

import asyncio
import stat
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any

import anyio
from anyio.to_thread import run_sync
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from viki_agent.api_json import failure, json_response
from viki_agent.domain import CanonicalRoot
from viki_agent.messaging.bot_memory import app_memory_area
from viki_agent.vaults.vault import ScanResult, VaultError, VaultService, _is_reparse, safe_ref
from viki_agent.vaults.vault_api import _error, _metadata_refs, _metadata_response
from viki_agent.workspaces import WorkspaceError, WorkspaceService, canonical_root

MAX_MEMORY_REQUESTS = 2
BASE = "/api/v2/bot-memory/{workspace_id}"


class BotMemoryArea(VaultService):
    """`VaultService` reads rooted at a registered vault's memory area instead of the vault."""

    def __init__(self, workspaces: WorkspaceService, data: Path) -> None:
        super().__init__(workspaces)
        self._data = data

    def folder(self, workspace_id: str) -> Path:
        try:
            return app_memory_area(self._data, workspace_id)
        except ValueError:
            raise VaultError("unknown_workspace") from None

    def _root(self, workspace_id: str) -> CanonicalRoot:
        area = self.folder(workspace_id)
        try:
            self._workspaces.get(workspace_id)
        except WorkspaceError as exc:
            raise VaultError("unknown_workspace") from exc
        # Claude Code creates these levels; a link, junction or file at either is refused here,
        # and every read re-walks the whole chain, so a swap after this check fails closed too.
        for level in (area.parent, area):
            try:
                info = level.lstat()
            except FileNotFoundError:
                raise VaultError("missing_folder") from None
            except OSError as exc:
                raise VaultError("root_unavailable") from exc
            if _is_reparse(info) or not stat.S_ISDIR(info.st_mode):
                raise VaultError("unsafe_path")
        try:
            return canonical_root(str(area), deny=())
        except WorkspaceError as exc:
            raise VaultError("root_unavailable") from exc

    def open_target(self, workspace_id: str, ref: str) -> Path | None:
        """The path of a readable Markdown note in the area, else None."""
        try:
            root = self._root(workspace_id)
            self._read_root(root, ref)
        except VaultError:
            return None
        return Path(root.display, *safe_ref(ref))


def bot_memory_routes(
    area: BotMemoryArea,
    *,
    authorized: Callable[[Request], bool],
    origins: frozenset[str],
    workers: set[asyncio.Task[Any]],
) -> list[Route]:
    capacity = anyio.CapacityLimiter(MAX_MEMORY_REQUESTS)
    metadata_capacity = anyio.CapacityLimiter(1)

    async def perform(request: Request) -> Response:
        workspace_id = request.path_params["workspace_id"]
        if request.method == "POST":
            refs = await _metadata_refs(request)
            return _metadata_response(await run_sync(area.metadata, workspace_id, refs))
        if request.scope.get("query_string", b""):
            raise VaultError("invalid_query")
        ref = request.path_params.get("ref")
        if ref is not None:
            note = await run_sync(area.read, workspace_id, ref)
            return json_response(
                {
                    "note": asdict(note),
                    "resolved_links": [],
                    "partial": False,
                    "partial_reasons": [],
                }
            )
        folder = str(area.folder(workspace_id))
        try:
            result = await run_sync(area.scan, workspace_id)
        except VaultError as exc:
            if exc.code != "missing_folder":
                raise
            result = ScanResult((), False, (), 0, 0, 0)
        return json_response(
            {
                "entries": [asdict(entry) for entry in result.entries],
                "partial": result.partial,
                "partial_reasons": result.partial_reasons,
                "scanned_bytes": result.scanned_bytes,
                "visited_items": result.visited_items,
                "omitted": result.omitted,
                "folder": folder,
            }
        )

    async def admitted(request: Request) -> Response:
        permit = object()
        wanted = (metadata_capacity, capacity) if request.method == "POST" else (capacity,)
        held: list[anyio.CapacityLimiter] = []
        for limiter in wanted:
            try:
                limiter.acquire_on_behalf_of_nowait(permit)
            except anyio.WouldBlock:
                for taken in held:
                    taken.release_on_behalf_of(permit)
                return failure("overloaded", "Busy reading Bot memory. Try again shortly.", 429)
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
            # The area appears with Claude Code's first memory save; until then no note exists.
            return _error(VaultError("missing_note") if exc.code == "missing_folder" else exc)

    async def endpoint(request: Request) -> Response:
        origin = request.headers.get("origin")
        if not authorized(request):
            response = failure("unauthorized", "Can't authenticate. Open VIKI Agent again.", 401)
        elif origin is not None and origin not in origins:
            response = failure("forbidden_origin", "This window can't show that.", 403)
        else:
            response = await admitted(request)
        response.headers["Cache-Control"] = "no-store"
        return response

    return [
        Route(BASE + "/metadata", endpoint, methods=["POST"]),
        Route(BASE + "/entries", endpoint, methods=["GET"]),
        Route(BASE + "/notes/{ref:path}", endpoint, methods=["GET"]),
    ]
