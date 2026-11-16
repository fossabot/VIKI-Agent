"""The worktree cleanup's archive plumbing on real repositories: what an archive holds, that the
worktree's own index stays as it was, restore, the ref writes and their refusals, and that no hook
or signing program runs."""

from __future__ import annotations

import errno
import hashlib
import os
import tempfile
from pathlib import Path
from typing import Any

import pytest

from tests.workspace_code.test_code_git import _commit, _git, _repo
from tests.workspace_code.test_code_range import _code_git
from tests.workspace_code.test_worktrees import (
    _empty_git_environment,  # noqa: F401 (autouse: no developer Git config)
    needs_git,
)
from viki_agent import winproc
from viki_agent.code.code_git import CodeGit
from viki_agent.code.worktree_archive import (
    PREFIX,
    Archive,
    ArchiveError,
    Snapshot,
    delete_branch,
    drop,
    list_archives,
    parents,
    restore_files,
    save,
    snapshot,
)

BRANCH = "viki/20261106-ab12"
WORKTREE_ID = "3f2c8a9e-1b4d-4c6e-9f0a-2b3c4d5e6f70"
OTHER_ID = "0b6f4c1e-7d2a-4e3b-8c5d-9a1f2e3d4c5b"
OID = "a" * 40
# What the linked worktree of `_dirty` has in its index, and what `add -A` takes from it.
INDEXED = {
    ".gitignore": "build/\n",
    "both.txt": "staged\n",
    "gone.txt": "base\n",
    "same.txt": "base\n",
    "staged.txt": "staged\n",
    "unstaged.txt": "base\n",
}
ARCHIVED = {
    ".gitignore": "build/\n",
    "both.txt": "unstaged\n",
    "dir/new.txt": "untracked\n",
    "new.txt": "untracked\n",
    "same.txt": "base\n",
    "staged.txt": "staged\n",
    "unstaged.txt": "unstaged\n",
}


