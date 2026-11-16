"""Synthetic launcher/token contracts; native window reuse is not established here."""

from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

import pytest
from starlette.websockets import WebSocketDisconnect

from tests.test_server import TOKEN, WS_HEADERS, recv, running
from tests.test_window import Spawner
from viki_agent import __main__ as launcher
from viki_agent import config, server


@pytest.mark.parametrize("platform", ["win32", "darwin", "linux"])
async def test_every_window_uses_its_launch_token_delivery_contract(
    tmp_path, monkeypatch, platform
):
    cfg = config.Config(
        vault=tmp_path / "vault",
        claude_home=tmp_path / "home",
        cli=None,
        edge=Path("fixture-browser"),
        data_dir=tmp_path / "data",
    )
    spawn = Spawner()
    argv = []
    redirects = []

    async def record_spawn(arguments, **kwargs):
        argv.append(arguments)
        return await spawn(arguments, **kwargs)

    def redirect(directory, target):
        redirects.append((directory, target))
        return f"file:///fixture-open-{len(redirects)}.html"

    monkeypatch.setattr(launcher, "sys", SimpleNamespace(platform=platform))
    monkeypatch.setattr(launcher.winproc, "spawn", record_spawn)
    monkeypatch.setattr(launcher, "_redirect_url", redirect)
    manager = launcher._windows(
        cfg,
        Path("fixture-browser"),
        "fixture-current-launch",
        dev=False,
        env={},
        signal=Mock(),
        on_last_closed=Mock(),
    )
    try:
        assert await manager.open() and await manager.open()
        assert manager.count == 2
        for arguments in argv:
            assert f"--user-data-dir={cfg.edge_profile}" in arguments
            if platform == "win32":
                assert arguments[-1].startswith("--app=")
            else:
                assert "fixture-current-launch" not in " ".join(arguments)
                assert arguments[-1].startswith("--app=file:///fixture-open-")
        if platform == "win32":
            assert redirects == []
            targets = [arguments[-1].removeprefix("--app=") for arguments in argv]
        else:
            assert [directory for directory, _ in redirects] == [cfg.data_dir / "run"] * 2
            targets = [target for _, target in redirects]
        ui_values = []
        for target in targets:
            parsed = urlsplit(target)
            assert (parsed.scheme, parsed.netloc, parsed.path) == ("http", "127.0.0.1:8765", "/")
            assert parsed.fragment == "token=fixture-current-launch"
            query = parse_qs(parsed.query, keep_blank_values=True)
            assert set(query) == {"ui"} and len(query["ui"]) == 1
            ui_value = query["ui"][0]
            assert uuid.UUID(ui_value).hex == ui_value
            assert "fixture-current-launch" not in parsed.query
            ui_values.append(ui_value)
        assert len(set(ui_values)) == 2
    finally:
        await manager.stop()
    assert all(child.killed and child.released for child in spawn.windows)


def test_previous_launch_token_is_refused_by_rest_and_ws_without_provider_launch(tmp_path):
    launched = []

    def forbid_driver(*args):
        launched.append(args)
        raise AssertionError("authentication must not start a provider")

    with running(tmp_path, make_driver=forbid_driver) as app:
        assert (
            app.client.get(
                "/api/sessions", headers={"authorization": "Bearer fixture-old-launch"}
            ).status_code
            == 401
        )
        assert (
            app.client.get(
                "/api/sessions", headers={"authorization": f"Bearer {TOKEN}"}
            ).status_code
            == 200
        )
        with app.client.websocket_connect("/ws", headers=WS_HEADERS) as ws:
            ws.send_json({"type": "auth", "token": "fixture-old-launch", "v": 2})
            with pytest.raises(WebSocketDisconnect) as refused:
                recv(ws, timeout=0.05)
            assert refused.value.code == server.UNAUTHORIZED
    assert launched == []
