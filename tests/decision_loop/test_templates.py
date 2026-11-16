"""Word the items from templates: `render` on the rules path and the model path, for every
candidate type, proving the "what"/"why now"/evidence/actions fields and the no-number,
no-"JEV" field rules."""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from viki_agent.decision_loop import contracts, playbook, rules, wire
from viki_agent.decision_loop.contracts import (
    Candidate,
    ChoiceAnswer,
    Evidence,
    EvidenceLabel,
    ItemBody,
    MissionRecord,
    NoulAnswer,
    Outcome,
    ScoreAnswer,
)
from viki_agent.decision_loop.templates import (
    _SKILL_DRAFT_CONTRACT,
    ACTION_LABELS,
    RULE_REASON_WORDS,
    render,
)

_REPO_ROOT = Path(__file__).parents[2]

# ---- builders: every field named explicitly once, varied with `replace` -------------------------

_BASE_CANDIDATE = Candidate(
    item_key="item-key-1",
    type="approvals",
    title="Finish the Q3 report",
    facts={},
    words={},
    evidence=(),
    related_chat=None,
    related_ref=None,
    deadline=None,
    shown_elsewhere=(),
    rules_evidence={},
    origin_session=None,
    work_allowed=(),
)


def _candidate(candidate_type: str, **overrides: object) -> Candidate:
    return replace(_BASE_CANDIDATE, type=candidate_type, **overrides)


_BASE_OUTCOME = Outcome(
    level="list",
    timing="now",
    defer_until=None,
    work="none",
    kind="inform",
    reason="rules:waiting_on_you",
    terms={},
    blocked_by=(),
)


def _outcome(**overrides: object) -> Outcome:
    return replace(_BASE_OUTCOME, **overrides)


