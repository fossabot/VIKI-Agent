"""Recovery's built frontend serves HTTP and declines its initial session socket."""

from __future__ import annotations

from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from viki_agent import server
from viki_agent.app.diagnostics import ProviderChecks, Readiness
from viki_agent.config import Config

BASE = "http://127.0.0.1:8765"
TOKEN = "test-token"
BEARER = {"authorization": f"Bearer {TOKEN}"}
INDEX = "<!doctype html><title>Synthetic recovery</title>"
ASSET = "console.log('synthetic recovery');"


@pytest.fixture
def recovery_static(tmp_path: Path) -> Starlette:
    static_dir = tmp_path / "static"
    (static_dir / "assets").mkdir(parents=True)
    (static_dir / "index.html").write_text(INDEX, encoding="utf-8")
    (static_dir / "assets" / "app.js").write_text(ASSET, encoding="utf-8")
    config = Config(
        vault=None, claude_home=tmp_path / "home", cli=None, edge=None, data_dir=tmp_path / "data"
    )
    readiness = Readiness(config=config, launch_id="launch-1", git_available=lambda: False)
    return server.create_recovery_app(
        config=config,
        token=TOKEN,
        readiness=readiness,
        checks=ProviderChecks(readiness=readiness, cli_version=lambda: "", git_version=lambda: ""),
        diagnostics_document=lambda: {"synthetic": True},
        static_dir=static_dir,
    )


@pytest.mark.parametrize(
    ("host", "origin", "code"),
    [
        ("127.0.0.1:8765", BASE, 1000),
        ("evil.example:8765", BASE, 1008),
        ("127.0.0.1:8765", "http://evil.example:8765", 1008),
    ],
)
def test_recovery_static_rejects_socket(
    recovery_static: Starlette, host: str, origin: str, code: int
) -> None:
    with TestClient(recovery_static, base_url=BASE) as client:
        with (
            pytest.raises(WebSocketDisconnect) as closed,
            client.websocket_connect("/ws", headers={"host": host, "origin": origin}),
        ):
            pytest.fail("recovery admitted a session socket")
        assert closed.value.code == code


@pytest.mark.parametrize("method", ["GET", "HEAD"])
@pytest.mark.parametrize(("path", "body"), [("/", INDEX), ("/assets/app.js", ASSET)])
def test_recovery_static_serves_http(
    recovery_static: Starlette, method: str, path: str, body: str
) -> None:
    assert str(recovery_static.url_path_for("static", path=path)) == path
    with TestClient(recovery_static, base_url=BASE) as client:
        response = client.request(method, path)
        assert response.status_code == 200
        assert response.text == (body if method == "GET" else "")
        assert response.headers["cache-control"] == "no-store"


def test_recovery_static_preserves_readiness_auth(recovery_static: Starlette) -> None:
    with TestClient(recovery_static, base_url=BASE) as client:
        for path in ("/api/v2/capabilities", "/api/v2/diagnostics"):
            assert client.get(path).status_code == 401
            assert client.get(path, headers=BEARER).status_code == 200
        assert client.get("/api/v2/capabilities", headers=BEARER).json()["app"] == {
            "mode": "recovery"
        }
        assert client.get("/api/v2/diagnostics", headers=BEARER).json() == {"synthetic": True}
        checks = "/api/v2/diagnostics/checks"
        assert client.post(checks).status_code == 401
        assert client.post(checks, headers=BEARER).status_code == 403
        assert client.get("/", headers={"host": "evil.example:8765"}).status_code == 400
        assert client.get("/api/v2/settings", headers=BEARER).status_code == 404
