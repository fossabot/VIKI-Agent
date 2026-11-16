"""The worktree service's create: the area, the checks in order, and the Git add under the
whole-root lease, with hooks on or off; then the Git-settings guard at every phase and the
state-selected reads, all against real repositories in `tmp_path`."""

from __future__ import annotations

import logging
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from tests.app.test_workspaces import COMMAND, service_at, store_at
from tests.workspace_code.test_code_git import _commit, _git, _repo
from tests.workspace_code.test_workspaces_code import history, trusted
from viki_agent.code import worktree_archive, worktrees
from viki_agent.code.code_files import CodeFiles
from viki_agent.code.code_git import CodeGit
from viki_agent.code.session_roots import SessionRoots
from viki_agent.code.worktree_rules import Decision
from viki_agent.code.worktrees import (
    MAX_WORKTREES,
    Worktree,
    WorktreeError,
    WorktreeService,
    valid_branch,
    worktree_area,
)
from viki_agent.protection import require_private_directory
from viki_agent.storage import SessionStore
from viki_agent.vaults import vault_mutation
from viki_agent.vaults.vault_mutation import MutationCoordinator, OrdinaryMutationRoot
from viki_agent.vaults.workspace_mutation import WorkspaceMutationResolver
from viki_agent.workspaces import WorkspaceService

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not available")
BRANCH = "viki/20261003-ab12"
BAD_BRANCHES = ["-x", "a..b", "a/", ".a", "a.lock", "HEAD", "a" * 101, "a b", "ä"]


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
class Rig:
    store: SessionStore
    workspaces: WorkspaceService
    git: CodeGit
    coordinator: MutationCoordinator
    resolver: WorkspaceMutationResolver
    service: WorktreeService
    workspace_id: str
    area: Path

    async def create(self, branch: str = BRANCH, *, hooks: bool = True) -> Worktree:
        workspace = self.workspaces.get(self.workspace_id, kind="code")
        return await self.service.create(
            self.workspace_id,
            workspace.policy_revision,
            workspace.root.physical_id,
            branch,
            hooks=hooks,
        )

    async def refusal(self, branch: str = BRANCH) -> str:
        with pytest.raises(WorktreeError) as refused:
            await self.create(branch)
        return refused.value.code

    def root(self) -> OrdinaryMutationRoot:
        workspace = self.workspaces.get(self.workspace_id, kind="code")
        return self.resolver(
            self.workspace_id, workspace.policy_revision, workspace.root.physical_id, kind="code"
        )

    def rows(self) -> list[Worktree]:
        return self.service.list()[0]

    def folders(self) -> list[Path]:
        return sorted(self.area.iterdir()) if self.area.exists() else []


def _rig(tmp_path: Path, root: Path, *, trust: bool = True) -> Rig:
    store = store_at(tmp_path)
    # Git available, so the registered identity holds the Git common dir the lease needs.
    workspaces = service_at(tmp_path, store, git=True)
    if trust:
        workspace_id = trusted(workspaces, root)
    else:
        workspace_id = workspaces.register_existing(COMMAND, str(root), kind="code")[0].workspace_id
    git = CodeGit(workspaces, CodeFiles(workspaces), git_available=lambda: True)
    coordinator = MutationCoordinator()
    resolver = WorkspaceMutationResolver(workspaces, git_available=lambda: True)
    roots = SessionRoots(store, workspaces, exists=lambda _: False, actor=lambda _: None)
    area = worktree_area(tmp_path / "data")
    service = WorktreeService(store, workspaces, git, coordinator, resolver, roots, area=area)
    return Rig(store, workspaces, git, coordinator, resolver, service, workspace_id, area)


def _committed(tmp_path: Path) -> Path:
    root = _repo(tmp_path / "repo")
    (root / "a.txt").write_text("alpha\n", encoding="utf-8")
    (root / "dir").mkdir()
    (root / "dir" / "b.txt").write_text("beta\n", encoding="utf-8")
    _commit(root, "first")
    return root


# --- create -----------------------------------------------------------------------------------


