"""Strict readers over a repository's Git output, and the integration range.

These decide what counts as reviewable: a path is `text` only when nothing else about it (its
name, mode, size, content or filter) says otherwise. Every reader checks all of its output
before it returns anything, so a malformed record is `git_failed`, never a partial answer.
Revisions are `HEAD` or an object id and paths follow `--`, so no argument is an option.
"""

from __future__ import annotations

import re
import tempfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from viki_agent.code.code_files import CodeError
from viki_agent.code.code_git import CodeGit, chunks
from viki_agent.code.worktrees import valid_branch
from viki_agent.vaults.vault_mutation import MutationError, code_ref

MAX_COMMITS = 50
MAX_RANGE_PATHS = 500
MAX_BLOB_BYTES = 1_048_576
MAX_SUBJECT_CHARS = 200

OID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
MODE = re.compile(r"[0-7]{6}")

Kind = Literal[
    "text",
    "binary",
    "symlink",
    "submodule",
    "nested_repository",
    "filtered",
    "private",
    "too_large",
    "unmerged",
    "unsupported_name",
    "other",
]
Change = Literal["added", "modified", "deleted"]

_RAW = re.compile(
    rf":({MODE.pattern}) ({MODE.pattern}) (?:{OID.pattern}) (?:{OID.pattern}) ([AMDT])"
)
_NUMSTAT = re.compile(r"([0-9]+|-)\t([0-9]+|-)\t(.+)", re.DOTALL)
_SIZE = re.compile(r"[0-9]+")
_COUNTS = re.compile(r"([0-9]+)\t([0-9]+)\n")
_OBJECT_TYPES = ("blob", "tree", "commit")
_UNFILTERED = ("unspecified", "unset")
_CHANGE: dict[str, Change] = {"A": "added", "M": "modified", "T": "modified", "D": "deleted"}


@dataclass(frozen=True)
class TreeEntry:
    mode: str
    type: str
    oid: str
    size: int | None


@dataclass(frozen=True)
class Commit:
    oid: str
    subject: str


@dataclass(frozen=True)
class RangePath:
    path: str
    change: Change
    kind: Kind


@dataclass(frozen=True)
class Integration:
    target_branch: str | None
    target_oid: str
    merge_base: str | None
    fast_forward: bool
    ahead: int
    behind: int
    commits: tuple[Commit, ...]
    commits_truncated: bool
    paths: tuple[RangePath, ...]
    paths_truncated: bool


@dataclass(frozen=True)
class _Delta:
    path: str
    status: str
    modes: tuple[str, str]


def nul_fields(raw: str) -> list[str]:
    """The fields of NUL-terminated output; an unterminated tail is `ValueError`."""
    if not raw:
        return []
    if not raw.endswith("\0"):
        raise ValueError("unterminated output")
    return raw.split("\0")[:-1]


def name_kind(path: str) -> Kind | None:
    """`private` or `unsupported_name` when the code reader would refuse the name, else None."""
    try:
        code_ref(path)
    except MutationError as refused:
        return "private" if refused.code == "excluded_path" else "unsupported_name"
    return None


def _rev(rev: str) -> None:
    if rev != "HEAD" and not OID.fullmatch(rev):
        raise ValueError("not a revision")


def _fields(raw: str) -> list[str]:
    try:
        return nul_fields(raw)
    except ValueError:
        raise CodeError("git_failed") from None


def _one_oid(raw: str) -> str:
    lines = raw.splitlines()
    if len(lines) != 1 or not OID.fullmatch(lines[0]):
        raise CodeError("git_failed")
    return lines[0]


def _tree_entry(record: str) -> tuple[str, TreeEntry]:
    meta, tab, path = record.partition("\t")
    fields = meta.split()
    if not tab or not path or len(fields) != 4:
        raise CodeError("git_failed")
    mode, kind, oid, size = fields
    sized = _SIZE.fullmatch(size) if kind == "blob" else size == "-"
    if kind not in _OBJECT_TYPES or not MODE.fullmatch(mode) or not OID.fullmatch(oid) or not sized:
        raise CodeError("git_failed")
    return path, TreeEntry(mode, kind, oid, None if size == "-" else int(size))


