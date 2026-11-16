"""The gates every start VIKI makes on its own reads again just before it, over the engine Rig of
`test_engine.py`: a change the user saves while a cycle is in flight (Never send, quiet hours,
the plan brake, Decision off or paused, the grant, the limit, the last window closed) holds that
cycle's mission, seat, check and inbox starts, each by its own share of the gates and at the
start's own clock, and a held start is not remembered; no plan reading yet this launch holds
VIKI's own missions, seat runs and inbox runs but not its saved checks; a Stop all holds VIKI's
missions and seat runs in the cycle in flight and for a cadence after it; a halt during a
mission or a seat start starts no further one; a failed cycle's log line names its step and the
store's own code; and an inbox item whose run quiet hours hold, or one stored before this
launch, offers Suggest destinations under Allowed, as VIKI starts no run for it then."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Callable, Iterator, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from tests.decision_loop.fakes import WALL
from tests.decision_loop.test_engine import (
    CARDS_HASH,
    CHECKS,
    DAY,
    HANG_GUARD_S,
    IDLE,
    KEY,
    LATE,
    LOGGER,
    LOOK,
    LOW_USE,
    MORE,
    OFFER,
    OVERDUE,
    OWNER_CARD,
    PREPARE,
    ROUTINE,
    SCOPE,
    SHED,
    SHED_KEY,
    STALL,
    STALLED,
    TRIAGE,
    TRIAGE_MORE,
    WAITING,
    Detect,
    RememberingInboxHost,
    Rig,
    at_wall,
    check_starts,
    checking,
    coding,
    count_starts,
    in_view,
    inbox_observation,
    inbox_starts,
    inboxing,
    kanban_observation,
    key_of,
    observation,
    plan_reading,
    projects,
    seat_facts,
    seat_starts,
    state_of,
    task,
    tasks,
)
from viki_agent import protection
from viki_agent.automation.kanban import Card
from viki_agent.automation.routines import stamp
from viki_agent.decision_loop.contracts import (
    COALESCE_S,
    OBSERVE_S,
    Candidate,
    CodeFolder,
    DecisionLoopError,
    ItemRecord,
    MissionRecord,
    SeatFacts,
    SeatRoutine,
    item_key,
)
from viki_agent.decision_loop.detectors import Observation
from viki_agent.events import TurnDone, Usage
from viki_agent.storage import StateError

WRITING = "00000000-0000-4000-8000-00000000d0d2"  # a second seat routine, for the writing seat
WRITING_CARD = Card(
    "kanban/draft.md", "Draft the post", "todo", ("owner/writing",), WALL - timedelta(hours=1)
)
SHED_APP_FOLDER = CodeFolder(
    "00000000-0000-4000-8000-0000000000ca", "shed-app", WALL - timedelta(days=2)
)
BARN_APP_FOLDER = CodeFolder(
    "00000000-0000-4000-8000-0000000000cb", "barn-app", WALL - timedelta(days=2)
)
UNDER_80 = plan_reading(use=0.6, reset=at_wall(3600))  # holds at a brake of 50, not of 80
QUIET: dict[str, object] = {  # WALL is 18:00 in JST
    "notifications.quiet_start": "17:00",
    "notifications.quiet_end": "19:00",
}


@pytest.fixture
async def rig(tmp_path: Path) -> AsyncIterator[Rig]:
    rig = Rig(tmp_path)
    yield rig
    await asyncio.wait_for(rig.engine.stop(), HANG_GUARD_S)


@pytest.fixture
def logged(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    """The engine's log, for a test that reads it itself."""
    return caplog


@pytest.fixture(autouse=True)
def no_failed_cycle(
    request: pytest.FixtureRequest, caplog: pytest.LogCaptureFixture
) -> Iterator[None]:
    """A start these tests see held must be held by its gate, not by a cycle that failed before
    it: no engine warning is logged, unless the test reads the log itself (`logged`)."""
    yield
    if "logged" not in request.fixturenames:
        assert [record.getMessage() for record in caplog.records if record.name == LOGGER] == []


