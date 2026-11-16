"""Private, bounded routine routes; only `.../run` leads to a provider process, through the task
runner."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import anyio
from anyio.to_thread import run_sync
from starlette.requests import ClientDisconnect, Request
from starlette.responses import Response
from starlette.routing import Route

from viki_agent.api_json import failure, json_response, unique_object
from viki_agent.app.settings import SettingsError
from viki_agent.automation.routines import (
    RevisionPlan,
    RoutineError,
    RoutineService,
    _uuid,
    parse_document,
    parse_stamp,
    stamp,
)
from viki_agent.automation.scheduler import Scheduler, next_five
from viki_agent.automation.tasks import TaskRunner
from viki_agent.profiles.service import ProfileError
from viki_agent.protection import ProtectionError
from viki_agent.storage import StateError

MAX_BODY = 128 << 10
MAX_RESPONSE = 2 << 20
PREVIEW = "/api/v2/routines/preview"

_STATUS = {
    "not_found": 404,
    **dict.fromkeys(
        ("conflict", "command_conflict", "invalid_transition", "run_active", "busy"), 409
    ),
    "not_ready": 422,
    "capacity": 429,
    **dict.fromkeys(("unavailable", "corrupt", "protection", "newer_version"), 503),
}


def _keys(value: object, keys: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise RoutineError("invalid_input")
    return value


def _item(row: dict[str, Any], five: list[dict[str, Any]], runs: dict[str, Any]) -> dict[str, Any]:
    """One routine's list fields; only an enabled routine has a next run."""
    run = runs.get(row["routine_id"], {})
    return {
        **{
            key: row[key]
            for key in ("routine_id", "name", "state", "current_revision", "state_revision")
        },
        "schedule": {key: row["schedule"][key] for key in ("kind", "timezone")},
        "next_run": next((slot["at"] for slot in five if slot["at"]), None)
        if row["state"] == "enabled"
        else None,
        "last_run": run.get("last_run"),
        "last_attempt": run.get("last_attempt"),
        "active_run": run.get("active_run"),
    }


