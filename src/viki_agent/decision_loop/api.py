"""The Decision routes under `/api/v2/decision/`: the trust boundary in front of
`DecisionLoopService`.

Stored data uses GET `data` for its summary, POST `data/records` for a filtered page,
POST `data/record` for one detail, and POST `data/delete` for selected rows.

Admission runs in this order: the Bearer (401 `unauthorized`), then an Origin from `origins`,
which only a GET may omit (403 `forbidden_origin`), then one of `MAX_DECISION_REQUESTS` permits
(429 `busy`). An admitted request runs as a task kept in `workers` and shielded from the
request's own cancellation; that task maps each failure to its response itself, so a request
cancelled before its call ends leaves the event loop no exception to report, and the task's end
releases the permit. A query string is refused (400). A
route with parameters takes one JSON object without a duplicate key, sent as uncompressed
`application/json` of at most `MAX_DECISION_BODY_BYTES` bytes (`MAX_KEY_BODY_BYTES` for the key,
`MAX_PROPOSAL_BODY_BYTES` for a proposed question wording), and hands it to its `wire.parse_*`
parser, which takes exactly its keys; a route without
parameters takes no body and no content type, as the client sends it. A wrong representation is
415, a body past its cap 413, one not read within `BODY_TIMEOUT_S` 408 (`intake_timeout`), and
any other refused body 400. A path id (a call's, a mission's, a proposal's, a seat's routine or a
code folder's) is a canonical UUID, else 404 `unknown_item`. The service's methods are awaited on
the event loop, the presence report's included.

Every response, refusals included, is `Cache-Control: no-store`, and a refusal is
`{"error": {"code", "message"}}` with a fixed English message. A `DecisionLoopError` keeps its
code when `_ERRORS` lists it, and a `MissionError` maps onto the same list (`_MISSION_CODES`); a
store failure, an unlisted code and anything unexpected answer 503 `unavailable`, the unexpected
with one fixed log line. No response or log line holds the key, a body or an exception's text,
and the service's views name no model id.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import anyio
from starlette.requests import ClientDisconnect, Request
from starlette.responses import Response
from starlette.routing import Route

from viki_agent.api_json import failure, json_response, unique_object
from viki_agent.decision_loop import wire
from viki_agent.decision_loop.contracts import DecisionLoopError
from viki_agent.decision_loop.missions import MissionError
from viki_agent.decision_loop.service import DecisionLoopService
from viki_agent.storage import StateError

log = logging.getLogger(__name__)

MAX_DECISION_REQUESTS = 4
MAX_DECISION_BODY_BYTES = 4096
MAX_KEY_BODY_BYTES = 8192  # a key of up to 4,096 characters, with room for its JSON
MAX_PROPOSAL_BODY_BYTES = 8192  # a wording of up to 3,000 canonical JSON characters, and its name
BODY_TIMEOUT_S = 5.0

# Each refusal code: its status and its fixed English message.
_ERRORS: dict[str, tuple[int, str]] = {
    "invalid_request": (400, "The request is invalid."),
    "unknown_item": (404, "This item was not found."),
    "not_applicable": (409, "That does not apply to this item now."),
    "limit": (409, "The daily limit is reached."),
    "too_large": (413, "The request is too large."),
    "invalid_representation": (415, "Send the request as uncompressed JSON, or with no body."),
    "busy": (429, "Decision is busy. Try again in a moment."),
    "unavailable": (503, "Decision is unavailable right now."),
}
# A refused mission start's code (`missions.MISSION_ERROR_CODES`) as one of `_ERRORS`.
_MISSION_CODES = {
    "limit": "limit",
    "busy": "busy",
    "off": "not_applicable",
    "from_mission": "not_applicable",
    "no_work": "not_applicable",
    "no_folder": "not_applicable",
    "not_sent": "unavailable",
    "tried": "not_applicable",
}


class _Refused(Exception):
    """A request refused before the service sees it; `code` is one of `_ERRORS`."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class _Stalled(Exception):
    """The body was not read within `BODY_TIMEOUT_S`: answered 408."""


