"""The Decision loop wired into the app: its routes in place of the earlier ones, the lifespan's
order (VIKI's chats reconciled before the engine starts, the engine stopped before the model
closes, also when startup fails partway), the first window waking the engine, a Decision left
off asking the model nothing across startup and a few wakes while the same wakes ask it once
Decision is on, a round trip with the app's Bearer token, the decision inbox listing the active
workspace's question and change proposals, a vault whose files stay as they were while a cycle
files change proposals and the user acts on them, and a skill of the vault suggested for the
chat in view with nothing about it sent. The app runs on the test's event loop through httpx's
ASGI transport, with the fake model and a fake Decision clock: no network and no port."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from starlette.applications import Starlette
from starlette.routing import Route

from tests.decision_loop.fakes import WALL, FakeClock, FakeModel
from tests.decision_loop.test_engine import PREPARE
from tests.decision_loop.test_service import PROPOSED
from tests.workspace_code.test_workspaces_code import trusted
from viki_agent import server
from viki_agent.app.settings import SettingsService
from viki_agent.automation.routines import stamp
from viki_agent.catalog import Catalog, HistoryPage
from viki_agent.config import Config
from viki_agent.conversations.limits_api import PlanLimits
from viki_agent.decision_loop import templates
from viki_agent.decision_loop.changes import ChangeProposals
from viki_agent.decision_loop.contracts import (
    COALESCE_S,
    CheckFolder,
    CheckLook,
    CodeFolder,
    CodeLook,
    DecisionLoopError,
    MissionRecord,
    canonical_json,
    item_key,
)
from viki_agent.decision_loop.engine import DecisionLoopEngine
from viki_agent.decision_loop.key_store import DecisionKeyStore
from viki_agent.decision_loop.missions import MissionService
from viki_agent.decision_loop.proposals import QuestionProposals
from viki_agent.domain import PolicyKind
from viki_agent.drivers.claude.driver import DriverConfig
from viki_agent.session import SessionManager
from viki_agent.state import AppState, Prefs
from viki_agent.vaults.capture_suggest import SuggestionRun
from viki_agent.vaults.vault_mutation import OrdinaryMutationRoot
from viki_agent.vaults.vault_operations import OperationJournal, OperationStep
from viki_agent.window import WindowManager
from viki_agent.workspaces import WorkspaceService

TOKEN = "wiring-token"
BASE = "http://127.0.0.1:8765"
ORIGIN = "http://127.0.0.1:8765"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
WRITE = {**AUTH, "Origin": ORIGIN}
PREFIX = "/api/v2/decision/"
KEY = "ts-wiring-key-4b7e"
TASK = "Water the seedlings"
TODAY = WALL.astimezone().date()  # the user's day as the engine reads the fake clock (zone None)
CALENDAR = chr(0x1F4C5)  # the due-date marker `brief.parse_tasks` reads
HANG_GUARD_S = 5.0  # bounds a wait that settles in milliseconds; it never fires in a healthy run

# The Decision routes (method, path under PREFIX), as `decision_loop_routes` serves them.
DECISION_ROUTES = {
    ("GET", "launcher"),
    ("POST", "launcher/open"),
    ("POST", "presence"),
    ("POST", "feedback"),
    ("POST", "observe"),
    ("POST", "reports"),
    ("GET", "page"),
    ("POST", "preferences"),
    ("DELETE", "episodes"),
    ("POST", "key"),
    ("DELETE", "key"),
    ("POST", "test"),
    ("GET", "calls"),
    ("GET", "calls/{call_id}"),
    ("GET", "missions"),
    ("POST", "missions"),
    ("POST", "missions/stop"),
    ("POST", "missions/{mission_id}/stop"),
    ("POST", "proposals"),
    ("GET", "proposals/{proposal_id}"),
    ("POST", "proposals/{proposal_id}"),
    ("POST", "changes/{proposal_id}"),
    ("POST", "seats/{routine_id}/start"),
    ("POST", "seats/{routine_id}/stop"),
    ("POST", "checks/{workspace_id}/run"),
    ("POST", "checks/{workspace_id}/stop"),
    ("POST", "inbox/suggest"),
    ("GET", "data"),
    ("POST", "data/records"),
    ("POST", "data/record"),
    ("POST", "data/delete"),
}
PRESENCE: dict[str, Any] = {
    "window": "00000000-0000-4000-8000-0000000000d1",
    "focused": True,
    "typing": False,
    "chat": None,
    "chat_title": None,
    "note": None,
    "launcher": False,
    "page": None,
}


class EmptyCatalog(Catalog):
    def exists(self, session_id: str) -> bool:
        return False

    def history(
        self, session_id: str, *, before_uuid: str | None = None, until: str | None = None
    ) -> HistoryPage:
        return HistoryPage(events=[], next_before_uuid=None)


class Windows:
    """The app windows create_app reads. `start` opens the first one once `ready`, when set,
    returns; a `broken` one never opens, so startup fails partway."""

    def __init__(self, *, broken: bool = False) -> None:
        self.admission_lock = asyncio.Lock()
        self.count = 0
        self.broken = broken
        self.ready: Callable[[], Awaitable[None]] | None = None

    async def start(self) -> None:
        if self.broken:
            raise RuntimeError("no window")
        if self.ready is not None:
            await self.ready()
        self.count = 1

    async def stop(self) -> None:
        self.count = 0

    def holds(self) -> tuple[object, ...]:
        return ()


class Model(FakeModel):
    """The fake model, noting in `events` when it closes."""

    def __init__(self, events: list[str]) -> None:
        super().__init__()
        self.events = events

    async def close(self) -> None:
        self.events.append("close")
        await super().close()


@contextlib.asynccontextmanager
async def serving(app: Starlette) -> AsyncIterator[httpx.AsyncClient]:
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE) as client,
    ):
        yield client


def build(
    tmp_path: Path, *, policy: PolicyKind = "ordinary", **extra: Any
) -> tuple[Starlette, AppState, SettingsService]:
    """create_app with the services Decision reads, over one store: settings, and workspaces
    whose active one is a vault at `policy` holding one task due today. `extra` goes to
    create_app."""
    data, home, notes = tmp_path / "data", tmp_path / "home", tmp_path / "notes"
    home.mkdir(parents=True)
    (notes / "10-projects" / "plan").mkdir(parents=True)
    (notes / "10-projects" / "plan" / "tasks.md").write_text(
        f"- [ ] {TASK} {CALENDAR} {TODAY.isoformat()}\n", encoding="utf-8"
    )
    state = AppState(data / "state.json", Prefs())
    spaces = WorkspaceService(
        state.store,
        env={},
        data_dir=data,
        home=tmp_path / "profile",
        install_dir=tmp_path / "install",
        git_available=lambda: False,
        hide_previews=lambda: False,
    )
    workspace, _ = spaces.register_existing(str(uuid.uuid4()), str(notes))
    spaces.select(str(uuid.uuid4()), workspace.workspace_id)
    if policy != workspace.policy:  # registered read-only
        set_policy(tmp_path, state, policy)
    settings = SettingsService(
        state.store, provider_defaults=Prefs, on_chat_defaults=lambda _prefs: None
    )
    manager = SessionManager(
        config=DriverConfig(cli=(), vault=notes, env={}),
        state=state,
        claude_home=home,
        make_driver=lambda *_: pytest.fail("no Decision path here may start a chat"),
    )
    app = server.create_app(
        config=Config(vault=notes, claude_home=home, cli=Path("fake"), edge=None, data_dir=data),
        token=TOKEN,
        manager=manager,
        catalog=EmptyCatalog(home, notes),
        static_dir=None,
        settings=settings,
        workspaces=spaces,
        **extra,
    )
    return app, state, settings


class Rig:
    """The app over `build`, on `Model` and a fake Decision clock. The engine and the missions
    are the real ones, noting in `events` the reconcile, the engine's start and stop and (through
    the model) the close; `polls` counts the missions' polls, one at the end of each cycle."""

    def __init__(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        *,
        windows: Windows | None = None,
        refuse_reconcile: bool = False,
        policy: PolicyKind = "ordinary",
    ) -> None:
        self.events: list[str] = []
        self.polls = 0
        self.polled = asyncio.Event()
        rig = self

        class Missions(MissionService):
            async def reconcile(self) -> int:
                rig.events.append("reconcile")
                if refuse_reconcile:
                    raise DecisionLoopError("unavailable")
                ended = await super().reconcile()
                rig.events.append("reconciled")
                return ended

            async def poll(self, now: datetime) -> list[MissionRecord]:
                ended = await super().poll(now)
                rig.polls += 1
                rig.polled.set()
                return ended

        class Engine(DecisionLoopEngine):
            def start(self) -> None:
                rig.events.append("start")
                super().start()

            async def stop(self) -> None:
                rig.events.append("stop")
                await super().stop()
                rig.events.append("stopped")

        monkeypatch.setattr(server, "MissionService", Missions)
        monkeypatch.setattr(server, "DecisionLoopEngine", Engine)
        self.model = Model(self.events)
        self.clock = FakeClock()
        self.app, self.state, self.settings = build(
            tmp_path,
            policy=policy,
            windows=cast(WindowManager, windows) if windows is not None else None,
            decision_model=self.model,
            decision_clock=self.clock,
        )

    async def cycles(self, count: int) -> None:
        """Until the engine has run `count` cycles: each ends polling the missions."""
        async with asyncio.timeout(HANG_GUARD_S):
            while self.polls < count:
                self.polled.clear()
                await self.polled.wait()

    async def wakes(self, client: httpx.AsyncClient) -> None:
        """Startup's cycle, then a presence change, the launcher's open and a key save, each
        with the cycle it wakes."""
        await self.cycles(1)
        reported = await client.post(f"{PREFIX}presence", headers=WRITE, json=PRESENCE)
        assert reported.status_code == 204
        self.clock.advance(COALESCE_S)
        await self.cycles(2)
        opened = await client.post(f"{PREFIX}launcher/open", headers=WRITE)
        assert opened.status_code == 200
        await self.cycles(3)
        saved = await client.post(f"{PREFIX}key", headers=WRITE, json={"key": KEY})
        assert saved.status_code == 200
        self.clock.advance(COALESCE_S)
        await self.cycles(4)


