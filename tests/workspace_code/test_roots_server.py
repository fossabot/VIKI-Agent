"""Session roots and worktrees wired through `create_app`: the spawn hook, the catalog, the fork
path and startup recovery. Transcripts are synthetic."""

from __future__ import annotations

import json
import os
import queue
import shutil
import sys
import tempfile
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient, WebSocketTestSession

from tests.app.test_workspaces import service_at
from tests.test_server import (
    BASE,
    BEARER,
    ORIGIN,
    TOKEN,
    EchoDriver,
    _mutate_v2,
    _open_v2,
    authed,
    recv,
)
from tests.workspace_code.test_code_git import _commit, _repo
from tests.workspace_code.test_workspaces_code import trusted
from viki_agent import server
from viki_agent.app.diagnostics import Readiness
from viki_agent.catalog import Catalog, project_dir
from viki_agent.code.session_roots import MSG_ROOT_UNTRUSTED
from viki_agent.code.worktrees import worktree_area
from viki_agent.config import Config
from viki_agent.drivers.claude.control import Launch
from viki_agent.drivers.claude.driver import DriverConfig
from viki_agent.events import Event
from viki_agent.session import SessionManager, now_iso
from viki_agent.state import AppState, Prefs
from viki_agent.workspaces import WorkspaceService

SID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
FORK = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
EXISTING = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
HELD = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
VAULT_COMMAND = "33333333-3333-4333-8333-333333333333"
TITLE = "Synthetic code-folder session"
JSON_HEADERS = {**BEARER, "origin": ORIGIN, "content-type": "application/json"}
BRANCH = "viki/20261003-c09a"

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not available")
needs_windows_git = pytest.mark.skipif(
    shutil.which("git") is None or sys.platform != "win32",
    reason="git is not available, or not Windows (checks and previews run on Windows only)",
)


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


@dataclass
class Folders:
    tmp_path: Path
    home: Path
    vault: Path
    code_id: str
    display: Path  # the code folder's root as the spawn uses it
    git: bool

    def spaces(self, state: AppState) -> WorkspaceService:
        return service_at(self.tmp_path, state.store, git=self.git)

    def transcript(self, root: Path, session_id: str, prompt: str) -> Path:
        """A synthetic transcript in `root`'s project, as the CLI would leave one."""
        folder = project_dir(self.home, root)
        folder.mkdir(parents=True, exist_ok=True)
        lines = [
            {
                "type": "user",
                "uuid": str(uuid.uuid4()),
                "parentUuid": None,
                "timestamp": "2026-10-03T00:00:01.000Z",
                "isSidechain": False,
                "entrypoint": "cli",
                "sessionId": session_id,
                "message": {"role": "user", "content": prompt},
            },
            {"type": "custom-title", "customTitle": TITLE, "sessionId": session_id},
        ]
        path = folder / f"{session_id}.jsonl"
        path.write_bytes("".join(json.dumps(line) + "\n" for line in lines).encode("utf-8"))
        return path


def _folders(tmp_path: Path, *, git: bool = False, active_code: bool = False) -> Folders:
    """A vault and one trusted code folder (a committed repository when `git`)."""
    # A short folder: a transcript path repeats the code folder's path, which must stay under
    # Windows MAX_PATH even beneath pytest's default basetemp.
    short = Path(tempfile.mkdtemp(dir=tmp_path.parent, prefix="c"))
    vault = short / "notes"
    vault.mkdir()
    home = short / "home"
    (home / "sessions").mkdir(parents=True)
    code_root = short / "repo"
    if git:
        _repo(code_root)
        (code_root / "a.txt").write_text("alpha\n", encoding="utf-8")
        _commit(code_root, "first")
    else:
        code_root.mkdir()
    state = AppState(tmp_path / "data" / "state.json", Prefs())
    spaces = service_at(tmp_path, state.store, git=git)
    code_id = trusted(spaces, code_root)
    spaces.register_existing(VAULT_COMMAND, str(vault))
    if active_code:
        spaces.select(str(uuid.uuid4()), code_id)
    display = Path(spaces.get(code_id, kind="code").root.display)
    return Folders(tmp_path, home, vault, code_id, display, git)


@dataclass
class Built:
    app: Starlette
    state: AppState
    spaces: WorkspaceService
    launches: list[Launch]
    opened: queue.Queue[str]


