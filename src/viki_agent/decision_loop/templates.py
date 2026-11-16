"""Word the items from templates: turn one candidate, its outcome and the
playbook's answers into the `ItemBody` a user reads — title, what, why, evidence and actions.

Pure: no I/O, no clock reads, no randomness. `render` never writes a number: quantities and
timing come only from `candidate.words`' pre-worded phrases (detectors must keep those free of
digits and ids: this module never formats numbers) and from this module's own fixed
vocabulary. The digit rule is about numbers this module or a model would produce, not the
user's own names: `title` (copied from `candidate.title` verbatim) and an evidence label built
from a ref's file name (e.g. "Second Brain MOC", "2026-10-21") may both carry digits, since
both are the user's own wording, not something generated here.

Contract this module reads from `Candidate.facts`/`Candidate.words` (owned here; the detectors
fill it): `facts["workspace_id"]` and, for type "routine",
`facts["routine_id"]`, both plain strings for the note/routine an action opens; `facts["overdue"]`
(tasks_due), `facts["review"]`/`facts["working"]` (agent_runs), `facts["partial"]` (inbox),
`facts["paused"]` (routine) and `facts["state"]`/`facts["reason"]` (mission_report: `state` one
of `MISSION_STATES`, `reason` the mission's own end or ready reason) are booleans/strings that
pick which fixed sentence applies; a mission_report's `reason` "ready" (a prepare mission's
first waiting plan card, still "running") always wins over `state`, and so does "nothing" (a
second look that found nothing, naming the chat from `facts["chat"]`, "your chat" when blank);
a "stopped" `state` also reads `reason` ("deadline" or "owner") to tell the user's own Stop from
the mission's deadline cutting it off. `words["due"]` (tasks_due's own
timing, e.g. "today"/"tomorrow"), `words["age"]` (inbox's oldest item, e.g. "a few days old") and
`words["next_run"]` (routine's next run, e.g. "soon") feed a type's `what`; `words["deadline"]`
(e.g. "tomorrow"), paired with `candidate.deadline` being set, feeds the universal "due {word}"
reason in `why` for any type. Every key is read with `.get` and a safe fallback, so a candidate
missing one never raises.

The two Kanban card types (`contracts.CARDS`) read `facts["status"]` (`blocked`, `review`,
`working` or `todo`), `facts["seat"]` (the rest of the card's first `owner/` tag, if any) and
`words["age"]` (one of `words.age_words`) for `what` and `why`; a seat, like `title`, is the
user's word and may hold a digit, which only `why` carries. Their evidence chip reads the card's
`title`, and their one action opens the card (`candidate.related_ref`) as "Open card".

The four routine-seat types (`contracts.SEATS`) read `facts["seat"]` (the seat name, if any,
read with its first letter in upper case), `facts["question"]` (seat_waiting),
`facts["handoff"]` (seat_start), `facts["reason"]` (seat_report), `facts["session_id"]` and
`facts["routine_id"]`. The seat is in `what` only; `why` is a fixed sentence per case, on the
rules and the model path alike, and a seat, like `title`, is the user's word that may hold a
digit. Their actions come from `_seat_actions`, not `_base_action`.

Four of the self-improvement types (owned here too) read the same contract:
`facts["trigger"]` (one of `SKILL_TRIGGERS`) and `facts["sessions"]` (the full session id tuple,
the chat in view first) pick skill_offer's `what` and evidence chips; its `evidence` holds the
*other* chats only, each `Evidence(None, title, False, ())` with the title in `excerpt` and
aligned with `facts["sessions"][1:]`, shown as chat chips (a `session_id`, never a note ref).
learn_source's `evidence` holds the captures themselves (each with a `ref`), read for their refs
alone, never `excerpt`. question_tune reads `facts["name"]` (one of `playbook.TUNABLE`) and
`facts["wording_json"]` (that question's live wording, already `canonical_json`). Every draft
(`ActionSpec.text`) is built only from these refs, names and the wording, never an
excerpt; `text` may hold newlines and the user's own ids, unlike the rendered
fields `_field` flattens. skill_suggest reads only `facts["session"]` and `facts["skill"]`: the
name goes into its one chip and its draft, never `title`, `what` or `why`; missing a
name, it renders neither.

The six Look types (`contracts.LOOKS`) read `facts["workspace_id"]` and, per type, `facts["reason"]`
(vault_recovery: blocked, stopped, expiring or full), `facts["count"]` (memory_drift: above one
reads plural), `facts["provider"]` and `facts["restart"]` (bridge_stopped), `facts["kind"]`
(chat_waiting: permission, plan or question) with `words["wait"]` (one of `words.wait_words`,
fallback "a while"), `facts["session_id"]` (the three chat types) and `facts["server"]`
(mcp_context). Their `what` and `why` are fixed sentences, the same on the rules and the model
path; a part whose fact is missing or unknown reads its type's rule words. A server name, like
`title`, is the user's word and may hold a digit, which only `what` carries. Their one action
opens the vault, Messaging or the chat.

The Look mission type (`contracts.LOOK_MISSIONS`, `code_look`) reads `facts["folder"]` (the code
folder's name) and `facts["folder_id"]` (the workspace its one base action opens in Code). The
folder name, like `title`, is the user's word and may hold a digit, which only `what` carries;
`why` is one fixed sentence, on the rules and the model path alike. When the cycle offers its
Prepare under Ask first (`grants={"prepare": <Look grant>}`), the `start_prepare` action reads
"Take a look" instead of "Prepare a plan".

The two second-look types (`contracts.SECOND_LOOKS`) read `facts["session"]` (the chat their one
base action opens) and, for an offer, `facts["refs"]` with `facts["workspace_id"]` (one note chip
a ref, labelled with its stem, at most `MAX_SECOND_LOOK_REFS`). `facts["found"]` makes the item
a finding instead: `what` words the kind (`contracts.FINDING_KINDS`, else "something worth a
look") and names the chat from `facts["chat"]` ("your chat" when blank), and the base action
reads "Open plan". The chat's title, like `title`, is the user's word and may hold a digit,
which only `what` carries; `why` is one fixed sentence per kind of item, on the rules and the
model path alike. When the cycle offers an offer's Prepare under Ask first, the `start_prepare`
action reads "Take a second look".

The two project-plan types (`contracts.PROJECT_PLANS`) read `facts["project"]` (the project's
name), `facts["ref"]` with `facts["workspace_id"]` (the note the base action opens and the one
note chip, labelled with the project, else the ref's stem) and, for a finding, `facts["session"]`
(the chat its base action opens, "Open plan") and `facts["found"]` (which makes the item a
finding). The project name, like `title`, is the user's word and may hold a digit, which only
`what` and the chip carry; a blank name reads the sentence without it. `why` is one fixed sentence
per type of item, on the rules and the model path alike. When the cycle offers an offer's Prepare
under Ask first, the `start_prepare` action reads "Plan next steps" (stalled_project) or "Check
these tasks" (overdue_tasks).

The two message-draft types (`contracts.MESSAGE_DRAFTS`) read `facts["session"]` (the chat their
one action, `draft_chat`, fills) and, for source_note, `facts["notes"]` (true when that chat's last
reply read a vault note). `what`, `why` and the drafted text are fixed sentences, the same on the
rules and the model path, with no digit and nothing from the chat; only a source_note whose
`notes` is not true says its reply read no note and adds the draft's search-first sentence.

The saved-check type (`contracts.SAVED_CHECKS`, `commit_checks`) reads `facts["folder"]` (the code
folder's name), `facts["folder_id"]` (the workspace its Open in Code opens) and `facts["phase"]`
(one of `CHECK_PHASES`; an unknown phase reads "waiting"). The folder name, like `title`, is the
user's word and may hold a digit, which only `what` carries; no check name is read. `why` is a
fixed sentence per phase, on the rules and the model path alike. Its actions come from
`_check_actions`, not `_base_action` or `_work_actions`.

The inbox-triage type (`contracts.INBOX_TRIAGE`, `inbox_triage`) reads only `facts["phase"]` (one
of `TRIAGE_PHASES`; an unknown phase reads "waiting"): no ref, count or name. `what` and `why` are
fixed sentences per phase, the same on the rules and the model path. Its actions come from
`_triage_actions`, not `_base_action` or `_work_actions`.

`why` follows two different registers on purpose: the rules path quotes a `rules.REASONS` text
verbatim (a full sentence, since that is what `reasonText` in `wording.ts` already reads like),
while the model path joins up to three lower-case fragments with "; ". Five of the `reasonText`
strings this module would otherwise copy verbatim name a number (the due-hour thresholds, or
counts from the old inbox/routine thresholds); since no field but `title` may carry a digit,
those five are reworded here to the same meaning without one, and none of the fifteen needed
the earlier engine's name and "decision engine" were reworded (none said either term).

`call_id` is not in the brief's own signature; it is accepted as an optional keyword so the
engine can add the "see why" action once it has recorded the model call that produced
`answers` — a rules-only decision has no call to link, so it is left unset.

`hold` is `render`'s other optional keyword, one of `cycle.HOLD_CODES` or None: it names why
`cycle.held_start` (or `_commit`'s own "since" case, for an inbox suggestion stored before the
engine began) predicts VIKI's own start of this item held back this cycle, ahead of the
engine's real gate. Any hold widens `why` with this module's own `HOLD_WHY` sentence (the two
code sets are checked equal by an assertion in `cycle.py`); "limit" alone also drops the start
action `_work_actions` would otherwise add on an "ask" or "allowed" grant — an item with a
mission already active still shows its open/stop actions, since those are decided first and
never consult `hold`.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping
from dataclasses import replace
from pathlib import PurePosixPath

from viki_agent.decision_loop import contracts, playbook, rules

_FIELD_LIMIT = 300
_EVIDENCE_LABEL_LIMIT = 120
# The Unicode bidi controls (marks, embeddings, overrides and isolates). A synced card's title or
# tag carries them into a sentence, where they reorder the text around them as it is shown.
_BIDI_CONTROLS = frozenset(
    "\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"
)

# A mission still going: it replaces a start_prepare/start_run offer with a chat to open and a
# stop button, regardless of `grants`. The other `MISSION_STATES` have ended.
_ACTIVE_MISSION_STATES = frozenset({"starting", "running"})

_OUTSIDE_EVIDENCE_LABEL = "A source outside the vault"
_VAULT_EVIDENCE_LABEL = "A note in your vault"
_BLANK_CHAT_LABEL = "A chat"

_RULE_REASON_FALLBACK = "VIKI's rules flagged this."
_MODEL_REASON_FALLBACK = "The Decision model found this worth mentioning."

_MISSION_REPORT_DEFAULT_WHAT = "A chat VIKI started has finished."
_MISSION_REPORT_READY_WHAT = "A plan from a chat VIKI started is ready for you."
_MISSION_REPORT_WHAT: Mapping[str, str] = {
    "done": "A chat VIKI started has finished.",
    "failed": "A chat VIKI started stopped with an error.",
    "unknown": "A chat VIKI started closed.",
}
# A "stopped" mission needs its own `facts["reason"]` too: the user's own Stop reads
# differently from the mission's own deadline cutting it off (missions.py's "owner"/"deadline").
_MISSION_REPORT_STOPPED_WHAT: Mapping[str, str] = {
    "deadline": "A chat VIKI started reached its time limit.",
    "owner": "You stopped a chat VIKI started.",
}

# Copied from the earlier wording module's `reasonText`, verbatim where it named no number;
# reworded (still the same meaning) where it did, since only `title` may carry a digit here.
# `mission_ended` has no entry in `wording.ts` (new to the Decision loop), so its text is this
# module's own.
RULE_REASON_WORDS: Mapping[str, str] = {
    "muted_by_owner": "You muted this topic.",
    "later_by_owner": "You chose Later.",
    "show_all": "VIKI is showing every item right now.",
    "waiting_on_you": "Something is waiting for your answer.",
    "due_or_overdue": "A task is due today or overdue.",
    "due_soon": "Tasks are due soon, none today.",
    "runs_to_review": "An agent run finished and is ready to review.",
    "runs_working": "Agents are still working; nothing to review yet.",
    "inbox_pressure": "The inbox has grown large or has an old item.",
    "inbox_below_nudge": "The inbox is small and recent.",
    "uncommitted_changes": "The vault has uncommitted changes.",
    "routines_paused": "Routines are paused.",
    "routine_soon": "A routine runs soon.",
    "routine_later": "The next routine is not due soon.",
    "mission_ended": "A chat VIKI started has finished.",
    "skill_hint": "This work may be worth keeping as a skill.",
    "needs_decision_model": "Only the Decision model offers this.",
    "labels_piled_up": "Several items were marked Not useful lately.",
    "question_drift": "A question's answers stopped matching yours.",
    "tasks_ticked": "Some tasks are ticked and could be archived.",
    "skill_fits": "This chat's title matches one of your own skills.",
    "card_needs_decision": "A card on Kanban needs your decision.",
    "card_to_review": "A card on Kanban is ready for your review.",
    "card_untouched": "A card on Kanban has stopped moving.",
    "seat_waiting": "A Bot run is waiting for you.",
    "seat_working": "A Bot is working on a card.",
    "seat_could_start": "A Bot could start on a waiting card.",
    "seat_report": "A Bot run needs a look.",
    "recovery_needs_you": "Something in the vault's Recovery needs a look.",
    "memory_source_changed": "A memory's source note changed after you approved it.",
    "bridge_stopped": "A messaging bridge stopped.",
    "chat_waiting": "A chat is waiting for your answer.",
    "send_uncertain": "A message may not have reached Claude Code.",
    "mcp_context_heavy": "An MCP server fills a large part of a chat's context.",
    "code_changes_waiting": "A code folder's changes have waited a day or more.",
    "work_finished": "Work just finished in a chat you have open.",
    "finding_ready": "A second look found something, and its plan waits for you.",
    "project_stalled": "An active project has had no change for two weeks or more.",
    "tasks_overdue": "Some tasks are overdue and may be done.",
    "plan_ready": "A plan VIKI prepared waits for your approval.",
    "context_nearly_full": "A chat you have open is nearly full.",
    "pages_to_note": "A reply in a chat you have open fetched web pages.",
    "checks_not_run": "A code folder's new commit has saved checks that have not run on it.",
    "inbox_unsorted": "The inbox has captures with no suggested destination.",
}
assert set(RULE_REASON_WORDS) == set(rules.REASONS)

# Exactly `contracts.ACTIONS`'s own words, one label each.
ACTION_LABELS: Mapping[str, str] = {
    "open_chat": "Open chat",
    "open_note": "Open note",
    "open_approvals": "Review approvals",
    "open_inbox": "Open inbox",
    "open_routine": "Open routine",
    "open_runs": "Open runs",
    "open_context": "See why",
    "open_vault": "Open vault",
    "open_messaging": "Open Messaging",
    "open_code": "Open in Code",
    "open_triage": "Triage inbox",
    "start_prepare": "Prepare a plan",
    "start_run": "Start a chat to do it",
    "stop_mission": "Stop",
    "start_seat": "Start",
    "stop_seat": "Stop",
    "run_checks": "Run checks",
    "stop_checks": "Stop",
    "suggest_inbox": "Suggest destinations",
    "draft_chat": "Draft it in this chat",
    "draft_new": "Draft it in a new chat",
}
assert set(ACTION_LABELS) == set(contracts.ACTIONS)

# `render`'s `hold` keyword, one sentence appended to `why` per code; see `cycle.held_start` and
# the module docstring. Checked against `cycle.HOLD_CODES` by an assertion in `cycle.py`.
HOLD_WHY: Mapping[str, str] = {
    "limit": (
        "Today's limit for these is reached, counting the ones you started; you can raise it "
        "in What VIKI may do."
    ),
    "brake": (
        "On hold under Pause automatic starts at Claude plan usage. You can start it yourself."
    ),
    "quiet": "On hold for your quiet hours. You can start it yourself.",
    "busy": "On hold while VIKI's other chat is open. You can start it yourself.",
    "later": "On hold for a better moment. You can start it yourself.",
    "since": "VIKI won't start this run itself after a restart. You can start it yourself.",
}


def render(
    candidate: contracts.Candidate,
    outcome: contracts.Outcome,
    answers: contracts.Answers,
    *,
    mission: contracts.MissionRecord | None,
    grants: Mapping[str, str],
    call_id: str | None = None,
    hold: str | None = None,
) -> contracts.ItemBody:
    """One item's user-facing body for `candidate` and the decision `outcome` reached for it.

    `answers` is the playbook's reply map; when it is empty or `outcome.reason` starts
    "rules:", the rules decided alone and `why` quotes the matching `rules.REASONS` text
    instead of reading `answers`. `mission` is a mission already running for this exact item.
    `grants` is `{"prepare": ..., "run": ...}` (each one of `contracts.GRANTS`; a missing key
    reads as "off"), from the settings the engine is honouring. `call_id` is the model call
    `answers` came from, when there was one; passing it adds a "see why" action. `hold` is one
    of `cycle.HOLD_CODES` or None; see the module docstring.
    """
    why = _why(candidate, outcome, answers)
    if hold is not None:
        why = f"{why} {HOLD_WHY[hold]}"
    return contracts.ItemBody(
        title=_field(candidate.title),
        what=_field(_what(candidate)),
        why=_field(why),
        evidence=_evidence(candidate),
        actions=_actions(candidate, outcome, mission, grants, call_id, hold),
    )


# ---- what ---------------------------------------------------------------------------------------


def _what(candidate: contracts.Candidate) -> str:
    facts = candidate.facts
    words = candidate.words
    candidate_type = candidate.type
    if candidate_type == "approvals":
        return "Something is waiting for your approval."
    if candidate_type == "tasks_due":
        if facts.get("overdue"):
            return "A task is overdue."
        due = words.get("due")
        return f"A task is due {due}." if due else "A task is due soon."
    if candidate_type == "agent_runs":
        if facts.get("review"):
            return "An agent run is ready for you to review."
        if facts.get("working"):
            return "An agent run is still working."
        return "An agent run needs attention."
    if candidate_type == "inbox":
        text = "There are items waiting in the inbox"
        age = words.get("age")
        if age:
            text += f", the oldest {age}"
        if facts.get("partial"):
            text += " (partly read)"
        return text + "."
    if candidate_type == "uncommitted":
        return "The vault has uncommitted changes."
    if candidate_type == "routine":
        if facts.get("paused"):
            return "Routines are paused."
        next_run = words.get("next_run")
        return f"A routine runs {next_run}." if next_run else "A routine runs soon."
    if candidate_type == "mission_report":
        return _mission_report_what(facts)
    if candidate_type == "skill_offer":
        trigger = facts.get("trigger")
        if isinstance(trigger, str) and trigger in _SKILL_OFFER_WHAT:
            return _SKILL_OFFER_WHAT[trigger]
        return RULE_REASON_WORDS["skill_hint"]
    if candidate_type == "skill_suggest":
        return _SKILL_SUGGEST_WHAT
    if candidate_type == "learn_source":
        plural = len(candidate.evidence) > 1
        return _LEARN_SOURCE_WHAT_PLURAL if plural else _LEARN_SOURCE_WHAT
    if candidate_type == "retro_due":
        return _RETRO_DUE_WHAT
    if candidate_type == "question_tune":
        name = facts.get("name")
        label = playbook.LABELS.get(name, name) if isinstance(name, str) else ""
        return f"One of Decision's questions isn't matching your answers: {label}"
    if candidate_type == "note_link":
        return "Two notes may belong together."
    if candidate_type == "note_superseded":
        return "A newer note may replace an older one."
    if candidate_type == "tasks_ticked":
        return "Ticked tasks are waiting to be archived."
    if candidate_type == "card_needs_you":
        return _CARD_REVIEW_WHAT if facts.get("status") == "review" else _CARD_DECISION_WHAT
    if candidate_type == "card_stale":
        return _CARD_STALE_WHAT
    if candidate_type in contracts.SEATS:
        return _seat_what(candidate)
    if candidate_type in contracts.LOOKS:
        return (
            _look_sentences(candidate)[0] or RULE_REASON_WORDS[rules._LOOK_REASONS[candidate_type]]
        )
    if candidate_type in contracts.LOOK_MISSIONS:
        folder = _str_or_none(facts.get("folder"))
        return (
            f"{folder} has uncommitted changes, untouched for a day or more."
            if folder
            else "A code folder has uncommitted changes, untouched for a day or more."
        )
    if candidate_type in contracts.SECOND_LOOKS:
        if "found" in facts:
            return _finding_what(facts)
        return _SECOND_LOOK_WHAT[candidate_type]
    if candidate_type in contracts.PROJECT_PLANS:
        return _plan_what(candidate_type, facts)
    if candidate_type in contracts.MESSAGE_DRAFTS:
        return _message_draft_what(candidate_type, facts)
    if candidate_type in contracts.SAVED_CHECKS:
        return _check_what(facts)
    if candidate_type in contracts.INBOX_TRIAGE:
        return _TRIAGE_WHAT[_triage_phase(candidate)]
    raise ValueError(f"unknown candidate type: {candidate_type!r}")


# skill_offer's `_what`, keyed by `facts["trigger"]` (one of `SKILL_TRIGGERS`); a trigger this
# module does not recognise falls back to `RULE_REASON_WORDS["skill_hint"]` (same meaning).
_SKILL_OFFER_WHAT: Mapping[str, str] = {
    "correction": "You corrected VIKI in this chat; a skill could keep the fix.",
    "repeated_request": "You keep starting chats like this one; a skill could save repeating it.",
    "long_session": "This chat ran a long task to the end; it may be worth keeping as a skill.",
}
_SECOND_LOOK_WHAT: Mapping[str, str] = {
    "long_task_look": "A long task just finished in this chat.",
    "correction_look": "Work you corrected just finished in this chat.",
}
_FINDING_KIND_WORDS: Mapping[str, str] = {
    "flaw": "a possible flaw",
    "approach": "a possible better approach",
    "missing": "a possible missing piece",
}


def _finding_what(facts: Mapping[str, object]) -> str:
    kind = facts.get("found")
    found = _FINDING_KIND_WORDS.get(kind) if isinstance(kind, str) else None
    chat = _str_or_none(facts.get("chat"))
    where = f'"{chat}"' if chat else "your chat"
    return f"VIKI found {found or 'something worth a look'} in {where}."


# A project plan's `what`: (named, blank) per type; a finding reads `_PLAN_FINDING_WHAT`.
_PLAN_WHAT: Mapping[str, tuple[str, str]] = {
    "stalled_project": (
        'Nothing in the project "{}" has changed for two weeks or more.',
        "Nothing in an active project has changed for two weeks or more.",
    ),
    "overdue_tasks": (
        'Overdue tasks in "{}" may be done or need a new date.',
        "Overdue tasks may be done or need a new date.",
    ),
}
_PLAN_FINDING_WHAT = (
    'VIKI\'s plan for "{}" waits for your approval.',
    "VIKI's plan for your project waits for your approval.",
)


def _project_name(facts: Mapping[str, object]) -> str | None:
    name = facts.get("project")
    return (name.strip() or None) if isinstance(name, str) else None


def _plan_what(candidate_type: str, facts: Mapping[str, object]) -> str:
    named, blank = _PLAN_FINDING_WHAT if "found" in facts else _PLAN_WHAT[candidate_type]
    project = _project_name(facts)
    return named.format(project) if project else blank


_HANDOFF_WHAT = "This chat's context is nearly full."
_SOURCE_NOTE_WHAT = "Your last reply in this chat fetched web pages."
_SOURCE_NOTE_NO_NOTE_WHAT = (
    "Your last reply in this chat fetched web pages and read no note in your vault."
)


def _message_draft_what(candidate_type: str, facts: Mapping[str, object]) -> str:
    if candidate_type == "context_handoff":
        return _HANDOFF_WHAT
    return _SOURCE_NOTE_WHAT if facts.get("notes") is True else _SOURCE_NOTE_NO_NOTE_WHAT


# Per phase: the sentence with the folder's name, then the one without it.
_CHECK_WHAT: Mapping[str, tuple[str, str]] = {
    "waiting": (
        "{} moved to a new commit that its saved checks have not run on.",
        "A code folder moved to a new commit that its saved checks have not run on.",
    ),
    "running": (
        "{}'s saved checks are running on its new commit.",
        "A code folder's saved checks are running on its new commit.",
    ),
    "passed": (
        "{}'s saved checks passed on its new commit.",
        "A code folder's saved checks passed on its new commit.",
    ),
    "failed": (
        "A saved check failed on {}'s new commit.",
        "A saved check failed on a code folder's new commit.",
    ),
    "stopped": (
        "{}'s saved checks did not finish on its new commit.",
        "A code folder's saved checks did not finish on its new commit.",
    ),
}


def _check_what(facts: Mapping[str, object]) -> str:
    named, nameless = _CHECK_WHAT.get(str(facts.get("phase")), _CHECK_WHAT["waiting"])
    folder = _str_or_none(facts.get("folder"))
    return named.format(folder) if folder else nameless


_TRIAGE_WHAT: Mapping[str, str] = {
    "waiting": "Your inbox has captures with no suggested destination.",
    "running": "VIKI is suggesting destinations for your inbox captures.",
    "ready": "Suggested destinations for your inbox captures are waiting.",
    "failed": "VIKI could not suggest destinations for your inbox captures.",
    "nothing": "VIKI found no destination it could suggest for your inbox captures.",
}

_CARD_REVIEW_WHAT = "A card is ready for your review."
_CARD_DECISION_WHAT = "A card needs your decision."
_CARD_STALE_WHAT = "A card has stopped moving."
_SKILL_SUGGEST_WHAT = "One of your own skills may help with this chat."
_LEARN_SOURCE_WHAT = "A link you captured may be worth a literature note."
_LEARN_SOURCE_WHAT_PLURAL = "Links you captured may be worth literature notes."
_RETRO_DUE_WHAT = (
    "You marked several items Not useful lately; a retro could turn them into a lesson."
)


def _seat_name(candidate: contracts.Candidate) -> str | None:
    """`facts["seat"]` trimmed with its first letter in upper case, None when blank."""
    seat = _without_bidi(_str_or_none(candidate.facts.get("seat")) or "").strip()
    return f"{seat[0].upper()}{seat[1:]}" if seat else None


# seat_report's `what` and `why`, keyed by `facts["reason"]`: ({Seat} sentence, why). The first
# five are how a run ended, the rest why VIKI holds a start back (`_SEAT_HOLDS`).
_SEAT_REPORTS: Mapping[str, tuple[str, str]] = {
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
_SEAT_HOLDS = frozenset({"limit", "plan_brake", "quiet", "untested", "two_routines"})
# Of `_SEAT_HOLDS`, the two the user can still start themselves: the plan brake and quiet hours
# hold only VIKI's own start, same as `HOLD_WHY`'s "brake" and "quiet" for every other type.
_SEAT_START_HOLDS = frozenset({"plan_brake", "quiet"})
_SEAT_WAITING_WHY = "Its run waits until you answer or it reaches its time limit."
_SEAT_WORKING_WHY = "VIKI started it for a card waiting for its seat."
_SEAT_START_WHY = "Start Bots' work is set to Ask first."
_SEAT_HANDOFF_WHY = "A Bot wrote this card, and seats may not hand work to each other."
_SEAT_OPEN_LABEL = "Open session"


def _seat_what(candidate: contracts.Candidate) -> str:
    facts = candidate.facts
    seat = _seat_name(candidate)
    if candidate.type == "seat_waiting":
        who = seat or "A routine"
        return (
            f"{who} has a question for you."
            if facts.get("question")
            else (f"{who} needs your OK to continue.")
        )
    if candidate.type == "seat_working":
        return f"{seat or 'A Bot'} is working on a card."
    if candidate.type == "seat_start":
        return f"{seat or 'A Bot'} could start on a waiting card."
    reason = facts.get("reason")
    report = _SEAT_REPORTS.get(reason) if isinstance(reason, str) else None
    if report is None:
        return RULE_REASON_WORDS["seat_report"]
    return report[0].format(seat or "A Bot")


def _seat_why(candidate: contracts.Candidate) -> str | None:
    """A seat item's why (the same sentence on both paths); None for a seat_report whose
    `facts["reason"]` is not one of `_SEAT_REPORTS`, which then reads its type's rule words."""
    if candidate.type == "seat_waiting":
        return _SEAT_WAITING_WHY
    if candidate.type == "seat_working":
        return _SEAT_WORKING_WHY
    if candidate.type == "seat_start":
        return _SEAT_HANDOFF_WHY if candidate.facts.get("handoff") else _SEAT_START_WHY
    reason = candidate.facts.get("reason")
    report = _SEAT_REPORTS.get(reason) if isinstance(reason, str) else None
    return report[1] if report else None


