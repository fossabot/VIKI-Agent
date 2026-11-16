"""The checkout a review, commit or integration acts on, and the helpers each Git
mutation of a code folder takes: admission, the whole-root lease and the busy guard.

A checkout is a trusted code folder itself or one of its `ready` worktrees. Trust is checked
before any Git command; a worktree must still be a repository's top level that shares the
folder's Git common dir, so a row whose folder now holds something else is refused.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from pathlib import Path

from viki_agent.code.code_files import CodeError
from viki_agent.code.code_git import CodeGit
from viki_agent.code.worktrees import _RENAMED, valid_branch
from viki_agent.session import now_iso
from viki_agent.storage import SessionStore
from viki_agent.vaults.vault_mutation import (
    MutationCoordinator,
    MutationError,
    OrdinaryMutationRoot,
)
from viki_agent.vaults.workspace_mutation import WorkspaceMutationResolver
from viki_agent.workspaces import WorkspaceError, WorkspaceService, root_key

LEASE_WAIT_S = 10.0
# The `--git-path` names whose presence means a merge, cherry-pick, revert or rebase is underway.
_IN_PROGRESS = ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply")


@dataclass(frozen=True)
class Checkout:
    workspace_id: str
    worktree_id: str | None
    folder: Path  # the code folder's root
    root: Path  # where Git runs: the folder, or the worktree's folder
    branch: str | None  # None on a detached head
    head_oid: str


def checkout(
    store: SessionStore, git: CodeGit, workspace_id: str, worktree_id: str | None
) -> Checkout:
    """Inside the caller's `git.scope()`, when it has one, the folder's and the worktree's Git
    settings checks made here also cover the caller's own commands on them."""
    with git.scope():
        folder = git.repository(workspace_id)
        root = folder
        if worktree_id is not None:
            root = _worktree(store, git, workspace_id, worktree_id, folder)
        branch = git.branch(root)
        if branch is not None and not valid_branch(branch):
            raise CodeError("unsupported_branch")
        return Checkout(workspace_id, worktree_id, folder, root, branch, git.head(root))


def _worktree(
    store: SessionStore, git: CodeGit, workspace_id: str, worktree_id: str, folder: Path
) -> Path:
    row = store.get_worktree(worktree_id)
    if row is None or row["workspace_id"] != workspace_id:
        raise CodeError("unknown_worktree")
    root = Path(row["path"])
    if row["state"] != "ready" or not root.is_dir():
        raise CodeError("worktree_not_ready")
    answer = git.answer(root, "rev-parse", "--is-inside-work-tree", "--show-prefix")
    lines = answer.splitlines() if answer is not None else []
    if lines[:1] != ["true"] or any(lines[1:2]):
        raise CodeError("worktree_moved")
    common = _common_dir(git, root)
    if common is None or common != _common_dir(git, folder):
        raise CodeError("worktree_moved")
    return root


def _common_dir(git: CodeGit, cwd: Path) -> str | None:
    """The key of the Git common dir as seen from `cwd`, or None when Git or the path does not
    answer. Non-strict `realpath` can still raise, such as on a dropped network share."""
    answer = git.answer(cwd, "rev-parse", "--git-common-dir")
    lines = answer.splitlines() if answer is not None else []
    if len(lines) != 1 or not lines[0]:
        return None
    try:
        return root_key(os.path.realpath(cwd / lines[0]))
    except OSError:
        return None


def in_progress(git: CodeGit, root: Path) -> bool:
    """Whether a merge, cherry-pick, revert or rebase is underway in `root`. Each path is only
    tested for existence; nothing is opened."""
    args = [arg for name in _IN_PROGRESS for arg in ("--git-path", name)]
    lines = git.output(root, "rev-parse", *args).splitlines()
    if len(lines) != len(_IN_PROGRESS) or not all(lines):
        raise CodeError("git_failed")
    # A worktree's answers are absolute, which the join keeps.
    return any((root / line).exists() for line in lines)


async def busy_in(
    store: SessionStore,
    busy: Callable[[str], bool],
    workspace_id: str,
    worktree_id: str | None,
) -> bool:
    """Whether an app session using this checkout has a turn running: a worktree's rows, its
    bound Bot chat and every session of its bound Bot thread, or the folder's rows outside its
    worktrees."""
    if worktree_id is None:
        ids = await asyncio.to_thread(store.session_roots_in_folder, workspace_id)
    else:
        ids = await asyncio.to_thread(store.sessions_using_worktree, worktree_id)
    return any(busy(session_id) for session_id in ids)


def is_bot_copy(store: SessionStore, worktree_id: str | None) -> bool:
    """Whether the worktree is a Bot code copy, bound, released or `former` (its release
    finished, its archive maybe restored since), as the store says now."""
    return worktree_id is not None and worktree_id in store.copy_states([worktree_id])


