"""The tool routes create_app mounts: the live inventory and the native asset inspector, the MCP
config sweep before serving, and the resolver an ordinary chat never calls."""

from __future__ import annotations

import dataclasses
import logging
import os
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from tests.profiles.test_profile_launch_api import AUTH, HANG_GUARD_S, TOKEN, Rig, authed, ident
from tests.test_server import BEARER as SERVER_BEARER
from tests.test_server import TOKEN as SERVER_TOKEN
from tests.test_server import EchoDriver, _mutate_v2, _open_v2, running
from viki_agent.conversations.limits_api import sanitize_reading
from viki_agent.drivers.claude.driver import DriverConfig
from viki_agent.profiles.mcp_config import write_config
from viki_agent.profiles.profile_launch import ProfileLaunchService
from viki_agent.session import SessionManager
from viki_agent.state import AppState, Prefs

BASE = "http://127.0.0.1:8765"


def listing(root: Path) -> list[tuple[str, int, int]]:
    """Every entry under `root`: its relative path, size and mtime."""
    found = []
    for folder, dirs, files in os.walk(root):
        for name in dirs + files:
            path = Path(folder) / name
            info = path.stat()
            found.append((path.relative_to(root).as_posix(), info.st_size, info.st_mtime_ns))
    return sorted(found)


