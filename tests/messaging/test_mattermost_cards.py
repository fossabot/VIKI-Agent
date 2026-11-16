"""Synthetic full-detail permission access; no provider or account calls."""

from __future__ import annotations

import asyncio
import json
import tracemalloc
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import PermissionResultDeny, ToolPermissionContext

from tests.conversations.test_session import Drivers, GatedDriver, gated
from viki_agent.drivers.base import PermissionAnswer
from viki_agent.drivers.claude import driver as driver_module
from viki_agent.drivers.claude.control import Launch
from viki_agent.drivers.claude.driver import ClaudeDriver, DriverConfig
from viki_agent.events import Event, PermissionRequested, RequestCancelled
from viki_agent.session import Envelope, SessionActor, SessionManager
from viki_agent.state import AppState, Prefs

SID = "22222222-2222-4222-8222-222222222222"
TOOL = "mcp__synthetic__request"


@dataclass
class Cards:
    actor: SessionActor
    driver: ClaudeDriver
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    events: list[Envelope] = field(default_factory=list)
    tasks: list[asyncio.Task[Any]] = field(default_factory=list)

    def emit(self, event: Event) -> None:
        self.events.append(self.actor._emit(event))
        if isinstance(event, PermissionRequested):
            self.ready.set()

    async def raise_card(
        self, tool_input: dict[str, Any], *, tool: str = TOOL, card_id: str = "card"
    ) -> dict[str, str]:
        self.ready.clear()
        self.tasks.append(
            asyncio.create_task(
                self.driver._can_use_tool(
                    tool, tool_input, ToolPermissionContext(tool_use_id=card_id)
                )
            )
        )
        async with asyncio.timeout(0.05):
            await self.ready.wait()
        return self.events[-1]["native_request"]


@pytest.fixture
async def cards(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Cards]:
    config = DriverConfig(cli=("claude.exe",), vault=tmp_path / "synthetic", env={})
    manager = SessionManager(
        config=config,
        state=AppState(tmp_path / "state.json", Prefs()),
        claude_home=tmp_path / "synthetic-home",
    )
    actor = manager.create(SID)

    def emit(event: Event) -> None:
        actor._emit(event)

    driver = ClaudeDriver(
        Launch(SID), mode="auto", effort="low", model=None, config=config, emit=emit
    )
    driver.alive = True
    actor._driver, actor._token = driver, object()
    harness = Cards(actor, driver)
    monkeypatch.setattr(driver, "_enqueue_card", harness.emit)

    async def close_control() -> None:
        pass  # This driver owns no process or SDK connection.

    monkeypatch.setattr(driver, "_close_control", close_control)
    yield harness
    await driver.close()
    await asyncio.gather(*harness.tasks, return_exceptions=True)


@pytest.mark.parametrize(
    ("tool", "tool_input"),
    [
        ("Bash", {"command": "  printf '%s\\n' 'synthetic'\t ", "timeout": 40}),
        ("PowerShell", {"command": "  Write-Output\t'synthetic'\r\n  "}),
        ("Edit", {"file_path": "a.md", "old_string": "hidden before", "new_string": "after"}),
        (TOOL, {"title": "shown", "hidden": {"arguments": [None, True, 1.25, "\u00e9\U0001f642"]}}),
    ],
)
async def test_remote_details_are_full_bounded_and_live(
    cards: Cards, tool: str, tool_input: dict[str, Any]
) -> None:
    binding = await cards.raise_card(tool_input, tool=tool)
    details = cards.actor.remote_permission("card", binding)
    assert details == {"tool": tool, "input": tool_input}
    assert len(json.dumps(details, ensure_ascii=True, separators=(",", ":"))) <= 8192
    assert details is not None and details["input"] is not tool_input
    details["input"]["changed"] = "only the returned copy"
    assert "changed" not in tool_input
    assert "input" not in cards.events[0]["data"]
    for key in ("runtime_id", "request_id", "turn_id"):
        assert cards.actor.remote_permission("card", {**binding, key: "stale"}) is None
    assert cards.actor.remote_permission("missing", binding) is None
    assert cards.actor.respond(
        "card", PermissionAnswer(False), expected_generation=cards.actor.generation,
        native_request=binding,
    )  # fmt: skip
    assert cards.actor.remote_permission("card", binding) is None
    assert not cards.driver._permission_inputs
    assert isinstance(await cards.tasks[0], PermissionResultDeny)


