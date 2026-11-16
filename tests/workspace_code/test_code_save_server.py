"""The code save route and the shared publication service through `create_app`.

Every save and journal row here runs on any OS whose volume `vault_mutation._supported_volume`
admits, not Windows alone.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from tests.app.test_workspaces import service_at
from tests.test_server import BASE, BEARER, ORIGIN, TOKEN, EmptyCatalog
from tests.workspace_code.test_code_save_recovery import crash_save
from tests.workspace_code.test_workspaces_code import trusted
from viki_agent import server
from viki_agent.config import Config
from viki_agent.drivers.claude.driver import DriverConfig
from viki_agent.session import SessionManager
from viki_agent.state import AppState, Prefs
from viki_agent.vaults.vault_publication import PublicationJournal
from viki_agent.vaults.workspace_mutation import WorkspaceMutationResolver
from viki_agent.workspaces import WorkspaceService

VAULT_COMMAND = "33333333-3333-4333-8333-333333333333"
PUT_HEADERS = {**BEARER, "origin": ORIGIN, "content-type": "application/json"}
Entry = tuple[str, bool, int, str]


@dataclass
class Built:
    app: Starlette
    state: AppState
    spaces: WorkspaceService
    code_id: str
    vault_id: str
    root: Path

    @property
    def journal(self) -> PublicationJournal:
        return PublicationJournal(self.state.store)

    def code_fields(self, content: str, expected_sha256: str | None) -> dict[str, Any]:
        code = self.spaces.get(self.code_id, kind="code")
        return {
            "policy_revision": code.policy_revision,
            "root_physical_id": code.root.physical_id,
            "content": content,
            "expected_sha256": expected_sha256,
            "encoding": "utf-8",
            "newline": "lf",
        }


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _snapshot(root: Path) -> list[Entry]:
    """Every entry under `root` with its size and hash, so a stage or a changed byte shows."""
    entries: list[Entry] = []
    pending = [root]
    while pending:
        with os.scandir(pending.pop()) as listing:
            for item in listing:
                relative = os.path.relpath(item.path, root)
                if item.is_dir(follow_symlinks=False):
                    entries.append((relative, True, 0, ""))
                    pending.append(Path(item.path))
                else:
                    data = Path(item.path).read_bytes()
                    entries.append((relative, False, len(data), _sha(data)))
    return sorted(entries)


def _build(tmp_path: Path) -> Built:
    """As `test_code_open.running` builds its server, but unstarted, so a test can run a crashed
    save against the database before the lifespan recovers it."""
    code_root = tmp_path / "repo"
    (code_root / "src" / "pkg").mkdir(parents=True)
    (code_root / "src" / "pkg" / "a.py").write_bytes(b"x = 1\n")  # write_text would add CRLF
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
        make_driver=lambda *_: pytest.fail("a save may not start a provider"),
    )
    app = server.create_app(
        config=config,
        token=TOKEN,
        manager=manager,
        catalog=EmptyCatalog(home, vault_root),
        static_dir=None,
        workspaces=spaces,
        opener=lambda _url: None,
    )
    return Built(app, state, spaces, code_id, vault_id, code_root)


@contextmanager
def running(tmp_path: Path) -> Iterator[tuple[Built, TestClient]]:
    built = _build(tmp_path)
    with TestClient(built.app, base_url=BASE) as client:
        yield built, client


def _put(client: TestClient, url: str, body: dict[str, Any]) -> Any:  # two unrelated response types
    return client.put(url, content=json.dumps(body), headers=PUT_HEADERS)


def test_put_saves_a_trusted_file_through_create_app(tmp_path: Path) -> None:
    with running(tmp_path) as (built, client):
        fields = built.code_fields("x = 2\n", _sha(b"x = 1\n"))
        response = _put(client, f"/api/v2/code/{built.code_id}/file?path=src/pkg/a.py", fields)
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "saved"
        assert (built.root / "src" / "pkg" / "a.py").read_bytes() == b"x = 2\n"
        assert built.journal.list_pending() == []


def test_the_vault_route_refuses_a_code_id_and_changes_nothing(tmp_path: Path) -> None:
    with running(tmp_path) as (built, client):
        before = (_snapshot(built.root), built.journal.list_pending())
        fields = built.code_fields("x\n", None)
        del fields["encoding"], fields["newline"]  # the note route takes four fields
        response = _put(client, f"/api/v2/vaults/{built.code_id}/notes/n.md", fields)
        assert response.status_code == 409, response.text
        assert response.json()["error"]["code"] == "ordinary_workspace_required"
        assert (_snapshot(built.root), built.journal.list_pending()) == before


def test_the_code_route_does_not_know_a_vault_id(tmp_path: Path) -> None:
    with running(tmp_path) as (built, client):
        fields = built.code_fields("x\n", None)
        response = _put(client, f"/api/v2/code/{built.vault_id}/file?path=a.py", fields)
        assert response.status_code == 404, response.text
        assert response.json()["error"]["code"] == "unknown_workspace"
        assert built.journal.list_pending() == []


@pytest.mark.parametrize(
    ("ref", "kind", "checkpoint"),
    [("src/pkg/new.py", "create", "staged"), ("src/pkg/a.py", "replace", "backed_up")],
)
def test_startup_recovers_a_code_save_row(
    tmp_path: Path, ref: str, kind: str, checkpoint: str
) -> None:
    built = _build(tmp_path)
    target = built.root.joinpath(*ref.split("/"))
    if kind == "replace":
        target.write_bytes(b"old")
    code = built.spaces.get(built.code_id, kind="code")
    # The root the real resolver gives the trusted folder, so the crashed row names what the
    # lifespan will look up.
    root = WorkspaceMutationResolver(built.spaces, git_available=lambda: False)(
        built.code_id, code.policy_revision, code.root.physical_id, kind="code"
    )
    crash_save(tmp_path, root, ref, kind, checkpoint, built.state.database_path)
    assert [row.ref for row in built.journal.list_pending()] == [ref]

    with TestClient(built.app, base_url=BASE):
        pass
    assert built.journal.list_pending() == []
    assert target.read_bytes() == b"new"
    # No stage or backup name is left beside the saved file.
    names = {Path(entry[0]).as_posix() for entry in _snapshot(built.root) if not entry[1]}
    assert names == {"src/pkg/a.py", ref}
