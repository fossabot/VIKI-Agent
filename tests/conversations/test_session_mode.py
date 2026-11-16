"""session.set_mode: a live switch is asked of the CLI, whose status frame then moves the mode
and the stored preference; with no process, the stored mode is what the next spawn takes."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from tests.conversations.test_session import (
    Drivers,
    GatedDriver,
    control_error,
    control_in,
    gated,
    setup,
    watch,
)
from tests.test_server import NEW, running
from tests.test_server_v2 import (
    RecordingDriver,
    RecordingDrivers,
    connected,
    mutation,
    receipt,
    subscribe,
)
from viki_agent.drivers.claude.control import Launch
from viki_agent.events import Event
from viki_agent.session import MSG_MODE, CommandDispatchRejected
from viki_agent.state import Prefs
from viki_agent.storage import StateError

from replay import SID, Entry, init_frame, out, result_frame, user_in

# Only stops a test whose awaited event or task never comes; each normally does in ms.
HANG_GUARD_S = 5
REFUSED = {"code": "bad_request", "message": MSG_MODE, "recoverable": True}


def mode_accepted(recorded_id: str) -> Entry:
    response = {"subtype": "success", "request_id": recorded_id, "response": {}}
    return out({"type": "control_response", "response": response})


def mode_status(mode: str) -> dict[str, Any]:
    return {
        "type": "system",
        "subtype": "status",
        "session_id": SID,
        "status": None,
        "permissionMode": mode,
    }


async def test_a_live_switch_mid_turn_follows_the_clis_status_frame(tmp_path: Path) -> None:
    script = [
        *(user_in(), out(init_frame()), control_in("set_permission_mode", "rec-mode")),
        *(mode_accepted("rec-mode"), out(mode_status("acceptEdits")), out(result_frame())),
    ]
    manager, spawner = setup(tmp_path, script)
    actor = manager.create(SID)
    seen = watch(actor)
    await actor.send("hello")
    assert actor.busy  # the switch does not wait for the turn to end
    await actor.set_mode("acceptEdits")
    replayer = spawner.children[0].process.replayer
    asked = [f["request"] for f in replayer.seen if f["type"] == "control_request"]
    assert {"subtype": "set_permission_mode", "mode": "acceptEdits"} in asked
    assert (await seen.until("status"))["data"]["mode"] == "acceptEdits"
    await seen.until("turn.done")
    assert manager.app_state.prefs(SID).mode == "acceptEdits"
    await actor.close()


@pytest.mark.parametrize("reply", [[control_error("rec-mode")], []], ids=["refused", "silent"])
async def test_a_refused_or_unanswered_switch_keeps_the_current_mode(
    tmp_path: Path, reply: list[Entry]
) -> None:
    manager, _ = setup(tmp_path, [control_in("set_permission_mode", "rec-mode"), *reply])
    actor = manager.create(SID)
    seen = watch(actor)
    assert await actor.start()
    await actor.set_mode("acceptEdits")  # silent: the 0.05 s control timeout ends the wait
    assert seen.types == ["session.started", "error"] and seen[-1]["data"] == REFUSED
    assert actor.live and manager.app_state.prefs(SID).mode == "auto"
    await actor.close()


async def test_before_a_process_the_mode_is_stored_for_the_next_spawn(tmp_path: Path) -> None:
    manager, spawner = setup(tmp_path, [])
    actor = manager.create(SID)
    seen = watch(actor)
    await actor.set_mode("plan")
    assert seen.types == ["status"] and seen[0]["data"]["mode"] == "plan"
    assert manager.app_state.prefs(SID).mode == "plan"
    assert await actor.start()
    argv = spawner.children[0].argv
    assert argv[argv.index("--permission-mode") + 1] == "plan"
    await actor.close()


async def test_a_live_driver_without_a_mode_switch_refuses_and_keeps_the_mode(
    tmp_path: Path,
) -> None:
    driver = GatedDriver()
    driver.release.set()
    actor = gated(tmp_path, driver).create(SID)
    assert await actor.start()
    seen = watch(actor)
    await actor.set_mode("plan")
    assert seen.types == ["error"] and seen[0]["data"] == REFUSED
    assert driver.calls == ["open", "opened"]
    assert actor._state.prefs(SID).mode == "auto"
    await actor.close()


async def test_a_profile_session_refuses_a_mode_change(tmp_path: Path) -> None:
    drivers = Drivers()
    actor = gated(tmp_path, drivers).create(SID)
    actor._profile = True
    seen = watch(actor)
    with pytest.raises(CommandDispatchRejected, match="profile_locked"):
        await actor.set_mode("plan")
    assert seen.types == [] and drivers.made == []
    assert actor._state.prefs(SID).mode == "auto"


async def test_a_switch_whose_runtime_changed_meanwhile_is_stale(tmp_path: Path) -> None:
    class SlowModeDriver(GatedDriver):
        def __init__(self) -> None:
            super().__init__()
            self.release.set()
            self.asked, self.answer = asyncio.Event(), asyncio.Event()

        async def set_permission_mode(self, mode: str) -> None:
            self.calls.append(f"mode {mode}")
            self.asked.set()
            await self.answer.wait()

    driver = SlowModeDriver()
    actor = gated(tmp_path, driver).create(SID)
    assert await actor.start()
    seen = watch(actor)
    task = asyncio.create_task(actor.set_mode("plan", expected_generation=actor.generation))
    await asyncio.wait_for(driver.asked.wait(), HANG_GUARD_S)
    actor._token = object()  # the runtime the CLI answered for is no longer the current one
    driver.answer.set()
    with pytest.raises(CommandDispatchRejected, match="stale_generation"):
        await asyncio.wait_for(task, HANG_GUARD_S)
    assert driver.calls[-1] == "mode plan" and seen.types == []
    assert actor._state.prefs(SID).mode == "auto"
    await actor.close()


# --- the socket ------------------------------------------------------------------------------


class ModeRecordingDriver(RecordingDriver):
    async def set_permission_mode(self, mode: str) -> None:
        self.calls.append(("mode", mode))


class ModeDrivers(RecordingDrivers):
    def __call__(
        self, launch: Launch, _prefs: Prefs, emit: Callable[[Event], None]
    ) -> RecordingDriver:
        self.made.append(ModeRecordingDriver(launch, emit))
        return self.made[-1]


@pytest.mark.parametrize("mode", ["bypassPermissions", "dontAsk", "turbo"])
def test_the_server_refuses_a_mode_viki_does_not_offer(tmp_path: Path, mode: str) -> None:
    drivers = ModeDrivers()
    with running(tmp_path, make_driver=drivers) as app, connected(app.client) as (ws, _):
        target = subscribe(ws)[-1]
        command = mutation("session.set_mode", target["generation"], mode=mode)
        ws.send_json(command)
        assert receipt(ws, command)["data"] == {"status": "rejected", "reason": "invalid_mode"}
        assert len(drivers.made) == 1 and drivers.made[0].calls == []
        assert app.manager.app_state.prefs(NEW).mode == "auto"


def test_the_server_hands_a_mode_switch_to_the_live_driver(tmp_path: Path) -> None:
    drivers = ModeDrivers()
    with running(tmp_path, make_driver=drivers) as app, connected(app.client) as (ws, _):
        target = subscribe(ws)[-1]
        command = mutation("session.set_mode", target["generation"], mode="acceptEdits")
        ws.send_json(command)
        assert receipt(ws, command)["data"] == {"status": "unknown", "reason": "native_unconfirmed"}
        assert drivers.made[-1].calls == [("mode", "acceptEdits")]


async def test_a_set_mode_queued_behind_the_lock_rechecks_its_target(tmp_path: Path) -> None:
    drivers = Drivers()
    actor = gated(tmp_path, drivers).open(SID)
    seen = watch(actor)
    current = True
    await actor._lock.acquire()
    task = asyncio.create_task(
        actor.set_mode("plan", expected_generation=actor.generation, current=lambda: current)
    )
    await asyncio.sleep(0)
    current = False  # the window forgot this chat while the command waited for the lock
    actor._lock.release()
    with pytest.raises(CommandDispatchRejected, match="no_target"):
        await task
    assert seen.types == [] and drivers.made == []
    assert actor._state.prefs(SID).mode == "auto"


async def test_a_failed_save_without_a_process_shows_no_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    drivers = Drivers()
    actor = gated(tmp_path, drivers).open(SID)
    seen = watch(actor)
    real = actor._state.remember

    def failing(session_id: str, **changes: Any) -> Any:
        if "mode" in changes:
            raise StateError("unavailable")
        return real(session_id, **changes)

    monkeypatch.setattr(actor._state, "remember", failing)
    await actor.set_mode("plan")
    assert seen.types == ["error"]  # A failed save shows no change
    assert actor._state.prefs(SID).mode == "auto"
