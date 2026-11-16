"""Preview runs: a command's dev server in a trusted code folder, its port ownership read from the
listener table and its Job, Open, Stop and the refusals.

Process tests skip off Windows and poll real OS state (a server's listen, a tree's end) under
`HANG_GUARD_S`; each ends its trees through the service or its Job. The other tests use fake
shells, whose `wait` returns when `kill_tree` is called, and run on every OS."""

from __future__ import annotations

import asyncio
import socket
import sys
import threading
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import pytest

from tests.app.test_workspaces import COMMAND, protected_at, service_at, store_at
from tests.workspace_code.test_workspaces_code import trusted
from viki_agent import winproc
from viki_agent.code import previews
from viki_agent.code.code_checks import CodeRunError
from viki_agent.code.preview_windows import PreviewWindows
from viki_agent.code.previews import MAX_ENDED_KEPT, SETTLE_WAIT_S, PreviewService, PreviewView
from viki_agent.code.session_roots import SessionRoots
from viki_agent.session import now_iso
from viki_agent.storage import SessionStore
from viki_agent.workspaces import WorkspaceService

HANG_GUARD_S = 10.0
windows_only = pytest.mark.skipif(sys.platform != "win32", reason="previews run on Windows only")
WORKTREE = "00000000-0000-4000-8000-000000000001"
UNKNOWN = "88888888-8888-4888-8888-888888888888"
PING = "ping -n 30 127.0.0.1 >nul"


@pytest.fixture
def windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """The refusal off Windows is its own test; the tests with fake shells run everywhere."""
    monkeypatch.setattr(previews, "PREVIEWS_SUPPORTED", True)


@dataclass
class FakeWindows:
    opened: list[dict[str, Any]] = field(default_factory=list)
    closed: list[str] = field(default_factory=list)

    async def open(
        self,
        url: str,
        *,
        allow: tuple[str, ...],
        owner: str,
        on_closed: Callable[[], None] | None = None,
    ) -> None:
        self.opened.append({"url": url, "allow": allow, "owner": owner})

    def close_owner(self, owner: str) -> None:
        self.closed.append(owner)


@dataclass
class HeldWindows(FakeWindows):
    """Holds each open until `release` is set."""

    entered: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)

    async def open(
        self,
        url: str,
        *,
        allow: tuple[str, ...],
        owner: str,
        on_closed: Callable[[], None] | None = None,
    ) -> None:
        self.entered.set()
        await self.release.wait()
        await super().open(url, allow=allow, owner=owner, on_closed=on_closed)


class FakeJob:
    def __init__(self) -> None:
        self.members: set[int] = set()

    def holds(self, pid: int) -> bool:
        return pid in self.members


class FakeShell:
    """A command that runs until `kill_tree`, or exits at once with `code`."""

    pid = 4242

    def __init__(self, code: int | None = None) -> None:
        self.job = FakeJob()
        self.code = code
        self.killed = threading.Event()
        self.released = False

    def read(self) -> bytes:
        return b""

    def wait(self, _timeout: float | None) -> int | None:
        if self.code is not None:
            return self.code
        self.killed.wait(HANG_GUARD_S)
        return 1

    def kill_tree(self) -> None:
        self.killed.set()

    def release(self) -> None:
        self.released = True


