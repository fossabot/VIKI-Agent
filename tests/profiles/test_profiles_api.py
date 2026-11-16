"""Authenticated profile routes using synthetic app data and no native provider."""

from __future__ import annotations

import logging
from pathlib import Path
from uuid import uuid4

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from tests.profiles.test_profile_document import document
from viki_agent.profiles import service as profiles
from viki_agent.profiles.profiles_api import MAX_BODY, profile_routes
from viki_agent.profiles.service import ProfileService
from viki_agent.state import AppState, Prefs

HEADERS = {"Authorization": "Bearer synthetic", "Origin": "http://localhost:8765"}


def client(tmp_path: Path) -> TestClient:
    return TestClient(
        Starlette(
            routes=profile_routes(
                ProfileService(AppState(tmp_path / "state.json", Prefs()).store),
                authorized=lambda request: (
                    request.headers.get("authorization") == "Bearer synthetic"
                ),
                origins=frozenset({"http://localhost:8765"}),
                workers=set(),
            )
        )
    )


def test_profile_routes_preserve_idempotency_and_revision_boundaries(tmp_path: Path) -> None:
    profile_id = str(uuid4())
    request = {"profile_id": profile_id, "command_id": str(uuid4()), "document": document()}
    with client(tmp_path) as api:
        first = api.post("/api/v2/profiles", headers=HEADERS, json=request)
        assert first.status_code == 200
        assert api.post("/api/v2/profiles", headers=HEADERS, json=request).json() == first.json()
        saved = api.get(f"/api/v2/profiles/{profile_id}", headers=HEADERS)
        assert saved.json()["document"] == request["document"]
        assert saved.headers["cache-control"] == "no-store"
        assert (
            api.get("/api/v2/profiles", headers=HEADERS).json()["profiles"][0]["profile_id"]
            == profile_id
        )
        revised = api.patch(
            f"/api/v2/profiles/{profile_id}",
            headers=HEADERS,
            json={
                "command_id": str(uuid4()),
                "expected_revision": 1,
                "document": {**document(), "instructions": "new synthetic instruction"},
            },
        )
        assert revised.json()["revision"] == 2
        diff = api.get(f"/api/v2/profiles/{profile_id}/compare?left=1&right=2", headers=HEADERS)
        assert diff.json()["changes"][0]["field"] == "instructions"
        assert (
            api.get(f"/api/v2/profiles/{profile_id}?revision=1", headers=HEADERS).json()["document"]
            == document()
        )


def test_profile_auth_origin_and_closed_input_are_enforced(tmp_path: Path) -> None:
    with client(tmp_path) as api:
        for method, headers, status in (
            ("GET", {}, 401),
            ("POST", {"Authorization": "Bearer synthetic"}, 403),
            ("POST", {**HEADERS, "Origin": "https://untrusted.example"}, 403),
        ):
            response = api.request(method, "/api/v2/profiles", headers=headers, json={})
            assert response.status_code == status
            assert response.headers["cache-control"] == "no-store"
        for raw in ('{"profile_id":1,"profile_id":2}', '{"document":NaN}', "[1]", "{"):
            response = api.post(
                "/api/v2/profiles",
                headers={**HEADERS, "Content-Type": "application/json"},
                content=raw,
            )
            assert response.status_code == 400
        too_large = api.post(
            "/api/v2/profiles",
            headers={**HEADERS, "Content-Type": "application/json"},
            content=b" " * (MAX_BODY + 1),
        )
        assert too_large.status_code == 413
        assert (
            api.get("/api/v2/profiles?archived=true&archived=false", headers=HEADERS).status_code
            == 400
        )
        assert api.get("/api/v2/profiles", headers=HEADERS).json() == {"profiles": []}


def with_server(**changes: object) -> dict:
    server = {
        "name": "echo",
        "transport": "stdio",
        "command": "synthetic-server",
        "args": ["--stdio"],
        "url": None,
        "note": None,
        **changes,
    }
    value = document()
    value["tools"]["mcp_configuration"] = {"servers": [server]}
    return value


