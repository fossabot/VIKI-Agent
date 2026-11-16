from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from tests.app.test_settings import sqlite_connection
from tests.conversations.test_session import Drivers, gated
from tests.llm.test_connections import FORM
from viki_agent.app.settings import SettingsService
from viki_agent.drivers.base import StartError
from viki_agent.drivers.claude.driver import DriverConfig
from viki_agent.llm.connections import Connections, parse_connection
from viki_agent.llm.gateway import Gateway
from viki_agent.protection import ProtectionError
from viki_agent.session import SessionManager
from viki_agent.state import AppState, Prefs
from viki_agent.storage import StateError


def _manager(state: AppState, settings: SettingsService, folder: Path) -> SessionManager:
    manager = SessionManager(
        config=DriverConfig(cli=("synthetic.exe",), vault=folder, env={}),
        state=state,
        claude_home=folder / "synthetic-home",
    )
    manager.bind_model_connections(Connections(settings), Gateway())
    return manager


@pytest.mark.parametrize("failure", ["newer", "corrupt"])
def test_unreadable_defaults_refuse_implicit_rows_but_keep_explicit_routes(
    tmp_path: Path, failure: str
) -> None:
    path = tmp_path / "state.json"
    state = AppState(path, Prefs(model="sonnet"))
    settings = SettingsService(
        state.store,
        provider_defaults=lambda: Prefs(model="sonnet"),
        on_chat_defaults=state.set_defaults,
    )
    connection = parse_connection(FORM)
    Connections(settings).write(0, [connection])
    settings.patch(str(uuid.uuid4()), 1, {"chat.default_model": connection.ref})
    known = str(uuid.uuid4())
    state.remember(known, model="sonnet")
    with sqlite_connection(state.store) as sql:
        if failure == "newer":
            sql.execute("UPDATE settings SET version=2 WHERE scope='app'")
        else:
            sql.execute("UPDATE settings SET document='not json' WHERE scope='app'")
        sql.commit()
    state.set_defaults(settings.chat_defaults())
    manager = _manager(state, settings, tmp_path)
    implicit = str(uuid.uuid4())
    with pytest.raises(StateError) as blocked:
        manager.create(implicit)
    assert blocked.value.code == "model_defaults_unavailable"
    assert not state.ours(implicit)
    assert manager.get(implicit) is None
    effort_only = str(uuid.uuid4())
    with pytest.raises(StateError):
        state.remember(effort_only, effort="low")
    assert not state.ours(effort_only)
    with pytest.raises(StateError) as unavailable:
        state.remember(effort_only, new_session=True, effort="low")
    assert unavailable.value.code == "model_defaults_unavailable"
    explicit = str(uuid.uuid4())
    state.remember(explicit, model="sonnet")
    manager.create(explicit)
    assert state.prefs(explicit).model == "sonnet"
    state.remember(known, effort="low")
    assert state.prefs(known).model == "sonnet"
    restarted = AppState(path, settings.chat_defaults())
    restart_settings = SettingsService(
        restarted.store, provider_defaults=Prefs, on_chat_defaults=restarted.set_defaults
    )
    restart_manager = _manager(restarted, restart_settings, tmp_path)
    with pytest.raises(StateError):
        restart_manager.create(implicit)
    assert not restarted.ours(implicit)


async def test_unreadable_defaults_guard_resume_fork_and_explicit_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = AppState(tmp_path / "state.json", Prefs(model="sonnet"))
    settings = SettingsService(
        state.store,
        provider_defaults=lambda: Prefs(model="sonnet"),
        on_chat_defaults=state.set_defaults,
    )
    settings.patch(str(uuid.uuid4()), 0, {"chat.default_model": "sonnet"})
    with sqlite_connection(state.store) as sql:
        sql.execute("UPDATE settings SET version=2 WHERE scope='app'")
        sql.commit()
    manager = _manager(state, settings, tmp_path)
    source = str(uuid.uuid4())
    actor = manager.open(source)
    attempts = []

    def capture(*args):
        attempts.append(args)
        raise StartError("bad_request", "Synthetic admission probe")

    monkeypatch.setattr(actor, "_make_driver", capture)
    assert not actor.set_effort("low")
    assert not state.ours(actor.session_id)
    assert not await actor.start()
    assert not attempts
    fork_id = str(uuid.uuid4())
    with pytest.raises(StateError, match="Saved model settings"):
        manager.fork(source, fork_id)
    assert manager.get(fork_id) is None
    assert not state.ours(fork_id)
    # An explicit choice on a held actor must not first inherit unavailable defaults.
    await actor.set_model("sonnet")
    assert state.ours(source)
    assert state.prefs(source).model == "sonnet"
    manager.fork(source, fork_id)
    assert state.prefs(fork_id).model == "sonnet"


