"""`open.code` and the code services' wiring through `create_app`."""

from __future__ import annotations

import os
import queue
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from tests.app.test_workspaces import OTHER_COMMAND, service_at
from tests.test_server import BASE, BEARER, TOKEN, EmptyCatalog, authed
from tests.test_server import running as bare_running
from tests.workspace_code.test_code_git import needs_git
from tests.workspace_code.test_workspaces_code import trusted
from viki_agent import rails, server, winproc
from viki_agent.app import diagnostics
from viki_agent.code.code_files import CodeFiles
from viki_agent.config import Config
from viki_agent.drivers.claude.driver import DriverConfig
from viki_agent.session import SessionManager
from viki_agent.state import AppState, Prefs
from viki_agent.workspaces import WorkspaceService

VAULT_COMMAND = "33333333-3333-4333-8333-333333333333"


def _git(cwd: Path, *args: str) -> None:
    """Repo setup only: the rails chip is the path under test."""
    done = winproc.run(["git", *args], cwd=cwd)
    assert done.returncode == 0, done.stderr


@dataclass
class App:
    client: TestClient
    opened: queue.Queue[str]
    code_id: str
    vault_id: str
    root: Path


@contextmanager
def running(tmp_path: Path, code_name: str = "repo") -> Iterator[App]:
    """A server with real workspaces holding one trusted code folder and one vault."""
    code_root = tmp_path / code_name
    (code_root / "src" / "pkg").mkdir(parents=True)
    (code_root / "src" / "pkg" / "a.py").write_text("x = 1\n", encoding="utf-8")
    (code_root / ".env").write_text("SYNTHETIC=1\n", encoding="utf-8")
    vault_root = tmp_path / "notes"
    vault_root.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    state = AppState(tmp_path / "data" / "state.json", Prefs())
    spaces = service_at(tmp_path, state.store)
    code_id = trusted(spaces, code_root)
    vault_id = spaces.register_existing(VAULT_COMMAND, str(vault_root))[0].workspace_id
    config = Config(
        vault=vault_root,
        claude_home=home,
        cli=Path("fake"),
        edge=None,
        data_dir=tmp_path / "data",
    )
    manager = SessionManager(
        config=DriverConfig(cli=(), vault=vault_root, env={}),
        state=state,
        claude_home=home,
        make_driver=lambda *_: pytest.fail("no open.code message may start a provider"),
    )
    opened: queue.Queue[str] = queue.Queue()
    app = server.create_app(
        config=config,
        token=TOKEN,
        manager=manager,
        catalog=EmptyCatalog(home, vault_root),
        static_dir=None,
        workspaces=spaces,
        opener=opened.put,
    )
    with TestClient(app, base_url=BASE) as client:
        yield App(client, opened, code_id, vault_id, code_root)


def _open(app: App, ws: Any, **fields: Any) -> None:
    ws.send_json(
        {"type": "open.code", "workspace_id": app.code_id, "path": "src/pkg/a.py", **fields}
    )


def _resolved(app: App) -> str:
    return (app.root / "src" / "pkg" / "a.py").resolve().as_posix()


def test_open_code_opens_vscode_at_the_line(tmp_path: Path) -> None:
    with running(tmp_path) as app, authed(app.client) as ws:
        _open(app, ws, line=12)
        assert app.opened.get(timeout=3) == f"vscode://file/{_resolved(app)}:12"
        _open(app, ws)
        assert app.opened.get(timeout=3) == f"vscode://file/{_resolved(app)}"
        assert app.opened.empty()


def test_open_code_with_an_empty_path_opens_the_root(tmp_path: Path) -> None:
    with running(tmp_path) as app, authed(app.client) as ws:
        _open(app, ws, path="")
        assert app.opened.get(timeout=3) == f"vscode://file/{app.root.resolve().as_posix()}"
        assert app.opened.empty()


@pytest.mark.parametrize("line", [0, -1, 10_000_000, True, "12", 1.5])
def test_a_bad_line_opens_nothing(tmp_path: Path, line: object) -> None:
    with running(tmp_path) as app, authed(app.client) as ws:
        _open(app, ws, line=line)
        _open(app, ws, line=9_999_999)  # barrier
        assert app.opened.get(timeout=3) == f"vscode://file/{_resolved(app)}:9999999"
        assert app.opened.empty()


def test_a_refused_target_opens_nothing(tmp_path: Path) -> None:
    (tmp_path / "x").write_text("outside", encoding="utf-8")
    with running(tmp_path) as app, authed(app.client) as ws:
        for path in (".env", "../x", "src/pkg", "src/missing.py"):
            _open(app, ws, path=path)
        _open(app, ws, workspace_id=app.vault_id)
        _open(app, ws, workspace_id=str(uuid.uuid4()))
        _open(app, ws, workspace_id="not-a-uuid")
        _open(app, ws, line=3)  # barrier
        assert app.opened.get(timeout=3) == f"vscode://file/{_resolved(app)}:3"
        assert app.opened.empty()


def test_open_code_without_code_services_opens_nothing(tmp_path: Path) -> None:
    with bare_running(tmp_path) as bare, authed(bare.client) as ws:
        ws.send_json(
            {"type": "open.code", "workspace_id": str(uuid.uuid4()), "path": "a.py", "line": 1}
        )
        ws.send_json({"type": "open.url", "url": "https://good.example"})  # barrier
        assert bare.opened.get(timeout=3) == "https://good.example"
        assert bare.opened.empty()


