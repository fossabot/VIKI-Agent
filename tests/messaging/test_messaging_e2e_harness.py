"""The scratch harness does not configure or construct remote messaging transports."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
from starlette.applications import Starlette

from viki_agent import __main__ as launcher
from viki_agent import server
from viki_agent.messaging import mattermost_config, messaging_config
from viki_agent.profiles.profile_capabilities import FIXTURE_VERSION

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))

import e2e_server as harness  # noqa: E402

TOKEN = "synthetic-harness-token"
BASE = "http://127.0.0.1:8765"
PROVIDERS = ("mattermost", "slack", "discord")


def test_synthetic_harness_has_no_remote_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    env = {
        "USERPROFILE": str(home),
        "HOME": str(home),
        "VIKI_TEST_TOKEN": TOKEN,
        "VIKI_MATTERMOST_URL": "https://example.test",
        "VIKI_MATTERMOST_BOT_TOKEN": "synthetic",
        "VIKI_MATTERMOST_OWNER_ID": "synthetic",
        "VIKI_SLACK_BOT_TOKEN": "xoxb-synthetic",
        "VIKI_SLACK_APP_TOKEN": "xapp-synthetic",
        "VIKI_SLACK_OWNER_ID": "U12345678",
        "VIKI_DISCORD_BOT_TOKEN": "synthetic",
        "VIKI_DISCORD_OWNER_ID": "123",
        "VIKI_DISCORD_CLIENT_URL": "https://example.test/client",
    }
    monkeypatch.setattr(harness.os, "environ", env)
    monkeypatch.setattr(harness, "SCRATCH", tmp_path / "viki-e2e")
    static = tmp_path / "static"
    static.mkdir()
    monkeypatch.setattr(harness, "STATIC_DIR", static)
    forbidden: list[str] = []

    def forbid(*_args: Any, **_kwargs: Any) -> Any:
        forbidden.append("remote transport, config loader or process")
        raise AssertionError("the synthetic harness must not reach remote messaging or processes")

    monkeypatch.setattr(harness.winproc, "spawn", forbid)
    monkeypatch.setattr(harness.winproc, "run", forbid)
    monkeypatch.setattr(messaging_config, "load_slack", forbid)
    monkeypatch.setattr(messaging_config, "load_discord", forbid)
    monkeypatch.setattr(mattermost_config, "load_mattermost", forbid)
    for name in ("MattermostHttp", "SlackChannel", "DiscordChannel"):
        monkeypatch.setattr(server, name, forbid)
    monkeypatch.setattr(harness, "_listen", lambda *_args: object())
    monkeypatch.setattr(harness.config, "_browser_for", lambda *_args: (None, ""))
    monkeypatch.setattr(harness, "cli_version", lambda _argv: FIXTURE_VERSION)
    monkeypatch.setattr(launcher, "cli_version", lambda _argv: FIXTURE_VERSION)
    real_load = harness.config.load
    real_app = server.create_app
    captured: dict[str, Any] = {}

    def load(clean: dict[str, str]) -> Any:
        captured["env"] = dict(clean)
        return real_load(clean)

    def app(**kwargs: Any) -> Starlette:
        captured["kwargs"] = kwargs
        return real_app(**kwargs)

    async def exercise(app: Starlette) -> None:
        expected = {
            provider: {
                "configured": False,
                "state": "not_configured",
                "restart_required": False,
                "reason": None,
            }
            for provider in PROVIDERS
        }
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE) as client,
        ):
            auth = {"Authorization": f"Bearer {TOKEN}"}
            write = {**auth, "Origin": BASE}
            assert (await client.get("/api/v2/messaging")).status_code == 401
            response = await client.get("/api/v2/messaging", headers=auth)
            assert response.status_code == 200 and response.json() == expected
            assert response.headers["cache-control"] == "no-store"
            for provider in PROVIDERS:
                response = await client.post(f"/api/v2/messaging/{provider}/enable", headers=write)
                assert response.status_code == 409
                assert response.json()["error"]["code"] == "not_configured"
                response = await client.post(f"/api/v2/messaging/{provider}/disable", headers=write)
                assert response.status_code == 200 and response.json() == expected
                assert app.state.messaging_runtime.bridge(provider) is None

    def serve(app: Starlette, _socket: object) -> int:
        asyncio.run(exercise(app))
        return 0

    monkeypatch.setattr(harness.config, "load", load)
    monkeypatch.setattr(harness.server, "create_app", app)
    monkeypatch.setattr(harness, "_serve", serve)
    assert (
        harness.main(["--scratch", "--fixture", "tests/fixtures/claude/text-two-turns.jsonl"]) == 0
    )
    assert not any(
        key.upper().startswith(("VIKI_MATTERMOST_", "VIKI_SLACK_", "VIKI_DISCORD_"))
        for key in captured["env"]
    )
    assert not {"mattermost", "slack_config", "discord_config"} & captured["kwargs"].keys()
    assert captured["kwargs"]["config"].claude_home == harness.SCRATCH / "home" / ".claude"
    assert captured["kwargs"]["config"].data_dir == harness.SCRATCH / "data"
    assert forbidden == []
