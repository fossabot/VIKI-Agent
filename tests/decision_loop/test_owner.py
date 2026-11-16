"""Tests for `owner.fill`: how `state.Owner` is rebuilt from recently changed vault notes and
the scope's stored history (`stats.OwnerRows`), within `state.py`'s own caps. Pure: no store, no
I/O -- `OwnerRows` is built by hand here, never read from a database.

The cut-title tests at the end go through `state.build_state` too: a cut title must not
let a never-send tag written past the cut slip through.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from viki_agent.decision_loop import owner
from viki_agent.decision_loop.contracts import Candidate, DecisionSettings, Evidence, canonical_json
from viki_agent.decision_loop.state import (
    MUTED_OR_WRONG,
    RECENT,
    RECENT_FEEDBACK,
    BuiltState,
    History,
    Mention,
    Now,
    Owner,
    Past,
    build_state,
)
from viki_agent.decision_loop.stats import OwnerRows

GOALS = (Mention("Ship PR D", "10-projects/viki-agent/viki-agent.md", ("project",)),)
BASE = Owner(goals=GOALS)
REF = "10-projects/x/tasks.md"


def _mention(key: str) -> Mention:
    return Mention(f"title-{key}", f"30-knowledge/{key}.md", ("note",))


def _titles(known: dict[str, Mention]) -> Callable[[str], Mention | None]:
    """A `title_of` double answering *known*'s Mention for a key it holds, else None."""
    return known.get


def _candidate() -> Candidate:
    """A candidate `build_state` never blocks: its own evidence matches `related_ref`, and
    neither carries any never-send tag, so only the `owner` part is under test."""
    return Candidate(
        item_key="a" * 64,
        type="tasks_due",
        title="Ship the thing",
        facts={},
        words={},
        evidence=(Evidence(REF, "Ship the thing by Friday", False, ()),),
        related_chat=None,
        related_ref=REF,
        deadline=None,
        shown_elsewhere=(),
        rules_evidence={},
        origin_session=None,
        work_allowed=("none",),
    )


def _now() -> Now:
    return Now(None, False, False, datetime(2026, 10, 23, 9, 0, tzinfo=UTC), 0)


def _sent(built: BuiltState) -> dict[str, Any]:
    """The built state as plain JSON values (untyped, for reading nested parts), as
    test_state.py's own `_sent` does."""
    return json.loads(canonical_json(built.state))


def test_fill_caps_and_order() -> None:
    """`recent` runs changed-then-seen, deduped, capped at RECENT; `muted_or_wrong` caps at
    MUTED_OR_WRONG; `recent_feedback` caps at RECENT_FEEDBACK -- each fed more rows than its cap
    here, each kept in the order its rows arrived (newest first)."""
    changed = (Mention("Changed note", "30-knowledge/a.md", ("note",)),)
    seen_keys = [f"seen{n}" for n in range(RECENT + 3)]
    marked_keys = [f"marked{n}" for n in range(MUTED_OR_WRONG + 3)]
    feedback_keys = [f"feedback{n}" for n in range(RECENT_FEEDBACK + 3)]
    known = {key: _mention(key) for key in (*seen_keys, *marked_keys, *feedback_keys)}
    rows = OwnerRows(
        seen=tuple((key, "list") for key in seen_keys),
        marked=tuple(marked_keys),
        feedback=tuple((key, "badge", "useful") for key in feedback_keys),
    )

    result = owner.fill(BASE, rows, _titles(known), changed)

    assert len(result.recent) == RECENT
    assert result.recent[0] == changed[0]  # changed notes come first
    assert result.recent[1:] == tuple(_mention(key) for key in seen_keys[: RECENT - 1])
    assert result.muted_or_wrong == tuple(_mention(key) for key in marked_keys[:MUTED_OR_WRONG])
    assert result.recent_feedback == tuple(
        Past(_mention(key), "badge", "useful") for key in feedback_keys[:RECENT_FEEDBACK]
    )


