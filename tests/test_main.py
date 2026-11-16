"""__main__: main()'s orchestration is not pinned by the frozen acceptance tests (only the
helpers are), so this file covers it directly — a ConfigError or a billing problem returns 2
without constructing or starting anything else; the happy path wires create_app and a uvicorn
Server together; --dev toggles static_dir/dev; the window manager reaches the app and is what
opens Edge; the last window closing closes the sessions with a grace and stops the server, or
first holds the app while a chat is retained, serving on when a window is back; a second launch
wakes the first and exits; the single-instance lock is always released."""

from __future__ import annotations

import asyncio
import ctypes
import html
import logging
import logging.handlers
import os
import socket
import stat
import sys
import threading
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

import pytest

from tests.test_protection import _acl_snapshot, _set_acl
from viki_agent import __main__ as launcher
from viki_agent import compiled, config, protection, server, winproc
from viki_agent.app.data_api import ResetService
from viki_agent.app.exports import ExportError
from viki_agent.app.settings import SettingsError
from viki_agent.conversations.drafts import DraftService
from viki_agent.domain import StorageReadiness
from viki_agent.state import AppState, Prefs
from viki_agent.storage import StateError
from viki_agent.window import WindowManager

# Storage state does not affect the provider or app fields these tests read out of a
# capabilities document; Readiness exposes its observations no other way.
_READY_STORAGE = StorageReadiness(state="ready", reason=None, private=True)

# The autouse `alerts` fixture below replaces launcher._alert for every test in this module;
# capture the real function here, at import time, before that happens, for the one test that
# exercises it directly.
_real_alert = launcher._alert


@pytest.fixture(autouse=True)
def _preserve_os_environ() -> Iterator[None]:
    """main() scrubs os.environ in place, so every test in this file that calls it for
    real mutates the actual process environment. This session runs inside Claude Code, so
    os.environ carries real CLAUDECODE/CLAUDE_CODE_*/ANTHROPIC_* vars beyond whatever a given
    test sets itself; restore the snapshot after every test regardless of what main() deleted."""
    snapshot = dict(os.environ)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(snapshot)


@pytest.fixture(autouse=True)
def _private_instance_names(monkeypatch: pytest.MonkeyPatch) -> None:
    """main() takes the real single-instance mutex. A test must never contend with, or wake, a
    VIKI Agent the user has running, so every test gets names of its own."""
    tag = uuid.uuid4().hex
    monkeypatch.setattr(launcher, "MUTEX_NAME", f"Local\\viki-test-{tag}")
    monkeypatch.setattr(launcher, "OPEN_EVENT", f"Local\\viki-test-{tag}-open")
    monkeypatch.setattr(launcher, "OPENED_EVENT", f"Local\\viki-test-{tag}-opened", raising=False)
    monkeypatch.setattr(launcher, "WAKE_MUTEX", f"Local\\viki-test-{tag}-caller", raising=False)
    monkeypatch.setattr(launcher, "WAKE_WAIT_S", 0.05)


