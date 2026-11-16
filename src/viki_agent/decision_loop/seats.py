"""Whose Kanban card it is and whether a seat's routine made progress since its last run.

A seat is the `<seat>` of a card's `owner/<seat>` tag, and a seat routine is a vault routine
whose revision claims that seat (`run_only_if.seat`). The rules here read only the Kanban
(`automation.kanban.KanbanRead`) and the facts `contracts.SeatHost.facts` returns:

- A card is the user's when the facts are complete and it last changed outside every run's
  window `[started, finished]` (an open run's window has no end), bounds inclusive, over the
  runs of every routine: a card a run may have written is never taken for the user's.
- A routine made progress when it never ran; when its newest run was dispatched and one of the
  seat's cards now in a `MOVED` status changed inside that run's window; or when a counted
  `todo` card changed after that run finished. An undispatched run moved nothing, so a run that
  could not start waits for a change to a counted card instead of starting again. What a run
  the user stopped (its reason `stopped`) moved starts nothing on VIKI's own: only a counted
  card changed since does, though Ask first still offers Start.
- A `todo` card whose change time is later than the Kanban's read (`KanbanRead.read_at`) counts
  for nothing until that time comes: a file time ahead of the clock says nothing of when the
  card changed, and it would count as changed after every run.
- A finished `decision` newest run reports how it ended (`run_report`).

`plan` turns one vault cycle's Kanban and facts into the records (`SeatStart` to `SeatPlan`) it
hands the cycle. Pure: no I/O, no clock read, no log.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from viki_agent.automation.kanban import Card, KanbanRead
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

# The card statuses a run moves a card into.
MOVED = ("working", "blocked", "review", "done")

_OWNER = "owner/"


@dataclass(frozen=True)
class SeatStart:
    """Start `routine_id` at `revision`; `newest` is the attempt id the plan saw (None when it
    never ran) and `cards` the hash of the counted cards."""

    routine_id: str
    revision: int
    newest: str | None
    cards: str


@dataclass(frozen=True)
class SeatOffer:
    """Offer the user Start; `handoff` says another seat's run wrote the card, and `cards` is
    the hash of the cards that justify the offer (the counted cards, or the seat's `todo` cards
    for a handoff)."""

    routine_id: str
    seat: str
    handoff: bool
    cards: str = ""


@dataclass(frozen=True)
class SeatHold:
    routine_id: str
    seat: str
    reason: str


@dataclass(frozen=True)
class SeatReport:
    routine_id: str
    seat: str
    attempt_id: str
    session_id: str
    reason: str


@dataclass(frozen=True)
class SeatWorking:
    """An open decision run; `seat` is None once its routine's revision claims no seat."""

    routine_id: str
    seat: str | None
    attempt_id: str
    session_id: str


@dataclass(frozen=True)
class SeatAsk:
    """A run waiting on a card; `seat` is None for a routine that claims no seat."""

    routine_id: str
    seat: str | None
    attempt_id: str
    session_id: str
    question: bool


@dataclass(frozen=True)
class SeatPlan:
    asks: tuple[SeatAsk, ...] = ()
    working: tuple[SeatWorking, ...] = ()
    starts: tuple[SeatStart, ...] = ()
    offers: tuple[SeatOffer, ...] = ()
    reports: tuple[SeatReport, ...] = ()
    holds: tuple[SeatHold, ...] = ()


def seat_cards(kanban: KanbanRead, seat: str) -> tuple[Card, ...]:
    """The cards tagged exactly `owner/<seat>`, in Kanban order."""
    tag = _OWNER + seat
    return tuple(card for card in kanban.cards if tag in card.tags)


def cards_since(kanban: KanbanRead) -> datetime | None:
    """The oldest change of a `todo` card for any seat, leaving out a card changed after the
    read: the runs that could have written one start no earlier. None when no such card
    waits."""
    return min(
        (
            card.changed
            for card in kanban.cards
            if card.status == "todo"
            and not _ahead(kanban, card)
            and any(tag.startswith(_OWNER) for tag in card.tags)
        ),
        default=None,
    )


def owners(card: Card, facts: SeatFacts) -> bool:
    """Whether `card` is the user's: facts complete and its change outside every run window."""
    return facts.complete and not any(_inside(card.changed, run) for run in facts.runs)


