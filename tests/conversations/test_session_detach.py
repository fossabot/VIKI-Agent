"""Raw authenticated socket coverage for optional detach and turn-scoped close frames."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.test_server import NEW, running
from tests.test_server_v2 import (
    DelayedCatalog,
    connected,
    finish_socket_commands,
    identity,
    mutation,
    receipt,
    subscribe,
    until,
)
from viki_agent import server
from viki_agent.events import TextDone, TurnDone, TurnStarted, Usage, UserMessage
from viki_agent.session import SessionActor


@pytest.mark.parametrize("field", ["session_id", "request_id"])
@pytest.mark.parametrize("value", [None, 1, {}, "bad", "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA"])
def test_detach_requires_canonical_uuids(field: str, value: Any) -> None:
    raw = {"type": "session.detach", "session_id": NEW, "request_id": identity()}
    assert server._validate(raw) is not None
    assert server._validate({**raw, field: value}) is None
    assert server._validate({**raw, "generation": identity()}) is None


@pytest.mark.parametrize("value", [None, 1, {}, "bad", "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA"])
def test_scoped_close_turn_id_requires_canonical_uuid(value: Any) -> None:
    raw = mutation("session.close", identity(), turn_id=identity())
    assert server._validate(raw) is not None
    assert server._validate({**raw, "turn_id": value}) is None


async def emit_after_commands(actor: SessionActor, text: str) -> None:
    await finish_socket_commands()
    actor._emit(TextDone(block_id=identity(), text=text))


def test_detach_is_request_scoped_and_only_removes_its_sockets_subscription(tmp_path: Path) -> None:
    with (
        running(tmp_path) as app,
        connected(app.client) as (first, _),
        connected(app.client) as (second, _),
    ):
        initial = subscribe(first)[-1]
        other = subscribe(second)[-1]
        actor = app.manager.get(NEW)
        assert actor is not None and actor.live
        replacement = subscribe(first, generation=initial["generation"])[-1]
        assert len(actor._subscribers) == 2
        first.send_json(
            {"type": "session.detach", "session_id": NEW, "request_id": initial["request_id"]}
        )
        first.portal.call(emit_after_commands, actor, "stale detach keeps latest")
        current = until(first, lambda frame: frame["type"] == "text.done")[-1]
        assert current["request_id"] == replacement["request_id"]
        until(second, lambda frame: frame["type"] == "text.done")
        first.send_json(
            {"type": "session.detach", "session_id": NEW, "request_id": replacement["request_id"]}
        )
        first.portal.call(emit_after_commands, actor, "other view still observes")
        current = until(second, lambda frame: frame["type"] == "text.done")[-1]
        assert current["request_id"] == other["request_id"]
        assert len(actor._subscribers) == 1
        assert app.manager.get(NEW) is actor and actor.live and not actor._closed


def test_detach_while_history_is_pending_drops_the_stale_page(tmp_path: Path) -> None:
    catalog = DelayedCatalog(tmp_path / "home", tmp_path / "vault")
    with running(tmp_path, catalog=catalog) as app, connected(app.client) as (ws, _):
        target = subscribe(ws)[-1]
        actor = app.manager.get(NEW)
        assert actor is not None
        old_history = identity()
        ws.send_json(
            {
                "type": "history.more",
                "session_id": NEW,
                "generation": target["generation"],
                "request_id": old_history,
            }
        )
        try:
            assert catalog.history_started.wait(timeout=0.05)
            ws.send_json(
                {"type": "session.detach", "session_id": NEW, "request_id": target["request_id"]}
            )
            replacement = subscribe(ws, generation=target["generation"])[-1]
        finally:
            catalog.release_history.set()
        ws.portal.call(emit_after_commands, actor, "history barrier")
        frames = until(ws, lambda frame: frame["type"] == "text.done")
        assert all(frame["type"] != "history.page" for frame in frames)
        assert frames[-1]["request_id"] == replacement["request_id"]
        assert actor.live and len(actor._subscribers) == 1


@pytest.mark.parametrize("newer", ["turn", "steer", "none"])
def test_wire_scoped_close_refuses_newer_work_and_preserves_subscription(
    tmp_path: Path, newer: str
) -> None:
    with running(tmp_path) as app, connected(app.client) as (ws, _):
        target = subscribe(ws)[-1]
        actor = app.manager.get(NEW)
        assert actor is not None
        own_turn = identity()

        async def finish() -> None:
            actor._emit(TurnStarted(turn_id=own_turn))
            actor._emit(TurnDone(status="error", usage=Usage()))
            if newer == "turn":
                actor._emit(TurnStarted(turn_id=identity()))
            elif newer == "steer":
                actor._steers[identity()] = UserMessage(text="pending manual work")

        ws.portal.call(finish)
        close = mutation("session.close", target["generation"], turn_id=own_turn)
        ws.send_json(close)
        result = receipt(ws, close)
        if newer == "none":
            assert result["data"]["status"] == "applied"
            assert app.manager.get(NEW) is None and not actor.live
        else:
            assert result["data"] == {"status": "rejected", "reason": "runtime_changed"}
            assert app.manager.get(NEW) is actor and actor.live and not actor._closed
            assert len(actor._subscribers) == 1
            ws.portal.call(emit_after_commands, actor, "newer work stays subscribed")
            assert (
                until(ws, lambda frame: frame["type"] == "text.done")[-1]["request_id"]
                == target["request_id"]
            )
