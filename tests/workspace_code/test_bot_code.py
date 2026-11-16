"""Bot code copies: the spawn recheck and list descriptions, on a real store and services."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import sys
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from tests.app.test_workspaces import service_at
from tests.messaging.test_bots import _attempt, _bind_chat, _bot, _id, _profile, _routine
from tests.messaging.test_bots import service as service
from tests.workspace_code.test_code_git import _commit as _git_commit
from tests.workspace_code.test_code_git import _repo, needs_git
from tests.workspace_code.test_workspaces_code import trusted
from viki_agent import winproc
from viki_agent.code.bot_code import (
    MSG_FAILED,
    MSG_MISSING,
    MSG_SETTINGS_UNREADABLE,
    MSG_THREAD_BUSY,
    MSG_UNAVAILABLE,
    MSG_UNGRANTED,
    MSG_UNTRUSTED,
    BotCode,
)
from viki_agent.code.code_api import GIT_SETTINGS_CHANGED
from viki_agent.code.code_files import CodeFiles
from viki_agent.code.code_git import CodeGit
from viki_agent.code.session_roots import LiveActor, SessionRoots
from viki_agent.code.worktrees import WorktreeService, worktree_area
from viki_agent.conversations.session_metadata import SessionMetadataService
from viki_agent.drivers.base import StartError
from viki_agent.events import CodeCopy
from viki_agent.messaging.bot_threads import BotThreadService
from viki_agent.messaging.bots import BotService
from viki_agent.protection import create_private_directory
from viki_agent.session import now_iso
from viki_agent.vaults.vault_mutation import MutationCoordinator
from viki_agent.vaults.workspace_mutation import WorkspaceMutationResolver

BRANCH = "viki/bot-20261111-abcdef"
STATES = ("creating", "ready", "removing", "kept", "removed")


@dataclass
class FakeActor:
    live: bool = False


class Rig:
    """One ordinary vault, one trusted code folder and a granted Bot with its main chat."""

    def __init__(self, service: BotService, tmp_path: Path) -> None:
        self.service, self.store, self.workspaces = service, service.store, service.workspaces
        self.tmp_path = tmp_path
        self.actors: dict[str, FakeActor] = {}
        self.roots = SessionRoots(
            self.store, self.workspaces, exists=lambda _sid: False, actor=self.actors.get
        )
        self.threads = BotThreadService(self.store, self.workspaces)
        data = self.store.path.parent
        self.area = worktree_area(data)
        create_private_directory(self.area)
        settings = data / "bot-memory-settings"
        settings.mkdir()
        self.settings = settings / ("a" * 64 + ".json")
        self.settings.write_text("{}", encoding="utf-8")
        self.code = self.bot_code(self.actors.get)
        self.folder = self.code_folder("repo")
        self.bot = _bot(service, profile_id=_profile(service, "Synthetic Engineer"))
        self.vault = self.bot["workspace_id"]
        self.grant(self.bot, self.folder)
        self.main = self.session(self.bot, self.bot["chat_session_id"])

    def bot_code(self, actor: Callable[[str], LiveActor | None]) -> BotCode:
        # No WorktreeService: every copy these tests resolve is bound by `copy`, and a copy to
        # make refuses (making one is test_bot_code_create's).
        return BotCode(
            self.store,
            self.workspaces,
            self.roots,
            self.service.profiles,
            self.threads,
            actor=actor,
            area=self.area,
        )

    def code_folder(self, name: str) -> str:
        root = self.tmp_path / name
        root.mkdir()
        return trusted(self.workspaces, root)

    def other_bot(self, name: str) -> dict[str, Any]:
        return _bot(self.service, profile_id=_profile(self.service, name), workspace_id=self.vault)

    def grant(self, bot: dict[str, Any], folder: str | None) -> None:
        with self.store._connection(write=True) as connection:
            connection.execute("DELETE FROM bot_code_folders WHERE bot_id=?", (bot["bot_id"],))
            if folder is not None:
                connection.execute(
                    "INSERT INTO bot_code_folders VALUES (?,?,?)",
                    (bot["bot_id"], folder, now_iso()),
                )

    def session(
        self, bot: dict[str, Any], session_id: str | None = None, origin: str = "interactive"
    ) -> str:
        session_id = session_id or _id()
        _bind_chat(self.service, {**bot, "chat_session_id": session_id})
        with self.store._connection(write=True) as connection:
            connection.execute(
                "UPDATE session_profile_bindings SET launch_origin=? WHERE session_id=?",
                (origin, session_id),
            )
            connection.execute(
                "INSERT INTO session_bot_memory VALUES (?,?)", (session_id, bot["bot_id"])
            )
        return session_id

    def copy(
        self,
        *,
        session: str | None = None,
        thread: str | None = None,
        bot: dict[str, Any] | None = None,
        state: str = "ready",
        make: bool = True,
        worktree_id: str | None = None,
    ) -> str:
        worktree_id = worktree_id or _id()
        stamp = now_iso()
        self.store.insert_bot_copy(
            {
                "worktree_id": worktree_id,
                "workspace_id": self.folder,
                "path": str(self.area / worktree_id),
                "branch": BRANCH,
                "base_oid": "a" * 40,
                "state": "creating",
                "created_at": stamp,
                "updated_at": stamp,
            },
            {
                "worktree_id": worktree_id,
                "workspace_id": self.folder,
                "bot_id": (bot or self.bot)["bot_id"],
                "session_id": session,
                "thread_id": thread,
                "state": "bound",
                "created_at": stamp,
            },
            limit=32,
        )
        if state != "creating":
            self.store.update_worktree_state(worktree_id, "creating", state, stamp)
        if make:
            (self.area / worktree_id).mkdir()
        return worktree_id

    def thread(self, members: list[tuple[dict[str, Any], str]], title: str = "Review") -> str:
        thread_id = _id()
        self.threads.save(
            self.vault,
            thread_id,
            0,
            {
                "title": title,
                "phase": "idle",
                "members": [
                    {
                        "key": key,
                        "bot_id": bot["bot_id"],
                        "profile_id": bot["profile_id"],
                        "session_id": session_id,
                        "enabled": True,
                    }
                    for (bot, session_id), key in zip(
                        members, ("chief", "checker", "ops"), strict=False
                    )
                ],
                "messages": [],
            },
        )
        return thread_id

    def expected(self, worktree_id: str) -> CodeCopy:
        name = self.workspaces.get(self.folder, kind="code").name
        return CodeCopy(path=str(self.area / worktree_id), folder=name, branch=BRANCH)

    def sql(self, statement: str, *values: object) -> None:
        with self.store._connection(write=True) as connection:
            connection.execute(statement, values)

    async def resolve(self, session_id: str, settings: str | None = None) -> CodeCopy | None:
        return await self.code.resolve(
            session_id, str(self.settings) if settings is None else settings
        )

    async def refused(self, session_id: str, message: str, settings: str | None = None) -> None:
        with pytest.raises(StartError) as refusal:
            await self.resolve(session_id, settings)
        assert (refusal.value.code, refusal.value.message) == ("bad_request", message)


@pytest.fixture
def rig(service: BotService, tmp_path: Path) -> Rig:
    return Rig(service, tmp_path)


def _scope(rig: Rig) -> str:
    key = rig.workspaces.get(rig.vault).root.key
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _link(kind: str, target: Path, link: Path) -> None:
    if kind == "junction":
        if sys.platform != "win32":
            pytest.skip("junctions are a Windows alias")
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
        return
    try:
        os.symlink(target, link, target_is_directory=True)
    except OSError:
        pytest.skip("this account cannot create symbolic links")


# --- the fixed texts --------------------------------------------------------------------------


def test_refusal_texts_are_the_fixed_sentences() -> None:
    assert MSG_UNGRANTED == (
        "This chat works in a copy of a code folder that is no longer this Bot's code folder, "
        "so it can't start. Give the folder back in Bot settings, or start a new conversation."
    )
    assert MSG_UNTRUSTED == (
        "This chat's code folder is no longer trusted or was removed from VIKI Agent, so it "
        "can't start. Trust it again in Settings, or start a new conversation."
    )
    assert MSG_MISSING == (
        "This chat's code folder isn't where it was registered, so it can't start. Put the "
        "folder back, then send again."
    )
    assert MSG_UNAVAILABLE == (
        "This chat's code copy was removed or isn't ready, so it can't start. Start a new "
        "conversation."
    )
    assert MSG_THREAD_BUSY == (
        "Another Bot in this Bot thread is using its code copy now, so this turn can't start "
        "until that one finishes."
    )


# --- resolving a bound copy -------------------------------------------------------------------


async def test_main_and_task_sessions_each_resolve_their_own_copy(rig: Rig) -> None:
    task = rig.session(rig.bot)
    main_copy, task_copy = rig.copy(session=rig.main), rig.copy(session=task)
    assert await rig.resolve(rig.main) == rig.expected(main_copy)
    assert await rig.resolve(task) == rig.expected(task_copy)
    assert rig.expected(main_copy).path != rig.expected(task_copy).path
    # Resume rechecks the same facts and keeps the same copy.
    assert await rig.resolve(rig.main) == rig.expected(main_copy)


@pytest.mark.parametrize(
    "case", ["granted", "unknown", "routine", "ungranted", "not_bot", "no_binding"]
)
async def test_sessions_without_a_bound_copy_keep_their_native_launch(rig: Rig, case: str) -> None:
    if case == "not_bot":
        session_id = _id()
        _bind_chat(rig.service, {**rig.bot, "chat_session_id": session_id})
    elif case == "no_binding":
        session_id = _id()
    else:
        origin = case if case in {"unknown", "routine"} else "interactive"
        session_id = rig.session(rig.bot, origin=origin)
        SessionMetadataService(rig.store, _scope(rig)).patch(session_id, 0, {"title": "Synthetic"})
        if case == "ungranted":
            rig.grant(rig.bot, None)
    before = rig.store.profile_launch_origin(session_id)
    assert await rig.resolve(session_id) is None
    assert await rig.resolve(session_id, "") is None
    # A read only: nothing promotes or rewrites the stored provenance.
    assert rig.store.profile_launch_origin(session_id) == before


@pytest.mark.parametrize(
    "case", ["unknown", "routine_pruned", "routine_retained", "routine_never_started", "unbound"]
)
async def test_bound_copy_needs_durable_interactive_provenance(rig: Rig, case: str) -> None:
    if case == "routine_retained":
        # Retained routine evidence outranks an inconsistent interactive marker.
        attempt = _attempt(rig.service, _routine(rig.service, rig.bot))
        session_id = rig.session(rig.bot, attempt.session_id, origin="interactive")
    elif case == "unbound":
        session_id = _id()  # a copy row naming a session with no profile binding
    else:
        session_id = rig.session(rig.bot, origin="unknown" if case == "unknown" else "routine")
        if case == "routine_never_started":
            rig.sql(
                "UPDATE session_profile_bindings SET native_started=0 WHERE session_id=?",
                session_id,
            )
    worktree_id = rig.copy(session=session_id)
    await rig.refused(session_id, MSG_UNAVAILABLE)
    assert rig.store.copy_states((worktree_id,))[worktree_id]["state"] == "bound"


@pytest.mark.parametrize("change", ["removed", "switched"])
async def test_grant_removal_or_switch_refuses_without_fallback(rig: Rig, change: str) -> None:
    worktree_id = rig.copy(session=rig.main)
    assert await rig.resolve(rig.main) == rig.expected(worktree_id)
    rig.grant(rig.bot, None if change == "removed" else rig.code_folder("other"))
    await rig.refused(rig.main, MSG_UNGRANTED)
    rig.grant(rig.bot, rig.folder)  # giving the folder back continues the same copy
    assert await rig.resolve(rig.main) == rig.expected(worktree_id)


@pytest.mark.parametrize("change", ["untrusted", "unregistered", "moved", "replaced"])
async def test_folder_trust_and_identity_are_rechecked_at_each_spawn(rig: Rig, change: str) -> None:
    rig.copy(session=rig.main)
    assert await rig.resolve(rig.main) is not None
    root = rig.tmp_path / "repo"
    if change == "untrusted":
        rig.sql("UPDATE workspaces SET policy='existing_readonly' WHERE workspace_id=?", rig.folder)
    elif change == "unregistered":
        # Removal releases the copy and its grant; the chat still never runs without it.
        rig.store.update_workspace(rig.folder, lambda _current, _rows: None)
    elif change == "moved":
        root.rename(rig.tmp_path / "repo-moved")
    else:
        root.rename(rig.tmp_path / "repo-moved")
        root.mkdir()  # same path, another folder
    message = MSG_UNTRUSTED if change in {"untrusted", "unregistered"} else MSG_MISSING
    await rig.refused(rig.main, message)


@pytest.mark.parametrize("change", ["vault_readonly", "bot_removed", "another_bots_copy"])
async def test_bot_and_ordinary_vault_are_rechecked(rig: Rig, change: str) -> None:
    owner = rig.other_bot("Another") if change == "another_bots_copy" else rig.bot
    rig.grant(owner, rig.folder)  # a copy is bound only under its Bot's grant
    rig.copy(session=rig.main, bot=owner)
    if change == "vault_readonly":
        rig.sql("UPDATE workspaces SET policy='existing_readonly' WHERE workspace_id=?", rig.vault)
    elif change == "bot_removed":
        rig.grant(rig.bot, None)
        rig.sql("DELETE FROM bots WHERE bot_id=?", rig.bot["bot_id"])
    await rig.refused(rig.main, MSG_UNAVAILABLE)


async def test_a_session_prepared_outside_an_ordinary_vault_gets_no_copy(rig: Rig) -> None:
    # A Bot-bound session prepared in a protected vault reaches the resolver, and a copy bound
    # before the policy changed is neither refused nor handed over: that session has none.
    worktree_id = rig.copy(session=rig.main)
    rig.sql("UPDATE workspaces SET policy='protected' WHERE workspace_id=?", rig.vault)
    rig.sql("UPDATE session_profile_bindings SET policy='protected' WHERE session_id=?", rig.main)
    before = rig.store.copy_states((worktree_id,))
    assert await rig.resolve(rig.main) is None
    assert rig.store.copy_states((worktree_id,)) == before
    assert before[worktree_id]["state"] == "bound"


@pytest.mark.parametrize(
    "case",
    [
        "creating",
        "removing",
        "removed",
        "kept",
        "no_folder",
        "outside_area",
        "other_folder",
        "upper_case_id",
        "climbing_id",
    ],
)
async def test_only_a_ready_copy_in_its_area_folder_resolves(rig: Rig, case: str) -> None:
    state = case if case in {"creating", "removing", "removed", "kept"} else "ready"
    if state == "removing":
        worktree_id = rig.copy(session=rig.main)
        rig.store.update_worktree_state(worktree_id, "ready", "removing", now_iso())
    elif state == "removed":
        worktree_id = rig.copy(session=rig.main, make=False)
        rig.store.update_worktree_state(worktree_id, "ready", "removed", now_iso())
    elif case in {"upper_case_id", "climbing_id"}:
        # A stored id that is not a canonical UUID never names a folder, even one that exists.
        odd = _id().upper() if case == "upper_case_id" else "../" + "x" * 33
        worktree_id = rig.copy(session=rig.main, worktree_id=odd)
    else:
        worktree_id = rig.copy(session=rig.main, state=state, make=case != "no_folder")
    if case == "outside_area":
        outside = rig.tmp_path / "elsewhere" / worktree_id
        outside.mkdir(parents=True)
        rig.sql("UPDATE worktrees SET path=? WHERE worktree_id=?", str(outside), worktree_id)
    elif case == "other_folder":
        other = rig.code_folder("other")
        rig.sql("UPDATE worktrees SET workspace_id=? WHERE worktree_id=?", other, worktree_id)
    await rig.refused(rig.main, MSG_UNAVAILABLE)


@pytest.mark.parametrize("kind", ["symlink", "junction"])
@pytest.mark.parametrize("where", ["copy", "area"])
async def test_a_linked_copy_or_area_refuses(rig: Rig, kind: str, where: str) -> None:
    worktree_id = rig.copy(session=rig.main, make=where == "area")
    if where == "copy":
        target = rig.tmp_path / "repo"  # the original folder itself, behind a link
        _link(kind, target, rig.area / worktree_id)
    else:
        moved = rig.tmp_path / "real-area"
        rig.area.rename(moved)
        _link(kind, moved, rig.area)
        assert (rig.area / worktree_id).is_dir()
    await rig.refused(rig.main, MSG_UNAVAILABLE)


@pytest.mark.parametrize("settings", ["", "missing"])
async def test_missing_memory_settings_refuses_a_bound_copy(rig: Rig, settings: str) -> None:
    value = "" if settings == "" else str(rig.settings.with_name("b" * 64 + ".json"))
    task = rig.session(rig.bot)
    assert await rig.resolve(task, value) is None  # no copy: nothing to refuse
    rig.copy(session=rig.main)
    await rig.refused(rig.main, MSG_UNAVAILABLE, value)


async def test_a_removal_block_refuses_the_spawn(rig: Rig) -> None:
    worktree_id = rig.copy(session=rig.main)
    rig.roots.block(worktree_id)
    await rig.refused(rig.main, MSG_UNAVAILABLE)
    rig.roots.unblock(worktree_id)
    assert await rig.resolve(rig.main) == rig.expected(worktree_id)


async def test_a_session_whose_copy_was_released_never_runs_without_it(rig: Rig) -> None:
    worktree_id = rig.copy(session=rig.main)
    rig.sql("UPDATE bot_code_copies SET state='released' WHERE worktree_id=?", worktree_id)
    await rig.refused(rig.main, MSG_UNAVAILABLE)
    assert rig.store.finish_release(worktree_id)  # nor once its release finished
    await rig.refused(rig.main, MSG_UNAVAILABLE)
    fresh = rig.copy(session=rig.main)  # until the session is bound to a copy again
    assert await rig.resolve(rig.main) == rig.expected(fresh)


# --- shared Bot-thread copies -----------------------------------------------------------------


async def test_granted_thread_members_share_one_copy_and_others_get_none(rig: Rig) -> None:
    checker, writer = rig.other_bot("Checker"), rig.other_bot("Writer")
    rig.grant(checker, rig.folder)
    first, second, third = (
        rig.session(rig.bot),
        rig.session(checker),
        rig.session(writer),
    )
    thread_id = rig.thread([(rig.bot, first), (checker, second), (writer, third)])
    worktree_id = rig.copy(thread=thread_id)
    assert await rig.resolve(first) == rig.expected(worktree_id)
    assert await rig.resolve(second) == rig.expected(worktree_id)
    assert await rig.resolve(third) is None  # ungranted members get none
    other = rig.code_folder("other")
    rig.grant(checker, other)
    # The thread has no copy of that folder yet, so this turn makes one: with nothing here to
    # make it, it refuses and reserves nothing.
    await rig.refused(second, MSG_FAILED)
    assert rig.store.bound_thread_copy(thread_id, other) is None
    assert [row["worktree_id"] for row in rig.store.worktrees_in_states(STATES)] == [worktree_id]
    # Its own copy beside the thread's is a contradiction: neither one is picked.
    rig.copy(session=first)
    await rig.refused(first, MSG_UNAVAILABLE)


async def test_another_live_member_refuses_a_thread_turn(rig: Rig) -> None:
    checker = rig.other_bot("Checker")
    rig.grant(checker, rig.folder)
    first, second = rig.session(rig.bot), rig.session(checker)
    worktree_id = rig.copy(thread=rig.thread([(rig.bot, first), (checker, second)]))
    rig.actors[first] = FakeActor(live=True)  # its own process does not count
    assert await rig.resolve(first) == rig.expected(worktree_id)
    rig.actors[second] = FakeActor(live=True)
    await rig.refused(first, MSG_THREAD_BUSY)
    # A session's own copy is never refused for another chat's process.
    own = rig.copy(session=rig.main)
    assert await rig.resolve(rig.main) == rig.expected(own)


async def test_parallel_windows_start_one_turn_in_a_shared_copy(rig: Rig) -> None:
    checker = rig.other_bot("Checker")
    rig.grant(checker, rig.folder)
    first, second = rig.session(rig.bot), rig.session(checker)
    rig.copy(thread=rig.thread([(rig.bot, first), (checker, second)]))

    async def start(session_id: str) -> CodeCopy | StartError:
        try:
            copy = await rig.resolve(session_id)
        except StartError as error:
            return error
        # The actor goes live synchronously after the resolver returns, as SessionActor does.
        rig.actors[session_id] = FakeActor(live=True)
        assert copy is not None
        return copy

    results = await asyncio.gather(start(first), start(second))
    assert sum(isinstance(result, CodeCopy) for result in results) == 1
    refusal = next(result for result in results if isinstance(result, StartError))
    assert refusal.message == MSG_THREAD_BUSY


async def test_the_final_checks_have_no_await_after_them(rig: Rig) -> None:
    checker = rig.other_bot("Checker")
    rig.grant(checker, rig.folder)
    first, second = rig.session(rig.bot), rig.session(checker)
    worktree_id = rig.copy(thread=rig.thread([(rig.bot, first), (checker, second)]))
    order: list[str] = []
    blocked = rig.roots.blocked

    def mark(name: str) -> None:
        order.append(name)
        asyncio.get_running_loop().call_soon(order.append, f"after {name}")

    def checked_block(candidate: str) -> bool:
        mark("blocked")
        return blocked(candidate)

    def checked_actor(session_id: str) -> FakeActor | None:
        mark("live")
        return rig.actors.get(session_id)

    rig.roots.blocked = checked_block  # type: ignore[method-assign]
    rig.code = rig.bot_code(checked_actor)
    assert await rig.resolve(first) == rig.expected(worktree_id)
    # Neither final check let the loop run anything before the copy came back.
    assert order == ["blocked", "live"]


async def test_refusals_log_fixed_codes_only(rig: Rig, caplog: pytest.LogCaptureFixture) -> None:
    worktree_id = rig.copy(session=rig.main)
    caplog.set_level(logging.DEBUG, logger="viki_agent.code.bot_code")
    rig.grant(rig.bot, None)
    await rig.refused(rig.main, MSG_UNGRANTED)
    rig.grant(rig.bot, rig.folder)
    rig.roots.block(worktree_id)
    await rig.refused(rig.main, MSG_UNAVAILABLE)
    records = [r for r in caplog.records if r.name == "viki_agent.code.bot_code"]
    assert [r.getMessage() for r in records] == [
        "bot code copy refused: ungranted",
        "bot code copy refused: unavailable",
    ]
    secrets = (rig.main, worktree_id, rig.bot["bot_id"], str(rig.area), str(rig.tmp_path), BRANCH)
    for record in records:
        assert not any(secret in record.getMessage() for secret in secrets)
        assert record.exc_info is None


# --- a first send over a real repository ------------------------------------------------------


@pytest.fixture
def git_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A synthetic host for real Git: no developer `GIT_*` variable, and an empty global and no
    system config for every Git child."""
    for name in list(os.environ):
        if name.upper().startswith("GIT_"):
            monkeypatch.delenv(name)
    empty = tmp_path / "global-config"
    empty.write_bytes(b"")
    host = {"GIT_CONFIG_GLOBAL": str(empty), "GIT_CONFIG_SYSTEM": str(empty)}
    host["GIT_CONFIG_NOSYSTEM"] = "1"
    real = winproc.run

    def run(*args: Any, **kwargs: Any) -> Any:
        kwargs["env"] = {**os.environ, **host, **(kwargs.get("env") or {})}
        return real(*args, **kwargs)

    monkeypatch.setattr(winproc, "run", run)


