"""`held_start`'s prediction that VIKI's own start of one item is held back this cycle, ahead
of the engine's real gate, and `start_family`'s day-limit grouping (`cycle.py`). What `_commit`
and `render` do with that prediction: Why now names the hold (`templates.HOLD_WHY`), a hold
other than "limit" still offers the item's own start action under its label, "limit" drops the
start action even under Ask first, and an active mission always wins over any hold. Also the
inbox-since "since" case and a second look's quiet "nothing" ending, each its own path into
`_commit`."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from tests.decision_loop.fakes import FakeModel
from tests.decision_loop.test_cycle import NOW, TRIAGE, Gate, Rig, candidate, code_look, offer
from viki_agent.decision_loop import arbiter, contracts, templates
from viki_agent.decision_loop.agenda import DecisionAgenda
from viki_agent.decision_loop.contracts import DecisionSettings
from viki_agent.decision_loop.cycle import HOLD_CODES, Holds, held_start, start_family
from viki_agent.decision_loop.store import DecisionLoopStore
from viki_agent.state import AppState, Prefs


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    store = AppState(tmp_path / "state.json", Prefs()).store
    model = FakeModel()
    return Rig(store, DecisionLoopStore(store), DecisionAgenda(store), model, Gate(model))


# ---- start_family: which day-limit family a start counts toward --------------------------------


@pytest.mark.parametrize(
    ("candidate_type", "work", "expected"),
    [
        ("code_look", "prepare", "look"),
        ("long_task_look", "prepare", "second_look"),
        ("correction_look", "prepare", "second_look"),
        ("stalled_project", "prepare", "project_plan"),
        ("overdue_tasks", "prepare", "project_plan"),
        ("tasks_due", "prepare", "prepare"),
        ("tasks_due", "run", "run"),
        ("commit_checks", "run", "run"),
        ("inbox_triage", "prepare", "prepare"),
    ],
)
def test_start_family_names_the_day_limit_group(
    candidate_type: str, work: str, expected: str
) -> None:
    assert start_family(candidate_type, work) == expected


def test_hold_codes_cover_every_templates_hold_why_key() -> None:
    # Guards the same equality cycle.py asserts at import time, so a drift fails here too.
    assert set(HOLD_CODES) == set(templates.HOLD_WHY)


# ---- held_start: the prediction's logic table ----------------------------------------------------


def test_held_start_is_none_without_prepare_or_run_work() -> None:
    assert held_start("tasks_due", "none", "ask", "now", Holds(busy=True)) is None
    assert held_start("tasks_due", "coordinate", "allowed", "now", Holds(brake=True)) is None


def test_held_start_is_none_on_an_off_grant() -> None:
    assert held_start("tasks_due", "run", "off", "now", Holds(busy=True)) is None


@pytest.mark.parametrize("grant", ["ask", "allowed"])
def test_held_start_limit_fires_for_a_mission_type_under_either_grant(grant: str) -> None:
    holds = Holds(limited=frozenset({"second_look"}))
    assert held_start("long_task_look", "prepare", grant, "now", holds) == "limit"


def test_held_start_limit_is_scoped_to_mission_types_only() -> None:
    # A saved check's and an inbox suggestion's own families ("run"/"prepare") may also be at
    # the limit, but "limit" is reserved for the four mission-type groups the Spec names.
    assert (
        held_start("commit_checks", "run", "allowed", "now", Holds(limited=frozenset({"run"})))
        is None
    )
    assert (
        held_start(
            "inbox_triage", "prepare", "allowed", "now", Holds(limited=frozenset({"prepare"}))
        )
        is None
    )


def test_held_start_other_holds_need_an_allowed_grant() -> None:
    # Under "ask" the item already shows its own start button, so brake/quiet/busy/later never
    # apply, whatever `holds` says.
    holds = Holds(brake=True, quiet=True, busy=True)
    assert held_start("tasks_due", "run", "ask", "later", holds) is None
    assert held_start("code_look", "prepare", "ask", "later", holds) is None


@pytest.mark.parametrize(
    "candidate_type",
    [
        "tasks_due",
        "code_look",
        "long_task_look",
        "correction_look",
        "stalled_project",
        "inbox_triage",
    ],
)
def test_held_start_brake_covers_mission_types_and_inbox_triage(candidate_type: str) -> None:
    work = "run" if candidate_type == "tasks_due" else "prepare"
    assert held_start(candidate_type, work, "allowed", "now", Holds(brake=True)) == "brake"


def test_held_start_brake_does_not_cover_a_saved_check() -> None:
    assert held_start("commit_checks", "run", "allowed", "now", Holds(brake=True)) is None


@pytest.mark.parametrize(
    "candidate_type",
    ["code_look", "long_task_look", "stalled_project", "commit_checks", "inbox_triage"],
)
def test_held_start_quiet_covers_looks_second_looks_plans_checks_and_triage(
    candidate_type: str,
) -> None:
    work = "run" if candidate_type == "commit_checks" else "prepare"
    assert held_start(candidate_type, work, "allowed", "now", Holds(quiet=True)) == "quiet"


def test_held_start_quiet_does_not_cover_an_ordinary_mission_type() -> None:
    assert held_start("tasks_due", "run", "allowed", "now", Holds(quiet=True)) is None


@pytest.mark.parametrize(
    "candidate_type", ["tasks_due", "code_look", "long_task_look", "stalled_project"]
)
def test_held_start_busy_covers_every_mission_type(candidate_type: str) -> None:
    work = "run" if candidate_type == "tasks_due" else "prepare"
    assert held_start(candidate_type, work, "allowed", "now", Holds(busy=True)) == "busy"


@pytest.mark.parametrize("candidate_type", ["commit_checks", "inbox_triage"])
def test_held_start_busy_does_not_cover_checks_or_triage(candidate_type: str) -> None:
    work = "run" if candidate_type == "commit_checks" else "prepare"
    assert held_start(candidate_type, work, "allowed", "now", Holds(busy=True)) is None


def test_held_start_later_fires_only_once_timing_leaves_now() -> None:
    assert held_start("tasks_due", "run", "allowed", "now", Holds()) is None
    assert held_start("tasks_due", "run", "allowed", "at", Holds()) == "later"
    assert held_start("long_task_look", "prepare", "allowed", "in_context", Holds()) == "later"


def test_held_start_later_does_not_cover_checks_or_triage() -> None:
    assert held_start("commit_checks", "run", "allowed", "at", Holds()) is None
    assert held_start("inbox_triage", "prepare", "allowed", "at", Holds()) is None


def test_held_start_first_match_order_is_brake_then_quiet_then_busy_then_later() -> None:
    # A Look mission is in every scope at once (brake, quiet, busy and later alike): the first
    # that holds wins, in that order.
    every = Holds(brake=True, quiet=True, busy=True)
    assert held_start("code_look", "prepare", "allowed", "at", every) == "brake"
    assert (
        held_start("code_look", "prepare", "allowed", "at", replace(every, brake=False)) == "quiet"
    )
    assert (
        held_start(
            "code_look", "prepare", "allowed", "at", replace(every, brake=False, quiet=False)
        )
        == "busy"
    )
    assert held_start("code_look", "prepare", "allowed", "at", Holds()) == "later"


def test_held_start_any_other_candidate_type_is_none() -> None:
    assert held_start("approvals", "run", "allowed", "now", Holds(brake=True, busy=True)) is None


# ---- render: a hold's wording and the "limit" action rule ----------------------------------------

_CANDIDATE = contracts.Candidate(
    item_key="item-key-1",
    type="tasks_due",
    title="Finish the report",
    facts={"workspace_id": "ws-1"},
    words={},
    evidence=(),
    related_chat=None,
    related_ref="10-projects/demo/tasks.md",
    deadline=None,
    shown_elsewhere=(),
    rules_evidence={},
    origin_session=None,
    work_allowed=(),
)
_OUTCOME = contracts.Outcome(
    level="list",
    timing="now",
    defer_until=None,
    work="run",
    kind="ask",
    reason="model:scored",
    terms={},
    blocked_by=(),
)
_MISSION = contracts.MissionRecord(
    mission_id="mission-1",
    scope="item",
    session_id="session-running",
    item_key="item-key-1",
    work="run",
    state="running",
    reason="",
    title="",
    brief="",
    created_at="2026-10-21T09:00:00.000Z",
    deadline_at="2026-10-21T09:30:00.000Z",
    finished_at=None,
)


@pytest.mark.parametrize("hold", HOLD_CODES)
def test_any_hold_widens_why_with_its_hold_why_sentence(hold: str) -> None:
    body = templates.render(
        _CANDIDATE, _OUTCOME, {}, mission=None, grants={"prepare": "off", "run": "ask"}, hold=hold
    )
    assert body.why.endswith(f" {templates.HOLD_WHY[hold]}")


def test_no_hold_adds_no_hold_why_sentence() -> None:
    held = templates.render(
        _CANDIDATE, _OUTCOME, {}, mission=None, grants={"prepare": "off", "run": "ask"}, hold="busy"
    )
    unheld = templates.render(
        _CANDIDATE, _OUTCOME, {}, mission=None, grants={"prepare": "off", "run": "ask"}
    )
    assert unheld.why != held.why
    assert held.why == f"{unheld.why} {templates.HOLD_WHY['busy']}"


def test_a_limit_hold_drops_the_start_action_even_under_an_ask_grant() -> None:
    body = templates.render(
        _CANDIDATE,
        _OUTCOME,
        {},
        mission=None,
        grants={"prepare": "off", "run": "ask"},
        hold="limit",
    )
    assert all(a.type != "start_run" for a in body.actions)
    assert body.why.endswith(templates.HOLD_WHY["limit"])


def test_a_non_limit_hold_still_offers_the_start_action() -> None:
    body = templates.render(
        _CANDIDATE,
        _OUTCOME,
        {},
        mission=None,
        grants={"prepare": "off", "run": "ask"},
        hold="brake",
    )
    assert any(a.type == "start_run" for a in body.actions)


def test_a_limit_hold_still_lets_an_active_mission_win() -> None:
    body = templates.render(
        _CANDIDATE,
        _OUTCOME,
        {},
        mission=_MISSION,
        grants={"prepare": "off", "run": "ask"},
        hold="limit",
    )
    types = [a.type for a in body.actions]
    assert "open_chat" in types
    assert "stop_mission" in types
    assert "start_run" not in types


# ---- a whole cycle: _commit wiring ctx.holds and the inbox-since/nothing cases -------------------


async def test_a_busy_hold_offers_take_a_second_look_under_allowed(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second-look offer under Allowed, held by a mission already open (`Holds.busy`), still
    offers its own start, labelled "Take a second look", with Why now naming the hold; VIKI
    itself starts nothing here (the engine's real gate does that; this module only predicts)."""
    decide = arbiter.decide
    monkeypatch.setattr(
        arbiter,
        "decide",
        lambda inp: replace(
            decide(inp), kind="suggest", level="list", timing="now", work="prepare"
        ),
    )
    settings = DecisionSettings(enabled=True, grant_second_look="allowed")
    result = await rig.cycle(offer(), settings=settings, model_ok=False, holds=Holds(busy=True))
    (done,) = result.committed
    assert done.outcome.work == "prepare"
    actions = [(a.type, a.label) for a in done.item.body.actions]
    assert ("start_prepare", "Take a second look") in actions
    assert done.item.body.why.endswith(templates.HOLD_WHY["busy"])


