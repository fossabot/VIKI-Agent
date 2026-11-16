"""The seat rules: whose card it is, whether a seat's routine made progress, how a run VIKI
started ended, which routine VIKI may start for its seat, and the plan of one cycle."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from viki_agent.automation.kanban import Card, KanbanRead
from viki_agent.decision_loop import templates
from viki_agent.decision_loop.contracts import (
    DecisionSettings,
    SeatFacts,
    SeatRoutine,
    SeatRun,
    SeatWaiting,
    canonical_json,
    sha256_hex,
)
from viki_agent.decision_loop.plan_brake import Hold
from viki_agent.decision_loop.seats import (
    MOVED,
    SeatAsk,
    SeatHold,
    SeatOffer,
    SeatPlan,
    SeatReport,
    SeatStart,
    SeatWorking,
    cards_since,
    owners,
    plan,
    progress,
    run_report,
    seat_cards,
    startable,
)

T0 = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)
RID = "00000000-0000-4000-8000-000000000001"
OTHER = "00000000-0000-4000-8000-000000000002"
THIRD = "00000000-0000-4000-8000-000000000003"


def at(minutes: int) -> datetime:
    return T0 + timedelta(minutes=minutes)


def card(ref: str, status: str | None, minute: int, *tags: str) -> Card:
    return Card(ref, ref, status, tags, at(minute))


def run(
    start: int,
    end: int | None,
    *,
    dispatched: bool = True,
    outcome: str | None = "succeeded",
    kind: str = "decision",
    routine_id: str = RID,
    attempt_id: str = "a1",
    reason: str = "",
) -> SeatRun:
    return SeatRun(
        attempt_id=attempt_id,
        routine_id=routine_id,
        kind=kind,
        session_id="s-" + attempt_id,
        started=at(start),
        dispatched=dispatched,
        finished=None if end is None else at(end),
        outcome=None if end is None else outcome,
        reason=reason,
    )


def routine(
    routine_id: str = RID,
    *,
    state: str = "enabled",
    revision: int = 2,
    tested: int | None = 2,
    seat: str | None = "research",
    stop_at_draft: bool = False,
) -> SeatRoutine:
    return SeatRoutine(routine_id, state, revision, tested, seat, stop_at_draft)


def facts(
    *runs: SeatRun,
    routines: tuple[SeatRoutine, ...] = (),
    complete: bool = True,
    free: int = 2,
    today: int = 0,
    paused: bool = False,
) -> SeatFacts:
    newest: dict[str, SeatRun] = {}
    for one in runs:
        if one.routine_id not in newest or one.started > newest[one.routine_id].started:
            newest[one.routine_id] = one
    return SeatFacts(
        routines=routines,
        runs=tuple(sorted(runs, key=lambda one: one.started, reverse=True)),
        newest=newest,
        free=free,
        today=today,
        paused=paused,
        complete=complete,
    )


def test_seat_cards_take_the_exact_tag_in_kanban_order_and_cards_since_the_oldest_todo() -> None:
    first = card("kanban/b.md", "todo", 5, "owner/research")
    other = card("kanban/c.md", "todo", 1, "owner/writing")
    near = card("kanban/d.md", "todo", 0, "owner/researcher", "research")
    done = card("kanban/a.md", "done", -9, "owner/research")
    untagged = card("kanban/e.md", "todo", -9)
    kanban = KanbanRead((first, other, near, done, untagged), (), True)

    assert seat_cards(kanban, "research") == (first, done)
    assert seat_cards(kanban, "writing") == (other,)
    assert cards_since(kanban) == at(0)
    assert cards_since(KanbanRead((done, untagged), (), True)) is None


def test_a_card_written_outside_every_run_is_the_owners() -> None:
    closed = run(10, 20)
    still_open = run(40, None)
    both = facts(closed, still_open)

    assert owners(card("kanban/a.md", "todo", 9), both)
    assert owners(card("kanban/a.md", "todo", 21), both)
    # Bounds are inclusive, and an open run's window has no end.
    assert not owners(card("kanban/a.md", "todo", 10), both)
    assert not owners(card("kanban/a.md", "todo", 20), both)
    assert not owners(card("kanban/a.md", "todo", 15), both)
    assert not owners(card("kanban/a.md", "todo", 400), both)
    # A run of any routine counts, and incomplete facts make no card the user's.
    assert not owners(card("kanban/a.md", "todo", 15), facts(run(10, 20, kind="schedule")))
    assert owners(card("kanban/a.md", "todo", 9), facts())
    assert not owners(card("kanban/a.md", "todo", 9), facts(complete=False))


def test_progress_takes_a_first_run_a_moved_card_or_a_later_counted_change() -> None:
    newest = run(10, 20)
    assert progress(None, (), ())
    for status in MOVED:
        assert progress(newest, (card("kanban/a.md", status, 15),), ())
    # A moved card outside the window, or a todo card inside it, moved nothing.
    assert not progress(newest, (card("kanban/a.md", "done", 25),), ())
    assert not progress(newest, (card("kanban/a.md", "todo", 15),), ())
    # A counted todo card changed after the run finished; at the end is the run's own write.
    assert progress(newest, (), (card("kanban/a.md", "todo", 21),))
    assert not progress(newest, (), (card("kanban/a.md", "todo", 20),))
    # An open run has not finished, so no counted change follows it.
    assert not progress(run(10, None), (), (card("kanban/a.md", "todo", 30),))


def test_an_undispatched_newest_run_is_no_progress() -> None:
    blocked = run(10, 20, dispatched=False, outcome="blocked")
    moved = (card("kanban/a.md", "done", 15),)

    assert progress(run(10, 20), moved, ())
    assert not progress(blocked, moved, ())
    # Only the user's change after it starts the seat again.
    assert progress(blocked, moved, (card("kanban/b.md", "todo", 30),))


@pytest.mark.parametrize(
    ("newest", "cards", "report"),
    [
        (run(10, 20, dispatched=False, outcome="failed"), (), "blocked"),
        (run(10, 20, dispatched=False, outcome="blocked"), (), "blocked"),
        (run(10, 20, dispatched=False, outcome="cancelled"), (), None),
        (run(10, 20, outcome="failed"), (), "failed"),
        (run(10, 20, outcome="expired"), (), "deadline"),
        (run(10, 20, outcome="cancelled"), (), "stopped"),
        (run(10, 20, outcome="succeeded"), (), "no_progress"),
        (run(10, 20, outcome="blocked"), (card("kanban/a.md", "done", 25),), "no_progress"),
        (run(10, 20, outcome="succeeded"), (card("kanban/a.md", "review", 15),), None),
        (run(10, 20, outcome="unknown"), (), None),
        (run(10, None), (), None),
        (run(10, 20, outcome="failed", kind="schedule"), (), None),
        (run(10, 20, outcome="succeeded", kind="manual"), (), None),
        (None, (), None),
    ],
)
def test_run_reports_name_how_a_viki_run_ended(
    newest: SeatRun | None, cards: tuple[Card, ...], report: str | None
) -> None:
    assert run_report(newest, cards) == report
    # The item that reports it words it, with the run's session to open.
    assert report is None or (
        report in templates._SEAT_REPORTS and report not in templates._SEAT_HOLDS
    )


def test_startable_needs_one_enabled_tested_card_gated_routine() -> None:
    ready = routine()
    assert startable(facts(routines=(ready,)), RID) == ready
    assert startable(facts(routines=(ready,)), OTHER) is None
    for refused in (
        routine(state="paused"),
        routine(state="tested"),
        routine(seat=None),
        routine(tested=None),
        routine(tested=1),
    ):
        assert startable(facts(routines=(refused,)), RID) is None
    # A second enabled routine for the seat refuses both; a paused one or another seat does not.
    rival = routine(OTHER)
    assert startable(facts(routines=(ready, rival)), RID) is None
    assert startable(facts(routines=(ready, rival)), OTHER) is None
    assert startable(facts(routines=(ready, routine(OTHER, state="paused"))), RID) == ready
    assert startable(facts(routines=(ready, routine(OTHER, seat="writing"))), RID) == ready


# ---- the plan ----------------------------------------------------------------------------------

RESEARCH = "owner/research"
WRITING = "owner/writing"
TEAM = (routine(), routine(OTHER, seat="writing"))
OWNER_CARD = card("kanban/a.md", "todo", 30, RESEARCH)


def planned(
    cards: Iterable[Card],
    seen: SeatFacts,
    *,
    waiting: Iterable[SeatWaiting] = (),
    policy: str | None = "ordinary",
    hold: Hold | None = None,
    quiet: bool = False,
    **settings: Any,
) -> SeatPlan:
    chosen = replace(DecisionSettings(), **{"grant_team": "allowed", **settings})
    kanban = KanbanRead(tuple(cards), (), True)
    return plan(kanban, seen, waiting, chosen, policy=policy, hold=hold, quiet=quiet)


def cards_hash(*cards: Card) -> str:
    return sha256_hex(canonical_json(sorted([c.ref, c.changed.isoformat()] for c in cards)))


Effect = Callable[[dict[str, Card], str, int], Iterable[Card]]


def cycles(cards: Iterable[Card], runs: Iterable[SeatRun], effect: Effect, **settings: Any) -> int:
    """Plans four cycles; each start becomes a finished run whose `effect` writes cards inside
    its window. Answers how many starts were planned."""
    kanban = {one.ref: one for one in cards}
    done = list(runs)
    started = 0
    for _ in range(4):
        step = planned(kanban.values(), facts(*done, routines=TEAM), **settings)
        for start in step.starts:
            begin = 100 + 20 * len(done)
            done.append(run(begin, begin + 10, routine_id=start.routine_id, attempt_id=f"r{begin}"))
            kanban.update((one.ref, one) for one in effect(kanban, start.routine_id, begin + 5))
        started += len(step.starts)
    return started


def test_the_grant_decides_start_offer_or_nothing() -> None:
    finished = run(10, 20, attempt_id="a1")
    working = run(40, None, routine_id=OTHER, attempt_id="a2")
    seen = facts(finished, working, routines=TEAM)

    allowed = planned((OWNER_CARD,), seen)
    assert allowed.starts == (
        SeatStart(RID, 2, "a1", sha256_hex('[["kanban/a.md","2026-11-01T09:30:00+00:00"]]')),
    )
    assert allowed.offers == () and allowed.holds == ()
    assert allowed.working == (SeatWorking(OTHER, "writing", "a2", "s-a2"),)
    # The run that moved nothing still reports, beside the start the user's later card earns.
    assert allowed.reports == (SeatReport(RID, "research", "a1", "s-a1", "no_progress"),)

    ask = planned((OWNER_CARD,), seen, grant_team="ask")
    assert ask.starts == ()
    assert ask.offers == (SeatOffer(RID, "research", False, cards_hash(OWNER_CARD)),)
    assert (ask.working, ask.reports) == (allowed.working, allowed.reports)

    assert planned((OWNER_CARD,), seen, grant_team="off") == SeatPlan()
    # A never-run routine starts with no newest attempt.
    assert planned((OWNER_CARD,), facts(routines=TEAM)).starts == (
        SeatStart(RID, 2, None, cards_hash(OWNER_CARD)),
    )


def test_waiting_runs_ask_and_open_decision_runs_work() -> None:
    plain = routine(THIRD, seat=None)
    open_runs = (
        run(40, None, attempt_id="a1"),
        run(40, None, routine_id=OTHER, attempt_id="a2"),
        run(40, None, routine_id=THIRD, attempt_id="a3", kind="schedule"),
    )
    seen = facts(*open_runs, routines=(*TEAM, plain))
    waiting = (
        SeatWaiting(RID, "a1", "s-a1", False),
        SeatWaiting(THIRD, "a3", "s-a3", True),
        SeatWaiting("another vault's routine", "a9", "s-a9", True),
    )

    step = planned((OWNER_CARD,), seen, waiting=waiting)
    assert step.asks == (
        SeatAsk(RID, "research", "a1", "s-a1", False),
        SeatAsk(THIRD, None, "a3", "s-a3", True),
    )
    # A waiting run asks instead of working; an open schedule run is not VIKI's.
    assert step.working == (SeatWorking(OTHER, "writing", "a2", "s-a2"),)
    assert step.starts == ()
    assert planned((OWNER_CARD,), seen, waiting=waiting, watch_routines=False).asks == ()
    assert planned((OWNER_CARD,), seen, waiting=waiting, grant_team="off") == SeatPlan(
        asks=step.asks
    )
    # Only a seat routine's decision run reports.
    failed = facts(run(10, 20, routine_id=THIRD, outcome="failed"), routines=(plain,))
    assert planned((), failed).reports == ()


@pytest.mark.parametrize(
    ("changes", "reasons"),
    [
        ({"routines": (routine(state="paused"),)}, ()),
        ({"runs": (run(40, None),)}, ()),
        ({"routines": (routine(tested=1),)}, ("untested",)),
        ({"routines": (routine(tested=None),)}, ("untested",)),
        ({"paused": True}, ()),
        ({"free": 0}, ()),
        ({"today": 4}, ("limit",)),
        ({"limit_team": 0}, ("limit",)),
        ({"routines": (routine(OTHER), routine())}, ("two_routines",)),
        ({"hold": Hold(None)}, ("plan_brake",)),
        ({"quiet": True}, ("quiet",)),
    ],
)
def test_no_start_when_a_check_fails(changes: dict[str, Any], reasons: tuple[str, ...]) -> None:
    def step(**edits: Any) -> SeatPlan:
        given = {"routines": (routine(),), "runs": (), **edits}
        seen = facts(
            *given.pop("runs"),
            routines=given.pop("routines"),
            **{k: given.pop(k) for k in ("free", "today", "paused") if k in given},
        )
        return planned((OWNER_CARD,), seen, **given)

    assert len(step().starts) == 1
    held = step(**changes)
    assert held.starts == () and held.offers == ()
    # Each reported hold names the lowest routine id of its seat.
    assert held.holds == tuple(SeatHold(RID, "research", reason) for reason in reasons)
    # The item that reports a hold words it and opens the routine, which has no session.
    assert set(reasons) <= templates._SEAT_HOLDS <= set(templates._SEAT_REPORTS)


def test_loops_start_nothing_more() -> None:
    owner = card("kanban/a.md", "todo", 0, RESEARCH)

    def self_append(kanban: dict[str, Card], routine_id: str, minute: int) -> Iterable[Card]:
        # The seat appends to its own card, which stays todo.
        return (card("kanban/a.md", "todo", minute, RESEARCH),)

    def cross(kanban: dict[str, Card], routine_id: str, minute: int) -> Iterable[Card]:
        # Each seat leaves its own card and writes a new one for the other seat.
        if routine_id == RID:
            return (card(f"kanban/w{minute}.md", "todo", minute, WRITING),)
        return (card(f"kanban/r{minute}.md", "todo", minute, RESEARCH),)

    def fetched(kanban: dict[str, Card], routine_id: str, minute: int) -> Iterable[Card]:
        # Text the run fetched has it write a card for another seat.
        return (card("kanban/a.md", "done", minute), card("kanban/x.md", "todo", minute, WRITING))

    assert cycles((owner,), (), self_append) == 1
    # Each seat starts once for the user's card, and neither starts for the other's cards.
    assert cycles((owner, card("kanban/b.md", "todo", 0, WRITING)), (), cross) == 2
    assert cycles((owner,), (), fetched) == 1
    # A scheduled run that failed left the user's card todo, and the user changed nothing.
    failed = run(10, 20, kind="schedule", outcome="failed")
    assert cycles((owner,), (failed,), self_append) == 0
    # The seat-written card is offered instead of started.
    left_card = card("kanban/x.md", "todo", 15, WRITING)
    left = planned(
        (card("kanban/a.md", "done", 15), left_card),
        facts(run(10, 20), routines=TEAM),
    )
    assert left.starts == ()
    assert left.offers == (SeatOffer(OTHER, "writing", True, cards_hash(left_card)),)


def test_two_owner_cards_start_the_seat_twice() -> None:
    def take_one(kanban: dict[str, Card], routine_id: str, minute: int) -> Iterable[Card]:
        first = next(one for one in kanban.values() if one.status == "todo")
        return (card(first.ref, "done", minute, RESEARCH),)

    owner_cards = (
        card("kanban/a.md", "todo", 0, RESEARCH),
        card("kanban/b.md", "todo", 1, RESEARCH),
    )
    assert cycles(owner_cards, (), take_one) == 2


def test_handoffs_decide_between_start_and_offer() -> None:
    handed = card("kanban/w.md", "todo", 15, WRITING)
    seen = facts(run(10, 20), routines=TEAM)

    on = planned((handed,), seen, team_handoffs=True)
    assert on.starts == (SeatStart(OTHER, 2, None, cards_hash(handed)),) and on.offers == ()
    off = planned((handed,), seen)
    assert off.starts == () and off.offers == (
        SeatOffer(OTHER, "writing", True, cards_hash(handed)),
    )
    asked = planned((handed,), seen, team_handoffs=True, grant_team="ask")
    assert asked.offers == (SeatOffer(OTHER, "writing", False, cards_hash(handed)),)
    # With handoffs on, a user's and a seat's card are both counted.
    both = planned((handed, card("kanban/v.md", "todo", 30, WRITING)), seen, team_handoffs=True)
    assert both.starts[0].cards == cards_hash(handed, card("kanban/v.md", "todo", 30, WRITING))


def test_an_offer_carries_the_hash_of_the_cards_that_justify_it() -> None:
    """The offer's hash moves when a counted todo card is added or changed, and stays put for a
    change to a card it does not count."""

    def offered(*cards: Card) -> str:
        (offer,) = planned(cards, facts(routines=TEAM), grant_team="ask").offers
        return offer.cards

    first = offered(OWNER_CARD)
    assert first == cards_hash(OWNER_CARD)
    added = card("kanban/b.md", "todo", 40, RESEARCH)
    assert offered(OWNER_CARD, added) == cards_hash(OWNER_CARD, added) != first
    assert offered(replace(OWNER_CARD, changed=at(35))) != first
    # A done card or an untagged one is not counted.
    others = (card("kanban/d.md", "done", 50, RESEARCH), card("kanban/u.md", "todo", 50))
    assert offered(OWNER_CARD, *others) == first
    assert offered(OWNER_CARD, replace(others[0], changed=at(55))) == first


@pytest.mark.parametrize("policy", [None, "protected", "read_only"])
@pytest.mark.parametrize("grant", ["allowed", "ask"])
def test_nothing_starts_or_is_offered_outside_an_ordinary_vault(
    policy: str | None, grant: str
) -> None:
    handed = card("kanban/w.md", "todo", 15, WRITING)
    for routines in (TEAM, (routine(tested=1),), (routine(), routine(OTHER))):
        seen = facts(routines=routines)
        step = planned((OWNER_CARD, handed), seen, policy=policy, grant_team=grant)
        assert (step.starts, step.offers, step.holds) == ((), (), ())
        # The same cards in an ordinary vault start, offer or report.
        ordinary = planned((OWNER_CARD, handed), seen, grant_team=grant)
        assert ordinary.starts or ordinary.offers or ordinary.holds


def test_quiet_hours_start_only_a_stop_at_draft_routine() -> None:
    drafting = facts(routines=(routine(stop_at_draft=True),))
    assert planned((OWNER_CARD,), drafting, quiet=True).starts == (
        SeatStart(RID, 2, None, cards_hash(OWNER_CARD)),
    )
    held = planned((OWNER_CARD,), facts(routines=(routine(),)), quiet=True)
    assert held.starts == () and held.holds == (SeatHold(RID, "research", "quiet"),)
    # Under Ask first, quiet hours and the brake keep the offer: the user's Start is not held.
    for quiet, hold in ((True, None), (False, Hold(None))):
        asked = planned(
            (OWNER_CARD,), facts(routines=(routine(),)), grant_team="ask", quiet=quiet, hold=hold
        )
        assert asked.offers == (SeatOffer(RID, "research", False, cards_hash(OWNER_CARD)),)
        assert asked.holds == ()


def test_starts_stop_at_free_slots_and_the_limit() -> None:
    ids = [f"00000000-0000-4000-8000-00000000001{n}" for n in range(3)]
    names = ("alpha", "beta", "gamma")
    team = tuple(routine(rid, seat=name) for rid, name in zip(ids, names, strict=True))
    cards = [card(f"kanban/{name}.md", "todo", 30, "owner/" + name) for name in names]

    def step(free: int, today: int, limit: int) -> SeatPlan:
        return planned(cards, facts(routines=team, free=free, today=today), limit_team=limit)

    # Seats go in name order; a cycle with no free slot holds nothing it reports.
    one_slot = step(free=1, today=0, limit=4)
    assert [s.routine_id for s in one_slot.starts] == ids[:1] and one_slot.holds == ()
    two_slots = step(free=2, today=0, limit=4)
    assert [s.routine_id for s in two_slots.starts] == ids[:2] and two_slots.holds == ()
    assert step(free=0, today=0, limit=4) == SeatPlan()
    # The day's limit, when it binds first, is reported for every seat past it.
    last_run = step(free=2, today=3, limit=4)
    assert [s.routine_id for s in last_run.starts] == ids[:1]
    assert last_run.holds == tuple(
        SeatHold(rid, n, "limit") for rid, n in zip(ids[1:], names[1:], strict=True)
    )


def test_incomplete_facts_start_and_offer_nothing() -> None:
    handed = card("kanban/w.md", "todo", 15, WRITING)
    for grant in ("allowed", "ask"):
        for handoffs in (False, True):
            step = planned(
                (OWNER_CARD, handed),
                facts(run(10, 20), routines=TEAM, complete=False),
                grant_team=grant,
                team_handoffs=handoffs,
            )
            assert (step.starts, step.offers, step.holds) == ((), (), ())


def test_no_progress_is_reported_only_over_a_whole_kanban() -> None:
    seen = facts(run(10, 20), routines=TEAM)
    # The run's card stayed todo in the Kanban as read.
    cards = (card("kanban/a.md", "todo", 0, RESEARCH),)
    report = (SeatReport(RID, "research", "a1", "s-a1", "no_progress"),)
    chosen = replace(DecisionSettings(), grant_team="allowed")

    def reports(kanban: KanbanRead) -> tuple[SeatReport, ...]:
        return plan(kanban, seen, (), chosen, policy="ordinary", hold=None, quiet=False).reports

    assert reports(KanbanRead(cards, (), True)) == report
    # A card the run moved may be one that went unread.
    assert reports(KanbanRead(cards, (), False)) == ()
    assert reports(KanbanRead(cards, ("kanban/b.md",), True)) == ()
    # A report that reads no card stays.
    failed = facts(run(10, 20, outcome="failed"), routines=TEAM)
    assert plan(
        KanbanRead(cards, (), False), failed, (), chosen, policy="ordinary", hold=None, quiet=False
    ).reports == (SeatReport(RID, "research", "a1", "s-a1", "failed"),)


def test_a_run_the_owner_stopped_is_no_progress_for_vikis_own_start() -> None:
    """After the user's Stop, the card that run moved starts nothing on VIKI's own; a counted
    card changed since does, and Ask first still offers Start."""
    moved = card("kanban/a.md", "working", 15, RESEARCH)  # the run set it working, then was stopped
    waiting = card("kanban/b.md", "todo", 0, RESEARCH)  # the user's, untouched since
    stopped = facts(run(10, 20, outcome="cancelled", reason="stopped"), routines=TEAM)

    allowed = planned((moved, waiting), stopped)
    assert (allowed.starts, allowed.offers, allowed.holds) == ((), (), ())
    assert allowed.reports == (SeatReport(RID, "research", "a1", "s-a1", "stopped"),)
    assert planned((moved, waiting), stopped, team_handoffs=True).starts == ()
    asked = planned((moved, waiting), stopped, grant_team="ask")
    assert asked.offers == (SeatOffer(RID, "research", False, cards_hash(waiting)),)
    changed = replace(waiting, changed=at(25))
    assert planned((moved, changed), stopped).starts == (
        SeatStart(RID, 2, "a1", cards_hash(changed)),
    )
    # A run an app close or a closed chat cut off was not stopped by the user.
    for reason in ("app_closed", "session_closed"):
        closed = facts(run(10, 20, outcome="cancelled", reason=reason), routines=TEAM)
        assert planned((moved, waiting), closed).starts == (
            SeatStart(RID, 2, "a1", cards_hash(waiting)),
        )


def test_a_card_changed_after_the_read_counts_for_nothing_until_its_time() -> None:
    """A todo card whose file time is ahead of the Kanban's read neither starts nor is offered,
    so a run that fails without moving it is not started again; once the clock passes that
    time, the card changed after the run and starts the seat once."""
    ahead = card("kanban/a.md", "todo", 1000, RESEARCH)
    failed = run(10, 20, outcome="failed")

    def step(read_at: int | None, *runs: SeatRun, grant: str = "allowed") -> SeatPlan:
        kanban = KanbanRead((ahead,), (), True, None if read_at is None else at(read_at))
        chosen = replace(DecisionSettings(), grant_team=grant)
        seen = facts(*runs, routines=TEAM)
        return plan(kanban, seen, (), chosen, policy="ordinary", hold=None, quiet=False)

    for runs in ((), (failed,)):
        assert step(100, *runs).starts == () and step(100, *runs, grant="ask").offers == ()
    behind = card("kanban/b.md", "todo", 50, RESEARCH)
    assert cards_since(KanbanRead((ahead,), (), True, at(100))) is None
    assert cards_since(KanbanRead((ahead, behind), (), True, at(100))) == at(50)
    # A Kanban no read made has no read time and trusts every change time, as before.
    assert step(None, failed).starts == (SeatStart(RID, 2, "a1", cards_hash(ahead)),)
    assert step(1001, failed).starts == (SeatStart(RID, 2, "a1", cards_hash(ahead)),)
    again = run(1001, 1010, attempt_id="a2", outcome="failed")
    assert step(1100, failed, again).starts == ()
