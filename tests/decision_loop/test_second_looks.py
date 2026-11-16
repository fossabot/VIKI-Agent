"""second_looks.py: the plan's kind, the refs a second look may name, the offer and the finding
items, what the model is sent, the yields and their pauses, and the restart stamps."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from viki_agent.automation.routines import stamp
from viki_agent.decision_loop import contracts, second_looks
from viki_agent.decision_loop.contracts import (
    Finding,
    LookRow,
    SecondLookHint,
    canonical_json,
)
from viki_agent.decision_loop.second_looks import Restarts
from viki_agent.decision_loop.state import History, Now, Owner, build_state

NOW = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)
SESSION = "fe07f4bf-5447-4a8d-9d58-b68385409b99"
TURN = "3f0b9c52-6a51-4c27-9a44-2f7e1d0c8b11"
SCOPE = "scope-garden"
LONG = "long_task_look"
CORRECTION = "correction_look"


# ---- kind_of -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("plan", "kind"),
    [
        ("Kind: flaw", "flaw"),
        ("Kind: better approach", "approach"),
        ("Kind: missing piece", "missing"),
        ("Kind: flaw\nThe evidence...", "flaw"),
        ("# Kind: flaw", "flaw"),
        ("### Kind: better approach\n", "approach"),
        ("#Kind: missing piece", "missing"),
        ("**Kind: flaw**", "flaw"),
        ("# **Kind: missing piece**", "missing"),
        ("**  Kind: flaw  **", "flaw"),
        ("kind: FLAW", "flaw"),
        ("KIND:   Better   Approach", "approach"),
        ("  \t Kind: flaw \r\n", "flaw"),
        ("\n\n  \nKind: flaw", "flaw"),
        ("Kind: flaw. The plan goes on.", "other"),
        ("Kind: flaw and more", "other"),
        ("Kind: gap", "other"),
        ("Kind:", "other"),
        ("Plan\nKind: flaw", "other"),
        ("The kind is a flaw\nKind: flaw", "other"),
        ("**Kind: flaw", "other"),
        ("***", "other"),
        ("****", "other"),
        ("", "other"),
        ("   \n\t\n", "other"),
        (None, "other"),
        (b"Kind: flaw", "other"),
        (["Kind: flaw"], "other"),
        (7, "other"),
    ],
)
def test_kind_of_reads_only_the_first_line(plan: object, kind: str) -> None:
    assert second_looks.kind_of(plan) == kind


def test_kind_of_reads_only_the_first_4096_characters() -> None:
    assert second_looks.kind_of("Kind: flaw" + " " * 4086 + "x") == "flaw"  # the cut is at 4,096
    assert second_looks.kind_of(" " * 4096 + "Kind: flaw") == "other"


# ---- admitted ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ref",
    [
        "notes/garden-plan.md",
        "a.md",
        "10-projects/viki/README.MD",
        "x/y.markdown",
        "caf\u00e9.md",
        "notes/~draft.md",
    ],
)
def test_admitted_takes_a_vault_note(ref: str) -> None:
    assert second_looks.admitted(ref)


@pytest.mark.parametrize(
    "ref",
    [
        "/notes/a.md",
        "C:/notes/a.md",
        "../a.md",
        "notes/../a.md",
        "notes\\a.md",
        "notes/a:b.md",
        ".git/config.md",
        "notes/.git/a.md",
        ".env",
        "notes/.env.md",
        "notes/a\x00.md",
        "notes/a\nb.md",
        "notes/a\x7f.md",
        "notes/a\u2028b.md",
        "notes/a\u2029b.md",
        "notes/\u2026",
        "notes/long-path-cu\u2026",
        "notes/a.txt",
        "notes/a",
        "notes/a.md.bak",
        "",
        ".",
        "notes//a.md",
        "~/notes/a.md",
        "~",
        "~/a.md",
        "~user/notes/a.md",
    ],
)
def test_admitted_refuses_anything_else(ref: str) -> None:
    assert not second_looks.admitted(ref)


@pytest.mark.parametrize("ref", [None, 7, b"a.md", ["a.md"], ("a.md",)])
def test_admitted_refuses_what_is_not_text(ref: object) -> None:
    assert not second_looks.admitted(ref)


def test_admitted_refuses_a_str_subclass() -> None:
    class Ref(str):
        __slots__ = ()

    assert not second_looks.admitted(Ref("a.md"))


# ---- offer -------------------------------------------------------------------------------------


def _hint(**changes: Any) -> SecondLookHint:
    base = SecondLookHint(
        SESSION, TURN, "long_session", "Garden plan", ("notes/garden-plan.md", "notes/beds.md")
    )
    return replace(base, **changes)


def test_a_hint_whose_only_ref_is_in_the_home_folder_gives_no_offer() -> None:
    assert second_looks.offer(_hint(refs=("~/notes/todo.md",)), SCOPE) is None


def test_an_offer_is_keyed_by_type_chat_and_turn() -> None:
    long_offer = second_looks.offer(_hint(), SCOPE)
    corrected = second_looks.offer(_hint(trigger="correction"), SCOPE)
    assert long_offer is not None
    assert corrected is not None
    assert long_offer.type == LONG
    assert corrected.type == CORRECTION
    assert long_offer.item_key == contracts.item_key(LONG, SESSION, TURN)
    assert corrected.item_key == contracts.item_key(CORRECTION, SESSION, TURN)
    other_turn = second_looks.offer(_hint(turn="7a1b2c3d-0000-4000-8000-000000000001"), SCOPE)
    assert other_turn is not None
    assert other_turn.item_key != long_offer.item_key


def test_an_offer_carries_its_chat_and_notes_in_facts_and_sends_nothing_else() -> None:
    offer = second_looks.offer(_hint(), SCOPE)
    assert offer is not None
    assert offer.title == "Garden plan"
    assert dict(offer.facts) == {
        "workspace_id": SCOPE,
        "session": SESSION,
        "refs": ("notes/garden-plan.md", "notes/beds.md"),
    }
    assert dict(offer.words) == {}
    assert offer.evidence == ()
    assert offer.related_chat == SESSION
    assert offer.related_ref is None
    assert offer.deadline is None
    assert offer.shown_elsewhere == ()
    assert dict(offer.rules_evidence) == {"trigger": "long_session"}
    assert offer.origin_session is None
    assert offer.work_allowed == ("prepare",)


def test_an_untitled_chat_takes_the_untitled_title() -> None:
    offer = second_looks.offer(_hint(title=""), SCOPE)
    assert offer is not None
    assert offer.title == contracts.UNTITLED_CHAT


def test_an_offer_keeps_only_admitted_distinct_refs_in_order() -> None:
    refs = ("a.md", "../b.md", "a.md", "c.txt", "/d.md", "e.md", "f\u2026")
    offer = second_looks.offer(_hint(refs=refs), SCOPE)
    assert offer is not None
    assert offer.facts["refs"] == ("a.md", "e.md")


def test_an_offer_cuts_its_refs_at_ten() -> None:
    refs = tuple(f"n{i}.md" for i in range(12))
    offer = second_looks.offer(_hint(refs=refs), SCOPE)
    assert offer is not None
    assert offer.facts["refs"] == refs[:10]


def _sized(label: str, size: int) -> str:
    """A note ref of exactly `size` bytes, in folders and a name short enough for `safe_ref`."""
    folders = []
    left = size
    while left > 200:
        folders.append(label * 100)
        left -= 101
    ref = "/".join([*folders, label * (left - 3) + ".md"])
    assert len(ref.encode()) == size
    return ref


def test_an_offer_keeps_refs_up_to_1024_bytes_in_all_and_no_more() -> None:
    fits = (_sized("a", 512), _sized("b", 512))
    offer = second_looks.offer(_hint(refs=fits), SCOPE)
    assert offer is not None
    assert offer.facts["refs"] == fits  # exactly 1,024 bytes
    over = (_sized("a", 512), _sized("b", 511), "c.md")  # 1,023 + 4 = 1,027
    cut = second_looks.offer(_hint(refs=over), SCOPE)
    assert cut is not None
    assert cut.facts["refs"] == over[:2]


def test_an_offer_stops_at_the_first_ref_that_does_not_fit() -> None:
    refs = (_sized("a", 500), _sized("b", 510), _sized("c", 20), "d.md")
    offer = second_looks.offer(_hint(refs=refs), SCOPE)
    assert offer is not None
    assert offer.facts["refs"] == refs[:2]  # like `missions`: not the later ref that would fit


def test_no_offer_without_an_admitted_ref_or_for_an_unknown_trigger() -> None:
    assert second_looks.offer(_hint(refs=()), SCOPE) is None
    assert second_looks.offer(_hint(refs=("../a.md", "b.txt", "/c.md")), SCOPE) is None
    assert second_looks.offer(_hint(trigger="repeated_request"), SCOPE) is None


# ---- what the model is sent --------------------------------------------------------------------


def test_a_second_look_sends_only_its_kind_and_title_to_the_model() -> None:
    refs = ("notes/garden-plan.md", "notes/beds.md")
    offer = second_looks.offer(_hint(refs=refs), SCOPE)
    found = second_looks.finding(
        Finding("m-" + SESSION, SCOPE, SESSION, LONG, "flaw", "Garden plan")
    )
    assert offer is not None
    now = Now(None, False, False, NOW, 0)
    for candidate in (offer, found):
        sent = canonical_json(
            build_state(candidate, Owner(), now, History(), contracts.DecisionSettings()).state
        )
        assert "second look at a finished long task" in sent
        assert "Garden plan" in sent
        for private in (*refs, "garden-plan", "beds", SESSION, TURN, SCOPE, "flaw", "m-"):
            assert private not in sent, private


# ---- finding -----------------------------------------------------------------------------------


def _found(**changes: Any) -> Finding:
    return replace(Finding("m-1", SCOPE, SESSION, CORRECTION, "approach", "Garden plan"), **changes)


def test_a_finding_names_the_look_the_kind_and_the_chat() -> None:
    item = second_looks.finding(_found())
    assert item.type == CORRECTION
    assert item.item_key == contracts.item_key(CORRECTION, "m-1", "found")
    assert item.title == "Garden plan"
    assert dict(item.facts) == {
        "workspace_id": SCOPE,
        "found": "approach",
        "session": SESSION,
        "chat": "Garden plan",
    }
    assert dict(item.words) == {}
    assert item.evidence == ()
    assert item.related_chat == SESSION
    assert item.related_ref is None
    assert item.deadline is None
    assert item.shown_elsewhere == ()
    assert dict(item.rules_evidence) == {"found": "approach"}
    assert item.origin_session is None
    assert item.work_allowed == ()


def test_a_finding_of_an_untitled_chat_has_a_blank_chat_fact() -> None:
    item = second_looks.finding(_found(title=contracts.UNTITLED_CHAT, kind="other"))
    assert item.title == contracts.UNTITLED_CHAT
    assert item.facts["chat"] == ""
    assert item.facts["found"] == "other"
    assert second_looks.finding(_found(title="")).facts["chat"] == ""


def test_a_finding_has_its_own_key_per_mission() -> None:
    assert (
        second_looks.finding(_found()).item_key
        != second_looks.finding(_found(mission_id="m-2")).item_key
    )
    offer = second_looks.offer(_hint(), SCOPE)
    assert offer is not None
    assert second_looks.finding(_found(look=LONG)).item_key != offer.item_key


# ---- yields ------------------------------------------------------------------------------------

START = NOW - timedelta(days=3)


def _rows(look: str, reasons: Sequence[str], *, first: datetime = START) -> list[LookRow]:
    """Rows for `look`, the first reason the oldest, one minute apart, newest first."""
    rows = [
        LookRow(look, "done", reason, first + timedelta(minutes=n))
        for n, reason in enumerate(reasons)
    ]
    return rows[::-1]


def _trigger(rows: Sequence[LookRow], look: str = LONG, **kw: Any) -> contracts.TriggerYield:
    found = second_looks.yields(rows, kw.pop("restarts", Restarts()), {}, learning=True, **kw)
    return next(t for t in found.triggers if t.look == look)


def test_ten_answered_with_two_accepted_pauses_the_type() -> None:
    reasons = ["accepted:flaw"] * 2 + ["rejected:flaw"] * 8
    trigger = _trigger(_rows(LONG, reasons))
    assert (trigger.made, trigger.accepted, trigger.rejected, trigger.nothing) == (10, 2, 8, 0)
    assert trigger.paused


def test_ten_answered_with_three_accepted_does_not_pause() -> None:
    reasons = ["accepted:flaw"] * 3 + ["rejected:flaw"] * 7
    assert not _trigger(_rows(LONG, reasons)).paused


def test_exactly_one_in_four_accepted_does_not_pause() -> None:
    reasons = ["accepted:flaw"] * 3 + ["rejected:flaw"] * 9
    assert not _trigger(_rows(LONG, reasons)).paused
    assert _trigger(_rows(LONG, ["accepted:flaw"] * 2 + ["rejected:flaw"] * 10)).paused


def test_nine_answered_never_pause() -> None:
    assert not _trigger(_rows(LONG, ["rejected:flaw"] * 9)).paused
    assert not _trigger(_rows(LONG, ["nothing"] * 9)).paused


def test_nothing_does_not_count_toward_a_second_look_pause() -> None:
    trigger = _trigger(_rows(LONG, ["nothing"] * 10))
    assert (trigger.made, trigger.accepted, trigger.rejected, trigger.nothing) == (10, 0, 0, 10)
    assert not trigger.paused
    mixed = _trigger(_rows(LONG, ["accepted:flaw"] * 3 + ["nothing"] * 7))
    assert not mixed.paused


def test_looks_that_did_not_answer_make_but_do_not_count_toward_a_pause() -> None:
    rows = _rows(LONG, ["rejected:flaw"] * 9 + ["app_closed", "", "nothing "])
    trigger = _trigger(rows)
    assert (trigger.made, trigger.rejected, trigger.nothing) == (12, 9, 0)
    assert not trigger.paused


def test_the_types_count_apart() -> None:
    rows = _rows(LONG, ["rejected:flaw"] * 10) + _rows(CORRECTION, ["accepted:flaw"])
    found = second_looks.yields(rows, Restarts(), {}, learning=True)
    assert [(t.look, t.made, t.paused) for t in found.triggers] == [
        (LONG, 10, True),
        (CORRECTION, 1, False),
    ]
    assert second_looks.paused_looks(found) == (LONG,)


def test_no_rows_count_nothing() -> None:
    found = second_looks.yields([], Restarts(), {}, learning=True)
    assert found == contracts.SecondLookYields(
        (
            contracts.TriggerYield(LONG, 0, 0, 0, 0, {}, False),
            contracts.TriggerYield(CORRECTION, 0, 0, 0, 0, {}, False),
        ),
        tuple(contracts.KindYield(kind, 0, False) for kind in contracts.FINDING_KINDS),
    )
    assert second_looks.paused_looks(found) == ()
    assert second_looks.paused_kinds(found) == ()


def test_a_try_again_stamp_cuts_the_rows_before_it() -> None:
    rows = _rows(LONG, ["rejected:flaw"] * 10)
    assert _trigger(rows).paused
    cut = START + timedelta(minutes=4)  # rows 4..9 remain, the stamp's own minute included
    again = _trigger(rows, restarts=Restarts(looks={LONG: cut}))
    assert (again.made, again.rejected, again.paused) == (6, 6, False)
    other = _trigger(rows, restarts=Restarts(looks={CORRECTION: cut}))
    assert (other.made, other.paused) == (10, True)  # the other type's stamp is not this one's


def test_a_restart_never_reaches_the_other_types_counts() -> None:
    rows = _rows(LONG, ["rejected:flaw"] * 10) + _rows(CORRECTION, ["rejected:flaw"] * 10)
    found = second_looks.yields(rows, Restarts(looks={LONG: NOW}), {}, learning=True)
    assert second_looks.paused_looks(found) == (CORRECTION,)


def test_learning_off_pauses_nothing_but_still_counts() -> None:
    rows = _rows(LONG, ["rejected:flaw"] * 10)
    found = second_looks.yields(rows, Restarts(), {}, learning=False)
    assert found.triggers[0].rejected == 10
    assert found.kinds[0].rejected_in_a_row == 10
    assert second_looks.paused_looks(found) == ()
    assert second_looks.paused_kinds(found) == ()


def test_the_not_useful_reasons_pass_through_for_their_type() -> None:
    reasons = {LONG: {"already_knew": 2, "bad_timing": 1}, CORRECTION: {"wrong_topic": 4}}
    found = second_looks.yields([], Restarts(), reasons, learning=True)
    assert dict(found.triggers[0].reasons) == {"already_knew": 2, "bad_timing": 1}
    assert dict(found.triggers[1].reasons) == {"wrong_topic": 4}
    unknown = second_looks.yields([], Restarts(), {LONG: {"other": 3}}, learning=True)
    assert dict(unknown.triggers[0].reasons) == {}


def _kind(rows: Sequence[LookRow], kind: str = "flaw", **kw: Any) -> contracts.KindYield:
    found = second_looks.yields(rows, kw.pop("restarts", Restarts()), {}, learning=True, **kw)
    return next(k for k in found.kinds if k.kind == kind)


def test_five_rejections_in_a_row_pause_a_kind() -> None:
    assert _kind(_rows(LONG, ["rejected:flaw"] * 4)) == contracts.KindYield("flaw", 4, False)
    assert _kind(_rows(LONG, ["rejected:flaw"] * 5)) == contracts.KindYield("flaw", 5, True)


def test_an_accept_resets_the_streak() -> None:
    older_first = ["rejected:flaw"] * 4 + ["accepted:flaw"] + ["rejected:flaw"] * 3
    assert _kind(_rows(LONG, older_first)) == contracts.KindYield("flaw", 3, False)
    newest_accept = ["rejected:flaw"] * 9 + ["accepted:flaw"]
    assert _kind(_rows(LONG, newest_accept)) == contracts.KindYield("flaw", 0, False)


def test_a_streak_reads_the_newest_rows_whatever_their_order() -> None:
    rows = _rows(LONG, ["accepted:flaw"] + ["rejected:flaw"] * 5)
    assert _kind(rows[::-1]).rejected_in_a_row == 5  # oldest first
    assert _kind(rows).paused


def test_a_kind_counts_across_types_and_apart_from_other_kinds() -> None:
    rows = _rows(LONG, ["rejected:flaw"] * 3, first=START) + _rows(
        CORRECTION,
        ["rejected:flaw"] * 2 + ["rejected:approach"] * 2,
        first=START + timedelta(hours=1),
    )
    assert _kind(rows, "flaw").rejected_in_a_row == 5
    assert _kind(rows, "flaw").paused
    assert _kind(rows, "approach").rejected_in_a_row == 2
    assert _kind(rows, "missing") == contracts.KindYield("missing", 0, False)


def test_nothing_and_other_make_no_streak() -> None:
    rows = _rows(LONG, ["rejected:other"] * 9 + ["nothing"] * 3 + ["app_closed"])
    found = second_looks.yields(rows, Restarts(), {}, learning=True)
    assert all(k.rejected_in_a_row == 0 and not k.paused for k in found.kinds)
    assert [k.kind for k in found.kinds] == list(contracts.FINDING_KINDS)  # no "other" entry
    # Neither a look that found nothing nor an unreadable one breaks a streak of the kind.
    between = ["rejected:flaw"] * 3 + ["nothing", "app_closed"] + ["rejected:flaw"] * 2
    assert _kind(_rows(LONG, between)).rejected_in_a_row == 5


def test_a_raise_again_stamp_cuts_the_kind_rows_before_it() -> None:
    rows = _rows(LONG, ["rejected:flaw"] * 6)
    assert _kind(rows).paused
    cut = START + timedelta(minutes=3)
    again = _kind(rows, restarts=Restarts(kinds={"flaw": cut}))
    assert (again.rejected_in_a_row, again.paused) == (3, False)
    other = _kind(rows, restarts=Restarts(kinds={"approach": cut}))
    assert other.paused  # another kind's stamp does not cut this one
    trigger = _trigger(rows, restarts=Restarts(kinds={"flaw": NOW}))
    assert (trigger.made, trigger.rejected) == (6, 6)  # Raise again leaves the type's counts


def test_paused_kinds_follow_the_kind_order() -> None:
    rows = _rows(LONG, ["rejected:missing"] * 5 + ["rejected:flaw"] * 5)
    assert second_looks.paused_kinds(second_looks.yields(rows, Restarts(), {}, learning=True)) == (
        "flaw",
        "missing",
    )


# ---- restarts ----------------------------------------------------------------------------------

LATER = NOW + timedelta(hours=2)


def test_restarted_and_restarts_round_trip() -> None:
    first = second_looks.restarted(None, look=LONG, now=NOW)
    assert first == {"looks": {LONG: stamp(NOW)}, "kinds": {}}
    both = second_looks.restarted(first, kind="flaw", now=LATER)
    assert both == {"looks": {LONG: stamp(NOW)}, "kinds": {"flaw": stamp(LATER)}}
    assert second_looks.restarts(both) == Restarts({LONG: NOW}, {"flaw": LATER})
    again = second_looks.restarted(both, look=LONG, now=LATER)
    assert second_looks.restarts(again).looks == {LONG: LATER}  # a newer stamp replaces
    assert second_looks.restarts(again).kinds == {"flaw": LATER}


def test_restarted_keeps_a_stamp_exactly() -> None:
    odd = datetime(2026, 11, 1, 9, 0, 0, 123_000, tzinfo=UTC)
    assert second_looks.restarts(second_looks.restarted(None, look=CORRECTION, now=odd)) == (
        Restarts({CORRECTION: odd}, {})
    )


def test_an_unset_preference_has_no_restarts() -> None:
    assert second_looks.restarts(None) == Restarts()
    assert second_looks.restarted(None, now=NOW) == {"looks": {}, "kinds": {}}


@pytest.mark.parametrize(
    "value",
    [
        [],
        "looks",
        7,
        {},
        {"looks": {}},
        {"kinds": {}},
        {"looks": {}, "kinds": {}, "extra": {}},
        {"looks": [], "kinds": {}},
        {"looks": {}, "kinds": "flaw"},
        {"looks": {"code_look": stamp(NOW)}, "kinds": {}},
        {"looks": {}, "kinds": {"other": stamp(NOW)}},
        {"looks": {LONG: stamp(NOW)}, "kinds": {"flaw": "2026-11-01"}},
        {"looks": {LONG: "2026-11-01T09:00:00Z"}, "kinds": {}},
        {"looks": {LONG: "2026-11-01T09:00:00.000+00:00"}, "kinds": {}},
        {"looks": {LONG: 7}, "kinds": {}},
        {"looks": {LONG: None}, "kinds": {}},
    ],
)
def test_restarts_refuses_an_unknown_key_or_a_bad_stamp(value: object) -> None:
    # A good half does not survive a bad half: a damaged value reads as no restarts at all.
    assert second_looks.restarts(value) == Restarts()
    assert second_looks.restarted(value, kind="flaw", now=NOW) == {
        "looks": {},
        "kinds": {"flaw": stamp(NOW)},
    }


def test_a_damaged_value_with_a_good_half_reads_as_no_restarts() -> None:
    value = {"looks": {LONG: stamp(NOW)}, "kinds": {"flaw": "bad"}}
    assert second_looks.restarts(value) == Restarts()


@pytest.mark.parametrize(
    "arguments",
    [{"look": "code_look"}, {"look": ""}, {"kind": "other"}, {"kind": "Flaw"}, {"look": "flaw"}],
)
def test_restarted_refuses_a_name_that_is_not_a_type_or_kind(arguments: dict[str, str]) -> None:
    with pytest.raises(ValueError, match="invalid_"):
        second_looks.restarted(None, now=NOW, **arguments)


# ---- project plans -----------------------------------------------------------------------------

STALLED = "stalled_project"
OVERDUE = "overdue_tasks"


def _plans(rows: Sequence[LookRow], restarts: Restarts | None = None) -> contracts.SecondLookYields:
    return second_looks.yields(
        rows, restarts or Restarts(), {}, learning=True, looks=contracts.PROJECT_PLANS, kinds=()
    )


def test_project_plan_yields_count_their_own_types_and_hold_no_kind_rows() -> None:
    found = _plans(_rows(STALLED, ["accepted:other"] * 2 + ["rejected:other"] * 8))
    assert tuple(t.look for t in found.triggers) == contracts.PROJECT_PLANS
    assert found.kinds == ()
    stalled, overdue = found.triggers
    assert (stalled.made, stalled.accepted, stalled.rejected, stalled.paused) == (10, 2, 8, True)
    assert (overdue.made, overdue.paused) == (0, False)


def test_ten_project_plans_with_three_accepted_do_not_pause() -> None:
    rows = _rows(OVERDUE, ["accepted:other"] * 3 + ["rejected:other"] * 7)
    assert not _plans(rows).triggers[1].paused


def test_nothing_counts_toward_a_project_plan_pause() -> None:
    found = _plans(_rows(STALLED, ["nothing"] * 10))
    assert (found.triggers[0].nothing, found.triggers[0].paused) == (10, True)


def test_second_look_rows_do_not_count_for_project_plans_and_the_reverse() -> None:
    rows = _rows(LONG, ["rejected:flaw"] * 10)
    assert all(t.made == 0 for t in _plans(rows).triggers)
    assert _trigger(_rows(STALLED, ["rejected:other"] * 10)).made == 0


def test_project_plan_restarts_use_the_given_names() -> None:
    value = second_looks.restarted(
        None, look=STALLED, now=NOW, looks=contracts.PROJECT_PLANS, kinds=()
    )
    assert value == {"looks": {STALLED: stamp(NOW)}, "kinds": {}}
    found = second_looks.restarts(value, looks=contracts.PROJECT_PLANS, kinds=())
    assert found == Restarts({STALLED: NOW}, {})
    assert second_looks.restarts(value) == Restarts()  # the default names refuse a plan name
    other = second_looks.restarted(None, look=LONG, now=NOW)
    assert second_looks.restarts(other, looks=contracts.PROJECT_PLANS, kinds=()) == Restarts()


def test_project_plan_restarts_pause_nothing_before_their_stamp() -> None:
    rows = _rows(STALLED, ["rejected:other"] * 10)
    stamps = Restarts(looks={STALLED: NOW})
    assert _plans(rows).triggers[0].paused
    assert not _plans(rows, stamps).triggers[0].paused


def test_restarted_with_no_kinds_refuses_a_kind() -> None:
    with pytest.raises(ValueError, match="invalid_kind"):
        second_looks.restarted(None, kind="flaw", now=NOW, kinds=())
    with pytest.raises(ValueError, match="invalid_look"):
        second_looks.restarted(None, look=STALLED, now=NOW)
