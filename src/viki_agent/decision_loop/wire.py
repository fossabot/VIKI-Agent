"""The Decision routes' JSON: views built from the loop's records, and parsers of client bodies.

Views (`item_view`, `launcher_view`, `page_view`, `missions_view`, `mission_view`, `calls_view`,
`call_detail_view`, `test_view`, `question_proposal_view`, `change_act_view`, `data_summary_view`,
`data_records_view`, `data_calls_view`, `data_decision_view`, `data_suggestion_view`,
`data_deleted_view`) are plain dicts
keyed as the client's `clients/decisionLoop/types.ts`; `example_views()` builds one of each from
fixed inputs, the twin of that folder's examples.json. A view reads no store and no clock: the
caller passes records, plain values and `now`. Stamps are the store's 24-character UTC text,
counts are ints and money (`cost_usd`, `cap_usd`) floats; an unset action or evidence member is
left out, and a member the client types as nullable is null when unset; a version is
`display_version` output ("1.14.0"), never a model id. Every free-text member is cut to fit the
client's own bound (`_client_safe`, in `clients/decisionLoop/parse.ts`'s UTF-16 units); a ref is
never cut, since a cut ref would point at the wrong note -- one that fails parse.ts's rule is
dropped with its pair instead (`_evidence`, `_action`).

Parsers (`parse_presence` with its `parse_page`, `parse_feedback`, `parse_observe`,
`parse_report`, `parse_preference`, `parse_mission_start`, `parse_inbox_suggest`,
`parse_proposal`,
`parse_proposal_act`, `parse_key`, `parse_data_query`, `parse_data_record`) take a decoded JSON body
and answer typed values, or raise `DecisionLoopError("invalid_request")`, whose text is the code
alone: exact key sets, real bools and ints, listed values, canonical ids and bounded text
without control characters. Body sizes are the routes' to check.
"""

from __future__ import annotations

import json
import re
import unicodedata
import uuid
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, NoReturn, Protocol, cast

from viki_agent.api_json import failure
from viki_agent.automation.routines import parse_stamp, stamp
from viki_agent.decision_loop import arbiter, playbook
from viki_agent.decision_loop.contracts import (
    AUDIT_SIZE,
    CALL_OUTCOMES,
    CANDIDATES,
    FEEDBACK_HELD,
    FEEDBACK_REASONS,
    FEEDBACK_SHOWN,
    FINDING_KINDS,
    INBOX_TRIAGE,
    ITEM_STATES,
    LEVELS,
    MAX_CALLS,
    MAX_EPISODES,
    MAX_OPEN_ITEMS,
    MAX_SECOND_LOOK_REF_BYTES,
    MAX_SECOND_LOOK_REFS,
    MESSAGE_DRAFTS,
    MIN_LABELS,
    MODEL_IDS,
    PROJECT_PLANS,
    QUESTION_LIMIT,
    REQUEST_LIMIT,
    RETENTION_DAYS,
    SAVED_CHECKS,
    SECOND_LOOK_TYPES,
    SECOND_LOOKS,
    SKILL_TRIGGERS,
    SOURCES,
    TOPICS,
    ActionSpec,
    Calibration,
    CalibrationBin,
    CallBody,
    CallRecord,
    ContextHint,
    DecisionLoopError,
    DecisionSettings,
    EvidenceLabel,
    ItemBody,
    ItemRecord,
    KindYield,
    Learned,
    MissionRecord,
    ModelError,
    ModelReply,
    PageFacts,
    Presence,
    QuestionRate,
    SeatTeam,
    SecondLookHint,
    SecondLookYields,
    ShadowStats,
    SkillHint,
    SourceHint,
    SuggestHint,
    TrialStats,
    TriggerYield,
    canonical_json,
    item_key,
)
from viki_agent.decision_loop.plan_brake import Hold
from viki_agent.profiles.native_assets import SKILL_NAME

# ---- vocabularies and limits -------------------------------------------------------------------

# The wire's own vocabularies, named as types.ts names them.
LAUNCHER_STATES = ("off", "paused", "on")
LAUNCHER_LEVELS = ("idle", "list", "badge", "question", "nudge", "alert")
MODEL_STATES = ("ready", "rules", "no_key", "key_rejected", "capped", "unreachable")
KEY_STATES = ("saved", "missing")
POLICIES = ("rules", "model")
OBSERVE_EVENTS = ("shown", "opened", "acted")
MISSION_WORK = ("prepare", "run")
GIT_STATES = ("known", "unknown", "not_repository")
PROPOSAL_ACTIONS = ("approve", "reject")
DATA_KINDS = ("decisions", "suggestions", "calls")
DATA_SHOWN = ("held", "listed", "attention", "report")
DATA_ANSWERS = (*FEEDBACK_SHOWN, *FEEDBACK_HELD, "none")
MAX_DATA_ROWS = 50
MAX_DATA_IDS = 50
MAX_DATA_DAYS = 366
MAX_DATA_EVIDENCE = 20

MAX_PAGE_COUNT = 999  # a presence report's page counts
MAX_DIRTY = 100_000
MAX_CHAT_TITLE = 120
MAX_NOTE_REF = 512
MAX_SKILL_SESSIONS = 10  # a page's own skill hint
MAX_SUGGEST_USED = 32  # the skills a page reports as already used in the chat in view
MAX_STARTED = 50  # chats VIKI started, on the launcher
MAX_HELD_BACK = AUDIT_SIZE
MAX_RECENT = 20
MAX_PROPOSAL_REASON = 200  # a question proposal act's reason, in UTF-16 units

# Response text bounds, mirroring frontend/src/clients/decisionLoop/parse.ts's own MAX_* (the
# client refuses a whole view if one string anywhere in it misses its bound or holds a bad
# character): what 300, why 400 (also a `recent` row's "why"), a label (evidence, action or a
# call_detail question) 120, a ref (evidence or action) 512, a draft action's own text 4,000
# keeping its own newlines and tabs, a question proposal's two wordings 4,000 with the same
# exception, state_text 20,000 with it too, questions_text 20,000 with no such exception, and
# everything else free-text (title, reason, playbook_version, a split part, a question's
# name/answer) 400.
MAX_WHAT = 300
MAX_WHY = 400
MAX_LABEL = 120
MAX_TEXT = 400
MAX_DRAFT_TEXT = 4_000
MAX_PROPOSAL_TEXT = 4_000
MAX_STATE_TEXT = 20_000
MAX_QUESTIONS_TEXT = 20_000
MAX_REF = 512

_LOUD = ("badge", "nudge", "alert")  # the levels the launcher's badge counts
_COUNTS = ("shown", *FEEDBACK_SHOWN, *FEEDBACK_HELD)
_HELD_BACK_MEMBERS = ("episode_id", "created_at", "candidate", "level", "reason", "why", "feedback")
_RECENT_MEMBERS = (
    "episode_id",
    "created_at",
    "candidate",
    "level",
    "policy",
    "reason",
    "why",
    "feedback",
)
_PAGE_KEYS = frozenset(
    {
        "native_waiting",
        "runs",
        "git",
        "routine",
        "skill",
        "suggest",
        "second_look",
        "context",
        "sources",
    }
)
_RUNS_KEYS = frozenset({"working", "review"})
_GIT_KEYS = frozenset({"state", "dirty"})
_ROUTINE_KEYS = frozenset({"routine_id", "next_run", "paused"})
_SKILL_KEYS = frozenset({"trigger", "sessions", "titles"})
_SUGGEST_KEYS = frozenset({"session", "title", "used"})
_SECOND_LOOK_KEYS = frozenset({"session", "turn", "trigger", "title", "refs"})
_CONTEXT_KEYS = frozenset({"session", "percent"})
_SOURCES_KEYS = frozenset({"session", "turn", "notes"})
_SKILL_NAME_CHARS = 64  # the longest skill name `native_assets` admits with `SKILL_NAME`
_PRESENCE_KEYS = frozenset(
    {"window", "focused", "typing", "chat", "chat_title", "note", "launcher", "page"}
)
_NOTE_KEYS = frozenset({"workspace_id", "ref"})
_FEEDBACK_KEYS = frozenset({"item_key", "episode_id", "value", "reason", "source"})
_OBSERVE_KEYS = frozenset({"event", "item_keys"})
_REPORT_KEYS = frozenset({"topic", "source"})
_UNMUTE_KEYS = frozenset({"name", "candidate"})
_RAISE_KEYS = frozenset({"name", "kind"})
_NAME_ONLY_KEYS = frozenset({"name"})
_MISSION_KEYS = frozenset({"item_key", "work"})
_INBOX_SUGGEST_KEYS = frozenset({"item_key"})
_PROPOSAL_KEYS = frozenset({"name", "question"})
_PROPOSAL_ACT_KEYS = frozenset({"action", "revision", "reason"})
_KEY_KEYS = frozenset({"key"})
# A stamp in ASCII digits, milliseconds optional: Python's `\d` also matches other scripts' digits.
_STAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{3})?Z")
_ITEM_KEY = re.compile(r"[0-9a-f]{64}")
_REVISION = re.compile(r"[0-9a-f]{64}")  # a question proposal's revision
_QUESTION_NAME = re.compile(r"[a-z_]{1,32}")  # as decision_proposals' own name column
_KEY = re.compile(r"[\x21-\x7e]{1,4096}")  # printable ASCII without the space
_VERSION = re.compile(r"\d+(?:\.\d+){1,3}", re.ASCII)


class CallRow(Protocol):
    """A stored model call as the call views read it: a `CallRecord`, or a listed row whose body
    did not open (None)."""

    @property
    def call_id(self) -> str: ...

    @property
    def created_at(self) -> str: ...

    @property
    def session_id(self) -> str | None: ...

    @property
    def input_tokens(self) -> int | None: ...

    @property
    def body(self) -> CallBody | None: ...


class DataCallRow(CallRow, Protocol):
    @property
    def outcome(self) -> str: ...

    @property
    def cost_micro_usd(self) -> int | None: ...


@dataclass(frozen=True)
class DataQuery:
    kind: str
    ids: tuple[str, ...] | None
    topic: str | None
    shown: str | None
    answer: str | None
    state: str | None
    outcome: str | None
    since: datetime | None
    until: datetime | None
    before: tuple[str, str] | None


@dataclass(frozen=True)
class FeedbackRequest:
    """A feedback body: exactly one of `item_key` (an item that was shown) and `episode_id` (a
    decision held back) is set."""

    item_key: str | None
    episode_id: str | None
    value: str
    reason: str | None
    source: str


# ---- views -------------------------------------------------------------------------------------


def display_version(model: str | None) -> str:
    """The version a model id carries ("1.14.0"): its first run of two to four dot-joined ASCII
    digit groups, or "" when it has none."""
    found = None if model is None else _VERSION.search(model)
    return "" if found is None else found.group()


def _stamp(value: datetime | None) -> str | None:
    return None if value is None else stamp(value)


def _until(value: datetime | None, now: datetime) -> str | None:
    """`value` as a stamp while it is later than `now`, else None."""
    return stamp(value) if value is not None and value > now else None


def _members(view: dict[str, Any], optional: Mapping[str, str | None]) -> dict[str, Any]:
    """`view` with each set member of `optional` added; an unset one stays out."""
    view.update((name, value) for name, value in optional.items() if value is not None)
    return view


def _bad_char(ch: str, *, allow_newline_tab: bool) -> bool:
    """A character `parse.ts`'s `isControlCode` refuses: a C0 control (U+0000-U+001F) or DEL
    (U+007F), always; a tab (U+0009) or newline (U+000A) passes only when `allow_newline_tab`
    (state_text's own exception). A lone surrogate (U+D800-U+DFFF) is refused too, stricter
    than parse.ts, which does not test for one: every such character a Python `str` can hold
    is unpaired, since an astral character already reads as one code point, not a pair. C1
    controls (U+0080-U+009F) are left alone: parse.ts does not flag them either."""
    code = ord(ch)
    if 0xD800 <= code <= 0xDFFF:
        return True
    if code == 0x7F:
        return True
    if code < 0x20:
        return not (allow_newline_tab and code in (0x09, 0x0A))
    return False


def _utf16_len(value: str) -> int:
    """`value`'s length the way the client's `parse.ts` measures it: JS's `string.length`, its
    count of UTF-16 code units. Python's `len` counts code points instead, one short for each
    astral character (outside the basic plane), which this counts as the two units its
    surrogate pair takes on the wire."""
    return sum(2 if ord(ch) > 0xFFFF else 1 for ch in value)


def _client_safe(value: str, limit: int, *, allow_newline_tab: bool = False) -> str:
    """`value` as `parse.ts`'s `text`/`stateText` will accept it at `limit`: every bad character
    (`_bad_char`) dropped, then cut to `limit` UTF-16 units if it still runs over, with "…"
    added to mark the cut. Python iterates by whole code point, so a kept character is always
    whole too: a cut only ever lands between characters, never inside the surrogate pair an
    astral one would need on the wire."""
    cleaned = "".join(ch for ch in value if not _bad_char(ch, allow_newline_tab=allow_newline_tab))
    if _utf16_len(cleaned) <= limit:
        return cleaned
    kept: list[str] = []
    units = 0
    budget = limit - 1  # room for the "…"
    for ch in cleaned:
        width = 2 if ord(ch) > 0xFFFF else 1
        if units + width > budget:
            break
        kept.append(ch)
        units += width
    return "".join(kept) + "…"


def _fits(value: str, limit: int) -> bool:
    """Whether `value` already satisfies `parse.ts`'s plain `text(value, limit)`, untouched: no
    bad character (`_bad_char`, without the state_text exception) and at most `limit` UTF-16
    units. A ref is checked with this, never cut by `_client_safe`: a cut ref would point at
    the wrong note, so one that fails is dropped instead, with its pair."""
    return _utf16_len(value) <= limit and not any(
        _bad_char(ch, allow_newline_tab=False) for ch in value
    )