def _write(root: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_bytes(text.encode())


def _dirty(tmp_path: Path) -> tuple[Path, Path, str]:
    """A main folder, a linked worktree on BRANCH and their commit: in the worktree, a file
    staged, one staged then changed again, one changed, one removed by `git rm` and one from disk,
    two untracked and one ignored."""
    root = _repo(tmp_path / "main")
    names = ("staged.txt", "both.txt", "unstaged.txt", "removed.txt", "gone.txt", "same.txt")
    _write(root, {".gitignore": "build/\n", **dict.fromkeys(names, "base\n")})
    _commit(root, "base")
    tree = tmp_path / "tree"
    _git(root, "worktree", "add", "-q", "-b", BRANCH, str(tree))
    _write(tree, {"staged.txt": "staged\n", "both.txt": "staged\n"})
    _git(tree, "add", "staged.txt", "both.txt")
    _git(tree, "rm", "-q", "removed.txt")
    (tree / "gone.txt").unlink()
    _write(
        tree,
        {
            "both.txt": "unstaged\n",
            "unstaged.txt": "unstaged\n",
            "new.txt": "untracked\n",
            "dir/new.txt": "untracked\n",
            "build/out.txt": "ignored\n",
        },
    )
    return root, tree, _git(root, "rev-parse", "HEAD").strip()


def _files(root: Path, tree: str) -> dict[str, str]:
    """Each file of the tree object `tree`, with its content."""
    names = _git(root, "ls-tree", "-r", "-z", "--name-only", tree).split("\0")[:-1]
    return {name: _git(root, "cat-file", "blob", f"{tree}:{name}") for name in names}


def _index(tree: Path) -> Path:
    """A linked worktree's index file (`--git-path` answers it absolute)."""
    return Path(_git(tree, "rev-parse", "--git-path", "index").strip())


def _status(tree: Path) -> list[str]:
    return sorted(_git(tree, "status", "--porcelain=v2", "--untracked-files=all").splitlines())


def _refs(root: Path, *patterns: str) -> list[str]:
    return _git(root, "for-each-ref", "--format=%(refname)", *patterns).split()


@needs_git
def test_a_snapshot_holds_every_change_but_no_ignored_file(tmp_path: Path) -> None:
    root, tree, head = _dirty(tmp_path)
    digest = hashlib.sha256(_index(tree).read_bytes()).hexdigest()

    snap = snapshot(_code_git(tmp_path), tree)

    assert (snap.head, snap.index_digest) == (head, digest)
    assert _files(root, snap.index_tree) == INDEXED
    assert _files(root, snap.tree) == ARCHIVED


@needs_git
def test_a_snapshot_and_a_save_leave_the_worktrees_own_index_and_status_alone(
    tmp_path: Path,
) -> None:
    root, tree, _ = _dirty(tmp_path)
    git = _code_git(tmp_path)
    index = _index(tree)
    before = index.read_bytes(), index.stat().st_mtime_ns
    status = git.git(tree, "status", "--porcelain=v2", "-z", "--untracked-files=all")

    save(git, root, WORKTREE_ID, snapshot(git, tree), branch=BRANCH)

    assert (index.read_bytes(), index.stat().st_mtime_ns) == before
    assert git.git(tree, "status", "--porcelain=v2", "-z", "--untracked-files=all") == status


@needs_git
def test_a_change_git_finds_only_by_content_is_archived(tmp_path: Path) -> None:
    # The file keeps its index entry's size and mtime, as new as the index file: Git compares
    # such an entry by content, so the index copy has to keep the index file's mtime.
    root = _repo(tmp_path / "main")
    _git(root, "config", "core.checkStat", "minimal")  # stat data: mtime seconds and size
    then = 1_700_000_000 * 10**9
    _write(root, {"racy.txt": "one\n"})
    os.utime(root / "racy.txt", ns=(then, then))
    _commit(root, "base")
    _write(root, {"racy.txt": "two\n"})
    for path in (root / "racy.txt", root / ".git" / "index"):
        os.utime(path, ns=(then, then))
    git = _code_git(tmp_path)
    assert git.git(root, "status", "--porcelain").splitlines() == [" M racy.txt"]  # control

    snap = snapshot(git, root)

    assert _files(root, snap.tree)["racy.txt"] == "two\n"


@needs_git
@pytest.mark.parametrize("held", ["index", "head"])
def test_a_snapshot_refuses_a_tree_whose_index_holds_a_nested_repository(
    tmp_path: Path, held: str
) -> None:
    """To tell whether a nested repository changed, `add -A` starts Git inside it, where that
    repository's own config decides what runs."""
    root = _repo(tmp_path / "main")
    _write(root, {"a.txt": "base\n"})
    _commit(root, "base")
    nested = _repo(root / "nested")
    _write(nested, {"f.txt": "nested\n"})
    _commit(nested, "nested")
    _git(root, "add", "nested")
    if held == "head":  # no index file: the snapshot starts from HEAD's tree
        _git(root, "commit", "-q", "-m", "nested")
        (root / ".git" / "index").unlink()
    marker = tmp_path / "nested-git-ran"
    with (nested / ".git" / "config").open("a", encoding="utf-8") as config:
        config.write(f'[filter "probe"]\n\tclean = "touch {marker.as_posix()}; cat"\n')
    _write(nested, {".gitattributes": "* filter=probe\n"})
    os.utime(nested / "f.txt", ns=(1, 1))  # the same bytes at another time: a status reads them

    with pytest.raises(ArchiveError, match="git_failed"):
        snapshot(_code_git(tmp_path), root)
    assert not marker.exists()

    if held == "head":
        _git(root, "read-tree", "HEAD")
    _git(root, "add", "-A")  # Git's own default, on the index the snapshot would have used
    assert marker.exists()


@needs_git
def test_save_commits_the_stash_shape_and_a_second_save_replaces_the_ref(tmp_path: Path) -> None:
    root, tree, head = _dirty(tmp_path)
    git = _code_git(tmp_path)
    snap = snapshot(git, tree)

    first = save(git, root, WORKTREE_ID, snap, branch=BRANCH)

    shape = parents(git, root, PREFIX + WORKTREE_ID)
    assert shape is not None and shape[0] == head
    index_commit = shape[1]
    trees = _git(root, "rev-parse", f"{first}^{{tree}}", f"{index_commit}^{{tree}}").split()
    assert trees == [snap.tree, snap.index_tree]
    by = "VIKI Agent <viki-agent@invalid> VIKI Agent <viki-agent@invalid>"
    for commit, subject in ((first, f"archive of {BRANCH}"), (index_commit, "archive: index")):
        logged = _git(root, "log", "-1", "--format=%an <%ae> %cn <%ce> %s", commit).strip()
        assert logged == f"{by} VIKI Agent {subject}"

    _write(tree, {"later.txt": "later\n"})
    second = save(git, root, WORKTREE_ID, snapshot(git, tree), branch=None)

    assert second != first
    assert _git(root, "rev-parse", PREFIX + WORKTREE_ID).strip() == second
    subject = _git(root, "log", "-1", "--format=%s", second).strip()
    assert subject == "VIKI Agent archive of detached HEAD"


@needs_git
def test_restore_files_brings_back_the_status_the_staged_blobs_and_the_files(
    tmp_path: Path,
) -> None:
    root, tree, _ = _dirty(tmp_path)
    git = _code_git(tmp_path)
    archive = save(git, root, WORKTREE_ID, snapshot(git, tree), branch=BRANCH)
    restored, base = tmp_path / "restored", f"{archive}^1"
    _git(root, "worktree", "add", "-q", "--no-checkout", "-b", "back", str(restored), base)

    restore_files(git, restored, archive)

    assert _status(restored) == _status(tree)
    staged = _git(tree, "ls-files", "-z").split("\0")[:-1]
    assert sorted(staged) == sorted(INDEXED)
    for name in staged:
        assert _git(restored, "show", f":{name}") == _git(tree, "show", f":{name}")
    for name in ARCHIVED:
        assert (restored / name).read_bytes() == (tree / name).read_bytes()
    assert not (restored / "build").exists()
    assert not (restored / "gone.txt").exists()


@needs_git
def test_drop_deletes_the_ref_only_while_it_names_the_given_commit(tmp_path: Path) -> None:
    root, tree, head = _dirty(tmp_path)
    git = _code_git(tmp_path)
    ref = PREFIX + WORKTREE_ID
    archive = save(git, root, WORKTREE_ID, snapshot(git, tree), branch=BRANCH)

    with pytest.raises(ArchiveError):
        drop(git, root, ref, head)
    assert _git(root, "rev-parse", ref).strip() == archive

    drop(git, root, ref, archive)
    assert _refs(root, PREFIX) == []


@needs_git
def test_a_full_disk_tempdir_failure_is_archive_error_not_oserror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, tree, head = _dirty(tmp_path)
    git = _code_git(tmp_path)
    dummy = Snapshot(head, None, head, head)

    def full(*args: object, **kwargs: object) -> str:
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(tempfile, "mkdtemp", full)

    for call in (
        lambda: snapshot(git, tree),
        lambda: save(git, root, WORKTREE_ID, dummy, branch=BRANCH),
        lambda: delete_branch(git, root, "nope", head),
        lambda: drop(git, root, PREFIX + WORKTREE_ID, head),
        lambda: restore_files(git, tree, head),
    ):
        with pytest.raises(ArchiveError) as refused:
            call()
        assert refused.value.code == "git_failed"


@pytest.mark.parametrize("old", ["0" * 40, "0" * 64, "HEAD", "a" * 39, "A" * 40, ""])
def test_a_zero_or_malformed_old_value_is_refused_before_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, old: str
) -> None:
    calls: list[tuple[Any, ...]] = []
    monkeypatch.setattr(CodeGit, "git", lambda *args, **options: calls.append(args) or "")
    git = _code_git(tmp_path)

    with pytest.raises(ValueError):
        drop(git, tmp_path, PREFIX + WORKTREE_ID, old)
    with pytest.raises(ValueError):
        delete_branch(git, tmp_path, BRANCH, old)
    assert calls == []


