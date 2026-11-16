"""Synthetic native metadata routes expose neither arbitrary paths nor prompt bodies."""

from __future__ import annotations

from pathlib import Path

from starlette.applications import Starlette
from starlette.testclient import TestClient

from tests.profiles.test_native_assets import _inspector, _write
from viki_agent.profiles.native_assets_api import native_asset_routes

HEADERS = {"Authorization": "Bearer synthetic", "Origin": "http://localhost:8765"}


def test_native_metadata_routes_are_private_and_closed(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write(
        workspace / ".claude" / "agents" / "review.md",
        "---\nname: Review\ndescription: Does work\n---\nPRIVATE BODY",
    )
    inspector, owner = _inspector(workspace)
    source_id = f"claude:workspace:{owner.workspace.workspace_id}"
    routes = native_asset_routes(
        inspector,
        lambda: [{"source_id": source_id, "provider": "claude", "scope": "workspace"}],
        authorized=lambda request: request.headers.get("authorization") == "Bearer synthetic",
        origins=frozenset({"http://localhost:8765"}),
        workers=set(),
    )
    with TestClient(Starlette(routes=routes)) as client:
        assert client.get("/api/v2/native-assets").status_code == 401
        assert (
            client.get(
                "/api/v2/native-assets", headers={**HEADERS, "Origin": "https://bad.example"}
            ).status_code
            == 403
        )
        response = client.get("/api/v2/native-assets", headers=HEADERS)
        assert response.json()["sources"][0]["source_id"] == source_id
        result = client.get(
            "/api/v2/native-assets",
            params={"source_id": source_id, "kind": "agent"},
            headers=HEADERS,
        )
        assert result.status_code == 200 and result.headers["cache-control"] == "no-store"
        assert result.json()["assets"][0]["name"] == "Review"
        assert "PRIVATE BODY" not in result.text and str(workspace) not in result.text
        for query in (
            "source_id=claude:user&source_id=other",
            "kind=agent",
            "path=C%3A%5C",
            "source_id=unknown",
            "kind=other",
        ):
            response = client.get(f"/api/v2/native-assets?{query}", headers=HEADERS)
            assert response.status_code == 400 and response.headers["cache-control"] == "no-store"