def _granted_repository(rig: Rig) -> tuple[Path, str, CodeGit]:
    """A committed repository granted to the Bot as its code folder, and `rig.code` remade as
    the server wires it (a guarded CodeGit and a WorktreeService), so a first send makes its
    copy."""
    repo = _repo(rig.tmp_path / "granted")
    (repo / "a.txt").write_text("alpha\n", encoding="utf-8")
    _git_commit(repo, "first")
    # Git available, so the registered identity holds the Git common dir the lease needs.
    workspaces = service_at(rig.tmp_path, rig.store, git=True)
    folder = trusted(workspaces, repo)
    rig.grant(rig.bot, folder)
    git = CodeGit(
        workspaces, CodeFiles(workspaces), git_available=lambda: True, bot_code_store=rig.store
    )
    worktrees = WorktreeService(
        rig.store,
        workspaces,
        git,
        MutationCoordinator(),
        WorkspaceMutationResolver(workspaces, git_available=lambda: True),
        rig.roots,
        area=rig.area,
    )
    rig.code = BotCode(
        rig.store,
        workspaces,
        rig.roots,
        rig.service.profiles,
        rig.threads,
        actor=rig.actors.get,
        area=rig.area,
        worktrees=worktrees,
    )
    return repo, folder, git


def _first_send(rig: Rig, session_id: str) -> str:
    """The session as before its first send: Claude Code never started it."""
    rig.sql("UPDATE session_profile_bindings SET native_started=0 WHERE session_id=?", session_id)
    return session_id


