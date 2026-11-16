"""SessionActor.requests() and stop_run(): the pass-through to the live driver's RunsDriver
capability, beside inventory(). No real provider; GatedDriver fakes plus the actor's own setup().
Also a background run's open request across a turn end: which bindings the actor keeps.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from tests.conversations.test_session import (
    HANG_GUARD_S,
    SID,
    GatedDriver,
    approval_runner,
    choice_for,
    gated,
    setup,
    watch,
    with_cards,
)
from viki_agent.conversations.approvals import PermissionChoice
from viki_agent.drivers.base import PermissionAnswer, RunsDriver, RunStopError
from viki_agent.events import (
    PermissionRequested,
    QuestionAsked,
    RequestAnswered,
    RequestCancelled,
    TurnDone,
    Usage,
)
from viki_agent.session import SessionActor

RAISED_AT = "2026-10-09T00:00:00.000Z"


class RunsGatedDriver(GatedDriver):
    """A GatedDriver that also reports RunsDriver: a fixed requests() row, and a stop_run that
    records its calls (in `calls`, like every other GatedDriver call) and raises for a chosen
    block id."""

    def __init__(self, *, refuse: str | None = None) -> None:
        super().__init__()
        self._refuse = refuse

    def requests(self) -> list[dict[str, object]]:
        return [{"tool_use_id": "tu-1", "run": None, "raised_at": RAISED_AT}]

    async def stop_run(self, block_id: str) -> None:
        if block_id == self._refuse:
            raise RunStopError("unknown_run")
        self.calls.append(f"stop {block_id}")


async def test_the_actor_returns_its_live_driver_s_requests_and_stops_its_run(
    tmp_path: Path,
) -> None:
    driver = RunsGatedDriver()
    driver.release.set()
    actor = gated(tmp_path, driver).create(SID)
    try:
        assert await actor.start()
        assert isinstance(driver, RunsDriver)
        assert actor.requests() == [{"tool_use_id": "tu-1", "run": None, "raised_at": RAISED_AT}]
        await actor.stop_run("tu-1")
        assert driver.calls[-1] == "stop tu-1"
    finally:
        await actor.close()


async def test_the_actor_has_no_requests_and_refuses_stop_run_before_start_and_after_close(
    tmp_path: Path,
) -> None:
    manager, _ = setup(tmp_path, [])
    actor = manager.create(SID)
    assert actor.requests() == []
    with pytest.raises(RunStopError) as before:
        await actor.stop_run("tu-1")
    assert before.value.code == "not_running"
    assert await actor.start()
    await manager.close_all()
    assert actor.requests() == []
    with pytest.raises(RunStopError) as after:
        await actor.stop_run("tu-1")
    assert after.value.code == "not_running"


async def test_the_actor_has_no_requests_and_refuses_stop_run_for_a_driver_without_the_capability(
    tmp_path: Path,
) -> None:
    driver = GatedDriver()
    driver.release.set()
    actor = gated(tmp_path, driver).create(SID)
    try:
        assert await actor.start()
        assert not isinstance(driver, RunsDriver)
        assert actor.requests() == []
        with pytest.raises(RunStopError) as excinfo:
            await actor.stop_run("tu-1")
        assert excinfo.value.code == "not_running"
    finally:
        await actor.close()


async def test_the_actor_s_stop_run_answers_while_a_start_holds_its_lock(tmp_path: Path) -> None:
    """A running turn holds the actor's lock across the whole of its start, including the wait
    for the driver to open; stop_run() must not wait behind it (mutant: take the lock, before or
    after the capability check, and the bounded wait below times out)."""
    driver = RunsGatedDriver()
    actor = gated(tmp_path, driver).create(SID)
    sending = asyncio.create_task(actor.send("hello"))
    try:
        await driver.opening.wait()  # inside send()'s lock, waiting for driver.open() to return
        await asyncio.wait_for(actor.stop_run("tu-1"), HANG_GUARD_S)
        assert driver.calls[-1] == "stop tu-1"
    finally:
        driver.release.set()
        assert await sending
        await actor.close()


def run_card(tool_use_id: str) -> PermissionRequested:
    """A card raised by a background run, not by the chat itself."""
    return PermissionRequested(
        tool_use_id=tool_use_id, kind="tool", tool="Bash", summary="ls", agent="researcher"
    )


def snapshot_cards(actor: SessionActor) -> list[str]:
    """The tool_use_ids of the cards a reconnecting client's snapshot carries."""
    events = actor.recovery(None, 0)[0]["data"]["events"]
    return [
        item["data"]["tool_use_id"]
        for item in events
        if item["type"] in {"permission.requested", "question.asked"}
    ]