def served(app: Starlette) -> set[tuple[str, str]]:
    return {
        (method, route.path)
        for route in app.routes
        if isinstance(route, Route)
        for method in route.methods or ()
        if method != "HEAD"
    }


def test_decision_routes_replace_jev_routes(tmp_path: Path) -> None:
    """Wired as the launcher wires it, with no Decision argument at all."""
    app, _state, _settings = build(tmp_path)
    routes = served(app)
    decision = {
        (method, path.removeprefix(PREFIX)) for method, path in routes if path.startswith(PREFIX)
    }
    assert decision == DECISION_ROUTES
    assert {path for _method, path in routes if "jev" in path.casefold()} == set()


async def test_the_lifespan_reconciles_then_starts_and_stops_then_closes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = Rig(tmp_path, monkeypatch)
    async with serving(rig.app):
        await rig.cycles(1)
        assert rig.events == ["reconcile", "reconciled", "start"]
        assert not rig.model.closed
    assert rig.events == ["reconcile", "reconciled", "start", "stop", "stopped", "close"]
    assert rig.model.closed and rig.model.requests == []


async def test_a_startup_that_fails_partway_still_stops_then_closes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)
    # VIKI's chats could not be reconciled: the engine still runs, and shuts down in order.
    refused = Rig(tmp_path / "reconcile", monkeypatch, refuse_reconcile=True)
    async with serving(refused.app):
        await refused.cycles(1)
    assert refused.events == ["reconcile", "start", "stop", "stopped", "close"]
    assert "decision mission recovery unavailable" in caplog.text
    # The first window never opened: startup fails, and the engine stops before the model closes.
    broken = Rig(tmp_path / "window", monkeypatch, windows=Windows(broken=True))
    with pytest.raises(RuntimeError, match="no window"):
        async with serving(broken.app):
            pass
    assert broken.events == ["reconcile", "reconciled", "start", "stop", "stopped", "close"]
    assert broken.model.closed


async def test_a_decision_model_close_that_raises_does_not_abort_the_rest_of_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The guard around `decision_client.close()` inside `cleanup()`: a raise there is logged
    with the fixed warning, never the exception's own text, and shutdown still reaches
    `manager.close_all()`, the step closing every Claude process."""
    caplog.set_level(logging.WARNING)

    class BreaksOnClose(FakeModel):
        async def close(self) -> None:
            raise RuntimeError("decision-model-close-boom")

    events: list[str] = []
    original_close_all = SessionManager.close_all

    async def tracked_close_all(self: SessionManager) -> None:
        events.append("close_all")
        await original_close_all(self)

    monkeypatch.setattr(SessionManager, "close_all", tracked_close_all)
    app, _state, _settings = build(
        tmp_path, decision_model=BreaksOnClose(), decision_clock=FakeClock()
    )
    async with serving(app):
        pass
    assert events == ["close_all"]
    assert "decision model cleanup unavailable" in caplog.text
    assert "decision-model-close-boom" not in caplog.text


async def test_the_first_window_wakes_the_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The engine's first cycle runs while no window is open, so it decides nothing; the first
    window's opening wakes a cycle that decides, as "app_open"."""
    windows = Windows()
    rig = Rig(tmp_path, monkeypatch, windows=windows)
    windows.ready = lambda: rig.cycles(1)
    DecisionKeyStore(rig.state.store).save(KEY)
    rig.settings.patch(str(uuid.uuid4()), 0, {"decision.enabled": True})
    async with serving(rig.app):
        assert rig.model.requests == []
        rig.clock.advance(COALESCE_S)
        await rig.cycles(2)
    assert rig.model.requests
    with rig.state.store._connection() as connection:
        triggers = {row[0] for row in connection.execute("SELECT trigger FROM decision_episodes")}
    assert triggers == {"app_open"}


