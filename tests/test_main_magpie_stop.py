"""`_serve_on` stops the bundled Magpie as the stop begins, not only in the lifespan cleanup that
runs after uvicorn has drained its connections: a quit that is forced during a long reply ends
the process before that drain does, and Magpie outlived it. The hook runs before the drain; on
macOS and Linux the end of serving stops Magpie again."""

from __future__ import annotations

import asyncio
import contextlib
import socket
import sys
import threading
from collections.abc import Callable, MutableMapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import uvicorn
from starlette.datastructures import State

from tests.test_main import (
    FakeServer,
    _cfg,
    _preserve_os_environ,  # noqa: F401 (autouse: restores os.environ main() scrubs)
    _private_instance_names,  # noqa: F401 (autouse: this launch's mutex/signal names are its own)
    _reaching_the_server,
    alerts,  # noqa: F401 (autouse: no real message box may show)
    nofile_raises,  # noqa: F401 (autouse: never raises this pytest process's fd limit for real)
)
from viki_agent import __main__ as launcher
from viki_agent import config

# A guard on a held response that normally ends within milliseconds of the stop; never a bound on
# work.
HANG_GUARD_S = 5.0

# `_reaching_the_server` replaces `uvicorn.Server` for the length of a test.
_UvicornServer = uvicorn.Server


class _HookedServer(_UvicornServer):
    """A real uvicorn server with `_GracefulServer`'s hook on every platform (the launcher's own
    exists only off Windows), that starts `client` once it listens."""

    def __init__(
        self, cfg: uvicorn.Config, *, before_exit: Callable[[], None], client: Callable[[], None]
    ) -> None:
        super().__init__(cfg)
        self._before_exit = before_exit
        self._client = client

    async def startup(self, sockets: list[socket.socket] | None = None) -> None:
        await super().startup(sockets=sockets)
        self._client()

    async def shutdown(self, sockets: list[socket.socket] | None = None) -> None:
        self._before_exit()
        await super().shutdown(sockets=sockets)


class _Magpie:
    """What `Magpie.stop` offers the hook: a synchronous, repeatable stop."""

    def __init__(self, events: list[str], *, fails: bool = False) -> None:
        self.events = events
        self.fails = fails
        self.calls = 0
        self.stopped = asyncio.Event()

    def stop(self) -> None:
        self.calls += 1
        if not self.stopped.is_set():
            self.events.append("magpie stop")
        self.stopped.set()
        if self.fails:
            raise RuntimeError("magpie would not stop")


class _App:
    """Answers one request, asks for the stop while its response is still open, and holds that
    response open until Magpie has been stopped (or the guard runs out)."""

    def __init__(
        self,
        state: object,
        servers: list[uvicorn.Server],
        events: list[str],
        magpie: _Magpie | None,
    ) -> None:
        self.state = state
        self._servers = servers
        self._events = events
        self._magpie = magpie

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                else:
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        await receive()
        await send(
            {"type": "http.response.start", "status": 200, "headers": [(b"content-length", b"2")]}
        )
        await send({"type": "http.response.body", "body": b"o", "more_body": True})
        self._servers[0].should_exit = True  # the stop, while the response is open
        if self._magpie is not None:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._magpie.stopped.wait(), HANG_GUARD_S)
        self._events.append("response end")
        await send({"type": "http.response.body", "body": b"k"})


def _drain(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    state: object,
    magpie: _Magpie | None,
    events: list[str],
) -> tuple[list[bytes], MutableMapping[str, Any]]:
    """Serve `_App` through `launcher.main` on a real uvicorn server and one real request."""
    _reaching_the_server(monkeypatch, _cfg(tmp_path))
    answers: list[bytes] = []
    created: MutableMapping[str, Any] = {}
    servers: list[uvicorn.Server] = []
    clients: list[threading.Thread] = []

    def request() -> None:
        with socket.create_connection((config.HOST, config.PORT), timeout=2 * HANG_GUARD_S) as s:
            s.sendall(b"GET / HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n")
            data = b""
            while chunk := s.recv(1024):
                data += chunk
        answers.append(data)

    def start_client() -> None:
        clients.append(threading.Thread(target=request, daemon=True))
        clients[0].start()

    def create_app(**kwargs: Any) -> _App:
        created.update(kwargs)
        return _App(state, servers, events, magpie)

    def server(cfg: uvicorn.Config, *, before_exit: Callable[[], None]) -> uvicorn.Server:
        servers.append(_HookedServer(cfg, before_exit=before_exit, client=start_client))
        return servers[0]

    monkeypatch.setattr(launcher.server, "create_app", create_app)
    monkeypatch.setattr(launcher, "_server", server)

    assert launcher.main([]) == 0
    clients[0].join(HANG_GUARD_S)
    return answers, created


