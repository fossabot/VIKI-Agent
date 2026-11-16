"""Integrate a worktree's reviewed head into its code folder, push a branch, and check a push.

Integrate is a fast-forward to the reviewed commit id, never to a branch name, and push sends
that commit id to the remote's branch without force, so neither can move a branch anywhere the
user did not review. The fast-forward overwrites no untracked or ignored file: Git refuses it
first. Both run under the whole-root lease; `check` only reads
the remote. For a Bot code copy, the fast-forward of its head and the push of its branch run with
`core.hooksPath` at an empty private folder, so no repository hook runs; every other integrate and
push keeps the user's hooks. Git runs through `CodeGit`, so `HARDENING` applies. A failing Git
call's message can
hold a remote's URL with a token: logs carry the subcommand and the exception type only.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from viki_agent.code.code_checkout import (
    Checkout,
    admit,
    busy_in,
    checkout,
    held,
    hooks_off,
    in_progress,
    is_bot_copy,
    keep_settings_change,
)
from viki_agent.code.code_files import CodeError
from viki_agent.code.code_git import CodeGit, status_records
from viki_agent.code.code_range import range_paths
from viki_agent.code.code_remote import remote_oid, select_remote
from viki_agent.code.worktrees import valid_branch
from viki_agent.storage import SessionStore
from viki_agent.vaults.vault_mutation import (
    MutationCoordinator,
    OrdinaryMutationRoot,
)
from viki_agent.vaults.workspace_mutation import WorkspaceMutationResolver

MERGE_TIMEOUT_S = 60.0
PUSH_TIMEOUT_S = 120.0

_LOGGER = logging.getLogger(__name__)
_OID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


@dataclass(frozen=True)
class PushResult:
    outcome: Literal["pushed", "unknown"]
    remote_oid: str | None


@dataclass(frozen=True)
class CheckResult:
    outcome: Literal["pushed", "not_pushed"]
    remote_oid: str | None


def _require_oid(*oids: str) -> None:
    # Callers pass validated ids; this keeps a malformed one from reaching an argument.
    if not all(_OID.fullmatch(oid) for oid in oids):
        raise ValueError("not a full commit id")


def _failed(subcommand: str, exc: Exception) -> None:
    # Only the subcommand and exception type: Git's stderr may hold a remote URL with a token.
    _LOGGER.warning("code: git %s failed (%s)", subcommand, type(exc).__name__)


class CodeIntegration:
    def __init__(
        self,
        git: CodeGit,
        store: SessionStore,
        coordinator: MutationCoordinator,
        resolve_root: WorkspaceMutationResolver,
        *,
        busy: Callable[[str], bool],
    ) -> None:
        self._git = git
        self._store = store
        self._coordinator = coordinator
        self._resolve_root = resolve_root
        self._busy = busy

    # --- integrate --------------------------------------------------------------------------------

    async def integrate(
        self,
        worktree_id: str,
        policy_revision: int,
        root_physical_id: str,
        target_branch: str,
        target_oid: str,
        head_oid: str,
    ) -> str:
        """Fast-forward the folder's branch to the worktree's reviewed head; the new head."""
        if not valid_branch(target_branch):
            raise CodeError("unsupported_branch")
        _require_oid(target_oid, head_oid)
        work, root = await asyncio.to_thread(
            self._integration_target, worktree_id, policy_revision, root_physical_id
        )
        if await busy_in(self._store, self._busy, work.workspace_id, None):
            raise CodeError("checkout_busy")
        async with held(self._coordinator, root, self._resolve_root):
            if await busy_in(self._store, self._busy, work.workspace_id, None):
                raise CodeError("checkout_busy")
            return await asyncio.to_thread(
                self._fast_forward, work, target_branch, target_oid, head_oid
            )

    def _integration_target(
        self, worktree_id: str, policy_revision: int, root_physical_id: str
    ) -> tuple[Checkout, OrdinaryMutationRoot]:
        row = self._store.get_worktree(worktree_id)
        if row is None:
            raise CodeError("unknown_worktree")
        work = checkout(self._store, self._git, row["workspace_id"], worktree_id)
        return work, admit(
            self._resolve_root, row["workspace_id"], policy_revision, root_physical_id
        )

    def _fast_forward(
        self, work: Checkout, target_branch: str, target_oid: str, head_oid: str
    ) -> str:
        """Under the lease: the Git settings and every expectation again, then
        `merge --ff-only` in the folder, with hooks off when the worktree is a Bot copy."""
        with self._git.operation(work.folder, fresh=True):
            return self._fast_forward_checked(work, target_branch, target_oid, head_oid)

    def _fast_forward_checked(
        self, work: Checkout, target_branch: str, target_oid: str, head_oid: str
    ) -> str:
        assert work.worktree_id is not None
        row = self._store.get_worktree(work.worktree_id)
        if row is None or row["state"] != "ready" or not Path(row["path"]).is_dir():
            raise CodeError("worktree_not_ready")
        folder = work.folder
        branch = self._git.branch(folder)
        if branch is None:
            raise CodeError("detached_target")
        if branch != target_branch or self._git.head(folder) != target_oid:
            raise CodeError("stale_base")
        if self._git.head(work.root) != head_oid:
            raise CodeError("stale_review")
        if head_oid == target_oid:
            raise CodeError("up_to_date")
        base = self._git.answer(folder, "merge-base", target_oid, head_oid)
        if base is None or base.splitlines() != [target_oid]:
            raise CodeError("not_fast_forward")
        paths, truncated = range_paths(self._git, folder, target_oid, head_oid)
        if truncated or any(path.kind != "text" for path in paths):
            raise CodeError("unsupported_change")
        if not self._clean(folder):
            raise CodeError("dirty_target")
        if in_progress(self._git, folder):
            raise CodeError("in_progress")
        with hooks_off(is_bot_copy(self._store, work.worktree_id)) as hooks:
            self._merge(folder, target_oid, head_oid, hooks)
        if self._head_or_none(folder) != head_oid:
            raise CodeError("merge_failed")
        return head_oid

    def _merge(self, folder: Path, target_oid: str, head_oid: str, hooks: tuple[str, ...]) -> None:
        try:
            # Without `--no-overwrite-ignore` Git silently replaces an ignored file at a path
            # the range adds.
            self._git.git(
                folder,
                *hooks,
                *("merge", "--ff-only", "--no-autostash", "--no-overwrite-ignore", head_oid),
                timeout=MERGE_TIMEOUT_S,
            )
        except Exception as exc:  # RuntimeError, or TimeoutExpired (TID251 bans naming it)
            keep_settings_change(exc)
            _failed("merge", exc)
            # Git refuses before touching anything (an untracked or ignored file in the way).
            unchanged = self._head_or_none(folder) == target_oid and self._clean_or_false(folder)
            raise CodeError("merge_refused" if unchanged else "merge_failed") from None

    def _clean(self, folder: Path) -> bool:
        """No tracked change, staged or not; untracked and ignored files may stay, since Git
        refuses a fast-forward that would overwrite one (`--no-overwrite-ignore` for ignored
        files)."""
        raw = self._git.output(folder, "status", "--porcelain=v2", "-z", "--untracked-files=no")
        try:
            return not status_records(raw)[1]
        except ValueError:
            raise CodeError("git_failed") from None

    def _clean_or_false(self, folder: Path) -> bool:
        try:
            return self._clean(folder)
        except CodeError as exc:
            keep_settings_change(exc)
            return False

    def _head_or_none(self, folder: Path) -> str | None:
        try:
            return self._git.head(folder)
        except CodeError as exc:
            keep_settings_change(exc)
            return None

    # --- push and check ---------------------------------------------------------------------------

    async def push(
        self,
        workspace_id: str,
        policy_revision: int,
        root_physical_id: str,
        remote: str,
        branch: str,
        head_oid: str,
    ) -> PushResult:
        """Push `head_oid` to the remote's `branch`, never forced."""
        if not valid_branch(branch):
            raise CodeError("unsupported_branch")
        _require_oid(head_oid)
        folder, root = await asyncio.to_thread(
            self._push_target,
            workspace_id,
            policy_revision,
            root_physical_id,
            remote,
            branch,
            head_oid,
        )
        async with held(self._coordinator, root, self._resolve_root):
            return await asyncio.to_thread(
                self._push, workspace_id, folder, remote, branch, head_oid
            )

    def _push_target(
        self,
        workspace_id: str,
        policy_revision: int,
        root_physical_id: str,
        remote: str,
        branch: str,
        head_oid: str,
    ) -> tuple[Path, OrdinaryMutationRoot]:
        with self._git.scope():  # one operation before the lease; `_push` checks again under it
            folder = self._git.repository(workspace_id)
            root = admit(self._resolve_root, workspace_id, policy_revision, root_physical_id)
            self._require(folder, remote, branch, head_oid)
        return folder, root

    def _remote(self, folder: Path, remote: str, branch: str) -> None:
        """The remote `branch` pushes to is ready and is the one the client reviewed."""
        selected, state = select_remote(self._git, folder, branch)
        if state == "none":
            raise CodeError("no_remote")
        if selected is None:
            raise CodeError("remote_unsupported")
        if selected.name != remote:
            raise CodeError("stale_review")

    def _require(self, folder: Path, remote: str, branch: str, head_oid: str) -> None:
        self._remote(folder, remote, branch)
        found = self._git.answer(
            folder, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}^{{commit}}"
        )
        if found is None or found.splitlines() != [head_oid]:
            raise CodeError("stale_review")

    def _bot_branch(self, workspace_id: str, branch: str) -> bool:
        """Whether `branch` is, as the store says now, the branch of a Bot code copy that is not
        removed. An ambiguous mapping is a `StateError`, never a guess."""
        row = self._store.worktree_by_branch(workspace_id, branch)
        return row is not None and is_bot_copy(self._store, row["worktree_id"])

    def _push(
        self, workspace_id: str, folder: Path, remote: str, branch: str, head_oid: str
    ) -> PushResult:
        """Under the lease: the Git settings and the expectations again, then one push, with
        hooks off for a Bot copy's branch. After a failure the remote's branch says what
        happened; after a timeout only reaching `head_oid` is certain."""
        with self._git.operation(folder, fresh=True):
            self._require(folder, remote, branch, head_oid)
            with hooks_off(self._bot_branch(workspace_id, branch)) as hooks:
                return self._push_checked(folder, remote, branch, head_oid, hooks)

    def _push_checked(
        self, folder: Path, remote: str, branch: str, head_oid: str, hooks: tuple[str, ...]
    ) -> PushResult:
        try:
            self._git.git(
                folder,
                *hooks,
                *("push", "--porcelain", "--no-follow-tags", "--recurse-submodules=no"),
                *(remote, f"{head_oid}:refs/heads/{branch}"),
                timeout=PUSH_TIMEOUT_S,
            )
        except RuntimeError as exc:
            _failed("push", exc)
            timed_out = False
        except Exception as exc:  # TimeoutExpired, which TID251 bans naming
            keep_settings_change(exc)
            _failed("push", exc)
            timed_out = True
        else:
            return PushResult("pushed", head_oid)
        if not timed_out:
            if remote_oid(self._git, folder, remote, branch) == head_oid:  # push_failed passes
                return PushResult("pushed", head_oid)
            raise CodeError("push_rejected")
        try:
            found = remote_oid(self._git, folder, remote, branch)
        except CodeError as exc:
            keep_settings_change(exc)
            found = None
        return PushResult("pushed" if found == head_oid else "unknown", found)

    async def check(
        self, workspace_id: str, remote: str, branch: str, head_oid: str
    ) -> CheckResult:
        """Whether the remote's `branch` is at `head_oid`; every platform, no lease."""
        if not valid_branch(branch):
            raise CodeError("unsupported_branch")
        _require_oid(head_oid)
        return await asyncio.to_thread(self._check, workspace_id, remote, branch, head_oid)

    def _check(self, workspace_id: str, remote: str, branch: str, head_oid: str) -> CheckResult:
        with self._git.scope():  # one operation: one Git settings check for its commands
            folder = self._git.repository(workspace_id)
            self._remote(folder, remote, branch)
            found = remote_oid(self._git, folder, remote, branch)  # push_failed passes
        return CheckResult("pushed" if found == head_oid else "not_pushed", found)
