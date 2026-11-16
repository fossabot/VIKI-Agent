"""Window-authenticated controls for the app-owned Magpie process and dashboard."""

from __future__ import annotations

import asyncio
import math
import re
from collections.abc import Callable

from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from viki_agent.api_json import failure, json_response
from viki_agent.llm.connections import ConnectionError
from viki_agent.llm.gateway import INTAKE_S, read_body
from viki_agent.llm.magpie import Magpie, MagpieError


def _view(value: object) -> dict[str, object]:
    """Only bounded geometry, appearance enums and resolved brand colors cross the host link."""
    if not isinstance(value, dict) or set(value) != {"id", "bounds", "appearance", "visible"}:
        raise ValueError()
    if not isinstance(value["id"], str) or not re.fullmatch(r"[0-9a-f]{32}", value["id"]):
        raise ValueError()
    if type(value["visible"]) is not bool:
        raise ValueError()
    bounds = value["bounds"]
    if not isinstance(bounds, dict) or set(bounds) != {"x", "y", "width", "height"}:
        raise ValueError()
    if not all(
        type(number) in (int, float) and 0 <= number <= 32768 and math.isfinite(number)
        for number in bounds.values()
    ):
        raise ValueError()
    appearance = value["appearance"]
    if not isinstance(appearance, dict) or set(appearance) != {
        "theme",
        "density",
        "scale",
        "reduce_motion",
        "locale",
        "palette",
    }:
        raise ValueError()
    if (
        appearance["theme"] not in ("dark", "light")
        or appearance["density"] not in ("comfortable", "compact")
        or appearance["locale"] != "en"
        or type(appearance["reduce_motion"]) is not bool
        or type(appearance["scale"]) not in (int, float)
        or not 0.75 <= appearance["scale"] <= 2
        or not math.isfinite(appearance["scale"])
    ):
        raise ValueError()
    palette = appearance["palette"]
    if not isinstance(palette, dict) or set(palette) != {
        "bg",
        "panel",
        "raised",
        "text",
        "secondary",
        "muted",
        "faint",
        "line",
        "hair",
        "accent",
        "onAccent",
        "popover",
    }:
        raise ValueError()
    if not all(
        isinstance(color, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", color)
        for color in palette.values()
    ):
        raise ValueError()
    return value


def magpie_routes(
    magpie: Magpie,
    *,
    authorized: Callable[[Request], bool],
    origins: frozenset[str],
) -> list[Route]:
    busy = asyncio.Lock()

    async def handler(request: Request) -> Response:
        if not authorized(request):
            return failure("unauthorized", "Open VIKI Agent again.", 401)
        if request.method != "GET" and request.headers.get("origin") not in origins:
            return failure("forbidden_origin", "This window can't do that.", 403)
        if request.scope.get("query_string"):
            return failure("invalid_request", "Check the Magpie request.", 400)
        try:
            if request.method == "GET":
                return json_response(await magpie.status())
            async with asyncio.timeout(INTAKE_S):
                body = await read_body(request, 2048)
            ui = body.get("ui")
            if not (ui is None or isinstance(ui, str) and re.fullmatch(r"[0-9a-f]{32}", ui)):
                raise MagpieError("invalid_request")
            if request.url.path.endswith("/view"):
                if set(body) != {"ui", "view"}:
                    raise ValueError()
                return json_response(await magpie.update_view(ui, _view(body["view"])))
            if request.url.path.endswith("/close"):
                if set(body) == {"ui"}:
                    return json_response(await magpie.hide(ui))
                view_id = body.get("view_id")
                if set(body) != {"ui", "view_id"} or not (
                    isinstance(view_id, str) and re.fullmatch(r"[0-9a-f]{32}", view_id)
                ):
                    raise ValueError()
                return json_response(await magpie.hide(ui, view_id))
            if set(body) not in ({"ui"}, {"ui", "view"}):
                raise ValueError()
            view = _view(body["view"]) if "view" in body else None
            if busy.locked():
                return failure("busy", "Magpie is starting.", 409)
            async with busy:
                if view is None:
                    return json_response(await magpie.open(ui))
                return json_response(await magpie.open(ui, view))
        except MagpieError, ConnectionError, TimeoutError, OSError, ValueError:
            # The window shows one sentence for every cause; Magpie.start logs a refused start's.
            return failure("unavailable", "Couldn't open Magpie. Check its bundle and ports.", 503)

    return [
        Route("/api/v2/llm/magpie", handler, methods=["GET"]),
        Route("/api/v2/llm/magpie/open", handler, methods=["POST"]),
        Route("/api/v2/llm/magpie/close", handler, methods=["POST"]),
        Route("/api/v2/llm/magpie/view", handler, methods=["POST"]),
    ]
