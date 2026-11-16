"""server: the hourly asset-collection loop's own wiring — the launch scope's own pass runs
in-process, then `collect_scopes` walks the other scopes on an injectable schedule."""

from __future__ import annotations

import queue
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from viki_agent import server
from viki_agent.catalog import Catalog
from viki_agent.config import Config
from viki_agent.conversations.drafts import draft_scope
from viki_agent.drivers.claude.driver import DriverConfig
from viki_agent.session import SessionManager
from viki_agent.state import AppState, Prefs

HANG_GUARD_S = 5
TOKEN = "test-token"
BASE = "http://127.0.0.1:8765"


def _build(tmp_path: Path, *, asset_poll_s: float) -> tuple[Any, SessionManager, Path, Path]:
    vault = tmp_path / "vault"
    vault.mkdir()
    home = tmp_path / "home"
    (home / "sessions").mkdir(parents=True)
    data_dir = tmp_path / "data"
    (data_dir / "assets").mkdir(parents=True)
    state = AppState(data_dir / "state.json", Prefs(mode="auto", effort="low"))
    manager = SessionManager(
        config=DriverConfig(cli=("fake",), vault=vault, env={}),
        state=state,
        claude_home=home,
    )
    config = Config(vault=vault, claude_home=home, cli=Path("fake"), edge=None, data_dir=data_dir)
    app = server.create_app(
        config=config,
        token=TOKEN,
        manager=manager,
        catalog=Catalog(home, vault),
        static_dir=None,
        auth_timeout_s=0.05,
        asset_poll_s=asset_poll_s,
    )
    return app, manager, data_dir, vault


def test_the_loop_collects_the_launch_scope_then_other_scopes_on_the_returned_cursor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: queue.Queue[dict[str, Any]] = queue.Queue()

    async def launch_pass(service: server.AssetService, **_kwargs: Any) -> None:
        seen.put({"call": "collect_due", "scope": service._scope})

    async def recorder(root: Path, store: Any, services: Any, cursor: str, *, skip: str) -> str:
        seen.put(
            {"call": "collect_scopes", "root": root, "store": store, "cursor": cursor, "skip": skip}
        )
        return "next-cursor" if cursor == "" else "second-pass-cursor"

    monkeypatch.setattr(server.AssetService, "collect_due", launch_pass)
    monkeypatch.setattr(server, "collect_scopes", recorder)
    app, manager, data_dir, vault = _build(tmp_path, asset_poll_s=0.01)
    launch_scope = draft_scope(vault)

    with TestClient(app, base_url=BASE):
        # The launch scope's own pass comes first; `collect_scopes` then skips that scope.
        assert seen.get(timeout=HANG_GUARD_S) == {"call": "collect_due", "scope": launch_scope}
        first = seen.get(timeout=HANG_GUARD_S)
        assert first["call"] == "collect_scopes"
        assert first["root"] == data_dir / "assets"
        assert first["store"] is manager.app_state.store
        assert (first["cursor"], first["skip"]) == ("", launch_scope)

        assert seen.get(timeout=HANG_GUARD_S)["call"] == "collect_due"
        second = seen.get(timeout=HANG_GUARD_S)
        assert second["cursor"] == "next-cursor"
