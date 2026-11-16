"""Synthetic launch URLs bypass previously cached production entry documents."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, Mock
from urllib.parse import parse_qs, urlsplit

import pytest

from viki_agent import __main__ as launcher
from viki_agent.config import Config

SESSION_ID = "a0000000-0000-4000-8000-000000000001"
TOKEN = "synthetic-token"


@pytest.mark.parametrize("platform", ["win32", "linux", "darwin"])
@pytest.mark.parametrize("dev", [False, True])
@pytest.mark.parametrize("session_id", [None, SESSION_ID])
async def test_runtime_window_urls_refresh_production_only_and_keep_identity_in_fragment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    dev: bool,
    session_id: str | None,
) -> None:
    edge = tmp_path / "browser"
    cfg = Config(
        vault=None,
        claude_home=tmp_path / "synthetic-provider",
        cli=None,
        edge=edge,
        data_dir=tmp_path / "data",
    )
    spawn = AsyncMock(
        side_effect=[Mock(wait_tree=AsyncMock(side_effect=asyncio.Event().wait)) for _ in range(2)]
    )
    public_ids = [uuid.UUID(int=1), uuid.UUID(int=2)]
    new_id = Mock(side_effect=public_ids)
    targets: list[str] = []

    def redirect(run_dir: Path, target: str) -> str:
        assert run_dir == cfg.data_dir / "run"
        targets.append(target)
        return f"file:///synthetic-private/open-{len(targets)}.html"

    monkeypatch.setattr(launcher.sys, "platform", platform)
    monkeypatch.setattr(launcher.winproc, "spawn", spawn)
    monkeypatch.setattr(launcher.uuid, "uuid4", new_id)
    monkeypatch.setattr(launcher, "_redirect_url", redirect)
    manager = launcher._windows(
        cfg, edge, TOKEN, dev=dev, env={}, signal=Mock(), on_last_closed=AsyncMock()
    )
    try:
        assert await manager.open(session_id)
        assert await manager.open(session_id)
        argvs = [call.args[0] for call in spawn.await_args_list]
        if platform == "win32":
            assert targets == []
            targets = [argv[-1].removeprefix("--app=") for argv in argvs]
        else:
            assert [argv[-1] for argv in argvs] == [
                "--app=file:///synthetic-private/open-1.html",
                "--app=file:///synthetic-private/open-2.html",
            ]
            assert not any(TOKEN in arg or SESSION_ID in arg for argv in argvs for arg in argv)

        fragment = f"token={TOKEN}" + (f"&session={session_id}" if session_id else "")
        for index, target in enumerate(targets):
            parsed = urlsplit(target)
            assert parsed.scheme == "http"
            assert parsed.netloc == ("127.0.0.1:5173" if dev else "127.0.0.1:8765")
            assert parsed.path == "/" and parsed.fragment == fragment
            assert parse_qs(parsed.query, keep_blank_values=True) == (
                {} if dev else {"ui": [public_ids[index].hex]}
            )
        assert new_id.call_count == (0 if dev else 2)
        assert (targets[0] == targets[1]) is dev
        assert all(call.kwargs["pipes"] is False for call in spawn.await_args_list)
    finally:
        await manager.stop()


@pytest.mark.parametrize("dev", [False, True])
def test_legacy_url_helper_remains_exact_and_validates_session_targets(dev: bool) -> None:
    base = "http://127.0.0.1:5173" if dev else "http://127.0.0.1:8765"
    assert launcher.app_url(TOKEN, dev=dev) == f"{base}/#token={TOKEN}"
    assert launcher.app_url(TOKEN, dev=dev, session_id=SESSION_ID) == (
        f"{base}/#token={TOKEN}&session={SESSION_ID}"
    )
    with pytest.raises(ValueError):
        launcher.app_url(TOKEN, dev=dev, session_id="x&token=y")
