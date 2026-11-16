"""`worktree_probe.inspect` and `has_process_inside`, against real repositories in `tmp_path`."""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tests.workspace_code.test_code_git import _commit, _git, _repo
from tests.workspace_code.test_code_range import _code_git
from tests.workspace_code.test_worktrees import (
    _empty_git_environment,  # noqa: F401 (autouse: no developer Git config)
    needs_git,
)
from viki_agent import winproc
from viki_agent.code import code_checkout, worktree_archive, worktree_probe
from viki_agent.code.worktree_probe import has_process_inside, inspect
from viki_agent.code.worktree_rules import Facts, decide

HANG_GUARD_S = 10  # guards a real process start/stop that normally takes milliseconds
BRANCH = "viki/20261106-ab12"


def _write(root: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_bytes(text.encode())


def _tree(tmp_path: Path) -> tuple[Path, Path]:
    """A main repo with one commit and a linked worktree on `BRANCH`."""
    root = _repo(tmp_path / "main")
    _write(root, {"a.txt": "base\n"})
    _commit(root, "base")
    tree = tmp_path / "tree"
    _git(root, "worktree", "add", "-q", "-b", BRANCH, str(tree))
    return root, tree


@needs_git
def test_a_clean_worktree_is_readable_with_nothing_owed(tmp_path: Path) -> None:
    root, tree = _tree(tmp_path)

    facts = inspect(_code_git(tmp_path), tree)

    assert facts.readable is True
    assert facts.branch == BRANCH
    assert facts.head == _git(root, "rev-parse", "HEAD").strip()
    assert (facts.changes, facts.unmerged, facts.unpushed) == (0, False, 0)
    assert (facts.submodule, facts.embedded, facts.worktree_refs, facts.hidden) == (
        False,
        False,
        False,
        False,
    )
    assert (facts.untracked_max, facts.untracked_total) == (0, 0)
    assert facts.activity is not None
    assert facts.bytes is not None and facts.bytes > 0
    assert facts.index_digest is not None


@needs_git
def test_a_tracked_edit_and_an_untracked_file_are_changes(tmp_path: Path) -> None:
    _, tree = _tree(tmp_path)
    _write(tree, {"a.txt": "changed\n", "untracked.txt": "u" * 10})

    facts = inspect(_code_git(tmp_path), tree)

    assert facts.changes == 2  # the tracked edit and the untracked file
    assert (facts.untracked_max, facts.untracked_total) == (10, 10)


@needs_git
def test_a_commit_on_no_other_ref_is_unpushed(tmp_path: Path) -> None:
    _, tree = _tree(tmp_path)
    _write(tree, {"a.txt": "more\n"})
    _commit(tree, "more")

    facts = inspect(_code_git(tmp_path), tree)

    assert (facts.changes, facts.unpushed) == (0, 1)


@needs_git
def test_a_branch_fully_reachable_elsewhere_has_nothing_unpushed(tmp_path: Path) -> None:
    # `tree` sits on BRANCH at the same commit "main" already holds: nothing is exclusive to it.
    _, tree = _tree(tmp_path)

    facts = inspect(_code_git(tmp_path), tree)

    assert (facts.changes, facts.unpushed) == (0, 0)


@needs_git
def test_a_detached_head_has_no_branch(tmp_path: Path) -> None:
    root = _repo(tmp_path / "main")
    _write(root, {"a.txt": "base\n"})
    _commit(root, "base")
    head = _git(root, "rev-parse", "HEAD").strip()
    tree = tmp_path / "tree"
    _git(root, "worktree", "add", "-q", "--detach", str(tree), head)

    facts = inspect(_code_git(tmp_path), tree)

    assert facts.readable is True
    assert facts.branch is None
    assert facts.head == head


@needs_git
def test_a_clean_committed_gitlink_is_still_a_submodule(tmp_path: Path) -> None:
    root, tree = _tree(tmp_path)
    sub_src = _repo(tmp_path / "sub-src")
    _write(sub_src, {"s.txt": "sub\n"})
    _commit(sub_src, "sub base")
    # The submodule is added and committed on "main", never inside `tree` itself, so `tree`'s own
    # per-worktree `modules` storage (a separate submodule signal) stays empty; `tree` only fast
    # forwards to the commit that already holds the gitlink, isolating the `ls-files --stage`
    # check (mode 160000) as the one signal left that can still see it.
    _git(root, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(sub_src), "sub")
    _commit(root, "add submodule")
    _git(tree, "merge", "-q", "--ff-only", "main")
    # Control: a fully committed, checked-out submodule leaves status clean, so only `ls-files
    # --stage` (not the status sub-field, which only reports a difference from HEAD) still shows
    # its gitlink.
    assert _git(tree, "status", "--porcelain=v2", "--untracked-files=all").strip() == ""

    facts = inspect(_code_git(tmp_path), tree)

    assert facts.submodule is True


@needs_git
def test_an_untracked_nested_repository_is_embedded(tmp_path: Path) -> None:
    _, tree = _tree(tmp_path)
    nested = tree / "nested" / "embedded"
    nested.mkdir(parents=True)
    _git(nested, "init", "-q")
    # Control: untracked-files=all reports the whole directory, not its files, when it is itself
    # a repository git refuses to enter.
    assert "? nested/embedded/" in _git(tree, "status", "--porcelain=v2", "--untracked-files=all")

    facts = inspect(_code_git(tmp_path), tree)

    assert facts.embedded is True


@needs_git
def test_a_nested_repository_under_a_gitignored_folder_is_still_embedded(tmp_path: Path) -> None:
    _, tree = _tree(tmp_path)
    _write(tree, {".gitignore": "build/\n"})
    nested = tree / "build" / "x"
    nested.mkdir(parents=True)
    _git(nested, "init", "-q")
    # Control: git status itself never mentions the ignored folder, so only the disk walk,
    # not a shortcut built on `git status`, can find the nested repository inside it.
    assert "build" not in _git(tree, "status", "--porcelain=v2", "--untracked-files=all")

    facts = inspect(_code_git(tmp_path), tree)

    assert facts.embedded is True


@needs_git
@pytest.mark.skipif(sys.platform == "linux", reason="needs a case-insensitive volume (NTFS/APFS)")
def test_a_gitignored_nested_repository_with_an_upper_case_git_folder_is_embedded(
    tmp_path: Path,
) -> None:
    _, tree = _tree(tmp_path)
    _write(tree, {".gitignore": "build/\n"})
    nested = tree / "build" / "x"
    nested.mkdir(parents=True)
    _git(nested, "init", "-q")
    (nested / ".git").rename(nested / ".GIT")
    # Control: Git still treats the renamed folder as that repository's `.git` on a
    # case-insensitive volume.
    assert winproc.run(["git", "rev-parse", "--git-dir"], cwd=nested).returncode == 0
    assert "build" not in _git(tree, "status", "--porcelain=v2", "--untracked-files=all")

    facts = inspect(_code_git(tmp_path), tree)

    assert facts.embedded is True


@needs_git
def test_a_nested_linked_worktree_under_a_gitignored_folder_is_embedded(tmp_path: Path) -> None:
    # Claude Code's documented default: `.claude/worktrees/<name>/`, which its docs say to
    # gitignore. A linked worktree's own `.git` is a file (a `gitdir:` pointer), not a folder.
    root = _repo(tmp_path / "main")
    _write(root, {".gitignore": ".claude/worktrees/\n", "a.txt": "base\n"})
    _commit(root, "base")
    tree = tmp_path / "tree"
    _git(root, "worktree", "add", "-q", "-b", BRANCH, str(tree))
    nested = tree / ".claude" / "worktrees" / "agent"
    _git(tree, "worktree", "add", "-q", "-b", "agent", str(nested))
    (nested / "work.txt").write_bytes(b"uncommitted work\n")
    assert (nested / ".git").is_file()  # control: a linked worktree's `.git` is a file
    # Control: status never looks inside the ignored folder, so it alone can't catch this.
    assert ".claude" not in _git(tree, "status", "--porcelain=v2", "--untracked-files=all")

    facts = inspect(_code_git(tmp_path), tree)

    assert facts.embedded is True


@needs_git
def test_a_lone_git_file_under_a_gitignored_folder_is_embedded(tmp_path: Path) -> None:
    _, tree = _tree(tmp_path)
    _write(tree, {".gitignore": "build/\n"})
    pointer = tree / "build" / "x"
    pointer.mkdir(parents=True)
    (pointer / ".git").write_text("gitdir: /elsewhere/.git\n", encoding="utf-8")
    assert "build" not in _git(tree, "status", "--porcelain=v2", "--untracked-files=all")

    facts = inspect(_code_git(tmp_path), tree)

    assert facts.embedded is True


@needs_git
def test_a_private_worktree_ref_is_worktree_refs(tmp_path: Path) -> None:
    _, tree = _tree(tmp_path)
    _git(tree, "update-ref", "refs/worktree/x", "HEAD")

    facts = inspect(_code_git(tmp_path), tree)

    assert facts.worktree_refs is True


@needs_git
def test_assume_unchanged_is_hidden(tmp_path: Path) -> None:
    _, tree = _tree(tmp_path)
    _git(tree, "update-index", "--assume-unchanged", "a.txt")

    facts = inspect(_code_git(tmp_path), tree)

    assert facts.hidden is True


@needs_git
def test_skip_worktree_is_hidden(tmp_path: Path) -> None:
    _, tree = _tree(tmp_path)
    _git(tree, "update-index", "--skip-worktree", "a.txt")
    _write(tree, {"a.txt": "local edit\n"})

    facts = inspect(_code_git(tmp_path), tree)

    assert facts.hidden is True


@needs_git
def test_a_real_merge_conflict_is_unmerged(tmp_path: Path) -> None:
    _, tree = _tree(tmp_path)
    _git(tree, "branch", "side")
    _git(tree, "checkout", "-q", "side")
    _write(tree, {"a.txt": "side\n"})
    _commit(tree, "side change")
    _git(tree, "checkout", "-q", BRANCH)
    _write(tree, {"a.txt": "main change\n"})
    _commit(tree, "main change")
    conflict = winproc.run(["git", "merge", "-q", "side"], cwd=tree)
    assert conflict.returncode != 0  # control: a real conflict, not a clean merge

    facts = inspect(_code_git(tmp_path), tree)

    assert facts.unmerged is True


@needs_git
def test_untracked_sizes_match_their_truncated_byte_counts(tmp_path: Path) -> None:
    _, tree = _tree(tmp_path)
    small, big = tree / "small.bin", tree / "big.bin"
    small.write_bytes(b"")
    big.write_bytes(b"")
    os.truncate(small, 100)
    os.truncate(big, 4096)

    facts = inspect(_code_git(tmp_path), tree)

    assert (facts.untracked_max, facts.untracked_total) == (4096, 4196)


@needs_git
def test_the_walk_cap_leaves_the_size_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, tree = _tree(tmp_path)
    for i in range(5):
        (tree / f"extra{i}.bin").write_bytes(b"x")
    monkeypatch.setattr(worktree_probe, "MAX_WALK_ENTRIES", 3)

    facts = inspect(_code_git(tmp_path), tree)

    assert facts.readable is True
    assert facts.bytes is None


@needs_git
def test_a_mount_point_inside_the_tree_leaves_the_walk_incomplete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A folder whose `st_dev` differs from the root's stands in for a real mount point (sshfs,
    rclone, a mounted disk image): the walk must stop there rather than cross onto it, the same
    way an unlistable folder stops it. `_MOUNT_CHECK` is forced on so the check itself runs on
    every OS this suite runs on; it is POSIX-only in the real module because `os.DirEntry.stat()`
    never reports a useful `st_dev` on Windows (the module's own platform test, monkeypatched)."""
    _, tree = _tree(tmp_path)
    mounted = tree / "mounted"
    mounted.mkdir()
    (mounted / "inside.txt").write_bytes(b"x")
    monkeypatch.setattr(worktree_probe, "_MOUNT_CHECK", True)
    real_scandir = os.scandir

    class _OtherDeviceStat:
        def __init__(self, real: os.stat_result, st_dev: int) -> None:
            self.st_mode = real.st_mode
            self.st_mtime = real.st_mtime
            self.st_dev = st_dev

    class _OtherDeviceEntry:
        def __init__(self, real: os.DirEntry[str]) -> None:
            self.name = real.name
            self.path = real.path
            self._real = real

        def stat(self, *, follow_symlinks: bool = True) -> object:
            info = self._real.stat(follow_symlinks=follow_symlinks)
            return _OtherDeviceStat(info, st_dev=info.st_dev + 1)

    class _FakeScandir:
        def __init__(self, path: Path) -> None:
            with real_scandir(path) as entries:
                self._entries = [
                    _OtherDeviceEntry(entry) if entry.name == "mounted" else entry
                    for entry in entries
                ]

        def __enter__(self) -> _FakeScandir:
            return self

        def __exit__(self, *exc: object) -> bool:
            return False

        def __iter__(self) -> Iterator[object]:
            return iter(self._entries)

    monkeypatch.setattr(worktree_probe.os, "scandir", _FakeScandir)

    facts = inspect(_code_git(tmp_path), tree)

    assert facts.readable is True
    assert facts.bytes is None


@needs_git
def test_a_stat_failure_other_than_missing_leaves_the_walk_incomplete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, tree = _tree(tmp_path)
    (tree / "extra.bin").write_bytes(b"x")
    real_scandir = os.scandir

    class _DeniedEntry:
        def __init__(self, real: os.DirEntry[str]) -> None:
            self.name = real.name
            self.path = real.path

        def stat(self, *, follow_symlinks: bool = True) -> os.stat_result:
            raise PermissionError(13, "denied")

    class _FakeScandir:
        def __init__(self, path: Path) -> None:
            with real_scandir(path) as entries:
                self._entries = [
                    _DeniedEntry(entry) if entry.name == "extra.bin" else entry for entry in entries
                ]

        def __enter__(self) -> _FakeScandir:
            return self

        def __exit__(self, *exc: object) -> bool:
            return False

        def __iter__(self) -> Iterator[object]:
            return iter(self._entries)

    monkeypatch.setattr(worktree_probe.os, "scandir", _FakeScandir)

    facts = inspect(_code_git(tmp_path), tree)

    assert facts.readable is True
    assert facts.bytes is None


@needs_git
@pytest.mark.skipif(sys.platform != "win32", reason="MAX_PATH is Windows-only")
def test_a_long_untracked_path_is_kept_unknown_or_fully_archived(tmp_path: Path) -> None:
    """Git on this machine may or may not be able to read a path past MAX_PATH (Windows's own
    long-paths switch, `core.longpaths` unset here). Either Git itself or the disk walk cannot
    finish, so `decide` keeps the row `unknown` rather than risk the file silently missing from
    an archive; or both can, and the file really is archived, through
    `worktree_archive.LONG_PATHS` on the Git calls that read it. One of the two must hold, on a
    machine with long paths enabled or without."""
    root, tree = _tree(tmp_path)
    deep = str(tree)
    while len(deep) < 300:
        deep += "\\" + "d" * 40
    os.makedirs("\\\\?\\" + deep)
    Path("\\\\?\\" + deep + "\\long.txt").write_bytes(b"precious\n")
    git = _code_git(tmp_path)

    facts = inspect(git, tree)
    now = facts.activity + timedelta(hours=25) if facts.activity is not None else datetime.now(UTC)
    verdict = decide(
        "w",
        BRANCH,
        in_area=True,
        trusted=True,
        live=False,
        locked=False,
        present=True,
        facts=facts,
        now=now,
    )

    if facts.readable and facts.bytes is not None:
        snap = worktree_archive.snapshot(git, tree)
        archived = _git(root, "ls-tree", "-r", "--name-only", snap.tree)
        assert "long.txt" in archived
    else:
        assert (verdict.action, verdict.reason) == ("keep", "unknown")


@needs_git
def test_activity_is_the_newest_mtime_set_by_utime(tmp_path: Path) -> None:
    _, tree = _tree(tmp_path)
    old = tree / "old.txt"
    old.write_text("old\n")
    deep = tree / "sub" / "newest.txt"
    deep.parent.mkdir()
    deep.write_text("new\n")
    old_stamp = datetime(2020, 1, 1, tzinfo=UTC).timestamp()
    new_stamp = datetime(2030, 6, 15, 12, 0, 0, tzinfo=UTC).timestamp()
    os.utime(old, (old_stamp, old_stamp))
    os.utime(deep, (new_stamp, new_stamp))
    os.utime(deep.parent, (new_stamp, new_stamp))

    facts = inspect(_code_git(tmp_path), tree)

    assert facts.activity == datetime(2030, 6, 15, 12, 0, 0, tzinfo=UTC)


@needs_git
@pytest.mark.skipif(sys.platform == "win32", reason="unprivileged symlinks need POSIX")
def test_a_symlink_is_not_followed_into_its_target(tmp_path: Path) -> None:
    _, tree = _tree(tmp_path)
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"x" * 10_000)
    (tree / "link").symlink_to(outside)

    facts = inspect(_code_git(tmp_path), tree)

    assert facts.bytes is not None
    assert facts.bytes < 10_000  # the link's own size counts, never the 10 KB it points at