async def test_endpoint_default_requires_explicit_choice_before_importing_native_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = AppState(tmp_path / "state.json", Prefs(model="sonnet"))
    settings = SettingsService(
        state.store,
        provider_defaults=lambda: Prefs(model="sonnet"),
        on_chat_defaults=state.set_defaults,
    )
    connection = parse_connection(FORM)
    Connections(settings).write(0, [connection])
    settings.patch(str(uuid.uuid4()), 1, {"chat.default_model": connection.ref})
    manager = _manager(state, settings, tmp_path)
    actor = manager.open(str(uuid.uuid4()))
    attempts = []

    def capture(*args):
        attempts.append(args)
        raise StartError("bad_request", "Synthetic admission probe")

    monkeypatch.setattr(actor, "_make_driver", capture)
    assert not await actor.start()
    assert not attempts
    assert not actor.set_effort("low")
    assert not state.ours(actor.session_id)
    with pytest.raises(StateError) as refused:
        manager.fork(actor.session_id, str(uuid.uuid4()))
    assert refused.value.code == "model_choice_required"
    await actor.set_model(connection.ref)
    assert state.launch_prefs(actor.session_id).model == connection.ref
    new_chat = manager.create(str(uuid.uuid4()))
    assert state.launch_prefs(new_chat.session_id).model == connection.ref
    prepared_id = str(uuid.uuid4())
    state.remember(prepared_id, new_session=True, effort="low")
    assert state.launch_prefs(prepared_id).model == connection.ref


def _preview_manager(tmp_path: Path):
    drivers = Drivers()
    manager = gated(tmp_path, drivers)
    state = manager.app_state
    settings = SettingsService(
        state.store, provider_defaults=Prefs, on_chat_defaults=state.set_defaults
    )
    first = parse_connection({**FORM, "name": "First", "model": "model-a"})
    second = parse_connection({**FORM, "name": "Second", "model": "model-b"})
    connections = Connections(settings)
    connections.write(0, [first, second])
    manager.bind_model_connections(connections, Gateway())
    return state, settings, first, second, manager, drivers


@pytest.mark.parametrize("custom_preview", [False, True])
def test_changed_default_refuses_preview_before_registering_a_new_chat(
    tmp_path: Path,
    custom_preview: bool,
) -> None:
    state, settings, first, second, manager, drivers = _preview_manager(tmp_path)
    if custom_preview:
        settings.patch(str(uuid.uuid4()), 1, {"chat.default_model": first.ref})
    session_id = str(uuid.uuid4())
    preview = state.prefs(session_id).model
    assert preview == (first.ref if custom_preview else None)
    revision = 2 if custom_preview else 1
    settings.patch(str(uuid.uuid4()), revision, {"chat.default_model": second.ref})
    with pytest.raises(StateError) as failure:
        manager.create(session_id, expected_model=preview)
    assert failure.value.code == "model_preview_changed"
    assert not state.ours(session_id)
    assert manager.get(session_id) is None
    assert not drivers.made


async def test_matching_preview_pins_first_model_across_later_default_changes(
    tmp_path: Path,
) -> None:
    state, settings, first, second, manager, drivers = _preview_manager(tmp_path)
    settings.patch(str(uuid.uuid4()), 1, {"chat.default_model": first.ref})
    session_id = str(uuid.uuid4())
    actor = manager.create(session_id, expected_model=state.prefs(session_id).model)
    settings.patch(str(uuid.uuid4()), 2, {"chat.default_model": second.ref})
    try:
        actor.register(expected_model=first.ref)
        assert await actor.start()
        assert state.launch_prefs(session_id).model == first.ref
        assert drivers.made[0][2].model == first.ref
    finally:
        await manager.close(session_id)


