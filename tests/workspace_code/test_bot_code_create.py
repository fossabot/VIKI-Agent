"""Bot code copies made on a first send (`BotCode.resolve`): real profile preparation, real
store, services and Git in synthetic folders. A granted Bot's main chat or task thread prepared
interactively gets its own hooks-off worktree; the granted Bots of a Bot thread share one per
folder; every other session keeps its native launch. One creation runs per chat or thread, a
refused one undoes what it reserved, and the folder's first copy records its Git-settings
baseline (AC-4, AC-5, AC-6)."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast

import pytest

from tests.app.test_workspaces import service_at
from tests.profiles.test_bot_code_provenance import main_bot
from tests.profiles.test_bot_memory import rig as rig
from tests.profiles.test_profile_document import document
from tests.profiles.test_profile_launch import ident
from tests.profiles.test_profile_launch import setup as setup
from tests.workspace_code.test_code_git import _commit, _git, _repo
from tests.workspace_code.test_workspaces_code import trusted
from tests.workspace_code.test_worktree_sweep import HANG_GUARD_S, change_settings
from tests.workspace_code.test_worktrees import (
    _committed,
    _empty_git_environment,  # noqa: F401 (autouse: no developer Git config)
    needs_git,
)
from viki_agent.code import worktrees as worktrees_module
from viki_agent.code.bot_code import (
    MSG_FAILED,
    MSG_FOLDER_BUSY,
    MSG_LIMIT,
    MSG_UNAVAILABLE,
    MSG_UNGRANTED,
    MSG_UNTRUSTED,
    BotCode,
)
from viki_agent.code.code_api import GIT_SETTINGS_CHANGED
from viki_agent.code.code_files import CodeFiles
from viki_agent.code.code_git import CodeGit
from viki_agent.code.session_roots import SessionRoots
from viki_agent.code.worktrees import MAX_WORKTREES, WorktreeService, worktree_area
from viki_agent.drivers.base import StartError
from viki_agent.events import CodeCopy
from viki_agent.messaging.bot_threads import BotThreadService
from viki_agent.profiles.profile_launch import ProfileLaunchService
from viki_agent.session import now_iso
from viki_agent.vaults.vault_mutation import MutationCoordinator
from viki_agent.vaults.workspace_mutation import WorkspaceMutationResolver

pytestmark = needs_git

BOT_BRANCH = re.compile(r"viki/bot-\d{8}-[0-9a-f]{6}")
STATES = ("creating", "ready", "removing", "kept", "removed")


class Live:
    live = True


class Copies:
    """An ordinary vault, a trusted committed code folder with a post-checkout hook that leaves
    a marker, and a Bot granted that folder; BotCode wired as the server wires it."""

    def __init__(self, prepared: Any, tmp_path: Path) -> None:
        self.launch: ProfileLaunchService = prepared[0]
        _state, _vault, self.pid, self.wid = prepared[1:]
        self.store = self.launch.store
        self.tmp_path = tmp_path
        self.workspaces = service_at(tmp_path, self.store, git=True)
        self.repo = _committed(tmp_path)
        self.marker = tmp_path / "post-checkout-ran"
        hook = self.repo / ".git" / "hooks" / "post-checkout"
        hook.write_bytes(f"#!/bin/sh\necho ran > '{self.marker.as_posix()}'\n".encode())
        hook.chmod(0o755)
        self.folder = trusted(self.workspaces, self.repo)
        self.bot_id, self.chat = main_bot(self.launch, self.pid, self.wid)
        self.grant(self.bot_id, self.folder)
        self.live: dict[str, Live] = {}
        self.git = CodeGit(
            self.workspaces, CodeFiles(self.workspaces), git_available=lambda: True,
            bot_code_store=self.store,
        )  # fmt: skip
        self.coordinator = MutationCoordinator()
        self.resolver = WorkspaceMutationResolver(self.workspaces, git_available=lambda: True)
        self.roots = SessionRoots(
            self.store, self.workspaces, exists=lambda _: False, actor=self.live.get
        )
        self.area = worktree_area(tmp_path / "data")
        self.worktrees = WorktreeService(
            self.store, self.workspaces, self.git, self.coordinator, self.resolver, self.roots,
            area=self.area,
        )  # fmt: skip
        self.code = BotCode(
            self.store, self.workspaces, self.roots, self.launch.profiles,
            BotThreadService(self.store, self.workspaces), actor=self.live.get, area=self.area,
            worktrees=self.worktrees,
        )  # fmt: skip

    def grant(self, bot_id: str, folder: str | None) -> None:
        with self.store._connection(write=True) as connection:
            connection.execute("DELETE FROM bot_code_folders WHERE bot_id=?", (bot_id,))
            if folder is not None:
                connection.execute(
                    "INSERT INTO bot_code_folders VALUES (?,?,?)", (bot_id, folder, now_iso())
                )

    def prepare(
        self,
        session_id: str | None = None,
        origin: str = "interactive",
        bot: tuple[str, str] | None = None,
    ) -> str:
        """A session prepared as the server prepares it, stamped with its launch origin."""
        session_id = session_id or ident()
        bot_id, profile_id = bot or (self.bot_id, self.pid)
        self.launch.prepare(
            profile_id, ident(), session_id, 1, self.wid, bot_id, launch_origin=cast(Any, origin)
        )
        return session_id

    def other_bot(self, folder: str | None) -> tuple[str, str]:
        profile_id = ident()
        self.launch.profiles.create(profile_id, ident(), document())
        bot_id, _chat = main_bot(self.launch, profile_id, self.wid)
        self.grant(bot_id, folder)
        return bot_id, profile_id

    def thread(self, members: list[tuple[tuple[str, str], str]]) -> str:
        thread_id = ident()
        BotThreadService(self.store, self.workspaces).save(
            self.wid,
            thread_id,
            0,
            {
                "title": "Review",
                "phase": "idle",
                "members": [
                    {
                        "key": key,
                        "bot_id": bot_id,
                        "profile_id": profile_id,
                        "session_id": session_id,
                        "enabled": True,
                    }
                    for ((bot_id, profile_id), session_id), key in zip(
                        members, ("chief", "checker", "ops"), strict=False
                    )
                ],
                "messages": [],
            },
        )
        return thread_id

    def sql(self, statement: str, *values: object) -> None:
        with self.store._connection(write=True) as connection:
            connection.execute(statement, values)

    async def resolve(self, session_id: str) -> CodeCopy | None:
        settings = self.launch.bot_memory_settings_path(session_id)
        assert settings is not None
        return await self.code.resolve(session_id, settings)

    async def refused(self, session_id: str, message: str) -> None:
        with pytest.raises(StartError) as refusal:
            await self.resolve(session_id)
        assert (refusal.value.code, refusal.value.message) == ("bad_request", message)

    def rows(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.store.worktrees_in_states(STATES)]

    def bindings(self) -> list[dict[str, Any]]:
        with self.store._connection() as connection:
            rows = connection.execute(
                "SELECT worktree_id,bot_id,session_id,thread_id,state FROM bot_code_copies "
                "ORDER BY created_at,worktree_id"
            ).fetchall()
        keys = ("worktree_id", "bot_id", "session_id", "thread_id", "state")
        return [dict(zip(keys, row, strict=True)) for row in rows]

    def copy_of(self, copy: CodeCopy) -> dict[str, Any]:
        worktree_id = Path(copy.path).name
        row = self.store.get_worktree(worktree_id)
        binding = self.store.copy_states([worktree_id])[worktree_id]
        assert row is not None
        return {**row, "binding": dict(binding)}


@pytest.fixture
def copies(rig: Any, tmp_path: Path) -> Copies:
    return Copies(rig, tmp_path)


def _head(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD").strip()


# --- main chat and task threads ---------------------------------------------------------------


async def test_main_chat_and_task_first_sends_make_their_own_hooks_off_copies(
    copies: Copies,
) -> None:
    status = _git(copies.repo, "status", "--porcelain")
    chat, task = copies.prepare(copies.chat), copies.prepare()
    main_copy, task_copy = await copies.resolve(chat), await copies.resolve(task)
    assert main_copy is not None and task_copy is not None
    assert main_copy.path != task_copy.path and main_copy.branch != task_copy.branch
    name = copies.workspaces.get(copies.folder, kind="code").name
    for copy, session_id in ((main_copy, chat), (task_copy, task)):
        assert copy.folder == name and BOT_BRANCH.fullmatch(copy.branch)
        assert Path(copy.path).parent == copies.area and Path(copy.path).is_dir()
        made = copies.copy_of(copy)
        assert (made["state"], made["branch"], made["base_oid"]) == (
            "ready", copy.branch, _head(copies.repo)
        )  # fmt: skip
        assert made["binding"] == {
            **made["binding"],
            "bot_id": copies.bot_id,
            "session_id": session_id,
            "thread_id": None,
            "state": "bound",
        }
        assert _git(Path(copy.path), "rev-parse", "--abbrev-ref", "HEAD").strip() == copy.branch
    # The repository's hook never ran, and the original folder is untouched.
    assert not copies.marker.exists()
    assert _git(copies.repo, "status", "--porcelain") == status
    # The first copy recorded the folder's baseline: its settings as they were.
    baseline = copies.store.bot_code_baseline(copies.folder)
    assert baseline is not None
    assert baseline["git_fingerprint"] == copies.git.settings_fingerprint(copies.repo)
    # Each later spawn, resume included, keeps the same copy and makes nothing.
    rows = copies.rows()
    assert await copies.resolve(chat) == main_copy and await copies.resolve(task) == task_copy
    assert copies.rows() == rows


@pytest.mark.parametrize(
    "case",
    ["unknown", "routine", "started", "attempt", "ungranted", "readonly_vault"],
)
async def test_only_an_interactive_first_send_of_a_granted_bot_gets_a_copy(
    copies: Copies, case: str
) -> None:
    origin = case if case in {"unknown", "routine"} else "interactive"
    session_id = copies.prepare(origin=origin)
    if case == "started":
        copies.sql(
            "UPDATE session_profile_bindings SET native_started=1 WHERE session_id=?", session_id
        )
    elif case == "attempt":
        # Retained routine evidence outranks the interactive marker.
        copies.sql(
            "INSERT INTO task_attempts VALUES "
            "(?,'routine',?,?,?,?,'claimed',NULL,'',NULL,NULL,NULL,?,NULL,NULL)",
            ident(), ident(), "a" * 64, session_id, ident(), now_iso(),
        )  # fmt: skip
    elif case == "ungranted":
        copies.grant(copies.bot_id, None)
    elif case == "readonly_vault":
        settings = copies.launch.bot_memory_settings_path(session_id)
        copies.sql(
            "UPDATE workspaces SET policy='existing_readonly' WHERE workspace_id=?", copies.wid
        )
        assert await copies.code.resolve(session_id, cast(str, settings)) is None
    if case != "readonly_vault":
        assert await copies.resolve(session_id) is None
    assert copies.rows() == [] and copies.bindings() == []
    assert copies.store.bot_code_baseline(copies.folder) is None
    assert not copies.area.exists() or not any(copies.area.iterdir())


@pytest.mark.parametrize("state", ["released", "former"])
async def test_a_chat_whose_copy_was_released_never_gets_another(
    copies: Copies, state: str
) -> None:
    session_id = copies.prepare()
    copy = await copies.resolve(session_id)
    assert copy is not None
    worktree_id = Path(copy.path).name
    copies.sql("UPDATE bot_code_copies SET state='released' WHERE worktree_id=?", worktree_id)
    if state == "former":
        assert copies.store.finish_release(worktree_id)
    rows = copies.rows()
    await copies.refused(session_id, MSG_UNAVAILABLE)
    assert copies.rows() == rows  # never rebound to that row nor given a new one


async def test_an_untrusted_or_moved_folder_refuses_rather_than_start_without_it(
    copies: Copies,
) -> None:
    session_id = copies.prepare()
    copies.sql(
        "UPDATE workspaces SET policy='existing_readonly' WHERE workspace_id=?", copies.folder
    )
    await copies.refused(session_id, MSG_UNTRUSTED)
    assert copies.rows() == [] and copies.bindings() == []


# --- shared Bot-thread copies -----------------------------------------------------------------


async def test_granted_thread_members_share_one_copy_per_folder(copies: Copies) -> None:
    checker = copies.other_bot(copies.folder)
    writer = copies.other_bot(None)
    main = (copies.bot_id, copies.pid)
    first, second, third = (copies.prepare(bot=bot) for bot in (main, checker, writer))
    thread_id = copies.thread([(main, first), (checker, second), (writer, third)])
    shared = await copies.resolve(first)
    assert shared is not None and BOT_BRANCH.fullmatch(shared.branch)
    assert copies.copy_of(shared)["binding"]["thread_id"] == thread_id
    assert copies.copy_of(shared)["binding"]["session_id"] is None
    assert await copies.resolve(second) == shared  # never a copy per turn
    assert await copies.resolve(third) is None  # an ungranted member gets none
    assert await copies.resolve(first) == shared
    assert len(copies.rows()) == 1 and not copies.marker.exists()

    # A member granted another folder gets the thread's copy of that folder.
    repo2 = _repo(copies.tmp_path / "repo2")
    (repo2 / "c.txt").write_text("gamma\n", encoding="utf-8")
    _commit(repo2, "first")
    copies.grant(checker[0], trusted(copies.workspaces, repo2))
    other = await copies.resolve(second)
    assert other is not None and other.path != shared.path
    assert copies.copy_of(other)["binding"]["thread_id"] == thread_id
    assert await copies.resolve(first) == shared

    # Another thread gets its own.
    fourth = copies.prepare()
    copies.thread([(main, fourth)])
    elsewhere = await copies.resolve(fourth)
    assert elsewhere is not None and elsewhere.path not in {shared.path, other.path}
    assert len(copies.rows()) == 3


async def test_parallel_turns_of_a_thread_make_one_copy_and_start_one(copies: Copies) -> None:
    checker = copies.other_bot(copies.folder)
    main = (copies.bot_id, copies.pid)
    first, second = copies.prepare(bot=main), copies.prepare(bot=checker)
    copies.thread([(main, first), (checker, second)])

    async def start(session_id: str) -> CodeCopy | StartError:
        try:
            copy = await copies.resolve(session_id)
        except StartError as error:
            return error
        copies.live[session_id] = Live()  # live at once after the resolver, as SessionActor is
        assert copy is not None
        return copy

    results = await asyncio.gather(start(first), start(second))
    assert sum(isinstance(result, CodeCopy) for result in results) == 1
    assert {r.message for r in results if isinstance(r, StartError)} == {
        "Another Bot in this Bot thread is using its code copy now, so this turn can't start "
        "until that one finishes."
    }
    assert len(copies.rows()) == 1 and len(copies.bindings()) == 1


async def test_two_windows_sending_first_make_one_copy(copies: Copies) -> None:
    session_id = copies.prepare()
    first, second = await asyncio.gather(copies.resolve(session_id), copies.resolve(session_id))
    assert first is not None and first == second
    assert len(copies.rows()) == 1 and len(copies.bindings()) == 1


# --- refusals and what they leave ---------------------------------------------------------------


@asynccontextmanager
async def _folder_leased(copies: Copies) -> AsyncIterator[None]:
    """The whole folder and its Git common dir, as another worktree operation holds them."""
    folder = copies.workspaces.get(copies.folder, kind="code")
    root = copies.resolver(
        copies.folder, folder.policy_revision, folder.root.physical_id, kind="code"
    )
    async with copies.coordinator.lease(root, (), include_git_common=True):
        yield


async def test_a_busy_folder_refuses_and_reserves_nothing(
    copies: Copies, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(worktrees_module, "LEASE_WAIT_S", 0.02)
    session_id = copies.prepare()
    async with _folder_leased(copies):
        await copies.refused(session_id, MSG_FOLDER_BUSY)
    assert copies.rows() == [] and copies.bindings() == []
    assert await copies.resolve(session_id) is not None  # send again when it finishes


async def test_a_cancelled_first_send_leaves_its_copy_to_finish(
    copies: Copies, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = copies.prepare()
    waiting = asyncio.Event()
    lease = copies.worktrees._lease

    @asynccontextmanager
    async def observed(root: Any) -> AsyncIterator[None]:
        waiting.set()
        async with lease(root):
            yield

    monkeypatch.setattr(copies.worktrees, "_lease", observed)
    async with _folder_leased(copies):
        spawn = asyncio.ensure_future(copies.resolve(session_id))
        await asyncio.wait_for(waiting.wait(), HANG_GUARD_S)
        spawn.cancel()
        with pytest.raises(asyncio.CancelledError):
            await spawn
    # The next send waits for the copy the cancelled one is still making, then uses it.
    copy = await asyncio.wait_for(copies.resolve(session_id), HANG_GUARD_S)
    assert copy is not None and copies.copy_of(copy)["state"] == "ready"
    assert len(copies.rows()) == 1 and len(copies.bindings()) == 1


async def test_the_worktree_limit_refuses_and_reserves_nothing(copies: Copies) -> None:
    session_id = copies.prepare()
    stamp = now_iso()
    for n in range(MAX_WORKTREES):
        copies.store.insert_worktree(
            {
                "worktree_id": ident(),
                "workspace_id": copies.folder,
                "path": str(copies.tmp_path / "elsewhere" / str(n)),
                "branch": f"viki/20261112-{n:04x}",
                "base_oid": "a" * 40,
                "state": "creating",
                "created_at": stamp,
                "updated_at": stamp,
            },
            limit=MAX_WORKTREES,
        )
    await copies.refused(session_id, MSG_LIMIT)
    assert copies.bindings() == [] and len(copies.rows()) == MAX_WORKTREES
    freed = copies.rows()[0]["worktree_id"]
    copies.store.delete_worktree(freed, expected_state="creating")
    assert await copies.resolve(session_id) is not None


@pytest.mark.parametrize("left", [True, False])
async def test_a_failed_add_keeps_what_it_left_and_never_makes_a_second_copy(
    copies: Copies, monkeypatch: pytest.MonkeyPatch, left: bool
) -> None:
    session_id = copies.prepare()
    add = copies.worktrees._git_add

    def failing(root: Path, row: Any, hooks: bool) -> None:
        if left:
            add(root, row, hooks)  # the tree is made, then the add reports a failure
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(copies.worktrees, "_git_add", failing)
    await copies.refused(session_id, MSG_FAILED)
    monkeypatch.setattr(copies.worktrees, "_git_add", add)
    if left:
        # The tree stays `kept`, still bound, so it stays a Bot copy to Git; the retry refuses
        # the same way rather than make another.
        (row,) = copies.rows()
        assert row["state"] == "kept" and Path(row["path"]).is_dir()
        assert [b["state"] for b in copies.bindings()] == ["bound"]
        await copies.refused(session_id, MSG_FAILED)
        assert copies.rows() == [row] and len(copies.bindings()) == 1
    else:
        # Nothing was made: the reservation is undone, so the next send makes the copy.
        assert copies.rows() == [] and copies.bindings() == []
        assert await copies.resolve(session_id) is not None


async def test_changed_git_settings_refuse_a_new_copy_and_keep_the_baseline(
    copies: Copies,
) -> None:
    assert await copies.resolve(copies.prepare()) is not None
    baseline = copies.store.bot_code_baseline(copies.folder)
    change_settings(copies.repo)
    rows, bindings = copies.rows(), copies.bindings()
    await copies.refused(copies.prepare(), GIT_SETTINGS_CHANGED)
    assert copies.rows() == rows and copies.bindings() == bindings
    assert copies.store.bot_code_baseline(copies.folder) == baseline


async def test_a_grant_removed_while_the_copy_is_made_refuses_its_start(
    copies: Copies, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = copies.prepare()
    add = copies.worktrees._git_add

    def revoking(root: Path, row: Any, hooks: bool) -> None:
        copies.grant(copies.bot_id, None)
        add(root, row, hooks)

    monkeypatch.setattr(copies.worktrees, "_git_add", revoking)
    await copies.refused(session_id, MSG_UNGRANTED)
    (row,) = copies.rows()
    assert row["state"] == "ready" and [b["state"] for b in copies.bindings()] == ["bound"]
    copies.grant(copies.bot_id, copies.folder)  # giving it back continues in the same copy
    copy = await copies.resolve(session_id)
    assert copy is not None and Path(copy.path).name == row["worktree_id"]


async def test_creation_logs_codes_only(
    copies: Copies, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="viki_agent.code.bot_code")
    monkeypatch.setattr(worktrees_module, "LEASE_WAIT_S", 0.02)
    session_id = copies.prepare()
    async with _folder_leased(copies):
        await copies.refused(session_id, MSG_FOLDER_BUSY)
    copy = await copies.resolve(session_id)
    assert copy is not None
    records = [r for r in caplog.records if r.name == "viki_agent.code.bot_code"]
    assert [r.getMessage() for r in records] == [
        "bot code copy not made: vault_locked",
        "bot code copy refused: folder_busy",
    ]
    secrets = (session_id, copy.path, Path(copy.path).name, copy.branch, str(copies.tmp_path))
    assert not any(secret in caplog.text for secret in secrets)
