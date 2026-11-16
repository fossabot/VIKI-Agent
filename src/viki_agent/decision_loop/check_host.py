"""Which trusted code folders sit at a commit their saved checks have not run on.

`SavedCheckHost` implements `contracts.CheckHost`; its read half reads the registered,
persistent code folders the user trusted (`ordinary`), one folder per `read`. A folder counts
only when one of its checks has a newest run in the folder itself that named a commit: a check the
user never ran there, one run only in a worktree and one whose newest run recorded no commit are
left out, so VIKI offers only what the user ran before.

A look reads the folder's HEAD through `code_checkout.checkout` (the trust check comes first) and
`in_progress` (a merge, rebase, cherry-pick or revert lists nothing), then, per runnable check, its
kept runs (`KEEP_RUNS` and the live one): the newest run in the folder itself that named that
commit. Checks with none are pending. An earlier run at the same commit counts, so a HEAD that
returns to a commit a kept run named reads as run. In a `read`, the cycle's view, a check with no
run at that commit that VIKI's own start was refused for there (`_refused`) is not pending but
unfinished, so the item stops offering a run VIKI will not make and the checks that ran still
report; `folder`, the user's fresh look, still lists it pending. The phase follows the runs:
running, else waiting while any check is pending, else failed, else stopped (a run cancelled or
interrupted, or a refused check), else passed.

Cost: two store queries per eligible folder per read, and for the one folder looked at, four Git
calls and one more run query. The run queries (`SessionStore.check_run_states`) read where each run
ran, its commit and its outcome, never its output. The memo (read time, HEAD, the
time the folder was first seen there, phase, pending, whether the look failed) lives in memory
under `_memo_lock`, which `read` holds from the listing to the answer, so a second read waits for a
read still inside Git. `every` decides only when a folder is due: a folder is answered from its
latest read until it is read again, so with several folders each one's item stays listed between
its reads. Among the folders due, one a start or stop marked stale (`_stale`, under `_runs_lock`)
comes first, then one with no memo, then the one read longest ago at least `every` ago.

The settle: a waiting folder is listed only once it has stayed at its HEAD for `settle`, counted
from the first read that saw that HEAD. A commit the user amends, rebases or a chat adds on top
of moves HEAD and starts the count again, so VIKI does not run checks on a transient commit. The
cost: a folder first looked at after a restart waits `settle` from then, whatever its commit's age.
Running, passed, failed and stopped folders are listed without the settle.

Read-only and fail closed: Git runs only through `CodeGit` (`winproc.git`, `--no-optional-locks`,
`HARDENING`) after its trust check, with `rev-parse` and `symbolic-ref` only; the reader never runs
`git status`, never opens a check's sealed name or command (`code_checks.unseal`), and writes
nothing to the repository, the worktree or the store. Any failure other than a folder that is not
a checkout reads unknown: the folder is not answered and the failure names `checks_partial`; a
listing failure answers `CheckLook(None, ("checks_unknown",))`. `read` and `folder` never raise.
The log carries the fixed warning and the exception type name, never a path, id or message.

Start and stop: a start given `stops`, the Stop all count its caller read, is VIKI's
own; the user's Run checks passes none. VIKI's start answers 0 before any look at a commit a stop
holds (`_stopped`), and a VIKI start given a count older than the current one holds its commit and
answers 0, so a cycle begun before a Stop all starts nothing and the cycle that Stop all wakes
starts nothing on that commit either. VIKI's start then drops the ids refused to VIKI at that
commit, and with none left answers 0 without a look, so an item whose checks were refused costs no
Git call or store query each cycle. Otherwise a start looks at the folder afresh (`folder`, in a
worker thread); a HEAD that moved or cannot be read starts nothing (`not_applicable`). Of the ids
asked, it starts only those still pending at that commit (and, for VIKI's, not refused there), one
at a time, through `CheckRunner.start(check_id, None)`, the Checks tab's own path (trust
re-checked, `winproc` only). Starts run one at a time (`_starting`), so a second start's look
follows the first one's recorded runs and a check runs at most once per commit. Each start runs in
a task of its own, kept in `_tasks` until done and awaited through `asyncio.shield`, so a caller
cancelled while the runner spawns still leaves a supervised run, recorded for Stop. `run_limit`
ends the batch and is asked again later; `check_running` is skipped; any other refusal of VIKI's
own start, a store failure or a failed spawn included, is remembered for that check at that commit
and never asked again by VIKI until HEAD moves or the app restarts: VIKI retries nothing. The
user's Run checks neither reads nor writes that memory, so each click asks the runner and meets
its refusal of the moment. The host keeps the newest `MAX_STARTED_RUNS` runs it started since
launch, in memory, and `stop`, `stop_all` and `running` reach only those, never a run the user
started in the Checks tab. `stop_all` counts up `_stops` and `stop` the folder's `_folder_stops`; a
start that sees either count change since it was called starts no further check, so a batch still
starting when the user stops ends there. A stop first holds the commit its folders' newest start
was asked for (`_asked`), whether that commit's checks were still pending, starting or running;
then it waits for the starts in flight before it cancels, so the run being spawned at that moment
is recorded and cancelled too, and it holds each cancelled run's commit as well. VIKI starts nothing
more on a held commit while VIKI Agent stays open; the user's own Run checks still can, and a new
commit starts afresh. A stop marks the folder of each run it cancelled stale, and so does a start
that created a start task, so the next read looks at it first. `_started`, `_refused`, `_asked`,
`_tasks`, `_stale`, `_stopped` and the stop counts change only under `_runs_lock`, which is never
held across Git, the store or an await.

Saved checks use `code_checks.CHECKS_SUPPORTED`, read at call time. On unsupported platforms
nothing is read, `start` is `unavailable` before any store call and `stop` cancels nothing.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from viki_agent.code import code_checks
from viki_agent.code.check_runs import KEEP_RUNS, CheckRunner
from viki_agent.code.code_checkout import checkout, in_progress
from viki_agent.code.code_checks import CodeRunError
from viki_agent.code.code_files import CodeError
from viki_agent.code.code_git import CodeGit
from viki_agent.decision_loop.contracts import (
    CHECK_HEAD_SETTLE,
    CHECK_LOOK_EVERY,
    CheckFolder,
    CheckLook,
    DecisionLoopError,
)
from viki_agent.domain import Workspace
from viki_agent.storage import CheckRunState, SessionStore, StateError
from viki_agent.workspaces import WorkspaceError, WorkspaceService

MAX_STARTED_RUNS = 64

_LOGGER = logging.getLogger(__name__)
# A folder that is not a checkout is a fact, not a failure.
_NOT_A_CHECKOUT = ("not_repository", "not_repository_root", "no_commits", "unsupported_branch")


@dataclass(frozen=True)
class _Seen:
    """One look: `head` is None when the folder has no commit to report (not a checkout, a merge
    or rebase under way, or a failed look); `since` is when this HEAD was first seen."""

    read_at: datetime
    head: str | None
    since: datetime
    phase: str
    pending: tuple[str, ...]
    failed: bool


class SavedCheckHost:
    def __init__(
        self,
        store: SessionStore,
        workspaces: WorkspaceService,
        git: CodeGit,
        runner: CheckRunner,
        *,
        every: timedelta = CHECK_LOOK_EVERY,
        settle: timedelta = CHECK_HEAD_SETTLE,
    ) -> None:
        self._store = store
        self._workspaces = workspaces
        self._git = git
        self._runner = runner
        self._every = every
        self._settle = settle
        self._memo: dict[str, _Seen] = {}
        self._memo_lock = threading.Lock()
        # Folders a start or stop changed, so the next read looks at them first.
        self._stale: set[str] = set()
        # Runs this host started since launch, oldest first: run id to (folder, check).
        self._started: dict[str, tuple[str, str]] = {}
        self._refused: dict[str, str] = {}  # check id to the HEAD VIKI's start was refused at
        self._tasks: set[asyncio.Task[str]] = set()
        # How many times Stop all, and Stop per folder, ran: a start in flight ends on a change.
        self._stops = 0
        self._folder_stops: dict[str, int] = {}
        # Folder to the commit its newest start was asked for: a stop holds it, so checks still
        # pending or starting there are held as well as those it cancels.
        self._asked: dict[str, str] = {}
        # Folder to the commits a stop held: VIKI starts no more on them.
        self._stopped: dict[str, set[str | None]] = {}
        self._runs_lock = threading.Lock()
        # One start at a time, so a second start's fresh look follows the first one's runs.
        self._starting = asyncio.Lock()

    def supported(self) -> bool:
        return code_checks.CHECKS_SUPPORTED  # read at call time, so tests can patch it

    def read(self, now: datetime) -> CheckLook:
        if not self.supported():
            return CheckLook((), ())
        with self._memo_lock:
            try:
                eligible = {
                    workspace.workspace_id: (workspace, ids)
                    for workspace in self._workspaces.list(kind="code")
                    if workspace.persistent
                    and workspace.policy == "ordinary"
                    and (ids := self._runnable(workspace.workspace_id))
                }
            except Exception as exc:
                _warn(exc)
                return CheckLook(None, ("checks_unknown",))
            self._memo = {key: seen for key, seen in self._memo.items() if key in eligible}
            due = self._due(sorted(eligible), now)
            if due is not None:
                workspace, ids = eligible[due]
                self._memo[due] = self._look_at(workspace, ids, self._memo.get(due), now)
            return self._answer(eligible, now)

    def folder(self, workspace_id: str, now: datetime) -> CheckFolder | None:
        """One fresh look at one folder: no memo and no settle, for the user's own Run checks,
        so a check refused to VIKI's own start is still pending here."""
        if not self.supported():
            return None
        try:
            workspace = self._workspaces.get(workspace_id, kind="code")
            if not (workspace.persistent and workspace.policy == "ordinary"):
                return None
            ids = self._runnable(workspace_id)
            head = self._head(workspace_id) if ids else None
            if head is None:
                return None
            phase, pending = self._phase(workspace_id, head, ids, refusals=False)
            return CheckFolder(workspace_id, workspace.name, head, phase, pending)
        except WorkspaceError:
            return None
        except Exception as exc:
            if not (isinstance(exc, CodeError) and exc.code in _NOT_A_CHECKOUT):
                _warn(exc)
            return None

    def running(self) -> int:
        """How many runs this host started are still running, counted in one store query; it
        runs in a worker thread."""
        with self._runs_lock:
            run_ids = list(self._started)
        return self._store.count_running_check_runs(run_ids)

    def stops(self) -> int:
        """How many times Stop all ran since launch."""
        with self._runs_lock:
            return self._stops

    async def start(
        self, workspace_id: str, head: str, check_ids: Sequence[str], *, stops: int | None = None
    ) -> int:
        """How many checks it started. It marks the folder stale only when it created a start
        task, so a folder whose checks were all refused is not read ahead of its turn each cycle."""
        if not self.supported():
            raise DecisionLoopError("unavailable")
        vikis = stops is not None  # VIKI's own start: the user's Run checks passes no count
        created = False
        with self._runs_lock:
            self._asked[workspace_id] = head
            if vikis and stops != self._stops:
                # A Stop all ran since the caller read the count, before this start could be
                # held as asked: hold it now, or the cycle that Stop all wakes would start it.
                self._hold(workspace_id, head)
            if vikis and head in self._stopped.get(workspace_id, ()):
                return 0  # the user stopped VIKI here: only the user's Run checks starts more
            generation = (self._stops, self._folder_stops.get(workspace_id, 0))
        try:
            async with self._starting:
                pending = await asyncio.to_thread(
                    self._still_pending, workspace_id, head, check_ids, vikis
                )
                started = 0
                for check_id in pending:
                    with self._runs_lock:
                        if (self._stops, self._folder_stops.get(workspace_id, 0)) != generation:
                            break  # the user stopped since: start nothing more
                        task = asyncio.create_task(
                            self._start_one(workspace_id, head, check_id, vikis)
                        )
                        self._tasks.add(task)
                        created = True
                    task.add_done_callback(self._forget)
                    # Shielded: a caller cancelled while the runner spawns does not cancel the
                    # start between its spawn and its supervisor, nor its record here.
                    outcome = await asyncio.shield(task)
                    if outcome == "run_limit":
                        break
                    started += outcome == "started"
                return started
        finally:
            if created:
                with self._runs_lock:
                    self._stale.add(workspace_id)

    def _still_pending(
        self, workspace_id: str, head: str, check_ids: Sequence[str], vikis: bool
    ) -> list[str]:
        """The asked ids still pending at `head`, which must still be the folder's HEAD, and, for
        VIKI's own start, not refused at it. A fresh look, so a check run at this commit since
        the read is left out; none, without a look, when VIKI's start finds every id refused."""
        with self._runs_lock:
            asked = [c for c in check_ids if not (vikis and self._refused.get(c) == head)]
        if not asked:
            return []
        found = self.folder(workspace_id, datetime.now(UTC))
        if found is None or found.head != head:
            raise DecisionLoopError("not_applicable")
        with self._runs_lock:
            return [
                c
                for c in asked
                if c in found.pending and not (vikis and self._refused.get(c) == head)
            ]

    async def _start_one(self, workspace_id: str, head: str, check_id: str, vikis: bool) -> str:
        """The outcome, "started" or the refusal's code. A refusal of VIKI's own start is
        remembered for this commit, so VIKI asks no more there; `run_limit` and `check_running`
        pass. The user's Run checks remembers none, so the user's next click asks again."""
        try:
            row = await self._runner.start(check_id, None)
        except (CodeRunError, CodeError) as refusal:
            code = refusal.code
        except StateError:
            code = "unavailable"
        else:
            with self._runs_lock:
                self._started[row["run_id"]] = (workspace_id, check_id)
                if len(self._started) > MAX_STARTED_RUNS:
                    del self._started[next(iter(self._started))]
            return "started"
        if vikis and code not in ("run_limit", "check_running"):
            with self._runs_lock:
                self._refused[check_id] = head
        return code

    def _forget(self, task: asyncio.Task[str]) -> None:
        with self._runs_lock:
            self._tasks.discard(task)
        if not task.cancelled() and (exc := task.exception()) is not None:
            # Its caller may have been cancelled, so nothing else sees it.
            _LOGGER.warning("decision check start failed: %s", type(exc).__name__)

    async def stop(self, workspace_id: str) -> int:
        """Hold the commit the folder's newest start was asked for, then cancel the runs this
        host started in the folder; how many were live."""
        with self._runs_lock:
            self._folder_stops[workspace_id] = self._folder_stops.get(workspace_id, 0) + 1
            if (head := self._asked.get(workspace_id)) is not None:
                self._hold(workspace_id, head)
            starting = set(self._tasks)
        return await self._cancel(starting, workspace_id)

    async def stop_all(self) -> int:
        """Hold the commit each folder's newest start was asked for, then cancel every run this
        host started; how many were live."""
        with self._runs_lock:
            self._stops += 1
            for folder, head in self._asked.items():
                self._hold(folder, head)
            starting = set(self._tasks)
        return await self._cancel(starting, None)

    def _hold(self, workspace_id: str, head: str | None) -> None:
        """Keep VIKI's own starts off the commit; under `_runs_lock`."""
        self._stopped.setdefault(workspace_id, set()).add(head)

    async def _cancel(self, starting: set[asyncio.Task[str]], workspace_id: str | None) -> int:
        """Cancel the started runs in the folder, or in every folder for None, once the starts in
        flight are done, so a run being spawned is recorded first."""
        if starting:
            # `wait`, not `gather`: a stop cancelled here leaves the starts and their runs alone.
            await asyncio.wait(starting)
        with self._runs_lock:
            run_ids = [
                key for key, (folder, _) in self._started.items() if workspace_id in (None, folder)
            ]
        live = 0
        for run_id in run_ids:
            try:
                row = await self._runner.cancel(run_id)
            except CodeRunError, StateError:
                # Unsupported here, or its row is gone (past `KEEP_RUNS`, or its check was deleted).
                continue
            live += row["outcome"] == "running"
            with self._runs_lock:
                self._stale.add(row["workspace_id"])
                self._hold(row["workspace_id"], row["head_oid"])
        return live

    def _runnable(self, workspace_id: str) -> tuple[str, ...]:
        """The folder's checks whose newest run in the folder itself named a commit, in
        `list_checks` order. Only ids are read: a check's name and command stay sealed."""
        newest: dict[str, str | None] = {}
        for run in self._store.check_run_states(workspace_id):  # newest first
            if run["worktree_id"] is None:
                newest.setdefault(run["check_id"], run["head_oid"])
        return tuple(
            row["check_id"]
            for row in self._store.list_checks(workspace_id)
            if newest.get(row["check_id"]) is not None
        )

    def _due(self, ids: list[str], now: datetime) -> str | None:
        with self._runs_lock:
            self._stale.intersection_update(ids)
            marked = min(self._stale, default=None)
            if marked is not None:
                self._stale.discard(marked)
                return marked
        for key in ids:
            if key not in self._memo:
                return key
        stale = [key for key in ids if now - self._memo[key].read_at >= self._every]
        return min(stale, key=lambda key: self._memo[key].read_at, default=None)

    def _look_at(
        self,
        workspace: Workspace,
        ids: tuple[str, ...],
        before: _Seen | None,
        now: datetime,
    ) -> _Seen:
        try:
            head = self._head(workspace.workspace_id)
            if head is None:
                return _Seen(now, None, now, "", (), False)
            phase, pending = self._phase(workspace.workspace_id, head, ids, refusals=True)
        except Exception as exc:
            if isinstance(exc, CodeError) and exc.code in _NOT_A_CHECKOUT:
                return _Seen(now, None, now, "", (), False)
            _warn(exc)
            return _Seen(now, None, now, "", (), True)
        since = before.since if before is not None and before.head == head else now
        return _Seen(now, head, since, phase, pending, False)

    def _head(self, workspace_id: str) -> str | None:
        """HEAD of the folder itself, or None while a merge, rebase, cherry-pick or revert is
        under way. `checkout` checks trust before any Git call."""
        found = checkout(self._store, self._git, workspace_id, None)
        return None if in_progress(self._git, found.root) else found.head_oid

    def _phase(
        self, workspace_id: str, head: str, ids: tuple[str, ...], *, refusals: bool
    ) -> tuple[str, tuple[str, ...]]:
        """The phase at `head` and the checks pending there, from one run query. With
        `refusals` (the cycle's read), a check refused to VIKI's own start at `head` that has no
        run there is not pending but unfinished."""
        kept: dict[str, list[CheckRunState]] = {}
        for run in self._store.check_run_states(workspace_id):  # newest first
            kept.setdefault(run["check_id"], []).append(run)
        runs = [_run_at(kept.get(check_id, [])[: KEEP_RUNS + 1], head) for check_id in ids]
        unrun = [check_id for check_id, run in zip(ids, runs, strict=True) if run is None]
        with self._runs_lock:
            refused = {c for c in unrun if refusals and self._refused.get(c) == head}
        pending = tuple(check_id for check_id in unrun if check_id not in refused)
        outcomes = {run["outcome"] for run in runs if run is not None}
        if "running" in outcomes:
            return "running", pending
        if pending:
            return "waiting", pending
        if outcomes & {"failed", "timed_out"}:
            return "failed", pending
        if refused or outcomes & {"cancelled", "interrupted"}:
            return "stopped", pending
        return "passed", pending

    def _answer(
        self, eligible: Mapping[str, tuple[Workspace, tuple[str, ...]]], now: datetime
    ) -> CheckLook:
        """Each folder from its latest read, however old: every start and the user's Run checks
        take a fresh look first, so an older answer only affects what the launcher shows until the
        folder's next read."""
        folders = tuple(
            CheckFolder(key, workspace.name, seen.head, seen.phase, seen.pending)
            for key, (workspace, _ids) in sorted(eligible.items())
            if (seen := self._memo.get(key)) is not None
            and seen.head is not None
            and (seen.phase != "waiting" or now - seen.since >= self._settle)
        )
        failed = any(seen.failed for seen in self._memo.values())
        return CheckLook(folders, ("checks_partial",) if failed else ())


def _run_at(runs: Sequence[CheckRunState], head: str) -> CheckRunState | None:
    """The newest of a check's kept runs (newest first) in the folder itself that named `head`."""
    return next(
        (run for run in runs if run["worktree_id"] is None and run["head_oid"] == head), None
    )


def _warn(exc: Exception) -> None:
    # The type name only: a message may hold a path or git's stderr.
    _LOGGER.warning("decision check read failed: %s", type(exc).__name__)