def _refusal(code: str) -> Response:
    """`code`'s status and fixed message; a code `_ERRORS` does not list answers `unavailable`."""
    if code not in _ERRORS:
        code = "unavailable"
    status, message = _ERRORS[code]
    return failure(code, message, status)


def _identity(request: Request) -> bool:
    return request.headers.get("content-encoding", "identity").strip().lower() == "identity"


async def _read(request: Request, limit: int) -> bytes:
    """The body: `too_large` once it runs past `limit` bytes, `_Stalled` when it takes longer
    than `BODY_TIMEOUT_S`."""
    encoded = bytearray()
    try:
        with anyio.fail_after(BODY_TIMEOUT_S):
            async for chunk in request.stream():
                if len(chunk) > limit - len(encoded):
                    raise _Refused("too_large")
                encoded.extend(chunk)
    except TimeoutError:
        raise _Stalled from None
    return bytes(encoded)


async def _json_object(request: Request, limit: int = MAX_DECISION_BODY_BYTES) -> dict[str, Any]:
    """The body as one JSON object without a duplicate key, sent as uncompressed
    `application/json` of at most `limit` bytes; its keys are the route parser's to check."""
    content_type = request.headers.get("content-type")
    if (
        not _identity(request)
        or content_type is None
        or content_type.split(";")[0].strip().lower() != "application/json"
    ):
        raise _Refused("invalid_representation")
    encoded = await _read(request, limit)
    try:
        value = json.loads(encoded.decode("utf-8"), object_pairs_hook=unique_object)
    except ValueError, RecursionError:
        raise _Refused("invalid_request") from None
    if not isinstance(value, dict):
        raise _Refused("invalid_request")
    return value


async def _no_body(request: Request) -> None:
    """A route without parameters takes no content type, no encoding and an empty body, as the
    client sends it; a body is read up to the cap, so a larger one is `too_large`."""
    if request.headers.get("content-type") is not None or not _identity(request):
        raise _Refused("invalid_representation")
    if await _read(request, MAX_DECISION_BODY_BYTES):
        raise _Refused("invalid_request")


def _path_id(request: Request, name: str) -> str:
    """The path's `name` as a canonical UUID (lower-case hex, hyphenated 8-4-4-4-12), else
    `unknown_item`: no other spelling names a call, a mission or a proposal."""
    value = str(request.path_params[name])
    try:
        canonical = len(value) == 36 and str(uuid.UUID(value)) == value
    except ValueError:
        canonical = False
    if not canonical:
        raise _Refused("unknown_item")
    return value