def _evidence(label: EvidenceLabel) -> dict[str, Any]:
    """A chip in one of the client's three shapes (`parse.ts`'s `itemEvidence`): a bare label,
    a vault note (`workspace_id` and `ref` together) or a chat (`session_id`). A lone
    `workspace_id` or `ref` is not one of them, so it is dropped, keeping the label; so is a
    `ref` that fails `parse.ts`'s own rule (`_fits`, at `MAX_REF`) -- dropped with its
    `workspace_id`, never cut, since a cut ref would point at the wrong note. `label` is cut to
    `MAX_LABEL` instead (`_client_safe`): unlike a ref, an approximate label is still useful."""
    workspace_id, ref = label.workspace_id, label.ref
    if (workspace_id is None) != (ref is None) or (ref is not None and not _fits(ref, MAX_REF)):
        workspace_id, ref = None, None
    return _members(
        {"label": _client_safe(label.label, MAX_LABEL)},
        {"workspace_id": workspace_id, "ref": ref, "session_id": label.session_id},
    )


_DRAFT_ACTIONS = ("draft_chat", "draft_new")  # the two types ACTION_EXTRAS gives a "text" member


def _action(action: ActionSpec) -> dict[str, Any] | None:
    """`action` in the client's shape (`parse.ts`'s `itemAction`), or None when its `ref` fails
    parse.ts's rule (`_fits`, at `MAX_REF`) or it is one of `_DRAFT_ACTIONS` with no `text`:
    unlike an evidence chip, `open_note` has no shape that omits `ref`, so the whole action is
    dropped rather than just the ref, and never cut -- a cut ref would point at the wrong note;
    a draft action with nothing to fill the message box with is just as useless whole. `label`
    is cut to `MAX_LABEL` and `text` to `MAX_DRAFT_TEXT`, keeping its own newlines and tabs like
    state_text (`_client_safe`)."""
    if action.ref is not None and not _fits(action.ref, MAX_REF):
        return None
    if action.type in _DRAFT_ACTIONS and action.text is None:
        return None
    return _members(
        {
            "type": action.type,
            "label": _client_safe(action.label, MAX_LABEL),
            "primary": action.primary,
        },
        {
            "session_id": action.session_id,
            "workspace_id": action.workspace_id,
            "ref": action.ref,
            "routine_id": action.routine_id,
            "call_id": action.call_id,
            "mission_id": action.mission_id,
            "text": None
            if action.text is None
            else _client_safe(action.text, MAX_DRAFT_TEXT, allow_newline_tab=True),
        },
    )


def item_view(item: ItemRecord) -> dict[str, Any]:
    """An item as the launcher lists it; an item without a kind shows as "inform". `what`/`why`
    are cut to `MAX_WHAT`/`MAX_WHY` (`_client_safe`); an evidence chip or action whose ref fails
    parse.ts's rule is dropped instead of cut (`_evidence`, `_action`)."""
    body = item.body
    return {
        "item_key": item.item_key,
        "episode_id": item.episode_id,
        "candidate": item.candidate,
        "kind": item.kind or "inform",
        "level": item.level,
        "what": _client_safe(body.what, MAX_WHAT),
        "why": _client_safe(body.why, MAX_WHY),
        "evidence": [_evidence(label) for label in body.evidence],
        "actions": [view for action in body.actions if (view := _action(action)) is not None],
        "created_at": item.created_at,
        "mission_id": item.mission_id,
    }


def _level(items: Sequence[ItemRecord]) -> str:
    """The loudest item's level, "idle" for none or only silent ones, and "question" in place of
    badge when a badge item asks (kind "ask"): nudge and alert outrank it, list stays list."""
    rank = max((LEVELS.index(item.level) for item in items if item.level in LEVELS), default=0)
    if not rank:
        return "idle"
    if LEVELS[rank] == "badge" and any(
        item.level == "badge" and item.kind == "ask" for item in items
    ):
        return "question"
    return LEVELS[rank]


def launcher_view(
    settings: DecisionSettings,
    items: Sequence[ItemRecord],
    *,
    now: datetime,
    model_state: str,
    checked_at: datetime | None,
    working: int,
    nudge: int,
    glow: int,
    started: Sequence[str],
    review: tuple[bool, int] = (False, 0),
    plans_waiting: int = 0,
) -> dict[str, Any]:
    """The launcher at `now`.

    Its state is "off" while Decision is off, "paused" while `paused_until` is later than `now`
    (that stamp shown), else "on"; `focus_until` shows while it is later. It lists the first
    `MAX_OPEN_ITEMS` of `items` in the order given (the agenda's puts the loudest first): `open`
    counts them, `count` those at badge or louder, and `level` is the loudest one's ("question"
    for a badge when a badge item asks). `model_state` is one of `MODEL_STATES`, `checked_at`
    the last check, `working` VIKI's running chats, `nudge` and `glow` the cue counters,
    `started` the chats VIKI started (the first `MAX_STARTED`), `review` (due, count) the
    weekly check card -- `stats.review`'s own shape, (False, 0) while its caller has none yet --
    and `plans_waiting` VIKI's chats whose plan waits.
    """
    listed = items[:MAX_OPEN_ITEMS]
    paused_until = _until(settings.paused_until, now) if settings.enabled else None
    if not settings.enabled:
        state = "off"
    elif paused_until is not None:
        state = "paused"
    else:
        state = "on"
    due, count = review
    return {
        "state": state,
        "level": _level(listed),
        "count": sum(1 for item in listed if item.level in _LOUD),
        "nudge": nudge,
        "glow": glow,
        "working": working,
        "open": len(listed),
        "checked_at": _stamp(checked_at),
        "paused_until": paused_until,
        "focus_until": _until(settings.focus_until, now),
        "model": model_state,
        "items": [item_view(item) for item in listed],
        "started": list(started[:MAX_STARTED]),
        "review": {"due": due, "count": count},
        "plans_waiting": plans_waiting,
    }


def mission_view(mission: MissionRecord) -> dict[str, Any]:
    """One of VIKI's chats; `started_at` is when its mission was created. `title`/`reason` are
    cut to `MAX_TEXT` (`_client_safe`)."""
    return {
        "mission_id": mission.mission_id,
        "session_id": mission.session_id,
        "work": mission.work,
        "state": mission.state,
        "title": _client_safe(mission.title, MAX_TEXT),
        "started_at": mission.created_at,
        "finished_at": mission.finished_at,
        "reason": _client_safe(mission.reason, MAX_TEXT),
    }


def missions_view(
    running: Sequence[MissionRecord],
    settings: DecisionSettings,
    *,
    prepare_today: int,
    run_today: int,
    hold: Hold | None = None,
    team: SeatTeam | None = None,
    look_today: int = 0,
    second_look_today: int = 0,
    project_plan_today: int = 0,
    inbox_today: int = 0,
    check_today: int | None = None,
    check_running: int = 0,
) -> dict[str, Any]:
    """The page's `missions` block: the running chats, today's starts by work, the daily
    limits the settings set, the plan brake's `hold` on VIKI's own starts: None, or the stamp
    it ends at (None when it names no end), and the `team`'s starts today against
    `settings.limit_team` and its runs open now (0 and 0 without a `team`), and `look_today`
    Look chats started today against `settings.limit_look`, and `second_look_today` second looks
    started today against `settings.limit_second_look`, and `project_plan_today` project plans
    started today against `settings.limit_project_plan`, and `inbox_today` inbox suggestion
    items raised today against `settings.limit_inbox_triage`. `checks` is null without a
    `check_today`, else that count of new commits checked today against `settings.limit_check`
    and the `check_running` runs VIKI started that are still going."""
    return {
        "running": [mission_view(mission) for mission in running],
        "today": {"prepare": prepare_today, "run": run_today},
        "limits": {"prepare": settings.limit_drafts, "run": settings.limit_chats},
        "hold": None if hold is None else {"until": _stamp(hold.until)},
        "team": {
            "today": 0 if team is None else team.today,
            "limit": settings.limit_team,
            "running": 0 if team is None else team.running,
        },
        "look": {"today": look_today, "limit": settings.limit_look},
        "second_look": {"today": second_look_today, "limit": settings.limit_second_look},
        "project_plan": {"today": project_plan_today, "limit": settings.limit_project_plan},
        "inbox": {"today": inbox_today, "limit": settings.limit_inbox_triage},
        "checks": None
        if check_today is None
        else {"today": check_today, "limit": settings.limit_check, "running": check_running},
    }


def _tokens(call: CallRow) -> int:
    """The call's measured input tokens, else its split's estimate (0 without a body)."""
    if call.input_tokens is not None:
        return call.input_tokens
    return 0 if call.body is None else sum(tokens for _part, tokens in call.body.split)


def _held_back_row(row: Mapping[str, object]) -> dict[str, Any]:
    """One `held_back` row with only the page's members; `reason` and `why` are cut to
    `MAX_TEXT`/`MAX_WHY` (`_client_safe`) -- the row's own text, read straight from storage and
    otherwise unchecked. `why` reads "" for a row that does not carry one yet (`stats.py`'s
    audit sample adds it; an older row without it still renders)."""
    built: dict[str, Any] = {name: row[name] for name in _HELD_BACK_MEMBERS if name != "why"}
    built["reason"] = _client_safe(cast(str, built["reason"]), MAX_TEXT)
    built["why"] = _client_safe(cast(str, row.get("why", "")), MAX_WHY)
    return built


def _recent_row(row: Mapping[str, object]) -> dict[str, Any]:
    """One `recent` row with only the page's members; `reason` and `why` are cut to `MAX_TEXT`/
    `MAX_WHY` (`_client_safe`)."""
    built: dict[str, Any] = {name: row[name] for name in _RECENT_MEMBERS}
    built["reason"] = _client_safe(cast(str, built["reason"]), MAX_TEXT)
    built["why"] = _client_safe(cast(str, built["why"]), MAX_WHY)
    return built


# ---- learning ----------------------------------------------------------------------------------

_LEARN_PLACES = 3
_REACH_LEVELS = tuple(arbiter.START_REACH)  # "list", "badge", "nudge", "alert"
_COST_LEVELS = tuple(arbiter.START_COSTS)  # "badge", "nudge", "alert"


def _round(value: float) -> float:
    return round(value, _LEARN_PLACES)


def _round_or_none(value: float | None) -> float | None:
    return None if value is None else _round(value)


def _calibration_view(calibration: Calibration) -> dict[str, Any]:
    return {
        "n": calibration.n,
        "brier": _round_or_none(calibration.brier),
        "bins": [
            {
                "low": _round(one.low),
                "high": _round(one.high),
                "n": one.n,
                "mean_p": _round_or_none(one.mean_p),
                "rate": _round_or_none(one.rate),
            }
            for one in calibration.bins
        ],
    }


def _trigger_rows(triggers: Sequence[TriggerYield], types: Sequence[str]) -> list[dict[str, Any]]:
    """One row per type in `types`, in that order, from the `triggers` that name it: zeros and
    false for a type none names."""
    by_type = {one.look: one for one in triggers}
    return [
        {
            "candidate": look,
            "made": 0 if (one := by_type.get(look)) is None else one.made,
            "accepted": 0 if one is None else one.accepted,
            "rejected": 0 if one is None else one.rejected,
            "nothing": 0 if one is None else one.nothing,
            "reasons": {
                reason: 0 if one is None else one.reasons.get(reason, 0)
                for reason in FEEDBACK_REASONS
            },
            "paused": False if one is None else one.paused,
        }
        for look in types
    ]


def _second_looks_view(yields: SecondLookYields | None) -> dict[str, Any]:
    """The learning block's `second_looks`, as `learning_view` describes it."""
    kinds = {} if yields is None else {one.kind: one for one in yields.kinds}
    return {
        "triggers": _trigger_rows(() if yields is None else yields.triggers, SECOND_LOOKS),
        "kinds": [
            {
                "kind": kind,
                "rejected_in_a_row": 0
                if (one := kinds.get(kind)) is None
                else one.rejected_in_a_row,
                "paused": False if one is None else one.paused,
            }
            for kind in FINDING_KINDS
        ],
    }


