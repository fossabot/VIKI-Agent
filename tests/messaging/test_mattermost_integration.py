"""Synthetic launch, pinned-root and lifespan proofs; no provider or remote connection."""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
import math
import os
import uuid
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from starlette.requests import Request

from tests.app.test_workspaces import COMMAND, service_at
from tests.messaging.test_mattermost_bot import CONFIG, HANG_GUARD_S, Driver, Http, take
from tests.test_main import FakeChild, FakeServer, _cfg, _fake_edge, _reaching_the_server
from tests.test_server import EmptyCatalog
from tests.workspace_code.test_workspaces_code import trusted
from viki_agent import __main__ as launcher
from viki_agent import server, winproc
from viki_agent.app import provider_login
from viki_agent.code import code_checks, terminal_launch
from viki_agent.code.check_runs import CheckRunner
from viki_agent.code.code_files import CodeFiles
from viki_agent.code.code_git import CodeGit
from viki_agent.code.preview_windows import PreviewWindows
from viki_agent.code.previews import PreviewService
from viki_agent.code.session_roots import SessionRoots
from viki_agent.conversations import approval_runtime
from viki_agent.drivers.base import PermissionAnswer, QuestionAnswer, StartError
from viki_agent.drivers.claude.driver import DriverConfig
from viki_agent.drivers.claude.transport import ClaudeTransport
from viki_agent.events import Question, QuestionAsked, QuestionOption
from viki_agent.messaging import mattermost_bot
from viki_agent.messaging.mattermost_bot import MattermostBot
from viki_agent.messaging.messaging_store import MessagingConfigStore
from viki_agent.state import AppState, Prefs
from viki_agent.storage import StateError
from viki_agent.workspaces import WorkspaceError

from replay import FakeSpawner


def clean(env: Any) -> None:
    assert not any(name.upper().startswith("VIKI_MATTERMOST_") for name in env)


