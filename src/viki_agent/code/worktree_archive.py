"""Git plumbing for the worktree cleanup's archives.

An archive is a commit shaped like `git stash`'s: its tree holds what `git add -A` takes from the
worktree (HEAD with the staged, unstaged and untracked changes, and no ignored file), its first
parent is HEAD and its second an index commit, whose tree is the index and whose one parent is
HEAD. `refs/viki/archive/<worktree id>` names it. `snapshot` builds both trees in a private
temporary index, a copy of the worktree's own, which it reads and does not write; it refuses an
index that holds a nested repository's entry, which `add -A` would start Git inside.

Every write here runs with `core.hooksPath` at an empty private folder (the `index=` calls get
theirs from `winproc.git`), so no repository hook runs; commits carry VIKI Agent's identity and
`--no-gpg-sign`, so no signing program runs; each `update-ref` names the old value it expects,
and `--no-deref` makes it change the ref it names rather than a symbolic ref's target. A Git or
OS failure is `ArchiveError("git_failed")`, logged by subcommand and exception type only.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from viki_agent.code.code_files import CodeError
from viki_agent.code.code_git import GIT_TIMEOUT_S, CodeGit
from viki_agent.code.git_guard import git_operation

PREFIX = "refs/viki/archive/"
FILES_TIMEOUT_S = 120.0  # `add -A` and `checkout-index -a`, which read or write every file
# Passed before the subcommand of every Git call here, and in `worktree_probe`, that reads or
# writes the work tree or its index: Git for Windows skips a path past MAX_PATH otherwise.
LONG_PATHS: tuple[str, ...] = ("-c", "core.longpaths=true") if sys.platform == "win32" else ()

_LOGGER = logging.getLogger(__name__)
_OID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_DATE = re.compile(r"[0-9]*")
_IDENTITY = ("-c", "user.name=VIKI Agent", "-c", "user.email=viki-agent@invalid")


class ArchiveError(Exception):
    """A failed archive step: only `.code`, never a path."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Snapshot:
    """A worktree as Git objects: HEAD, the sha256 of its index file (None without one), the
    index's tree and the tree of what `add -A` takes."""

    head: str
    index_digest: str | None
    index_tree: str
    tree: str


@dataclass(frozen=True)
class Archive:
    worktree_id: str
    oid: str
    archived_at: str


def _run(
    git: CodeGit,
    root: Path,
    *args: str,
    config: tuple[str, ...] = (),
    timeout: float = GIT_TIMEOUT_S,
    index: Path | None = None,
) -> str:
    """`CodeGit.git` with `config` before the subcommand; settings refusals propagate."""
    try:
        if index is None:
            return git.git(root, *config, *args, timeout=timeout)
        return git.git(root, *config, *args, timeout=timeout, index=index)
    except CodeError as exc:
        if exc.code == "git_settings_changed":
            raise
        raise ArchiveError("git_failed") from None
    except Exception as exc:  # RuntimeError, or TimeoutExpired (TID251 bans naming it)
        # Only the subcommand and exception type: a message may hold a path or git stderr.
        _LOGGER.warning("worktree archive: git %s failed (%s)", args[0], type(exc).__name__)
        raise ArchiveError("git_failed") from None


def _oid(raw: str) -> str:
    lines = raw.splitlines()
    if len(lines) != 1 or not _OID.fullmatch(lines[0]):
        raise ArchiveError("git_failed")
    return lines[0]


def _old_value(oid: str) -> None:
    """An `update-ref -d` old value: all zeros there would delete without any check."""
    if not _OID.fullmatch(oid) or not oid.strip("0"):
        raise ValueError("not an object id")


@contextmanager
def _hooks_off() -> Iterator[tuple[str, ...]]:
    """`-c core.hooksPath=<an empty private folder>`, so no repository hook runs. An `OSError`
    making that folder (no usable temp folder, a full disk) is `ArchiveError("git_failed")`,
    like every other failure here."""
    try:
        tmp = tempfile.TemporaryDirectory(prefix="viki-archive-", ignore_cleanup_errors=True)
    except OSError:
        raise ArchiveError("git_failed") from None
    with tmp as folder:
        yield ("-c", f"core.hooksPath={folder}")