@needs_git
async def test_create_makes_a_ready_worktree_at_the_pinned_base(tmp_path: Path) -> None:
    root = _committed(tmp_path)
    (root / "a.txt").write_text("edited\n", encoding="utf-8")
    (root / "untracked.txt").write_text("new\n", encoding="utf-8")
    status = _git(root, "status", "--porcelain")
    head = _git(root, "rev-parse", "HEAD").strip()
    rig = _rig(tmp_path, root)

    worktree = await rig.create()

    assert worktree.state == "ready"
    assert (worktree.workspace_id, worktree.branch, worktree.base_oid) == (
        rig.workspace_id,
        BRANCH,
        head,
    )
    folder = Path(worktree.path)
    assert folder == rig.area / worktree.worktree_id
    assert (folder / "a.txt").read_text(encoding="utf-8") == "alpha\n"
    assert (folder / "dir" / "b.txt").read_text(encoding="utf-8") == "beta\n"
    assert not (folder / "untracked.txt").exists()
    assert _git(folder, "rev-parse", "--abbrev-ref", "HEAD").strip() == BRANCH
    assert _git(folder, "rev-parse", "HEAD").strip() == head
    assert _git(root, "status", "--porcelain") == status
    assert _git(root, "rev-parse", "--abbrev-ref", "HEAD").strip() == "main"
    assert rig.rows() == [worktree]
    assert rig.store.get_worktree(worktree.worktree_id) == {
        "worktree_id": worktree.worktree_id,
        "workspace_id": rig.workspace_id,
        "path": worktree.path,
        "branch": BRANCH,
        "base_oid": head,
        "state": "ready",
        "created_at": worktree.created_at,
        "updated_at": worktree.updated_at,
    }


@pytest.mark.parametrize("branch", BAD_BRANCHES)
def test_valid_branch_refuses_each_bad_name(branch: str) -> None:
    assert not valid_branch(branch)


@pytest.mark.parametrize(
    "branch",
    ["/a", "a.", "a//b", "a@{b", "a/.b", "a.lock/b", "", "a~b", "a^b", "a:b", "a*", "a\\b"],
)
def test_valid_branch_refuses_the_other_rules(branch: str) -> None:
    assert not valid_branch(branch)


@pytest.mark.parametrize("branch", [BRANCH, "a", "a" * 100, "feature/x.y_z-1", "HEADS", "a.b"])
def test_valid_branch_admits_plain_names(branch: str) -> None:
    assert valid_branch(branch)


@needs_git
async def test_create_refuses_an_invalid_branch_before_any_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _rig(tmp_path, _committed(tmp_path))
    calls: list[str] = []
    monkeypatch.setattr(rig.git, "repository", lambda workspace_id: calls.append(workspace_id))
    for branch in BAD_BRANCHES:
        assert await rig.refusal(branch) == "invalid_branch"
    assert calls == [] and rig.rows() == [] and rig.folders() == []


@needs_git
async def test_create_refuses_an_existing_branch_and_leaves_nothing(tmp_path: Path) -> None:
    root = _committed(tmp_path)
    _git(root, "branch", "taken")
    rig = _rig(tmp_path, root)

    assert await rig.refusal("taken") == "branch_exists"
    assert rig.rows() == [] and rig.folders() == []
    assert _git(root, "worktree", "list", "--porcelain").count("worktree ") == 1


@needs_git
async def test_create_refuses_a_repository_without_commits(tmp_path: Path) -> None:
    rig = _rig(tmp_path, _repo(tmp_path / "repo"))
    assert await rig.refusal() == "no_commits"
    assert rig.rows() == []


@needs_git
async def test_create_refuses_an_untrusted_folder_before_any_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _rig(tmp_path, _committed(tmp_path), trust=False)
    calls: list[tuple[str, ...]] = []
    real = rig.git.git

    def counting(root: Path, *args: str, timeout: float = 5.0) -> str:
        calls.append(args)
        return real(root, *args, timeout=timeout)

    monkeypatch.setattr(rig.git, "git", counting)
    assert await rig.refusal() == "trust_required"
    assert calls == [] and rig.rows() == []


