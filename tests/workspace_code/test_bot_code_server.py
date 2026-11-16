"""Bot code copies wired through `server.create_app`: the one baseline-aware CodeGit behind
Bot grants, copies made on a granted Bot chat's first send (main chat, task thread, shared Bot
thread), none for a routine, the worktree lists and Git-settings routes, and the post-commit
release after a Bot, chat or data reset deletion, kept while a driver still holds the copy.

Real store, services, routes and Git in synthetic folders; synthetic drivers, no provider."""

from __future__ import annotations

import asyncio
import logging
import re
import tempfile
import threading
import time
import uuid
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from starlette.applications import Starlette

from tests.app.test_workspaces import service_at
from tests.automation.test_routines import _document_raw, _manual_schedule
from tests.profiles.test_profile_document import document
from tests.profiles.test_profile_launch_api import (
    AUTH,
    BASE,
    TOKEN,
    WRITE,
    FakeDriver,
    SyntheticCatalog,
)
from tests.workspace_code.test_code_git import _git
from tests.workspace_code.test_workspaces_code import trusted
from tests.workspace_code.test_worktrees import (
    _committed,
    _empty_git_environment,  # noqa: F401 (autouse: no developer Git config)
    needs_git,
)
from viki_agent import server
from viki_agent.app.data_api import LocationsService, ResetService
from viki_agent.app.diagnostics import Readiness
from viki_agent.app.exports import ExportService
from viki_agent.app.imports import ImportService
from viki_agent.app.settings import KEYS, SettingsService
from viki_agent.automation import tasks as task_module
from viki_agent.code.bot_code import MSG_THREAD_BUSY, MSG_UNTRUSTED
from viki_agent.code.worktrees import worktree_area
from viki_agent.config import Config
from viki_agent.conversations.commands import CommandInput, CommandService
from viki_agent.conversations.drafts import DraftService, draft_scope
from viki_agent.conversations.session_metadata import SessionMetadataService
from viki_agent.conversations.session_metadata_api import session_metadata_routes
from viki_agent.drivers.base import StartError
from viki_agent.drivers.claude.control import Launch
from viki_agent.drivers.claude.driver import DriverConfig
from viki_agent.events import Event, SessionStarted, TurnDone, Usage
from viki_agent.messaging.bot_threads import BotThreadService
from viki_agent.profiles.profile_capabilities import FIXTURE_VERSION
from viki_agent.profiles.service import ProfileService
from viki_agent.session import SessionActor, SessionManager, now_iso
from viki_agent.state import AppState, Prefs

pytestmark = needs_git

HANG_GUARD_S = 30.0  # guards real Git work on a busy machine; never a bound on it
OWNER = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
GENERATION = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
BOT_BRANCH = re.compile(r"viki/bot-\d{8}-[0-9a-f]{6}")


def ident() -> str:
    return str(uuid.uuid4())


class Driver(FakeDriver):
    """A synthetic driver whose open can refuse and whose close can be held."""

    def __init__(self, stack: Stack, launch: Launch, emit: Callable[[Event], None]) -> None:
        super().__init__(launch, emit, stack.state, stack.catalog)
        self.stack = stack
        self.closing = asyncio.Event()
        self.close_gate = asyncio.Event()
        self.close_gate.set()

    async def open(self) -> SessionStarted:
        if self.stack.refuse_open:
            raise StartError("crashed", "synthetic start failure")
        return await super().open()

    async def send(self, text: str) -> None:
        await super().send(text)
        for event in self.stack.script:
            self.emit(event)

    async def close(self) -> None:
        self.closing.set()
        await self.close_gate.wait()
        self.alive = False


