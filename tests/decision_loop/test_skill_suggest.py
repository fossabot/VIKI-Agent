"""Tests for skill_suggest: `improve.match_tokens` and `improve.pick_skill` match a vault chat's
title against that vault's own skills on this computer, and `improve.skill_suggest` turns one
match into one candidate per chat. Its model state carries the fixed title and the type's words
alone -- never the skill's name or description, the chat's title or the skills already used
there."""

from __future__ import annotations

import unicodedata
from datetime import UTC, datetime
from typing import Any

import pytest

from viki_agent.decision_loop import contracts, improve, words
from viki_agent.decision_loop.detectors import Observation
from viki_agent.decision_loop.state import History, Now, Owner, build_state, state_text

WID = "ws-1"
SESSION = "fe07f4bf-5447-4a8d-9d58-b68385409b99"
OTHER_SESSION = "8387dec2-0e72-4e24-a7e5-e4f64a28d23d"
NOW = datetime(2026, 10, 21, 9, 0, tzinfo=UTC)
FIXED_TITLE = "A skill for this chat"


def sw(name: str, description: str = "") -> improve.SkillWords:
    """`improve.skill_words(name, description)`, for building a `skills` sequence inline."""
    return improve.skill_words(name, description)


# The app's own starter skills (src/viki_agent/resources/starter/dot-claude/skills/*/SKILL.md),
# name and description as a new vault ships them: ten of the twelve.
_STARTER_PAIRS: tuple[tuple[str, str], ...] = (
    (
        "capture",
        'Zero-friction capture into 00-inbox/. Use when the owner says "note this", "capture", '
        "or dumps any thought, link, or task fragment that should not be lost.",
    ),
    (
        "connect",
        "Find non-obvious connections across recent notes, add the links, surface surprising "
        'pairings. Use when the owner says "connect my notes", "what links", "find patterns", or '
        "after a burst of new notes.",
    ),
    (
        "daily",
        "Open or create today's daily note and deliver the morning brief — open tasks, loose "
        'ends, one resurfaced note. Use when the owner says "daily", "brief me", "start my day".',
    ),
    (
        "inbox",
        "Librarian pass — GTD-clarify every item in 00-inbox/ and file it into the vault. Use "
        'when the owner says "process the inbox", "file my notes", or asks what\'s in the inbox.',
    ),
    (
        "learn",
        "Ingest an external source (URL, file, or repo) — immutable raw copy, literature note "
        'with provenance, knowledge-note updates. Use when the owner says "learn this", "read '
        'this", "ingest", or shares a link/article/repo to absorb.',
    ),
    (
        "lint",
        "Vault health check — dead links, orphans, schema violations, duplicates, stale facts, "
        'inbox age — written to system/metrics.md. Use when the owner says "lint", "vault '
        'health", "check the vault", or before a weekly review.',
    ),
    (
        "research",
        "Deep-dive a topic on the web and synthesize it into evergreen knowledge notes with "
        'confidence-marked claims. Use when the owner says "research X", "find out about", or '
        "asks a question the vault can't answer.",
    ),
    (
        "retro",
        "The assistant's self-improvement loop — extract evidence-cited learnings from recent "
        "sessions, propose diffs to memory/skills, apply only what the owner approves. Use when "
        'the owner says "retro", "reflect", "improve yourself", or weekly.',
    ),
    (
        "think",
        "Structured thinking partner using the QSA loop (Question → Structure → Answer → "
        'Thought), captured as a thinking note. Use when the owner says "think with me", "help '
        'me think through", or brings a hard open question.',
    ),
    (
        "weekly",
        "GTD weekly review — project and Kanban sweep, stale-task triage, archive done work, "
        'inbox-zero check. Use when the owner says "weekly review", "review the week", or once '
        "a week.",
    ),
)
STARTER: tuple[improve.SkillWords, ...] = tuple(
    sw(name, description) for name, description in _STARTER_PAIRS
)


def _page(suggest: contracts.SuggestHint | None) -> contracts.PageFacts:
    return contracts.PageFacts(0, 0, 0, "unknown", None, None, None, None, suggest=suggest)