def learning_view(
    settings: DecisionSettings,
    learned: Learned | None,
    *,
    labels: int,
    since: datetime | None,
    lam: float,
    calibration: Calibration,
    questions: Sequence[QuestionRate],
    shadow: ShadowStats,
    second_looks: SecondLookYields | None = None,
    project_plans: SecondLookYields | None = None,
    message_drafts: SecondLookYields | None = None,
    saved_checks: SecondLookYields | None = None,
    inbox_triage: SecondLookYields | None = None,
) -> dict[str, Any]:
    """The page's `learning` block.

    While `settings.learning` is off, or with no snapshot (`learned` None: nothing fitted yet,
    or an unreadable one), every `now` reads as its start value, `fitted_at` is null and every
    `support` count is 0 -- the numbers actually steering `engine.py`'s cycles. Otherwise `now`
    and `support` come from `learned`: weights in `arbiter.FEATURES` order, topics in
    `CANDIDATES` order, reach `list` to `alert`, costs `badge` to `alert`. `calibration`,
    `questions` and `shadow` render as given regardless of `settings.learning`: they describe
    what happened, not what is deciding now. Every float is rounded to 3 places.
    `second_looks` lists one row per `SECOND_LOOKS` type and one per `FINDING_KINDS` kind, in
    that order, each reading zeros and false while `second_looks` has no row for it (None
    included); a trigger's `reasons` count each of `FEEDBACK_REASONS`. `project_plans` lists one
    such row per `PROJECT_PLANS` type, from the triggers of `project_plans`, in that order;
    `message_drafts` lists one per `MESSAGE_DRAFTS` type, from its triggers, in that order;
    `saved_checks` does the same for `SAVED_CHECKS`, and `inbox_triage` for `INBOX_TRIAGE`.
    """
    snapshot = learned if settings.learning else None
    if snapshot is None:
        weights: Mapping[str, float] = {}
        reach: Mapping[str, float] = {}
        costs: Mapping[str, float] = {}
        support: Mapping[str, int] = {}
        fitted_at = None
    else:
        weights = snapshot.weights
        reach = snapshot.reach
        costs = snapshot.costs
        support = snapshot.support
        fitted_at = _stamp(snapshot.fitted_at)
    return {
        "enabled": settings.learning,
        "labels": labels,
        "min_labels": MIN_LABELS,
        "fitted_at": fitted_at,
        "since": _stamp(since),
        "attention_price": _round(lam),
        "bias": {
            "start": _round(arbiter.START_WEIGHTS["bias"]),
            "now": _round(weights.get("bias", arbiter.START_WEIGHTS["bias"])),
        },
        "weights": [
            {
                "name": name,
                "label": playbook.LABELS[name],
                "start": _round(arbiter.START_WEIGHTS[name]),
                "now": _round(weights.get(name, arbiter.START_WEIGHTS[name])),
            }
            for name in arbiter.FEATURES
        ],
        "topics": [
            {
                "candidate": candidate,
                "start": 0.0,
                "now": _round(weights.get(f"topic.{candidate}", 0.0)),
                "support": support.get(f"topic.{candidate}", 0),
            }
            for candidate in CANDIDATES
        ],
        "reach": [
            {
                "level": level,
                "start": _round(arbiter.START_REACH[level]),
                "now": _round(reach.get(level, arbiter.START_REACH[level])),
                "support": support.get(f"reach.{level}", 0),
            }
            for level in _REACH_LEVELS
        ],
        "costs": [
            {
                "level": level,
                "start": _round(arbiter.START_COSTS[level]),
                "now": _round(costs.get(level, arbiter.START_COSTS[level])),
                "support": support.get(f"costs.{level}", 0),
            }
            for level in _COST_LEVELS
        ],
        "calibration": _calibration_view(calibration),
        "questions": [
            {
                "name": rate.name,
                "label": playbook.LABELS[rate.name],
                "yes_n": rate.yes_n,
                "yes_rate": _round_or_none(rate.yes_rate),
                "no_n": rate.no_n,
                "no_rate": _round_or_none(rate.no_rate),
            }
            for rate in questions
        ],
        "shadow": {
            "setting": shadow.setting,
            "decisions": shadow.decisions,
            "agree": shadow.agree,
            "kind_agree": shadow.kind_agree,
            "labels": shadow.labels,
            "brier": _round_or_none(shadow.brier),
            "live_brier": _round_or_none(shadow.live_brier),
        },
        "second_looks": _second_looks_view(second_looks),
        "project_plans": _trigger_rows(
            () if project_plans is None else project_plans.triggers, PROJECT_PLANS
        ),
        "message_drafts": _trigger_rows(
            () if message_drafts is None else message_drafts.triggers, MESSAGE_DRAFTS
        ),
        "saved_checks": _trigger_rows(
            () if saved_checks is None else saved_checks.triggers, SAVED_CHECKS
        ),
        "inbox_triage": _trigger_rows(
            () if inbox_triage is None else inbox_triage.triggers, INBOX_TRIAGE
        ),
    }


def page_view(
    settings: DecisionSettings,
    *,
    now: datetime,
    key_saved: bool,
    model_state: str,
    last_error: str | None,
    last_test: tuple[datetime, int] | None,
    checked_at: datetime | None,
    open_items: int,
    decisions_today: int,
    model_calls_today: int,
    tokens_today: int,
    spent_micro_usd: int,
    last_call: CallRow | None,
    held_back_today: int,
    muted: Collection[str],
    held_back: Sequence[Mapping[str, object]],
    recent: Sequence[Mapping[str, object]],
    counts: Mapping[str, int],
    running: Sequence[MissionRecord],
    prepare_today: int,
    run_today: int,
    episodes: int,
    oldest: str | None,
    learning: Mapping[str, Any],
    shadow_calls_today: int = 0,
    changes_today: int = 0,
    hold: Hold | None = None,
    team: SeatTeam | None = None,
    look_today: int = 0,
    second_look_today: int = 0,
    project_plan_today: int = 0,
    inbox_today: int = 0,
    message_draft_today: int = 0,
    check_today: int | None = None,
    check_running: int = 0,
) -> dict[str, Any]:
    """The Decision page at `now`.

    `paused_until` and `focus_until` are stamps while later than `now`, else "". The version
    shows the setting and the number in its model id ("" for latest and preview). `last_error`
    is a model error code, `last_test` (when, latency_ms) of the last test that answered.
    `today.cost_usd` is `spent_micro_usd` in dollars, `today.shadow_calls` is
    `shadow_calls_today`, and `today.capped` is true exactly when `model_state` is "capped".
    `last_call` is the newest call, with `_tokens`'s count. `muted` is listed in `CANDIDATES`
    order; `held_back` and `recent` rows keep only the page's members, the first `MAX_HELD_BACK`
    and `MAX_RECENT` of them; a count `counts` lacks is 0. `learning` is `learning_view`'s own
    output, embedded as given. `changes` is the vault-change proposals' own daily count
    (`changes_today`) and the setting's own limit (`settings.limit_changes`). `missions.hold` and
    `missions.team`, `missions.look`, `missions.second_look`, `missions.project_plan` and
    `missions.inbox` are `hold`, `team`, `look_today`, `second_look_today`, `project_plan_today`
    and `inbox_today`, as `missions_view` shows them. `message_drafts` is `message_draft_today`
    against `settings.limit_message_draft`. `missions.checks` is `check_today` and
    `check_running`, as `missions_view` shows them: null without a `check_today`.
    """
    return {
        "enabled": settings.enabled,
        "paused_until": _until(settings.paused_until, now) or "",
        "focus_until": _until(settings.focus_until, now) or "",
        "status": {"open": open_items, "checked_at": _stamp(checked_at)},
        "model": {
            "key": "saved" if key_saved else "missing",
            "version": {
                "setting": settings.model_version,
                "display": display_version(MODEL_IDS.get(settings.model_version)),
            },
            "state": model_state,
            "last_error": last_error,
            "last_test": None
            if last_test is None
            else {"at": stamp(last_test[0]), "latency_ms": last_test[1]},
        },
        "today": {
            "decisions": decisions_today,
            "model_calls": model_calls_today,
            "tokens": tokens_today,
            "cost_usd": spent_micro_usd / 1_000_000,
            "cap_usd": float(settings.daily_cap_usd),
            "capped": model_state == "capped",
            "shadow_calls": shadow_calls_today,
        },
        "privacy": {
            "last_call": None
            if last_call is None
            else {
                "call_id": last_call.call_id,
                "at": last_call.created_at,
                "tokens": _tokens(last_call),
            },
            "held_back_today": held_back_today,
        },
        "muted": [candidate for candidate in CANDIDATES if candidate in muted],
        "held_back": [_held_back_row(row) for row in held_back[:MAX_HELD_BACK]],
        "recent": [_recent_row(row) for row in recent[:MAX_RECENT]],
        "counts": {name: counts.get(name, 0) for name in _COUNTS},
        "missions": missions_view(
            running,
            settings,
            prepare_today=prepare_today,
            run_today=run_today,
            hold=hold,
            team=team,
            look_today=look_today,
            second_look_today=second_look_today,
            project_plan_today=project_plan_today,
            inbox_today=inbox_today,
            check_today=check_today,
            check_running=check_running,
        ),
        "changes": {"today": changes_today, "limit": settings.limit_changes},
        "message_drafts": {"today": message_draft_today, "limit": settings.limit_message_draft},
        "data": {
            "episodes": episodes,
            "oldest": oldest,
            "max": MAX_EPISODES,
            "retention_days": RETENTION_DAYS,
        },
        "learning": dict(learning),
    }


def calls_view(
    settings: DecisionSettings, calls: Sequence[CallRow], *, latest: str | None
) -> dict[str, Any]:
    """The Context tab's call list: whether Decision is on, how the latest decision was made
    (one of `POLICIES`, or None before the first) and the first `MAX_CALLS` of `calls` whose body
    opened, in the order given (the agenda's puts the newest first). `title` is cut to
    `MAX_TEXT` (`_client_safe`)."""
    readable = [(call, body) for call in calls if (body := call.body) is not None]
    return {
        "enabled": settings.enabled,
        "latest": latest,
        "calls": [
            {
                "call_id": call.call_id,
                "at": call.created_at,
                "title": _client_safe(body.title, MAX_TEXT),
                "candidate": body.candidate,
                "tokens": _tokens(call),
                "session_id": call.session_id,
            }
            for call, body in readable[:MAX_CALLS]
        ],
    }


def data_summary_view(
    parts: Mapping[str, Any], *, now: datetime, key_saved: bool, key_changed_at: str | None
) -> dict[str, Any]:
    """Stored data counts and bounded activity, with the key's saved state from settings."""
    return {
        **parts,
        "as_of": stamp(now),
        "today": now.date().isoformat(),
        "key": {"saved": key_saved, "updated": key_changed_at},
        "days": list(parts["days"][:MAX_DATA_DAYS]),
        "topics": list(parts["topics"][: len(TOPICS)]),
    }


def data_records_view(kind: str, records: Mapping[str, Any]) -> dict[str, Any]:
    """One page of decision or suggestion rows in the store's keyset order."""
    rows = records["rows"][:MAX_DATA_ROWS]
    if kind == "suggestions":
        rows = [{**row, "title": _client_safe(row["title"], MAX_TEXT)} for row in rows]
    return {
        "kind": kind,
        "total": records["total"],
        "deletable": records["deletable"],
        "next": records["next"],
        "rows": rows,
    }


def data_calls_view(calls: Sequence[DataCallRow]) -> dict[str, Any]:
    """Stored calls, including rows whose sealed body could not open."""
    listed = calls[:MAX_CALLS]
    return {
        "kind": "calls",
        "total": len(listed),
        "deletable": len(listed),
        "next": None,
        "rows": [
            {
                "id": call.call_id,
                "at": call.created_at,
                "title": "" if call.body is None else _client_safe(call.body.title, MAX_TEXT),
                "topic": None if call.body is None else call.body.candidate,
                "outcome": call.outcome,
                "tokens": _tokens(call),
                "cost_usd": (call.cost_micro_usd or 0) / 1e6,
            }
            for call in listed
        ],
    }


def data_decision_view(record: Mapping[str, Any]) -> dict[str, Any]:
    """One stored decision and its current linked suggestion text, when available."""
    return {
        **record,
        "kind": "decisions",
        "why": _client_safe(record["why"], MAX_WHY),
        "evidence": [_evidence(label) for label in record["evidence"][:MAX_DATA_EVIDENCE]],
    }


def data_suggestion_view(detail: Mapping[str, Any]) -> dict[str, Any]:
    """One stored suggestion with bounded display text and evidence."""
    return {
        **detail,
        "kind": "suggestions",
        "title": _client_safe(detail["title"], MAX_TEXT),
        "what": _client_safe(detail["what"], MAX_WHAT),
        "why": _client_safe(detail["why"], MAX_WHY),
        "evidence": [_evidence(label) for label in detail["evidence"][:MAX_DATA_EVIDENCE]],
    }


def data_deleted_view(deleted: int, kept: int) -> dict[str, int]:
    return {"deleted": deleted, "kept": kept}


def call_detail_view(
    call: CallRecord, *, questions: Sequence[tuple[str, str, str]], largest_question: int
) -> dict[str, Any]:
    """One call for the Context tab. `questions` are (name, label, answer text) in playbook
    order and `largest_question` the estimated tokens of the state with its largest question.
    A call that measured nothing shows 0 ms, $0 and its split's estimate as the request. Every
    free-text member is cut to its own parse.ts bound (`_client_safe`): `title`,
    `playbook_version`, a split part and a question's name/answer at `MAX_TEXT`, a question's
    label at `MAX_LABEL`, `questions_text` at `MAX_QUESTIONS_TEXT`, and `state_text` at
    `MAX_STATE_TEXT` -- the one field that keeps its own newlines and tabs, since the Context
    tab renders it in a `<pre>`."""
    body = call.body
    return {
        "call_id": call.call_id,
        "at": call.created_at,
        "title": _client_safe(body.title, MAX_TEXT),
        "candidate": body.candidate,
        "session_id": call.session_id,
        "version": display_version(call.model),
        "version_setting": body.version_setting,
        "latency_ms": call.latency_ms or 0,
        "cost_usd": (call.cost_micro_usd or 0) / 1_000_000,
        "long_context": False,
        "request": {"used": _tokens(call), "max": REQUEST_LIMIT},
        "largest_question": {"used": largest_question, "max": QUESTION_LIMIT},
        "split": [
            {"part": _client_safe(part, MAX_TEXT), "tokens": tokens} for part, tokens in body.split
        ],
        "questions": [
            {
                "name": _client_safe(name, MAX_TEXT),
                "label": _client_safe(label, MAX_LABEL),
                "answer": _client_safe(answer, MAX_TEXT),
            }
            for name, label, answer in questions
        ],
        "playbook_version": _client_safe(body.playbook_version, MAX_TEXT),
        "state_text": _client_safe(body.state_text, MAX_STATE_TEXT, allow_newline_tab=True),
        "questions_text": _client_safe(body.questions_text, MAX_QUESTIONS_TEXT),
        "held_back": body.held_back,
        "outcome": call.outcome,
        "failure": call.failure,
    }


def test_view(result: ModelReply | ModelError) -> dict[str, Any]:
    """A key test's result: the reply's latency and version, or the failure's code. Tests call it
    as `wire.test_view`: pytest collects a bare `test_*` name that a test module imports."""
    if isinstance(result, ModelError):
        return {"ok": False, "latency_ms": None, "version": None, "error": result.code}
    return {
        "ok": True,
        "latency_ms": result.latency_ms,
        "version": display_version(result.model),
        "error": None,
    }


