"""The checkout and the mutation helpers, against real repositories: a folder's and a worktree's
checkout and each refusal, the in-progress test, the busy guard, admission and the whole-root
lease."""

from __future__ import annotations

import os
import shutil
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from tests.app.test_workspaces import COMMAND, service_at, store_at
from tests.workspace_code.test_code_git import _commit, _git, _repo
from tests.workspace_code.test_workspaces_code import trusted
from viki_agent import winproc
from viki_agent.code import code_checkout
from viki_agent.code.code_checkout import Checkout, admit, busy_in, checkout, held, in_progress
from viki_agent.code.code_files import CodeError, CodeFiles
from viki_agent.code.code_git import CodeGit
from viki_agent.storage import SessionStore
from viki_agent.vaults.vault_mutation import MutationCoordinator, OrdinaryMutationRoot
from viki_agent.vaults.workspace_mutation import WorkspaceMutationResolver
from viki_agent.workspaces import WorkspaceService

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not available")
BRANCH = "viki/20261004-ab12"
WORKTREE = "00000000-0000-4000-8000-000000000001"
OTHER = "00000000-0000-4000-8000-000000000002"
STAMP = "2026-10-04T00:00:00.000Z"


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
    workspace_id: str
    folder: Path
    short: Path  # a short folder for worktrees: Windows MAX_PATH under the default basetemp

    def checkout(self, worktree_id: str | None = None) -> Checkout:
        return checkout(self.store, self.git, self.workspace_id, worktree_id)

    def refusal(self, worktree_id: str | None = None) -> str:
        with pytest.raises(CodeError) as refused:
            self.checkout(worktree_id)
        return refused.value.code

    def admit(self) -> OrdinaryMutationRoot:
        workspace = self.workspaces.get(self.workspace_id, kind="code")
        return admit(
            self.resolver, self.workspace_id, workspace.policy_revision, workspace.root.physical_id
        )

    def row(
        self,
        path: Path,
        *,
        state: str = "ready",
        worktree_id: str = WORKTREE,
        workspace_id: str | None = None,
    ) -> str:
        self.store.insert_worktree(
            {
                "worktree_id": worktree_id,
                "workspace_id": workspace_id or self.workspace_id,
                "path": str(path),
                "branch": BRANCH,
                "base_oid": "a" * 40,
                "state": "creating",
                "created_at": STAMP,
                "updated_at": STAMP,
            },
            limit=32,
        )
        if state != "creating":
            self.store.update_worktree_state(worktree_id, "creating", state, STAMP)
        return worktree_id

    def worktree(self, *, state: str = "ready") -> tuple[str, Path]:
        """A real `git worktree add` of the folder and its row."""
        path = self.short / "w"
        _git(self.folder, "worktree", "add", "-q", "-b", BRANCH, str(path), "HEAD")
        return self.row(path, state=state), path


def _committed(root: Path) -> Path:
    _repo(root)
    (root / "a.txt").write_text("alpha\n", encoding="utf-8")
    (root / "dir").mkdir()
    (root / "dir" / "b.txt").write_text("beta\n", encoding="utf-8")
    _commit(root, "first")
    return root


def _rig(tmp_path: Path, *, trust: bool = True) -> Rig:
    short = Path(tempfile.mkdtemp(dir=tmp_path.parent, prefix="c"))
    folder = _committed(short / "repo")
    store = store_at(tmp_path)
    # Git available, so the registered identity holds the Git common dir the lease needs.
    workspaces = service_at(tmp_path, store, git=True)
    if trust:
        workspace_id = trusted(workspaces, folder)
    else:
        workspace_id = workspaces.register_existing(COMMAND, str(folder), kind="code")[
            0
        ].workspace_id
    git = CodeGit(workspaces, CodeFiles(workspaces), git_available=lambda: True)
    resolver = WorkspaceMutationResolver(workspaces, git_available=lambda: True)
    return Rig(store, workspaces, git, MutationCoordinator(), resolver, workspace_id, folder, short)


def _head(cwd: Path) -> str:
    return _git(cwd, "rev-parse", "HEAD").strip()


