"""state.py, the privacy boundary: the exact keys, word facts without numerals, excerpts cut to
the send level, never-send dropped and counted, a blocked candidate sending nothing, the
12,000-byte budget, outside text kept under `outside`, and a state hash that only the item's
own state moves."""

from __future__ import annotations

import ast
import json
import re
import unicodedata
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from viki_agent.decision_loop import contracts, state, words
from viki_agent.decision_loop.contracts import (
    STATE_BYTES,
    Candidate,
    DecisionSettings,
    Evidence,
    canonical_json,
    parse_settings,
    sha256_hex,
)
from viki_agent.decision_loop.playbook import (
    GUIDE,
    PLAYBOOK_VERSION,
    estimate_tokens,
    questions,
    questions_text,
)
from viki_agent.decision_loop.state import (
    BuiltState,
    History,
    Mention,
    Now,
    Owner,
    Past,
    build_state,
    state_text,
)

TOKYO = timezone(timedelta(hours=9))
MORNING = datetime(2026, 10, 22, 10, 30, tzinfo=TOKYO)
TASKS = "10-projects/viki/tasks.md"
KANJI = chr(0x65E5)  # built at runtime, never typed as a literal
NUMERALS = (chr(0xFF13), chr(0x0663), chr(0x216B), chr(0x2460), chr(0xB2))  # wide 3, Arabic 3,
# Roman twelve, circled one, superscript two


def _candidate(**changes: Any) -> Candidate:
    base = Candidate(
        item_key="a" * 64,
        type="tasks_due",
        title="Ship the launcher by 10/24",
        facts={"due": MORNING + timedelta(days=1)},
        words={"due": "due tomorrow"},
        evidence=(Evidence(TASKS, "Ship the launcher by Friday", False, ()),),
        related_chat=None,
        related_ref=TASKS,
        deadline=MORNING + timedelta(days=1),
        shown_elsewhere=(),
        rules_evidence={},
        origin_session=None,
        work_allowed=("none",),
    )
    return replace(base, **changes)


def _now(**changes: Any) -> Now:
    return replace(Now(None, False, False, MORNING, 0), **changes)


def _never_send(*entries: str, **changes: Any) -> DecisionSettings:
    return replace(DecisionSettings(), never_send=entries, **changes)


def _build(
    candidate: Candidate | None = None,
    owner: Owner | None = None,
    now: Now | None = None,
    history: History | None = None,
    settings: DecisionSettings | None = None,
) -> BuiltState:
    return build_state(
        candidate or _candidate(),
        owner or Owner(),
        now or _now(),
        history or History(),
        settings or DecisionSettings(),
    )


def _sent(built: BuiltState) -> dict[str, Any]:
    """The built state as plain JSON values (untyped, for reading nested parts)."""
    return json.loads(canonical_json(built.state))


def _size(built: BuiltState) -> int:
    """The bytes the budget counts: the state's canonical JSON less the guide, which rides on top
    of the budget as the questions do."""
    item = {part: value for part, value in built.state.items() if part != "guide"}
    return len(canonical_json(item).encode("utf-8"))


def _chat(title: str) -> Mention:
    return Mention(title, None, ())


def _past(title: str, ref: str | None = None, tags: tuple[str, ...] | None = ()) -> Past:
    return Past(Mention(title, ref, tags), "badge", "useful")


def _numerals_in(value: object) -> list[str]:
    """Every digit or other numeral in `value`'s JSON, read without ASCII escapes (an escape's
    hex would look like digits)."""
    text = json.dumps(value, ensure_ascii=False)
    return [ch for ch in text if unicodedata.category(ch).startswith("N")] + re.findall(r"\d", text)


def _assert_blocked(built: BuiltState, held_back: int) -> None:
    assert built.blocked
    assert built.state == {}
    assert built.split == ()
    assert built.state_hash is None
    assert built.held_back == held_back
    assert state_text(built.state) == "{}"


# ---- shape ---------------------------------------------------------------------------------------


def test_state_has_exactly_the_designed_keys() -> None:
    candidate = _candidate(
        evidence=(
            Evidence(TASKS, "Ship the launcher by Friday", False, ()),
            Evidence(None, "", False, ()),
            Evidence("https://example.com/post", "A reader says hello", True, ()),
        ),
        shown_elsewhere=("open note",),
    )
    built = _build(
        candidate,
        Owner(
            goals=(Mention("VIKI Agent", "10-projects/viki/viki.md", ("project",)),),
            recent=(_chat("Fix the launcher"),),
            muted_or_wrong=(_chat("uncommitted changes"),),
            recent_feedback=(_past("Review the PR"),),
        ),
        _now(active=_chat("Launcher work"), focus=True, typing=False, nudges_this_hour=2),
        History(similar=(_past("Ship the panel"),), counts={"shown": 4, "useful": 1}),
    )
    sent = _sent(built)
    assert not built.blocked
    assert set(sent) == {"guide", "candidate", "owner", "now", "history", "outside"}
    assert sent["guide"] == list(GUIDE)
    assert sent["candidate"] == {
        "type": "task due",
        "title": "Ship the launcher by 10/24",
        "project": "viki",
        "facts": {"due": "due tomorrow"},
        "evidence": [{"ref": TASKS, "excerpt": "Ship the launcher by Friday"}, {"ref": None}],
        "shown_elsewhere": ["open note"],
    }
    assert sent["owner"] == {
        "goals": ["VIKI Agent"],
        "recent": ["Fix the launcher"],
        "muted_or_wrong": ["uncommitted changes"],
        "recent_feedback": [{"item": "Review the PR", "shown_as": "badge", "outcome": "useful"}],
    }
    assert sent["now"] == {
        "active": "Launcher work",
        "focus": True,
        "typing": False,
        "time": "morning",
        "nudges_this_hour": "two",
    }
    assert sent["history"] == {
        "similar": [{"item": "Ship the panel", "shown_as": "badge", "outcome": "useful"}],
        "counts": {"shown": "a few", "useful": "one"},
    }
    assert sent["outside"] == [
        {"ref": "https://example.com/post", "excerpt": "A reader says hello"}
    ]
    assert re.fullmatch(r"[0-9a-f]{64}", built.state_hash or "")