def progress(newest: SeatRun | None, cards: Iterable[Card], counted: Iterable[Card]) -> bool:
    """Whether the routine whose newest run is `newest` may start again: it never ran, its
    dispatched run moved one of the seat's `cards`, or a `counted` todo card changed after that
    run finished."""
    if newest is None:
        return True
    return _moved(newest, cards) or _changed_since(newest, counted)


def run_report(newest: SeatRun | None, cards: Iterable[Card]) -> str | None:
    """How a finished decision `newest` run ended, as its report says ("failed", "deadline",
    "stopped", "blocked" or "no_progress"), else None."""
    if newest is None or newest.kind != "decision" or newest.finished is None:
        return None
    outcome = newest.outcome
    if not newest.dispatched:
        return "blocked" if outcome in ("failed", "blocked") else None
    if outcome == "failed":
        return "failed"
    if outcome == "expired":
        return "deadline"
    if outcome == "cancelled":
        return "stopped"
    if outcome in ("succeeded", "blocked") and not _moved(newest, cards):
        return "no_progress"
    return None


def startable(facts: SeatFacts, routine_id: str) -> SeatRoutine | None:
    """The routine VIKI may start for its seat: enabled, card-gated, tested at its current
    revision and the only enabled routine claiming that seat. None otherwise."""
    routine = next((r for r in facts.routines if r.routine_id == routine_id), None)
    if (
        routine is None
        or routine.state != "enabled"
        or routine.seat is None
        or routine.tested_revision != routine.revision
    ):
        return None
    rivals = sum(1 for r in facts.routines if r.state == "enabled" and r.seat == routine.seat)
    return routine if rivals == 1 else None


