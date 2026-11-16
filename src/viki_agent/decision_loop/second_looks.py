"""The second looks' pure parts: read a plan's kind, build the offer and the finding items, and
count what the looks yielded.

`kind_of` reads one model-written plan, in memory only: its first non-empty line, within its
first `PLAN_CHARS` characters, must be exactly "Kind: flaw", "Kind: better approach" or "Kind:
missing piece" (a "#" heading mark, one wrapping "**" pair, spacing and letter case aside); a
longer line, a later Kind line, an empty plan or anything that is not text reads "other". The
plan itself goes nowhere. `admitted` is the one rule for a note ref a second look may name (the
same one `missions` applies to a brief's refs): an exact str that `safe_ref` takes, with a note
suffix, no `_UNSAFE` character, no trailing "…" and no first part starting with "~". `offer` and
`finding` build the two items; `offer` is None without an admitted ref. Neither puts a ref, id or
plan text in `words` or `evidence`, so `state.build_state` sends the model only the type words and
the chat's title.

`yields` counts `LookRow`s: per type over the rows since its Try again stamp, per kind over the
`accepted:<kind>` and `rejected:<kind>` rows since its Raise again stamp. `restarts` reads those
stamps back from the stored "second_looks" preference and `restarted` writes one; a value that
is not exactly the stored shape, or holds a bad stamp, reads as no restarts at all, so a damaged
value only makes pauses more likely, never fewer. Learning off pauses nothing. The project plans
use the same counts over `PROJECT_PLANS` with no kinds (`looks=` and `kinds=()`), and keep their
stamps in the "project_plans" preference.

Pure: no I/O, no clock read (`now` is passed in) and nothing logged.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import PurePosixPath
from typing import TypeIs

from viki_agent.automation.routines import parse_stamp, stamp
from viki_agent.decision_loop.contracts import (
    FEEDBACK_REASONS,
    FINDING_KINDS,
    MAX_SECOND_LOOK_REF_BYTES,
    MAX_SECOND_LOOK_REFS,
    SECOND_LOOK_ACCEPT_ONE_IN,
    SECOND_LOOK_ANSWERED,
    SECOND_LOOK_KIND_REJECTS,
    SECOND_LOOK_TYPES,
    SECOND_LOOKS,
    UNTITLED_CHAT,
    Candidate,
    Finding,
    KindYield,
    LookRow,
    SecondLookHint,
    SecondLookYields,
    TriggerYield,
    item_key,
)
from viki_agent.decision_loop.vault_guard import _UNSAFE
from viki_agent.vaults.vault import NOTE_SUFFIXES, VaultError, safe_ref

PLAN_CHARS = 4096
_KINDS: Mapping[str, str] = {
    "kind: flaw": "flaw",
    "kind: better approach": "approach",
    "kind: missing piece": "missing",
}
_ACCEPTED = "accepted:"
_REJECTED = "rejected:"
_NOTHING = "nothing"


def kind_of(plan: object) -> str:
    """The kind a plan's first line names: one of `FINDING_KINDS`, else "other"."""
    if not isinstance(plan, str):
        return "other"
    lines = (line.strip() for line in plan[:PLAN_CHARS].splitlines())
    line = next((line for line in lines if line), "")
    line = line.lstrip("#").strip()
    if len(line) >= 4 and line.startswith("**") and line.endswith("**"):
        line = line[2:-2]
    return _KINDS.get(" ".join(line.split()).casefold(), "other")


def admitted(ref: object) -> TypeIs[str]:
    """Whether `ref` names a vault note a second look may list."""
    if type(ref) is not str or _UNSAFE.search(ref) or ref.endswith("\N{HORIZONTAL ELLIPSIS}"):
        return False
    if ref.startswith("~"):  # Claude Code expands a leading "~/" to the home folder
        return False
    try:
        safe_ref(ref)
    except VaultError:
        return False
    return PurePosixPath(ref).suffix.casefold() in NOTE_SUFFIXES