def test_project_only_for_a_ref_inside_a_project_folder() -> None:
    for ref in (None, "00-inbox/capture.md", "10-projects/tasks.md", "notes/10-projects/x/y.md"):
        sent = _sent(_build(_candidate(related_ref=ref, evidence=())))
        assert "project" not in sent["candidate"], ref
    windows = _sent(_build(_candidate(related_ref="10-Projects\\viki\\tasks.md")))
    assert windows["candidate"]["project"] == "viki"


def test_lists_are_cut_to_their_sizes_keeping_the_first() -> None:
    titles = [f"t{i}" for i in range(12)]
    built = _build(
        owner=Owner(
            goals=tuple(_chat(t) for t in titles),
            recent=tuple(_chat(t) for t in titles),
            muted_or_wrong=tuple(_chat(t) for t in titles),
            recent_feedback=tuple(_past(t) for t in titles),
        ),
        history=History(similar=tuple(_past(t) for t in titles)),
    )
    sent = _sent(built)
    assert sent["owner"]["goals"] == titles[: state.GOALS]
    assert sent["owner"]["recent"] == titles[: state.RECENT]
    assert sent["owner"]["muted_or_wrong"] == titles[: state.MUTED_OR_WRONG]
    assert [p["item"] for p in sent["owner"]["recent_feedback"]] == titles[: state.RECENT_FEEDBACK]
    assert [p["item"] for p in sent["history"]["similar"]] == titles[: state.SIMILAR]
    assert (state.GOALS, state.RECENT, state.MUTED_OR_WRONG) == (8, 5, 5)
    assert (state.RECENT_FEEDBACK, state.SIMILAR) == (5, 5)


def test_unknown_levels_and_outcomes_are_left_out() -> None:
    pasts = (
        Past(_chat("kept"), "nudge", None),
        Past(_chat("bad level"), "loud", "useful"),
        Past(_chat("bad outcome"), "list", "meh"),
        Past(_chat("held"), "silent", "right_to_hold"),
    )
    sent = _sent(_build(history=History(similar=pasts)))
    assert sent["history"]["similar"] == [
        {"item": "kept", "shown_as": "nudge", "outcome": None},
        {"item": "held", "shown_as": "silent", "outcome": "right_to_hold"},
    ]


def test_split_estimates_each_part_and_then_the_questions() -> None:
    built = _build(owner=Owner(goals=(_chat("VIKI Agent"),)))
    assert [name for name, _ in built.split] == [*state.PARTS, "questions"]
    assert state.PARTS == ("guide", "candidate", "owner", "now", "history", "outside")
    tokens = dict(built.split)
    for part in state.PARTS:
        assert tokens[part] == estimate_tokens(canonical_json(built.state[part]))
    assert tokens["questions"] == estimate_tokens(questions_text(questions()))


def test_state_text_is_the_sent_json_indented() -> None:
    built = _build(_candidate(title=f"Plan {KANJI}"))
    text = state_text(built.state)
    plain = json.loads(canonical_json(built.state))
    assert text == json.dumps(plain, indent=2, sort_keys=True, ensure_ascii=False)
    assert '\n  "candidate": {\n' in text
    assert json.loads(text) == plain


# Captured from build_state before state_text showed characters as themselves, and again when
# the playbook version the hash names became "2".
JAPANESE_TITLE_HASH = "f6675d0739f2fd4b19e8b9b7a618a0f7e904475c964fd5e2d5a4ece3e090d972"


def test_state_text_shows_a_japanese_title_as_itself_and_leaves_the_hash() -> None:
    title = "".join(chr(code) for code in (0x65E5, 0x672C, 0x8A9E)) + " plan"
    built = _build(_candidate(title=title))
    text = state_text(built.state)
    assert f'"title": "{title}"' in text
    assert chr(92) + "u" not in text  # no escapes at all in this state
    assert built.state_hash == JAPANESE_TITLE_HASH
    own = {
        "candidate": built.state["candidate"],
        "outside": built.state["outside"],
        "goals": [],
        "playbook_version": PLAYBOOK_VERSION,
        "model_version": "pinned",
    }
    assert canonical_json(own).isascii()  # the hash still reads the escaped canonical form
    assert built.state_hash == sha256_hex(canonical_json(own))


def test_state_text_keeps_control_and_format_characters_escaped() -> None:
    hidden = (
        chr(0x7F),  # delete (Cc)
        chr(0x85),  # next line (Cc)
        chr(0x200B),  # zero-width space (Cf)
        chr(0x202E),  # right-to-left override (Cf)
        chr(0xE0001),  # language tag, outside the BMP (Cf)
        chr(0x2028),  # line separator (Zl)
        chr(0x2029),  # paragraph separator (Zp)
        chr(0xD800),  # a lone surrogate (Cs)
    )
    title = "a" + "b".join(hidden) + f"c {KANJI}"
    evidence = (Evidence(TASKS, "tab\there\nnew line\r" + chr(0), False, ()),)
    built = _build(_candidate(title=title, evidence=evidence))
    text = state_text(built.state)
    shown = {ch for ch in text if unicodedata.category(ch) in {"Cc", "Cf", "Cs", "Zl", "Zp"}}
    assert shown == {"\n"}  # only the JSON's own line breaks
    for ch in hidden:
        assert json.dumps(ch)[1:-1] in text  # shown as its JSON escape
    assert KANJI in text
    assert json.loads(text) == json.loads(canonical_json(built.state))
    text.encode("utf-8")  # no raw surrogate left to break the encoding


