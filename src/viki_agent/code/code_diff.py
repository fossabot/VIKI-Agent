"""The diff of one file in a checkout of a trusted code folder: its worktree against HEAD,
or the change between two commits.

Both sides are sized before `git diff` starts, so a huge file never reaches Git. The
diff runs with the external diff driver and textconv off, so a repository's own config and
`.gitattributes` start nothing.
"""

from __future__ import annotations

import errno
import logging
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from viki_agent.code.code_files import CodeError, CodeFiles, is_private, resolve_path_at, split_ref
from viki_agent.code.code_git import _OID, CodeGit

MAX_DIFF_SOURCE_BYTES = 1_048_576

_LOGGER = logging.getLogger(__name__)
_DIFF_FLAGS = ("--no-color", "--no-ext-diff", "--no-textconv", "--unified=3")

Reason = Literal["binary", "too_large", "no_commits", "no_diff"]


@dataclass(frozen=True)
class FileDiff:
    path: str
    text: str | None
    reason: Reason | None


def _unavailable(subcommand: str, exc: Exception) -> CodeError:
    if isinstance(exc, CodeError) and exc.code == "git_settings_changed":
        return exc
    # Only the subcommand and exception type: a message may hold a path or git stderr.
    _LOGGER.warning("code: git %s failed (%s)", subcommand, type(exc).__name__)
    return CodeError("git_unavailable")


def _worktree_size(path: Path) -> int:
    """The file's size, 0 when it is gone. A folder is refused: its pathspec would diff every
    file under it, and its own size says nothing about that."""
    try:
        info = path.lstat()
    except FileNotFoundError, NotADirectoryError:
        return 0
    except OSError as error:
        # Windows answers EINVAL for a name it cannot hold (`*`, `?`): no such file, and git
        # still gets the ref literally.
        if error.errno == errno.EINVAL:
            return 0
        raise CodeError("unavailable") from None
    if stat.S_ISDIR(info.st_mode):
        raise CodeError("not_a_file")
    return info.st_size


def _probe(git: CodeGit, root: Path, *args: str) -> str | None:
    """The output of a question git answers by its exit code: None when it exits non-zero. A
    timeout or any other failure is `git_unavailable`, not an answer."""
    try:
        return git.git(root, *args)
    except RuntimeError:
        return None
    except Exception as exc:  # TimeoutExpired, which TID251 bans naming
        raise _unavailable(args[0], exc) from None


def _parts(ref: str) -> tuple[str, ...]:
    """The parts of a ref naming a file that may be shown. Pure string work."""
    parts = split_ref(ref)
    if not parts:
        raise CodeError("invalid_path")
    if is_private(parts):
        raise CodeError("private_path")
    return parts


def _too_large_at(git: CodeGit, root: Path, rev: str, ref: str) -> bool:
    """Whether `ref` at `rev` is a blob over the limit; absent there is not. A tree has a small
    object size but a pathspec that diffs every file under it, so it is `not_a_file`."""
    found = _probe(git, root, "cat-file", "-t", f"{rev}:{ref}")
    if found is None:
        return False
    if found.strip() != "blob":
        raise CodeError("not_a_file")
    size = _probe(git, root, "cat-file", "-s", f"{rev}:{ref}")
    return size is not None and int(size) > MAX_DIFF_SOURCE_BYTES


def diff(git: CodeGit, files: CodeFiles, workspace_id: str, ref: str) -> FileDiff:
    """Worktree against HEAD for `ref`. An untracked file is absent from HEAD, so it answers
    `no_diff`. `files` is part of the call shape: `git.repository` resolves the root through it."""
    _parts(ref)
    with git.workspace_operation(workspace_id):
        return diff_at(git, git.repository(workspace_id), ref)


def diff_at(
    git: CodeGit, root: Path, ref: str, *, between: tuple[str, str] | None = None
) -> FileDiff:
    """`ref` in the checkout at `root`: its worktree against HEAD, or with `between` the change
    from one commit to another, which reads no worktree. `between` holds two full object ids, so
    no revision reaches Git as an option; anything else is `ValueError` before any Git call."""
    parts = _parts(ref)
    if between is not None and not all(_OID.fullmatch(oid) for oid in between):
        raise ValueError("between needs two full object ids")
    with git.operation(root):
        return _diff_at(git, root, ref, parts, between=between)


def _diff_at(
    git: CodeGit,
    root: Path,
    ref: str,
    parts: tuple[str, ...],
    *,
    between: tuple[str, str] | None = None,
) -> FileDiff:
    if between is None:
        resolve_path_at(root, ref, strict=False)
        if _worktree_size(root.joinpath(*parts)) > MAX_DIFF_SOURCE_BYTES:
            return FileDiff(ref, None, "too_large")
        if _probe(git, root, "rev-parse", "--verify", "--quiet", "HEAD^{commit}") is None:
            return FileDiff(ref, None, "no_commits")
        revs: tuple[str, ...] = ("HEAD",)
    else:
        revs = between
    if any(_too_large_at(git, root, rev, ref) for rev in revs):
        return FileDiff(ref, None, "too_large")
    try:
        text = git.git(root, "diff", *_DIFF_FLAGS, *revs, "--", ref)
    except Exception as exc:  # RuntimeError, or TimeoutExpired (TID251 bans naming it)
        raise _unavailable("diff", exc) from None
    if not text:
        return FileDiff(ref, None, "no_diff")
    if any(line.startswith("Binary files ") for line in text.splitlines()):
        return FileDiff(ref, None, "binary")
    return FileDiff(ref, text, None)