@needs_git
async def test_create_refuses_a_subfolder_of_a_repository(tmp_path: Path) -> None:
    root = _committed(tmp_path)
    rig = _rig(tmp_path, root / "dir")
    before = (rig.store.list_session_roots(100), rig.store.list_worktrees(100), rig.folders())
    assert await rig.refusal() == "not_repository_root"
    assert (
        rig.store.list_session_roots(100),
        rig.store.list_worktrees(100),
        rig.folders(),
    ) == before
    assert rig.rows() == [] and rig.folders() == []


@needs_git
async def test_create_refuses_past_the_worktree_limit(tmp_path: Path) -> None:
    rig = _rig(tmp_path, _committed(tmp_path))
    head = _git(tmp_path / "repo", "rev-parse", "HEAD").strip()
    for index in range(MAX_WORKTREES):
        worktree_id = f"00000000-0000-4000-8000-{index:012d}"
        stamp = "2026-10-03T00:00:00.000Z"
        rig.store.insert_worktree(
            {
                "worktree_id": worktree_id,
                "workspace_id": rig.workspace_id,
                "path": str(tmp_path / f"held-{index}"),
                "branch": f"held-{index}",
                "base_oid": head,
                "state": "creating",
                "created_at": stamp,
                "updated_at": stamp,
            },
            limit=MAX_WORKTREES,
        )
        rig.store.update_worktree_state(worktree_id, "creating", "ready", stamp)

    before = (rig.store.list_session_roots(100), rig.store.list_worktrees(100), rig.folders())
    assert await rig.refusal() == "worktree_limit"
    assert (
        rig.store.list_session_roots(100),
        rig.store.list_worktrees(100),
        rig.folders(),
    ) == before
    assert len(rig.rows()) == MAX_WORKTREES and rig.folders() == []
    assert "refs/heads/" + BRANCH not in _git(tmp_path / "repo", "for-each-ref")


