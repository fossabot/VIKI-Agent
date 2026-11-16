"""The code-folder workspace routes over a synthetic store; no port is ever bound."""

from __future__ import annotations

import sys
import uuid
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from tests.app.test_workspace_api import AUTH, BASE, MUTATION, app_at
from tests.app.test_workspaces import (
    COMMAND,
    OTHER_COMMAND,
    fake_stat,
    make_junction,
    service_at,
    store_at,
)

CODE = "/api/v2/workspaces/code"
VAULT = "/api/v2/vaults/open"


def folder(tmp_path: Path, name: str) -> Path:
    root = tmp_path / name
    root.mkdir()
    return root


def open_code(client: TestClient, path: Path | str, command: str = COMMAND) -> Any:
    return client.post(CODE, headers=MUTATION, json={"command_id": command, "path": str(path)})


def open_vault(client: TestClient, path: Path | str, command: str = COMMAND) -> Any:
    return client.post(VAULT, headers=MUTATION, json={"command_id": command, "path": str(path)})


def test_open_code_folder_returns_201_then_200(tmp_path: Path) -> None:
    app, _ = app_at(tmp_path)
    client = TestClient(app, base_url=BASE)
    root = folder(tmp_path, "repo")

    opened = open_code(client, root)
    assert opened.status_code == 201, opened.text
    workspace = opened.json()["workspace"]
    assert workspace["kind"] == "code" and workspace["policy"] == "existing_readonly"
    assert workspace["root"] == str(root) and workspace["persistent"] is True
    assert opened.json()["inspection"]["workspace_id"] == workspace["workspace_id"]
    assert opened.json()["inspection"]["markers"]["mcp_json"] is False

    again = open_code(client, root, OTHER_COMMAND)
    assert again.status_code == 200
    assert again.json()["workspace"]["workspace_id"] == workspace["workspace_id"]


def test_the_inspection_shows_mcp_json_and_requires_claude_config(tmp_path: Path) -> None:
    app, _ = app_at(tmp_path)
    client = TestClient(app, base_url=BASE)
    root = folder(tmp_path, "repo")
    (root / ".mcp.json").write_text("{}\n", encoding="utf-8")
    inspection = open_code(client, root).json()["inspection"]
    assert inspection["markers"]["mcp_json"] is True
    assert inspection["acknowledgements_required"] == ["claude_config_present"]

    # A vault keeps its markers: `.mcp.json` is not one of them and asks for nothing.
    notes = folder(tmp_path, "notes")
    (notes / ".mcp.json").write_text("{}\n", encoding="utf-8")
    vault = open_vault(client, notes, OTHER_COMMAND).json()["inspection"]
    assert "mcp_json" not in vault["markers"] and vault["acknowledgements_required"] == []


def test_the_listing_and_the_single_get_include_the_code_row(tmp_path: Path) -> None:
    app, _ = app_at(tmp_path)
    client = TestClient(app, base_url=BASE)
    code_id = open_code(client, folder(tmp_path, "repo")).json()["workspace"]["workspace_id"]
    vault_id = open_vault(client, folder(tmp_path, "notes"), OTHER_COMMAND).json()["workspace"][
        "workspace_id"
    ]

    rows = client.get("/api/v2/workspaces", headers=AUTH).json()["workspaces"]
    assert {row["workspace_id"]: row["kind"] for row in rows} == {
        code_id: "code",
        vault_id: "vault",
    }
    single = client.get(f"/api/v2/workspaces/{code_id}", headers=AUTH)
    assert single.status_code == 200 and single.json()["kind"] == "code"
    missing = client.get(f"/api/v2/workspaces/{uuid.uuid4()}", headers=AUTH)
    assert missing.status_code == 404


def test_a_get_of_the_code_path_is_an_unknown_workspace_and_nothing_falls_to_the_patch(
    tmp_path: Path,
) -> None:
    app, service = app_at(tmp_path)
    client = TestClient(app, base_url=BASE)
    answer = client.get(CODE, headers=AUTH)
    assert answer.status_code == 404 and answer.json()["error"]["code"] == "unknown_workspace"
    # The policy patch has no route of its own here; a PATCH or a bad body never reaches it.
    patched = client.patch(CODE, headers=MUTATION, json={})
    assert patched.status_code == 405
    assert service.list(kind=None) == []


