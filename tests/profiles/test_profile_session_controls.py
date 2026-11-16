"""Prepared profile sessions keep their immutable launch controls."""

from __future__ import annotations

import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from tests.app.test_workspaces import service_at
from tests.profiles.test_profile_document import document
from tests.test_server import EchoDriver
from viki_agent.app.settings import SettingsService
from viki_agent.domain import ProviderReadiness
from viki_agent.drivers.base import StartError, StartFacts
from viki_agent.drivers.claude.driver import DriverConfig
from viki_agent.events import ModelOption, SessionStarted, Status
from viki_agent.llm.connections import Connections
from viki_agent.profiles.profile_capabilities import (
    ADAPTER_VERSION,
    FIXTURE_VERSION,
    baseline,
    observed_models,
)
from viki_agent.profiles.profile_launch import ProfileLaunchService
from viki_agent.profiles.provider_catalogs import ProviderCatalogService
from viki_agent.profiles.service import ProfileError, ProfileService
from viki_agent.session import CommandDispatchRejected, SessionManager
from viki_agent.state import AppState, Prefs

CLI_VERSION = FIXTURE_VERSION


def _id() -> str:
    return str(uuid.uuid4())


def _prepared_manager(tmp_path: Path) -> tuple[SessionManager, str, Prefs]:
    state = AppState(tmp_path / "data" / "state.json", Prefs())
    session_id = _id()
    prefs = Prefs(mode="plan", effort="high", model="bound-model")
    state.remember(session_id, **prefs.__dict__)
    with state.store._connection(write=True) as connection:
        connection.execute("UPDATE sessions SET origin='profile' WHERE session_id=?", (session_id,))
        connection.execute(
            "INSERT INTO session_profile_bindings "
            "(session_id,command_id,request_hash,profile_id,profile_revision,workspace_id,"
            "workspace_revision,policy,policy_revision,settings_revision,runtime,capability_key,"
            "launch_spec_hash,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                session_id,
                _id(),
                "a" * 64,
                _id(),
                1,
                _id(),
                1,
                "ordinary",
                1,
                0,
                "claude",
                "b" * 64,
                "c" * 64,
                "2026-09-21T00:00:00.000Z",
            ),
        )
    manager = SessionManager(
        config=DriverConfig(cli=(), vault=tmp_path, env={}),
        state=state,
        claude_home=tmp_path / "provider",
    )
    return manager, session_id, prefs


@pytest.mark.asyncio
async def test_profile_session_refuses_model_and_effort_changes(tmp_path: Path) -> None:
    manager, session_id, prefs = _prepared_manager(tmp_path)
    actor = manager.attach_prepared(session_id)

    with pytest.raises(CommandDispatchRejected, match="profile_locked") as model_failure:
        await actor.set_model("other-model")
    assert model_failure.value.code == "profile_locked"
    with pytest.raises(CommandDispatchRejected, match="profile_locked") as effort_failure:
        actor.set_effort("low")
    assert effort_failure.value.code == "profile_locked"
    assert manager.app_state.prefs(session_id) == prefs


def test_profile_session_cannot_fork_into_an_unbound_session(tmp_path: Path) -> None:
    manager, session_id, _prefs = _prepared_manager(tmp_path)
    fork_id = _id()

    with pytest.raises(CommandDispatchRejected, match="profile_locked") as failure:
        manager.fork(session_id, fork_id)
    assert failure.value.code == "profile_locked"
    assert manager.get(fork_id) is None
    assert manager.app_state.store.session_origin(fork_id) is None


class MenuDriver(EchoDriver):
    """Starts like the fixture CLI version and reports an initialize model menu."""

    async def open(self) -> SessionStarted:
        self.alive = True
        return SessionStarted(
            model="default", mode="plan", effort="high", commands=[], agents=[],
            models=[ModelOption(value="synthetic-model", label="Synthetic")],
            cli_version=CLI_VERSION,
        )  # fmt: skip


def _prepared(tmp_path: Path, **runtime: Any) -> tuple[ProfileLaunchService, AppState, str]:
    """A launchable prepared profile session over the real services; nothing is spawned."""
    state = AppState(tmp_path / "data" / "state.json", Prefs())
    workspaces = service_at(tmp_path, state.store)
    vault = tmp_path / "vault"
    vault.mkdir()
    workspace, _ = workspaces.register_existing(_id(), str(vault))
    workspaces.select(_id(), workspace.workspace_id)
    profiles = ProfileService(state.store)
    profile_id = _id()
    raw = document()
    raw["runtime"].update(runtime)
    profiles.create(profile_id, _id(), raw)
    settings = SettingsService(
        state.store, provider_defaults=lambda: Prefs(), on_chat_defaults=lambda _: None
    )
    readiness = ProviderReadiness("ready", CLI_VERSION, "signed_in", "clear", None, True, ())
    service = ProfileLaunchService(
        profiles,
        settings,
        workspaces,
        ProviderCatalogService(state.store),
        lambda: readiness,
        vault,
        lambda _: False,
        connections=Connections(settings),
        route_can_start=lambda: True,
    )
    session_id = _id()
    prepared = service.prepare(profile_id, _id(), session_id, 1, workspace.workspace_id)
    assert prepared["launchable"]
    return service, state, session_id


