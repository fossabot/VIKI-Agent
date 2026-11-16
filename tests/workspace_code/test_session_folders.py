"""Ticked folders (`chat.workspace_ids`, ADR-050): a spawn with no session root adds each trusted
code folder the user ticked as its own `--add-dir`, asked again at every spawn."""

from __future__ import annotations

import functools
import json
import uuid
from pathlib import Path

import pytest

from tests.app.test_workspaces import COMMAND, service_at, store_at
from tests.conversations.test_session import SID, Drivers, gated, setup, watch
from tests.test_server import _open_v2, authed
from tests.workspace_code.test_roots_server import _build, _folders, running
from tests.workspace_code.test_session_roots import REVOKE, SESSION, Env
from tests.workspace_code.test_workspaces_code import trusted
from viki_agent import server
from viki_agent.app.settings import SettingsService
from viki_agent.code.session_roots import SessionRoots
from viki_agent.drivers.base import StartError
from viki_agent.drivers.claude.control import build_argv
from viki_agent.session import SessionManager
from viki_agent.state import AppState, Prefs
from viki_agent.workspaces import WorkspaceService

from replay import init_frame, out, result_frame, user_in

ROOT = Path("C:/Code/app")


@pytest.fixture
def env(tmp_path: Path) -> Env:
    store = store_at(tmp_path)
    return Env(tmp_path, store, service_at(tmp_path, store))


def _roots(env: Env, ticked: list[str]) -> SessionRoots:
    return SessionRoots(
        env.store,
        env.service,
        exists=env.known.__contains__,
        actor=env.actors.get,
        ticked=lambda: tuple(ticked),
    )


def _code(spaces: WorkspaceService, tmp_path: Path, name: str) -> tuple[str, str]:
    """A trusted code folder: its id and the root a spawn names."""
    folder = tmp_path / name
    folder.mkdir()
    workspace_id = trusted(spaces, folder)
    return workspace_id, spaces.get(workspace_id, kind="code").root.display


def _chats(tmp_path: Path, ticked: list[str]) -> tuple[SessionManager, Drivers, WorkspaceService]:
    """A manager bound to real session roots on its own store; `ticked` is the user's setting."""
    drivers = Drivers()
    manager = gated(tmp_path, drivers)
    spaces = service_at(tmp_path, manager.app_state.store)
    roots = SessionRoots(
        manager.app_state.store,
        spaces,
        exists=lambda _session_id: False,
        actor=manager.get,
        ticked=lambda: tuple(ticked),
    )
    manager.bind_roots(roots.resolve, context=roots.context, folders=roots.folders)
    return manager, drivers, spaces


# --- the spawn ----------------------------------------------------------------------------------


async def test_ticked_folder_reaches_the_argv(tmp_path: Path) -> None:
    ticked: list[str] = []
    manager, drivers, spaces = _chats(tmp_path, ticked)
    workspace_id, display = _code(spaces, tmp_path, "repo")
    ticked.append(workspace_id)
    actor = manager.create(SESSION)
    seen = watch(actor)
    assert await actor.start()
    _, launch, prefs, _ = drivers.made[0]
    assert launch.cwd is None and launch.add_dirs == (display,)
    argv = build_argv(["claude.exe"], launch, mode=prefs.mode, effort=prefs.effort)
    assert argv[argv.index(f"--session-id={SESSION}") + 1 :] == [f"--add-dir={display}"]
    started = await seen.until("session.started")
    assert started["data"]["additional_folders"] == [display]
    assert actor.started is not None and actor.started.additional_folders == [display]
    await actor.close()


async def test_a_live_chat_keeps_its_folders_and_the_next_spawn_takes_the_new_set(
    tmp_path: Path,
) -> None:
    ticked: list[str] = []
    manager, drivers, spaces = _chats(tmp_path, ticked)
    first, first_display = _code(spaces, tmp_path, "first")
    second, second_display = _code(spaces, tmp_path, "second")
    ticked.append(first)
    actor = manager.create(SESSION)
    assert await actor.start()
    ticked[:] = [second]  # the user changes the setting while the chat runs
    assert await actor.start() and await actor.send("hello")
    assert len(drivers.made) == 1  # the running process keeps the launch it was checked with
    await manager.close(SESSION)
    again = manager.open(SESSION)
    assert await again.start()
    assert [launch.add_dirs for _, launch, _, _ in drivers.made] == [
        (first_display,),
        (second_display,),
    ]
    assert again.started is not None and again.started.additional_folders == [second_display]
    await again.close()


async def test_a_model_switch_re_emits_session_started_with_the_same_folders(
    tmp_path: Path,
) -> None:
    """After a model switch, `set_model` re-emits the driver's own stored SessionStarted, not the
    actor's corrected copy, so the driver must carry `additional_folders` from the start too."""
    script = [user_in(), out(init_frame()), out(result_frame())]
    manager, spawner = setup(tmp_path, script)
    root = tmp_path / "app"  # absolute on every OS; build_argv refuses a relative add-dir

    async def no_root(_session_id: str) -> Path | None:
        return None

    async def folders(_session_id: str) -> tuple[Path, ...]:
        return (root,)

    manager.bind_roots(no_root, folders=folders)
    actor = manager.create(SID)
    seen = watch(actor)
    await actor.send("hello")
    await seen.until("turn.done")
    assert f"--add-dir={root}" in spawner.children[0].argv
    first = await seen.until("session.started")
    assert first["data"]["additional_folders"] == [str(root)]
    await actor.set_model("opus")
    second = await seen.until("session.started", count=2)
    assert second["data"]["model"] == "opus"
    assert second["data"]["additional_folders"] == [str(root)]
    await actor.close()