# vault_recovery's (what, why) by `facts["reason"]`; any other reason reads the rule words.
_RECOVERY_SENTENCES: Mapping[str, tuple[str, str]] = {
    "blocked": (
        "Saves to this vault are stopped.",
        "A note operation didn't finish. Open Recovery in the vault view to clear it.",
    ),
    "stopped": (
        "A save to this vault stopped partway through.",
        "Open Recovery in the vault view before you save that note again.",
    ),
    "expiring": (
        "A deleted note will soon be gone for good.",
        "Restore it from Recovery in the vault view before it expires.",
    ),
    "full": (
        "The storage for deleted notes, shared by your vaults, is nearly full.",
        "Once it is full, deleting a note fails in every vault. Restore the notes you need from "
        "Recovery in the vault that holds them.",
    ),
}
_MEMORY_DRIFT_WHY = (
    "Open Memory in the vault view, and Correct or Retire any memory that no longer holds."
)
_BRIDGE_NAMES: Mapping[str, str] = {
    "mattermost": "Mattermost",
    "slack": "Slack",
    "discord": "Discord",
}
_CHAT_WAITING_WHAT: Mapping[str, str] = {
    "permission": "A chat has waited {} for your OK.",
    "plan": "A plan has waited {} for your approval.",
    "question": "A question has waited {} for your answer.",
}
_CHAT_WAITING_WHY = "Claude Code pauses that chat until you answer."
_SEND_UNCERTAIN_SENTENCES = (
    "A message you sent may not have reached Claude Code.",
    "Check the chat before you send it again; Drafts and send history keeps its text.",
)
_MCP_CONTEXT_WHY = (
    "Its loaded tools take a tenth or more of that chat's context. Turn it off with the "
    "chat's MCP servers button if the chat doesn't need it."
)


