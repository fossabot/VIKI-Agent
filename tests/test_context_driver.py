"""The context report beside the plan-limit observer: ClaudeDriver.context(),
SessionActor.context() and SessionManager.bind_limits(). Synthetic scripts only; no native
provider, transcript or vault access."""

from __future__ import annotations

import asyncio
import copy
import logging
import re
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest

from tests.conversations.test_session import HANG_GUARD_S, SID, GatedDriver, gated, setup
from tests.profiles.test_inventory import fixture_usage
from tests.test_driver_limits import make_driver
from viki_agent.drivers.base import ContextDriver
from viki_agent.drivers.claude.context_report import context_report, failed_report
from viki_agent.events import RateLimit, TurnDone

from replay import expect, init_frame, out, result_frame, user_in

STAMP = "2026-09-28T02:00:00.000Z"
SECOND_SID = "22222222-2222-4222-8222-222222222222"


async def test_a_turn_end_keeps_the_report_from_its_one_read() -> None:
    reply = fixture_usage()
    script = [
        user_in("synthetic"),
        out(init_frame()),
        out(result_frame()),
        expect(
            {
                "type": "control_request",
                "request_id": "req_ctx",
                "request": {"subtype": "get_context_usage"},
            }
        ),
        out(
            {
                "type": "control_response",
                "response": {"subtype": "success", "request_id": "req_ctx", "response": reply},
            }
        ),
    ]
    driver, spawner, events = make_driver(script)
    assert driver._context_report is None
    try:
        await driver.open()
        await driver.send("synthetic")
        await events.until(TurnDone)
    finally:
        await driver.close()
    seen = spawner.children[0].process.replayer.seen
    asked = [f["request"]["subtype"] for f in seen if f.get("type") == "control_request"]
    assert asked.count("get_context_usage") == 1
    report = driver._context_report
    assert report is not None and driver._context_snapshot is not None
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", str(report["observed_at"]))
    expected = context_report(copy.deepcopy(reply), STAMP)
    assert {**report, "observed_at": STAMP} == expected
    assert await driver.context() == report


async def test_no_report_reads_not_observed() -> None:
    driver, _, _ = make_driver([])
    assert await driver.context() == failed_report("not_observed")


@pytest.mark.parametrize(
    ("error", "reason"), [(TimeoutError(), "timeout"), (RuntimeError("gone"), "unavailable")]
)
async def test_a_failed_first_read_says_why_until_one_succeeds(
    monkeypatch: pytest.MonkeyPatch, error: Exception, reason: str
) -> None:
    driver, _, _ = make_driver([])
    replies: list[object] = [error, fixture_usage()]

    async def context_usage() -> dict[str, Any]:
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return cast(dict[str, Any], reply)

    monkeypatch.setattr(driver._control, "context_usage", context_usage)
    assert await driver._context_percent() is None
    assert await driver.context() == failed_report(reason)
    await driver._context_percent()
    succeeded = driver._context_report
    assert succeeded is not None
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", str(succeeded["observed_at"]))
    expected = context_report(copy.deepcopy(fixture_usage()), STAMP)
    assert {**succeeded, "observed_at": STAMP} == expected
    assert await driver.context() == succeeded


@pytest.mark.parametrize(
    ("error", "reason"), [(TimeoutError(), "timeout"), (RuntimeError("gone"), "unavailable")]
)
async def test_a_later_failure_keeps_the_last_report(
    monkeypatch: pytest.MonkeyPatch, error: Exception, reason: str
) -> None:
    driver, _, _ = make_driver([])
    replies: list[object] = [fixture_usage(), error]

    async def context_usage() -> dict[str, Any]:
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return cast(dict[str, Any], reply)

    monkeypatch.setattr(driver._control, "context_usage", context_usage)
    await driver._context_percent()
    succeeded = driver._context_report
    assert succeeded is not None
    await driver._context_percent()  # a failure after a success keeps the last good report
    assert driver._context_report is succeeded
    assert await driver.context() is succeeded


