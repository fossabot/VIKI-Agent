from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any, cast

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from viki_agent.state import AppState, Prefs
from viki_agent.vaults.vault_bookmark_api import bookmark_routes
from viki_agent.vaults.vault_bookmarks import MAX_BOOKMARKS, BookmarkService
from viki_agent.workspaces import WorkspaceError, WorkspaceService

AUTH = {"Authorization": "Bearer secret"}
ORIGIN = "https://app.invalid"
# PUT and DELETE change state, so they need a listed Origin, as a browser always sends one.
WRITE = {**AUTH, "Origin": ORIGIN}
WID = str(uuid.uuid4())
BASE = f"/api/v2/vaults/{WID}/bookmarks"


class _Workspaces:
    def get(self, workspace_id: str) -> object:
        if workspace_id != WID:
            raise WorkspaceError("unknown_workspace")
        return object()


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    state = AppState(tmp_path / "state.json", Prefs())
    service = BookmarkService(state, cast(WorkspaceService, _Workspaces()))
    workers: set[asyncio.Task[Any]] = set()
    routes = bookmark_routes(
        service,
        authorized=lambda request: request.headers.get("authorization") == "Bearer secret",
        origins=frozenset({ORIGIN}),
        workers=workers,
    )
    return TestClient(Starlette(routes=routes))


def test_put_list_delete_round_trip(client: TestClient) -> None:
    put = client.put(f"{BASE}/a/b.md", headers=WRITE)
    assert put.status_code == 200 and put.json()["bookmark"]["ref"] == "a/b.md"
    assert client.put(f"{BASE}/a/b.md", headers=WRITE).json() == put.json()
    listed = client.get(BASE, headers=AUTH).json()
    assert [mark["ref"] for mark in listed["bookmarks"]] == ["a/b.md"]
    assert client.delete(f"{BASE}/a/b.md", headers=WRITE).json() == {"removed": True}
    assert client.delete(f"{BASE}/a/b.md", headers=WRITE).json() == {"removed": False}


def test_missing_bearer_is_401(client: TestClient) -> None:
    for call in (client.get(BASE), client.put(f"{BASE}/a.md"), client.delete(f"{BASE}/a.md")):
        assert call.status_code == 401


def test_foreign_origin_is_403(client: TestClient) -> None:
    headers = {**AUTH, "Origin": "https://evil.invalid"}
    assert client.put(f"{BASE}/a.md", headers=headers).status_code == 403
    assert client.delete(f"{BASE}/a.md", headers=headers).status_code == 403
    assert client.get(BASE, headers=headers).status_code == 403
    assert client.get(BASE, headers={**AUTH, "Origin": ORIGIN}).status_code == 200


def test_a_put_or_delete_without_an_origin_is_403_and_a_get_without_one_is_answered(
    client: TestClient,
) -> None:
    """The form `proposal_api.endpoint` uses: an absent Origin is refused on a state-changing
    method exactly as a foreign one is, while a GET still answers without one."""
    refused = client.put(f"{BASE}/a.md", headers=AUTH)
    assert refused.status_code == 403 and refused.json()["error"]["code"] == "forbidden_origin"
    assert client.delete(f"{BASE}/a.md", headers=AUTH).status_code == 403
    assert client.get(BASE, headers=AUTH).json() == {"bookmarks": []}


def test_a_body_on_put_or_delete_is_415(client: TestClient) -> None:
    for method in ("PUT", "DELETE"):
        body = client.request(method, f"{BASE}/a.md", headers=WRITE, content=b"{}")
        assert body.status_code == 415
        assert body.json()["error"]["code"] == "invalid_representation"
    assert client.get(BASE, headers=AUTH).json() == {"bookmarks": []}


def test_content_type_and_encoding_are_refused(client: TestClient) -> None:
    for extra in ({"Content-Type": "text/plain"}, {"Content-Encoding": "gzip"}):
        refused = client.put(f"{BASE}/a.md", headers={**WRITE, **extra})
        assert refused.status_code == 415
    ok = client.put(f"{BASE}/a.md", headers={**WRITE, "Content-Type": "application/json"})
    assert ok.status_code == 200


def test_typed_errors(client: TestClient) -> None:
    assert client.put(f"{BASE}/x/.git/y.md", headers=WRITE).status_code == 404
    assert client.put(f"{BASE}/a//b.md", headers=WRITE).status_code == 400
    other = f"/api/v2/vaults/{uuid.uuid4()}/bookmarks"
    unknown = client.get(other, headers=AUTH)
    assert unknown.status_code == 404 and unknown.json()["error"]["code"] == "unknown_workspace"


def test_the_201st_bookmark_is_409(client: TestClient) -> None:
    for index in range(MAX_BOOKMARKS):
        assert client.put(f"{BASE}/n{index}.md", headers=WRITE).status_code == 200
    full = client.put(f"{BASE}/more.md", headers=WRITE)
    assert full.status_code == 409 and full.json()["error"]["code"] == "bookmarks_full"