async def test_off_by_default_asks_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rig = Rig(tmp_path, monkeypatch)
    DecisionKeyStore(rig.state.store).save(KEY)  # the model path is open but for the switch
    async with serving(rig.app) as client:
        await rig.wakes(client)
    assert rig.polls >= 4
    assert (rig.model.requests, rig.model.keys, rig.model.listed) == ([], [], [])


async def test_on_the_same_wakes_ask_the_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control for the test above: its gather is bound to the active vault."""
    rig = Rig(tmp_path, monkeypatch)
    DecisionKeyStore(rig.state.store).save(KEY)
    rig.settings.patch(str(uuid.uuid4()), 0, {"decision.enabled": True})
    async with serving(rig.app) as client:
        await rig.wakes(client)
    assert rig.model.requests and set(rig.model.keys) == {KEY}
    candidate = rig.model.requests[0].state["candidate"]
    assert isinstance(candidate, dict) and candidate["title"] == TASK


async def test_the_launcher_answers_the_apps_bearer_with_no_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = Rig(tmp_path, monkeypatch)
    async with serving(rig.app) as client:
        refused = await client.get(f"{PREFIX}launcher")
        answered = await client.get(f"{PREFIX}launcher", headers=AUTH)
    assert refused.status_code == 401
    assert answered.status_code == 200
    assert answered.headers["cache-control"] == "no-store"
    view = answered.json()
    assert (view["state"], view["open"], view["items"], view["model"]) == ("off", 0, [], "no_key")


async def test_inbox_lists_question_proposals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The decision inbox lists, after the vault and skill proposals, the active workspace's
    question proposals, as the Decision route created them, and not another registered
    workspace's; while they cannot be read it still lists the rest."""
    vault_item: dict[str, Any] = {"kind": "vault_proposal", "id": str(uuid.uuid4())}
    monkeypatch.setattr(server.ProposalService, "inbox_items", lambda _self: [vault_item])
    rig = Rig(tmp_path, monkeypatch)
    (tmp_path / "other").mkdir()
    spaces = WorkspaceService(
        rig.state.store,
        env={},
        data_dir=tmp_path / "data",
        home=tmp_path / "profile",
        install_dir=tmp_path / "install",
        git_available=lambda: False,
        hide_previews=lambda: False,
    )
    other, _ = spaces.register_existing(str(uuid.uuid4()), str(tmp_path / "other"))
    async with serving(rig.app) as client:
        body = {"name": "goal_link", "question": PROPOSED}
        created = await client.post(f"{PREFIX}proposals", headers=WRITE, json=body)
        assert created.status_code == 200
        proposal = created.json()["proposal"]
        QuestionProposals(rig.state.store).create(other.workspace_id, "goal_link", PROPOSED)
        listed = await client.get("/api/v2/decisions", headers=AUTH)
        assert (listed.status_code, listed.json()) == (200, {"items": [vault_item, proposal]})

        def unreadable(_self: QuestionProposals, scope: str) -> list[dict[str, object]]:
            raise DecisionLoopError("unavailable")

        monkeypatch.setattr(QuestionProposals, "inbox_items", unreadable)
        rest = await client.get("/api/v2/decisions", headers=AUTH)
    assert (rest.status_code, rest.json()) == (200, {"items": [vault_item]})


async def test_proposal_read_decision_loop_error_leaves_inbox_listed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the proposal read raises a DecisionLoopError, the inbox still lists the rest."""
    vault_item: dict[str, Any] = {"kind": "vault_proposal", "id": str(uuid.uuid4())}
    monkeypatch.setattr(server.ProposalService, "inbox_items", lambda _self: [vault_item])
    rig = Rig(tmp_path, monkeypatch)
    async with serving(rig.app) as client:

        def raises_decision_error(_self: QuestionProposals, scope: str) -> list[dict[str, object]]:
            raise DecisionLoopError("unavailable")

        monkeypatch.setattr(QuestionProposals, "inbox_items", raises_decision_error)
        listed = await client.get("/api/v2/decisions", headers=AUTH)
    assert (listed.status_code, listed.json()) == (200, {"items": [vault_item]})


async def test_proposal_read_runtime_error_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the proposal read raises a plain RuntimeError (not DecisionLoopError),
    the error propagates and is not silently suppressed."""
    vault_item: dict[str, Any] = {"kind": "vault_proposal", "id": str(uuid.uuid4())}
    monkeypatch.setattr(server.ProposalService, "inbox_items", lambda _self: [vault_item])
    rig = Rig(tmp_path, monkeypatch)
    async with serving(rig.app) as client:

        def raises_plain_error(_self: QuestionProposals, scope: str) -> list[dict[str, object]]:
            raise RuntimeError("boom")

        monkeypatch.setattr(QuestionProposals, "inbox_items", raises_plain_error)
        # A plain RuntimeError propagates (it is not silently caught): the server raises, which
        # httpx reflects as an exception.
        with pytest.raises(RuntimeError, match="boom"):
            await client.get("/api/v2/decisions", headers=AUTH)


# ---- vault-change proposals ----------------------------------------------------

PLAN_TASKS = "10-projects/plan/tasks.md"
GARDEN_TASKS = "10-projects/garden/tasks.md"


def ticked(names: str) -> str:
    """Ticked task lines, one per letter of `names`."""
    return "".join(f"- [x] Ticked task {name}\n" for name in names)


def tree(root: Path) -> dict[str, str]:
    """Every folder and file under `root`, by its path from there: a file's SHA-256, size and
    modification time, a folder's mark."""
    found: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        name = path.relative_to(root).as_posix()
        if path.is_dir():
            found[name] = "folder"
        else:
            stat = path.stat()
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            found[name] = f"{digest} {stat.st_size} {stat.st_mtime_ns}"
    return found


def active_scope(tmp_path: Path, state: AppState) -> str:
    """The workspace `build` made active."""
    spaces = WorkspaceService(
        state.store,
        env={},
        data_dir=tmp_path / "data",
        home=tmp_path / "profile",
        install_dir=tmp_path / "install",
        git_available=lambda: False,
        hide_previews=lambda: False,
    )
    return spaces.resolve_active().workspace.workspace_id


def set_policy(tmp_path: Path, state: AppState, policy: PolicyKind) -> None:
    """Move the workspace `build` made active to `policy`, as Settings does, with every
    acknowledgement an ordinary vault can ask for."""
    spaces = WorkspaceService(
        state.store,
        env={},
        data_dir=tmp_path / "data",
        home=tmp_path / "profile",
        install_dir=tmp_path / "install",
        git_available=lambda: False,
        hide_previews=lambda: False,
    )
    workspace = spaces.resolve_active().workspace
    revision = spaces.get(workspace.workspace_id).policy_revision
    acknowledged = ["instructions_present", "hooks_present", "git_present"]
    spaces.set_policy(str(uuid.uuid4()), workspace.workspace_id, revision, policy, acknowledged)