@needs_git
def test_delete_branch_keeps_a_pushed_or_checked_out_branch_and_refuses_a_moved_one(
    tmp_path: Path,
) -> None:
    root = _repo(tmp_path / "main")
    _write(root, {"a.txt": "a\n"})
    _commit(root, "base")
    head = _git(root, "rev-parse", "HEAD").strip()
    for name in (BRANCH, "checked-out", "moved", "idle"):
        _git(root, "branch", name)
    _git(root, "update-ref", f"refs/remotes/origin/{BRANCH}", head)
    _git(root, "switch", "-q", "checked-out")
    other = _git(root, "commit-tree", "-m", "other", f"{head}^{{tree}}").strip()
    git = _code_git(tmp_path)
    branches = [f"refs/heads/{name}" for name in ("checked-out", "idle", "main", "moved", BRANCH)]

    assert delete_branch(git, root, BRANCH, head) is False
    assert delete_branch(git, root, "checked-out", head) is False
    with pytest.raises(ArchiveError):
        delete_branch(git, root, "moved", other)
    assert _refs(root, "refs/heads/") == branches

    assert delete_branch(git, root, "idle", head) is True
    assert _refs(root, "refs/heads/") == [ref for ref in branches if ref != "refs/heads/idle"]


@needs_git
def test_a_symbolic_ref_is_deleted_itself_not_its_target(tmp_path: Path) -> None:
    root = _repo(tmp_path / "main")
    _write(root, {"a.txt": "a\n"})
    _commit(root, "base")
    head = _git(root, "rev-parse", "HEAD").strip()
    _git(root, "symbolic-ref", "refs/heads/alias", "refs/heads/main")
    _git(root, "symbolic-ref", PREFIX + WORKTREE_ID, "refs/heads/main")
    git = _code_git(tmp_path)

    assert delete_branch(git, root, "alias", head) is True
    drop(git, root, PREFIX + WORKTREE_ID, head)

    assert _refs(root) == ["refs/heads/main"]


