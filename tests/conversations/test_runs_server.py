"""Server wiring for the runs routes: create_app registers run_routes behind the same
Bearer/Origin/Host stack as every other v2 route, and a live actor's RunsDriver capability
reaches it through that wiring. Route-internal branch coverage (gate order, body/shape checks,
RunStopError codes, overload, logging) is test_runs_api.py's job; this file proves the wiring.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from tests.test_server import BEARER, NEW, ORIGIN, TOKEN, EchoDriver, _open_v2, authed, running
from viki_agent.drivers.base import RunStopError
from viki_agent.drivers.claude.control import Launch
from viki_agent.events import Event
from viki_agent.state import Prefs

RAISED_AT = "2026-10-09T00:00:00.000Z"


class RunsEchoDriver(EchoDriver):
    """An EchoDriver that also reports RunsDriver: a fixed requests() row, and a stop_run that
    raises for one chosen block id and otherwise succeeds silently -- proof the route passes
    through the real tool_use_id rather than a fixed one."""

    def requests(self) -> list[dict[str, object]]:
        return [{"tool_use_id": "tu-1", "run": None, "raised_at": RAISED_AT}]

    async def stop_run(self, block_id: str) -> None:
        if block_id == "bad-run":
            raise RunStopError("unknown_run")


def make_driver(launch: Launch, prefs: Prefs, emit: Callable[[Event], None]) -> Any:
    return RunsEchoDriver(launch, emit)


def test_requests_route_gates_before_any_actor(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    good = str(uuid.uuid4())
    with running(tmp_path) as app:
        client = app.client
        answers = [
            client.get(f"/api/v2/sessions/{good}/requests"),
            client.get(
                f"/api/v2/sessions/{good}/requests",
                headers={**BEARER, "origin": "http://evil.example"},
            ),
            client.get("/api/v2/sessions/not-a-uuid/requests", headers=BEARER),
            client.get(f"/api/v2/sessions/{good}/requests", headers=BEARER),
        ]
    assert [answer.status_code for answer in answers] == [401, 403, 400, 404]
    assert [answer.headers["cache-control"] for answer in answers] == ["no-store"] * 4
    assert TOKEN not in caplog.text and str(tmp_path) not in caplog.text


def test_stop_route_refuses_a_missing_origin_and_an_unknown_chat(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    path = f"/api/v2/sessions/{uuid.uuid4()}/runs/tu-1/stop"
    with running(tmp_path) as app:
        client = app.client
        no_origin = client.post(path, headers=BEARER)
        unknown_chat = client.post(path, headers={**BEARER, "origin": ORIGIN})
    assert (no_origin.status_code, unknown_chat.status_code) == (403, 404)
    assert no_origin.headers["cache-control"] == "no-store"
    assert unknown_chat.headers["cache-control"] == "no-store"
    assert TOKEN not in caplog.text and str(tmp_path) not in caplog.text


def test_a_live_actor_s_requests_and_stop_reach_its_driver(tmp_path: Path) -> None:
    """GET echoes the live driver's own requests() row, and POST reaches stop_run with the exact
    tool_use_id from the URL: one id succeeds, the other is the driver's own refusal, so the id
    travelled through the route for real rather than being ignored."""
    with running(tmp_path, make_driver=make_driver) as app:
        with authed(app.client) as ws:
            _open_v2(ws)
        client = app.client
        requests = client.get(f"/api/v2/sessions/{NEW}/requests", headers=BEARER)
        ok = client.post(
            f"/api/v2/sessions/{NEW}/runs/tu-1/stop", headers={**BEARER, "origin": ORIGIN}
        )
        refused = client.post(
            f"/api/v2/sessions/{NEW}/runs/bad-run/stop", headers={**BEARER, "origin": ORIGIN}
        )
    assert requests.json() == {
        "requests": [{"tool_use_id": "tu-1", "run": None, "raised_at": RAISED_AT}]
    }
    assert ok.status_code == 200
    assert ok.json() == {"status": "sent"}
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "unknown_run"