@needs_git
async def test_git_settings_the_guard_cannot_read_refuse_the_copy_with_their_own_sentence(
    rig: Rig, git_host: None, caplog: pytest.LogCaptureFixture
) -> None:
    repo, folder, _git = _granted_repository(rig)
    (repo / ".git" / "hooks" / "_").mkdir()  # a subfolder in the hooks folder, as husky 5 to 8 keep
    caplog.set_level(logging.DEBUG, logger="viki_agent.code.bot_code")
    _first_send(rig, rig.main)
    for _ in range(2):  # nothing was reserved, so the next send meets the same answer
        await rig.refused(rig.main, MSG_SETTINGS_UNREADABLE)
    # Not the generic sentence, whose Worktrees tab would show nothing: nothing was left behind.
    assert MSG_SETTINGS_UNREADABLE == "VIKI Agent can't read this folder's Git settings safely."
    assert rig.store.worktrees_in_states(STATES) == []
    assert rig.store.bot_code_baseline(folder) is None
    with rig.store._connection() as connection:
        assert connection.execute("SELECT count(*) FROM bot_code_copies").fetchone() == (0,)
    records = [r for r in caplog.records if r.name == "viki_agent.code.bot_code"]
    assert [r.getMessage() for r in records] == [
        "bot code copy not made: git_settings_unavailable",
        "bot code copy refused: git_settings_unavailable",
    ] * 2
    assert str(rig.tmp_path) not in caplog.text and rig.main not in caplog.text