def tree_entries(git: CodeGit, root: Path, rev: str, paths: Sequence[str]) -> dict[str, TreeEntry]:
    """The entries of `paths` at `rev`; a path absent there is absent from the answer."""
    _rev(rev)
    asked = set(paths)
    found: dict[str, TreeEntry] = {}
    for group in chunks(paths):
        for record in _fields(git.output(root, "ls-tree", "-l", "-z", rev, "--", *group)):
            path, entry = _tree_entry(record)
            if path not in asked or path in found:
                raise CodeError("git_failed")
            found[path] = entry
    return found


def _filter_values(group: Sequence[str], raw: str) -> dict[str, str]:
    """One group's `check-attr -z filter` answer: each asked path once, nothing else."""
    fields = _fields(raw)
    asked = set(group)
    if (
        len(fields) != 3 * len(asked)
        or set(fields[0::3]) != asked
        or any(attribute != "filter" for attribute in fields[1::3])
        or not all(fields[2::3])
    ):
        raise CodeError("git_failed")
    return dict(zip(fields[0::3], fields[2::3], strict=True))


def filters(
    git: CodeGit, root: Path, paths: Sequence[str], *, source: str | None = None
) -> dict[str, str]:
    """Each path's `filter` attribute (`unspecified` or `unset` when nothing filters it), from
    the worktree's attributes or, with `source`, from that commit's (`check-attr --source`).

    Git before 2.40 has no `--source`, so a non-zero exit on it drops the answers so far and
    falls back to `_filters_from_index`: the same attributes for a well-formed tree, read
    through a private temporary index that holds the commit's tree, so the repository's own
    index and worktree are neither read nor written. Malformed output (`git_failed`) and a
    timeout (`git_unavailable`) never fall back."""
    found: dict[str, str] = {}
    if source is None:
        for group in chunks(paths):
            raw = git.output(root, "check-attr", "-z", "filter", "--", *group)
            found.update(_filter_values(group, raw))
        return found
    _rev(source)
    for group in chunks(paths):
        answered = git.answer(
            root, "check-attr", f"--source={source}", "-z", "filter", "--", *group
        )
        if answered is None:
            return _filters_from_index(git, root, paths, source)
        found.update(_filter_values(group, answered))
    return found


def _filters_from_index(
    git: CodeGit, root: Path, paths: Sequence[str], source: str
) -> dict[str, str]:
    """`filters` at commit `source` for a Git without `check-attr --source`: `read-tree` writes
    the commit's tree into `index` in a fresh private folder, and `check-attr --cached` reads
    the attributes from there, not from the repository's own index or worktree. The folder is
    removed when this returns or raises (a removal error is ignored). A step that exits
    non-zero, or malformed output, is `git_failed`; a timeout is `git_unavailable`."""
    _rev(source)
    found: dict[str, str] = {}
    with tempfile.TemporaryDirectory(
        prefix="viki-attributes-", ignore_cleanup_errors=True
    ) as folder:
        index = Path(folder) / "index"
        git.output(root, "read-tree", source, index=index)
        for group in chunks(paths):
            raw = git.output(
                root, "check-attr", "--cached", "-z", "filter", "--", *group, index=index
            )
            found.update(_filter_values(group, raw))
    return found


def _deltas(raw: str) -> list[_Delta]:
    fields = _fields(raw)
    if len(fields) % 2:
        raise CodeError("git_failed")
    deltas: list[_Delta] = []
    for meta, path in zip(fields[0::2], fields[1::2], strict=True):
        match = _RAW.fullmatch(meta)
        if match is None or not path:
            raise CodeError("git_failed")
        deltas.append(_Delta(path, match[3], (match[1], match[2])))
    if len({delta.path for delta in deltas}) != len(deltas):
        raise CodeError("git_failed")
    return deltas


def _binary_by_path(raw: str) -> dict[str, bool]:
    binary: dict[str, bool] = {}
    for field in _fields(raw):
        match = _NUMSTAT.fullmatch(field)
        if match is None or (match[1] == "-") != (match[2] == "-") or match[3] in binary:
            raise CodeError("git_failed")
        binary[match[3]] = match[1] == "-"
    return binary


