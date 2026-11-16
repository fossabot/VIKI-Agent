"""The state a Decision model request carries about one candidate, and the privacy rules
that decide what goes into it.

What `build_state` lets through:
- the candidate's type and facts as words, with no digit or other numeral in
  `candidate.facts`, `now` or `history.counts`; titles and excerpts are the user's own text and
  keep theirs, except the open item's title in `now.active`, which loses its numerals;
- excerpts cut to the send level (`contracts.EXCERPT`: titles 0, excerpts 200, sections 2,000
  characters), ending "…" when cut;
- never-send (`DecisionSettings.never_send`, folders ending "/" and tags starting "#"): an
  evidence entry whose ref sits under a listed folder (by path segment, ignoring case and
  width) or whose tags meet a listed tag (that tag or one nested under it) is dropped and
  counted in `held_back`; its tags are the ones it carries plus those written inline in its
  whole excerpt, before any cut (`inline_tags`); with any tag rule, an entry whose tags are
  unknown (None) is dropped too. The titles in `owner`, `now.active` and `history.similar` are
  held back by the same rules, inline tags included, uncounted. A candidate whose own note
  (`related_ref`) is never-send, whose own title carries a listed tag inline, or a never-send
  list that cannot be trusted, makes the state `blocked`: it is empty, for the rules to decide;
  so does any held-back evidence on a `contracts.PAIRED` item (note_link/note_superseded),
  since one of its two notes is never `related_ref` and so never checked above — a never-send
  folder or tag on either one blocks the whole pair, silently, rather than asking about half of
  it;
- text from other people or the web (`Evidence.outside`) only under `outside`;
- at most `contracts.STATE_BYTES` of canonical JSON: past that, `history.similar` is dropped
  from the end, then excerpts are cut to `CUT_EXCERPT` characters, then to none, then the state
  is `blocked`.

A state that is not blocked opens with `guide`, the playbook's fixed instructions to the model
(`playbook.GUIDE`). It says nothing about the item, so it rides on top of that budget, as the
questions do, and never-send does not read it.

`state_hash` names the item's own state (`candidate`, `outside` and `owner.goals` as the send
level and never-send leave them, before any budget cut) with the version of the questions asked
(`playbook_version`, which also covers the guide) and the model setting; `now`, `history` and the
rest of `owner` ride along without moving it. `state_text` is the indented form the Context tab
shows, its characters as sent (text in any script as itself) bar control and format characters,
which stay escaped.

Pure: no I/O, no clock reads, nothing logged.
"""

from __future__ import annotations

import json
import posixpath
import unicodedata
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from viki_agent.decision_loop.contracts import (
    EXCERPT,
    FEEDBACK_HELD,
    FEEDBACK_SHOWN,
    LEVELS,
    PAIRED,
    STATE_BYTES,
    Candidate,
    DecisionSettings,
    Evidence,
    canonical_json,
    sha256_hex,
)
from viki_agent.decision_loop.playbook import (
    GUIDE,
    PLAYBOOK_VERSION,
    estimate_tokens,
    questions,
    questions_text,
)
from viki_agent.decision_loop.words import count_words, time_of_day, type_words

# The most entries each list carries; longer inputs are cut, keeping the first.
GOALS = 8
RECENT = 5
MUTED_OR_WRONG = 5
RECENT_FEEDBACK = 5
SIMILAR = 5
# The budget's first excerpt cut, in characters.
CUT_EXCERPT = 100

PARTS = ("guide", "candidate", "owner", "now", "history", "outside")
_OUTCOMES = (*FEEDBACK_SHOWN, *FEEDBACK_HELD)
_PROJECTS = "10-projects"
_QUESTIONS_TOKENS = estimate_tokens(questions_text(questions()))
# Unicode categories `state_text` keeps as JSON escapes: control and format characters,
# surrogates, and line and paragraph separators, which would hide, reorder or break the text.
_ESCAPED = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})


@dataclass(frozen=True)
class Mention:
    """A title the state may carry besides the candidate's. `ref` and `tags` say where it sits
    in the vault, so never-send can hold it back: a chat or app title has `ref` None and `tags`
    (); a note whose tags are unknown has `tags` None."""

    title: str
    ref: str | None
    tags: tuple[str, ...] | None


@dataclass(frozen=True)
class Past:
    """An earlier item: what it was, the level it was shown at (one of `LEVELS`) and the
    user's feedback (one of `FEEDBACK_SHOWN` or `FEEDBACK_HELD`; None when there was none)."""

    item: Mention
    shown_as: str
    outcome: str | None


