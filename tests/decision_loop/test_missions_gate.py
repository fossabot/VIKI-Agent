"""VIKI's chats (missions), beside test_missions.py: a quiet end frees its chat's process,
outside the lock; VIKI's own starts take one of the two places at most; a second look's
brief names no note under a Never send folder; a failed second look or project plan
reports its failure; an open plan holds VIKI's next one after its item is gone; and the
user's Stop holds an item against VIKI's own restarts. Every chat is a
`FakeHost`/`FakeActor`; no session, process or network starts."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from tests.decision_loop.fakes import WALL, FakeActor, FakeHost
from tests.decision_loop.test_missions import (
    HANG_GUARD_S,
    LOGGER,
    NEW_STATE_HASH,
    NOTE,
    OTHER,
    SCOPE,
    STATE_HASH,
    TASKS_NOTE,
    TITLE,
    Rig,
    bullets,
    done,
    flaw_plan,
    item,
    messages,
    plan_card,
    planned,
    project_plan,
    refused,
    rows,
    second,
    second_look,
    stored,
    tool_card,
    uid,
)
from viki_agent.decision_loop.agenda import CLOSED_KEEP_DAYS, DecisionAgenda
from viki_agent.decision_loop.contracts import MISSION_MINUTES, ItemRecord, MissionRecord
from viki_agent.events import Error, Event, RequestAnswered, SessionClosed, Status
from viki_agent.state import AppState, Prefs
from viki_agent.storage import SessionStore


@pytest.fixture
def store(tmp_path: Path) -> SessionStore:
    return AppState(tmp_path / "state.json", Prefs()).store


@pytest.fixture
def rig(store: SessionStore) -> Rig:
    return Rig(store)


def closes(rig: Rig) -> list[str]:
    """The chat of every close the host was asked for, in order."""
    return [call[1] for call in rig.host.calls if call[0] == "close"]


class SlowCloseHost(FakeHost):
    """Its `close` records the call, says so, then holds until `release` is set."""

    def __init__(self) -> None:
        super().__init__()
        self.closing, self.release = asyncio.Event(), asyncio.Event()

    async def close(self, session_id: str) -> None:
        self.calls.append(("close", session_id))
        self.closing.set()
        await self.release.wait()
        self.actors.pop(session_id, None)


# ---- a quiet end frees its chat's process --------------------------------------------------------


@pytest.mark.parametrize(
    ("events", "ending"),
    [
        ([done()], ("done", "")),
        (
            [done("error"), Error(code="crashed", message="x", recoverable=True)],
            ("failed", "crashed"),
        ),
    ],
)
async def test_a_turn_that_ends_closes_its_chat_at_that_poll(
    rig: Rig, events: list[Event], ending: tuple[str, str]
) -> None:
    mission = await rig.start()
    actor = rig.actor(mission)
    for event in events:
        actor.emit(event)

    (ended,) = await rig.service.poll(WALL)
    assert (ended.state, ended.reason) == ending
    assert closes(rig) == [mission.session_id] and rig.host.get(mission.session_id) is None
    assert actor.subscribers == []  # unsubscribed first, so it never reads its own close
    assert await rig.service.poll(WALL) == [] and closes(rig) == [mission.session_id]


async def test_the_deadline_interrupts_the_turn_then_closes_its_chat(rig: Rig) -> None:
    mission = await rig.start()
    actor = rig.actor(mission)
    rig.clock.advance(MISSION_MINUTES * 60)

    (ended,) = await rig.service.poll(rig.clock.wall())
    assert (ended.state, ended.reason) == ("stopped", "deadline")
    # Its turn still runs as the close begins: the close interrupts it as it ends the process.
    assert actor.busy and actor.interrupts == 1
    assert closes(rig) == [mission.session_id]


async def test_a_second_look_or_plan_that_finds_nothing_closes_its_chat(rig: Rig) -> None:
    # A second look that found nothing reports it as one quiet line; a project plan stays quiet.
    for start, reported in ((second, ["nothing"]), (planned, [])):
        mission = await start(rig)
        rig.actor(mission).emit(done())
        assert [ended.reason for ended in await rig.service.poll(WALL)] == reported
        assert closes(rig)[-1] == mission.session_id
    assert len(closes(rig)) == 2


@pytest.mark.parametrize(
    ("events", "ending"),
    [
        # The user answered a card: the chat is theirs, as their own chats are.
        ([tool_card(), RequestAnswered(tool_use_id="tool-1"), done()], ("done", "")),
        # A turn interrupted in the chat itself is the user's doing, as Stop is.
        ([done("cancelled")], ("failed", "cancelled")),
        ([SessionClosed(reason="requested")], ("unknown", "closed")),
        ([SessionClosed(reason="browser_gone")], ("unknown", "app_closed")),
    ],
)
async def test_a_chat_the_owner_took_over_interrupted_or_closed_is_not_closed(
    rig: Rig, events: list[Event], ending: tuple[str, str]
) -> None:
    mission = await rig.start()
    for event in events:
        rig.actor(mission).emit(event)

    (ended,) = await rig.service.poll(WALL)
    assert (ended.state, ended.reason) == ending
    assert closes(rig) == []


async def test_an_answered_second_look_and_a_waiting_plan_keep_their_chats(rig: Rig) -> None:
    looked = await second(rig)
    for event in (flaw_plan(), RequestAnswered(tool_use_id="plan-1"), Status(mode="auto"), done()):
        rig.actor(looked).emit(event)
    assert await rig.service.poll(WALL) == []
    assert [row["reason"] for row in rows(rig.store)] == ["accepted:flaw"]

    waiting = await rig.start(2)
    rig.actor(waiting).emit(plan_card())
    rig.clock.advance(3 * MISSION_MINUTES * 60)
    assert [m.reason for m in await rig.service.poll(rig.clock.wall())] == ["ready"]
    assert closes(rig) == [] and rig.service.running() == [waiting]


async def test_a_chat_someone_sends_to_after_its_end_stays_open(rig: Rig) -> None:
    mission = await rig.start()
    actor = rig.actor(mission)
    actor.emit(done())
    actor.busy = True  # the user sent a follow-up before VIKI's next check

    (ended,) = await rig.service.poll(WALL)
    assert (ended.state, ended.reason) == ("done", "")
    assert closes(rig) == [] and rig.host.get(mission.session_id) is actor


async def test_stop_keeps_its_chats_and_leaves_an_ended_one_to_the_next_poll(rig: Rig) -> None:
    """Stop and Stop all never close a chat, and never wait on a close: a mission whose turn had
    already ended keeps that ending, and the next poll closes its chat, unless by then the app
    no longer holds that chat as the one VIKI watched (the user closed or opened it again)."""
    kept = await rig.start(1)
    rig.actor(kept).emit(done())
    rig.clock.advance(1)  # Stop all settles the oldest first
    reopened = await rig.start(2)
    rig.actor(reopened).emit(done())
    assert [(m.state, m.reason) for m in await rig.service.stop_all()] == [("done", "")] * 2
    stopped = await rig.start(3)
    rig.clock.advance(60)
    assert (await rig.service.stop(stopped.mission_id)).reason == "owner"
    assert closes(rig) == []

    rig.host.actors[reopened.session_id] = FakeActor(reopened.session_id)  # opened again
    reports = await rig.service.poll(rig.clock.wall())
    assert [m.mission_id for m in reports] == [m.mission_id for m in (kept, reopened, stopped)]
    assert closes(rig) == [kept.session_id]
    assert rig.host.get(stopped.session_id) is not None


async def test_stop_and_a_start_answer_while_a_close_is_pending(store: SessionStore) -> None:
    host = SlowCloseHost()
    rig = Rig(store, host=host)
    ended = await rig.start(1)
    other = await rig.start(2, work="run")
    rig.actor(ended).emit(done())
    polling = asyncio.create_task(rig.service.poll(WALL))
    async with asyncio.timeout(HANG_GUARD_S):
        await host.closing.wait()

    # The close waits with the lock released: Stop and a start answer meanwhile.
    async with asyncio.timeout(HANG_GUARD_S):
        stopped = await rig.service.stop(other.mission_id)
        started = await rig.start(3)
    assert (stopped.state, stopped.reason, started.state) == ("stopped", "owner", "running")
    assert not polling.done()
    host.release.set()
    async with asyncio.timeout(HANG_GUARD_S):
        reports = await polling
    assert [(m.mission_id, m.state) for m in reports] == [
        (ended.mission_id, "done"),
        (other.mission_id, "stopped"),
    ]
    assert closes(rig) == [ended.session_id]


async def test_a_close_that_fails_or_hangs_is_logged_and_leaves_the_chat(
    store: SessionStore, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    secret = f"{TITLE} {NOTE}"

    class StuckHost(FakeHost):
        """Its first close fails; every later one never returns."""

        failed = False

        async def close(self, session_id: str) -> None:
            self.calls.append(("close", session_id))
            if not self.failed:
                self.failed = True
                raise RuntimeError(secret)
            await asyncio.Event().wait()

    monkeypatch.setattr("viki_agent.decision_loop.missions.CLOSE_S", 0.01)
    rig = Rig(store, host=StuckHost())
    for n in (1, 2):
        mission = await rig.start(n)
        rig.actor(mission).emit(done())
        (ended,) = await rig.service.poll(WALL)
        assert ended.mission_id == mission.mission_id
        assert rig.host.get(mission.session_id) is not None  # the chat stays as it was
    assert len(closes(rig)) == 2
    assert messages(caplog) == [
        "decision mission chat not closed: RuntimeError",
        "decision mission chat not closed: TimeoutError",
    ]
    ours = [record for record in caplog.records if record.name == LOGGER]
    assert all(record.exc_info is None and secret not in record.getMessage() for record in ours)


# ---- VIKI's own chats take one place at most -----------------------------------------------------


async def test_while_the_owners_chat_is_open_viki_starts_none_of_its_own(rig: Rig) -> None:
    await rig.start(1)
    for record in (item(2), second_look(), project_plan(3)):
        await refused("busy", rig.service.start(SCOPE, record, "prepare", by_owner=False))
    assert (await rig.start(4, work="run")).state == "running"  # the user's own second place
    await refused("busy", rig.start(5))

    assert len(await rig.service.stop_all()) == 2 and rig.service.running() == []
    assert (await rig.start(2, by_owner=False)).state == "running"  # a busy refusal stored nothing
    await refused("busy", rig.service.start(SCOPE, second_look(), "prepare", by_owner=False))


# ---- a second look names no note Never send holds back ------------------------------------------


async def test_a_second_look_names_no_note_under_a_never_send_folder(store: SessionStore) -> None:
    rig = Rig(store, {"decision.never_send": '["private/"]'})
    refs = (NOTE, "private/diary.md", "Private/plans.md", "notes/private/beds.md")
    for by_owner in (False, True):
        record = second_look(uid(1 if by_owner else 2), refs=refs)
        mission = await rig.service.start(SCOPE, record, "prepare", by_owner=by_owner)
        (sent,) = rig.actor(mission).sent
        assert sent == mission.brief
        assert bullets(sent) == [NOTE, "notes/private/beds.md"]  # by path segment, any case
        assert "diary" not in sent and "plans.md" not in sent
        await rig.service.stop(mission.mission_id)


@pytest.mark.parametrize("by_owner", [False, True])
async def test_a_second_look_whose_every_note_never_send_holds_is_refused(
    store: SessionStore, by_owner: bool
) -> None:
    rig = Rig(store, {"decision.never_send": '["private/", "#health"]'})
    record = second_look(refs=("private/diary.md", "PRIVATE/b.md"))
    await refused("no_work", rig.service.start(SCOPE, record, "prepare", by_owner=by_owner))
    assert rig.host.calls == [] and rows(store) == []


async def test_never_send_is_read_again_at_each_start(store: SessionStore) -> None:
    rig = Rig(store)
    record = second_look(refs=(NOTE,))
    first = await rig.service.start(SCOPE, record, "prepare", by_owner=True)
    await rig.service.stop(first.mission_id)
    rig.values["decision.never_send"] = '["notes/"]'
    again = second_look(uid(2), refs=(NOTE,))
    await refused("no_work", rig.service.start(SCOPE, again, "prepare", by_owner=True))
    assert len(rows(store)) == 1


async def test_a_never_send_tag_does_not_hold_a_second_look_back(store: SessionStore) -> None:
    """The item holds no note's tags, so a tag rule cannot be judged here: the notes are named,
    as ADR-069 records for the tags VIKI does not see."""
    rig = Rig(store, {"decision.never_send": '["#private"]'})
    mission = await rig.service.start(SCOPE, second_look(), "prepare", by_owner=False)
    assert bullets(mission.brief) == [NOTE]


async def test_an_unusable_never_send_list_holds_every_second_look_and_plan(
    store: SessionStore,
) -> None:
    rig = Rig(store, {"decision.never_send": "not a list"})
    assert rig.read().never_send_invalid
    for record in (second_look(), project_plan()):
        for by_owner in (False, True):
            await refused("no_work", rig.service.start(SCOPE, record, "prepare", by_owner=by_owner))
    assert rig.host.calls == [] and rows(store) == []


async def test_a_project_plan_whose_note_a_never_send_folder_holds_is_refused(
    store: SessionStore,
) -> None:
    rig = Rig(store, {"decision.never_send": '["10-projects/garden/"]'})
    for record in (project_plan(), project_plan(2, candidate="overdue_tasks", ref=TASKS_NOTE)):
        await refused("no_work", rig.service.start(SCOPE, record, "prepare", by_owner=True))
    assert rig.host.calls == [] and rows(store) == []


# ---- a failed second look reports its failure ---------------------------------------------------


async def test_a_second_look_whose_turn_fails_reports_it(rig: Rig) -> None:
    mission = await second(rig)
    rig.actor(mission).emit(done("error"))
    rig.actor(mission).emit(Error(code="crashed", message="x", recoverable=True))

    (failed,) = await rig.service.poll(WALL)
    assert (failed.mission_id, failed.state, failed.reason) == (
        mission.mission_id,
        "failed",
        "crashed",
    )
    assert rig.service.findings() == [] and closes(rig) == [mission.session_id]


async def test_a_second_look_whose_brief_is_not_sent_reports_it(rig: Rig) -> None:
    rig.host.send_result = False
    await refused("not_sent", second(rig))

    (failed,) = await rig.service.poll(WALL)
    assert (failed.state, failed.reason) == ("failed", "not_sent")
    assert closes(rig) == [failed.session_id]


# ---- an open plan keeps its place without its item -----------------------------------------------


def overdue(n: int) -> ItemRecord:
    return project_plan(n, candidate="overdue_tasks", ref=TASKS_NOTE)


@pytest.mark.parametrize("gone", ["pruned", "deleted"])
async def test_an_open_plan_holds_vikis_next_plan_after_its_item_is_gone(
    store: SessionStore, gone: str
) -> None:
    """Its overdue set grew, so the next offer has another key: no read of the first plan's
    item, which the prune or Delete all removes while its plan card waits, decides the refusal."""
    rig = Rig(store, {"decision.limit.project_plan": 9})
    agenda = DecisionAgenda(store)
    first = await rig.service.start(
        SCOPE, stored(store, SCOPE, overdue(1)), "prepare", by_owner=False
    )
    rig.actor(first).emit(plan_card())
    if gone == "pruned":
        agenda.close_missing(SCOPE, [], WALL)  # a started offer drops out of the next cycle
        agenda.prune(SCOPE, WALL + timedelta(days=CLOSED_KEEP_DAYS + 1))
    else:
        agenda.delete_all(SCOPE)
    assert agenda.item(SCOPE, overdue(1).item_key) is None

    grown = stored(store, SCOPE, overdue(2))
    await refused("busy", rig.service.start(SCOPE, grown, "prepare", by_owner=False))
    assert rig.service.running() == [first]
    assert (await rig.service.start(SCOPE, grown, "prepare", by_owner=True)).state == "running"


# ---- the user's Stop holds an item against VIKI's own restarts ---------------------------------


async def test_the_owners_stop_holds_the_item_against_vikis_own_restarts(
    store: SessionStore,
) -> None:
    rig = Rig(store, {"decision.grant.run": "allowed", "decision.limit.drafts": 9})
    record = replace(item(1), state_hash=STATE_HASH)
    first = await rig.service.start(SCOPE, record, "prepare", by_owner=False)
    await rig.service.stop(first.mission_id)

    # The task falls due (a new state hash), or VIKI would run it: neither starts again.
    moved = replace(record, state_hash=NEW_STATE_HASH)
    for again, work in ((moved, "prepare"), (moved, "run"), (record, "run")):
        await refused("tried", rig.service.start(SCOPE, again, work, by_owner=False))
    # The user's own start still may, and does not lift the hold.
    owners = await rig.service.start(SCOPE, moved, "prepare", by_owner=True)
    rig.actor(owners).emit(done())
    await rig.service.poll(WALL)
    later = replace(record, state_hash="c" * 64)
    await refused("tried", rig.service.start(SCOPE, later, "prepare", by_owner=False))
    # Stop all holds its items the same way.
    other = await rig.service.start(SCOPE, item(2), "prepare", by_owner=False)
    await rig.service.stop_all()
    moved_other = replace(item(2), state_hash=NEW_STATE_HASH)
    await refused("tried", rig.service.start(SCOPE, moved_other, "prepare", by_owner=False))
    assert {row["mission_id"]: row["reason"] for row in rows(store)}[other.mission_id] == "owner"


async def test_a_deadline_does_not_hold_the_item_past_its_state(rig: Rig) -> None:
    record = replace(item(1), state_hash=STATE_HASH)
    await rig.service.start(SCOPE, record, "prepare", by_owner=False)
    rig.clock.advance(MISSION_MINUTES * 60)
    (ended,) = await rig.service.poll(rig.clock.wall())
    assert (ended.state, ended.reason) == ("stopped", "deadline")

    await refused("tried", rig.service.start(SCOPE, record, "prepare", by_owner=False))
    moved = replace(record, state_hash=NEW_STATE_HASH)
    started: MissionRecord = await rig.service.start(SCOPE, moved, "prepare", by_owner=False)
    assert started.state == "running"
    # Another scope's item under the same key is another item.
    await rig.service.stop(started.mission_id)
    assert (await rig.service.start(OTHER, record, "prepare", by_owner=False)).state == "running"
