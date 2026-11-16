"""Commit the paths the user approved in a review.

Every client value is checked before any Git command, and under the whole-root lease the
checkout must still be what the user reviewed: the same branch and head, nothing else staged,
and each approved path a current `text` change with the digest the review showed. Only then are
exactly those paths staged and committed, with the user's hooks and Git identity; the app sets
no identity. A Bot code copy's add and commit run with `core.hooksPath` at an empty private
folder instead, so no repository hook runs there, and with a copy's other options
(`CodeGit.operation`): a signing program named by a path inside the copy is not started, so a
commit that must be signed is refused there. A hook may still change what lands, so the
result names the approved paths a hook
rewrote and any other path the commit holds. Git runs through `CodeGit`, so `HARDENING` applies;
a failing Git call is logged by its subcommand and exception type only.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

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
from viki_agent.code.code_git import CodeGit
from viki_agent.code.code_range import OID, TreeEntry, nul_fields, tree_entries
from viki_agent.code.code_review import CodeReview
from viki_agent.storage import SessionStore
from viki_agent.vaults.vault_mutation import (
    MutationCoordinator,
    MutationError,
    OrdinaryMutationRoot,
    code_ref,
)
from viki_agent.vaults.workspace_mutation import WorkspaceMutationResolver

ADD_TIMEOUT_S = 30.0
COMMIT_TIMEOUT_S = 120.0
MAX_COMMIT_PATHS = 200
MAX_COMMIT_PATH_CHARS = 16_000
MAX_MESSAGE_CHARS = 4000

_PATH_CODES = {"invalid_ref": "invalid_path", "excluded_path": "private_path"}


@dataclass(frozen=True)
class CommitResult:
    head_oid: str
    # Approved paths whose committed blob is not the one VIKI's add staged: a hook, or another
    # program after the post-add checks, rewrote it. The commit still lands, so the user is told.
    hook_changed: tuple[str, ...]
    unexpected: tuple[str, ...]  # paths the commit changed outside the approval


def _check_message(message: str) -> None:
    if not 1 <= len(message) <= MAX_MESSAGE_CHARS or not message.strip() or "\0" in message:
        raise CodeError("invalid_message")
    try:
        message.encode("utf-8")
    except UnicodeEncodeError:
        raise CodeError("invalid_message") from None


def _approval(paths: Sequence[tuple[str, str]]) -> dict[str, str]:
    """Path to the digest the review showed, each path a code ref."""
    approval = dict(paths)
    if (
        not 1 <= len(paths) <= MAX_COMMIT_PATHS
        or len(approval) != len(paths)
        or sum(len(path) for path in approval) > MAX_COMMIT_PATH_CHARS
    ):
        raise CodeError("too_many_paths")
    for path in approval:
        try:
            code_ref(path)
        except MutationError as refused:
            raise CodeError(_PATH_CODES[refused.code]) from None
    return approval


def _fields(raw: str) -> list[str]:
    try:
        return nul_fields(raw)
    except ValueError:
        raise CodeError("git_failed") from None


def _blob(entry: TreeEntry | None) -> str | None:
    return entry.oid if entry is not None and entry.type == "blob" else None


class CodeCommitter:
    def __init__(
        self,
        review: CodeReview,
        git: CodeGit,
        store: SessionStore,
        coordinator: MutationCoordinator,
        resolve_root: WorkspaceMutationResolver,
        *,
        busy: Callable[[str], bool],
    ) -> None:
        self._review = review
        self._git = git
        self._store = store
        self._coordinator = coordinator
        self._resolve_root = resolve_root
        self._busy = busy

    async def commit(
        self,
        workspace_id: str,
        worktree_id: str | None,
        policy_revision: int,
        root_physical_id: str,
        branch: str,
        head_oid: str,
        message: str,
        paths: Sequence[tuple[str, str]],
    ) -> CommitResult:
        _check_message(message)
        approval = _approval(paths)
        work, root = await asyncio.to_thread(
            self._target, workspace_id, worktree_id, policy_revision, root_physical_id
        )
        if await busy_in(self._store, self._busy, workspace_id, worktree_id):
            raise CodeError("checkout_busy")
        async with held(self._coordinator, root, self._resolve_root):
            if await busy_in(self._store, self._busy, workspace_id, worktree_id):
                raise CodeError("checkout_busy")
            return await asyncio.to_thread(self._commit, work, branch, head_oid, message, approval)

    def _target(
        self,
        workspace_id: str,
        worktree_id: str | None,
        policy_revision: int,
        root_physical_id: str,
    ) -> tuple[Checkout, OrdinaryMutationRoot]:
        with self._git.scope():  # one operation before the lease; `_commit` checks again under it
            work = checkout(self._store, self._git, workspace_id, worktree_id)
            root = admit(self._resolve_root, workspace_id, policy_revision, root_physical_id)
            # The user's identity or none; Git would otherwise guess one from the host.
            for name in ("GIT_AUTHOR_IDENT", "GIT_COMMITTER_IDENT"):
                if self._git.answer(work.root, "var", name) is None:
                    raise CodeError("identity_missing")
        return work, root

    def _commit(
        self, work: Checkout, branch: str, head_oid: str, message: str, approval: dict[str, str]
    ) -> CommitResult:
        """Under the lease: the Git settings and the review's expectations again, then `add`
        and `commit`, with hooks off in a Bot copy."""
        with self._git.operation(work.root, fresh=True):
            if work.worktree_id is not None:
                row = self._store.get_worktree(work.worktree_id)
                if row is None or row["state"] != "ready" or not Path(row["path"]).is_dir():
                    raise CodeError("worktree_not_ready")
            with hooks_off(is_bot_copy(self._store, work.worktree_id)) as hooks:
                return self._land(work, branch, head_oid, message, approval, hooks)

    def _land(
        self,
        work: Checkout,
        branch: str,
        head_oid: str,
        message: str,
        approval: dict[str, str],
        hooks: tuple[str, ...],
    ) -> CommitResult:
        root = work.root
        current = self._git.branch(root)
        if current is None:
            raise CodeError("detached_head")
        if current != branch or self._git.head(root) != head_oid:
            raise CodeError("stale_review")
        if in_progress(self._git, root):
            raise CodeError("in_progress")
        if not self._staged(root) <= approval.keys():
            raise CodeError("unrelated_staged")
        self._require_reviewed(work, approval, not_text="unsupported_path")

        paths = list(approval)
        self._git.output(root, *hooks, "add", "--", *paths, timeout=ADD_TIMEOUT_S)
        # The blobs are taken first, so a stage by a hook, or by another program after the checks
        # below, shows in `hook_changed`: those checks cannot see one that lands after them, and
        # one that lands before them is `stale_review`. From here a refusal leaves the add in the
        # index. Exactly the approved
        # paths staged rules out another program's stage before the add; the digests again prove
        # the worktree still holds the reviewed bytes (a write before the add changes them), and
        # the empty diff that the index equals that worktree. Only digests are compared: a clean
        # filter or `core.autocrlf` makes the staged blob differ from the worktree bytes.
        staged = self._staged_blobs(root, paths)
        if self._staged(root) != approval.keys():
            raise CodeError("stale_review")
        self._require_reviewed(work, approval, not_text="stale_review")
        if self._git.output(root, "diff", "--name-only", "-z", "--no-renames", "--", *paths):
            raise CodeError("stale_review")
        try:
            self._git.output(
                root, *hooks, "commit", f"--message={message}", timeout=COMMIT_TIMEOUT_S
            )
        except CodeError as exc:  # a refusal or a timeout: HEAD tells what landed
            keep_settings_change(exc)
        new = self._git.head(root)
        if new == head_oid:
            raise CodeError("commit_refused")

        committed = tree_entries(self._git, root, new, paths)
        hook_changed = sorted(
            path for path in paths if staged.get(path) != _blob(committed.get(path))
        )
        changed = set(
            _fields(
                self._git.output(
                    root, "diff", "--name-only", "-z", "--no-renames", head_oid, new, "--"
                )
            )
        )
        return CommitResult(new, tuple(hook_changed), tuple(sorted(changed - approval.keys())))

    def _require_reviewed(self, work: Checkout, approval: dict[str, str], *, not_text: str) -> None:
        """Each approved path a current `text` change with the digest the review showed; a path
        that is no longer `text` refuses `not_text`."""
        found = {entry.path: entry for entry in self._review.entries(work, only=approval).items}
        for path, digest in approval.items():
            entry = found.get(path)
            if entry is None:
                raise CodeError("stale_review")
            if entry.kind != "text":
                raise CodeError(not_text)
            if entry.digest != digest:
                raise CodeError("stale_review")

    def _staged(self, root: Path) -> set[str]:
        return set(
            _fields(self._git.output(root, "diff", "--cached", "--name-only", "-z", "--no-renames"))
        )

    def _staged_blobs(self, root: Path, paths: list[str]) -> dict[str, str]:
        """Each staged path's blob id; a staged deletion is absent."""
        blobs: dict[str, str] = {}
        for record in _fields(self._git.output(root, "ls-files", "-s", "-z", "--", *paths)):
            meta, _, path = record.partition("\t")
            fields = meta.split(" ")
            if (
                len(fields) != 3
                or not OID.fullmatch(fields[1])
                or fields[2] != "0"
                or path in blobs
            ):
                raise CodeError("git_failed")
            blobs[path] = fields[1]
        if not blobs.keys() <= set(paths):
            raise CodeError("git_failed")
        return blobs
