"""Tests for the rules policy: presence, decisions, evidence hashing.

A port of the earlier engine's decision-path tests, with every case about its modes, study
schedule or fallback left out: this module ships rules only.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from viki_agent.decision_loop import contracts, rules
from viki_agent.decision_loop.rules import Prefs, Verdict, decide, evidence_key, present

NOW = datetime(2026, 10, 9, 9, 0, 0, tzinfo=UTC)


# ---- vocabularies and constants ------------------------------------------------------


def test_vocabularies_hold_every_value_the_design_names() -> None:
    assert rules.ATTENTION == rules.ELIGIBLE == ("silent", "defer", "notify")
    assert rules.POLICY_VERSION == "rules-1"


def test_reasons_cover_every_rule_with_no_duplicates() -> None:
    expected = {
        "muted_by_owner",
        "later_by_owner",
        "show_all",
        "waiting_on_you",
        "due_or_overdue",
        "due_soon",
        "runs_to_review",
        "runs_working",
        "inbox_pressure",
        "inbox_below_nudge",
        "uncommitted_changes",
        "routines_paused",
        "routine_soon",
        "routine_later",
        "mission_ended",
        "skill_hint",
        "needs_decision_model",
        "labels_piled_up",
        "question_drift",
        "tasks_ticked",
        "skill_fits",
        "card_needs_decision",
        "card_to_review",
        "card_untouched",
        "seat_waiting",
        "seat_working",
        "seat_could_start",
        "seat_report",
        "recovery_needs_you",
        "memory_source_changed",
        "bridge_stopped",
        "chat_waiting",
        "send_uncertain",
        "mcp_context_heavy",
        "code_changes_waiting",
        "work_finished",
        "finding_ready",
        "project_stalled",
        "tasks_overdue",
        "plan_ready",
        "context_nearly_full",
        "pages_to_note",
        "checks_not_run",
        "inbox_unsorted",
    }
    assert set(rules.REASONS) == expected
    assert len(rules.REASONS) == len(expected)  # no duplicates


def test_constants_match_the_threshold_decisions() -> None:
    assert rules.INBOX_ITEMS == 3
    assert rules.INBOX_DAYS == 3
    assert timedelta(hours=12) == rules.ROUTINE_SOON
    assert rules.RETRO_LABELS == 5


# The earlier engine's candidates, in its order: rows migrated at storage step 24 carry them.
LEGACY_CANDIDATES = ("approvals", "tasks_due", "agent_runs", "inbox", "uncommitted", "routine")


def test_candidate_names_match_the_legacy_policy_one_to_one() -> None:
    """The earlier engine's own candidates, in order, plus the new mission_report, the Kanban card
    types and the self-improvement types (`IMPROVE`), appended last."""
    assert (
        *LEGACY_CANDIDATES,
        "mission_report",
        *contracts.CARDS,
        *contracts.SEATS,
        *contracts.LOOKS,
        *contracts.LOOK_MISSIONS,
        *contracts.SECOND_LOOKS,
        *contracts.PROJECT_PLANS,
        *contracts.MESSAGE_DRAFTS,
        *contracts.SAVED_CHECKS,
        *contracts.INBOX_TRIAGE,
        *contracts.IMPROVE,
    ) == contracts.CANDIDATES


# ---- present() ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("candidate", "evidence", "expected"),
    [
        ("approvals", {"native": 0, "proposals": 0}, False),
        ("approvals", {"native": 1, "proposals": 0}, True),
        ("approvals", {"native": 0, "proposals": None}, False),
        ("approvals", {"native": 0, "proposals": 1}, True),
        ("tasks_due", {"overdue": 0, "today": 0, "soon": 0}, False),
        ("tasks_due", {"overdue": 0, "today": 0, "soon": 1}, True),
        ("agent_runs", {"working": 0, "review": 0}, False),
        ("agent_runs", {"working": 1, "review": 0}, True),
        ("inbox", {"count": 0}, False),
        ("inbox", {"count": 1}, True),
        ("uncommitted", {"dirty": 0}, False),
        ("uncommitted", {"dirty": 1}, True),
        ("routine", {"routine_id": None}, False),
        ("routine", {"routine_id": "r1"}, True),
    ],
)
def test_present_follows_the_per_candidate_rule(
    candidate: str, evidence: dict[str, object], expected: bool
) -> None:
    assert present(candidate, evidence) is expected


def test_present_rejects_an_unknown_candidate() -> None:
    with pytest.raises(ValueError, match="unknown candidate"):
        present("nope", {})


def test_present_has_no_rule_for_mission_report() -> None:
    """A finished mission is reported through `decide` directly, never polled for presence."""
    with pytest.raises(ValueError, match="unknown candidate"):
        present("mission_report", {})


@pytest.mark.parametrize(
    ("candidate", "evidence", "expected"),
    [
        ("skill_offer", {"chats": 0, "trigger": "correction"}, False),
        ("skill_offer", {"chats": 1, "trigger": "correction"}, True),
        ("skill_suggest", {"score": 0}, False),
        ("skill_suggest", {"score": 1}, True),
        ("learn_source", {"links": 0}, False),
        ("learn_source", {"links": 1}, True),
    ],
)
def test_present_follows_the_per_candidate_rule_for_self_improvement(
    candidate: str, evidence: dict[str, object], expected: bool
) -> None:
    assert present(candidate, evidence) is expected


@pytest.mark.parametrize(
    ("candidate", "evidence", "expected"),
    [
        ("note_link", {"pairs": 0}, False),
        ("note_link", {"pairs": 1}, True),
        ("note_superseded", {"pairs": 0}, False),
        ("note_superseded", {"pairs": 1}, True),
        ("tasks_ticked", {"ticked": 0}, False),
        ("tasks_ticked", {"ticked": 1}, True),
    ],
)
def test_present_follows_the_per_candidate_rule_for_changes(
    candidate: str, evidence: dict[str, object], expected: bool
) -> None:
    """The paired types present on a pair count, tasks_ticked on a ticked count."""
    assert present(candidate, evidence) is expected


@pytest.mark.parametrize("candidate", ["retro_due", "question_tune"])
def test_present_has_no_rule_for_the_beat_cached_self_improvement_types(candidate: str) -> None:
    """Like `mission_report`, `retro_due` and `question_tune` are never gathered per request
    (the engine caches them once a beat and appends them straight into `_decide`), so they are
    never polled for presence either."""
    with pytest.raises(ValueError, match="unknown candidate"):
        present(candidate, {})


@pytest.mark.parametrize(
    ("candidate", "status", "expected"),
    [
        ("card_needs_you", "blocked", True),
        ("card_needs_you", "review", True),
        ("card_needs_you", "working", False),
        ("card_needs_you", "todo", False),
        ("card_needs_you", "done", False),
        ("card_needs_you", "Blocked", False),
        ("card_stale", "working", True),
        ("card_stale", "todo", True),
        ("card_stale", "blocked", False),
        ("card_stale", "review", False),
        ("card_stale", "done", False),
    ],
)
def test_present_follows_the_card_status(candidate: str, status: str, expected: bool) -> None:
    assert present(candidate, {"status": status}) is expected


@pytest.mark.parametrize(
    ("candidate", "status", "reason"),
    [
        ("card_needs_you", "blocked", "card_needs_decision"),
        ("card_needs_you", "review", "card_to_review"),
        ("card_stale", "working", "card_untouched"),
        ("card_stale", "todo", "card_untouched"),
    ],
)
def test_decide_notifies_each_card_status(candidate: str, status: str, reason: str) -> None:
    evidence = {"status": status}
    assert decide(candidate, evidence, NOW, Prefs()) == Verdict(
        candidate, evidence, "notify", reason, None
    )


@pytest.mark.parametrize(
    ("candidate", "reason"),
    [
        ("seat_waiting", "seat_waiting"),
        ("seat_working", "seat_working"),
        ("seat_start", "seat_could_start"),
        ("seat_report", "seat_report"),
    ],
)
def test_seat_items_are_present_and_notify(candidate: str, reason: str) -> None:
    assert present(candidate, {}) is True
    assert decide(candidate, {}, NOW, Prefs()) == Verdict(candidate, {}, "notify", reason, None)
    assert reason in rules.REASONS


@pytest.mark.parametrize(
    ("candidate", "reason"),
    [
        ("vault_recovery", "recovery_needs_you"),
        ("memory_drift", "memory_source_changed"),
        ("bridge_stopped", "bridge_stopped"),
        ("chat_waiting", "chat_waiting"),
        ("send_uncertain", "send_uncertain"),
        ("mcp_context", "mcp_context_heavy"),
    ],
)
def test_look_items_are_present_and_notify(candidate: str, reason: str) -> None:
    assert candidate in contracts.LOOKS
    assert present(candidate, {}) is True
    assert decide(candidate, {}, NOW, Prefs()) == Verdict(candidate, {}, "notify", reason, None)
    assert reason in rules.REASONS


def test_a_code_look_is_present_and_notifies() -> None:
    assert contracts.LOOK_MISSIONS == ("code_look",)
    assert present("code_look", {}) is True
    assert decide("code_look", {}, NOW, Prefs()) == Verdict(
        "code_look", {}, "notify", "code_changes_waiting", None
    )
    assert set(rules._LOOK_MISSION_REASONS) == set(contracts.LOOK_MISSIONS)
    assert "code_changes_waiting" in rules.REASONS


@pytest.mark.parametrize("candidate", contracts.SECOND_LOOKS)
def test_a_second_look_is_present_and_notifies_by_what_it_carries(candidate: str) -> None:
    offer = {"trigger": "correction"}
    finding = {"found": "flaw"}
    assert present(candidate, offer) is True
    assert present(candidate, finding) is True
    assert decide(candidate, offer, NOW, Prefs()) == Verdict(
        candidate, offer, "notify", "work_finished", None
    )
    assert decide(candidate, finding, NOW, Prefs()) == Verdict(
        candidate, finding, "notify", "finding_ready", None
    )
    # An unknown kind still reads as a finding: the key decides, not its value.
    assert decide(candidate, {"found": "other"}, NOW, Prefs()).reason == "finding_ready"


@pytest.mark.parametrize(
    ("candidate", "offer_reason"),
    [("stalled_project", "project_stalled"), ("overdue_tasks", "tasks_overdue")],
)
def test_a_project_plan_is_present_and_notifies_by_what_it_carries(
    candidate: str, offer_reason: str
) -> None:
    offer: dict[str, object] = {}
    finding = {"found": "plan"}
    assert contracts.PROJECT_PLANS == ("stalled_project", "overdue_tasks")
    assert present(candidate, offer) is True
    assert present(candidate, finding) is True
    assert decide(candidate, offer, NOW, Prefs()) == Verdict(
        candidate, offer, "notify", offer_reason, None
    )
    assert decide(candidate, finding, NOW, Prefs()) == Verdict(
        candidate, finding, "notify", "plan_ready", None
    )


@pytest.mark.parametrize(
    ("candidate", "reason"),
    [("context_handoff", "context_nearly_full"), ("source_note", "pages_to_note")],
)
def test_a_message_draft_is_present_and_notifies_with_its_types_reason(
    candidate: str, reason: str
) -> None:
    evidence: dict[str, object] = {}
    assert contracts.MESSAGE_DRAFTS == ("context_handoff", "source_note")
    assert present(candidate, evidence) is True
    assert decide(candidate, evidence, NOW, Prefs()) == Verdict(
        candidate, evidence, "notify", reason, None
    )
    assert reason in rules.REASONS


def test_a_saved_check_is_present_and_notifies_with_its_reason() -> None:
    evidence: dict[str, object] = {}
    assert contracts.SAVED_CHECKS == ("commit_checks",)
    assert present("commit_checks", evidence) is True
    assert decide("commit_checks", evidence, NOW, Prefs()) == Verdict(
        "commit_checks", evidence, "notify", "checks_not_run", None
    )
    assert "checks_not_run" in rules.REASONS


def test_an_inbox_triage_item_is_present_and_notifies_with_its_reason() -> None:
    evidence: dict[str, object] = {}
    assert contracts.INBOX_TRIAGE == ("inbox_triage",)
    assert present("inbox_triage", evidence) is True
    assert decide("inbox_triage", evidence, NOW, Prefs()) == Verdict(
        "inbox_triage", evidence, "notify", "inbox_unsorted", None
    )
    assert "inbox_unsorted" in rules.REASONS


def test_every_look_has_a_reason() -> None:
    assert set(rules._LOOK_REASONS) == set(contracts.LOOKS)


# ---- decide(): input validation --------------------------------------------------------


def test_decide_rejects_an_unknown_candidate() -> None:
    with pytest.raises(ValueError, match="unknown candidate"):
        decide("nope", {}, NOW, Prefs())


# ---- decide(): mute and later precedence, for every candidate --------------------------


def test_mute_silences_the_candidate() -> None:
    prefs = Prefs(muted=frozenset({"inbox"}))
    evidence = {"count": 10, "oldest": NOW, "checked": 0, "undated": 0, "partial": False}
    result = decide("inbox", evidence, NOW, prefs)
    assert result == Verdict("inbox", evidence, "silent", "muted_by_owner", None)


def test_later_defers_while_still_due() -> None:
    later_until = NOW + timedelta(hours=1)
    prefs = Prefs(later={"inbox": later_until})
    evidence = {"count": 10, "oldest": NOW, "checked": 0, "undated": 0, "partial": False}
    result = decide("inbox", evidence, NOW, prefs)
    assert result == Verdict("inbox", evidence, "defer", "later_by_owner", later_until)


def test_later_stops_deferring_once_its_time_has_passed() -> None:
    prefs = Prefs(later={"inbox": NOW - timedelta(seconds=1)})
    evidence = {"count": 1, "oldest": NOW, "checked": 0, "undated": 0, "partial": False}
    result = decide("inbox", evidence, NOW, prefs)
    assert result.reason == "inbox_below_nudge"


def test_mute_outranks_a_pending_later() -> None:
    prefs = Prefs(muted=frozenset({"inbox"}), later={"inbox": NOW + timedelta(hours=1)})
    evidence = {"count": 1, "oldest": NOW, "checked": 0, "undated": 0, "partial": False}
    result = decide("inbox", evidence, NOW, prefs)
    assert result.reason == "muted_by_owner"


def test_mute_and_later_are_per_candidate() -> None:
    prefs = Prefs(muted=frozenset({"inbox"}))
    evidence = {"dirty": 1}
    result = decide("uncommitted", evidence, NOW, prefs)
    assert result.reason == "uncommitted_changes"  # unaffected by inbox's mute


# ---- decide(): per-candidate rules, every threshold boundary ---------------------------


def test_approvals_always_notifies() -> None:
    result = decide("approvals", {"native": 1, "proposals": 0}, NOW, Prefs())
    assert (result.attention, result.reason) == ("notify", "waiting_on_you")


@pytest.mark.parametrize(
    ("overdue", "today", "soon", "attention", "reason"),
    [
        (0, 0, 1, "silent", "due_soon"),
        (1, 0, 0, "notify", "due_or_overdue"),
        (0, 1, 0, "notify", "due_or_overdue"),
    ],
)
def test_tasks_due_boundary(
    overdue: int, today: int, soon: int, attention: str, reason: str
) -> None:
    evidence = {"overdue": overdue, "today": today, "soon": soon, "files": 1, "files_key": "x"}
    result = decide("tasks_due", evidence, NOW, Prefs())
    assert (result.attention, result.reason) == (attention, reason)


@pytest.mark.parametrize(
    ("working", "review", "attention", "reason"),
    [(1, 0, "silent", "runs_working"), (0, 1, "notify", "runs_to_review")],
)
def test_agent_runs_boundary(working: int, review: int, attention: str, reason: str) -> None:
    result = decide("agent_runs", {"working": working, "review": review}, NOW, Prefs())
    assert (result.attention, result.reason) == (attention, reason)


@pytest.mark.parametrize(
    ("count", "age", "attention", "reason"),
    [
        (3, timedelta(days=3), "silent", "inbox_below_nudge"),  # 3 items, exactly 3 days old
        (4, timedelta(days=3), "notify", "inbox_pressure"),  # 4 items tips it over
        (3, timedelta(days=3, seconds=1), "notify", "inbox_pressure"),  # 1s past 3 days
    ],
)
def test_inbox_pressure_boundary(count: int, age: timedelta, attention: str, reason: str) -> None:
    evidence = {"count": count, "oldest": NOW - age, "checked": 0, "undated": 0, "partial": False}
    result = decide("inbox", evidence, NOW, Prefs())
    assert (result.attention, result.reason) == (attention, reason)


def test_inbox_pressure_follows_the_daily_nudge() -> None:
    """Mirrors /daily's own nudge: more than 3 items, or the oldest over 3 days old."""
    stale = {
        "count": 1,
        "oldest": NOW - timedelta(days=3, seconds=1),
        "checked": 0,
        "undated": 0,
        "partial": False,
    }
    fresh = {"count": 3, "oldest": NOW, "checked": 0, "undated": 0, "partial": False}
    assert decide("inbox", stale, NOW, Prefs()).reason == "inbox_pressure"
    assert decide("inbox", fresh, NOW, Prefs()).reason == "inbox_below_nudge"


