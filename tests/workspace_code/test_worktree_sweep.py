"""`WorktreeService.plan`, the dry run that decides every `ready` row's cleanup action from real
Git and disk state without leasing, renaming or writing anything (AC-8), and `sweep`, which acts
on those verdicts at once, and `run_sweeps`, its loop (AC-3, AC-4, AC-5, AC-7, AC-9)."""

from __future__ import annotations

import asyncio
import errno
import logging
import os
import shutil
import stat
import sys
import tempfile
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from tests.app.test_workspaces import COMMAND, make_junction, service_at, store_at
from tests.workspace_code.test_code_git import _commit, _git
from tests.workspace_code.test_workspaces_code import trusted
from tests.workspace_code.test_worktree_removal import SESSION, FakeActor
from tests.workspace_code.test_worktrees import (
    _committed,
    _empty_git_environment,  # noqa: F401 (autouse: no developer Git config)
    needs_git,
)
from viki_agent import winproc
from viki_agent.code import worktree_archive, worktrees
from viki_agent.code.code_files import CodeFiles
from viki_agent.code.code_git import CodeGit
from viki_agent.code.session_roots import LiveActor, SessionRoots
from viki_agent.code.worktree_archive import PREFIX, Snapshot
from viki_agent.code.worktree_rules import ARCHIVE_DAYS, MAX_UNTRACKED_FILE, Decision, Facts
from viki_agent.code.worktrees import Worktree, WorktreeError, WorktreeService, worktree_area
from viki_agent.session import now_iso
from viki_agent.storage import SessionStore
from viki_agent.vaults.vault_mutation import MutationCoordinator, OrdinaryMutationRoot
from viki_agent.vaults.workspace_mutation import WorkspaceMutationResolver
from viki_agent.workspaces import WorkspaceService, root_key

BRANCH = "viki/20261003-ab12"  # matches AUTO_BRANCH: a 24h tier
NOW = datetime(2026, 11, 10, 12, 0, 0, tzinfo=UTC)
RECENT = NOW - timedelta(hours=1)  # inside the 24h tier
PAST = NOW - timedelta(hours=25)  # past the 24h tier
HANG_GUARD_S = 10.0  # guards a real process start or stop, and events that fire in milliseconds


@dataclass
class Rig:
    store: SessionStore
    workspaces: WorkspaceService
    git: CodeGit
    coordinator: MutationCoordinator
    resolver: WorkspaceMutationResolver
    roots: SessionRoots
    service: WorktreeService
    workspace_id: str
    repo: Path
    area: Path

    async def create(self, branch: str = BRANCH) -> Worktree:
        workspace = self.workspaces.get(self.workspace_id, kind="code")
        return await self.service.create(
            self.workspace_id, workspace.policy_revision, workspace.root.physical_id, branch
        )

    def state(self, worktree_id: str) -> str | None:
        row = self.store.get_worktree(worktree_id)
        return row["state"] if row is not None else None

    def root(self) -> OrdinaryMutationRoot:
        workspace = self.workspaces.get(self.workspace_id, kind="code")
        return self.resolver(
            self.workspace_id, workspace.policy_revision, workspace.root.physical_id, kind="code"
        )

    def listed(self) -> set[str]:
        raw = _git(self.repo, "worktree", "list", "--porcelain", "-z")
        return {
            root_key(part[len("worktree ") :])
            for part in raw.split("\0")
            if part.startswith("worktree ")
        }

    def insert_ready(
        self,
        worktree_id: str,
        *,
        path: Path,
        branch: str = BRANCH,
        workspace_id: str | None = None,
    ) -> None:
        """A `ready` row Git never made, as `plan` sees rows it cannot admit or find."""
        stamp = "2026-01-01T00:00:00.000Z"
        self.store.insert_worktree(
            {
                "worktree_id": worktree_id,
                "workspace_id": workspace_id if workspace_id is not None else self.workspace_id,
                "path": str(path),
                "branch": branch,
                "base_oid": "a" * 40,
                "state": "creating",
                "created_at": stamp,
                "updated_at": stamp,
            },
            limit=32,
        )
        self.store.update_worktree_state(worktree_id, "creating", "ready", stamp)


def _rig(
    tmp_path: Path,
    *,
    trust: bool = True,
    now: Callable[[], datetime] = lambda: NOW,
    in_use: Callable[[str], bool] = lambda _worktree_id: False,
    enabled: Callable[[], bool] = lambda: True,
    actor: Callable[[str], LiveActor | None] = lambda _session_id: None,
) -> Rig:
    repo = _committed(tmp_path)
    store = store_at(tmp_path)
    # Git available, so the registered identity holds the Git common dir admission needs.
    workspaces = service_at(tmp_path, store, git=True)
    if trust:
        workspace_id = trusted(workspaces, repo)
    else:
        workspace_id = workspaces.register_existing(COMMAND, str(repo), kind="code")[0].workspace_id
    git = CodeGit(workspaces, CodeFiles(workspaces), git_available=lambda: True)
    coordinator = MutationCoordinator()
    resolver = WorkspaceMutationResolver(workspaces, git_available=lambda: True)
    roots = SessionRoots(store, workspaces, exists=lambda _: False, actor=actor)
    area = worktree_area(tmp_path / "data")
    service = WorktreeService(
        store, workspaces, git, coordinator, resolver, roots,
        area=area, now=now, in_use=in_use, enabled=enabled,
    )  # fmt: skip
    return Rig(
        store, workspaces, git, coordinator, resolver, roots, service, workspace_id, repo, area
    )


def _stamp_tree(tree: Path, stamp: float) -> None:
    paths = [tree]
    for root, dirs, files in os.walk(tree):
        paths.extend(Path(root) / name for name in dirs)
        paths.extend(Path(root) / name for name in files)
    for path in paths:
        os.utime(path, (stamp, stamp))