def _kind(
    delta: _Delta,
    binary: dict[str, bool],
    sizes: Iterable[TreeEntry | None],
    attribute: str,
) -> Kind:
    named = name_kind(delta.path)
    if named is not None:
        return named
    if "160000" in delta.modes:
        return "submodule"
    if "120000" in delta.modes:
        return "symlink"
    if delta.status == "T" or delta.path not in binary:
        return "other"
    if binary[delta.path]:
        return "binary"
    if any(entry is not None and (entry.size or 0) > MAX_BLOB_BYTES for entry in sizes):
        return "too_large"
    return "text" if attribute in _UNFILTERED else "filtered"


def range_paths(
    git: CodeGit, root: Path, base: str, head: str
) -> tuple[tuple[RangePath, ...], bool]:
    """The first `MAX_RANGE_PATHS` paths that differ between two commits, each with its kind,
    and whether more followed."""
    _rev(base)
    _rev(head)
    # `--` ends the revisions: a file named like an object id is not a second meaning of one.
    deltas = _deltas(
        git.output(root, "diff", "--raw", "-z", "--no-renames", "--no-abbrev", base, head, "--")
    )
    if not deltas:
        return (), False
    binary = _binary_by_path(
        git.output(
            root,
            *("diff", "--numstat", "-z", "--no-renames", "--no-ext-diff", "--no-textconv"),
            *(base, head, "--"),
        )
    )
    if not binary.keys() <= {delta.path for delta in deltas}:
        raise CodeError("git_failed")
    kept = deltas[:MAX_RANGE_PATHS]
    paths = [delta.path for delta in kept]
    before = tree_entries(git, root, base, paths)
    after = tree_entries(git, root, head, paths)
    attributes = filters(git, root, paths, source=head)
    return (
        tuple(
            RangePath(
                delta.path,
                _CHANGE[delta.status],
                _kind(
                    delta,
                    binary,
                    (before.get(delta.path), after.get(delta.path)),
                    attributes[delta.path],
                ),
            )
            for delta in kept
        ),
        len(deltas) > MAX_RANGE_PATHS,
    )


def _commits(raw: str) -> tuple[tuple[Commit, ...], bool]:
    fields = _fields(raw)
    if len(fields) % 2 or len(fields) > 2 * (MAX_COMMITS + 1):
        raise CodeError("git_failed")
    commits = [
        Commit(oid, subject) for oid, subject in zip(fields[0::2], fields[1::2], strict=True)
    ]
    if not all(OID.fullmatch(commit.oid) for commit in commits):
        raise CodeError("git_failed")
    return (
        tuple(Commit(c.oid, c.subject[:MAX_SUBJECT_CHARS]) for c in commits[:MAX_COMMITS]),
        len(commits) > MAX_COMMITS,
    )


def integration(git: CodeGit, folder: Path, head_oid: str) -> Integration:
    """What integrating `head_oid` into the folder's current branch would bring in."""
    _rev(head_oid)
    target_branch = git.branch(folder)
    if target_branch is not None and not valid_branch(target_branch):
        raise CodeError("unsupported_branch")
    target = git.head(folder)
    found = git.answer(folder, "merge-base", target, head_oid)
    merge_base = None if found is None else _one_oid(found)
    counts = _COUNTS.fullmatch(
        git.output(folder, "rev-list", "--left-right", "--count", f"{target}...{head_oid}", "--")
    )
    if counts is None:
        raise CodeError("git_failed")
    commits, commits_truncated = _commits(
        git.output(
            folder,
            *("log", "-z", "--format=%H%x00%s", f"--max-count={MAX_COMMITS + 1}"),
            *(f"{target}..{head_oid}", "--"),
        )
    )
    paths, paths_truncated = range_paths(git, folder, target, head_oid)
    return Integration(
        target_branch=target_branch,
        target_oid=target,
        merge_base=merge_base,
        fast_forward=merge_base == target,
        ahead=int(counts[2]),
        behind=int(counts[1]),
        commits=commits,
        commits_truncated=commits_truncated,
        paths=paths,
        paths_truncated=paths_truncated,
    )