@pytest.mark.parametrize(
    ("count", "attention", "reason"),
    [
        (3, "silent", "inbox_below_nudge"),  # undated, at the threshold: count alone holds back
        (4, "notify", "inbox_pressure"),  # undated, over the threshold: count alone decides
    ],
)
def test_inbox_with_no_oldest_decides_by_count_alone(
    count: int, attention: str, reason: str
) -> None:
    """An undated oldest (a legal null) skips the age rule instead of raising."""
    evidence = {"count": count, "oldest": None, "checked": 0, "undated": count, "partial": False}
    result = decide("inbox", evidence, NOW, Prefs())
    assert (result.attention, result.reason) == (attention, reason)


def test_uncommitted_always_notifies() -> None:
    result = decide("uncommitted", {"dirty": 1}, NOW, Prefs())
    assert (result.attention, result.reason) == ("notify", "uncommitted_changes")


@pytest.mark.parametrize(
    ("paused", "delta", "attention", "reason"),
    [
        (True, timedelta(hours=1), "silent", "routines_paused"),
        (False, timedelta(hours=12), "notify", "routine_soon"),  # exactly 12h: soon
        (False, timedelta(hours=12, seconds=1), "silent", "routine_later"),  # 1s past 12h
    ],
)
def test_routine_boundary(paused: bool, delta: timedelta, attention: str, reason: str) -> None:
    evidence = {"routine_id": "r1", "next_run": NOW + delta, "paused": paused}
    result = decide("routine", evidence, NOW, Prefs())
    assert (result.attention, result.reason) == (attention, reason)