class Stack:
    """An ordinary vault, a trusted committed code folder whose post-checkout hook leaves a
    marker, two profiles and the whole app, with Worktree cleanup off."""

    def __init__(self, tmp_path: Path) -> None:
        # A short folder: transcript and worktree paths must stay under Windows MAX_PATH.
        short = Path(tempfile.mkdtemp(dir=tmp_path.parent, prefix="c"))
        self.data = short / "data"
        self.vault = short / "vault"
        self.home = short / "home"
        self.vault.mkdir()
        (self.home / "sessions").mkdir(parents=True)
        self.state = AppState(self.data / "state.json", Prefs(mode="auto", effort="low"))
        self.store = self.state.store
        self.workspaces = service_at(short, self.store, git=True)
        vault, _ = self.workspaces.register_existing(ident(), str(self.vault))
        self.workspace_id = vault.workspace_id
        self.workspaces.select(ident(), self.workspace_id)
        self.sql(
            "UPDATE workspaces SET policy='ordinary',bootstrap='complete' WHERE workspace_id=?",
            self.workspace_id,
        )
        self.repo = _committed(short)
        self.marker = short / "post-checkout-ran"
        hook = self.repo / ".git" / "hooks" / "post-checkout"
        hook.write_bytes(f"#!/bin/sh\necho ran > '{self.marker.as_posix()}'\n".encode())
        hook.chmod(0o755)
        self.folder = trusted(self.workspaces, self.repo)
        self.profiles = [ident(), ident()]
        for profile_id in self.profiles:
            ProfileService(self.store).create(profile_id, ident(), document())
        self.settings = SettingsService(
            self.store, provider_defaults=lambda: Prefs(), on_chat_defaults=lambda _: None
        )
        self.settings.patch(
            ident(), self.settings.read().document.revision, {"workbench.worktree_cleanup": False}
        )
        self.config = Config(
            vault=self.vault,
            claude_home=self.home,
            cli=Path("synthetic-claude.exe"),
            edge=None,
            data_dir=self.data,
        )
        self.catalog = SyntheticCatalog(self.home, self.vault)
        self.readiness = Readiness(
            config=self.config, launch_id=ident(), git_available=lambda: True
        )
        self.readiness.observe_version(FIXTURE_VERSION)
        self.readiness.observe_billing([])
        self.refuse_open = False
        self.script: tuple[Event, ...] = ()  # what a send emits; a routine needs its turn end
        self.settings_fail = False
        self.drivers: list[Driver] = []
        self.manager = SessionManager(
            config=DriverConfig(cli=("synthetic",), vault=self.vault, env={}),
            state=self.state,
            claude_home=self.home,
            make_driver=self.driver,
        )
        self.app = server.create_app(
            config=self.config,
            token=TOKEN,
            manager=self.manager,
            catalog=self.catalog,
            static_dir=None,
            settings=self.settings,
            workspaces=self.workspaces,
            readiness=self.readiness,
            vault_poll_s=60,
            data=self.data_services(),
            owner=OWNER,
        )
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url=BASE)

    def data_services(self) -> server.DataServices:
        def settings_reset(_command_id: str) -> None:
            if self.settings_fail:
                raise RuntimeError("synthetic later category failure")

        reset = ResetService(
            store=self.store,
            active_workspace=self.workspaces.resolve_active,
            settings_reset=settings_reset,
            scratch_root=self.data / "scratch" / "chat",
            scratch_in_use=lambda: True,
            owner=OWNER,
        )
        exports = ExportService(
            diagnostics_document=lambda paths, ident: {},
            redact=lambda text, roots: (text, 0),
            roots=lambda: (),
            settings_export=lambda: {},
            settings_specs={},
            workspaces_export=lambda: [],
            denied_roots=lambda: (self.vault,),
            destination_guard=threading.RLock(),
            log_path=self.data / "app.log",
            app_version="0.1.0",
            launch_id="launch-1",
        )
        imports = ImportService(
            settings_specs=KEYS, current_settings=lambda: self.settings.read().document,
            patch=lambda *_: (_ for _ in ()).throw(AssertionError("not exercised")),
        )  # fmt: skip
        locations = LocationsService(
            workspaces=lambda: [],
            data_dir=self.data,
            claude_home=self.home,
            scratch_in_use=lambda: True,
        )
        return locations, exports, imports, reset

    def driver(self, launch: Launch, _prefs: Prefs, emit: Callable[[Event], None]) -> Driver:
        made = Driver(self, launch, emit)
        self.drivers.append(made)
        return made

    def sql(self, statement: str, *values: object) -> None:
        with self.store._connection(write=True) as connection:
            connection.execute(statement, values)

    def bindings(self) -> dict[str, str]:
        with self.store._connection() as connection:
            return dict(connection.execute("SELECT worktree_id,state FROM bot_code_copies"))

    def origin(self, session_id: str) -> str | None:
        return self.store.profile_launch_origin(session_id)

    async def bot(self, profile: int = 0, *, grant: bool = True) -> dict[str, Any]:
        response = await self.client.post(
            "/api/v2/bots",
            headers=WRITE,
            json={
                "bot_id": ident(),
                "profile_id": self.profiles[profile],
                "workspace_id": self.workspace_id,
                "expected_profile_revision": 1,
            },
        )
        assert response.status_code == 201, response.text
        bot = response.json()["bot"]
        if grant:
            granted = await self.grant(bot)
            assert granted.status_code == 200, granted.text
            bot = granted.json()["bot"]
        return bot

    async def grant(self, bot: dict[str, Any]) -> httpx.Response:
        return await self.client.post(
            f"/api/v2/bots/{bot['bot_id']}/code-folder",
            headers=WRITE,
            json={"expected_state_revision": bot["state_revision"], "workspace_id": self.folder},
        )

    async def chat(self, bot: dict[str, Any]) -> dict[str, Any]:
        response = await self.client.post(
            f"/api/v2/bots/{bot['bot_id']}/chat",
            headers=WRITE,
            json={"expected_state_revision": bot["state_revision"]},
        )
        assert response.status_code == 200, response.text
        return response.json()["preview"]

    async def task(self, bot: dict[str, Any], **extra: object) -> httpx.Response:
        """A task thread or Bot-thread seat, prepared through the public start-preview."""
        return await self.client.post(
            f"/api/v2/profiles/{bot['profile_id']}/start-preview",
            headers=WRITE,
            json={
                "command_id": ident(),
                "session_id": ident(),
                "expected_profile_revision": 1,
                "workspace_id": self.workspace_id,
                "bot_id": bot["bot_id"],
                **extra,
            },
        )

    async def send(self, session_id: str) -> tuple[bool | None, SessionActor]:
        actor = self.manager.get(session_id)
        assert actor is not None
        return await actor.send("synthetic prompt"), actor

    async def worktrees(self) -> dict[str, dict[str, Any]]:
        response = await self.client.get("/api/v2/worktrees", headers=AUTH)
        assert response.status_code == 200, response.text
        return {row["worktree_id"]: row for row in response.json()["worktrees"]}

    def folder_body(self) -> dict[str, Any]:
        folder = self.workspaces.get(self.folder, kind="code")
        return {
            "policy_revision": folder.policy_revision,
            "root_physical_id": folder.root.physical_id,
        }