def _notes(refs: Sequence[object]) -> tuple[str, ...]:
    """The admitted refs, each once and in order, stopping at `MAX_SECOND_LOOK_REFS` or before
    `MAX_SECOND_LOOK_REF_BYTES` UTF-8 bytes in all, as `missions` cuts a brief's."""
    notes: list[str] = []
    size = 0
    for ref in refs:
        if not admitted(ref) or ref in notes:
            continue
        size += len(ref.encode("utf-8"))
        if len(notes) == MAX_SECOND_LOOK_REFS or size > MAX_SECOND_LOOK_REF_BYTES:
            break
        notes.append(ref)
    return tuple(notes)


def offer(hint: SecondLookHint, scope: str) -> Candidate | None:
    """The offer for the chat turn `hint` names in vault `scope`; None for an unknown trigger or
    without an admitted ref. `facts` keep the refs and the chat for the user's page; `words`
    and `evidence` stay empty, so the model sees only the type and the title."""
    refs = _notes(hint.refs)
    look = SECOND_LOOK_TYPES.get(hint.trigger)
    if look is None or not refs:
        return None
    title = hint.title or UNTITLED_CHAT
    return Candidate(
        item_key=item_key(look, hint.session, hint.turn),
        type=look,
        title=title,
        facts={"workspace_id": scope, "session": hint.session, "refs": refs},
        words={},
        evidence=(),
        related_chat=hint.session,
        related_ref=None,
        deadline=None,
        shown_elsewhere=(),
        rules_evidence={"trigger": hint.trigger},
        origin_session=None,
        work_allowed=("prepare",),
    )


def finding(found: Finding) -> Candidate:
    """The item for a second look whose plan waits in chat `found.session_id`. It starts no
    work; `facts["chat"]` is blank for an untitled chat, which the wording reads as "your chat"."""
    title = found.title or UNTITLED_CHAT
    chat = "" if title == UNTITLED_CHAT else title
    return Candidate(
        item_key=item_key(found.look, found.mission_id, "found"),
        type=found.look,
        title=title,
        facts={
            "workspace_id": found.scope,
            "found": found.kind,
            "session": found.session_id,
            "chat": chat,
        },
        words={},
        evidence=(),
        related_chat=found.session_id,
        related_ref=None,
        deadline=None,
        shown_elsewhere=(),
        rules_evidence={"found": found.kind},
        origin_session=None,
        work_allowed=(),
    )


# ---- restarts ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Restarts:
    """When each type's Try again and each kind's Raise again last happened; a missing name
    counts everything the rows hold."""

    looks: Mapping[str, datetime] = field(default_factory=dict)
    kinds: Mapping[str, datetime] = field(default_factory=dict)


def restarts(
    value: object, *, looks: Sequence[str] = SECOND_LOOKS, kinds: Sequence[str] = FINDING_KINDS
) -> Restarts:
    """The `Restarts` a stored "second_looks" (or "project_plans") preference holds; none when
    `value` is not exactly {"looks": {type: stamp}, "kinds": {kind: stamp}} over the names `looks`
    and `kinds` give and `stamp` forms."""
    if not isinstance(value, dict) or set(value) != {"looks", "kinds"}:
        return Restarts()
    try:
        return Restarts(_stamps(value["looks"], looks), _stamps(value["kinds"], kinds))
    except ValueError:
        return Restarts()


def _stamps(value: object, names: Sequence[str]) -> dict[str, datetime]:
    if not isinstance(value, dict) or not set(value) <= set(names):
        raise ValueError("invalid_restarts")
    return {name: parse_stamp(text) for name, text in value.items()}