# ---- question proposals ------------------------------------------------------------------------


def _wording_text(wording: Mapping[str, object]) -> str:
    """A question's wording as its inspector shows it: JSON with sorted keys and a two-space
    indent, text outside ASCII kept as written, cut to `MAX_PROPOSAL_TEXT` keeping its newlines
    (`_client_safe`)."""
    pretty = json.dumps(wording, indent=2, ensure_ascii=False, sort_keys=True)
    return _client_safe(pretty, MAX_PROPOSAL_TEXT, allow_newline_tab=True)


def _trial_rate(rate: QuestionRate) -> dict[str, Any]:
    return {
        "yes_n": rate.yes_n,
        "yes_rate": _round_or_none(rate.yes_rate),
        "no_n": rate.no_n,
        "no_rate": _round_or_none(rate.no_rate),
    }


def question_proposal_view(
    proposal: Mapping[str, Any],
    *,
    current: Mapping[str, object],
    proposed: Mapping[str, object],
    tried: TrialStats,
) -> dict[str, Any]:
    """One question proposal for its inspector (GET proposals/{id}). `proposal` is its Approvals
    inbox item, the shape the client's `isQuestionProposalItem` takes, embedded as given; `label`
    is its question's `playbook.LABELS` entry. `current` is the wording in use for that question
    and `proposed` the proposed one, each shown as `_wording_text`. `tried` compares them over
    the decisions the proposed wording was tried on: `TrialStats` without its name, each rate
    rounded to 3 places."""
    return {
        "proposal": dict(proposal),
        "label": playbook.LABELS[proposal["name"]],
        "current_text": _wording_text(current),
        "proposed_text": _wording_text(proposed),
        "tried": {
            "decisions": tried.decisions,
            "agree": tried.agree,
            "labels": tried.labels,
            "live": _trial_rate(tried.live),
            "trial": _trial_rate(tried.trial),
        },
    }


# ---- change proposals ---------------------------------------------------------------------------


def change_act_view(item: Mapping[str, object], draft: str | None) -> dict[str, Any]:
    """POST changes/{proposal_id}'s result: `item` is the acted change's Approvals inbox item,
    the shape the client's `isChangeProposalItem` takes (`changes.ChangeProposals.item`'s own
    output), embedded as given -- never built from `changes.py` here, since that module imports
    `missions.py`, which imports this one. `draft` is the chat message an approval answers, cut
    to `MAX_DRAFT_TEXT` keeping its own newlines and tabs like a draft action's text
    (`_client_safe`), or null after a rejection."""
    return {
        "change": dict(item),
        "draft": None
        if draft is None
        else _client_safe(draft, MAX_DRAFT_TEXT, allow_newline_tab=True),
    }


# ---- parsers -----------------------------------------------------------------------------------


def _refuse() -> NoReturn:
    raise DecisionLoopError("invalid_request")


def _object(value: object, keys: frozenset[str]) -> dict[str, Any]:
    """`value` as a JSON object with exactly `keys`."""
    if not isinstance(value, dict) or set(value) != keys:
        _refuse()
    return value


def _flag(value: object) -> bool:
    if not isinstance(value, bool):
        _refuse()
    return value


def _choice(value: object, allowed: tuple[str, ...]) -> str:
    if not isinstance(value, str) or value not in allowed:
        _refuse()
    return value


def _uuid(value: object) -> str:
    """`value` as a canonical UUID: lower-case hex in the hyphenated 8-4-4-4-12 form."""
    if isinstance(value, str) and len(value) == 36:
        try:
            if str(uuid.UUID(value)) == value:
                return value
        except ValueError:
            pass
    _refuse()


def _item_key(value: object) -> str:
    """`value` as an item key: 64 lower-case hex characters."""
    if not isinstance(value, str) or _ITEM_KEY.fullmatch(value) is None:
        _refuse()
    return value


def _text(value: object, low: int, high: int) -> str:
    """`value` as text of `low` to `high` characters, none a control character or a lone
    surrogate (which UTF-8 cannot carry)."""
    if (
        not isinstance(value, str)
        or not low <= len(value) <= high
        or any(unicodedata.category(ch) in ("Cc", "Cs") for ch in value)
    ):
        _refuse()
    return value


def _count(value: object, limit: int) -> int:
    """`value` as a JSON integer from 0 to `limit`; a bool is not one."""
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= limit:
        _refuse()
    return value


def _instant(value: object) -> datetime:
    """A stamp as an aware UTC instant. A stamp without milliseconds reads as `.000`; an
    impossible date or time, hour 24 included, fails `parse_stamp`'s round trip."""
    if not isinstance(value, str) or _STAMP.fullmatch(value) is None:
        _refuse()
    try:
        return parse_stamp(value if len(value) == 24 else value[:-1] + ".000Z")
    except ValueError:
        raise DecisionLoopError("invalid_request") from None


_DATA_KEYS = frozenset(
    {"kind", "ids", "topic", "shown", "answer", "state", "outcome", "since", "until", "before"}
)


def parse_data_query(raw: object, *, delete: bool) -> DataQuery:
    """Parse an exact data list or delete body before it reaches any store query."""
    body = _object(raw, _DATA_KEYS)
    kind = _choice(body["kind"], DATA_KINDS)
    allowed = {
        "decisions": frozenset({"topic", "shown", "answer", "since", "until", "before"}),
        "suggestions": frozenset({"topic", "state", "since", "until", "before"}),
        "calls": frozenset({"outcome"}),
    }[kind]
    if any(body[key] is not None for key in _DATA_KEYS - allowed - {"kind", "ids"}):
        _refuse()
    if delete and body["before"] is not None:
        _refuse()
    if not delete and body["ids"] is not None:
        _refuse()

    topic_vocab = TOPICS if kind == "decisions" else CANDIDATES
    topic = None if body["topic"] is None else _choice(body["topic"], topic_vocab)
    shown = None if body["shown"] is None else _choice(body["shown"], DATA_SHOWN)
    answer = None if body["answer"] is None else _choice(body["answer"], DATA_ANSWERS)
    state = None if body["state"] is None else _choice(body["state"], ITEM_STATES)
    outcome = None if body["outcome"] is None else _choice(body["outcome"], CALL_OUTCOMES)
    since = None if body["since"] is None else _instant(body["since"])
    until = None if body["until"] is None else _instant(body["until"])
    if since is not None and until is not None and since >= until:
        _refuse()

    before = None
    if body["before"] is not None:
        cursor = _object(body["before"], frozenset({"at", "id"}))
        cursor_id = _item_key(cursor["id"]) if kind == "suggestions" else _uuid(cursor["id"])
        before = (stamp(_instant(cursor["at"])), cursor_id)

    ids = None
    if body["ids"] is not None:
        raw_ids = body["ids"]
        if not isinstance(raw_ids, list) or not 1 <= len(raw_ids) <= MAX_DATA_IDS:
            _refuse()
        parse_id = _item_key if kind == "suggestions" else _uuid
        ids = tuple(parse_id(value) for value in raw_ids)
        if len(set(ids)) != len(ids):
            _refuse()
        if any(value is not None for value in (topic, shown, answer, state, outcome, since, until)):
            _refuse()
    elif delete:
        if (
            kind == "calls"
            or until is None
            or all(value is None for value in (topic, shown, answer, state, since))
        ):
            _refuse()
    return DataQuery(kind, ids, topic, shown, answer, state, outcome, since, until, before)


def parse_data_record(raw: object) -> tuple[str, str]:
    body = _object(raw, frozenset({"kind", "id"}))
    kind = _choice(body["kind"], ("decisions", "suggestions"))
    return kind, _item_key(body["id"]) if kind == "suggestions" else _uuid(body["id"])


def _skill_name(value: object) -> str:
    """`value` as a skill name: `native_assets.SKILL_NAME` in at most `_SKILL_NAME_CHARS`
    characters."""
    if (
        not isinstance(value, str)
        or len(value) > _SKILL_NAME_CHARS
        or SKILL_NAME.fullmatch(value) is None
    ):
        _refuse()
    return value


def parse_page(raw: object) -> PageFacts:
    """The page's own counts in a presence body, checked against their shape exactly: the key
    sets, real ints and bools, the ranges and the stamp pattern. `skill`, when not null, is the
    chat in view's skill hint: `trigger` one of `SKILL_TRIGGERS`, 1 to `MAX_SKILL_SESSIONS`
    distinct canonical UUIDs in `sessions`, and `titles` matching it one for one, each 0 to
    `MAX_CHAT_TITLE` characters without control characters (as `chat_title` below). `suggest`,
    when not null, is the chat in view's facts for a skill suggestion (`improve.skill_suggest`):
    `session` a canonical UUID, `title` 1 to `MAX_CHAT_TITLE` characters without control
    characters, and `used` 0 to `MAX_SUGGEST_USED` distinct skill names (`_skill_name`).
    `second_look`, when not null, is the chat in view's finished turn worth a second look:
    `session` and `turn` canonical UUIDs, `trigger` one of `SECOND_LOOK_TYPES`, `title` 0 to
    `MAX_CHAT_TITLE` characters without control characters, and `refs` 1 to
    `MAX_SECOND_LOOK_REFS` distinct note refs of 1 to `MAX_NOTE_REF` characters, together at most
    `MAX_SECOND_LOOK_REF_BYTES` UTF-8 bytes. Whether a ref is a note of the vault is the
    detector's to judge. `context`, when not null, is the chat in view's context share: `session`
    a canonical UUID and `percent` a whole number from 0 to 100. `sources`, when not null, is
    the chat in view's last reply that fetched web pages: `session` and `turn` canonical UUIDs
    and `notes` a bool, whether that reply read a vault note."""
    page = _object(raw, _PAGE_KEYS)
    runs = _object(page["runs"], _RUNS_KEYS)
    git = _object(page["git"], _GIT_KEYS)
    routine_id: str | None = None
    next_run: datetime | None = None
    paused: bool | None = None
    if page["routine"] is not None:
        routine = _object(page["routine"], _ROUTINE_KEYS)
        routine_id = _uuid(routine["routine_id"])
        next_run = _instant(routine["next_run"])
        paused = _flag(routine["paused"])
    skill: SkillHint | None = None
    if page["skill"] is not None:
        skill_body = _object(page["skill"], _SKILL_KEYS)
        sessions = skill_body["sessions"]
        if not isinstance(sessions, list) or not 1 <= len(sessions) <= MAX_SKILL_SESSIONS:
            _refuse()
        session_ids = tuple(_uuid(value) for value in sessions)
        if len(set(session_ids)) != len(session_ids):
            _refuse()
        titles = skill_body["titles"]
        if not isinstance(titles, list) or len(titles) != len(session_ids):
            _refuse()
        skill = SkillHint(
            trigger=_choice(skill_body["trigger"], SKILL_TRIGGERS),
            sessions=session_ids,
            titles=tuple(_text(value, 0, MAX_CHAT_TITLE) for value in titles),
        )
    suggest: SuggestHint | None = None
    if page["suggest"] is not None:
        suggest_body = _object(page["suggest"], _SUGGEST_KEYS)
        used = suggest_body["used"]
        if not isinstance(used, list) or len(used) > MAX_SUGGEST_USED:
            _refuse()
        names = tuple(_skill_name(value) for value in used)
        if len(set(names)) != len(names):
            _refuse()
        suggest = SuggestHint(
            session=_uuid(suggest_body["session"]),
            title=_text(suggest_body["title"], 1, MAX_CHAT_TITLE),
            used=names,
        )
    second_look: SecondLookHint | None = None
    if page["second_look"] is not None:
        look_body = _object(page["second_look"], _SECOND_LOOK_KEYS)
        refs = look_body["refs"]
        if not isinstance(refs, list) or not 1 <= len(refs) <= MAX_SECOND_LOOK_REFS:
            _refuse()
        notes = tuple(_text(value, 1, MAX_NOTE_REF) for value in refs)
        if (
            len(set(notes)) != len(notes)
            or sum(len(note.encode("utf-8")) for note in notes) > MAX_SECOND_LOOK_REF_BYTES
        ):
            _refuse()
        second_look = SecondLookHint(
            session=_uuid(look_body["session"]),
            turn=_uuid(look_body["turn"]),
            trigger=_choice(look_body["trigger"], tuple(SECOND_LOOK_TYPES)),
            title=_text(look_body["title"], 0, MAX_CHAT_TITLE),
            refs=notes,
        )
    context: ContextHint | None = None
    if page["context"] is not None:
        context_body = _object(page["context"], _CONTEXT_KEYS)
        context = ContextHint(
            session=_uuid(context_body["session"]), percent=_count(context_body["percent"], 100)
        )
    sources: SourceHint | None = None
    if page["sources"] is not None:
        sources_body = _object(page["sources"], _SOURCES_KEYS)
        sources = SourceHint(
            session=_uuid(sources_body["session"]),
            turn=_uuid(sources_body["turn"]),
            notes=_flag(sources_body["notes"]),
        )
    return PageFacts(
        native_waiting=_count(page["native_waiting"], MAX_PAGE_COUNT),
        runs_working=_count(runs["working"], MAX_PAGE_COUNT),
        runs_review=_count(runs["review"], MAX_PAGE_COUNT),
        git_state=_choice(git["state"], GIT_STATES),
        git_dirty=None if git["dirty"] is None else _count(git["dirty"], MAX_DIRTY),
        routine_id=routine_id,
        next_run=next_run,
        routines_paused=paused,
        skill=skill,
        suggest=suggest,
        second_look=second_look,
        context=context,
        sources=sources,
    )