@dataclass(frozen=True)
class Owner:
    """The user's side: goals (active projects, in ref order so a reorder never changes an
    item's state), then, most relevant first, what they saw or wrote recently, items they muted
    or marked wrong, and their latest feedback."""

    goals: tuple[Mention, ...] = ()
    recent: tuple[Mention, ...] = ()
    muted_or_wrong: tuple[Mention, ...] = ()
    recent_feedback: tuple[Past, ...] = ()


@dataclass(frozen=True)
class Now:
    """The moment: what is open (None for nothing), whether Focus is on and the user is
    typing, the user's local time (an aware datetime on their wall clock) and how many nudges
    and alerts the last hour showed."""

    active: Mention | None
    focus: bool
    typing: bool
    time: datetime
    nudges_this_hour: int


@dataclass(frozen=True)
class History:
    """Earlier items like this one, most similar first, and counts by name (such as shown,
    useful, wrong)."""

    similar: tuple[Past, ...] = ()
    counts: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class BuiltState:
    """`state` is what may be sent, the guide first, {} when `blocked`; `held_back` counts the
    evidence entries never-send kept out (every one when never-send blocks the candidate);
    `split` is (part, estimated tokens) for `PARTS` and then the questions, empty when blocked;
    `state_hash` is the item's own state hash, None when blocked."""

    state: Mapping[str, object]
    held_back: int
    split: tuple[tuple[str, int], ...]
    blocked: bool
    state_hash: str | None


@dataclass(frozen=True)
class _Rules:
    """Never-send, normalised: each folder as its path segments, each tag without "#"."""

    folders: tuple[tuple[str, ...], ...]
    tags: tuple[str, ...]


def build_state(
    candidate: Candidate,
    owner: Owner,
    now: Now,
    history: History,
    settings: DecisionSettings,
    *,
    playbook_version: str = PLAYBOOK_VERSION,
) -> BuiltState:
    """The state for `candidate`, never-send and the send level applied, within the budget; see
    the module docstring for every rule. `playbook_version` is the version of the questions the
    state is asked with (`playbook.version` of the wording in use), part of its own hash."""
    rules = _rules(settings.never_send, settings.never_send_invalid)
    if rules is None or _note_blocked(rules, candidate):
        return BuiltState({}, len(candidate.evidence), (), True, None)
    sent = [
        entry
        for entry in candidate.evidence
        if not _held(rules, entry.ref, entry.tags, entry.excerpt)
    ]
    held_back = len(candidate.evidence) - len(sent)
    if candidate.type in PAIRED and held_back > 0:
        # A paired item (note_link/note_superseded) rests on two notes, only one of which is
        # `related_ref`; `_note_blocked` above only ever checks the note at `related_ref`, so a
        # never-send folder or tag on the *other* note would otherwise just narrow `sent`
        # instead of silencing the ask. Never asked, so never filed.
        return BuiltState({}, held_back, (), True, None)
    inside = [entry for entry in sent if not entry.outside]
    outside = [entry for entry in sent if entry.outside]
    owner_part = {
        "goals": _titles(rules, owner.goals[:GOALS]),
        "recent": _titles(rules, owner.recent[:RECENT]),
        "muted_or_wrong": _titles(rules, owner.muted_or_wrong[:MUTED_OR_WRONG]),
        "recent_feedback": _pasts(rules, owner.recent_feedback[:RECENT_FEEDBACK]),
    }
    now_part = {
        "active": _active(rules, now.active),
        "focus": bool(now.focus),
        "typing": bool(now.typing),
        "time": time_of_day(now.time),
        "nudges_this_hour": count_words(now.nudges_this_hour),
    }
    similar = _pasts(rules, history.similar[:SIMILAR])
    counts = {
        name: count_words(n)
        for name, n in history.counts.items()
        if _is_word(name) and isinstance(n, int) and not isinstance(n, bool)
    }

    def assemble(cap: int, kept: int) -> dict[str, object]:
        return {
            "candidate": _candidate(candidate, inside, cap),
            "owner": owner_part,
            "now": now_part,
            "history": {"similar": similar[:kept], "counts": counts},
            "outside": [
                {"ref": entry.ref, "excerpt": _cut(entry.excerpt, cap)} for entry in outside
            ],
        }

    cap = EXCERPT.get(settings.send_level, 0)
    kept = len(similar)
    state = assemble(cap, kept)
    own = {
        "candidate": state["candidate"],
        "outside": state["outside"],
        "goals": owner_part["goals"],
        "playbook_version": playbook_version,
        "model_version": settings.model_version,
    }
    state_hash = sha256_hex(canonical_json(own))
    while _size(state) > STATE_BYTES and kept > 0:
        kept -= 1
        state = assemble(cap, kept)
    for smaller in (CUT_EXCERPT, 0):
        if _size(state) <= STATE_BYTES:
            break
        cap = min(cap, smaller)
        state = assemble(cap, kept)
    if _size(state) > STATE_BYTES:
        return BuiltState({}, held_back, (), True, None)
    # First, so the model reads its instructions before the item (the request keeps this order).
    state = {"guide": list(GUIDE), **state}
    split = tuple((part, estimate_tokens(canonical_json(state[part]))) for part in PARTS)
    return BuiltState(
        state, held_back, (*split, ("questions", _QUESTIONS_TOKENS)), False, state_hash
    )