@pytest.mark.parametrize("configured", ["valid", "partial", "mixed", "absent"])
@pytest.mark.parametrize("branch", ["normal", "recovery", "second", "refused"])
@pytest.mark.parametrize("platform", ["win32", "linux", "darwin"])
def test_bot_secret_removed_before_all_children(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    configured: str,
    branch: str,
    platform: str,
) -> None:
    git_bin = tmp_path / "git-bin"
    git_bin.mkdir()
    git_exe = git_bin / ("git.exe" if os.name == "nt" else "git")
    git_exe.touch()
    git_exe.chmod(0o700)
    env = {"PATH": str(git_bin), "vIkI_mAtTeRmOsT_future": "synthetic-secret"}
    if configured != "absent":
        env["VIKI_MATTERMOST_URL"] = CONFIG.url
    if configured in {"valid", "mixed"}:
        env["VIKI_MATTERMOST_OWNER_ID"] = CONFIG.owner_user_id
        env["VIKI_MATTERMOST_BOT_TOKEN"] = CONFIG.bot_token
    if configured == "mixed":
        env["viki_mattermost_bot_token"] = CONFIG.bot_token
    monkeypatch.setattr(os, "environ", env)
    monkeypatch.setattr(Path, "home", classmethod(lambda _: tmp_path / "home"))
    cfg = replace(_cfg(tmp_path), cli=Path("synthetic-cli"))
    calls = _reaching_the_server(monkeypatch, cfg)
    monkeypatch.setattr(launcher, "sys", SimpleNamespace(platform=platform, argv=[], stderr=None))
    monkeypatch.setattr(launcher, "_STOP_SIGNALS", (), raising=False)
    monkeypatch.setattr(launcher, "_server", lambda conf, **_: FakeServer(conf))
    monkeypatch.setattr(launcher.config, "data_dir", lambda env: tmp_path / "data")
    monkeypatch.setattr(launcher, "_raise_nofile_limit", lambda: None)
    monkeypatch.setattr(launcher.protection, "visit_key_source", lambda: None)
    monkeypatch.setattr(launcher.protection, "keychain_source", lambda _: None)
    monkeypatch.setattr(launcher.protection, "set_key_source", lambda _: None)
    monkeypatch.setattr(launcher, "user_defaults", lambda *_, **__: Prefs())
    monkeypatch.setattr(
        winproc,
        "single_instance",
        lambda _: None if branch == "second" else SimpleNamespace(release=lambda: None),
    )
    monkeypatch.setattr(launcher, "_wake_running_instance", lambda: True)
    monkeypatch.setattr(launcher, "_browser_left_open", lambda _: False)
    monkeypatch.setattr(winproc.Signal, "create", lambda _: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(launcher, "_alert", lambda _: clean(os.environ))
    inherited: list[dict[str, str]] = []

    def run(*_: Any, **__: Any) -> Any:
        clean(os.environ)
        inherited.append(dict(os.environ))
        return SimpleNamespace(stdout="2.1.79", stderr="", returncode=0)

    monkeypatch.setattr(winproc, "run", run)
    monkeypatch.setattr(launcher, "git_version", lambda: winproc.git(tmp_path, "--version"))
    if branch == "recovery":
        monkeypatch.setattr(
            launcher, "AppState", lambda *_: (_ for _ in ()).throw(StateError("corrupt"))
        )
    if branch == "refused":
        monkeypatch.setattr(launcher.config, "load", lambda _: replace(cfg, edge=None))
    children, spawned = _fake_edge(monkeypatch)
    assert launcher.main(["--dev"]) == (5 if branch == "refused" else 0)
    clean(os.environ)
    assert CONFIG.bot_token not in caplog.text and CONFIG.url not in caplog.text
    if branch in {"second", "refused"}:
        assert not calls and not children
        return
    key = "create_recovery_app" if branch == "recovery" else "create_app"
    made = calls[key]
    assert CONFIG.bot_token not in repr(asdict(made["config"]))
    assert CONFIG.url not in repr(asdict(made["config"]))
    assert "mattermost" not in made
    clean(made["windows"]._env)
    if branch == "normal":
        clean(made["manager"]._config.env)
    assert inherited

    async def browser() -> None:
        await made["windows"].start()
        clean(spawned["env"])
        await made["windows"].stop()

    monkeypatch.setattr(launcher, "_keep_icon", lambda _: 0)
    monkeypatch.setattr(launcher, "_redirect_url", lambda *_: "file:///synthetic.html")
    asyncio.run(browser())


class Integration:
    def __init__(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        kind: str = "code",
        *,
        windows: Any = None,
    ) -> None:
        self.state = AppState(tmp_path / "data" / "state.json", Prefs(mode="auto"))
        self.spaces = service_at(tmp_path, self.state.store)
        root = tmp_path / "workspace"
        root.mkdir()
        if kind == "code":
            wid = trusted(self.spaces, root)
            self.spaces.select(COMMAND, wid)
        elif kind == "vault":
            workspace, _ = self.spaces.register_existing(COMMAND, str(root))
            self.spaces.select(COMMAND, workspace.workspace_id)
        elif kind == "ephemeral":
            self.spaces = service_at(tmp_path, self.state.store, env={"VIKI_VAULT": str(root)})
        self.active = self.spaces.resolve_active()
        self.drivers: list[tuple[Any, Prefs, Driver]] = []

        def driver(launch: Any, prefs: Prefs, emit: Any) -> Driver:
            made = Driver(emit)
            self.drivers.append((launch, prefs, made))
            return made

        self.manager = launcher.SessionManager(
            config=DriverConfig(cli=("must-not-run",), vault=self.active.cwd, env={}),
            state=self.state,
            claude_home=tmp_path / "home",
            make_driver=driver,
        )
        self.http = Http()
        self.bots: list[MattermostBot] = []
        self.bot: MattermostBot

        def bot(*args: Any, **kwargs: Any) -> MattermostBot:
            made = MattermostBot(*args, **kwargs)
            self.bots.append(made)
            return made

        async def posted(*_: Any, **__: Any) -> Any:
            await asyncio.Event().wait()
            yield {}

        monkeypatch.setattr(mattermost_bot, "posted", posted)
        monkeypatch.setattr(server, "MattermostHttp", lambda _: self.http, raising=False)
        monkeypatch.setattr(server, "MattermostBot", bot, raising=False)
        MessagingConfigStore(self.state.store).save("mattermost", CONFIG)
        self.app = server.create_app(
            config=replace(_cfg(tmp_path), vault=self.active.cwd),
            token="synthetic-local-token",
            manager=self.manager,
            catalog=EmptyCatalog(tmp_path / "home", self.active.cwd),
            static_dir=None,
            workspaces=self.spaces,
            windows=windows,
        )

    async def connect(self) -> None:
        await self.app.state.messaging_runtime.set_enabled("mattermost", True)
        self.bot = self.bots[-1]

    async def close(self) -> None:
        await self.app.state.messaging_runtime.close()
        await self.app.state.approvals.close()
        await self.manager.close_all()

    async def chat(self) -> tuple[Any, Driver]:
        await self.bot._intake({"message": "Synthetic prompt", "id": "p" * 26})
        assert self.bot._native_task is not None
        await asyncio.wait_for(self.bot._native_task, HANG_GUARD_S)
        assert self.bot._actor is not None
        return self.bot._actor, self.drivers[-1][2]


@pytest.mark.parametrize("kind", ["code", "vault", "ephemeral", "scratch"])
async def test_bot_pins_root_and_auto_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    rig = Integration(tmp_path, monkeypatch, kind)
    await rig.connect()
    rig.spaces.select(COMMAND, None)
    rig.state.set_defaults(Prefs(mode="acceptEdits"))
    original = rig.manager.create
    remember = rig.state.remember

    def persist(sid: str, **kwargs: Any) -> Prefs:
        if kwargs.get("mode") == "auto":
            assert (rig.state.store.get_session_root(sid) is not None) == (kind == "code")
            assert rig.manager.get(sid) is None
        return remember(sid, **kwargs)

    def create(sid: str, **kwargs: Any) -> Any:
        assert str(uuid.UUID(sid)) == sid and rig.state.prefs(sid).mode == "auto"
        row = rig.state.store.get_session_root(sid)
        assert (row is not None) == (kind == "code")
        return original(sid, **kwargs)

    monkeypatch.setattr(rig.manager, "create", create)
    monkeypatch.setattr(rig.state, "remember", persist)
    try:
        actor = await rig.bot._create_actor()
        assert not actor.live
        assert await actor.start()
        launch, prefs, _ = rig.drivers[-1]
        assert launch.cwd == rig.active.cwd and prefs.mode == "auto"
        assert actor._root_resolver is not rig.manager._resolve_root
    finally:
        await rig.close()


@pytest.mark.parametrize("phase", ["create", "spawn", "respawn"])
@pytest.mark.parametrize(
    "drift",
    ["revision", "policy_revision", "policy", "kind", "removed", "physical", "missing", "alias"],
)
async def test_bot_refuses_workspace_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    drift: str,
) -> None:
    rig = Integration(tmp_path, monkeypatch)
    await rig.connect()
    actor = await rig.bot._create_actor() if phase != "create" else None
    if phase == "respawn":
        assert actor is not None and await actor.start()
        await actor._retire(finish_cleanup=True)
    captured = rig.active.workspace
    if drift in {"physical", "missing"}:
        rig.active.cwd.rename(tmp_path / "old-root")
        if drift == "physical":
            rig.active.cwd.mkdir()
    elif drift == "alias":
        monkeypatch.setattr(
            rig.spaces, "current_root", lambda _: replace(captured.root, aliased=True)
        )
    elif drift == "removed":
        monkeypatch.setattr(
            rig.spaces,
            "get",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(WorkspaceError("unknown_workspace")),
        )
    else:
        changed = {
            "revision": 99,
            "policy_revision": 99,
            "policy": "existing_readonly",
            "kind": "vault",
        }
        original = rig.spaces.get
        monkeypatch.setattr(
            rig.spaces,
            "get",
            lambda *args, **kwargs: replace(original(*args, **kwargs), **{drift: changed[drift]}),
        )
    try:
        if actor is None:
            with pytest.raises(StartError):
                await rig.bot._create_actor()
        else:
            assert not await actor.start()
        assert len(rig.drivers) == (1 if phase == "respawn" else 0)
    finally:
        await rig.close()