def _observation(
    title: str,
    skills: tuple[improve.SkillWords, ...] = STARTER,
    used: tuple[str, ...] = (),
    *,
    session: str = SESSION,
    **overrides: Any,
) -> Observation:
    """A vault cycle whose page reports `title` and `used` for `session`, over `skills`."""
    base: dict[str, Any] = {
        "scope": WID,
        "kind": "vault",
        "vault": None,
        "goals": (),
        "approvals": (),
        "page": _page(contracts.SuggestHint(session, title, used)),
        "failures": (),
        "skills": skills,
    }
    base.update(overrides)
    return Observation(**base)


# ---- match_tokens ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "tokens"),
    [
        ("Sprint-Retro notes", {"sprint", "retro", "notes"}),
        ("ＲＥＴＲＯ", {"retro"}),  # NFKC folds full-width letters
        ("Straße STRASSE", {"strasse"}),  # casefold
        ("build_script e-mail", {"build", "script", "mail"}),  # one letter is no token
        ("The 2026 plan for Q4", {"plan", "q4"}),  # stopwords and all-numeral runs drop
        ("٢٠٢٦ ２０２６", set()),  # numerals of any script
        ("about the owner's skills", set()),
        ("a b c x", set()),
        ("", set()),
        ("  !? …", set()),
        ("회의록", {"회의록"}),  # a run outside kana/Han gives itself
    ],
)
def test_match_tokens_folds_text_into_runs_of_letters_and_numbers(
    text: str, tokens: set[str]
) -> None:
    assert improve.match_tokens(text) == frozenset(tokens)


@pytest.mark.parametrize(
    ("text", "tokens"),
    [
        ("週次振り返り", {"週次", "次振", "振り", "り返", "返り"}),
        ("ふりかえり", set()),  # all-hiragana pairs drop
        ("レビュー", {"レビ", "ビュ", "ュー"}),  # katakana pairs stay
        ("ﾚﾋﾞｭｰ", {"レビ", "ビュ", "ュー"}),  # half-width katakana folds first
        ("AI活用ガイド", {"ai", "活用", "用ガ", "ガイ", "イド"}),  # a run breaks at kana/Han
        ("週次Review振り返り", {"週次", "review", "振り", "り返", "返り"}),
        ("GTDで整理", {"gtd", "で整", "整理"}),
        ("雨", set()),  # one character makes no pair
        (chr(0x3400) + chr(0x4DBF), {chr(0x3400) + chr(0x4DBF)}),  # CJK extension A
    ],
)
def test_match_tokens_pairs_kana_and_han(text: str, tokens: set[str]) -> None:
    assert improve.match_tokens(text) == frozenset(tokens)


def test_match_tokens_cuts_the_folded_text_at_max_match_chars() -> None:
    """NFKC can expand one input character into several (each U+FDFA becomes 18), so a cut
    on the input's own length would not bound the cost -- the inspector's 2,048-byte description
    cut applies before NFKC. 700 U+FDFA characters fold past `MAX_MATCH_CHARS`; `match_tokens`
    reads only the first `MAX_MATCH_CHARS` of the *folded* text, and scans no further: real
    words appended after the cut, which would otherwise add tokens, change nothing."""
    plain = chr(0xFDFA) * 700  # NFKC decomposes each into 18 Arabic characters
    folded = unicodedata.normalize("NFKC", plain).casefold()
    assert len(folded) > improve.MAX_MATCH_CHARS
    assert improve.match_tokens(plain) == improve.match_tokens(folded[: improve.MAX_MATCH_CHARS])
    assert improve.match_tokens(plain + "weekly review") == improve.match_tokens(plain)