def _age(tree: Path, moment: datetime) -> None:
    """Back-date `tree` (every file and folder) and its per-worktree `HEAD`, `index` and
    `logs/HEAD` to `moment`, so `worktree_probe.inspect` reads the worktree as idle since
    then. A clean tracked file's mtime-only mismatch makes Git rewrite the index once, to
    the real "now", the next time something reads status; one settling `status` call (the
    exact one `inspect` itself runs) absorbs that rewrite before the three git-path files
    are stamped last, so nothing moves their mtime again before the test's own `plan` call."""
    stamp = moment.timestamp()
    _stamp_tree(tree, stamp)
    _git(tree, "status", "--porcelain=v2", "-z", "--branch", "--untracked-files=all")
    for name in ("HEAD", "index", "logs/HEAD"):
        git_path = Path(_git(tree, "rev-parse", "--git-path", name).strip())
        os.utime(git_path, (stamp, stamp))


# ---- the gates ahead of any disk probe (no idle simulation needed) ----------------------


@needs_git
async def test_outside_the_area_keeps_unknown(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    worktree_id = "00000000-0000-4000-8000-000000000001"
    rig.insert_ready(worktree_id, path=tmp_path / "elsewhere" / worktree_id)

    decisions = await rig.service.plan()

    assert decisions == [Decision(worktree_id, "keep", "unknown", None, None, None)]


@needs_git
async def test_an_untrusted_folder_keeps_untrusted_without_running_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _rig(tmp_path, trust=False)
    worktree_id = "00000000-0000-4000-8000-000000000002"
    rig.insert_ready(worktree_id, path=rig.area / worktree_id)
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(rig.git, "git", lambda root, *args, **kw: calls.append(args) or "")

    decisions = await rig.service.plan()

    assert decisions == [Decision(worktree_id, "keep", "untrusted", None, None, None)]
    assert calls == []  # an unadmitted folder is never asked anything


@needs_git
async def test_a_live_viki_session_keeps_live(tmp_path: Path) -> None:
    live_ids: set[str] = set()
    rig = _rig(tmp_path, in_use=lambda worktree_id: worktree_id in live_ids)
    worktree = await rig.create()
    live_ids.add(worktree.worktree_id)

    decisions = await rig.service.plan()

    assert decisions == [Decision(worktree.worktree_id, "keep", "live", None, None, None)]


@needs_git
async def test_a_locked_worktree_keeps_locked(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    worktree = await rig.create()
    _git(rig.repo, "worktree", "lock", worktree.path)

    decisions = await rig.service.plan()

    assert decisions == [Decision(worktree.worktree_id, "keep", "locked", None, None, None)]


@needs_git
async def test_a_gone_folder_removes_missing(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    worktree = await rig.create()
    shutil.rmtree(worktree.path)

    decisions = await rig.service.plan()

    assert decisions == [Decision(worktree.worktree_id, "remove", "missing", None, None, None)]
    assert rig.state(worktree.worktree_id) == "ready"  # a dry run: nothing acted on it
    assert root_key(worktree.path) in rig.listed()


# ---- recency, then every reason `inspect` can surface, each isolated -------------------


@needs_git
async def test_idle_within_the_tier_keeps_recent(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    worktree = await rig.create()
    _age(Path(worktree.path), RECENT)

    decisions = await rig.service.plan()

    assert len(decisions) == 1
    d = decisions[0]
    assert (d.action, d.reason) == ("keep", "recent")
    assert d.idle_since is not None and d.eligible_at is not None and d.bytes is not None


@needs_git
async def test_a_clean_idle_worktree_removes_merged(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    worktree = await rig.create()
    _age(Path(worktree.path), PAST)

    decisions = await rig.service.plan()

    assert len(decisions) == 1
    d = decisions[0]
    assert (d.action, d.reason) == ("remove", "merged")
    assert d.idle_since is not None and d.eligible_at is not None and d.bytes is not None


@needs_git
async def test_a_committed_gitlink_keeps_submodule(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    worktree = await rig.create()
    tree = Path(worktree.path)
    head = _git(tree, "rev-parse", "HEAD").strip()
    _git(tree, "update-index", "--add", "--cacheinfo", "160000", head, "sub")
    # Not `_commit`: its `add -A` scans the working tree, finds no real "sub" there, and
    # stages the gitlink's removal again, since it is only an index entry, never a real file.
    _git(tree, "commit", "-q", "-m", "add gitlink")
    _age(tree, PAST)

    decisions = await rig.service.plan()

    assert len(decisions) == 1
    assert (decisions[0].action, decisions[0].reason) == ("keep", "submodule")


@needs_git
async def test_a_nested_repository_keeps_embedded_repo(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    worktree = await rig.create()
    tree = Path(worktree.path)
    nested = tree / "nested" / "embedded"
    nested.mkdir(parents=True)
    _git(nested, "init", "-q")
    _age(tree, PAST)

    decisions = await rig.service.plan()

    assert len(decisions) == 1
    assert (decisions[0].action, decisions[0].reason) == ("keep", "embedded_repo")


@needs_git
async def test_a_private_worktree_ref_keeps_worktree_refs(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    worktree = await rig.create()
    tree = Path(worktree.path)
    _git(tree, "update-ref", "refs/worktree/x", "HEAD")
    _age(tree, PAST)

    decisions = await rig.service.plan()

    assert len(decisions) == 1
    assert (decisions[0].action, decisions[0].reason) == ("keep", "worktree_refs")


@needs_git
async def test_an_assume_unchanged_entry_keeps_hidden_changes(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    worktree = await rig.create()
    tree = Path(worktree.path)
    _git(tree, "update-index", "--assume-unchanged", "a.txt")
    _age(tree, PAST)

    decisions = await rig.service.plan()

    assert len(decisions) == 1
    assert (decisions[0].action, decisions[0].reason) == ("keep", "hidden_changes")


@needs_git
async def test_a_merge_in_progress_keeps_in_progress(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    worktree = await rig.create()
    tree = Path(worktree.path)
    marker = Path(_git(tree, "rev-parse", "--git-path", "MERGE_HEAD").strip())
    marker.write_text(_git(tree, "rev-parse", "HEAD").strip() + "\n", encoding="utf-8")
    _age(tree, PAST)

    decisions = await rig.service.plan()

    assert len(decisions) == 1
    assert (decisions[0].action, decisions[0].reason) == ("keep", "in_progress")


@needs_git
async def test_a_switched_branch_keeps_branch_changed(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    worktree = await rig.create()
    tree = Path(worktree.path)
    _git(tree, "checkout", "-q", "-b", "other")
    _age(tree, PAST)

    decisions = await rig.service.plan()

    assert len(decisions) == 1
    assert (decisions[0].action, decisions[0].reason) == ("keep", "branch_changed")


@needs_git
async def test_an_oversized_untracked_file_keeps_large_untracked(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    worktree = await rig.create()
    tree = Path(worktree.path)
    big = tree / "big.bin"
    big.write_bytes(b"")
    os.truncate(big, MAX_UNTRACKED_FILE + 1)
    _age(tree, PAST)

    decisions = await rig.service.plan()

    assert len(decisions) == 1
    assert (decisions[0].action, decisions[0].reason) == ("keep", "large_untracked")


@needs_git
async def test_a_tracked_edit_archives_dirty(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    worktree = await rig.create()
    tree = Path(worktree.path)
    (tree / "a.txt").write_text("edited\n", encoding="utf-8")
    _age(tree, PAST)

    decisions = await rig.service.plan()

    assert len(decisions) == 1
    d = decisions[0]
    assert (d.action, d.reason) == ("archive", "dirty")
    assert d.idle_since is not None and d.eligible_at is not None and d.bytes is not None


@needs_git
async def test_a_commit_on_no_other_ref_archives_unpushed(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    worktree = await rig.create()
    tree = Path(worktree.path)
    (tree / "a.txt").write_text("more\n", encoding="utf-8")
    _commit(tree, "more")
    _age(tree, PAST)

    decisions = await rig.service.plan()

    assert len(decisions) == 1
    assert (decisions[0].action, decisions[0].reason) == ("archive", "unpushed")


@needs_git
async def test_an_unreadable_listing_keeps_every_row_in_the_folder_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _rig(tmp_path)
    worktree = await rig.create()
    _age(Path(worktree.path), PAST)  # otherwise eligible to remove, were the listing read
    real = rig.git.git

    def failing(root: Path, *args: str, timeout: float = 5.0) -> str:
        if args[:2] == ("worktree", "list"):
            raise RuntimeError("git worktree failed")
        return real(root, *args, timeout=timeout)

    monkeypatch.setattr(rig.git, "git", failing)

    decisions = await rig.service.plan()

    assert decisions == [Decision(worktree.worktree_id, "keep", "unknown", None, None, None)]


# ---- scoping, and the no-op guarantee ----------------------------------------------------


@needs_git
async def test_plan_of_an_untrusted_workspace_refuses_trust_required(tmp_path: Path) -> None:
    rig = _rig(tmp_path, trust=False)

    with pytest.raises(WorktreeError) as refused:
        await rig.service.plan(rig.workspace_id)

    assert refused.value.code == "trust_required"


@needs_git
async def test_plan_scoped_to_one_workspace_excludes_another_folders_rows(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    worktree = await rig.create()
    other_repo = _committed(tmp_path / "other")
    other_workspace_id = trusted(rig.workspaces, other_repo)
    other_id = "00000000-0000-4000-8000-000000000099"
    rig.insert_ready(other_id, path=tmp_path / "elsewhere", workspace_id=other_workspace_id)

    scoped = await rig.service.plan(rig.workspace_id)
    everyone = await rig.service.plan()

    assert [d.worktree_id for d in scoped] == [worktree.worktree_id]
    assert {d.worktree_id for d in everyone} == {worktree.worktree_id, other_id}


@needs_git
async def test_a_plan_changes_no_row_ref_or_file(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    mergeable = await rig.create("viki/20261003-aa11")
    dirty = await rig.create("viki/20261003-bb22")
    (Path(dirty.path) / "new.txt").write_text("new\n", encoding="utf-8")
    _age(Path(mergeable.path), PAST)
    _age(Path(dirty.path), PAST)
    before_listed = rig.listed()
    before_rows, _ = rig.store.list_worktrees(100)
    before_refs = _git(rig.repo, "for-each-ref", "--format=%(refname)")

    decisions = await rig.service.plan()

    actions = {d.worktree_id: d.action for d in decisions}
    assert actions[mergeable.worktree_id] == "remove"
    assert actions[dirty.worktree_id] == "archive"
    assert rig.listed() == before_listed
    rows_after, _ = rig.store.list_worktrees(100)
    assert rows_after == before_rows
    assert _git(rig.repo, "for-each-ref", "--format=%(refname)") == before_refs
    assert Path(mergeable.path).is_dir() and Path(dirty.path).is_dir()
    assert rig.state(mergeable.worktree_id) == "ready"
    assert rig.state(dirty.worktree_id) == "ready"


@needs_git
async def test_a_path_that_cannot_be_resolved_keeps_unknown_in_the_plan_and_the_sweep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    rig = _rig(tmp_path)
    worktree = await rig.create()
    _age(Path(worktree.path), PAST)  # otherwise eligible to remove

    def unresolved(path: str) -> str:
        raise OSError(64, "network name deleted")

    monkeypatch.setattr(worktrees, "_listing_key", unresolved)
    caplog.set_level(logging.WARNING)
    kept = [Decision(worktree.worktree_id, "keep", "unknown", None, None, None)]

    assert await rig.service.plan() == kept
    assert await rig.service.sweep() == kept

    _assert_untouched(rig, worktree)
    assert "path not resolved (OSError)" in caplog.text


@needs_git
async def test_the_plan_deadline_keeps_a_row_not_yet_reached_unknown_without_inspecting_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R11: a dry run over many rows must still answer within the client's own timeout, so a row
    `plan` has not reached once `PLAN_DEADLINE_S` is up answers `keep unknown` untouched, while a
    row it already reached keeps its real, inspected verdict."""
    rig = _rig(tmp_path)
    one = await rig.create("viki/20261003-aa11")
    two = await rig.create("viki/20261003-bb22")
    _age(Path(one.path), PAST)
    _age(Path(two.path), PAST)
    monkeypatch.setattr(worktrees, "PLAN_DEADLINE_S", 0.2)
    real_inspect = worktrees.inspect
    inspected: list[str] = []

    def slow_inspect(git: CodeGit, tree: Path) -> Facts:
        facts = real_inspect(git, tree)
        inspected.append(str(tree))
        time.sleep(0.4)  # pushes `plan` past its 0.2s deadline before the next row's check
        return facts

    monkeypatch.setattr(worktrees, "inspect", slow_inspect)

    decisions = await rig.service.plan()

    assert len(inspected) == 1  # the deadline stopped `plan` from ever inspecting the second row
    reached, unreached = (one, two) if inspected == [one.path] else (two, one)
    verdicts = {d.worktree_id: d for d in decisions}
    assert (verdicts[reached.worktree_id].action, verdicts[reached.worktree_id].reason) == (
        "remove",
        "merged",
    )
    assert verdicts[unreached.worktree_id] == Decision(
        unreached.worktree_id, "keep", "unknown", None, None, None
    )


# ---- the acting sweep ------------------------------------------------------------------


def _branches(rig: Rig) -> set[str]:
    return set(_git(rig.repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/").split())


def _archive(rig: Rig, worktree_id: str) -> str:
    """The commit `refs/viki/archive/<worktree_id>` names, or "" without one."""
    return _git(rig.repo, "for-each-ref", "--format=%(objectname)", PREFIX + worktree_id).strip()


def _assert_untouched(rig: Rig, worktree: Worktree) -> None:
    """The tree, its row, its Git registration and its branch as the sweep found them."""
    assert Path(worktree.path).is_dir()
    assert rig.state(worktree.worktree_id) == "ready"
    assert root_key(worktree.path) in rig.listed()
    assert worktree.branch in _branches(rig)
    assert not rig.roots.blocked(worktree.worktree_id)
    assert not os.path.lexists(rig.area / f".trash-{worktree.worktree_id}")


def _verdicts(decisions: list[Decision]) -> dict[str, tuple[str, str]]:
    return {d.worktree_id: (d.action, d.reason) for d in decisions}


@needs_git
async def test_a_sweep_removes_an_idle_merged_tree_and_archives_an_idle_dirty_one(
    tmp_path: Path,
) -> None:
    rig = _rig(tmp_path)
    merged = await rig.create("viki/20261003-aa11")
    dirty = await rig.create("viki/20261003-bb22")
    (Path(dirty.path) / "a.txt").write_text("edited\n", encoding="utf-8")
    (Path(dirty.path) / "new.txt").write_text("new\n", encoding="utf-8")
    _age(Path(merged.path), PAST)
    _age(Path(dirty.path), PAST)

    decisions = await rig.service.sweep()

    assert _verdicts(decisions) == {
        merged.worktree_id: ("remove", "merged"),
        dirty.worktree_id: ("archive", "dirty"),
    }
    for worktree in (merged, dirty):
        assert not Path(worktree.path).exists()
        assert root_key(worktree.path) not in rig.listed()
        assert rig.state(worktree.worktree_id) == "removed"
        assert not rig.roots.blocked(worktree.worktree_id)
    assert list(rig.area.iterdir()) == []  # the renamed-aside folders are deleted too
    archive = _archive(rig, dirty.worktree_id)
    assert _git(rig.repo, "show", f"{archive}:a.txt") == "edited\n"
    assert _git(rig.repo, "show", f"{archive}:new.txt") == "new\n"
    assert _archive(rig, merged.worktree_id) == ""
    assert _branches(rig) == {"main"}


@needs_git
async def test_a_branch_also_on_a_remote_stays_when_its_tree_goes(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    worktree = await rig.create()
    _git(rig.repo, "update-ref", f"refs/remotes/origin/{BRANCH}", worktree.base_oid)
    _age(Path(worktree.path), PAST)

    decisions = await rig.service.sweep()

    assert _verdicts(decisions) == {worktree.worktree_id: ("remove", "merged")}
    assert not Path(worktree.path).exists()
    assert rig.state(worktree.worktree_id) == "removed"
    assert BRANCH in _branches(rig)


@needs_git
async def test_a_folder_deleted_by_hand_is_unregistered_and_its_branch_stays(
    tmp_path: Path,
) -> None:
    rig = _rig(tmp_path)
    worktree = await rig.create()
    shutil.rmtree(worktree.path)

    decisions = await rig.service.sweep()

    assert decisions == [Decision(worktree.worktree_id, "remove", "missing", None, None, None)]
    assert rig.state(worktree.worktree_id) == "removed"
    assert root_key(worktree.path) not in rig.listed()
    assert BRANCH in _branches(rig)


@needs_git
@pytest.mark.parametrize("detach", [True, False], ids=["detached", "other-branch"])
async def test_a_gone_folder_on_another_head_keeps_branch_changed_and_its_registration(
    tmp_path: Path, detach: bool
) -> None:
    """Git still lists the gone folder on a detached HEAD or another branch: that HEAD and its
    reflog live only in the registration, so the plan and the sweep keep it, and the commit
    stays reachable for Git's gc."""
    rig = _rig(tmp_path)
    worktree = await rig.create()
    tree = Path(worktree.path)
    _git(tree, "checkout", "-q", *(["--detach"] if detach else ["-b", "other"]))
    (tree / "work.txt").write_text("work\n", encoding="utf-8")
    _commit(tree, "work")
    commit = _git(tree, "rev-parse", "HEAD").strip()
    shutil.rmtree(tree)  # deleted by hand
    kept = [Decision(worktree.worktree_id, "keep", "branch_changed", None, None, None)]

    assert await rig.service.plan() == kept
    assert await rig.service.sweep() == kept

    assert rig.state(worktree.worktree_id) == "ready"
    assert root_key(worktree.path) in rig.listed()
    assert commit in _git(rig.repo, "rev-list", "--all", "--reflog").split()


@needs_git
async def test_a_named_tree_idle_for_48_hours_stays_recent(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    worktree = await rig.create("feature/x")
    _age(Path(worktree.path), NOW - timedelta(hours=48))

    decisions = await rig.service.sweep()

    assert _verdicts(decisions) == {worktree.worktree_id: ("keep", "recent")}
    _assert_untouched(rig, worktree)


@needs_git
async def test_the_sweep_keeps_each_guarded_tree_with_its_reason(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    reasons = (
        "locked",
        "submodule",
        "embedded_repo",
        "worktree_refs",
        "hidden_changes",
        "in_progress",
        "branch_changed",
        "large_untracked",
    )
    trees = {reason: await rig.create(f"viki/20261003-{n:04x}") for n, reason in enumerate(reasons)}
    path = {reason: Path(worktree.path) for reason, worktree in trees.items()}
    _git(rig.repo, "worktree", "lock", str(path["locked"]))
    head = _git(path["submodule"], "rev-parse", "HEAD").strip()
    _git(path["submodule"], "update-index", "--add", "--cacheinfo", "160000", head, "sub")
    _git(path["submodule"], "commit", "-q", "-m", "add gitlink")
    (path["embedded_repo"] / "nested").mkdir()
    _git(path["embedded_repo"] / "nested", "init", "-q")
    _git(path["worktree_refs"], "update-ref", "refs/worktree/x", "HEAD")
    _git(path["hidden_changes"], "update-index", "--assume-unchanged", "a.txt")
    marker = Path(_git(path["in_progress"], "rev-parse", "--git-path", "MERGE_HEAD").strip())
    marker.write_text(head + "\n", encoding="utf-8")
    _git(path["branch_changed"], "checkout", "-q", "-b", "other")
    big = path["large_untracked"] / "big.bin"
    big.write_bytes(b"")
    os.truncate(big, MAX_UNTRACKED_FILE + 1)
    for tree in path.values():
        _age(tree, PAST)

    decisions = await rig.service.sweep()

    assert _verdicts(decisions) == {
        worktree.worktree_id: ("keep", reason) for reason, worktree in trees.items()
    }
    for worktree in trees.values():
        _assert_untouched(rig, worktree)


@needs_git
async def test_a_live_viki_session_keeps_its_tree(tmp_path: Path) -> None:
    actors: dict[str, FakeActor] = {}
    rig = _rig(tmp_path, actor=actors.get)
    worktree = await rig.create()
    rig.store.put_session_root(
        {
            "session_id": SESSION,
            "workspace_id": rig.workspace_id,
            "worktree_id": worktree.worktree_id,
            "display_root": worktree.path,
            "created_at": now_iso(),
        }
    )
    actors[SESSION] = FakeActor(live=True)
    _age(Path(worktree.path), PAST)

    decisions = await rig.service.sweep()

    assert _verdicts(decisions) == {worktree.worktree_id: ("keep", "live")}
    _assert_untouched(rig, worktree)


@needs_git
async def test_a_check_that_starts_after_the_verdict_keeps_its_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The live re-check after the spawn block: a check or preview starts while `inspect`
    runs, after the verdict's own live check passed."""
    live: set[str] = set()
    rig = _rig(tmp_path, in_use=lambda worktree_id: worktree_id in live)
    worktree = await rig.create()
    _age(Path(worktree.path), PAST)
    real = worktrees.inspect

    def inspect_then_start(git: CodeGit, tree: Path) -> Facts:
        facts = real(git, tree)
        live.add(worktree.worktree_id)
        return facts

    monkeypatch.setattr(worktrees, "inspect", inspect_then_start)

    decisions = await rig.service.sweep()

    assert _verdicts(decisions) == {worktree.worktree_id: ("keep", "live")}
    _assert_untouched(rig, worktree)


@needs_git
@pytest.mark.skipif(sys.platform == "darwin", reason="no macOS runner for the lsof check")
async def test_a_process_with_its_cwd_inside_keeps_in_use(tmp_path: Path) -> None:
    """Windows refuses the rename aside; Linux finds the cwd in `/proc` before it."""
    rig = _rig(tmp_path)
    worktree = await rig.create()
    deep = Path(worktree.path) / "dir" / "deep"
    deep.mkdir()
    _age(Path(worktree.path), PAST)
    child = await winproc.spawn(
        [sys.executable, "-c", "import time; time.sleep(60)"], cwd=deep, env=os.environ
    )
    try:
        decisions = await rig.service.sweep()
    finally:
        child.kill_tree()
        await asyncio.wait_for(child.process.wait(), HANG_GUARD_S)
        child.release()

    assert _verdicts(decisions) == {worktree.worktree_id: ("keep", "in_use")}
    _assert_untouched(rig, worktree)


@needs_git
async def test_a_busy_lease_keeps_busy_and_puts_the_row_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _rig(tmp_path)
    worktree = await rig.create()
    _age(Path(worktree.path), PAST)
    monkeypatch.setattr(worktrees, "LEASE_WAIT_S", 0.05)

    async with rig.coordinator.lease(rig.root(), ()):
        decisions = await rig.service.sweep()

    assert _verdicts(decisions) == {worktree.worktree_id: ("keep", "busy")}
    _assert_untouched(rig, worktree)


@needs_git
async def test_the_archive_ref_exists_before_the_tree_is_renamed_aside(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _rig(tmp_path)
    worktree = await rig.create()
    tree = Path(worktree.path)
    (tree / "a.txt").write_text("edited\n", encoding="utf-8")
    _age(tree, PAST)
    real = os.rename
    seen: list[tuple[str, str | None, bool]] = []

    def spy(source: str | os.PathLike[str], target: str | os.PathLike[str]) -> None:
        worktree_id = worktree.worktree_id
        seen.append(
            (_archive(rig, worktree_id), rig.state(worktree_id), rig.roots.blocked(worktree_id))
        )
        real(source, target)

    monkeypatch.setattr(worktrees.os, "rename", spy)

    decisions = await rig.service.sweep()

    assert _verdicts(decisions) == {worktree.worktree_id: ("archive", "dirty")}
    assert len(seen) == 1
    archive, state, blocked = seen[0]
    assert archive != "" and archive == _archive(rig, worktree.worktree_id)
    assert (state, blocked) == ("removing", True)
    assert not tree.exists() and rig.state(worktree.worktree_id) == "removed"


@needs_git
async def test_a_second_snapshot_that_differs_keeps_changed_and_the_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _rig(tmp_path)
    worktree = await rig.create()
    tree = Path(worktree.path)
    (tree / "a.txt").write_text("edited\n", encoding="utf-8")
    _age(tree, PAST)
    real = worktree_archive.snapshot
    taken: list[Snapshot] = []

    def snapshot(git: CodeGit, path: Path) -> Snapshot:
        taken.append(real(git, path))
        # The second look finds another tree, as a write during the archive would leave.
        return taken[0] if len(taken) == 1 else replace(taken[-1], tree=taken[-1].index_tree)

    monkeypatch.setattr(worktree_archive, "snapshot", snapshot)

    decisions = await rig.service.sweep()

    assert _verdicts(decisions) == {worktree.worktree_id: ("keep", "changed")}
    assert len(taken) == 2
    assert _archive(rig, worktree.worktree_id) != ""  # saved before the second look, and kept
    _assert_untouched(rig, worktree)
    assert (tree / "a.txt").read_text(encoding="utf-8") == "edited\n"


@needs_git
@pytest.mark.parametrize("dirty", [True, False], ids=["archive", "merged"])
async def test_a_change_after_the_inspection_keeps_changed_without_an_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dirty: bool
) -> None:
    """An archive candidate's changed file touched past the inspected activity, or a merged
    one's status no longer empty: either keeps the tree, before any snapshot."""
    rig = _rig(tmp_path)
    worktree = await rig.create()
    tree = Path(worktree.path)
    if dirty:
        (tree / "a.txt").write_text("edited\n", encoding="utf-8")
    _age(tree, PAST)
    real = worktrees.inspect
    later = (PAST + timedelta(minutes=1)).timestamp()

    def inspect_then_change(git: CodeGit, path: Path) -> Facts:
        facts = real(git, path)
        if dirty:
            os.utime(path / "a.txt", (later, later))
        else:
            (path / "new.txt").write_text("new\n", encoding="utf-8")
        return facts

    monkeypatch.setattr(worktrees, "inspect", inspect_then_change)

    decisions = await rig.service.sweep()

    assert _verdicts(decisions) == {worktree.worktree_id: ("keep", "changed")}
    assert _archive(rig, worktree.worktree_id) == ""
    _assert_untouched(rig, worktree)


@needs_git
async def test_a_refused_worktree_remove_renames_the_tree_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    rig = _rig(tmp_path)
    worktree = await rig.create()
    _age(Path(worktree.path), PAST)
    real = rig.git.git

    def refusing(root: Path, *args: str, timeout: float = 5.0, index: Path | None = None) -> str:
        if ("worktree", "remove") in zip(args, args[1:], strict=False):  # after any `-c` pair
            raise RuntimeError(f"git worktree remove failed for {worktree.path}")
        if index is None:
            return real(root, *args, timeout=timeout)
        return real(root, *args, timeout=timeout, index=index)

    monkeypatch.setattr(rig.git, "git", refusing)
    caplog.set_level(logging.WARNING)

    decisions = await rig.service.sweep()

    assert _verdicts(decisions) == {worktree.worktree_id: ("keep", "git_refused")}
    _assert_untouched(rig, worktree)
    assert (Path(worktree.path) / "a.txt").read_text(encoding="utf-8") == "alpha\n"
    assert "git worktree remove failed (RuntimeError)" in caplog.text
    for secret in (worktree.path, worktree.worktree_id, BRANCH, str(tmp_path)):
        assert secret not in caplog.text


@needs_git
async def test_no_folder_for_the_hooks_keeps_git_refused_and_renames_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_hooks_off` cannot make its empty folder (a full disk): an idle merged tree is kept
    before its rename aside, and a gone folder before its `worktree remove`."""
    rig = _rig(tmp_path)
    merged = await rig.create("viki/20261003-aa11")
    gone = await rig.create("viki/20261003-bb22")
    _age(Path(merged.path), PAST)  # otherwise removed as merged
    shutil.rmtree(gone.path)  # otherwise unregistered as missing

    def full(*args: object, **kwargs: object) -> str:
        raise OSError(errno.ENOSPC, "no space left on device")

    monkeypatch.setattr(tempfile, "mkdtemp", full)

    decisions = await rig.service.sweep()

    assert _verdicts(decisions) == {
        merged.worktree_id: ("keep", "git_refused"),
        gone.worktree_id: ("keep", "git_refused"),
    }
    _assert_untouched(rig, merged)
    assert rig.state(gone.worktree_id) == "ready"
    assert root_key(gone.path) in rig.listed()


@needs_git
async def test_a_disabled_sweep_answers_nothing_and_runs_no_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _rig(tmp_path, enabled=lambda: False)
    worktree = await rig.create()
    _age(Path(worktree.path), PAST)
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(rig.git, "git", lambda root, *args, **kw: calls.append(args) or "")

    assert await rig.service.sweep() == []

    assert calls == []
    _assert_untouched(rig, worktree)


@needs_git
async def test_the_sweep_purges_leftover_trash_but_no_link_or_other_name(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    rig.area.mkdir()
    leftover = rig.area / f".trash-{uuid.uuid4()}"
    (leftover / "dir").mkdir(parents=True)
    held = leftover / "dir" / "read-only.txt"
    held.write_text("x\n", encoding="utf-8")
    held.chmod(stat.S_IREAD)  # Windows refuses to delete it until made writable again
    other = rig.area / ".trash-not-a-uuid"
    other.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep\n", encoding="utf-8")
    link = rig.area / f".trash-{uuid.uuid4()}"
    if sys.platform == "win32":
        make_junction(link, outside)
    else:
        link.symlink_to(outside, target_is_directory=True)

    assert await rig.service.sweep() == []

    assert not os.path.lexists(leftover)
    assert other.is_dir()
    assert os.path.lexists(link)
    assert (outside / "keep.txt").read_text(encoding="utf-8") == "keep\n"


@needs_git
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX folder modes: VIKI's Linux check")
async def test_the_sweep_purges_trash_with_folders_closed_to_writes_or_search(
    tmp_path: Path,
) -> None:
    rig = _rig(tmp_path)
    rig.area.mkdir()
    leftover = rig.area / f".trash-{uuid.uuid4()}"
    closed = leftover / "read-only" / "closed"
    closed.mkdir(parents=True)
    (closed.parent / "file.txt").write_text("x\n", encoding="utf-8")
    (closed / "file.txt").write_text("x\n", encoding="utf-8")
    closed.chmod(0o000)  # no read or search: `rmtree` cannot list it
    closed.parent.chmod(0o555)  # no write: no entry in it can be deleted

    assert await rig.service.sweep() == []

    assert not os.path.lexists(leftover)


def _dated_archive(rig: Rig, worktree_id: str, moment: datetime) -> None:
    """An archive ref whose commit the test's own Git dates at `moment`."""
    tree = _git(rig.repo, "rev-parse", "HEAD^{tree}").strip()
    stamp = f"{int(moment.timestamp())} +0000"
    done = winproc.run(
        ["git", "commit-tree", tree, "-m", "archive"],
        cwd=rig.repo,
        env={**os.environ, "GIT_COMMITTER_DATE": stamp, "GIT_AUTHOR_DATE": stamp},
    )
    assert done.returncode == 0, done.stderr
    _git(rig.repo, "update-ref", PREFIX + worktree_id, done.stdout.strip())


@needs_git
async def test_retention_drops_old_archives_of_removed_or_absent_rows_only(
    tmp_path: Path,
) -> None:
    rig = _rig(tmp_path)
    ready = await rig.create()
    _age(Path(ready.path), RECENT)  # kept `recent`, so its row stays `ready`
    old = NOW - timedelta(days=ARCHIVE_DAYS, hours=1)
    young = NOW - timedelta(days=ARCHIVE_DAYS - 1)
    removed_old, removed_young, absent = (str(uuid.uuid4()) for _ in range(3))
    for worktree_id in (removed_old, removed_young):
        rig.insert_ready(worktree_id, path=rig.area / worktree_id)
        rig.store.update_worktree_state(worktree_id, "ready", "removed", now_iso())
    _dated_archive(rig, removed_old, old)
    _dated_archive(rig, absent, old)
    _dated_archive(rig, removed_young, young)
    _dated_archive(rig, ready.worktree_id, old)

    decisions = await rig.service.sweep()

    assert _verdicts(decisions) == {ready.worktree_id: ("keep", "recent")}
    assert _archive(rig, removed_old) == ""
    assert _archive(rig, absent) == ""
    assert _archive(rig, removed_young) != ""
    assert _archive(rig, ready.worktree_id) != ""


@needs_git
async def test_run_sweeps_runs_at_once_and_after_the_interval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _rig(tmp_path)
    interval = 3600.0
    events: list[str | float] = []
    second = asyncio.Event()
    real_sleep = asyncio.sleep

    async def sweep() -> list[Decision]:
        events.append("sweep")
        if events.count("sweep") == 2:
            second.set()
        return []

    async def sleep(delay: float) -> None:
        if delay != interval:
            return await real_sleep(delay)
        events.append(delay)
        if events.count("sweep") == 2:
            await asyncio.Event().wait()  # parked here until cancelled

    with monkeypatch.context() as patch:
        patch.setattr(rig.service, "sweep", sweep)
        patch.setattr(worktrees.asyncio, "sleep", sleep)
        task = asyncio.create_task(rig.service.run_sweeps(interval))
        await asyncio.wait_for(second.wait(), HANG_GUARD_S)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert events == ["sweep", interval, "sweep", interval]


@needs_git
async def test_run_sweeps_logs_a_failed_round_by_type_and_goes_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    rig = _rig(tmp_path)
    rounds: list[int] = []
    second = asyncio.Event()

    async def sweep() -> list[Decision]:
        rounds.append(len(rounds) + 1)
        if len(rounds) == 1:
            raise RuntimeError(f"failed at {tmp_path}")
        second.set()
        return []

    monkeypatch.setattr(rig.service, "sweep", sweep)
    caplog.set_level(logging.WARNING)
    task = asyncio.create_task(rig.service.run_sweeps(0))
    await asyncio.wait_for(second.wait(), HANG_GUARD_S)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert "worktree: sweep failed (RuntimeError)" in caplog.text
    assert str(tmp_path) not in caplog.text


# ---- Bot copies, the Git-settings guard and retention by state ----------------------------

BOT = "00000000-0000-4000-8000-0000000000b0"


def bind_copy(rig: Rig, worktree_id: str, state: str = "bound") -> None:
    """Record `worktree_id` as a Bot's chat copy in `state`, as `BotCode` and a release do."""
    with rig.store._connection(write=True) as connection:
        connection.execute(
            "INSERT INTO bot_code_copies VALUES (?,?,?,?,NULL,?,?)",
            (worktree_id, rig.workspace_id, BOT, str(uuid.uuid4()), state, now_iso()),
        )


def baseline(rig: Rig) -> Path:
    """Swap in a CodeGit that checks the folder's Git settings against a Bot-copy baseline of
    their current state; answers the folder's root."""
    git = CodeGit(
        rig.workspaces, CodeFiles(rig.workspaces), git_available=lambda: True,
        bot_code_store=rig.store,
    )  # fmt: skip
    assert rig.store.insert_bot_code_baseline(
        {
            "workspace_id": rig.workspace_id,
            "git_fingerprint": git.settings_fingerprint(rig.repo),
            "created_at": now_iso(),
        }
    )
    rig.git = git
    rig.service._git = git
    return rig.repo


def change_settings(root: Path) -> None:
    _git(root, "config", "guard.synthetic", "changed")


def change_on_resolve(rig: Rig, monkeypatch: pytest.MonkeyPatch, call: int) -> None:
    """Change the folder's Git settings when the service admits it for the `call`th time."""
    resolve = rig.service._resolve_root
    calls = 0

    def resolving(*args: Any, **kwargs: Any) -> OrdinaryMutationRoot:
        nonlocal calls
        calls += 1
        if calls == call:
            change_settings(rig.repo)
        return resolve(*args, **kwargs)

    monkeypatch.setattr(rig.service, "_resolve_root", resolving)


@needs_git
async def test_a_bound_copy_is_kept_for_its_chat_idle_merged_or_gone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _rig(tmp_path)
    idle = await rig.create("viki/bot-20261003-aa1111")
    gone = await rig.create("viki/bot-20261003-bb2222")
    _age(Path(idle.path), PAST)
    shutil.rmtree(gone.path)
    for worktree in (idle, gone):
        bind_copy(rig, worktree.worktree_id)
    inspected: list[Path] = []
    monkeypatch.setattr(worktrees, "inspect", lambda git, tree: inspected.append(tree) or Facts())
    kept = {worktree.worktree_id: ("keep", "bot_chat") for worktree in (idle, gone)}

    assert _verdicts(await rig.service.plan()) == kept
    assert _verdicts(await rig.service.sweep()) == kept

    assert inspected == []  # nothing in a bound copy is even read
    _assert_untouched(rig, idle)
    assert rig.state(gone.worktree_id) == "ready" and root_key(gone.path) in rig.listed()
    assert rig.store.copy_states([idle.worktree_id])[idle.worktree_id]["state"] == "bound"


@needs_git
async def test_the_sweep_decides_a_released_copy_once_through_its_release(
    tmp_path: Path,
) -> None:
    """A released copy kept by its release (here: live) is not decided again by the same
    sweep, so no round acts on it twice."""
    live: set[str] = set()
    rig = _rig(tmp_path, in_use=lambda worktree_id: worktree_id in live)
    baseline(rig)  # the CodeGit the server builds, which knows a Bot copy
    worktree = await rig.create()
    _age(Path(worktree.path), PAST)
    bind_copy(rig, worktree.worktree_id, "released")
    live.add(worktree.worktree_id)

    decisions = await rig.service.sweep()

    assert [(d.worktree_id, d.action, d.reason) for d in decisions] == [
        (worktree.worktree_id, "keep", "live")
    ]
    _assert_untouched(rig, worktree)


@needs_git
async def test_retention_reaches_a_folder_whose_rows_are_all_removed(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    removed = str(uuid.uuid4())
    rig.insert_ready(removed, path=rig.area / removed)
    rig.store.update_worktree_state(removed, "ready", "removed", now_iso())
    _dated_archive(rig, removed, NOW - timedelta(days=ARCHIVE_DAYS, hours=1))

    assert await rig.service.sweep() == []

    assert _archive(rig, removed) == ""


@needs_git
@pytest.mark.parametrize("phase", ["lease", "look"])
async def test_settings_changed_under_the_lease_keep_the_row_unknown_and_put_it_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    phase: str,
) -> None:
    """Changed during the lease wait (the act's own fresh check) or before the act's fresh
    look at the tree: the row goes back to `ready`, kept `unknown`, its code logged."""
    rig = _rig(tmp_path)
    worktree = await rig.create()
    (Path(worktree.path) / "a.txt").write_text("edited\n", encoding="utf-8")
    _age(Path(worktree.path), PAST)
    baseline(rig)
    if phase == "lease":
        change_on_resolve(rig, monkeypatch, 2)  # 1: the folder's load, 2: after the lease wait
    else:
        real = rig.git.output
        looks = 0

        def output(root: Path, *args: str, **kwargs: Any) -> str:
            nonlocal looks
            if "--branch" in args:
                looks += 1
                if looks == 2:  # 1: the inspection, 2: the act's fresh look
                    change_settings(rig.repo)
            return real(root, *args, **kwargs)

        monkeypatch.setattr(rig.git, "output", output)
    caplog.set_level(logging.WARNING)

    decisions = await rig.service.sweep()

    assert _verdicts(decisions) == {worktree.worktree_id: ("keep", "unknown")}
    _assert_untouched(rig, worktree)
    assert _archive(rig, worktree.worktree_id) == ""
    assert "sweep skipped a row (git_settings_changed)" in caplog.text
    for secret in (worktree.path, worktree.worktree_id, BRANCH, str(tmp_path)):
        assert secret not in caplog.text


@needs_git
async def test_retention_skips_a_folder_whose_settings_changed_under_its_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    rig = _rig(tmp_path)
    removed = str(uuid.uuid4())
    rig.insert_ready(removed, path=rig.area / removed)
    rig.store.update_worktree_state(removed, "ready", "removed", now_iso())
    _dated_archive(rig, removed, NOW - timedelta(days=ARCHIVE_DAYS, hours=1))
    baseline(rig)
    change_on_resolve(rig, monkeypatch, 2)  # 1: the folder's load, 2: retention's lease
    caplog.set_level(logging.WARNING)

    assert await rig.service.sweep() == []

    assert _archive(rig, removed) != ""
    assert "retention skipped a folder (git_settings_changed)" in caplog.text