@pytest.fixture(autouse=True)
def alerts(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """No modal box may pop up under pytest; the texts main() would have shown."""
    shown: list[str] = []
    monkeypatch.setattr(launcher, "_alert", shown.append)
    return shown


@pytest.fixture(autouse=True)
def nofile_raises(monkeypatch: pytest.MonkeyPatch) -> list[None]:
    """The real call would raise this pytest process's own descriptor limit for the rest of the
    session, so later modules would run under a different limit than earlier ones."""
    calls: list[None] = []
    monkeypatch.setattr(launcher, "_raise_nofile_limit", lambda: calls.append(None))
    return calls


class FakeServer:
    """uvicorn.Server without the serving: keeps its Config, whether run() was called, and the
    already-bound listening sockets it was handed."""

    instances: list[FakeServer] = []

    def __init__(self, config: Any, *, before_exit: Any = None) -> None:
        self.config = config
        self.before_exit = before_exit  # _GracefulServer's hook, on macOS and Linux
        self.should_exit = False
        self.ran = False
        self.sockets: list[socket.socket] | None = None
        self.bound: list[Any] = []
        FakeServer.instances.append(self)

    def run(self, sockets: list[socket.socket] | None = None) -> None:
        self.ran = True
        self.sockets = sockets
        # Read here, not from the test: _serve closes the socket on its way out, and getsockname
        # on a closed socket raises rather than reporting what it was bound to.
        self.bound = [sock.getsockname() for sock in sockets or []]


class CrashingServer(FakeServer):
    def run(self, sockets: list[socket.socket] | None = None) -> None:
        raise RuntimeError("the server crashed")


class FakeChild:
    def __init__(self) -> None:
        self.exited = asyncio.Event()
        self.killed = False
        self.released = False

    async def wait_tree(self) -> None:
        await self.exited.wait()

    def kill_tree(self) -> None:
        self.killed = True

    def release(self) -> None:
        self.released = True


class FakeUser32:
    """Spy standing in for user32: the one Windows call `_alert` makes to speak at all."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, str, str, int]] = []

    def MessageBoxW(self, hwnd: Any, text: str, caption: str, flags: int) -> int:
        self.calls.append((hwnd, text, caption, flags))
        return 1


def _cfg(tmp_path: Path, *, edge: Path | None | bool = True) -> config.Config:
    """edge=True (default): an Edge path exists (never spawned outside the app's lifespan)."""
    return config.Config(
        vault=tmp_path / "vault",
        claude_home=tmp_path / "home",
        cli=tmp_path / "no-such-claude.exe",  # never spawned: cli_version() just returns ""
        edge=tmp_path / "msedge.exe" if edge is True else (edge or None),
        data_dir=tmp_path / "data",
    )


def _spare_port() -> int:
    """A port nothing holds. _serve binds config.PORT for real and keeps the socket, so a
    test that reaches the server must not be pointed at 8765: it would either fail whenever the
    user's own VIKI Agent is up, or take the port away from it for the length of the test."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((config.HOST, 0))
        return int(probe.getsockname()[1])


def _forbid(name: str) -> Any:
    def raiser(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError(f"{name} must not be called")

    return raiser


@contextmanager
def _fresh_root_handlers() -> Any:
    """The real root logger is a process-wide singleton; a test that lets _configure_logging
    run for real must remove (and close, releasing the Windows file lock) whatever handler it
    added, or it leaks into every later test in the session."""
    root = logging.getLogger()
    before = list(root.handlers)
    try:
        yield
    finally:
        for handler in list(root.handlers):
            if handler not in before:
                root.removeHandler(handler)
                handler.close()


@pytest.mark.skipif(sys.platform != "win32", reason="MessageBoxW is Windows-only")
def test_alert_titles_the_message_box_viki_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    """_alert's only voice on Windows is a MessageBoxW; its title must read the product's
    display name, "VIKI Agent", never the bare assistant name. This calls the real function
    captured at import time (`_real_alert`), bypassing the autouse `alerts` fixture that stands
    in for every other test in this module, and never lets a real box reach the screen."""
    user32 = FakeUser32()
    monkeypatch.setattr(ctypes, "WinDLL", lambda name: user32)

    _real_alert("boom")

    assert len(user32.calls) == 1
    _, text, title, _ = user32.calls[0]
    assert text == "boom"
    assert title == "VIKI Agent"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows ACLs")
def test_logging_creates_private_new_app_data_before_state_initialization(tmp_path: Path) -> None:
    assert sys.platform == "win32"
    sid = protection._current_user_sid()
    _set_acl(tmp_path, f"O:{sid}D:P(A;OICI;FA;;;{sid})(A;OICI;GR;;;WD)")
    original = _acl_snapshot(tmp_path)
    data = tmp_path / "app-data"
    with _fresh_root_handlers():
        launcher._configure_logging(data / "logs")
        state = AppState(data / "state.json", Prefs())
        assert DraftService(state, tmp_path / "vault").get("new").revision == 0
    protection.require_private_directory(data)
    protection.require_private_file(state.database_path)
    assert _acl_snapshot(tmp_path) == original


@pytest.mark.skipif(sys.platform != "win32", reason="Windows ACLs")
def test_logging_does_not_change_existing_app_directory_permissions(tmp_path: Path) -> None:
    assert sys.platform == "win32"
    sid = protection._current_user_sid()
    _set_acl(tmp_path, f"O:{sid}D:P(A;OICI;FA;;;{sid})(A;OICI;GR;;;WD)")
    original = _acl_snapshot(tmp_path)
    with _fresh_root_handlers():
        launcher._configure_logging(tmp_path / "logs")
    assert _acl_snapshot(tmp_path) == original
    with pytest.raises(protection.ProtectionError):
        protection.require_private_directory(tmp_path)


def _forbid_serving(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(launcher.server, "create_app", _forbid("server.create_app"))
    monkeypatch.setattr(launcher.winproc, "spawn", _forbid("winproc.spawn"))
    monkeypatch.setattr(launcher.uvicorn, "Server", _forbid("uvicorn.Server"))
    _patch_posix_server(monkeypatch, _forbid("_GracefulServer"))


def _patch_posix_server(monkeypatch: pytest.MonkeyPatch, replacement: Any) -> None:
    """On macOS and Linux the launcher builds its own uvicorn.Server subclass, which a patch of
    uvicorn.Server cannot reach."""
    if sys.platform != "win32":
        monkeypatch.setattr(launcher, "_GracefulServer", replacement)


def _fake_bundle(monkeypatch: pytest.MonkeyPatch, cfg: config.Config) -> Path:
    """A built frontend/dist for the launch to find. _serve_on refuses to start without one, and
    the real one is gitignored: a checkout that has never run `npm run build` must not turn every
    test in this file into a MSG_NO_BUNDLE refusal. cfg.data_dir.parent is the test's tmp_path."""
    bundle = cfg.data_dir.parent / "dist"
    bundle.mkdir(parents=True, exist_ok=True)
    (bundle / "index.html").write_text("<!doctype html>", encoding="utf-8")
    monkeypatch.setattr(launcher, "STATIC_DIR", bundle)
    return bundle


def _reaching_the_server(
    monkeypatch: pytest.MonkeyPatch, cfg: config.Config, *, server_cls: type = FakeServer
) -> dict[str, Any]:
    """Patch config, logging, billing, create_app and uvicorn so main() reaches Server.run();
    the returned dict records create_app's keyword arguments."""
    calls: dict[str, Any] = {}
    monkeypatch.setattr(launcher.config, "load", lambda env: cfg)
    monkeypatch.setattr(launcher, "_configure_logging", lambda log_dir: None)
    monkeypatch.setattr(launcher.config, "billing_problems", lambda c, env: [])
    monkeypatch.setattr(launcher.config, "PORT", _spare_port())  # the user's app may run
    monkeypatch.setattr(launcher, "git_version", lambda: "")  # no real git process
    _fake_bundle(monkeypatch, cfg)
    monkeypatch.setattr(
        launcher.server,
        "create_app",
        lambda **kw: calls.__setitem__("create_app", kw) or "the-app",
    )
    monkeypatch.setattr(
        launcher.server,
        "create_recovery_app",
        lambda **kw: calls.__setitem__("create_recovery_app", kw) or "the-recovery-app",
    )
    monkeypatch.setattr(launcher.uvicorn, "Server", server_cls)
    _patch_posix_server(monkeypatch, server_cls)
    FakeServer.instances.clear()
    return calls


def _fake_edge(monkeypatch: pytest.MonkeyPatch) -> tuple[list[FakeChild], dict[str, Any]]:
    """winproc.spawn as the WindowManager will call it: a fresh window per call, all kept in
    the returned list; `spawned` records the last call's arguments."""
    children: list[FakeChild] = []
    spawned: dict[str, Any] = {}

    async def fake_spawn(
        argv: Sequence[str], *, cwd: Path, env: Mapping[str, str], pipes: bool
    ) -> FakeChild:
        spawned.update(argv=list(argv), cwd=cwd, env=dict(env), pipes=pipes)
        children.append(FakeChild())
        return children[-1]

    monkeypatch.setattr(launcher.winproc, "spawn", fake_spawn)
    return children, spawned


def _assert_lock_is_free() -> None:
    lock = winproc.single_instance(launcher.MUTEX_NAME)
    assert lock is not None
    lock.release()


def _acknowledge_wakes(monkeypatch: pytest.MonkeyPatch) -> None:
    """These orchestration tests fake admission; WindowManager tests pin when it is acknowledged."""
    original_set = winproc.Signal.set

    def acknowledge(signal: winproc.Signal) -> None:
        original_set(signal)
        reply = winproc.Signal.open(launcher.OPENED_EVENT)
        if reply is not None:
            original_set(reply)
            reply.close()

    monkeypatch.setattr(winproc.Signal, "set", acknowledge)


def test_a_launch_with_nothing_to_spawn_still_serves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No vault, no CLI and CLAUDE_CONFIG_DIR used to be three ConfigErrors that
    returned 2 with a message box and no window. The app opens on all three now.

    The CLI here is present and usable; CLAUDE_CONFIG_DIR alone is what refuses the spawn, so
    this pins the single predicate rather than the empty `cli` field. An empty argv is what a
    session start then refuses on, and the version probe is skipped for the same reason — it
    would be a claude.exe reading a different ~/.claude from the one this app reads."""
    cfg = replace(_cfg(tmp_path), vault=None, claude_config_dir_set=True)
    calls = _reaching_the_server(monkeypatch, cfg)
    monkeypatch.setattr(launcher, "cli_version", _forbid("cli_version"))

    assert launcher.main([]) == 0

    created = calls["create_app"]
    assert created["readiness"].may_spawn() is False
    assert created["manager"]._config.cli == ()
    assert created["manager"]._config.cli_version == ""
    # Chat-only: the working directory is the shared scratch root, never the process's own.
    assert created["config"].vault == launcher.workspaces.scratch_root(cfg.data_dir)


def test_a_data_dir_inside_the_vault_is_refused_before_any_file_is_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refusal must run before logging opens its file, or the log file itself
    lands in the vault (hard rule 1). The reason is logged at the default location instead."""
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "CLAUDE.md").write_text("# vault", encoding="utf-8")
    monkeypatch.setenv("VIKI_VAULT", str(vault))
    monkeypatch.setenv("VIKI_DATA_DIR", str(vault / "10-projects" / "viki-data"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    # On POSIX the fallback log location comes from config.default_data_dir(), which falls back
    # to HOME (and XDG_DATA_HOME on Linux) rather than LOCALAPPDATA; a test must never write into
    # the runner's real home, so point HOME at tmp_path and drop XDG_DATA_HOME.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setattr(launcher.config, "load", _forbid("config.load"))
    _forbid_serving(monkeypatch)

    if sys.platform == "win32":
        default = tmp_path / "local" / "viki-agent"
    elif sys.platform == "darwin":
        default = tmp_path / "home" / "Library" / "Application Support" / "viki-agent"
    else:
        default = tmp_path / "home" / ".local" / "share" / "viki-agent"

    with _fresh_root_handlers():
        assert launcher.main([]) == 2
        assert [p.name for p in vault.rglob("*")] == ["CLAUDE.md"]
        log_file = default / "logs" / launcher.LOG_NAME
        assert "inside" in log_file.read_text(encoding="utf-8")


@pytest.mark.parametrize("protected", ["claude", "vault", "alias"])
@pytest.mark.parametrize("override", [False, True])
def test_a_protected_default_is_refused_for_fallback_logging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    alerts: list[str],
    protected: str,
    override: bool,
) -> None:
    home, vault = tmp_path / "home", tmp_path / "vault"
    base = {"claude": home / ".claude", "vault": vault, "alias": tmp_path / "local"}[protected]
    env = {"VIKI_VAULT": str(vault), "LOCALAPPDATA": str(base)}
    if override:
        env["VIKI_DATA_DIR"] = str(vault / "refused-override")
    monkeypatch.setattr(os, "environ", env)
    monkeypatch.setattr(Path, "home", classmethod(lambda _: home))
    # Exercise the Windows default calculation on every host without changing pathlib's OS.
    monkeypatch.setattr(config, "sys", SimpleNamespace(platform="win32"))
    default = base / "viki-agent"
    resolve = Path.resolve

    def resolved(path: Path, *args: Any, **kwargs: Any) -> Path:
        # Model a pre-existing default-directory alias without platform symlink privileges.
        if protected == "alias" and path == default:
            return home / ".codex" / "aliased-data"
        return resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolved)
    monkeypatch.setattr(launcher, "_configure_logging", _forbid("_configure_logging"))
    monkeypatch.setattr(config, "load", _forbid("config.load"))
    _forbid_serving(monkeypatch)

    assert launcher.main([]) == 2
    assert alerts == [launcher.MSG_BAD_DATA_DIR]
    assert list(tmp_path.iterdir()) == []


class _Stop(Exception):
    pass


def test_the_descriptor_limit_is_raised_after_logging_and_before_the_config_loads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POSIX only: raised after logging, so a failed raise is logged, and before config.load,
    the lock and the server, which open the descriptors it is for. Windows never calls it."""
    order: list[str] = []
    monkeypatch.setenv("VIKI_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(launcher, "_configure_logging", lambda log_dir: order.append("logging"))
    monkeypatch.setattr(launcher, "_raise_nofile_limit", lambda: order.append("nofile"))

    def load(env: Mapping[str, str]) -> config.Config:
        order.append("load")
        raise _Stop

    monkeypatch.setattr(launcher.config, "load", load)

    with pytest.raises(_Stop):
        launcher.main([])

    if sys.platform == "win32":
        assert order == ["logging", "load"]
    else:
        assert order == ["logging", "nofile", "load"]


def _stop_before_serving(monkeypatch: pytest.MonkeyPatch, cfg: config.Config) -> None:
    """Get `_main` to the point right after the lock is held, with nothing past it: `config.load`
    and `_configure_logging` are faked, and `_serve` raises before it can build or run anything."""
    monkeypatch.setattr(launcher.config, "load", lambda env: cfg)
    monkeypatch.setattr(launcher, "_configure_logging", lambda log_dir: None)


def test_main_installs_the_keychain_source_holding_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The launch must install `protection.keychain_source(cfg.data_dir)` as the key source,
    holding the lock and before `_serve`. On Windows DPAPI needs no key, so the recorder must
    never run there."""
    cfg = _cfg(tmp_path)
    _stop_before_serving(monkeypatch, cfg)
    recorded: list[Path] = []
    sentinel: Callable[[], bytes] = lambda: b"\x00" * 32  # noqa: E731

    def recorder(data_dir: Path) -> Callable[[], bytes]:
        recorded.append(data_dir)
        assert winproc.single_instance(launcher.MUTEX_NAME) is None  # this launch holds it
        return sentinel

    monkeypatch.setattr(launcher.protection, "keychain_source", recorder)
    monkeypatch.setattr(launcher, "_serve", _raise(_Stop()))

    with pytest.raises(_Stop):
        launcher.main([])

    if sys.platform == "win32":
        assert recorded == []
    else:
        assert recorded == [cfg.data_dir]
        assert launcher.protection._key_source is sentinel


def _visits(monkeypatch: pytest.MonkeyPatch) -> list[None]:
    """Every visit to the key source main() installs."""
    visited: list[None] = []

    def source() -> bytes:
        visited.append(None)
        return b"\x00" * 32

    monkeypatch.setattr(launcher.protection, "keychain_source", lambda data_dir: source)
    return visited


@pytest.mark.skipif(sys.platform == "win32", reason="the POSIX keychain visit")
def test_main_visits_the_key_source_just_before_the_services_are_built(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The visit itself, not just the source's installation, must happen before the services and
    the app, where the first seal can happen. A locked or slow keychain must never be found for
    the first time inside a write transaction, where every other writer answers `busy`."""
    assert sys.platform != "win32"
    _reaching_the_server(monkeypatch, _cfg(tmp_path))
    visited = _visits(monkeypatch)

    def build_services(*_args: Any, **_kwargs: Any) -> Any:
        assert visited == [None], "the key source must be visited before anything seals"
        raise _Stop

    monkeypatch.setattr(launcher, "build_services", build_services)

    with pytest.raises(_Stop):
        launcher.main([])


@pytest.mark.skipif(sys.platform == "win32", reason="the POSIX keychain visit")
@pytest.mark.parametrize(
    "refusal", ["port_taken", "no_bundle", "data_dir_in_workspace", "bad_scratch", "recovery"]
)
def test_a_launch_that_refuses_never_visits_the_key_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, refusal: str
) -> None:
    """A locked keychain's unlock prompt, or a first launch minting a key and writing its record,
    must not come before a refusal: `_serve`'s (a taken port), `_serve_on`'s own (no built
    bundle; the data directory found inside a registered workspace; an unsafe chat-only scratch
    root), or the read-only recovery shell that serves instead of a store that cannot be opened,
    which never visits at all."""
    assert sys.platform != "win32"
    cfg = _cfg(tmp_path)
    _reaching_the_server(monkeypatch, cfg)
    visited = _visits(monkeypatch)
    if refusal == "data_dir_in_workspace":
        monkeypatch.setattr(
            launcher.WorkspaceService,
            "contains_data_dir",
            lambda self, data_dir: SimpleNamespace(workspace_id="a-registered-workspace"),
        )
        assert launcher.main([]) == 2
    elif refusal == "bad_scratch":
        monkeypatch.setattr(
            launcher.WorkspaceService,
            "resolve_active",
            _raise(StateError("unsafe_path")),
        )
        assert launcher.main([]) == 2
    elif refusal == "recovery":
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        (cfg.data_dir / "app.sqlite3").write_bytes(b"not a database")
        assert launcher.main([]) == 0
    else:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as other_program:
            if refusal == "port_taken":
                other_program.bind((config.HOST, config.PORT))
                other_program.listen()
            else:
                (launcher.STATIC_DIR / "index.html").unlink()
            assert launcher.main([]) == (4 if refusal == "port_taken" else 7)

    assert visited == []


@pytest.mark.skipif(sys.platform == "win32", reason="the POSIX keychain visit")
def test_a_second_launch_that_only_wakes_never_visits_the_key_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second launch that wakes the running one never serves anything, so there is nothing
    for a keychain visit to protect."""
    assert sys.platform != "win32"
    cfg = _cfg(tmp_path)
    _stop_before_serving(monkeypatch, cfg)
    _forbid_serving(monkeypatch)
    visited = _visits(monkeypatch)
    _acknowledge_wakes(monkeypatch)

    running = winproc.single_instance(launcher.MUTEX_NAME)
    assert running is not None
    wake = winproc.Signal.create(launcher.OPEN_EVENT)
    try:
        assert launcher.main([]) == 0
        assert wake.wait(0) is True  # it asked the running one for a window
    finally:
        wake.close()
        running.release()

    assert visited == []


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX private-directory bits")
def test_configure_logging_creates_the_data_directory_private_under_a_desktop_umask(
    tmp_path: Path, desktop_umask: None
) -> None:
    """The test session's own umask 077 would hide a missing create_private_directory call —
    every directory would come out 0700 either way. A desktop's ordinary umask (022) does not."""
    assert sys.platform != "win32"
    data_dir = tmp_path / "data"
    with _fresh_root_handlers():
        launcher._configure_logging(data_dir / "logs")

    assert stat.S_IMODE(data_dir.stat().st_mode) == 0o700
    protection.require_private_directory(data_dir)  # raises if it is not


def test_the_log_file_rotates_instead_of_growing_for_ever(tmp_path: Path) -> None:
    """The launcher's log is the only record a windowless launch leaves, and nothing ever trims
    it: no launch truncates it and no other tool knows it exists. A plain FileHandler would grow
    for as long as the user keeps the app."""
    log_dir = tmp_path / "logs"
    with _fresh_root_handlers():
        launcher._configure_logging(log_dir)
        (handler,) = [
            h
            for h in logging.getLogger().handlers
            if isinstance(h, logging.handlers.RotatingFileHandler)
        ]
        handler.maxBytes = 200  # rotate after a line or two rather than after a megabyte
        for _ in range(20):
            logging.error("a warning long enough to fill the file %s", "x" * 40)

    assert (log_dir / f"{launcher.LOG_NAME}.1").is_file()
    assert not (log_dir / f"{launcher.LOG_NAME}.{launcher.LOG_BACKUPS + 1}").exists()


def test_an_exception_from_create_app_is_logged_before_it_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """create_app (a route the app cannot build, say) can fail after logging is configured
    but escape main() unlogged. The user's log file must show why the app never came up, not
    stay empty. And neither the lock nor the port this launch bound may stay taken."""
    monkeypatch.setenv("VIKI_DATA_DIR", str(tmp_path / "data"))
    cfg = _cfg(tmp_path)
    port = _spare_port()
    monkeypatch.setattr(launcher.config, "load", lambda env: cfg)
    monkeypatch.setattr(launcher.config, "billing_problems", lambda c, env: [])
    monkeypatch.setattr(launcher.config, "PORT", port)
    # This test configures logging for real, so it cannot go through _reaching_the_server; the
    # patch that keeps a launch off the machine it runs on still applies: the Git chip runs a
    # real git process, which does not belong in a unit test.
    monkeypatch.setattr(launcher, "git_version", lambda: "")
    _fake_bundle(monkeypatch, cfg)

    def boom(**kw: Any) -> Any:
        raise RuntimeError("the app could not be built")

    monkeypatch.setattr(launcher.server, "create_app", boom)
    monkeypatch.setattr(launcher.uvicorn, "Server", _forbid("uvicorn.Server"))
    _patch_posix_server(monkeypatch, _forbid("_GracefulServer"))

    with _fresh_root_handlers():
        with pytest.raises(RuntimeError, match="could not be built"):
            launcher.main([])
        log_file = tmp_path / "data" / "logs" / launcher.LOG_NAME
        assert "the app could not be built" in log_file.read_text(encoding="utf-8")
    _assert_lock_is_free()
    # _serve owns the listening socket from the moment it checks the port, so every path out of
    # it has to give the port back — a failure here used to release nothing.
    assert launcher._port_free(config.HOST, port) is True


def test_main_scrubs_the_process_environment_in_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """winproc.scrub_env returns a copy; main() must also strip the leaked variables from
    its own os.environ, since anything that inherits the process environment directly (a
    fresh os.startfile launch, or the SDK reading CLAUDE_CODE_STREAM_CLOSE_TIMEOUT) otherwise
    still sees them. This test session already runs inside Claude Code, so os.environ carries
    real CLAUDECODE/CLAUDE_CODE_*/etc. vars beyond the two set below; the module's autouse
    _preserve_os_environ fixture restores the real environment afterward regardless."""
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-leak")
    _reaching_the_server(monkeypatch, _cfg(tmp_path))

    assert launcher.main([]) == 0
    assert "CLAUDECODE" not in os.environ
    assert "ANTHROPIC_API_KEY" not in os.environ


@pytest.mark.parametrize("switched_off", [False, True], ids=["default", "switched_off"])
def test_billing_problems_are_logged_without_stopping_the_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    switched_off: bool,
) -> None:
    """A billing route off the subscription login used to return 2 with a message box
    and no window, which left the user with nothing to read the reason in. It is a readiness
    fact now: the shell opens and the preflight runs against the workspace the CLI will run in.
    Native sessions still start while other sign-in methods are allowed, as they are by default;
    with that switch off, a session start is refused with the `billing` code. Either way the
    conflict stays visible, so a model connection still refuses on it."""
    cfg = _cfg(tmp_path)
    calls = _reaching_the_server(monkeypatch, cfg)
    if switched_off:
        # tests/app/test_other_sign_in.py pins how the switch itself is read from the store.
        monkeypatch.setattr(launcher, "_other_sign_in", lambda settings: False)
    seen: list[Path | None] = []

    def problems(config_: config.Config, env: Mapping[str, str]) -> list[str]:
        del env
        seen.append(config_.vault)
        return ["settings.json: apiKeyHelper is set"]

    monkeypatch.setattr(launcher.config, "billing_problems", problems)

    with caplog.at_level(logging.ERROR):
        assert launcher.main([]) == 0

    assert seen == [launcher.workspaces.scratch_root(cfg.data_dir)]  # not the raw VIKI_VAULT
    assert "apiKeyHelper" in caplog.text
    readiness = calls["create_app"]["readiness"]
    provider = readiness.capabilities(None, _READY_STORAGE).provider["claude"]
    assert readiness.other_sign_in is not switched_off
    assert provider.billing == ("blocked" if switched_off else "clear")
    assert provider.can_start is not switched_off
    assert readiness.billing_preflight_blocked is True


def test_a_nested_json_settings_file_in_the_active_vault_never_crashes_the_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """json.loads raises RecursionError, not ValueError, on JSON nested this deep. The real
    billing preflight runs against the active workspace's own settings.json before
    build_services is ever reached; that file being nested too deep must read as unreadable,
    never crash the launch."""
    vault = tmp_path / "env-vault"
    settings = vault / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text("[" * 200_000, encoding="utf-8")
    for name in ("TEMP", "TMP", "TMPDIR", "LOCALAPPDATA", "APPDATA", "OneDrive"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("VIKI_VAULT", str(vault))
    # The real preflight must never scan this machine's managed settings (test_config.py's
    # _synthetic_managed_dirs fixture does the same).
    managed = {os_name: tmp_path / "managed" / os_name for os_name in ("win32", "darwin", "linux")}
    monkeypatch.setattr(config, "_MANAGED_DIRS", managed)
    monkeypatch.setattr(config, "_LEGACY_MANAGED_SETTINGS", tmp_path / "legacy-managed.json")
    cfg = _cfg(tmp_path)
    real_billing = config.billing_problems  # before _reaching_the_server stubs it
    calls = _reaching_the_server(monkeypatch, cfg)
    seen: list[Path | None] = []

    def wrapper(config_: config.Config, env: Mapping[str, str] | None = None) -> list[str]:
        del env
        seen.append(config_.vault)
        return real_billing(config_, env={})

    monkeypatch.setattr(launcher.config, "billing_problems", wrapper)

    with caplog.at_level(logging.WARNING, logger="viki_agent.config"):
        assert launcher.main([]) == 0

    assert seen == [vault]
    assert any(
        str(settings) in r.getMessage() and "RecursionError" in r.getMessage()
        for r in caplog.records
    )
    created = calls["create_app"]
    assert created["config"].vault == vault
    provider = created["readiness"].capabilities(None, _READY_STORAGE).provider["claude"]
    assert provider.billing != "blocked"


def test_an_unregistered_environment_vault_is_denied_as_an_export_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`VIKI_VAULT` naming a folder that was never registered is an ephemeral workspace: it is
    the working directory this launch serves, but it is never a row, so the persisted listing
    the export service filters on cannot see it. Unless the active root is named in its own
    right, an export lands inside the live vault and its paths leave unredacted."""
    vault = tmp_path / "env-vault"
    vault.mkdir()
    # tmp_path lives under the real %TEMP% (TMPDIR on POSIX), which the deny set covers by
    # prefix: leave those variables in place and every workspace a test can create is refused
    # as denied_location.
    for name in ("TEMP", "TMP", "TMPDIR", "LOCALAPPDATA", "APPDATA", "OneDrive"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("VIKI_VAULT", str(vault))
    calls = _reaching_the_server(monkeypatch, _cfg(tmp_path))

    assert launcher.main([]) == 0

    created = calls["create_app"]
    active = created["config"].vault
    assert active == vault  # the launch really is running in the override, unregistered
    _locations, exports, _imports, _reset = created["data"]
    preview = exports.preview("settings", None)

    with pytest.raises(ExportError) as refused:
        exports.write(
            command_id=str(uuid.uuid4()), preview_id=preview.preview_id, destination=str(vault)
        )

    assert refused.value.code == "denied_destination"
    assert list(vault.iterdir()) == []
    assert (str(active), "<workspace>") in exports._roots()


def test_a_data_directory_inside_a_registered_workspace_stops_the_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, alerts: list[str], caplog
) -> None:
    """The one containment case config.data_dir() cannot see: the data directory was moved by
    environment into a root the store already has a row for. It is checked after the store
    opens, so the log folder (and, for a new store, app.sqlite3) already exist there — which is
    why the box names the folder to remove, and why this is a refusal rather than a warning."""
    cfg = _cfg(tmp_path)
    calls = _reaching_the_server(monkeypatch, cfg)
    held = SimpleNamespace(workspace_id="11111111-1111-4111-8111-111111111111")

    class _Holding(launcher.WorkspaceService):
        def contains_data_dir(self, data_dir: Path) -> Any:
            del data_dir
            return held

    monkeypatch.setattr(launcher, "WorkspaceService", _Holding)

    with caplog.at_level(logging.ERROR):
        assert launcher.main([]) == 2

    assert alerts == [launcher.MSG_DATA_DIR_IN_WORKSPACE.format(data=cfg.data_dir)]
    assert str(cfg.data_dir) in alerts[0]
    assert held.workspace_id in caplog.text  # the workspace is named in the log, not on screen
    assert "create_app" not in calls and FakeServer.instances == []
    _assert_lock_is_free()


def test_a_scratch_root_that_cannot_be_used_names_that_folder_and_stops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, alerts: list[str], caplog
) -> None:
    """resolve_active refuses a link anywhere in the chain of the chat-only scratch root, and
    without a working directory there is nothing to serve. No registration is involved, so the
    box has to name that folder: told only that "a workspace" is unusable, the user would go
    looking through the workspaces they have instead of the one folder they have to repair."""
    cfg = _cfg(tmp_path)
    calls = _reaching_the_server(monkeypatch, cfg)

    class _UnsafeScratch(launcher.WorkspaceService):
        def resolve_active(self) -> Any:
            raise StateError("unsafe_path")

    monkeypatch.setattr(launcher, "WorkspaceService", _UnsafeScratch)

    with caplog.at_level(logging.ERROR):
        assert launcher.main([]) == 2

    scratch = cfg.data_dir / "scratch" / "chat"
    assert alerts == [launcher.MSG_BAD_SCRATCH.format(scratch=scratch)]
    assert str(scratch) in alerts[0]
    assert "unsafe_path" in caplog.text
    assert "create_app" not in calls and FakeServer.instances == []
    _assert_lock_is_free()


def test_a_refused_data_dir_says_so_without_asking_the_refused_question_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, alerts: list[str]
) -> None:
    """The fallback log location must not go back through the containment check that just
    refused: a second ConfigError there would escape before any logging exists, and the reason
    would be lost with nothing on screen either."""
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "notes.md").write_text("# vault", encoding="utf-8")
    monkeypatch.setenv("VIKI_VAULT", str(vault))
    monkeypatch.setenv("VIKI_DATA_DIR", str(vault / "data"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    # As above: the fallback log location falls back to HOME/XDG_DATA_HOME on POSIX, never the
    # runner's real home.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setattr(launcher.config, "load", _forbid("config.load"))
    _forbid_serving(monkeypatch)

    with _fresh_root_handlers():
        assert launcher.main([]) == 2
    assert alerts == [launcher.MSG_BAD_DATA_DIR]


@pytest.mark.parametrize("args", [[], ["--dev"]])
def test_test_environment_cannot_change_production_provider_argv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, args: list[str]
) -> None:
    cfg = _cfg(tmp_path)
    calls = _reaching_the_server(monkeypatch, cfg)
    monkeypatch.setenv("VIKI_TEST_TOKEN", "synthetic-test-token")
    monkeypatch.setenv("VIKI_TEST_CLI_SCRIPT", str(tmp_path / "must-not-run.py"))
    probes: list[tuple[str, ...]] = []

    def version(argv: Sequence[str]) -> str:
        probes.append(tuple(argv))
        return "1.2.3"

    monkeypatch.setattr(launcher, "cli_version", version)
    monkeypatch.setattr(winproc, "run", _forbid("winproc.run"))
    monkeypatch.setattr(winproc, "spawn", _forbid("winproc.spawn"))

    assert launcher.main(args) == 0
    assert probes == [(str(cfg.cli),)]
    created = calls["create_app"]
    assert created["manager"]._config.cli == (str(cfg.cli),)
    assert created["token"] != "synthetic-test-token"
    _assert_lock_is_free()


@pytest.mark.parametrize(
    "cli_source,pin_updates",
    [("bundled", True), ("installed", False), ("override", False), ("", False)],
)
def test_sessions_pin_updates_only_for_the_sdk_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cli_source: config.CliSource,
    pin_updates: bool,
) -> None:
    """DISABLE_AUTOUPDATER=1 is pinned for the SDK's own bundled copy only; the user's own
    installation keeps its own update setting, on or off, so security fixes reach someone who
    chats only through VIKI even though VIKI itself never runs an installer."""
    cfg = replace(_cfg(tmp_path), cli_source=cli_source)
    _reaching_the_server(monkeypatch, cfg)
    real_child_env = launcher.winproc.child_env
    calls: dict[str, Any] = {}

    def recording(
        entrypoint: str, base: Mapping[str, str] | None = None, *, pin_updates: bool = True
    ) -> dict[str, str]:
        calls["pin_updates"] = pin_updates
        return real_child_env(entrypoint, base, pin_updates=pin_updates)

    monkeypatch.setattr(launcher.winproc, "child_env", recording)

    assert launcher.main([]) == 0

    assert calls["pin_updates"] is pin_updates


def test_one_launch_owner_reaches_both_ledgers_and_the_reset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One ledger `owner` per launch, minted in `build_services` and handed to `create_app` and
    to the reset service from that one place, so the two cannot drift.

    A reset holding a different value than the ledgers deletes exactly the in-flight rows the
    narrowing exists to keep, and reports them as deleted.
    """
    cfg = _cfg(tmp_path)
    calls = _reaching_the_server(monkeypatch, cfg)

    assert launcher.main([]) == 0

    owner = calls["create_app"]["owner"]
    assert str(uuid.UUID(owner)) == owner
    reset = next(s for s in calls["create_app"]["data"] if isinstance(s, ResetService))
    assert reset._owner == owner
    # Not the diagnostics identity: that one is published in the export bundle and the
    # diagnostics document, and a durable-storage meaning would couple the two.
    assert owner != calls["create_app"]["readiness"].launch_id


def test_happy_path_wires_create_app_and_the_server_together(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _cfg(tmp_path)
    calls = _reaching_the_server(monkeypatch, cfg)

    assert launcher.main([]) == 0

    created = calls["create_app"]
    assert created["dev"] is False
    assert created["static_dir"] == launcher.STATIC_DIR
    # `config` is no longer the object load() returned: the launcher replaces its vault with
    # the active workspace's working directory.
    assert created["config"] == replace(cfg, vault=launcher.workspaces.scratch_root(cfg.data_dir))
    assert isinstance(created["windows"], WindowManager)
    assert isinstance(created["token"], str) and created["token"]

    (uvi,) = FakeServer.instances
    assert uvi.ran
    assert uvi.config.app == "the-app"
    assert uvi.config.host == config.HOST
    assert uvi.config.port == config.PORT
    assert uvi.config.ws_max_size == server.WS_MAX_SIZE
    assert uvi.config.reload is False
    assert uvi.config.access_log is False
    # Uvicorn's default LOGGING_CONFIG formatter calls sys.stdout.isatty(), which raises
    # when there is no stdout (the windowless pythonw launcher). log_config=None avoids it.
    assert uvi.config.log_config is None
    _assert_lock_is_free()


@pytest.mark.parametrize("filename", ["state.json", "app.sqlite3"])
def test_damaged_app_state_serves_the_recovery_shell_and_preserves_its_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    alerts: list[str],
    caplog: pytest.LogCaptureFixture,
    filename: str,
) -> None:
    """A damaged or newer store used to return 8 with a message box and no window, so
    the user's only instructions were in a modal they had already dismissed. It opens the
    read-only recovery shell now — the bundle, the face, capabilities and diagnostics — and ends
    0 when that window closes. Nothing is repaired, reset or read out of the damaged file."""
    cfg = _cfg(tmp_path)
    calls = _reaching_the_server(monkeypatch, cfg)
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    damaged = cfg.data_dir / filename
    original = b"damaged-private-content-must-not-reach-logs"
    damaged.write_bytes(original)
    probed: list[tuple[str, ...]] = []
    monkeypatch.setattr(launcher, "cli_version", lambda argv: probed.append(tuple(argv)) or "2.0.0")

    assert launcher.main([]) == 0

    assert damaged.read_bytes() == original
    assert "create_app" not in calls  # no manager, no catalog, no socket
    recovery = calls["create_recovery_app"]
    assert isinstance(recovery["windows"], WindowManager)
    assert recovery["static_dir"] == launcher.STATIC_DIR
    # Recovery never resolves a workspace, so the config it serves is the one load() returned.
    assert recovery["config"] == cfg
    assert recovery["readiness"].may_spawn() is True
    # The CLI probe is the real one, and it is still only run when the shell asks for it: a
    # stubbed check reported a healthy CLI as `failed` and sent the user after a second
    # problem they do not have, and an eager probe would spawn a process during a failure path.
    assert probed == []
    checked = asyncio.run(recovery["checks"].run("provider_cli"))
    assert probed == [tuple(launcher.cli_argv(cfg.cli, os.environ))]
    assert checked.status == "ok" and checked.result["version"] == "2.0.0"
    (uvi,) = FakeServer.instances
    assert uvi.ran and uvi.config.app == "the-recovery-app"
    assert alerts == []  # the shell says it, not a modal
    assert original.decode() not in caplog.text
    _assert_lock_is_free()
    assert launcher._port_free(config.HOST, config.PORT)


def test_the_recovery_diagnostics_document_names_the_refusal_capabilities_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`server.storage_readiness` exists so that capabilities and the diagnostics document never
    disagree about the store in one launch, and the recovery shell puts both on one screen: the
    headline from capabilities, the document below it. The builder used to take the
    `recovery_required` default whatever had actually refused the store, so a corrupt database
    was reported on that screen as the one refusal it is not — the hot rollback journal, whose
    troubleshooting procedure the user would then follow against a database that has none."""
    cfg = _cfg(tmp_path)
    calls = _reaching_the_server(monkeypatch, cfg)
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    (cfg.data_dir / "app.sqlite3").write_bytes(b"not a database")

    assert launcher.main([]) == 0

    recovery = calls["create_recovery_app"]
    storage = recovery["diagnostics_document"]()["storage"]
    assert recovery["storage_refusal"] == "corrupt"  # what capabilities reports on that screen
    assert storage == {"state": "unavailable", "reason": "corrupt", "private": None}


def test_a_recovery_shell_whose_first_window_never_opens_says_so_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, alerts: list[str]
) -> None:
    """A store failure and a spawn failure together used to end in exit 0 with nothing on
    screen: the recovery shell reports a windowless launch exactly as an ordinary launch does,
    because a shell nobody can see is not a recovery, it is the silent icon the box exists for."""
    cfg = _cfg(tmp_path)
    calls = _reaching_the_server(monkeypatch, cfg)
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    (cfg.data_dir / "app.sqlite3").write_bytes(b"not a database")

    def run(self: FakeServer, sockets: list[socket.socket] | None = None) -> None:
        self.ran = True
        calls["create_recovery_app"]["windows"].first_failed = True

    monkeypatch.setattr(FakeServer, "run", run)

    assert launcher.main([]) == 6
    assert alerts == [launcher.MSG_NO_WINDOW]
    _assert_lock_is_free()


def test_dev_flag_serves_no_static_dir_and_flags_dev(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _reaching_the_server(monkeypatch, _cfg(tmp_path))
    # --dev serves the Vite dev server, so an unbuilt frontend/dist must not refuse the launch.
    monkeypatch.setattr(launcher, "STATIC_DIR", tmp_path / "never-built")

    assert launcher.main(["--dev"]) == 0
    assert calls["create_app"]["dev"] is True
    assert calls["create_app"]["static_dir"] is None


def test_a_frontend_that_was_never_built_says_so_and_names_the_build_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, alerts: list[str]
) -> None:
    """frontend/dist is gitignored, so an unbuilt bundle is the ordinary state of a fresh clone,
    a `git clean` or a build that stopped halfway — not an exotic failure. Left to create_app,
    StaticFiles raises and the exception leaves main() into a pythonw process with no console
    and no parent: the user double-clicks the icon and gets literally nothing, which looks
    exactly like an app that was never installed. The box has to name the command that fixes
    it, because nothing else on screen will."""
    calls = _reaching_the_server(monkeypatch, _cfg(tmp_path))
    monkeypatch.setattr(launcher.winproc, "spawn", _forbid("winproc.spawn"))
    never_built = tmp_path / "never-built"
    monkeypatch.setattr(launcher, "STATIC_DIR", never_built)

    assert launcher.main([]) == 7
    assert alerts == [
        launcher.MSG_NO_BUNDLE.format(build=f'npm --prefix "{never_built.parent}" run build')
    ]
    assert "npm" in alerts[0] and "run build" in alerts[0]
    assert "create_app" not in calls and FakeServer.instances == []
    _assert_lock_is_free()
    assert launcher._port_free(config.HOST, config.PORT) is True  # the port was given back

    # A dist directory that exists but holds no index.html is a half-finished build, and
    # StaticFiles is happy to mount it and then serve 404s at the window forever.
    alerts.clear()
    never_built.mkdir()
    assert launcher.main([]) == 7
    assert len(alerts) == 1


def test_asset_paths_choose_checkout_or_installed_package(tmp_path: Path) -> None:
    module = tmp_path / "src" / "viki_agent" / "__main__.py"
    module.parent.mkdir(parents=True)
    module.touch()
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "package.json").write_text("{}", encoding="utf-8")

    assert launcher._asset_paths(module) == (
        frontend / "dist",
        frontend / "public" / "favicon.ico",
        True,
    )

    # Compiled, the packaged UI serves even beside a checkout's own frontend/package.json:
    # a Nuitka folder has no src/ layer, so the checkout branch must never be taken there.
    packaged = module.parent / "resources" / "ui"
    assert launcher._asset_paths(module, compiled_app=True) == (
        packaged,
        packaged / "favicon.ico",
        False,
    )

    (frontend / "package.json").unlink()
    assert launcher._asset_paths(module) == (packaged, packaged / "favicon.ico", False)


def test_repo_is_the_compiled_install_dir() -> None:
    """launcher._REPO is compiled.INSTALL_DIR itself (ADR-054): see the parallel config.py test."""
    assert launcher._REPO is compiled.INSTALL_DIR


def test_installed_package_without_ui_explains_product_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, alerts: list[str]
) -> None:
    calls = _reaching_the_server(monkeypatch, _cfg(tmp_path))
    monkeypatch.setattr(launcher, "_SOURCE_CHECKOUT", False)
    monkeypatch.setattr(launcher, "STATIC_DIR", tmp_path / "missing-package-ui")

    assert launcher.main([]) == 7
    assert alerts == [launcher.MSG_NO_PACKAGED_BUNDLE]
    assert "build_product.py" in alerts[0]
    assert "create_app" not in calls


def test_the_window_manager_opens_edge_on_the_token_url_and_kills_it_when_stopped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The app's lifespan drives the WindowManager main() hands to create_app; here the test
    drives it the same way, over a fake winproc.spawn."""
    cfg = _cfg(tmp_path)
    calls = _reaching_the_server(monkeypatch, cfg)
    children, spawned = _fake_edge(monkeypatch)

    assert launcher.main([]) == 0
    windows = calls["create_app"]["windows"]
    assert isinstance(windows, WindowManager)
    assert cfg.edge_profile.is_dir()

    async def open_then_stop() -> None:
        await windows.start()
        assert windows.count == 1
        await windows.stop()

    asyncio.run(open_then_stop())
    assert spawned["argv"][0] == str(cfg.edge)
    assert spawned["cwd"] == cfg.edge_profile
    assert spawned["pipes"] is False
    app_arg = next(a for a in spawned["argv"] if a.startswith("--app="))
    token = calls["create_app"]["token"]
    if sys.platform == "win32":
        target = app_arg.removeprefix("--app=")
    else:
        # The token reaches the window through a private page, never through argv.
        (page,) = (cfg.data_dir / "run").glob("open-*.html")
        assert app_arg == f"--app={page.as_uri()}"
        assert token not in " ".join(spawned["argv"])
        target = html.unescape(
            page.read_text(encoding="utf-8").partition('content="0;url=')[2].partition('"')[0]
        )
    parsed = urlsplit(target)
    assert (parsed.scheme, parsed.netloc, parsed.path) == ("http", "127.0.0.1:8765", "/")
    assert parsed.fragment == f"token={token}"
    query = parse_qs(parsed.query, keep_blank_values=True)
    assert set(query) == {"ui"} and len(query["ui"]) == 1
    assert uuid.UUID(query["ui"][0]).hex == query["ui"][0]
    assert token not in parsed.query
    assert len(token) >= 43
    assert children[0].killed and children[0].released


def test_the_last_window_closing_closes_sessions_with_a_grace_then_stops_the_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _reaching_the_server(monkeypatch, _cfg(tmp_path))
    children, _ = _fake_edge(monkeypatch)
    graces: list[float] = []
    closed: list[bool] = []

    class RecordingManager(launcher.SessionManager):
        async def settle(self, grace_s: float, until: Callable[[], bool] | None = None) -> None:
            graces.append(grace_s)
            await super().settle(grace_s, until)

        async def close_all(self) -> None:
            closed.append(True)
            await super().close_all()

    monkeypatch.setattr(launcher, "SessionManager", RecordingManager)

    assert launcher.main([]) == 0
    windows = calls["create_app"]["windows"]
    (uvi,) = FakeServer.instances

    async def close_the_window() -> None:
        await windows.start()
        children[0].exited.set()
        for _ in range(20):
            await asyncio.sleep(0)
        await windows.stop()

    asyncio.run(close_the_window())
    assert graces == [launcher.WINDOW_GRACE_S]
    assert closed == [True]
    assert uvi.should_exit is True


def test_a_close_that_fails_still_stops_the_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """should_exit is set in a finally, so a session that will not close cannot leave the app
    running with no window."""
    calls = _reaching_the_server(monkeypatch, _cfg(tmp_path))
    children, _ = _fake_edge(monkeypatch)

    class FailingManager(launcher.SessionManager):
        async def close_all(self) -> None:
            raise RuntimeError("close failed")

    monkeypatch.setattr(launcher, "SessionManager", FailingManager)
    assert launcher.main([]) == 0
    windows = calls["create_app"]["windows"]
    (uvi,) = FakeServer.instances

    async def close_the_window() -> None:
        await windows.start()
        children[0].exited.set()
        for _ in range(50):
            await asyncio.sleep(0)
        await windows.stop()

    asyncio.run(close_the_window())
    assert uvi.should_exit is True


def test_a_first_window_that_never_opens_ends_the_launch_with_a_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, alerts: list[str]
) -> None:
    """A spawn that fails would otherwise leave a server with no window, no UI and no way to
    stop it. The lifespan's start() marks it; the launcher turns that into code 6."""
    calls = _reaching_the_server(monkeypatch, _cfg(tmp_path))

    def run(self: FakeServer, sockets: list[socket.socket] | None = None) -> None:
        self.ran = True  # what the lifespan would have left behind
        calls["create_app"]["windows"].first_failed = True

    monkeypatch.setattr(FakeServer, "run", run)
    assert launcher.main([]) == 6
    assert alerts == [launcher.MSG_NO_WINDOW]


def test_a_window_reopened_during_the_grace_keeps_the_sessions_and_the_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _reaching_the_server(monkeypatch, _cfg(tmp_path))
    children, _ = _fake_edge(monkeypatch)
    holder: list[WindowManager] = []
    closed: list[bool] = []

    class ReopeningManager(launcher.SessionManager):
        async def settle(self, grace_s: float, until: Callable[[], bool] | None = None) -> None:
            await holder[0].open()  # a second launch, while the grace is running
            # Then the real loop, so this covers the predicate rather than stubbing it out:
            # with a window back, settle must return at once instead of spending the grace
            # denying cards the user is sitting there to answer.
            await super().settle(grace_s, until)

        async def close_all(self) -> None:
            closed.append(True)

    monkeypatch.setattr(launcher, "SessionManager", ReopeningManager)

    assert launcher.main([]) == 0
    windows = calls["create_app"]["windows"]
    holder.append(windows)
    (uvi,) = FakeServer.instances

    async def close_then_reopen() -> None:
        await windows.start()
        children[0].exited.set()
        for _ in range(20):
            await asyncio.sleep(0)
        assert windows.count == 1
        await windows.stop()

    asyncio.run(close_then_reopen())
    assert closed == []
    assert uvi.should_exit is False


def test_a_task_keeps_the_app_after_the_last_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """While the manager retains a chat (a task still working), the last window closing
    settles, closes only the other chats and holds for up to TASK_HOLD_S. Once nothing is
    retained, every session closes and only then does the server stop."""
    calls = _reaching_the_server(monkeypatch, _cfg(tmp_path))
    children, _ = _fake_edge(monkeypatch)
    order: list[str] = []
    limits: list[float] = []
    stopping: list[bool] = []  # should_exit as close_all began
    retained = [True]

    class TaskManager(launcher.SessionManager):
        async def settle(self, grace_s: float, until: Callable[[], bool] | None = None) -> None:
            order.append("settle")
            await super().settle(grace_s, until)

        def retaining(self) -> bool:
            return retained[0]

        async def close_unretained(self) -> None:
            order.append("close_unretained")
            await super().close_unretained()

        async def hold(
            self, limit_s: float, until: Callable[[], bool], *, poll_s: float = 1.0
        ) -> None:
            order.append("hold")
            limits.append(limit_s)
            await super().hold(limit_s, until, poll_s=0.01)  # the real loop, polled faster

        async def close_all(self) -> None:
            order.append("close_all")
            stopping.append(FakeServer.instances[-1].should_exit)
            await super().close_all()

    monkeypatch.setattr(launcher, "SessionManager", TaskManager)

    assert launcher.main([]) == 0
    windows = calls["create_app"]["windows"]
    (uvi,) = FakeServer.instances
    held: list[object] = []

    async def close_the_window_then_let_the_task_go() -> None:
        await windows.start()
        children[0].exited.set()
        async with asyncio.timeout(5.0):
            while "hold" not in order and not uvi.should_exit:
                await asyncio.sleep(0.005)
        await asyncio.sleep(0.05)  # several polls of the hold, the task still retained
        held.extend([uvi.should_exit, list(order)])
        retained[0] = False  # nothing is retained any more
        async with asyncio.timeout(5.0):
            while not uvi.should_exit:
                await asyncio.sleep(0.005)
        await windows.stop()

    asyncio.run(close_the_window_then_let_the_task_go())
    assert held == [False, ["settle", "close_unretained", "hold"]]  # the task kept the app
    assert order == ["settle", "close_unretained", "hold", "close_all"]
    assert stopping == [False] and uvi.should_exit is True  # close_all, then should_exit
    assert limits == [launcher.TASK_HOLD_S] and launcher.TASK_HOLD_S == 12 * 3600


def test_a_window_back_during_the_hold_serves_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second launch while a retained chat holds the app: the hold ends at once and the app
    serves on, with no further close and the server still running."""
    calls = _reaching_the_server(monkeypatch, _cfg(tmp_path))
    children, _ = _fake_edge(monkeypatch)
    order: list[str] = []

    class TaskManager(launcher.SessionManager):
        def retaining(self) -> bool:
            return True  # the task is still working when the window comes back

        async def close_unretained(self) -> None:
            order.append("close_unretained")
            await super().close_unretained()

        async def hold(
            self, limit_s: float, until: Callable[[], bool], *, poll_s: float = 1.0
        ) -> None:
            order.append("hold")
            await super().hold(limit_s, until, poll_s=0.01)
            order.append("held")

        async def close_all(self) -> None:
            order.append("close_all")

    monkeypatch.setattr(launcher, "SessionManager", TaskManager)

    assert launcher.main([]) == 0
    windows = calls["create_app"]["windows"]
    (uvi,) = FakeServer.instances
    reopened: list[bool] = []

    async def close_then_reopen_during_the_hold() -> None:
        await windows.start()
        children[0].exited.set()
        async with asyncio.timeout(5.0):
            while "hold" not in order and "close_all" not in order:
                await asyncio.sleep(0.005)
        await asyncio.sleep(0.05)  # the hold is polling
        reopened.append(await windows.open())  # a second launch, during the hold
        async with asyncio.timeout(5.0):
            while "held" not in order and "close_all" not in order:
                await asyncio.sleep(0.005)
        for _ in range(20):
            await asyncio.sleep(0)
        reopened.append(windows.count == 1)
        await windows.stop()

    asyncio.run(close_then_reopen_during_the_hold())
    assert reopened == [True, True]
    assert order == ["close_unretained", "hold", "held"]  # no close_all: it serves on
    assert uvi.should_exit is False


def test_no_edge_is_reported_in_a_message_box_and_returns_5(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, alerts: list[str]
) -> None:
    calls = _reaching_the_server(monkeypatch, _cfg(tmp_path, edge=None))

    assert launcher.main([]) == 5
    assert alerts == [launcher.MSG_NO_EDGE]
    assert "create_app" not in calls and FakeServer.instances == []
    _assert_lock_is_free()


def test_a_second_launch_wakes_the_running_instance_and_exits_0(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(launcher.config, "load", lambda env: cfg)
    monkeypatch.setattr(launcher, "_configure_logging", lambda log_dir: None)
    monkeypatch.setattr(launcher.config, "billing_problems", lambda c, env: [])
    _forbid_serving(monkeypatch)
    _acknowledge_wakes(monkeypatch)

    running = winproc.single_instance(launcher.MUTEX_NAME)  # "the first instance"
    assert running is not None
    signal = winproc.Signal.create(launcher.OPEN_EVENT)
    try:
        assert launcher.main([]) == 0
        assert signal.wait(0) is True  # it asked for a window
    finally:
        signal.close()
        running.release()


def test_a_delivered_wake_without_admission_reports_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, alerts: list[str]
) -> None:
    _stop_before_serving(monkeypatch, _cfg(tmp_path))
    _forbid_serving(monkeypatch)
    monkeypatch.setattr(launcher, "_lock_when_free", lambda *args: None)
    request, reply, caller = Mock(), Mock(), Mock()
    reply.wait.return_value = False
    monkeypatch.setattr(
        launcher.winproc,
        "single_instance",
        lambda name: caller if name == launcher.WAKE_MUTEX else None,
    )
    monkeypatch.setattr(
        launcher.winproc.Signal,
        "open",
        lambda name: request if name == launcher.OPEN_EVENT else None,
    )
    monkeypatch.setattr(launcher.winproc.Signal, "create", lambda name: reply)
    assert launcher.main([]) == 3
    assert alerts == [launcher.MSG_NOT_LISTENING]
    request.set.assert_called_once()


def test_a_second_launch_takes_over_when_the_holder_goes_away(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The holder was shutting down: its watcher is gone, so the wake is lost, but the lock
    frees within WAKE_WAIT_S and this launch serves instead of exiting 0 with nothing open."""
    calls = _reaching_the_server(monkeypatch, _cfg(tmp_path))
    monkeypatch.setattr(launcher, "WAKE_WAIT_S", 2.0)
    running = winproc.single_instance(launcher.MUTEX_NAME)
    assert running is not None
    threading.Timer(0.05, running.release).start()

    assert launcher.main([]) == 0
    assert "create_app" in calls and FakeServer.instances[0].ran
    _assert_lock_is_free()


def test_a_second_launch_that_cannot_wake_the_first_says_so_and_returns_3(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, alerts: list[str]
) -> None:
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(launcher.config, "load", lambda env: cfg)
    monkeypatch.setattr(launcher, "_configure_logging", lambda log_dir: None)
    monkeypatch.setattr(launcher.config, "billing_problems", lambda c, env: [])
    _forbid_serving(monkeypatch)

    running = winproc.single_instance(launcher.MUTEX_NAME)
    assert running is not None
    try:
        assert launcher.main([]) == 3  # no signal to set: the first is not listening
        assert alerts == [launcher.MSG_NOT_LISTENING]
    finally:
        running.release()


def test_a_taken_port_is_reported_in_a_message_box_before_anything_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, alerts: list[str]
) -> None:
    """A real listener on the port, not a stubbed check: the refusal has to come from the bind
    the launch actually attempts, since that bind is the whole of the check."""
    calls = _reaching_the_server(monkeypatch, _cfg(tmp_path))
    monkeypatch.setattr(launcher.winproc, "spawn", _forbid("winproc.spawn"))

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as other_program:
        other_program.bind((config.HOST, config.PORT))
        other_program.listen()
        assert launcher.main([]) == 4
    assert alerts == [launcher.MSG_PORT_TAKEN.format(port=config.PORT)]
    assert str(config.PORT) in alerts[0]
    assert "create_app" not in calls and FakeServer.instances == []
    _assert_lock_is_free()