# ---- pick_skill --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("title", "pick"),
    [
        ("Weekly review of my projects", ("weekly", 3)),
        ("Process the inbox notes", ("inbox", 4)),
        ("Lint the vault health", ("lint", 4)),
        ("Help me think through pricing", ("think", 4)),
        ("Start my day with the morning brief", ("daily", 4)),  # the description alone
        ("Learn this article on vector search", ("learn", 3)),
        ("Connect my notes on pricing", ("connect", 3)),
        ("Retro on last week's sessions", ("retro", 3)),
        ("Find patterns across recent notes", ("connect", 5)),
        ("Capture this thought before it's lost", ("capture", 4)),
        ("Check the vault for dead links", ("lint", 4)),
        # Near misses: one signal (the name, or two description words) scores only 2.
        ("I think the build is broken", None),
        ("Weekly sales report numbers", None),
        ("Research GLP-1 market trends", None),
        ("Deep dive into protein folding", None),
        ("Draft the quarterly budget", None),
        ("Fix the login bug", None),
        ("", None),
    ],
)
def test_pick_skill_on_the_starter_skills(title: str, pick: tuple[str, int] | None) -> None:
    assert improve.pick_skill(title, STARTER, ()) == pick


def test_pick_skill_needs_a_score_of_min_score(monkeypatch: pytest.MonkeyPatch) -> None:
    """At the minimum score of 3 no near miss is picked; at 2, two wrong ones are."""
    assert improve.MIN_SCORE == 3
    monkeypatch.setattr(improve, "MIN_SCORE", 2)
    assert improve.pick_skill("I think the build is broken", STARTER, ()) == ("think", 2)
    assert improve.pick_skill("Weekly sales report numbers", STARTER, ()) == ("weekly", 2)


def test_pick_skill_counts_a_name_word_twice_and_another_description_word_once() -> None:
    assert improve.pick_skill("Sprint retro", (sw("sprint-retro"),), ()) == ("sprint-retro", 4)
    assert improve.pick_skill(
        "Sprint retro planning", (sw("sprint", "Sprint planning for the team"),), ()
    ) == ("sprint", 3)
    # A title word the name already matched does not count again from the description.
    assert improve.pick_skill("Sprint retro", (sw("sprint", "sprint sprint"),), ()) is None


@pytest.mark.parametrize(
    ("skills", "name"),
    [
        # Score first: three description words beat one name word.
        ((sw("alpha"), sw("omega", "alpha beta gamma")), "omega"),
        # Equal scores: more name words first.
        ((sw("zeta", "alpha beta gamma"), sw("alpha", "beta")), "alpha"),
        # Equal scores and name words: the name that sorts first.
        ((sw("beta", "alpha"), sw("alpha", "beta")), "alpha"),
    ],
)
def test_pick_skill_breaks_ties_by_name_words_then_by_name(
    skills: tuple[improve.SkillWords, ...], name: str
) -> None:
    title = "alpha beta gamma delta"
    assert improve.pick_skill(title, skills, ()) == improve.pick_skill(title, skills[::-1], ())
    picked = improve.pick_skill(title, skills, ())
    assert picked is not None and picked[0] == name


def test_pick_skill_blocks_every_pick_once_a_used_skill_still_fits() -> None:
    """A skill already in `used` is skipped as a pick, but scored first all the same -- if
    it still scores `MIN_SCORE` or more for this title, the chat already has a fitting skill, so
    this returns None for every skill, not only that one ("at most once a chat"), rather than
    fall back to the next best. A used skill that falls short of `MIN_SCORE` blocks nothing."""
    title = "Weekly review of the vault health"
    assert improve.pick_skill(title, STARTER, ()) == ("lint", 4)
    assert improve.pick_skill(title, STARTER, ("lint",)) is None  # lint (used) still fits at 4
    assert improve.pick_skill(title, STARTER, ("lint", "weekly")) is None

    other = "Process the inbox notes"
    # weekly (used) scores only 1 here, short of MIN_SCORE, so it blocks nothing and is skipped.
    assert improve.pick_skill(other, STARTER, ["weekly"]) == ("inbox", 4)
    assert improve.pick_skill(other, STARTER, ["inbox"]) is None  # inbox (used) still fits at 4


# ---- skill_suggest -----------------------------------------------------------------------------


def test_skill_suggest_builds_the_chats_one_candidate() -> None:
    candidate = improve.skill_suggest(_observation("Process the inbox notes", used=("lint",)))
    assert candidate == contracts.Candidate(
        item_key=contracts.item_key("skill_suggest", SESSION),
        type="skill_suggest",
        title=FIXED_TITLE,
        facts={"session": SESSION, "skill": "inbox"},
        words={},
        evidence=(),
        related_chat=SESSION,
        related_ref=None,
        deadline=None,
        shown_elsewhere=(),
        rules_evidence={"score": 4},
        origin_session=None,
        work_allowed=(),
    )


