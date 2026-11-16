"""The rules policy: a deterministic `decide` for every candidate, no model call needed.

Pure: no I/O, no clock reads and no randomness. A port of the earlier engine's decision
path, with its modes, study schedule and fallback dropped — this build ships rules only.
`mission_report` is new here: it always notifies, once neither a mute nor a pending later
holds it back. The server, the bots and the client import the vocabulary here, so one name
always means one thing.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from viki_agent.decision_loop import contracts

ATTENTION = ELIGIBLE = ("silent", "defer", "notify")

REASONS = (
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
)

POLICY_VERSION = "rules-1"

INBOX_ITEMS = 3  # matches /daily's own nudge threshold
INBOX_DAYS = 3  # matches /daily's own nudge threshold
ROUTINE_SOON = timedelta(hours=12)
RETRO_LABELS = 5  # wrong or mute labels since the last retro_due that call for another one


@dataclass(frozen=True)
class Prefs:
    """The user's per-candidate mute and Later state; the rules read these before anything
    else, regardless of candidate."""

    muted: frozenset[str] = frozenset()
    later: Mapping[str, datetime] = field(default_factory=dict)


@dataclass(frozen=True)
class Verdict:
    """One candidate's rules outcome: `attention` is one of `ELIGIBLE`, `reason` one of
    `REASONS`, and `defer_until` set only when `attention` is "defer"."""

    candidate: str
    evidence: Mapping[str, object]
    attention: str
    reason: str
    defer_until: datetime | None


def present(candidate: str, evidence: Mapping[str, Any]) -> bool:
    """Whether *candidate* has anything to show, before mute and later are applied.

    `mission_report` has no rule here: a finished mission is reported through `decide`
    directly, never polled for presence.
    """
    if candidate == "approvals":
        return evidence["native"] + (evidence.get("proposals") or 0) > 0
    if candidate == "tasks_due":
        return evidence["overdue"] + evidence["today"] + evidence["soon"] > 0
    if candidate == "agent_runs":
        return evidence["working"] + evidence["review"] > 0
    if candidate == "inbox":
        return evidence["count"] > 0
    if candidate == "uncommitted":
        return evidence["dirty"] > 0
    if candidate == "routine":
        return evidence.get("routine_id") is not None
    # The per-request self-improvement types gate like the others, on their own
    # `rules_evidence` count (learn_source's "links", skill_offer's "chats",
    # skill_suggest's "score", the paired vault-change types' "pairs", tasks_ticked's "ticked").
    # `retro_due` and `question_tune` are beat-cached (never gathered per request) and, like
    # `mission_report`, have no rule here.
    if candidate == "skill_offer":
        return evidence.get("chats", 0) > 0
    if candidate == "skill_suggest":
        return evidence.get("score", 0) > 0
    if candidate == "learn_source":
        return evidence.get("links", 0) > 0
    if candidate in contracts.PAIRED:
        return evidence.get("pairs", 0) > 0
    if candidate == "tasks_ticked":
        return evidence.get("ticked", 0) > 0
    if candidate == "card_needs_you":
        return evidence.get("status") in ("blocked", "review")
    if candidate == "card_stale":
        return evidence.get("status") in ("working", "todo")
    if candidate in contracts.SEATS:
        return True  # the seat planner only builds a seat candidate that has something to show
    if candidate in contracts.LOOKS:
        return True  # the Look readers only build an item for a fact that is true right now
    if candidate in contracts.LOOK_MISSIONS:
        return True  # the code reader only builds an item for a folder that is idle right now
    if candidate in contracts.SECOND_LOOKS:
        return True  # a second look is only built for a finished chat or a waiting plan
    if candidate in contracts.PROJECT_PLANS:
        return True  # a project plan is only built for a stalled project or overdue tasks
    if candidate in contracts.MESSAGE_DRAFTS:
        return True  # a message draft is only built for a chat fact that is true right now
    if candidate in contracts.SAVED_CHECKS:
        return True  # a saved-check item is only built for a folder commit that is true right now
    if candidate in contracts.INBOX_TRIAGE:
        return True  # the triage item is only built for captures that are waiting right now
    raise ValueError(f"unknown candidate: {candidate!r}")


def decide(candidate: str, evidence: Mapping[str, Any], now: datetime, prefs: Prefs) -> Verdict:
    """The rule chain: a mute silences, then a pending later defers, then the rules decide.
    A mission report always notifies once neither holds it back."""
    if candidate not in contracts.CANDIDATES:
        raise ValueError(f"unknown candidate: {candidate!r}")
    if candidate in prefs.muted:
        return Verdict(candidate, evidence, "silent", "muted_by_owner", None)
    later_until = prefs.later.get(candidate)
    if later_until is not None and later_until > now:
        return Verdict(candidate, evidence, "defer", "later_by_owner", later_until)
    if candidate == "mission_report":
        return Verdict(candidate, evidence, "notify", "mission_ended", None)
    return _decide(candidate, evidence, now)


def _decide(candidate: str, evidence: Mapping[str, Any], now: datetime) -> Verdict:
    if candidate == "approvals":
        return Verdict(candidate, evidence, "notify", "waiting_on_you", None)
    if candidate == "tasks_due":
        if evidence["overdue"] + evidence["today"] > 0:
            return Verdict(candidate, evidence, "notify", "due_or_overdue", None)
        return Verdict(candidate, evidence, "silent", "due_soon", None)
    if candidate == "agent_runs":
        if evidence["review"] > 0:
            return Verdict(candidate, evidence, "notify", "runs_to_review", None)
        return Verdict(candidate, evidence, "silent", "runs_working", None)
    if candidate == "inbox":
        oldest = evidence["oldest"]
        stale = oldest is not None and now - oldest > timedelta(days=INBOX_DAYS)
        if evidence["count"] > INBOX_ITEMS or stale:
            return Verdict(candidate, evidence, "notify", "inbox_pressure", None)
        return Verdict(candidate, evidence, "silent", "inbox_below_nudge", None)
    if candidate == "uncommitted":
        return Verdict(candidate, evidence, "notify", "uncommitted_changes", None)
    # Five self-improvement types decide unconditionally, like `uncommitted` above: the detector
    # that created the candidate already weighed its own threshold (a link, a hint, a title
    # match, enough piled-up labels, a drifting question), so rules alone can list or quiet it.
    # Only `learn_source` is quiet: judging a link needs the model's own `worth_note` question
    # (`arbiter`'s own_question gate), which rules alone cannot ask.
    if candidate == "skill_offer":
        return Verdict(candidate, evidence, "notify", "skill_hint", None)
    if candidate == "skill_suggest":
        return Verdict(candidate, evidence, "notify", "skill_fits", None)
    if candidate == "learn_source":
        return Verdict(candidate, evidence, "silent", "needs_decision_model", None)
    if candidate == "retro_due":
        return Verdict(candidate, evidence, "notify", "labels_piled_up", None)
    if candidate == "question_tune":
        return Verdict(candidate, evidence, "notify", "question_drift", None)
    # The three vault-change types decide unconditionally too: a detected pair always needs the
    # Decision model's own `should_link`/`superseded` question (the arbiter's own_question
    # gate), so the two in `contracts.PAIRED` stay silent here, exactly like `learn_source`;
    # ticked tasks need no model question, so rules alone notify.
    if candidate in contracts.PAIRED:
        return Verdict(candidate, evidence, "silent", "needs_decision_model", None)
    if candidate == "tasks_ticked":
        return Verdict(candidate, evidence, "notify", "tasks_ticked", None)
    if candidate == "card_needs_you":
        reason = "card_to_review" if evidence.get("status") == "review" else "card_needs_decision"
        return Verdict(candidate, evidence, "notify", reason, None)
    if candidate == "card_stale":
        return Verdict(candidate, evidence, "notify", "card_untouched", None)
    if candidate in contracts.SEATS:
        return Verdict(candidate, evidence, "notify", _SEAT_REASONS[candidate], None)
    if candidate in contracts.LOOKS:
        return Verdict(candidate, evidence, "notify", _LOOK_REASONS[candidate], None)
    if candidate in contracts.LOOK_MISSIONS:
        return Verdict(candidate, evidence, "notify", _LOOK_MISSION_REASONS[candidate], None)
    if candidate in contracts.SECOND_LOOKS:
        # A finding carries `found` in its rules evidence; an offer carries only its trigger.
        reason = "finding_ready" if "found" in evidence else "work_finished"
        return Verdict(candidate, evidence, "notify", reason, None)
    if candidate in contracts.PROJECT_PLANS:
        # A finding carries `found`; an offer is told apart by its type.
        if "found" in evidence:
            return Verdict(candidate, evidence, "notify", "plan_ready", None)
        reason = "project_stalled" if candidate == "stalled_project" else "tasks_overdue"
        return Verdict(candidate, evidence, "notify", reason, None)
    if candidate in contracts.MESSAGE_DRAFTS:
        reason = "context_nearly_full" if candidate == "context_handoff" else "pages_to_note"
        return Verdict(candidate, evidence, "notify", reason, None)
    if candidate in contracts.SAVED_CHECKS:
        return Verdict(candidate, evidence, "notify", _CHECK_REASONS[candidate], None)
    if candidate in contracts.INBOX_TRIAGE:
        return Verdict(candidate, evidence, "notify", _TRIAGE_REASONS[candidate], None)
    # candidate == "routine"
    if evidence["paused"]:
        return Verdict(candidate, evidence, "silent", "routines_paused", None)
    if evidence["next_run"] - now <= ROUTINE_SOON:
        return Verdict(candidate, evidence, "notify", "routine_soon", None)
    return Verdict(candidate, evidence, "silent", "routine_later", None)


_SEAT_REASONS = {
    "seat_waiting": "seat_waiting",
    "seat_working": "seat_working",
    "seat_start": "seat_could_start",
    "seat_report": "seat_report",
}

_LOOK_REASONS = {
    "vault_recovery": "recovery_needs_you",
    "memory_drift": "memory_source_changed",
    "bridge_stopped": "bridge_stopped",
    "chat_waiting": "chat_waiting",
    "send_uncertain": "send_uncertain",
    "mcp_context": "mcp_context_heavy",
}

_LOOK_MISSION_REASONS = {"code_look": "code_changes_waiting"}

_CHECK_REASONS = {"commit_checks": "checks_not_run"}

_TRIAGE_REASONS = {"inbox_triage": "inbox_unsorted"}


def evidence_key(evidence: Mapping[str, object]) -> str:
    """The sha256 hex of *evidence*'s canonical JSON: stable key order, stamps for datetimes."""
    canonical = json.dumps(
        evidence,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
        default=_json_default,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _json_default(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"not JSON-serializable: {type(value).__name__}")