def _bound_manager(
    service: ProfileLaunchService, state: AppState, drivers: list[EchoDriver], tmp_path: Path
) -> SessionManager:
    """The server's profile wiring (server.py bind_profiles) over synthetic drivers."""

    def factory(launch: Any, prefs: Prefs, emit: Any) -> EchoDriver:
        drivers.append(MenuDriver(launch, emit))
        return drivers[-1]

    async def guard(session_id: str, prefs: Prefs) -> None:
        try:
            service.validate_launch(session_id, prefs)
        except ProfileError as error:
            raise StartError("bad_request", error.code) from None

    async def initialized(session_id: str, facts: StartFacts | None) -> None:
        service.native_initialized(session_id, facts)

    async def observe(started: SessionStarted) -> None:
        service.catalogs.observe(
            "claude",
            ADAPTER_VERSION,
            started.cli_version,
            baseline(started.cli_version, models_observed=bool(started.models)),
            observed_models(started),
        )

    manager = SessionManager(
        config=DriverConfig(cli=(), vault=tmp_path, env={}),
        state=state,
        claude_home=tmp_path / "provider",
        make_driver=factory,
    )
    manager.bind_profiles(guard, observe, initialized=initialized)
    return manager


def _bound_key(service: ProfileLaunchService) -> str:
    latest = service.catalogs.latest("claude", ADAPTER_VERSION, CLI_VERSION)
    assert latest is not None
    return latest["catalog_key"]


@pytest.mark.asyncio
async def test_a_reported_mode_never_rewrites_a_profile_sessions_launch(tmp_path: Path) -> None:
    service, state, session_id = _prepared(tmp_path, permission_mode="plan")
    drivers: list[EchoDriver] = []
    manager = _bound_manager(service, state, drivers, tmp_path)
    actor = manager.attach_prepared(session_id)
    seen: list[dict[str, Any]] = []
    actor.subscribe(seen.append)
    assert await actor.send("first")
    # After an approved plan, setMode restores the mode before it and the CLI reports it.
    drivers[0]._emit(Status(mode="default"))
    assert seen[-1]["type"] == "status" and seen[-1]["data"]["mode"] == "default"
    assert state.prefs(session_id).mode == "plan"
    assert await actor.send("second")
    await manager.close(session_id)
    assert await manager.open(session_id).send("after a reopen")
    assert len(drivers) == 2 and drivers[1].launch.resume


@pytest.mark.asyncio
async def test_a_live_profile_session_outlives_a_version_class_change_until_it_reopens(
    tmp_path: Path,
) -> None:
    service, state, session_id = _prepared(tmp_path)
    drivers: list[EchoDriver] = []
    manager = _bound_manager(service, state, drivers, tmp_path)
    actor = manager.attach_prepared(session_id)
    assert await actor.send("first")
    # Readiness now reads an older Claude Code, outside the version class this binding was made at.
    older = replace(service.readiness(), version="2.1.259")
    service.readiness = lambda: older
    # A running process keeps the launch it was checked with (`SessionActor._start`).
    assert await actor.send("second")
    await manager.close(session_id)
    reopened = manager.open(session_id)
    seen: list[dict[str, Any]] = []
    reopened.subscribe(seen.append)
    # Drift is checked before a spawn.
    assert not await reopened.send("after a reopen")
    assert len(drivers) == 1
    assert seen[-1]["type"] == "error" and seen[-1]["data"]["message"] == "capability_changed"


@pytest.mark.asyncio
async def test_only_an_ordinary_start_records_the_provider_catalog(tmp_path: Path) -> None:
    service, state, session_id = _prepared(tmp_path)
    bound = _bound_key(service)
    drivers: list[EchoDriver] = []
    manager = _bound_manager(service, state, drivers, tmp_path)
    assert await manager.attach_prepared(session_id).send("first")
    assert _bound_key(service) == bound
    # The same menu from an ordinary session is recorded, so the check above is not vacuous.
    assert await manager.create(_id()).start()
    assert _bound_key(service) != bound
    await manager.close_all()
