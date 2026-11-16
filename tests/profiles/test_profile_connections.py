"""Profile connection refs require Bot ownership and a resolved route."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.app.test_diagnostics import _FakeConfig
from tests.automation.test_routines import _document_raw
from tests.llm.test_connections import FORM
from tests.profiles.test_bot_memory import bot
from tests.profiles.test_profile_document import document
from tests.profiles.test_profile_launch import FIXTURE_VERSION, ident
from tests.profiles.test_profile_launch import setup as setup
from viki_agent.app.diagnostics import Readiness
from viki_agent.automation.routines import RoutineService
from viki_agent.automation.tasks import TaskStore
from viki_agent.domain import ProviderReadiness, StorageReadiness
from viki_agent.llm.connections import KEY, Connections, parse_connection
from viki_agent.profiles.profile_document import evaluate_profile, parse_profile_document
from viki_agent.profiles.service import ProfileError


def _pin(service: Any, profile_id: str) -> str:
    connection = parse_connection(FORM)
    Connections(service.settings).write(0, [connection])
    raw = document()
    raw["runtime"]["model"] = connection.ref
    service.profiles.revise(profile_id, ident(), 1, raw)
    return connection.ref


def test_pure_evaluation_requires_connection_resolution() -> None:
    raw = document()
    raw["runtime"]["model"] = "viki-llm/not-a-valid-ref"
    row = next(
        field
        for field in evaluate_profile(parse_profile_document(raw)).fields
        if field.field == "runtime.model"
    )
    assert (row.status, row.reason) == ("unverified", "connection_resolution_required")


def test_runtime_billing_failure_allows_resolved_bot_only(setup: Any) -> None:
    service, state, _vault, profile_id, workspace_id = setup
    ref = _pin(service, profile_id)
    service.connections = Connections(service.settings)
    readiness = Readiness(
        config=_FakeConfig(cli=Path("synthetic-cli")),
        launch_id=ident(),
        git_available=lambda: True,
    )
    readiness.observe_version(FIXTURE_VERSION)
    readiness.observe_billing([])
    readiness.observe_start_error("billing")
    storage = StorageReadiness(state="ready", reason=None, private=True)
    service.readiness = lambda: readiness.capabilities(None, storage).provider["claude"]
    service.route_can_start = lambda: (
        readiness.may_spawn()
        and not readiness.billing_preflight_blocked
        and storage.state == "ready"
    )
    plain = service.prepare(profile_id, ident(), ident(), 2, workspace_id)
    model = next(field for field in plain["fields"] if field["field"] == "runtime.model")
    assert not plain["launchable"] and model["reason"] == "connection_bot_only"
    owner = bot(service, profile_id, workspace_id)
    session_id = ident()
    prepared = service.prepare(profile_id, ident(), session_id, 2, workspace_id, owner)
    assert prepared["launchable"]
    assert state.prefs(session_id).model == ref
    readiness.observe_billing(["billing_env_key"])
    with pytest.raises(ProfileError, match="provider_unavailable"):
        service.prepare(profile_id, ident(), ident(), 2, workspace_id, owner)


@pytest.mark.parametrize("blocked", ["cli", "storage", "preflight", "version"])
def test_connection_route_requires_other_start_gates(setup: Any, blocked: str) -> None:
    service, _state, _vault, profile_id, workspace_id = setup
    _pin(service, profile_id)
    service.connections = Connections(service.settings)
    service.route_can_start = lambda: blocked not in ("cli", "storage", "preflight")
    service.readiness = lambda: ProviderReadiness(
        "missing" if blocked == "cli" else "ready",
        "2.1.280" if blocked == "version" else FIXTURE_VERSION,
        "signed_in",
        "blocked",
        None,
        False,
        (),
    )
    owner = bot(service, profile_id, workspace_id)
    with pytest.raises(ProfileError, match="provider_unavailable"):
        service.prepare(profile_id, ident(), ident(), 2, workspace_id, owner)


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ("removed", "connection_missing"),
        ("corrupt", "connections_unavailable"),
        ("malformed", "connection_invalid"),
    ],
)
@pytest.mark.parametrize("billing_blocked", [False, True])
def test_connection_refusals_are_distinct(
    setup: Any, change: str, reason: str, billing_blocked: bool
) -> None:
    service, _state, _vault, profile_id, workspace_id = setup
    _pin(service, profile_id)
    service.connections = Connections(service.settings)
    service.route_can_start = lambda: True
    if billing_blocked:
        service.readiness = lambda: ProviderReadiness(
            "ready", FIXTURE_VERSION, "signed_in", "blocked", None, False, ()
        )
    owner = bot(service, profile_id, workspace_id)
    if change == "removed":
        service.connections.write(1, [])
    elif change == "corrupt":
        service.settings.patch(ident(), 1, {KEY: "corrupt"})
    else:
        raw = document()
        raw["runtime"]["model"] = "viki-llm/invalid"
        service.profiles.revise(profile_id, ident(), 2, raw)
    revision = 3 if change == "malformed" else 2
    preview = service.prepare(profile_id, ident(), ident(), revision, workspace_id, owner)
    row = next(field for field in preview["fields"] if field["field"] == "runtime.model")
    assert row["reason"] == reason
    assert not preview["launchable"]


def test_routine_pinned_ref_uses_native_model_without_resolving(setup: Any) -> None:
    service, state, _vault, profile_id, workspace_id = setup
    _pin(service, profile_id)
    service.connections = Connections(service.settings)
    service.route_can_start = lambda: True
    service.connections.write(1, [])
    routine = RoutineService(state.store, service.profiles, service.settings, service.workspaces)
    saved, _ = routine.create(
        ident(), _document_raw(profile_id=profile_id, workspace_id=workspace_id)
    )
    tasks = TaskStore(state.store, service.scope, ident())
    with state.store._connection(write=True) as connection:
        attempt = tasks.insert_claim(
            connection,
            routine_id=saved["routine_id"],
            kind="manual",
            revision=1,
            now="2026-11-10T00:00:00.000Z",
            trigger_id=ident(),
            request_hash="a" * 64,
        )
    prepared = service.prepare(profile_id, ident(), attempt.session_id, 2, workspace_id)
    assert prepared["launchable"]
    assert state.prefs(attempt.session_id).model is None
    service.validate_launch(attempt.session_id, state.prefs(attempt.session_id))