@contextmanager
def hooks_off(off: bool) -> Iterator[tuple[str, ...]]:
    """`-c core.hooksPath=<an empty private folder>` when `off`, so no repository hook runs
    for a Bot copy's Git writes; nothing otherwise, so the user's hooks run as they set them.
    An `OSError` making the folder is `git_unavailable`, before any Git write."""
    if not off:
        yield ()
        return
    try:
        tmp = tempfile.TemporaryDirectory(prefix="viki-hooks-", ignore_cleanup_errors=True)
    except OSError:
        raise CodeError("git_unavailable") from None
    with tmp as folder:
        yield ("-c", f"core.hooksPath={folder}")


def keep_settings_change(exc: BaseException) -> None:
    """Re-raise a Git-settings refusal that a broader catch would otherwise fold away."""
    if isinstance(exc, CodeError) and exc.code == "git_settings_changed":
        raise exc


def admit(
    resolve_root: WorkspaceMutationResolver,
    workspace_id: str,
    policy_revision: int,
    root_physical_id: str,
) -> OrdinaryMutationRoot:
    try:
        return resolve_root(workspace_id, policy_revision, root_physical_id, kind="code")
    except MutationError as exc:
        raise CodeError(_RENAMED.get(exc.code, exc.code)) from None


@asynccontextmanager
async def held(
    coordinator: MutationCoordinator,
    root: OrdinaryMutationRoot,
    resolve_root: WorkspaceMutationResolver,
) -> AsyncIterator[None]:
    """The whole root and its Git common dir, then the exact admission snapshot again.

    A wait can outlast trust in the folder. No body Git call runs before this second check.
    """
    try:
        async with coordinator.lease(root, (), include_git_common=True, wait_s=LEASE_WAIT_S):
            current = await asyncio.to_thread(
                admit, resolve_root, root.workspace_id, root.policy_revision, root.physical_id
            )
            if current != root:
                raise CodeError("workspace_changed")
            yield
    except MutationError as exc:
        raise CodeError(_RENAMED.get(exc.code, exc.code)) from None


class GitSettings:
    """A trusted code folder's saved Git-settings baseline: read its fingerprint, or confirm the
    current settings in place of the one the user reviewed. Confirming never creates a
    baseline, and the current fingerprint comes from the guard's own safe admission, whatever
    the user accepted. `admission_lock` is the app's shared one; without it confirmations
    serialize on a lock of their own."""

    def __init__(
        self,
        workspaces: WorkspaceService,
        store: SessionStore,
        git: CodeGit,
        coordinator: MutationCoordinator,
        resolve_root: WorkspaceMutationResolver,
        *,
        admission_lock: asyncio.Lock | None = None,
    ) -> None:
        self._workspaces = workspaces
        self._store = store
        self._git = git
        self._coordinator = coordinator
        self._resolve_root = resolve_root
        self._admission_lock = admission_lock if admission_lock is not None else asyncio.Lock()

    def _trusted(self, workspace_id: str) -> None:
        """A registered code folder (`unknown_workspace`, 404) that is trusted."""
        try:
            workspace = self._workspaces.get(workspace_id, kind="code")
        except WorkspaceError:
            raise CodeError("unknown_workspace") from None
        if workspace.policy != "ordinary":
            raise CodeError("trust_required")

    def saved(self, workspace_id: str) -> str | None:
        """The stored fingerprint of a trusted code folder; no Git and no settings read."""
        self._trusted(workspace_id)
        baseline = self._store.bot_code_baseline(workspace_id)
        return None if baseline is None else baseline["git_fingerprint"]

    async def confirm(
        self, workspace_id: str, policy_revision: int, root_physical_id: str, expected: str
    ) -> str:
        """Replace the baseline `expected` names with the current settings' fingerprint."""
        async with self._admission_lock:
            await asyncio.to_thread(self._trusted, workspace_id)
            root = await asyncio.to_thread(
                admit, self._resolve_root, workspace_id, policy_revision, root_physical_id
            )
            async with held(self._coordinator, root, self._resolve_root):
                return await asyncio.to_thread(self._replace, root, expected)

    def _replace(self, root: OrdinaryMutationRoot, expected: str) -> str:
        # The stored value first, so a stale or absent baseline runs no Git at all; the update
        # then names `expected` again, so a replacement in between still refuses.
        baseline = self._store.bot_code_baseline(root.workspace_id)
        if baseline is None or baseline["git_fingerprint"] != expected:
            raise CodeError("baseline_changed")
        current = self._git.settings_fingerprint(root.path)
        if not self._store.replace_bot_code_baseline(
            root.workspace_id, expected, current, now_iso()
        ):
            raise CodeError("baseline_changed")
        return current