def _build(folders: Folders) -> Built:
    """One launch of the app over the folders' data dir and home, unstarted."""
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
    opened: queue.Queue[str] = queue.Queue()
    app = server.create_app(
        config=config,
        token=TOKEN,
        manager=manager,
        catalog=Catalog(folders.home, cwd),
        static_dir=None,
        workspaces=spaces,
        # `code_git`'s gate reads the readiness Git state, as `scripts/e2e_server.py --git` sets it.
        readiness=Readiness(
            config=config, launch_id=str(uuid.uuid4()), git_available=lambda: folders.git
        ),
        opener=opened.put,
    )
    return Built(app, state, spaces, launches, opened)


@contextmanager
def running(built: Built) -> Iterator[TestClient]:
    with TestClient(built.app, base_url=BASE) as client:
        yield client


def _record(client: TestClient, folders: Folders, session_id: str = SID, **fields: Any) -> None:
    body = {"session_id": session_id, "workspace_id": folders.code_id, "worktree_id": None}
    response = client.post(
        "/api/v2/session-roots", content=json.dumps({**body, **fields}), headers=JSON_HEADERS
    )
    assert response.status_code == 201, response.text


def _until_receipt(
    ws: WebSocketTestSession, command_id: str
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Every frame before the command's receipt, and the receipt."""
    frames: list[dict[str, Any]] = []
    for _ in range(20):
        frame = recv(ws)
        if frame["type"] == "control.receipt" and frame["command_id"] == command_id:
            return frames, frame
        frames.append(frame)
    raise AssertionError("no receipt")


def _send(ws: WebSocketTestSession, target: dict[str, Any]) -> list[dict[str, Any]]:
    frames, _ = _until_receipt(ws, _mutate_v2(ws, target, "turn.send", text="go on"))
    return frames


def _first_session_in_the_code_folder(folders: Folders) -> None:
    """Launch one: record the root, open the session, see it spawn there."""
    built = _build(folders)
    with running(built) as client, authed(client) as ws:
        _record(client, folders)
        _open_v2(ws, SID)
    assert [(launch.session_id, launch.resume, launch.cwd) for launch in built.launches] == [
        (SID, False, folders.display)
    ]


def test_a_session_posted_into_a_trusted_code_folder_spawns_there(tmp_path: Path) -> None:
    _first_session_in_the_code_folder(_folders(tmp_path))


def test_a_session_recorded_in_a_code_folder_follows_it_across_a_restart(tmp_path: Path) -> None:
    folders = _folders(tmp_path)
    _first_session_in_the_code_folder(folders)
    record = folders.transcript(folders.display, SID, "synthetic prompt")

    built = _build(folders)
    with running(built) as client, authed(client) as ws:
        listing = client.get("/api/sessions", headers=BEARER).json()
        assert [row["title"] for row in listing["sessions"] if row["session_id"] == SID] == [TITLE]
        ws.send_json({"type": "open.transcript", "session_id": SID})
        assert built.opened.get(timeout=3) == f"vscode://file/{record.as_posix()}"
        _, target = _open_v2(ws, SID)
        _send(ws, target)
    assert [(launch.session_id, launch.resume, launch.cwd) for launch in built.launches] == [
        (SID, True, folders.display)
    ]


def test_a_resume_in_a_folder_untrusted_since_is_refused_without_a_launch(
    tmp_path: Path,
) -> None:
    folders = _folders(tmp_path)
    _first_session_in_the_code_folder(folders)
    folders.transcript(folders.display, SID, "synthetic prompt")

    built = _build(folders)
    workspace = built.spaces.get(folders.code_id, kind="code")
    built.spaces.set_policy(
        str(uuid.uuid4()), folders.code_id, workspace.policy_revision, "existing_readonly", []
    )
    with running(built) as client, authed(client) as ws:
        _, target = _open_v2(ws, SID)
        frames = _send(ws, target)
    errors = [frame["data"]["message"] for frame in frames if frame["type"] == "error"]
    assert errors == [MSG_ROOT_UNTRUSTED]
    assert built.launches == []


def test_a_fork_of_a_recorded_session_spawns_in_the_same_folder(tmp_path: Path) -> None:
    folders = _folders(tmp_path)
    _first_session_in_the_code_folder(folders)
    folders.transcript(folders.display, SID, "synthetic prompt")

    built = _build(folders)
    with running(built) as client, authed(client) as ws:
        _, source = _open_v2(ws, SID)
        _, receipt = _until_receipt(ws, _mutate_v2(ws, source, "session.fork", fork_id=FORK))
        assert receipt["data"]["status"] == "applied", receipt
        _, fork = _open_v2(ws, FORK)
        _send(ws, fork)
    assert [(launch.session_id, launch.fork_of, launch.cwd) for launch in built.launches] == [
        (FORK, SID, folders.display)
    ]
    assert built.state.store.get_session_root(FORK) is not None


def test_a_fork_onto_an_existing_session_is_a_conflict_and_records_no_root(
    tmp_path: Path,
) -> None:
    folders = _folders(tmp_path)
    # EXISTING has a transcript in the vault and no root; HELD has a root of its own.
    folders.transcript(folders.vault, EXISTING, "an unrelated vault session")
    built = _build(folders)
    built.state.store.put_session_root(
        {
            "session_id": HELD,
            "workspace_id": folders.code_id,
            "worktree_id": None,
            "display_root": str(folders.tmp_path / "elsewhere"),
            "created_at": now_iso(),
        }
    )
    with running(built) as client, authed(client) as ws:
        _record(client, folders)
        _, source = _open_v2(ws, SID)
        for fork_id in (EXISTING, HELD):
            command = _mutate_v2(ws, source, "session.fork", fork_id=fork_id)
            _, receipt = _until_receipt(ws, command)
            assert receipt["data"] == {"status": "rejected", "reason": "conflict"}
    assert built.state.store.get_session_root(EXISTING) is None
    held = built.state.store.get_session_root(HELD)
    assert held is not None and held["display_root"] == str(folders.tmp_path / "elsewhere")
    assert [launch.session_id for launch in built.launches] == [SID]


def test_a_new_session_in_the_active_code_folder_gets_a_root(tmp_path: Path) -> None:
    folders = _folders(tmp_path, active_code=True)
    built = _build(folders)
    with running(built) as client, authed(client) as ws:
        _open_v2(ws, SID)
        roots = client.get("/api/v2/session-roots", headers=BEARER).json()
    assert roots == {
        "roots": [{"session_id": SID, "workspace_id": folders.code_id, "worktree_id": None}],
        "truncated": False,
    }
    row = built.state.store.get_session_root(SID)
    assert row is not None and Path(row["display_root"]) == folders.display


def _create_worktree(client: TestClient, built: Built, folders: Folders) -> dict[str, Any]:
    workspace = built.spaces.get(folders.code_id, kind="code")
    body = {
        "policy_revision": workspace.policy_revision,
        "root_physical_id": workspace.root.physical_id,
        "branch": BRANCH,
    }
    response = client.post(
        f"/api/v2/code/{folders.code_id}/worktrees", content=json.dumps(body), headers=JSON_HEADERS
    )
    assert response.status_code == 201, response.text
    return response.json()["worktree"]


@needs_git
def test_the_worktree_routes_answer_through_the_app(tmp_path: Path) -> None:
    folders = _folders(tmp_path, git=True)
    built = _build(folders)
    with running(built) as client, authed(client) as ws:
        worktree = _create_worktree(client, built, folders)
        listed = client.get("/api/v2/worktrees", headers=BEARER).json()
        _record(client, folders, worktree_id=worktree["worktree_id"])
        _open_v2(ws, SID)
    assert worktree["state"] == "ready" and worktree["branch"] == BRANCH
    assert Path(worktree["path"]).parent == worktree_area(tmp_path / "data")
    assert listed == {"worktrees": [worktree], "truncated": False}
    assert [launch.cwd for launch in built.launches] == [Path(worktree["path"])]


@needs_git
def test_startup_recovery_settles_a_creating_row_of_a_listed_worktree(tmp_path: Path) -> None:
    folders = _folders(tmp_path, git=True)
    built = _build(folders)
    with running(built) as client:
        worktree = _create_worktree(client, built, folders)
    built.state.store.update_worktree_state(worktree["worktree_id"], "ready", "creating", now_iso())

    again = _build(folders)
    with running(again) as client:
        listed = client.get("/api/v2/worktrees", headers=BEARER).json()["worktrees"]
    assert [(row["worktree_id"], row["state"]) for row in listed] == [
        (worktree["worktree_id"], "ready")
    ]