def plan(
    kanban: KanbanRead,
    facts: SeatFacts,
    waiting: Iterable[SeatWaiting],
    settings: DecisionSettings,
    *,
    policy: str | None,
    hold: Hold | None,
    quiet: bool,
) -> SeatPlan:
    """One vault cycle's seat plan. `waiting` is every run waiting on a card (any vault),
    `policy` the vault's policy, `hold` the plan brake's hold and `quiet` whether quiet hours
    hold now.

    While `watch_routines`, each waiting run of a routine in `facts` asks. With the grant off
    nothing else is planned. Otherwise each routine whose newest run is an open decision run
    not waiting is working, and each seat routine's finished decision newest run reports how it
    ended (`no_progress` only over a Kanban read whole: complete, nothing unread). In an ordinary
    vault only, each seat (ascending) with an enabled routine and a `todo` card (one changed no
    later than the Kanban's read) goes through the checks below in order, and the first that
    applies decides:

    - two enabled routines claim it: held `two_routines`, under the lowest routine id;
    - its routine has an open run: nothing;
    - neither a start (a counted `todo` card, counted being every `todo` card with handoffs on
      and the user's otherwise, and progress over the counted cards; under Allowed, after a run
      the user stopped, a counted card changed since it finished) nor a handoff offer
      (handoffs off, no user's card, progress over every `todo` card): nothing;
    - incomplete facts or Pause all: nothing;
    - untested at its current revision: held `untested`; today's limit used: held `limit`;
    - the grant is not Allowed, or it is a handoff: a Start offer, with the hash of the counted
      cards (the `todo` cards for a handoff);
    - quiet hours and the routine does not stop at a draft: held `quiet`; the brake: held
      `plan_brake`;
    - no room left this cycle (free slots and the day's limit, less the starts already
      planned): held `limit` when the limit is the bound, else nothing;
    - otherwise a start, with the hash of the counted cards.
    """
    waiting = tuple(waiting)
    routines = {routine.routine_id: routine for routine in facts.routines}
    asks = tuple(
        SeatAsk(
            run.routine_id,
            routines[run.routine_id].seat,
            run.attempt_id,
            run.session_id,
            run.question,
        )
        for run in waiting
        if settings.watch_routines and run.routine_id in routines
    )
    if settings.grant_team == "off":
        return SeatPlan(asks=asks)

    waits = {run.attempt_id for run in waiting}
    whole = kanban.complete and not kanban.unread
    working: list[SeatWorking] = []
    reports: list[SeatReport] = []
    for routine in facts.routines:
        newest = facts.newest.get(routine.routine_id)
        if newest is None:
            continue
        if newest.kind == "decision" and newest.finished is None and newest.attempt_id not in waits:
            working.append(
                SeatWorking(routine.routine_id, routine.seat, newest.attempt_id, newest.session_id)
            )
        if routine.seat is not None:
            ended = run_report(newest, seat_cards(kanban, routine.seat))
            # A card the run moved may be one the Kanban read left out.
            if ended == "no_progress" and not whole:
                ended = None
            if ended is not None:
                reports.append(
                    SeatReport(
                        routine.routine_id,
                        routine.seat,
                        newest.attempt_id,
                        newest.session_id,
                        ended,
                    )
                )

    starts: list[SeatStart] = []
    offers: list[SeatOffer] = []
    holds: list[SeatHold] = []
    # Outside an ordinary vault VIKI starts, offers and reports nothing for a seat.
    seats = (
        sorted({r.seat for r in facts.routines if r.state == "enabled" and r.seat is not None})
        if policy == "ordinary"
        else []
    )
    left = settings.limit_team - facts.today
    for seat in seats:
        cards = seat_cards(kanban, seat)
        todo = [card for card in cards if card.status == "todo" and not _ahead(kanban, card)]
        if not todo:
            continue
        claims = sorted(
            (r for r in facts.routines if r.state == "enabled" and r.seat == seat),
            key=lambda r: r.routine_id,
        )
        routine = claims[0]
        if len(claims) > 1:
            holds.append(SeatHold(routine.routine_id, seat, "two_routines"))
            continue
        newest = facts.newest.get(routine.routine_id)
        if newest is not None and newest.finished is None:
            continue
        owned = [card for card in todo if owners(card, facts)]
        counted = todo if settings.team_handoffs else owned
        start = bool(counted) and progress(newest, cards, counted)
        if newest is not None and newest.reason == "stopped" and settings.grant_team == "allowed":
            # The user's Stop holds VIKI's own start: what that run moved is not progress, a
            # counted card changed since is (Ask first still offers Start).
            start = start and _changed_since(newest, counted)
        handoff = not settings.team_handoffs and not owned and progress(newest, cards, todo)
        if not (start or handoff) or not facts.complete or facts.paused:
            continue
        reason: str | None = None
        if routine.tested_revision != routine.revision:
            reason = "untested"
        elif left <= 0:
            reason = "limit"
        elif settings.grant_team != "allowed" or handoff:
            # The brake and quiet hours keep the offer: the user's own Start is not held.
            offers.append(
                SeatOffer(
                    routine.routine_id, seat, handoff, _cards_hash(todo if handoff else counted)
                )
            )
        elif quiet and not routine.stop_at_draft:
            reason = "quiet"
        elif hold is not None:
            reason = "plan_brake"
        elif min(facts.free, left) - len(starts) <= 0:
            # A full day is reported; a cycle with no free slot is not.
            reason = "limit" if left <= facts.free else None
        else:
            starts.append(
                SeatStart(
                    routine.routine_id,
                    routine.revision,
                    None if newest is None else newest.attempt_id,
                    _cards_hash(counted),
                )
            )
        if reason is not None:
            holds.append(SeatHold(routine.routine_id, seat, reason))
    return SeatPlan(
        asks=asks,
        working=tuple(working),
        starts=tuple(starts),
        offers=tuple(offers),
        reports=tuple(reports),
        holds=tuple(holds),
    )


def _cards_hash(counted: Iterable[Card]) -> str:
    """The counted cards' refs and change times, sorted, hashed: what a start's claim stores and
    what keys a Start offer."""
    return sha256_hex(
        canonical_json(sorted([card.ref, card.changed.isoformat()] for card in counted))
    )


def _ahead(kanban: KanbanRead, card: Card) -> bool:
    """Whether `card`'s change time is later than the Kanban's read, as a file synced from a
    computer whose clock runs ahead, or written before this clock was set back, can be."""
    return kanban.read_at is not None and card.changed > kanban.read_at


def _changed_since(run: SeatRun, counted: Iterable[Card]) -> bool:
    """Whether a `counted` card changed after finished `run` ended."""
    return run.finished is not None and any(card.changed > run.finished for card in counted)


def _inside(moment: datetime, run: SeatRun) -> bool:
    return run.started <= moment and (run.finished is None or moment <= run.finished)


def _moved(run: SeatRun, cards: Iterable[Card]) -> bool:
    """Whether dispatched `run` moved a card: one now in a MOVED status changed in its window."""
    return run.dispatched and any(
        card.status in MOVED and _inside(card.changed, run) for card in cards
    )