@pytest.mark.parametrize("extra", [0, 1])
async def test_ascii_json_byte_boundary(cards: Cards, extra: int) -> None:
    framing = len(json.dumps({"tool": TOOL, "input": {"value": ""}}, separators=(",", ":")))
    binding = await cards.raise_card({"value": "x" * (8192 - framing + extra)})
    details = cards.actor.remote_permission("card", binding)
    assert (details is None) == bool(extra)
    if details is not None:
        assert len(json.dumps(details, separators=(",", ":"))) == 8192


@pytest.mark.parametrize("extra", [0, 1])
async def test_del_ascii_json_byte_boundary(cards: Cards, extra: int) -> None:
    framing = len(json.dumps({"tool": "Bash", "input": {"command": ""}}, separators=(",", ":")))
    count, padding = divmod(8192 - framing, 6)
    tool_input = {"command": chr(127) * count + "x" * (padding + extra)}
    binding = await cards.raise_card(tool_input, tool="Bash")
    details = cards.actor.remote_permission("card", binding)
    assert (details is None) == bool(extra)
    if details is not None:
        assert details == {"tool": "Bash", "input": tool_input}
        assert len(json.dumps(details, ensure_ascii=True, separators=(",", ":"))) == 8192


def test_ascii_escape_accounting_matches_encoder_at_byte_boundary() -> None:
    framing = len(json.dumps({"tool": "Bash", "input": {"command": ""}}, separators=(",", ":")))
    for code in range(128):
        char = chr(code)
        escaped_size = len(json.dumps(char, ensure_ascii=True)) - 2
        count, padding = divmod(8192 - framing, escaped_size)
        tool_input = {"command": char * count + "x" * padding}
        encoded = driver_module._permission_json("Bash", tool_input)
        assert encoded is not None and len(encoded) == 8192, code
        tool_input["command"] += "x"
        assert driver_module._permission_json("Bash", tool_input) is None, code


def test_final_encoded_json_cap_is_checked(monkeypatch: pytest.MonkeyPatch) -> None:
    oversized = json.dumps({"tool": "Bash", "input": {"command": "x" * 8192}})
    monkeypatch.setattr(driver_module.json, "dumps", lambda *args, **kwargs: oversized)
    assert driver_module._permission_json("Bash", {"command": "small"}) is None


@pytest.mark.parametrize(
    "bad",
    [
        "x" * (2 << 20),
        "\u00e9" * 2000,
        chr(127) * 2000,
        [None] * 3000,
        float("nan"),
        {1: "bad key"},
        object(),
    ],
    ids=["huge-string", "ascii-expansion", "del-expansion", "nodes", "nan", "key", "non-json"],
)
async def test_invalid_or_large_raw_values_never_reach_json_serialization(
    cards: Cards, monkeypatch: pytest.MonkeyPatch, bad: Any
) -> None:
    calls: list[dict[str, Any]] = []
    dumps = driver_module.json.dumps

    def bounded_dumps(value: Any, *args: Any, **kwargs: Any) -> str:
        if isinstance(value, dict) and set(value) == {"tool", "input"}:
            calls.append(value)
        return dumps(value, *args, **kwargs)

    monkeypatch.setattr(driver_module.json, "dumps", bounded_dumps)
    tracemalloc.start()
    try:
        binding = await cards.raise_card({"label": "safe", "hidden": bad})
        assert cards.actor.remote_permission("card", binding) is None
        assert calls == []
        assert tracemalloc.get_traced_memory()[1] < 128 << 10
    finally:
        tracemalloc.stop()


async def test_deep_or_cyclic_input_is_not_copied(cards: Cards) -> None:
    deep: dict[str, Any] = {}
    for _ in range(40):
        deep = {"nested": deep}
    deep["cycle"] = deep
    binding = await cards.raise_card({"label": "safe", "hidden": deep})
    assert cards.actor.remote_permission("card", binding) is None


async def test_mutated_input_and_reused_native_binding_fail_closed(cards: Cards) -> None:
    tool_input = {"label": "safe", "hidden": {"before": "original"}}
    binding = await cards.raise_card(tool_input)
    tool_input["hidden"]["before"] = "mutated"
    assert cards.actor.remote_permission("card", binding) is None
    assert not cards.driver._permission_inputs
    cards.tasks[0].cancel()
    await asyncio.gather(cards.tasks[0], return_exceptions=True)
    current = await cards.raise_card({"label": "new request"})
    assert current != binding
    assert cards.actor.remote_permission("card", binding) is None
    assert cards.actor.remote_permission("card", current) == {
        "tool": TOOL,
        "input": {"label": "new request"},
    }