# ---- send levels ---------------------------------------------------------------------------------


@pytest.mark.parametrize(("level", "cap"), [("titles", 0), ("excerpts", 200), ("sections", 2000)])
def test_send_levels_cap_excerpts(level: str, cap: int) -> None:
    assert contracts.EXCERPT[level] == cap
    texts = ("a" * (cap - 1), "b" * cap, "c" * (cap + 1), "d" * 5000)
    candidate = _candidate(
        evidence=(
            *(Evidence(f"notes/{i}.md", text, False, ()) for i, text in enumerate(texts)),
            Evidence("https://example.com", "e" * 5000, True, ()),
        )
    )
    sent = _sent(_build(candidate, settings=replace(DecisionSettings(), send_level=level)))
    excerpts = [entry.get("excerpt", "") for entry in sent["candidate"]["evidence"]]
    outside = sent["outside"][0]["excerpt"]
    if cap == 0:
        assert all(set(entry) == {"ref"} for entry in sent["candidate"]["evidence"])
        assert outside == ""
        return
    assert excerpts[:2] == [texts[0], texts[1]]  # at or under the cap: unchanged
    assert excerpts[2] == texts[2][: cap - 1] + "…"
    assert excerpts[3] == texts[3][: cap - 1] + "…"
    assert outside == "e" * (cap - 1) + "…"
    assert max(len(text) for text in [*excerpts, outside]) == cap


def test_the_default_level_sends_two_hundred_characters() -> None:
    long = _candidate(evidence=(Evidence(TASKS, "x" * 500, False, ()),))
    for settings in (DecisionSettings(), parse_settings({})):
        sent = _sent(_build(long, settings=settings))
        assert len(sent["candidate"]["evidence"][0]["excerpt"]) == 200


def test_an_unknown_send_level_sends_no_excerpt() -> None:
    long = _candidate(evidence=(Evidence(TASKS, "x" * 500, False, ()),))
    sent = _sent(_build(long, settings=replace(DecisionSettings(), send_level="everything")))
    assert sent["candidate"]["evidence"] == [{"ref": TASKS}]


# ---- never-send ----------------------------------------------------------------------------------


def test_never_send_drops_and_counts() -> None:
    evidence = (
        Evidence(TASKS, "kept: the task itself", False, ()),
        Evidence("health/diary.md", "SECRET-1", False, ()),
        Evidence("Health/Sub/visit.md", "SECRET-2", False, ()),
        Evidence("notes\\..\\HEALTH\\plan.md", "SECRET-3", False, ()),
        Evidence("healthcare/plan.md", "kept: another folder", False, ()),
        Evidence("notes/a.md", "SECRET-4", False, ("private",)),
        Evidence("notes/b.md", "SECRET-5", False, ("#PRIVATE/Clients",)),
        Evidence("notes/c.md", "kept: privateer is not private", False, ("privateer",)),
        Evidence("health/web.md", "SECRET-6", True, ()),
        Evidence("https://example.com", "kept: outside", True, ()),
    )
    built = _build(
        _candidate(evidence=evidence),
        Owner(
            goals=(
                Mention("SECRET-goal-folder", "Health/goals.md", ()),
                Mention("SECRET-goal-tag", "10-projects/x/x.md", ("private",)),
                Mention("SECRET-goal-unknown", "10-projects/y/y.md", None),
                Mention("kept goal", "10-projects/viki/viki.md", ("project",)),
            ),
            recent=(Mention("SECRET-recent", "health/x.md", ()), _chat("kept recent")),
            muted_or_wrong=(Mention("SECRET-muted", None, ("private",)),),
            recent_feedback=(_past("SECRET-feedback", "health/y.md"),),
        ),
        _now(active=Mention("SECRET-active", "health/today.md", ())),
        History(similar=(_past("SECRET-similar", "notes/z.md", ("private",)), _past("kept"))),
        _never_send("Health/", "#Private"),
    )
    assert not built.blocked
    assert built.held_back == 6  # evidence entries only, outside ones included
    text = canonical_json(built.state)
    assert "SECRET" not in text
    sent = _sent(built)
    assert [entry["excerpt"] for entry in sent["candidate"]["evidence"]] == [
        "kept: the task itself",
        "kept: another folder",
        "kept: privateer is not private",
    ]
    assert sent["outside"] == [{"ref": "https://example.com", "excerpt": "kept: outside"}]
    assert sent["owner"]["goals"] == ["kept goal"]
    assert sent["owner"]["recent"] == ["kept recent"]
    assert sent["owner"]["muted_or_wrong"] == []
    assert sent["owner"]["recent_feedback"] == []
    assert sent["now"]["active"] == ""
    assert [p["item"] for p in sent["history"]["similar"]] == ["kept"]


