"""VIKI's "once per item, state and work" rule for its own chats across an app restart.

A restart is what the server's lifespan does on a new launch: a new mission service and engine
over the same store, the service reconciling what the old launch left open. However VIKI's first
chat for an item ended (the user stopped it, it finished, or the app closed while it ran), the
new launch starts no second chat for the same item, state and work. A changed state starts the
work again, unless the user stopped the first chat, and the user's own start of the same item
and state stays allowed.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path

import pytest

from tests.decision_loop.test_engine import (
    HANG_GUARD_S,
    KEY,
    PREPARE,
    SCOPE,
    TODAY,
    Rig,
    at_wall,
    key_of,
    observation,
    plan_reading,
    task,
)
from viki_agent.decision_loop.missions import MissionService
from viki_agent.events import TurnDone, Usage

ENDINGS = {
    "owner": ("stopped", "owner"),
    "done": ("done", ""),
    "app_closed": ("unknown", "app_closed"),
}


@pytest.fixture
async def rig(tmp_path: Path) -> AsyncIterator[Rig]:
    rig = Rig(tmp_path)
    # This launch's plan reading, well under the brake: without one VIKI starts nothing itself.
    rig.reading = plan_reading(use=0.1, reset=at_wall(3600))
    yield rig
    await asyncio.wait_for(rig.engine.stop(), HANG_GUARD_S)


async def restart(rig: Rig) -> None:
    """A new launch over the same store: the old engine stops, a new mission service and engine
    are built, and the service ends what the old launch left open."""
    await rig.engine.stop()
    rig.missions = MissionService(rig.state, rig.host, rig.settings, wall=rig.clock.wall)
    rig.engine = rig.build(rig.keys)
    await rig.missions.reconcile()


def creates(rig: Rig) -> list[str]:
    """The chat of every create the host was asked for, in order."""
    return [call[1] for call in rig.host.calls if call[0] == "create"]


async def first_chat(rig: Rig, ending: str) -> str:
    """VIKI starts a plan chat for Task a, which then ends as `ending` says ("app_closed" leaves
    it running for the restart to cut off); answer its chat."""
    rig.keys.save(KEY)
    rig.model.answers = dict(PREPARE)
    rig.observation = observation(task("Task a"))
    await rig.engine.recheck()
    (first,) = rig.missions.running()
    if ending == "owner":
        await rig.missions.stop(first.mission_id)
    elif ending == "done":
        rig.host.actors[first.session_id].emit(TurnDone(status="success", usage=Usage()))
    await rig.engine.recheck()  # the old launch's last cycle
    return first.session_id


@pytest.mark.parametrize("ending", list(ENDINGS))
async def test_a_restart_starts_no_second_chat_for_the_same_item_state_and_work(
    rig: Rig, ending: str
) -> None:
    first = await first_chat(rig, ending)
    await restart(rig)
    await rig.engine.recheck()
    await rig.engine.recheck()
    assert creates(rig) == [first]
    item = rig.agenda.item(SCOPE, key_of("Task a"))
    assert item is not None and item.state_hash is not None
    (row,) = rig.rows("decision_missions")
    assert (row["state"], row["reason"], row["state_hash"]) == (*ENDINGS[ending], item.state_hash)

    # A new state (the task moved to tomorrow) is new work: a second chat, unless the user
    # stopped the first, which holds the item against VIKI's own starts whatever its state.
    rig.observation = observation(task("Task a", TODAY + timedelta(days=1)))
    await rig.engine.recheck()
    if ending == "owner":
        assert rig.missions.running() == [] and creates(rig) == [first]
        return
    (second,) = rig.missions.running()
    assert second.item_key == key_of("Task a")
    assert creates(rig) == [first, second.session_id]


@pytest.mark.parametrize("ending", list(ENDINGS))
async def test_the_owners_own_start_of_the_same_item_and_state_is_allowed(
    rig: Rig, ending: str
) -> None:
    first = await first_chat(rig, ending)
    await restart(rig)
    await rig.engine.recheck()
    assert creates(rig) == [first]

    # As the page's start does: the item's current record, started by the user.
    item = rig.agenda.item(SCOPE, key_of("Task a"))
    assert item is not None and item.state_hash is not None
    own = await rig.missions.start(SCOPE, item, "prepare", by_owner=True)
    assert own.state == "running" and creates(rig) == [first, own.session_id]
    hashes = {row["session_id"]: row["state_hash"] for row in rig.rows("decision_missions")}
    assert hashes == {first: item.state_hash, own.session_id: item.state_hash}