@needs_git
async def test_reserving_a_copy_checks_the_git_settings_again_only_for_a_new_baseline(
    rig: Rig, git_host: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _repo_path, folder, git = _granted_repository(rig)
    assert git._guard is not None
    checks: list[Path] = []
    verify = git._guard.verify

    def counted(path: Path) -> None:
        checks.append(path)
        verify(path)

    monkeypatch.setattr(git._guard, "verify", counted)
    reserve = rig.code._reserve
    reserving: list[int] = []

    def reserved(*args: Any) -> Any:
        before = len(checks)
        try:
            return reserve(*args)
        finally:
            reserving.append(len(checks) - before)

    monkeypatch.setattr(rig.code, "_reserve", reserved)
    assert await rig.resolve(_first_send(rig, rig.main)) is not None
    assert rig.store.bot_code_baseline(folder) is not None
    assert await rig.resolve(_first_send(rig, rig.session(rig.bot))) is not None
    # The first copy: before the baseline exists, then against the one just saved. A later
    # copy: the saved baseline once.
    assert reserving == [2, 1]


@needs_git
async def test_a_baseline_another_spawn_saves_during_the_first_check_is_compared_before_any_use(
    rig: Rig, git_host: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two first copies of one folder at once: the baseline that wins may be the other spawn's,
    saved after this one's check found none to compare the settings with."""
    _repo_path, folder, git = _granted_repository(rig)
    repository, head = git.repository, git.head
    heads: list[Path] = []

    def repository_then_the_other_spawn(workspace_id: str) -> Path:
        root = repository(workspace_id)
        if rig.store.bot_code_baseline(folder) is None:
            # Taken from settings that are not the folder's now.
            assert rig.store.insert_bot_code_baseline(
                {"workspace_id": folder, "git_fingerprint": "0" * 64, "created_at": now_iso()}
            )
        return root

    def counted_head(root: Path) -> str:
        heads.append(root)
        return head(root)

    monkeypatch.setattr(git, "repository", repository_then_the_other_spawn)
    monkeypatch.setattr(git, "head", counted_head)

    await rig.refused(_first_send(rig, rig.main), GIT_SETTINGS_CHANGED)

    assert heads == []  # no command ran on settings nothing had compared, and nothing was reserved
    assert rig.store.worktrees_in_states(STATES) == []
    saved = rig.store.bot_code_baseline(folder)
    assert saved is not None and saved["git_fingerprint"] == "0" * 64


# --- describing copies for the worktree lists ------------------------------------------------


async def test_describe_names_each_copy_by_stored_names_in_one_batch(rig: Rig) -> None:
    task = rig.session(rig.bot)
    metadata = SessionMetadataService(rig.store, _scope(rig))
    metadata.patch(task, 0, {"title": "Fix the parser"})
    metadata.patch(rig.main, 0, {"title": ""})
    checker = rig.other_bot("Checker")
    second = rig.session(checker)
    thread_id = rig.thread([(rig.bot, rig.session(rig.bot)), (checker, second)], title="Review")
    main_copy, task_copy = rig.copy(session=rig.main), rig.copy(session=task)
    thread_copy = rig.copy(thread=thread_id)
    rig.sql("UPDATE bot_code_copies SET state='released' WHERE worktree_id=?", task_copy)
    ordinary = _id()
    described = rig.code.describe([main_copy, task_copy, thread_copy, ordinary])
    assert described == {
        main_copy: {
            "kind": "session",
            "bot_name": "Synthetic Engineer",
            "title": None,  # an empty stored title is no label
            "released": False,
        },
        task_copy: {
            "kind": "task",
            "bot_name": "Synthetic Engineer",
            "title": "Fix the parser",
            "released": True,
        },
        thread_copy: {
            "kind": "thread",
            "bot_name": "Synthetic Engineer",
            "title": "Review",
            "released": False,
        },
    }
    assert rig.code.describe([]) == {} and rig.code.describe([ordinary]) == {}


async def test_describe_leaves_unknown_labels_empty_and_never_uses_ids(rig: Rig) -> None:
    checker = rig.other_bot("Checker")
    first, second = rig.session(rig.bot), rig.session(checker)
    thread_id = rig.thread([(rig.bot, first), (checker, second)], title="")
    session_copy, thread_copy = rig.copy(session=rig.main), rig.copy(thread=thread_id)
    gone_thread = rig.copy(thread=_id())
    rig.sql("UPDATE bot_code_copies SET state='released' WHERE worktree_id=?", session_copy)
    rig.grant(rig.bot, None)
    rig.sql("DELETE FROM bots WHERE bot_id=?", rig.bot["bot_id"])
    described = rig.code.describe([session_copy, thread_copy, gone_thread])
    # Without its Bot, a session copy is neither a known main chat nor a known task thread.
    assert described == {
        thread_copy: {"kind": "thread", "bot_name": None, "title": None, "released": False},
        gone_thread: {"kind": "thread", "bot_name": None, "title": None, "released": False},
    }
    labels = [value for row in described.values() for value in row.values()]
    assert not any(isinstance(value, str) and _is_uuid(value) for value in labels)


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True