def test_inventory_route_gates_before_any_actor(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    path = f"/api/v2/sessions/{ident()}/inventory"
    with running(tmp_path) as app:
        client = app.client
        answers = [
            client.get(path),
            client.get(path, headers={**SERVER_BEARER, "origin": "http://evil.example"}),
            client.get(path, headers={**SERVER_BEARER, "host": "evil.example"}),
            client.get(path, headers=SERVER_BEARER),
        ]
    assert [answer.status_code for answer in answers] == [401, 403, 400, 404]
    assert [a.headers["cache-control"] for a in answers if a.status_code != 400] == ["no-store"] * 3
    assert SERVER_TOKEN not in caplog.text and str(tmp_path) not in caplog.text


def test_context_and_limits_routes_gate_before_any_actor(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    context_path = f"/api/v2/sessions/{ident()}/context"
    limits_path = "/api/v2/limits"
    with running(tmp_path) as app:
        client = app.client
        context_answers = [
            client.get(context_path),
            client.get(context_path, headers={**SERVER_BEARER, "origin": "http://evil.example"}),
            client.get(context_path, headers={**SERVER_BEARER, "host": "evil.example"}),
            client.get(context_path, headers=SERVER_BEARER),
        ]
        limits_answers = [
            client.get(limits_path),
            client.get(limits_path, headers={**SERVER_BEARER, "origin": "http://evil.example"}),
            client.get(limits_path, headers={**SERVER_BEARER, "host": "evil.example"}),
            client.get(limits_path, headers=SERVER_BEARER),
        ]
    assert [a.status_code for a in context_answers] == [401, 403, 400, 404]
    assert [a.headers["cache-control"] for a in context_answers if a.status_code != 400] == [
        "no-store"
    ] * 3
    assert [a.status_code for a in limits_answers] == [401, 403, 400, 200]
    assert limits_answers[-1].json() == {"observation": None}
    assert [a.headers["cache-control"] for a in limits_answers if a.status_code != 400] == [
        "no-store"
    ] * 3
    assert SERVER_TOKEN not in caplog.text and str(tmp_path) not in caplog.text


def test_the_bound_observer_feeds_the_limits_route(tmp_path: Path) -> None:
    state = AppState(tmp_path / "data" / "state.json", Prefs(mode="auto", effort="low"))
    manager = SessionManager(
        config=DriverConfig(cli=("fake",), vault=tmp_path / "vault", env={}),
        state=state,
        claude_home=tmp_path / "home",
        make_driver=lambda launch, prefs, emit: EchoDriver(launch, emit),
    )
    reading = {
        "observed_at": "2026-10-09T12:00:00.000Z",
        "status": "allowed_warning",
        "limiting": "five_hour",
        "utilization": 2.5,
        "resets_at": 1_760_000_000,
        "windows": {
            "five_hour": {"utilization": 2.5, "resets_at": 1_760_000_000},
            "seven_day": {"utilization": 1.0, "resets_at": 1_760_500_000},
        },
    }
    with running(tmp_path, manager=manager) as app:
        observe = manager._config.observe_limits
        assert observe is not None
        observe(reading)
        response = app.client.get("/api/v2/limits", headers=SERVER_BEARER)
    assert response.status_code == 200
    assert response.json() == {"observation": sanitize_reading(reading)}


def test_native_route_is_not_mounted_without_workspaces(tmp_path: Path) -> None:
    with running(tmp_path) as app:
        assert app.client.get("/api/v2/native-assets", headers=SERVER_BEARER).status_code == 404


def test_native_route_lists_both_sources_and_reads_the_home_without_writing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    rig = Rig(tmp_path)
    (rig.home / "agents").mkdir()
    (rig.home / "agents" / "x.md").write_text(
        "---\nname: X\ndescription: Does work\n---\nbody\n", encoding="utf-8"
    )
    before = listing(rig.home)
    with TestClient(rig.app(rig.manager()), base_url=BASE) as client:
        sources = client.get("/api/v2/native-assets", headers=AUTH)
        user = client.get("/api/v2/native-assets?source_id=claude:user", headers=AUTH)
    assert sources.status_code == 200
    assert sources.json()["sources"] == [
        {"source_id": "claude:user", "provider": "claude", "scope": "user"},
        {
            "source_id": f"claude:workspace:{rig.workspace_id}",
            "provider": "claude",
            "scope": "workspace",
        },
    ]
    assert user.status_code == 200
    assert [asset["relative_id"] for asset in user.json()["assets"]] == ["agents/x.md"]
    assert listing(rig.home) == before
    assert TOKEN not in caplog.text and str(tmp_path) not in caplog.text


# A workspace source appears only for a persistent vault that is not chat-only.
@pytest.mark.parametrize("case", ["chat_only", "not_persistent", "scratch_kind"])
def test_native_route_hides_the_workspace_source_when_not_a_persistent_vault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    rig = Rig(tmp_path)
    active = rig.workspaces.resolve_active()
    if case == "chat_only":
        changed = dataclasses.replace(active, chat_only=True)
    elif case == "not_persistent":
        changed = dataclasses.replace(
            active, workspace=dataclasses.replace(active.workspace, persistent=False)
        )
    else:
        changed = dataclasses.replace(
            active, workspace=dataclasses.replace(active.workspace, kind="scratch")
        )
    monkeypatch.setattr(rig.workspaces, "resolve_active", lambda: changed)
    with TestClient(rig.app(rig.manager()), base_url=BASE) as client:
        sources = client.get("/api/v2/native-assets", headers=AUTH)
    assert sources.status_code == 200
    assert sources.json()["sources"] == [
        {"source_id": "claude:user", "provider": "claude", "scope": "user"}
    ]


def test_native_route_drops_the_user_source_when_the_config_dir_is_set(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.config = dataclasses.replace(rig.config, claude_config_dir_set=True)
    with TestClient(rig.app(rig.manager()), base_url=BASE) as client:
        sources = client.get("/api/v2/native-assets", headers=AUTH).json()["sources"]
        user = client.get("/api/v2/native-assets?source_id=claude:user", headers=AUTH)
    assert [source["scope"] for source in sources] == ["workspace"]
    assert user.status_code == 409


def test_startup_sweeps_generated_configs_and_keeps_other_files(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    rig = Rig(tmp_path)
    generated = write_config(rig.data / "mcp", b'{"mcpServers":{}}')
    note = rig.data / "mcp" / "note.txt"
    note.write_text("kept\n", encoding="utf-8")
    with TestClient(rig.app(rig.manager()), base_url=BASE):
        assert not generated.exists()
    assert note.exists()
    assert str(tmp_path) not in caplog.text


def test_an_ordinary_chat_never_resolves_mcp_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        ProfileLaunchService, "mcp_config_path", lambda _self, sid: calls.append(sid)
    )
    rig, session_id = Rig(tmp_path), ident()
    with TestClient(rig.app(rig.manager()), base_url=BASE) as client, authed(client) as ws:
        _snapshot, target = _open_v2(ws, session_id)
        _mutate_v2(ws, target, "turn.send", text="hi")
        assert rig.driver_created.wait(HANG_GUARD_S) and rig.drivers[0].sent.wait(HANG_GUARD_S)
    assert calls == []
    assert rig.drivers[0].launch.mcp_config is None