def test_routine_paused_outranks_an_otherwise_soon_next_run() -> None:
    evidence = {"routine_id": "r1", "next_run": NOW + timedelta(minutes=1), "paused": True}
    result = decide("routine", evidence, NOW, Prefs())
    assert result.reason == "routines_paused"


# ---- decide(): mission_report, the one candidate with no presence poll -----------------


def test_mission_report_always_notifies() -> None:
    evidence = {"mission_id": "m1"}
    result = decide("mission_report", evidence, NOW, Prefs())
    assert result == Verdict("mission_report", evidence, "notify", "mission_ended", None)


def test_mission_report_is_muted_like_any_other_candidate() -> None:
    prefs = Prefs(muted=frozenset({"mission_report"}))
    result = decide("mission_report", {"mission_id": "m1"}, NOW, prefs)
    assert (result.attention, result.reason) == ("silent", "muted_by_owner")


def test_mission_report_defers_like_any_other_candidate() -> None:
    later_until = NOW + timedelta(hours=1)
    prefs = Prefs(later={"mission_report": later_until})
    result = decide("mission_report", {"mission_id": "m1"}, NOW, prefs)
    assert (result.attention, result.reason, result.defer_until) == (
        "defer",
        "later_by_owner",
        later_until,
    )


# ---- decide(): the four self-improvement types, each unconditional once present -------


