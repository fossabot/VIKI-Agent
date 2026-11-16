"""Git and disk facts about one worktree, for the cleanup policy in `worktree_rules`.

`inspect` reads real Git state and walks the tree to build a `worktree_rules.Facts`; it never
raises, fails closed to the all-false, zero `Facts()` ("unreadable") on any Git or parse failure,
so a cleanup decision never trusts a partial guess. `has_process_inside` is the per-OS "is
anything using this folder" check the sweep's act phase runs before it renames a tree aside.

New modules import neither `worktrees` nor `code_checkout` (an import cycle), so the in-progress
markers `code_checkout._IN_PROGRESS` names are duplicated here as `_IN_PROGRESS`; a test imports
`code_checkout` to check the two stay equal. `worktree_archive.LONG_PATHS` is passed before the
subcommand of every Git call here that reads the work tree or its index.
"""

from __future__ import annotations

import errno
import hashlib
import os
import re
import stat
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from viki_agent import osfs, winproc
from viki_agent.code.code_files import CodeError
from viki_agent.code.code_git import CodeGit, StatusRecord, chunks, status_records
from viki_agent.code.worktree_archive import LONG_PATHS
from viki_agent.code.worktree_rules import MAX_WALK_ENTRIES, WALK_TIMEOUT_S, Facts

# `code_checkout._IN_PROGRESS`, duplicated to avoid importing a module that imports `worktrees`.
_IN_PROGRESS = ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply")
# POSIX only: `os.DirEntry.stat()` answers `st_dev` 0 for every entry on Windows, where this
# check would then end every walk incomplete; a Windows mount point is a reparse point, which
# the walk already never enters.
_MOUNT_CHECK = sys.platform != "win32"
# One `rev-parse --git-path` batch: the first three feed `activity`'s extra mtimes, `modules`
# also marks a submodule, and the `_IN_PROGRESS` markers also mark `unmerged`.
_GIT_PATHS = ("index", "HEAD", "logs/HEAD", "modules", *_IN_PROGRESS)
_OID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


def inspect(git: CodeGit, tree: Path) -> Facts:
    """Git state and disk usage; settings refusals propagate, other failures give `Facts()`."""
    try:
        with git.operation(tree):
            return _inspect(git, tree)
    except CodeError as exc:
        if exc.code == "git_settings_changed":
            raise
        return Facts()
    except ValueError, OSError:
        return Facts()


def _inspect(git: CodeGit, tree: Path) -> Facts:
    # A gitlink's row stays in a Bot copy too (`CodeGit.git` reads it without entering the
    # nested repository): one staged for deletion is in no index entry, only in this status.
    headers, records = status_records(
        git.output(
            tree,
            *LONG_PATHS,
            "status",
            "--porcelain=v2",
            "-z",
            "--branch",
            "--untracked-files=all",
        )
    )
    branch = headers.get("branch.head")
    if branch == "(detached)":
        branch = None
    head = _oid(headers.get("branch.oid"))

    changes = len(records)
    unmerged = False
    submodule = False
    embedded = False
    untracked_max = untracked_total = 0
    for record in records:
        if record.type == "u":
            unmerged = True
        elif record.type == "?":
            if record.path.endswith("/"):
                embedded = True
            size = (tree / record.path).lstat().st_size
            untracked_total += size
            untracked_max = max(untracked_max, size)
        elif record.fields[2][0] == "S":
            submodule = True

    if not submodule:
        staged = git.output(tree, *LONG_PATHS, "ls-files", "-z", "--stage")
        submodule = any(entry.startswith("160000 ") for entry in staged.split("\0") if entry)

    hidden = _hidden(git, tree)
    filtered = _filtered(git, tree, records)

    refs = git.output(
        tree,
        "for-each-ref",
        "--format=%(refname)",
        "refs/worktree/",
        "refs/bisect/",
        "refs/rewritten/",
    )
    worktree_refs = bool(refs.strip())

    index_path, head_path, logs_path, modules_path, *markers = _git_paths(git, tree)
    if (tree / modules_path).exists():
        submodule = True
    if any((tree / marker).exists() for marker in markers):
        unmerged = True
    try:
        index_digest = hashlib.sha256((tree / index_path).read_bytes()).hexdigest()
    except FileNotFoundError:
        index_digest = None

    size, activity, walk_embedded = _walk(tree)
    embedded = embedded or walk_embedded
    for extra in (index_path, head_path, logs_path):
        try:
            moment = datetime.fromtimestamp((tree / extra).stat().st_mtime, tz=UTC)
        except OSError:
            continue
        if moment > activity:
            activity = moment

    unpushed = _unpushed(git, tree, branch)

    return Facts(
        readable=True,
        head=head,
        branch=branch,
        index_digest=index_digest,
        changes=changes,
        unmerged=unmerged,
        submodule=submodule,
        embedded=embedded,
        worktree_refs=worktree_refs,
        hidden=hidden,
        filtered=filtered,
        unpushed=unpushed,
        untracked_max=untracked_max,
        untracked_total=untracked_total,
        activity=activity,
        bytes=size,
    )