async def test_missing_scratch_is_not_recreated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = Integration(tmp_path, monkeypatch, "scratch")
    await rig.connect()
    rig.active.cwd.rmdir()
    try:
        with pytest.raises(StartError):
            await rig.bot._create_actor()
        assert not rig.active.cwd.exists()
    finally:
        await rig.close()


@pytest.mark.parametrize("cancelled", [False, True])
async def test_failed_actor_factory_closes_half_created_actor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancelled: bool
) -> None:
    rig = Integration(tmp_path, monkeypatch)
    await rig.connect()
    original = rig.manager.create
    created: list[str] = []

    def failed(sid: str, **kwargs: Any) -> Any:
        original(sid, **kwargs)
        created.append(sid)
        raise asyncio.CancelledError if cancelled else StateError("unavailable")

    monkeypatch.setattr(rig.manager, "create", failed)
    try:
        with pytest.raises(asyncio.CancelledError if cancelled else StateError):
            await rig.bot._create_actor()
        assert created and rig.manager.get(created[0]) is None and not rig.drivers
    finally:
        await rig.close()


async def test_bridge_failure_preserves_desktop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    rig = Integration(tmp_path, monkeypatch)
    await rig.connect()
    order: list[str] = []
    entered = asyncio.Event()

    async def run() -> None:
        order.append("run")
        entered.set()
        raise RuntimeError(f"{CONFIG.bot_token} {CONFIG.url} synthetic response body")

    async def close() -> None:
        order.append("bot")
        await MattermostBot.close(rig.bot)

    monkeypatch.setattr(rig.bot, "run", run)
    monkeypatch.setattr(rig.bot, "close", close)
    original = rig.app.state.approvals.close

    async def approvals() -> None:
        order.append("approvals")
        await original()

    monkeypatch.setattr(rig.app.state.approvals, "close", approvals)
    async with rig.app.router.lifespan_context(rig.app):
        await asyncio.wait_for(entered.wait(), 0.05)
        assert order == ["run"]
        actor = rig.manager.create(str(uuid.uuid4()))
        assert await actor.start()
    assert order == ["run", "bot", "approvals"] and rig.http.closed
    assert CONFIG.bot_token not in caplog.text and CONFIG.url not in caplog.text