def test_the_launcher_binds_the_port_itself_and_hands_the_socket_to_uvicorn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Probing the port and letting uvicorn bind it seconds later left a gap. uvicorn
    opens the first Edge window from the ASGI lifespan *before* loop.create_server and turns a
    bind failure there into sys.exit(3) — not an Exception, so it walks past main()'s handler
    and the user gets a window that flashes open and vanishes with no message box at all. The
    check and the bind have to be one operation, and the socket has to be given back on the way
    out."""
    calls = _reaching_the_server(monkeypatch, _cfg(tmp_path))

    assert launcher.main([]) == 0

    (uvi,) = FakeServer.instances
    assert uvi.bound == [(config.HOST, config.PORT)]  # uvicorn served the socket _serve bound
    assert calls["create_app"]  # which happened before any of this was built
    assert uvi.sockets is not None and uvi.sockets[0].fileno() == -1  # and closed on the way out
    assert launcher._port_free(config.HOST, config.PORT) is True


def test_listen_holds_the_port_until_it_is_closed() -> None:
    """No SO_REUSEADDR on Windows (POSIX sets it, and a live listener still refuses the second
    bind there), and listening from the start: the window opens before uvicorn serves, so
    its first request needs a backlog already waiting for it rather than a refused connection."""
    port = _spare_port()
    owned = launcher._listen(config.HOST, port)
    assert owned is not None
    try:
        assert launcher._listen(config.HOST, port) is None  # really exclusive, second bind fails
        with socket.create_connection((config.HOST, port), timeout=1.0):
            pass  # accepted into the backlog, so nothing is refused before uvicorn takes over
    finally:
        owned.close()
    again = launcher._listen(config.HOST, port)
    assert again is not None
    again.close()


def test_port_free_sees_a_listener_and_the_port_again_once_it_is_gone() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind((config.HOST, 0))
        listener.listen()
        port = listener.getsockname()[1]
        assert launcher._port_free(config.HOST, port) is False
    assert launcher._port_free(config.HOST, port) is True


def test_the_lock_is_released_even_when_the_server_crashes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _reaching_the_server(monkeypatch, _cfg(tmp_path), server_cls=CrashingServer)

    with pytest.raises(RuntimeError, match="the server crashed"):
        launcher.main([])
    _assert_lock_is_free()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX: no kill-on-close Job")
@pytest.mark.parametrize("recovery", [False, True])
def test_a_server_that_ends_without_its_lifespan_stop_still_kills_the_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, recovery: bool
) -> None:
    """A crash in uvi.run() can end the process before the app's own lifespan gets to run its
    stop; on macOS and Linux nothing else then kills a browser it leaves behind. (Uvicorn
    itself, unable to serve the socket, raises OSError instead, with that stop already run.)"""
    assert sys.platform != "win32"
    cfg = _cfg(tmp_path)
    calls = _reaching_the_server(monkeypatch, cfg)
    if recovery:
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        (cfg.data_dir / "app.sqlite3").write_bytes(b"not a database")
    children, _ = _fake_edge(monkeypatch)

    def run(self: FakeServer, sockets: list[socket.socket] | None = None) -> None:
        windows = calls["create_recovery_app" if recovery else "create_app"]["windows"]
        assert asyncio.run(windows.open())
        raise SystemExit(1)

    monkeypatch.setattr(FakeServer, "run", run)

    with pytest.raises(SystemExit):
        launcher.main([])

    assert children[0].killed and children[0].released
    _assert_lock_is_free()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX stop signals")
@pytest.mark.parametrize("recovery", [False, True])
def test_a_signal_stop_first_stops_this_launchs_windows_opening(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, recovery: bool
) -> None:
    """The hook the server runs before any stop drains it reaches this launch's own window
    manager, in the ordinary launch and in the recovery shell."""
    assert sys.platform != "win32"
    cfg = _cfg(tmp_path)
    calls = _reaching_the_server(monkeypatch, cfg)
    if recovery:
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        (cfg.data_dir / "app.sqlite3").write_bytes(b"not a database")
    _fake_edge(monkeypatch)
    opened: list[bool] = []

    def run(self: FakeServer, sockets: list[socket.socket] | None = None) -> None:
        windows = calls["create_recovery_app" if recovery else "create_app"]["windows"]
        self.before_exit()
        opened.append(asyncio.run(windows.open()))

    monkeypatch.setattr(FakeServer, "run", run)

    assert launcher.main([]) == 0
    assert opened == [False]


# --- launcher services: readiness probes, startup failures and wiring --------------------------


def test_identity_probes_follow_the_git_readiness_after_a_later_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The workspace service used to get a cached launch-time `git --version` answer, so a git
    installed (or removed) after the launch was seen by the readiness screen but never by the
    identity probes: `git_available` reads `Readiness.git_state`, which a `git` check updates."""
    calls = _reaching_the_server(monkeypatch, _cfg(tmp_path))
    monkeypatch.setattr(launcher.rails, "_has_git_scope_override", lambda: False)

    assert launcher.main([]) == 0

    created = calls["create_app"]
    readiness, spaces = created["readiness"], created["workspaces"]
    assert readiness.git_state == "missing" and spaces._git_available() is False
    readiness.observe_git("2.40.0")
    assert spaces._git_available() is True
    readiness.observe_git("")
    assert spaces._git_available() is False


