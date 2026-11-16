from __future__ import annotations

import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from tests.conversations.test_session import _login, _readiness
from tests.llm.test_connections import FORM
from tests.test_driver_limits import Events
from tests.test_driver_reports import initialize
from viki_agent.app.settings import SettingsService
from viki_agent.drivers.base import PermissionAnswer, StartError
from viki_agent.drivers.claude.control import Launch
from viki_agent.drivers.claude.driver import ClaudeDriver, DriverConfig
from viki_agent.events import PermissionRequested
from viki_agent.llm.connections import Connections, parse_connection
from viki_agent.llm.gateway import Gateway
from viki_agent.session import SessionManager
from viki_agent.state import AppState, Prefs

from replay import (
    SID,
    FakeSpawner,
    init_frame,
    out,
    permission_request,
    permission_response_in,
    result_frame,
    user_in,
)


async def test_custom_route_keeps_claude_permissions_and_hides_upstream_key(tmp_path: Path) -> None:
    gateway = Gateway()
    route = gateway.issue(parse_connection(FORM))
    spawner = FakeSpawner(
        [
            *initialize(account={}),
            out(permission_request("permission-a", "WebFetch")),
            permission_response_in("permission-a"),
        ]
    )
    envs: list[dict[str, str]] = []
    argvs: list[list[str]] = []

    async def capture(argv: Any, cwd: Path, env: Any):
        argvs.append(list(argv))
        envs.append(dict(env))
        return await spawner(argv, cwd, env)

    events = Events()
    config = DriverConfig(
        cli=("claude.exe",),
        vault=tmp_path,
        env={},
        spawner=capture,
        grace_s=0.01,
        poll_s=0.01,
        control_timeout_s=0.05,
        llm_route=route,
    )
    driver = ClaudeDriver(
        Launch(SID),
        mode="default",
        effort="high",
        model=route.connection.ref,
        config=config,
        emit=events.emit,
    )
    try:
        started = await driver.open()
        await events.until(PermissionRequested)
        assert started.model == route.connection.ref
        assert started.models[0].label == "Local test · example/model"
        assert envs[0]["ANTHROPIC_AUTH_TOKEN"] == route.token
        assert envs[0]["CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST"] == "1"
        assert FORM["api_key"] not in repr(envs)
        assert "--model=example/model" in argvs[0]
        assert driver.respond("toolu_1", PermissionAnswer(allow=False))
        with pytest.raises(ValueError):
            await driver.set_model("sonnet")
    finally:
        await driver.close()
    assert route.id not in gateway._routes

    native = ClaudeDriver(
        Launch(SID),
        mode="default",
        effort="high",
        model=None,
        config=replace(config, llm_route=None, spawner=FakeSpawner(initialize(account={}))),
        emit=lambda event: None,
    )
    try:
        with pytest.raises(StartError, match="Not logged in"):
            await native.open()
    finally:
        await native.close()


def test_unresolved_connection_never_becomes_a_subscription_model(tmp_path: Path) -> None:
    row = parse_connection(FORM)
    with pytest.raises(StartError):
        ClaudeDriver(
            Launch(SID),
            mode="default",
            effort="high",
            model=row.ref,
            config=DriverConfig(cli=("claude.exe",), vault=tmp_path, env={}),
            emit=lambda event: None,
        )


async def test_a_custom_turn_does_not_establish_subscription_login(tmp_path: Path) -> None:
    row = parse_connection(FORM)
    state = AppState(tmp_path / "state.json", Prefs())
    settings = SettingsService(
        state.store, provider_defaults=Prefs, on_chat_defaults=state.set_defaults
    )
    connections = Connections(settings)
    connections.write(0, [row])
    settings.patch(str(uuid.uuid4()), 1, {"chat.default_model": row.ref})
    gateway = Gateway()
    readiness = _readiness()
    readiness.observe_version("2.1.281")
    readiness.observe_start_error("not_logged_in")
    spawner = FakeSpawner(
        [*initialize(account={}), user_in(), out(init_frame()), out(result_frame())]
    )
    manager = SessionManager(
        config=DriverConfig(
            cli=("claude.exe",),
            vault=tmp_path,
            env={},
            spawner=spawner,
            grace_s=0.01,
            poll_s=0.01,
            control_timeout_s=0.05,
        ),
        state=state,
        claude_home=tmp_path / "home",
        readiness=readiness,
    )
    manager.bind_model_connections(connections, gateway)
    actor = manager.create(SID)
    try:
        assert await actor.send("synthetic")
        assert await actor.wait_idle(0.05)
        assert actor.started is not None and actor.started.model == row.ref
        assert _login(readiness) == "signed_out"
    finally:
        await actor.close()
    assert not gateway._routes