@pytest.mark.parametrize("pause_at", ["bot", "windows", "tasks"])
async def test_cancelled_lifespan_finishes_all_cleanup_before_propagating(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    pause_at: str,
) -> None:
    order: list[str] = []
    entered, release, running = asyncio.Event(), asyncio.Event(), asyncio.Event()
    task_closed = asyncio.Event()

    async def record(name: str) -> None:
        order.append(name)
        if name == pause_at:
            entered.set()
            await release.wait()

    class Windows:
        count = 1
        admission_lock = asyncio.Lock()
        closed = False

        async def start(self) -> None:
            pass

        async def stop(self) -> None:
            await record("windows")
            self.closed = True

    windows = Windows()
    rig = Integration(tmp_path, monkeypatch, windows=windows)
    await rig.connect()
    originals = rig.http.close, rig.app.state.approvals.close, rig.manager.close_all
    close_tasks = server.TaskRunner.close

    async def posted(*_: Any, **__: Any) -> Any:
        running.set()
        await asyncio.Event().wait()
        yield {}

    async def http_close() -> None:
        await record("bot")
        await originals[0]()

    async def approvals_close() -> None:
        await record("approvals")
        await originals[1]()

    async def manager_close() -> None:
        await record("manager")
        await originals[2]()

    async def tasks_close(tasks: Any) -> None:
        await record("tasks")
        await close_tasks(tasks)
        task_closed.set()

    monkeypatch.setattr(mattermost_bot, "posted", posted)
    monkeypatch.setattr(rig.http, "close", http_close)
    monkeypatch.setattr(rig.app.state.approvals, "close", approvals_close)
    monkeypatch.setattr(rig.manager, "close_all", manager_close)
    monkeypatch.setattr(server.TaskRunner, "close", tasks_close)
    before = asyncio.all_tasks()
    context = rig.app.router.lifespan_context(rig.app)
    await context.__aenter__()
    await asyncio.wait_for(running.wait(), HANG_GUARD_S)
    _, driver = await rig.chat()
    driver.ask()
    sibling = rig.manager.create(str(uuid.uuid4()))
    assert await sibling.start()
    owned = asyncio.all_tasks() - before
    exiting = asyncio.create_task(context.__aexit__(None, None, None))
    checkpoint = asyncio.Event()
    snapshots: list[tuple[bool, list[str]]] = []

    def observe_disarm() -> None:
        snapshots.append((rig.bot._armed, driver.pending()))
        checkpoint.set()

    asyncio.get_running_loop().call_soon(observe_disarm)
    try:
        await asyncio.wait_for(checkpoint.wait(), 0.05)
        assert snapshots == [(False, [])]
        assert driver.answers.get_nowait()[1] == PermissionAnswer(False)
        await asyncio.wait_for(entered.wait(), HANG_GUARD_S)
        for _ in range(2):
            exiting.cancel()
            checkpoint.clear()
            asyncio.get_running_loop().call_soon(checkpoint.set)
            await asyncio.wait_for(checkpoint.wait(), 0.05)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(exiting, HANG_GUARD_S)
        assert order == ["bot", "windows", "approvals", "manager", "tasks"]
        assert rig.app.state.approvals._closing and not sibling.live and rig.http.closed
        assert windows.closed and task_closed.is_set()
        assert all(task.done() for task in owned)
    finally:
        release.set()
        await asyncio.gather(exiting, return_exceptions=True)
        await rig.bot.close()
        await originals[1]()
        await originals[2]()
        for task in owned:
            task.cancel()
        await asyncio.gather(*owned, return_exceptions=True)