@pytest.mark.parametrize("terminal", ["cancel", "exit", "close", "overflow"])
async def test_detail_references_end_with_card_lifecycle(cards: Cards, terminal: str) -> None:
    binding = await cards.raise_card({"label": "safe"})
    if terminal == "cancel":
        cards.tasks[0].cancel()
        await asyncio.gather(cards.tasks[0], return_exceptions=True)
    elif terminal == "exit":
        cards.driver._on_exit("")
    elif terminal == "close":
        await cards.driver.close()
    else:
        cards.driver._overflow()
    assert cards.actor.remote_permission("card", binding) is None
    assert not cards.driver._permission_inputs


@pytest.mark.parametrize("change", ["binding", "mutated-binding", "token", "driver", "closed"])
async def test_native_binding_is_rechecked_after_capability_lookup(
    cards: Cards, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    binding = await cards.raise_card({"label": "safe"})
    details = cards.driver.permission_details

    def changing_lookup(tool_use_id: str) -> dict[str, Any] | None:
        result = details(tool_use_id)
        if change == "binding":
            cards.actor._emit(RequestCancelled(tool_use_id="card"))
        elif change == "mutated-binding":
            cards.actor._native_requests["card"]["request_id"] = "changed"
        elif change == "token":
            cards.actor._token = object()
        elif change == "driver":
            cards.actor._driver = GatedDriver()
        else:
            cards.actor._closed = True
        return result

    monkeypatch.setattr(cards.driver, "permission_details", changing_lookup)
    assert cards.actor.remote_permission("card", binding) is None


async def test_legacy_driver_has_no_remote_permission_authority(cards: Cards) -> None:
    binding = await cards.raise_card({"label": "safe"})
    cards.actor._driver = GatedDriver()
    assert cards.actor.remote_permission("card", binding) is None


async def test_details_are_available_to_first_card_subscriber(cards: Cards) -> None:
    received: list[dict[str, Any] | None] = []

    def subscriber(envelope: Envelope) -> None:
        if envelope["type"] == "permission.requested":
            received.append(cards.actor.remote_permission("card", envelope["native_request"]))

    cards.actor.subscribe(subscriber)
    await cards.raise_card({"label": "safe", "hidden": "complete"})
    assert received == [{"tool": TOOL, "input": {"label": "safe", "hidden": "complete"}}]


async def test_card_emission_failure_releases_input_reference(
    cards: Cards, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failed_emit(event: Event) -> None:
        raise RuntimeError("synthetic card emission failure")

    monkeypatch.setattr(cards.driver, "_enqueue_card", failed_emit)
    with pytest.raises(RuntimeError, match="synthetic card emission failure"):
        await cards.driver._can_use_tool(
            TOOL, {"label": "safe"}, ToolPermissionContext(tool_use_id="failed")
        )
    assert not cards.driver._permission_inputs
    assert not cards.driver._cards


async def test_create_root_override_stays_with_only_its_actor(tmp_path: Path) -> None:
    drivers = Drivers()
    manager = gated(tmp_path, drivers)
    roots: list[str] = []

    async def ordinary(session_id: str) -> Path:
        roots.append(session_id)
        return tmp_path / "ordinary"

    async def fixed(session_id: str) -> Path:
        return tmp_path / "fixed"

    manager.bind_roots(ordinary)
    own = manager.create(SID, root_resolver=fixed)
    other = manager.create("33333333-3333-4333-8333-333333333333", read_only_tools=True)
    assert await own.start() and await other.start()
    assert [launch.cwd for _, launch, _, _ in drivers.made] == [
        tmp_path / "fixed",
        tmp_path / "ordinary",
    ]
    assert roots == [other.session_id]
    assert drivers.made[1][1].read_only_tools
    assert own.set_effort("medium")
    assert await own.send("synthetic respawn")
    assert drivers.made[2][1].cwd == tmp_path / "fixed"
    assert roots == [other.session_id]
    await manager.close_all()


@pytest.mark.parametrize("first_raises", [False, True])
async def test_settle_composes_hooks_before_grace(tmp_path: Path, first_raises: bool) -> None:
    manager = gated(tmp_path, GatedDriver())
    calls: list[str] = []

    def queue_hook() -> None:
        calls.append("queue")
        if first_raises:
            raise RuntimeError("synthetic")

    manager.bind_settle(queue_hook)
    manager.bind_settle(lambda: calls.append("mobile"))
    if first_raises:
        with pytest.raises(RuntimeError, match="synthetic"):
            await manager.settle(0)
    else:
        await manager.settle(0)
    assert calls == ["queue", "mobile"]
