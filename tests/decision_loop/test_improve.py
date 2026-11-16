"""Tests for `improve.py`'s four self-improvement detectors: `learn_source`/`skill_offer` over
one cycle's `Observation`, and `from_rows`' retro_due/question_tune over a window of learning
rows. Privacy throughout: every candidate's `work_allowed` is empty and its `words` carry no
sentence (so nothing here can word a number), and `rules_evidence` holds only counts and codes:
the small closed vocabularies (`SKILL_TRIGGERS`, a question name)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from viki_agent.automation.brief import InboxFacts, InboxItem, TaskFacts, TickedFile, VaultFacts
from viki_agent.automation.routines import stamp
from viki_agent.decision_loop import arbiter, contracts, improve, playbook, rules
from viki_agent.decision_loop.cycle import encode_answers
from viki_agent.decision_loop.detectors import Observation
from viki_agent.decision_loop.stats import LearningRow
from viki_agent.decision_loop.vault_watch import LinkPair, NoteFacts, SupersededPair, VaultChanges

WID = "ws-1"
NOW = datetime(2026, 10, 21, 9, 0, tzinfo=UTC)


def _vault(items: tuple[InboxItem, ...] = ()) -> VaultFacts:
    inbox = InboxFacts(
        count=len(items),
        oldest=items[0].captured if items else None,
        checked=len(items),
        undated=0,
        partial=False,
        items=items,
    )
    tasks = TaskFacts(overdue=0, today=0, soon=0, items=(), files=0, files_key="k", partial=False)
    return VaultFacts(inbox=inbox, tasks=tasks)


def _observation(**overrides: Any) -> Observation:
    base: dict[str, Any] = {
        "scope": WID,
        "kind": "vault",
        "vault": _vault(),
        "goals": (),
        "approvals": (),
        "page": None,
        "failures": (),
    }
    base.update(overrides)
    return Observation(**base)


def _page(skill: contracts.SkillHint | None) -> contracts.PageFacts:
    return contracts.PageFacts(
        native_waiting=0,
        runs_working=0,
        runs_review=0,
        git_state="unknown",
        git_dirty=None,
        routine_id=None,
        next_run=None,
        routines_paused=None,
        skill=skill,
    )


def _capture(ref: str, excerpt: str, *, days_old: int = 1) -> InboxItem:
    return InboxItem(
        ref=ref,
        title=None,
        captured=stamp(NOW - timedelta(days=days_old)),
        excerpt=excerpt,
        tags=None,
    )


# ---- learn_source ------------------------------------------------------------------------------


def test_learn_source_from_a_captured_link() -> None:
    link = _capture("00-inbox/a.md", "https://example.com/post", days_old=2)
    obs = _observation(vault=_vault((link,)))
    candidate = improve.learn_source(obs)
    assert candidate is not None
    assert candidate.type == "learn_source"
    assert candidate.title == "Sources to learn"
    assert candidate.related_ref == "00-inbox/a.md"
    assert candidate.related_chat is None
    assert candidate.evidence == (
        contracts.Evidence("00-inbox/a.md", "https://example.com/post", False, None),
    )
    assert candidate.item_key == contracts.item_key("learn_source", "00-inbox/a.md")
    assert candidate.work_allowed == ()


def test_learn_source_none_without_a_link() -> None:
    plain = _capture("00-inbox/a.md", "just a note, no link")
    assert improve.learn_source(_observation(vault=_vault((plain,)))) is None


def test_learn_source_none_without_any_captures() -> None:
    assert improve.learn_source(_observation()) is None


def test_learn_source_none_outside_a_vault() -> None:
    link = _capture("00-inbox/a.md", "https://example.com")
    assert improve.learn_source(_observation(kind="folder", vault=None)) is None
    assert improve.learn_source(_observation(kind="chat_only", vault=_vault((link,)))) is None


def test_learn_source_aggregates_every_linked_capture_oldest_first() -> None:
    older = _capture("00-inbox/old.md", "https://old.example", days_old=3)
    newer = _capture("00-inbox/new.md", "https://new.example", days_old=1)
    plain = _capture("00-inbox/plain.md", "no link here", days_old=2)
    candidate = improve.learn_source(_observation(vault=_vault((older, plain, newer))))
    assert candidate is not None
    assert candidate.related_ref == "00-inbox/old.md"
    assert candidate.evidence == (
        contracts.Evidence("00-inbox/old.md", "https://old.example", False, None),
        contracts.Evidence("00-inbox/new.md", "https://new.example", False, None),
    )
    assert candidate.rules_evidence == {"links": 2}


def test_learn_source_rules_evidence_holds_a_count_never_text() -> None:
    # Mutant target: rules_evidence must carry only the link count, never the capture's excerpt
    # or any other free text.
    link = _capture("00-inbox/a.md", "https://example.com/post")
    candidate = improve.learn_source(_observation(vault=_vault((link,))))
    assert candidate is not None
    assert set(candidate.rules_evidence) == {"links"}
    assert candidate.rules_evidence["links"] == 1
    assert isinstance(candidate.rules_evidence["links"], int)


def test_learn_source_facts_carry_the_scope_for_the_evidence_chip() -> None:
    link = _capture("00-inbox/a.md", "https://example.com")
    candidate = improve.learn_source(_observation(vault=_vault((link,))))
    assert candidate is not None
    assert candidate.facts == {"workspace_id": WID}


# ---- skill_offer --------------------------------------------------------------------------------

_HINT = contracts.SkillHint(
    trigger="correction", sessions=("s-1", "s-2", "s-3"), titles=("Chat A", "Chat B", "Chat C")
)


def test_skill_offer_from_the_page_hint() -> None:
    candidate = improve.skill_offer(_observation(page=_page(_HINT)))
    assert candidate is not None
    assert candidate.type == "skill_offer"
    assert candidate.title == "Chat A"
    assert candidate.related_chat == "s-1"
    assert candidate.related_ref is None
    assert candidate.evidence == (
        contracts.Evidence(None, "Chat B", False, ()),
        contracts.Evidence(None, "Chat C", False, ()),
    )
    assert candidate.facts == {
        "trigger": "correction",
        "sessions": ("s-1", "s-2", "s-3"),
        "titles": ("Chat A", "Chat B", "Chat C"),
    }
    assert candidate.work_allowed == ()


def test_skill_offer_titles_an_untitled_chat_this_chat() -> None:
    hint = contracts.SkillHint(trigger="long_session", sessions=("s-1",), titles=("",))
    candidate = improve.skill_offer(_observation(page=_page(hint)))
    assert candidate is not None
    assert candidate.title == "This chat"
    assert candidate.evidence == ()
    assert candidate.related_chat == "s-1"


def test_skill_offer_none_without_a_hint() -> None:
    assert improve.skill_offer(_observation(page=_page(None))) is None
    assert improve.skill_offer(_observation(page=None)) is None


def test_skill_offer_none_outside_a_vault() -> None:
    assert improve.skill_offer(_observation(page=_page(_HINT), kind="folder")) is None
    assert improve.skill_offer(_observation(page=_page(_HINT), kind="chat_only")) is None


def test_skill_offer_key_ignores_session_order() -> None:
    forward = contracts.SkillHint(
        trigger="repeated_request", sessions=("s-2", "s-1"), titles=("B", "A")
    )
    backward = contracts.SkillHint(
        trigger="repeated_request", sessions=("s-1", "s-2"), titles=("A", "B")
    )
    first = improve.skill_offer(_observation(page=_page(forward)))
    second = improve.skill_offer(_observation(page=_page(backward)))
    assert first is not None
    assert second is not None
    assert first.item_key == second.item_key


def test_skill_offer_rules_evidence_holds_counts_and_codes_only() -> None:
    candidate = improve.skill_offer(_observation(page=_page(_HINT)))
    assert candidate is not None
    assert candidate.rules_evidence == {"chats": 3, "trigger": "correction"}
    assert isinstance(candidate.rules_evidence["chats"], int)
    assert candidate.rules_evidence["trigger"] in contracts.SKILL_TRIGGERS
    assert not any(ch.isdigit() for ch in candidate.rules_evidence["trigger"])


# ---- note_link, note_superseded, tasks_ticked (vault_watch.read_changes) ------------------


def _note(
    ref: str,
    *,
    title: str | None = "A title",
    tags: tuple[str, ...] | None = (),
    opening: str = "An opening",
) -> NoteFacts:
    return NoteFacts(ref=ref, title=title, tags=tags, opening=opening, linked_stems=())


def _ticked_file(
    ref: str = "10-projects/alpha/tasks.md",
    *,
    count: int = 5,
    digest: str = "d" * 64,
    tags: tuple[str, ...] | None = ("alpha",),
) -> TickedFile:
    return TickedFile(ref=ref, count=count, digest=digest, tags=tags)


def _changes(
    links: tuple[LinkPair, ...] = (),
    superseded: tuple[SupersededPair, ...] = (),
    archive: dict[str, str] | None = None,
) -> VaultChanges:
    return VaultChanges(
        changed=(), links=links, superseded=superseded, archive=archive or {}, partial=False
    )


def test_note_link_one_candidate_per_pair_with_mention_and_opening_evidence() -> None:
    a = _note("30-knowledge/alpha.md", tags=("x",))
    b = _note("30-knowledge/beta.md", tags=("y",), opening="Beta's own opening")
    pair = LinkPair(a, b, "Alpha mentions beta right here.")
    candidate_list = improve.note_link(_observation(changes=_changes(links=(pair,))))
    [candidate] = candidate_list
    assert candidate.type == "note_link"
    assert candidate.title == "Notes to link"
    assert candidate.item_key == contracts.item_key("note_link", a.ref, b.ref)
    assert candidate.related_ref == a.ref
    assert candidate.related_chat is None
    assert candidate.evidence == (
        contracts.Evidence(a.ref, "Alpha mentions beta right here.", False, ("x",)),
        contracts.Evidence(b.ref, "Beta's own opening", False, ("y",)),
    )
    assert candidate.rules_evidence == {"pairs": 1}
    assert candidate.facts == {"workspace_id": WID}
    assert candidate.words == {}
    assert candidate.work_allowed == ()


def test_note_link_none_without_changes_or_links() -> None:
    assert improve.note_link(_observation()) == []
    assert improve.note_link(_observation(changes=_changes())) == []


def test_note_link_keeps_unknown_tags_as_none_never_coerced() -> None:
    a = _note("30-knowledge/alpha.md", tags=None)
    b = _note("30-knowledge/beta.md", tags=None)
    pair = LinkPair(a, b, "mention")
    [candidate] = improve.note_link(_observation(changes=_changes(links=(pair,))))
    assert candidate.evidence[0].tags is None
    assert candidate.evidence[1].tags is None


def test_note_link_a_missing_title_never_reaches_a_candidate_field() -> None:
    # Neither note has a title (its display metadata could not be read); the candidate still
    # builds, since no field here reads `.title` -- the evidence label is the ref's own stem,
    # built later by templates.py, never a note's own title.
    a = _note("30-knowledge/alpha.md", title=None)
    b = _note("30-knowledge/beta.md", title=None)
    [candidate] = improve.note_link(_observation(changes=_changes(links=(LinkPair(a, b, "m"),))))
    assert candidate.related_ref == a.ref
    assert candidate.evidence[0].ref == a.ref


def test_note_link_many_pairs_each_their_own_candidate_in_order() -> None:
    a, b, c = (
        _note("30-knowledge/alpha.md"),
        _note("30-knowledge/beta.md"),
        _note("30-knowledge/gamma.md"),
    )
    pairs = (LinkPair(a, b, "m1"), LinkPair(a, c, "m2"))
    candidates = improve.note_link(_observation(changes=_changes(links=pairs)))
    assert [c.item_key for c in candidates] == [
        contracts.item_key("note_link", a.ref, b.ref),
        contracts.item_key("note_link", a.ref, c.ref),
    ]


def test_note_superseded_one_candidate_per_pair_older_then_newer() -> None:
    older = _note("30-knowledge/old-note.md", tags=("a",), opening="Old opening")
    newer = _note("30-knowledge/new-note.md", tags=("b",), opening="New opening")
    pair = SupersededPair(older, newer)
    [candidate] = improve.note_superseded(_observation(changes=_changes(superseded=(pair,))))
    assert candidate.type == "note_superseded"
    assert candidate.title == "Superseded note"
    assert candidate.item_key == contracts.item_key("note_superseded", older.ref, newer.ref)
    assert candidate.related_ref == older.ref
    assert candidate.evidence == (
        contracts.Evidence(older.ref, "Old opening", False, ("a",)),
        contracts.Evidence(newer.ref, "New opening", False, ("b",)),
    )
    assert candidate.rules_evidence == {"pairs": 1}
    assert candidate.facts == {"workspace_id": WID}


def test_note_superseded_none_without_changes_or_pairs() -> None:
    assert improve.note_superseded(_observation()) == []
    assert improve.note_superseded(_observation(changes=_changes())) == []


def test_tasks_ticked_one_candidate_per_file_with_an_archive_target() -> None:
    tf = _ticked_file(count=7, tags=("alpha",))
    vault = _vault()
    vault = replace(vault, tasks=replace(vault.tasks, ticked=(tf,)))
    changes = _changes(archive={tf.ref: "90-archive/alpha tasks done 2026.md"})
    [candidate] = improve.tasks_ticked(_observation(vault=vault, changes=changes))
    assert candidate.type == "tasks_ticked"
    assert candidate.title == "Ticked tasks"
    assert candidate.item_key == contracts.item_key("tasks_ticked", tf.ref, tf.digest)
    assert candidate.related_ref == tf.ref
    assert candidate.evidence == (contracts.Evidence(tf.ref, "", False, ("alpha",)),)
    assert candidate.rules_evidence == {"ticked": 7}
    assert candidate.facts == {"workspace_id": WID}


def test_tasks_ticked_skips_a_file_with_no_archive_target() -> None:
    tf = _ticked_file()
    vault = _vault()
    vault = replace(vault, tasks=replace(vault.tasks, ticked=(tf,)))
    # `changes.archive` stays empty: vault_watch found no 90-archive/ folder, or refused the
    # target -- either way, this file is not ready to archive yet.
    assert improve.tasks_ticked(_observation(vault=vault, changes=_changes())) == []


def test_tasks_ticked_none_without_changes_or_a_vault() -> None:
    assert improve.tasks_ticked(_observation()) == []
    assert improve.tasks_ticked(_observation(kind="folder", vault=None, changes=_changes())) == []


def test_changes_candidates_never_start_work_or_word_a_sentence() -> None:
    a, b = _note("30-knowledge/alpha.md"), _note("30-knowledge/beta.md")
    tf = _ticked_file()
    vault = _vault()
    vault = replace(vault, tasks=replace(vault.tasks, ticked=(tf,)))
    changes = _changes(
        links=(LinkPair(a, b, "m"),),
        superseded=(SupersededPair(a, b),),
        archive={tf.ref: "90-archive/alpha tasks done 2026.md"},
    )
    obs = _observation(vault=vault, changes=changes)
    candidates = [
        *improve.note_link(obs),
        *improve.note_superseded(obs),
        *improve.tasks_ticked(obs),
    ]
    assert len(candidates) == 3
    for candidate in candidates:
        assert candidate.work_allowed == ()
        assert candidate.words == {}
        assert candidate.type in contracts.CHANGES


# ---- from_rows: retro_due -----------------------------------------------------------------------


def _row(**overrides: Any) -> LearningRow:
    defaults: dict[str, Any] = {
        "episode_id": "ep",
        "created_at": NOW,
        "candidate": "tasks_due",
        "level": "nudge",
        "timing": "now",
        "policy": "rules",
        "answers_json": None,
        "p": None,
        "feedback": None,
        "feedback_reason": None,
        "feedback_at": None,
        "first_exposed_at": None,
        "opened_at": None,
        "acted_at": None,
        "label": None,
    }
    return LearningRow(**{**defaults, **overrides})


def _wrong_rows(
    n: int, *, candidate: str = "tasks_due", age: timedelta = timedelta(hours=1)
) -> list[LearningRow]:
    return [
        _row(created_at=NOW - age - timedelta(hours=i), candidate=candidate, feedback="wrong")
        for i in range(n)
    ]


def test_retro_due_fires_at_the_rules_threshold() -> None:
    rows = _wrong_rows(rules.RETRO_LABELS)
    candidates = improve.from_rows(rows, scope=WID, wording={}, trial_open=False, now=NOW)
    [retro] = [c for c in candidates if c.type == "retro_due"]
    assert retro.title == "A retro"
    assert retro.rules_evidence == {"labels": rules.RETRO_LABELS}
    assert retro.work_allowed == ()
    assert retro.evidence == ()
    assert retro.words == {}


def test_retro_due_absent_below_the_threshold() -> None:
    rows = _wrong_rows(rules.RETRO_LABELS - 1)
    candidates = improve.from_rows(rows, scope=WID, wording={}, trial_open=False, now=NOW)
    assert "retro_due" not in {c.type for c in candidates}


def test_retro_due_ignores_labels_outside_the_window() -> None:
    rows = _wrong_rows(rules.RETRO_LABELS, age=timedelta(days=20))
    candidates = improve.from_rows(rows, scope=WID, wording={}, trial_open=False, now=NOW)
    assert "retro_due" not in {c.type for c in candidates}


def test_retro_due_ignores_other_self_improvement_rows() -> None:
    rows = _wrong_rows(rules.RETRO_LABELS, candidate="learn_source")
    candidates = improve.from_rows(rows, scope=WID, wording={}, trial_open=False, now=NOW)
    assert "retro_due" not in {c.type for c in candidates}


def test_retro_due_ignores_labels_before_the_last_reset() -> None:
    earlier = _row(
        created_at=NOW - timedelta(days=10),
        candidate="retro_due",
        acted_at=NOW - timedelta(days=5),
    )
    stale = _wrong_rows(rules.RETRO_LABELS, age=timedelta(days=6))  # all before the reset
    candidates = improve.from_rows(
        [earlier, *stale], scope=WID, wording={}, trial_open=False, now=NOW
    )
    assert "retro_due" not in {c.type for c in candidates}


def test_retro_due_counts_labels_after_the_last_reset() -> None:
    earlier = _row(
        created_at=NOW - timedelta(days=10),
        candidate="retro_due",
        acted_at=NOW - timedelta(days=5),
    )
    fresh = _wrong_rows(rules.RETRO_LABELS, age=timedelta(days=1))  # after the reset
    candidates = improve.from_rows(
        [earlier, *fresh], scope=WID, wording={}, trial_open=False, now=NOW
    )
    assert "retro_due" in {c.type for c in candidates}


def test_retro_due_key_is_stable_five_minutes_apart_with_no_earlier_retro() -> None:
    # Mutant target: with no earlier retro_due, the old key (`stamp(since)`) slid with `now`
    # since `since` is `now - 14 days`; the key must not move between beats on its own.
    rows = _wrong_rows(rules.RETRO_LABELS, age=timedelta(hours=1))
    [first] = [
        c
        for c in improve.from_rows(rows, scope=WID, wording={}, trial_open=False, now=NOW)
        if c.type == "retro_due"
    ]
    [second] = [
        c
        for c in improve.from_rows(
            rows, scope=WID, wording={}, trial_open=False, now=NOW + timedelta(minutes=5)
        )
        if c.type == "retro_due"
    ]
    assert first.item_key == second.item_key == contracts.item_key("retro_due", WID, "start")


def test_retro_due_key_is_stable_a_day_apart_while_the_count_still_holds() -> None:
    rows = _wrong_rows(rules.RETRO_LABELS, age=timedelta(hours=1))
    [first] = [
        c
        for c in improve.from_rows(rows, scope=WID, wording={}, trial_open=False, now=NOW)
        if c.type == "retro_due"
    ]
    [second] = [
        c
        for c in improve.from_rows(
            rows, scope=WID, wording={}, trial_open=False, now=NOW + timedelta(days=1)
        )
        if c.type == "retro_due"
    ]
    # the labels are still inside _RETRO_WINDOW a day later, so the count still clears the
    # threshold here -- this is a later beat, not the "goes stale and reappears" case.
    assert second.rules_evidence == {"labels": rules.RETRO_LABELS}
    assert first.item_key == second.item_key == contracts.item_key("retro_due", WID, "start")


def test_retro_due_key_changes_once_an_earlier_retro_is_acted_on() -> None:
    fresh = _wrong_rows(rules.RETRO_LABELS, age=timedelta(hours=1))
    [before] = [
        c
        for c in improve.from_rows(fresh, scope=WID, wording={}, trial_open=False, now=NOW)
        if c.type == "retro_due"
    ]
    assert before.item_key == contracts.item_key("retro_due", WID, "start")

    acted_at = NOW - timedelta(hours=10)
    earlier = _row(created_at=NOW - timedelta(days=2), candidate="retro_due", acted_at=acted_at)
    [after] = [
        c
        for c in improve.from_rows(
            [earlier, *fresh], scope=WID, wording={}, trial_open=False, now=NOW
        )
        if c.type == "retro_due"
    ]
    assert after.item_key == contracts.item_key("retro_due", WID, stamp(acted_at))
    assert after.item_key != before.item_key


@pytest.mark.parametrize("value", ["useful", "wrong", "mute"])
def test_retro_due_key_changes_once_an_earlier_retro_gets_feedback(value: str) -> None:
    fresh = _wrong_rows(rules.RETRO_LABELS, age=timedelta(hours=1))
    feedback_at = NOW - timedelta(hours=8)
    earlier = _row(
        created_at=NOW - timedelta(days=2),
        candidate="retro_due",
        feedback=value,
        feedback_at=feedback_at,
    )
    [after] = [
        c
        for c in improve.from_rows(
            [earlier, *fresh], scope=WID, wording={}, trial_open=False, now=NOW
        )
        if c.type == "retro_due"
    ]
    assert after.item_key == contracts.item_key("retro_due", WID, stamp(feedback_at))


@pytest.mark.parametrize("value", ["later", "missed", "right_to_hold"])
def test_retro_due_keeps_its_key_and_count_through_a_label_that_closes_nothing(
    value: str,
) -> None:
    # Mutant target: Not now ("later") only defers the same item, and a held-back label closes
    # none; counted as a reset, its stamp would move the key and restart the count after the
    # very labels that raised the item. The row's own `acted_at` still counts.
    fresh = _wrong_rows(rules.RETRO_LABELS, age=timedelta(hours=1))
    labelled = _row(
        created_at=NOW - timedelta(minutes=50),
        candidate="retro_due",
        feedback=value,
        feedback_at=NOW - timedelta(minutes=30),
    )
    [before] = [
        c
        for c in improve.from_rows(fresh, scope=WID, wording={}, trial_open=False, now=NOW)
        if c.type == "retro_due"
    ]
    [after] = [
        c
        for c in improve.from_rows(
            [labelled, *fresh], scope=WID, wording={}, trial_open=False, now=NOW
        )
        if c.type == "retro_due"
    ]
    assert after.item_key == before.item_key == contracts.item_key("retro_due", WID, "start")
    assert after.rules_evidence == before.rules_evidence == {"labels": rules.RETRO_LABELS}

    acted = replace(labelled, acted_at=NOW - timedelta(minutes=10))
    candidates = improve.from_rows(
        [acted, *fresh], scope=WID, wording={}, trial_open=False, now=NOW
    )
    assert "retro_due" not in {c.type for c in candidates}


def test_retro_due_key_anchors_on_learn_since_with_no_earlier_retro() -> None:
    # Mutant target: after Reset learning the engine reads rows from `learn_since` alone, so the
    # retro the user acted on under "start" is gone from them; "start" again would reuse its
    # key, which the agenda keeps closed, and no retro could show again.
    learn_since = NOW - timedelta(days=1)
    fresh = _wrong_rows(rules.RETRO_LABELS, age=timedelta(hours=1))
    [retro] = [
        c
        for c in improve.from_rows(
            fresh, scope=WID, wording={}, trial_open=False, now=NOW, learn_since=learn_since
        )
        if c.type == "retro_due"
    ]
    assert retro.item_key == contracts.item_key("retro_due", WID, stamp(learn_since))

    acted_at = NOW - timedelta(hours=10)
    earlier = _row(created_at=NOW - timedelta(hours=20), candidate="retro_due", acted_at=acted_at)
    [after] = [
        c
        for c in improve.from_rows(
            [earlier, *fresh],
            scope=WID,
            wording={},
            trial_open=False,
            now=NOW,
            learn_since=learn_since,
        )
        if c.type == "retro_due"
    ]
    assert after.item_key == contracts.item_key("retro_due", WID, stamp(acted_at))


# ---- from_rows: question_tune -------------------------------------------------------------------


def _answer_row(name: str, p: float, label: int, **overrides: Any) -> LearningRow:
    merged: dict[str, Any] = {
        "policy": "model",
        "p": p,
        "label": label,
        "answers_json": encode_answers({name: contracts.NoulAnswer(p=p)}),
    }
    merged.update(overrides)
    return _row(**merged)


def _balanced_answers(name: str) -> list[LearningRow]:
    """20 rows for *name*: 10 answered yes (p=0.8) and 10 no (p=0.2), each side split evenly
    between useful and not -- `yes_rate`/`no_rate` both 0.5, a gap of 0."""
    labels = [1, 1, 1, 1, 1, 0, 0, 0, 0, 0]
    return [_answer_row(name, 0.8, label) for label in labels] + [
        _answer_row(name, 0.2, label) for label in labels
    ]


def test_question_tune_picks_the_drifting_feature() -> None:
    rows = _balanced_answers("goal_link")
    candidates = improve.from_rows(rows, scope=WID, wording={}, trial_open=False, now=NOW)
    [tune] = [c for c in candidates if c.type == "question_tune"]
    assert tune.title == "Decision's questions"
    assert tune.facts["name"] == "goal_link"
    assert tune.rules_evidence == {"name": "goal_link"}
    assert tune.work_allowed == ()
    assert tune.words == {}
    assert tune.facts["wording_json"] == contracts.canonical_json(playbook.questions()["goal_link"])


def test_question_tune_wording_json_follows_an_accepted_override() -> None:
    override = playbook.questions()["goal_link"]
    instructions = override["instructions"]
    assert isinstance(instructions, dict)
    instructions["question"] = "Does this link to one of your goals?"
    wording = {"goal_link": override}
    rows = _balanced_answers("goal_link")
    candidates = improve.from_rows(rows, scope=WID, wording=wording, trial_open=False, now=NOW)
    [tune] = [c for c in candidates if c.type == "question_tune"]
    assert tune.facts["wording_json"] == contracts.canonical_json(
        playbook.questions_for("question_tune", wording)["goal_link"]
    )
    assert tune.facts["wording_json"] != contracts.canonical_json(playbook.questions()["goal_link"])


def test_question_tune_none_when_no_feature_qualifies() -> None:
    # Ten rows per side but every rate is None: `_scored` needs a label and a `p`, and none is
    # labelled here, so no FEATURES question has a decodable yes or no side.
    rows = [_row(policy="model", p=0.5, label=None) for _ in range(20)]
    candidates = improve.from_rows(rows, scope=WID, wording={}, trial_open=False, now=NOW)
    assert "question_tune" not in {c.type for c in candidates}


def test_question_tune_falls_back_to_the_worst_topic_when_nothing_drifts() -> None:
    # Too few rows for branch (i)'s yes_n/no_n >= 10, so branch (ii) must find the FEATURES
    # question with the higher mean `p` on one topic's wrong-labelled rows.
    rows = [
        _row(
            created_at=NOW - timedelta(hours=i),
            candidate="tasks_due",
            feedback="wrong",
            policy="model",
            p=0.9,
            label=0,
            answers_json=encode_answers(
                {"news": contracts.NoulAnswer(p=0.9), "actionable": contracts.NoulAnswer(p=0.3)}
            ),
        )
        for i in range(rules.RETRO_LABELS)  # as many wrong labels as branch (ii) needs
    ]
    candidates = improve.from_rows(rows, scope=WID, wording={}, trial_open=False, now=NOW)
    [tune] = [c for c in candidates if c.type == "question_tune"]
    assert tune.facts["name"] == "news"


def test_question_tune_branch_i_tie_picks_earlier_feature() -> None:
    # Branch (i) tie-breaking: when two features have the same gap below DRIFT_GAP,
    # the one earlier in arbiter.FEATURES is picked.
    # Create two features with the same small gap, both below 0.05.
    # goal_link is at index 0, news is at index 1 in arbiter.FEATURES.
    # Both features: 10 yes (p >= 0.5) with 8 useful, 10 no (p < 0.5) with 8 useful
    # → yes_rate = 0.8, no_rate = 0.8, gap = 0 (below 0.05 threshold)
    goal_link_yes = [_answer_row("goal_link", 0.8, 1) for _ in range(8)] + [
        _answer_row("goal_link", 0.8, 0) for _ in range(2)
    ]
    goal_link_no = [_answer_row("goal_link", 0.2, 1) for _ in range(8)] + [
        _answer_row("goal_link", 0.2, 0) for _ in range(2)
    ]
    news_yes = [_answer_row("news", 0.8, 1) for _ in range(8)] + [
        _answer_row("news", 0.8, 0) for _ in range(2)
    ]
    news_no = [_answer_row("news", 0.2, 1) for _ in range(8)] + [
        _answer_row("news", 0.2, 0) for _ in range(2)
    ]
    rows = goal_link_yes + goal_link_no + news_yes + news_no
    candidates = improve.from_rows(rows, scope=WID, wording={}, trial_open=False, now=NOW)
    [tune] = [c for c in candidates if c.type == "question_tune"]
    # Both features have gap = 0 (both 80% useful on both sides)
    # goal_link should be picked because it comes first in arbiter.FEATURES
    assert tune.facts["name"] == "goal_link"


def test_question_tune_branch_ii_floor_requires_five_wrong_labels() -> None:
    # Branch (ii) floor test: with only 4 wrong labels on a topic, should not trigger.
    rows = [
        _row(
            created_at=NOW - timedelta(hours=i),
            candidate="tasks_due",
            feedback="wrong",
            policy="model",
            p=0.9,
            label=0,
            answers_json=encode_answers(
                {"news": contracts.NoulAnswer(p=0.9), "actionable": contracts.NoulAnswer(p=0.3)}
            ),
        )
        for i in range(4)  # Just below _TUNE_WRONG_LABELS (which is 5)
    ]
    candidates = improve.from_rows(rows, scope=WID, wording={}, trial_open=False, now=NOW)
    assert "question_tune" not in {c.type for c in candidates}


def test_question_tune_branch_ii_tie_picks_earlier_feature() -> None:
    # Branch (ii) tie-breaking: when two features have the same mean p on wrong-labelled rows,
    # the one earlier in arbiter.FEATURES is picked.
    rows = [
        _row(
            created_at=NOW - timedelta(hours=i),
            candidate="tasks_due",
            feedback="wrong",
            policy="model",
            p=0.9,
            label=0,
            answers_json=encode_answers(
                {
                    "goal_link": contracts.NoulAnswer(p=0.7),
                    "news": contracts.NoulAnswer(p=0.7),  # Same mean as goal_link
                }
            ),
        )
        for i in range(5)  # Meets _TUNE_WRONG_LABELS floor
    ]
    candidates = improve.from_rows(rows, scope=WID, wording={}, trial_open=False, now=NOW)
    [tune] = [c for c in candidates if c.type == "question_tune"]
    # goal_link comes before news in arbiter.FEATURES, so it should be picked
    assert tune.facts["name"] == "goal_link"


def test_question_tune_none_while_a_trial_is_open() -> None:
    rows = _balanced_answers("goal_link")
    candidates = improve.from_rows(rows, scope=WID, wording={}, trial_open=True, now=NOW)
    assert "question_tune" not in {c.type for c in candidates}


def test_question_tune_key_stays_the_same_when_the_oldest_pool_rows_are_dropped() -> None:
    # Mutant target: with no earlier question_tune, the old key was built from the pool's oldest
    # `created_at`; dropping the oldest rows (the engine's own row cap or retention) must not
    # turn the same drifting question into a fresh item.
    core = _balanced_answers("goal_link")
    filler = [_row(created_at=NOW - timedelta(days=30)) for _ in range(3)]
    with_filler = improve.from_rows(
        [*filler, *core], scope=WID, wording={}, trial_open=False, now=NOW
    )
    without_filler = improve.from_rows(core, scope=WID, wording={}, trial_open=False, now=NOW)
    [tune_with] = [c for c in with_filler if c.type == "question_tune"]
    [tune_without] = [c for c in without_filler if c.type == "question_tune"]
    assert tune_with.item_key == tune_without.item_key
    assert tune_with.item_key == contracts.item_key("question_tune", WID, "goal_link", "start")


def test_question_tune_key_changes_once_an_earlier_tune_is_acted_on() -> None:
    core = _balanced_answers("goal_link")
    [before] = [
        c
        for c in improve.from_rows(core, scope=WID, wording={}, trial_open=False, now=NOW)
        if c.type == "question_tune"
    ]
    assert before.item_key == contracts.item_key("question_tune", WID, "goal_link", "start")

    acted_at = NOW - timedelta(hours=2)
    earlier = _row(
        created_at=NOW - timedelta(hours=3), candidate="question_tune", acted_at=acted_at
    )
    [after] = [
        c
        for c in improve.from_rows(
            [earlier, *core], scope=WID, wording={}, trial_open=False, now=NOW
        )
        if c.type == "question_tune"
    ]
    assert after.item_key == contracts.item_key("question_tune", WID, "goal_link", stamp(acted_at))
    assert after.item_key != before.item_key


def test_question_tune_key_anchors_on_learn_since_with_no_earlier_tune() -> None:
    learn_since = NOW - timedelta(days=1)
    [tune] = [
        c
        for c in improve.from_rows(
            _balanced_answers("goal_link"),
            scope=WID,
            wording={},
            trial_open=False,
            now=NOW,
            learn_since=learn_since,
        )
        if c.type == "question_tune"
    ]
    assert tune.item_key == contracts.item_key(
        "question_tune", WID, "goal_link", stamp(learn_since)
    )


def test_from_rows_orders_retro_due_before_question_tune() -> None:
    rows = [*_wrong_rows(rules.RETRO_LABELS), *_balanced_answers("goal_link")]
    candidates = improve.from_rows(rows, scope=WID, wording={}, trial_open=False, now=NOW)
    assert [c.type for c in candidates] == ["retro_due", "question_tune"]


def test_from_rows_empty_without_enough_history() -> None:
    assert improve.from_rows((), scope=WID, wording={}, trial_open=False, now=NOW) == []


# ---- privacy, across all four types -------------------------------------------------------------


def test_every_improve_candidate_has_no_work_and_no_worded_sentence() -> None:
    link = _capture("00-inbox/a.md", "https://example.com")
    beat = improve.from_rows(
        _wrong_rows(rules.RETRO_LABELS), scope=WID, wording={}, trial_open=False, now=NOW
    )
    candidates = [
        improve.skill_offer(_observation(page=_page(_HINT))),
        improve.learn_source(_observation(vault=_vault((link,)))),
        *beat,
    ]
    for candidate in candidates:
        assert candidate is not None
        assert candidate.work_allowed == ()
        assert candidate.words == {}
        assert candidate.type in contracts.IMPROVE


def test_arbiter_features_is_a_subset_of_the_tunable_questions() -> None:
    # A sanity check on the module's own assumption: every name `_drifting_feature`/
    # `_worst_topic_feature` can return is one the user can actually propose wording for.
    assert set(arbiter.FEATURES) <= set(playbook.TUNABLE)