@dataclass
class Rig:
    store: SessionStore
    workspaces: WorkspaceService
    roots: SessionRoots
    workspace_id: str
    folder: Path
    windows: FakeWindows = field(default_factory=FakeWindows)
    starts: list[Any] = field(default_factory=list)

    def service(
        self,
        *,
        start: Callable[..., Any] | None = None,
        listeners: Callable[[int], frozenset[int]] | None = None,
    ) -> PreviewService:
        return PreviewService(
            self.store,
            self.workspaces,
            self.roots,
            cast(PreviewWindows, self.windows),
            start=start or self.record,
            listeners=listeners,
        )

    def record(self, command: str, *, cwd: Path, env: dict[str, str]) -> winproc.ShellRun:
        shell = winproc.start_shell(command, cwd=cwd, env=env)
        self.starts.append(shell)
        return shell

    def fake(self, code: int | None = None) -> Callable[..., FakeShell]:
        def start(command: str, *, cwd: Path, env: dict[str, str]) -> FakeShell:
            shell = FakeShell(code)
            self.starts.append(shell)
            return shell

        return start

    def worktree(self, *, state: str = "ready") -> Path:
        path = self.folder.parent / "w"
        path.mkdir()
        stamp = now_iso()
        self.store.insert_worktree(
            {
                "worktree_id": WORKTREE,
                "workspace_id": self.workspace_id,
                "path": str(path),
                "branch": "viki/test",
                "base_oid": "a" * 40,
                "state": "creating",
                "created_at": stamp,
                "updated_at": stamp,
            },
            limit=32,
        )
        self.store.update_worktree_state(WORKTREE, "creating", state, stamp)
        return path

    def revoke(self) -> None:
        self.workspaces.set_policy(str(uuid.uuid4()), self.workspace_id, 2, "existing_readonly", [])


def _rig(tmp_path: Path) -> Rig:
    folder = tmp_path / "repo"
    folder.mkdir()
    store = store_at(tmp_path)
    workspaces = service_at(tmp_path, store)
    workspace_id = trusted(workspaces, folder)
    roots = SessionRoots(store, workspaces, exists=lambda _sid: False, actor=lambda _sid: None)
    return Rig(store, workspaces, roots, workspace_id, folder)


def _none(_port: int) -> frozenset[int]:
    return frozenset()


def _forbidden(*_args: object, **_kwargs: object) -> Any:
    raise AssertionError("called")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def _refusal(call: Awaitable[Any]) -> str:
    with pytest.raises(CodeRunError) as refused:
        await call
    return refused.value.code


async def _ended(service: PreviewService, preview_id: str) -> None:
    await asyncio.wait_for(service._previews[preview_id].done.wait(), HANG_GUARD_S)


async def _view(service: PreviewService, rig: Rig, preview_id: str) -> PreviewView:
    [view] = [v for v in await service.list(rig.workspace_id) if v.preview_id == preview_id]
    return view


async def _until_state(
    service: PreviewService, rig: Rig, preview_id: str, state: str
) -> PreviewView:
    async with asyncio.timeout(HANG_GUARD_S):
        while (view := await _view(service, rig, preview_id)).state != state:
            await asyncio.sleep(0.05)  # a server's listen is a real OS event; poll its state
    return view


async def _until_dead(pid: int) -> None:
    async with asyncio.timeout(HANG_GUARD_S):
        while winproc.process_start_time(pid) is not None:
            await asyncio.sleep(0.05)  # kill-on-close is asynchronous; poll the real OS state


# --- real processes (Windows) -------------------------------------------------------------------