async def test_a_quiet_hold_offers_take_a_look_for_a_look_mission(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    decide = arbiter.decide
    monkeypatch.setattr(
        arbiter, "decide", lambda inp: replace(decide(inp), level="list", timing="now")
    )
    settings = DecisionSettings(enabled=True, grant_look="allowed")
    result = await rig.cycle(
        code_look(), settings=settings, model_ok=False, holds=Holds(quiet=True)
    )
    (done,) = result.committed
    actions = [(a.type, a.label) for a in done.item.body.actions]
    assert ("start_prepare", "Take a look") in actions
    assert done.item.body.why.endswith(templates.HOLD_WHY["quiet"])


async def test_a_limit_hold_wires_through_a_whole_cycle_with_no_start_action(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A regular mission type (`tasks_due`) whose family ("run") is at today's limit offers no
    start even under Ask first, Why now naming the limit."""
    decide = arbiter.decide
    monkeypatch.setattr(
        arbiter,
        "decide",
        lambda inp: replace(decide(inp), kind="ask", level="list", timing="now", work="run"),
    )
    item = replace(
        candidate(),
        type="tasks_due",
        related_ref="10-projects/demo/tasks.md",
        rules_evidence={"overdue": 0, "today": 0},
    )
    settings = DecisionSettings(enabled=True, grant_run="ask")
    result = await rig.cycle(
        item, settings=settings, model_ok=False, holds=Holds(limited=frozenset({"run"}))
    )
    (done,) = result.committed
    assert all(a.type != "start_run" for a in done.item.body.actions)
    assert done.item.body.why.endswith(templates.HOLD_WHY["limit"])


async def test_since_names_the_hold_for_an_inbox_item_stored_before_the_engine_began(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An inbox suggestion item already open before `ctx.inbox_since`, under Allowed, still
    offers Suggest destinations, Why now naming "since": VIKI starts no run for an item from an
    earlier launch, so the start is the user's own."""
    decide = arbiter.decide
    monkeypatch.setattr(
        arbiter,
        "decide",
        lambda inp: replace(
            decide(inp), kind="suggest", level="list", timing="now", work="prepare"
        ),
    )
    settings = DecisionSettings(enabled=True, grant_inbox_triage="allowed")
    await rig.cycle(TRIAGE, settings=settings, model_ok=False)
    result = await rig.cycle(
        TRIAGE,
        settings=settings,
        model_ok=False,
        now=NOW + timedelta(hours=1),
        inbox_since=NOW + timedelta(minutes=30),
    )
    (done,) = result.committed
    actions = [(a.type, a.label) for a in done.item.body.actions]
    assert ("suggest_inbox", "Suggest destinations") in actions
    assert done.item.body.why.endswith(templates.HOLD_WHY["since"])


async def test_a_nothing_second_look_report_is_capped_at_list_and_names_the_chat(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second look's mission_report ending with reason "nothing" words the chat it looked at
    and asks no more attention than a list, whatever level the policy would otherwise show."""
    decide = arbiter.decide
    monkeypatch.setattr(
        arbiter, "decide", lambda inp: replace(decide(inp), level="alert", timing="now")
    )
    item = replace(
        candidate(),
        type="mission_report",
        facts={"state": "done", "reason": "nothing", "chat": "Garden plan"},
    )
    result = await rig.cycle(item, model_ok=False)
    (done,) = result.committed
    assert done.outcome.level == "list"
    assert done.item.body.what == 'Second look at "Garden plan": nothing worth your time.'
    assert done.item.body.why == (
        "VIKI looked again in plan mode and found no flaw, better approach or missing piece."
    )


async def test_a_nothing_second_look_report_reads_your_chat_when_the_title_is_blank(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    decide = arbiter.decide
    monkeypatch.setattr(
        arbiter, "decide", lambda inp: replace(decide(inp), level="list", timing="now")
    )
    item = replace(
        candidate(), type="mission_report", facts={"state": "done", "reason": "nothing", "chat": ""}
    )
    result = await rig.cycle(item, model_ok=False)
    (done,) = result.committed
    assert done.item.body.what == "Second look at your chat: nothing worth your time."