def file_change(state: AppState, scope: str) -> None:
    """File an open change proposal of `scope` outside any cycle: archive the plan's ticked
    tasks."""
    ChangeProposals(state.store).file(
        scope,
        kind="tasks_ticked",
        ref=PLAN_TASKS,
        other=None,
        archive="90-archive/plan tasks done 2026.md",
        item_key=item_key("tasks_ticked", PLAN_TASKS, scope),
        episode_id=str(uuid.uuid4()),
        day=TODAY.isoformat(),
        limit=10,
        now=WALL,
    )


async def test_inbox_lists_change_proposals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The decision inbox lists the active workspace's change proposals after its question
    proposals, as `ChangeProposals.inbox_items` words them, and not another workspace's; while
    either family cannot be read (a `DecisionLoopError`) the other and the rest are still
    listed."""
    vault_item: dict[str, Any] = {"kind": "vault_proposal", "id": str(uuid.uuid4())}
    monkeypatch.setattr(server.ProposalService, "inbox_items", lambda _self: [vault_item])
    rig = Rig(tmp_path, monkeypatch)
    scope = active_scope(tmp_path, rig.state)
    file_change(rig.state, scope)
    file_change(rig.state, str(uuid.uuid4()))
    changes = ChangeProposals(rig.state.store).inbox_items(scope)
    assert [item["kind"] for item in changes] == ["change_proposal"]

    def unreadable(_self: object, scope: str) -> list[dict[str, object]]:
        raise DecisionLoopError("unavailable")

    async with serving(rig.app) as client:
        body = {"name": "goal_link", "question": PROPOSED}
        created = await client.post(f"{PREFIX}proposals", headers=WRITE, json=body)
        assert created.status_code == 200
        proposal = created.json()["proposal"]
        listed = await client.get("/api/v2/decisions", headers=AUTH)
        assert (listed.status_code, listed.json()) == (
            200,
            {"items": [vault_item, proposal, *changes]},
        )
        with monkeypatch.context() as patch:
            patch.setattr(ChangeProposals, "inbox_items", unreadable)
            rest = await client.get("/api/v2/decisions", headers=AUTH)
        assert (rest.status_code, rest.json()) == (200, {"items": [vault_item, proposal]})
        with monkeypatch.context() as patch:
            patch.setattr(QuestionProposals, "inbox_items", unreadable)
            rest = await client.get("/api/v2/decisions", headers=AUTH)
        assert (rest.status_code, rest.json()) == (200, {"items": [vault_item, *changes]})


async def test_filing_and_change_acts_leave_the_vault_tree_as_it_was(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With Decision on, a cycle over a real vault files a change proposal for each project's
    ticked tasks, which the decision inbox lists; the user approves one, drafts it again and
    rejects the other through the route, each act labelling its episode as it moves its state.
    Every folder and file of the vault is as it was before the cycle, after the filing and after
    each act: only a draft the user sends can change the vault."""
    rig = Rig(tmp_path, monkeypatch)
    notes = tmp_path / "notes"
    plan = notes / PLAN_TASKS
    plan.write_text(plan.read_text(encoding="utf-8") + ticked("abcde"), encoding="utf-8")
    (notes / GARDEN_TASKS).parent.mkdir(parents=True)
    (notes / GARDEN_TASKS).write_text("- [ ] Sow the beans\n" + ticked("fghij"), encoding="utf-8")
    (notes / "90-archive").mkdir()
    (notes / "90-archive" / "old plan.md").write_text("# Old plan\n", encoding="utf-8")
    before = tree(notes)
    async with serving(rig.app) as client:
        await rig.cycles(1)  # Decision is off: the startup cycle decides nothing
        assert tree(notes) == before
        rig.settings.patch(str(uuid.uuid4()), 0, {"decision.enabled": True})
        opened = await client.post(f"{PREFIX}launcher/open", headers=WRITE)
        assert opened.status_code == 200
        inbox = await client.get("/api/v2/decisions", headers=AUTH)
        assert inbox.status_code == 200
        rows = {
            item["ref"]: item for item in inbox.json()["items"] if item["kind"] == "change_proposal"
        }
        assert sorted(rows) == [GARDEN_TASKS, PLAN_TASKS]
        assert {row["state"] for row in rows.values()} == {"open"}
        assert tree(notes) == before

        async def act(row: dict[str, Any], action: str) -> dict[str, Any]:
            body = {"action": action, "revision": row["revision"], "reason": None}
            acted = await client.post(f"{PREFIX}changes/{row['id']}", headers=WRITE, json=body)
            assert acted.status_code == 200, acted.text
            assert tree(notes) == before
            return acted.json()

        approved = await act(rows[PLAN_TASKS], "approve")
        draft = approved["draft"]
        assert approved["change"]["state"] == "approved"
        assert f"`{PLAN_TASKS}`" in draft and "`90-archive/plan tasks done 2026.md`" in draft
        assert await act(approved["change"], "approve") == approved  # Draft again
        rejected = await act(rows[GARDEN_TASKS], "reject")
        assert (rejected["change"]["state"], rejected["draft"]) == ("rejected", None)
    assert tree(notes) == before
    with rig.state.store._connection() as connection:
        labels = connection.execute(
            "SELECT feedback, feedback_source FROM decision_episodes "
            "WHERE candidate='tasks_ticked' ORDER BY feedback"
        ).fetchall()
    assert labels == [("useful", "page"), ("wrong", "page")]


async def test_a_read_only_vault_files_no_change_proposals_until_it_is_ordinary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The filing test's vault, registered read-only: a cycle with Decision on
    files no change proposal, and the inbox lists none. Once the user makes the vault ordinary
    mid-launch, the next cycle files one for each project's ticked tasks. The vault's tree stays
    as it was throughout."""
    rig = Rig(tmp_path, monkeypatch, policy="existing_readonly")
    notes = tmp_path / "notes"
    plan = notes / PLAN_TASKS
    plan.write_text(plan.read_text(encoding="utf-8") + ticked("abcde"), encoding="utf-8")
    (notes / GARDEN_TASKS).parent.mkdir(parents=True)
    (notes / GARDEN_TASKS).write_text("- [ ] Sow the beans\n" + ticked("fghij"), encoding="utf-8")
    (notes / "90-archive").mkdir()
    (notes / "90-archive" / "old plan.md").write_text("# Old plan\n", encoding="utf-8")
    before = tree(notes)
    async with serving(rig.app) as client:

        async def changes() -> list[str]:
            opened = await client.post(f"{PREFIX}launcher/open", headers=WRITE)
            assert opened.status_code == 200
            inbox = await client.get("/api/v2/decisions", headers=AUTH)
            assert inbox.status_code == 200
            items = inbox.json()["items"]
            return sorted(item["ref"] for item in items if item["kind"] == "change_proposal")

        await rig.cycles(1)
        rig.settings.patch(str(uuid.uuid4()), 0, {"decision.enabled": True})
        assert await changes() == []
        assert tree(notes) == before
        set_policy(tmp_path, rig.state, "ordinary")
        assert await changes() == [GARDEN_TASKS, PLAN_TASKS]
        assert tree(notes) == before
    assert tree(notes) == before


# ---- a skill suggestion ------------------------------------------------------------------

CHAT = "00000000-0000-4000-8000-0000000000c3"
SKILL = "sprint-retro"
# Shares "design" and "team" with the chat's title, whose "sprint" and "retro" name the skill.
SKILL_ABOUT = "Guides a design team through a look back at what went well."
# An untitled chat's title, its first prompt: presence withholds it, the page reports it.
CHAT_TITLE = "Run the sprint retro with the design team"


def suggesting(used: list[str]) -> dict[str, Any]:
    """PRESENCE in `CHAT`, untitled, with a full page body whose skill-suggestion facts carry
    the chat's title and the skills it used."""
    page = {
        "native_waiting": 0,
        "runs": {"working": 0, "review": 0},
        "git": {"state": "unknown", "dirty": None},
        "routine": None,
        "skill": None,
        "suggest": {"session": CHAT, "title": CHAT_TITLE, "used": used},
        "second_look": None,
        "context": None,
        "sources": None,
    }
    return {**PRESENCE, "chat": CHAT, "page": page}