def parse_presence(raw: object) -> tuple[str, Presence]:
    """A presence body: its window id and what that window reports. `chat` and the note's
    `workspace_id` are canonical UUIDs, `chat_title` at most `MAX_CHAT_TITLE` characters and the
    note's `ref` 1 to `MAX_NOTE_REF`, neither with control characters. `away` is False: one
    window's report cannot tell it."""
    body = _object(raw, _PRESENCE_KEYS)
    note_ref: tuple[str, str] | None = None
    if body["note"] is not None:
        note = _object(body["note"], _NOTE_KEYS)
        note_ref = (_uuid(note["workspace_id"]), _text(note["ref"], 1, MAX_NOTE_REF))
    title = body["chat_title"]
    return _uuid(body["window"]), Presence(
        focused=_flag(body["focused"]),
        typing=_flag(body["typing"]),
        away=False,
        chat_id=None if body["chat"] is None else _uuid(body["chat"]),
        chat_title=None if title is None else _text(title, 0, MAX_CHAT_TITLE),
        note_ref=note_ref,
        launcher_open=_flag(body["launcher"]),
        page=None if body["page"] is None else parse_page(body["page"]),
    )


def parse_feedback(raw: object) -> FeedbackRequest:
    """A feedback body. An item key takes a value from `FEEDBACK_SHOWN`, an episode id (a row
    held back) one from `FEEDBACK_HELD`; `reason`, from `FEEDBACK_REASONS`, comes only with
    "wrong"; `source` is one of `SOURCES`."""
    body = _object(raw, _FEEDBACK_KEYS)
    item_key: str | None = None
    episode_id: str | None = None
    if body["item_key"] is not None and body["episode_id"] is None:
        item_key, values = _item_key(body["item_key"]), FEEDBACK_SHOWN
    elif body["episode_id"] is not None and body["item_key"] is None:
        episode_id, values = _uuid(body["episode_id"]), FEEDBACK_HELD
    else:
        _refuse()
    value = _choice(body["value"], values)
    reason = None if body["reason"] is None else _choice(body["reason"], FEEDBACK_REASONS)
    if reason is not None and value != "wrong":
        _refuse()
    return FeedbackRequest(item_key, episode_id, value, reason, _choice(body["source"], SOURCES))


def parse_observe(raw: object) -> tuple[str, tuple[str, ...]]:
    """An observe body: the event, one of `OBSERVE_EVENTS`, and 1 to `MAX_OPEN_ITEMS` distinct
    item keys, the most the launcher lists."""
    body = _object(raw, _OBSERVE_KEYS)
    event = _choice(body["event"], OBSERVE_EVENTS)
    keys = body["item_keys"]
    if not isinstance(keys, list) or not 1 <= len(keys) <= MAX_OPEN_ITEMS:
        _refuse()
    item_keys = tuple(_item_key(key) for key in keys)
    if len(set(item_keys)) != len(item_keys):
        _refuse()
    return event, item_keys


def parse_report(raw: object) -> tuple[str, str]:
    """A report body (the user's "you should have told me"): the topic, one of `TOPICS`, and
    the source, one of `SOURCES`."""
    body = _object(raw, _REPORT_KEYS)
    return _choice(body["topic"], TOPICS), _choice(body["source"], SOURCES)


_NAME_ONLY_PREFERENCES = ("review_seen", "reset_learning")


def parse_preference(raw: object) -> tuple[str, str | None]:
    """A preference body: ("unmute", candidate) from `{name: "unmute", candidate}` with one of
    `CANDIDATES`; ("try_again", candidate) from `{name: "try_again", candidate}` with one of
    `SECOND_LOOKS`, `PROJECT_PLANS`, `MESSAGE_DRAFTS`, `SAVED_CHECKS` or `INBOX_TRIAGE`;
    ("raise_again", kind) from `{name: "raise_again", kind}` with one of `FINDING_KINDS`;
    or (name, None) from `{name}` alone, name being "review_seen" or "reset_learning"."""
    if isinstance(raw, dict) and raw.get("name") == "unmute":
        body = _object(raw, _UNMUTE_KEYS)
        return "unmute", _choice(body["candidate"], CANDIDATES)
    if isinstance(raw, dict) and raw.get("name") == "try_again":
        body = _object(raw, _UNMUTE_KEYS)
        return "try_again", _choice(
            body["candidate"],
            SECOND_LOOKS + PROJECT_PLANS + MESSAGE_DRAFTS + SAVED_CHECKS + INBOX_TRIAGE,
        )
    if isinstance(raw, dict) and raw.get("name") == "raise_again":
        body = _object(raw, _RAISE_KEYS)
        return "raise_again", _choice(body["kind"], FINDING_KINDS)
    body = _object(raw, _NAME_ONLY_KEYS)
    return _choice(body["name"], _NAME_ONLY_PREFERENCES), None


def parse_mission_start(raw: object) -> tuple[str, str]:
    """A mission start: an item key and the work, one of `MISSION_WORK`."""
    body = _object(raw, _MISSION_KEYS)
    return _item_key(body["item_key"]), _choice(body["work"], MISSION_WORK)


def parse_inbox_suggest(raw: object) -> str:
    """An inbox suggestion request: `{item_key}` alone, as the item key."""
    return _item_key(_object(raw, _INBOX_SUGGEST_KEYS)["item_key"])


def parse_proposal(raw: object) -> tuple[str, dict[str, Any]]:
    """A proposed wording: the question's name, 1 to 32 of the characters a-z and "_", and the
    wording, any JSON object. Whether that name may be reworded and that wording holds is
    `playbook.check_question`'s to judge."""
    body = _object(raw, _PROPOSAL_KEYS)
    name, question = body["name"], body["question"]
    if (
        not isinstance(name, str)
        or _QUESTION_NAME.fullmatch(name) is None
        or not isinstance(question, dict)
    ):
        _refuse()
    return name, question


def parse_proposal_act(raw: object) -> tuple[str, str, str | None]:
    """An act on a question proposal: the action, one of `PROPOSAL_ACTIONS`; the revision the
    user saw, 64 lower-case hex characters; and a reason, null or, with "reject" alone, one line
    of 1 to `MAX_PROPOSAL_REASON` UTF-16 units once stripped, with no control character, line or
    paragraph separator or lone surrogate anywhere in it. The reason is answered as sent."""
    body = _object(raw, _PROPOSAL_ACT_KEYS)
    action = _choice(body["action"], PROPOSAL_ACTIONS)
    revision, reason = body["revision"], body["reason"]
    if not isinstance(revision, str) or _REVISION.fullmatch(revision) is None:
        _refuse()
    if reason is not None and (
        action != "reject"
        or not isinstance(reason, str)
        or any(unicodedata.category(ch) in ("Cc", "Cs", "Zl", "Zp") for ch in reason)
        or not 1 <= _utf16_len(reason.strip()) <= MAX_PROPOSAL_REASON
    ):
        _refuse()
    return action, revision, reason


def parse_key(raw: object) -> str:
    """The key in a `{key}` body: 1 to 4,096 printable ASCII characters without spaces. A
    refusal is the code alone, so no error holds the key."""
    key = _object(raw, _KEY_KEYS)["key"]
    if not isinstance(key, str) or _KEY.fullmatch(key) is None:
        _refuse()
    return key


# ---- the shared examples -----------------------------------------------------------------------

_EXAMPLE_SCOPE = "315d31aa-25ed-45a7-a4b8-5c582ce841fb"
_EXAMPLE_KEY = "287012d26416912533449255733ff6202b794dcc9c0d7c5d825fba2494a8e906"
_EXAMPLE_EPISODE = "69a85af7-0708-4498-9e54-6920f9378cc0"
_EXAMPLE_CALL = "d2df3dad-403c-4e31-a5f0-1f07efb73934"


def _example_item(
    key: str,
    episode: str,
    candidate: str,
    kind: str,
    level: str,
    what: str,
    why: str,
    created_at: str,
    evidence: tuple[EvidenceLabel, ...],
    actions: tuple[ActionSpec, ...],
    mission_id: str | None = None,
) -> ItemRecord:
    return ItemRecord(
        item_key=key,
        episode_id=episode,
        candidate=candidate,
        kind=kind,
        level=level,
        state="open",
        timing="now",
        defer_until=None,
        created_at=created_at,
        shown_at=None,
        mission_id=mission_id,
        answers_json=None,
        state_hash=None,
        body=ItemBody(title=what, what=what, why=why, evidence=evidence, actions=actions),
    )


# Four self-improvement items' drafts, as templates.py words them: a skill for a repeated
# request, a source captured in the inbox, a retro, and better wording for the "news" question.
# The fifth, a skill suggestion's, is the skill's own "/sprint-retro".
_EXAMPLE_SKILL_DRAFT = "\n".join(
    (
        "Make this a skill. Draft a reusable Claude Code skill from the work in this conversation.",
        "Reply with exactly one fenced code block whose info string is SKILL.md, holding the whole "
        "file:",
        "Wrap it in a fence longer than any fence inside it (for example four backticks), so a "
        "bare inner fence cannot cut it short.",
        "- first the frontmatter between --- lines, with name (lowercase letters, digits and "
        "single hyphens, at most 64 characters) and description (one line of at most 1,536 "
        "characters: what it does and when to use it);",
        "- then the steps, written so that a new session can follow them without this "
        "conversation.",
        "If this work refines one of your existing skills, give that skill's whole updated file "
        "under its existing name.",
        "Do not create or change any file: VIKI Agent turns your block into a proposal that I "
        "review in Approvals.",
        "Why now: I started several chats with a similar first message.",
    )
)
_EXAMPLE_LEARN_DRAFT = "\n".join(
    (
        "Learn the sources I captured in these notes:",
        "- 00-inbox/seedling-care.md",
        "Use this vault's own learn skill if it has one. Show me each literature note before you "
        "write it.",
    )
)
_EXAMPLE_RETRO_DRAFT = (
    "Run a retro on my recent work with VIKI: I marked several of Decision's items Not useful "
    "lately. Use this vault's own retro skill if it has one. Ask me before you change any "
    "memory, skill or instruction."
)

# The two message drafts, as templates.py words them, for the chat in view.
_EXAMPLE_DRAFT_CHAT = "2b4d6f8a-1c3e-4a5b-8d7f-9e0a1b2c3d4e"
_EXAMPLE_HANDOFF_WHY = (
    "VIKI can put a message in this chat's box asking for a handoff: the decisions, open "
    "questions and next steps. Send it, then choose New chat with this turn on the reply to "
    "carry on in a fresh chat."
)
_EXAMPLE_HANDOFF_DRAFT = (
    "This chat's context is nearly full. Write a handoff I can take to a fresh chat: the "
    "decisions we made, the open questions and the next steps, each as a short list that names "
    "the files and notes it concerns. Reply with the handoff alone, and do not create or change "
    "any file."
)
_EXAMPLE_CHECK_FOLDER = "e84e8a26-0fb1-43b5-8e8b-57f98ba18a62"
_EXAMPLE_CHECK_FOLDER_RUNNING = "5b9f2c71-3e8a-4d6b-9c0e-7a1f4b2d8e63"
_EXAMPLE_SOURCE_NOTE_WHY = (
    "VIKI can put a message in this chat's box that asks for a literature note for each page "
    "and asks to see each note before it is written."
)
_EXAMPLE_SOURCE_NOTE_DRAFT = " ".join(
    (
        "Write a literature note in this vault for each web page you fetched in your last reply.",
        "Use this vault's own learn skill if it has one.",
        "Your last reply read no note in this vault, so first search it for notes on the same "
        "subject and link the ones you find from each new note.",
        "Show me each literature note and wait for my go-ahead before you write it.",
    )
)


def _example_tune_draft() -> str:
    return "\n".join(
        (
            'Decision\'s question "New to you?" (news) could use better wording.',
            "Its current wording:",
            "```json",
            canonical_json(playbook.questions()["news"]),
            "```",
            "Reply with exactly one fenced code block whose info string is decision-question, "
            'holding one JSON object with two keys: "name" (this question\'s name) and "question" '
            "(the wording above, changed as needed).",
            "Keep the question's meaning and the last two sentences of its focus.",
            "Add not_for examples for what went wrong.",
            "Do not create or change any file: VIKI Agent turns your block into a proposal that I "
            "review in Approvals.",
        )
    )


