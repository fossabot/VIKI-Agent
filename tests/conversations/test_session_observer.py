"""SessionManager.bind_observer wiring and SessionActor.pending_cards."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest

from tests.conversations.test_session import SID, setup, watch, with_cards
from viki_agent.events import (
    Error,
    PermissionRequested,
    Question,
    QuestionAsked,
    RequestAnswered,
    RequestCancelled,
    TurnDone,
    Usage,
)
from viki_agent.session import SessionActor

BEFORE = "20000000-0000-4000-8000-000000000000"
AFTER_CREATE = "20000000-0000-4000-8000-000000000001"
AFTER_OPEN = "20000000-0000-4000-8000-000000000002"
AFTER_FORK = "20000000-0000-4000-8000-000000000003"
AFTER_ATTACH = "20000000-0000-4000-8000-000000000004"


def _emit_error(actor: SessionActor, label: str) -> dict[str, Any]:
    return actor._emit(Error(code="internal", message=label, recoverable=True))


async def test_bind_observer_reports_actors_made_after_not_before(tmp_path: Path) -> None:
    manager, _spawner = setup(tmp_path, [])
    before = manager.create(BEFORE)

    seen: list[tuple[SessionActor, dict[str, Any]]] = []
    manager.bind_observer(lambda actor, envelope: seen.append((actor, envelope)))

    _emit_error(before, "too early")
    assert seen == []

    made = [
        manager.create(AFTER_CREATE),
        manager.open(AFTER_OPEN, profile=False),
        manager.fork(AFTER_CREATE, AFTER_FORK, source_profile=False),
        manager.attach_prepared(AFTER_ATTACH, profile=True),
    ]
    # The observer rides its own attribute, never the socket-subscription set: a leaked
    # entry there would fail the server's own-subscriber-left leak checks after it closes.
    assert all(not actor._subscribers for actor in made)
    for actor in made:
        _emit_error(actor, "hello")

    assert [actor for actor, _envelope in seen] == made
    assert [envelope["data"]["message"] for _actor, envelope in seen] == ["hello"] * 4
    assert all(envelope["type"] == "error" for _actor, envelope in seen)


async def test_a_raising_observer_does_not_escape_emit_or_starve_other_subscribers(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    manager, _spawner = setup(tmp_path, [])

    def boom(_actor: SessionActor, _envelope: dict[str, Any]) -> None:
        raise RuntimeError("synthetic observer failure")

    manager.bind_observer(boom)
    actor = manager.create(SID)
    probe = watch(actor)  # a second, plain subscriber the raising one must not starve

    with caplog.at_level(logging.WARNING, logger="viki_agent.session"):
        _emit_error(actor, "one")
        _emit_error(actor, "two")

    assert probe.types == ["error", "error"]
    messages = [record.getMessage() for record in caplog.records]
    assert messages.count("a session observer failed") == 2


async def test_pending_cards_lists_copies_of_waiting_question_and_permission_cards(
    tmp_path: Path,
) -> None:
    manager, driver, _emits = with_cards(tmp_path)
    actor = manager.create(SID)
    assert await actor.start()
    driver.cards = ["q1", "p1"]
    actor._emit(
        QuestionAsked(
            tool_use_id="q1", questions=[Question(header="Loc", question="Where?", options=[])]
        )
    )
    actor._emit(
        PermissionRequested(tool_use_id="p1", kind="tool", tool="Read", summary="read a.md")
    )

    cards = actor.pending_cards()
    assert [card["type"] for card in cards] == ["question.asked", "permission.requested"]
    assert all("native_request" in card for card in cards)

    cards[0]["data"]["questions"][0]["header"] = "mutated"
    again = actor.pending_cards()
    assert again[0]["data"]["questions"][0]["header"] == "Loc"  # a copy; the original is untouched


async def test_pending_cards_drops_answered_cancelled_and_turn_ended_requests(
    tmp_path: Path,
) -> None:
    manager, driver, _emits = with_cards(tmp_path)
    actor = manager.create(SID)
    assert await actor.start()
    driver.cards = ["q1", "p1", "p2"]
    actor._emit(
        QuestionAsked(
            tool_use_id="q1", questions=[Question(header="Loc", question="Where?", options=[])]
        )
    )
    actor._emit(
        PermissionRequested(tool_use_id="p1", kind="tool", tool="Read", summary="read a.md")
    )
    actor._emit(
        PermissionRequested(tool_use_id="p2", kind="tool", tool="Edit", summary="edit b.md")
    )
    assert len(actor.pending_cards()) == 3

    actor._emit(RequestCancelled(tool_use_id="q1"))
    actor._emit(RequestAnswered(tool_use_id="p1"))
    assert [card["data"]["tool_use_id"] for card in actor.pending_cards()] == ["p2"]

    actor._emit(TurnDone(status="success", usage=Usage()))
    assert actor.pending_cards() == []
