from __future__ import annotations

import json
import unicodedata
import uuid
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from viki_agent.app.settings import SettingsService
from viki_agent.llm.api import connection_routes
from viki_agent.llm.connections import (
    KEY,
    ConnectionError,
    Connections,
    parse_connection,
    validate_url,
)
from viki_agent.llm.gateway import Gateway
from viki_agent.state import AppState, Prefs

BASE = "http://127.0.0.1:8765"
TOKEN = "synthetic-window-token"
AUTH = {"authorization": f"Bearer {TOKEN}", "origin": BASE}
FORM = {
    "name": "Local test",
    "base_url": "http://127.0.0.1:11434/v1",
    "api_mode": "chat_completions",
    "model": "example/model",
    "api_key": "synthetic-key-only",
}


def service(tmp_path: Path) -> tuple[SettingsService, Connections]:
    state = AppState(tmp_path / "state.json", Prefs())
    settings = SettingsService(
        state.store, provider_defaults=Prefs, on_chat_defaults=state.set_defaults
    )
    return settings, Connections(settings)


def test_connections_are_sealed_and_resolve_exact_identity(tmp_path: Path) -> None:
    settings, connections = service(tmp_path)
    row = parse_connection(FORM)
    connections.write(0, [row])
    raw = json.dumps(settings.export_values())
    assert FORM["api_key"] not in raw and FORM["base_url"] not in raw
    assert FORM["api_key"] not in repr(row) and FORM["api_key"] not in repr(row.public())
    assert connections.resolve(row.ref) == row
    with pytest.raises(ConnectionError):
        connections.resolve(f"viki-llm/{uuid.uuid4()}/example/model")
    with pytest.raises(ConnectionError):
        connections.resolve(row.ref + "-different")
    settings.patch(str(uuid.uuid4()), 1, {KEY: "corrupt"})
    with pytest.raises(ConnectionError, match="connections_unavailable"):
        connections.resolve(row.ref)


def test_qualified_default_preserves_opaque_unicode_model_identity(tmp_path: Path) -> None:
    settings, connections = service(tmp_path)
    row = parse_connection({**FORM, "model": "model-e\u0301"})
    connections.write(0, [row])
    settings.patch(str(uuid.uuid4()), 1, {"chat.default_model": row.ref})
    saved = settings.chat_defaults().model
    assert saved is not None
    assert saved == row.ref
    assert connections.resolve(saved) == row
    with pytest.raises(ConnectionError, match="connection_missing"):
        connections.resolve(unicodedata.normalize("NFC", row.ref))
    settings.patch(str(uuid.uuid4()), 2, {"chat.default_model": "native-e\u0301"})
    assert settings.chat_defaults().model == "native-\u00e9"


@pytest.mark.parametrize(
    "url",
    [
        "http://remote.example/v1",
        "http://localhost.attacker.test",
        "https://key" + "@" + "host.test",
        "https://host.test?api_key=secret",
        "https://host.test/#fragment",
        "file:///private",
        "https://host.test\\evil",
        "http://localhost:0",
        "https://host.test\n/injected",
    ],
)
def test_endpoint_url_rejects_credential_and_transport_confusion(url: str) -> None:
    with pytest.raises(ConnectionError):
        validate_url(url)


@pytest.mark.parametrize(
    "url", ["http://localhost:11434", "http://[::1]:1234/v1", "https://models.example/api/v1"]
)
def test_local_and_https_endpoints(url: str) -> None:
    assert validate_url(url) == url


@pytest.mark.parametrize(
    ("base", "expected"),
    [
        ("https://models.example", "https://models.example/v1/chat/completions"),
        ("https://models.example/v1", "https://models.example/v1/chat/completions"),
        ("https://models.example/custom/api", "https://models.example/custom/api/chat/completions"),
    ],
)
def test_endpoint_preserves_the_servers_api_prefix(base: str, expected: str) -> None:
    assert parse_connection({**FORM, "base_url": base}).url("chat/completions") == expected


@pytest.mark.parametrize("model", ["a" * 513, "\u3042" * 400, "\ud800"])
def test_model_identifier_fits_the_chat_preference_and_is_utf8(model: str) -> None:
    with pytest.raises(ConnectionError):
        parse_connection({**FORM, "model": model})


def test_api_auth_revision_and_removal_do_not_return_keys(tmp_path: Path) -> None:
    settings, connections = service(tmp_path)
    gateway = Gateway()
    app = Starlette(
        routes=connection_routes(
            connections,
            gateway,
            authorized=lambda request: (
                request.headers.get("authorization") == AUTH["authorization"]
            ),
            origins=frozenset({BASE}),
        )
    )
    with TestClient(app, base_url=BASE) as client:
        path = "/api/v2/llm/connections"
        assert client.get(path).status_code == 401
        assert (
            client.post(path, headers={"authorization": AUTH["authorization"]}, json={}).status_code
            == 403
        )
        saved = client.post(path, headers=AUTH, json={"expected_revision": 0, "connection": FORM})
        assert saved.status_code == 200
        assert FORM["api_key"] not in saved.text and '"api_key"' not in saved.text
        listing = saved.json()
        row = listing["connections"][0]
        assert row["has_api_key"] is True
        stale = client.post(path, headers=AUTH, json={"expected_revision": 0, "connection": FORM})
        assert stale.status_code == 409 and len(connections.read()[1]) == 1
        settings.patch(
            str(uuid.uuid4()), listing["revision"], {"chat.default_model": row["model_ref"]}
        )
        revision = client.get(path, headers=AUTH).json()["revision"]
        removed = client.request(
            "DELETE", path + "/" + row["id"], headers=AUTH, json={"expected_revision": revision}
        )
        assert removed.status_code == 200 and removed.json()["connections"] == []
        assert settings.chat_defaults().model is None
        with pytest.raises(ConnectionError):
            connections.resolve(row["model_ref"])


def test_settings_exports_drop_connection_material_with_identity_enabled(tmp_path: Path) -> None:
    from tests.app.test_exports import service as export_service

    settings, connections = service(tmp_path)
    connections.write(0, [parse_connection(FORM)])
    exporter = export_service(tmp_path, settings_values=settings.export_values())
    for kind in ("settings", "diagnostics"):
        preview = exporter.preview(kind, {"identity": True, "settings": True, "paths": True})
        assert KEY not in preview.content[1]