async def test_a_vault_skill_is_suggested_and_nothing_about_it_is_sent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With a skill in the active vault's `.claude/skills` and the chat in view reporting its
    title, the app lists one skill suggestion: the skill's name on its chip and "/sprint-retro"
    drafted into that chat; none once the chat has used the skill. With a key saved the model is
    asked about the suggestion, and no request carries the skill's name or description or the
    chat's title. Every folder and file of the vault stays as it was."""
    rig = Rig(tmp_path, monkeypatch)
    notes = tmp_path / "notes"
    skill = notes / ".claude" / "skills" / SKILL / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        f"---\nname: {SKILL}\ndescription: {SKILL_ABOUT}\n---\n\n# Sprint retro\n", encoding="utf-8"
    )
    before = tree(notes)
    async with serving(rig.app) as client:
        await rig.cycles(1)  # Decision is off: the startup cycle decides nothing
        rig.settings.patch(str(uuid.uuid4()), 0, {"decision.enabled": True})

        async def suggested(used: list[str]) -> list[dict[str, Any]]:
            """Report the chat in view having used `used`, open the launcher, which runs a cycle
            first, and answer its skill suggestions."""
            reported = await client.post(f"{PREFIX}presence", headers=WRITE, json=suggesting(used))
            assert reported.status_code == 204
            opened = await client.post(f"{PREFIX}launcher/open", headers=WRITE)
            assert opened.status_code == 200
            items = opened.json()["items"]
            return [item for item in items if item["candidate"] == "skill_suggest"]

        (item,) = await suggested([])
        assert item["evidence"] == [{"label": SKILL}]
        assert item["actions"] == [
            {
                "type": "draft_chat",
                "label": "Use this skill",
                "primary": True,
                "session_id": CHAT,
                "text": f"/{SKILL}",
            }
        ]
        assert await suggested([SKILL]) == []
        assert rig.model.requests == []  # no key yet: the rules decided
        saved = await client.post(f"{PREFIX}key", headers=WRITE, json={"key": KEY})
        assert saved.status_code == 200
        await suggested([])
    asked = [request.state.get("candidate") for request in rig.model.requests]
    # The model was asked about the task and the suggestion, which goes by its fixed title.
    assert {found["title"] for found in asked if isinstance(found, dict)} == {
        TASK,
        "A skill for this chat",
    }
    unsent = [text.casefold() for text in (SKILL, SKILL_ABOUT, CHAT_TITLE, "retro")]
    for request in rig.model.requests:
        sent = (canonical_json(request.state) + canonical_json(request.questions)).casefold()
        assert [text for text in unsent if text in sent] == []
    assert tree(notes) == before


# ---- message drafts -----------------------------------------------------------------

TURN = "00000000-0000-4000-8000-0000000000e7"


async def test_message_drafts_wait_in_the_launcher_and_nothing_about_the_chat_is_sent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the chat in view 85% full and its last reply having fetched pages and read no note,
    the launcher lists a handoff draft and a literature-note draft, each drafted into that chat.
    With a key saved the model is asked about them by their type words only: no request carries
    the chat's id, the turn's id, the share or the workspace, and no question carries an id. No
    mission starts and the vault stays as it was."""
    rig = Rig(tmp_path, monkeypatch)
    notes = tmp_path / "notes"
    before = tree(notes)
    page = {
        **cast(dict[str, Any], suggesting([])["page"]),
        "suggest": None,
        "context": {"session": CHAT, "percent": 85},
        "sources": {"session": CHAT, "turn": TURN, "notes": False},
    }
    async with serving(rig.app) as client:
        await rig.cycles(1)  # Decision is off: the startup cycle decides nothing
        rig.settings.patch(str(uuid.uuid4()), 0, {"decision.enabled": True})

        async def listed() -> dict[str, dict[str, Any]]:
            reported = await client.post(
                f"{PREFIX}presence", headers=WRITE, json={**PRESENCE, "chat": CHAT, "page": page}
            )
            assert reported.status_code == 204
            opened = await client.post(f"{PREFIX}launcher/open", headers=WRITE)
            assert opened.status_code == 200
            return {item["candidate"]: item for item in opened.json()["items"]}

        items = await listed()
        assert items["context_handoff"]["actions"] == [
            {
                "type": "draft_chat",
                "label": "Draft a handoff",
                "primary": True,
                "session_id": CHAT,
                "text": templates._HANDOFF_DRAFT,
            }
        ]
        (note_action,) = items["source_note"]["actions"]
        assert note_action["label"] == "Draft literature notes"
        assert note_action["session_id"] == CHAT
        assert templates._SOURCE_NOTE_SEARCH_FIRST in note_action["text"]
        assert rig.model.requests == []  # no key yet: the rules decided
        saved = await client.post(f"{PREFIX}key", headers=WRITE, json={"key": KEY})
        assert saved.status_code == 200
        await listed()
    asked = [request.state.get("candidate") for request in rig.model.requests]
    titles = {found["title"] for found in asked if isinstance(found, dict)}
    assert {
        "handoff for a chat that is nearly full",
        "literature notes for fetched web pages",
    } <= titles
    unsent = [CHAT, TURN, "85", active_scope(tmp_path, rig.state)]
    for request in rig.model.requests:
        assert [text for text in unsent if text in canonical_json(request.state)] == []
        assert [text for text in (CHAT, TURN) if text in canonical_json(request.questions)] == []
    with rig.state.store._connection() as connection:
        started = connection.execute("SELECT COUNT(*) FROM decision_missions").fetchone()[0]
    assert started == 0
    assert tree(notes) == before


# ---- the plan brake -----------------------------------------------------------------------


async def test_the_plan_reading_reaches_the_brake_and_the_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reading the app's observer keeps (the one `/api/v2/limits` serves) is the one the engine
    brakes on and the page and the missions view report: a five-hour window at 0.9 resetting in an
    hour holds the allowed prepare for the vault's due task. After a cycle no chat started and both
    views carry the hold ending at that reset."""
    recorded: list[PlanLimits] = []

    class Recording(PlanLimits):
        def __init__(self) -> None:
            super().__init__()
            recorded.append(self)

    monkeypatch.setattr(server, "PlanLimits", Recording)
    rig = Rig(tmp_path, monkeypatch)
    (limits,) = recorded
    reset = int(WALL.timestamp()) + 3600
    limits.observe(
        {
            "observed_at": stamp(WALL),
            "status": "allowed_warning",
            "limiting": "five_hour",
            "utilization": 0.9,
            "resets_at": reset,
            "windows": {
                "five_hour": {"utilization": 0.9, "resets_at": reset},
                "seven_day": None,
            },
        }
    )
    DecisionKeyStore(rig.state.store).save(KEY)
    rig.model.answers = dict(PREPARE)
    rig.settings.patch(str(uuid.uuid4()), 0, {"decision.enabled": True})
    until = stamp(WALL + timedelta(hours=1))
    async with serving(rig.app) as client:
        await rig.cycles(1)
        opened = await client.post(f"{PREFIX}launcher/open", headers=WRITE)
        assert opened.status_code == 200
        assert rig.model.requests  # the cycle decided: only the brake kept the chat from starting
        page = await client.get(f"{PREFIX}page", headers=AUTH)
        missions = await client.get(f"{PREFIX}missions", headers=AUTH)
    with rig.state.store._connection() as connection:
        started = connection.execute("SELECT COUNT(*) FROM decision_missions").fetchone()[0]
    assert started == 0
    assert page.status_code == missions.status_code == 200
    assert page.json()["missions"]["hold"] == {"until": until}
    assert missions.json()["hold"] == {"until": until}