def test_never_send_folders_match_whole_segments_ignoring_case_and_width() -> None:
    wide_h = chr(0xFF28)  # a full-width H
    evidence = tuple(
        Evidence(ref, "x", False, ())
        for ref in (
            "/health/a.md",
            "./Health//b.md",
            f"{wide_h}ealth/c.md",
            "health",
            "Clients/Acme/notes.md",
            "clients/acme-two/notes.md",
            "Clients/notes.md",
            "health.md",
        )
    )
    built = _build(_candidate(evidence=evidence), settings=_never_send("health/", "clients/ACME/"))
    assert [entry["ref"] for entry in _sent(built)["candidate"]["evidence"]] == [
        "clients/acme-two/notes.md",
        "Clients/notes.md",
        "health.md",
    ]
    assert built.held_back == 5


def test_a_root_folder_rule_and_a_bare_tag_rule_hold_everything_they_can() -> None:
    evidence = (
        Evidence("notes/a.md", "x", False, ()),
        Evidence(None, "app fact", False, ()),
        Evidence(None, "tagged", False, ("anything",)),
    )
    root = _build(_candidate(related_ref=None, evidence=evidence), settings=_never_send("/"))
    assert [entry["ref"] for entry in _sent(root)["candidate"]["evidence"]] == [None, None]
    bare = _build(_candidate(related_ref=None, evidence=evidence), settings=_never_send("#"))
    assert _sent(bare)["candidate"]["evidence"] == [
        {"ref": "notes/a.md", "excerpt": "x"},
        {"ref": None, "excerpt": "app fact"},
    ]
    assert (root.held_back, bare.held_back) == (1, 1)


def test_an_entry_that_is_both_a_folder_and_a_tag_holds_as_both() -> None:
    evidence = (
        Evidence("#private/a.md", "x", False, ()),
        Evidence("notes/b.md", "x", False, ("private",)),
        Evidence("notes/c.md", "x", False, ()),
    )
    built = _build(
        _candidate(related_ref=None, evidence=evidence), settings=_never_send("#private/")
    )
    assert [entry["ref"] for entry in _sent(built)["candidate"]["evidence"]] == ["notes/c.md"]


def test_unknown_tags_with_a_tag_rule_are_dropped() -> None:
    evidence = (
        Evidence(TASKS, "own note", False, ()),
        Evidence("notes/unknown.md", "UNKNOWN", False, None),
        Evidence("notes/none.md", "no tags", False, ()),
        Evidence(None, "UNKNOWN-app", False, None),
        Evidence("https://example.com", "UNKNOWN-outside", True, None),
    )
    tagged = _build(_candidate(evidence=evidence), settings=_never_send("#private"))
    assert tagged.held_back == 3
    assert "UNKNOWN" not in canonical_json(tagged.state)
    assert [entry["ref"] for entry in _sent(tagged)["candidate"]["evidence"]] == [
        TASKS,
        "notes/none.md",
    ]
    stored = parse_settings({"decision.never_send": '["#private"]'})
    assert _build(_candidate(evidence=evidence), settings=stored).held_back == 3
    for settings in (DecisionSettings(), _never_send("health/")):
        untagged = _build(_candidate(evidence=evidence), settings=settings)
        assert untagged.held_back == 0
        assert "UNKNOWN-outside" in canonical_json(untagged.state)


@pytest.mark.parametrize(
    ("related_ref", "evidence", "settings"),
    [
        ("Health/visit.md", (Evidence("Health/visit.md", "x", False, ()),), _never_send("health/")),
        ("health/visit.md", (Evidence(TASKS, "x", False, ()),), _never_send("HEALTH/")),
        (
            "notes/x.md",
            (Evidence("Notes/X.md", "x", False, ("Private",)),),
            _never_send("#private"),
        ),
        ("notes/x.md", (Evidence("notes/x.md", "x", False, None),), _never_send("#private")),
        ("notes/x.md", (Evidence("notes/y.md", "x", False, ()),), _never_send("#private")),
        (TASKS, (Evidence(TASKS, "x", False, ()),), _never_send(never_send_invalid=True)),
        (None, (Evidence(TASKS, "x", False, ()),), _never_send(never_send_invalid=True)),
        (TASKS, (Evidence(TASKS, "x", False, ()),), _never_send("health")),
        (
            TASKS,
            (Evidence(TASKS, "x", False, ()), Evidence("notes/y.md", "y", False, ())),
            parse_settings({"decision.never_send": "!unreadable"}),
        ),
    ],
    ids=[
        "folder",
        "folder-without-own-evidence",
        "own-tag",
        "own-tags-unknown",
        "own-tags-missing",
        "invalid-list",
        "invalid-list-without-a-ref",
        "neither-folder-nor-tag",
        "damaged-stored-value",
    ],
)
def test_blocked_related_ref_sends_nothing(
    related_ref: str | None, evidence: tuple[Evidence, ...], settings: DecisionSettings
) -> None:
    built = _build(_candidate(related_ref=related_ref, evidence=evidence), settings=settings)
    _assert_blocked(built, held_back=len(evidence))


def test_a_related_ref_outside_never_send_is_sent() -> None:
    evidence = (Evidence("healthcare/x.md", "x", False, ("public",)),)
    for settings in (_never_send("health/"), _never_send("health/", "#private")):
        built = _build(
            _candidate(related_ref="healthcare/x.md", evidence=evidence), settings=settings
        )
        assert not built.blocked
        assert _sent(built)["candidate"]["evidence"] == [{"ref": "healthcare/x.md", "excerpt": "x"}]


# ---- paired items: a never-send hit on either note blocks the whole pair -----------------