@needs_git
async def test_create_refuses_while_the_root_is_leased(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _rig(tmp_path, _committed(tmp_path))
    monkeypatch.setattr(worktrees, "LEASE_WAIT_S", 0.05)
    async with rig.coordinator.lease(rig.root(), ()):
        assert await rig.refusal() == "vault_locked"
    assert rig.rows() == [] and rig.folders() == []


@needs_git
async def test_create_holds_the_git_common_dir_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _rig(tmp_path, _committed(tmp_path))
    real = rig.coordinator.lease
    asked: list[dict[str, Any]] = []

    def spy(*args: Any, **kwargs: Any) -> Any:
        asked.append(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(rig.coordinator, "lease", spy)

    await rig.create()

    assert [kwargs["include_git_common"] for kwargs in asked] == [True]


@needs_git
async def test_create_refuses_off_windows_before_any_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A drive the publication service does not back (an exotic or network filesystem, on any
    OS) refuses the same way Windows itself no longer needs to: nothing durable survives, though
    the area folder may now exist, made before the lease ever runs the volume check."""
    rig = _rig(tmp_path, _committed(tmp_path))
    monkeypatch.setattr(vault_mutation, "_supported_volume", lambda _path: False)

    assert await rig.refusal() == "unsupported_filesystem"

    assert rig.rows() == [] and rig.folders() == []
    assert "refs/heads/" + BRANCH not in _git(tmp_path / "repo", "for-each-ref")


@needs_git
@pytest.mark.parametrize("hooks", [True, False])
@pytest.mark.parametrize("left_folder", [False, True])
async def test_a_failed_add_keeps_a_left_folder_or_drops_the_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, left_folder: bool, hooks: bool
) -> None:
    rig = _rig(tmp_path, _committed(tmp_path))
    real = rig.git.git

    def failing_add(root: Path, *args: str, timeout: float = 5.0) -> str:
        if ("worktree", "add") in zip(args, args[1:], strict=False):
            if left_folder:
                Path(args[-2]).mkdir()  # `... -b <branch> <path> <base>`
            raise RuntimeError("git worktree failed")
        return real(root, *args, timeout=timeout)

    monkeypatch.setattr(rig.git, "git", failing_add)
    with pytest.raises(WorktreeError) as refused:
        await rig.create(hooks=hooks)
    assert refused.value.code == "git_failed"
    if left_folder:
        assert [row.state for row in rig.rows()] == ["kept"]
        assert rig.folders() == [Path(rig.rows()[0].path)]
    else:
        assert rig.rows() == [] and rig.folders() == []


@needs_git
async def test_a_repository_fsmonitor_hook_never_runs(tmp_path: Path) -> None:
    root = _committed(tmp_path)
    rig = _rig(tmp_path, root)
    marker = tmp_path / "hook-ran"
    hook = f"\"echo ran > '{marker.as_posix()}'\""
    with (root / ".git" / "config").open("a", encoding="utf-8") as config:
        config.write(f"[core]\n\tfsmonitor = {hook}\n")

    worktree = await rig.create()
    assert worktree.state == "ready"
    assert not marker.exists()

    # The hook is live: a status without the hardening runs it.
    _git(root, "status", "--porcelain")
    assert marker.exists()


@needs_git
async def test_the_area_is_private(tmp_path: Path) -> None:
    rig = _rig(tmp_path, _committed(tmp_path))
    await rig.create()
    assert rig.area == tmp_path / "data-worktrees"
    require_private_directory(rig.area)


def test_worktree_area_sits_beside_the_data_folder(tmp_path: Path) -> None:
    assert worktree_area(tmp_path / "viki-agent") == tmp_path / "viki-agent-worktrees"


# --- hooks and long paths ---------------------------------------------------------------------


def _guard(rig: Rig) -> Path:
    """Swap in a CodeGit that checks the folder's Git settings against a Bot-copy baseline of
    their current state; answers the folder's root."""
    git = CodeGit(
        rig.workspaces, CodeFiles(rig.workspaces), git_available=lambda: True,
        bot_code_store=rig.store,
    )  # fmt: skip
    root = Path(rig.workspaces.get(rig.workspace_id, kind="code").root.display)
    assert rig.store.insert_bot_code_baseline(
        {
            "workspace_id": rig.workspace_id,
            "git_fingerprint": git.settings_fingerprint(root),
            "created_at": "2026-11-11T00:00:00.000Z",
        }
    )
    rig.git = git
    rig.service._git = git
    return root


def _state(rig: Rig, worktree_id: str) -> str | None:
    row = rig.store.get_worktree(worktree_id)
    return row["state"] if row is not None else None


def _change_settings(root: Path) -> None:
    _git(root, "config", "guard.synthetic", "changed")


@needs_git
@pytest.mark.parametrize("hooks", [True, False])
async def test_a_bot_copy_runs_no_checkout_hook_and_an_ordinary_worktree_does(
    tmp_path: Path, hooks: bool
) -> None:
    root = _committed(tmp_path)
    marker = tmp_path / "post-checkout-ran"
    hook = root / ".git" / "hooks" / "post-checkout"
    hook.write_bytes(f"#!/bin/sh\necho ran > '{marker.as_posix()}'\n".encode())
    hook.chmod(0o755)
    rig = _rig(tmp_path, root)
    _guard(rig)  # a Bot folder's create runs under its baseline

    worktree = await rig.create(hooks=hooks)

    assert worktree.state == "ready"
    assert (Path(worktree.path) / "a.txt").read_text(encoding="utf-8") == "alpha\n"
    assert marker.exists() is hooks


def _exists(path: Path) -> bool:
    # Past MAX_PATH, Windows answers only through the extended-length prefix.
    return os.path.exists("\\\\?\\" + str(path) if sys.platform == "win32" else path)


@needs_git
@pytest.mark.parametrize("hooks", [True, False])
async def test_a_file_past_max_path_is_checked_out(tmp_path: Path, hooks: bool) -> None:
    """A file whose worktree path passes 260 characters while its path in the folder does
    not: Git for Windows writes it only with `core.longpaths`, on both kinds of create."""
    root = _repo(tmp_path / "repo")
    area = worktree_area(tmp_path / "data")
    tree_length = len(str(area / ("0" * 36)))
    left = 270 - tree_length - 1 - len("x.txt")
    parts: list[str] = []
    while left > 50:
        parts.append("d" * 49)
        left -= 50
    relative = Path(*parts, "x" * max(left, 1) + ".txt")
    (root / relative).parent.mkdir(parents=True)
    (root / relative).write_text("long\n", encoding="utf-8")
    _commit(root, "long")
    assert len(str(root / relative)) < 250
    rig = _rig(tmp_path, root)

    worktree = await rig.create(hooks=hooks)
    try:
        assert worktree.state == "ready"
        assert len(str(Path(worktree.path) / relative)) > 260
        assert _exists(Path(worktree.path) / relative)
    finally:  # pytest's own cleanup cannot always delete past MAX_PATH
        _git(root, "-c", "core.longpaths=true", "worktree", "remove", "--force", worktree.path)


# --- the Git-settings guard -------------------------------------------------------------------


@needs_git
@pytest.mark.parametrize("phase", ["prepare", "lease", "add"])
async def test_changed_settings_refuse_create_at_each_phase_and_leave_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, phase: str
) -> None:
    """A change after admission (`prepare`), during the lease wait (`lease`) or after the
    branch check (`add`) is `git_settings_changed`, never `no_commits` or `git_failed`."""
    rig = _rig(tmp_path, _committed(tmp_path))
    root = _guard(rig)
    resolve = rig.service._resolve_root
    resolved = 0

    def resolving(*args: Any, **kwargs: Any) -> OrdinaryMutationRoot:
        nonlocal resolved
        resolved += 1
        # The first call admits the folder before the lease, the second after its wait.
        if resolved == {"prepare": 1, "lease": 2}.get(phase):
            _change_settings(root)
        return resolve(*args, **kwargs)

    real = rig.git.git

    def git(cwd: Path, *args: str, timeout: float = 5.0) -> str:
        answer = real(cwd, *args, timeout=timeout)
        if phase == "add" and "for-each-ref" in args:
            _change_settings(root)
        return answer

    monkeypatch.setattr(rig.service, "_resolve_root", resolving)
    monkeypatch.setattr(rig.git, "git", git)
    caplog.set_level(logging.WARNING)

    assert await rig.refusal() == "git_settings_changed"

    assert rig.rows() == [] and rig.folders() == []
    assert rig.store.list_worktrees(100) == ([], False)
    assert "refs/heads/" + BRANCH not in _git(root, "for-each-ref")
    assert str(tmp_path) not in caplog.text


@needs_git
async def test_changed_settings_after_the_lease_wait_refuse_removal_and_keep_the_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _rig(tmp_path, _committed(tmp_path))
    worktree = await rig.create()
    root = _guard(rig)
    resolve = rig.service._resolve_root
    resolved = 0

    def resolving(*args: Any, **kwargs: Any) -> OrdinaryMutationRoot:
        nonlocal resolved
        resolved += 1
        if resolved == 2:  # inside the lease, before any Git call
            _change_settings(root)
        return resolve(*args, **kwargs)

    monkeypatch.setattr(rig.service, "_resolve_root", resolving)
    workspace = rig.workspaces.get(rig.workspace_id, kind="code")
    with pytest.raises(WorktreeError) as refused:
        await rig.service.remove(
            worktree.worktree_id, workspace.policy_revision, workspace.root.physical_id
        )

    assert refused.value.code == "git_settings_changed"
    assert [row.state for row in rig.rows()] == ["ready"]
    assert Path(worktree.path).is_dir()
    assert _git(root, "worktree", "list", "--porcelain").count("worktree ") == 2


@needs_git
@pytest.mark.parametrize(("phase", "state"), [("lease", "removed"), ("checkout", "kept")])
async def test_changed_settings_refuse_restore_and_keep_the_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str, state: str
) -> None:
    """Changed under the lease, nothing is added and the row stays `removed`; changed after
    the add, before the new tree's checkout, the tree stays `kept`; the archive stays."""
    root = _committed(tmp_path)
    rig = _rig(tmp_path, root)
    worktree = await rig.create()
    tree = Path(worktree.path)
    (tree / "new.txt").write_text("new\n", encoding="utf-8")
    snap = worktree_archive.snapshot(rig.git, tree)
    worktree_archive.save(rig.git, root, worktree.worktree_id, snap, branch=BRANCH)
    _git(root, "worktree", "remove", "--force", str(tree))
    rig.store.update_worktree_state(worktree.worktree_id, "ready", "removed", worktree.updated_at)
    _guard(rig)
    resolve = rig.service._resolve_root
    resolved = 0

    def resolving(*args: Any, **kwargs: Any) -> OrdinaryMutationRoot:
        nonlocal resolved
        resolved += 1
        if phase == "lease" and resolved == 2:
            _change_settings(root)
        return resolve(*args, **kwargs)

    real = rig.git.git

    def git(cwd: Path, *args: str, timeout: float = 5.0, index: Path | None = None) -> str:
        if index is None:
            answer = real(cwd, *args, timeout=timeout)
        else:
            answer = real(cwd, *args, timeout=timeout, index=index)
        if phase == "checkout" and "--no-checkout" in args:
            _change_settings(root)
        return answer

    monkeypatch.setattr(rig.service, "_resolve_root", resolving)
    monkeypatch.setattr(rig.git, "git", git)
    workspace = rig.workspaces.get(rig.workspace_id, kind="code")
    with pytest.raises(WorktreeError) as refused:
        await rig.service.restore(
            worktree.worktree_id, workspace.policy_revision, workspace.root.physical_id
        )

    assert refused.value.code == "git_settings_changed"
    assert _state(rig, worktree.worktree_id) == state
    assert tree.exists() is (state == "kept")
    assert _git(root, "for-each-ref", worktree_archive.PREFIX + worktree.worktree_id)


