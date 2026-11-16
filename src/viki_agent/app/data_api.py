"""HTTP routes and services for data export, import, locations and reset."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any

import anyio
from anyio.to_thread import run_sync
from starlette.requests import ClientDisconnect, Request
from starlette.responses import Response
from starlette.routing import Route

from viki_agent.api_json import failure as _failure
from viki_agent.api_json import json_response as _json
from viki_agent.api_json import unique_object as _unique
from viki_agent.app.exports import ExportError, ExportService
from viki_agent.app.imports import ImportService, ImportServiceError
from viki_agent.app.settings import SettingsConflict, SettingsError, to_view
from viki_agent.conversations.drafts import draft_scope
from viki_agent.domain import ActiveWorkspace, Workspace
from viki_agent.protection import ACCOUNT_HINT, ProtectionError
from viki_agent.storage import SCHEMA_VERSION, SessionStore, StateError
from viki_agent.workspaces import WorkspaceError, scratch_path

MAX_REQUESTS = 4
# Generous wire cap for intake; ImportService.preview() enforces the real 256 KiB content rule
# against the decoded string, which is smaller than its JSON-escaped, enveloped wire form.
MAX_BODY_BYTES = 1 << 20
BODY_TIMEOUT_S = 5.0
MEASURE_MAX_ENTRIES = 20_000
MEASURE_TIMEOUT_S = 2.0
MEASURE_TOTAL_TIMEOUT_S = 10.0  # every item of one document together; the client waits 30 s
_FIXED_MEASURES = 5  # logs, Edge profile, scratch, provider home and assets
_VALID_RESET_CATEGORIES = {
    "settings",
    "drafts",
    "commands",
    "approvals",
    "scratch",
    "edge_profile",
}

logger = logging.getLogger(__name__)


def _uuid(value: Any) -> bool:
    if not isinstance(value, str) or len(value) != 36:
        return False
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False


def _measure(
    path: Path, *, max_entries: int = MEASURE_MAX_ENTRIES, timeout_s: float = MEASURE_TIMEOUT_S
) -> tuple[int, bool]:
    """Sum apparent file sizes under path; stop on either budget and report partial."""
    if not path.exists():
        return 0, False
    start = time.monotonic()
    total = 0
    seen = 0
    partial = False
    stack = [path]
    while stack and not partial:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    seen += 1
                    if seen > max_entries or time.monotonic() - start > timeout_s:
                        partial = True
                        break
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total, partial


class LocationsService:
    def __init__(
        self,
        *,
        workspaces: Callable[[], Sequence[Workspace]],
        data_dir: Path,
        claude_home: Path,
        scratch_in_use: Callable[[], bool],
    ) -> None:
        self._workspaces = workspaces
        self._data_dir = data_dir
        self._claude_home = claude_home
        self._scratch_in_use = scratch_in_use

    async def document(self) -> Mapping[str, object]:
        return await run_sync(self._build)

    def _build(self) -> dict[str, object]:
        vaults = [workspace for workspace in self._workspaces() if workspace.kind == "vault"]
        # Registrations accumulate and nothing forgets them, so 2 s per item is no bound on the
        # document: a dozen vaults outlast the browser's fixed timeout, and the abandoned walk
        # holds one of this route's four permits until it finishes anyway. One budget shared out
        # keeps the whole answer a constant the client's timeout can be set against.
        budget = min(MEASURE_TIMEOUT_S, MEASURE_TOTAL_TIMEOUT_S / (len(vaults) + _FIXED_MEASURES))
        vault_items: list[dict[str, object]] = []
        for workspace in vaults:
            root = Path(workspace.root.display)
            available = root.is_dir()
            size, partial = _measure(root, timeout_s=budget) if available else (0, False)
            vault_items.append(
                {
                    "workspace_id": workspace.workspace_id,
                    "name": workspace.name,
                    "path": workspace.root.display,
                    "policy": workspace.policy,
                    "available": available,
                    "bytes": size,
                    "partial": partial,
                }
            )
        db_path = self._data_dir / "app.sqlite3"
        logs_dir = self._data_dir / "logs"
        edge_dir = self._data_dir / "edge"
        scratch_dir = scratch_path(self._data_dir)
        legacy_path = self._data_dir / "state.json"
        # storage._backup names each migration backup beside the database; there is no folder.
        backups = [
            path
            for path in self._data_dir.glob(f"{db_path.name}.v*-backup-*.sqlite3")
            if path.is_file()
        ]
        backups_size = sum(path.stat().st_size for path in backups)
        logs_size, _ = _measure(logs_dir, timeout_s=budget)
        edge_size, edge_partial = _measure(edge_dir, timeout_s=budget)
        scratch_size, _ = _measure(scratch_dir, timeout_s=budget)
        claude_size, claude_partial = _measure(self._claude_home, timeout_s=budget)
        assets_root = self._data_dir / "assets"
        assets_size, assets_partial = _measure(assets_root, timeout_s=budget)
        return {
            "categories": [
                {"id": "vaults", "items": vault_items},
                {
                    "id": "app_data",
                    "items": [
                        {
                            "kind": "database",
                            "path": str(db_path),
                            "bytes": db_path.stat().st_size if db_path.is_file() else 0,
                            "schema": SCHEMA_VERSION,
                        },
                        {
                            "kind": "backups",
                            "path": str(self._data_dir),
                            "count": len(backups),
                            "bytes": backups_size,
                        },
                        {"kind": "logs", "path": str(logs_dir), "bytes": logs_size},
                        {
                            "kind": "edge_profile",
                            "path": str(edge_dir),
                            "bytes": edge_size,
                            "partial": edge_partial,
                        },
                        {
                            "kind": "scratch",
                            "path": str(scratch_dir),
                            "bytes": scratch_size,
                            "in_use": self._scratch_in_use(),
                            "shared": True,
                        },
                        {
                            "kind": "legacy_state",
                            "path": str(legacy_path),
                            "bytes": legacy_path.stat().st_size if legacy_path.is_file() else 0,
                        },
                    ],
                },
                {
                    "id": "provider",
                    "items": [
                        {
                            "provider": "claude",
                            "kind": "home",
                            "path": str(self._claude_home),
                            "contains": ["transcripts", "session_registry", "settings"],
                            "read_by_app": True,
                            "bytes": claude_size,
                            "partial": claude_partial,
                        },
                        {
                            "provider": "claude",
                            "kind": "credentials",
                            "path": None,
                            "read_by_app": False,
                        },
                    ],
                },
                {"id": "indexes", "items": []},
                {
                    "id": "assets",
                    "items": [
                        {"path": str(assets_root), "bytes": assets_size, "partial": assets_partial}
                    ]
                    if assets_root.is_dir()
                    else [],
                },
            ]
        }


class ResetService:
    def __init__(
        self,
        *,
        store: SessionStore,
        active_workspace: Callable[[], ActiveWorkspace],
        settings_reset: Callable[[str], object],
        scratch_root: Path,
        scratch_in_use: Callable[[], bool],
        owner: str,
    ) -> None:
        self._store = store
        self._active_workspace = active_workspace
        self._settings_reset = settings_reset
        self._scratch_root = scratch_root
        self._scratch_in_use = scratch_in_use
        # This launch's ledger owner, the one fact that tells a live in-flight row from a dead
        # launch's. Passed straight through; the store does the length check.
        self._owner = owner

    @property
    def owner(self) -> str:
        """The launch owner this reset narrows on — the one `create_app` must also stamp on
        the command and approval ledgers it builds alongside this service (server.py)."""
        return self._owner

    def reset(
        self,
        *,
        command_id: str,
        categories: Sequence[str],
        on_commands_committed: Callable[[], None] | None = None,
    ) -> dict[str, object]:
        """`on_commands_committed` is called in this thread right after a `commands` deletion
        that removed rows has committed, whatever a later category then does: that deletion
        can release Bot code copies (`SessionStore.delete_scope_rows`), and the caller settles
        them outside the transaction."""
        active = self._active_workspace()
        scope = draft_scope(active.cwd)
        results: dict[str, dict[str, object]] = {}
        for category in categories:
            results[category] = self._reset_one(category, command_id, scope)
            if category == "commands" and results[category]["count"] and on_commands_committed:
                on_commands_committed()
        return {"scope_of": active.workspace.workspace_id, "results": results}

    def _reset_one(self, category: str, command_id: str, scope: str) -> dict[str, object]:
        if category == "edge_profile":
            # The app serves from inside this profile; it can never delete it while running.
            return {"status": "refused", "reason": "in_use", "count": 0}
        if category == "scratch":
            if self._scratch_in_use():
                return {"status": "refused", "reason": "in_use", "count": 0}
            count, partial = self._clear_scratch()
            return {"status": "partial" if partial else "done", "count": count}
        if category == "settings":
            try:
                self._settings_reset(command_id)
            except SettingsError as exc:
                return {"status": "refused", "reason": exc.code, "count": 0}
            return {"status": "done", "count": 1}
        if category in ("drafts", "commands", "approvals"):
            # What each table keeps is the store's rule, not this service's: `commands` keeps the
            # turns this launch admitted and has not finished, `approvals` keeps everything this
            # launch recorded (a native request gets one choice, ever), `drafts` keeps
            # nothing. Rows a dead launch left behind carry another owner and go in every case.
            deleted, kept = self._store.delete_scope_rows(category, scope, self._owner)
            if kept:
                return {"status": "partial", "count": deleted, "kept": kept}
            return {"status": "done", "count": deleted}
        return {"status": "refused", "reason": "unknown_category", "count": 0}

    def _clear_scratch(self) -> tuple[int, bool]:
        try:
            entries = list(self._scratch_root.iterdir())
        except OSError:
            return 0, False
        count = 0
        partial = False
        for entry in entries:
            try:
                if entry.is_symlink() or entry.is_junction():
                    # Never follow a reparse point out of scratch: rmtree/rmdir on it could act
                    # on whatever it points at instead of just the pointer, so it is left
                    # untouched and counted as an entry this reset could not remove.
                    partial = True
                    continue
                if entry.is_dir():
                    shutil.rmtree(entry)
                else:
                    entry.unlink()
                count += 1
            except OSError:
                partial = True
                continue
        return count, partial


_EXPORT_STATUS = {
    "invalid_kind": 400,
    "invalid_include": 400,
    "invalid_path": 400,
    "denied_destination": 403,
    "preview_expired": 404,
    "exists": 409,
    "command_conflict": 409,
    "unavailable": 503,
}
_IMPORT_STATUS = {
    "invalid_format": 400,
    "too_large": 413,
    "preview_expired": 404,
    "unsupported_format": 422,
    "invalid_selection": 400,
}
# Import apply forwards to SettingsService.patch: the same codes and body as PATCH /settings.
_SETTINGS_STATUS = {
    "invalid_settings": 400,
    "stale_revision": 409,
    "command_conflict": 409,
    "read_only": 409,
    "settings_too_large": 413,
}
_WORKSPACE_STATUS = {"unknown_workspace": 404}
_PROTECTION_MESSAGE = (
    "Couldn't protect the save data. Check the save location's access rights and "
    + ACCOUNT_HINT
    + "."
)


def data_routes(
    locations: LocationsService,
    exports: ExportService,
    imports: ImportService,
    reset: ResetService,
    *,
    authorized: Callable[[Request], bool],
    origins: frozenset[str],
    workers: set[asyncio.Task[Any]],
    copies_released: Callable[[], None] | None = None,
) -> list[Route]:
    """`copies_released` runs on the loop after a reset whose `commands` deletion committed,
    so the Bot code copies it released are settled outside the transaction."""
    capacity = anyio.CapacityLimiter(MAX_REQUESTS)

    async def endpoint(request: Request) -> Response:
        if not authorized(request):
            return _failure("unauthorized", "Can't authenticate. Open VIKI Agent again.", 401)
        origin = request.headers.get("origin")
        if (origin is not None and origin not in origins) or (
            request.method == "POST" and origin not in origins
        ):
            return _failure("forbidden_origin", "This window can't do that.", 403)
        permit = object()
        try:
            capacity.acquire_on_behalf_of_nowait(permit)
        except anyio.WouldBlock:
            return _failure("overloaded", "Busy processing the data request. Wait a moment.", 429)
        worker = asyncio.create_task(perform(request))
        workers.add(worker)

        def done(task: asyncio.Task[Any]) -> None:
            workers.discard(task)
            capacity.release_on_behalf_of(permit)
            if not task.cancelled():
                task.exception()

        worker.add_done_callback(done)
        try:
            return await asyncio.shield(worker)
        except ExportError as exc:
            status = _EXPORT_STATUS.get(exc.code, 503)
            if status >= 500:
                logger.warning("export refused: %s", exc.code)
            return _failure(exc.code, str(exc), status)
        except ImportServiceError as exc:
            status = _IMPORT_STATUS.get(exc.code, 503)
            if status >= 500:
                logger.warning("import refused: %s", exc.code)
            return _failure(exc.code, str(exc), status)
        except SettingsConflict as exc:
            error = {
                "code": exc.code,
                "message": exc.message,
                "current_revision": exc.current_revision,
                "current": to_view(exc.current),
            }
            return _json({"error": error}, 409)
        except SettingsError as exc:
            status = _SETTINGS_STATUS.get(exc.code, 503)
            body: dict[str, Any] = {"code": exc.code, "message": exc.message}
            if exc.fields:
                body["fields"] = exc.fields
            body.update(exc.extra)
            if status >= 500:
                logger.warning("data request refused: %s", exc.code)
            return _json({"error": body}, status)
        except WorkspaceError as exc:
            return _failure(exc.code, exc.message, _WORKSPACE_STATUS.get(exc.code, 503))
        except StateError as exc:
            logger.warning("data request refused: %s", exc.code)
            return _failure(exc.code, exc.message, 400 if exc.code == "invalid_input" else 503)
        except ProtectionError:
            logger.warning("data request refused: protection")
            return _failure("unavailable", _PROTECTION_MESSAGE, 503)
        except TimeoutError:
            return _failure("intake_timeout", "Receiving the request took too long.", 408)
        except ClientDisconnect:
            return _failure("disconnected", "Receiving the request was interrupted.", 400)
        except Exception as exc:
            # Anything untyped is a defect, not a refusal: the class name is the whole record.
            logger.warning("data request failed: %s", type(exc).__name__)
            return _failure("unavailable", "Couldn't complete the data operation.", 503)

    async def read_body(request: Request, limit: int) -> dict[str, Any] | None:
        if (
            request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            != "application/json"
            or request.headers.get("content-encoding", "identity").lower() != "identity"
        ):
            return None
        length = request.headers.get("content-length")
        if length is not None:
            if not length.isascii() or not length.isdecimal() or len(length) > 10:
                return None
            if int(length) > limit:
                return None
        body = bytearray()
        with anyio.fail_after(BODY_TIMEOUT_S):
            async for chunk in request.stream():
                if len(chunk) > limit - len(body):
                    return None
                body.extend(chunk)
        try:
            value = json.loads(body.decode("utf-8"), object_pairs_hook=_unique)
        except ValueError, RecursionError:
            # A pathologically deep body is malformed input, not a 500: Python's own JSON
            # scanner recurses per nesting level and can exhaust the recursion limit before
            # ever raising a plain decode error.
            return None
        return value if isinstance(value, dict) else None

    async def perform(request: Request) -> Response:
        path = request.url.path
        if path == "/api/v2/data/locations":
            return _json(dict(await locations.document()))
        if path == "/api/v2/exports/preview":
            return await do_export_preview(request)
        if path == "/api/v2/exports":
            return await do_export_write(request)
        if path == "/api/v2/imports/preview":
            return await do_import_preview(request)
        if path == "/api/v2/data/reset":
            return await do_reset(request)
        if path.startswith("/api/v2/imports/") and path.endswith("/apply"):
            return await do_import_apply(request)
        return _failure("not_found", "Operation not found.", 404)

    async def do_export_preview(request: Request) -> Response:
        body = await read_body(request, MAX_BODY_BYTES)
        if (
            body is None
            or set(body) - {"kind", "include"} != set()
            or body.get("kind") not in ("diagnostics", "settings")
        ):
            return _failure("invalid_body", "Check the export request.", 400)
        include = body.get("include")
        if include is not None and not isinstance(include, dict):
            return _failure("invalid_body", "Check the export request.", 400)
        preview = await run_sync(lambda: exports.preview(body["kind"], include))
        return _json(_export_preview_json(preview))

    async def do_export_write(request: Request) -> Response:
        body = await read_body(request, MAX_BODY_BYTES)
        if (
            body is None
            or set(body) != {"command_id", "preview_id", "destination"}
            or not _uuid(body["command_id"])
            or not _uuid(body["preview_id"])
            or not isinstance(body["destination"], str)
            or not 1 <= len(body["destination"]) <= 4096
        ):
            return _failure("invalid_body", "Check the export destination.", 400)
        result, replayed = await run_sync(
            lambda: exports.write(
                command_id=body["command_id"],
                preview_id=body["preview_id"],
                destination=body["destination"],
            )
        )
        return _json(
            {**asdict(result), "command_id": body["command_id"], "replayed": replayed},
            200 if replayed else 201,
        )

    async def do_import_preview(request: Request) -> Response:
        body = await read_body(request, MAX_BODY_BYTES)
        if (
            body is None
            or set(body) != {"kind", "content"}
            or body["kind"] != "settings"
            or not isinstance(body["content"], str)
        ):
            return _failure("invalid_body", "Check the import content.", 400)
        preview = await run_sync(lambda: imports.preview(body["kind"], body["content"]))
        return _json(_import_preview_json(preview))

    async def do_import_apply(request: Request) -> Response:
        preview_id = request.path_params.get("preview_id")
        if not isinstance(preview_id, str) or not _uuid(preview_id):
            return _failure("invalid_id", "Unknown preview.", 400)
        body = await read_body(request, MAX_BODY_BYTES)
        if (
            body is None
            or set(body) != {"command_id", "expected_revision", "keys"}
            or not _uuid(body["command_id"])
            or type(body["expected_revision"]) is not int
            or body["expected_revision"] < 0
            or not isinstance(body["keys"], list)
            or not all(isinstance(key, str) for key in body["keys"])
        ):
            return _failure("invalid_body", "Check the import request.", 400)
        settings, replayed = await run_sync(
            lambda: imports.apply(
                preview_id,
                command_id=body["command_id"],
                expected_revision=body["expected_revision"],
                keys=body["keys"],
            )
        )
        return _json({**to_view(settings), "command_id": body["command_id"], "replayed": replayed})

    async def do_reset(request: Request) -> Response:
        body = await read_body(request, MAX_BODY_BYTES)
        if (
            body is None
            or set(body) != {"command_id", "categories", "confirmation"}
            or not _uuid(body["command_id"])
            or not isinstance(body["categories"], list)
            or not body["categories"]
            or not all(isinstance(c, str) for c in body["categories"])
            or len(set(body["categories"])) != len(body["categories"])
        ):
            return _failure("invalid_body", "Check the reset request.", 400)
        if body["confirmation"] != "RESET":
            return _failure("invalid_confirmation", "Type RESET exactly.", 400)
        if any(c not in _VALID_RESET_CATEGORIES for c in body["categories"]):
            return _failure("invalid_categories", "Unknown reset category.", 400)
        # Set in the worker thread; read here, on the loop, even when a later category raised.
        committed = threading.Event()
        try:
            result = await run_sync(
                lambda: reset.reset(
                    command_id=body["command_id"],
                    categories=body["categories"],
                    on_commands_committed=committed.set,
                )
            )
        finally:
            if committed.is_set() and copies_released is not None:
                copies_released()
        return _json(result)

    return [
        Route("/api/v2/data/locations", endpoint, methods=["GET"]),
        Route("/api/v2/exports/preview", endpoint, methods=["POST"]),
        Route("/api/v2/exports", endpoint, methods=["POST"]),
        Route("/api/v2/imports/preview", endpoint, methods=["POST"]),
        Route("/api/v2/imports/{preview_id}/apply", endpoint, methods=["POST"]),
        Route("/api/v2/data/reset", endpoint, methods=["POST"]),
    ]


def _export_preview_json(preview: Any) -> dict[str, object]:
    return {
        "preview_id": preview.preview_id,
        "kind": preview.kind,
        "files": [asdict(file) for file in preview.files],
        "content": {preview.content[0]: preview.content[1]},
        "expires_at": preview.expires_at,
    }


def _import_preview_json(preview: Any) -> dict[str, object]:
    return {
        "preview_id": preview.preview_id,
        "content_hash": preview.content_hash,
        "settings_revision": preview.settings_revision,
        "format_version": preview.format_version,
        "supported": preview.supported,
        "unsupported_reason": preview.unsupported_reason,
        "provenance": dict(preview.provenance),
        "changes": [asdict(change) for change in preview.changes],
        "workspaces": [asdict(row) for row in preview.workspaces],
        "executable_definitions": [dict(item) for item in preview.executable_definitions],
        "duplicate_ids": list(preview.duplicate_ids),
    }