@pytest.mark.parametrize("kind", ["root", "profile", "bot", "read_only"])
async def test_only_an_ordinary_chat_without_a_root_asks_for_ticked_folders(
    tmp_path: Path, kind: str
) -> None:
    asked: list[str] = []

    async def folders(session_id: str) -> tuple[Path, ...]:
        asked.append(session_id)
        return (tmp_path,)

    async def root(_session_id: str) -> Path | None:
        return ROOT if kind == "root" else None

    async def allow(*_args: object) -> None:
        return None

    drivers = Drivers()
    manager = gated(tmp_path, drivers)
    manager.bind_roots(root, folders=folders)
    if kind == "profile":
        manager.bind_profiles(allow, allow, initialized=allow)
        actor = manager.attach_prepared(SESSION, profile=True)
        assert await actor.send("hello")
    else:
        actor = (
            manager.create(SESSION, root_resolver=root)
            if kind == "bot"
            else manager.create(SESSION, read_only_tools=kind == "read_only")
        )
        assert await actor.start()
    assert asked == [] and drivers.made[0][1].add_dirs == ()
    assert actor.started is not None and actor.started.additional_folders == []
    await actor.close()


def test_create_app_gives_a_new_chat_the_ticked_folders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folders = _folders(tmp_path)
    settings = SettingsService(
        AppState(tmp_path / "data" / "state.json", Prefs()).store,
        provider_defaults=Prefs,
        on_chat_defaults=lambda _prefs: None,
    )
    settings.patch(str(uuid.uuid4()), 0, {"chat.workspace_ids": json.dumps([folders.code_id])})
    # `_build` with the user's settings: the one argument it does not pass itself.
    monkeypatch.setattr(
        server, "create_app", functools.partial(server.create_app, settings=settings)
    )
    built = _build(folders)
    display = built.spaces.get(folders.code_id, kind="code").root.display
    with running(built) as client, authed(client) as ws:
        snapshot, _ = _open_v2(ws, SESSION)
    assert [launch.add_dirs for launch in built.launches] == [(display,)]
    assert snapshot["data"]["state"]["info"]["additional_folders"] == [display]


# --- which folders ------------------------------------------------------------------------------


async def test_folders_are_the_ticked_trusted_code_folders(env: Env) -> None:
    ticked = sorted((env.code(), env.code("other")))
    assert await _roots(env, ticked).folders(SESSION) == tuple(
        Path(env.display(workspace_id)) for workspace_id in ticked
    )


@pytest.mark.parametrize("bad", ["untrusted", "protected", "unknown", "missing", "vault"])
async def test_folders_skip_an_id_that_is_no_trusted_code_folder(env: Env, bad: str) -> None:
    good = env.code()
    if bad == "untrusted":
        bad_id = env.code("bad", trust=False)
    elif bad == "unknown":
        bad_id = str(uuid.uuid4())
    elif bad == "vault":
        notes = env.tmp_path / "notes"
        notes.mkdir()
        bad_id = env.service.register_existing(COMMAND, str(notes))[0].workspace_id
    else:
        bad_id = env.code("bad")
        if bad == "protected":
            env.service.set_policy(REVOKE, bad_id, 2, "protected", [])
        else:
            (env.tmp_path / "bad").rmdir()
    assert await _roots(env, [bad_id, good]).folders(SESSION) == (Path(env.display(good)),)


async def test_a_session_with_a_root_adds_no_folders(env: Env) -> None:
    workspace_id, ticked = env.code(), env.code("other")
    roots = _roots(env, [ticked])
    await roots.prepare(SESSION, workspace_id, None)
    assert await roots.folders(SESSION) == ()


async def test_a_launch_in_a_code_folder_adds_no_folders(env: Env) -> None:
    active, ticked = env.code(), env.code("other")
    env.service.select(COMMAND, active)
    assert await _roots(env, [ticked]).folders(SESSION) == ()
    assert env.store.get_session_root(SESSION) is None  # the launch decides, not a row


# --- a removed folder -----------------------------------------------------------------------------


async def test_a_session_root_on_a_removed_folder_refuses_with_the_removal_text(env: Env) -> None:
    workspace_id = env.code()
    roots = _roots(env, [])
    await roots.prepare(SESSION, workspace_id, None)
    env.service.remove(workspace_id)
    with pytest.raises(StartError) as refusal:
        await roots.resolve(SESSION)
    assert (refusal.value.code, refusal.value.message) == (
        "bad_request",
        "This session's folder is not trusted, or was removed from VIKI Agent, so it can't "
        "start. Trust it in Settings and send again, or start a new chat.",
    )