def _example_items() -> tuple[ItemRecord, ...]:
    scope = _EXAMPLE_SCOPE
    garden = "10-projects/garden-planner/tasks.md"
    trip = "10-projects/trip-budget/tasks.md"
    planner = "fda2250d-d245-468a-9d7c-5fa673b14d3a"
    reporter = "23a50b40-2561-439f-bb6f-ff20189487ba"
    viewed = "fe07f4bf-5447-4a8d-9d58-b68385409b99"  # the chat in view: skill_offer's first session
    garden_note = "10-projects/garden/garden.md"
    garden_tasks = "10-projects/garden/tasks.md"
    return (
        _example_item(
            _EXAMPLE_KEY,
            _EXAMPLE_EPISODE,
            "tasks_due",
            "suggest",
            "badge",
            "Two garden planner tasks are due today",
            "due today; linked to one of your goals",
            "2026-10-21T14:02:00.000Z",
            (EvidenceLabel("Garden planner tasks", workspace_id=scope, ref=garden),),
            (
                ActionSpec("start_prepare", "Draft a plan", True),
                ActionSpec("open_note", "Open the tasks", False, workspace_id=scope, ref=garden),
                ActionSpec("open_context", "See what was sent", False, call_id=_EXAMPLE_CALL),
            ),
        ),
        _example_item(
            "30971fff12b29139a3f5ceb65c3e75cc65c7dbc25827b5d10b59620b3b6971b0",
            "3a4136ba-71a9-45d6-a3e1-84b57d339d7c",
            "approvals",
            "ask",
            "badge",
            "A chat is waiting for your permission",
            "needs your decision",
            "2026-10-21T13:58:00.000Z",
            (
                EvidenceLabel(
                    "Tidy the import script", session_id="5b12dcff-af5c-4940-a802-371d7dd2e0b2"
                ),
            ),
            (ActionSpec("open_approvals", "Open Approvals", True),),
        ),
        _example_item(
            "3653264fb97ed58523146a31c08244258c0a5899c63511bde3ec214c2ce5bfc7",
            "9f86bf99-cf37-4e69-b3ef-b4b557b6da17",
            "tasks_due",
            "inform",
            "list",
            "VIKI is drafting a plan for the trip budget",
            "due tomorrow",
            "2026-10-21T13:10:00.000Z",
            (EvidenceLabel("Trip budget tasks", workspace_id=scope, ref=trip),),
            (
                ActionSpec("open_chat", "Open chat", True, session_id=planner),
                ActionSpec(
                    "stop_mission", "Stop", False, mission_id="9273d0ed-e3e0-4ac6-8319-80e6c5e10247"
                ),
            ),
            "9273d0ed-e3e0-4ac6-8319-80e6c5e10247",
        ),
        _example_item(
            "5a62d41d228dfd669a202e4500ec85194b30c3239913826ebe1521518f74608b",
            "f7521e9b-e737-4e5a-81cb-824df327b7b1",
            "inbox",
            "suggest",
            "list",
            "Several notes are waiting in your inbox",
            "you haven't seen it lately",
            "2026-10-21T12:30:00.000Z",
            (
                EvidenceLabel(
                    "Reading list ideas", workspace_id=scope, ref="00-inbox/reading-list-ideas.md"
                ),
            ),
            (
                ActionSpec("start_run", "File them in a chat", True),
                ActionSpec("open_inbox", "Open the inbox", False),
            ),
        ),
        _example_item(
            "3290ae688f532954c14345a31bd0c1ef05c4f1d0a2fa3e14ff8258b2ac5477b8",
            "1723ba82-d0de-4b00-a7e5-5e6a221280ca",
            "agent_runs",
            "ready",
            "list",
            "An agent run is ready for review",
            "needs your decision",
            "2026-10-21T11:45:00.000Z",
            (EvidenceLabel("Agent runs"),),
            (ActionSpec("open_runs", "Open runs", True),),
        ),
        _example_item(
            "9df75fcb0ad10891f19d9d33cbabad07c0b7734f314fa575237e927497556422",
            "c8e9dc2e-0eea-4cef-b33e-17d7ca337ecb",
            "mission_report",
            "report",
            "list",
            "VIKI finished a plan for the reading list",
            "you haven't seen it lately",
            "2026-10-21T11:20:00.000Z",
            (EvidenceLabel("Started by VIKI", session_id=reporter),),
            (ActionSpec("open_chat", "Open the chat", True, session_id=reporter),),
            "ce32ea75-840d-417a-8478-6445a8956798",
        ),
        _example_item(
            "21aedbd03ab9eec1194976e584babb2fd2735db85803395ef47b91485f130ac2",
            "1664387f-25e5-4a91-b7d6-45832363c17e",
            "routine",
            "inform",
            "list",
            "The weekly review routine runs tonight",
            "a routine runs soon",
            "2026-10-21T09:00:00.000Z",
            (),
            (
                ActionSpec(
                    "open_routine",
                    "Open the routine",
                    True,
                    routine_id="771411e1-348a-4e26-bd6b-385fd3b89284",
                ),
            ),
        ),
        _example_item(
            "8229925b2e9c069a1fd36de6161efea2e693356cc15c87d76b586fc3181da0b3",
            "cc6d44c5-1c71-42a0-854f-941dc18f977b",
            "skill_offer",
            "suggest",
            "list",
            "You keep starting chats like this one; a skill could save repeating it.",
            "This work may be worth keeping as a skill.",
            "2026-10-21T08:50:00.000Z",
            (
                EvidenceLabel("Water the herbs", session_id="8387dec2-0e72-4e24-a7e5-e4f64a28d23d"),
                EvidenceLabel(
                    "Water the tomatoes", session_id="9bb86c11-2130-46f7-a84b-030ca9993745"
                ),
            ),
            (
                ActionSpec(
                    "draft_chat",
                    "Make this a skill",
                    True,
                    session_id=viewed,
                    text=_EXAMPLE_SKILL_DRAFT,
                ),
            ),
        ),
        _example_item(
            "9f8e7d6c5b4a3f2e1d0c9b8a7f6e5d4c3b2a1f0e9d8c7b6a5f4e3d2c1b0a9f8e",
            "3c2b1a09-f8e7-d6c5-b4a3-f2e1d0c9b8a7",
            "skill_suggest",
            "suggest",
            "list",
            "One of your own skills may help with this chat.",
            "This chat's title matches one of your own skills.",
            "2026-10-21T08:45:00.000Z",
            (EvidenceLabel("sprint-retro"),),
            (
                ActionSpec(
                    "draft_chat", "Use this skill", True, session_id=viewed, text="/sprint-retro"
                ),
            ),
        ),
        _example_item(
            "1cea2f2f18d4db2a78fd473d2f55a2670ef6814b891315695b857dd783056a8f",
            "aebd30b0-d1b8-4239-900f-ce39529d590b",
            "learn_source",
            "suggest",
            "list",
            "A link you captured may be worth a literature note.",
            "you haven't seen it lately",
            "2026-10-21T08:40:00.000Z",
            (EvidenceLabel("seedling-care", workspace_id=scope, ref="00-inbox/seedling-care.md"),),
            (ActionSpec("draft_new", "Learn these sources", True, text=_EXAMPLE_LEARN_DRAFT),),
        ),
        _example_item(
            "1d0877d374c88394259143272b00d68e5b9808acaf744515868030ee286f16d7",
            "abbf8d77-d7b9-4b01-9dcb-9312e5122621",
            "retro_due",
            "suggest",
            "list",
            "You marked several items Not useful lately; a retro could turn them into a lesson.",
            "Several items were marked Not useful lately.",
            "2026-10-21T08:20:00.000Z",
            (),
            (ActionSpec("draft_new", "Start a retro", True, text=_EXAMPLE_RETRO_DRAFT),),
        ),
        _example_item(
            "f06ddac2ede7ea95e9ae691aeafba4f861a011e7f4a678e4f1f46817071a08bf",
            "4990b7e5-3c81-459a-b666-7a06fe1b9050",
            "question_tune",
            "suggest",
            "list",
            "One of Decision's questions isn't matching your answers: New to you?",
            "A question's answers stopped matching yours.",
            "2026-10-21T08:10:00.000Z",
            (),
            (ActionSpec("draft_new", "Draft better wording", True, text=_example_tune_draft()),),
        ),
        _example_item(
            "b007a61fbd413be041489d689612a430007e420d714838b32bd6e8a3b16c21ee",
            "43beab08-e524-4b12-9c79-e1c7556b15a2",
            "card_needs_you",
            "ask",
            "list",
            "A card needs your decision.",
            "Research blocked it yesterday.",
            "2026-10-21T08:05:00.000Z",
            (
                EvidenceLabel(
                    "Find three sources", workspace_id=scope, ref="kanban/find-sources.md"
                ),
            ),
            (
                ActionSpec(
                    "open_note", "Open card", True, workspace_id=scope, ref="kanban/find-sources.md"
                ),
            ),
        ),
        _example_item(
            "37507e1ede452687a2ad18820a461d9afc8c69059eba5def31b3aa6c0a56b2a0",
            "28a1ed50-ccd1-4a17-8ba7-5539c5d9e697",
            "card_stale",
            "report",
            "list",
            "A card has stopped moving.",
            "Waiting for research, last changed last week.",
            "2026-10-21T08:00:00.000Z",
            (
                EvidenceLabel(
                    "Draft the outline", workspace_id=scope, ref="kanban/draft-outline.md"
                ),
            ),
            (
                ActionSpec(
                    "open_note",
                    "Open card",
                    True,
                    workspace_id=scope,
                    ref="kanban/draft-outline.md",
                ),
            ),
        ),
        _example_item(
            item_key("seat_waiting", "810c0315-9fba-4b49-915a-916cb928ff50"),
            "810c0315-9fba-4b49-915a-916cb928ff50",
            "seat_waiting",
            "ask",
            "list",
            "Research needs your OK to continue.",
            "Its run waits until you answer or it reaches its time limit.",
            "2026-10-21T09:40:00.000Z",
            (),
            (
                ActionSpec(
                    "open_chat",
                    "Open session",
                    True,
                    session_id="c5f6d7a7-d863-4007-a9a7-690ab332ea16",
                ),
            ),
        ),
        _example_item(
            item_key("seat_working", "dc943074-c6e5-487e-bed9-b6556ac4cdc7"),
            "dc943074-c6e5-487e-bed9-b6556ac4cdc7",
            "seat_working",
            "inform",
            "list",
            "Ops is working on a card.",
            "VIKI started it for a card waiting for its seat.",
            "2026-10-21T09:12:00.000Z",
            (),
            (
                ActionSpec(
                    "open_chat",
                    "Open session",
                    True,
                    session_id="5f0d08ce-8535-4a92-a37e-6c1e8814558b",
                ),
                ActionSpec(
                    "stop_seat", "Stop", False, routine_id="802cfd61-3418-4373-8d49-3f5e5b019d92"
                ),
            ),
        ),
        _example_item(
            item_key("seat_start", "e581b965-797c-4dbf-8ae3-2ee3dc143c6a"),
            "e581b965-797c-4dbf-8ae3-2ee3dc143c6a",
            "seat_start",
            "suggest",
            "list",
            "Writing could start on a waiting card.",
            "Start Bots' work is set to Ask first.",
            "2026-10-21T08:30:00.000Z",
            (),
            (
                ActionSpec(
                    "start_seat", "Start", True, routine_id="802cfd61-3418-4373-8d49-3f5e5b019d92"
                ),
            ),
        ),
        _example_item(
            item_key("seat_report", "221e6977-e369-47db-b5ea-7f560ad13f34"),
            "221e6977-e369-47db-b5ea-7f560ad13f34",
            "seat_report",
            "report",
            "list",
            "Review ran without moving its cards.",
            "VIKI starts it again once you add or change one of its todo cards.",
            "2026-10-21T08:20:00.000Z",
            (),
            (
                ActionSpec(
                    "open_chat",
                    "Open session",
                    True,
                    session_id="6805019e-faa4-4f93-a5ee-815f870aa922",
                ),
            ),
        ),
        _example_item(
            item_key("vault_recovery", scope, "stopped", "cf6b9885-3b15-4cd5-9b86-2f28ff16add2"),
            "1de5b4ad-8395-4fab-a743-b5a0ae67db24",
            "vault_recovery",
            "report",
            "list",
            "A save to this vault stopped partway through.",
            "Open Recovery in the vault view before you save that note again.",
            "2026-10-21T07:55:00.000Z",
            (),
            (ActionSpec("open_vault", "Open vault", True, workspace_id=scope),),
        ),
        _example_item(
            item_key("memory_drift", scope, "add3b51b-1627-4d3c-b7d2-c8c5379931eb"),
            "4ab83be4-38b0-4c4d-972b-336751a5329d",
            "memory_drift",
            "ask",
            "list",
            "A memory cites a note that changed after you approved it.",
            "Open Memory in the vault view, and Correct or Retire any memory that no longer holds.",
            "2026-10-21T07:45:00.000Z",
            (),
            (ActionSpec("open_vault", "Open vault", True, workspace_id=scope),),
        ),
        _example_item(
            item_key("bridge_stopped", "slack", "connection_lost", "2026-10-21"),
            "f4a3be66-850e-49de-8814-67d91c3c09dd",
            "bridge_stopped",
            "report",
            "list",
            "The Slack bridge stopped.",
            "Turn it on again in Messaging.",
            "2026-10-21T07:40:00.000Z",
            (),
            (ActionSpec("open_messaging", "Open Messaging", True),),
        ),
        _example_item(
            item_key(
                "chat_waiting", "fd068991-5bf6-49c5-9e7e-d81128425e94", "toolu_example_waiting"
            ),
            "76cff2ed-bff5-4862-b0c8-e72ac0e10df9",
            "chat_waiting",
            "ask",
            "list",
            "A chat has waited over an hour for your OK.",
            "Claude Code pauses that chat until you answer.",
            "2026-10-21T07:35:00.000Z",
            (),
            (
                ActionSpec(
                    "open_chat",
                    "Open chat",
                    True,
                    session_id="fd068991-5bf6-49c5-9e7e-d81128425e94",
                ),
            ),
        ),
        _example_item(
            item_key("send_uncertain", "35ab6ade-f70a-4bb2-8710-28be18b29b56"),
            "58048629-48ce-44e5-9933-03e9257b31a2",
            "send_uncertain",
            "report",
            "list",
            "A message you sent may not have reached Claude Code.",
            "Check the chat before you send it again; Drafts and send history keeps its text.",
            "2026-10-21T07:30:00.000Z",
            (),
            (
                ActionSpec(
                    "open_chat",
                    "Open chat",
                    True,
                    session_id="d866141e-51b3-49ac-b634-6524e2e40eac",
                ),
            ),
        ),
        _example_item(
            item_key("mcp_context", "c28246d7-6e29-463f-a02f-ee6902e80066", "github"),
            "2a034f9a-dc09-4d9b-8a34-2ba060b800b2",
            "mcp_context",
            "report",
            "list",
            "The github MCP server fills a large part of a chat's context.",
            "Its loaded tools take a tenth or more of that chat's context. Turn it off with the "
            "chat's MCP servers button if the chat doesn't need it.",
            "2026-10-21T07:25:00.000Z",
            (),
            (
                ActionSpec(
                    "open_chat",
                    "Open chat",
                    True,
                    session_id="c28246d7-6e29-463f-a02f-ee6902e80066",
                ),
            ),
        ),
        _example_item(
            item_key(
                "code_look",
                "e84e8a26-0fb1-43b5-8e8b-57f98ba18a62",
                "2026-10-19T16:00:00+00:00",
            ),
            "7a681149-9f5d-42e7-b75d-b1184f15b1b0",
            "code_look",
            "suggest",
            "list",
            "garden-app has uncommitted changes, untouched for a day or more.",
            "VIKI can look them over in plan mode and tell you what is half-done or risky.",
            "2026-10-21T07:20:00.000Z",
            (),
            (
                ActionSpec(
                    "open_code",
                    "Open in Code",
                    True,
                    workspace_id="e84e8a26-0fb1-43b5-8e8b-57f98ba18a62",
                ),
                ActionSpec("start_prepare", "Take a look", False),
            ),
        ),
        _example_item(
            item_key(
                "long_task_look",
                "fe07f4bf-5447-4a8d-9d58-b68385409b99",
                "3f0b9c52-6a51-4c27-9a44-2f7e1d0c8b11",
            ),
            "b3c9a8e1-2d47-4f60-9a15-7e8c4d2f6b30",
            "long_task_look",
            "suggest",
            "list",
            "A long task just finished in this chat.",
            "VIKI can take a second look in plan mode and tell you if it finds a flaw, a better "
            "approach or a missing piece.",
            "2026-10-21T07:30:00.000Z",
            (
                EvidenceLabel(
                    "garden-plan", workspace_id=_EXAMPLE_SCOPE, ref="notes/garden-plan.md"
                ),
            ),
            (
                ActionSpec(
                    "open_chat",
                    "Open chat",
                    True,
                    session_id="fe07f4bf-5447-4a8d-9d58-b68385409b99",
                ),
                ActionSpec("start_prepare", "Take a second look", False),
            ),
        ),
        _example_item(
            item_key("correction_look", "5d1e2c3b-4a59-4f68-8a7b-9c0d1e2f3a4b", "found"),
            "c4d0b9f2-3e58-4071-8b26-8f9d5e3a7c41",
            "correction_look",
            "suggest",
            "list",
            'VIKI found a possible flaw in "Garden plan".',
            "Its plan waits in the chat VIKI started: Approve makes the change, and Keep "
            "planning refines it first.",
            "2026-10-21T07:35:00.000Z",
            (),
            (
                ActionSpec(
                    "open_chat",
                    "Open plan",
                    True,
                    session_id="9a8b7c6d-5e4f-4a3b-8c2d-1e0f9a8b7c6d",
                ),
            ),
        ),
        _example_item(
            item_key("stalled_project", garden_note, "2026-10-01T09:00:00.000Z"),
            "d5e1c0a3-4f69-4182-9c37-9a0e6f4b8d52",
            "stalled_project",
            "suggest",
            "list",
            'Nothing in the project "Garden" has changed for two weeks or more.',
            "VIKI can look the project over in plan mode and propose three next actions; it "
            "changes nothing until you approve.",
            "2026-10-21T07:40:00.000Z",
            (EvidenceLabel("Garden", workspace_id=scope, ref=garden_note),),
            (
                ActionSpec("open_note", "Open note", True, workspace_id=scope, ref=garden_note),
                ActionSpec("start_prepare", "Plan next steps", False),
            ),
        ),
        _example_item(
            item_key("overdue_tasks", garden_tasks, "2026-10-15 Order seeds"),
            "e6f2d1b4-5a7a-4293-8d48-ab1f705c9e63",
            "overdue_tasks",
            "suggest",
            "list",
            'Overdue tasks in "garden" may be done or need a new date.',
            "VIKI can check them in plan mode and propose ticking the done ones and new dates "
            "for the rest; it changes nothing until you approve.",
            "2026-10-21T07:45:00.000Z",
            (EvidenceLabel("garden", workspace_id=scope, ref=garden_tasks),),
            (
                ActionSpec("open_note", "Open note", True, workspace_id=scope, ref=garden_tasks),
                ActionSpec("start_prepare", "Check these tasks", False),
            ),
        ),
        _example_item(
            item_key("stalled_project", "6c2d8e1f-4a3b-4c5d-9e8f-7a6b5c4d3e2f", "found"),
            "f7a3e2c5-6b8b-4304-9e59-bc2a816dafa4",
            "stalled_project",
            "suggest",
            "list",
            'VIKI\'s plan for "Garden" waits for your approval.',
            "Its plan waits in the chat VIKI started: Approve makes the change, and Keep "
            "planning refines it first.",
            "2026-10-21T07:50:00.000Z",
            (),
            (
                ActionSpec(
                    "open_chat",
                    "Open plan",
                    True,
                    session_id="8e7d6c5b-4a39-4281-9f0e-1d2c3b4a5f6e",
                ),
            ),
        ),
        _example_item(
            item_key("context_handoff", _EXAMPLE_DRAFT_CHAT),
            "a8b4f3d6-7c9c-4415-8f6a-cd3b927eb0b5",
            "context_handoff",
            "suggest",
            "list",
            "This chat's context is nearly full.",
            _EXAMPLE_HANDOFF_WHY,
            "2026-10-21T07:55:00.000Z",
            (),
            (
                ActionSpec(
                    "draft_chat",
                    "Draft a handoff",
                    True,
                    session_id=_EXAMPLE_DRAFT_CHAT,
                    text=_EXAMPLE_HANDOFF_DRAFT,
                ),
            ),
        ),
        _example_item(
            item_key("source_note", _EXAMPLE_DRAFT_CHAT, "7c9e1a3b-5d2f-4e6a-9b8c-0d1e2f3a4b5c"),
            "b9c5a4e7-8d0d-4526-9a7b-de4ca38fc1c6",
            "source_note",
            "suggest",
            "list",
            "Your last reply in this chat fetched web pages and read no note in your vault.",
            _EXAMPLE_SOURCE_NOTE_WHY,
            "2026-10-21T08:00:00.000Z",
            (),
            (
                ActionSpec(
                    "draft_chat",
                    "Draft literature notes",
                    True,
                    session_id=_EXAMPLE_DRAFT_CHAT,
                    text=_EXAMPLE_SOURCE_NOTE_DRAFT,
                ),
            ),
        ),
        _example_item(
            item_key(
                "commit_checks",
                _EXAMPLE_CHECK_FOLDER,
                "4f1c0d9e8b7a6f5e4d3c2b1a0f9e8d7c6b5a4f3e",
            ),
            "c3a1e5f7-2b4d-4c6e-8a0f-1d3e5b7a9c2e",
            "commit_checks",
            "suggest",
            "list",
            "garden-app moved to a new commit that its saved checks have not run on.",
            "VIKI can run the saved checks you have run there before and report how they went.",
            "2026-10-21T08:05:00.000Z",
            (),
            (
                ActionSpec("open_code", "Open in Code", True, workspace_id=_EXAMPLE_CHECK_FOLDER),
                ActionSpec("run_checks", "Run checks", False, workspace_id=_EXAMPLE_CHECK_FOLDER),
            ),
        ),
        _example_item(
            item_key(
                "commit_checks",
                _EXAMPLE_CHECK_FOLDER_RUNNING,
                "8a7b6c5d4e3f2a1b0c9d8e7f6a5b4c3d2e1f0a9b",
            ),
            "d4b2f6a8-3c5e-4d7f-9b1a-2e4f6c8a0b3d",
            "commit_checks",
            "inform",
            "list",
            "garden-api's saved checks are running on its new commit.",
            "The Code workbench's Checks tab shows their output.",
            "2026-10-21T08:10:00.000Z",
            (),
            (
                ActionSpec(
                    "open_code", "Open in Code", True, workspace_id=_EXAMPLE_CHECK_FOLDER_RUNNING
                ),
                ActionSpec(
                    "stop_checks", "Stop", False, workspace_id=_EXAMPLE_CHECK_FOLDER_RUNNING
                ),
            ),
        ),
        _example_item(
            item_key(
                "inbox_triage",
                scope,
                canonical_json(
                    [
                        ["00-inbox/reading-list-ideas.md", "2026-10-20T07:00:00.000Z"],
                        ["00-inbox/seedling-care.md", "2026-10-20T07:30:00.000Z"],
                    ]
                ),
            ),
            "e5c3a7b9-4d6f-4e8a-8c2b-3f5a7d9b1e4c",
            "inbox_triage",
            "suggest",
            "list",
            "Your inbox has captures with no suggested destination.",
            "VIKI can ask Claude Code, with read-only tools, to suggest where each capture goes; "
            "nothing in your vault changes until you send the filing draft from Triage inbox.",
            "2026-10-21T08:10:00.000Z",
            (),
            (
                ActionSpec("open_triage", "Triage inbox", True),
                ActionSpec("suggest_inbox", "Suggest destinations", False),
            ),
        ),
    )