def _raise(exc: Exception) -> Callable[..., Any]:
    def raiser(*_args: Any, **_kwargs: Any) -> Any:
        raise exc

    return raiser


@pytest.mark.parametrize(
    ("owner", "method", "exc"),
    [
        (launcher.WorkspaceService, "contains_data_dir", StateError("unsafe_path")),
        (launcher.WorkspaceService, "contains_data_dir", protection.ProtectionError()),
        (launcher.SettingsService, "chat_defaults", SettingsError("read_only")),
    ],
)
def test_a_startup_state_failure_after_the_store_opens_says_so_and_stops(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    alerts: list[str],
    caplog: pytest.LogCaptureFixture,
    owner: type,
    method: str,
    exc: Exception,
) -> None:
    """The store opened, then a startup read (the registrations or the chat defaults) failed: a
    lock, an unsafe path, a DPAPI refusal. That used to be a traceback out of main(), which under
    pythonw reaches nobody; it is a box naming the data folder and exit 2 now."""
    cfg = _cfg(tmp_path)
    calls = _reaching_the_server(monkeypatch, cfg)
    monkeypatch.setattr(owner, method, _raise(exc))

    with caplog.at_level(logging.ERROR):
        assert launcher.main([]) == 2

    assert alerts == [launcher.MSG_STARTUP_STATE.format(data=cfg.data_dir)]
    assert "startup state failed" in caplog.text
    assert "create_app" not in calls and FakeServer.instances == []
    _assert_lock_is_free()


