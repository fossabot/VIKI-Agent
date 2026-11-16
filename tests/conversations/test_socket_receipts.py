"""A control receipt must not report a settings write that failed."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.test_server_v2 import NEW, connected, mutation, receipt, running, subscribe
from viki_agent.storage import StateError


def test_oversized_history_returns_a_bounded_error_and_socket_remains_usable(
    tmp_path: Path,
) -> None:
    from tests.test_server_v2 import EmptyCatalog, HistoryPage, TextDone, identity, recv

    class LargeHistory(EmptyCatalog):
        def history(self, session_id: str, **_kwargs: Any) -> HistoryPage:
            return HistoryPage(
                events=[TextDone(block_id="large", text="x" * (4 << 20))], next_before_uuid=None
            )

    catalog = LargeHistory(tmp_path / "home", tmp_path / "vault")
    with running(tmp_path, catalog=catalog) as app, connected(app.client) as (ws, _):
        target = subscribe(ws)[-1]
        request_id = identity()
        ws.send_json(
            {
                "type": "history.more",
                "session_id": NEW,
                "generation": target["generation"],
                "request_id": request_id,
            }
        )
        page = recv(ws, timeout=0.05)
        assert page["request_id"] == request_id and page["generation"] == target["generation"]
        assert page["data"] == {"events": [], "next_before_uuid": None, "error": "too_large"}
        command = mutation("session.set_effort", target["generation"], effort="max")
        ws.send_json(command)
        assert receipt(ws, command)["data"]["status"] == "applied"


def test_effort_storage_failure_is_unknown_and_keeps_previous_setting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with running(tmp_path) as app, connected(app.client) as (ws, _):
        subscribe(ws)
        actor = app.manager.get(NEW)
        assert actor is not None
        state = app.manager.app_state
        before = state.prefs(NEW)

        def fail(*_args: Any, **_kwargs: Any) -> None:
            raise StateError("unavailable")

        monkeypatch.setattr(state, "remember", fail)
        command = mutation("session.set_effort", actor.generation, effort="max")
        ws.send_json(command)
        assert receipt(ws, command)["data"] == {
            "status": "unknown",
            "reason": "storage_unavailable",
        }
        assert state.prefs(NEW) == before