def _example_call(
    call_id: str, created_at: str, title: str, candidate: str, tokens: int, session_id: str | None
) -> CallRecord:
    split = (
        ("candidate", 1104),
        ("owner", 512),
        ("now", 96),
        ("history", 640),
        ("outside", 100),
        ("questions", 1460),
    )
    body = CallBody(
        title=title,
        candidate=candidate,
        state_text=json.dumps({"candidate": {"title": title, "type": candidate}}, indent=2),
        questions_text=json.dumps({"goal_link": {"type": "noul"}}, separators=(",", ":")),
        split=split,
        answers_json=None,
        held_back=4,
        version_setting="pinned",
        playbook_version="1",
    )
    return CallRecord(
        call_id=call_id,
        created_at=created_at,
        item_key=_EXAMPLE_KEY,
        episode_id=_EXAMPLE_EPISODE,
        session_id=session_id,
        model=MODEL_IDS["pinned"],
        outcome="answered",
        failure=None,
        latency_ms=112,
        input_tokens=tokens,
        cost_micro_usd=160,
        body=body,
    )


def _example_mission(
    mission_id: str,
    session_id: str,
    work: str,
    state: str,
    title: str,
    created_at: str,
    deadline_at: str,
    finished_at: str | None,
    reason: str,
) -> MissionRecord:
    return MissionRecord(
        mission_id=mission_id,
        scope=_EXAMPLE_SCOPE,
        session_id=session_id,
        item_key=_EXAMPLE_KEY,
        work=work,
        state=state,
        reason=reason,
        title=title,
        brief="VIKI started this chat (Decision).",
        created_at=created_at,
        deadline_at=deadline_at,
        finished_at=finished_at,
    )


