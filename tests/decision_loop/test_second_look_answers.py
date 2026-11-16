"""A second look's outcome read from a real `SessionActor` stream: the user's plan answer is not on
the stream, but its effect is (the mode leaves plan on Approve), so these drive a `SessionManager`
over `replay.FakeSpawner` as `tests/conversations/test_session.py::setup` does and let the user
answer the plan card through the actor."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from tests.conversations.test_session import Seen
from tests.conversations.test_session import fixture as recorded
from tests.conversations.test_session import setup as manager_over
from tests.decision_loop.fakes import WALL, FakeClock, settings_values
from tests.decision_loop.test_missions import SCOPE, second_look
from viki_agent.decision_loop.contracts import DecisionSettings, parse_settings
from viki_agent.decision_loop.missions import MissionService
from viki_agent.drivers.base import PermissionAnswer
from viki_agent.session import SessionActor, SessionManager

from replay import (
    SID,
    Entry,
    init_frame,
    out,
    permission_request,
    permission_response_in,
    result_frame,
    user_in,
)

HANG_GUARD_S = 5  # bounds a wait that settles in milliseconds; it never fires in a healthy run


class TappedHost:
    """The `SessionManager` as a mission reaches it, plus a `Seen` on every chat it creates,
    subscribed before the brief goes out, so the test can wait on the plan card."""

    def __init__(self, manager: SessionManager) -> None:
        self.manager = manager
        self.seen = Seen()

    @property
    def app_state(self) -> Any:
        return self.manager.app_state

    def create(self, session_id: str) -> SessionActor:
        actor = self.manager.create(session_id)
        actor.subscribe(self.seen)
        return actor

    def get(self, session_id: str) -> SessionActor | None:
        return self.manager.get(session_id)

    async def close(self, session_id: str) -> None:
        await self.manager.close(session_id)


def settings() -> DecisionSettings:
    return parse_settings(settings_values(**{"decision.enabled": True}))


async def ending_after_the_owner_answers(
    tmp_path: Path, script: list[Entry], answer: PermissionAnswer
) -> tuple[str, str]:
    """VIKI's second look over `script`: the user answers its plan card with `answer`; the
    stored ending once its turn is over."""
    manager, _ = manager_over(tmp_path, script)
    host = TappedHost(manager)
    store = manager.app_state.store
    service = MissionService(store, host, settings, wall=FakeClock(WALL).wall)  # type: ignore[arg-type]
    mission = await service.start(SCOPE, second_look(), "prepare", by_owner=False)
    actor = manager.get(mission.session_id)
    assert actor is not None
    card = (await host.seen.until("permission.requested"))["data"]
    assert card["kind"] == "plan"
    assert [finding.session_id for finding in service.findings()] == [mission.session_id]
    assert actor.respond(card["tool_use_id"], answer)
    await host.seen.until("turn.done")

    assert await service.poll(WALL) == []  # no report names a second look
    with store._connection() as connection:
        (row,) = connection.execute("SELECT state,reason FROM decision_missions").fetchall()
    await actor.close()
    return row[0], row[1]


def plan_script(plan: str) -> list[Entry]:
    """One turn that asks to leave plan mode with `plan`; the answer is the next frame the client
    writes, and the turn then ends with no status frame."""
    request = permission_request("rec-plan", "ExitPlanMode", "toolu_plan")
    request["request"]["input"] = {"plan": plan}
    return [
        user_in(),
        out(init_frame(SID)),
        out(request),
        permission_response_in("rec-plan"),
        out(result_frame(SID)),
    ]


async def test_the_recorded_plan_approval_reads_as_accepted(tmp_path: Path) -> None:
    """The recorded capture reports permissionMode "auto" right after the user's allow."""
    ending = await asyncio.wait_for(
        ending_after_the_owner_answers(
            tmp_path, recorded("plan-mode-exit"), PermissionAnswer(allow=True)
        ),
        HANG_GUARD_S,
    )
    assert ending == ("done", "accepted:other")


async def test_a_kept_plan_reads_as_rejected(tmp_path: Path) -> None:
    script = plan_script("Kind: missing piece\nThe watering schedule has no owner.")
    ending = await asyncio.wait_for(
        ending_after_the_owner_answers(tmp_path, script, PermissionAnswer(allow=False)),
        HANG_GUARD_S,
    )
    assert ending == ("done", "rejected:missing")
