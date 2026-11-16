"""Detecting a mid-session sign-in failure.

An `assistant` frame (main thread or subagent) can carry a top-level `error`; the Agent SDK's
`SDKAssistantMessageError` enumerates it alongside billing_error, rate_limit and others.
`authentication_failed` is the one value `ClaudeDriver._watch` treats as an expired or invalid
login: the turn still ends through the ordinary `turn.done`, and only then, if that turn ended
`error`, does a single `not_logged_in` card follow — the process is never killed for it, since one
sign-in re-authenticates every session on the machine. `SessionActor` folds both that card and a
later successful turn into `Readiness`'s login state.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from tests.conversations.test_session import GatedDriver, _login, _readiness, aborted
from tests.test_driver_limits import make_driver
from viki_agent.drivers.claude.control import Launch
from viki_agent.drivers.claude.driver import AUTH_FAILED, MSG_LOGIN, DriverConfig
from viki_agent.events import Error, Event, TurnDone, Usage
from viki_agent.session import SessionManager
from viki_agent.state import AppState, Prefs

from replay import SID, init_frame, out, result_frame, user_in


def _assistant_error(error: str, *, parent_tool_use_id: str | None = None) -> dict[str, Any]:
    """An `assistant` frame carrying a top-level `error`, the shape the driver checks: main
    thread when `parent_tool_use_id` is None and a subagent's when it is set."""
    return {
        "type": "assistant",
        "error": error,
        "parent_tool_use_id": parent_tool_use_id,
        "message": {
            "id": "m1",
            "content": [{"type": "text", "text": "Login expired · Please run /login"}],
        },
    }


async def test_an_auth_failure_ends_the_turn_then_reports_not_logged_in() -> None:
    script = [
        user_in(),
        out(init_frame()),
        out(_assistant_error(AUTH_FAILED)),
        out({**result_frame(), "is_error": True}),
    ]
    driver, spawner, events = make_driver(script)
    try:
        await driver.open()
        await driver.send("synthetic")
        await events.until(Error)
        turn_done, error = events[-2], events[-1]
        assert isinstance(turn_done, TurnDone) and turn_done.status == "error"
        assert isinstance(error, Error)
        assert (error.code, error.message, error.recoverable) == ("not_logged_in", MSG_LOGIN, True)
        assert sum(isinstance(event, Error) for event in events) == 1
        assert driver.alive  # one sign-in re-authenticates every session; the process is kept
        assert len(spawner.children) == 1 and not spawner.children[0].killed
    finally:
        await driver.close()


async def test_a_subagent_auth_failure_counts_and_reports_once() -> None:
    script = [
        user_in(),
        out(init_frame()),
        out(_assistant_error(AUTH_FAILED, parent_tool_use_id="toolu_agent")),
        out(_assistant_error(AUTH_FAILED, parent_tool_use_id="toolu_agent")),
        out({**result_frame(), "is_error": True}),
    ]
    driver, spawner, events = make_driver(script)
    try:
        await driver.open()
        await driver.send("synthetic")
        await events.until(Error)
        turn_done, error = events[-2], events[-1]
        assert isinstance(turn_done, TurnDone) and turn_done.status == "error"
        assert isinstance(error, Error) and error.code == "not_logged_in"
        assert sum(isinstance(event, Error) for event in events) == 1  # a flag, never a counter
        assert driver.alive
        assert len(spawner.children) == 1 and not spawner.children[0].killed
    finally:
        await driver.close()


async def test_a_cancelled_turn_after_a_flagged_frame_adds_nothing() -> None:
    """A cancelled turn is not an error turn, so a flagged frame before one reports
    nothing — and the flag still clears on it, so a later plain error turn, with no flag left,
    reports nothing either."""
    script = [
        *(user_in(), out(init_frame()), out(_assistant_error(AUTH_FAILED)), out(aborted())),
        *(user_in(), out(init_frame()), out({**result_frame(), "is_error": True})),
    ]
    driver, _, events = make_driver(script)
    try:
        await driver.open()
        await driver.send("flagged, then stopped")
        await events.until(TurnDone, count=1)
        await driver.send("a plain error turn with no flag left to report")
        await events.until(TurnDone, count=2)
        assert [e.status for e in events if isinstance(e, TurnDone)] == ["cancelled", "error"]
        assert not any(isinstance(event, Error) for event in events)
    finally:
        await driver.close()


async def test_other_assistant_errors_and_successful_turns_add_nothing() -> None:
    script = [
        *(user_in(), out(init_frame()), out(_assistant_error("rate_limit")), out(result_frame())),
        *(user_in(), out(init_frame()), out(_assistant_error(AUTH_FAILED)), out(result_frame())),
        *(user_in(), out(init_frame()), out({**result_frame(), "is_error": True})),
    ]
    driver, _, events = make_driver(script)
    try:
        await driver.open()
        await driver.send("an unrelated error")
        await events.until(TurnDone, count=1)
        await driver.send("flagged, but the turn still succeeds")
        await events.until(TurnDone, count=2)
        await driver.send("a plain error turn with no flag left to report")
        await events.until(TurnDone, count=3)
        assert [e.status for e in events if isinstance(e, TurnDone)] == [
            "success",
            "success",
            "error",
        ]
        assert not any(isinstance(event, Error) for event in events)
        assert driver.alive
    finally:
        await driver.close()


async def test_readiness_follows_auth_failures_and_successful_turns(tmp_path: Path) -> None:
    """Login does not move only from the launch-time preflight: the `not_logged_in` card, and
    a later successful turn, update `Readiness` the same way a fresh start's billing gate
    already does."""
    readiness = _readiness()
    driver = GatedDriver()
    driver.release.set()
    captured: list[Callable[[Event], None]] = []

    def make_gated_driver(
        launch: Launch, prefs: Prefs, emit: Callable[[Event], None]
    ) -> GatedDriver:
        captured.append(emit)
        return driver

    home = tmp_path / "home"
    (home / "sessions").mkdir(parents=True, exist_ok=True)
    manager = SessionManager(
        config=DriverConfig(cli=("claude.exe",), vault=tmp_path / "vault", env={}),
        state=AppState(tmp_path / "state.json", Prefs()),
        claude_home=home,
        make_driver=make_gated_driver,
        readiness=readiness,
    )
    actor = manager.create(SID)
    assert await actor.start()
    assert _login(readiness) == "signed_in"  # the billing gate's own evidence, already in place
    emit = captured[0]

    emit(Error(code="not_logged_in", message=MSG_LOGIN, recoverable=True))
    assert _login(readiness) == "signed_out"

    emit(TurnDone(status="error", usage=Usage()))
    assert _login(readiness) == "signed_out"  # an unrelated error turn keeps it

    emit(TurnDone(status="cancelled", usage=Usage()))
    assert _login(readiness) == "signed_out"  # a cancelled turn keeps it too

    emit(TurnDone(status="success", usage=Usage()))
    assert _login(readiness) == "signed_in"

    await actor.close()
