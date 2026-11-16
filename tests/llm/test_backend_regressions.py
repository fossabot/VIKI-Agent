from __future__ import annotations

import uuid
from dataclasses import replace
from pathlib import Path

import pytest

from tests.conversations.test_session import _readiness
from tests.llm.test_connections import FORM
from tests.test_driver_reports import initialize
from viki_agent.app.settings import SettingsService
from viki_agent.drivers.base import StartError
from viki_agent.drivers.claude.control import Launch
from viki_agent.drivers.claude.driver import ClaudeDriver, DriverConfig
from viki_agent.events import Error, TurnDone, Usage
from viki_agent.llm.connections import Connections, parse_connection
from viki_agent.llm.gateway import Gateway
from viki_agent.session import SessionManager
from viki_agent.state import AppState, Prefs

from replay import SID, FakeSpawner


def setup(tmp_path: Path, *, readiness=None, version: str = "2.1.281"):
    state = AppState(tmp_path / "state.json", Prefs(model="sonnet"))
    settings = SettingsService(
        state.store,
        provider_defaults=lambda: Prefs(model="sonnet"),
        on_chat_defaults=state.set_defaults,
    )
    connections = Connections(settings)
    row = parse_connection(FORM)
    connections.write(0, [row])
    gateway = Gateway()
    spawner = FakeSpawner(initialize(account={}))
    manager = SessionManager(
        config=DriverConfig(
            cli=("synthetic.exe",),
            vault=tmp_path,
            env={},
            cli_version=version,
            spawner=spawner,
            grace_s=0.01,
            poll_s=0.01,
            control_timeout_s=0.05,
        ),
        state=state,
        claude_home=tmp_path / "synthetic-home",
        readiness=readiness,
    )
    manager.bind_model_connections(connections, gateway)
    return state, settings, connections, row, gateway, manager


async def test_missing_cli_revokes_route_before_explicit_driver_close(tmp_path: Path) -> None:
    gateway = Gateway()
    route = gateway.issue(parse_connection(FORM))

    async def missing(*args):
        raise FileNotFoundError("synthetic missing executable")

    driver = ClaudeDriver(
        Launch(SID),
        mode="default",
        effort="high",
        model=route.connection.ref,
        config=DriverConfig(
            cli=("missing.exe",), vault=tmp_path, env={}, spawner=missing, llm_route=route
        ),
        emit=lambda event: None,
    )
    try:
        with pytest.raises(StartError) as failure:
            await driver.open()
        assert failure.value.code == "cli_missing"
        assert not gateway._routes
    finally:
        await driver.close()


async def test_missing_cli_actor_does_not_retain_gateway_route(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, _, _, row, gateway, manager = setup(tmp_path)

    async def missing(*args):
        raise FileNotFoundError("synthetic missing executable")

    monkeypatch.setattr(manager, "_config", replace(manager._config, spawner=missing))
    state.remember(SID, model=row.ref)
    actor = manager.create(SID)
    try:
        assert not await actor.start()
        assert not gateway._routes
        assert not actor.live
    finally:
        await manager.close(SID)


async def test_runtime_subscription_billing_refusal_does_not_block_explicit_connection(
    tmp_path: Path,
) -> None:
    readiness = _readiness()
    readiness.observe_billing([])
    readiness.observe_start_error("billing")
    readiness.observe_version("2.1.281")
    state, _, _, row, gateway, manager = setup(tmp_path, readiness=readiness)
    state.remember(SID, model=row.ref)
    actor = manager.create(SID)
    try:
        assert await actor.start()
        assert readiness.billing == "blocked"
        with pytest.raises(StartError) as native:
            manager._claude_driver(Launch(str(uuid.uuid4())), Prefs(model="sonnet"), lambda e: None)
        assert native.value.code == "billing"
    finally:
        await manager.close(SID)
    assert not gateway._routes


@pytest.mark.parametrize(
    "problem",
    [
        "environment: ANTHROPIC_API_KEY is set",
        "settings: apiKeyHelper is set",
        "settings: CLAUDE_CODE_USE_BEDROCK is set",
    ],
)
def test_preflight_conflicts_refuse_custom_before_connection_resolution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    problem: str,
) -> None:
    readiness = _readiness()
    readiness.observe_billing([problem])
    _, _, connections, row, gateway, manager = setup(tmp_path, readiness=readiness)

    def forbidden(*args):
        pytest.fail("conflicting settings must refuse before resolving a connection")

    monkeypatch.setattr(connections, "resolve", forbidden)
    with pytest.raises(StartError) as failure:
        manager._claude_driver(Launch(SID), Prefs(model=row.ref), lambda e: None)
    assert failure.value.code == "billing"
    assert not gateway._routes


@pytest.mark.parametrize(
    "version",
    ["", "unknown", "2.1.280", "2.1.9", "2.1.281-beta", "2.1.281.0"],
)
def test_custom_route_requires_observed_supported_cli_before_issuing_capability(
    tmp_path: Path,
    version: str,
) -> None:
    _, _, _, row, gateway, manager = setup(tmp_path, version=version)
    with pytest.raises(StartError) as failure:
        manager._claude_driver(Launch(SID), Prefs(model=row.ref), lambda e: None)
    assert failure.value.code == "bad_request"
    assert "2.1.281" in failure.value.message
    assert not gateway._routes
    # The native subscription path keeps its existing version policy.
    assert isinstance(manager._claude_driver(Launch(SID), Prefs(), lambda e: None), ClaudeDriver)


@pytest.mark.parametrize("version", ["2.1.281", "2.1.282", "2.1.300", "3.0.0"])
async def test_custom_route_uses_latest_observed_version(tmp_path: Path, version: str) -> None:
    readiness = _readiness()
    readiness.observe_version(version)
    _, _, _, row, gateway, manager = setup(tmp_path, readiness=readiness, version="2.1.1")
    driver = manager._claude_driver(Launch(SID), Prefs(model=row.ref), lambda e: None)
    try:
        assert isinstance(driver, ClaudeDriver)
        assert driver._config.cli_version == version
        assert len(gateway._routes) == 1
    finally:
        await driver.close()


def test_failed_latest_version_probe_does_not_reuse_stale_config_for_custom_route(
    tmp_path: Path,
) -> None:
    readiness = _readiness()
    readiness.observe_version("2.1.281")
    _, _, _, row, gateway, manager = setup(tmp_path, readiness=readiness)
    readiness.observe_version("")
    with pytest.raises(StartError) as failure:
        manager._claude_driver(Launch(SID), Prefs(model=row.ref), lambda e: None)
    assert failure.value.code == "bad_request"
    assert not gateway._routes


async def test_latched_gateway_failure_explains_process_lifetime_recovery(tmp_path: Path) -> None:
    gateway = Gateway()
    route = gateway.issue(parse_connection(FORM))
    events = []
    driver = ClaudeDriver(
        Launch(SID),
        mode="default",
        effort="high",
        model=route.connection.ref,
        config=DriverConfig(cli=("synthetic.exe",), vault=tmp_path, env={}, llm_route=route),
        emit=events.append,
    )
    try:
        route.failure_code = "upstream_failed"
        driver._finish_turn(TurnDone(status="error", usage=Usage()))
        assert len(events) == 2
        assert isinstance(events[0], TurnDone)
        assert events[1] == Error(
            code="bad_request",
            message="Close and reopen this chat to retry.",
            recoverable=False,
        )
        assert FORM["api_key"] not in repr(events)
        assert FORM["base_url"] not in repr(events)
        assert route.failure_code == "upstream_failed"
    finally:
        await driver.close()
