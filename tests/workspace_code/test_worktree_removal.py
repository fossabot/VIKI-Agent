"""The worktree service's remove and launch recovery, against real repositories in `tmp_path`:
removal only of a clean worktree under the lease, the spawn block around it, and the reconciling of
rows a crash left in `creating` or `removing`."""

from __future__ import annotations

import asyncio
import ctypes
import logging
import os
import shutil
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from tests.app.test_workspaces import COMMAND, service_at, store_at
from tests.workspace_code.test_code_git import _commit, _git, _repo
from tests.workspace_code.test_workspaces_code import trusted
from viki_agent.code import worktrees
from viki_agent.code.code_files import CodeFiles
from viki_agent.code.code_git import CodeGit
from viki_agent.code.session_roots import SessionRoots
from viki_agent.code.worktrees import (
    Removal,
    Worktree,
    WorktreeEntry,
    WorktreeError,
    WorktreeService,
    parse_worktree_list,
    worktree_area,
)
from viki_agent.drivers.base import StartError
from viki_agent.session import now_iso
from viki_agent.storage import SessionStore, StateError
from viki_agent.vaults import vault_mutation
from viki_agent.vaults.vault_mutation import MutationCoordinator, OrdinaryMutationRoot
from viki_agent.vaults.workspace_mutation import WorkspaceMutationResolver
from viki_agent.workspaces import WorkspaceService, root_key

needs_windows_git = pytest.mark.skipif(
    shutil.which("git") is None or sys.platform != "win32",
    reason="git is not available, or not Windows (8.3 short names exist only on NTFS)",
)
needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not available")
BRANCH = "viki/20261003-ab12"
SESSION = "33333333-3333-4333-8333-333333333333"
OID = "a" * 40
HANG_GUARD_S = 5.0  # a guard on events that fire in milliseconds, never a bound on real work


@pytest.fixture(autouse=True)
def _empty_git_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No developer `GIT_*` variable (each turns the gate into `git_unavailable`), and an empty
    home, so the user's global Git config is not read."""
    for name in list(os.environ):
        if name.upper().startswith("GIT_"):
            monkeypatch.delenv(name)
    home = tmp_path / "git-home"
    home.mkdir()
    for name in ("HOME", "USERPROFILE", "XDG_CONFIG_HOME"):
        monkeypatch.setenv(name, str(home))


@dataclass
class FakeActor:
    live: bool


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
    actors: dict[str, FakeActor] = field(default_factory=dict)

    async def create(self, branch: str = BRANCH) -> Worktree:
        workspace = self.workspaces.get(self.workspace_id, kind="code")
        return await self.service.create(
            self.workspace_id, workspace.policy_revision, workspace.root.physical_id, branch
        )

    async def remove(self, worktree_id: str) -> Removal:
        workspace = self.workspaces.get(self.workspace_id, kind="code")
        return await self.service.remove(
            worktree_id, workspace.policy_revision, workspace.root.physical_id
        )

    async def refusal(self, worktree_id: str) -> str:
        with pytest.raises(WorktreeError) as refused:
            await self.remove(worktree_id)
        return refused.value.code

    def root(self) -> OrdinaryMutationRoot:
        workspace = self.workspaces.get(self.workspace_id, kind="code")
        return self.resolver(
            self.workspace_id, workspace.policy_revision, workspace.root.physical_id, kind="code"
        )

    def state(self, worktree_id: str) -> str | None:
        row = self.store.get_worktree(worktree_id)
        return row["state"] if row is not None else None

    def force(self, worktree_id: str, state: str) -> None:
        self.store.update_worktree_state(
            worktree_id, self.state(worktree_id) or "", state, now_iso()
        )

    def insert(self, worktree_id: str, state: str, *, folder: bool) -> Path:
        """A row for a folder Git never made (or none at all), as a crash may leave one."""
        path = self.area / worktree_id
        if folder:
            path.mkdir(parents=True)
        stamp = now_iso()
        self.store.insert_worktree(
            {
                "worktree_id": worktree_id,
                "workspace_id": self.workspace_id,
                "path": str(path),
                "branch": f"held-{worktree_id[:8]}",
                "base_oid": OID,
                "state": "creating",
                "created_at": stamp,
                "updated_at": stamp,
            },
            limit=worktrees.MAX_WORKTREES,
        )
        if state != "creating":
            self.force(worktree_id, state)
        return path

    def session_in(self, worktree: Worktree) -> None:
        self.store.put_session_root(
            {
                "session_id": SESSION,
                "workspace_id": self.workspace_id,
                "worktree_id": worktree.worktree_id,
                "display_root": worktree.path,
                "created_at": now_iso(),
            }
        )

    def listed(self) -> set[str]:
        raw = _git(self.repo, "worktree", "list", "--porcelain", "-z")
        return {
            root_key(part[len("worktree ") :])
            for part in raw.split("\0")
            if part.startswith("worktree ")
        }


