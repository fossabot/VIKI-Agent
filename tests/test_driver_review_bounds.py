"""Synthetic regressions for turn-owned stops and complete, bounded native review."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny, ToolPermissionContext

from tests.conversations.test_session import control_error, interrupt_in, setup, watch
from tests.test_driver_limits import assert_terminal, feed, make_driver
from tests.test_mapper import _stream, assistant, text, tool_use
from viki_agent.drivers.base import PermissionAnswer
from viki_agent.drivers.claude import mapper as mapper_module
from viki_agent.drivers.claude.driver import ClaudeDriver
from viki_agent.drivers.claude.mapper import CARD_REST_LIMIT, Mapper
from viki_agent.events import (
    Error,
    SubagentDone,
    TextDelta,
    TextDone,
    ToolDone,
    ToolStarted,
    TurnDone,
)

from replay import SID, init_frame, out, result_frame, text_delta, user_in


@pytest.mark.parametrize("late_error", [False, True], ids=["timeout", "late-error"])
async def test_a_midturn_stop_cannot_kill_the_next_turn(tmp_path: Path, late_error: bool) -> None:
    manager, spawner = setup(
        tmp_path,
        [
            user_in("first"),
            out(init_frame()),
            out(text_delta("first running")),
            interrupt_in(),
            out(result_frame()),
            user_in("second"),
            out(init_frame()),
            out(text_delta("second running")),
        ],
    )
    manager._config = replace(
        manager._config, control_timeout_s=0.02, grace_s=0.01, poll_s=0.01, stop_flush_s=0.01
    )
    actor = manager.create(SID)
    seen = watch(actor)
    stopping: asyncio.Task[None] | None = None
    try:
        assert await actor.send("first")
        await seen.until("text.delta", timeout=0.05)
        stopping = asyncio.create_task(actor.interrupt())
        assert (await seen.until("turn.done", timeout=0.05))["data"]["status"] == "success"
        request = next(
            frame
            for frame in spawner.children[0].process.replayer.seen
            if frame.get("request", {}).get("subtype") == "interrupt"
        )
        assert await actor.send("second")
        await seen.until("text.delta", count=2, timeout=0.05)
        if late_error:
            feed(
                spawner,
                {
                    "type": "control_response",
                    "response": {
                        "subtype": "error",
                        "request_id": request["request_id"],
                        "error": "synthetic late interrupt refusal",
                    },
                },
            )
        async with asyncio.timeout(0.05):
            await stopping  # completing a turn must not cancel its WebSocket dispatch caller
        assert not stopping.cancelled()
        elapsed = asyncio.Event()
        timer = asyncio.get_running_loop().call_later(0.03, elapsed.set)
        try:
            async with asyncio.timeout(0.05):
                await elapsed.wait()
        finally:
            timer.cancel()
        assert actor.busy and actor.live and not spawner.children[0].killed
        assert "error" not in seen.types
        feed(spawner, result_frame())
        assert (await seen.until("turn.done", count=2, timeout=0.05))["data"]["status"] == "success"
    finally:
        await manager.close_all()
        if stopping is not None:
            await asyncio.gather(stopping, return_exceptions=True)


async def test_a_refused_stop_still_kills_its_own_running_turn() -> None:
    driver, spawner, events = make_driver(
        [
            user_in(),
            out(init_frame()),
            out(text_delta("running")),
            interrupt_in(),
            control_error("rec-int"),
        ]
    )
    try:
        await driver.open()
        await driver.send("synthetic")
        await events.until(TextDelta)
        await driver.interrupt()
        await events.until(Error)
        assert spawner.children[0].killed
        assert [event.code for event in events if isinstance(event, Error)] == ["forced_stop"]
    finally:
        await driver.close()


async def test_unexpected_stop_failure_does_not_expose_exception_text(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    driver, _, events = make_driver(
        [
            user_in(),
            out(init_frame()),
            out(text_delta("running")),
            interrupt_in(),
            control_error("rec-int"),
        ]
    )

    def failed_kill() -> None:
        raise RuntimeError("synthetic private provider text")

    try:
        await driver.open()
        await driver.send("synthetic")
        await events.until(TextDelta)
        monkeypatch.setattr(driver._transport, "kill", failed_kill)
        await driver.interrupt()
        assert "turn stop failed" in caplog.text
        assert "synthetic private provider text" not in caplog.text
    finally:
        await driver.close()


@pytest.mark.parametrize("tool", ["Bash", "PowerShell"])
@pytest.mark.parametrize("head", [False, True], ids=["argument", "head"])
@pytest.mark.parametrize("extra", [0, 1], ids=["at-limit", "over-limit"])
async def test_a_shell_permission_is_complete_or_denied(
    tmp_path: Path, tool: str, head: bool, extra: int
) -> None:
    prefix = "" if head else "echo "
    command = prefix + "x" * (CARD_REST_LIMIT - len(prefix) + extra)
    await _check_shell_permission(tmp_path, tool, command, denied=bool(extra))


@pytest.mark.parametrize("tool", ["Bash", "PowerShell"])
async def test_a_shell_permission_preserves_whitespace_quotes_and_literal_ellipsis(
    tmp_path: Path, tool: str
) -> None:
    await _check_shell_permission(
        tmp_path, tool, "  echo 'a  b'\n\techo literal\u2026  ", denied=False
    )


@pytest.mark.parametrize("tool", ["Bash", "PowerShell"])
async def test_a_shell_permission_cannot_allow_a_hidden_suffix(tmp_path: Path, tool: str) -> None:
    command = "echo " + "x" * 2100 + "; echo UNSEEN_SUFFIX"
    await _check_shell_permission(tmp_path, tool, command, denied=True)


async def _check_shell_permission(tmp_path: Path, tool: str, command: str, *, denied: bool) -> None:
    manager, _ = setup(tmp_path, [])
    actor = manager.create(SID)
    seen = watch(actor)
    try:
        assert await actor.start()
        driver = actor._driver
        assert isinstance(driver, ClaudeDriver)

        def allow_displayed(envelope: dict[str, Any]) -> None:
            if envelope["type"] == "permission.requested":
                assert actor.respond(
                    "review",
                    PermissionAnswer(True),
                    expected_generation=actor.generation,
                    native_request=envelope["native_request"],
                )

        actor.subscribe(allow_displayed)
        async with asyncio.timeout(0.05):
            result = await driver._can_use_tool(
                tool, {"command": command}, ToolPermissionContext(tool_use_id="review")
            )
        cards = [event for event in seen if event["type"] == "permission.requested"]
        if denied:
            assert isinstance(result, PermissionResultDeny)
            assert cards == [] and not actor._native_requests and not driver.pending()
            assert (await seen.until("error", timeout=0.05))["data"]["code"] == "bad_request"
            assert not actor.respond("review", PermissionAnswer(True))
        else:
            assert isinstance(result, PermissionResultAllow) and result.updated_input is None
            assert len(cards) == 1 and cards[0]["data"]["summary"] == command
    finally:
        await manager.close_all()


def _start(mapper: Mapper, message: str) -> None:
    mapper.frame(_stream({"type": "message_start", "message": {"id": message}}))


def _delta(mapper: Mapper, value: str) -> list[Any]:
    return mapper.frame(
        _stream(
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": value},
            }
        )
    )


def test_the_pending_routing_budget_is_total_and_applies_without_a_text_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mapper_module, "MAX_ROUTING_CHARS", 128, raising=False)
    mapper = Mapper(vault="synthetic", mode="auto", text_limit=None)
    for message in ("first", "second"):
        _start(mapper, message)
        assert _delta(mapper, "\u25b8" + "x" * 63) == []
    assert sum(map(len, mapper._held.values())) == 128
    with pytest.raises(BufferError):
        _delta(mapper, "x")
    assert sum(map(len, mapper._held.values())) <= 128
    mapper.frame(assistant("first", text("\u25b8" + "x" * 63)))
    _start(mapper, "third")
    assert _delta(mapper, "\u25b8" + "x" * 63) == []  # the finished block released its share
    assert sum(map(len, mapper._held.values())) == 128


@pytest.mark.parametrize("valid_route", [False, True])
def test_long_routing_prefixes_keep_the_exact_suppression_and_release_grammar(
    valid_route: bool,
) -> None:
    mapper = Mapper(vault="synthetic", mode="auto")
    _start(mapper, "message")
    common = "\u25b8 routing: Explore \u2192 [opus[1m]] (" + "x" * (32 << 10)
    assert _delta(mapper, common) == []
    suffix = ")\n" if valid_route else "!\n"
    assert _delta(mapper, suffix) == (
        [] if valid_route else [TextDelta(block_id="message:0", text=common + suffix)]
    )


@pytest.mark.parametrize("valid_route", [False, True])
@pytest.mark.parametrize("chunking", ["combined", "newline-split", "single-frame"])
def test_a_resolving_newline_leaves_ordinary_tail_outside_the_candidate_budget(
    valid_route: bool, chunking: str
) -> None:
    prefix = (
        "\u25b8 routing: Explore \u2192 [opus] (reason" if valid_route else "\u25b8 ordinary text"
    )
    ending = ")\n" if valid_route else "\n"
    ordinary = "ordinary trailing text " + "x" * (64 << 10)
    chunks = {
        "combined": [prefix, ending + ordinary],
        "newline-split": [prefix, ending, ordinary],
        "single-frame": [prefix + ending + ordinary],
    }
    mapper = Mapper(vault="synthetic", mode="auto", text_limit=None)
    _start(mapper, "message")
    events = [event for chunk in chunks[chunking] for event in _delta(mapper, chunk)]
    expected = ordinary if valid_route else prefix + ending + ordinary
    assert "".join(event.text for event in events) == expected
    assert not mapper._held and mapper._held_chars == 0


def test_a_resolved_candidate_releases_only_its_own_share_before_a_new_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mapper_module, "MAX_ROUTING_CHARS", 128)
    mapper = Mapper(vault="synthetic", mode="auto", text_limit=None)
    _start(mapper, "other")
    assert _delta(mapper, "\u25b8" + "x" * 63) == []
    _start(mapper, "current")
    assert _delta(mapper, "\u25b8 routing: Explore \u2192 [opus] (reason") == []
    ordinary = "ordinary trailing text " + "x" * 256 + "\n"
    pending = "\u25b8" + "y" * 63
    assert _delta(mapper, ")\n" + ordinary + pending) == [
        TextDelta(block_id="current:0", text=ordinary)
    ]
    assert mapper._held == {"other:0": "\u25b8" + "x" * 63, "current:0": pending}
    assert mapper._held_chars == sum(map(len, mapper._held.values())) == 128
    with pytest.raises(BufferError):
        _delta(mapper, "x")
    assert mapper._held_chars == 128


@pytest.mark.parametrize("valid_route", [False, True])
async def test_a_resolved_candidate_and_large_ordinary_tail_finish_the_driver_turn(
    valid_route: bool,
) -> None:
    prefix = (
        "\u25b8 routing: Explore \u2192 [opus] (reason" if valid_route else "\u25b8 ordinary text"
    )
    ending = ")\n" if valid_route else "\n"
    ordinary = "ordinary trailing text " + "x" * (64 << 10)
    driver, spawner, events = make_driver(
        [
            user_in(),
            out(init_frame()),
            out(text_delta(prefix)),
            out(text_delta(ending + ordinary)),
            out(result_frame()),
        ]
    )
    try:
        await driver.open()
        await driver.send("synthetic")
        await events.until(TurnDone)
        assert [event.status for event in events if isinstance(event, TurnDone)] == ["success"]
        assert not any(isinstance(event, Error) for event in events)
        expected = ordinary if valid_route else prefix + ending + ordinary
        assert "".join(event.text for event in events if isinstance(event, TextDelta)) == expected
        assert driver.alive and not driver._overflowed and not spawner.children[0].killed
    finally:
        await driver.close()


@pytest.mark.parametrize("malformed_usage", [False, True])
def test_a_terminal_result_clears_only_main_streaming_scratch(malformed_usage: bool) -> None:
    mapper = Mapper(vault="synthetic", mode="auto", artifacts_enabled=True)
    mapper.frame(assistant("launch", tool_use("run", "Agent", {"subagent_type": "Explore"})))
    mapper.frame(
        {
            "type": "system",
            "subtype": "task_started",
            "task_id": "task",
            "tool_use_id": "run",
        }
    )
    assert (
        mapper.frame(
            {
                "type": "user",
                "tool_use_result": {"status": "async_launched"},
                "message": {"content": [{"type": "tool_result", "tool_use_id": "run"}]},
            }
        )
        == []
    )
    started = mapper.frame(
        assistant("nested", tool_use("read", "Read", {"file_path": "note.md"}), parent="run")
    )[0]
    assert isinstance(started, ToolStarted) and started.artifact is not None
    for turn in range(3):
        _start(mapper, f"plain-{turn}")
        _delta(mapper, "visible")
        _start(mapper, f"held-{turn}")
        _delta(mapper, "\u25b8 routing: Explore \u2192 [opus] (unfinished")
        mapper.frame(
            _stream(
                {
                    "type": "content_block_start",
                    "index": 1,
                    "content_block": {"type": "thinking"},
                }
            )
        )
        assert (
            mapper._held
            and mapper._at_line_start
            and mapper._delta_emitted
            and mapper._thinking_start
        )
        terminal = {**result_frame(), "terminal_reason": "aborted_streaming"}
        if malformed_usage:
            terminal["usage"] = {"input_tokens": "not-a-count"}
            with pytest.raises(ValueError):
                mapper.frame(terminal)
        else:
            assert mapper.frame(terminal)[0].data()["status"] == "cancelled"
        assert not (
            mapper._held or mapper._at_line_start or mapper._delta_emitted or mapper._thinking_start
        )
    assert mapper.frame(
        {
            "type": "system",
            "subtype": "task_updated",
            "task_id": "task",
            "patch": {"status": "stopped"},
        }
    ) == [SubagentDone(block_id="run", status="stopped")]
    done = mapper.frame(
        {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "read",
                        "content": "# synthetic",
                    }
                ]
            },
        }
    )[0]
    assert isinstance(done, ToolDone) and done.artifact is not None
    _start(mapper, "continuation")
    assert _delta(mapper, "exact continuation") == [
        TextDelta(block_id="continuation:0", text="exact continuation")
    ]
    assert mapper.frame(assistant("continuation", text("exact continuation"))) == [
        TextDone(block_id="continuation:0", text="exact continuation")
    ]


async def test_routing_overflow_uses_the_existing_terminal_driver_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mapper_module, "MAX_ROUTING_CHARS", 128, raising=False)
    driver, spawner, events = make_driver(
        [
            user_in(),
            out(init_frame()),
            out(text_delta("\u25b8" + "x" * 63)),
            out(text_delta("y" * 64)),
            out(text_delta("z")),
            out(result_frame()),
        ]
    )
    try:
        await driver.open()
        await driver.send("synthetic")
        await events.until(Error)
        assert_terminal(driver, events)
        assert not any(isinstance(event, TextDelta) for event in events)
    finally:
        await driver.close()
    assert len(spawner.children) == 1 and spawner.children[0].released
