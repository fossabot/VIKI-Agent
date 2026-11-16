"""Preview runs: a command the user typed, run through cmd.exe in a trusted code
folder or one of its ready worktrees, and the port it names, opened in a preview window.

Nothing is stored: a preview lives in memory until VIKI closes. Its state is read when asked:
`listening` when the preview's Job holds the pid of every listener on its port, `port_taken` when
it misses one, `starting` when there is none. A start passes the root gate twice, the second time
right before the spawn, and is reserved on the event loop in the same step that reads
`SessionRoots.blocked`, with no await between. Each preview has a supervisor thread, which waits
for the command, ends its process tree and settles it, and a reader thread, which copies its
output into a bounded buffer; they reach the loop only through `call_soon_threadsafe`. A log line
names the operation and the exception type, never a command, port, URL, output or path.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sys
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

from viki_agent import config, winproc
from viki_agent.code import code_checks
from viki_agent.code.check_output import OutputBuffer, Slice
from viki_agent.code.code_checks import CodeRunError
from viki_agent.code.preview_windows import PreviewWindows
from viki_agent.code.session_roots import SessionRoots
from viki_agent.session import now_iso
from viki_agent.storage import SessionStore
from viki_agent.winproc import ShellRun
from viki_agent.workspaces import WorkspaceError, WorkspaceService

PREVIEWS_SUPPORTED = sys.platform == "win32"  # read at call time, so tests can patch it
MIN_PORT, MAX_PORT = 1024, 65535
RESERVED_PORTS = frozenset({config.PORT})
MAX_LIVE_PREVIEWS = 2
MAX_ENDED_KEPT = 4
SETTLE_WAIT_S = 5.0

_LOGGER = logging.getLogger(__name__)


def _supported() -> None:
    if not PREVIEWS_SUPPORTED:
        raise CodeRunError("unsupported_platform")


@dataclass(frozen=True)
class PreviewView:
    preview_id: str
    workspace_id: str
    worktree_id: str | None
    command: str
    port: int
    state: str  # starting, listening, port_taken, stopped or exited
    pid: int
    listener_pid: int | None  # exactly when listening
    exit_code: int | None  # exactly when exited
    started_at: str
    ended_at: str | None  # None exactly while live


@dataclass(eq=False)
class _Preview:
    """A preview. `shell` stays None while it is only reserved; `ended_at` None while live."""

    preview_id: str
    workspace_id: str
    worktree_id: str | None
    command: str
    port: int
    done: asyncio.Event = field(default_factory=asyncio.Event)  # set once it has ended
    buffer: OutputBuffer = field(default_factory=OutputBuffer)
    # Guards the flags below, and keeps a kill_tree or a Job read from racing the release.
    lock: threading.Lock = field(default_factory=threading.Lock)
    shell: ShellRun | None = None
    started_at: str = ""
    waited: bool = False  # the wait returned: a stop no longer changes the outcome
    stopped: bool = False
    state: str = ""  # stopped or exited, once ended
    exit_code: int | None = None
    ended_at: str | None = None


def _stop(preview: _Preview) -> None:
    with preview.lock:
        if preview.waited:
            return
        preview.stopped = True
        if preview.shell is not None:
            preview.shell.kill_tree()


def _drain(shell: ShellRun, buffer: OutputBuffer) -> None:
    while chunk := shell.read():
        buffer.append(chunk)


class PreviewService:
    def __init__(
        self,
        store: SessionStore,
        workspaces: WorkspaceService,
        roots: SessionRoots,
        windows: PreviewWindows,
        *,
        start: Callable[..., ShellRun] | None = None,
        listeners: Callable[[int], frozenset[int]] | None = None,
    ) -> None:
        self._store = store
        self._workspaces = workspaces
        self._roots = roots
        self._windows = windows
        self._start = start  # None: winproc.start_shell, looked up at call time
        self._listeners = listeners  # None: winproc.tcp_listeners, looked up at call time
        # Every preview by id, oldest first, reserved ones included; touched only on the loop.
        self._previews: dict[str, _Preview] = {}
        self._closing = False

    # --- starting ----------------------------------------------------------------------------

    async def start(
        self, workspace_id: str, worktree_id: str | None, command: str, port: int
    ) -> PreviewView:
        _supported()
        if not code_checks.valid_command(command):
            raise CodeRunError("invalid_command")
        if type(port) is not int or not MIN_PORT <= port <= MAX_PORT:
            raise CodeRunError("invalid_port")
        if port in RESERVED_PORTS:
            raise CodeRunError("port_reserved")
        listened = await asyncio.to_thread(self._gate_and_listened, workspace_id, worktree_id, port)
        # No await from here to the reservation: a removal's block and this test cannot interleave.
        if self._closing:
            raise CodeRunError("unavailable")
        live = [p for p in self._previews.values() if p.ended_at is None]
        if len(live) >= MAX_LIVE_PREVIEWS:
            raise CodeRunError("preview_limit")
        if any(p.port == port for p in live):
            raise CodeRunError("port_in_use")
        if listened:  # after port_in_use: another preview's own listener is not another program's
            raise CodeRunError("port_taken")
        if worktree_id is not None and self._roots.blocked(worktree_id):
            raise CodeRunError("worktree_not_ready")
        preview_id = str(uuid.uuid4())
        preview = self._previews[preview_id] = _Preview(
            preview_id, workspace_id, worktree_id, command, port
        )
        try:
            shell = await asyncio.to_thread(self._launch, workspace_id, worktree_id, command)
        except BaseException:
            del self._previews[preview_id]
            preview.done.set()
            raise
        with preview.lock:
            preview.shell, preview.started_at = shell, now_iso()
            if preview.stopped:  # close() came while it started
                shell.kill_tree()
        threading.Thread(
            target=self._supervise,
            args=(preview, shell, asyncio.get_running_loop()),
            name="viki-preview-supervisor",
            daemon=True,
        ).start()
        return self._view(preview, "starting", None)

    def _gate_and_listened(self, workspace_id: str, worktree_id: str | None, port: int) -> bool:
        """In a thread: the root gate, which raises, then whether anything listens on the port."""
        code_checks.trusted_root(self._store, self._workspaces, workspace_id, worktree_id)
        return bool((self._listeners or winproc.tcp_listeners)(port))

    def _launch(self, workspace_id: str, worktree_id: str | None, command: str) -> ShellRun:
        # Trust, the root or the worktree can change since the first gate, so it runs again and
        # the spawn uses its answer.
        root = code_checks.trusted_root(self._store, self._workspaces, workspace_id, worktree_id)
        start = self._start or winproc.start_shell
        try:
            return start(command, cwd=root, env=winproc.scrub_env(os.environ))
        except OSError:
            raise CodeRunError("start_failed") from None

    # --- supervising (threads) ---------------------------------------------------------------

    def _supervise(
        self, preview: _Preview, shell: ShellRun, loop: asyncio.AbstractEventLoop
    ) -> None:
        reader = threading.Thread(
            target=_drain, args=(shell, preview.buffer), name="viki-preview-reader", daemon=True
        )
        reader.start()
        code: int | None = None
        try:
            code = shell.wait(None)
        except Exception as exc:
            _LOGGER.warning("preview: wait failed (%s)", type(exc).__name__)
        finally:
            with preview.lock:
                preview.waited = True
                shell.release()  # ends whatever the command left running, so the reader ends
            reader.join()
            preview.buffer.close()
            with contextlib.suppress(RuntimeError):  # the loop has closed; nothing awaits it
                loop.call_soon_threadsafe(self._settle, preview, code)

    def _settle(self, preview: _Preview, code: int | None) -> None:
        exited = code is not None and not preview.stopped
        preview.state = "exited" if exited else "stopped"
        preview.exit_code = code if exited else None
        preview.ended_at = now_iso()
        preview.done.set()
        self._windows.close_owner(preview.preview_id)
        ended = [p.preview_id for p in self._previews.values() if p.ended_at is not None]
        for old in ended[:-MAX_ENDED_KEPT]:
            del self._previews[old]

    # --- observing ---------------------------------------------------------------------------

    def _observe(self, preview: _Preview, shell: ShellRun) -> tuple[str, int | None]:
        """In a thread: the live state and the listener pid the preview's Job holds."""
        pids = (self._listeners or winproc.tcp_listeners)(preview.port)
        with preview.lock:
            if preview.waited:  # its Job is closed; the settle is on its way
                return "starting", None
            held = [pid for pid in pids if shell.job.holds(pid)]
        if not pids:
            return "starting", None
        # Fail closed: a foreign listener beside the preview's own could answer the window's
        # `localhost`, which reaches [::1] as well as 127.0.0.1.
        if len(held) < len(pids):
            return "port_taken", None
        return "listening", min(held)

    async def _observed(self, preview: _Preview) -> PreviewView:
        state, listener_pid = "", None
        if preview.ended_at is None and preview.shell is not None:
            state, listener_pid = await asyncio.to_thread(self._observe, preview, preview.shell)
        if preview.ended_at is not None:  # also when it ended during the observation
            state, listener_pid = preview.state, None
        return self._view(preview, state, listener_pid)

    @staticmethod
    def _view(preview: _Preview, state: str, listener_pid: int | None) -> PreviewView:
        assert preview.shell is not None
        return PreviewView(
            preview.preview_id,
            preview.workspace_id,
            preview.worktree_id,
            preview.command,
            preview.port,
            state,
            preview.shell.pid,
            listener_pid,
            preview.exit_code,
            preview.started_at,
            preview.ended_at,
        )

    # --- by id -------------------------------------------------------------------------------

    def _get(self, preview_id: str) -> _Preview:
        preview = self._previews.get(preview_id)
        if preview is None or preview.shell is None:
            raise CodeRunError("unknown_preview")
        return preview

    async def open(self, preview_id: str) -> None:
        _supported()
        preview = self._get(preview_id)
        if preview.ended_at is not None:
            raise CodeRunError("not_listening")
        await asyncio.to_thread(
            code_checks.trusted_root,
            self._store,
            self._workspaces,
            preview.workspace_id,
            preview.worktree_id,
        )
        view = await self._observed(preview)
        if view.state == "port_taken":
            raise CodeRunError("port_taken")
        if view.state != "listening":
            raise CodeRunError("not_listening")
        port = preview.port
        await self._windows.open(
            f"http://localhost:{port}/",
            allow=(f"localhost:{port}", f"127.0.0.1:{port}", f"[::1]:{port}"),
            owner=preview_id,
        )
        if preview.ended_at is not None:  # it ended while the window opened: the settle missed it
            self._windows.close_owner(preview_id)

    async def stop(self, preview_id: str) -> PreviewView:
        _supported()
        preview = self._get(preview_id)
        _stop(preview)  # the settle closes its windows
        return await self._observed(preview)

    async def remove(self, preview_id: str) -> None:
        _supported()
        if self._get(preview_id).ended_at is None:
            raise CodeRunError("preview_running")
        del self._previews[preview_id]

    async def output(self, preview_id: str, after: int) -> tuple[PreviewView, Slice, bool]:
        """The preview, its output after the offset `after`, and whether it has ended and the
        answer reaches its end."""
        _supported()
        preview = self._get(preview_id)
        view = await self._observed(preview)
        part = preview.buffer.read(after)
        return view, part, view.ended_at is not None and part.next == preview.buffer.end

    def in_worktree(self, worktree_id: str) -> bool:
        """Whether a live or reserved preview uses the worktree (the removal's `in_use`); it
        answers on every OS, since the removal asks everywhere."""
        return any(
            p.worktree_id == worktree_id and p.ended_at is None for p in self._previews.values()
        )

    # --- shutdown ----------------------------------------------------------------------------

    async def close(self) -> None:
        """Kill every live preview's tree and wait, at most `SETTLE_WAIT_S`, for each to end."""
        self._closing = True
        live = [p for p in self._previews.values() if p.ended_at is None]
        for preview in live:
            _stop(preview)
        try:
            async with asyncio.timeout(SETTLE_WAIT_S):
                for preview in live:
                    await preview.done.wait()
        except TimeoutError as exc:
            _LOGGER.warning("previews: close failed (%s)", type(exc).__name__)

    async def list(self, workspace_id: str) -> list[PreviewView]:
        """The folder's previews, newest first. Only a code row is needed, so a preview whose
        folder lost its trust can still be seen and stopped."""
        _supported()
        try:
            await asyncio.to_thread(self._workspaces.get, workspace_id, kind="code")
        except WorkspaceError:
            raise CodeRunError("unknown_workspace") from None
        mine = [
            p
            for p in reversed(self._previews.values())
            if p.workspace_id == workspace_id and p.shell is not None
        ]
        return [await self._observed(p) for p in mine]