def _oid(raw: str | None) -> str | None:
    """`raw` (a `branch.oid` header value) as a commit id, or None for an absent or unborn one."""
    if raw is None or raw == "(initial)":
        return None
    if not _OID.fullmatch(raw):
        raise ValueError("malformed branch.oid")
    return raw


def _hidden(git: CodeGit, tree: Path) -> bool:
    """Whether `ls-files -v` shows an assume-unchanged (lowercase tag) or skip-worktree (`S`)
    entry."""
    raw = git.output(tree, *LONG_PATHS, "ls-files", "-z", "-v")
    hidden = False
    for record in raw.split("\0"):
        if not record:
            continue
        if record[1:2] != " ":
            raise ValueError("malformed ls-files -v record")
        if record[0].islower() or record[0] == "S":
            hidden = True
    return hidden


def _filtered(git: CodeGit, tree: Path, records: tuple[StatusRecord, ...]) -> bool:
    """Whether an archive could not hold `tree` as it is, because Git runs there without a
    filter driver (`CodeGit.unfiltered`): one that rewrites files on checkout, where any file
    may be under it and would come back as its stored form, or one a changed or untracked file
    is under, whose unfiltered bytes `add -A` would store. Never true outside a Bot copy."""
    drivers, rewrites = git.unfiltered(tree)
    if rewrites or not drivers:
        return rewrites
    # A `? folder/` record is a nested repository, kept for that (`embedded`).
    for group in chunks(record.path for record in records if not record.path.endswith("/")):
        fields = git.output(tree, "check-attr", "-z", "filter", "--", *group).split("\0")
        if len(fields) != 3 * len(group) + 1 or fields[-1]:
            raise ValueError("malformed check-attr answer")
        if any(value in drivers for value in fields[2::3]):
            return True
    return False


def _git_paths(git: CodeGit, tree: Path) -> list[str]:
    """One `rev-parse --git-path` answer per name in `_GIT_PATHS`, in order, each absolute."""
    args = [arg for name in _GIT_PATHS for arg in ("--git-path", name)]
    lines = git.output(tree, "rev-parse", *args).splitlines()
    if len(lines) != len(_GIT_PATHS) or not all(lines):
        raise ValueError("malformed git-path answer")
    return lines


def _unpushed(git: CodeGit, tree: Path, branch: str | None) -> int:
    """Commits on HEAD no other branch, tag or remote-tracking ref also has; `branch` itself is
    excluded from that comparison so a worktree is never "unpushed" against its own history."""
    exclude = (f"--exclude={branch}",) if branch is not None else ()
    raw = git.output(
        tree, "rev-list", "--count", "HEAD", "--not", *exclude, "--branches", "--tags", "--remotes"
    )
    lines = raw.splitlines()
    if len(lines) != 1 or not lines[0].isdigit():
        raise ValueError("malformed rev-list count")
    return int(lines[0])


def _reparse(info: os.stat_result) -> bool:
    """Whether `info` (an `lstat`-style result) is a symlink or a Windows reparse point (a
    junction included), from the one `stat()` the walk already took, no extra call."""
    return bool(
        stat.S_ISLNK(info.st_mode)
        or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    )