def in_flight(rig: Rig, monkeypatch: pytest.MonkeyPatch, change: Callable[[], None]) -> None:
    """Make `change` once as the cycle reads its inputs (`MissionService.running`): after it has
    detected and admitted its candidates and planned the seats, before the model and the starts.
    This is the user saving while a cycle is in flight."""
    running = rig.missions.running
    made: list[bool] = []

    def changing() -> list[MissionRecord]:
        if not made:
            made.append(True)
            change()
        return running()

    monkeypatch.setattr(rig.missions, "running", changing)


def actions(item: ItemRecord | None) -> list[tuple[str, str]]:
    assert item is not None
    return [(action.type, action.label) for action in item.body.actions]


async def halted(rig: Rig) -> None:
    """Until every run the engine holds is halted."""
    async with asyncio.timeout(HANG_GUARD_S):
        while not all(run.halt.is_set() for run in rig.engine._runs):
            await asyncio.sleep(0)


# ---- missions ----------------------------------------------------------------------------------

KINDS = ("look", "second_look", "stalled_plan", "overdue_plan", "prepare")
LOOK_KINDS = KINDS[:4]  # the ones quiet hours hold


def offering(rig: Rig, kind: str) -> tuple[str, str]:
    """Set the rig so its next cycle commits one item of `kind` that VIKI starts on its own under
    Allowed (the default grant of each); answer its key and its grant's setting."""
    if kind == "look":
        coding(rig, IDLE)
        return LOOK, "decision.grant.look"
    if kind == "second_look":
        rig.observation = in_view()
        return OFFER, "decision.grant.second_look"
    if kind == "stalled_plan":
        rig.observation = projects(STALLED)
        return STALL, "decision.grant.project_plan"
    if kind == "overdue_plan":
        rig.observation = projects(late=(LATE,))
        return OVERDUE, "decision.grant.project_plan"
    rig.keys.save(KEY)
    rig.model.answers = dict(PREPARE)
    rig.observation = observation(task("Task a"))
    return key_of("Task a"), "decision.grant.prepare"


def saving(rig: Rig, hold: str, grant: str) -> tuple[Callable[[], None], Callable[[], None]]:
    """What the user does while the cycle is in flight for `hold`, and how it is undone."""
    values: dict[str, dict[str, object]] = {
        "never_send_tag": {"decision.never_send": json.dumps(["#private"])},
        "never_send_folder": {"decision.never_send": json.dumps(["90-private/"])},
        "never_send_invalid": {"decision.never_send": "not a list"},
        "brake": {"decision.plan_brake": "50"},
        "quiet": QUIET,
        "decision_off": {"decision.enabled": False},
        "paused": {"decision.paused_until": stamp(WALL + timedelta(hours=1))},
        "grant_ask": {grant: "ask"},
    }

    def save() -> None:
        if hold == "unattended":  # the last app window closed
            rig.attended = False
        elif hold == "stop_all":
            rig.engine.note_stop_all()
        else:
            rig.values.update(values[hold])

    before = {name: rig.values[name] for name in values.get(hold, {})}

    def undo() -> None:
        if hold == "unattended":
            rig.attended = True
        elif hold == "stop_all":  # the hold lasts a cadence: "change", one minute
            rig.clock.advance(OBSERVE_S, wall=False)
        else:
            rig.values.update(before)

    return save, undo


HOLDS = (
    "never_send_tag",
    "never_send_folder",
    "never_send_invalid",
    "brake",
    "quiet",
    "decision_off",
    "paused",
    "grant_ask",
    "unattended",
    "stop_all",
)