def _look_sentences(candidate: contracts.Candidate) -> tuple[str | None, str | None]:
    """A Look item's (what, why), the same on the rules and the model path; None for a part
    whose fact is unknown, which then reads its reason's rule words."""
    facts = candidate.facts
    candidate_type = candidate.type
    if candidate_type == "vault_recovery":
        reason = facts.get("reason")
        return (
            _RECOVERY_SENTENCES.get(reason, (None, None))
            if isinstance(reason, str)
            else (None, None)
        )
    if candidate_type == "memory_drift":
        count = facts.get("count")
        many = isinstance(count, int) and count > 1
        what = (
            "Some memories cite notes that changed after you approved them."
            if many
            else "A memory cites a note that changed after you approved it."
        )
        return what, _MEMORY_DRIFT_WHY
    if candidate_type == "bridge_stopped":
        provider = facts.get("provider")
        name = _BRIDGE_NAMES.get(provider) if isinstance(provider, str) else None
        what = f"The {name} bridge stopped." if name else None
        return what, (
            "Restart VIKI Agent to use this bot again."
            if facts.get("restart")
            else "Turn it on again in Messaging."
        )
    if candidate_type == "chat_waiting":
        kind = facts.get("kind")
        sentence = _CHAT_WAITING_WHAT.get(kind) if isinstance(kind, str) else None
        if sentence is None:
            return None, None
        return sentence.format(candidate.words.get("wait") or "a while"), _CHAT_WAITING_WHY
    if candidate_type == "send_uncertain":
        return _SEND_UNCERTAIN_SENTENCES
    server = _str_or_none(facts.get("server"))  # mcp_context
    what = f"The {server} MCP server fills a large part of a chat's context." if server else None
    return what, _MCP_CONTEXT_WHY