def test_a_scratch_root_that_cannot_be_created_privately_names_that_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, alerts: list[str]
) -> None:
    """scratch_root() creates the chat-only folder privately; a ProtectionError from that is the
    same "this folder cannot be prepared" as the link refusal and gets the same box, not a
    traceback."""
    cfg = _cfg(tmp_path)
    calls = _reaching_the_server(monkeypatch, cfg)
    monkeypatch.setattr(
        launcher.WorkspaceService, "resolve_active", _raise(protection.ProtectionError())
    )

    assert launcher.main([]) == 2

    scratch = launcher.workspaces.scratch_path(cfg.data_dir)
    assert alerts == [launcher.MSG_BAD_SCRATCH.format(scratch=scratch)]
    assert "create_app" not in calls


def test_an_unreadable_settings_store_hides_previews_and_still_renders_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both adapters read settings lazily, after the launch: a SettingsError there used to
    escape into an identity probe or the diagnostics route. Previews default to hidden (the
    safer answer) and the document renders with `settings` null, which is what the user needs
    to read to find out that settings cannot be read."""
    calls = _reaching_the_server(monkeypatch, _cfg(tmp_path))
    monkeypatch.setattr(launcher.SettingsService, "read", _raise(SettingsError("read_only")))

    assert launcher.main([]) == 0

    created = calls["create_app"]
    assert created["workspaces"]._hide_previews() is True
    document = created["diagnostics_document"]()
    assert document["settings"] is None and document["launch_id"]


def test_an_unwritable_log_directory_gets_its_own_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, alerts: list[str]
) -> None:
    """A log folder that cannot be created (here: the name is taken by a file) is not a data
    folder in a forbidden place, and the box used to say it was, sending the user to move a
    folder that was allowed where it is. The box names the folder, since there is no log."""
    data = tmp_path / "data"
    data.mkdir()
    (data / "logs").write_bytes(b"not a directory")
    monkeypatch.setenv("VIKI_DATA_DIR", str(data))
    monkeypatch.setattr(launcher.config, "load", _forbid("config.load"))
    _forbid_serving(monkeypatch)

    with _fresh_root_handlers():
        assert launcher.main([]) == 2

    assert alerts == [launcher.MSG_NO_LOG_DIR.format(logs=data / "logs")]
    assert alerts != [launcher.MSG_BAD_DATA_DIR]


def test_scratch_in_use_reads_the_session_table_on_the_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The locations and reset services call `scratch_in_use` from anyio worker threads, while
    the session table is mutated on the loop as sessions end; iterating it from the worker
    raised "dictionary changed size during iteration" at the worst moment. The read hops to
    the loop thread, which is the only thread that may touch the table."""
    calls = _reaching_the_server(monkeypatch, _cfg(tmp_path))
    on_loop: list[bool] = []

    def live(self: Any) -> list[Any]:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            on_loop.append(False)
        else:
            on_loop.append(True)
        return []

    monkeypatch.setattr(launcher.SessionManager, "live", live)

    assert launcher.main([]) == 0

    locations = calls["create_app"]["data"][0]
    document = asyncio.run(locations.document())
    assert on_loop == [True]
    assert "in_use" in str(document)