# ---- the seat host ------------------------------------------------------------------


async def test_the_seat_host_reaches_the_engine_the_page_and_the_card_watch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one seat host create_app builds serves the engine (a cycle in the vault reads its
    facts), the page (`missions.team`) and the Start route (an unknown routine is refused with
    409); the runner's card watch, bound to it, wakes the engine."""
    facts_calls: list[str] = []
    wakes: list[str] = []

    class Recording(server.SeatHost):
        def facts(self, workspace_id: str, since: Any, day: str) -> Any:
            facts_calls.append(workspace_id)
            return super().facts(workspace_id, since, day)

    original_wake = DecisionLoopEngine.wake

    def wake(self: DecisionLoopEngine, reason: Any) -> None:
        wakes.append(reason)
        original_wake(self, reason)

    monkeypatch.setattr(server, "SeatHost", Recording)
    monkeypatch.setattr(DecisionLoopEngine, "wake", wake)
    rig = Rig(tmp_path, monkeypatch)
    rig.settings.patch(str(uuid.uuid4()), 0, {"decision.enabled": True})
    async with serving(rig.app) as client:
        await rig.cycles(1)
        opened = await client.post(f"{PREFIX}launcher/open", headers=WRITE)
        assert opened.status_code == 200
        assert set(facts_calls) == {active_scope(tmp_path, rig.state)}
        page = await client.get(f"{PREFIX}page", headers=AUTH)
        assert page.status_code == 200
        assert page.json()["missions"]["team"] == {"today": 0, "limit": 4, "running": 0}
        refused = await client.post(f"{PREFIX}seats/{uuid.uuid4()}/start", headers=WRITE)
        assert refused.status_code == 409
        wakes.clear()
        rig.app.state.routine_tasks._watch()
        assert wakes == ["change"]


# ---- the Look readers --------------------------------------------------------------------


async def test_the_look_readers_reach_the_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The vault reader create_app builds lists an operation left `recovery_required` in the
    active, ordinary vault as a `vault_recovery` item that opens the vault; the app reader it
    builds is asked for every part while every watch is on."""
    asked: list[dict[str, bool]] = []
    answers: list[Any] = []

    class Recording(server.AppLookReader):
        async def read(self, **flags: bool) -> Any:  # type: ignore[override]
            asked.append(flags)
            answer = await super().read(**flags)  # type: ignore[arg-type]
            answers.append((flags, answer))
            return answer

    monkeypatch.setattr(server, "AppLookReader", Recording)
    rig = Rig(tmp_path, monkeypatch)
    scope = active_scope(tmp_path, rig.state)
    operations = OperationJournal(rig.state.store)
    root = OrdinaryMutationRoot(scope, tmp_path / "notes", "vault", "1:2", 1, None)
    operation_id = str(uuid.uuid4())
    step = OperationStep(0, "remove", "stuck.md", None, "a" * 64, None, None)
    operations.plan(operation_id, root, "delete", (step,), "a" * 64)
    operations.transition(operation_id, "planned", "recovery_required")
    rig.settings.patch(str(uuid.uuid4()), 0, {"decision.enabled": True})
    async with serving(rig.app) as client:
        await rig.cycles(1)
        opened = await client.post(f"{PREFIX}launcher/open", headers=WRITE)
        assert opened.status_code == 200
        items = [i for i in opened.json()["items"] if i["candidate"] == "vault_recovery"]
    (item,) = items
    assert [action["type"] for action in item["actions"]] == ["open_vault"]
    assert item["actions"][0]["workspace_id"] == scope
    every = {"waiting": True, "heavy": True, "sends": True, "bridges": True}
    assert every in asked
    # The messaging source is the runtime's status: a bridges part that read None would mean it
    # was not.
    fulls = [look for flags, look in answers if flags == every]
    assert fulls and all(isinstance(f.bridges, tuple) and f.failures == () for f in fulls)


async def test_the_code_reader_and_the_look_root_reach_the_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The code reader create_app builds is the engine's: a trusted folder it finds idle lists as
    a code look offering that folder in Code, then Take a look. The root port the missions get
    records a new chat's root in a trusted folder and answers False for one that is not."""
    built: list[tuple[object, object]] = []
    reads: list[datetime] = []
    idle: list[CodeFolder] = []
    ports: list[Callable[[str, str], Awaitable[bool]] | None] = []

    class Recording(server.CodeLookReader):
        def __init__(self, workspaces: WorkspaceService, git: server.CodeGit) -> None:
            super().__init__(workspaces, git)
            built.append((workspaces, git))

        def read(self, now: datetime) -> CodeLook:
            reads.append(now)
            return CodeLook(tuple(idle))

    original_init = MissionService.__init__

    def init(self: MissionService, *args: Any, **kwargs: Any) -> None:
        ports.append(kwargs.get("roots"))
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(server, "CodeLookReader", Recording)
    monkeypatch.setattr(MissionService, "__init__", init)
    rig = Rig(tmp_path, monkeypatch)
    spaces = WorkspaceService(
        rig.state.store,
        env={},
        data_dir=tmp_path / "data",
        home=tmp_path / "profile",
        install_dir=tmp_path / "install",
        git_available=lambda: False,
        hide_previews=lambda: False,
    )
    garden, shed = tmp_path / "garden-app", tmp_path / "shed"
    garden.mkdir()
    shed.mkdir()
    folder_id = trusted(spaces, garden)
    untrusted_id = spaces.register_existing(str(uuid.uuid4()), str(shed), kind="code")[
        0
    ].workspace_id
    idle.append(CodeFolder(folder_id, "garden-app", WALL - timedelta(days=2)))
    rig.settings.patch(
        str(uuid.uuid4()), 0, {"decision.enabled": True, "decision.grant.look": "ask"}
    )
    async with serving(rig.app) as client:
        await rig.cycles(1)
        opened = await client.post(f"{PREFIX}launcher/open", headers=WRITE)
        assert opened.status_code == 200
        items = [i for i in opened.json()["items"] if i["candidate"] == "code_look"]
    assert [(type(w), type(g)) for w, g in built] == [(WorkspaceService, server.CodeGit)]
    assert reads
    (item,) = items
    assert [(a["type"], a["label"]) for a in item["actions"]] == [
        ("open_code", "Open in Code"),
        ("start_prepare", "Take a look"),
    ]
    assert item["actions"][0]["workspace_id"] == folder_id
    (port,) = ports
    assert port is not None
    session_id = str(uuid.uuid4())
    assert await port(session_id, folder_id) is True
    row = rig.state.store.get_session_root(session_id)
    assert row is not None and row["workspace_id"] == folder_id
    refused = str(uuid.uuid4())
    assert await port(refused, untrusted_id) is False
    assert rig.state.store.get_session_root(refused) is None