def example_views() -> dict[str, dict[str, Any]]:
    """One of each response view from fixed inputs, the twin of the client's examples.json; the
    error body is `api_json.failure`'s. Each call builds fresh dicts."""
    now = datetime(2026, 10, 21, 14, 2, 30, tzinfo=UTC)
    checked = datetime(2026, 10, 21, 14, 2, tzinfo=UTC)
    settings = DecisionSettings(enabled=True, focus_until=datetime(2026, 10, 21, 15, tzinfo=UTC))
    items = _example_items()
    calls = (
        _example_call(
            _EXAMPLE_CALL,
            "2026-10-21T14:02:00.000Z",
            "Water the seedlings",
            "tasks_due",
            3912,
            "fe07f4bf-5447-4a8d-9d58-b68385409b99",
        ),
        _example_call(
            "e5d94faa-166d-47dd-be3c-08390a4d43d8",
            "2026-10-21T13:10:00.000Z",
            "Trip budget",
            "tasks_due",
            3488,
            None,
        ),
        _example_call(
            "2d02b528-b6d3-4feb-a140-b2743410c18f",
            "2026-10-21T12:30:00.000Z",
            "Inbox",
            "inbox",
            2204,
            None,
        ),
    )
    running = _example_mission(
        "9273d0ed-e3e0-4ac6-8319-80e6c5e10247",
        "fda2250d-d245-468a-9d7c-5fa673b14d3a",
        "prepare",
        "running",
        "Draft a plan for the trip budget",
        "2026-10-21T13:10:30.000Z",
        "2026-10-21T13:40:30.000Z",
        None,
        "",
    )
    stopped = _example_mission(
        "93719668-5a33-415f-9092-576cd7435c8b",
        "06c6b586-1f0e-4829-9bf3-7c3198c20a3d",
        "run",
        "stopped",
        "File the inbox notes",
        "2026-10-21T08:40:00.000Z",
        "2026-10-21T09:10:00.000Z",
        "2026-10-21T08:52:00.000Z",
        "owner",
    )
    held = {
        "episode_id": "1cb1aece-b18a-46ee-962f-c8346104dc7d",
        "created_at": "2026-10-21T10:05:00.000Z",
        "candidate": "inbox",
        "level": "silent",
        "reason": "inbox_below_nudge",
        "why": "a few notes in the inbox",
        "feedback": "right_to_hold",
    }
    held_back = (
        held,
        {
            "episode_id": "3c6418ec-4ce4-42a8-b6f3-a74dde91a050",
            "created_at": "2026-10-20T18:30:00.000Z",
            "candidate": "routine",
            "level": "silent",
            "reason": "routine_later",
            "why": "a routine runs soon",
            "feedback": None,
        },
    )
    recent = (
        {
            "episode_id": _EXAMPLE_EPISODE,
            "created_at": "2026-10-21T14:02:00.000Z",
            "candidate": "tasks_due",
            "level": "badge",
            "policy": "model",
            "reason": "score",
            "why": "due today; linked to one of your goals",
            "feedback": None,
        },
        {
            "episode_id": "3a4136ba-71a9-45d6-a3e1-84b57d339d7c",
            "created_at": "2026-10-21T13:58:00.000Z",
            "candidate": "approvals",
            "level": "badge",
            "policy": "rules",
            "reason": "waiting_on_you",
            "why": "needs your decision",
            "feedback": None,
        },
        {**held, "policy": "rules"},
        {
            "episode_id": "62dc717c-522a-4f06-b408-4b936c9d4c61",
            "created_at": "2026-10-21T08:15:00.000Z",
            "candidate": "agent_runs",
            "level": "list",
            "policy": "rules",
            "reason": "runs_to_review",
            "why": "an agent run is ready for review",
            "feedback": "useful",
        },
    )
    questions = (
        ("goal_link", "Linked to a goal?", "Yes, 0.91"),
        ("owner_only", "Needs your decision?", "No, 0.12"),
        ("news", "New to you?", "Yes, 0.77"),
        ("actionable", "One clear next step?", "Yes, 0.88"),
        ("same_as_muted", "Same as things you stopped?", "No, 0.05"),
        ("fits_now", "About what you're doing now?", "Yes, 0.64"),
        ("sensitive", "A private matter?", "No, 0.02"),
        ("importance", "How much it matters", "2.3 of 3, confidence 0.62"),
        ("kind", "What kind", "suggest, 0.71"),
        ("work", "What work", "prepare, 0.74"),
    )
    error = failure("unknown_item", "This item was not found.", 404)
    learned = Learned(
        fitted_at=datetime(2026, 10, 20, 3, 0, tzinfo=UTC),
        labels=58,
        weights={
            "bias": -0.42,
            "goal_link": 0.71,
            "news": 0.55,
            "actionable": 0.38,
            "owner_only": 0.33,
            "fits_now": 0.21,
            "topic.approvals": 0.12,
            "topic.tasks_due": 0.46,
            "topic.agent_runs": -0.08,
            "topic.inbox": 0.05,
            "topic.uncommitted": -0.31,
            "topic.routine": 0.02,
            "topic.mission_report": 0.18,
        },
        reach={"list": 0.28, "badge": 0.64, "nudge": 0.88, "alert": 0.95},
        costs={"badge": 0.06, "nudge": 0.37, "alert": 0.91},
        support={
            "topic.approvals": 7,
            "topic.tasks_due": 19,
            "topic.agent_runs": 5,
            "topic.inbox": 10,
            "topic.uncommitted": 6,
            "topic.routine": 5,
            "topic.mission_report": 6,
            "reach.list": 30,
            "reach.badge": 22,
            "reach.nudge": 10,
            "reach.alert": 4,
            "costs.badge": 22,
            "costs.nudge": 10,
            "costs.alert": 4,
        },
    )
    calibration = Calibration(
        n=64,
        brier=0.18,
        bins=(
            CalibrationBin(0.0, 0.2, 9, 0.11, 0.09),
            CalibrationBin(0.2, 0.4, 13, 0.29, 0.31),
            CalibrationBin(0.4, 0.6, 16, 0.51, 0.47),
            CalibrationBin(0.6, 0.8, 15, 0.69, 0.74),
            CalibrationBin(0.8, 1.0, 11, 0.9, 0.86),
        ),
    )
    learning_questions = (
        QuestionRate("goal_link", 41, 0.68, 19, 0.21),
        QuestionRate("owner_only", 25, 0.52, 35, 0.31),
        QuestionRate("news", 38, 0.61, 22, 0.18),
        QuestionRate("actionable", 44, 0.7, 16, 0.25),
        QuestionRate("same_as_muted", 12, 0.17, 48, 0.59),
        QuestionRate("fits_now", 30, 0.55, 30, 0.4),
        QuestionRate("sensitive", 8, 0.25, 52, 0.49),
    )
    shadow = ShadowStats(
        setting="preview",
        decisions=50,
        agree=41,
        kind_agree=45,
        labels=12,
        brier=0.17,
        live_brier=0.19,
    )
    second_looks = SecondLookYields(
        triggers=(
            TriggerYield("long_task_look", 4, 1, 2, 1, {"already_knew": 1}, False),
            TriggerYield("correction_look", 0, 0, 0, 0, {}, False),
        ),
        kinds=(
            KindYield("flaw", 0, False),
            KindYield("approach", 2, False),
            KindYield("missing", 0, False),
        ),
    )
    project_plans = SecondLookYields(
        triggers=(
            TriggerYield("stalled_project", 3, 1, 1, 1, {"wrong_topic": 1}, False),
            TriggerYield("overdue_tasks", 0, 0, 0, 0, {}, False),
        ),
        kinds=(),
    )
    message_drafts = SecondLookYields(
        triggers=(
            TriggerYield("context_handoff", 4, 1, 1, 2, {"already_knew": 1}, False),
            TriggerYield("source_note", 0, 0, 0, 0, {}, False),
        ),
        kinds=(),
    )
    saved_checks = SecondLookYields(
        triggers=(TriggerYield("commit_checks", 3, 1, 1, 0, {"already_knew": 1}, False),),
        kinds=(),
    )
    inbox_triage = SecondLookYields(
        triggers=(TriggerYield("inbox_triage", 4, 1, 1, 1, {"already_knew": 1}, False),),
        kinds=(),
    )
    learning = learning_view(
        settings,
        learned,
        labels=64,
        since=datetime(2026, 9, 1, tzinfo=UTC),
        lam=0.42,
        calibration=calibration,
        questions=learning_questions,
        shadow=shadow,
        second_looks=second_looks,
        project_plans=project_plans,
        message_drafts=message_drafts,
        saved_checks=saved_checks,
        inbox_triage=inbox_triage,
    )
    # A proposed wording for "news", approved after its trial and then undone, with a reason.
    news = playbook.questions()["news"]
    proposed = {
        **news,
        "criteria": {
            "true": "The owner has not seen this recently",
            "false": {
                "what": "Already on screen or seen recently",
                "not_for": "A capture the owner filed but never read",
            },
        },
    }
    proposal = {
        "kind": "question_proposal",
        "id": "e61a512d-4cce-4c11-b792-e6932dde8c3e",
        "origin": {"workspace_id": _EXAMPLE_SCOPE, "session_id": None},
        "revision": "e73801d660dea203c70a987d6f0487e3dd376f4f5faee2aa985622594e6cef57",
        "state": "rejected",
        "attention": False,
        "title": "Question change: New to you?",
        "name": "news",
        "created_at": "2026-10-19T09:30:00.000Z",
        "updated_at": "2026-10-21T10:15:00.000Z",
        "approved_at": "2026-10-20T08:00:00.000Z",
        "rejected_at": "2026-10-21T10:15:00.000Z",
        "reason": "It held back notes I had not read yet",
    }
    tried = TrialStats(
        "news",
        decisions=24,
        agree=19,
        labels=6,
        live=QuestionRate("news", 4, 0.75, 2, 0.5),
        trial=QuestionRate("news", 3, 2 / 3, 3, 1 / 3),
    )
    # A note_link change, approved: the user's answer to "Draft again" in Approvals.
    change = {
        "kind": "change_proposal",
        "id": "bc21adb7-872b-5924-b52a-16a0476ff6a3",
        "origin": {"workspace_id": _EXAMPLE_SCOPE, "session_id": None},
        "revision": "1f44767c51ab81c0e6531908fa99c5d713dbba08c2670dfaa6b84782902e60eb",
        "state": "approved",
        "attention": False,
        "title": "Link seedlings-need-even-moisture to overwatering-signs-in-seedlings",
        "change": "note_link",
        "ref": "30-knowledge/seedlings-need-even-moisture.md",
        "created_at": "2026-10-21T07:50:00.000Z",
        "updated_at": "2026-10-21T07:55:00.000Z",
        "approved_at": "2026-10-21T07:55:00.000Z",
        "rejected_at": None,
        "reason": None,
    }
    change_draft = (
        "Link two notes in this vault by adding one wikilink line to the first note, as "
        "described below:\n"
        "- In `30-knowledge/seedlings-need-even-moisture.md`, add one line "
        "`- [[overwatering-signs-in-seedlings]]` under a `## Related` heading at the end of the "
        "note (add the heading if the note has none).\n"
        "Change no other line, and keep every word already there as it is."
    )
    data_parts = {
        "decisions": {
            "decisions": 1280,
            "reports": 4,
            "oldest": "2026-08-02T09:00:00.000Z",
            "newest": "2026-10-21T14:02:00.000Z",
            "max": MAX_EPISODES,
            "retention_days": RETENTION_DAYS,
        },
        "suggestions": {
            "open": 6,
            "closed": 19,
            "oldest": "2026-10-01T08:00:00.000Z",
            "newest": "2026-10-21T14:02:00.000Z",
            "max_open": MAX_OPEN_ITEMS,
            "closed_days": 30,
        },
        "calls": {
            "count": 3,
            "oldest": calls[-1].created_at,
            "newest": calls[0].created_at,
            "max": MAX_CALLS,
        },
        "chats": {
            "count": 2,
            "running": 1,
            "oldest": stopped.created_at,
            "newest": running.created_at,
        },
        "proposals": {
            "wordings": 1,
            "changes": 1,
            "oldest": proposal["created_at"],
            "newest": change["created_at"],
            "max_wordings": 50,
            "max_changes": 50,
        },
        "preferences": {"muted": 1, "learned": True, "updated": "2026-10-21T10:00:00.000Z"},
        "days": [
            {
                "day": "2026-10-21",
                "held": 4,
                "listed": 3,
                "attention": 5,
                "reports": 1,
                "useful": 2,
                "wrong": 1,
                "later": 1,
                "mute": 1,
                "right_to_hold": 1,
                "missed": 1,
            }
        ],
        "topics": [
            {"topic": "unwatched", "held": 2, "shown": 3, "reports": 1, "useful": 1, "wrong": 1}
        ],
    }
    decision_rows = [
        {
            "id": _EXAMPLE_EPISODE,
            "at": calls[0].created_at,
            "entry": "decision",
            "topic": "tasks_due",
            "level": "badge",
            "policy": "model",
            "answer": "useful",
            "kept": "open_suggestion",
        },
        {
            "id": held["episode_id"],
            "at": held["created_at"],
            "entry": "report",
            "topic": "unwatched",
            "level": None,
            "policy": "rules",
            "answer": None,
            "kept": None,
        },
    ]
    suggestion_rows = [
        {
            "id": items[0].item_key,
            "at": items[0].created_at,
            "created_at": items[0].created_at,
            "title": items[0].body.title,
            "topic": items[0].candidate,
            "state": "open",
            "level": items[0].level,
            "kept": "open",
        },
        {
            "id": items[1].item_key,
            "at": items[1].created_at,
            "created_at": items[1].created_at,
            "title": items[1].body.title,
            "topic": items[1].candidate,
            "state": "done",
            "level": items[1].level,
            "kept": None,
        },
    ]
    data_evidence = (
        EvidenceLabel("A linked note", _EXAMPLE_SCOPE, "30-knowledge/seedlings.md"),
        EvidenceLabel("A chat", session_id=running.session_id),
    )
    return {
        "launcher": launcher_view(
            settings,
            items,
            now=now,
            model_state="ready",
            checked_at=checked,
            working=1,
            nudge=3,
            glow=5,
            started=(
                running.session_id,
                "23a50b40-2561-439f-bb6f-ff20189487ba",
                stopped.session_id,
            ),
            review=(False, 0),
        ),
        "page": page_view(
            settings,
            now=now,
            key_saved=True,
            model_state="ready",
            last_error="rate_limited",
            last_test=(datetime(2026, 10, 21, 9, 15, tzinfo=UTC), 112),
            checked_at=checked,
            open_items=len(items),
            decisions_today=412,
            model_calls_today=426,
            tokens_today=1_666_000,
            spent_micro_usd=70_000,
            last_call=calls[0],
            held_back_today=4,
            muted=("uncommitted",),
            held_back=held_back,
            recent=recent,
            counts={
                "shown": 128,
                "useful": 41,
                "wrong": 9,
                "later": 17,
                "mute": 3,
                "missed": 2,
                "right_to_hold": 11,
            },
            running=(running,),
            prepare_today=2,
            run_today=1,
            episodes=1284,
            oldest="2026-08-02T09:00:00.000Z",
            learning=learning,
            shadow_calls_today=6,
            changes_today=1,
            team=SeatTeam(1, 1, ()),
            look_today=1,
            second_look_today=1,
            project_plan_today=1,
            inbox_today=1,
            message_draft_today=1,
            check_today=1,
            check_running=0,
        ),
        "calls": calls_view(settings, calls, latest="model"),
        "call_detail": call_detail_view(calls[0], questions=questions, largest_question=2870),
        "mission": mission_view(stopped),
        "test_result": test_view(ModelReply(MODEL_IDS["pinned"], {}, 12, 112)),
        "question_proposal": question_proposal_view(
            proposal, current=news, proposed=proposed, tried=tried
        ),
        "change_act": change_act_view(change, change_draft),
        "data_summary": data_summary_view(
            data_parts, now=now, key_saved=True, key_changed_at="2026-10-20T09:00:00.000Z"
        ),
        "data_decisions": data_records_view(
            "decisions",
            {
                "total": 2,
                "deletable": 1,
                "next": {"at": decision_rows[-1]["at"], "id": decision_rows[-1]["id"]},
                "rows": decision_rows,
            },
        ),
        "data_suggestions": data_records_view(
            "suggestions",
            {
                "total": 2,
                "deletable": 1,
                "next": {"at": suggestion_rows[-1]["at"], "id": suggestion_rows[-1]["id"]},
                "rows": suggestion_rows,
            },
        ),
        "data_calls": data_calls_view(calls),
        "data_decision": data_decision_view(
            {
                **decision_rows[0],
                "why": "Due today; linked to your goal",
                "evidence": data_evidence,
                "answer_reason": "I needed this",
                "answered_at": calls[0].created_at,
                "shown": 2,
                "opened_at": calls[0].created_at,
                "acted_at": calls[0].created_at,
                "tokens": 3912,
                "cost_usd": 0.00016,
            }
        ),
        "data_suggestion": data_suggestion_view(
            {
                **{key: value for key, value in suggestion_rows[0].items() if key != "at"},
                "updated_at": suggestion_rows[0]["at"],
                "shown_at": suggestion_rows[0]["at"],
                "opened_at": suggestion_rows[0]["at"],
                "what": items[0].body.what,
                "why": items[0].body.why,
                "evidence": data_evidence,
            }
        ),
        "data_deleted": data_deleted_view(2, 1),
        "error": json.loads(bytes(error.body)),
    }