@windows_only
async def test_a_preview_listens_in_its_job_and_stops_with_its_tree(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    service = rig.service()
    port = _free_port()
    command = f'"{sys.executable}" -m http.server {port} --bind 127.0.0.1'
    started = await service.start(rig.workspace_id, None, command, port)
    try:
        assert (started.command, started.port, started.ended_at) == (command, port, None)
        view = await _until_state(service, rig, started.preview_id, "listening")
        assert view.listener_pid is not None and view.exit_code is None
        [shell] = rig.starts
        assert shell.job.holds(view.listener_pid) and view.pid == shell.pid
        await service.open(started.preview_id)
        assert rig.windows.opened == [
            {
                "url": f"http://localhost:{port}/",
                "allow": (f"localhost:{port}", f"127.0.0.1:{port}", f"[::1]:{port}"),
                "owner": started.preview_id,
            }
        ]
        await service.stop(started.preview_id)
        await _ended(service, started.preview_id)
    finally:
        await service.close()
    view = await _view(service, rig, started.preview_id)
    assert (view.state, view.exit_code, view.listener_pid) == ("stopped", None, None)
    assert view.ended_at is not None
    assert rig.windows.closed == [started.preview_id]
    await _until_dead(shell.pid)
    assert winproc.tcp_listeners(port) == frozenset()


@windows_only
async def test_an_exit_is_recorded_with_its_code_and_output(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    service = rig.service()
    started = await service.start(rig.workspace_id, None, "echo viki& exit /b 4", _free_port())
    await _ended(service, started.preview_id)
    view, part, done = await service.output(started.preview_id, 0)
    assert (view.state, view.exit_code, view.listener_pid) == ("exited", 4, None)
    assert (part.text, done) == ("viki\r\n", True)
    assert rig.windows.closed == [started.preview_id]


@windows_only
async def test_a_port_another_program_holds_is_refused_without_a_start(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        port = sock.getsockname()[1]
        service = rig.service(start=_forbidden)
        assert await _refusal(service.start(rig.workspace_id, None, "exit /b 0", port)) == (
            "port_taken"
        )
    assert await service.list(rig.workspace_id) == []


@windows_only
async def test_a_listener_outside_the_preview_is_port_taken_and_not_opened(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    service = rig.service()
    port = _free_port()
    started = await service.start(rig.workspace_id, None, PING, port)
    try:
        assert (await _view(service, rig, started.preview_id)).state == "starting"
        assert await _refusal(service.open(started.preview_id)) == "not_listening"
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", port))
            sock.listen()
            view = await _view(service, rig, started.preview_id)
            assert (view.state, view.listener_pid, view.ended_at) == ("port_taken", None, None)
            assert await _refusal(service.open(started.preview_id)) == "port_taken"
    finally:
        await service.close()
    assert rig.windows.opened == []


@windows_only
async def test_a_preview_sees_no_claude_or_anthropic_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_G03_PROBE", "x")
    monkeypatch.setenv("CLAUDE_CODE_G03_PROBE", "x")
    monkeypatch.setenv("VIKI_G03_PROBE", "x")
    rig = _rig(tmp_path)
    service = rig.service()
    started = await service.start(rig.workspace_id, None, "set", _free_port())
    await _ended(service, started.preview_id)
    _view_, part, _done = await service.output(started.preview_id, 0)
    names = [line.partition("=")[0].upper() for line in part.text.splitlines()]
    assert "VIKI_G03_PROBE" in names
    assert not [name for name in names if name.startswith(("ANTHROPIC_", "CLAUDE_CODE_"))]


@windows_only
async def test_close_ends_every_tree(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    service = rig.service()
    ids = [
        (await service.start(rig.workspace_id, None, PING, _free_port())).preview_id
        for _ in range(2)
    ]
    began = time.monotonic()
    await service.close()
    assert time.monotonic() - began < SETTLE_WAIT_S
    assert {v.state for v in await service.list(rig.workspace_id)} == {"stopped"}
    assert sorted(rig.windows.closed) == sorted(ids)
    for shell in rig.starts:
        await _until_dead(shell.pid)
    assert await _refusal(service.start(rig.workspace_id, None, PING, _free_port())) == (
        "unavailable"
    )


# --- fake shells (every OS) ---------------------------------------------------------------------


@pytest.mark.usefixtures("windows")
async def test_a_live_port_and_a_third_preview_are_refused(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    service = rig.service(start=rig.fake(), listeners=_none)
    await service.start(rig.workspace_id, None, PING, 5000)
    assert await _refusal(service.start(rig.workspace_id, None, PING, 5000)) == "port_in_use"
    await service.start(rig.workspace_id, None, PING, 5001)
    assert await _refusal(service.start(rig.workspace_id, None, PING, 5002)) == "preview_limit"
    assert len(rig.starts) == 2
    await service.close()


@pytest.mark.usefixtures("windows")
async def test_a_second_start_on_a_listening_previews_port_is_port_in_use(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    table: set[int] = set()
    service = rig.service(start=rig.fake(), listeners=lambda _p: frozenset(table))
    preview = await service.start(rig.workspace_id, None, PING, 5000)
    table.add(20)
    rig.starts[0].job.members.add(20)
    assert (await _view(service, rig, preview.preview_id)).state == "listening"
    # The listener is the first preview's own, so the answer is not port_taken.
    assert await _refusal(service.start(rig.workspace_id, None, PING, 5000)) == "port_in_use"
    assert len(rig.starts) == 1
    await service.close()


@pytest.mark.usefixtures("windows")
async def test_a_listener_held_by_the_job_is_listening_with_the_smallest_pid(
    tmp_path: Path,
) -> None:
    rig = _rig(tmp_path)
    table: set[int] = set()  # the start itself refuses a port with any listener
    service = rig.service(start=rig.fake(), listeners=lambda _p: frozenset(table))
    preview = await service.start(rig.workspace_id, None, PING, 5000)
    table.update({30, 20})
    rig.starts[0].job.members.update({30, 20})
    view = await _view(service, rig, preview.preview_id)
    assert (view.state, view.listener_pid) == ("listening", 20)
    await service.close()


@pytest.mark.usefixtures("windows")
async def test_a_foreign_listener_beside_the_previews_own_is_port_taken(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    table: set[int] = set()
    service = rig.service(start=rig.fake(), listeners=lambda _p: frozenset(table))
    preview = await service.start(rig.workspace_id, None, PING, 5000)
    # Say the preview's server listens on 127.0.0.1 and another program on [::1]: the window's
    # `localhost` could reach either.
    table.update({20, 99})
    rig.starts[0].job.members.add(20)
    view = await _view(service, rig, preview.preview_id)
    assert (view.state, view.listener_pid) == ("port_taken", None)
    assert await _refusal(service.open(preview.preview_id)) == "port_taken"
    assert rig.windows.opened == []
    await service.close()


@pytest.mark.usefixtures("windows")
async def test_a_window_that_opens_as_its_preview_ends_is_closed(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    held = HeldWindows()
    rig.windows = held
    table: set[int] = set()
    service = rig.service(start=rig.fake(), listeners=lambda _p: frozenset(table))
    preview = await service.start(rig.workspace_id, None, PING, 5000)
    table.add(20)
    rig.starts[0].job.members.add(20)
    opening = asyncio.create_task(service.open(preview.preview_id))
    await asyncio.wait_for(held.entered.wait(), HANG_GUARD_S)
    await service.stop(preview.preview_id)
    await _ended(service, preview.preview_id)
    assert held.closed == [preview.preview_id]  # the settle came before the window existed
    held.release.set()
    await opening
    assert len(held.opened) == 1
    assert held.closed == [preview.preview_id] * 2


@pytest.mark.usefixtures("windows")
async def test_two_starts_on_one_port_start_one_shell(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    both_in_the_gate = threading.Barrier(2)

    def gated(_port: int) -> frozenset[int]:
        both_in_the_gate.wait(HANG_GUARD_S)  # each start awaits its gate before the live checks
        return frozenset()

    service = rig.service(start=rig.fake(), listeners=gated)
    results = await asyncio.gather(
        service.start(rig.workspace_id, None, PING, 5000),
        service.start(rig.workspace_id, None, PING, 5000),
        return_exceptions=True,
    )
    started = [r for r in results if isinstance(r, PreviewView)]
    refused = [r.code for r in results if isinstance(r, CodeRunError)]
    assert (len(started), refused) == (1, ["port_in_use"])
    assert len(rig.starts) == 1
    await service.close()


@pytest.mark.usefixtures("windows")
async def test_bad_ports_and_commands_are_refused_before_any_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _rig(tmp_path)
    monkeypatch.setattr(rig.workspaces, "get", _forbidden)
    service = rig.service(start=_forbidden, listeners=_forbidden)
    ports: tuple[Any, ...] = (80, 1023, 65536, True, "5000", 5000.0)
    for port in ports:
        assert await _refusal(service.start(rig.workspace_id, None, PING, port)) == "invalid_port"
    assert await _refusal(service.start(rig.workspace_id, None, PING, 8765)) == "port_reserved"
    commands: tuple[Any, ...] = ("", " ", "a\nb", 5)
    for command in commands:
        assert await _refusal(service.start(rig.workspace_id, None, command, 8765)) == (
            "invalid_command"
        )


@pytest.mark.usefixtures("windows")
async def test_a_blocked_worktree_is_refused_without_a_start(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    rig.worktree()
    rig.roots.block(WORKTREE)
    service = rig.service(start=_forbidden, listeners=_none)
    assert await _refusal(service.start(rig.workspace_id, WORKTREE, PING, 5000)) == (
        "worktree_not_ready"
    )
    assert not service.in_worktree(WORKTREE)


@pytest.mark.usefixtures("windows")
async def test_untrusted_protected_and_vault_folders_are_refused_without_a_start(
    tmp_path: Path,
) -> None:
    rig = _rig(tmp_path)
    service = rig.service(start=_forbidden, listeners=_none)
    plain = tmp_path / "plain"
    plain.mkdir()
    untrusted, _ = rig.workspaces.register_existing(COMMAND, str(plain), kind="code")
    vault = tmp_path / "VIKI"
    vault.mkdir()
    (vault / "CLAUDE.md").write_text("# governed\n", encoding="utf-8")
    protected_at(rig.workspaces, vault)
    copy = tmp_path / "copy"
    copy.mkdir()
    (copy / "CLAUDE.md").write_text("# governed\n", encoding="utf-8")
    bound, _ = rig.workspaces.register_existing(COMMAND, str(copy), kind="code")
    assert bound.policy == "protected"
    notes = tmp_path / "notes"
    notes.mkdir()
    notes_vault, _ = rig.workspaces.register_existing(COMMAND, str(notes))
    for workspace_id, code in (
        (untrusted.workspace_id, "trust_required"),
        (bound.workspace_id, "trust_required"),
        (notes_vault.workspace_id, "unknown_workspace"),
        (UNKNOWN, "unknown_workspace"),
    ):
        assert await _refusal(service.start(workspace_id, None, PING, 5000)) == code
    assert await _refusal(service.list(notes_vault.workspace_id)) == "unknown_workspace"


@pytest.mark.usefixtures("windows")
async def test_trust_revoked_during_the_start_does_not_start(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    entered, release = threading.Event(), threading.Event()

    def held(_port: int) -> frozenset[int]:
        entered.set()
        release.wait(HANG_GUARD_S)
        return frozenset()

    service = rig.service(start=rig.fake(), listeners=held)
    start = asyncio.create_task(service.start(rig.workspace_id, None, PING, 5000))
    assert await asyncio.to_thread(entered.wait, HANG_GUARD_S)
    rig.revoke()
    release.set()
    assert await _refusal(start) == "trust_required"
    assert rig.starts == [] and service._previews == {}


@pytest.mark.usefixtures("windows")
async def test_a_start_that_fails_leaves_nothing_reserved(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    rig.worktree()
    calls: list[Path] = []

    def fail(command: str, *, cwd: Path, env: dict[str, str]) -> Any:
        calls.append(cwd)
        raise OSError("no")

    service = rig.service(start=fail, listeners=_none)
    assert await _refusal(service.start(rig.workspace_id, WORKTREE, PING, 5000)) == "start_failed"
    assert calls == [rig.folder.parent / "w"]
    assert not service.in_worktree(WORKTREE) and service._previews == {}
    assert await service.list(rig.workspace_id) == []


@pytest.mark.usefixtures("windows")
async def test_trust_revoked_after_a_start_still_lists_and_stops(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    service = rig.service(start=rig.fake(), listeners=_none)
    preview = await service.start(rig.workspace_id, None, PING, 5000)
    rig.revoke()
    assert (await _view(service, rig, preview.preview_id)).state == "starting"
    assert await _refusal(service.open(preview.preview_id)) == "trust_required"
    await service.stop(preview.preview_id)
    await _ended(service, preview.preview_id)
    view = await _view(service, rig, preview.preview_id)
    assert (view.state, view.exit_code) == ("stopped", None)
    assert rig.starts[0].released and rig.windows.opened == []


@pytest.mark.usefixtures("windows")
async def test_remove_refuses_a_live_preview_and_drops_an_ended_one(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    service = rig.service(start=rig.fake(), listeners=_none)
    preview = await service.start(rig.workspace_id, None, PING, 5000)
    assert await _refusal(service.remove(preview.preview_id)) == "preview_running"
    await service.stop(preview.preview_id)
    await _ended(service, preview.preview_id)
    assert await _refusal(service.open(preview.preview_id)) == "not_listening"
    await service.remove(preview.preview_id)
    assert await service.list(rig.workspace_id) == []
    for call in (
        service.open(preview.preview_id),
        service.stop(preview.preview_id),
        service.remove(preview.preview_id),
        service.output(preview.preview_id, 0),
    ):
        assert await _refusal(call) == "unknown_preview"


@pytest.mark.usefixtures("windows")
async def test_the_newest_four_ended_previews_are_kept(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    service = rig.service(start=rig.fake(0), listeners=_none)
    ids: list[str] = []
    for _ in range(MAX_ENDED_KEPT + 1):
        ids.append((await service.start(rig.workspace_id, None, "exit /b 0", 5000)).preview_id)
        await _ended(service, ids[-1])
    views = await service.list(rig.workspace_id)
    assert [v.preview_id for v in views] == ids[:0:-1]  # newest first, the oldest dropped
    assert {(v.state, v.exit_code) for v in views} == {("exited", 0)}


@pytest.mark.usefixtures("windows")
async def test_in_worktree_follows_a_live_preview(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    rig.worktree()
    service = rig.service(start=rig.fake(), listeners=_none)
    preview = await service.start(rig.workspace_id, WORKTREE, PING, 5000)
    assert preview.worktree_id == WORKTREE and service.in_worktree(WORKTREE)
    await service.stop(preview.preview_id)
    await _ended(service, preview.preview_id)
    assert not service.in_worktree(WORKTREE)


@pytest.mark.usefixtures("windows")
async def test_close_during_a_start_ends_the_tree_once_launched(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    entered, release = threading.Event(), threading.Event()
    fake = rig.fake()

    def slow(command: str, *, cwd: Path, env: dict[str, str]) -> FakeShell:
        entered.set()
        release.wait(HANG_GUARD_S)
        return fake(command, cwd=cwd, env=env)

    service = rig.service(start=slow, listeners=_none)
    start = asyncio.create_task(service.start(rig.workspace_id, None, PING, 5000))
    assert await asyncio.to_thread(entered.wait, HANG_GUARD_S)
    close = asyncio.create_task(service.close())
    await asyncio.sleep(0)  # let close() mark the reservation before the spawn returns
    release.set()
    await start
    await asyncio.wait_for(close, HANG_GUARD_S)
    assert rig.starts[0].killed.is_set() and rig.starts[0].released


@pytest.mark.usefixtures("windows")
async def test_unknown_ids_are_refused(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    service = rig.service(start=_forbidden, listeners=_none)
    for call in (
        service.open(UNKNOWN),
        service.stop(UNKNOWN),
        service.remove(UNKNOWN),
        service.output(UNKNOWN, 0),
    ):
        assert await _refusal(call) == "unknown_preview"
    assert await _refusal(service.list(UNKNOWN)) == "unknown_workspace"


async def test_off_windows_every_method_refuses_before_any_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _rig(tmp_path)
    monkeypatch.setattr(previews, "PREVIEWS_SUPPORTED", False)
    monkeypatch.setattr(rig.workspaces, "get", _forbidden)
    monkeypatch.setattr(rig.store, "get_worktree", _forbidden)
    service = rig.service(start=_forbidden, listeners=_forbidden)
    for call in (
        service.start(rig.workspace_id, None, "", 80),
        service.list(rig.workspace_id),
        service.open(UNKNOWN),
        service.stop(UNKNOWN),
        service.remove(UNKNOWN),
        service.output(UNKNOWN, 0),
    ):
        assert await _refusal(call) == "unsupported_platform"
    assert not service.in_worktree(WORKTREE)
    await service.close()


def test_the_limits() -> None:
    assert (previews.MIN_PORT, previews.MAX_PORT) == (1024, 65535)
    assert 8765 in previews.RESERVED_PORTS and len(previews.RESERVED_PORTS) == 1
    assert (previews.MAX_LIVE_PREVIEWS, MAX_ENDED_KEPT, SETTLE_WAIT_S) == (2, 4, 5.0)