def _count_git(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, ...]]:
    calls: list[tuple[str, ...]] = []
    real = rig.git.git

    def counting(root: Path, *args: str, timeout: float = 5.0) -> str:
        calls.append(args)
        return real(root, *args, timeout=timeout)

    monkeypatch.setattr(rig.git, "git", counting)
    return calls


# --- checkout -----------------------------------------------------------------------------------


@needs_git
def test_a_folders_checkout(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    assert rig.checkout() == Checkout(
        rig.workspace_id, None, rig.folder, rig.folder, "main", _head(rig.folder)
    )


@needs_git
def test_a_ready_worktrees_checkout(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    worktree_id, path = rig.worktree()
    (path / "a.txt").write_text("worktree\n", encoding="utf-8")
    _commit(path, "in the worktree")

    assert rig.checkout(worktree_id) == Checkout(
        rig.workspace_id, worktree_id, rig.folder, path, BRANCH, _head(path)
    )
    assert _head(path) != _head(rig.folder)


@needs_git
def test_a_detached_head_has_no_branch(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    _git(rig.folder, "switch", "-q", "--detach")
    found = rig.checkout()
    assert (found.branch, found.head_oid) == (None, _head(rig.folder))


@needs_git
def test_a_branch_viki_cannot_name_is_refused(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    _git(rig.folder, "switch", "-q", "-c", "a+b")
    assert rig.refusal() == "unsupported_branch"


@needs_git
def test_an_untrusted_folder_is_refused_before_any_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _rig(tmp_path, trust=False)
    calls = _count_git(rig, monkeypatch)
    assert rig.refusal() == "trust_required"
    assert rig.refusal(WORKTREE) == "trust_required"
    assert calls == []


@needs_git
def test_an_absent_row_or_another_folders_row_is_unknown(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    assert rig.refusal(WORKTREE) == "unknown_worktree"
    other = trusted(rig.workspaces, _committed(rig.short / "other"))
    _git(rig.folder, "worktree", "add", "-q", "-b", BRANCH, str(rig.short / "w"), "HEAD")
    rig.row(rig.short / "w", workspace_id=other)
    assert rig.refusal(WORKTREE) == "unknown_worktree"


@needs_git
def test_a_kept_worktree_is_not_ready(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    worktree_id, _ = rig.worktree(state="kept")
    assert rig.refusal(worktree_id) == "worktree_not_ready"


@needs_git
def test_a_missing_worktree_folder_is_not_ready(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    assert rig.refusal(rig.row(rig.short / "gone")) == "worktree_not_ready"


@needs_git
def test_a_worktree_of_another_repository_is_refused(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    # A repository's own top level at the row's path, sharing nothing with the folder.
    elsewhere = _committed(rig.short / "w")
    assert rig.refusal(rig.row(elsewhere)) == "worktree_moved"


@needs_git
@pytest.mark.parametrize("where", ["plain", "subfolder"])
def test_a_folder_that_is_not_a_worktree_top_level_is_refused(tmp_path: Path, where: str) -> None:
    rig = _rig(tmp_path)
    if where == "plain":
        path = rig.short / "plain"
        path.mkdir()
    else:  # inside the folder's own repository, so the prefix is not empty
        path = rig.folder / "dir"
    assert rig.refusal(rig.row(path)) == "worktree_moved"


@needs_git
def test_an_unresolvable_git_common_dir_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _rig(tmp_path)
    worktree_id, _ = rig.worktree()
    real = os.path.realpath
    raised: list[str] = []

    def failing(path: Any, *args: Any, **kwargs: Any) -> str:
        if Path(path).name == ".git":
            raised.append(str(path))
            raise OSError(64, "The specified network name is no longer available")
        return real(path, *args, **kwargs)

    monkeypatch.setattr(os.path, "realpath", failing)
    assert rig.refusal(worktree_id) == "worktree_moved"
    assert raised


# --- in_progress --------------------------------------------------------------------------------


@needs_git
@pytest.mark.parametrize("in_worktree", [False, True])
def test_in_progress_during_a_conflicted_merge(tmp_path: Path, in_worktree: bool) -> None:
    rig = _rig(tmp_path)
    _git(rig.folder, "switch", "-q", "-c", "side")
    (rig.folder / "a.txt").write_text("side\n", encoding="utf-8")
    _commit(rig.folder, "side")
    _git(rig.folder, "switch", "-q", "main")
    cwd = rig.worktree()[1] if in_worktree else rig.folder
    (cwd / "a.txt").write_text("here\n", encoding="utf-8")
    _commit(cwd, "here")
    assert not in_progress(rig.git, cwd)

    assert winproc.run(["git", "merge", "side"], cwd=cwd).returncode != 0
    assert in_progress(rig.git, cwd)
    if in_worktree:  # the worktree's state is its own
        assert not in_progress(rig.git, rig.folder)

    _git(cwd, "merge", "--abort")
    assert not in_progress(rig.git, cwd)


@needs_git
def test_in_progress_refuses_an_unexpected_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _rig(tmp_path)
    monkeypatch.setattr(rig.git, "git", lambda root, *args, timeout=5.0: ".git/MERGE_HEAD\n")
    with pytest.raises(CodeError) as refused:
        in_progress(rig.git, rig.folder)
    assert refused.value.code == "git_failed"


# --- busy_in ------------------------------------------------------------------------------------


async def test_busy_in_reads_the_checkouts_own_sessions(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    folder, other = "00000000-0000-4000-8000-000000000101", "00000000-0000-4000-8000-000000000102"
    sessions = {
        "33333333-3333-4333-8333-000000000001": (folder, None),
        "33333333-3333-4333-8333-000000000002": (folder, WORKTREE),
        "33333333-3333-4333-8333-000000000003": (other, None),
    }
    for session_id, (workspace_id, worktree_id) in sessions.items():
        store.put_session_root(
            {
                "session_id": session_id,
                "workspace_id": workspace_id,
                "worktree_id": worktree_id,
                "display_root": "<repo>",
                "created_at": STAMP,
            }
        )
    in_folder, in_worktree, elsewhere = sessions

    async def busy(running: set[str], worktree_id: str | None) -> bool:
        def ask(session_id: str) -> bool:
            assert threading.current_thread() is threading.main_thread()  # on the loop
            return session_id in running

        return await busy_in(store, ask, folder, worktree_id)

    assert await busy({in_folder}, None)
    assert not await busy({in_folder}, WORKTREE)
    assert await busy({in_worktree}, WORKTREE)
    assert not await busy({in_worktree}, None)
    assert not await busy({elsewhere}, None)
    assert not await busy(set(), None)
    assert not await busy(set(sessions), OTHER)


# --- admit and held -----------------------------------------------------------------------------


@needs_git
def test_admit_returns_the_code_root_and_refuses_an_untrusted_folder(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    root = rig.admit()
    assert (root.workspace_id, root.kind) == (rig.workspace_id, "code")

    (tmp_path / "u").mkdir()
    untrusted = _rig(tmp_path / "u", trust=False)
    with pytest.raises(CodeError) as refused:
        untrusted.admit()
    assert refused.value.code == "trust_required"


@needs_git
async def test_held_refuses_while_another_holds_the_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _rig(tmp_path)
    root = rig.admit()
    monkeypatch.setattr(code_checkout, "LEASE_WAIT_S", 0.05)
    entered = False
    async with rig.coordinator.lease(root, ()):
        with pytest.raises(CodeError) as refused:
            async with held(rig.coordinator, root, rig.resolver):
                entered = True
    assert refused.value.code == "vault_locked"
    assert not entered


@needs_git
async def test_held_takes_the_git_common_dir_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _rig(tmp_path)
    root = rig.admit()
    real = rig.coordinator.lease
    asked: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def spy(*args: Any, **kwargs: Any) -> Any:
        asked.append((args, kwargs))
        return real(*args, **kwargs)

    monkeypatch.setattr(rig.coordinator, "lease", spy)
    entered = False
    async with held(rig.coordinator, root, rig.resolver):
        entered = True
    assert entered
    assert asked == [((root, ()), {"include_git_common": True, "wait_s": 10.0})]