@pytest.mark.parametrize("candidate_type", contracts.PAIRED)
def test_paired_blocked_by_never_send_folder(candidate_type: str) -> None:
    """A note_link/note_superseded candidate rests on two notes, but only the first is
    `related_ref` — `_note_blocked` (and so the early `rules is None or _note_blocked(...)`
    check above) never looks at the second on its own. A never-send folder on that *second*
    note must still silence the whole pair rather than just narrow its evidence: asking about
    half a blocked pair would itself describe the other half."""
    evidence = (
        Evidence(TASKS, "own note, the pair's first half", False, ()),
        Evidence("health/other.md", "SECRET: the pair's second half", False, ()),
    )
    candidate = _candidate(type=candidate_type, related_ref=TASKS, evidence=evidence)
    built = _build(candidate, settings=_never_send("health/"))
    _assert_blocked(built, held_back=1)


@pytest.mark.parametrize("candidate_type", contracts.PAIRED)
def test_paired_blocked_by_never_send_tag(candidate_type: str) -> None:
    """Same as `test_paired_blocked_by_never_send_folder`, for a tag rule on the second note."""
    evidence = (
        Evidence(TASKS, "own note", False, ()),
        Evidence("notes/other.md", "SECRET", False, ("private",)),
    )
    candidate = _candidate(type=candidate_type, related_ref=TASKS, evidence=evidence)
    built = _build(candidate, settings=_never_send("#private"))
    _assert_blocked(built, held_back=1)


def test_tasks_ticked_not_blocked_by_held_back_evidence() -> None:
    """Contrast with the two tests above: `tasks_ticked` rests on one ref, not a pair, and is
    not in `contracts.PAIRED`, so held-back evidence only narrows what is sent, as for any
    ordinary candidate — but that evidence is still never sent."""
    evidence = (
        Evidence(TASKS, "own note", False, ()),
        Evidence("health/other.md", "SECRET", False, ()),
    )
    candidate = _candidate(type="tasks_ticked", related_ref=TASKS, evidence=evidence)
    built = _build(candidate, settings=_never_send("health/"))
    assert not built.blocked
    assert built.held_back == 1
    assert "SECRET" not in canonical_json(built.state)
    assert [entry["ref"] for entry in _sent(built)["candidate"]["evidence"]] == [TASKS]


# ---- inline tags ---------------------------------------------------------------------------------


WIDE_HASH = chr(0xFF03)  # a full-width "#", which NFKC folds to "#"


def test_an_inline_never_send_tag_in_the_title_blocks_the_candidate() -> None:
    evidence = (Evidence(TASKS, "Book the follow-up", False, ()),)
    for title in (
        "Book the follow-up #private",
        "#Private/health: book the follow-up",
        f"Book the follow-up {WIDE_HASH}PRIVATE",
    ):
        built = _build(_candidate(title=title, evidence=evidence), settings=_never_send("#private"))
        _assert_blocked(built, held_back=1)
    loose = _candidate(title="Call the bank #private", related_ref=None, evidence=())
    _assert_blocked(_build(loose, settings=_never_send("#private")), held_back=0)
    for settings in (DecisionSettings(), _never_send("#work"), _never_send("health/")):
        sent = _build(_candidate(title="Book it #private", evidence=evidence), settings=settings)
        assert not sent.blocked
        assert _sent(sent)["candidate"]["title"] == "Book it #private"
    other = _candidate(title="Book it #privateish", evidence=evidence)
    assert not _build(other, settings=_never_send("#private")).blocked


def test_an_inline_never_send_tag_in_an_excerpt_drops_that_evidence() -> None:
    evidence = (
        Evidence(TASKS, "Ship the launcher by Friday", False, ()),
        Evidence("notes/a.md", "SECRET-1: call Ann about the scan #Private/notes", False, ()),
        Evidence("notes/b.md", "kept: #privateish is another tag", False, ()),
        # Past the 200-character cut, the tag still marks the text before it.
        Evidence("notes/c.md", "SECRET-2 " + "x" * 300 + " #private", False, ()),
        Evidence("https://example.com", "SECRET-3 #private", True, ()),
    )
    built = _build(_candidate(evidence=evidence), settings=_never_send("#private"))
    assert not built.blocked
    assert built.held_back == 3
    assert "SECRET" not in canonical_json(built.state)
    sent = _sent(built)
    assert [entry["ref"] for entry in sent["candidate"]["evidence"]] == [TASKS, "notes/b.md"]
    assert sent["outside"] == []


@pytest.mark.parametrize(
    ("text", "held"),
    [
        ("#private", True),
        ("see #private.", True),
        ("(#private)", True),
        ("##private", True),
        ("#PRIVATE", True),
        (f"{WIDE_HASH}private", True),
        ("#private/notes/x", True),
        ("#private/", True),
        (f"#private{KANJI}", False),
        ("#privateish", False),
        ("#private_x", False),
        ("#private-x", False),
        ("mail#private", False),
        ("a/#private", False),
        ("# private", False),
        ("private", False),
    ],
)
def test_inline_tags_follow_the_vault_tag_syntax(text: str, held: bool) -> None:
    evidence = (Evidence(TASKS, "own", False, ()), Evidence("notes/a.md", text, False, ()))
    built = _build(_candidate(evidence=evidence), settings=_never_send("#private"))
    assert built.held_back == int(held)