@needs_git
def test_list_archives_answers_uuid_names_of_commits_with_the_commit_date(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, tree, _ = _dirty(tmp_path)
    git = _code_git(tmp_path)
    monkeypatch.setenv("GIT_COMMITTER_DATE", "2026-01-02 03:04:05 +0000")
    archive = save(git, root, WORKTREE_ID, snapshot(git, tree), branch=BRANCH)
    for name in ("not-a-uuid", "A1B2C3D4-E5F6-4A7B-8C9D-0E1F2A3B4C5D", f"{WORKTREE_ID}0"):
        _git(root, "update-ref", PREFIX + name, archive)
    _git(root, "update-ref", PREFIX + OTHER_ID, f"{archive}^{{tree}}")  # names no commit

    assert list_archives(git, root) == [Archive(WORKTREE_ID, archive, "2026-01-02T03:04:05.000Z")]


@pytest.mark.parametrize(
    "raw",
    [
        f"{PREFIX}{WORKTREE_ID}\x00{OID}\n",
        f"{PREFIX}{WORKTREE_ID}\x00HEAD\x001\n",
        f"{PREFIX}{WORKTREE_ID}\x00{OID}\x00soon\n",
        f"{PREFIX}{WORKTREE_ID}\x00{OID}\x00{10**12}\n",  # past the year 9999
        f"refs/heads/main\x00{OID}\x001\n",
        f"{PREFIX}{WORKTREE_ID}\x00{OID}\x001",
    ],
    ids=["two-fields", "not-an-oid", "not-a-date", "far-date", "outside", "unterminated"],
)
def test_a_malformed_listing_is_git_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setattr(CodeGit, "git", lambda *args, **options: raw)

    with pytest.raises(ArchiveError) as refused:
        list_archives(_code_git(tmp_path), tmp_path)
    assert refused.value.code == "git_failed"


@needs_git
def test_parents_answers_only_for_a_commit_shaped_like_an_archive(tmp_path: Path) -> None:
    root, tree, head = _dirty(tmp_path)
    git = _code_git(tmp_path)
    archive = save(git, root, WORKTREE_ID, snapshot(git, tree), branch=BRANCH)
    index_commit = _git(root, "rev-parse", f"{archive}^2").strip()
    files = f"{head}^{{tree}}"
    lone = _git(root, "commit-tree", "-m", "lone", files).strip()
    child = _git(root, "commit-tree", "-p", head, "-m", "child", files).strip()
    unrelated = _git(root, "commit-tree", "-p", head, "-p", lone, "-m", "merge", files).strip()
    crossed = _git(root, "commit-tree", "-p", lone, "-p", child, "-m", "merge", files).strip()

    assert parents(git, root, PREFIX + WORKTREE_ID) == (head, index_commit)
    assert parents(git, root, archive) == (head, index_commit)
    for plain in (head, child, unrelated, crossed, files, PREFIX + OTHER_ID):
        assert parents(git, root, plain) is None


@needs_git
def test_a_signing_config_with_a_failing_program_still_saves(tmp_path: Path) -> None:
    root, tree, _ = _dirty(tmp_path)
    _git(root, "config", "commit.gpgSign", "true")
    _git(root, "config", "gpg.program", (tmp_path / "no-such-gpg").as_posix())
    # Control: the config is live, so a commit that signs fails.
    signed = winproc.run(["git", "commit", "--allow-empty", "-q", "-m", "x"], cwd=root)
    assert signed.returncode != 0
    git = _code_git(tmp_path)

    archive = save(git, root, WORKTREE_ID, snapshot(git, tree), branch=BRANCH)

    assert "gpgsig" not in _git(root, "cat-file", "commit", archive)


@needs_git
def test_no_repository_hook_runs(tmp_path: Path) -> None:
    root, tree, head = _dirty(tmp_path)
    restored = tmp_path / "restored"
    _git(root, "worktree", "add", "-q", "--no-checkout", "-b", "back", str(restored), head)
    _git(root, "branch", "idle")
    hooks, marker = tmp_path / "hooks", tmp_path / "hook-ran"
    hooks.mkdir()
    for name in ("reference-transaction", "post-index-change"):
        (hooks / name).write_bytes(f"#!/bin/sh\necho {name} >> '{marker.as_posix()}'\n".encode())
        (hooks / name).chmod(0o755)
    _git(root, "config", "core.hooksPath", hooks.as_posix())
    _git(root, "update-ref", "refs/heads/control", head)
    assert marker.exists()  # control: the hooks run
    marker.unlink()
    git = _code_git(tmp_path)

    archive = save(git, root, WORKTREE_ID, snapshot(git, tree), branch=BRANCH)
    restore_files(git, restored, archive)
    drop(git, root, PREFIX + WORKTREE_ID, archive)
    assert delete_branch(git, root, "idle", head) is True

    assert not marker.exists()