def state_text(state: Mapping[str, object]) -> str:
    """`state` as the Context tab shows it: JSON with sorted keys, indented two spaces, each
    character as itself, as sent, except the categories in `_ESCAPED`, which stay JSON escapes;
    so the only line breaks are the indentation's."""
    text = json.dumps(
        json.loads(canonical_json(state)), indent=2, sort_keys=True, ensure_ascii=False
    )
    return "".join(
        json.dumps(ch)[1:-1] if ch != "\n" and unicodedata.category(ch) in _ESCAPED else ch
        for ch in text
    )


# ---- the parts -----------------------------------------------------------------------------------


def _candidate(candidate: Candidate, evidence: Sequence[Evidence], cap: int) -> dict[str, object]:
    part: dict[str, object] = {
        "type": type_words(candidate.type),
        "title": candidate.title,
        "facts": {
            name: value
            for name, value in candidate.words.items()
            if _is_word(name) and _is_word(value)
        },
        "evidence": [_evidence(entry, cap) for entry in evidence],
        "shown_elsewhere": list(candidate.shown_elsewhere),
    }
    project = _project(candidate.related_ref)
    if project is not None:
        part["project"] = project
    return part


def _evidence(entry: Evidence, cap: int) -> dict[str, object]:
    """One evidence entry: its ref and, unless cut to nothing, its excerpt."""
    excerpt = _cut(entry.excerpt, cap)
    return {"ref": entry.ref, "excerpt": excerpt} if excerpt else {"ref": entry.ref}


def _project(ref: str | None) -> str | None:
    """The `<slug>` of a `10-projects/<slug>/...` ref, else None."""
    parts = (ref or "").replace("\\", "/").split("/")
    if len(parts) >= 3 and parts[0].casefold() == _PROJECTS and parts[1]:
        return parts[1]
    return None


def _titles(rules: _Rules, mentions: Iterable[Mention]) -> list[str]:
    return [
        mention.title
        for mention in mentions
        if not _held(rules, mention.ref, mention.tags, mention.title)
    ]


def _pasts(rules: _Rules, pasts: Iterable[Past]) -> list[dict[str, object]]:
    """Each earlier item as {item, shown_as, outcome}; one never-send holds, or whose level or
    feedback is not a known value, is left out."""
    return [
        {"item": past.item.title, "shown_as": past.shown_as, "outcome": past.outcome}
        for past in pasts
        if past.shown_as in LEVELS
        and (past.outcome is None or past.outcome in _OUTCOMES)
        and not _held(rules, past.item.ref, past.item.tags, past.item.title)
    ]


def _active(rules: _Rules, active: Mention | None) -> str:
    """What is open: its title without numerals, or "" for nothing or a never-send note."""
    if active is None or _held(rules, active.ref, active.tags, active.title):
        return ""
    return " ".join("".join(ch for ch in active.title if not _is_numeral(ch)).split())


def _cut(text: str, limit: int) -> str:
    """`text` in at most `limit` characters, ending "…" when cut; "" for a limit of 0."""
    if limit <= 0:
        return ""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _size(state: Mapping[str, object]) -> int:
    """The UTF-8 bytes of `state`'s canonical JSON. It is ASCII-escaped, so it is at least the
    size of the same state written as raw UTF-8."""
    return len(canonical_json(state).encode("utf-8"))