def routine_routes(
    service: RoutineService,
    runner: TaskRunner,
    scheduler: Scheduler,
    *,
    authorized: Callable[[Request], bool],
    origins: frozenset[str],
    workers: set[asyncio.Task[Any]],
) -> list[Route]:
    capacity = anyio.CapacityLimiter(4)

    def summaries() -> dict[str, dict[str, Any]]:
        """The store's summaries, each open attempt with `stop_by` in place of its claim time
        and revision: the claim time plus that revision's deadline. The runner's deadline runs
        from the run's start on a monotonic clock, so this is approximate."""
        runs = runner.store.summaries()
        for routine_id, run in runs.items():
            active = run["active_run"]
            if active is not None:
                claimed, revision = active.pop("created_at"), active.pop("revision")
                try:
                    minutes = service.frozen(routine_id, revision)["deadline_minutes"]
                    active["stop_by"] = stamp(parse_stamp(claimed) + timedelta(minutes=minutes))
                except ValueError:  # RoutineError included: an unreadable revision
                    active["stop_by"] = None
        return runs

    def rehearsal(document: dict[str, Any]) -> dict[str, Any]:
        return {
            "readiness": {
                "blockers": service.readiness(document, scheduled=True),
                "run_blockers": service.readiness(document, scheduled=False),
                "warnings": service.warnings(document),
            },
            "resolved": service.resolve(document),
        }

    def preview(raw: object) -> dict[str, Any]:
        """A rehearsal of `raw` that writes nothing: its next five slots as one revision that
        governs from now, its readiness and what a run would bind."""
        document = parse_document(raw)[0]
        now = scheduler.clock.wall()
        plan = RevisionPlan(1, stamp(now), document["schedule"])
        return {"next_five": next_five((plan,), now), **rehearsal(document)}

    def listing() -> dict[str, Any]:
        now = scheduler.clock.wall()
        plans = {plan.routine_id: plan.revisions for plan in service.plans()}
        runs = summaries()
        return {
            "routines": [
                _item(row, next_five(plans.get(row["routine_id"], ()), now), runs)
                for row in service.list()
            ],
            "scheduler": scheduler.status(),
        }

    def detail(routine_id: str) -> dict[str, Any]:
        row = service.get(routine_id)
        document = row["document"]
        five = next_five(service.plan(routine_id).revisions, scheduler.clock.wall())
        item = _item(
            {**row, "name": document["name"], "schedule": document["schedule"]},
            five,
            summaries(),
        )
        return {
            **item,
            "document": document,
            "tested_revision": row["tested_revision"],
            "activates_at": row["activates_at"],
            "next_five": five,
            **rehearsal(document),
        }

    async def perform(request: Request) -> Response:
        status = 200
        if len(request.scope.get("query_string", b"")) > 512:
            raise RoutineError("invalid_input")
        query = request.query_params
        if any(len(query.getlist(key)) != 1 for key in query):
            raise RoutineError("invalid_input")
        routine_id = request.path_params.get("routine_id")
        action = request.path_params.get("action")
        if routine_id is not None:
            _uuid(routine_id)
        if request.method in {"GET", "HEAD"}:
            if routine_id is not None and action == "runs":
                limit = query.get("limit", "50")
                if set(query) - {"limit", "before"} or not re.fullmatch("[1-9][0-9]?", limit):
                    raise RoutineError("invalid_input")
                runs = await run_sync(
                    lambda: runner.store.runs(routine_id, int(limit), query.get("before"))
                )
                result: Any = {"runs": runs}
            elif query:
                raise RoutineError("invalid_input")
            elif action is not None:
                return failure("not_found", "Routine action is unavailable.", 404)
            elif routine_id is None:
                result = await run_sync(listing)
            else:
                result = await run_sync(lambda: detail(routine_id))
        else:
            if (
                query
                or request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                != "application/json"
            ):
                raise RoutineError("invalid_input")
            if any(
                item.strip().lower() != "identity"
                for item in request.headers.getlist("content-encoding")
            ):
                raise RoutineError("invalid_input")
            body = bytearray()
            with anyio.fail_after(5):
                async for chunk in request.stream():
                    if len(chunk) > MAX_BODY - len(body):
                        return failure("too_large", "Routine request is too large.", 413)
                    body.extend(chunk)
            try:
                value = json.loads(body.decode("utf-8"), object_pairs_hook=unique_object)
            except ValueError, RecursionError:
                raise RoutineError("invalid_input") from None
            if request.url.path == PREVIEW:
                raw = _keys(value, {"document"})
                result = await run_sync(lambda: preview(raw["document"]))
                created = False
            elif routine_id is None and request.method == "POST":
                raw = _keys(value, {"command_id", "document"})
                result, created = await run_sync(
                    lambda: service.create(raw["command_id"], raw["document"])
                )
                status = 201 if created else 200
            elif routine_id is not None and request.method == "PATCH":
                raw = _keys(value, {"command_id", "expected_revision", "document"})
                result, created = await run_sync(
                    lambda: service.revise(
                        routine_id, raw["command_id"], raw["expected_revision"], raw["document"]
                    )
                )
            elif action in {"enable", "pause", "archive"}:
                raw = _keys(value, {"command_id", "expected_state_revision"})
                change = getattr(service, action)
                result, created = await run_sync(
                    lambda: change(routine_id, raw["command_id"], raw["expected_state_revision"])
                )
            elif routine_id is not None and action == "run":
                raw = _keys(value, {"command_id", "expected_revision"})
                attempt, created = await runner.run_now(
                    routine_id,
                    raw["command_id"],
                    raw["expected_revision"],
                    scheduler_ready=lambda: scheduler.state == "running",
                )
                result = {
                    "occurrence_id": attempt.occurrence_id,
                    "attempt_id": attempt.attempt_id,
                    "session_id": attempt.session_id,
                }
                status = 202 if created else 200
            else:
                return failure("not_found", "Routine action is unavailable.", 404)
            if created:
                scheduler.wake()
        response = json_response(result, status=status)
        if len(response.body) > MAX_RESPONSE:
            return failure("too_large", "Routine response is too large.", 413)
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
            return failure("overloaded", "Routine operations are busy.", 429)
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
        except RoutineError as error:
            message = "The routine operation could not be completed."
            status = _STATUS.get(error.code, 400)
            if error.code == "not_ready":
                return json_response(
                    {
                        "error": {
                            "code": error.code,
                            "message": message,
                            "blockers": list(error.blockers),
                        }
                    },
                    status,
                )
            return failure(error.code, message, status)
        except StateError, ProtectionError, ProfileError, SettingsError:
            return failure("unavailable", "Routine storage is unavailable.", 503)
        except TimeoutError:
            return failure("intake_timeout", "The request timed out.", 408)
        except ClientDisconnect:
            return failure("disconnected", "The request was interrupted.", 400)

    return [
        Route("/api/v2/routines", endpoint, methods=["GET", "POST"]),
        # Before `{routine_id}`, which would read "preview" as an id.
        Route(PREVIEW, endpoint, methods=["POST"]),
        Route("/api/v2/routines/{routine_id}", endpoint, methods=["GET", "PATCH"]),
        Route("/api/v2/routines/{routine_id}/{action}", endpoint, methods=["GET", "POST"]),
    ]