def test_goals_are_kept_unchanged() -> None:
    result = owner.fill(BASE, OwnerRows(), lambda key: None, ())
    assert result == Owner(goals=GOALS)


def test_recent_dedupes_changed_and_seen_by_title_and_ref() -> None:
    same = Mention("Same note", "30-knowledge/dup.md", ("note",))
    rows = OwnerRows(seen=(("seen-dup", "list"), ("seen-new", "list")))
    known = {"seen-dup": same, "seen-new": _mention("new")}
    result = owner.fill(BASE, rows, _titles(known), (same,))
    assert result.recent == (same, _mention("new"))


def test_a_key_with_no_title_is_skipped() -> None:
    rows = OwnerRows(
        seen=(("gone", "list"), ("here", "list")),
        marked=("gone", "here"),
        feedback=(("gone", "list", "useful"), ("here", "badge", "wrong")),
    )
    known = {"here": _mention("here")}
    result = owner.fill(BASE, rows, _titles(known), ())
    assert result.recent == (_mention("here"),)
    assert result.muted_or_wrong == (_mention("here"),)
    assert result.recent_feedback == (Past(_mention("here"), "badge", "wrong"),)


def test_titles_land_cut_to_120_characters_never_as_an_excerpt() -> None:
    """A cut title's tags go to None (unknown), not the mention's own tags -- a never-send
    tag written inline past the cut would otherwise be invisible to `state._held`."""
    long_title = "x" * 200
    mention = Mention(long_title, "30-knowledge/long.md", ("note",))
    cut = Mention(long_title[:120], mention.ref, None)
    rows = OwnerRows(seen=(("k", "list"),), marked=("k",), feedback=(("k", "list", "useful"),))
    result = owner.fill(BASE, rows, lambda key: mention, ())
    assert result.recent == (cut,)
    assert result.muted_or_wrong == (cut,)
    assert result.recent_feedback == (Past(cut, "list", "useful"),)


def test_a_changed_notes_long_title_is_also_cut() -> None:
    """Same as above, for a `changed` mention (no `title_of` lookup involved)."""
    long_title = "y" * 150
    changed = (Mention(long_title, "30-knowledge/changed.md", ("note",)),)
    result = owner.fill(BASE, OwnerRows(), lambda key: None, changed)
    assert result.recent == (Mention(long_title[:120], changed[0].ref, None),)


def test_a_cut_title_losing_its_inline_never_send_tag_is_still_held_back() -> None:
    """A 126-character changed-note title carries "#private" at index 118. Cut to 120
    characters it would leave only a stray "#p" that no never-send rule matches -- the bug --
    so `_cut` marks a cut title's tags unknown instead, and `state.build_state` holds any
    unknown-tagged entry back while a tag rule exists, the never-send list included."""
    long_title = "z" * 117 + " #private"
    assert len(long_title) == 126
    assert long_title[118] == "#"
    changed = (Mention(long_title, "30-knowledge/long.md", ()),)
    filled = owner.fill(BASE, OwnerRows(), lambda key: None, changed)
    built = build_state(
        _candidate(), filled, _now(), History(), DecisionSettings(never_send=("#private",))
    )
    assert _sent(built)["owner"]["recent"] == []


def test_an_uncut_title_with_the_same_inline_tag_is_held_by_the_inline_rule_alone() -> None:
    """The same shape kept under 120 characters is never cut, so it keeps its own (empty) tags
    and is still held back -- by the inline-tag scan alone, unchanged by the cut-title fix above."""
    short_title = "z" * 110 + " #private"
    assert len(short_title) < 120
    changed = (Mention(short_title, "30-knowledge/short.md", ()),)
    filled = owner.fill(BASE, OwnerRows(), lambda key: None, changed)
    built = build_state(
        _candidate(), filled, _now(), History(), DecisionSettings(never_send=("#private",))
    )
    assert _sent(built)["owner"]["recent"] == []