@needs_git
async def test_the_dry_run_reads_a_tree_whose_settings_changed_as_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    rig = _rig(tmp_path, _committed(tmp_path))
    worktree = await rig.create()
    root = _guard(rig)
    load = rig.service._load_folder

    def loading(workspace_id: str) -> Any:
        folder = load(workspace_id)  # the folder's own check passes
        _change_settings(root)  # then the tree's inspection meets the change
        return folder

    monkeypatch.setattr(rig.service, "_load_folder", loading)
    caplog.set_level(logging.WARNING)

    decisions = await rig.service.plan()

    assert decisions == [Decision(worktree.worktree_id, "keep", "unknown", None, None, None)]
    assert "skipped a row (git_settings_changed)" in caplog.text
    assert str(tmp_path) not in caplog.text and worktree.worktree_id not in caplog.text


@needs_git
async def test_recovery_skips_a_folder_whose_settings_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    rig = _rig(tmp_path, _committed(tmp_path))
    worktree = await rig.create()
    rig.store.update_worktree_state(worktree.worktree_id, "ready", "creating", worktree.updated_at)
    root = _guard(rig)
    repository = rig.git.repository

    def admitted(workspace_id: str) -> Path:
        found = repository(workspace_id)
        _change_settings(root)  # after the admission, before the folder's own Git reads
        return found

    monkeypatch.setattr(rig.git, "repository", admitted)
    caplog.set_level(logging.WARNING)

    await rig.service.recover()

    assert _state(rig, worktree.worktree_id) == "creating"
    assert "recovery skipped a folder (git_settings_changed)" in caplog.text
    assert "failed (CodeError)" not in caplog.text


# --- state-selected reads -----------------------------------------------------------------------


@needs_git
async def test_recovery_and_the_dry_run_find_their_rows_behind_any_history(
    tmp_path: Path,
) -> None:
    rig = _rig(tmp_path, _committed(tmp_path))
    crashed = await rig.create("crashed")
    ready = await rig.create("ready")
    rig.store.update_worktree_state(crashed.worktree_id, "ready", "creating", crashed.updated_at)
    history(rig.store, 4100, "removed")
    history(rig.store, 4100, "kept")

    await rig.service.recover()
    decisions = await rig.service.plan()

    assert _state(rig, crashed.worktree_id) == "ready"
    assert {d.worktree_id for d in decisions} == {crashed.worktree_id, ready.worktree_id}
