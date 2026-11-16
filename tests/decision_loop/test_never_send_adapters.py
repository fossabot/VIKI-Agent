"""The two adapters that turn live data into what the never-send list can check:
`cycle._active` (the open note or chat, as `now.active`) and `engine.owner_of` (the active
projects, as `owner.goals`). Both must carry a note's `ref` and its `tags` (None while unknown)
through to `state.build_state`, so a folder or tag rule can hold it back. test_state.py's own
tests only ever fed `build_state` hand-made `Mention`s, so a broken adapter would pass
unnoticed; these go through the real adapters instead."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from tests.decision_loop.fakes import WALL, FakeModel
from viki_agent.automation.brief import Goal
from viki_agent.automation.kanban import Card, KanbanRead
from viki_agent.decision_loop.agenda import DecisionAgenda
from viki_agent.decision_loop.arbiter import START_LAMBDA, START_WEIGHTS
from viki_agent.decision_loop.contracts import (
    Candidate,
    DecisionSettings,
    Evidence,
    ModelError,
    ModelReply,
    ModelRequest,
    Presence,
    canonical_json,
    item_key,
)
from viki_agent.decision_loop.cycle import CycleContext, CycleResult, run_cycle
from viki_agent.decision_loop.detectors import Observation, detect
from viki_agent.decision_loop.engine import owner_of
from viki_agent.decision_loop.state import History, Now, Owner, build_state
from viki_agent.decision_loop.store import DecisionLoopStore
from viki_agent.state import AppState, Prefs

SCOPE = "00000000-0000-4000-8000-0000000000a1"
NOW = WALL

HERE = Presence(
    focused=True,
    typing=False,
    away=False,
    chat_id=None,
    chat_title=None,
    note_ref=None,
    launcher_open=False,
    page=None,
)


def _candidate(evidence: tuple[Evidence, ...] = (), **changes: Any) -> Candidate:
    """A minimal approvals candidate the rules can decide on (same shape as test_cycle.py's)."""
    base = Candidate(
        item_key=item_key("approvals", SCOPE, "launch"),
        type="approvals",
        title="Review the launch",
        facts={"workspace_id": SCOPE},
        words={},
        evidence=evidence,
        related_chat=None,
        related_ref=None,
        deadline=None,
        shown_elsewhere=(),
        rules_evidence={"native": 1, "proposals": 0},
        origin_session=None,
        work_allowed=(),
    )
    return replace(base, **changes)


def _observation(*goals: Goal) -> Observation:
    return Observation(SCOPE, "vault", None, goals, (), None, ())


def _sent(state: Mapping[str, object]) -> dict[str, Any]:
    """`state` as plain JSON values (untyped, for reading nested parts)."""
    return json.loads(canonical_json(state))


async def _run(
    tmp_path: Path, presence: Presence, settings: DecisionSettings, *candidates: Candidate
) -> tuple[FakeModel, DecisionAgenda, CycleResult]:
    """One real cycle over a fresh store: `candidates` through `cycle._active` (fed by
    `presence`) and `state.build_state`, with the model recording exactly what was sent."""
    store = AppState(tmp_path / "state.json", Prefs()).store
    agenda = DecisionAgenda(store)
    model = FakeModel()

    async def ask(
        item_key: str, request: ModelRequest, *, reversal: bool
    ) -> ModelReply | ModelError | None:
        try:
            return await model.ask(request, key="test-key")
        except ModelError as error:
            return error

    ctx = CycleContext(
        scope=SCOPE,
        store=DecisionLoopStore(store),
        agenda=agenda,
        settings=settings,
        presence=presence,
        owner=Owner(),
        history_for=lambda _key: History(),
        now=NOW,
        lam=START_LAMBDA,
        weights=START_WEIGHTS,
        ask=ask,
        model_ok=True,
    )
    result = await run_cycle(ctx, candidates, trigger="change")
    return model, agenda, result


async def _sent_state(
    tmp_path: Path, presence: Presence, settings: DecisionSettings, candidate: Candidate
) -> tuple[FakeModel, DecisionAgenda]:
    model, agenda, _result = await _run(tmp_path, presence, settings, candidate)
    return model, agenda


# ---- 1. cycle level, folder rule ----------------------------------------------------------------


async def test_cycle_drops_the_open_notes_name_under_a_never_send_folder(tmp_path: Path) -> None:
    ref = "10-projects/secret/SECRETPLAN.md"
    presence = replace(HERE, note_ref=(SCOPE, ref))
    settings = DecisionSettings(enabled=True, never_send=("10-projects/secret/",))
    candidate = _candidate(evidence=(Evidence(ref, "SECRETPLAN excerpt", False, ()),))

    model, agenda = await _sent_state(tmp_path, presence, settings, candidate)

    main = model.requests[0]
    assert "SECRETPLAN" not in canonical_json(main.state)
    sent = _sent(main.state)
    assert sent["now"]["active"] == ""
    assert sent["candidate"]["evidence"] == []
    calls = agenda.calls(SCOPE)
    assert any(call.body is not None and call.body.held_back == 1 for call in calls)


# ---- 2. cycle level, tag rule: unknown tags fail closed -----------------------------------------


async def test_cycle_holds_back_the_open_note_when_its_tags_are_unknown(tmp_path: Path) -> None:
    ref = "30-knowledge/SECRETTAG.md"
    presence = replace(HERE, note_ref=(SCOPE, ref))
    settings = DecisionSettings(enabled=True, never_send=("#secret",))
    kept = Evidence("10-projects/launch/brief.md", "Kickoff notes", False, ())

    model, _agenda = await _sent_state(tmp_path, presence, settings, _candidate(evidence=(kept,)))

    main = model.requests[0]
    sent = _sent(main.state)
    assert sent["now"]["active"] == ""
    assert "SECRETTAG" not in canonical_json(main.state)
    assert sent["candidate"]["evidence"] == [
        {"ref": "10-projects/launch/brief.md", "excerpt": "Kickoff notes"}
    ]


# ---- 3. the `owner` part: goals held back by folder and separately by tag ------------------------


def test_owner_of_drops_never_send_goals_by_folder_and_by_tag() -> None:
    folder_blocked = Goal(ref="10-projects/secret-folder/a.md", title="FOLDERGOAL", tags=())
    tag_blocked = Goal(ref="10-projects/visible/b.md", title="TAGGOAL", tags=("secret",))
    kept = Goal(ref="10-projects/visible/c.md", title="KeptGoal", tags=())
    settings = DecisionSettings(never_send=("10-projects/secret-folder/", "#secret"))

    owner = owner_of(_observation(folder_blocked, tag_blocked, kept))
    built = build_state(_candidate(), owner, Now(None, False, False, NOW, 0), History(), settings)

    assert not built.blocked
    assert _sent(built.state)["owner"]["goals"] == ["KeptGoal"]


# ---- 4. control: without a never-send rule, the note and the goals reach the state --------------


async def test_without_a_never_send_rule_the_note_and_goals_reach_the_state(
    tmp_path: Path,
) -> None:
    ref = "10-projects/secret/SECRETPLAN.md"
    presence = replace(HERE, note_ref=(SCOPE, ref))
    candidate = _candidate(evidence=(Evidence(ref, "SECRETPLAN excerpt", False, ()),))
    model, _agenda = await _sent_state(
        tmp_path, presence, DecisionSettings(enabled=True), candidate
    )
    main = model.requests[0]
    sent = _sent(main.state)
    assert sent["now"]["active"] == "SECRETPLAN"
    assert sent["candidate"]["evidence"] == [{"ref": ref, "excerpt": "SECRETPLAN excerpt"}]

    folder_blocked = Goal(ref="10-projects/secret-folder/a.md", title="FOLDERGOAL", tags=())
    tag_blocked = Goal(ref="10-projects/visible/b.md", title="TAGGOAL", tags=("secret",))
    kept = Goal(ref="10-projects/visible/c.md", title="KeptGoal", tags=())
    owner = owner_of(_observation(folder_blocked, tag_blocked, kept))
    built = build_state(
        _candidate(), owner, Now(None, False, False, NOW, 0), History(), DecisionSettings()
    )
    assert _sent(built.state)["owner"]["goals"] == ["FOLDERGOAL", "TAGGOAL", "KeptGoal"]


# ---- 5. Kanban cards: the card's own tags never reach the evidence ----------------------------


def _cards(title: str = "Plan the move") -> list[Candidate]:
    """A `card_needs_you` (blocked) and a `card_stale` (working 2 days) candidate, from `detect`
    over a Kanban read, each card tagged with a seat and `xylo`."""
    tags = ("owner/zeta-seat", "xylo")
    kanban = KanbanRead(
        (
            Card("kanban/plan.md", title, "blocked", tags, NOW),
            Card("kanban/plan-two.md", title, "working", tags, NOW - timedelta(days=2)),
        ),
        (),
        True,
    )
    obs = Observation(SCOPE, "vault", None, (), (), None, (), kanban=kanban)
    found = detect(obs, DecisionSettings(), (), NOW)
    assert [c.type for c in found] == ["card_needs_you", "card_stale"]
    return found


def _card_state(candidate: Candidate, never_send: tuple[str, ...]) -> Any:
    return build_state(
        candidate,
        Owner(),
        Now(None, False, False, NOW, 0),
        History(),
        DecisionSettings(never_send=never_send),
    )


@pytest.mark.parametrize(
    "title,never_send,blocked",
    [
        ("Plan the move", ("kanban/",), True),
        ("Plan the move", ("#unrelated",), True),
        ("Plan the #xylo move", ("#xylo",), True),
        ("Plan the move", (), False),
    ],
    ids=["folder", "any-tag-rule", "inline-title-tag", "no-rule"],
)
def test_a_card_is_blocked_by_any_never_send_rule_and_leaks_no_tag(
    title: str, never_send: tuple[str, ...], blocked: bool
) -> None:
    for candidate in _cards(title):
        built = _card_state(candidate, never_send)
        assert built.blocked is blocked
        if blocked:
            assert built.state == {}
        else:
            text = canonical_json(built.state)
            assert candidate.related_ref is not None and candidate.related_ref in text
            assert not any(part in text for part in ("zeta", "xylo", "owner/"))
    if never_send == ("kanban/",):
        assert _card_state(_cards(title)[0], never_send).held_back == 1


async def test_a_cycle_asks_nothing_for_cards_under_a_tag_rule_and_commits_them_at_list(
    tmp_path: Path,
) -> None:
    cards = _cards()
    settings = DecisionSettings(enabled=True, never_send=("#unrelated",))

    model, agenda, result = await _run(tmp_path, HERE, settings, *cards)

    assert model.requests == []
    assert result.asked == 0
    for card in cards:
        record = agenda.item(SCOPE, card.item_key)
        assert record is not None and record.level == "list"
