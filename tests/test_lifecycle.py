"""The app lifecycle end to end, in one process: the real app under a real uvicorn.Server on
config.PORT, the real WindowManager over winproc.spawn with a Python stub as the "browser",
and the real open-window signal. Skipped when something else holds the port (the user's own
VIKI Agent, say)."""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
import uvicorn

from viki_agent import __main__ as launcher
from viki_agent import config, server, winproc
from viki_agent.catalog import Catalog
from viki_agent.drivers.claude.driver import DriverConfig
from viki_agent.session import SessionManager
from viki_agent.state import AppState, Prefs
from viki_agent.window import WindowManager

# A window the way Edge is one: the launched process hands off to a child and exits at once,
# so only the Job's process count says when the window is gone. The child lives as long as
# its sentinel file exists; the test ends a window by deleting the file. Popen takes creation
# flags only on Windows (CREATE_NO_WINDOW there).
STUB = (
    "import subprocess, sys;"
    "subprocess.Popen([sys.executable, '-c', 'import os, sys, time\\n"
    "while os.path.exists(sys.argv[1]): time.sleep(0.05)', sys.argv[1]],"
    f" creationflags={0x08000000 if sys.platform == 'win32' else 0})"
)


async def _until(flag: Callable[[], bool], timeout_s: float) -> None:
    async with asyncio.timeout(timeout_s):
        while not flag():
            await asyncio.sleep(0.01)


async def test_windows_open_the_app_serves_and_the_last_exit_stops_it(tmp_path: Path) -> None:
    if not launcher._port_free(config.HOST, config.PORT):
        pytest.skip(f"port {config.PORT} is in use (is VIKI Agent running?)")
    vault, home = tmp_path / "vault", tmp_path / "home"
    vault.mkdir()
    (home / "sessions").mkdir(parents=True)
    cfg = config.Config(
        vault=vault, claude_home=home, cli=Path("fake"), edge=None, data_dir=tmp_path / "data"
    )
    manager = SessionManager(
        config=DriverConfig(cli=("fake",), vault=vault, env={}),
        state=AppState(cfg.state_file, Prefs(mode="auto", effort="low")),
        claude_home=home,
    )
    name = f"Local\\viki-test-{uuid.uuid4().hex}"
    signal = winproc.Signal.create(name)
    sentinels = [tmp_path / "window-1", tmp_path / "window-2"]
    for sentinel in sentinels:
        sentinel.touch()
    remaining = iter(sentinels)
    uvi: uvicorn.Server | None = None
    last_closed: list[int] = []

    async def last_window_closed() -> None:
        last_closed.append(windows.count)
        await manager.settle(0.05)
        if windows.count:
            return
        await manager.close_all()
        if uvi is not None:
            uvi.should_exit = True

    windows = WindowManager(
        spawn=winproc.spawn,
        argv=lambda: [sys.executable, "-c", STUB, str(next(remaining))],
        cwd=tmp_path,
        env=os.environ,
        on_last_closed=last_window_closed,
        signal=signal,
        poll_ms=20,
    )
    app = server.create_app(
        config=cfg,
        token="t",
        manager=manager,
        catalog=Catalog(home, vault),
        static_dir=None,
        windows=windows,
    )
    uvi = uvicorn.Server(
        uvicorn.Config(app, host=config.HOST, port=config.PORT, log_config=None, access_log=False)
    )
    serving = asyncio.create_task(uvi.serve())
    try:
        await _until(lambda: uvi.started or serving.done(), 10)
        assert not serving.done()
        await asyncio.sleep(0.3)  # the launched stub has handed off and exited by now
        assert windows.count == 1  # ... and the window still counts: the Job is its lifetime
        async with httpx.AsyncClient() as http:
            response = await http.get(f"http://{config.HOST}:{config.PORT}/api/sessions")
        assert response.status_code == 401  # served, and the token is required
        assert response.headers["x-content-type-options"] == "nosniff"

        other = winproc.Signal.open(name)  # a second launch asks for one more window
        assert other is not None
        other.set()
        other.close()
        await _until(lambda: windows.count == 2, 5)

        sentinels[1].unlink()  # the second window closes: the app serves on
        await _until(lambda: windows.count == 1, 5)
        await asyncio.sleep(0.2)
        assert uvi.should_exit is False and last_closed == []

        sentinels[0].unlink()  # the last window closes: sessions closed, server off
        await asyncio.wait_for(serving, 10)
        assert last_closed == [0]
        assert windows.count == 0 and uvi.should_exit is True
    finally:
        for sentinel in sentinels:
            sentinel.unlink(missing_ok=True)
        if not serving.done():
            uvi.should_exit = True
            await asyncio.wait_for(serving, 10)
        signal.close()