async def test_every_rate_limit_frame_reaches_the_observer_and_the_events_are_unchanged() -> None:
    allowed = {"status": "allowed", "rateLimitType": "five_hour", "resetsAt": 1}
    warning = {"status": "allowed_warning", "rateLimitType": "seven_day", "resetsAt": 1789128000}
    script = [
        user_in(),
        out(init_frame()),
        out({"type": "rate_limit_event", "rate_limit_info": allowed}),
        out({"type": "rate_limit_event", "rate_limit_info": warning}),
        out(result_frame()),
    ]
    driver, _, events = make_driver(script)
    observed: list[dict[str, object]] = []
    driver._config = replace(driver._config, observe_limits=observed.append)
    try:
        await driver.open()
        await driver.send("synthetic")
        await events.until(TurnDone)
    finally:
        await driver.close()
    rate_events = [event for event in events if isinstance(event, RateLimit)]
    # The mapper's rule: "allowed" is silent, "allowed_warning" emits one RateLimit.
    assert len(rate_events) == 1
    assert rate_events[0].status == "allowed_warning"
    assert rate_events[0].kind == "seven_day" and rate_events[0].resets_at == 1789128000
    # Both frames reached the observer regardless of whether they produced an event.
    assert [reading["status"] for reading in observed] == ["allowed", "allowed_warning"]


async def test_a_failing_observer_logs_one_fixed_warning_and_the_pump_goes_on(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)

    def boom(_: dict[str, object]) -> None:
        raise RuntimeError("boom")

    warning = {"status": "allowed_warning", "rateLimitType": "five_hour", "resetsAt": 1}
    script = [
        user_in(),
        out(init_frame()),
        out({"type": "rate_limit_event", "rate_limit_info": warning}),
        out(result_frame()),
    ]
    driver, _, events = make_driver(script)
    driver._config = replace(driver._config, observe_limits=boom)
    try:
        await driver.open()
        await driver.send("synthetic")
        await events.until(TurnDone)  # the pump reached the end of the turn despite the raise
    finally:
        await driver.close()
    assert [(r.levelno, r.getMessage()) for r in caplog.records] == [
        (logging.WARNING, "plan limit observer failed")
    ]
    assert [event.status for event in events if isinstance(event, RateLimit)] == ["allowed_warning"]


def test_the_claude_driver_is_a_context_driver() -> None:
    driver, _, _ = make_driver([])
    assert isinstance(driver, ContextDriver)


async def test_the_actor_returns_its_live_driver_s_context(tmp_path: Path) -> None:
    manager, _ = setup(tmp_path, [])
    actor = manager.create(SID)
    try:
        assert await actor.start()
        document = await actor.context()
    finally:
        await manager.close_all()
    assert document == failed_report("not_observed")


async def test_the_actor_has_no_context_before_start_after_close_or_without_the_capability(
    tmp_path: Path,
) -> None:
    manager, _ = setup(tmp_path, [])
    actor = manager.create(SID)
    assert await actor.context() is None  # before start
    assert await actor.start()
    await manager.close_all()
    assert await actor.context() is None  # after close

    driver = GatedDriver()
    driver.release.set()
    capped_home = tmp_path / "capped"
    capped_home.mkdir()
    capped = gated(capped_home, driver).create(SID)
    try:
        assert await capped.start()
        assert not isinstance(driver, ContextDriver)
        assert await capped.context() is None  # the driver has no context() of its own
    finally:
        await capped.close()


async def test_the_actor_s_context_answers_while_a_start_holds_its_lock(tmp_path: Path) -> None:
    """A running turn holds the actor's lock across the whole of its start, including the wait
    for the driver to open; context() must not wait behind it (mutant: take the lock, before or
    after the capability check, and the bounded wait below times out)."""

    class ContextGatedDriver(GatedDriver):
        async def context(self) -> dict[str, object]:
            return {"marker": True}

    driver = ContextGatedDriver()
    actor = gated(tmp_path, driver).create(SID)
    sending = asyncio.create_task(actor.send("hello"))
    try:
        await driver.opening.wait()  # inside send()'s lock, waiting for driver.open() to return
        document = await asyncio.wait_for(actor.context(), HANG_GUARD_S)
        assert document == {"marker": True}
    finally:
        driver.release.set()
        assert await sending
        await actor.close()


async def test_bound_limits_reach_every_later_driver(tmp_path: Path) -> None:
    info = {"status": "allowed_warning", "rateLimitType": "five_hour", "resetsAt": 1}
    script = [
        user_in(),
        out(init_frame()),
        out({"type": "rate_limit_event", "rate_limit_info": info}),
        out(result_frame()),
    ]
    manager, _ = setup(tmp_path, script)
    observed: list[dict[str, object]] = []
    manager.bind_limits(observed.append)

    first = manager.create(SID)
    second = manager.create(SECOND_SID)
    try:
        assert await first.send("hi")
        await first.wait_idle(HANG_GUARD_S)
        assert await second.send("hi")
        await second.wait_idle(HANG_GUARD_S)
    finally:
        await manager.close_all()
    assert [reading["status"] for reading in observed] == ["allowed_warning", "allowed_warning"]