@pytest.fixture
async def stack(tmp_path: Path) -> AsyncIterator[Stack]:
    built = Stack(tmp_path)
    try:
        async with built.app.router.lifespan_context(built.app), built.client:
            yield built
    finally:
        for driver in built.drivers:
            driver.close_gate.set()


def started(actor: SessionActor) -> dict[str, Any]:
    [frame] = [e for e in actor.events_after(0) if e["type"] == "session.started"][-1:]
    return frame["data"]


def errors(actor: SessionActor) -> list[str]:
    return [e["data"]["message"] for e in actor.events_after(0) if e["type"] == "error"]


async def until(flag: Callable[[], bool]) -> None:
    """Poll real background work (a release runs Git in worker threads) up to the guard."""
    deadline = time.monotonic() + HANG_GUARD_S
    while not flag():
        assert time.monotonic() < deadline, "the background release never settled"
        await asyncio.sleep(0.01)


def kept_logs(caplog: pytest.LogCaptureFixture) -> int:
    return sum("release kept a copy (live)" in r.getMessage() for r in caplog.records)


def assert_code_only_logs(caplog: pytest.LogCaptureFixture, *secrets: str) -> None:
    for record in caplog.records:
        if record.levelno >= logging.WARNING:
            assert not any(secret in record.getMessage() for secret in secrets), record


# --- construction: one baseline-aware CodeGit behind the grants -------------------------------