def refusal(field: str, reason: str) -> dict:
    return {
        "error": {
            "code": "invalid_document",
            "message": "The profile document is invalid.",
            "field": field,
            "reason": reason,
        }
    }


def test_a_non_conforming_mcp_configuration_is_refused_with_its_field(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    profile_id = str(uuid4())
    servers = "tools.mcp_configuration.servers[0]"
    with client(tmp_path) as api:
        refused = api.post(
            "/api/v2/profiles",
            headers=HEADERS,
            json={
                "profile_id": profile_id,
                "command_id": str(uuid4()),
                "document": with_server(args=["${SYNTHETIC_HOME}"]),
            },
        )
        assert refused.status_code == 400
        assert refused.json() == refusal(f"{servers}.args[0]", "forbidden_text")
        assert api.get("/api/v2/profiles", headers=HEADERS).json() == {"profiles": []}

        conforming = with_server()
        created = api.post(
            "/api/v2/profiles",
            headers=HEADERS,
            json={"profile_id": profile_id, "command_id": str(uuid4()), "document": conforming},
        )
        assert created.status_code == 200
        saved = api.get(f"/api/v2/profiles/{profile_id}", headers=HEADERS).json()
        assert saved["document"] == conforming

        revised = api.patch(
            f"/api/v2/profiles/{profile_id}",
            headers=HEADERS,
            json={
                "command_id": str(uuid4()),
                "expected_revision": 1,
                "document": with_server(env={"SYNTHETIC": "value"}),
            },
        )
        assert revised.status_code == 400
        assert revised.json() == refusal(f"{servers}.env", "unknown_key")
        assert api.get(f"/api/v2/profiles/{profile_id}", headers=HEADERS).json() == saved
    assert "SYNTHETIC" not in caplog.text
    assert "synthetic-server" not in caplog.text


def test_duplicating_a_stored_non_conforming_revision_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, target = str(uuid4()), str(uuid4())
    stored = with_server(args=["${SYNTHETIC_HOME}"])
    # A revision saved before the schema refused this value.
    with monkeypatch.context() as patch:
        patch.setattr(profiles, "_mcp", lambda value: value)
        ProfileService(AppState(tmp_path / "state.json", Prefs()).store).create(
            source, str(uuid4()), stored
        )
    with client(tmp_path) as api:
        duplicated = api.post(
            f"/api/v2/profiles/{source}/duplicate",
            headers=HEADERS,
            json={
                "command_id": str(uuid4()),
                "expected_revision": 1,
                "target_profile_id": target,
                "target_runtime": "claude",
            },
        )
        assert duplicated.status_code == 400
        assert duplicated.json() == refusal(
            "tools.mcp_configuration.servers[0].args[0]", "forbidden_text"
        )
        assert api.get(f"/api/v2/profiles/{target}", headers=HEADERS).status_code == 404
        read = api.get(f"/api/v2/profiles/{source}", headers=HEADERS)
        assert read.status_code == 200
        assert read.json()["document"] == stored


def test_corrupt_profile_receipt_is_a_private_storage_failure(tmp_path: Path) -> None:
    request = {"profile_id": str(uuid4()), "command_id": str(uuid4()), "document": document()}
    with client(tmp_path) as api:
        assert api.post("/api/v2/profiles", headers=HEADERS, json=request).status_code == 200
        store = AppState(tmp_path / "state.json", Prefs()).store
        with store._connection(write=True) as connection:
            connection.execute(
                "UPDATE profile_commands SET result_revision=NULL WHERE command_id=?",
                (request["command_id"],),
            )
        failed = api.post("/api/v2/profiles", headers=HEADERS, json=request)
        assert failed.status_code == 503
        assert failed.json()["error"]["code"] == "unavailable"
        assert failed.headers["cache-control"] == "no-store"