def test_inline_tags_read_any_script() -> None:
    # Katakana "private", and its half-width spelling, which NFKC folds to the same tag.
    tag = "".join(chr(code) for code in (0x30D7, 0x30E9, 0x30A4, 0x30D9, 0x30FC, 0x30C8))
    half = "".join(chr(code) for code in (0xFF8C, 0xFF9F, 0xFF97, 0xFF72, 0xFF8D, 0xFF9E))
    half += "".join(chr(code) for code in (0xFF70, 0xFF84))
    # A Devanagari tag with a virama (a combining mark NFKC keeps): marks belong to the tag.
    ksha = "".join(chr(code) for code in (0x0915, 0x094D, 0x0937))
    evidence = (
        Evidence("notes/a.md", f"memo {WIDE_HASH}{half}", False, ()),
        Evidence("notes/b.md", f"note #{ksha}", False, ()),
        Evidence("notes/c.md", f"note #{ksha[0]}", False, ()),
    )
    candidate = _candidate(related_ref=None, evidence=evidence)
    built = _build(candidate, settings=_never_send(f"#{tag}", f"#{ksha}"))
    assert [entry["ref"] for entry in _sent(built)["candidate"]["evidence"]] == ["notes/c.md"]
    short = _build(candidate, settings=_never_send(f"#{ksha[0]}"))
    assert [entry["ref"] for entry in _sent(short)["candidate"]["evidence"]] == [
        "notes/a.md",
        "notes/b.md",
    ]


def test_a_number_alone_is_no_inline_tag() -> None:
    evidence = (
        Evidence("notes/a.md", "PR #114, issue #7", False, ()),
        Evidence("notes/b.md", "the #y1984 plan", False, ()),
    )
    built = _build(_candidate(related_ref=None, evidence=evidence), settings=_never_send("#"))
    assert [entry["ref"] for entry in _sent(built)["candidate"]["evidence"]] == ["notes/a.md"]


def test_an_inline_never_send_tag_in_the_related_refs_excerpt_blocks() -> None:
    # The excerpt at related_ref is the candidate's own note: a tag written in it marks the
    # note, as one in its frontmatter would.
    oldest = "00-inbox/a.md"
    evidence = (
        Evidence(oldest, "Biopsy results next week #private", False, ()),
        Evidence("00-inbox/b.md", "Call the bank", False, ()),
    )
    inbox = _candidate(
        type="inbox", title=words.type_words("inbox"), related_ref=oldest, evidence=evidence
    )
    _assert_blocked(_build(inbox, settings=_never_send("#private")), held_back=2)
    assert not _build(inbox, settings=_never_send("#work")).blocked


def test_inline_never_send_tags_hold_back_titles_in_owner_now_and_history() -> None:
    built = _build(
        owner=Owner(
            goals=(_chat("SECRET goal #private"), _chat("kept goal")),
            recent=(_chat("SECRET recent #Private/x"),),
            muted_or_wrong=(_chat("SECRET muted #private"),),
            recent_feedback=(_past("SECRET feedback #private"),),
        ),
        now=_now(active=_chat("SECRET active #private")),
        history=History(similar=(_past("SECRET similar #private"), _past("kept #privateish"))),
        settings=_never_send("#private"),
    )
    assert (built.blocked, built.held_back) == (False, 0)
    assert "SECRET" not in canonical_json(built.state)
    sent = _sent(built)
    assert sent["owner"]["goals"] == ["kept goal"]
    assert sent["now"]["active"] == ""
    assert [p["item"] for p in sent["history"]["similar"]] == ["kept #privateish"]


# ---- budget --------------------------------------------------------------------------------------


def test_state_fits_12000_bytes() -> None:
    # One similar item too many: only history.similar shrinks, from the end.
    pasts = tuple(_past(f"s{i}-" + "x" * 2600) for i in range(5))
    built = _build(
        _candidate(evidence=(Evidence(TASKS, "y" * 300, False, ()),)), history=History(pasts)
    )
    sent = _sent(built)
    assert _size(built) <= STATE_BYTES
    assert [p["item"] for p in sent["history"]["similar"]] == [p.item.title for p in pasts[:4]]
    assert sent["candidate"]["evidence"][0]["excerpt"] == "y" * 199 + "…"

    # Too much evidence at 200 characters: every similar item goes, then excerpts drop to 100.
    many = tuple(Evidence(f"notes/{i}.md", "z" * 300, False, ()) for i in range(60))
    built = _build(_candidate(related_ref=None, evidence=many), history=History(pasts[:2]))
    sent = _sent(built)
    assert _size(built) <= STATE_BYTES
    assert sent["history"]["similar"] == []
    assert {entry["excerpt"] for entry in sent["candidate"]["evidence"]} == {"z" * 99 + "…"}

    # Still too much at 100: excerpts go entirely, outside ones too.
    more = (*(Evidence(f"notes/{i}.md", "z" * 300, False, ()) for i in range(100)),)
    outside = Evidence("https://example.com", "w" * 300, True, ())
    built = _build(_candidate(related_ref=None, evidence=(*more, outside)))
    sent = _sent(built)
    assert _size(built) <= STATE_BYTES
    assert all(set(entry) == {"ref"} for entry in sent["candidate"]["evidence"])
    assert sent["outside"] == [{"ref": "https://example.com", "excerpt": ""}]

    # Nothing left to cut: blocked, never-send's count kept.
    huge = _candidate(
        title="t" * STATE_BYTES,
        evidence=(Evidence(TASKS, "x", False, ()), Evidence("health/a.md", "x", False, ())),
    )
    _assert_blocked(_build(huge, settings=_never_send("health/")), held_back=1)


def test_the_budget_counts_ascii_escaped_bytes() -> None:
    # 12 excerpts of 200 kanji: 14,400 bytes as canonical (escaped) JSON, 7,200 as raw UTF-8.
    entries = tuple(Evidence(f"notes/{i}.md", KANJI * 300, False, ()) for i in range(12))
    built = _build(_candidate(related_ref=None, evidence=entries))
    excerpts = [entry["excerpt"] for entry in _sent(built)["candidate"]["evidence"]]
    assert excerpts == [KANJI * 99 + "…"] * 12
    assert _size(built) <= STATE_BYTES