@pytest.mark.parametrize("native_choice", [False, True])
async def test_pinned_explicit_model_is_usable_after_global_default_changes(
    tmp_path: Path,
    native_choice: bool,
) -> None:
    state, settings, first, second, manager, drivers = _preview_manager(tmp_path)
    chosen = "sonnet" if native_choice else first.ref
    session_id = str(uuid.uuid4())
    state.remember(session_id, model=chosen)
    settings.patch(str(uuid.uuid4()), 1, {"chat.default_model": second.ref})
    actor = manager.create(session_id, expected_model=chosen)
    try:
        assert await actor.start()
        assert state.launch_prefs(session_id).model == chosen
        assert drivers.made[0][2].model == chosen
    finally:
        await manager.close(session_id)


def test_registered_actor_refuses_a_different_model_preview_before_start(tmp_path: Path) -> None:
    state, settings, first, second, manager, drivers = _preview_manager(tmp_path)
    settings.patch(str(uuid.uuid4()), 1, {"chat.default_model": first.ref})
    session_id = str(uuid.uuid4())
    actor = manager.create(session_id)
    with pytest.raises(StateError) as failure:
        actor.register(expected_model=second.ref)
    assert failure.value.code == "model_preview_changed"
    assert manager.get(session_id) is actor
    assert state.launch_prefs(session_id).model == first.ref
    assert not actor.live and not drivers.made


def test_omitted_model_preview_keeps_the_legacy_registration_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state, _, _, _, manager, _ = _preview_manager(tmp_path)
    remember = state.remember
    calls = []

    def record(session_id, **changes):
        calls.append((session_id, changes))
        return remember(session_id, **changes)

    monkeypatch.setattr(state, "remember", record)
    session_id = str(uuid.uuid4())
    manager.create(session_id)
    assert calls == [(session_id, {})]


@pytest.mark.parametrize("expected_model", [None, "sonnet"])
@pytest.mark.parametrize(
    "error", [StateError("unavailable"), StateError("corrupt"), ProtectionError()]
)
def test_guarded_registration_error_does_not_keep_a_fresh_actor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, expected_model: str | None, error: Exception
) -> None:
    state, _, _, _, manager, drivers = _preview_manager(tmp_path)
    session_id = str(uuid.uuid4())

    def unavailable(*args, **kwargs):
        raise error

    monkeypatch.setattr(state, "remember", unavailable)
    with pytest.raises(type(error)) as failure:
        manager.create(session_id, expected_model=expected_model)
    assert failure.value is error
    assert manager.get(session_id) is None
    assert not state.ours(session_id)
    assert not drivers.made


@pytest.mark.parametrize("error", [StateError("unavailable"), ProtectionError()])
def test_generic_registration_error_without_preview_keeps_legacy_actor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    state, _, _, _, manager, drivers = _preview_manager(tmp_path)
    session_id = str(uuid.uuid4())

    def unavailable(*args, **kwargs):
        raise error

    monkeypatch.setattr(state, "remember", unavailable)
    with pytest.raises(type(error)):
        manager.create(session_id)
    actor = manager.get(session_id)
    assert actor is not None and not actor.live
    assert not state.ours(session_id) and not drivers.made


@pytest.mark.parametrize("error", [StateError("unavailable"), ProtectionError()])
def test_generic_preview_error_keeps_an_already_registered_actor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    state, _, _, _, manager, drivers = _preview_manager(tmp_path)
    session_id = str(uuid.uuid4())
    actor = manager.create(session_id, expected_model=None)

    def unavailable(*args, **kwargs):
        raise error

    monkeypatch.setattr(state, "launch_prefs", unavailable)
    with pytest.raises(type(error)):
        actor.register(expected_model=None)
    assert manager.get(session_id) is actor
    assert state.ours(session_id)
    assert not actor.live and not drivers.made
