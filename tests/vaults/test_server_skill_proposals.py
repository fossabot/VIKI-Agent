"""The skill proposal routes wired into the real app: registered only with workspaces, and the
whole create-to-applied flow alongside the decision inbox."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import cast

from starlette.applications import Starlette
from starlette.testclient import TestClient

from tests.test_server import (
    BASE,
    BEARER,
    ORIGIN,
    _p1_app,
    _protected_vault_app,
    _v2_routes,
    running,
)

POST = {**BEARER, "origin": ORIGIN}
SKILL_PATHS = {
    "/api/v2/vaults/{workspace_id}/skill-proposals",
    "/api/v2/decisions/skills/{proposal_id}",
}
NAME = "weekly-recap"
SECRET = "SECRET-WEEKLY-STEPS-9f2"
TEXT = f"---\nname: {NAME}\ndescription: Runs the weekly recap\n---\n{SECRET}\n"


def test_the_skill_proposal_routes_are_registered_only_with_workspaces(tmp_path: Path) -> None:
    with running(tmp_path) as app:
        bare = {path for path, _ in _v2_routes(cast(Starlette, app.client.app))}
    assert bare & SKILL_PATHS == set()
    wired = {path for path, _ in _v2_routes(_p1_app(tmp_path / "p1"))}
    assert wired >= SKILL_PATHS


def test_the_skill_proposal_flow_answers_through_the_app(tmp_path: Path) -> None:
    app, workspace_id = _protected_vault_app(tmp_path)
    with TestClient(app, base_url=BASE) as client:
        created = client.post(
            f"/api/v2/vaults/{workspace_id}/skill-proposals",
            headers=POST,
            json={"text": TEXT, "trigger": "button", "sessions": [str(uuid.uuid4())]},
        )
        assert created.status_code == 201 and "SECRET" not in created.text
        proposal = created.json()["proposal"]
        proposal_id, revision = proposal["id"], proposal["revision"]

        items = client.get("/api/v2/decisions", headers=BEARER).json()["items"]
        [listed] = [item for item in items if item["kind"] == "skill_proposal"]
        assert listed["id"] == proposal_id and listed["state"] == "open"

        detail = client.get(f"/api/v2/decisions/skills/{proposal_id}", headers=BEARER)
        assert detail.status_code == 200 and detail.json()["proposal"]["text"] == TEXT

        approved = client.post(
            f"/api/v2/decisions/skills/{proposal_id}",
            headers=POST,
            json={"action": "approve", "revision": revision},
        )
        assert approved.status_code == 200 and approved.json()["proposal"]["state"] == "handed"

        # Approving only records the approval; the app never writes the file itself (a chat the
        # user sends would, later). The test plays that part to prove the inbox re-derives
        # "applied" from the installed bytes alone.
        skill_file = tmp_path / "notes" / ".claude" / "skills" / NAME / "SKILL.md"
        skill_file.parent.mkdir(parents=True)
        skill_file.write_bytes(TEXT.encode("utf-8"))

        items_after = client.get("/api/v2/decisions", headers=BEARER).json()["items"]
    [after] = [item for item in items_after if item["kind"] == "skill_proposal"]
    assert after["state"] == "applied"