def _mission_report_what(facts: Mapping[str, object]) -> str:
    """`facts["reason"]` of "ready" always wins, whatever `facts["state"]` says, since that
    report's mission is still "running"; "nothing" also wins, naming the chat (`facts["chat"]`,
    "your chat" when blank), since a second look that found nothing ends quietly whatever
    `state` it stored; otherwise `facts["state"]` picks the sentence, "stopped" also reading
    `facts["reason"]` ("deadline" or "owner")."""
    reason = facts.get("reason")
    reason_key = reason if isinstance(reason, str) else ""
    if reason_key == "ready":
        return _MISSION_REPORT_READY_WHAT
    if reason_key == "nothing":
        chat = _str_or_none(facts.get("chat"))
        where = f'"{chat}"' if chat else "your chat"
        return f"Second look at {where}: nothing worth your time."
    state = facts.get("state")
    state_key = state if isinstance(state, str) else ""
    if state_key == "stopped":
        return _MISSION_REPORT_STOPPED_WHAT.get(reason_key, _MISSION_REPORT_DEFAULT_WHAT)
    return _MISSION_REPORT_WHAT.get(state_key, _MISSION_REPORT_DEFAULT_WHAT)


# ---- why ----------------------------------------------------------------------------------------


def _why(
    candidate: contracts.Candidate, outcome: contracts.Outcome, answers: contracts.Answers
) -> str:
    if candidate.type in contracts.CARDS or candidate.type in contracts.SEATS:
        # A card's or seat's why is its own sentence on the rules and the model path alike.
        sentence = (
            _card_why(candidate) if candidate.type in contracts.CARDS else _seat_why(candidate)
        )
        if sentence:
            return sentence
    if candidate.type in contracts.LOOKS:
        sentence = _look_sentences(candidate)[1]
        if sentence:
            return sentence
    if candidate.type in contracts.LOOK_MISSIONS:
        return _CODE_LOOK_WHY
    if candidate.type in contracts.SECOND_LOOKS:
        return _FINDING_WHY if "found" in candidate.facts else _SECOND_LOOK_WHY
    if candidate.type in contracts.PROJECT_PLANS:
        return _FINDING_WHY if "found" in candidate.facts else _PLAN_WHY[candidate.type]
    if candidate.type in contracts.MESSAGE_DRAFTS:
        return _MESSAGE_DRAFT_WHY[candidate.type]
    if candidate.type in contracts.SAVED_CHECKS:
        return _CHECK_WHY_WAITING if _check_phase(candidate) == "waiting" else _CHECK_WHY
    if candidate.type in contracts.INBOX_TRIAGE:
        return _TRIAGE_WHY[_triage_phase(candidate)]
    if candidate.type == "mission_report" and candidate.facts.get("reason") == "nothing":
        return _SECOND_LOOK_NOTHING_WHY
    if not answers or outcome.reason.startswith("rules:"):
        code = outcome.reason.removeprefix("rules:")
        return RULE_REASON_WORDS.get(code, _RULE_REASON_FALLBACK)
    fragments = _answer_reasons(candidate, answers)
    return "; ".join(fragments[:3]) if fragments else _MODEL_REASON_FALLBACK


