"""`server.create_app`'s launch sweep: an idle dirty worktree made before this launch is
archived once the new `run_sweeps` task's first round runs; off, nothing moves."""

from __future__ import annotations

import os
import queue
import shutil
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from tests.test_server import BEARER, TOKEN, EchoDriver
from tests.workspace_code.test_roots_server import (
    Built,
    Folders,
    _create_worktree,
    _folders,
    running,
)
from tests.workspace_code.test_worktree_sweep import _age
from viki_agent import server
from viki_agent.app.diagnostics import Readiness
from viki_agent.app.settings import SettingsService
from viki_agent.catalog import Catalog
from viki_agent.code.worktrees import SWEEP_INTERVAL_S
from viki_agent.config import Config
from viki_agent.drivers.claude.control import Launch
from viki_agent.drivers.claude.driver import DriverConfig
from viki_agent.events import Event
from viki_agent.session import SessionManager
from viki_agent.state import AppState, Prefs

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not available")
HANG_GUARD_S = 10.0  # guards a real sweep (git archive and remove); never a bound on real work


@pytest.fixture(autouse=True)
def _empty_git_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No developer `GIT_*` variable and an empty home, so the user's Git config is not read."""
    for name in list(os.environ):
        if name.upper().startswith("GIT_"):
            monkeypatch.delenv(name)
    home = tmp_path / "git-home"
    home.mkdir()
    for name in ("HOME", "USERPROFILE", "XDG_CONFIG_HOME"):
        monkeypatch.setenv(name, str(home))


def _build(folders: Folders, *, worktree_sweep_s: float, cleanup_enabled: bool = True) -> Built:
    """Like `test_roots_server._build`, with a real `SettingsService` wired to the launch
    sweep and a short sweep interval, so the test need not wait for `SWEEP_INTERVAL_S`."""
    state = AppState(folders.tmp_path / "data" / "state.json", Prefs(mode="auto", effort="low"))
    spaces = folders.spaces(state)
    cwd = spaces.resolve_active().cwd if spaces.selected_id() else folders.vault
    launches: list[Launch] = []

    def make(launch: Launch, _prefs: Prefs, emit: Callable[[Event], None]) -> EchoDriver:
        launches.append(launch)
        return EchoDriver(launch, emit)

    config = Config(
        vault=cwd,
        claude_home=folders.home,
        cli=Path("fake"),
        edge=None,
        data_dir=folders.tmp_path / "data",
    )
    manager = SessionManager(
        config=DriverConfig(cli=("fake",), vault=cwd, env={}),
        state=state,
        claude_home=folders.home,
        make_driver=make,
    )
    settings = SettingsService(
        state.store, provider_defaults=lambda: Prefs(), on_chat_defaults=lambda _prefs: None
    )
    if not cleanup_enabled:
        settings.patch(
            str(uuid.uuid4()),
            settings.read().document.revision,
            {"workbench.worktree_cleanup": False},
        )
    opened: queue.Queue[str] = queue.Queue()
    app = server.create_app(
        config=config,
        token=TOKEN,
        manager=manager,
        catalog=Catalog(folders.home, cwd),
        static_dir=None,
        workspaces=spaces,
        readiness=Readiness(
            config=config, launch_id=str(uuid.uuid4()), git_available=lambda: folders.git
        ),
        opener=opened.put,
        settings=settings,
        worktree_sweep_s=worktree_sweep_s,
    )
    return Built(app, state, spaces, launches, opened)


def _dirty_and_idle(path: Path) -> None:
    """A real change, then backdated past the 24h tier `_create_worktree`'s auto-named
    branch gets, so the next launch's sweep reads this worktree as eligible at once."""
    (path / "dirty.txt").write_text("x\n", encoding="utf-8")
    _age(path, datetime.now(UTC) - timedelta(hours=25))


def _worktree_state(client: TestClient, worktree_id: str) -> str:
    rows = client.get("/api/v2/worktrees", headers=BEARER).json()["worktrees"]
    return next(row["state"] for row in rows if row["worktree_id"] == worktree_id)


@needs_git
def test_an_idle_dirty_worktree_made_before_launch_is_archived_by_the_launch_sweep(
    tmp_path: Path,
) -> None:
    folders = _folders(tmp_path, git=True)
    first = _build(folders, worktree_sweep_s=SWEEP_INTERVAL_S)
    with running(first) as client:
        worktree = _create_worktree(client, first, folders)
    _dirty_and_idle(Path(worktree["path"]))

    again = _build(folders, worktree_sweep_s=0.05)
    with running(again) as client:
        # The public list leaves out `removed` rows: a removed worktree drops off it, and the
        # archives endpoint below, which lists only `removed` rows' archives, proves the rest.
        listed = True
        deadline = time.monotonic() + HANG_GUARD_S
        while listed and time.monotonic() < deadline:
            time.sleep(0.05)
            rows = client.get("/api/v2/worktrees", headers=BEARER).json()["worktrees"]
            listed = any(row["worktree_id"] == worktree["worktree_id"] for row in rows)
        assert not listed
        archives = client.get(
            f"/api/v2/code/{folders.code_id}/worktree-archives", headers=BEARER
        ).json()
    assert archives["enabled"] is True
    assert [a["worktree_id"] for a in archives["archives"]] == [worktree["worktree_id"]]
    assert not Path(worktree["path"]).exists()


@needs_git
def test_the_same_idle_dirty_worktree_stays_with_the_setting_off(tmp_path: Path) -> None:
    folders = _folders(tmp_path, git=True)
    first = _build(folders, worktree_sweep_s=SWEEP_INTERVAL_S)
    with running(first) as client:
        worktree = _create_worktree(client, first, folders)
    _dirty_and_idle(Path(worktree["path"]))

    again = _build(folders, worktree_sweep_s=0.02, cleanup_enabled=False)
    with running(again) as client:
        time.sleep(0.3)  # several idle no-op rounds at this interval
        state = _worktree_state(client, worktree["worktree_id"])
    assert state == "ready"
    assert Path(worktree["path"]).exists()


@needs_git
def test_the_same_idle_dirty_worktree_stays_with_a_read_only_settings_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-9: a read-only settings document (or a failing store) answers `flag(...)` with None,
    never True, so the launch sweep must stay off rather than default on."""
    folders = _folders(tmp_path, git=True)
    first = _build(folders, worktree_sweep_s=SWEEP_INTERVAL_S)
    with running(first) as client:
        worktree = _create_worktree(client, first, folders)
    _dirty_and_idle(Path(worktree["path"]))

    monkeypatch.setattr(SettingsService, "flag", lambda self, key: None)
    again = _build(folders, worktree_sweep_s=0.02)
    with running(again) as client:
        # Polls for the whole guard window: with the fix, nothing ever happens, so this always
        # runs it out; a real archive-and-remove (the mutant, or a regression) ends it early.
        deadline = time.monotonic() + HANG_GUARD_S
        state = "ready"
        while state in ("ready", "removing") and time.monotonic() < deadline:
            time.sleep(0.05)
            state = _worktree_state(client, worktree["worktree_id"])
    assert state == "ready"
    assert Path(worktree["path"]).exists()