def test_a_state_of_exactly_12000_bytes_is_sent_and_one_more_is_blocked() -> None:
    titles = replace(DecisionSettings(), send_level="titles")
    empty = _build(_candidate(title=""), settings=titles)
    pad = STATE_BYTES - _size(empty)
    exact = _build(_candidate(title="x" * pad), settings=titles)
    assert not exact.blocked
    assert _size(exact) == STATE_BYTES
    assert exact.state["guide"] == list(GUIDE)  # on top of the budget, whole
    _assert_blocked(_build(_candidate(title="x" * (pad + 1)), settings=titles), held_back=0)


# ---- the guide -----------------------------------------------------------------------------------


def test_the_guide_opens_every_state_that_is_sent() -> None:
    """First, so the request (which keeps the state's order) gives the model its instructions
    before the item; a fresh list each time, so no caller's change reaches another state."""
    first, second = _build(), _build()
    assert list(first.state) == ["guide", "candidate", "owner", "now", "history", "outside"]
    assert first.state["guide"] == list(GUIDE)
    assert first.state["guide"] is not second.state["guide"]


def test_the_guide_does_not_move_the_state_hash() -> None:
    """The hash reads the item's own parts and the playbook version, which covers the guide."""
    built = _build()
    own = {
        "candidate": built.state["candidate"],
        "outside": built.state["outside"],
        "goals": [],
        "playbook_version": PLAYBOOK_VERSION,
        "model_version": "pinned",
    }
    assert built.state_hash == sha256_hex(canonical_json(own))


# ---- words only ----------------------------------------------------------------------------------


def test_no_digits_in_word_facts() -> None:
    candidate = _candidate(
        words={
            "due": "due tomorrow",
            "age": "3 days old",
            "wide": f"{NUMERALS[0]} left",
            "arabic": NUMERALS[1],
            "roman": NUMERALS[2],
            "circled": NUMERALS[3],
            "power": f"x{NUMERALS[4]}",
            "count7": "a few",
            "size": "a few",
        }
    )
    built = _build(
        candidate,
        now=_now(active=_chat("PR 114 review " + "".join(NUMERALS)), nudges_this_hour=7),
        history=History(counts={"shown": 12, "useful": 3, "x9": 1, "flag": True}),
    )
    sent = _sent(built)
    assert sent["candidate"]["facts"] == {"due": "due tomorrow", "size": "a few"}
    assert sent["now"]["active"] == "PR review"
    assert sent["now"]["nudges_this_hour"] == "several"
    assert sent["history"]["counts"] == {"shown": "many", "useful": "a few"}
    for part in (sent["candidate"]["facts"], sent["now"], sent["history"]["counts"]):
        assert _numerals_in(part) == []
    assert sent["candidate"]["title"] == "Ship the launcher by 10/24"  # titles keep theirs


def test_now_and_counts_are_words_for_every_value() -> None:
    for hour in range(24):
        for nudges in range(12):
            built = _build(now=_now(time=MORNING.replace(hour=hour), nudges_this_hour=nudges))
            assert _numerals_in(_sent(built)["now"]) == []
    counts = {"k" + "x" * n: n for n in range(30)}
    sent = _sent(_build(history=History(counts=counts)))
    assert len(sent["history"]["counts"]) == 30
    assert _numerals_in(sent["history"]) == []


# ---- outside -------------------------------------------------------------------------------------


def test_outside_text_stays_under_outside() -> None:
    evidence = (
        Evidence(TASKS, "the owner's own task", False, ()),
        Evidence("https://example.com/a", "OUTSIDE-A " + "o" * 400, True, ()),
        Evidence(None, "OUTSIDE-B", True, ()),
    )
    built = _build(_candidate(evidence=evidence))
    sent = _sent(built)
    assert sent["outside"] == [
        {"ref": "https://example.com/a", "excerpt": ("OUTSIDE-A " + "o" * 400)[:199] + "…"},
        {"ref": None, "excerpt": "OUTSIDE-B"},
    ]
    assert sent["candidate"]["evidence"] == [{"ref": TASKS, "excerpt": "the owner's own task"}]
    for part in ("candidate", "owner", "now", "history"):
        assert "OUTSIDE" not in canonical_json(built.state[part])
        assert "example.com" not in canonical_json(built.state[part])


# ---- the state hash ------------------------------------------------------------------------------


HASHED = _candidate(
    evidence=(
        Evidence(TASKS, "e" * 300, False, ()),
        Evidence("notes/b.md", "f" * 300, False, ()),
        Evidence("notes/c.md", "g" * 300, False, ()),
    )
)


def _hashed(**changes: Any) -> BuiltState:
    base: dict[str, Any] = {
        "candidate": HASHED,
        "owner": Owner(goals=(_chat("VIKI Agent"),)),
        "now": _now(),
        "history": History(),
        "settings": DecisionSettings(),
    }
    return _build(**{**base, **changes})