# ---- saved checks ------------------------------------------------------------------------


async def test_saved_checks_reach_the_launcher_and_nothing_about_them_is_sent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one check host create_app builds serves the engine, the page and the Run checks route: a
    folder it reads waiting at a commit lists a saved-check item offering the folder in Code, then
    Run checks. With a key saved the model is asked about it by its type words only: no request
    carries the folder's name or id, the commit, the check or the scope. The user's Run checks
    reaches the host's start and no mission starts. With saved checks not supported the page
    reports no checks and the host is not read."""
    monkeypatch.setattr("viki_agent.code.code_checks.CHECKS_SUPPORTED", True)
    folder_id, commit, check_id = str(uuid.uuid4()), "c" * 40, str(uuid.uuid4())
    waiting = CheckFolder(folder_id, "garden-app", commit, "waiting", (check_id,))
    reads: list[datetime] = []
    starts: list[tuple[str, str, tuple[str, ...]]] = []

    class Recording(server.SavedCheckHost):
        def read(self, now: datetime) -> CheckLook:
            reads.append(now)
            return CheckLook((waiting,))

        def folder(self, workspace_id: str, now: datetime) -> CheckFolder | None:
            return waiting if workspace_id == folder_id else None

        async def start(
            self, workspace_id: str, head: str, check_ids: Any, *, stops: int | None = None
        ) -> int:
            starts.append((workspace_id, head, tuple(check_ids)))
            return len(check_ids)

    monkeypatch.setattr(server, "SavedCheckHost", Recording)
    rig = Rig(tmp_path, monkeypatch)
    rig.settings.patch(
        str(uuid.uuid4()), 0, {"decision.enabled": True, "decision.grant.check": "ask"}
    )
    async with serving(rig.app) as client:
        await rig.cycles(1)
        opened = await client.post(f"{PREFIX}launcher/open", headers=WRITE)
        assert opened.status_code == 200
        items = [i for i in opened.json()["items"] if i["candidate"] == "commit_checks"]
        assert rig.model.requests == []  # no key yet: the rules decided
        saved = await client.post(f"{PREFIX}key", headers=WRITE, json={"key": KEY})
        assert saved.status_code == 200
        rig.clock.advance(COALESCE_S)
        await rig.cycles(rig.polls + 1)
        run = await client.post(f"{PREFIX}checks/{folder_id}/run", headers=WRITE)
        assert run.status_code == 200
        assert run.json()["checks"]["running"] == 0
        monkeypatch.setattr("viki_agent.code.code_checks.CHECKS_SUPPORTED", False)
        read_before = len(reads)
        reported = await client.post(f"{PREFIX}presence", headers=WRITE, json=PRESENCE)
        assert reported.status_code == 204
        rig.clock.advance(COALESCE_S)
        await rig.cycles(rig.polls + 1)
        page = await client.get(f"{PREFIX}page", headers=AUTH)
    (item,) = items
    assert [(a["type"], a.get("workspace_id")) for a in item["actions"]] == [
        ("open_code", folder_id),
        ("run_checks", folder_id),
    ]
    assert starts == [(folder_id, commit, (check_id,))]
    asked = [request.state.get("candidate") for request in rig.model.requests]
    assert "saved checks on a code folder's new commit" in {
        found["title"] for found in asked if isinstance(found, dict)
    }
    unsent = ["garden-app", folder_id, commit, check_id, active_scope(tmp_path, rig.state)]
    for request in rig.model.requests:
        sent = canonical_json(request.state) + canonical_json(request.questions)
        assert [text for text in unsent if text in sent] == []
    with rig.state.store._connection() as connection:
        started = connection.execute("SELECT COUNT(*) FROM decision_missions").fetchone()[0]
    assert started == 0
    assert len(reads) == read_before  # the cycle after the flag turned false read nothing
    assert page.status_code == 200
    assert page.json()["missions"]["checks"] is None


# ---- inbox suggestions ------------------------------------------------------------

CAPTURES = {
    "00-inbox/2026-10-21-orchid-feed.md": "Orchid fertiliser schedule for the greenhouse\n",
    "00-inbox/2026-10-21-tax-forms.md": "Ask the accountant about the quarterly tax forms\n",
    "00-inbox/2026-10-21-bike-brakes.md": "Replace the cracked bicycle brake pads\n",
    "00-inbox/2026-10-21-poem-idea.md": "A poem about lighthouse keepers\n",
}
CAPTURE_WORDS = ("orchid", "tax forms", "bicycle", "lighthouse")
TRIAGE = "suggested destinations for inbox captures"
RUN = SuggestionRun("run-1", "workspace", "session", "running", "2026-10-21T09:00:00Z")


def write_captures(tmp_path: Path) -> list[str]:
    """Four captures in the active vault's inbox, each last changed an hour before the rig's
    clock, so the inbox is pressing and has settled. Returns their refs."""
    when = (WALL - timedelta(hours=1)).timestamp()
    for ref, text in CAPTURES.items():
        path = tmp_path / "notes" / ref
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        os.utime(path, (when, when))
    return list(CAPTURES)


def unsent_captures(rig: Rig, tmp_path: Path, refs: list[str]) -> list[str]:
    """What the requests carry of the captures: a ref, a file name, a word of their text or the
    scope."""
    unsent = [*refs, *(Path(ref).stem for ref in refs), *CAPTURE_WORDS]
    unsent.append(active_scope(tmp_path, rig.state))
    found: list[str] = []
    for request in rig.model.requests:
        sent = (canonical_json(request.state) + canonical_json(request.questions)).casefold()
        found.extend(text for text in unsent if text.casefold() in sent)
    return found


def asked_about_triage(rig: Rig) -> bool:
    asked = [request.state.get("candidate") for request in rig.model.requests]
    return TRIAGE in {found["title"] for found in asked if isinstance(found, dict)}


async def test_one_suggester_serves_the_triage_and_decision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one suggester create_app builds backs both the capture routes (the user's **Ask VIKI
    to suggest**) and the host Decision starts its suggestion run through."""
    routed: list[object] = []
    hosted: list[object] = []
    capture_routes = server.capture_routes

    def recording_routes(captures: Any, suggester: Any, **kwargs: Any) -> list[Route]:
        routed.append(suggester)
        return capture_routes(captures, suggester, **kwargs)

    class Recording(server.SuggestionHost):
        def __init__(self, suggester: Any, **kwargs: Any) -> None:
            hosted.append(suggester)
            super().__init__(suggester, **kwargs)

    monkeypatch.setattr(server, "capture_routes", recording_routes)
    monkeypatch.setattr(server, "SuggestionHost", Recording)
    Rig(tmp_path, monkeypatch)
    assert len(routed) == 1
    assert hosted == routed