def test_skill_suggest_keys_the_item_by_the_chat_alone() -> None:
    inbox = improve.skill_suggest(_observation("Process the inbox notes"))
    lint = improve.skill_suggest(_observation("Check the vault for dead links"))
    elsewhere = improve.skill_suggest(
        _observation("Process the inbox notes", session=OTHER_SESSION)
    )
    assert inbox is not None and lint is not None and elsewhere is not None
    assert (inbox.facts["skill"], lint.facts["skill"]) == ("inbox", "lint")
    assert inbox.item_key == lint.item_key != elsewhere.item_key


def test_skill_suggest_words_a_japanese_chat_in_english() -> None:
    skills = (
        sw("weekly-review", "週次の振り返りを手伝う。Use for a weekly review of your projects."),
    )
    candidate = improve.skill_suggest(_observation("週次振り返りの準備をする", skills))
    assert candidate is not None
    assert (candidate.facts["skill"], candidate.rules_evidence) == ("weekly-review", {"score": 4})
    assert candidate.title == FIXED_TITLE


@pytest.mark.parametrize(
    "obs",
    [
        pytest.param(_observation("Process the inbox notes", kind="folder"), id="folder"),
        pytest.param(_observation("Process the inbox notes", kind="chat_only"), id="chat_only"),
        pytest.param(_observation("Process the inbox notes", page=None), id="no page"),
        pytest.param(_observation("Process the inbox notes", page=_page(None)), id="no suggest"),
        pytest.param(_observation("Process the inbox notes", ()), id="no skills"),
        pytest.param(
            _observation("Process the inbox notes", (STARTER[3],), ("inbox",)), id="only used ones"
        ),
        pytest.param(_observation("Fix the login bug"), id="no pick"),
    ],
)
def test_skill_suggest_is_none_without_a_vault_chat_and_a_pick(obs: Observation) -> None:
    assert improve.skill_suggest(obs) is None


# ---- the state holds nothing of the match -------------------------------------------------------


def test_a_suggestion_state_holds_no_skill_title_or_used_name() -> None:
    """Two candidates that differ only in their skill, the chat's title and the used names
    build the same state and hash at every send level, and neither state's text holds any of
    them."""
    sprint_about = "Run a sprint retro with the team and file its actions"
    weekly_about = "週次の振り返りを手伝う。Use for a weekly review of your projects."
    sprint = (sw("sprint-retro", sprint_about),)
    weekly = (sw("weekly-review", weekly_about),)
    first = improve.skill_suggest(
        _observation("Sprint retro for the mobile team", sprint, ("tax-filing", "garden-plan"))
    )
    second = improve.skill_suggest(
        _observation("週次振り返りの準備をする", weekly, ("seedling-care",))
    )
    assert first is not None and second is not None
    assert (first.facts["skill"], second.facts["skill"]) == ("sprint-retro", "weekly-review")
    secrets = (
        "sprint-retro",
        sprint_about,
        "Sprint retro for the mobile team",
        "tax-filing",
        "garden-plan",
        "weekly-review",
        weekly_about,
        "週次振り返りの準備をする",
        "seedling-care",
    )
    now = Now(active=None, focus=False, typing=False, time=NOW, nudges_this_hour=0)
    for level in contracts.EXCERPT:
        settings = contracts.DecisionSettings(enabled=True, send_level=level)
        one = build_state(first, Owner(), now, History(), settings)
        two = build_state(second, Owner(), now, History(), settings)
        assert not one.blocked and one.state_hash is not None, level
        assert one.state["candidate"] == {
            "type": words.type_words("skill_suggest"),
            "title": FIXED_TITLE,
            "facts": {},
            "evidence": [],
            "shown_elsewhere": [],
        }, level
        assert (one.state, one.state_hash) == (two.state, two.state_hash), level
        sent = (contracts.canonical_json(one.state) + state_text(one.state)).casefold()
        assert [secret for secret in secrets if secret.casefold() in sent] == [], level
