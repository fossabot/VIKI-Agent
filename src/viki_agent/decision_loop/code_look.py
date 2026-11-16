"""When each trusted code folder's uncommitted changes last changed.

`CodeLookReader` implements `contracts.CodeHost`. It reads the registered, persistent code folders
the user trusted (`ordinary`), one folder per `read`: the first without a memo entry, else the
oldest read at least `every` ago, so a check costs at most two git calls and one stat per changed
path (`MAX_CHANGES` caps both). The memo (read time, newest change, whether the read failed) lives
in memory under a lock and drops a folder once it stops being eligible.

A folder is listed only from a read no older than `every` that saw its newest change at least
`idle` old, so a look never starts from an old read (a folder committed or edited since, after a
sleep) or from a read made before the crossing. Among the folders due for a read, one with no
memo comes first, then one whose memo's change is now `idle` old (a fresh read decides whether it
lists), then the rest, each oldest read first. The cost: with more trusted folders than checks in
an interval, a folder's item can close until its next read.

Git keeps no time for a working-tree change, so the fact is the newest stat time of the tracked
and unmerged changes. Untracked, deleted and submodule entries carry no time. A private
path, a truncated or unreadable status, a path that resolves outside the folder, or any failure
reads unknown: the folder is not answered and the failure names `code_partial`. A folder that is
not a repository's top level is a fact, not a failure.

Fail closed: git runs only through `CodeGit` (`winproc.git`, `--no-optional-locks`, `HARDENING`),
after its trust check. The reader writes no tracked file and no index entry; like the Code tab's
status, `git status` runs the folder's own Git filters, such as Git LFS, which may write their
own cache under `.git` (in a Bot copy, Git LFS's alone: `CodeGit.operation`). A change that
cannot be stat'ed is never guessed. `read` never raises. The log names only the exception type,
never a path.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from viki_agent.code.code_files import CodeError, resolve_path_at
from viki_agent.code.code_git import CodeGit
from viki_agent.decision_loop.contracts import (
    CODE_LOOK_EVERY,
    CODE_LOOK_IDLE,
    CodeFolder,
    CodeLook,
)
from viki_agent.domain import Workspace
from viki_agent.workspaces import WorkspaceService

_LOGGER = logging.getLogger(__name__)
_NOT_A_REPOSITORY = ("not_repository", "not_repository_root")


class _Unreadable(Exception):
    """A status that cannot give every change's time: truncated, not ready or a private path."""


@dataclass(frozen=True)
class _Seen:
    read_at: datetime
    newest: datetime | None
    failed: bool


class CodeLookReader:
    def __init__(
        self,
        workspaces: WorkspaceService,
        git: CodeGit,
        *,
        every: timedelta = CODE_LOOK_EVERY,
        idle: timedelta = CODE_LOOK_IDLE,
    ) -> None:
        self._workspaces = workspaces
        self._git = git
        self._every = every
        self._idle = idle
        self._memo: dict[str, _Seen] = {}
        self._lock = threading.Lock()

    def read(self, now: datetime) -> CodeLook:
        with self._lock:
            try:
                eligible = {
                    workspace.workspace_id: workspace
                    for workspace in self._workspaces.list(kind="code")
                    if workspace.persistent and workspace.policy == "ordinary"
                }
            except Exception as exc:
                _warn(exc)
                return CodeLook(None, ("code_unknown",))
            self._memo = {key: seen for key, seen in self._memo.items() if key in eligible}
            due = self._due(sorted(eligible), now)
            if due is not None:
                self._memo[due] = self._look_at(eligible[due], now)
            return self._answer(eligible, now)

    def _due(self, ids: list[str], now: datetime) -> str | None:
        for key in ids:
            if key not in self._memo:
                return key
        stale = [key for key in ids if now - self._memo[key].read_at >= self._every]
        crossing = [key for key in stale if self._crossed(self._memo[key], now)]
        return min(crossing or stale, key=lambda key: self._memo[key].read_at, default=None)

    def _crossed(self, seen: _Seen, now: datetime) -> bool:
        return seen.newest is not None and now - seen.newest >= self._idle

    def _answer(self, eligible: Mapping[str, Workspace], now: datetime) -> CodeLook:
        folders = tuple(
            CodeFolder(key, workspace.name, seen.newest)
            for key, workspace in sorted(eligible.items())
            if (seen := self._memo.get(key)) is not None
            and seen.newest is not None
            and seen.read_at - seen.newest >= self._idle
            and now - seen.read_at <= self._every
        )
        failed = any(seen.failed for seen in self._memo.values())
        return CodeLook(folders, ("code_partial",) if failed else ())

    def _look_at(self, workspace: Workspace, now: datetime) -> _Seen:
        try:
            return _Seen(now, self._newest(workspace), False)
        except Exception as exc:
            if isinstance(exc, CodeError) and exc.code in _NOT_A_REPOSITORY:
                return _Seen(now, None, False)
            _warn(exc)
            return _Seen(now, None, True)

    def _newest(self, workspace: Workspace) -> datetime | None:
        changes = self._git.changes(workspace.workspace_id)
        if changes.state != "ready" or changes.truncated:
            raise _Unreadable
        root = self._workspaces.current_root(workspace)
        if root is None:
            raise CodeError("root_unavailable")
        times: list[float] = []
        for entry in changes.entries:
            if entry.kind not in ("tracked", "unmerged") or "D" in (entry.index, entry.worktree):
                continue
            if entry.private:
                raise _Unreadable
            _, target = resolve_path_at(Path(root.display), entry.path)
            times.append(target.stat().st_mtime)
        return datetime.fromtimestamp(max(times), UTC) if times else None


def _warn(exc: Exception) -> None:
    # The type name only: a message may hold a path or git's stderr.
    _LOGGER.warning("decision code look failed: %s", type(exc).__name__)
