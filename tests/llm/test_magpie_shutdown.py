"""server: where the app's own start and stop reach the bundled Magpie."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from viki_agent import server
from viki_agent.catalog import Catalog
from viki_agent.config import Config
from viki_agent.drivers.claude.driver import DriverConfig
from viki_agent.session import SessionManager
from viki_agent.state import AppState, Prefs

BASE = "http://127.0.0.1:8765"


def build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, reap_fails: bool = False
) -> tuple[Any, list[str]]:
    events: list[str] = []

    class Magpie:
        def __init__(self, data_dir: Path, **_kwargs: Any) -> None:
            pass

        async def reap_leftover(self) -> None:
            events.append("reap")
            if reap_fails:
                raise OSError("synthetic detail")

        def stop(self) -> None:
            events.append("stop")

        async def close(self) -> None:
            events.append("close")

    monkeypatch.setattr(server, "Magpie", Magpie)
    vault = tmp_path / "vault"
    vault.mkdir()
    home = tmp_path / "home"
    (home / "sessions").mkdir(parents=True)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    manager = SessionManager(
        config=DriverConfig(cli=("fake",), vault=vault, env={}),
        state=AppState(data_dir / "state.json", Prefs(mode="auto", effort="low")),
        claude_home=home,
    )
    app = server.create_app(
        config=Config(
            vault=vault, claude_home=home, cli=Path("fake"), edge=None, data_dir=data_dir
        ),
        token="test-token",
        manager=manager,
        catalog=Catalog(home, vault),
        static_dir=None,
    )
    close_all = manager.close_all
    disarm_all = app.state.messaging_runtime.disarm_all

    async def chats_close() -> None:
        events.append("chats")
        await close_all()

    def first_cleanup_step() -> None:
        events.append("cleanup")
        disarm_all()

    monkeypatch.setattr(manager, "close_all", chats_close)
    monkeypatch.setattr(app.state.messaging_runtime, "disarm_all", first_cleanup_step)
    return app, events


def test_a_leftover_is_reaped_at_app_start_and_magpie_stops_before_any_other_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, events = build(tmp_path, monkeypatch)
    with TestClient(app, base_url=BASE):
        # Before any chat could use Magpie: the leftover holds its ports and its loaded keys.
        assert events == ["reap"]
        # The app's own Magpie is reachable from outside the lifespan, which runs only after
        # the server has drained its connections.
        app.state.magpie.stop()
        assert events == ["reap", "stop"]
        events.pop()
    # The kill comes first: a stop that is killed while the chats close has already ended
    # Magpie, and no closing chat can start it again.
    assert events[:3] == ["reap", "stop", "cleanup"]
    assert [event for event in events if event != "cleanup"] == ["reap", "stop", "chats", "close"]


def test_a_leftover_check_that_fails_does_not_stop_the_app_from_starting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    app, events = build(tmp_path, monkeypatch, reap_fails=True)
    with caplog.at_level("WARNING", logger="viki_agent.server"), TestClient(app, base_url=BASE):
        assert events == ["reap"]
    assert "magpie leftover check unavailable" in caplog.text
    assert "synthetic detail" not in caplog.text
    assert events[-1] == "close"