async def test_bot_grants_check_the_folder_baseline_with_the_one_store_backed_code_git(
    stack: Stack,
) -> None:
    bots = stack.app.state.bots
    assert bots.code_git is not None and bots.code_git._bot_code_store is stack.store
    bot = await stack.bot(grant=False)
    stack.store.insert_bot_code_baseline(
        {"workspace_id": stack.folder, "git_fingerprint": "0" * 64, "created_at": now_iso()}
    )
    refused = await stack.grant(bot)
    assert refused.status_code == 409, refused.text
    assert refused.json()["error"]["code"] == "git_settings_changed"
    stack.sql("DELETE FROM bot_code_baselines WHERE workspace_id=?", stack.folder)
    granted = await stack.grant(bot)
    assert granted.status_code == 200, granted.text
    assert granted.json()["bot"]["code_folder"]["workspace_id"] == stack.folder


# --- copies on a first send ------------------------------------------------------------------


async def test_main_chat_and_task_thread_each_get_a_hooks_off_copy_named_in_the_lists(
    stack: Stack, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)
    bot = await stack.bot()
    main = (await stack.chat(bot))["session_id"]
    injected = await stack.task(bot, launch_origin="routine")
    assert injected.status_code == 400  # a body never names the origin
    prepared = await stack.task(bot)
    assert prepared.status_code == 200, prepared.text
    task = prepared.json()["session_id"]
    assert (stack.origin(main), stack.origin(task)) == ("interactive", "interactive")
    copies = {}
    for session_id in (main, task):
        ok, actor = await stack.send(session_id)
        assert ok, errors(actor)
        launch = stack.drivers[-1].launch
        path = Path(launch.code_worktree or "")
        assert path.parent == worktree_area(stack.data) and path.is_dir()
        assert launch.cwd is None and launch.bot_memory_settings
        copy = started(actor)["code_copy"]
        assert copy["path"] == str(path) and copy["folder"] == "repo"
        assert BOT_BRANCH.fullmatch(copy["branch"])
        copies[session_id] = path.name
    assert copies[main] != copies[task] and not stack.marker.exists()
    listed = await stack.worktrees()
    assert {copies[main]: "session", copies[task]: "task"} == {
        worktree_id: listed[worktree_id]["bot_copy"]["kind"] for worktree_id in copies.values()
    }
    assert all(listed[w]["bot_copy"]["released"] is False for w in copies.values())
    branch = listed[copies[main]]["branch"]
    preview = await stack.chat(bot)
    assert preview["code_copy"] == {"workspace_id": stack.folder, "name": "repo", "branch": branch}

    # The folder's first copy recorded its baseline: read, CAS-confirmed and enforced.
    url = f"/api/v2/code/{stack.folder}/git-settings"
    saved = (await stack.client.get(url, headers=AUTH)).json()["fingerprint"]
    assert re.fullmatch("[0-9a-f]{64}", saved)
    stale = await stack.client.post(
        url, headers=WRITE, json={**stack.folder_body(), "expected_fingerprint": "f" * 64}
    )
    assert (stale.status_code, stale.json()["error"]["code"]) == (409, "baseline_changed")
    root = await stack.client.post(
        "/api/v2/session-roots",
        headers=WRITE,
        json={"session_id": ident(), "workspace_id": stack.folder, "worktree_id": copies[main]},
    )
    assert (root.status_code, root.json()["error"]["code"]) == (409, "bot_copy_root")
    _git(stack.repo, "config", "guard.synthetic", "changed")
    action = await stack.client.post(
        f"/api/v2/code/{stack.folder}/worktrees",
        headers=WRITE,
        json={**stack.folder_body(), "branch": "viki/20261112-c0de"},
    )
    assert (action.status_code, action.json()["error"]["code"]) == (409, "git_settings_changed")
    confirmed = await stack.client.post(
        url, headers=WRITE, json={**stack.folder_body(), "expected_fingerprint": saved}
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["fingerprint"] not in {saved, None}
    assert_code_only_logs(caplog, *copies.values(), branch, str(stack.repo))


async def test_bot_thread_members_share_one_copy_one_turn_at_a_time(stack: Stack) -> None:
    first, second = await stack.bot(0), await stack.bot(1)
    seats = []
    for bot in (first, second):
        response = await stack.task(bot)
        assert response.status_code == 200, response.text
        seats.append(response.json()["session_id"])
    thread_id = ident()
    BotThreadService(stack.store, stack.workspaces).save(
        stack.workspace_id,
        thread_id,
        0,
        {
            "title": "Synthetic review",
            "phase": "idle",
            "members": [
                {
                    "key": key,
                    "bot_id": bot["bot_id"],
                    "profile_id": bot["profile_id"],
                    "session_id": seat,
                    "enabled": True,
                }
                for key, bot, seat in zip(("chief", "checker"), (first, second), seats, strict=True)
            ],
            "messages": [],
        },
    )
    ok, chief = await stack.send(seats[0])
    assert ok, errors(chief)
    shared = stack.drivers[-1].launch.code_worktree
    refused, checker = await stack.send(seats[1])
    assert not refused and errors(checker) == [MSG_THREAD_BUSY] and not checker.live
    await stack.manager.close(seats[0])
    ok, checker = await stack.send(seats[1])
    assert ok, errors(checker)
    assert stack.drivers[-1].launch.code_worktree == shared
    [(worktree_id, row)] = [(w, r) for w, r in (await stack.worktrees()).items()]
    assert Path(shared or "").name == worktree_id and row["bot_copy"]["kind"] == "thread"


async def test_a_copy_that_cannot_be_made_refuses_the_turn_visibly(stack: Stack) -> None:
    bot = await stack.bot()
    session_id = (await stack.chat(bot))["session_id"]
    stack.sql("UPDATE workspaces SET policy='existing_readonly' WHERE workspace_id=?", stack.folder)
    refused, actor = await stack.send(session_id)
    assert not refused and errors(actor) == [MSG_UNTRUSTED]
    assert not actor.live and stack.drivers == [] and stack.bindings() == {}


async def test_a_routine_of_a_granted_bot_gets_no_copy_before_or_after_prune(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot = await stack.bot()
    routines = stack.app.state.routines
    raw = _document_raw(
        profile_id=bot["profile_id"], workspace_id=stack.workspace_id, schedule=_manual_schedule()
    )
    routine_id = routines.create(ident(), raw)[0]["routine_id"]
    runner = stack.app.state.routine_tasks
    stack.refuse_open = True  # the run ends before Claude Code ever initialized
    await runner.run_now(routine_id, ident(), 1)
    await asyncio.wait_for(asyncio.gather(*list(runner._runs)), HANG_GUARD_S)
    [run] = runner.store.runs(routine_id, 1, None)
    session_id = run["attempt"]["session_id"]
    assert run["attempt"]["outcome"] != "succeeded"
    assert stack.origin(session_id) == "routine"
    with stack.store._connection() as connection:
        started_row = connection.execute(
            "SELECT native_started FROM session_profile_bindings WHERE session_id=?", (session_id,)
        ).fetchone()
    assert started_row == (0,)
    monkeypatch.setattr(task_module, "MAX_OCCURRENCES", 0)
    with stack.store._connection(write=True) as connection:
        runner.store.prune(connection, routine_id)
        assert not connection.execute(
            "SELECT 1 FROM task_attempts WHERE session_id=?", (session_id,)
        ).fetchall()
    assert stack.origin(session_id) == "routine"
    review = await stack.client.get(f"/api/v2/profile-sessions/{session_id}", headers=AUTH)
    assert review.status_code == 200 and "code_copy" not in review.json()
    stack.refuse_open = False
    if stack.manager.get(session_id) is None:
        stack.manager.attach_prepared(session_id, profile=True)
    ok, actor = await stack.send(session_id)
    assert ok, errors(actor)
    assert stack.drivers[-1].launch.code_worktree is None and "code_copy" not in started(actor)
    assert stack.bindings() == {} and stack.store.worktrees_in_states(("ready",)) == []


# --- release after commit --------------------------------------------------------------------


async def test_a_deleted_bots_running_copy_is_kept_until_its_close_completes(
    stack: Stack, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)
    bot = await stack.bot()
    session_id = (await stack.chat(bot))["session_id"]
    ok, actor = await stack.send(session_id)
    assert ok, errors(actor)
    driver = stack.drivers[-1]
    path = Path(driver.launch.code_worktree or "")
    worktree_id = path.name
    removed = await stack.client.request(
        "DELETE",
        f"/api/v2/bots/{bot['bot_id']}",
        headers=WRITE,
        json={"expected_state_revision": bot["state_revision"]},
    )
    assert removed.status_code == 200, removed.text
    # Membership is gone, the driver still runs: the scheduled release keeps the copy.
    await until(lambda: kept_logs(caplog) >= 1)
    assert stack.bindings() == {worktree_id: "released"} and path.is_dir()
    driver.close_gate.clear()
    closing = asyncio.create_task(stack.manager.close(session_id))
    await asyncio.wait_for(driver.closing.wait(), HANG_GUARD_S)
    assert not actor.live
    # Any committed deletion schedules the same release; this one finds the close unfinished.
    unrelated = await stack.client.request(
        "DELETE",
        f"/api/v2/session-metadata/{ident()}",
        headers=WRITE,
        json={"expected_revision": 0},
    )
    assert unrelated.status_code == 404
    await until(lambda: kept_logs(caplog) >= 2)
    assert stack.bindings() == {worktree_id: "released"} and path.is_dir()
    driver.close_gate.set()
    await closing
    again = await stack.client.request(
        "DELETE",
        f"/api/v2/session-metadata/{ident()}",
        headers=WRITE,
        json={"expected_revision": 0},
    )
    assert again.status_code == 404
    await until(lambda: stack.bindings() == {worktree_id: "former"})
    row = stack.store.get_worktree(worktree_id)
    assert row is not None and row["state"] == "removed" and not path.exists()
    assert_code_only_logs(caplog, worktree_id, row["branch"], str(path))


async def test_a_deleted_chat_label_releases_its_copy_after_commit(stack: Stack) -> None:
    bot = await stack.bot()
    session_id = (await stack.chat(bot))["session_id"]
    ok, _actor = await stack.send(session_id)
    assert ok
    await stack.manager.close(session_id)
    worktree_id = Path(stack.drivers[-1].launch.code_worktree or "").name
    url = f"/api/v2/session-metadata/{session_id}"
    stale = await stack.client.request("DELETE", url, headers=WRITE, json={"expected_revision": 0})
    # A refused delete committed nothing, so it released nothing.
    assert stale.status_code == 409 and stack.bindings() == {worktree_id: "bound"}
    revision = stale.json()["error"]["current_revision"]
    deleted = await stack.client.request(
        "DELETE", url, headers=WRITE, json={"expected_revision": revision}
    )
    assert deleted.status_code == 204
    await until(lambda: stack.bindings() == {worktree_id: "former"})
    assert worktree_id not in await stack.worktrees()


async def test_a_data_reset_settles_released_copies_with_cleanup_off(stack: Stack) -> None:
    """Commands reset of a Bot's main chat and two task threads: a clean copy is removed, a
    dirty one archived (both free their slot), an in-flight chat's stays bound; a later
    category's failure does not stop the release of what the committed deletion released."""
    bot = await stack.bot()
    sessions = [(await stack.chat(bot))["session_id"]]
    for _ in range(2):
        prepared = await stack.task(bot)
        assert prepared.status_code == 200, prepared.text
        sessions.append(prepared.json()["session_id"])
    copies = []
    for session_id in sessions:
        ok, _actor = await stack.send(session_id)
        assert ok
        await stack.manager.close(session_id)
        copy = stack.drivers[-1].launch.code_worktree
        assert copy, "a missing copy must not fall back to the working directory"
        copies.append(Path(copy))
    (copies[1] / "dirty.txt").write_text("synthetic change\n", encoding="utf-8")
    ledger = CommandService(stack.state, stack.vault, owner=OWNER)
    drafts = DraftService(stack.state, stack.vault)
    for index, session_id in enumerate(sessions):
        revision = drafts.save(session_id, drafts.get(session_id).revision, "synthetic turn")
        command_id = ident()
        ledger.accept(CommandInput(command_id, session_id, GENERATION, revision.revision))
        if index < 2:
            ledger.finish(command_id, "unknown", "internal")
    stack.settings_fail = True
    response = await stack.client.post(
        "/api/v2/data/reset",
        headers=WRITE,
        json={
            "command_id": ident(),
            "categories": ["commands", "settings"],
            "confirmation": "RESET",
        },
    )
    assert response.status_code == 503
    ids = [path.name for path in copies]
    await until(lambda: stack.bindings() == {ids[0]: "former", ids[1]: "former", ids[2]: "bound"})
    listed = await stack.worktrees()
    assert ids[0] not in listed and ids[1] not in listed and ids[2] in listed
    assert not copies[0].exists() and not copies[1].exists()
    archives = await stack.client.get(
        f"/api/v2/code/{stack.folder}/worktree-archives", headers=AUTH
    )
    assert [a["worktree_id"] for a in archives.json()["archives"]] == [ids[1]]
    # The restored archive is the user's tree now: no Bot's copy, and never rebound.
    restored = await stack.client.post(
        f"/api/v2/worktrees/{ids[1]}/restore", headers=WRITE, json=stack.folder_body()
    )
    assert restored.status_code == 200, restored.text
    assert (copies[1] / "dirty.txt").is_file() and stack.bindings()[ids[1]] == "former"
    assert "bot_copy" not in (await stack.worktrees())[ids[1]]
    assert stack.bindings()[ids[2]] == "bound"


async def test_a_label_delete_notifies_only_after_its_transaction_committed(
    stack: Stack,
) -> None:
    """The route's own order, with a synchronous callback: the label is already gone."""
    scope = draft_scope(stack.vault)
    labels = SessionMetadataService(stack.store, scope)
    session_id = ident()
    labels.patch(session_id, 0, {"title": "Synthetic chat"})
    seen: list[bool] = []
    routes = session_metadata_routes(
        labels,
        authorized=lambda request: request.headers.get("authorization") == AUTH["authorization"],
        origins=frozenset({BASE}),
        workers=set(),
        copies_released=lambda: seen.append(
            stack.store.get_session_metadata(scope, session_id) is None
        ),
    )
    transport = httpx.ASGITransport(app=Starlette(routes=routes))
    async with httpx.AsyncClient(transport=transport, base_url=BASE) as client:
        url = f"/api/v2/session-metadata/{session_id}"
        stale = await client.request("DELETE", url, headers=WRITE, json={"expected_revision": 9})
        assert stale.status_code == 409 and seen == []  # rolled back: nothing to settle
        done = await client.request("DELETE", url, headers=WRITE, json={"expected_revision": 1})
    assert done.status_code == 204 and seen == [True]


async def test_a_routine_chat_reopens_and_continues_without_a_copy_or_promotion(
    stack: Stack,
) -> None:
    """A routine's chat with its attempt retained: continuing it later works, natively and
    with no copy, and an interactive replay of its preparation never promotes the marker."""
    bot = await stack.bot()
    routines = stack.app.state.routines
    raw = _document_raw(
        profile_id=bot["profile_id"], workspace_id=stack.workspace_id, schedule=_manual_schedule()
    )
    routine_id = routines.create(ident(), raw)[0]["routine_id"]
    runner = stack.app.state.routine_tasks
    stack.script = (TurnDone(status="success", usage=Usage()),)
    await runner.run_now(routine_id, ident(), 1)
    await asyncio.wait_for(asyncio.gather(*list(runner._runs)), HANG_GUARD_S)
    stack.script = ()
    [run] = runner.store.runs(routine_id, 1, None)
    attempt = run["attempt"]
    assert attempt["outcome"] == "succeeded"
    session_id = attempt["session_id"]
    assert stack.origin(session_id) == "routine" and stack.drivers[-1].launch.code_worktree is None
    replay = await stack.client.post(
        f"/api/v2/profiles/{bot['profile_id']}/start-preview",
        headers=WRITE,
        json={
            "command_id": attempt["attempt_id"],
            "session_id": session_id,
            "expected_profile_revision": 1,
            "workspace_id": stack.workspace_id,
            "bot_id": bot["bot_id"],
        },
    )
    assert replay.status_code == 409 and replay.json()["error"]["code"] == "command_conflict"
    assert stack.origin(session_id) == "routine"
    if stack.manager.get(session_id) is None:
        stack.manager.open(session_id, profile=True)
    ok, actor = await stack.send(session_id)
    assert ok, errors(actor)
    assert stack.drivers[-1].launch.code_worktree is None and "code_copy" not in started(actor)
    assert stack.origin(session_id) == "routine" and stack.bindings() == {}
