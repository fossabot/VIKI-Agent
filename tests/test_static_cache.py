"""The built UI is served fresh in both ordinary and recovery launches."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from tests.profiles.test_profile_launch_api import BASE, TOKEN, Rig
from viki_agent import server
from viki_agent.app.diagnostics import ProviderChecks

ASSETS = {
    "index.html": b"<!doctype html><title>Synthetic UI</title>",
    "assets/app.js": b"console.log('synthetic-ui');",
    "assets/app.css": b"body { color: #fff; }",
    "assets/brand.woff2": b"synthetic-font",
    "assets/logo.svg": b"<svg xmlns='http://www.w3.org/2000/svg'/>",
}


@pytest.fixture(params=["ordinary", "recovery"])
def client(tmp_path: Path, request: pytest.FixtureRequest) -> Iterator[TestClient]:
    rig = Rig(tmp_path)
    static_dir = tmp_path / "static"
    for ref, body in ASSETS.items():
        path = static_dir / ref
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
    checks = ProviderChecks(readiness=rig.readiness, cli_version=lambda: "", git_version=lambda: "")
    if request.param == "recovery":
        app = server.create_recovery_app(
            config=rig.config,
            token=TOKEN,
            readiness=rig.readiness,
            checks=checks,
            diagnostics_document=lambda: {},
            static_dir=static_dir,
        )
    else:
        app = server.create_app(
            config=rig.config,
            token=TOKEN,
            manager=rig.manager(),
            catalog=rig.catalog,
            readiness=rig.readiness,
            checks=checks,
            diagnostics_document=lambda: {},
            static_dir=static_dir,
            vault_poll_s=60,
        )
    with TestClient(app, base_url=BASE) as running:
        yield running
    assert rig.drivers == []


@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_ui_and_static_errors_do_not_store(client: TestClient, method: str) -> None:
    for url, body in {"/": ASSETS["index.html"], **ASSETS}.items():
        response = client.request(method, url)
        assert response.status_code == 200, url
        assert response.content == (body if method == "GET" else b""), url
        assert response.headers["cache-control"] == "no-store", url
        for key, value in server.SECURITY_HEADERS.items():
            assert response.headers[key] == value, url
    missing = client.request(method, "/assets/missing.js")
    assert missing.status_code == 404
    assert missing.headers["cache-control"] == "no-store"


def test_conditional_static_responses_do_not_store(client: TestClient) -> None:
    for url in ("/", "/assets/app.js", "/assets/app.css", "/assets/brand.woff2"):
        original = client.get(url)
        for validator, header in (
            ("if-none-match", "etag"),
            ("if-modified-since", "last-modified"),
        ):
            response = client.get(url, headers={validator: original.headers[header]})
            assert response.status_code == 304, (url, validator)
            assert response.content == b""
            assert response.headers["cache-control"] == "no-store", (url, validator)


def test_api_authentication_refusals_do_not_store(client: TestClient) -> None:
    response = client.get("/api/v2/capabilities")
    assert response.status_code == 401
    assert response.headers["cache-control"] == "no-store"


def test_foreign_host_still_refuses_before_serving_ui(client: TestClient) -> None:
    response = client.get("/", headers={"host": "example.com:8765"})
    assert response.status_code == 400
    assert response.text == "Bad Host header"
    assert "cache-control" not in response.headers
    for key, value in server.SECURITY_HEADERS.items():
        assert response.headers[key] == value