async def test_inbox_suggestions_reach_the_launcher_and_nothing_about_the_captures_is_sent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With Suggest destinations for my inbox on Ask, four settled captures in an Ordinary vault
    list one inbox-suggestion item offering Triage inbox, then Suggest destinations. With a key
    saved the model is asked about it by its type words only: no request carries a capture's
    ref, name or text or the scope. The user's Suggest destinations reaches the host's start
    and no mission starts."""
    refs = write_captures(tmp_path)
    starts: list[tuple[str, str]] = []

    class Recording(server.SuggestionHost):
        async def start(self, workspace_id: str, item_key: str) -> str:
            starts.append((workspace_id, item_key))
            return RUN.run_id

    monkeypatch.setattr(server, "SuggestionHost", Recording)
    rig = Rig(tmp_path, monkeypatch)
    rig.settings.patch(
        str(uuid.uuid4()), 0, {"decision.enabled": True, "decision.grant.inbox_triage": "ask"}
    )
    async with serving(rig.app) as client:
        await rig.cycles(1)
        opened = await client.post(f"{PREFIX}launcher/open", headers=WRITE)
        assert opened.status_code == 200
        items = [i for i in opened.json()["items"] if i["candidate"] == "inbox_triage"]
        assert rig.model.requests == []  # no key yet: the rules decided
        saved = await client.post(f"{PREFIX}key", headers=WRITE, json={"key": KEY})
        assert saved.status_code == 200
        rig.clock.advance(COALESCE_S)
        await rig.cycles(rig.polls + 1)
        (item,) = items
        assert [a["type"] for a in item["actions"]] == ["open_triage", "suggest_inbox"]
        assert starts == []  # Ask: nothing starts until the user chooses
        suggest = await client.post(
            f"{PREFIX}inbox/suggest", headers=WRITE, json={"item_key": item["item_key"]}
        )
        assert suggest.status_code == 200
    assert starts == [(active_scope(tmp_path, rig.state), item["item_key"])]
    assert asked_about_triage(rig)
    assert unsent_captures(rig, tmp_path, refs) == []
    with rig.state.store._connection() as connection:
        started = connection.execute("SELECT COUNT(*) FROM decision_missions").fetchone()[0]
    assert started == 0


async def test_allowed_pre_runs_once_and_sends_nothing_about_the_captures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With Suggest destinations for my inbox Allowed (its default), the waiting item's run starts
    through the suggester once across the cycles that follow, and no request carries a capture's
    ref, name or text or the scope."""
    refs = write_captures(tmp_path)
    started: list[str] = []

    async def start(_suggester: Any, workspace_id: str) -> SuggestionRun:
        started.append(workspace_id)
        return RUN

    monkeypatch.setattr(server.CaptureSuggester, "start", start)
    recorded: list[PlanLimits] = []

    class Recording(PlanLimits):
        def __init__(self) -> None:
            super().__init__()
            recorded.append(self)

    monkeypatch.setattr(server, "PlanLimits", Recording)
    rig = Rig(tmp_path, monkeypatch)
    # VIKI holds its own starts until a launch's first plan reading: one far below every brake.
    (limits,) = recorded
    limits.observe({"observed_at": stamp(WALL), "status": "allowed", "utilization": 0.1})
    rig.settings.patch(str(uuid.uuid4()), 0, {"decision.enabled": True})
    async with serving(rig.app) as client:
        await rig.cycles(1)
        saved = await client.post(f"{PREFIX}key", headers=WRITE, json={"key": KEY})
        assert saved.status_code == 200
        rig.clock.advance(COALESCE_S)
        await rig.cycles(rig.polls + 1)
        # More cycles over the same inbox state: a presence change, then the launcher's open.
        reported = await client.post(f"{PREFIX}presence", headers=WRITE, json=PRESENCE)
        assert reported.status_code == 204
        rig.clock.advance(COALESCE_S)
        await rig.cycles(rig.polls + 1)
        opened = await client.post(f"{PREFIX}launcher/open", headers=WRITE)  # runs its cycle
        assert opened.status_code == 200
    assert started == [active_scope(tmp_path, rig.state)]
    assert asked_about_triage(rig)
    assert unsent_captures(rig, tmp_path, refs) == []


async def test_shutdown_closes_the_suggestion_host_after_the_engine_stops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lifespan closes the one host create_app builds once the engine has stopped, so a start
    still in flight creates no chat while the chats close; a start after that is refused."""
    built: list[server.SuggestionHost] = []
    events_at_close: list[list[str]] = []
    holder: list[Rig] = []

    class Recording(server.SuggestionHost):
        def __init__(self, suggester: Any, **kwargs: Any) -> None:
            super().__init__(suggester, **kwargs)
            built.append(self)

        async def close(self) -> None:
            events_at_close.append(list(holder[0].events))
            await super().close()

    monkeypatch.setattr(server, "SuggestionHost", Recording)
    rig = Rig(tmp_path, monkeypatch)
    holder.append(rig)
    (host,) = built
    async with serving(rig.app):
        await rig.cycles(1)
        assert events_at_close == []
    (seen,) = events_at_close
    assert seen[-2:] == ["stop", "stopped"] and "close" not in seen  # the model closes after
    with pytest.raises(DecisionLoopError) as refused:
        await host.start(active_scope(tmp_path, rig.state), KEY)
    assert refused.value.code == "unavailable"