def _is_word(value: object) -> bool:
    """A string holding no digit or other numeral (any Unicode number category)."""
    return isinstance(value, str) and not any(_is_numeral(ch) for ch in value)


def _is_numeral(ch: str) -> bool:
    return unicodedata.category(ch).startswith("N")


# ---- never-send ----------------------------------------------------------------------------------


def _rules(entries: Iterable[str], invalid: bool) -> _Rules | None:
    """The never-send rules, or None when they cannot be trusted: a stored list that could not
    be read, or an entry that is neither a folder ("…/") nor a tag ("#…"). An entry that is
    both counts as both."""
    if invalid:
        return None
    folders: list[tuple[str, ...]] = []
    tags: list[str] = []
    for entry in entries:
        if not isinstance(entry, str) or not (entry.endswith("/") or entry.startswith("#")):
            return None
        if entry.endswith("/"):
            folders.append(_segments(entry))
        if entry.startswith("#"):
            tags.append(_tag(entry))
    return _Rules(tuple(folders), tuple(tags))


def _note_blocked(rules: _Rules, candidate: Candidate) -> bool:
    """Whether the candidate's own note is never-send: its title carries a listed tag inline,
    its `related_ref` sits under a listed folder, or, with any tag rule, the evidence at that
    ref meets a listed tag (inline in its excerpt too), has unknown tags, or is missing, which
    leaves the note's tags unknown."""
    if _held(rules, None, (), candidate.title):
        return True
    ref = candidate.related_ref
    if ref is None:
        return False
    if _under(ref, rules.folders):
        return True
    if not rules.tags:
        return False
    own = _segments(ref)
    entries = [entry for entry in candidate.evidence if entry.ref and _segments(entry.ref) == own]
    return not entries or any(_held(rules, None, entry.tags, entry.excerpt) for entry in entries)


def _held(rules: _Rules, ref: str | None, tags: tuple[str, ...] | None, text: str) -> bool:
    """Whether never-send holds back an entry at `ref` carrying `tags` (None: unknown) whose
    `text` (a title or excerpt) may carry more tags inline."""
    if ref is not None and _under(ref, rules.folders):
        return True
    if not rules.tags:
        return False
    if tags is None:
        return True
    found = (*(_tag(tag) for tag in tags), *inline_tags(text))
    return any(_meets(tag, rule) for tag in found for rule in rules.tags)


def inline_tags(text: str) -> Iterator[str]:
    """The tags written inline in `text`, folded as a rule is, in the vault's (Obsidian's) tag
    syntax: a "#" right after no tag character, then the longest run of tag characters
    (letters, marks, numbers, "_", "-", and "/" for nesting), less a trailing "/", which must
    hold something besides numbers. So "see #Private/notes." carries "private/notes" and
    "#privateish" carries "privateish", while "C#", "mail#private" and "#1984" carry none.
    The text is folded first, so a full-width "#" counts as "#"."""
    folded = _fold(text)
    at = folded.find("#")
    while at != -1:
        end = at + 1
        if at == 0 or not _tag_char(folded[at - 1]):
            while end < len(folded) and _tag_char(folded[end]):
                end += 1
            name = folded[at + 1 : end].rstrip("/")
            if not all(_is_numeral(ch) for ch in name):
                yield name
        at = folded.find("#", end)


def _tag_char(ch: str) -> bool:
    """A character an inline tag may hold: a letter, mark or number, "_", "-" or "/"."""
    return ch in "_-/" or unicodedata.category(ch)[0] in "LMN"


def _under(ref: str, folders: Iterable[tuple[str, ...]]) -> bool:
    segments = _segments(ref)
    return any(segments[: len(folder)] == folder for folder in folders)


def _meets(tag: str, rule: str) -> bool:
    """A tag meets a rule that names it or a tag it is nested under ("health/diet" meets
    "health"); an empty rule ("#" alone) meets every tag."""
    return rule == "" or tag == rule or tag.startswith(rule + "/")


def _segments(path: str) -> tuple[str, ...]:
    """A path's segments for comparing: case and width folded, "\\" read as "/", and empty, "."
    and resolved ".." segments left out."""
    text = posixpath.normpath(_fold(path).replace("\\", "/"))
    return tuple(part for part in text.split("/") if part not in ("", "."))


def _tag(text: str) -> str:
    """A tag for comparing: case and width folded, one leading "#" and any trailing "/" off."""
    return _fold(text).strip().removeprefix("#").rstrip("/")


def _fold(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()
