"""`WorktreeService.restore`, which brings a swept tree back from its archive at its path, on its
branch, and `archives`, the list it restores from (AC-6), against real repositories in
`tmp_path`."""

from __future__ import annotations

import errno
import logging
import os
import sys
import tempfile
import uuid
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from tests.app.test_workspaces import make_junction
from tests.workspace_code.test_code_git import _git
from tests.workspace_code.test_worktree_removal import SESSION
from tests.workspace_code.test_worktree_sweep import (
    BRANCH,
    NOW,
    PAST,
    Rig,
    _age,
    _archive,
    _branches,
    _dated_archive,
    _rig,
    _stamp_tree,
)
from tests.workspace_code.test_worktrees import (
    _empty_git_environment,  # noqa: F401 (autouse: no developer Git config)
    needs_git,
)
from viki_agent.code import worktree_archive, worktrees
from viki_agent.code.code_git import CodeGit
from viki_agent.code.session_roots import MSG_WORKTREE_UNAVAILABLE
from viki_agent.code.worktree_archive import PREFIX
from viki_agent.code.worktrees import (
    MAX_WORKTREES,
    Worktree,
    WorktreeError,
    WorktreeService,
    worktree_area,
)
from viki_agent.drivers.base import StartError
from viki_agent.protection import require_private_directory
from viki_agent.session import now_iso
from viki_agent.workspaces import root_key


async def _restore(rig: Rig, worktree_id: str) -> Worktree:
    workspace = rig.workspaces.get(rig.workspace_id, kind="code")
    return await rig.service.restore(
        worktree_id, workspace.policy_revision, workspace.root.physical_id
    )


async def _refusal(rig: Rig, worktree_id: str) -> str:
    with pytest.raises(WorktreeError) as refused:
        await _restore(rig, worktree_id)
    return refused.value.code


async def _start_refusal(rig: Rig) -> str:
    with pytest.raises(StartError) as refused:
        await rig.roots.resolve(SESSION)
    return refused.value.message


def _status(tree: Path) -> str:
    return _git(tree, "status", "--porcelain=v2", "--branch", "--untracked-files=all")


async def _swept(rig: Rig, *, remote: bool = False, chat: bool = False) -> tuple[Worktree, str]:
    """A dirty tree the sweep archived and removed, with its status just before: `a.txt` staged
    and then edited again, `c.txt` added, `dir/b.txt` deleted and `new.txt` untracked. `remote`
    puts its branch on a remote, so the sweep keeps it; `chat` gives `SESSION` a root in it."""
    worktree = await rig.create()
    tree = Path(worktree.path)
    if remote:
        _git(rig.repo, "update-ref", f"refs/remotes/origin/{BRANCH}", worktree.base_oid)
    if chat:
        rig.store.put_session_root(
            {
                "session_id": SESSION,
                "workspace_id": rig.workspace_id,
                "worktree_id": worktree.worktree_id,
                "display_root": worktree.path,
                "created_at": now_iso(),
            }
        )
    (tree / "a.txt").write_bytes(b"staged\n")
    (tree / "c.txt").write_bytes(b"added\n")
    _git(tree, "add", "a.txt", "c.txt")
    (tree / "a.txt").write_bytes(b"unstaged\n")
    (tree / "dir" / "b.txt").unlink()
    (tree / "new.txt").write_bytes(b"new\n")
    status = _status(tree)
    _age(tree, PAST)

    decisions = await rig.service.sweep()

    assert [(d.worktree_id, d.action, d.reason) for d in decisions] == [
        (worktree.worktree_id, "archive", "dirty")
    ]
    assert not tree.exists() and rig.state(worktree.worktree_id) == "removed"
    assert _archive(rig, worktree.worktree_id) != ""
    return worktree, status


def _scene(rig: Rig, worktree: Worktree) -> tuple[object, ...]:
    """What a refused restore leaves as it found it: the row's state, every ref with its commit,
    the worktrees Git lists, and what sits at the tree's path."""
    path = worktree.path
    return (
        rig.state(worktree.worktree_id),
        _git(rig.repo, "for-each-ref", "--format=%(refname) %(objectname)"),
        rig.listed(),
        sorted(os.listdir(path)) if os.path.isdir(path) else os.path.lexists(path),
    )