@pytest.mark.parametrize(
    ("candidate", "attention", "reason"),
    [
        ("skill_offer", "notify", "skill_hint"),
        ("skill_suggest", "notify", "skill_fits"),
        ("learn_source", "silent", "needs_decision_model"),
        ("retro_due", "notify", "labels_piled_up"),
        ("question_tune", "notify", "question_drift"),
    ],
)
def test_self_improvement_types_decide_unconditionally(
    candidate: str, attention: str, reason: str
) -> None:
    """Rules alone list skill_offer, skill_suggest, retro_due and question_tune; learn_source
    stays silent by rules alone (judging a link needs the model's own `worth_note` question)."""
    evidence = {"anything": "ignored"}
    result = decide(candidate, evidence, NOW, Prefs())
    assert result == Verdict(candidate, evidence, attention, reason, None)


@pytest.mark.parametrize(
    ("candidate", "attention", "reason"),
    [
        ("note_link", "silent", "needs_decision_model"),
        ("note_superseded", "silent", "needs_decision_model"),
        ("tasks_ticked", "notify", "tasks_ticked"),
    ],
)
def test_change_rules(candidate: str, attention: str, reason: str) -> None:
    """The paired types stay silent (they always need the Decision model's own
    should_link/superseded question, like learn_source needs worth_note); tasks_ticked needs no
    model question, so rules alone notify it, unconditionally once present."""
    evidence = {"pairs": 1, "ticked": 5}
    result = decide(candidate, evidence, NOW, Prefs())
    assert result == Verdict(candidate, evidence, attention, reason, None)