_PLAN_WHY: Mapping[str, str] = {
    "stalled_project": (
        "VIKI can look the project over in plan mode and propose three next actions; it changes "
        "nothing until you approve."
    ),
    "overdue_tasks": (
        "VIKI can check them in plan mode and propose ticking the done ones and new dates for the "
        "rest; it changes nothing until you approve."
    ),
}
_MESSAGE_DRAFT_WHY: Mapping[str, str] = {
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
_CHECK_WHY_WAITING = (
    "VIKI can run the saved checks you have run there before and report how they went."
)
_CHECK_WHY = "The Code workbench's Checks tab shows their output."
_TRIAGE_WHY_AGAIN = (
    "Triage inbox says what happened, and Ask VIKI to suggest there runs it again when you choose."
)
_TRIAGE_WHY_SHOWN = (
    "Triage inbox shows each suggestion beside its capture; you choose, then send the filing draft."
)
_TRIAGE_WHY: Mapping[str, str] = {
    "waiting": (
        "VIKI can ask Claude Code, with read-only tools, to suggest where each capture goes; "
        "nothing in your vault changes until you send the filing draft from Triage inbox."
    ),
    "running": _TRIAGE_WHY_SHOWN,
    "ready": _TRIAGE_WHY_SHOWN,
    "failed": _TRIAGE_WHY_AGAIN,
    "nothing": _TRIAGE_WHY_AGAIN,
}
_CODE_LOOK_WHY = "VIKI can look them over in plan mode and tell you what is half-done or risky."
_SECOND_LOOK_WHY = (
    "VIKI can take a second look in plan mode and tell you if it finds a flaw, a better approach "
    "or a missing piece."
)
_FINDING_WHY = (
    "Its plan waits in the chat VIKI started: Approve makes the change, and Keep planning "
    "refines it first."
)
_SECOND_LOOK_NOTHING_WHY = (
    "VIKI looked again in plan mode and found no flaw, better approach or missing piece."
)


def _card_why(candidate: contracts.Candidate) -> str | None:
    """A Kanban card's why from `facts["status"]`, `facts["seat"]` and `words["age"]`; None
    for any other status, which then reads its type's rule words."""
    status = candidate.facts.get("status")
    ago = _CARD_AGO.get(candidate.words.get("age", ""), "recently")
    seat = _without_bidi(_str_or_none(candidate.facts.get("seat")) or "").strip() or None
    if status in ("blocked", "review"):
        verb = "blocked" if status == "blocked" else "finished"
        if seat:
            return f"{seat[0].upper()}{seat[1:]} {verb} it {ago}."
        return f"It was {verb} {ago}."
    if status == "working":
        return f"Working, last changed {ago}, with no handoff."
    if status == "todo":
        return f"Waiting for {seat or 'a seat'}, last changed {ago}."
    return None


def _answer_reasons(candidate: contracts.Candidate, answers: contracts.Answers) -> list[str]:
    """Up to six reason fragments, in priority order; `why` keeps the first
    three. A deadline fragment needs both `candidate.deadline` and its word, so a word left over
    from a deadline the detector since cleared never shows."""
    fragments: list[str] = []
    if _noul_at_least(answers, "goal_link", 0.7):
        fragments.append("linked to one of your goals")
    if _noul_at_least(answers, "owner_only", 0.7):
        fragments.append("needs your decision")
    if _noul_at_least(answers, "news", 0.7):
        fragments.append("you haven't seen it lately")
    deadline_word = candidate.words.get("deadline")
    if candidate.deadline is not None and deadline_word:
        fragments.append(f"due {deadline_word}")
    if _noul_at_least(answers, "fits_now", 0.7):
        fragments.append("about what you're working on")
    importance = answers.get("importance")
    if isinstance(importance, contracts.ScoreAnswer) and importance.score >= 2:
        fragments.append("matters to a goal")
    return fragments


# `words.age_words`' six words as a card's why reads them; any other word reads "recently".
_CARD_AGO: Mapping[str, str] = {
    "today": "today",
    "yesterday": "yesterday",
    "this week": "a few days ago",
    "last week": "last week",
    "this month": "a few weeks ago",
    "older": "over a month ago",
}


def _noul_at_least(answers: contracts.Answers, name: str, threshold: float) -> bool:
    answer = answers.get(name)
    return isinstance(answer, contracts.NoulAnswer) and answer.p >= threshold


# ---- evidence -----------------------------------------------------------------------------------


def _evidence(candidate: contracts.Candidate) -> tuple[contracts.EvidenceLabel, ...]:
    """At most three `EvidenceLabel`s from `candidate.evidence`, never from an excerpt: a ref's
    label is its file name without the extension — a vault basename is already the note's
    display title (e.g. "Second Brain MOC", "2026-10-21"), so it is shown as-is, digits and
    all. With no ref, a fixed label names only whether the evidence sits inside the vault or
    came from outside it. `workspace_id` and `ref` are set only together, for a vault entry
    (not `outside`) that has a ref: the client's note chip needs both or neither, so an
    outside ref or a no-ref vault entry carries neither. skill_offer is its own shape
    (`_skill_offer_evidence`): chat chips, never a note ref. skill_suggest is its own shape too
    (`_skill_suggest_evidence`): one plain chip, the matched skill's name alone."""
    if candidate.type == "skill_offer":
        return _skill_offer_evidence(candidate)
    if candidate.type == "skill_suggest":
        return _skill_suggest_evidence(candidate)
    workspace_id = _str_or_none(candidate.facts.get("workspace_id"))
    if candidate.type in contracts.SECOND_LOOKS:
        return _second_look_evidence(candidate.facts.get("refs"), workspace_id)
    if candidate.type in contracts.PROJECT_PLANS:
        return _plan_evidence(candidate.facts, workspace_id)
    # A card's launcher chip reads its title, else the stem label like any note.
    card_title = (
        _field(candidate.title, _EVIDENCE_LABEL_LIMIT) if candidate.type in contracts.CARDS else ""
    )
    labels: list[contracts.EvidenceLabel] = []
    for one in candidate.evidence[:3]:
        ref = one.ref if not one.outside else None
        labels.append(
            contracts.EvidenceLabel(
                label=card_title or _field(_evidence_label_text(one), _EVIDENCE_LABEL_LIMIT),
                workspace_id=workspace_id if ref else None,
                ref=ref,
                session_id=None,
            )
        )
    return tuple(labels)


def _skill_offer_evidence(candidate: contracts.Candidate) -> tuple[contracts.EvidenceLabel, ...]:
    """skill_offer's own evidence shape: evidence chips are chats (label the
    title, session_id). `candidate.evidence` holds the *other* chats only (never the chat in
    view, `related_chat`, which is `facts["sessions"][0]`), each `Evidence(None, title, False,
    ())`; zipped with `facts["sessions"][1:]` in the same order for each chip's `session_id`.
    A missing or short `facts["sessions"]` yields fewer chips, never a raise. A blank title
    (an untitled chat's title must never be its first message) reads the fixed "A
    chat" label instead of an empty one."""
    sessions = candidate.facts.get("sessions")
    other_sessions = tuple(sessions)[1:] if isinstance(sessions, (tuple, list)) else ()
    labels: list[contracts.EvidenceLabel] = []
    for session_id, one in zip(other_sessions, candidate.evidence[:3], strict=False):
        labels.append(
            contracts.EvidenceLabel(
                label=_field(one.excerpt, _EVIDENCE_LABEL_LIMIT) or _BLANK_CHAT_LABEL,
                session_id=str(session_id),
            )
        )
    return tuple(labels)


def _skill_suggest_evidence(candidate: contracts.Candidate) -> tuple[contracts.EvidenceLabel, ...]:
    """skill_suggest's own evidence shape: one plain chip labelled with the
    matched skill's name (`facts["skill"]`), never a session or note ref. Missing a name, there
    is no chip, matching `_base_action`'s "no action without a name"."""
    name = _str_or_none(candidate.facts.get("skill"))
    if name is None:
        return ()
    return (contracts.EvidenceLabel(label=_field(name, _EVIDENCE_LABEL_LIMIT)),)


def _second_look_evidence(
    refs: object, workspace_id: str | None
) -> tuple[contracts.EvidenceLabel, ...]:
    """A second-look offer's note chips, one per `facts["refs"]` entry in order (a finding has
    none), each labelled with the ref's stem; a ref that is not text is skipped."""
    if not isinstance(refs, (tuple, list)):
        return ()
    texts = [ref for ref in refs if isinstance(ref, str) and ref]
    return tuple(
        contracts.EvidenceLabel(
            label=_field(PurePosixPath(ref).stem or _VAULT_EVIDENCE_LABEL, _EVIDENCE_LABEL_LIMIT),
            workspace_id=workspace_id,
            ref=ref,
        )
        for ref in texts[: contracts.MAX_SECOND_LOOK_REFS]
    )


def _plan_evidence(
    facts: Mapping[str, object], workspace_id: str | None
) -> tuple[contracts.EvidenceLabel, ...]:
    """A project-plan offer's one note chip, labelled with the project (else the ref's stem); a
    finding, or an offer without a ref, has none."""
    ref = _str_or_none(facts.get("ref"))
    if ref is None or "found" in facts:
        return ()
    label = _project_name(facts) or PurePosixPath(ref).stem or _VAULT_EVIDENCE_LABEL
    return (
        contracts.EvidenceLabel(
            label=_field(label, _EVIDENCE_LABEL_LIMIT), workspace_id=workspace_id, ref=ref
        ),
    )


def _evidence_label_text(one: contracts.Evidence) -> str:
    """`one.ref`'s file name without its extension, when it has a ref and a name is left after
    stripping it; the two fixed strings are a fallback for no ref (vault) or outside text with
    no ref."""
    if one.ref:
        name = PurePosixPath(one.ref).stem
        if name:
            return name
    return _OUTSIDE_EVIDENCE_LABEL if one.outside else _VAULT_EVIDENCE_LABEL


# ---- actions ------------------------------------------------------------------------------------


def _actions(
    candidate: contracts.Candidate,
    outcome: contracts.Outcome,
    mission: contracts.MissionRecord | None,
    grants: Mapping[str, str],
    call_id: str | None,
    hold: str | None,
) -> tuple[contracts.ActionSpec, ...]:
    built: list[contracts.ActionSpec] = []
    if candidate.type in contracts.SAVED_CHECKS:
        built.extend(_check_actions(candidate, outcome, grants))
    elif candidate.type in contracts.INBOX_TRIAGE:
        built.extend(_triage_actions(candidate, outcome, grants))
    elif candidate.type in contracts.SEATS:
        built.extend(_seat_actions(candidate))
    else:
        base = _base_action(candidate, mission)
        if base is not None:
            built.append(base)
    own_actions = contracts.SAVED_CHECKS + contracts.INBOX_TRIAGE
    if candidate.type not in own_actions and outcome.work in ("prepare", "run"):
        work_actions = _work_actions(outcome.work, grants, mission, hold)
        if candidate.type in contracts.LOOK_MISSIONS:
            work_actions = tuple(
                replace(action, label=_LOOK_START_LABEL)
                if action.type == "start_prepare"
                else action
                for action in work_actions
            )
        if candidate.type in contracts.SECOND_LOOKS:
            work_actions = tuple(
                replace(action, label=_SECOND_LOOK_START_LABEL)
                if action.type == "start_prepare"
                else action
                for action in work_actions
            )
            if any(action.type == "open_chat" for action in work_actions):
                # The running look's chat takes the base's place: the base opens the chat the
                # user has open, and two buttons both named Open chat cannot be told apart.
                built = [action for action in built if action.type != "open_chat"]
        if candidate.type in contracts.PROJECT_PLANS:
            work_actions = tuple(
                replace(action, label=_PLAN_START_LABELS[candidate.type])
                if action.type == "start_prepare"
                else action
                for action in work_actions
            )
        built.extend(work_actions)
    if call_id is not None:
        built.append(_action("open_context", call_id=call_id))
    if built:
        built[0] = replace(built[0], primary=True)
    return tuple(built)


def _base_action(
    candidate: contracts.Candidate, mission: contracts.MissionRecord | None
) -> contracts.ActionSpec | None:
    candidate_type = candidate.type
    if candidate_type == "approvals":
        return _action("open_approvals")
    if candidate_type == "tasks_due":
        return _action(
            "open_note",
            ref=candidate.related_ref,
            workspace_id=_str_or_none(candidate.facts.get("workspace_id")),
        )
    if candidate_type == "agent_runs":
        return _action("open_runs")
    if candidate_type == "inbox":
        return _action("open_inbox")
    if candidate_type == "routine":
        return _action("open_routine", routine_id=_str_or_none(candidate.facts.get("routine_id")))
    if candidate_type == "uncommitted":
        return None  # no action: informational only
    if candidate_type == "mission_report":
        session_id = (mission.session_id if mission is not None else None) or candidate.related_chat
        return _action("open_chat", session_id=session_id)
    if candidate_type == "skill_offer":
        return _action(
            "draft_chat",
            label=_SKILL_OFFER_LABEL,
            session_id=candidate.related_chat,
            text=_skill_offer_draft(candidate),
        )
    if candidate_type == "skill_suggest":
        name = _str_or_none(candidate.facts.get("skill"))
        if name is None:
            return None
        return _action(
            "draft_chat",
            label=_SKILL_SUGGEST_LABEL,
            session_id=_str_or_none(candidate.facts.get("session")),
            text=f"/{name}",
        )
    if candidate_type == "learn_source":
        return _action("draft_new", label=_LEARN_SOURCE_LABEL, text=_learn_source_draft(candidate))
    if candidate_type == "retro_due":
        return _action("draft_new", label=_RETRO_DUE_LABEL, text=_RETRO_DUE_DRAFT)
    if candidate_type == "question_tune":
        return _action(
            "draft_new", label=_QUESTION_TUNE_LABEL, text=_question_tune_draft(candidate)
        )
    if candidate_type in contracts.CARDS:
        return _action(
            "open_note",
            label=_CARD_LABEL,
            ref=candidate.related_ref,
            workspace_id=_str_or_none(candidate.facts.get("workspace_id")),
        )
    if candidate_type in contracts.VAULT_LOOKS:
        return _action("open_vault", workspace_id=_str_or_none(candidate.facts.get("workspace_id")))
    if candidate_type == "bridge_stopped":
        return _action("open_messaging")
    if candidate_type in contracts.LOOKS:
        return _action("open_chat", session_id=_str_or_none(candidate.facts.get("session_id")))
    if candidate_type in contracts.LOOK_MISSIONS:
        return _action("open_code", workspace_id=_str_or_none(candidate.facts.get("folder_id")))
    if candidate_type in contracts.SECOND_LOOKS:
        return _action(
            "open_chat",
            label=_FINDING_OPEN_LABEL if "found" in candidate.facts else None,
            session_id=_str_or_none(candidate.facts.get("session")),
        )
    if candidate_type in contracts.PROJECT_PLANS:
        if "found" in candidate.facts:
            return _action(
                "open_chat",
                label=_FINDING_OPEN_LABEL,
                session_id=_str_or_none(candidate.facts.get("session")),
            )
        return _action(
            "open_note",
            ref=_str_or_none(candidate.facts.get("ref")),
            workspace_id=_str_or_none(candidate.facts.get("workspace_id")),
        )
    if candidate_type in contracts.MESSAGE_DRAFTS:
        return _action(
            "draft_chat",
            label=_DRAFT_LABELS[candidate_type],
            session_id=_str_or_none(candidate.facts.get("session")),
            text=_message_draft(candidate),
        )
    if candidate_type in contracts.CHANGES:
        # `_base_action`: the vault-write itself is the draft (a chat the user sends from
        # Approvals, per `changes.py`'s fixed texts); this module only points there.
        return _action("open_approvals")
    raise ValueError(f"unknown candidate type: {candidate_type!r}")


def _seat_actions(candidate: contracts.Candidate) -> tuple[contracts.ActionSpec, ...]:
    """A seat item's actions: its session to open (a run report's hold opens its routine
    instead, plus Start too when the hold is the plan brake or quiet hours, since the user may
    start it themselves), plus Stop on a working seat and Start on an offer."""
    facts = candidate.facts
    session_id = _str_or_none(facts.get("session_id"))
    routine_id = _str_or_none(facts.get("routine_id"))
    open_chat = _action("open_chat", label=_SEAT_OPEN_LABEL, session_id=session_id)
    if candidate.type == "seat_working":
        return (open_chat, _action("stop_seat", routine_id=routine_id))
    if candidate.type == "seat_start":
        return (_action("start_seat", routine_id=routine_id),)
    reason = facts.get("reason")
    if candidate.type == "seat_report" and reason in _SEAT_HOLDS:
        open_routine = _action("open_routine", routine_id=routine_id)
        if reason in _SEAT_START_HOLDS:
            return (open_routine, _action("start_seat", routine_id=routine_id))
        return (open_routine,)
    return (open_chat,)


def _check_phase(candidate: contracts.Candidate) -> str:
    phase = candidate.facts.get("phase")
    return phase if phase in contracts.CHECK_PHASES else "waiting"


def _check_actions(
    candidate: contracts.Candidate, outcome: contracts.Outcome, grants: Mapping[str, str]
) -> tuple[contracts.ActionSpec, ...]:
    """A saved-check item's actions: its folder in Code, plus Run checks on a waiting folder the
    cycle offers under Ask first (the cycle passes the check grant as `grants["run"]`), and Stop
    while the checks run."""
    folder_id = _str_or_none(candidate.facts.get("folder_id"))
    phase = _check_phase(candidate)
    built = [_action("open_code", workspace_id=folder_id)]
    if phase == "waiting" and outcome.work == "run" and grants.get("run", "off") == "ask":
        built.append(_action("run_checks", workspace_id=folder_id))
    if phase == "running":
        built.append(_action("stop_checks", workspace_id=folder_id))
    return tuple(built)


def _triage_phase(candidate: contracts.Candidate) -> str:
    phase = candidate.facts.get("phase")
    return phase if phase in contracts.TRIAGE_PHASES else "waiting"


def _triage_actions(
    candidate: contracts.Candidate, outcome: contracts.Outcome, grants: Mapping[str, str]
) -> tuple[contracts.ActionSpec, ...]:
    """An inbox-triage item's actions: Triage inbox, plus Suggest destinations on a waiting item
    the cycle offers under Ask first (the cycle passes the grant as `grants["prepare"]`)."""
    built = [_action("open_triage")]
    if (
        _triage_phase(candidate) == "waiting"
        and outcome.work == "prepare"
        and grants.get("prepare", "off") == "ask"
    ):
        built.append(_action("suggest_inbox"))
    return tuple(built)


def _work_actions(
    work: str,
    grants: Mapping[str, str],
    mission: contracts.MissionRecord | None,
    hold: str | None,
) -> tuple[contracts.ActionSpec, ...]:
    """`work` is "prepare" or "run". A mission already active for this item always wins, so the
    user can reach or stop it no matter what `grants` or `hold` now says; otherwise `hold` of
    "limit" shows no start action (today's count is used, raised nowhere by this button), and
    the matching start action shows only on "ask" ("allowed" means the engine already started
    it, "off" means never)."""
    if mission is not None and mission.state in _ACTIVE_MISSION_STATES:
        return (
            _action("open_chat", session_id=mission.session_id),
            _action("stop_mission", mission_id=mission.mission_id),
        )
    if hold == "limit":
        return ()
    if grants.get(work, "off") == "ask":
        return (_action("start_prepare" if work == "prepare" else "start_run"),)
    return ()


def _action(
    action_type: str,
    *,
    label: str | None = None,
    session_id: str | None = None,
    workspace_id: str | None = None,
    ref: str | None = None,
    routine_id: str | None = None,
    call_id: str | None = None,
    mission_id: str | None = None,
    text: str | None = None,
) -> contracts.ActionSpec:
    """One action; `label` overrides the generic `ACTION_LABELS[action_type]` for an item that
    needs its own call to action (the four self-improvement drafts: "Make this a skill" and
    friends, never the generic "Draft it in this/a new chat")."""
    return contracts.ActionSpec(
        type=action_type,
        label=label if label is not None else ACTION_LABELS[action_type],
        primary=False,
        session_id=session_id,
        workspace_id=workspace_id,
        ref=ref,
        routine_id=routine_id,
        call_id=call_id,
        mission_id=mission_id,
        text=text,
    )


# ---- self-improvement drafts: facts, refs and names only, never an excerpt ----------------------

_SKILL_OFFER_LABEL = "Make this a skill"
_SKILL_SUGGEST_LABEL = "Use this skill"
_LEARN_SOURCE_LABEL = "Learn these sources"
_RETRO_DUE_LABEL = "Start a retro"
_QUESTION_TUNE_LABEL = "Draft better wording"
_CARD_LABEL = "Open card"
_LOOK_START_LABEL = "Take a look"
_SECOND_LOOK_START_LABEL = "Take a second look"
_FINDING_OPEN_LABEL = "Open plan"
_PLAN_START_LABELS: Mapping[str, str] = {
    "stalled_project": "Plan next steps",
    "overdue_tasks": "Check these tasks",
}
_DRAFT_LABELS: Mapping[str, str] = {
    "context_handoff": "Draft a handoff",
    "source_note": "Draft literature notes",
}

_HANDOFF_DRAFT = (
    "This chat's context is nearly full. Write a handoff I can take to a fresh chat: the "
    "decisions we made, the open questions and the next steps, each as a short list that names "
    "the files and notes it concerns. Reply with the handoff alone, and do not create or change "
    "any file."
)
_SOURCE_NOTE_DRAFT = (
    "Write a literature note in this vault for each web page you fetched in your last reply.",
    "Use this vault's own learn skill if it has one.",
)
_SOURCE_NOTE_SEARCH_FIRST = (
    "Your last reply read no note in this vault, so first search it for notes on the same "
    "subject and link the ones you find from each new note."
)
_SOURCE_NOTE_SHOW_FIRST = (
    "Show me each literature note and wait for my go-ahead before you write it."
)

# The lead line without `draftPrompt`'s turn-specific lead-in (this module has no transcript to
# quote) and the six contract lines verbatim, checked against `skillTriggers.ts` by
# `test_skill_draft_matches_the_client_prompt` (the drift guard; the lead and "Why now" differ
# by design).
_SKILL_DRAFT_LEAD = (
    "Make this a skill. Draft a reusable Claude Code skill from the work in this conversation."
)
_SKILL_DRAFT_CONTRACT: tuple[str, ...] = (
    "Reply with exactly one fenced code block whose info string is SKILL.md, holding the whole "
    "file:",
    "Wrap it in a fence longer than any fence inside it (for example four backticks), so a bare "
    "inner fence cannot cut it short.",
    "- first the frontmatter between --- lines, with name (lowercase letters, digits and single "
    "hyphens, at most 64 characters) and description (one line of at most 1,536 characters: what "
    "it does and when to use it);",
    "- then the steps, written so that a new session can follow them without this conversation.",
    "If this work refines one of your existing skills, give that skill's whole updated file under "
    "its existing name.",
    "Do not create or change any file: VIKI Agent turns your block into a proposal that I review "
    "in Approvals.",
)
# "Why now", one of `SKILL_TRIGGERS`; "several" replaces the client's own chat count (no digit).
_SKILL_WHY_NOW: Mapping[str, str] = {
    "correction": "I corrected you in this chat; keep the corrected way.",
    "repeated_request": "I started several chats with a similar first message.",
    "long_session": "this chat ran a long task to the end.",
}


def _skill_offer_draft(candidate: contracts.Candidate) -> str:
    """The Make-this-a-skill draft: the lead plus the six contract lines, then a
    trigger-worded "Why now" when `facts["trigger"]` is one `_SKILL_WHY_NOW` recognises."""
    trigger = candidate.facts.get("trigger")
    why_now = _SKILL_WHY_NOW.get(trigger) if isinstance(trigger, str) else None
    lines = [_SKILL_DRAFT_LEAD, *_SKILL_DRAFT_CONTRACT]
    if why_now:
        lines.append(f"Why now: {why_now}")
    return "\n".join(lines)


_LEARN_SOURCE_DRAFT_INTRO = "Learn the sources I captured in these notes:"
_LEARN_SOURCE_DRAFT_OUTRO = (
    "Use this vault's own learn skill if it has one. Show me each literature note before you "
    "write it."
)


def _learn_source_draft(candidate: contracts.Candidate) -> str:
    """learn_source's draft: one "- {ref}" line per capture (never its excerpt)."""
    refs = [one.ref for one in candidate.evidence if one.ref]
    lines = [_LEARN_SOURCE_DRAFT_INTRO, *(f"- {ref}" for ref in refs), _LEARN_SOURCE_DRAFT_OUTRO]
    return "\n".join(lines)


def _message_draft(candidate: contracts.Candidate) -> str:
    """A message draft's fixed text (no chat text, URL or ref); the search-first sentence is
    left out only when the reply is known to have read a vault note."""
    if candidate.type == "context_handoff":
        return _HANDOFF_DRAFT
    search = () if candidate.facts.get("notes") is True else (_SOURCE_NOTE_SEARCH_FIRST,)
    return " ".join((*_SOURCE_NOTE_DRAFT, *search, _SOURCE_NOTE_SHOW_FIRST))


_RETRO_DUE_DRAFT = (
    "Run a retro on my recent work with VIKI: I marked several of Decision's items Not useful "
    "lately. Use this vault's own retro skill if it has one. Ask me before you change any "
    "memory, skill or instruction."
)


def _question_tune_draft(candidate: contracts.Candidate) -> str:
    """question_tune's draft: the question's label and name, its live wording in a json fence,
    then the `decision-question` reply contract (one JSON object, "name" and "question") that
    turns a reply into a proposal `SkillTurnActions` can offer in Approvals. `facts["name"]`
    and `facts["wording_json"]` are read with a safe fallback, like every other fact here."""
    raw_name = candidate.facts.get("name")
    name = raw_name if isinstance(raw_name, str) else ""
    label = playbook.LABELS.get(name, name)
    raw_wording = candidate.facts.get("wording_json")
    wording = raw_wording if isinstance(raw_wording, str) else "{}"
    lines = [
        f'Decision\'s question "{label}" ({name}) could use better wording.',
        "Its current wording:",
        "```json",
        wording,
        "```",
        "Reply with exactly one fenced code block whose info string is decision-question, "
        'holding one JSON object with two keys: "name" (this question\'s name) and "question" '
        "(the wording above, changed as needed).",
        # The last two: the pointer to the guide (`playbook.GUIDE_SENTENCE`), then the
        # outside-data sentence a proposal must end with (`playbook.check_question`).
        "Keep the question's meaning and the last two sentences of its focus.",
        "Add not_for examples for what went wrong.",
        "Do not create or change any file: VIKI Agent turns your block into a proposal that I "
        "review in Approvals.",
    ]
    return "\n".join(lines)


# ---- small helpers -------------------------------------------------------------------------------


def _str_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _without_bidi(text: str) -> str:
    """`text` less its bidi controls: read before a seat name is capitalised, and by `_field`."""
    return "".join(ch for ch in text if ch not in _BIDI_CONTROLS)


def _field(text: str, limit: int = _FIELD_LIMIT) -> str:
    """`text` as one line: control characters become spaces, bidi controls are dropped,
    whitespace runs collapse to one space, the ends are trimmed, and anything past `limit`
    characters is cut with "…"."""
    flattened = "".join(
        " " if unicodedata.category(ch) == "Cc" else ch for ch in _without_bidi(text)
    )
    collapsed = " ".join(flattened.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1] + "…"
