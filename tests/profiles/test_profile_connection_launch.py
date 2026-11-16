"""Pinned Bot connections are checked at each launch without native-menu fallback."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from tests.app.test_diagnostics import _FakeConfig
from tests.automation.test_routines import _document_raw
from tests.profiles.test_bot_memory import bot
from tests.profiles.test_profile_connections import _pin
from tests.profiles.test_profile_document import document
from tests.profiles.test_profile_launch import (
    FIXTURE_VERSION,
    NEWER,
    _server,
    ident,
    native_started,
    ready,
)
from tests.profiles.test_profile_launch import setup as setup
from viki_agent import protection
from viki_agent.app.diagnostics import Readiness
from viki_agent.automation import tasks as task_module
from viki_agent.automation.routines import RoutineService
from viki_agent.automation.tasks import TaskStore
from viki_agent.domain import StorageReadiness
from viki_agent.drivers.base import StartFacts
from viki_agent.profiles.profile_document import parse_profile_document
from viki_agent.profiles.profile_launch import ProfileLaunchService
from viki_agent.profiles.service import ProfileError, _context

NOW = "2026-11-10T00:00:00.000Z"


@pytest.fixture
def pinned(setup: Any):
    service, state, vault, profile_id, workspace_id = setup
    ref = _pin(service, profile_id)
    owner = bot(service, profile_id, workspace_id)
    return service, state, vault, profile_id, workspace_id, owner, ref


def claim(service: Any, profile_id: str, workspace_id: str):
    routines = RoutineService(service.store, service.profiles, service.settings, service.workspaces)
    saved, _ = routines.create(
        ident(), _document_raw(profile_id=profile_id, workspace_id=workspace_id)
    )
    tasks = TaskStore(service.store, service.scope, ident())
    with service.store._connection(write=True) as connection:
        attempt = tasks.insert_claim(
            connection,
            routine_id=saved["routine_id"],
            kind="manual",
            revision=1,
            now=NOW,
            trigger_id=ident(),
            request_hash="a" * 64,
        )
    return tasks, attempt


def restart(service: Any, vault: Path) -> ProfileLaunchService:
    return ProfileLaunchService(
        service.profiles,
        service.settings,
        service.workspaces,
        service.catalogs,
        service.readiness,
        vault,
        service.transcript_exists,
        connections=service.connections,
        route_can_start=service.route_can_start,
    )


def test_bot_main_task_and_thread_launches_keep_exact_ref(pinned: Any) -> None:
    service, state, _vault, profile_id, workspace_id, owner, ref = pinned
    seen: list[str] = []
    resolve = service.connections.resolve

    def resolved(value: str):
        seen.append(value)
        return resolve(value)

    service.connections.resolve = resolved
    # Main chat, a separate task, and a Bot thread use distinct profile bindings.
    for _ in range(3):
        session_id = ident()
        assert service.prepare(profile_id, ident(), session_id, 2, workspace_id, owner)[
            "launchable"
        ]
        assert state.prefs(session_id).model == ref
        seen.clear()
        service.validate_launch(session_id, state.prefs(session_id))
        assert seen and set(seen) == {ref}
        service.native_initialized(session_id, StartFacts(None, "default", (), None))
        assert native_started(state, session_id) == 1


@pytest.mark.parametrize("version", [FIXTURE_VERSION, NEWER])
def test_connection_start_skips_native_menu_but_keeps_plan_and_mcp_checks(
    pinned: Any, version: str
) -> None:
    service, state, _vault, profile_id, workspace_id, owner, ref = pinned
    service.readiness = lambda: ready(version)
    service.workspaces.set_policy(ident(), workspace_id, 1, "ordinary", [])
    raw = document()
    raw["runtime"].update(model=ref, permission_mode="plan")
    raw["tools"]["mcp_configuration"] = {"servers": [_server()]}
    service.profiles.revise(profile_id, ident(), 2, raw)
    session_id = ident()
    assert service.prepare(profile_id, ident(), session_id, 3, workspace_id, owner)["launchable"]
    if version == NEWER:
        for facts, code in (
            (None, "start_unconfirmed"),
            (StartFacts(None, "default", (), ("viki_echo",)), "plan_mode_unconfirmed"),
            (StartFacts(None, "plan", (), ()), "mcp_config_unconfirmed"),
        ):
            with pytest.raises(ProfileError, match=code):
                service.native_initialized(session_id, facts)
            assert native_started(state, session_id) == 0
    service.native_initialized(session_id, StartFacts(None, "plan", (), ("viki_echo",)))
    assert native_started(state, session_id) == 1


@pytest.mark.parametrize("stored", [False, True])
@pytest.mark.parametrize("model", [None, "sonnet", "viki-llm/invalid"])
def test_changed_connection_prefs_refuse_without_resolving(
    pinned: Any, monkeypatch: pytest.MonkeyPatch, stored: bool, model: str | None
) -> None:
    service, state, _vault, profile_id, workspace_id, owner, _ref = pinned
    session_id = ident()
    service.prepare(profile_id, ident(), session_id, 2, workspace_id, owner)
    prefs = replace(state.prefs(session_id), model=model)
    if stored:
        with state.store._connection(write=True) as connection:
            connection.execute(
                "UPDATE sessions SET model=? WHERE session_id=?", (model, session_id)
            )

    def unexpected(_: str):
        raise AssertionError("Changed preferences must not resolve a connection")

    monkeypatch.setattr(service.connections, "resolve", unexpected)
    with pytest.raises(ProfileError, match="launch_changed"):
        service.validate_launch(session_id, prefs)
    if stored:
        assert not service.inspect_session(session_id)["launchable"]


@pytest.mark.parametrize("change", ["removed", "unreadable", "bot"])
def test_reopen_rechecks_exact_connection_and_bot(pinned: Any, change: str) -> None:
    service, state, vault, profile_id, workspace_id, owner, _ref = pinned
    session_id = ident()
    service.prepare(profile_id, ident(), session_id, 2, workspace_id, owner)
    service.native_initialized(session_id)
    service.transcript_exists = lambda _: True
    if change == "removed":
        service.connections.write(1, [])
        code = "connection_missing"
    elif change == "unreadable":
        service.settings.patch(ident(), 1, {"chat.llm_connections": "corrupt"})
        code = "connections_unavailable"
    else:
        with state.store._connection(write=True) as connection:
            connection.execute("DELETE FROM bots WHERE bot_id=?", (owner,))
        code = "launch_changed"
    reopened = restart(service, vault)
    reopened.reopenable(session_id, True)
    with pytest.raises(ProfileError, match=code):
        reopened.validate_launch(session_id, state.prefs(session_id))
    preview = reopened.inspect_session(session_id)
    assert not preview["launchable"] and code in preview["warnings"]


@pytest.mark.parametrize(
    ("change", "code"),
    [("read_only", "connections_unavailable"), ("insecure", "connection_invalid")],
)
def test_validate_launch_refuses_with_the_field_reason(pinned: Any, change: str, code: str) -> None:
    service, state, _vault, profile_id, workspace_id, owner, _ref = pinned
    session_id = ident()
    assert service.prepare(profile_id, ident(), session_id, 2, workspace_id, owner)["launchable"]
    if change == "read_only":
        # A newer settings format leaves the connection store read-only.
        with state.store._connection(write=True) as connection:
            connection.execute("UPDATE settings SET version=version+1")
    else:
        # Same identity, a URL today's validation refuses (as another build might have stored).
        revision, [row] = service.connections.read()
        service.connections.write(revision, [replace(row, base_url="http://example.com")])
    with pytest.raises(ProfileError) as raised:
        service.validate_launch(session_id, state.prefs(session_id))
    assert raised.value.code == code
    preview = service.inspect_session(session_id)
    model = next(field for field in preview["fields"] if field["field"] == "runtime.model")
    assert model["reason"] == code
    assert preview["blockers"] == ["runtime.model", code] and preview["warnings"] == [code]


def test_connection_binding_cannot_become_a_routine(pinned: Any) -> None:
    service, state, _vault, profile_id, workspace_id, owner, _ref = pinned
    session_id = ident()
    service.prepare(profile_id, ident(), session_id, 2, workspace_id, owner)
    _tasks, attempt = claim(service, profile_id, workspace_id)
    with state.store._connection(write=True) as connection:
        connection.execute(
            "UPDATE task_attempts SET session_id=? WHERE attempt_id=?",
            (session_id, attempt.attempt_id),
        )
    with pytest.raises(ProfileError, match="launch_blocked"):
        service.validate_launch(session_id, state.prefs(session_id))
    assert not service.inspect_session(session_id)["launchable"]


@pytest.mark.parametrize("pruned", [False, True])
def test_routine_effective_document_survives_prune_without_resolving(
    pinned: Any, monkeypatch: pytest.MonkeyPatch, pruned: bool
) -> None:
    service, state, vault, profile_id, workspace_id, owner, _ref = pinned
    service.readiness = lambda: ready(NEWER)
    service.settings.patch(ident(), 1, {"chat.default_model": "sonnet"})
    tasks, attempt = claim(service, profile_id, workspace_id)

    def unexpected(_: str):
        raise AssertionError("A routine must not resolve the profile connection")

    monkeypatch.setattr(service.connections, "resolve", unexpected)
    prepared = service.prepare(profile_id, ident(), attempt.session_id, 2, workspace_id, owner)
    assert prepared["launchable"] and state.prefs(attempt.session_id).model == "sonnet"
    if pruned:
        assert tasks.finish(attempt.attempt_id, "succeeded", "", NOW)
        monkeypatch.setattr(task_module, "MAX_OCCURRENCES", 0)
        with state.store._connection(write=True) as connection:
            tasks.prune(connection, attempt.routine_id)
            assert (
                connection.execute(
                    "SELECT 1 FROM task_attempts WHERE session_id=?", (attempt.session_id,)
                ).fetchone()
                is None
            )
    reopened = restart(service, vault)
    assert reopened.inspect_session(attempt.session_id)["launchable"]
    reopened.validate_launch(attempt.session_id, state.prefs(attempt.session_id))
    with pytest.raises(ProfileError, match="model_unconfirmed"):
        reopened.native_initialized(attempt.session_id, StartFacts(None, "default", (), None))
    assert native_started(state, attempt.session_id) == 0
    reopened.native_initialized(attempt.session_id, StartFacts(None, "default", ("sonnet",), None))
    assert native_started(state, attempt.session_id) == 1
    service.readiness = lambda: replace(ready(NEWER), can_start=False)
    with pytest.raises(ProfileError, match="provider_unavailable"):
        service.validate_launch(attempt.session_id, state.prefs(attempt.session_id))


@pytest.mark.parametrize("pruned", [False, True])
def test_null_native_routine_keeps_inherited_model_and_plan_mcp_confirmation(
    pinned: Any, monkeypatch: pytest.MonkeyPatch, pruned: bool
) -> None:
    service, state, _vault, profile_id, workspace_id, owner, ref = pinned
    service.readiness = lambda: ready(NEWER)
    service.workspaces.set_policy(ident(), workspace_id, 1, "ordinary", [])
    raw = document()
    raw["runtime"].update(model=ref, permission_mode="plan")
    raw["tools"]["mcp_configuration"] = {"servers": [_server()]}
    service.profiles.revise(profile_id, ident(), 2, raw)
    tasks, attempt = claim(service, profile_id, workspace_id)
    assert service.prepare(profile_id, ident(), attempt.session_id, 3, workspace_id, owner)[
        "launchable"
    ]
    assert state.prefs(attempt.session_id).model is None
    if pruned:
        tasks.finish(attempt.attempt_id, "succeeded", "", NOW)
        monkeypatch.setattr(task_module, "MAX_OCCURRENCES", 0)
        with state.store._connection(write=True) as connection:
            tasks.prune(connection, attempt.routine_id)
    for facts, code in (
        (None, "start_unconfirmed"),
        (StartFacts(None, "default", (), ("viki_echo",)), "plan_mode_unconfirmed"),
        (StartFacts(None, "plan", (), ()), "mcp_config_unconfirmed"),
    ):
        with pytest.raises(ProfileError, match=code):
            service.native_initialized(attempt.session_id, facts)
        assert native_started(state, attempt.session_id) == 0
    service.native_initialized(attempt.session_id, StartFacts(None, "plan", (), ("viki_echo",)))
    assert native_started(state, attempt.session_id) == 1


@pytest.mark.parametrize("check", ["validate", "initialize"])
@pytest.mark.parametrize("change", ["prefs", "document", "model"])
def test_routine_still_verifies_original_document_and_prefs_hash(
    pinned: Any, check: str, change: str
) -> None:
    service, state, _vault, profile_id, workspace_id, owner, ref = pinned
    _tasks, attempt = claim(service, profile_id, workspace_id)
    service.prepare(profile_id, ident(), attempt.session_id, 2, workspace_id, owner)
    with state.store._connection(write=True) as connection:
        if change == "prefs":
            connection.execute(
                "UPDATE sessions SET model='sonnet' WHERE session_id=?", (attempt.session_id,)
            )
        else:
            raw = document()
            raw["runtime"]["model"] = None if change == "model" else ref
            raw["instructions"] = "Changed after binding"
            plain = parse_profile_document(raw).canonical
            body = protection.protect(plain, context=_context(profile_id, 2))
            connection.execute(
                "UPDATE profile_revisions SET body=?,body_bytes=?,body_sha256=? "
                "WHERE profile_id=? AND revision=2",
                (body, len(plain), hashlib.sha256(plain).hexdigest(), profile_id),
            )
    with pytest.raises(ProfileError, match="launch_changed"):
        if check == "validate":
            service.validate_launch(attempt.session_id, state.prefs(attempt.session_id))
        else:
            service.native_initialized(
                attempt.session_id, StartFacts(None, "default", ("sonnet",), None)
            )
    assert native_started(state, attempt.session_id) == 0


@pytest.mark.parametrize("blocked", [None, "cli", "override", "storage", "preflight"])
def test_bot_reopen_uses_route_readiness_after_runtime_billing_failure(
    pinned: Any, blocked: str | None
) -> None:
    service, state, vault, profile_id, workspace_id, owner, ref = pinned
    session_id = ident()
    service.prepare(profile_id, ident(), session_id, 2, workspace_id, owner)
    service.native_initialized(session_id)
    service.transcript_exists = lambda _: True
    config = _FakeConfig(cli=Path("synthetic-cli"))
    readiness = Readiness(config=config, launch_id=ident(), git_available=lambda: True)
    readiness.observe_version(FIXTURE_VERSION)
    readiness.observe_billing([])
    readiness.observe_start_error("billing")
    if blocked == "cli":
        config.cli = None
    elif blocked == "override":
        config.claude_config_dir_set = True
    elif blocked == "preflight":
        readiness.observe_billing(["billing_env_key"])
    storage = StorageReadiness(
        state="unavailable" if blocked == "storage" else "ready", reason=None, private=True
    )
    service.readiness = lambda: readiness.capabilities(None, storage).provider["claude"]
    service.route_can_start = lambda: (
        readiness.may_spawn()
        and not readiness.billing_preflight_blocked
        and storage.state == "ready"
    )
    reopened = restart(service, vault)
    reopened.reopenable(session_id, True)
    if blocked is None:
        reopened.validate_launch(session_id, state.prefs(session_id))
        assert reopened.inspect_session(session_id)["launchable"]
        assert state.prefs(session_id).model == ref
    else:
        with pytest.raises(ProfileError, match="provider_unavailable"):
            reopened.validate_launch(session_id, state.prefs(session_id))
        assert not reopened.inspect_session(session_id)["launchable"]