@pytest.mark.parametrize("candidate", contracts.IMPROVE)
def test_self_improvement_types_are_still_muted_and_deferred_like_any_other(
    candidate: str,
) -> None:
    prefs = Prefs(muted=frozenset({candidate}))
    assert decide(candidate, {}, NOW, prefs).reason == "muted_by_owner"
    later_until = NOW + timedelta(hours=1)
    prefs = Prefs(later={candidate: later_until})
    result = decide(candidate, {}, NOW, prefs)
    assert (result.attention, result.reason, result.defer_until) == (
        "defer",
        "later_by_owner",
        later_until,
    )


# ---- evidence_key -----------------------------------------------------------------------


def test_evidence_key_is_stable_across_key_order() -> None:
    a = {"count": 1, "oldest": NOW, "checked": 0}
    b = {"oldest": NOW, "checked": 0, "count": 1}
    assert evidence_key(a) == evidence_key(b)


def test_evidence_key_changes_with_the_values() -> None:
    assert evidence_key({"count": 1}) != evidence_key({"count": 2})


def test_evidence_key_is_a_sha256_hex_digest() -> None:
    key = evidence_key({"dirty": 1})
    assert len(key) == 64
    assert all(char in "0123456789abcdef" for char in key)


# ---- frozen dataclasses ---------------------------------------------------------------


def test_prefs_is_frozen_with_the_documented_defaults() -> None:
    prefs = Prefs()
    assert prefs.muted == frozenset()
    assert prefs.later == {}
    with pytest.raises(AttributeError):
        prefs.muted = frozenset({"inbox"})  # type: ignore[misc]


def test_verdict_is_frozen() -> None:
    verdict = Verdict("inbox", {}, "silent", "muted_by_owner", None)
    with pytest.raises(AttributeError):
        verdict.attention = "notify"  # type: ignore[misc]
