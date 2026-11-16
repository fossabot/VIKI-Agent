"""The worktree in-use guard: a removal refuses a worktree that a check or terminal holds, and the
removal's block is visible through `SessionRoots.blocked` while it holds the id, against real
repositories under a short folder (Git breaks in deep Windows paths)."""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import uuid
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.workspace_code.test_code_git import _git
from tests.workspace_code.test_worktree_removal import HANG_GUARD_S, Rig, _rig, needs_git
from viki_agent import winproc
from viki_agent.code import worktrees
from viki_agent.code.worktrees import WorktreeService

BRANCH = "viki/20261004-e05a"


@pytest.fixture(autouse=True)
def _empty_git_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No developer `GIT_*` variable and an empty home, as in `test_worktree_removal`."""
    for name in list(os.environ):
        if name.upper().startswith("GIT_"):
            monkeypatch.delenv(name)
    home = tmp_path / "git-home"
    home.mkdir()
    for name in ("HOME", "USERPROFILE", "XDG_CONFIG_HOME"):
        monkeypatch.setenv(name, str(home))


def _short_rig(tmp_path: Path) -> Rig:
    return _rig(Path(tempfile.mkdtemp(dir=tmp_path.parent, prefix="c")))


def _worktree(rig: Rig) -> tuple[str, Path]:
    """A real `git worktree add` of the folder and its `ready` row."""
    worktree_id = str(uuid.uuid4())
    path = rig.insert(worktree_id, "ready", folder=False)
    _git(rig.repo, "worktree", "add", "-b", BRANCH, str(path), "HEAD")
    return worktree_id, path


def _with_in_use(rig: Rig, in_use: Callable[[str], bool]) -> None:
    rig.service = WorktreeService(
        rig.store, rig.workspaces, rig.git, rig.coordinator, rig.resolver, rig.roots,
        area=rig.area, in_use=in_use,
    )  # fmt: skip


@needs_git
async def test_removal_refuses_a_worktree_in_use(tmp_path: Path) -> None:
    rig = _short_rig(tmp_path)
    worktree_id, path = _worktree(rig)
    asked: list[str] = []

    def in_use(asked_id: str) -> bool:
        asked.append(asked_id)
        return True

    _with_in_use(rig, in_use)

    assert await rig.refusal(worktree_id) == "worktree_in_use"
    assert asked == [worktree_id]
    assert rig.state(worktree_id) == "ready"
    assert path.is_dir()
    assert not rig.roots.blocked(worktree_id)


@needs_git
async def test_blocked_holds_only_while_a_removal_holds_the_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _short_rig(tmp_path)
    worktree_id, path = _worktree(rig)
    blocked_when_asked: list[bool] = []

    def in_use(asked_id: str) -> bool:
        blocked_when_asked.append(rig.roots.blocked(asked_id))
        return False

    _with_in_use(rig, in_use)
    entered, release = asyncio.Event(), asyncio.Event()
    real = rig.roots.live_in

    async def held_live_in(asked_id: str) -> bool:
        entered.set()
        await release.wait()
        return await real(asked_id)

    monkeypatch.setattr(rig.roots, "live_in", held_live_in)
    assert not rig.roots.blocked(worktree_id)

    removing = asyncio.create_task(rig.remove(worktree_id))
    await asyncio.wait_for(entered.wait(), HANG_GUARD_S)
    assert rig.roots.blocked(worktree_id)
    assert not rig.roots.blocked(str(uuid.uuid4()))

    release.set()
    removal = await asyncio.wait_for(removing, HANG_GUARD_S)
    assert removal.outcome == "removed"
    assert blocked_when_asked == [True]
    assert not rig.roots.blocked(worktree_id)
    assert not path.exists()


@needs_git
async def test_the_default_in_use_keeps_todays_removal(tmp_path: Path) -> None:
    rig = _short_rig(tmp_path)
    worktree_id, path = _worktree(rig)

    removal = await rig.remove(worktree_id)

    assert (removal.outcome, removal.reason) == ("removed", None)
    assert rig.state(worktree_id) == "removed"
    assert not path.exists()


@needs_git
@pytest.mark.parametrize("state", ["ready", "kept"])
async def test_a_posix_process_in_the_worktree_refuses_before_state_or_git_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    rig = _short_rig(tmp_path)
    worktree_id, path = _worktree(rig)
    if state == "kept":
        rig.force(worktree_id, "kept")
    before = rig.store.get_worktree(worktree_id)
    inspected: list[Path] = []
    monkeypatch.setattr(worktrees, "sys", SimpleNamespace(platform="linux"))

    def inside(candidate: Path) -> bool:
        inspected.append(candidate)
        assert rig.store.get_worktree(worktree_id) == before
        return True

    monkeypatch.setattr(worktrees, "has_process_inside", inside)
    real_git = rig.git.git
    writes: list[tuple[str, ...]] = []

    def git(cwd: Path, *args: str, timeout: float = 5.0, index: Path | None = None) -> str:
        if args[:2] == ("worktree", "remove"):
            writes.append(args)
        return real_git(cwd, *args, timeout=timeout, index=index)

    monkeypatch.setattr(rig.git, "git", git)
    assert await rig.refusal(worktree_id) == "worktree_in_use"
    assert inspected == [path] and writes == []
    assert rig.store.get_worktree(worktree_id) == before
    assert path.is_dir() and not rig.roots.blocked(worktree_id)


@needs_git
async def test_a_posix_process_entering_during_the_lease_wait_keeps_the_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _short_rig(tmp_path)
    worktree_id, path = _worktree(rig)
    monkeypatch.setattr(worktrees, "sys", SimpleNamespace(platform="linux"))
    inspected: list[Path] = []

    def inside(candidate: Path) -> bool:
        inspected.append(candidate)
        return len(inspected) == 2

    monkeypatch.setattr(worktrees, "has_process_inside", inside)
    assert await rig.refusal(worktree_id) == "worktree_in_use"
    assert inspected == [path, path]
    assert rig.state(worktree_id) == "ready" and path.is_dir()
    assert not rig.roots.blocked(worktree_id)


@needs_git
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX open-directory removal")
async def test_a_real_posix_process_keeps_its_worktree_until_it_exits(tmp_path: Path) -> None:
    rig = _short_rig(tmp_path)
    worktree_id, path = _worktree(rig)
    child = await winproc.spawn(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        cwd=path,
        env=winproc.scrub_env(os.environ),
    )
    try:
        assert await rig.refusal(worktree_id) == "worktree_in_use"
        assert rig.state(worktree_id) == "ready" and path.is_dir()
    finally:
        child.kill_tree()
        await asyncio.wait_for(child.process.wait(), HANG_GUARD_S)
        child.release()
    assert (await rig.remove(worktree_id)).outcome == "removed"