def decision_loop_routes(
    service: DecisionLoopService,
    *,
    authorized: Callable[[Request], bool],
    origins: frozenset[str],
    workers: set[asyncio.Task[Any]],
) -> list[Route]:
    """The Decision routes over `service`; see the module docstring. `authorized` checks a
    request's Bearer, `origins` lists the Origins a request may come from, and `workers` keeps
    each admitted request's task until it ends."""
    capacity = anyio.CapacityLimiter(MAX_DECISION_REQUESTS)

    async def launcher(request: Request) -> Response:
        await _no_body(request)
        return json_response(await service.launcher())

    async def open_launcher(request: Request) -> Response:
        await _no_body(request)
        return json_response(await service.open_launcher())

    async def presence(request: Request) -> Response:
        await service.presence(*wire.parse_presence(await _json_object(request)))
        return Response(status_code=204)

    async def feedback(request: Request) -> Response:
        body = await _json_object(request)
        return json_response(await service.feedback(wire.parse_feedback(body)))

    async def observe(request: Request) -> Response:
        await service.observe(*wire.parse_observe(await _json_object(request)))
        return Response(status_code=204)

    async def reports(request: Request) -> Response:
        body = await _json_object(request)
        return json_response(await service.report(*wire.parse_report(body)))

    async def page(request: Request) -> Response:
        await _no_body(request)
        return json_response(await service.page())

    async def data(request: Request) -> Response:
        await _no_body(request)
        return json_response(await service.data())

    async def data_records(request: Request) -> Response:
        query = wire.parse_data_query(await _json_object(request), delete=False)
        return json_response(await service.records(query))

    async def data_record(request: Request) -> Response:
        kind, record_id = wire.parse_data_record(await _json_object(request))
        return json_response(await service.record(kind, record_id))

    async def data_delete(request: Request) -> Response:
        query = wire.parse_data_query(await _json_object(request), delete=True)
        return json_response(await service.delete(query))

    async def preferences(request: Request) -> Response:
        body = await _json_object(request)
        return json_response(await service.preference(*wire.parse_preference(body)))

    async def delete_episodes(request: Request) -> Response:
        await _no_body(request)
        return json_response(await service.delete_all())

    async def save_key(request: Request) -> Response:
        key = wire.parse_key(await _json_object(request, MAX_KEY_BODY_BYTES))
        return json_response(await service.save_key(key))

    async def remove_key(request: Request) -> Response:
        await _no_body(request)
        return json_response(await service.remove_key())

    async def test_key(request: Request) -> Response:
        await _no_body(request)
        return json_response(await service.test())

    async def calls(request: Request) -> Response:
        await _no_body(request)
        return json_response(await service.calls())

    async def call(request: Request) -> Response:
        call_id = _path_id(request, "call_id")
        await _no_body(request)
        return json_response(await service.call(call_id))

    async def missions(request: Request) -> Response:
        await _no_body(request)
        return json_response(await service.missions())

    async def start_mission(request: Request) -> Response:
        body = await _json_object(request)
        return json_response(await service.start_mission(*wire.parse_mission_start(body)))

    async def stop_mission(request: Request) -> Response:
        mission_id = _path_id(request, "mission_id")
        await _no_body(request)
        return json_response(await service.stop_mission(mission_id))

    async def stop_missions(request: Request) -> Response:
        await _no_body(request)
        return json_response(await service.stop_missions())

    async def start_seat(request: Request) -> Response:
        routine_id = _path_id(request, "routine_id")
        await _no_body(request)
        return json_response(await service.start_seat(routine_id))

    async def stop_seat(request: Request) -> Response:
        routine_id = _path_id(request, "routine_id")
        await _no_body(request)
        return json_response(await service.stop_seat(routine_id))

    async def run_checks(request: Request) -> Response:
        workspace_id = _path_id(request, "workspace_id")
        await _no_body(request)
        return json_response(await service.run_checks(workspace_id))

    async def stop_checks(request: Request) -> Response:
        workspace_id = _path_id(request, "workspace_id")
        await _no_body(request)
        return json_response(await service.stop_checks(workspace_id))

    async def suggest_inbox(request: Request) -> Response:
        body = await _json_object(request)
        return json_response(await service.suggest_inbox(wire.parse_inbox_suggest(body)))

    async def create_proposal(request: Request) -> Response:
        body = await _json_object(request, MAX_PROPOSAL_BODY_BYTES)
        return json_response(await service.create_proposal(*wire.parse_proposal(body)))

    async def proposal(request: Request) -> Response:
        proposal_id = _path_id(request, "proposal_id")
        await _no_body(request)
        return json_response(await service.proposal(proposal_id))

    async def act_proposal(request: Request) -> Response:
        proposal_id = _path_id(request, "proposal_id")
        body = await _json_object(request)
        act = wire.parse_proposal_act(body)
        return json_response(await service.act_proposal(proposal_id, *act))

    async def act_change(request: Request) -> Response:
        proposal_id = _path_id(request, "proposal_id")
        body = await _json_object(request)
        act = wire.parse_proposal_act(body)
        return json_response(await service.act_change(proposal_id, *act))

    def admitted(
        handler: Callable[[Request], Awaitable[Response]],
    ) -> Callable[[Request], Awaitable[Response]]:
        async def respond(request: Request) -> Response:
            # The admitted task maps each failure itself: once its request is cancelled, the
            # shield hands an exception the task ends with to the loop's exception log, text
            # and all.
            try:
                if request.scope.get("query_string"):
                    raise _Refused("invalid_request")
                return await handler(request)
            except (_Refused, DecisionLoopError) as exc:
                return _refusal(exc.code)
            except MissionError as exc:
                return _refusal(_MISSION_CODES.get(exc.code, "unavailable"))
            except _Stalled:
                return failure("intake_timeout", "The request timed out.", 408)
            except ClientDisconnect:
                return failure("disconnected", "The request was interrupted.", 400)
            except StateError:
                return _refusal("unavailable")
            except Exception:
                log.warning("decision route failed")  # never the exception: it may hold text
                return _refusal("unavailable")

        async def admit(request: Request) -> Response:
            permit = object()
            try:
                capacity.acquire_on_behalf_of_nowait(permit)
            except anyio.WouldBlock:
                return _refusal("busy")
            worker = asyncio.create_task(respond(request))
            workers.add(worker)

            def done(task: asyncio.Task[Response]) -> None:
                workers.discard(task)
                capacity.release_on_behalf_of(permit)

            worker.add_done_callback(done)
            return await asyncio.shield(worker)

        async def endpoint(request: Request) -> Response:
            if not authorized(request):
                response = failure("unauthorized", "Authentication is required.", 401)
            else:
                origin = request.headers.get("origin")
                # A state-changing request needs a listed Origin; only a GET may omit it.
                if origin not in origins and (origin is not None or request.method != "GET"):
                    response = failure("forbidden_origin", "This origin is not allowed.", 403)
                else:
                    response = await admit(request)
            response.headers["Cache-Control"] = "no-store"
            return response

        return endpoint

    return [
        Route("/api/v2/decision/launcher", admitted(launcher), methods=["GET"]),
        Route("/api/v2/decision/launcher/open", admitted(open_launcher), methods=["POST"]),
        Route("/api/v2/decision/presence", admitted(presence), methods=["POST"]),
        Route("/api/v2/decision/feedback", admitted(feedback), methods=["POST"]),
        Route("/api/v2/decision/observe", admitted(observe), methods=["POST"]),
        Route("/api/v2/decision/reports", admitted(reports), methods=["POST"]),
        Route("/api/v2/decision/page", admitted(page), methods=["GET"]),
        Route("/api/v2/decision/data", admitted(data), methods=["GET"]),
        Route("/api/v2/decision/data/records", admitted(data_records), methods=["POST"]),
        Route("/api/v2/decision/data/record", admitted(data_record), methods=["POST"]),
        Route("/api/v2/decision/data/delete", admitted(data_delete), methods=["POST"]),
        Route("/api/v2/decision/preferences", admitted(preferences), methods=["POST"]),
        Route("/api/v2/decision/episodes", admitted(delete_episodes), methods=["DELETE"]),
        Route("/api/v2/decision/key", admitted(save_key), methods=["POST"]),
        Route("/api/v2/decision/key", admitted(remove_key), methods=["DELETE"]),
        Route("/api/v2/decision/test", admitted(test_key), methods=["POST"]),
        Route("/api/v2/decision/calls", admitted(calls), methods=["GET"]),
        Route("/api/v2/decision/calls/{call_id}", admitted(call), methods=["GET"]),
        Route("/api/v2/decision/missions", admitted(missions), methods=["GET"]),
        Route("/api/v2/decision/missions", admitted(start_mission), methods=["POST"]),
        Route("/api/v2/decision/missions/stop", admitted(stop_missions), methods=["POST"]),
        Route(
            "/api/v2/decision/missions/{mission_id}/stop",
            admitted(stop_mission),
            methods=["POST"],
        ),
        Route("/api/v2/decision/seats/{routine_id}/start", admitted(start_seat), methods=["POST"]),
        Route("/api/v2/decision/seats/{routine_id}/stop", admitted(stop_seat), methods=["POST"]),
        Route("/api/v2/decision/checks/{workspace_id}/run", admitted(run_checks), methods=["POST"]),
        Route(
            "/api/v2/decision/checks/{workspace_id}/stop", admitted(stop_checks), methods=["POST"]
        ),
        Route("/api/v2/decision/inbox/suggest", admitted(suggest_inbox), methods=["POST"]),
        Route("/api/v2/decision/proposals", admitted(create_proposal), methods=["POST"]),
        Route("/api/v2/decision/proposals/{proposal_id}", admitted(proposal), methods=["GET"]),
        Route("/api/v2/decision/proposals/{proposal_id}", admitted(act_proposal), methods=["POST"]),
        Route("/api/v2/decision/changes/{proposal_id}", admitted(act_change), methods=["POST"]),
    ]