def _rig(tmp_path: Path, *, trust: bool = True, data: Path | None = None) -> Rig:
    repo = _repo(tmp_path / "repo")
    (repo / "a.txt").write_text("alpha\n", encoding="utf-8")
    (repo / ".gitignore").write_text("build/\n", encoding="utf-8")
    _commit(repo, "first")
    store = store_at(tmp_path)
    # Git available, so the registered identity holds the Git common dir the lease needs.
    workspaces = service_at(tmp_path, store, git=True)
    if trust:
        workspace_id = trusted(workspaces, repo)
    else:
        workspace_id = workspaces.register_existing(COMMAND, str(repo), kind="code")[0].workspace_id
    git = CodeGit(workspaces, CodeFiles(workspaces), git_available=lambda: True)
    coordinator = MutationCoordinator()
    resolver = WorkspaceMutationResolver(workspaces, git_available=lambda: True)
    actors: dict[str, FakeActor] = {}
    roots = SessionRoots(store, workspaces, exists=lambda _: False, actor=actors.get)
    area = worktree_area(data or tmp_path / "data")
    service = WorktreeService(store, workspaces, git, coordinator, resolver, roots, area=area)
    return Rig(
        store, workspaces, git, coordinator, resolver, roots, service, workspace_id, repo, area,
        actors,
    )  # fmt: skip


def _short_path(path: Path) -> str:
    """The 8.3 form of an existing folder, or the folder itself when the volume keeps none."""
    assert sys.platform == "win32"
    kernel32 = ctypes.windll.kernel32
    size = kernel32.GetShortPathNameW(str(path), None, 0)
    if size == 0:
        return str(path)
    buffer = ctypes.create_unicode_buffer(size)
    kernel32.GetShortPathNameW(str(path), buffer, size)
    return buffer.value


def _short_data_rig(tmp_path: Path) -> Rig:
    """A rig whose data folder is reached through an 8.3 ancestor, as `VIKI_DATA_DIR` may name
    it. The short name sits on an ancestor because the worktree area is a sibling of the data
    folder, so a short name on the data folder itself would not reach the area."""
    parent = tmp_path / "longparentdir"
    parent.mkdir()
    short = Path(_short_path(parent))
    if str(short) == str(parent):
        pytest.skip("this volume keeps no 8.3 names")
    return _rig(tmp_path, data=short / "data")


# --- remove -----------------------------------------------------------------------------------