def test_a_code_route_answers_through_create_app(tmp_path: Path) -> None:
    with running(tmp_path) as app:
        response = app.client.get(f"/api/v2/code/{app.code_id}/tree", headers=BEARER)
        assert response.status_code == 200, response.text


def test_a_vault_read_route_does_not_know_a_code_folder(tmp_path: Path) -> None:
    with running(tmp_path) as app:
        response = app.client.get(f"/api/v2/vaults/{app.code_id}/entries", headers=BEARER)
        assert response.status_code == 404, response.text
        assert response.json()["error"]["code"] == "unknown_workspace"


def test_open_code_escapes_what_a_vscode_url_parses_specially(tmp_path: Path) -> None:
    # VS Code splits the URL at the first "?" or "#" and percent-decodes the path, so a
    # resolved path holding "#", "?" or "%" must reach it escaped.
    with running(tmp_path, "C#proj") as app, authed(app.client) as ws:
        (app.root / "q#frag.txt").write_text("a\n", encoding="utf-8")
        (app.root / "x%41y.txt").write_text("b\n", encoding="utf-8")
        _open(app, ws, path="q#frag.txt", line=3)
        first = app.opened.get(timeout=3)
        _open(app, ws, path="x%41y.txt")
        second = app.opened.get(timeout=3)
    assert first.endswith("C%23proj/q%23frag.txt:3")
    assert second.endswith("C%23proj/x%2541y.txt")
    assert "#" not in first.removeprefix("vscode://file/")
    assert "#" not in second.removeprefix("vscode://file/")


def test_open_code_escapes_a_question_mark_whatever_the_platform(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Resolution is stubbed (no Windows file can hold "?"), so this pins only the escaping."""
    monkeypatch.setattr(
        CodeFiles, "open_target", lambda self, workspace_id, ref: Path("C:/repo/a?b#c%d.txt")
    )
    with running(tmp_path) as app, authed(app.client) as ws:
        _open(app, ws, line=2)
        assert app.opened.get(timeout=3) == "vscode://file/C:/repo/a%3Fb%23c%25d.txt:2"
        assert app.opened.empty()


def _rails_client(tmp_path: Path, spaces: WorkspaceService, vault: Path) -> TestClient:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    state = AppState(tmp_path / "data" / "state.json", Prefs())
    config = Config(
        vault=vault, claude_home=home, cli=Path("fake"), edge=None, data_dir=tmp_path / "data",
    )  # fmt: skip
    manager = SessionManager(
        config=DriverConfig(cli=(), vault=vault, env={}),
        state=state,
        claude_home=home,
        make_driver=lambda *_: pytest.fail("a rails read may not start a provider"),
    )
    app = server.create_app(
        config=config,
        token=TOKEN,
        manager=manager,
        catalog=EmptyCatalog(home, vault),
        static_dir=None,
        workspaces=spaces,
        opener=lambda _url: None,
    )
    return TestClient(app, base_url=BASE)


@needs_git
def test_the_rails_git_chip_in_a_code_folder_runs_no_repository_hook(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in list(os.environ):
        if name.upper().startswith("GIT_"):
            monkeypatch.delenv(name)
    root = tmp_path / "repo"
    root.mkdir()
    for args in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "t@example.com"),
        ("config", "user.name", "t"),
        ("config", "core.autocrlf", "false"),
        ("config", "commit.gpgsign", "false"),
    ):
        _git(root, *args)
    (root / "a.txt").write_text("a\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "first")
    (root / "a.txt").write_text("a2\n", encoding="utf-8")
    marker = tmp_path / "hook-ran"
    hook = f"\"echo ran > '{marker.as_posix()}'\""
    with (root / ".git" / "config").open("a", encoding="utf-8") as config:
        config.write(f"[core]\n\tfsmonitor = {hook}\n")

    state = AppState(tmp_path / "data" / "state.json", Prefs())
    spaces = service_at(tmp_path, state.store)
    spaces.select(OTHER_COMMAND, trusted(spaces, root))
    with _rails_client(tmp_path, spaces, root) as client:
        response = client.get("/api/rails", headers=BEARER)
    assert response.status_code == 200, response.text
    chip = response.json()["git"]
    assert (chip["state"], chip["dirty"]) == ("known", 1)
    assert not marker.exists()

    # The hook is live: a status without the hardening runs it.
    winproc.git(root, "status", "--porcelain")
    assert marker.exists()


def test_the_rails_read_for_a_vault_takes_exactly_two_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def record(*args: object, **kwargs: object) -> rails.Rails:
        calls.append((args, kwargs))
        return rails.Rails(
            agents=[],
            gates=[],
            routines=[],
            counts=rails.VaultCounts(knowledge=0, sources=0, inbox=0),
            git=rails.GitChip(branch="", head="", committed_at="", dirty=None),
        )

    monkeypatch.setattr(rails, "rails_read", record)
    with running(tmp_path) as app:
        assert app.client.get("/api/rails", headers=BEARER).status_code == 200
    assert len(calls) == 1
    assert len(calls[0][0]) == 2
    assert calls[0][1] == {}


def test_a_selected_trusted_code_folder_is_the_active_kind(tmp_path: Path) -> None:
    spaces = service_at(tmp_path)
    folder = tmp_path / "repo"
    folder.mkdir()
    spaces.select(OTHER_COMMAND, trusted(spaces, folder))
    active = spaces.resolve_active()
    assert active.cwd == Path(active.workspace.root.display)
    shown = diagnostics._workspace_json(active, root_available=True)  # pyright: ignore[reportPrivateUsage]
    assert shown["active"]["kind"] == "code"  # type: ignore[index]
