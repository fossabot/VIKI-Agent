"""Capture routes wired through `create_app`: the inbox listing and the suggestion
status answer through the app once a vault is the active workspace."""

from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path

from starlette.applications import Starlette
from starlette.testclient import TestClient

from tests.test_server import BASE, BEARER, _p1_app
from viki_agent.workspaces import WorkspaceService


def _admit_vault(root: Path, spaces: WorkspaceService) -> str:
    """Register an empty folder as a vault and select it, the way a launch resolves one."""
    root.mkdir(parents=True, exist_ok=True)
    workspace, _ = spaces.register_existing(str(uuid.uuid4()), str(root))
    spaces.select(str(uuid.uuid4()), workspace.workspace_id)
    return workspace.workspace_id


def _vault_app(tmp_path: Path) -> tuple[Starlette, str]:
    """An app wired as the launcher wires it (`_p1_app`), whose active workspace is a fresh, empty
    vault; the second item is its id."""
    ids: list[str] = []
    app = _p1_app(
        tmp_path, prepare=lambda spaces: ids.append(_admit_vault(tmp_path / "notes", spaces))
    )
    return app, ids[0]


def test_the_capture_routes_answer_through_the_app(tmp_path: Path) -> None:
    app, workspace_id = _vault_app(tmp_path)
    with TestClient(app, base_url=BASE) as client:
        listing = client.get(f"/api/v2/vaults/{workspace_id}/captures", headers=BEARER)
        assert listing.status_code == 200
        assert listing.json()["captures"] == []

        suggest = client.get(f"/api/v2/vaults/{workspace_id}/captures/suggest", headers=BEARER)
        assert suggest.status_code == 200
        assert suggest.json() == {"run": None}


def test_probe_the_poll_answers_while_a_writer_holds_the_store(tmp_path: Path) -> None:
    """The poll is what the dialog calls every 2 s during a run; one failure ends its polling
    (useCaptureListing sets `error` and schedules nothing more)."""
    app, workspace_id = _vault_app(tmp_path)
    url = f"/api/v2/vaults/{workspace_id}/captures/suggest"
    with TestClient(app, base_url=BASE) as client:
        assert client.get(url, headers=BEARER).status_code == 200  # free store: fine
        writer = sqlite3.connect(tmp_path / "data" / "app.sqlite3")
        writer.execute("BEGIN EXCLUSIVE")  # what a commit holds for its fsyncs
        try:
            held = client.get(url, headers=BEARER)
        finally:
            writer.rollback()
            writer.close()
    assert held.status_code == 200, held.text