def _walk(root: Path) -> tuple[int | None, datetime, bool]:
    """Every file and folder under `root`: total regular-file bytes (None once `MAX_WALK_ENTRIES`
    or `WALK_TIMEOUT_S` trips, a folder cannot be listed, an entry cannot be stat'ed, one that
    has since vanished aside, or a folder's `st_dev` differs from `root`'s own: a mount point,
    POSIX only, per `_MOUNT_CHECK`), the newest `lstat` mtime seen (seeded by `root`'s own), and
    whether a `.git` entry (matched case-insensitively: Git itself accepts `.GIT` or `.Git` as a
    repository on a case-insensitive volume, NTFS and APFS by default), of any type (file,
    folder, link or junction: a nested linked worktree's `.git` is a file), sits below `root` (an
    embedded repository, checked before anything else about an entry and so never entered or
    sized; the root's own `.git` is walked like any other entry there). A symlink or junction is
    lstat'ed for its own mtime but neither entered nor sized; a mount point's own mtime also
    counts, but it is never crossed into either."""
    root_info = root.lstat()
    activity = datetime.fromtimestamp(root_info.st_mtime, tz=UTC)
    root_dev = root_info.st_dev
    start = time.monotonic()
    total = 0
    seen = 0
    complete = True
    embedded = False
    stack = [root]
    while stack and complete:
        current = stack.pop()
        try:
            scanned = os.scandir(current)
        except OSError:
            complete = False  # the `while` guard below stops the walk here, same as the budgets
            break
        with scanned:
            for entry in scanned:
                if entry.name.casefold() == ".git" and current != root:
                    embedded = True
                    continue
                seen += 1
                if seen > MAX_WALK_ENTRIES or time.monotonic() - start > WALK_TIMEOUT_S:
                    complete = False
                    break
                try:
                    info = entry.stat(follow_symlinks=False)
                except FileNotFoundError:
                    continue
                except OSError:
                    complete = False  # unlistable by stat, same as an unlistable folder
                    break
                moment = datetime.fromtimestamp(info.st_mtime, tz=UTC)
                if moment > activity:
                    activity = moment
                if _reparse(info):
                    continue
                if stat.S_ISDIR(info.st_mode):
                    if _MOUNT_CHECK and info.st_dev != root_dev:
                        complete = False  # a mount point: never cross onto another filesystem
                        break
                    stack.append(Path(entry.path))
                elif stat.S_ISREG(info.st_mode):
                    total += info.st_size
    return (total if complete else None), activity, embedded


def has_process_inside(path: Path) -> bool:
    """Whether a process has `path`, or anything beneath it, as its cwd or an open file: Linux
    walks `/proc`, macOS asks `lsof`, Windows always answers False (the rename the sweep does
    instead already refuses while a handle is open inside). Anything this cannot settle for
    certain answers True (fail closed): a pid or fd it cannot read is skipped, but `/proc` itself
    being unreadable, or `lsof` failing outright, is not."""
    if sys.platform == "win32":
        return False
    root = os.path.realpath(path)
    if sys.platform == "darwin":
        return _lsof_inside(root)
    return _proc_inside(root)


def _under(root: str, target: str) -> bool:
    return target == root or target.startswith(root + os.sep)


def _proc_inside(root: str) -> bool:
    try:
        pids = os.listdir("/proc")
    except OSError:
        return True
    for pid in pids:
        if not pid.isdigit():
            continue
        try:
            if _under(root, os.readlink(f"/proc/{pid}/cwd")):
                return True
        except OSError:
            pass
        try:
            fds = os.listdir(f"/proc/{pid}/fd")
        except OSError:
            continue
        for fd in fds:
            try:
                if _under(root, os.readlink(f"/proc/{pid}/fd/{fd}")):
                    return True
            except OSError:
                continue
    return False


def _lsof_inside(root: str) -> bool:
    """Compare kernel-spelled paths using lsof's LC_ALL=C byte escaping.

    Backslashes are doubled; non-ASCII UTF-8 bytes print as `\\xHH`. Escaping the root too
    preserves path boundaries without decoding a literal backslash as a filename escape.
    Unprintable or unreadable roots and failed queries keep the worktree.
    """
    if sys.platform == "win32":
        raise OSError(errno.ENOSYS, "_lsof_inside is macOS-only")  # os.getuid is POSIX-only
    try:
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            # F_GETPATH supplies the spelling on disk on macOS, including normalization/case.
            root = osfs.final_path(fd)
        finally:
            os.close(fd)
        encoded = root.encode("utf-8")
    except FileNotFoundError:
        return False  # A pruned, absent tree has no folder whose users removal could disrupt.
    except OSError, UnicodeError:
        return True
    if any(byte < 0x20 or byte == 0x7F for byte in encoded):
        return True
    printed_root = "".join(
        "\\\\" if byte == 0x5C else chr(byte) if byte < 0x80 else f"\\x{byte:02x}"
        for byte in encoded
    )
    try:
        done = winproc.run(
            ["/usr/sbin/lsof", "-nP", "-w", "-u", str(os.getuid()), "-Fn"],
            cwd="/",
            env={**os.environ, "LC_ALL": "C"},  # fixed escaping, whatever the app's own locale
        )
    except Exception:  # TimeoutExpired (TID251 bans naming it), or lsof missing
        return True
    if done.returncode != 0:
        return True
    for line in done.stdout.splitlines():
        if not line.startswith("n"):
            continue
        name = line[1:]
        if _under(printed_root, name):
            return True
    return False