@pytest.mark.parametrize(
    ("kind", "hold"),
    [(kind, hold) for kind in KINDS for hold in HOLDS if hold != "quiet" or kind in LOOK_KINDS],
)
async def test_a_mission_start_reads_its_gates_again_just_before_it(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, kind: str, hold: str
) -> None:
    """The user saves a change while the cycle is in flight, after it detected the item: VIKI
    asks the missions for no start, so nothing is stored, and nothing is remembered either: once
    the change is undone, the next cycle starts the same item."""
    rig.reading = UNDER_80
    starts = count_starts(rig, monkeypatch)
    key, grant = offering(rig, kind)
    save, undo = saving(rig, hold, grant)
    in_flight(rig, monkeypatch, save)
    await rig.engine.recheck()
    held = rig.agenda.item(SCOPE, key)
    assert held is not None and (held.state, held.mission_id) == ("open", None)
    assert starts == [] and rig.rows("decision_missions") == []

    undo()
    await rig.engine.recheck()
    assert starts == [key]
    (mission,) = rig.missions.running()
    assert mission.item_key == key


@pytest.mark.parametrize(
    ("kind", "rule"),
    [("stalled_plan", "10-projects/garden/"), ("overdue_plan", "10-projects/plan/")],
)
async def test_never_send_saved_mid_cycle_holds_a_project_plan_for_good(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, kind: str, rule: str
) -> None:
    """The user holds the project back once the cycle has detected its plan: VIKI makes no chat
    whose brief would name the project's notes, and the next cycle, which detects with the rule,
    offers no plan to start."""
    starts = count_starts(rig, monkeypatch)
    offering(rig, kind)
    in_flight(rig, monkeypatch, lambda: rig.values.update({"decision.never_send": f'["{rule}"]'}))
    await rig.engine.recheck()
    await rig.engine.recheck()
    assert starts == [] and rig.host.calls == [] and rig.rows("decision_missions") == []


