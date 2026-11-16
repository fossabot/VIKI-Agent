"""The check runner: runs a user-written check's command through cmd.exe in a
trusted code folder or one of its ready worktrees, follows its output and records each run.

A run is reserved on the event loop in the same step that reads `SessionRoots.blocked`, with no
await between, so a worktree removal and a run start cannot both pass. Each run has a supervisor
thread, which waits for the command, ends its process tree and settles the record, and a reader
thread, which copies its output into a bounded buffer; they reach the loop only through
`call_soon_threadsafe`. A log line names the operation and the exception type, never a command,
name, output or path.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

from viki_agent import protection, winproc
from viki_agent.code import code_checkout, code_checks
from viki_agent.code.check_output import OutputBuffer, Slice, read_stored
from viki_agent.code.code_checks import CodeRunError
from viki_agent.code.code_files import CodeError
from viki_agent.code.code_git import CodeGit
from viki_agent.code.session_roots import SessionRoots
from viki_agent.session import now_iso
from viki_agent.storage import CheckRow, CheckRunRow, SessionStore, StateError
from viki_agent.winproc import ShellRun
from viki_agent.workspaces import WorkspaceService

MAX_LIVE_RUNS = 4
KEEP_RUNS = 20
SETTLE_WAIT_S = 5.0
# The `check_runs.duration_ms` CHECK bound: the monotonic clock can count sleep that the wait's
# timeout does not, and a longer value would fail the settle and leave the row `running`.
MAX_DURATION_MS = 86_400_000

_LOGGER = logging.getLogger(__name__)


def _supported() -> None:
    if not code_checks.CHECKS_SUPPORTED:  # read at call time, so tests can patch it
        raise CodeRunError("unsupported_platform")


def _tail_context(run_id: str) -> bytes:
    return b"VIKI check run v1\0" + run_id.encode("ascii")


@dataclass(eq=False)
class _Run:
    """A live run. `row` and `shell` stay None while the run is only reserved."""

    check_id: str
    worktree_id: str | None
    done: asyncio.Event = field(default_factory=asyncio.Event)  # set once it leaves the live set
    buffer: OutputBuffer = field(default_factory=OutputBuffer)
    # Guards the flags below, and keeps a kill_tree from racing the supervisor's release.
    lock: threading.Lock = field(default_factory=threading.Lock)
    row: CheckRunRow | None = None
    shell: ShellRun | None = None
    started: float = 0.0  # the monotonic clock at the spawn
    waited: bool = False  # the wait returned: a cancel or close no longer changes the outcome
    cancelled: bool = False
    interrupted: bool = False


def _stop(run: _Run, *, interrupt: bool) -> None:
    with run.lock:
        if run.waited:
            return
        if interrupt:
            run.interrupted = True
        else:
            run.cancelled = True
        if run.shell is not None:
            run.shell.kill_tree()


def _outcome(run: _Run, code: int | None) -> str:
    if run.interrupted:
        return "interrupted"
    if code is None:
        return "timed_out"
    if run.cancelled:
        return "cancelled"
    return "passed" if code == 0 else "failed"


def _drain(shell: ShellRun, buffer: OutputBuffer) -> None:
    while chunk := shell.read():
        buffer.append(chunk)


class CheckRunner:
    def __init__(
        self,
        store: SessionStore,
        workspaces: WorkspaceService,
        git: CodeGit,
        roots: SessionRoots,
        *,
        start: Callable[..., ShellRun] | None = None,
        timeout_of: Callable[[int], float] = float,
    ) -> None:
        self._store = store
        self._workspaces = workspaces
        self._git = git
        self._roots = roots
        self._start = start  # None: winproc.start_shell, looked up at call time
        self._timeout_of = timeout_of
        self._live: dict[str, _Run] = {}  # live and reserved runs by id; touched only on the loop
        self._deleting: set[str] = set()
        self._closing = False

    # --- starting ----------------------------------------------------------------------------

    async def start(self, check_id: str, worktree_id: str | None) -> CheckRunRow:
        _supported()
        check, command = await asyncio.to_thread(self._prepare, check_id, worktree_id)
        # No await from here to the reservation: a removal's block and this test cannot interleave.
        if self._closing:
            raise CodeRunError("unavailable")
        if len(self._live) >= MAX_LIVE_RUNS:
            raise CodeRunError("run_limit")
        if self.running(check_id):
            raise CodeRunError("check_running")
        if check_id in self._deleting:
            raise CodeRunError("unknown_check")
        if worktree_id is not None and self._roots.blocked(worktree_id):
            raise CodeRunError("worktree_not_ready")
        run_id = str(uuid.uuid4())
        run = self._live[run_id] = _Run(check_id, worktree_id)
        try:
            row, shell, started = await asyncio.to_thread(
                self._launch, run_id, check, worktree_id, command
            )
        except BaseException:
            self._drop(run_id, run)
            raise
        with run.lock:
            run.row, run.shell, run.started = row, shell, started
            if run.interrupted:  # close() came while it started
                shell.kill_tree()
        threading.Thread(
            target=self._supervise,
            args=(
                run_id,
                run,
                shell,
                asyncio.get_running_loop(),
                self._timeout_of(check["timeout_s"]),
            ),
            name="viki-check-supervisor",
            daemon=True,
        ).start()
        return row

    def _prepare(self, check_id: str, worktree_id: str | None) -> tuple[CheckRow, str]:
        check = self._store.get_check(check_id)
        if check is None:
            raise CodeRunError("unknown_check")
        code_checks.trusted_root(self._store, self._workspaces, check["workspace_id"], worktree_id)
        _name, command = code_checks.unseal(check)
        return check, command

    def _launch(
        self, run_id: str, check: CheckRow, worktree_id: str | None, command: str
    ) -> tuple[CheckRunRow, ShellRun, float]:
        head_oid, dirty = self._snapshot(check["workspace_id"], worktree_id)
        row: CheckRunRow = {
            "run_id": run_id,
            "check_id": check["check_id"],
            "workspace_id": check["workspace_id"],
            "worktree_id": worktree_id,
            "head_oid": head_oid,
            "dirty": dirty,
            "outcome": "running",
            "exit_code": None,
            "duration_ms": None,
            "output_bytes": None,
            "tail": None,
            "started_at": now_iso(),
            "ended_at": None,
        }
        # The run is reserved, so a delete that starts now answers `check_running` and one under
        # way at the reservation was refused by `_deleting`; a delete that finished before it
        # removed the row, which this read sees. Trust, the root or the worktree can change
        # during the snapshot, so the gate runs again and the spawn uses its answer.
        if self._store.get_check(check["check_id"]) is None:
            raise CodeRunError("unknown_check")
        root = code_checks.trusted_root(
            self._store, self._workspaces, check["workspace_id"], worktree_id
        )
        start = self._start or winproc.start_shell
        try:
            shell = start(command, cwd=root, env=winproc.scrub_env(os.environ))
        except OSError:
            raise CodeRunError("start_failed") from None
        started = time.monotonic()
        try:
            self._store.insert_check_run(row, keep=KEEP_RUNS)
        except Exception as exc:
            shell.kill_tree()
            shell.release()
            if isinstance(exc, StateError) and exc.code == "not_found":
                raise CodeRunError("unknown_check") from None  # deleted since `_prepare` read it
            _LOGGER.warning("check run: record failed (%s)", type(exc).__name__)
            raise CodeRunError("unavailable") from None
        return row, shell, started

    def _snapshot(
        self, workspace_id: str, worktree_id: str | None
    ) -> tuple[str | None, int | None]:
        """HEAD and whether Git lists any change; both None when Git cannot say. One operation:
        the status runs under the Git settings checks the checkout made."""
        try:
            with self._git.scope():
                found = code_checkout.checkout(self._store, self._git, workspace_id, worktree_id)
                status = self._git.output(
                    found.root, "status", "--porcelain=v2", "-z", "--untracked-files=normal"
                )
        except CodeError:
            return None, None
        return found.head_oid, int(status != "")

    # --- supervising (threads) ---------------------------------------------------------------

    def _supervise(
        self,
        run_id: str,
        run: _Run,
        shell: ShellRun,
        loop: asyncio.AbstractEventLoop,
        timeout_s: float,
    ) -> None:
        try:
            outcome, exit_code, duration_ms = self._wait(run, shell, timeout_s)
            self._settle(run_id, run.buffer, outcome, exit_code, duration_ms)
        except Exception as exc:
            # The row stays `running` until the next launch's recover().
            _LOGGER.warning("check run: settle failed (%s)", type(exc).__name__)
        finally:
            with contextlib.suppress(RuntimeError):  # the loop has closed; nothing awaits the drop
                loop.call_soon_threadsafe(self._drop, run_id, run)

    @staticmethod
    def _wait(run: _Run, shell: ShellRun, timeout_s: float) -> tuple[str, int | None, int]:
        reader = threading.Thread(
            target=_drain, args=(shell, run.buffer), name="viki-check-reader", daemon=True
        )
        reader.start()
        try:
            code = shell.wait(timeout_s)
            with run.lock:
                run.waited = True
                if code is None:
                    shell.kill_tree()
                outcome = _outcome(run, code)
            duration_ms = min(int((time.monotonic() - run.started) * 1000), MAX_DURATION_MS)
        finally:
            shell.release()  # ends whatever the command left running, so the reader reaches EOF
            reader.join()
            run.buffer.close()
        return outcome, code if outcome in ("passed", "failed") else None, duration_ms

    def _settle(
        self,
        run_id: str,
        buffer: OutputBuffer,
        outcome: str,
        exit_code: int | None,
        duration_ms: int,
    ) -> None:
        tail = buffer.tail()
        self._store.finish_check_run(
            run_id,
            outcome=outcome,
            exit_code=exit_code,
            duration_ms=duration_ms,
            output_bytes=buffer.end,
            tail=protection.protect(tail, context=_tail_context(run_id)) if tail else None,
            ended_at=now_iso(),
            keep=KEEP_RUNS,
        )

    def _drop(self, run_id: str, run: _Run) -> None:
        self._live.pop(run_id, None)
        run.done.set()

    # --- by id -------------------------------------------------------------------------------

    async def cancel(self, run_id: str) -> CheckRunRow:
        _supported()
        run = self._live.get(run_id)
        if run is not None and run.row is not None:
            _stop(run, interrupt=False)
            return run.row
        row = await asyncio.to_thread(self._store.get_check_run, run_id)
        if row is None:
            raise CodeRunError("unknown_run")
        return row

    async def output(self, run_id: str, after: int) -> tuple[CheckRunRow, Slice, bool]:
        """The run, its output after the offset `after`, and whether the run is settled and the
        answer reaches its end."""
        _supported()
        run = self._live.get(run_id)
        if run is not None and run.row is not None:
            return run.row, run.buffer.read(after), False
        row, tail = await asyncio.to_thread(self._stored, run_id)
        total = row["output_bytes"] or 0  # None for a run interrupted by a restart
        part = read_stored(tail, total, after)
        return row, part, row["outcome"] != "running" and part.next == total

    def _stored(self, run_id: str) -> tuple[CheckRunRow, bytes]:
        row = self._store.get_check_run(run_id)
        if row is None:
            raise CodeRunError("unknown_run")
        if row["tail"] is None:
            return row, b""
        try:
            return row, protection.unprotect(row["tail"], context=_tail_context(run_id))
        except protection.ProtectionError:
            raise CodeRunError("unavailable") from None

    async def runs(self, check_id: str) -> list[CheckRunRow]:
        _supported()
        return await asyncio.to_thread(self._runs_of, check_id)

    def _runs_of(self, check_id: str) -> list[CheckRunRow]:
        if self._store.get_check(check_id) is None:
            raise CodeRunError("unknown_check")
        return self._store.check_runs_of(check_id, KEEP_RUNS + 1)

    async def delete(self, check_id: str) -> None:
        _supported()
        if self.running(check_id):
            raise CodeRunError("check_running")
        self._deleting.add(check_id)
        try:
            deleted = await asyncio.to_thread(self._store.delete_check, check_id)
        finally:
            self._deleting.discard(check_id)
        if not deleted:
            raise CodeRunError("unknown_check")

    def running(self, check_id: str) -> bool:
        return any(run.check_id == check_id for run in self._live.values())

    def in_worktree(self, worktree_id: str) -> bool:
        """Whether a live or reserved run uses the worktree (the removal's `in_use`)."""
        return any(run.worktree_id == worktree_id for run in self._live.values())

    # --- launch and shutdown -----------------------------------------------------------------

    async def recover(self) -> int:
        """End every run a previous launch left `running` as `interrupted`; the count."""
        return await asyncio.to_thread(self._store.interrupt_check_runs, now_iso(), keep=KEEP_RUNS)

    async def close(self) -> None:
        """Kill every live run and wait, at most `SETTLE_WAIT_S`, for each to settle."""
        self._closing = True
        runs = list(self._live.values())
        for run in runs:
            _stop(run, interrupt=True)
        try:
            async with asyncio.timeout(SETTLE_WAIT_S):
                for run in runs:
                    await run.done.wait()
        except TimeoutError as exc:
            _LOGGER.warning("check runs: close failed (%s)", type(exc).__name__)