def restarted(
    value: object,
    *,
    look: str | None = None,
    kind: str | None = None,
    now: datetime,
    looks: Sequence[str] = SECOND_LOOKS,
    kinds: Sequence[str] = FINDING_KINDS,
) -> dict[str, dict[str, str]]:
    """The preference value after Try again for `look` and/or Raise again for `kind` at `now`,
    on top of what `value` already holds. Raises ValueError for a name that `looks` or `kinds`
    does not hold."""
    if look is not None and look not in looks:
        raise ValueError("invalid_look")
    if kind is not None and kind not in kinds:
        raise ValueError("invalid_kind")
    held = restarts(value, looks=looks, kinds=kinds)
    stamped_looks = {**held.looks, **({look: now} if look is not None else {})}
    stamped_kinds = {**held.kinds, **({kind: now} if kind is not None else {})}
    return {
        "looks": {name: stamp(at) for name, at in stamped_looks.items()},
        "kinds": {name: stamp(at) for name, at in stamped_kinds.items()},
    }


# ---- yields ------------------------------------------------------------------------------------


def yields(
    rows: Sequence[LookRow],
    restarts: Restarts,
    reasons: Mapping[str, Mapping[str, int]],
    *,
    learning: bool,
    looks: Sequence[str] = SECOND_LOOKS,
    kinds: Sequence[str] = FINDING_KINDS,
) -> SecondLookYields:
    """What `rows` say each type of `looks` and kind of `kinds` yielded. `reasons` is each
    type's Not useful reasons by count. A type is paused once `SECOND_LOOK_ANSWERED` of its looks
    were accepted or rejected -- a project plan's found-nothing rows count toward that too, a
    second look's do not, since a look finding nothing is not a turned-down offer -- and fewer
    than one in `SECOND_LOOK_ACCEPT_ONE_IN` of them was accepted; a kind once its newest
    `SECOND_LOOK_KIND_REJECTS` answers were all rejections. `learning` off pauses neither."""
    return SecondLookYields(
        tuple(_trigger(rows, restarts, reasons, look, learning) for look in looks),
        tuple(_kind(rows, restarts, kind, learning) for kind in kinds),
    )


def _trigger(
    rows: Sequence[LookRow],
    restarts: Restarts,
    reasons: Mapping[str, Mapping[str, int]],
    look: str,
    learning: bool,
) -> TriggerYield:
    since = restarts.looks.get(look)
    mine = [r.reason for r in rows if r.look == look and (since is None or r.started >= since)]
    accepted = sum(reason.startswith(_ACCEPTED) for reason in mine)
    rejected = sum(reason.startswith(_REJECTED) for reason in mine)
    nothing = sum(reason == _NOTHING for reason in mine)
    # A second look that found nothing is not a turned-down offer, so it no longer counts toward
    # pausing the type; a project plan (never in `SECOND_LOOKS`) still counts its own.
    answered = accepted + rejected + (0 if look in SECOND_LOOKS else nothing)
    paused = (
        learning
        and answered >= SECOND_LOOK_ANSWERED
        and accepted * SECOND_LOOK_ACCEPT_ONE_IN < answered
    )
    counted = {
        reason: count
        for reason, count in reasons.get(look, {}).items()
        if reason in FEEDBACK_REASONS
    }
    return TriggerYield(look, len(mine), accepted, rejected, nothing, counted, paused)


def _kind(rows: Sequence[LookRow], restarts: Restarts, kind: str, learning: bool) -> KindYield:
    since = restarts.kinds.get(kind)
    answers = sorted(
        (
            r
            for r in rows
            if r.reason in (_ACCEPTED + kind, _REJECTED + kind)
            and (since is None or r.started >= since)
        ),
        key=lambda r: r.started,
        reverse=True,
    )
    streak = 0
    for row in answers:
        if not row.reason.startswith(_REJECTED):
            break
        streak += 1
    return KindYield(kind, streak, learning and streak >= SECOND_LOOK_KIND_REJECTS)


def paused_looks(found: SecondLookYields) -> tuple[str, ...]:
    """The types `found` pauses, in `SECOND_LOOKS` order."""
    return tuple(t.look for t in found.triggers if t.paused)


def paused_kinds(found: SecondLookYields) -> tuple[str, ...]:
    """The kinds `found` pauses, in `FINDING_KINDS` order."""
    return tuple(k.kind for k in found.kinds if k.paused)