async def test_last_window_settle_disarms_before_grace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = Integration(tmp_path, monkeypatch)
    await rig.connect()
    _, driver = await rig.chat()
    driver.ask()
    assert driver.pending()
    try:
        await rig.manager.settle(0)
        assert not rig.bot._armed
        assert (await take(driver.answers))[1] == PermissionAnswer(False)
    finally:
        await rig.close()


@pytest.mark.parametrize("pause_at", [1, 2, 3])
async def test_settle_blocks_allow_after_approval_storage_wait(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    pause_at: int,
) -> None:
    rig = Integration(tmp_path, monkeypatch)
    await rig.connect()
    _, driver = await rig.chat()
    driver.ask()
    nonce = next(iter(rig.bot._cards))
    rig.bot._cards[nonce].deadline = rig.bot._clock() + 120
    entered, release = asyncio.Event(), asyncio.Event()
    original, calls = approval_runtime.storage_call, 0

    async def paused(operation: Any, observer: Any = None) -> Any:
        nonlocal calls
        result = await original(operation, observer)
        calls += 1
        if calls == pause_at:
            entered.set()
            await release.wait()
        return result

    monkeypatch.setattr(approval_runtime, "storage_call", paused)
    choice = asyncio.create_task(
        rig.bot._intake(
            {"message": f"!viki allow {nonce}", "id": "a" * 26},
            received_card=rig.bot._cards[nonce],
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), HANG_GUARD_S)
        # Retention would skip the manager's generic deny; the composed bot hook still denies.
        rig.manager.bind_retain(lambda _: True)
        await rig.manager.settle(0.05)
        assert (await take(driver.answers))[1] == PermissionAnswer(False)
        assert not rig.bot._armed
        driver.ask("late")
        assert (await take(driver.answers))[1] == PermissionAnswer(False)
        release.set()
        await asyncio.wait_for(choice, HANG_GUARD_S)
        assert driver.answers.empty()
    finally:
        release.set()
        await asyncio.gather(choice, return_exceptions=True)
        await rig.close()