@needs_git
async def test_a_clean_worktree_is_removed_and_its_branch_stays(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    worktree = await rig.create()

    removal = await rig.remove(worktree.worktree_id)

    assert (removal.outcome, removal.reason, removal.changes) == ("removed", None, None)
    assert removal.worktree.state == "removed"
    assert removal.worktree.worktree_id == worktree.worktree_id
    assert not Path(worktree.path).exists()
    assert BRANCH in _git(rig.repo, "branch", "--list", BRANCH)
    assert rig.state(worktree.worktree_id) == "removed"
    assert root_key(worktree.path) not in rig.listed()


@needs_git
async def test_remove_holds_the_git_common_dir_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _rig(tmp_path)
    worktree = await rig.create()
    real = rig.coordinator.lease
    asked: list[dict[str, Any]] = []

    def spy(*args: Any, **kwargs: Any) -> Any:
        asked.append(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(rig.coordinator, "lease", spy)

    await rig.remove(worktree.worktree_id)

    assert [kwargs["include_git_common"] for kwargs in asked] == [True]


@needs_git
async def test_a_dirty_worktree_is_kept_with_its_change_count(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    worktree = await rig.create()
    (Path(worktree.path) / "new.txt").write_text("new\n", encoding="utf-8")

    removal = await rig.remove(worktree.worktree_id)

    assert (removal.outcome, removal.reason, removal.changes) == ("kept", "dirty", 1)
    assert removal.worktree.state == "ready"
    assert (Path(worktree.path) / "new.txt").read_text(encoding="utf-8") == "new\n"
    assert rig.state(worktree.worktree_id) == "ready"
    assert root_key(worktree.path) in rig.listed()


@needs_git
async def test_a_modified_tracked_file_is_dirty(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    worktree = await rig.create()
    (Path(worktree.path) / "a.txt").write_text("edited\n", encoding="utf-8")

    removal = await rig.remove(worktree.worktree_id)

    assert (removal.outcome, removal.reason, removal.changes) == ("kept", "dirty", 1)
    assert (Path(worktree.path) / "a.txt").read_text(encoding="utf-8") == "edited\n"
    assert rig.state(worktree.worktree_id) == "ready"


@needs_git
async def test_only_ignored_files_are_removed_with_the_folder(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    worktree = await rig.create()
    (Path(worktree.path) / "build").mkdir()
    (Path(worktree.path) / "build" / "out.o").write_text("object\n", encoding="utf-8")

    removal = await rig.remove(worktree.worktree_id)

    assert removal.outcome == "removed"
    assert not Path(worktree.path).exists()


@needs_git
async def test_a_locked_worktree_is_kept_as_git_refused(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    rig = _rig(tmp_path)
    worktree = await rig.create()
    _git(rig.repo, "worktree", "lock", worktree.path)
    caplog.set_level(logging.WARNING)

    removal = await rig.remove(worktree.worktree_id)

    assert (removal.outcome, removal.reason, removal.changes) == ("kept", "git_refused", None)
    assert removal.worktree.state == "kept"
    assert rig.state(worktree.worktree_id) == "kept"
    assert (Path(worktree.path) / "a.txt").exists()
    assert "worktree remove" in caplog.text
    for secret in (worktree.path, worktree.worktree_id, BRANCH, str(tmp_path)):
        assert secret not in caplog.text


@needs_git
async def test_a_folder_deleted_by_hand_is_removed(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    worktree = await rig.create()
    shutil.rmtree(worktree.path)

    removal = await rig.remove(worktree.worktree_id)

    assert removal.outcome == "removed"
    assert rig.state(worktree.worktree_id) == "removed"
    assert root_key(worktree.path) not in rig.listed()


def _spy_git(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, ...]]:
    calls: list[tuple[str, ...]] = []
    real = rig.git.git

    def spy(root: Path, *args: str, timeout: float = 5.0) -> str:
        if args[:1] == ("worktree",):
            calls.append(args[:2])
        return real(root, *args, timeout=timeout)

    monkeypatch.setattr(rig.git, "git", spy)
    return calls


@needs_git
async def test_a_folder_deleted_by_hand_still_listed_goes_through_git_remove(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _rig(tmp_path)
    worktree = await rig.create()
    shutil.rmtree(worktree.path)
    calls = _spy_git(rig, monkeypatch)

    removal = await rig.remove(worktree.worktree_id)

    assert removal.outcome == "removed" and rig.state(worktree.worktree_id) == "removed"
    assert calls == [("worktree", "list"), ("worktree", "remove")]


@needs_windows_git
async def test_a_short_form_path_is_matched_to_the_listing_by_its_real_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _short_data_rig(tmp_path)
    worktree = await rig.create()
    long_key = root_key(os.path.realpath(worktree.path))
    assert long_key in rig.listed()
    assert root_key(worktree.path) != long_key  # the row keeps the short form
    shutil.rmtree(worktree.path)
    calls = _spy_git(rig, monkeypatch)

    removal = await rig.remove(worktree.worktree_id)

    assert removal.outcome == "removed" and rig.state(worktree.worktree_id) == "removed"
    assert calls == [("worktree", "list"), ("worktree", "remove")]
    assert long_key not in rig.listed()


@needs_git
async def test_a_path_that_cannot_be_resolved_counts_as_listed_in_a_removal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    rig = _rig(tmp_path)
    worktree = await rig.create()
    key = root_key(os.path.realpath(worktree.path))
    shutil.rmtree(worktree.path)

    def unresolved(path: str) -> str:
        raise OSError(64, "network name deleted")

    monkeypatch.setattr(worktrees, "_listing_key", unresolved)
    calls = _spy_git(rig, monkeypatch)
    caplog.set_level(logging.WARNING)

    removal = await rig.remove(worktree.worktree_id)

    assert removal.outcome == "removed" and rig.state(worktree.worktree_id) == "removed"
    assert calls == [("worktree", "list"), ("worktree", "remove")]
    assert key not in rig.listed()
    assert "path not resolved (OSError)" in caplog.text
    for secret in (worktree.path, worktree.worktree_id, str(tmp_path)):
        assert secret not in caplog.text


@needs_git
async def test_a_pruned_worktree_without_a_folder_removes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _rig(tmp_path)
    worktree = await rig.create()
    shutil.rmtree(worktree.path)
    _git(rig.repo, "worktree", "prune")
    assert root_key(worktree.path) not in rig.listed()
    calls = _spy_git(rig, monkeypatch)

    removal = await rig.remove(worktree.worktree_id)

    assert (removal.outcome, removal.reason) == ("removed", None)
    assert rig.state(worktree.worktree_id) == "removed"
    assert ("worktree", "remove") not in calls
    assert BRANCH in _git(rig.repo, "branch", "--list", BRANCH)


@needs_git
@pytest.mark.parametrize(
    "answer",
    [f"worktree C:/x\0HEAD {OID}\0frobnicate\0\0", RuntimeError("git worktree failed")],
    ids=["unknown-shape", "git-failed"],
)
async def test_an_unknown_listing_falls_back_to_git_remove(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, answer: str | Exception
) -> None:
    rig = _rig(tmp_path)
    worktree = await rig.create()
    shutil.rmtree(worktree.path)
    calls: list[tuple[str, ...]] = []
    real = rig.git.git

    def listing(root: Path, *args: str, timeout: float = 5.0) -> str:
        if args[:1] == ("worktree",):
            calls.append(args[:2])
        if args[:2] == ("worktree", "list"):
            if isinstance(answer, Exception):
                raise answer
            return answer
        return real(root, *args, timeout=timeout)

    monkeypatch.setattr(rig.git, "git", listing)
    removal = await rig.remove(worktree.worktree_id)

    assert removal.outcome == "removed" and rig.state(worktree.worktree_id) == "removed"
    assert calls == [("worktree", "list"), ("worktree", "remove")]
    assert root_key(worktree.path) not in rig.listed()


@needs_git
async def test_a_kept_worktree_can_be_removed_again(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    worktree = await rig.create()
    rig.force(worktree.worktree_id, "kept")

    removal = await rig.remove(worktree.worktree_id)

    assert removal.outcome == "removed" and rig.state(worktree.worktree_id) == "removed"


@needs_git
async def test_an_unreadable_status_keeps_the_worktree_as_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _rig(tmp_path)
    worktree = await rig.create()
    real = rig.git.git

    def odd_status(root: Path, *args: str, timeout: float = 5.0) -> str:
        if args[:1] == ("status",):
            return "! unknown\0"
        return real(root, *args, timeout=timeout)

    monkeypatch.setattr(rig.git, "git", odd_status)
    removal = await rig.remove(worktree.worktree_id)

    assert (removal.outcome, removal.reason, removal.changes) == ("kept", "unknown_state", None)
    assert rig.state(worktree.worktree_id) == "kept"
    assert Path(worktree.path).is_dir()


@needs_git
async def test_a_live_session_in_the_worktree_refuses_and_releases_the_block(
    tmp_path: Path,
) -> None:
    rig = _rig(tmp_path)
    worktree = await rig.create()
    rig.session_in(worktree)
    rig.actors[SESSION] = FakeActor(live=True)

    assert await rig.refusal(worktree.worktree_id) == "worktree_in_use"

    assert rig.state(worktree.worktree_id) == "ready"
    assert Path(worktree.path).is_dir()
    assert await rig.roots.resolve(SESSION) == Path(worktree.path)


@needs_git
async def test_a_spawn_is_refused_while_remove_waits_on_the_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _rig(tmp_path)
    worktree = await rig.create()
    rig.session_in(worktree)
    waiting = asyncio.Event()
    real = rig.coordinator.lease

    @asynccontextmanager
    async def announcing(*args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        waiting.set()
        async with real(*args, **kwargs) as lease:
            yield lease

    async with rig.coordinator.lease(rig.root(), ()):
        monkeypatch.setattr(rig.coordinator, "lease", announcing)
        removing = asyncio.create_task(rig.remove(worktree.worktree_id))
        await asyncio.wait_for(waiting.wait(), HANG_GUARD_S)
        with pytest.raises(StartError):
            await rig.roots.resolve(SESSION)
        assert rig.state(worktree.worktree_id) == "removing"

    removal = await asyncio.wait_for(removing, HANG_GUARD_S)
    assert removal.outcome == "removed"
    with pytest.raises(StartError):
        await rig.roots.resolve(SESSION)


@needs_git
async def test_a_lease_refusal_puts_the_state_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _rig(tmp_path)
    worktree = await rig.create()
    rig.session_in(worktree)
    monkeypatch.setattr(worktrees, "LEASE_WAIT_S", 0.05)

    async with rig.coordinator.lease(rig.root(), ()):
        assert await rig.refusal(worktree.worktree_id) == "vault_locked"

    assert rig.state(worktree.worktree_id) == "ready"
    assert Path(worktree.path).is_dir()
    assert await rig.roots.resolve(SESSION) == Path(worktree.path)


@needs_git
async def test_a_second_concurrent_remove_is_busy_and_keeps_the_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _rig(tmp_path)
    worktree = await rig.create()
    rig.session_in(worktree)
    entered, release = asyncio.Event(), asyncio.Event()
    real = rig.roots.live_in

    async def held_live_in(worktree_id: str) -> bool:
        if not entered.is_set():
            entered.set()
            await release.wait()
        return await real(worktree_id)

    monkeypatch.setattr(rig.roots, "live_in", held_live_in)
    first = asyncio.create_task(rig.remove(worktree.worktree_id))
    await asyncio.wait_for(entered.wait(), HANG_GUARD_S)

    assert await rig.refusal(worktree.worktree_id) == "worktree_busy"
    # Still `ready`, so only the first removal's block refuses this spawn.
    assert rig.state(worktree.worktree_id) == "ready"
    with pytest.raises(StartError):
        await rig.roots.resolve(SESSION)

    release.set()
    assert (await asyncio.wait_for(first, HANG_GUARD_S)).outcome == "removed"


@needs_git
@pytest.mark.parametrize("state", ["creating", "removing", "removed"])
async def test_a_row_not_ready_or_kept_is_refused(tmp_path: Path, state: str) -> None:
    rig = _rig(tmp_path)
    worktree = await rig.create()
    rig.force(worktree.worktree_id, state)

    assert await rig.refusal(worktree.worktree_id) == "worktree_not_ready"
    assert rig.state(worktree.worktree_id) == state and Path(worktree.path).is_dir()


@needs_git
async def test_an_unknown_worktree_is_refused(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    assert await rig.refusal("00000000-0000-4000-8000-000000000000") == "unknown_worktree"


@needs_git
async def test_remove_refuses_off_windows_before_any_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same drive refusal as create's, met after the row is already marked `removing`: the
    put-back on the way out restores it to `ready`, and the folder and its Git registration are
    never touched."""
    rig = _rig(tmp_path)
    worktree = await rig.create()
    monkeypatch.setattr(vault_mutation, "_supported_volume", lambda _path: False)

    assert await rig.refusal(worktree.worktree_id) == "unsupported_filesystem"

    assert rig.state(worktree.worktree_id) == "ready"
    assert Path(worktree.path).is_dir()
    assert root_key(worktree.path) in rig.listed()


# --- recover ----------------------------------------------------------------------------------


@needs_git
async def test_recover_reconciles_each_crashed_row_and_deletes_nothing(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    creating_listed = await rig.create("c-listed")
    creating_locked = await rig.create("c-locked")
    removing_listed = await rig.create("r-listed")
    removing_gone = await rig.create("r-gone")
    ready = await rig.create("ready")
    _git(rig.repo, "worktree", "lock", creating_locked.path)
    _git(rig.repo, "worktree", "remove", removing_gone.path)
    for worktree in (creating_listed, creating_locked):
        rig.force(worktree.worktree_id, "creating")
    for worktree in (removing_listed, removing_gone):
        rig.force(worktree.worktree_id, "removing")
    creating_folder = rig.insert("00000000-0000-4000-8000-000000000001", "creating", folder=True)
    rig.insert("00000000-0000-4000-8000-000000000002", "creating", folder=False)
    removing_folder = rig.insert("00000000-0000-4000-8000-000000000003", "removing", folder=True)
    before = sorted(rig.area.iterdir())

    await rig.service.recover()

    assert rig.state(creating_listed.worktree_id) == "ready"
    assert rig.state(creating_locked.worktree_id) == "kept"
    assert rig.state("00000000-0000-4000-8000-000000000001") == "kept"
    assert rig.state("00000000-0000-4000-8000-000000000002") is None
    assert rig.state(removing_listed.worktree_id) == "kept"
    assert rig.state(removing_gone.worktree_id) == "removed"
    assert rig.state("00000000-0000-4000-8000-000000000003") == "kept"
    assert rig.state(ready.worktree_id) == "ready"
    assert sorted(rig.area.iterdir()) == before
    assert creating_folder in before and removing_folder in before
    assert BRANCH not in _git(rig.repo, "branch")  # nothing made
    for name in ("c-listed", "c-locked", "r-listed", "r-gone", "ready"):
        assert name in _git(rig.repo, "branch", "--list", name)


@needs_windows_git
async def test_recover_keeps_a_listed_short_form_row_and_a_later_remove_unregisters_it(
    tmp_path: Path,
) -> None:
    rig = _short_data_rig(tmp_path)
    worktree = await rig.create()
    long_key = root_key(os.path.realpath(worktree.path))
    assert root_key(worktree.path) != long_key
    shutil.rmtree(worktree.path)
    rig.force(worktree.worktree_id, "removing")

    await rig.service.recover()

    assert rig.state(worktree.worktree_id) == "kept"  # Git still lists it
    assert long_key in rig.listed()
    assert (await rig.remove(worktree.worktree_id)).outcome == "removed"
    assert long_key not in rig.listed()


@needs_git
async def test_recover_continues_past_a_row_whose_path_cannot_be_resolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    rig = _rig(tmp_path)
    await rig.create("one")
    await rig.create("two")
    rows, _ = rig.store.list_worktrees(10)
    first, second = rows[0], rows[1]  # the order `_recover` walks them in
    for row in (first, second):
        rig.force(row["worktree_id"], "removing")
    shutil.rmtree(first["path"])
    real = worktrees._listing_key

    def unresolved_for_first(path: str) -> str:
        if path == first["path"]:
            raise OSError(64, "network name deleted")
        return real(path)

    monkeypatch.setattr(worktrees, "_listing_key", unresolved_for_first)
    caplog.set_level(logging.WARNING)

    await rig.service.recover()

    assert rig.state(first["worktree_id"]) == "removing"
    assert rig.state(second["worktree_id"]) == "kept"
    assert "recovery skipped a row (OSError)" in caplog.text
    for secret in (first["path"], first["worktree_id"], str(tmp_path)):
        assert secret not in caplog.text


@needs_windows_git
async def test_recover_readies_a_listed_short_form_creating_row(tmp_path: Path) -> None:
    rig = _short_data_rig(tmp_path)
    worktree = await rig.create()
    assert root_key(worktree.path) != root_key(os.path.realpath(worktree.path))
    rig.force(worktree.worktree_id, "creating")

    await rig.service.recover()

    assert rig.state(worktree.worktree_id) == "ready"


@needs_git
@pytest.mark.parametrize(
    "answer",
    [
        f"worktree C:/x\0HEAD {OID}\0frobnicate\0\0",
        f"worktree C:/x\0HEAD {OID}\0",
        f"HEAD {OID}\0\0",
        RuntimeError("git worktree failed"),
    ],
    ids=["unknown-field", "unterminated", "no-worktree", "git-failed"],
)
async def test_an_unknown_listing_leaves_the_rows_as_they_are(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, answer: str | Exception
) -> None:
    rig = _rig(tmp_path)
    rig.insert("00000000-0000-4000-8000-000000000001", "creating", folder=True)
    rig.insert("00000000-0000-4000-8000-000000000002", "creating", folder=False)
    rig.insert("00000000-0000-4000-8000-000000000003", "removing", folder=False)
    real = rig.git.git

    def listing(root: Path, *args: str, timeout: float = 5.0) -> str:
        if args[:2] == ("worktree", "list"):
            if isinstance(answer, Exception):
                raise answer
            return answer
        return real(root, *args, timeout=timeout)

    monkeypatch.setattr(rig.git, "git", listing)
    await rig.service.recover()

    assert rig.state("00000000-0000-4000-8000-000000000001") == "creating"
    assert rig.state("00000000-0000-4000-8000-000000000002") == "creating"
    assert rig.state("00000000-0000-4000-8000-000000000003") == "removing"


@needs_git
async def test_recover_leaves_the_rows_of_an_untrusted_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _rig(tmp_path, trust=False)
    rig.insert("00000000-0000-4000-8000-000000000002", "creating", folder=False)
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(rig.git, "git", lambda root, *args, **kw: calls.append(args) or "")

    await rig.service.recover()

    assert rig.state("00000000-0000-4000-8000-000000000002") == "creating"
    assert calls == []


@needs_git
async def test_recover_never_raises_when_the_store_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    rig = _rig(tmp_path)

    def failing(states: Any) -> Any:
        raise StateError("unavailable")

    monkeypatch.setattr(rig.store, "worktrees_in_states", failing)
    caplog.set_level(logging.WARNING)
    await rig.service.recover()
    assert "StateError" in caplog.text


# --- parse_worktree_list ----------------------------------------------------------------------


def test_parse_worktree_list_reads_the_spike_shapes() -> None:
    raw = (
        f"worktree C:/repo\0HEAD {OID}\0branch refs/heads/main\0\0"
        f"worktree C:/area/plain\0HEAD {OID}\0branch refs/heads/viki/x\0\0"
        f"worktree C:/area/locked\0HEAD {OID}\0branch refs/heads/l\0locked\0\0"
        f"worktree C:/area/reason\0HEAD {OID}\0detached\0locked held for a reason\0\0"
        f"worktree C:/area/gone\0HEAD {OID}\0branch refs/heads/g\0"
        "prunable gitdir file points to non-existent location\0\0"
        f"worktree C:/area/detached\0HEAD {OID}\0detached\0\0"
        "worktree C:/bare\0bare\0\0"
    )
    entries = parse_worktree_list(raw)
    entry = WorktreeEntry
    assert entries == {
        root_key("C:/repo"): entry("C:/repo", False, False, "refs/heads/main", False),
        root_key("C:/area/plain"): entry("C:/area/plain", False, False, "refs/heads/viki/x", False),
        root_key("C:/area/locked"): entry("C:/area/locked", True, False, "refs/heads/l", False),
        root_key("C:/area/reason"): entry("C:/area/reason", True, False, None, True),
        root_key("C:/area/gone"): entry("C:/area/gone", False, True, "refs/heads/g", False),
        root_key("C:/area/detached"): entry("C:/area/detached", False, False, None, True),
        root_key("C:/bare"): entry("C:/bare", False, False, None, False),
    }


@pytest.mark.parametrize(
    "raw",
    [
        "",
        f"worktree C:/x\0HEAD {OID}\0",  # the record never ends
        f"worktree C:/x\0HEAD {OID}",  # unterminated field
        f"worktree C:/x\0HEAD {OID}\0frobnicate\0\0",  # unknown field
        f"HEAD {OID}\0worktree C:/x\0\0",  # does not start with worktree
        f"worktree C:/x\0\0HEAD {OID}\0\0",  # a record without worktree
        "worktree C:/x\0\0\0",  # an empty record
        "worktree \0\0",  # no path
        "worktree C:/x\0HEAD nothex\0\0",
        "worktree C:/x\0branch\0\0",
        "worktree C:/x\0worktree C:/y\0\0",
    ],
)
def test_parse_worktree_list_refuses_an_unknown_shape(raw: str) -> None:
    with pytest.raises(ValueError):
        parse_worktree_list(raw)