@needs_git
def test_a_path_outside_any_repository_is_the_unreadable_facts(tmp_path: Path) -> None:
    assert inspect(_code_git(tmp_path), tmp_path / "not-a-repository") == Facts()


def test_in_progress_markers_match_code_checkouts_copy() -> None:
    assert worktree_probe._IN_PROGRESS == code_checkout._IN_PROGRESS


@pytest.mark.skipif(sys.platform != "win32", reason="checks the Windows-only branch directly")
def test_has_process_inside_is_always_false_on_windows() -> None:
    assert has_process_inside(Path("C:/does/not/exist")) is False


@pytest.mark.skipif(sys.platform != "linux", reason="/proc is Linux-only")
async def test_has_process_inside_follows_a_real_childs_cwd(tmp_path: Path) -> None:
    deep = tmp_path / "a" / "b"
    deep.mkdir(parents=True)
    child = await winproc.spawn(
        [sys.executable, "-c", "import time; time.sleep(60)"], cwd=deep, env=os.environ
    )
    try:
        assert has_process_inside(tmp_path) is True
    finally:
        child.kill_tree()
        await asyncio.wait_for(child.process.wait(), HANG_GUARD_S)
        child.release()
    assert has_process_inside(tmp_path) is False


def _lsof_escaped(text: str) -> str:
    """How `lsof -Fn`, run with `LC_ALL=C` as `_lsof_inside` does, prints `text`: every ASCII
    byte of its UTF-8 encoding verbatim, every non-ASCII byte escaped as `\\xHH`."""
    return "".join(
        "\\\\" if byte == 0x5C else chr(byte) if byte < 0x80 else f"\\x{byte:02x}"
        for byte in text.encode()
    )