async def test_a_turn_end_keeps_the_binding_of_a_request_the_driver_still_waits_on(
    tmp_path: Path,
) -> None:
    """The CLI starts and ends a turn of its own while a background run goes on.
    Run A's request is still pending in the driver, so it can still be answered after that turn
    ends; B's, which the driver no longer holds, is dropped with the turn."""
    manager, driver, emits = with_cards(tmp_path)
    actor = manager.create(SID)
    seen = watch(actor)
    assert await actor.start()
    driver.cards = ["toolu_A", "toolu_B"]
    emits[0](run_card("toolu_A"))
    a = seen[-1]["native_request"]
    emits[0](run_card("toolu_B"))
    b = seen[-1]["native_request"]
    driver.cards.remove("toolu_B")  # the CLI withdrew B; nothing reported it yet
    emits[0](TurnDone(status="success", usage=Usage()))
    assert actor.request_pending("toolu_A", a)
    assert not actor.request_pending("toolu_B", b)
    assert snapshot_cards(actor) == ["toolu_A"]  # B's card leaves the snapshot with its binding
    assert not actor.respond(
        "toolu_B", PermissionAnswer(allow=True), expected_generation=actor.generation,
        native_request=b,
    )  # fmt: skip
    assert actor.respond(
        "toolu_A", PermissionAnswer(allow=True), expected_generation=actor.generation,
        native_request=a,
    )  # fmt: skip
    assert driver.answers == [("toolu_A", PermissionAnswer(allow=True))]
    await actor.close()


async def test_an_answered_request_keeps_its_binding_for_its_answered_event_after_a_turn_end(
    tmp_path: Path,
) -> None:
    """The answer resolves the driver's future at once, but the driver's own answered event can
    reach the actor after a turn end. That event must still carry the binding: the browser keeps
    a run's card past the turn end and closes it only on an event naming the same request."""
    manager, driver, emits = with_cards(tmp_path)
    actor = manager.create(SID)
    seen = watch(actor)
    assert await actor.start()
    driver.cards = ["toolu_A"]
    emits[0](run_card("toolu_A"))
    a = seen[-1]["native_request"]
    assert actor.respond(
        "toolu_A", PermissionAnswer(allow=True), expected_generation=actor.generation,
        native_request=a,
    )  # fmt: skip
    emits[0](TurnDone(status="success", usage=Usage()))
    assert not actor.request_pending("toolu_A", a)  # answered: no second answer
    emits[0](RequestAnswered(tool_use_id="toolu_A"))
    assert seen[-1].get("native_request") == a
    await actor.close()


async def test_a_background_run_s_request_is_answered_through_the_ledger_after_a_turn_end(
    tmp_path: Path,
) -> None:
    """The recorded choice for run A's card, made after the CLI's own turn has ended, reaches the
    driver. The ledger needs both the binding and the displayed card."""
    manager, driver, emits = with_cards(tmp_path)
    runner = approval_runner(manager)
    actor = manager.create(SID)
    seen = watch(actor)
    assert await actor.start()
    driver.cards = ["toolu_A"]
    emits[0](run_card("toolu_A"))
    card = seen[-1]
    emits[0](TurnDone(status="success", usage=Usage()))
    summary, created = await runner.decide(choice_for(actor, card, PermissionChoice(True)))
    assert created and (summary.state, summary.reason) == ("delivered_locally", "")
    assert driver.answers == [("toolu_A", PermissionAnswer(allow=True))]
    await actor.close()