def test_a_refused_vault_override_is_judged_and_logged_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """config.load() canonicalises VIKI_VAULT and records the refusal in Config.vault_problem;
    the workspace service used to canonicalise the same value again and log the same refusal a
    second time. The launcher reads the config verdict and withholds a refused override."""
    real_load = config.load
    cfg = _cfg(tmp_path)
    calls = _reaching_the_server(monkeypatch, cfg)
    monkeypatch.setattr(launcher.config, "load", real_load)
    inside = tmp_path / "data" / "vault"  # inside the data directory: denied_location
    inside.mkdir(parents=True)
    monkeypatch.setenv("VIKI_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("VIKI_VAULT", str(inside))
    monkeypatch.setenv("VIKI_CLAUDE_CLI", str(tmp_path / "no-such-claude.exe"))
    monkeypatch.setenv("VIKI_EDGE", str(tmp_path / "msedge.exe"))

    with caplog.at_level(logging.WARNING):
        assert launcher.main([]) == 0

    assert caplog.text.count("VIKI_VAULT ignored") == 1
    created = calls["create_app"]
    assert created["config"].vault_problem == "denied_location"
    assert created["config"].vault == launcher.workspaces.scratch_root(tmp_path / "data")


def test_build_services_wires_defaults_and_the_spawn_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The launcher's service assembly, called on its own.

    Two wires cannot be seen from `create_app`'s keyword arguments and are the reason this
    function exists as a function: the settings service tells `AppState` about a committed
    `chat.*` change, and the provider argv is empty unless `may_spawn()` allows a spawn — which
    is what keeps the end-to-end harness and the launcher honest about the same gate.
    """
    monkeypatch.setattr(launcher, "git_version", lambda: "")
    monkeypatch.setattr(launcher, "cli_version", lambda argv: "9.9.9")
    cfg = replace(_cfg(tmp_path), cli=tmp_path / "claude.exe")
    (tmp_path / "vault").mkdir()
    state = AppState(cfg.state_file, Prefs())
    spaces = launcher.WorkspaceService(
        state.store,
        env={},
        data_dir=cfg.data_dir,
        home=tmp_path / "home",
        install_dir=tmp_path / "install",
        git_available=lambda: False,
        hide_previews=lambda: False,
    )
    active = spaces.resolve_active()
    readiness = launcher.Readiness(config=cfg, launch_id="launch", git_available=lambda: False)
    launch_env = {"ANTHROPIC_CONFIG_DIR": str(tmp_path / "anthropic")}
    defaults_envs: list[Mapping[str, str]] = []

    def defaults(home: Path, vault: Path | None, *, env: Mapping[str, str]) -> Prefs:
        assert (home, vault) == (cfg.claude_home, cfg.vault)
        defaults_envs.append(env)
        return Prefs()

    monkeypatch.setattr(launcher, "user_defaults", defaults)

    services = launcher.build_services(
        cfg,
        launch_env,
        state=state,
        spaces=spaces,
        active=active,
        readiness=readiness,
        launch_id="launch",
        log_file=cfg.data_dir / "logs" / launcher.LOG_NAME,
        scratch_in_use=lambda: False,
    )

    assert services.argv_cli == (str(cfg.cli),)
    assert services.version == "9.9.9"
    # A committed chat default reaches the session defaults without a restart.
    services.settings.patch(str(uuid.uuid4()), 0, {"chat.default_effort": "low"})
    assert state.defaults.effort == "low"

    # The same assembly with the one fact that closes the spawn gate: no argv, no version probe.
    blocked = launcher.Readiness(
        config=replace(cfg, claude_config_dir_set=True), launch_id="launch", git_available=bool
    )
    gated = launcher.build_services(
        cfg,
        launch_env,
        state=state,
        spaces=spaces,
        active=active,
        readiness=blocked,
        launch_id="launch",
        log_file=cfg.data_dir / "logs" / launcher.LOG_NAME,
        scratch_in_use=lambda: False,
    )
    assert gated.argv_cli == ()
    assert gated.version == ""
    assert defaults_envs and all(env is launch_env for env in defaults_envs)


def test_initial_session_defaults_receive_the_launch_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _cfg(tmp_path)
    env = {"ANTHROPIC_CONFIG_DIR": str(tmp_path / "anthropic")}
    defaults = Mock(return_value=Prefs())
    monkeypatch.setattr(launcher, "user_defaults", defaults)
    monkeypatch.setattr(launcher, "git_version", lambda: "")
    monkeypatch.setattr(launcher, "AppState", _raise(_Stop()))

    with pytest.raises(_Stop):
        launcher._serve_on(
            cfg,
            cfg.edge or tmp_path / "browser",
            env,
            args=["--dev"],
            signal=Mock(),
            later=[],
            sock=Mock(),
        )

    defaults.assert_called_once_with(cfg.claude_home, cfg.vault, env=env)


def test_build_services_hands_one_deny_list_to_the_exports_and_the_app(tmp_path: Path) -> None:
    """Identity, not equality: the export destination check and the knowledge routes must judge
    a path against the very same deny list."""
    cfg = replace(_cfg(tmp_path), cli=tmp_path / "claude.exe")
    (tmp_path / "vault").mkdir()
    state = AppState(cfg.state_file, Prefs())
    spaces = launcher.WorkspaceService(
        state.store,
        env={},
        data_dir=cfg.data_dir,
        home=tmp_path / "home",
        install_dir=tmp_path / "install",
        git_available=lambda: False,
        hide_previews=lambda: False,
    )
    services = launcher.build_services(
        cfg,
        {},
        state=state,
        spaces=spaces,
        active=spaces.resolve_active(),
        readiness=launcher.Readiness(config=cfg, launch_id="launch", git_available=lambda: False),
        launch_id="launch",
        log_file=cfg.data_dir / "logs" / launcher.LOG_NAME,
        scratch_in_use=lambda: False,
    )
    exports = next(s for s in services.data if isinstance(s, launcher.ExportService))
    assert exports._denied_roots is services.denied_roots  # pyright: ignore[reportPrivateUsage]
    assert exports._destination_guard is spaces.registration_lock