def test_magpie_is_stopped_before_the_drain_of_a_held_response_ends(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    events: list[str] = []
    magpie = _Magpie(events)

    answers, _ = _drain(monkeypatch, tmp_path, SimpleNamespace(magpie=magpie), magpie, events)

    assert events == ["magpie stop", "response end"]  # the response ends once Magpie is stopped
    assert answers[0].endswith(b"ok")


def test_a_magpie_that_will_not_stop_does_not_stop_the_shutdown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    events: list[str] = []
    magpie = _Magpie(events, fails=True)

    answers, created = _drain(monkeypatch, tmp_path, SimpleNamespace(magpie=magpie), magpie, events)

    assert events == ["magpie stop", "response end"]
    assert answers[0].endswith(b"ok")
    assert created["windows"]._stop.is_set()  # the hook ran _stop_opening first


def test_an_app_without_magpie_still_shuts_down(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A stand-in for `create_app` without `state.magpie`, as the launcher tests use; the
    real app always has one (tests/llm/test_magpie_shutdown.py stops it through `state`)."""
    events: list[str] = []

    answers, created = _drain(monkeypatch, tmp_path, State(), None, events)

    assert events == ["response end"]
    assert answers[0].endswith(b"ok")
    assert created["windows"]._stop.is_set()


@pytest.mark.parametrize("fails", [False, True])
def test_the_recovery_shell_ends_a_leftover_magpie_before_it_serves(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture, fails: bool
) -> None:
    """A store that cannot be opened serves the recovery shell instead of the app, whose own
    start is where an ordinary launch ends a Magpie a killed launch left running. Without this
    that Magpie kept its ports and loaded keys for as long as the store stayed unreadable."""
    cfg = _cfg(tmp_path)
    _reaching_the_server(monkeypatch, cfg)
    cfg.data_dir.mkdir(parents=True)
    (cfg.data_dir / "app.sqlite3").write_bytes(b"damaged")
    events: list[object] = []

    class Leftover:
        def __init__(self, data_dir: Path) -> None:
            events.append(data_dir)

        async def reap_leftover(self) -> None:
            events.append("reap")
            if fails:
                raise OSError("synthetic detail")

    class Server(FakeServer):
        def run(self, sockets: list[socket.socket] | None = None) -> None:
            events.append("serve")
            super().run(sockets)

    monkeypatch.setattr(launcher, "Magpie", Leftover)
    monkeypatch.setattr(
        launcher, "_server", lambda cfg, *, before_exit: Server(cfg, before_exit=before_exit)
    )

    with caplog.at_level("WARNING"):
        assert launcher.main([]) == 0

    assert events == [cfg.data_dir, "reap", "serve"]
    assert ("magpie leftover check unavailable" in caplog.text) is fails
    assert "synthetic detail" not in caplog.text


@pytest.mark.skipif(sys.platform == "win32", reason="a Windows Job ends Magpie with VIKI")
def test_serving_that_ends_without_the_hook_still_stops_magpie(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """FakeServer.run returns at once and never runs the hook: only the end of serving can."""
    assert sys.platform != "win32"
    _reaching_the_server(monkeypatch, _cfg(tmp_path))
    magpie = _Magpie([])
    monkeypatch.setattr(
        launcher.server,
        "create_app",
        lambda **kwargs: SimpleNamespace(state=SimpleNamespace(magpie=magpie)),
    )

    assert launcher.main([]) == 0

    assert magpie.calls == 1
