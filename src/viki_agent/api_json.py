"""Strict JSON objects and ASCII-safe private API responses."""

from __future__ import annotations

import json
from typing import Any

from starlette.responses import Response


def json_response(body: object, status: int = 200) -> Response:
    return Response(
        json.dumps(body, ensure_ascii=True, separators=(",", ":")),
        status_code=status,
        media_type="application/json",
    )


def failure(code: str, message: str, status: int) -> Response:
    return json_response({"error": {"code": code, "message": message}}, status)


def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result