async def test_after_a_turn_end_the_snapshot_carries_the_cards_the_browser_keeps(
    tmp_path: Path,
) -> None:
    """A reconnecting client rebuilds its cards from the snapshot, so after a turn end it must get
    what sessionReducer's turn.done keeps: the run's card. The chat's own cards end with the turn,
    as before, even ones the driver still lists."""
    manager, driver, emits = with_cards(tmp_path)
    actor = manager.create(SID)
    seen = watch(actor)
    assert await actor.start()
    driver.cards = ["toolu_A", "toolu_M", "toolu_Q"]
    emits[0](run_card("toolu_A"))
    a = seen[-1]["native_request"]
    emits[0](PermissionRequested(tool_use_id="toolu_M", kind="tool", tool="Bash", summary="ls"))
    m = seen[-1]["native_request"]
    emits[0](QuestionAsked(tool_use_id="toolu_Q", questions=[]))
    q = seen[-1]["native_request"]
    emits[0](TurnDone(status="success", usage=Usage()))
    assert snapshot_cards(actor) == ["toolu_A"]
    assert actor.request_pending("toolu_A", a)
    assert not actor.request_pending("toolu_M", m)
    assert not actor.request_pending("toolu_Q", q)
    await actor.close()


async def test_a_turn_end_closes_a_withdrawn_run_s_card_with_its_binding(tmp_path: Path) -> None:
    """Continues the scenario above. B's card is dropped with the turn (the driver no
    longer waits on it), but the browser still shows it, so the turn end must close it with its
    binding before `turn.done` — the driver's own cancel for B is still queued behind and must
    not find anything left to reopen or duplicate once it arrives."""
    manager, driver, emits = with_cards(tmp_path)
    actor = manager.create(SID)
    seen = watch(actor)
    assert await actor.start()
    driver.cards = ["toolu_B"]
    emits[0](run_card("toolu_B"))
    b = seen[-1]["native_request"]
    driver.cards.remove("toolu_B")  # the CLI withdrew B; its own cancel is still queued
    emits[0](TurnDone(status="success", usage=Usage()))
    assert seen[-2]["type"] == "request.cancelled" and seen[-2]["native_request"] == b
    assert seen[-1]["type"] == "turn.done" and not seen[-1].get("native_request")
    assert snapshot_cards(actor) == []
    emits[0](RequestCancelled(tool_use_id="toolu_B"))  # the driver's queued cancel now arrives
    assert seen[-1]["type"] == "request.cancelled" and not seen[-1].get("native_request")
    assert snapshot_cards(actor) == []  # unbound: nothing left to reopen or duplicate
    await actor.close()


async def test_deny_pending_just_before_a_turn_end_closes_the_run_s_card_too(
    tmp_path: Path,
) -> None:
    """deny_pending() answers the driver directly and sets no answered mark, so the turn end's
    loop finds the card unwaited exactly as it does for a CLI withdrawal and must close it with
    its binding the same way."""
    manager, driver, emits = with_cards(tmp_path)
    actor = manager.create(SID)
    seen = watch(actor)
    assert await actor.start()
    driver.cards = ["toolu_A"]
    emits[0](run_card("toolu_A"))
    a = seen[-1]["native_request"]
    actor.deny_pending()
    assert driver.answers == [("toolu_A", PermissionAnswer(allow=False))]
    emits[0](TurnDone(status="success", usage=Usage()))
    assert seen[-2]["type"] == "request.cancelled" and seen[-2]["native_request"] == a
    assert seen[-1]["type"] == "turn.done" and not seen[-1].get("native_request")
    assert snapshot_cards(actor) == []
    await actor.close()