def _copy_index(source: Path, target: Path) -> str | None:
    """Copy the index file `source` to `target` with its mtime, and answer the sha256 of the
    bytes copied; None when there is no index. Git compares an entry's content, not only its
    stat data, while the entry is as new as the index file, so the copy keeps the original's
    mtime, read before the bytes and so no newer than they are."""
    try:
        stat = source.stat()
        data = source.read_bytes()
    except FileNotFoundError:
        return None
    target.write_bytes(data)
    os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    return hashlib.sha256(data).hexdigest()


@git_operation
def snapshot(git: CodeGit, tree: Path) -> Snapshot:
    """The worktree `tree` as a `Snapshot`, built in a private temporary index that starts as a
    copy of its own index (HEAD's tree when it has none): that index's tree, then `add -A` there
    and the tree of the result. An index that holds a nested repository's entry is refused:
    to tell whether one changed, `add -A` starts Git inside it, under settings of its own."""
    head = _oid(_run(git, tree, "rev-parse", "--verify", "HEAD^{commit}"))
    found = _run(git, tree, "rev-parse", "--git-path", "index").splitlines()
    if len(found) != 1 or not found[0]:
        raise ArchiveError("git_failed")
    try:
        tmp = tempfile.TemporaryDirectory(prefix="viki-archive-", ignore_cleanup_errors=True)
    except OSError:
        raise ArchiveError("git_failed") from None
    with tmp as folder:
        index = Path(folder) / "index"
        try:
            digest = _copy_index(tree / found[0], index)  # an absolute answer stays as it is
        except OSError:
            raise ArchiveError("git_failed") from None
        if digest is None:
            _run(git, tree, "read-tree", head, index=index, config=LONG_PATHS)
        staged = _run(git, tree, "ls-files", "-z", "--stage", index=index, config=LONG_PATHS)
        if any(entry.startswith("160000 ") for entry in staged.split("\0")):
            raise ArchiveError("git_failed")
        index_tree = _oid(_run(git, tree, "write-tree", index=index))
        _run(git, tree, "add", "-A", timeout=FILES_TIMEOUT_S, index=index, config=LONG_PATHS)
        files_tree = _oid(_run(git, tree, "write-tree", index=index))
    return Snapshot(head, digest, index_tree, files_tree)


@git_operation
def save(
    git: CodeGit, code_root: Path, worktree_id: str, snap: Snapshot, *, branch: str | None
) -> str:
    """Commit `snap` as the archive of the worktree `worktree_id` and point its ref at it,
    moving the ref only from the value read here (zeros when it is absent). Answers the
    archive commit."""
    ref = PREFIX + worktree_id
    with _hooks_off() as hooks:
        config = (*hooks, *_IDENTITY)
        index_commit = _oid(
            _run(
                git,
                code_root,
                "commit-tree",
                "--no-gpg-sign",
                "-p",
                snap.head,
                "-m",
                "VIKI Agent archive: index",
                snap.index_tree,
                config=config,
            )
        )
        archive = _oid(
            _run(
                git,
                code_root,
                "commit-tree",
                "--no-gpg-sign",
                "-p",
                snap.head,
                "-p",
                index_commit,
                "-m",
                f"VIKI Agent archive of {branch or 'detached HEAD'}",
                snap.tree,
                config=config,
            )
        )
        current = _run(git, code_root, "for-each-ref", "--format=%(objectname)", ref).splitlines()
        if len(current) > 1:
            raise ArchiveError("git_failed")
        old = _oid(current[0]) if current else "0" * len(archive)
        _run(git, code_root, "update-ref", "--no-deref", ref, archive, old, config=hooks)
    return archive


@git_operation
def restore_files(git: CodeGit, tree: Path, oid: str) -> None:
    """Check the archive `oid` out into `tree`, a fresh `--no-checkout` worktree: the archive's
    tree into the index and onto disk, then the index commit's tree into the index, so the
    staged, unstaged and untracked changes come back as a checkout writes them."""
    with _hooks_off() as hooks:
        config = (*hooks, *LONG_PATHS)
        _run(git, tree, "read-tree", f"{oid}^{{tree}}", config=config)
        _run(git, tree, "checkout-index", "-a", config=config, timeout=FILES_TIMEOUT_S)
        _run(git, tree, "read-tree", f"{oid}^2^{{tree}}", config=config)