@pytest.mark.skipif(sys.platform == "win32", reason="lsof is POSIX-only")
def test_lsof_inside_does_not_hold_a_missing_worktree(tmp_path: Path) -> None:
    assert worktree_probe._lsof_inside(str(tmp_path / "missing")) is False


@pytest.mark.skipif(sys.platform == "win32", reason="lsof is POSIX-only")
@pytest.mark.parametrize("suffix", ["日本語", r"folder\name", r"literal\xe6"])
@pytest.mark.parametrize("inside", [False, True])
def test_lsof_inside_matches_escaped_names_without_confusing_siblings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, suffix: str, inside: bool
) -> None:
    folder = tmp_path / suffix
    folder.mkdir()
    root = os.path.realpath(folder)
    calls: list[tuple[object, ...]] = []

    class _Done:
        returncode = 0
        stdout = "p1\nn" + _lsof_escaped(root) + ("/inside.txt\n" if inside else "2/file.txt\n")

    def fake_run(*args: object, **kwargs: object) -> object:
        calls.append(args)
        return _Done()

    monkeypatch.setattr(winproc, "run", fake_run)

    assert worktree_probe._lsof_inside(root) is inside
    assert len(calls) == 1


@pytest.mark.skipif(sys.platform == "win32", reason="lsof is POSIX-only")
def test_lsof_inside_trusts_an_ascii_root_against_an_escaped_sibling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = os.path.realpath(tmp_path)

    class _Done:
        returncode = 0
        # Stands in for `lsof -Fn` escaping a non-ASCII byte deeper in a name: the ASCII `root`
        # prefix itself stays verbatim, so a sibling that merely shares it as a text prefix
        # (root + "2/...", not root + the separator) must not read as inside `root`.
        stdout = "p1\nn" + root + "2/\\xe6\n"

    monkeypatch.setattr(winproc, "run", lambda *args, **kwargs: _Done())

    assert worktree_probe._lsof_inside(root) is False


@pytest.mark.skipif(sys.platform == "win32", reason="lsof is POSIX-only")
def test_lsof_inside_trusts_an_ascii_root_for_an_escaped_path_within_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = os.path.realpath(tmp_path)

    class _Done:
        returncode = 0
        stdout = "p1\nn" + root + "/\\xe6\n"

    monkeypatch.setattr(winproc, "run", lambda *args, **kwargs: _Done())

    assert worktree_probe._lsof_inside(root) is True