@pytest.mark.parametrize("kind", LOOK_KINDS)
async def test_quiet_hours_reached_mid_cycle_hold_a_look_at_the_starts_own_clock(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    """Quiet hours begin at 18:30 and the cycle reads its clock at 18:00; it reaches its start at
    18:31, which holds the look, as the start reads the clock again."""
    rig.values["notifications.quiet_start"] = "18:30"
    rig.values["notifications.quiet_end"] = "19:00"
    starts = count_starts(rig, monkeypatch)
    offering(rig, kind)
    in_flight(rig, monkeypatch, lambda: rig.clock.advance(31 * 60, monotonic=False))
    await rig.engine.recheck()
    assert starts == [] and rig.rows("decision_missions") == []


@pytest.mark.parametrize("brake", ["80", "off"])
@pytest.mark.parametrize("kind", KINDS)
async def test_no_plan_reading_yet_holds_vikis_own_mission_until_one_comes(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, kind: str, brake: str
) -> None:
    """Before this launch's first plan reading VIKI cannot tell a reached limit from a free one:
    at every brake, Off included, its own start waits, unstored and unremembered, and it starts
    at the first cycle after a reading comes."""
    rig.values["decision.plan_brake"] = brake
    rig.reading = None
    starts = count_starts(rig, monkeypatch)
    key, _grant = offering(rig, kind)
    await rig.engine.recheck()
    await rig.engine.recheck()
    held = rig.agenda.item(SCOPE, key)
    assert held is not None and (held.state, held.mission_id) == ("open", None)
    assert starts == [] and rig.rows("decision_missions") == []

    rig.reading = LOW_USE  # Claude Code reports the plan's use
    await rig.engine.recheck()
    assert starts == [key]


async def test_a_draft_refused_while_busy_starts_once_the_open_mission_ends(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """VIKI's own start waits while any mission is open, so one place stays the user's: Task b's
    draft is refused busy beside Task a's running one. That refusal holds it rather than answers
    it, so though its state never changes, the first cycle after Task a's chat ends starts it."""
    rig.keys.save(KEY)
    rig.model.answers = dict(PREPARE)
    rig.observation = observation(*tasks("a", "b"))
    starts = count_starts(rig, monkeypatch)
    await rig.engine.recheck()
    (first,) = rig.missions.running()
    assert first.item_key == key_of("Task a")
    assert starts == [key_of("Task a"), key_of("Task b")]  # b refused busy, not stored

    rig.host.actors[first.session_id].emit(TurnDone(status="success", usage=Usage()))
    await rig.engine.recheck()
    await rig.engine.recheck()
    (second,) = rig.missions.running()
    assert second.item_key == key_of("Task b")


# ---- Stop all ----------------------------------------------------------------------------------


async def test_a_stop_all_holds_vikis_mission_starts_for_a_cadence(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The user presses Stop all between cycles: the next cycle (the one its wake brings) starts
    no mission and remembers none; a cycle a cadence after the press starts it."""
    starts = count_starts(rig, monkeypatch)
    key, _grant = offering(rig, "prepare")
    rig.engine.note_stop_all()
    await rig.engine.recheck()
    rig.clock.advance(OBSERVE_S - 1, wall=False)
    await rig.engine.recheck()
    assert starts == [] and rig.rows("decision_missions") == []

    rig.clock.advance(1, wall=False)
    await rig.engine.recheck()
    assert starts == [key]


async def test_a_stop_all_holds_the_cycle_in_flight_however_long_it_then_runs(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The user presses Stop all while a cycle is in flight, and its reads then take longer than
    a cadence: it still starts no mission, as it read the Stop all count before its first await.
    The clock's next cycle starts it."""
    starts = count_starts(rig, monkeypatch)
    key, _grant = offering(rig, "look")

    def press_then_read_on() -> None:
        rig.engine.note_stop_all()
        rig.clock.advance(OBSERVE_S + 1, wall=False)

    in_flight(rig, monkeypatch, press_then_read_on)
    seen = len(rig.clock.waits)
    rig.engine.start()
    await rig.parked(seen)
    assert starts == [] and rig.rows("decision_missions") == []

    seen = len(rig.clock.waits)
    rig.clock.advance(OBSERVE_S)
    await rig.parked(seen)
    assert starts == [key]


async def test_the_cycle_a_stop_all_wakes_does_not_refill_the_slots_it_freed(rig: Rig) -> None:
    """Three code folders wait for a look and VIKI started what the running missions let it; the
    rest were refused while busy. The user presses Stop all, which notes it first, stops the
    chats and wakes the clock: the cycle that wake brings `COALESCE_S` later, and the one the
    stopped chats' poll brings, start no look in the freed slots. The clock's next cycle on its
    cadence starts a waiting one."""
    coding(rig, IDLE, SHED_APP_FOLDER, BARN_APP_FOLDER)

    def creates() -> list[tuple[Any, ...]]:
        return [call for call in rig.host.calls if call[0] == "create"]

    seen = len(rig.clock.waits)
    rig.engine.start()
    await rig.parked(seen)
    first = creates()
    assert 0 < len(first) < 3

    rig.engine.note_stop_all()  # as `DecisionLoopService.stop_missions` does, before any stop
    await rig.missions.stop_all()
    for _wake in range(2):  # the Stop all's own wake, then the stopped chats' poll
        seen = len(rig.clock.waits)
        rig.engine.wake("mission")
        rig.clock.advance(COALESCE_S)
        await rig.parked(seen)
    assert creates() == first and rig.missions.running() == []

    seen = len(rig.clock.waits)
    rig.clock.advance(OBSERVE_S)
    await rig.parked(seen)
    assert len(creates()) > len(first) and rig.missions.running() != []


async def test_a_stop_all_does_not_hold_vikis_inbox_run(rig: Rig) -> None:
    """Stop all stops no inbox suggestion run, so it holds none of VIKI's either."""
    host = inboxing(rig)
    rig.engine.note_stop_all()
    await rig.engine.recheck()
    assert inbox_starts(host) == [("start", SCOPE, TRIAGE)]


# ---- seats -------------------------------------------------------------------------------------


def two_seats(*, writing_drafts: bool = False) -> SeatFacts:
    """Two enabled, tested routines, research (revision 3) and writing (revision 2, stopping at a
    draft when `writing_drafts`), neither run yet, both slots free and no start today."""
    research = SeatRoutine(ROUTINE, "enabled", 3, 3, "research", False)
    writing = SeatRoutine(WRITING, "enabled", 2, 2, "writing", writing_drafts)
    return SeatFacts((research, writing), (), {}, 2, 0, False, True)


def seating(rig: Rig, *, writing_drafts: bool = False) -> None:
    """Set the rig so its next cycle plans both seats' starts under Allowed: research first."""
    rig.observation = kanban_observation(OWNER_CARD, WRITING_CARD)
    rig.seats.seat_facts = two_seats(writing_drafts=writing_drafts)
    rig.values["decision.grant.team"] = "allowed"


def started_routines(rig: Rig) -> list[str]:
    return [call[1] for call in seat_starts(rig)]


SEAT_HOLDS: dict[str, Callable[[Rig], None]] = {
    # The plan was made under a reading at 0.7; Claude Code reports 0.85 mid-cycle.
    "brake": lambda rig: setattr(rig, "reading", plan_reading(use=0.85, reset=at_wall(3600))),
    "brake_setting": lambda rig: rig.values.update({"decision.plan_brake": "50"}),
    "quiet": lambda rig: rig.values.update(QUIET),
    "decision_off": lambda rig: rig.values.update({"decision.enabled": False}),
    "grant_ask": lambda rig: rig.values.update({"decision.grant.team": "ask"}),
    "unattended": lambda rig: setattr(rig, "attended", False),
    "stop_all": lambda rig: rig.engine.note_stop_all(),
}


@pytest.mark.parametrize("hold", list(SEAT_HOLDS))
async def test_seat_starts_read_their_gates_again_just_before_them(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, hold: str
) -> None:
    """The plan names both seats' starts, then the user's change lands before the starts: no
    seat starts."""
    rig.reading = plan_reading(use=0.7, reset=at_wall(3600))
    seating(rig)
    in_flight(rig, monkeypatch, lambda: SEAT_HOLDS[hold](rig))
    await rig.engine.recheck()
    assert ("facts", SCOPE, OWNER_CARD.changed, DAY) in rig.seats.calls  # it planned them
    assert started_routines(rig) == []


@pytest.mark.parametrize("hold", list(SEAT_HOLDS))
async def test_a_change_after_the_first_seat_start_holds_the_second(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, hold: str
) -> None:
    """The user's change lands while the first seat's run starts: the second seat's start,
    which reads the gates again, does not start."""
    rig.reading = plan_reading(use=0.7, reset=at_wall(3600))
    seating(rig)
    start = rig.seats.start

    async def first_then_change(routine_id: str, revision: int, **options: Any) -> str:
        answer = await start(routine_id, revision, **options)
        SEAT_HOLDS[hold](rig)
        return answer

    monkeypatch.setattr(rig.seats, "start", first_then_change)
    await rig.engine.recheck()
    assert started_routines(rig) == [ROUTINE]


async def test_quiet_hours_reached_mid_cycle_start_only_a_seat_that_stops_at_a_draft(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Quiet hours turn on after the plan: research, whose routine does not stop at a draft, is
    held; writing, whose routine does, still starts, as in quiet hours planned from the start."""
    seating(rig, writing_drafts=True)
    in_flight(rig, monkeypatch, lambda: rig.values.update(QUIET))
    await rig.engine.recheck()
    assert started_routines(rig) == [WRITING]


async def test_no_plan_reading_yet_holds_vikis_seat_start_and_reports_it(rig: Rig) -> None:
    """With no plan reading yet this launch the seat's start is held and reported as the brake's,
    as the page names the hold; it starts at the first cycle after a reading comes."""
    rig.observation = kanban_observation(OWNER_CARD)
    rig.seats.seat_facts = seat_facts()
    rig.values["decision.grant.team"] = "allowed"
    rig.reading = None
    await rig.engine.recheck()
    assert seat_starts(rig) == []
    held = rig.agenda.item(SCOPE, item_key("seat_report", ROUTINE, "plan_brake", DAY))
    assert held is not None and held.state == "open"

    rig.reading = LOW_USE
    await rig.engine.recheck()
    assert seat_starts(rig) == [("start", ROUTINE, 3, None, CARDS_HASH, 4, DAY)]


async def test_a_stop_all_holds_vikis_seat_starts_for_a_cadence(rig: Rig) -> None:
    """A Stop all pressed between cycles holds the seat start of every cycle within a cadence of
    it; the cycle after starts it."""
    rig.observation = kanban_observation(OWNER_CARD)
    rig.seats.seat_facts = seat_facts()
    rig.values["decision.grant.team"] = "allowed"
    rig.engine.note_stop_all()
    await rig.engine.recheck()
    assert seat_starts(rig) == []

    rig.clock.advance(OBSERVE_S, wall=False)
    await rig.engine.recheck()
    assert seat_starts(rig) == [("start", ROUTINE, 3, None, CARDS_HASH, 4, DAY)]


# ---- a halt in the start loops -----------------------------------------------------------------


def between_cycles_then(rig: Rig, spawned: list[asyncio.Future[None]]) -> Callable[[], Any]:
    """An awaitable maker: the user's Delete all (`between_cycles`), which halts the cycle at
    once and then waits for it to end; it answers once the halt has landed."""

    async def delete_all() -> None:
        spawned.append(asyncio.ensure_future(rig.engine.between_cycles(lambda: None)))
        await halted(rig)

    return delete_all


async def test_a_halt_during_a_mission_start_asks_for_no_further_start(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two code folders' looks are committed; Delete all lands while the first chat is being
    made: the cycle asks the missions for no second start."""
    coding(rig, IDLE, SHED_APP_FOLDER)
    asked: list[str] = []
    spawned: list[asyncio.Future[None]] = []
    delete_all = between_cycles_then(rig, spawned)
    start = rig.missions.start

    async def first_halts(scope: str, item: ItemRecord, work: str, **options: Any) -> Any:
        asked.append(item.item_key)
        try:
            return await start(scope, item, work, **options)
        finally:
            if len(asked) == 1:
                await delete_all()

    monkeypatch.setattr(rig.missions, "start", first_halts)
    await rig.engine.recheck()
    await asyncio.wait_for(spawned[0], HANG_GUARD_S)
    assert len(asked) == 1 and len(rig.rows("decision_missions")) == 1


async def test_a_halt_during_a_seat_start_starts_no_further_seat(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both seats' starts are planned; Delete all lands while the first starts: the second does
    not."""
    seating(rig)
    spawned: list[asyncio.Future[None]] = []
    delete_all = between_cycles_then(rig, spawned)
    start = rig.seats.start

    async def first_halts(routine_id: str, revision: int, **options: Any) -> str:
        answer = await start(routine_id, revision, **options)
        await delete_all()
        return answer

    monkeypatch.setattr(rig.seats, "start", first_halts)
    await rig.engine.recheck()
    await asyncio.wait_for(spawned[0], HANG_GUARD_S)
    assert started_routines(rig) == [ROUTINE]


# ---- saved checks ------------------------------------------------------------------------------

CHECK_HOLDS: dict[str, Callable[[Rig], None]] = {
    "decision_off": lambda rig: rig.values.update({"decision.enabled": False}),
    "paused": lambda rig: rig.values.update(
        {"decision.paused_until": stamp(WALL + timedelta(hours=1))}
    ),
    "grant_off": lambda rig: rig.values.update({"decision.grant.check": "off"}),
    "grant_ask": lambda rig: rig.values.update({"decision.grant.check": "ask"}),
    "limit_0": lambda rig: rig.values.update({"decision.limit.check": 0}),
    "quiet": lambda rig: rig.values.update(QUIET),
    "unattended": lambda rig: setattr(rig, "attended", False),
}


@pytest.mark.parametrize("hold", list(CHECK_HOLDS))
async def test_a_change_during_the_starts_count_holds_vikis_checks(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, hold: str
) -> None:
    """The user's change lands while the start reads the day's count: the settings are read
    after it, so no check starts."""
    rig.values["decision.grant.check"] = "allowed"
    host = checking(rig, WAITING)
    stats = rig.engine._stats
    real = stats.drafts_today
    counts: list[str] = []

    def counting(*args: Any, **family: Any) -> Any:
        counts.append(args[0])
        if len(counts) == 2:  # the first is the admission's, the second the start's
            CHECK_HOLDS[hold](rig)
        return real(*args, **family)

    monkeypatch.setattr(stats, "drafts_today", counting)
    await rig.engine.recheck()
    assert counts == [SCOPE, SCOPE]
    assert state_of(rig, CHECKS) == "open" and check_starts(host) == []


@pytest.mark.parametrize("hold", list(CHECK_HOLDS))
async def test_a_change_after_the_first_folders_checks_holds_the_next_folder(
    rig: Rig, hold: str
) -> None:
    """Two folders wait; the user's change lands while the first folder's checks start: the
    second folder's start reads the settings again and does not start."""
    rig.values["decision.grant.check"] = "allowed"
    host = checking(rig, WAITING, SHED)
    start = host.start

    async def first_then_change(
        workspace_id: str, head: str, check_ids: Sequence[str], *, stops: int | None = None
    ) -> int:
        answer = await start(workspace_id, head, check_ids, stops=stops)
        CHECK_HOLDS[hold](rig)
        return answer

    host.start = first_then_change  # type: ignore[method-assign]
    await rig.engine.recheck()
    assert len(check_starts(host)) == 1
    assert [state_of(rig, key) for key in (CHECKS, SHED_KEY)] == ["open", "open"]


async def test_no_plan_reading_yet_holds_no_saved_check(rig: Rig) -> None:
    """The plan brake holds no check, as a check spends no plan: with no reading yet this launch
    VIKI still runs a waiting folder's checks."""
    rig.values["decision.grant.check"] = "allowed"
    rig.reading = None
    host = checking(rig, WAITING)
    await rig.engine.recheck()
    assert len(check_starts(host)) == 1


# ---- inbox suggestions -------------------------------------------------------------------------


async def test_no_plan_reading_yet_holds_vikis_inbox_run_until_one_comes(rig: Rig) -> None:
    rig.reading = None
    host = inboxing(rig)
    await rig.engine.recheck()
    await rig.engine.recheck()
    assert state_of(rig, TRIAGE) == "open" and inbox_starts(host) == []

    rig.reading = LOW_USE
    await rig.engine.recheck()
    assert inbox_starts(host) == [("start", SCOPE, TRIAGE)]


@pytest.mark.parametrize("hold", ["unattended", "quiet_by_the_clock"])
async def test_an_inbox_start_reads_the_window_and_the_clock_again(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, hold: str
) -> None:
    """The last window closes while the cycle is in flight, or quiet hours begin (at 18:30, after
    the cycle's clock read at 18:00) before the start: no run starts."""
    if hold == "quiet_by_the_clock":
        rig.values["notifications.quiet_start"] = "18:30"
        rig.values["notifications.quiet_end"] = "19:00"
    host = inboxing(rig)

    def change() -> None:
        if hold == "unattended":
            rig.attended = False
        else:
            rig.clock.advance(31 * 60, monotonic=False)

    in_flight(rig, monkeypatch, change)
    await rig.engine.recheck()
    assert state_of(rig, TRIAGE) == "open" and inbox_starts(host) == []


async def test_an_allowed_inbox_item_from_an_earlier_launch_offers_suggest_destinations(
    rig: Rig,
) -> None:
    """Quiet hours hold VIKI's run for a waiting inbox, whose item then offers Suggest
    destinations, as under Ask first, so the user can start it. VIKI Agent then restarts after
    quiet hours: the new engine starts no run for the stored item, so its capture text is not
    sent on every launch, and the item still offers Suggest destinations. A state raised in this
    launch starts, and offers none."""
    rig.values.update(QUIET)
    first = inboxing(rig)
    await rig.engine.recheck()
    assert inbox_starts(first) == []
    assert actions(rig.agenda.item(SCOPE, TRIAGE)) == [
        ("open_triage", "Triage inbox"),
        ("suggest_inbox", "Suggest destinations"),
    ]

    rig.clock.advance(2 * 3600)  # 20:00 in JST: past quiet hours
    second = RememberingInboxHost()
    rig.inbox = second
    rig.engine = rig.build(rig.keys)
    await rig.engine.recheck()
    assert inbox_starts(second) == []
    assert actions(rig.agenda.item(SCOPE, TRIAGE)) == [
        ("open_triage", "Triage inbox"),
        ("suggest_inbox", "Suggest destinations"),
    ]

    rig.observation = inbox_observation(MORE)
    await rig.engine.recheck()
    assert inbox_starts(second) == [("start", SCOPE, TRIAGE_MORE)]
    assert actions(rig.agenda.item(SCOPE, TRIAGE_MORE)) == [("open_triage", "Triage inbox")]


# ---- a failed cycle's log line -----------------------------------------------------------------


def replaced(error: BaseException) -> DecisionLoopError:
    """The loop's own error as its readers raise it over a store failure: `from None`."""
    try:
        raise error
    except StateError, protection.ProtectionError:
        try:
            raise DecisionLoopError("unavailable") from None
        except DecisionLoopError as raised:
            return raised


SECRET = "private/payroll planning.md: SELECT * FROM decision_items"  # text no log line holds


def failing_seats(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    def facts(*args: object) -> SeatFacts:
        raise StateError("corrupt")

    monkeypatch.setattr(rig.seats, "facts", facts)


def failing_code(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    host = coding(rig, IDLE)

    def read(now: datetime) -> Any:
        raise replaced(StateError("busy"))

    monkeypatch.setattr(host, "read", read)


def failing_taken(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    coding(rig, IDLE)

    def taken(item_keys: Sequence[str]) -> frozenset[str]:
        raise replaced(protection.ProtectionError())

    monkeypatch.setattr(rig.missions, "taken", taken)


def failing_detect(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    def detect(obs: Observation, *rest: Any) -> Sequence[Candidate]:
        raise ValueError(SECRET)

    broken: Detect = detect
    rig.engine = rig.build(rig.keys, broken)


def failing_findings(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    def findings() -> Any:
        raise RuntimeError(SECRET)

    monkeypatch.setattr(rig.missions, "findings", findings)


@pytest.mark.parametrize(
    ("failing", "line"),
    [
        (failing_seats, "decision cycle failed at seats: StateError (corrupt)"),
        (failing_code, "decision cycle failed at code_read: unavailable (busy)"),
        (failing_taken, "decision cycle failed at taken: unavailable (ProtectionError)"),
        (failing_detect, "decision cycle failed at detect: ValueError"),
        (failing_findings, "decision cycle failed at second_looks: RuntimeError"),
    ],
    ids=["seat_facts", "code_read", "taken", "detect", "second_looks"],
)
async def test_a_failed_cycle_names_its_step_and_the_stores_own_code(
    rig: Rig,
    monkeypatch: pytest.MonkeyPatch,
    logged: pytest.LogCaptureFixture,
    failing: Callable[[Rig, pytest.MonkeyPatch], None],
    line: str,
) -> None:
    """Each failing read logs its own fixed step name, then the error's code or type and, in
    brackets, the store's or sealing's own failure the loop's error replaced; never the error's
    text, a ref or a path."""
    rig.observation = kanban_observation(OWNER_CARD)
    failing(rig, monkeypatch)
    with logged.at_level(logging.WARNING, logger=LOGGER):
        await rig.engine.recheck()
    messages = [record.getMessage() for record in logged.records if record.name == LOGGER]
    assert messages == [line]
    assert all(record.exc_info is None for record in logged.records)
    assert SECRET not in logged.text and "private/" not in logged.text