_BASE_MISSION = MissionRecord(
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


def _mission(**overrides: object) -> MissionRecord:
    return replace(_BASE_MISSION, **overrides)


_VAULT_EVIDENCE = Evidence(
    ref="30-knowledge/some-note.md", excerpt="THE-SECRET-EXCERPT-42", outside=False, tags=()
)
_OUTSIDE_EVIDENCE = Evidence(
    ref="https://example.com/outside", excerpt="THE-SECRET-EXCERPT-42", outside=True, tags=None
)

# A plausible `rules.REASONS` code per type, for the rules-path half of the sweep below.
_SAMPLE_REASON: Mapping[str, str] = {
    "approvals": "waiting_on_you",
    "tasks_due": "due_or_overdue",
    "agent_runs": "runs_to_review",
    "inbox": "inbox_pressure",
    "uncommitted": "uncommitted_changes",
    "routine": "routine_soon",
    "mission_report": "mission_ended",
    "skill_offer": "skill_hint",
    "skill_suggest": "skill_fits",
    "learn_source": "needs_decision_model",
    "retro_due": "labels_piled_up",
    "question_tune": "question_drift",
    "note_link": "needs_decision_model",
    "note_superseded": "needs_decision_model",
    "tasks_ticked": "tasks_ticked",
    "card_needs_you": "card_needs_decision",
    "card_stale": "card_untouched",
    "seat_waiting": "seat_waiting",
    "seat_working": "seat_working",
    "seat_start": "seat_could_start",
    "seat_report": "seat_report",
    "vault_recovery": "recovery_needs_you",
    "memory_drift": "memory_source_changed",
    "bridge_stopped": "bridge_stopped",
    "chat_waiting": "chat_waiting",
    "send_uncertain": "send_uncertain",
    "mcp_context": "mcp_context_heavy",
    "code_look": "code_changes_waiting",
    "long_task_look": "work_finished",
    "correction_look": "work_finished",
    "stalled_project": "project_stalled",
    "overdue_tasks": "tasks_overdue",
    "context_handoff": "context_nearly_full",
    "source_note": "pages_to_note",
    "commit_checks": "checks_not_run",
    "inbox_triage": "inbox_unsorted",
}

_CARD_SAMPLE_WHY = {
    "card_needs_you": "Research blocked it last week.",
    "card_stale": "Working, last changed last week, with no handoff.",
}

_SEAT_SAMPLE_WHY = {
    "seat_waiting": "Its run waits until you answer or it reaches its time limit.",
    "seat_working": "VIKI started it for a card waiting for its seat.",
    "seat_start": "Start Bots' work is set to Ask first.",
    "seat_report": "Open its session to see where it stopped.",
}

# The five self-improvement types whose base action is a primary draft (which never carries an
# excerpt). The three `contracts.CHANGES` types are also in `contracts.IMPROVE`, but their
# base action opens Approvals instead (`_base_action`), so the draft-only sweeps below
# exclude them; `test_change_items_open_approvals` covers them on their own.
_DRAFT_IMPROVE = tuple(t for t in contracts.IMPROVE if t not in contracts.CHANGES)

# Crosses every threshold at once, so the model-path sweep exercises every `_what` branch
# alongside a non-trivial `why` built from answers, not the rules path's reason text.
_RICH_MODEL_ANSWERS: contracts.Answers = {
    "goal_link": NoulAnswer(0.9),
    "owner_only": NoulAnswer(0.8),
    "news": NoulAnswer(0.75),
    "fits_now": NoulAnswer(0.71),
    "importance": ScoreAnswer(2.5, 0.8, {2: 0.6, 3: 0.2}),
    "kind": ChoiceAnswer("suggest", 0.9, {"suggest": 0.9}),
    "work": ChoiceAnswer("none", 0.9, {"none": 0.9}),
}


def _card(candidate_type: str, status: str, seat: str | None = "research") -> Candidate:
    facts: dict[str, object] = {"workspace_id": "ws-1", "status": status}
    if seat is not None:
        facts["seat"] = seat
    return _candidate(
        candidate_type,
        title="Find three sources",
        facts=facts,
        words={"age": "last week"},
        evidence=(Evidence("kanban/find-sources.md", "", False, None),),
        related_ref="kanban/find-sources.md",
    )


def _seat(candidate_type: str, seat: str | None = "research", **facts: object) -> Candidate:
    base: dict[str, object] = {"workspace_id": "ws-1", "routine_id": "routine-1"}
    base["session_id"] = "session-1"
    if seat is not None:
        base["seat"] = seat
    return _candidate(candidate_type, title="Research", facts={**base, **facts})


def _look(candidate_type: str, wait: str | None = None, **facts: object) -> Candidate:
    base: dict[str, object] = {"workspace_id": "ws-1", "session_id": "session-1"}
    return _candidate(
        candidate_type,
        title="Look",
        facts={**base, **facts},
        words={} if wait is None else {"wait": wait},
    )


def _code_look(**facts: object) -> Candidate:
    return _candidate("code_look", title="Code", facts={"folder_id": "ws-code", **facts})


def _second_look(candidate_type: str = "long_task_look", **facts: object) -> Candidate:
    base: dict[str, object] = {"workspace_id": "ws-1", "session": "session-1"}
    return _candidate(candidate_type, title="Weekly review", facts={**base, **facts})


def _project_plan(candidate_type: str, **facts: object) -> Candidate:
    base: dict[str, object] = {"workspace_id": "ws-1", "project": "Garden"}
    return _candidate(candidate_type, title="project", facts={**base, **facts})


def _message_draft(candidate_type: str, **facts: object) -> Candidate:
    base: dict[str, object] = {"workspace_id": "ws-1", "session": "session-1"}
    return _candidate(candidate_type, title="draft", facts={**base, **facts})


def _saved_check(**facts: object) -> Candidate:
    base: dict[str, object] = {"workspace_id": "ws-1", "folder_id": "ws-code"}
    return _candidate("commit_checks", title="Checks", facts={**base, **facts})


def _inbox_triage(**facts: object) -> Candidate:
    return _candidate("inbox_triage", title="Inbox suggestions", facts=facts)


def _filled_candidate(candidate_type: str) -> Candidate:
    """A candidate for `candidate_type` with the facts/words/evidence/refs a real detector would
    fill, so the sweep below exercises every `_what`, evidence and base-action branch."""
    evidence = (_VAULT_EVIDENCE, _OUTSIDE_EVIDENCE)
    facts: dict[str, object] = {"workspace_id": "ws-1"}
    if candidate_type == "approvals":
        return _candidate(candidate_type, evidence=evidence, facts=facts)
    if candidate_type == "tasks_due":
        return _candidate(
            candidate_type,
            evidence=evidence,
            facts=facts,
            related_ref="10-projects/demo/tasks.md",
            words={"due": "tomorrow"},
        )
    if candidate_type == "agent_runs":
        return _candidate(candidate_type, evidence=evidence, facts={**facts, "review": True})
    if candidate_type == "inbox":
        return _candidate(
            candidate_type, evidence=evidence, facts=facts, words={"age": "a few days old"}
        )
    if candidate_type == "uncommitted":
        return _candidate(candidate_type, evidence=evidence, facts=facts)
    if candidate_type == "routine":
        return _candidate(
            candidate_type,
            evidence=evidence,
            facts={**facts, "routine_id": "routine-1"},
            words={"next_run": "this evening"},
        )
    if candidate_type == "mission_report":
        return _candidate(
            candidate_type, evidence=evidence, facts=facts, related_chat="session-ended"
        )
    if candidate_type == "skill_offer":
        return _candidate(
            candidate_type,
            evidence=(
                Evidence(None, "Weekly review", False, ()),
                Evidence(None, "Draft the memo", False, ()),
            ),
            facts={"trigger": "correction", "sessions": ("session-a", "session-b", "session-c")},
            related_chat="session-a",
        )
    if candidate_type == "skill_suggest":
        return _candidate(
            candidate_type,
            facts={"session": "session-a", "skill": "sprint-retro"},
            related_chat="session-a",
        )
    if candidate_type == "learn_source":
        return _candidate(
            candidate_type,
            evidence=(Evidence("00-inbox/old.md", "old body", False, ()),),
            facts=facts,
            related_ref="00-inbox/old.md",
        )
    if candidate_type == "retro_due":
        return _candidate(candidate_type, evidence=evidence, facts=facts)
    if candidate_type == "question_tune":
        return _candidate(
            candidate_type,
            evidence=evidence,
            facts={
                **facts,
                "name": "goal_link",
                "wording_json": contracts.canonical_json(playbook.questions()["goal_link"]),
            },
        )
    if candidate_type in contracts.CHANGES:
        # `_what`/`_base_action` read no fact or word for these three: a fixed sentence and
        # "open_approvals" regardless, so the generic evidence/facts fixture is enough.
        return _candidate(candidate_type, evidence=evidence, facts=facts)
    if candidate_type in contracts.CARDS:
        status = "blocked" if candidate_type == "card_needs_you" else "working"
        return _card(candidate_type, status)
    if candidate_type in contracts.SEATS:
        return _seat(candidate_type, reason="failed")
    if candidate_type in contracts.LOOKS:
        return _look(candidate_type, reason="stopped", kind="plan")
    if candidate_type in contracts.LOOK_MISSIONS:
        return _code_look(folder="Notes")
    if candidate_type in contracts.SECOND_LOOKS:
        return _second_look(candidate_type, refs=("notes/a.md", "notes/b.md"))
    if candidate_type in contracts.PROJECT_PLANS:
        return _project_plan(candidate_type, ref="10-projects/garden/garden.md")
    if candidate_type in contracts.MESSAGE_DRAFTS:
        return _message_draft(candidate_type, notes=False)
    if candidate_type in contracts.SAVED_CHECKS:
        return _saved_check(folder="Notes", phase="waiting")
    if candidate_type in contracts.INBOX_TRIAGE:
        return _inbox_triage(phase="waiting")
    raise AssertionError(f"unhandled type in test fixture: {candidate_type!r}")


# ---- shared assertions ---------------------------------------------------------------------------

_BANNED_SUBSTRINGS = ("jev", "decision engine")


def _assert_field_text(text: str, *, limit: int, allow_digits: bool) -> None:
    assert len(text) <= limit
    assert "\n" not in text
    assert "\r" not in text
    assert all(unicodedata.category(ch) != "Cc" for ch in text)
    lowered = text.lower()
    for banned in _BANNED_SUBSTRINGS:
        assert banned not in lowered
    if not allow_digits:
        assert not any(ch.isdigit() for ch in text), text


def _assert_valid_body(body: ItemBody) -> None:
    _assert_field_text(body.title, limit=300, allow_digits=True)
    _assert_field_text(body.what, limit=300, allow_digits=False)
    _assert_field_text(body.why, limit=300, allow_digits=False)
    assert body.what
    assert body.why
    assert len(body.evidence) <= 3
    for label in body.evidence:
        # Allowed, like `title`: a ref-derived label is the user's own note or file name.
        _assert_field_text(label.label, limit=120, allow_digits=True)
    for action in body.actions:
        _assert_field_text(action.label, limit=300, allow_digits=False)
        assert action.type in contracts.ACTIONS
    if body.actions:
        assert body.actions[0].primary
        assert all(not later.primary for later in body.actions[1:])


# ---- the full sweep: every candidate type, on both paths ----------------------------------------


@pytest.mark.parametrize("candidate_type", contracts.CANDIDATES)
def test_render_every_type_on_the_rules_path(candidate_type: str) -> None:
    candidate = _filled_candidate(candidate_type)
    outcome = _outcome(work="none", reason=f"rules:{_SAMPLE_REASON[candidate_type]}")
    body = render(candidate, outcome, {}, mission=None, grants={"prepare": "off", "run": "off"})
    _assert_valid_body(body)
    if candidate_type in contracts.CARDS:
        # A card's why is its own sentence, not the rule words.
        assert body.why == _CARD_SAMPLE_WHY[candidate_type]
    elif candidate_type in contracts.SEATS:
        assert body.why == _SEAT_SAMPLE_WHY[candidate_type]
    elif (
        candidate_type in contracts.LOOKS
        or candidate_type in contracts.LOOK_MISSIONS
        or candidate_type in contracts.SECOND_LOOKS
        or candidate_type in contracts.PROJECT_PLANS
        or candidate_type in contracts.MESSAGE_DRAFTS
        or candidate_type in contracts.SAVED_CHECKS
        or candidate_type in contracts.INBOX_TRIAGE
    ):
        # A Look's why is its own sentence; the rule words are only the fallback.
        assert body.why != RULE_REASON_WORDS[_SAMPLE_REASON[candidate_type]]
    else:
        assert body.why == RULE_REASON_WORDS[_SAMPLE_REASON[candidate_type]]


@pytest.mark.parametrize("candidate_type", contracts.CANDIDATES)
def test_render_every_type_on_the_model_path(candidate_type: str) -> None:
    candidate = _filled_candidate(candidate_type)
    outcome = _outcome(work="none", reason="model:scored")
    body = render(
        candidate,
        outcome,
        _RICH_MODEL_ANSWERS,
        mission=None,
        grants={"prepare": "off", "run": "off"},
    )
    _assert_valid_body(body)
    # The model path never quotes a rules reason verbatim.
    assert body.why not in RULE_REASON_WORDS.values()


def test_running_mission_is_also_covered_by_the_full_field_check() -> None:
    """A candidate with a mission already running passes the full field check too."""
    candidate = _filled_candidate("tasks_due")
    outcome = _outcome(work="run", reason="model:scored")
    mission = _mission(state="running")
    body = render(
        candidate,
        outcome,
        _RICH_MODEL_ANSWERS,
        mission=mission,
        grants={"prepare": "off", "run": "ask"},
    )
    _assert_valid_body(body)
    types = [action.type for action in body.actions]
    assert "open_chat" in types
    assert "stop_mission" in types
    assert "start_run" not in types


# ---- title: passthrough and sanitizing -----------------------------------------------------------


def test_title_is_the_candidates_own_text_verbatim() -> None:
    candidate = _candidate("approvals", title="Review budget v2 for 2026")
    body = render(candidate, _outcome(), {}, mission=None, grants={"prepare": "off", "run": "off"})
    assert body.title == "Review budget v2 for 2026"  # digits allowed: it is the user's own text


def test_title_strips_control_characters_and_collapses_whitespace() -> None:
    candidate = _candidate("approvals", title="Line one\nLine\ttwo\x07bell   spaced")
    body = render(candidate, _outcome(), {}, mission=None, grants={"prepare": "off", "run": "off"})
    assert body.title == "Line one Line two bell spaced"


_BIDI = "؜‎‏‪‫‬‭‮⁦⁧⁨⁩"


def test_bidi_controls_from_a_synced_card_never_reach_the_launcher() -> None:
    """A card whose title and seat tag carry an override (`owner/‮ops`) reads in logical order:
    every bidi control is dropped from the title, the sentence and the card's chip."""
    card = replace(
        _card("card_needs_you", "blocked", seat="‮ops"),
        title="Rotate the API key ‮tsoh ot gnimoc‬" + _BIDI,
    )
    body = render(card, _outcome(reason="rules:card_needs_decision"), {}, mission=None, grants={})

    assert body.title == "Rotate the API key tsoh ot gnimoc"
    assert body.why == "Ops blocked it last week."
    shown = [body.title, body.what, body.why, *(one.label for one in body.evidence)]
    assert not [text for text in shown if set(text) & set(_BIDI)]


def test_overlong_field_is_cut_with_ellipsis() -> None:
    candidate = _candidate("approvals", title="x" * 400)
    body = render(candidate, _outcome(), {}, mission=None, grants={"prepare": "off", "run": "off"})
    assert len(body.title) == 300
    assert body.title.endswith("…")
    assert body.title[:299] == "x" * 299


# ---- why: rules path --------------------------------------------------------------------------


@pytest.mark.parametrize("reason_code", rules.REASONS)
def test_why_on_rules_path_quotes_the_matching_reason_text(reason_code: str) -> None:
    candidate = _candidate("approvals")
    outcome = _outcome(reason=f"rules:{reason_code}")
    body = render(candidate, outcome, {}, mission=None, grants={"prepare": "off", "run": "off"})
    assert body.why == RULE_REASON_WORDS[reason_code]


def test_why_falls_back_to_the_rules_path_when_answers_are_empty_even_without_the_prefix() -> None:
    # No "rules:" prefix at all, but empty answers still mean "the rules decided".
    candidate = _candidate("approvals")
    outcome = _outcome(reason="waiting_on_you")
    body = render(candidate, outcome, {}, mission=None, grants={"prepare": "off", "run": "off"})
    assert body.why == RULE_REASON_WORDS["waiting_on_you"]


def test_why_on_rules_path_falls_back_for_an_unknown_reason_code() -> None:
    candidate = _candidate("approvals")
    outcome = _outcome(reason="rules:some_future_reason_not_yet_in_the_table")
    body = render(candidate, outcome, {}, mission=None, grants={"prepare": "off", "run": "off"})
    assert body.why  # never empty
    _assert_field_text(body.why, limit=300, allow_digits=False)


# ---- why: model path --------------------------------------------------------------------------


def test_why_on_model_path_joins_up_to_three_reasons_in_priority_order() -> None:
    # goal_link, owner_only and news alone already fill the cap of three, ahead of the deadline,
    # fits_now and importance fragments that `_RICH_MODEL_ANSWERS` would also qualify for.
    candidate = _candidate("tasks_due")
    outcome = _outcome(reason="model:scored")
    body = render(
        candidate,
        outcome,
        _RICH_MODEL_ANSWERS,
        mission=None,
        grants={"prepare": "off", "run": "off"},
    )
    assert body.why == (
        "linked to one of your goals; needs your decision; you haven't seen it lately"
    )


def test_why_on_model_path_falls_back_when_nothing_crosses_threshold() -> None:
    candidate = _candidate("approvals")
    outcome = _outcome(reason="model:scored")
    quiet_answers: contracts.Answers = {
        "goal_link": NoulAnswer(0.1),
        "owner_only": NoulAnswer(0.1),
        "news": NoulAnswer(0.1),
        "fits_now": NoulAnswer(0.1),
        "importance": ScoreAnswer(0.0, 0.9, {0: 0.9}),
    }
    body = render(
        candidate, outcome, quiet_answers, mission=None, grants={"prepare": "off", "run": "off"}
    )
    assert body.why
    assert body.why not in RULE_REASON_WORDS.values()


def test_deadline_reason_needs_both_the_field_and_its_word() -> None:
    soon = datetime(2026, 10, 22, 9, 0, tzinfo=UTC)
    answers: contracts.Answers = {"goal_link": NoulAnswer(0.0)}  # every noul threshold fails

    with_both = _candidate("tasks_due", deadline=soon, words={"deadline": "tomorrow"})
    body_both = render(
        with_both,
        _outcome(reason="model:scored"),
        answers,
        mission=None,
        grants={"prepare": "off", "run": "off"},
    )
    assert body_both.why == "due tomorrow"

    word_only = _candidate("tasks_due", deadline=None, words={"deadline": "tomorrow"})
    body_word_only = render(
        word_only,
        _outcome(reason="model:scored"),
        answers,
        mission=None,
        grants={"prepare": "off", "run": "off"},
    )
    assert "due tomorrow" not in body_word_only.why

    deadline_only = _candidate("tasks_due", deadline=soon, words={})
    body_deadline_only = render(
        deadline_only,
        _outcome(reason="model:scored"),
        answers,
        mission=None,
        grants={"prepare": "off", "run": "off"},
    )
    assert "due" not in body_deadline_only.why


# ---- actions: grants gate start_prepare / start_run ----------------------------------------------


@pytest.mark.parametrize(
    ("grant", "expect_start"),
    [("ask", True), ("allowed", False), ("off", False)],
)
def test_start_run_appears_only_when_its_grant_is_ask(grant: str, expect_start: bool) -> None:
    candidate = _candidate("tasks_due", related_ref="10-projects/demo/tasks.md")
    outcome = _outcome(work="run", reason="model:scored")
    body = render(
        candidate,
        outcome,
        _RICH_MODEL_ANSWERS,
        mission=None,
        grants={"prepare": "off", "run": grant},
    )
    has_start_run = any(action.type == "start_run" for action in body.actions)
    assert has_start_run is expect_start


@pytest.mark.parametrize(
    ("grant", "expect_start"),
    [("ask", True), ("allowed", False), ("off", False)],
)
def test_start_prepare_appears_only_when_its_grant_is_ask(grant: str, expect_start: bool) -> None:
    candidate = _candidate("tasks_due", related_ref="10-projects/demo/tasks.md")
    outcome = _outcome(work="prepare", reason="model:scored")
    body = render(
        candidate,
        outcome,
        _RICH_MODEL_ANSWERS,
        mission=None,
        grants={"prepare": grant, "run": "off"},
    )
    has_start_prepare = any(action.type == "start_prepare" for action in body.actions)
    assert has_start_prepare is expect_start


def test_coordinate_work_never_adds_an_action() -> None:
    candidate = _candidate("tasks_due", related_ref="10-projects/demo/tasks.md")
    outcome = _outcome(work="coordinate", reason="model:scored")
    body = render(
        candidate,
        outcome,
        _RICH_MODEL_ANSWERS,
        mission=None,
        grants={"prepare": "ask", "run": "ask"},
    )
    assert all(action.type not in ("start_prepare", "start_run") for action in body.actions)


# ---- actions: a running mission always wins; an ended one does not ------------------------------


def test_running_mission_replaces_start_run_with_open_chat_and_stop() -> None:
    candidate = _candidate("tasks_due", related_ref="10-projects/demo/tasks.md")
    outcome = _outcome(work="run", reason="model:scored")
    mission = _mission(state="running", session_id="sess-live", mission_id="mission-live")
    body = render(
        candidate,
        outcome,
        _RICH_MODEL_ANSWERS,
        mission=mission,
        grants={"prepare": "off", "run": "ask"},
    )
    by_type = {action.type: action for action in body.actions}
    assert "start_run" not in by_type
    assert by_type["open_chat"].session_id == "sess-live"
    assert by_type["stop_mission"].mission_id == "mission-live"


@pytest.mark.parametrize("state", ["done", "failed", "stopped", "unknown"])
def test_ended_mission_does_not_replace_the_start_action(state: str) -> None:
    candidate = _candidate("tasks_due", related_ref="10-projects/demo/tasks.md")
    outcome = _outcome(work="run", reason="model:scored")
    mission = _mission(state=state)
    body = render(
        candidate,
        outcome,
        _RICH_MODEL_ANSWERS,
        mission=mission,
        grants={"prepare": "off", "run": "ask"},
    )
    types = [action.type for action in body.actions]
    assert "start_run" in types
    assert "stop_mission" not in types


# ---- actions: open_context, mission_report's chat, and uncommitted's bare item -------------------


def test_call_id_adds_an_open_context_action_that_is_never_primary() -> None:
    candidate = _candidate("approvals")
    outcome = _outcome(work="none", reason="model:scored")
    body = render(
        candidate,
        outcome,
        _RICH_MODEL_ANSWERS,
        mission=None,
        grants={"prepare": "off", "run": "off"},
        call_id="call-123",
    )
    context_actions = [a for a in body.actions if a.type == "open_context"]
    assert len(context_actions) == 1
    assert context_actions[0].call_id == "call-123"
    assert context_actions[0].primary is False


def test_no_call_id_means_no_open_context_action() -> None:
    candidate = _candidate("approvals")
    body = render(
        candidate,
        _outcome(),
        _RICH_MODEL_ANSWERS,
        mission=None,
        grants={"prepare": "off", "run": "off"},
    )
    assert all(a.type != "open_context" for a in body.actions)


def test_mission_report_opens_the_related_chat_without_a_mission_kwarg() -> None:
    candidate = _candidate("mission_report", related_chat="session-ended")
    body = render(candidate, _outcome(), {}, mission=None, grants={"prepare": "off", "run": "off"})
    assert body.actions[0].type == "open_chat"
    assert body.actions[0].session_id == "session-ended"
    assert body.actions[0].primary is True


def test_mission_report_prefers_the_mission_kwargs_session_when_given() -> None:
    candidate = _candidate("mission_report", related_chat="session-ended")
    mission = _mission(state="done", session_id="session-from-mission")
    body = render(
        candidate, _outcome(), {}, mission=mission, grants={"prepare": "off", "run": "off"}
    )
    assert body.actions[0].session_id == "session-from-mission"


def test_uncommitted_has_no_base_action() -> None:
    candidate = _candidate("uncommitted")
    body = render(
        candidate, _outcome(work="none"), {}, mission=None, grants={"prepare": "off", "run": "off"}
    )
    assert body.actions == ()


# ---- what: a mission_report's wording depends on its state and reason ---------------------------


def _mission_report_candidate(*, state: str | None = None, reason: str | None = None) -> Candidate:
    """A mission_report candidate carrying just the facts `_mission_report_what` reads."""
    facts: dict[str, object] = {"workspace_id": "ws-1"}
    if state is not None:
        facts["state"] = state
    if reason is not None:
        facts["reason"] = reason
    return _candidate("mission_report", facts=facts, related_chat="session-x")


@pytest.mark.parametrize(
    ("state", "reason", "expected"),
    [
        ("running", "ready", "A plan from a chat VIKI started is ready for you."),
        ("done", "", "A chat VIKI started has finished."),
        ("failed", "unreachable", "A chat VIKI started stopped with an error."),
        ("failed", "not_sent", "A chat VIKI started stopped with an error."),
        ("stopped", "deadline", "A chat VIKI started reached its time limit."),
        ("stopped", "owner", "You stopped a chat VIKI started."),
        ("unknown", "closed", "A chat VIKI started closed."),
        ("unknown", "app_closed", "A chat VIKI started closed."),
    ],
    ids=[
        "ready",
        "done",
        "failed_unreachable",
        "failed_not_sent",
        "stopped_deadline",
        "stopped_owner",
        "unknown_closed",
        "unknown_app_closed",
    ],
)
def test_mission_report_what_matches_its_state_and_reason(
    state: str, reason: str, expected: str
) -> None:
    candidate = _mission_report_candidate(state=state, reason=reason)
    body = render(candidate, _outcome(), {}, mission=None, grants={"prepare": "off", "run": "off"})
    assert body.what == expected
    _assert_field_text(body.what, limit=300, allow_digits=False)


def test_mission_report_what_falls_back_safely_without_a_known_state_or_reason() -> None:
    candidate = _mission_report_candidate()  # neither "state" nor "reason" set
    body = render(candidate, _outcome(), {}, mission=None, grants={"prepare": "off", "run": "off"})
    assert body.what == "A chat VIKI started has finished."


def test_ready_mission_reports_primary_action_is_still_open_chat() -> None:
    # The ready report's primary action stays "open that chat", same as every other
    # mission_report case: only its wording changes.
    candidate = _mission_report_candidate(state="running", reason="ready")
    body = render(candidate, _outcome(), {}, mission=None, grants={"prepare": "off", "run": "off"})
    assert body.actions[0].type == "open_chat"
    assert body.actions[0].session_id == "session-x"
    assert body.actions[0].primary is True


# ---- evidence: capped at three, labelled by outside, excerpt never leaks -------------------------


def test_evidence_caps_at_three_and_labels_by_outside() -> None:
    four = (_VAULT_EVIDENCE, _OUTSIDE_EVIDENCE, _VAULT_EVIDENCE, _OUTSIDE_EVIDENCE)
    candidate = _candidate("approvals", evidence=four, facts={"workspace_id": "ws-1"})
    body = render(candidate, _outcome(), {}, mission=None, grants={"prepare": "off", "run": "off"})
    assert len(body.evidence) == 3
    assert body.evidence[0].label == body.evidence[2].label  # both vault entries, same label
    assert body.evidence[0].workspace_id == "ws-1"
    assert body.evidence[1].workspace_id is None  # outside: no vault workspace
    assert body.evidence[0].ref == "30-knowledge/some-note.md"
    # Outside evidence keeps its ref off the label too: workspace_id and ref are a pair or
    # neither, never one alone (the client's note chip needs both; see test_wire.py).
    assert body.evidence[1].ref is None


def test_every_evidence_chip_shape_matches_the_client_contract() -> None:
    """Every chip `_evidence` can build from a candidate's evidence — a vault entry with a
    ref, an outside entry with a ref, an outside entry without one, and a vault entry without
    one — plus the chat shape the wire layer also carries (built directly elsewhere, as in
    `wire.py`'s mission_report example, never by `templates.render`), sent through
    `wire.item_view` has exactly one of the client's three key sets (`parse.ts`'s
    `itemEvidence`): a bare label, a vault note (`workspace_id` and `ref` together) or a chat
    (`session_id`). Before the fix, an outside ref produced a lone `ref` and a no-ref vault
    entry (e.g. a skill proposal) a lone `workspace_id` — both shapes the client refuses."""
    outside_without_ref = Evidence(ref=None, excerpt="x", outside=True, tags=())
    vault_without_ref = Evidence(ref=None, excerpt="x", outside=False, tags=())
    candidate = _candidate(
        "approvals",
        evidence=(_VAULT_EVIDENCE, _OUTSIDE_EVIDENCE, outside_without_ref),
        facts={"workspace_id": "ws-1"},
    )
    body = render(candidate, _outcome(), {}, mission=None, grants={"prepare": "off", "run": "off"})
    lone_candidate = _candidate(
        "approvals", evidence=(vault_without_ref,), facts={"workspace_id": "ws-1"}
    )
    lone_body = render(
        lone_candidate, _outcome(), {}, mission=None, grants={"prepare": "off", "run": "off"}
    )
    chat_label = EvidenceLabel("Started by VIKI", session_id="session-1")

    chips = (
        _item_view_evidence(body.evidence)
        + _item_view_evidence(lone_body.evidence)
        + _item_view_evidence((chat_label,))
    )
    assert len(chips) == 5
    valid_shapes = (
        frozenset({"label"}),
        frozenset({"label", "workspace_id", "ref"}),
        frozenset({"label", "session_id"}),
    )
    for chip in chips:
        assert frozenset(chip) in valid_shapes, chip


def _item_view_evidence(evidence: tuple[EvidenceLabel, ...]) -> list[dict[str, object]]:
    """`evidence` as `wire.item_view` sends it, for the client-contract sweep above."""
    item = contracts.ItemRecord(
        item_key="item-key-1",
        episode_id="episode-1",
        candidate="approvals",
        kind="ask",
        level="list",
        state="open",
        timing="now",
        defer_until=None,
        created_at="2026-10-21T14:00:00.000Z",
        shown_at=None,
        mission_id=None,
        answers_json=None,
        state_hash=None,
        body=ItemBody(title="t", what="w", why="y", evidence=evidence, actions=()),
    )
    return wire.item_view(item)["evidence"]


def test_evidence_label_is_the_refs_file_name_without_extension() -> None:
    candidate = _candidate("approvals", evidence=(_VAULT_EVIDENCE,), facts={"workspace_id": "ws-1"})
    body = render(candidate, _outcome(), {}, mission=None, grants={"prepare": "off", "run": "off"})
    assert body.evidence[0].label == "some-note"


def test_evidence_label_allows_a_digit_from_a_dated_note_ref() -> None:
    # A daily note's basename is its date, digits and all — the user's own name, not a number
    # this module produced.
    dated = replace(_VAULT_EVIDENCE, ref="60-calendar/2026/2026-10-21.md")
    candidate = _candidate("approvals", evidence=(dated,), facts={"workspace_id": "ws-1"})
    body = render(candidate, _outcome(), {}, mission=None, grants={"prepare": "off", "run": "off"})
    assert body.evidence[0].label == "2026-10-21"


@pytest.mark.parametrize(
    ("outside", "expected"),
    [(False, "A note in your vault"), (True, "A source outside the vault")],
)
def test_evidence_label_falls_back_when_there_is_no_ref(outside: bool, expected: str) -> None:
    no_ref = Evidence(ref=None, excerpt="(never shown)", outside=outside, tags=())
    candidate = _candidate("approvals", evidence=(no_ref,), facts={"workspace_id": "ws-1"})
    body = render(candidate, _outcome(), {}, mission=None, grants={"prepare": "off", "run": "off"})
    assert body.evidence[0].label == expected


def test_evidence_excerpt_text_never_appears_in_the_rendered_body() -> None:
    candidate = _filled_candidate("approvals")  # its evidence excerpts are "THE-SECRET-EXCERPT-42"
    for path, answers in (("rules", {}), ("model", _RICH_MODEL_ANSWERS)):
        outcome = _outcome(reason="rules:waiting_on_you" if path == "rules" else "model:scored")
        body = render(
            candidate, outcome, answers, mission=None, grants={"prepare": "off", "run": "off"}
        )
        haystack = " ".join(
            [body.title, body.what, body.why]
            + [label.label for label in body.evidence]
            + [action.label for action in body.actions]
        )
        assert "THE-SECRET-EXCERPT-42" not in haystack


# ---- vocabulary completeness: no drift against rules.py / contracts.py --------------------------


def test_rule_reason_words_cover_every_rules_reason() -> None:
    assert set(RULE_REASON_WORDS) == set(rules.REASONS)


def test_action_labels_cover_every_action_type() -> None:
    assert set(ACTION_LABELS) == set(contracts.ACTIONS)


def test_unknown_candidate_type_raises() -> None:
    candidate = _candidate("not_a_real_type")
    with pytest.raises(ValueError, match="not_a_real_type"):
        render(candidate, _outcome(), {}, mission=None, grants={"prepare": "off", "run": "off"})


# ---- self-improvement: the draft action and its text ---------------------------------------------


@pytest.mark.parametrize("candidate_type", _DRAFT_IMPROVE)
def test_improve_items_draft_only(candidate_type: str) -> None:
    """A self-improvement item's only action is its primary draft, which carries text and
    sends nothing; a grant for prepare/run (never actually offered to these types) adds
    nothing, since their `outcome.work` stays "none"."""
    candidate = _filled_candidate(candidate_type)
    outcome = _outcome(work="none", reason=f"rules:{_SAMPLE_REASON[candidate_type]}")
    body = render(candidate, outcome, {}, mission=None, grants={"prepare": "ask", "run": "ask"})
    assert len(body.actions) == 1
    [action] = body.actions
    assert action.primary is True
    assert action.type in ("draft_chat", "draft_new")
    assert action.text
    assert len(action.text) <= 4_000


def test_drafts_quote_no_excerpt() -> None:
    """A draft's text is built from facts, refs and names only, never an evidence
    excerpt — including skill_offer's own evidence, whose `excerpt` holds a chat *title*, not a
    vault or outside excerpt, and which the draft text never repeats either."""
    secret = "THE-SECRET-EXCERPT-99"
    cases = {
        "skill_offer": _candidate(
            "skill_offer",
            evidence=(Evidence(None, secret, False, ()),),
            facts={"trigger": "correction", "sessions": ("session-a", "session-b")},
            related_chat="session-a",
        ),
        "learn_source": _candidate(
            "learn_source",
            evidence=(Evidence("00-inbox/old.md", secret, False, ()),),
            related_ref="00-inbox/old.md",
        ),
        "retro_due": _candidate("retro_due", evidence=(Evidence(None, secret, False, ()),)),
        "question_tune": _candidate(
            "question_tune",
            evidence=(Evidence(None, secret, False, ()),),
            facts={"name": "goal_link", "wording_json": "{}"},
        ),
    }
    for candidate_type, candidate in cases.items():
        body = render(
            candidate,
            _outcome(work="none"),
            {},
            mission=None,
            grants={"prepare": "off", "run": "off"},
        )
        [action] = [a for a in body.actions if a.text]
        assert secret not in (action.text or ""), candidate_type


def test_skill_draft_matches_the_client_prompt() -> None:
    """Drift guard: the skill_offer draft's six contract lines match
    `skillTriggers.ts::draftPrompt` verbatim; only the lead line and "Why now" differ by design
    (the client's lead quotes the turn, and its "Why now" counts chats where this module says
    "several", per the no-digit rule)."""
    source = (_REPO_ROOT / "frontend/src/state/skillTriggers.ts").read_text(encoding="utf-8")
    for line in _SKILL_DRAFT_CONTRACT:
        assert line in source, line

    candidate = _candidate(
        "skill_offer",
        facts={"trigger": "correction", "sessions": ("session-a",)},
        related_chat="session-a",
    )
    body = render(
        candidate, _outcome(work="none"), {}, mission=None, grants={"prepare": "off", "run": "off"}
    )
    [draft] = [a for a in body.actions if a.text]
    for line in _SKILL_DRAFT_CONTRACT:
        assert line in (draft.text or ""), line
    assert draft.text is not None and draft.text.endswith(
        "Why now: I corrected you in this chat; keep the corrected way."
    )


def test_skill_offer_evidence_chips_are_chats_not_notes() -> None:
    """A skill_offer's evidence chips are chats (labelled with the title, carrying the
    session_id): the other chats' titles, each paired with its own session id from
    `facts["sessions"][1:]`, never a note ref."""
    candidate = _candidate(
        "skill_offer",
        evidence=(
            Evidence(None, "Weekly review", False, ()),
            Evidence(None, "Draft the memo", False, ()),
        ),
        facts={"trigger": "long_session", "sessions": ("session-a", "session-b", "session-c")},
        related_chat="session-a",
    )
    body = render(
        candidate, _outcome(work="none"), {}, mission=None, grants={"prepare": "off", "run": "off"}
    )
    assert body.evidence == (
        EvidenceLabel("Weekly review", session_id="session-b"),
        EvidenceLabel("Draft the memo", session_id="session-c"),
    )


def test_skill_offer_evidence_blank_title_reads_a_chat() -> None:
    """An untitled chat's title must never reach a skill_offer evidence chip; a blank
    title (the client reports "" rather than that chat's first message) reads the fixed "A
    chat" label instead of an empty one."""
    candidate = _candidate(
        "skill_offer",
        evidence=(Evidence(None, "", False, ()),),
        facts={"trigger": "long_session", "sessions": ("session-a", "session-b")},
        related_chat="session-a",
    )
    body = render(
        candidate, _outcome(work="none"), {}, mission=None, grants={"prepare": "off", "run": "off"}
    )
    assert body.evidence == (EvidenceLabel("A chat", session_id="session-b"),)


def test_learn_source_what_reads_singular_and_plural() -> None:
    one = _candidate("learn_source", evidence=(Evidence("00-inbox/a.md", "a", False, ()),))
    two = _candidate(
        "learn_source",
        evidence=(
            Evidence("00-inbox/a.md", "a", False, ()),
            Evidence("00-inbox/b.md", "b", False, ()),
        ),
    )
    one_body = render(one, _outcome(), {}, mission=None, grants={"prepare": "off", "run": "off"})
    two_body = render(two, _outcome(), {}, mission=None, grants={"prepare": "off", "run": "off"})
    assert one_body.what == "A link you captured may be worth a literature note."
    assert two_body.what == "Links you captured may be worth literature notes."


def test_learn_source_draft_lists_every_captured_ref() -> None:
    candidate = _candidate(
        "learn_source",
        evidence=(
            Evidence("00-inbox/a.md", "a", False, ()),
            Evidence("00-inbox/b.md", "b", False, ()),
        ),
    )
    body = render(
        candidate, _outcome(work="none"), {}, mission=None, grants={"prepare": "off", "run": "off"}
    )
    [draft] = [a for a in body.actions if a.text]
    assert draft.text == (
        "Learn the sources I captured in these notes:\n"
        "- 00-inbox/a.md\n"
        "- 00-inbox/b.md\n"
        "Use this vault's own learn skill if it has one. Show me each literature note before "
        "you write it."
    )


def test_question_tune_what_and_draft_name_the_questions_label() -> None:
    wording_json = contracts.canonical_json(playbook.questions()["goal_link"])
    candidate = _candidate(
        "question_tune", facts={"name": "goal_link", "wording_json": wording_json}
    )
    body = render(
        candidate, _outcome(work="none"), {}, mission=None, grants={"prepare": "off", "run": "off"}
    )
    assert body.what == "One of Decision's questions isn't matching your answers: Linked to a goal?"
    [draft] = [a for a in body.actions if a.text]
    assert draft.text is not None
    assert 'Decision\'s question "Linked to a goal?" (goal_link)' in draft.text
    assert wording_json in draft.text
    assert "decision-question" in draft.text
    # A proposal keeps the focus's pointer to the guide as well as its outside-data ending.
    assert "the last two sentences of its focus" in draft.text
    assert f"{playbook.GUIDE_SENTENCE} {playbook.OUTSIDE_SENTENCE}" in wording_json


def test_retro_due_what_and_draft_are_fixed() -> None:
    candidate = _candidate("retro_due")
    body = render(
        candidate, _outcome(work="none"), {}, mission=None, grants={"prepare": "off", "run": "off"}
    )
    assert body.what == (
        "You marked several items Not useful lately; a retro could turn them into a lesson."
    )
    [draft] = [a for a in body.actions if a.text]
    assert draft.text == (
        "Run a retro on my recent work with VIKI: I marked several of Decision's items Not "
        "useful lately. Use this vault's own retro skill if it has one. Ask me before you "
        "change any memory, skill or instruction."
    )


def test_self_improvement_draft_labels_are_their_own_not_the_generic_one() -> None:
    """The four drafts each carry their own call to action, never the generic
    `ACTION_LABELS["draft_chat"/"draft_new"]`."""
    labels = {}
    for candidate_type in _DRAFT_IMPROVE:
        candidate = _filled_candidate(candidate_type)
        outcome = _outcome(work="none", reason=f"rules:{_SAMPLE_REASON[candidate_type]}")
        body = render(candidate, outcome, {}, mission=None, grants={"prepare": "off", "run": "off"})
        labels[candidate_type] = body.actions[0].label
    assert labels == {
        "skill_offer": "Make this a skill",
        "skill_suggest": "Use this skill",
        "learn_source": "Learn these sources",
        "retro_due": "Start a retro",
        "question_tune": "Draft better wording",
    }
    assert not set(labels.values()) & set(ACTION_LABELS.values())


def test_skill_suggest_name_appears_only_in_the_chip_and_the_draft() -> None:
    """The matched skill's name must reach only the evidence chip and the draft text —
    `title` (always the fixed `candidate.title`), `what` and `why` stay free of it, on both the
    rules and the model path."""
    name = "sprint-retro-marker"
    candidate = _candidate(
        "skill_suggest",
        title="A skill for this chat",
        facts={"session": "session-a", "skill": name},
        related_chat="session-a",
    )
    for outcome, answers in (
        (_outcome(work="none", reason="rules:skill_fits"), {}),
        (_outcome(work="none", reason="model:scored"), _RICH_MODEL_ANSWERS),
    ):
        body = render(
            candidate, outcome, answers, mission=None, grants={"prepare": "off", "run": "off"}
        )
        assert name not in body.title
        assert name not in body.what
        assert name not in body.why
        assert body.evidence == (EvidenceLabel(name),)
        [action] = body.actions
        assert action.text == f"/{name}"


def test_skill_suggest_without_a_name_renders_no_chip_and_no_action() -> None:
    """A defensive fallback, like every other type's facts read here: a skill_suggest candidate
    missing `facts["skill"]` renders no evidence chip and no action, never a raise."""
    candidate = _candidate(
        "skill_suggest", facts={"session": "session-a"}, related_chat="session-a"
    )
    body = render(
        candidate,
        _outcome(work="none", reason="rules:skill_fits"),
        {},
        mission=None,
        grants={"prepare": "off", "run": "off"},
    )
    assert body.evidence == ()
    assert body.actions == ()


# ---- vault-change items: `_what`/`_base_action` -----------------------------------
# The never-send block itself is state.py's job (`build_state`, proved in test_state.py); here
# only the fixed wording and the Approvals action, same as every other candidate type.


@pytest.mark.parametrize(
    ("candidate_type", "expected_what"),
    [
        ("note_link", "Two notes may belong together."),
        ("note_superseded", "A newer note may replace an older one."),
        ("tasks_ticked", "Ticked tasks are waiting to be archived."),
    ],
)
def test_change_items_what_is_fixed(candidate_type: str, expected_what: str) -> None:
    candidate = _candidate(candidate_type)
    body = render(candidate, _outcome(), {}, mission=None, grants={"prepare": "off", "run": "off"})
    assert body.what == expected_what
    _assert_field_text(body.what, limit=300, allow_digits=False)


@pytest.mark.parametrize("candidate_type", contracts.CHANGES)
def test_change_items_open_approvals(candidate_type: str) -> None:
    """`_base_action`: the vault edit itself is the draft (a chat the user sends from
    Approvals, per `decision_loop.changes`'s fixed texts, once they approve); this module's
    only action for the three vault-change types points there, with no text of its own."""
    candidate = _filled_candidate(candidate_type)
    outcome = _outcome(work="none", reason=f"rules:{_SAMPLE_REASON[candidate_type]}")
    body = render(candidate, outcome, {}, mission=None, grants={"prepare": "ask", "run": "ask"})
    assert len(body.actions) == 1
    [action] = body.actions
    assert action.primary is True
    assert action.type == "open_approvals"
    assert action.label == ACTION_LABELS["open_approvals"]
    assert action.text is None


# ---- Kanban cards -----------------------------------------------------------------------------

_GRANTS = {"prepare": "off", "run": "off"}

_AGE_AGO = {
    "today": "today",
    "yesterday": "yesterday",
    "this week": "a few days ago",
    "last week": "last week",
    "this month": "a few weeks ago",
    "older": "over a month ago",
    "": "recently",
    "someday": "recently",
}


def _card_body(candidate: Candidate, reason: str) -> ItemBody:
    return render(candidate, _outcome(reason=f"rules:{reason}"), {}, mission=None, grants=_GRANTS)


@pytest.mark.parametrize(
    ("candidate_type", "status", "seat", "reason", "what", "why"),
    [
        ("card_needs_you", "blocked", "research", "card_needs_decision",
         "A card needs your decision.", "Research blocked it last week."),
        ("card_needs_you", "blocked", None, "card_needs_decision",
         "A card needs your decision.", "It was blocked last week."),
        ("card_needs_you", "review", "research", "card_to_review",
         "A card is ready for your review.", "Research finished it last week."),
        ("card_needs_you", "review", None, "card_to_review",
         "A card is ready for your review.", "It was finished last week."),
        ("card_stale", "working", "research", "card_untouched",
         "A card has stopped moving.", "Working, last changed last week, with no handoff."),
        ("card_stale", "working", None, "card_untouched",
         "A card has stopped moving.", "Working, last changed last week, with no handoff."),
        ("card_stale", "todo", "research", "card_untouched",
         "A card has stopped moving.", "Waiting for research, last changed last week."),
        ("card_stale", "todo", None, "card_untouched",
         "A card has stopped moving.", "Waiting for a seat, last changed last week."),
    ],
)  # fmt: skip
def test_card_items_word_each_status_with_and_without_a_seat(
    candidate_type: str, status: str, seat: str | None, reason: str, what: str, why: str
) -> None:
    body = _card_body(_card(candidate_type, status, seat), reason)
    _assert_valid_body(body)
    assert (body.what, body.why) == (what, why)
    # The model path reads the same sentence.
    modelled = render(
        _card(candidate_type, status, seat),
        _outcome(reason="model:scored"),
        _RICH_MODEL_ANSWERS,
        mission=None,
        grants=_GRANTS,
    )
    assert modelled.why == why


def test_card_why_keeps_a_digit_seat_and_reads_another_status_as_its_rule_words() -> None:
    body = _card_body(_card("card_needs_you", "blocked", "seat2"), "card_needs_decision")
    assert body.why == "Seat2 blocked it last week."
    odd = _card_body(_card("card_needs_you", "Blocked"), "card_needs_decision")
    assert odd.why == RULE_REASON_WORDS["card_needs_decision"]
    stale = _card_body(_card("card_stale", "done"), "card_untouched")
    assert stale.why == RULE_REASON_WORDS["card_untouched"]


def test_card_why_reads_a_blank_or_space_led_seat_as_trimmed() -> None:
    def why(candidate_type: str, status: str, seat: str) -> str | None:
        candidate = replace(_card(candidate_type, status, seat), words={"age": "yesterday"})
        return _card_body(candidate, "card_untouched").why

    assert why("card_needs_you", "blocked", " ") == "It was blocked yesterday."
    assert why("card_needs_you", "blocked", " research") == "Research blocked it yesterday."
    assert why("card_stale", "todo", " ") == "Waiting for a seat, last changed yesterday."


@pytest.mark.parametrize(("age", "ago"), list(_AGE_AGO.items()))
def test_card_why_reads_every_age_word(age: str, ago: str) -> None:
    candidate = replace(_card("card_needs_you", "blocked"), words={"age": age})
    assert _card_body(candidate, "card_needs_decision").why == f"Research blocked it {ago}."
    no_age = replace(_card("card_stale", "todo"), words={"age": age})
    assert _card_body(no_age, "card_untouched").why == (
        f"Waiting for research, last changed {ago}."
    )


@pytest.mark.parametrize("candidate_type", contracts.CARDS)
def test_card_items_open_the_card_and_label_it_by_title(candidate_type: str) -> None:
    body = _card_body(_card(candidate_type, "blocked"), "card_needs_decision")
    assert body.title == "Find three sources"
    (action,) = body.actions
    assert (action.type, action.label, action.primary) == ("open_note", "Open card", True)
    assert (action.workspace_id, action.ref) == ("ws-1", "kanban/find-sources.md")
    (chip,) = body.evidence
    assert (chip.label, chip.workspace_id, chip.ref) == (
        "Find three sources",
        "ws-1",
        "kanban/find-sources.md",
    )
    untitled = replace(_card(candidate_type, "blocked"), title="")
    assert _card_body(untitled, "card_needs_decision").evidence[0].label == "find-sources"


# ---- routine seats ------------------------------------------------------------------------------


def _seat_body(candidate: Candidate, reason: str = "seat_report") -> ItemBody:
    return render(candidate, _outcome(reason=f"rules:{reason}"), {}, mission=None, grants=_GRANTS)


_HOLD_ENDS = {
    "limit": (
        "{} is held: today's Bot runs are used.",
        "It can start tomorrow, or raise Daily limit: Bot runs VIKI starts.",
    ),
    "plan_brake": (
        "{} is held by your plan brake.",
        "VIKI starts it once your plan use is below Pause automatic starts at Claude plan "
        "usage. You can start it yourself.",
    ),
    "quiet": (
        "{} is held during your quiet hours.",
        "In quiet hours VIKI starts only routines that stop at a draft. You can start it yourself.",
    ),
    "untested": (
        "{} is held: its routine changed since its test.",
        "Test the routine again so VIKI can start it.",
    ),
    "two_routines": (
        "{} is held: two routines work for this seat.",
        "Keep one enabled routine for this seat.",
    ),
}
_RUN_ENDS = {
    "failed": ("{} stopped with an error.", "Open its session to see where it stopped."),
    "deadline": ("{} reached its time limit.", "Open its session to see where it stopped."),
    "stopped": ("{} was stopped.", "Open its session to see where it stopped."),
    "blocked": (
        "{} could not start.",
        "Open its session to see why, then change one of its todo cards to try again.",
    ),
    "no_progress": (
        "{} ran without moving its cards.",
        "VIKI starts it again once you add or change one of its todo cards.",
    ),
}


def _seat_cases() -> list[tuple[str, dict[str, object], str, str]]:
    """(type, facts, what with "{}" for the seat, why)."""
    cases: list[tuple[str, dict[str, object], str, str]] = []
    waits = "Its run waits until you answer or it reaches its time limit."
    cases.append(("seat_waiting", {}, "{} needs your OK to continue.", waits))
    cases.append(("seat_waiting", {"question": True}, "{} has a question for you.", waits))
    working = "VIKI started it for a card waiting for its seat."
    cases.append(("seat_working", {}, "{} is working on a card.", working))
    ask = "Start Bots' work is set to Ask first."
    cases.append(("seat_start", {}, "{} could start on a waiting card.", ask))
    handoff = "A Bot wrote this card, and seats may not hand work to each other."
    cases.append(("seat_start", {"handoff": True}, "{} could start on a waiting card.", handoff))
    for reason, (what, why) in {**_RUN_ENDS, **_HOLD_ENDS}.items():
        cases.append(("seat_report", {"reason": reason}, what, why))
    return cases


@pytest.mark.parametrize(("candidate_type", "facts", "what", "why"), _seat_cases())
def test_seat_items_word_each_case_with_and_without_a_seat(
    candidate_type: str, facts: dict[str, object], what: str, why: str
) -> None:
    named = _seat_body(_seat(candidate_type, "research", **facts))
    _assert_valid_body(named)
    assert (named.what, named.why) == (what.format("Research"), why)
    # A seat name never reaches why, and the model path reads the same sentence.
    modelled = render(
        _seat(candidate_type, "research", **facts),
        _outcome(reason="model:scored"),
        _RICH_MODEL_ANSWERS,
        mission=None,
        grants=_GRANTS,
    )
    assert modelled.why == why
    anonymous = _seat_body(_seat(candidate_type, None, **facts))
    _assert_valid_body(anonymous)
    nobody = "A routine" if candidate_type == "seat_waiting" else "A Bot"
    assert (anonymous.what, anonymous.why) == (what.format(nobody), why)


def test_seat_items_carry_their_actions() -> None:
    def shape(candidate: Candidate) -> list[tuple[object, ...]]:
        return [
            (a.type, a.label, a.primary, a.session_id, a.routine_id)
            for a in _seat_body(candidate).actions
        ]

    session = "session-1"
    open_session = ("open_chat", "Open session", True, session, None)
    assert shape(_seat("seat_waiting")) == [open_session]
    assert shape(_seat("seat_working")) == [
        open_session,
        ("stop_seat", "Stop", False, None, "routine-1"),
    ]
    assert shape(_seat("seat_start")) == [("start_seat", "Start", True, None, "routine-1")]
    for reason in _RUN_ENDS:
        assert shape(_seat("seat_report", reason=reason)) == [open_session]
    for reason in _HOLD_ENDS:
        # The plan brake and quiet hours hold only VIKI's own start, so the user's Start stays.
        expected = [("open_routine", "Open routine", True, None, "routine-1")]
        if reason in ("plan_brake", "quiet"):
            expected.append(("start_seat", "Start", False, None, "routine-1"))
        assert shape(_seat("seat_report", reason=reason)) == expected


def test_seat_items_keep_a_digit_seat_and_read_an_unknown_report_as_rule_words() -> None:
    body = _seat_body(_seat("seat_working", "seat2"), "seat_working")
    assert body.what == "Seat2 is working on a card."
    odd = _seat_body(_seat("seat_report", reason="mystery"))
    assert (odd.what, odd.why) == (
        RULE_REASON_WORDS["seat_report"],
        RULE_REASON_WORDS["seat_report"],
    )
    blank = _seat_body(_seat("seat_report", " ", reason="failed"))
    assert blank.what == "A Bot stopped with an error."


# ---- Look items ---------------------------------------------------------------------------------


def _look_body(candidate: Candidate, *, model: bool = False) -> ItemBody:
    reason = "model:scored" if model else f"rules:{rules._LOOK_REASONS[candidate.type]}"
    answers = _RICH_MODEL_ANSWERS if model else {}
    return render(candidate, _outcome(reason=reason), answers, mission=None, grants=_GRANTS)


_RECOVERY_BLOCKED = (
    "Saves to this vault are stopped.",
    "A note operation didn't finish. Open Recovery in the vault view to clear it.",
)
_RECOVERY_STOPPED = (
    "A save to this vault stopped partway through.",
    "Open Recovery in the vault view before you save that note again.",
)
_RECOVERY_EXPIRING = (
    "A deleted note will soon be gone for good.",
    "Restore it from Recovery in the vault view before it expires.",
)
_RECOVERY_FULL = (
    "The storage for deleted notes, shared by your vaults, is nearly full.",
    "Once it is full, deleting a note fails in every vault. Restore the notes you need from "
    "Recovery in the vault that holds them.",
)
_MEMORY_WHY = (
    "Open Memory in the vault view, and Correct or Retire any memory that no longer holds."
)
_PAUSE_WHY = "Claude Code pauses that chat until you answer."
_MCP_WHY = (
    "Its loaded tools take a tenth or more of that chat's context. Turn it off with the chat's "
    "MCP servers button if the chat doesn't need it."
)
_MCP_WHAT_WITHOUT_SERVER = "An MCP server fills a large part of a chat's context."


@pytest.mark.parametrize(
    ("candidate", "what", "why"),
    [
        (_look("vault_recovery", reason="blocked"), *_RECOVERY_BLOCKED),
        (_look("vault_recovery", reason="stopped"), *_RECOVERY_STOPPED),
        (_look("vault_recovery", reason="expiring"), *_RECOVERY_EXPIRING),
        (_look("vault_recovery", reason="full"), *_RECOVERY_FULL),
        (
            _look("vault_recovery", reason="odd"),
            RULE_REASON_WORDS["recovery_needs_you"],
            RULE_REASON_WORDS["recovery_needs_you"],
        ),
        (
            _look("memory_drift", count=1),
            "A memory cites a note that changed after you approved it.",
            _MEMORY_WHY,
        ),
        (
            _look("memory_drift", count=2),
            "Some memories cite notes that changed after you approved them.",
            _MEMORY_WHY,
        ),
        (
            _look("memory_drift"),
            "A memory cites a note that changed after you approved it.",
            _MEMORY_WHY,
        ),
        (
            _look("bridge_stopped", provider="slack", restart=False),
            "The Slack bridge stopped.",
            "Turn it on again in Messaging.",
        ),
        (
            _look("bridge_stopped", provider="mattermost", restart=True),
            "The Mattermost bridge stopped.",
            "Restart VIKI Agent to use this bot again.",
        ),
        (
            _look("bridge_stopped", provider="discord"),
            "The Discord bridge stopped.",
            "Turn it on again in Messaging.",
        ),
        (
            _look("bridge_stopped", provider="irc"),
            "A messaging bridge stopped.",
            "Turn it on again in Messaging.",
        ),
        (
            _look("chat_waiting", "over an hour", kind="permission"),
            "A chat has waited over an hour for your OK.",
            _PAUSE_WHY,
        ),
        (
            _look("chat_waiting", "hours", kind="plan"),
            "A plan has waited hours for your approval.",
            _PAUSE_WHY,
        ),
        (
            _look("chat_waiting", "a few minutes", kind="question"),
            "A question has waited a few minutes for your answer.",
            _PAUSE_WHY,
        ),
        (
            _look("chat_waiting", kind="question"),
            "A question has waited a while for your answer.",
            _PAUSE_WHY,
        ),
        (
            _look("chat_waiting", "hours", kind="odd"),
            RULE_REASON_WORDS["chat_waiting"],
            RULE_REASON_WORDS["chat_waiting"],
        ),
        (
            _look("send_uncertain"),
            "A message you sent may not have reached Claude Code.",
            "Check the chat before you send it again; Drafts and send history keeps its text.",
        ),
        (
            _look("mcp_context", server="github"),
            "The github MCP server fills a large part of a chat's context.",
            _MCP_WHY,
        ),
        (_look("mcp_context"), _MCP_WHAT_WITHOUT_SERVER, _MCP_WHY),
    ],
)
def test_look_items_word_each_case(candidate: Candidate, what: str, why: str) -> None:
    for model in (False, True):
        body = _look_body(candidate, model=model)
        _assert_valid_body(body)
        assert body.what == what
        # An unknown fact leaves why to the rule words, which only the rules path quotes.
        if why != RULE_REASON_WORDS[rules._LOOK_REASONS[candidate.type]] or not model:
            assert body.why == why


def test_a_digit_server_name_passes_through_what_only() -> None:
    body = _look_body(_look("mcp_context", server="gh2"))
    assert body.what == "The gh2 MCP server fills a large part of a chat's context."
    assert not any(ch.isdigit() for ch in body.why)


@pytest.mark.parametrize(
    ("candidate_type", "action_type", "field", "value"),
    [
        ("vault_recovery", "open_vault", "workspace_id", "ws-1"),
        ("memory_drift", "open_vault", "workspace_id", "ws-1"),
        ("bridge_stopped", "open_messaging", "session_id", None),
        ("chat_waiting", "open_chat", "session_id", "session-1"),
        ("send_uncertain", "open_chat", "session_id", "session-1"),
        ("mcp_context", "open_chat", "session_id", "session-1"),
    ],
)
def test_look_items_open_their_feature(
    candidate_type: str, action_type: str, field: str, value: str | None
) -> None:
    body = _look_body(_look(candidate_type))
    (action,) = body.actions
    assert (action.type, action.label, action.primary) == (
        action_type,
        ACTION_LABELS[action_type],
        True,
    )
    assert getattr(action, field) == value
    assert body.evidence == ()


# ---- the code look ------------------------------------------------------------------------------

_CODE_LOOK_WHY = "VIKI can look them over in plan mode and tell you what is half-done or risky."


def _code_look_body(candidate: Candidate, *, model: bool) -> ItemBody:
    reason = "model:scored" if model else "rules:code_changes_waiting"
    answers = _RICH_MODEL_ANSWERS if model else {}
    return render(candidate, _outcome(reason=reason), answers, mission=None, grants=_GRANTS)


def test_a_code_look_words_its_folder_and_opens_it_in_code() -> None:
    for model in (False, True):
        named = _code_look_body(_code_look(folder="Notes"), model=model)
        _assert_valid_body(named)
        assert named.what == "Notes has uncommitted changes, untouched for a day or more."
        assert named.why == _CODE_LOOK_WHY
        (action,) = named.actions
        assert (action.type, action.label, action.primary) == ("open_code", "Open in Code", True)
        assert action.workspace_id == "ws-code"
        assert named.evidence == ()

        for folder in (None, "", 7):
            nameless = _code_look_body(
                _code_look() if folder is None else _code_look(folder=folder), model=model
            )
            _assert_valid_body(nameless)
            assert nameless.what == (
                "A code folder has uncommitted changes, untouched for a day or more."
            )
            assert nameless.why == _CODE_LOOK_WHY

        digits = _code_look_body(_code_look(folder="app2"), model=model)
        assert digits.what == "app2 has uncommitted changes, untouched for a day or more."
        assert not any(ch.isdigit() for ch in digits.why)


def test_a_code_look_offers_take_a_look_on_ask_first() -> None:
    def labels(candidate: Candidate, prepare: str) -> list[tuple[str, str, bool]]:
        body = render(
            candidate,
            _outcome(work="prepare", reason="rules:code_changes_waiting"),
            {},
            mission=None,
            grants={"prepare": prepare, "run": "off"},
        )
        return [(a.type, a.label, a.primary) for a in body.actions]

    code = _code_look(folder="Notes")
    assert labels(code, "ask") == [
        ("open_code", "Open in Code", True),
        ("start_prepare", "Take a look", False),
    ]
    assert labels(code, "allowed") == [("open_code", "Open in Code", True)]
    assert labels(code, "off") == [("open_code", "Open in Code", True)]
    due = _candidate("tasks_due", related_ref="tasks.md", facts={"workspace_id": "ws-1"})
    assert ("start_prepare", "Prepare a plan", False) in labels(due, "ask")


# ---- the saved checks ---------------------------------------------------------------------------

_CHECK_WHY_WAITING = (
    "VIKI can run the saved checks you have run there before and report how they went."
)
_CHECK_WHY = "The Code workbench's Checks tab shows their output."
_CHECK_WHAT = {
    "waiting": "{} moved to a new commit that its saved checks have not run on.",
    "running": "{}'s saved checks are running on its new commit.",
    "passed": "{}'s saved checks passed on its new commit.",
    "failed": "A saved check failed on {}'s new commit.",
    "stopped": "{}'s saved checks did not finish on its new commit.",
}


def _check_body(
    candidate: Candidate, *, model: bool, work: str = "none", run: str = "off"
) -> ItemBody:
    outcome = _outcome(work=work, reason="model:scored" if model else "rules:checks_not_run")
    answers = _RICH_MODEL_ANSWERS if model else {}
    return render(candidate, outcome, answers, mission=None, grants={"run": run})


def test_a_saved_check_item_words_each_phase_and_its_actions() -> None:
    nameless = {
        "waiting": "A code folder moved to a new commit that its saved checks have not run on.",
        "running": "A code folder's saved checks are running on its new commit.",
        "passed": "A code folder's saved checks passed on its new commit.",
        "failed": "A saved check failed on a code folder's new commit.",
        "stopped": "A code folder's saved checks did not finish on its new commit.",
    }
    assert tuple(nameless) == contracts.CHECK_PHASES
    for model in (False, True):
        for phase, template in _CHECK_WHAT.items():
            named = _check_body(_saved_check(folder="Notes", phase=phase), model=model)
            _assert_valid_body(named)
            assert named.what == template.format("Notes")
            assert named.why == (_CHECK_WHY_WAITING if phase == "waiting" else _CHECK_WHY)
            assert named.evidence == ()
            for folder in (None, "", 7):
                facts = {"phase": phase} if folder is None else {"phase": phase, "folder": folder}
                body = _check_body(_saved_check(**facts), model=model)
                _assert_valid_body(body)
                assert body.what == nameless[phase]
            # A digit rides only in `what`, and the sentences add none.
            digits = _check_body(_saved_check(folder="app2", phase=phase), model=model)
            assert digits.what == template.format("app2")
            assert not any(ch.isdigit() for ch in digits.why)
            assert not any(ch.isdigit() for ch in nameless[phase])
    # An unknown or missing phase reads waiting.
    for facts in ({"phase": "odd"}, {}):
        assert _check_body(_saved_check(**facts), model=False).what == nameless["waiting"]


def test_a_saved_check_item_offers_run_checks_only_for_a_waiting_folder_on_ask_first() -> None:
    def actions(phase: str, *, work: str, run: str) -> list[tuple[str, str, bool, str | None]]:
        body = _check_body(
            _saved_check(folder="Notes", phase=phase), model=False, work=work, run=run
        )
        return [(a.type, a.label, a.primary, a.workspace_id) for a in body.actions]

    open_code = ("open_code", "Open in Code", True, "ws-code")
    assert actions("waiting", work="run", run="ask") == [
        open_code,
        ("run_checks", "Run checks", False, "ws-code"),
    ]
    assert actions("waiting", work="run", run="allowed") == [open_code]
    assert actions("waiting", work="run", run="off") == [open_code]
    assert actions("waiting", work="none", run="ask") == [open_code]
    assert actions("running", work="none", run="ask") == [
        open_code,
        ("stop_checks", "Stop", False, "ws-code"),
    ]
    for phase in ("passed", "failed", "stopped"):
        assert actions(phase, work="run", run="ask") == [open_code]
    # Never a generic start action, even when the outcome asks for prepare.
    prepared = _check_body(_saved_check(phase="waiting"), model=False, work="prepare", run="ask")
    assert [a.type for a in prepared.actions] == ["open_code"]


# ---- the inbox triage ---------------------------------------------------------------------------

_TRIAGE_WHAT = {
    "waiting": "Your inbox has captures with no suggested destination.",
    "running": "VIKI is suggesting destinations for your inbox captures.",
    "ready": "Suggested destinations for your inbox captures are waiting.",
    "failed": "VIKI could not suggest destinations for your inbox captures.",
    "nothing": "VIKI found no destination it could suggest for your inbox captures.",
}
_TRIAGE_SHOWN = (
    "Triage inbox shows each suggestion beside its capture; you choose, then send the filing draft."
)
_TRIAGE_AGAIN = (
    "Triage inbox says what happened, and Ask VIKI to suggest there runs it again when you choose."
)
_TRIAGE_WHY = {
    "waiting": (
        "VIKI can ask Claude Code, with read-only tools, to suggest where each capture goes; "
        "nothing in your vault changes until you send the filing draft from Triage inbox."
    ),
    "running": _TRIAGE_SHOWN,
    "ready": _TRIAGE_SHOWN,
    "failed": _TRIAGE_AGAIN,
    "nothing": _TRIAGE_AGAIN,
}


def test_an_inbox_triage_item_words_each_phase_and_its_actions() -> None:
    def body(phase: str | None, *, model: bool, work: str, prepare: str) -> ItemBody:
        facts = {} if phase is None else {"phase": phase}
        reason = "model:scored" if model else "rules:inbox_unsorted"
        return render(
            _inbox_triage(**facts),
            _outcome(work=work, reason=reason),
            _RICH_MODEL_ANSWERS if model else {},
            mission=None,
            grants={"prepare": prepare},
        )

    assert tuple(_TRIAGE_WHAT) == contracts.TRIAGE_PHASES
    for model in (False, True):
        for phase, what in _TRIAGE_WHAT.items():
            item = body(phase, model=model, work="none", prepare="off")
            _assert_valid_body(item)
            assert (item.what, item.why) == (what, _TRIAGE_WHY[phase])
            assert item.evidence == ()
            assert not any(ch.isdigit() for ch in item.what + item.why)
        # An unknown or missing phase reads waiting.
        for phase in (None, "odd"):
            item = body(phase, model=model, work="none", prepare="off")
            assert (item.what, item.why) == (_TRIAGE_WHAT["waiting"], _TRIAGE_WHY["waiting"])

    def actions(phase: str, *, work: str, prepare: str) -> list[tuple[str, str, bool]]:
        item = body(phase, model=False, work=work, prepare=prepare)
        return [(a.type, a.label, a.primary) for a in item.actions]

    triage = ("open_triage", "Triage inbox", True)
    assert actions("waiting", work="prepare", prepare="ask") == [
        triage,
        ("suggest_inbox", "Suggest destinations", False),
    ]
    for phase in contracts.TRIAGE_PHASES:
        for prepare in ("allowed", "off"):
            assert actions(phase, work="prepare", prepare=prepare) == [triage]
        if phase != "waiting":
            assert actions(phase, work="prepare", prepare="ask") == [triage]
    assert actions("waiting", work="none", prepare="ask") == [triage]
    assert actions("waiting", work="run", prepare="ask") == [triage]


# ---- the second looks ---------------------------------------------------------------------------

_SECOND_LOOK_WHY = (
    "VIKI can take a second look in plan mode and tell you if it finds a flaw, a better approach "
    "or a missing piece."
)
_FINDING_WHY = (
    "Its plan waits in the chat VIKI started: Approve makes the change, and Keep planning "
    "refines it first."
)


def _second_look_body(
    candidate: Candidate, *, model: bool, reason: str = "work_finished"
) -> ItemBody:
    outcome = _outcome(reason="model:scored" if model else f"rules:{reason}")
    answers = _RICH_MODEL_ANSWERS if model else {}
    return render(candidate, outcome, answers, mission=None, grants=_GRANTS)


def test_a_second_look_offer_words_its_trigger_and_lists_its_refs() -> None:
    expected = {
        "long_task_look": "A long task just finished in this chat.",
        "correction_look": "Work you corrected just finished in this chat.",
    }
    refs = ("notes/plan.md", "a/b/Second Brain MOC.md")
    for model in (False, True):
        for candidate_type, what in expected.items():
            body = _second_look_body(_second_look(candidate_type, refs=refs), model=model)
            _assert_valid_body(body)
            assert (body.what, body.why) == (what, _SECOND_LOOK_WHY)
            assert [(e.label, e.workspace_id, e.ref) for e in body.evidence] == [
                ("plan", "ws-1", "notes/plan.md"),
                ("Second Brain MOC", "ws-1", "a/b/Second Brain MOC.md"),
            ]
            (action,) = body.actions
            assert (action.type, action.label, action.primary) == ("open_chat", "Open chat", True)
            assert action.session_id == "session-1"
    many = tuple(f"n/{i}.md" for i in range(contracts.MAX_SECOND_LOOK_REFS + 3))
    capped = _second_look_body(_second_look(refs=many), model=False)
    assert len(capped.evidence) == contracts.MAX_SECOND_LOOK_REFS
    assert _second_look_body(_second_look(), model=False).evidence == ()
    junk = _second_look_body(_second_look(refs=(7, "", "x/ok.md")), model=False)
    assert [e.ref for e in junk.evidence] == ["x/ok.md"]


def test_a_finding_names_its_kind_and_opens_its_plan() -> None:
    kinds = {
        "flaw": "a possible flaw",
        "approach": "a possible better approach",
        "missing": "a possible missing piece",
        "other": "something worth a look",
    }
    for model in (False, True):
        for candidate_type in contracts.SECOND_LOOKS:
            for kind, words in kinds.items():
                candidate = _second_look(candidate_type, found=kind, chat="Weekly review")
                body = _second_look_body(candidate, model=model, reason="finding_ready")
                _assert_valid_body(body)
                assert body.what == f'VIKI found {words} in "Weekly review".'
                assert body.why == _FINDING_WHY
                assert body.evidence == ()
                (action,) = body.actions
                assert (action.type, action.label, action.primary) == (
                    "open_chat",
                    "Open plan",
                    True,
                )
                assert action.session_id == "session-1"
        for chat in (None, "", 7):
            untitled = (
                _second_look(found="flaw")
                if chat is None
                else _second_look(found="flaw", chat=chat)
            )
            body = _second_look_body(untitled, model=model, reason="finding_ready")
            assert body.what == "VIKI found a possible flaw in your chat."
        unknown = _second_look(found="surprise", chat="Weekly review")
        assert _second_look_body(unknown, model=model).what == (
            'VIKI found something worth a look in "Weekly review".'
        )
        digits = _second_look_body(_second_look(found="flaw", chat="Q3 plan"), model=model)
        assert digits.what == 'VIKI found a possible flaw in "Q3 plan".'
        assert not any(ch.isdigit() for ch in digits.why)


def test_a_second_look_offers_take_a_second_look_on_ask_first() -> None:
    def labels(prepare: str) -> list[tuple[str, str, bool]]:
        body = render(
            _second_look(refs=("notes/a.md",)),
            _outcome(work="prepare", reason="rules:work_finished"),
            {},
            mission=None,
            grants={"prepare": prepare, "run": "off"},
        )
        return [(a.type, a.label, a.primary) for a in body.actions]

    assert labels("ask") == [
        ("open_chat", "Open chat", True),
        ("start_prepare", "Take a second look", False),
    ]
    assert labels("allowed") == [("open_chat", "Open chat", True)]
    assert labels("off") == [("open_chat", "Open chat", True)]


@pytest.mark.parametrize("found", [None, "flaw"])
def test_a_running_second_look_offers_one_chat_to_open(found: str | None) -> None:
    """While the look's mission runs, its chat and Stop are the actions: the user's own chat is
    not offered beside it, so no two buttons share a name (or a React key)."""
    look = _second_look(refs=("notes/a.md",)) if found is None else _second_look(found=found)
    body = render(
        look,
        _outcome(work="prepare", reason="rules:work_finished"),
        {},
        mission=_mission(work="prepare", session_id="sess-live"),
        grants={"prepare": "allowed", "run": "off"},
    )

    assert [(a.type, a.label, a.primary, a.session_id) for a in body.actions] == [
        ("open_chat", "Open chat", True, "sess-live"),
        ("stop_mission", "Stop", False, None),
    ]


# ---- the project plans --------------------------------------------------------------------------

_PLAN_REF = "10-projects/garden/garden.md"
_PLAN_WHY = {
    "stalled_project": (
        "VIKI can look the project over in plan mode and propose three next actions; it changes "
        "nothing until you approve."
    ),
    "overdue_tasks": (
        "VIKI can check them in plan mode and propose ticking the done ones and new dates for the "
        "rest; it changes nothing until you approve."
    ),
}


def test_a_project_plan_offer_words_its_project_and_opens_its_note() -> None:
    named = {
        "stalled_project": 'Nothing in the project "Garden 2" has changed for two weeks or more.',
        "overdue_tasks": 'Overdue tasks in "Garden 2" may be done or need a new date.',
    }
    blank = {
        "stalled_project": "Nothing in an active project has changed for two weeks or more.",
        "overdue_tasks": "Overdue tasks may be done or need a new date.",
    }
    for model in (False, True):
        for candidate_type in contracts.PROJECT_PLANS:
            reason = _SAMPLE_REASON[candidate_type]
            body = _second_look_body(
                _project_plan(candidate_type, ref=_PLAN_REF, project="Garden 2"),
                model=model,
                reason=reason,
            )
            # `what` and the chip carry the project's digit; every other field stays digit-free.
            assert (body.what, body.why) == (named[candidate_type], _PLAN_WHY[candidate_type])
            assert not any(ch.isdigit() for ch in body.why + body.title)
            plain = _project_plan(candidate_type, ref=_PLAN_REF)
            _assert_valid_body(_second_look_body(plain, model=model, reason=reason))
            assert [(e.label, e.workspace_id, e.ref) for e in body.evidence] == [
                ("Garden 2", "ws-1", _PLAN_REF)
            ]
            (action,) = body.actions
            assert (action.type, action.label, action.primary) == ("open_note", "Open note", True)
            assert (action.workspace_id, action.ref) == ("ws-1", _PLAN_REF)
            for project in (None, "", "  ", 7):
                facts = (
                    {"ref": _PLAN_REF}
                    if project is None
                    else {"ref": _PLAN_REF, "project": project}
                )
                candidate = _candidate(candidate_type, title="project", facts=facts)
                unnamed = _second_look_body(candidate, model=model, reason=reason)
                assert unnamed.what == blank[candidate_type]
                assert [e.label for e in unnamed.evidence] == ["garden"]


def test_a_project_plans_finding_opens_its_plan() -> None:
    for model in (False, True):
        for candidate_type in contracts.PROJECT_PLANS:
            candidate = _project_plan(candidate_type, found="plan", session="session-1")
            body = _second_look_body(candidate, model=model, reason="plan_ready")
            _assert_valid_body(body)
            assert body.what == 'VIKI\'s plan for "Garden" waits for your approval.'
            assert body.why == _FINDING_WHY
            assert body.evidence == ()
            (action,) = body.actions
            assert (action.type, action.label, action.primary) == ("open_chat", "Open plan", True)
            assert action.session_id == "session-1"
            blank = _project_plan(candidate_type, found="plan", session="s", project="")
            assert _second_look_body(blank, model=model).what == (
                "VIKI's plan for your project waits for your approval."
            )
        digits = _project_plan("stalled_project", found="plan", project="Q3 plan")
        body = _second_look_body(digits, model=model)
        assert body.what == 'VIKI\'s plan for "Q3 plan" waits for your approval.'
        assert not any(ch.isdigit() for ch in body.why)


def test_a_project_plan_offers_its_start_on_ask_first() -> None:
    def labels(candidate_type: str, prepare: str) -> list[tuple[str, str, bool]]:
        body = render(
            _project_plan(candidate_type, ref=_PLAN_REF),
            _outcome(work="prepare", reason=f"rules:{_SAMPLE_REASON[candidate_type]}"),
            {},
            mission=None,
            grants={"prepare": prepare, "run": "off"},
        )
        return [(a.type, a.label, a.primary) for a in body.actions]

    start = {"stalled_project": "Plan next steps", "overdue_tasks": "Check these tasks"}
    for candidate_type, label in start.items():
        assert labels(candidate_type, "ask") == [
            ("open_note", "Open note", True),
            ("start_prepare", label, False),
        ]
        assert labels(candidate_type, "allowed") == [("open_note", "Open note", True)]
        assert labels(candidate_type, "off") == [("open_note", "Open note", True)]


# ---- the message drafts -------------------------------------------------------------------------

_HANDOFF_TEXT = (
    "This chat's context is nearly full. Write a handoff I can take to a fresh chat: the "
    "decisions we made, the open questions and the next steps, each as a short list that names "
    "the files and notes it concerns. Reply with the handoff alone, and do not create or change "
    "any file."
)
_SOURCE_NOTE_TEXT = (
    "Write a literature note in this vault for each web page you fetched in your last reply. "
    "Use this vault's own learn skill if it has one. "
    "{search}Show me each literature note and wait for my go-ahead before you write it."
)
_SOURCE_NOTE_SEARCH = (
    "Your last reply read no note in this vault, so first search it for notes on the same "
    "subject and link the ones you find from each new note. "
)
_MESSAGE_DRAFT_WHY = {
    "context_handoff": (
        "VIKI can put a message in this chat's box asking for a handoff: the decisions, open "
        "questions and next steps. Send it, then choose New chat with this turn on the reply to "
        "carry on in a fresh chat."
    ),
    "source_note": (
        "VIKI can put a message in this chat's box that asks for a literature note for each page "
        "and asks to see each note before it is written."
    ),
}


def test_a_message_draft_drafts_its_fixed_text_into_its_chat() -> None:
    cases = [
        (
            "context_handoff",
            {},
            "Draft a handoff",
            "This chat's context is nearly full.",
            _HANDOFF_TEXT,
        ),
        (
            "context_handoff",
            {"notes": True},
            "Draft a handoff",
            "This chat's context is nearly full.",
            _HANDOFF_TEXT,
        ),
        (
            "source_note",
            {"notes": True},
            "Draft literature notes",
            "Your last reply in this chat fetched web pages.",
            _SOURCE_NOTE_TEXT.format(search=""),
        ),
        (
            "source_note",
            {"notes": False},
            "Draft literature notes",
            "Your last reply in this chat fetched web pages and read no note in your vault.",
            _SOURCE_NOTE_TEXT.format(search=_SOURCE_NOTE_SEARCH),
        ),
    ]
    for model in (False, True):
        for candidate_type, facts, label, what, text in cases:
            candidate = _message_draft(candidate_type, **facts)
            reason = _SAMPLE_REASON[candidate_type]
            body = _second_look_body(candidate, model=model, reason=reason)
            _assert_valid_body(body)
            assert (body.what, body.why) == (what, _MESSAGE_DRAFT_WHY[candidate_type])
            assert body.evidence == ()
            (action,) = body.actions
            assert (action.type, action.label, action.primary) == ("draft_chat", label, True)
            assert (action.session_id, action.text) == ("session-1", text)
            assert not any(ch.isdigit() for ch in body.what + body.why + text)
    # Only `notes is True` drops the search-first sentence.
    plain = _message_draft("source_note", notes="yes")
    assert _second_look_body(plain, model=False).actions[0].text == _SOURCE_NOTE_TEXT.format(
        search=_SOURCE_NOTE_SEARCH
    )