async def test_auto_bridge_still_posts_cards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = Integration(tmp_path, monkeypatch)
    await rig.connect()
    _, driver = await rig.chat()
    assert rig.drivers[-1][1].mode == "auto"
    driver.ask()
    nonce = next(iter(rig.bot._cards))
    try:
        detail = (await take(rig.http.sent))[0]
        assert "synthetic hidden argument" in detail
        instructions = (await take(rig.http.sent))[0]
        assert f"Allow once: !viki allow {nonce}" in instructions
        assert f"Deny: !viki deny {nonce}" in instructions
        card = rig.bot._cards[nonce]
        if card.deadline is None:
            card.deadline = rig.bot._clock() + 120
        await rig.bot._intake(
            {"message": f"!viki allow {nonce}", "id": "a" * 26}, received_card=card
        )
        assert (await take(driver.answers))[1] == PermissionAnswer(True)
        await take(rig.http.sent)  # the allow choice's receipt reply
        driver.ask("q", question=True)
        question_nonce = next(iter(rig.bot._cards))
        await take(rig.http.sent)  # the question's detail post
        question_post = (await take(rig.http.sent))[0]
        assert f"!viki answer {question_nonce}" in question_post
    finally:
        await rig.close()


async def test_desktop_question_reaches_the_dm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Through real create_app wiring, manager.bind_observer(messaging_runtime.observe), installed
    next to bind_settle, carries a desktop chat's own question (not the bridge's own
    conversation) to the connected DM once `!viki questions on` was sent, and a pick answers it
    exactly as a desktop Submit would."""
    rig = Integration(tmp_path, monkeypatch)
    await rig.connect()
    await rig.bot._intake({"message": "!viki questions on", "id": "o" * 26})
    await take(rig.http.sent)  # the opt-in confirmation

    async def root_resolver(_session_id: str) -> Path | None:
        return rig.active.cwd

    session_id = str(uuid.uuid4())
    rig.state.remember(session_id, mode="default")
    desktop = rig.manager.create(session_id, root_resolver=root_resolver)
    assert await desktop.send("Desktop synthetic prompt")
    driver = rig.drivers[-1][2]
    assert await take(driver.sent) == "Desktop synthetic prompt"
    tool_id = "desk"
    driver.inputs[tool_id] = {
        "tool": "AskUserQuestion",
        "input": {"hidden": "synthetic hidden argument"},
    }
    driver.emit(
        QuestionAsked(
            tool_use_id=tool_id,
            questions=[
                Question(
                    header="",
                    question="Which?",
                    options=[QuestionOption(label="One"), QuestionOption(label="Two")],
                )
            ],
        )
    )
    nonce, card = next(iter(rig.bot._desktop.items()))
    try:
        detail, detail_root = await take(rig.http.sent)
        assert "Question from a desktop chat" in detail and "Which?" in detail
        assert detail_root == ""  # top-level, outside the bridge's own conversation
        instructions, instructions_root = await take(rig.http.sent)
        assert f"!viki pick {nonce}" in instructions and instructions_root == ""
        if card.deadline is None:
            card.deadline = math.inf
        await rig.bot._intake(
            {"message": f"!viki pick {nonce} 2", "id": "p" * 26}, received_card=card
        )
        assert (await take(driver.answers))[1] == QuestionAnswer({"Which?": "Two"}, "")
        await take(rig.http.sent)  # the pick's receipt reply
    finally:
        await rig.close()


def test_create_app_takes_no_launch_messaging_settings() -> None:
    parameters = inspect.signature(server.create_app).parameters
    assert not {"mattermost", "slack_config", "discord_config"} & parameters.keys()


def test_runtime_httpx2_imports_are_the_fixed_client_and_model_gateway() -> None:
    root = Path(server.__file__).parent
    imports = []
    for file in root.rglob("*.py"):
        for node in ast.walk(ast.parse(file.read_text(encoding="utf-8"))):
            names = (
                [name.name for name in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else []
            )
            if any(name == "httpx2" or name.startswith("httpx2.") for name in names):
                imports.append(file.relative_to(root).as_posix())
    assert sorted(imports) == ["llm/gateway.py", "llm/magpie.py", "messaging/mattermost_http.py"]


async def test_check_preview_cli_and_console_callers_scrub_bridge_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = Integration(tmp_path, monkeypatch)
    await rig.connect()
    monkeypatch.setattr(
        os,
        "environ",
        {
            "VIKI_MATTERMOST_BOT_TOKEN": CONFIG.bot_token,
            "vIkI_mAtTeRmOsT_future": "synthetic-secret",
        },
    )
    monkeypatch.setattr(Path, "home", classmethod(lambda _: tmp_path / "home"))
    monkeypatch.setattr(code_checks, "CHECKS_SUPPORTED", True)
    monkeypatch.setattr(provider_login, "LOGIN_SUPPORTED", True)
    monkeypatch.setattr(terminal_launch, "TERMINAL_SUPPORTED", True)
    seen: list[str] = []

    def start(name: str, *, cwd: Any, env: Any) -> Any:
        clean(env)
        seen.append(name)
        return SimpleNamespace(kill_tree=lambda: None, release=lambda: None)

    monkeypatch.setattr(winproc, "start_shell", start)
    monkeypatch.setattr(winproc, "open_console", lambda *_, **kwargs: start("console", **kwargs))
    monkeypatch.setattr(
        winproc, "open_terminal", lambda cwd, env: start("terminal", cwd=cwd, env=env)
    )
    roots = SessionRoots(rig.state.store, rig.spaces, exists=lambda _: False, actor=rig.manager.get)
    code_git = CodeGit(rig.spaces, CodeFiles(rig.spaces), git_available=lambda: False)
    checks = CheckRunner(rig.state.store, rig.spaces, code_git, roots)
    monkeypatch.setattr(checks, "_snapshot", lambda *_: (None, None))
    wid = rig.active.workspace.workspace_id
    check = code_checks.CheckService(rig.state.store, rig.spaces).add_check(
        wid, "Synthetic", "check", 600
    )
    row = rig.state.store.get_check(check.check_id)
    assert row is not None
    checks._launch(str(uuid.uuid4()), row, None, "check")
    previews = PreviewService(rig.state.store, rig.spaces, roots, PreviewWindows(None, tmp_path))
    previews._launch(wid, None, "preview")

    closed = asyncio.Event()

    async def spawn(*_: Any, **kwargs: Any) -> Any:
        clean(kwargs["env"])
        seen.append("preview-browser")
        child = FakeChild()
        child.exited.set()
        return child

    windows = PreviewWindows(tmp_path / "synthetic-browser", tmp_path / "data", spawn=spawn)
    monkeypatch.setattr(windows, "_refusing_proxy", lambda: "synthetic-proxy")
    await windows.open("https://preview.invalid", allow=(), owner="synthetic", on_closed=closed.set)
    await asyncio.wait_for(closed.wait(), HANG_GUARD_S)
    await windows.close_all()

    fake = FakeSpawner([])

    async def cli(argv: Any, cwd: Any, env: Any) -> Any:
        clean(env)
        seen.append("cli")
        return await fake(argv, cwd, env)

    transport = ClaudeTransport(
        ["synthetic-cli"],
        cwd=rig.active.cwd,
        env=winproc.child_env("sdk-py"),
        sink=lambda *_: None,
        spawner=cli,
        grace_s=0.05,
    )
    await transport.connect()
    await transport.close()
    origins = frozenset({"http://synthetic.invalid"})
    workers: set[asyncio.Task[Any]] = set()

    async def receive() -> Any:
        return {
            "type": "http.request",
            "body": json.dumps({"worktree_id": None}).encode(),
            "more_body": False,
        }

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "headers": [
                (b"origin", b"http://synthetic.invalid"),
                (b"content-type", b"application/json"),
            ],
            "query_string": b"",
            "path_params": {"workspace_id": wid},
        },
        receive,
    )
    login = provider_login.provider_login_routes(
        cli=lambda: tmp_path / "synthetic-cli",
        authorized=lambda _: True,
        origins=origins,
        workers=workers,
    )
    terminal = terminal_launch.terminal_launch_routes(
        rig.state.store,
        rig.spaces,
        roots,
        authorized=lambda _: True,
        origins=origins,
        workers=workers,
    )
    try:
        assert (await login[0].endpoint(request)).status_code == 200
        assert (await terminal[0].endpoint(request)).status_code == 200
        assert seen == ["check", "preview", "preview-browser", "cli", "console", "terminal"]
        assert all(child.released for child in fake.children)
    finally:
        await asyncio.gather(*workers, return_exceptions=True)
        await rig.close()