def test_hash_ignores_now_and_history() -> None:
    base = _hashed()
    assert base.state_hash is not None
    goals = Owner(goals=(_chat("VIKI Agent"),))
    over = STATE_BYTES - _size(base) + 50  # one title this long pushes the state 52 bytes over
    same = [
        _hashed(now=_now(active=_chat("Something else"), focus=True, typing=True)),
        _hashed(now=_now(time=MORNING + timedelta(hours=9), nudges_this_hour=2)),
        _hashed(history=History(similar=(_past("A past item"),), counts={"shown": 9})),
        _hashed(owner=replace(goals, recent=(_chat("Seen"),), muted_or_wrong=(_chat("Muted"),))),
        _hashed(owner=replace(goals, recent_feedback=(_past("Feedback"),))),
        _hashed(history=History(similar=(_past("p" * over),))),  # budget drops it
        _hashed(owner=replace(goals, recent=(_chat("r" * over),))),  # budget cuts excerpts
    ]
    assert [built.state_hash for built in same] == [base.state_hash] * len(same)
    assert _sent(same[-2])["history"]["similar"] == []
    assert {len(entry["excerpt"]) for entry in _sent(same[-1])["candidate"]["evidence"]} == {100}


def test_owner_part_hash_invariant() -> None:
    """`owner.fill`'s three parts (`recent`, `muted_or_wrong`, `recent_feedback`) change what is
    sent but never move `state_hash` — only `owner.goals` is part of the item's own state hash
    (see state.py's module docstring); `base.state_hash` already proves `not None`, so a
    blocked state is not hiding a vacuous match here."""
    base = _hashed()
    filled = _hashed(
        owner=Owner(
            goals=(_chat("VIKI Agent"),),
            recent=(_chat("Fix the launcher"), _chat("Review budget")),
            muted_or_wrong=(_chat("uncommitted changes"),),
            recent_feedback=(_past("Review the PR"), _past("Ship the panel")),
        )
    )
    assert base.state_hash is not None
    assert filled.state_hash == base.state_hash
    sent = _sent(filled)
    assert sent["owner"]["recent"] == ["Fix the launcher", "Review budget"]
    assert sent["owner"]["muted_or_wrong"] == ["uncommitted changes"]
    assert [p["item"] for p in sent["owner"]["recent_feedback"]] == [
        "Review the PR",
        "Ship the panel",
    ]


def test_hash_follows_the_items_own_state() -> None:
    base = _hashed()
    assert _hashed().state_hash == base.state_hash  # rebuilt alike
    assert base.state_hash == sha256_hex(
        canonical_json(
            {
                "candidate": base.state["candidate"],
                "outside": base.state["outside"],
                "goals": ["VIKI Agent"],
                "playbook_version": PLAYBOOK_VERSION,
                "model_version": "pinned",
            }
        )
    )
    first, *rest = HASHED.evidence
    changed = [
        _hashed(candidate=replace(HASHED, title="Another title")),
        _hashed(candidate=replace(HASHED, evidence=(replace(first, excerpt="E" * 300), *rest))),
        _hashed(candidate=replace(HASHED, evidence=(*rest, first))),
        _hashed(
            candidate=replace(HASHED, evidence=(*HASHED.evidence, Evidence(None, "o", True, ())))
        ),
        _hashed(candidate=replace(HASHED, shown_elsewhere=("open note",))),
        _hashed(candidate=replace(HASHED, words={"due": "due today"})),
        _hashed(owner=Owner(goals=(_chat("Another goal"),))),
        _hashed(settings=replace(DecisionSettings(), model_version="latest")),
        _hashed(settings=replace(DecisionSettings(), send_level="sections")),
        _hashed(settings=_never_send("notes/")),
    ]
    hashes = [built.state_hash for built in changed]
    assert None not in hashes
    assert base.state_hash not in hashes
    assert len(set(hashes)) == len(hashes)


def test_hash_follows_the_playbook_version() -> None:
    """The version of the questions asked (`playbook.version` of the wording in use) is part of
    the item's own state: another version moves the hash and nothing else, so answers given to
    one wording are not reused for another."""
    goals = Owner(goals=(_chat("VIKI Agent"),))

    def built(**version: str) -> BuiltState:
        return build_state(HASHED, goals, _now(), History(), DecisionSettings(), **version)

    base = _hashed()
    assert built(playbook_version=PLAYBOOK_VERSION) == built() == base
    tried = built(playbook_version="1.0a1b2c3d")
    assert (tried.state, tried.split, tried.held_back, tried.blocked) == (
        base.state,
        base.split,
        base.held_back,
        base.blocked,
    )
    assert tried.state_hash == sha256_hex(
        canonical_json(
            {
                "candidate": base.state["candidate"],
                "outside": base.state["outside"],
                "goals": ["VIKI Agent"],
                "playbook_version": "1.0a1b2c3d",
                "model_version": "pinned",
            }
        )
    )
    other = built(playbook_version="1.ffffffff")
    assert len({base.state_hash, tried.state_hash, other.state_hash}) == 3
    # A blocked state has no hash, whatever the version.
    blocked = build_state(
        HASHED,
        goals,
        _now(),
        History(),
        _never_send(never_send_invalid=True),
        playbook_version="1.0a1b2c3d",
    )
    _assert_blocked(blocked, held_back=len(HASHED.evidence))


# ---- purity --------------------------------------------------------------------------------------


@pytest.mark.parametrize("module", [state, words], ids=["state", "words"])
def test_modules_import_nothing_that_reads_logs_or_sends(module: object) -> None:
    source = Path(str(getattr(module, "__file__", ""))).read_text(encoding="utf-8")
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert imported <= {
        "__future__",
        "collections.abc",
        "dataclasses",
        "datetime",
        "json",
        "posixpath",
        "unicodedata",
        "viki_agent.decision_loop.contracts",
        "viki_agent.decision_loop.playbook",
        "viki_agent.decision_loop.words",
    }