# ---- restore ---------------------------------------------------------------------------


@needs_git
async def test_a_swept_dirty_tree_comes_back_at_its_path_on_its_branch(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    worktree, status = await _swept(rig, chat=True)
    tree = Path(worktree.path)
    archive = _archive(rig, worktree.worktree_id)
    assert BRANCH not in _branches(rig)  # the sweep deleted it: no remote holds it
    assert await _start_refusal(rig) == MSG_WORKTREE_UNAVAILABLE
    assert "restore it from the Code view's Worktrees tab" in MSG_WORKTREE_UNAVAILABLE
    hooks, marker = tmp_path / "hooks", tmp_path / "hook-ran"
    hooks.mkdir()
    for name in ("reference-transaction", "post-checkout", "post-index-change"):
        (hooks / name).write_bytes(f"#!/bin/sh\necho {name} >> '{marker.as_posix()}'\n".encode())
        (hooks / name).chmod(0o755)
    _git(rig.repo, "config", "core.hooksPath", hooks.as_posix())
    _git(rig.repo, "update-ref", "refs/heads/control", worktree.base_oid)
    assert marker.exists()  # control: the hooks run
    marker.unlink()

    restored = await _restore(rig, worktree.worktree_id)

    assert not marker.exists()
    _git(rig.repo, "config", "--unset", "core.hooksPath")
    assert replace(restored, updated_at=worktree.updated_at) == worktree
    assert rig.state(worktree.worktree_id) == "ready"
    assert _status(tree) == status  # on its branch at the archived HEAD, every change back
    assert _git(tree, "show", ":a.txt") == "staged\n"
    assert (tree / "a.txt").read_bytes() == b"unstaged\n"
    assert (tree / "c.txt").read_bytes() == b"added\n"
    assert (tree / "new.txt").read_bytes() == b"new\n"
    assert not (tree / "dir" / "b.txt").exists()
    head = _git(rig.repo, "rev-parse", f"{archive}^1").strip()
    assert _git(rig.repo, "rev-parse", f"refs/heads/{BRANCH}").strip() == head
    assert head == worktree.base_oid
    assert _archive(rig, worktree.worktree_id) == ""
    assert rig.service.archives(rig.workspace_id) == []
    assert await rig.roots.resolve(SESSION) == tree  # its chat starts again


@needs_git
async def test_a_branch_a_remote_kept_at_the_archived_head_is_checked_out_again(
    tmp_path: Path,
) -> None:
    rig = _rig(tmp_path)
    worktree, status = await _swept(rig, remote=True)
    assert BRANCH in _branches(rig)  # the sweep kept it: a remote holds it
    rig.area.rmdir()  # emptied by the sweep; the restore makes it again, private

    restored = await _restore(rig, worktree.worktree_id)

    assert restored.state == "ready" and rig.state(worktree.worktree_id) == "ready"
    assert _status(Path(worktree.path)) == status
    assert _git(rig.repo, "rev-parse", f"refs/heads/{BRANCH}").strip() == worktree.base_oid
    assert _archive(rig, worktree.worktree_id) == ""
    require_private_directory(rig.area)


@needs_git
async def test_an_area_replaced_by_a_link_refuses_before_any_write(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    worktree, _ = await _swept(rig)
    outside = tmp_path / "outside"
    outside.mkdir()
    rig.area.rmdir()  # emptied by the sweep
    if sys.platform == "win32":
        make_junction(rig.area, outside)
    else:
        rig.area.symlink_to(outside, target_is_directory=True)
    row = rig.store.get_worktree(worktree.worktree_id)
    before = _scene(rig, worktree)

    assert await _refusal(rig, worktree.worktree_id) == "unavailable"

    assert _scene(rig, worktree) == before
    assert rig.store.get_worktree(worktree.worktree_id) == row
    assert list(outside.iterdir()) == []


@needs_git
async def test_a_branch_moved_since_the_archive_refuses_and_changes_nothing(
    tmp_path: Path,
) -> None:
    rig = _rig(tmp_path)
    worktree, _ = await _swept(rig, remote=True)
    base = worktree.base_oid
    later = _git(rig.repo, "commit-tree", "-p", base, "-m", "later", f"{base}^{{tree}}").strip()
    _git(rig.repo, "update-ref", f"refs/heads/{BRANCH}", later, base)
    before = _scene(rig, worktree)

    assert await _refusal(rig, worktree.worktree_id) == "branch_moved"

    assert _scene(rig, worktree) == before
    assert rig.state(worktree.worktree_id) == "removed"


@needs_git
async def test_a_branch_checked_out_elsewhere_refuses_and_changes_nothing(
    tmp_path: Path,
) -> None:
    rig = _rig(tmp_path)
    worktree, _ = await _swept(rig, remote=True)
    _git(rig.repo, "checkout", "-q", BRANCH)
    before = _scene(rig, worktree)

    assert await _refusal(rig, worktree.worktree_id) == "branch_in_use"

    assert _scene(rig, worktree) == before
    assert rig.state(worktree.worktree_id) == "removed"


@needs_git
@pytest.mark.parametrize("ref", [False, True], ids=["no-ref", "plain-commit"])
async def test_a_tree_removed_by_hand_has_no_archive_to_restore(tmp_path: Path, ref: bool) -> None:
    rig = _rig(tmp_path)
    worktree = await rig.create()
    workspace = rig.workspaces.get(rig.workspace_id, kind="code")
    removal = await rig.service.remove(
        worktree.worktree_id, workspace.policy_revision, workspace.root.physical_id
    )
    assert removal.outcome == "removed"
    if ref:  # a commit, but not of the archive's shape
        _git(rig.repo, "update-ref", PREFIX + worktree.worktree_id, worktree.base_oid)
    before = _scene(rig, worktree)

    assert await _refusal(rig, worktree.worktree_id) == "no_archive"

    assert _scene(rig, worktree) == before
    assert rig.state(worktree.worktree_id) == "removed"


@needs_git
async def test_past_the_worktree_limit_the_row_stays_removed(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    worktree, _ = await _swept(rig)
    for _ in range(MAX_WORKTREES):
        live = str(uuid.uuid4())
        rig.insert_ready(live, path=rig.area / live)
    row = rig.store.get_worktree(worktree.worktree_id)
    before = _scene(rig, worktree)

    assert await _refusal(rig, worktree.worktree_id) == "worktree_limit"

    assert _scene(rig, worktree) == before
    assert rig.store.get_worktree(worktree.worktree_id) == row  # not even its stamp moved


@needs_git
async def test_something_at_the_path_refuses_busy_and_changes_nothing(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    worktree, _ = await _swept(rig)
    Path(worktree.path).mkdir()
    before = _scene(rig, worktree)

    assert await _refusal(rig, worktree.worktree_id) == "worktree_busy"

    assert _scene(rig, worktree) == before
    assert rig.state(worktree.worktree_id) == "removed"


@needs_git
async def test_a_row_not_removed_or_unknown_is_refused_before_any_change(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    worktree = await rig.create()
    before = _scene(rig, worktree)

    assert await _refusal(rig, worktree.worktree_id) == "worktree_not_ready"
    assert await _refusal(rig, str(uuid.uuid4())) == "unknown_worktree"

    assert _scene(rig, worktree) == before


@needs_git
async def test_a_refused_add_puts_the_row_back_and_changes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    rig = _rig(tmp_path)
    worktree, _ = await _swept(rig)
    before = _scene(rig, worktree)
    real = rig.git.git

    def refusing(root: Path, *args: str, timeout: float = 5.0, index: Path | None = None) -> str:
        if "--no-checkout" in args:
            raise RuntimeError(f"git worktree add failed for {worktree.path}")
        if index is None:
            return real(root, *args, timeout=timeout)
        return real(root, *args, timeout=timeout, index=index)

    monkeypatch.setattr(rig.git, "git", refusing)
    caplog.set_level(logging.WARNING)

    assert await _refusal(rig, worktree.worktree_id) == "git_failed"

    assert _scene(rig, worktree) == before
    assert rig.state(worktree.worktree_id) == "removed"
    assert "git worktree add failed (RuntimeError)" in caplog.text
    for secret in (worktree.path, worktree.worktree_id, BRANCH, str(tmp_path)):
        assert secret not in caplog.text


@needs_git
@pytest.mark.parametrize(
    "failure",
    [worktree_archive.ArchiveError("git_failed"), OSError("no private folder")],
    ids=["archive-error", "os-error"],
)
async def test_a_failure_after_the_add_keeps_the_tree_and_the_archive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failure: Exception,
) -> None:
    rig = _rig(tmp_path)
    worktree, _ = await _swept(rig)
    archive = _archive(rig, worktree.worktree_id)

    def failing(git: CodeGit, tree: Path, oid: str) -> None:
        raise failure

    monkeypatch.setattr(worktree_archive, "restore_files", failing)
    caplog.set_level(logging.WARNING)

    assert await _refusal(rig, worktree.worktree_id) == "git_failed"

    assert rig.state(worktree.worktree_id) == "kept"  # never `ready`: no sweep archives it
    assert _archive(rig, worktree.worktree_id) == archive
    assert root_key(worktree.path) in rig.listed()
    assert f"restore kept a partial tree ({type(failure).__name__})" in caplog.text
    for secret in (worktree.path, worktree.worktree_id, BRANCH, str(tmp_path)):
        assert secret not in caplog.text


@needs_git
async def test_removing_a_half_restored_tree_keeps_it_kept_and_a_sweep_leaves_its_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Remove of the tree a failed restore left (no index yet, so every file reads as deleted)
    keeps it `kept`, never `ready`, so no later sweep archives that broken tree over the
    archive that holds its changes."""
    rig = _rig(tmp_path)
    worktree, _ = await _swept(rig)
    archive = _archive(rig, worktree.worktree_id)
    tree = Path(worktree.path)

    def failing(git: CodeGit, path: Path, oid: str) -> None:
        raise worktree_archive.ArchiveError("git_failed")  # a smudge filter missing, a timeout

    with monkeypatch.context() as patch:
        patch.setattr(worktree_archive, "restore_files", failing)
        assert await _refusal(rig, worktree.worktree_id) == "git_failed"
    assert rig.state(worktree.worktree_id) == "kept"
    workspace = rig.workspaces.get(rig.workspace_id, kind="code")

    removal = await rig.service.remove(
        worktree.worktree_id, workspace.policy_revision, workspace.root.physical_id
    )

    assert (removal.outcome, removal.reason, removal.worktree.state) == ("kept", "dirty", "kept")
    assert rig.state(worktree.worktree_id) == "kept"
    stamp = PAST.timestamp()  # idle past its tier, so a `ready` row would be archived again
    _stamp_tree(tree, stamp)
    for name in ("HEAD", "index", "logs/HEAD"):
        git_path = _git(tree, "rev-parse", "--git-path", name).strip()
        if os.path.lexists(git_path):  # no index: the restore stopped before writing one
            os.utime(git_path, (stamp, stamp))
    left = sorted(os.listdir(tree))

    assert await rig.service.sweep() == []

    assert rig.state(worktree.worktree_id) == "kept"
    assert _archive(rig, worktree.worktree_id) == archive
    assert sorted(os.listdir(tree)) == left
    assert root_key(worktree.path) in rig.listed()


@needs_git
async def test_no_folder_for_the_hooks_refuses_unavailable_before_the_add(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _rig(tmp_path)
    worktree, _ = await _swept(rig)
    archive = _archive(rig, worktree.worktree_id)
    before = _scene(rig, worktree)

    def full(*args: object, **kwargs: object) -> str:
        raise OSError(errno.ENOSPC, "no space left on device")

    monkeypatch.setattr(tempfile, "mkdtemp", full)

    assert await _refusal(rig, worktree.worktree_id) == "unavailable"

    assert _scene(rig, worktree) == before
    assert rig.state(worktree.worktree_id) == "removed"
    assert _archive(rig, worktree.worktree_id) == archive


@needs_git
async def test_a_row_outside_the_current_area_refuses_before_any_write(tmp_path: Path) -> None:
    """A row recorded before the data folder moved: its path is outside the private area now,
    so a restore there would write outside it."""
    rig = _rig(tmp_path)
    worktree, _ = await _swept(rig)
    moved = worktree_area(tmp_path / "moved")
    service = WorktreeService(
        rig.store, rig.workspaces, rig.git, rig.coordinator, rig.resolver, rig.roots,
        area=moved, now=lambda: NOW, enabled=lambda: True,
    )  # fmt: skip
    workspace = rig.workspaces.get(rig.workspace_id, kind="code")
    row = rig.store.get_worktree(worktree.worktree_id)
    before = _scene(rig, worktree)

    with pytest.raises(WorktreeError) as refused:
        await service.restore(
            worktree.worktree_id, workspace.policy_revision, workspace.root.physical_id
        )

    assert refused.value.code == "unavailable"
    assert _scene(rig, worktree) == before
    assert rig.store.get_worktree(worktree.worktree_id) == row  # not even its stamp moved
    assert not os.path.lexists(moved)


@needs_git
@pytest.mark.parametrize("added", [False, True], ids=["before-the-add", "after-the-add"])
async def test_a_restore_cut_short_keeps_its_archive_through_recovery(
    tmp_path: Path, added: bool
) -> None:
    """The launch after a restore stopped part way puts the row back to `removed` while no tree
    was made, so the archive stays listed, and keeps a tree it began as `kept`: never deleted,
    never `ready` for a later sweep to archive over the archive."""
    rig = _rig(tmp_path)
    worktree, _ = await _swept(rig)
    archive = _archive(rig, worktree.worktree_id)
    rig.store.revive_worktree(worktree.worktree_id, now_iso(), limit=MAX_WORKTREES)
    if added:
        head = f"{archive}^1"
        _git(rig.repo, "worktree", "add", "--no-checkout", "-b", BRANCH, worktree.path, head)

    await rig.service.recover()

    assert rig.state(worktree.worktree_id) == ("kept" if added else "removed")
    assert _archive(rig, worktree.worktree_id) == archive
    listed = [found.worktree_id for found in rig.service.archives(rig.workspace_id)]
    assert listed == ([] if added else [worktree.worktree_id])


# ---- archives --------------------------------------------------------------------------


@needs_git
async def test_archives_lists_only_this_folders_removed_rows(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    worktree, _ = await _swept(rig)
    ready = await rig.create("viki/20261003-cc33")
    foreign, absent = str(uuid.uuid4()), str(uuid.uuid4())
    rig.insert_ready(foreign, path=rig.area / foreign, workspace_id=str(uuid.uuid4()))
    rig.store.update_worktree_state(foreign, "ready", "removed", now_iso())
    for worktree_id in (ready.worktree_id, foreign, absent):
        _dated_archive(rig, worktree_id, NOW)

    found = rig.service.archives(rig.workspace_id)

    assert [(a.worktree_id, a.oid) for a in found] == [
        (worktree.worktree_id, _archive(rig, worktree.worktree_id))
    ]


@needs_git
async def test_archives_answers_the_newest_first_up_to_the_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _rig(tmp_path)
    ids = [str(uuid.uuid4()) for _ in range(3)]
    for days, worktree_id in enumerate(ids):
        rig.insert_ready(worktree_id, path=rig.area / worktree_id)
        rig.store.update_worktree_state(worktree_id, "ready", "removed", now_iso())
        _dated_archive(rig, worktree_id, NOW - timedelta(days=days))
    monkeypatch.setattr(worktrees, "MAX_ARCHIVES", 2)

    assert [found.worktree_id for found in rig.service.archives(rig.workspace_id)] == ids[:2]


@needs_git
async def test_archives_of_an_untrusted_folder_refuses_before_any_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _rig(tmp_path, trust=False)
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(rig.git, "git", lambda root, *args, **kw: calls.append(args) or "")

    with pytest.raises(WorktreeError) as refused:
        rig.service.archives(rig.workspace_id)

    assert refused.value.code == "trust_required"
    assert calls == []