def _commit_and_parents(git: CodeGit, root: Path, rev: str) -> list[str] | None:
    """The commit `rev` names, then its parents; None when Git finds no such commit (`rev-list`
    answers nothing, with exit 0, for a tree or blob, hence the `^{commit}`)."""
    try:
        raw = git.answer(root, "rev-list", "--parents", "-n", "1", f"{rev}^{{commit}}", "--")
    except CodeError as exc:
        if exc.code == "git_settings_changed":
            raise
        raise ArchiveError("git_failed") from None
    if raw is None:
        return None
    lines = raw.splitlines()
    if len(lines) != 1 or not all(_OID.fullmatch(oid) for oid in lines[0].split(" ")):
        raise ArchiveError("git_failed")
    return lines[0].split(" ")


@git_operation
def parents(git: CodeGit, code_root: Path, ref: str) -> tuple[str, str] | None:
    """(HEAD, index commit) of the archive `ref`, a ref name or a commit id; None unless it is a
    commit with exactly two parents whose second has one parent, equal to the first."""
    archive = _commit_and_parents(git, code_root, ref)
    if archive is None or len(archive) != 3:
        return None
    index = _commit_and_parents(git, code_root, archive[2])
    if index is None or index[1:] != [archive[1]]:
        return None
    return archive[1], archive[2]


def _stamp(epoch: int) -> str:
    """`epoch` in `session.now_iso`'s format."""
    try:
        moment = datetime.fromtimestamp(epoch, UTC)
    except OverflowError, OSError, ValueError:
        raise ArchiveError("git_failed") from None
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


@git_operation
def list_archives(git: CodeGit, code_root: Path) -> list[Archive]:
    """Every archive ref with its commit's committer date. A ref whose name after `PREFIX` is not
    a canonical lower-case UUID, or which names no commit (so has no committer date), is
    skipped; a malformed line is `git_failed`."""
    raw = _run(
        git,
        code_root,
        "for-each-ref",
        "--format=%(refname)%00%(objectname)%00%(committerdate:unix)",
        PREFIX,
    )
    if raw and not raw.endswith("\n"):
        raise ArchiveError("git_failed")
    archives: list[Archive] = []
    for line in raw.split("\n")[:-1]:
        fields = line.split("\0")
        if (
            len(fields) != 3
            or not fields[0].startswith(PREFIX)
            or not _OID.fullmatch(fields[1])
            or not _DATE.fullmatch(fields[2])
        ):
            raise ArchiveError("git_failed")
        name, oid, date = fields
        worktree_id = name.removeprefix(PREFIX)
        if _UUID.fullmatch(worktree_id) and date:
            archives.append(Archive(worktree_id, oid, _stamp(int(date))))
    return archives


@git_operation
def drop(git: CodeGit, code_root: Path, ref: str, oid: str) -> None:
    """Delete the archive ref `ref` only while it names `oid`."""
    _old_value(oid)
    with _hooks_off() as hooks:
        _run(git, code_root, "update-ref", "--no-deref", "-d", ref, oid, config=hooks)


@git_operation
def delete_branch(git: CodeGit, code_root: Path, branch: str, head: str) -> bool:
    """Delete the branch `branch` only while it names `head`. False, with nothing written, while
    a `refs/remotes/*/<branch>` exists or a worktree, the main folder included, has it checked
    out: `update-ref` itself deletes a branch checked out elsewhere."""
    _old_value(head)
    remote = _run(git, code_root, "for-each-ref", "--format=%(refname)", f"refs/remotes/*/{branch}")
    if remote.strip():
        return False
    held = _run(git, code_root, "for-each-ref", "--format=%(worktreepath)", f"refs/heads/{branch}")
    if held.strip():
        return False
    with _hooks_off() as hooks:
        _run(
            git,
            code_root,
            "update-ref",
            "--no-deref",
            "-d",
            f"refs/heads/{branch}",
            head,
            config=hooks,
        )
    return True