def test_denied_overlapping_and_colliding_folders_are_refused_as_a_vault_is(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, service = app_at(tmp_path)
    client = TestClient(app, base_url=BASE)
    data = tmp_path / "data"
    for raw in (data, tmp_path / "nowhere", "relative", "x" * 5000):
        code, vault = open_code(client, raw), open_vault(client, raw)
        assert (code.status_code, code.json()["error"]["code"]) == (
            vault.status_code,
            vault.json()["error"]["code"],
        ), raw
    assert open_code(client, data).json()["error"]["code"] == "denied_location"

    root = folder(tmp_path, "repo")
    registered = open_code(client, root).json()["workspace"]["workspace_id"]
    child = folder(root, "inner")
    parent = open_code(client, tmp_path, OTHER_COMMAND)
    inside = open_code(client, child, OTHER_COMMAND)
    assert inside.status_code == 409
    assert inside.json()["error"]["code"] == "overlaps_registration"
    assert inside.json()["error"]["workspace_id"] == registered
    assert open_vault(client, child, OTHER_COMMAND).status_code == 409
    assert parent.status_code in (403, 409)

    if sys.platform == "win32":
        # An equal key with a different identity is a per-directory case difference.
        monkeypatch.setattr("os.stat", fake_stat({"st_ino": 7}))
        collision = open_code(client, root, OTHER_COMMAND)
        monkeypatch.undo()
        assert collision.status_code == 409
        assert collision.json()["error"]["code"] == "case_collision"
    assert [row.workspace_id for row in service.list(kind=None)] == [registered]


def test_a_vault_and_a_code_folder_never_relabel_each_other(tmp_path: Path) -> None:
    app, service = app_at(tmp_path)
    client = TestClient(app, base_url=BASE)
    notes, repo = folder(tmp_path, "notes"), folder(tmp_path, "repo")
    assert open_vault(client, notes).status_code == 201
    assert open_code(client, repo).status_code == 201

    as_code = open_code(client, notes, OTHER_COMMAND)
    assert as_code.status_code == 409 and as_code.json()["error"]["code"] == "kind_mismatch"
    as_vault = open_vault(client, repo, OTHER_COMMAND)
    assert as_vault.status_code == 409 and as_vault.json()["error"]["code"] == "kind_mismatch"
    assert as_vault.json()["error"]["message"] == as_code.json()["error"]["message"]
    assert sorted(row.kind for row in service.list(kind=None)) == ["code", "vault"]

    if sys.platform == "win32":
        for target, label, opener in ((notes, "n", open_code), (repo, "r", open_vault)):
            alias = tmp_path / f"alias-{label}"
            make_junction(alias, target)
            through = opener(client, alias, OTHER_COMMAND)
            assert through.status_code == 409, label
            assert through.json()["error"]["code"] == "kind_mismatch", label


def test_an_untrusted_code_folder_cannot_be_selected_and_a_trusted_one_can(
    tmp_path: Path,
) -> None:
    app, service = app_at(tmp_path)
    client = TestClient(app, base_url=BASE)
    workspace = open_code(client, folder(tmp_path, "repo")).json()["workspace"]
    select = {"command_id": COMMAND, "workspace_id": workspace["workspace_id"]}

    refused = client.post("/api/v2/workspaces/select", headers=MUTATION, json=select)
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "trust_required"
    assert service.selected_id() is None

    trusted = client.patch(
        f"/api/v2/vaults/{workspace['workspace_id']}/policy",
        headers=MUTATION,
        json={
            "command_id": OTHER_COMMAND,
            "expected_policy_revision": workspace["policy_revision"],
            "policy": "ordinary",
            "acknowledged": [],
        },
    )
    assert trusted.status_code == 200, trusted.text
    chosen = client.post("/api/v2/workspaces/select", headers=MUTATION, json=select)
    assert chosen.status_code == 200
    assert chosen.json()["selected_id"] == workspace["workspace_id"]


def test_the_active_code_folder_cannot_be_untrusted(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    app, _ = app_at(tmp_path, service_at(tmp_path, store))
    client = TestClient(app, base_url=BASE)
    workspace = open_code(client, folder(tmp_path, "repo")).json()["workspace"]
    workspace_id = workspace["workspace_id"]
    patch = {
        "command_id": OTHER_COMMAND,
        "expected_policy_revision": workspace["policy_revision"],
        "policy": "ordinary",
        "acknowledged": [],
    }
    url = f"/api/v2/vaults/{workspace_id}/policy"
    revision = client.patch(url, headers=MUTATION, json=patch).json()["policy_revision"]
    client.post(
        "/api/v2/workspaces/select",
        headers=MUTATION,
        json={"command_id": COMMAND, "workspace_id": workspace_id},
    )
    # The next launch runs in it; this launch resolves it once and then keeps it.
    fresh = service_at(tmp_path, store)
    assert fresh.resolve_active().workspace.workspace_id == workspace_id
    app_fresh, _ = app_at(tmp_path, fresh)
    client = TestClient(app_fresh, base_url=BASE)
    revoke = client.patch(
        url,
        headers=MUTATION,
        json={**patch, "expected_policy_revision": revision, "policy": "existing_readonly"},
    )
    assert revoke.status_code == 409 and revoke.json()["error"]["code"] == "workspace_active"


def test_the_code_route_needs_the_token_and_the_origin(tmp_path: Path) -> None:
    app, service = app_at(tmp_path)
    client = TestClient(app, base_url=BASE)
    root = folder(tmp_path, "repo")
    body = {"command_id": COMMAND, "path": str(root)}
    anonymous = client.post(CODE, json=body)
    assert anonymous.status_code == 401 and anonymous.json()["error"]["code"] == "unauthorized"
    no_origin = client.post(CODE, headers=AUTH, json=body)
    assert no_origin.status_code == 403 and no_origin.json()["error"]["code"] == "forbidden_origin"
    stranger = client.post(CODE, headers={**AUTH, "origin": "http://evil"}, json=body)
    assert stranger.status_code == 403
    assert service.list(kind=None) == []


def test_the_code_route_body_is_strict(tmp_path: Path) -> None:
    app, service = app_at(tmp_path)
    client = TestClient(app, base_url=BASE)
    root = str(folder(tmp_path, "repo"))
    for payload in (
        {"command_id": COMMAND},
        {"command_id": COMMAND, "path": root, "kind": "vault"},
        {"command_id": "x", "path": root},
        {"command_id": COMMAND, "path": 5},
    ):
        answer = client.post(CODE, headers=MUTATION, json=payload)
        assert answer.status_code == 400, payload
        assert answer.json()["error"]["code"] == "invalid_body", payload
    plain = client.post(CODE, headers={**MUTATION, "content-type": "text/plain"}, content=b"{}")
    assert plain.status_code == 415
    assert service.list(kind=None) == []
