"""Integrate, push and check, against real repositories in short temp folders: a worktree ahead of
its folder, and a local bare repository as `origin`, so no test reaches a network. Mutations run
under the whole-root lease."""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
from collections.abc import Awaitable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from tests.app.test_workspaces import service_at, store_at
from tests.workspace_code.test_code_git import _commit, _git, _repo
from tests.workspace_code.test_workspaces_code import trusted
from viki_agent import winproc
from viki_agent.code.code_files import CodeError, CodeFiles
from viki_agent.code.code_git import HARDENING, CodeGit
from viki_agent.code.code_integration import CheckResult, CodeIntegration, PushResult
from viki_agent.storage import SessionStore
from viki_agent.vaults import vault_mutation
from viki_agent.vaults.vault_mutation import MutationCoordinator
from viki_agent.vaults.workspace_mutation import WorkspaceMutationResolver
from viki_agent.workspaces import WorkspaceService

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not available")
BRANCH = "viki/20261004-cd34"
WORKTREE = "00000000-0000-4000-8000-000000000001"
SESSION = "33333333-3333-4333-8333-000000000001"
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
    workspace_id: str
    folder: Path
    short: Path  # a short folder: Windows MAX_PATH under the default basetemp
    busy: set[str] = field(default_factory=set[str])
    integration: CodeIntegration = field(init=False)

    def __post_init__(self) -> None:
        resolver = WorkspaceMutationResolver(self.workspaces, git_available=lambda: True)
        self.integration = CodeIntegration(
            self.git, self.store, MutationCoordinator(), resolver, busy=self.busy.__contains__
        )

    def admission(self) -> tuple[int, str]:
        workspace = self.workspaces.get(self.workspace_id, kind="code")
        return workspace.policy_revision, workspace.root.physical_id

    def worktree(self, commits: int = 2) -> tuple[str, Path]:
        """A real worktree of the folder, `commits` ahead of it, and its `ready` row."""
        path = self.short / "w"
        _git(self.folder, "worktree", "add", "-q", "-b", BRANCH, str(path), "HEAD")
        self.store.insert_worktree(
            {
                "worktree_id": WORKTREE,
                "workspace_id": self.workspace_id,
                "path": str(path),
                "branch": BRANCH,
                "base_oid": _head(self.folder),
                "state": "creating",
                "created_at": STAMP,
                "updated_at": STAMP,
            },
            limit=32,
        )
        self.store.update_worktree_state(WORKTREE, "creating", "ready", STAMP)
        for number in range(1, commits + 1):
            (path / "a.txt").write_text(f"worktree {number}\n", encoding="utf-8")
            _commit(path, f"worktree {number}")
        return WORKTREE, path

    async def integrate(
        self,
        *,
        target_branch: str = "main",
        target_oid: str | None = None,
        head_oid: str | None = None,
    ) -> str:
        revision, physical_id = self.admission()
        return await self.integration.integrate(
            WORKTREE,
            revision,
            physical_id,
            target_branch,
            target_oid or _head(self.folder),
            head_oid or _head(self.short / "w"),
        )

    def origin(self) -> Path:
        bare = self.short / "b"
        bare.mkdir()
        _git(bare, "init", "-q", "--bare", "-b", "main")
        _git(self.folder, "remote", "add", "origin", str(bare))
        return bare

    async def push(
        self, *, branch: str = "main", head_oid: str | None = None, remote: str = "origin"
    ) -> PushResult:
        revision, physical_id = self.admission()
        return await self.integration.push(
            self.workspace_id,
            revision,
            physical_id,
            remote,
            branch,
            head_oid or _head(self.folder),
        )


def _rig(tmp_path: Path) -> Rig:
    short = Path(tempfile.mkdtemp(dir=tmp_path.parent, prefix="c"))
    folder = _repo(short / "repo")
    (folder / "a.txt").write_text("alpha\n", encoding="utf-8")
    _commit(folder, "first")
    store = store_at(tmp_path)
    # Git available, so the registered identity holds the Git common dir the lease needs.
    workspaces = service_at(tmp_path, store, git=True)
    workspace_id = trusted(workspaces, folder)
    git = CodeGit(workspaces, CodeFiles(workspaces), git_available=lambda: True)
    return Rig(store, workspaces, git, workspace_id, folder, short)


def _head(cwd: Path) -> str:
    return _git(cwd, "rev-parse", "HEAD").strip()


def _remote_head(bare: Path, branch: str = "main") -> str | None:
    found = _git(bare, "for-each-ref", "--format=%(objectname)", f"refs/heads/{branch}").strip()
    return found or None


def _more(cwd: Path, name: str, text: str) -> str:
    (cwd / name).write_text(text, encoding="utf-8")
    _commit(cwd, name)
    return _head(cwd)


async def _refused(awaitable: Awaitable[object]) -> str:
    with pytest.raises(CodeError) as refused:
        await awaitable
    return refused.value.code


def _count_git(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, ...]]:
    calls: list[tuple[str, ...]] = []
    real = rig.git.git

    def counting(root: Path, *args: str, timeout: float = 5.0) -> str:
        calls.append(args)
        return real(root, *args, timeout=timeout)

    monkeypatch.setattr(rig.git, "git", counting)
    return calls


# --- integrate ----------------------------------------------------------------------------------


@needs_git
async def test_integrate_fast_forwards_the_folder_to_the_reviewed_head(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    _, path = rig.worktree()
    head = _head(path)

    assert await rig.integrate() == head

    assert _head(rig.folder) == head
    assert _git(rig.folder, "symbolic-ref", "HEAD").strip() == "refs/heads/main"
    assert (rig.folder / "a.txt").read_text(encoding="utf-8") == "worktree 2\n"
    assert _git(rig.folder, "status", "--porcelain") == ""
    assert _head(path) == head  # the worktree is untouched
    assert _git(path, "symbolic-ref", "HEAD").strip() == f"refs/heads/{BRANCH}"


@needs_git
async def test_an_unrelated_untracked_file_stays(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    _, path = rig.worktree()
    (rig.folder / "notes.txt").write_text("mine\n", encoding="utf-8")

    assert await rig.integrate() == _head(path)
    assert (rig.folder / "notes.txt").read_text(encoding="utf-8") == "mine\n"


@needs_git
async def test_an_untracked_file_at_a_range_path_is_merge_refused(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    _, path = rig.worktree()
    _more(path, "new.txt", "theirs\n")
    before = _head(rig.folder)
    (rig.folder / "new.txt").write_text("mine\n", encoding="utf-8")

    assert await _refused(rig.integrate()) == "merge_refused"
    assert _head(rig.folder) == before
    assert (rig.folder / "new.txt").read_text(encoding="utf-8") == "mine\n"


@needs_git
@pytest.mark.parametrize(("ignored", "ref"), [("local.cfg", "local.cfg"), ("build/", "build/out")])
async def test_an_ignored_file_at_a_range_path_is_merge_refused(
    tmp_path: Path, ignored: str, ref: str
) -> None:
    rig = _rig(tmp_path)
    _more(rig.folder, ".gitignore", f"{ignored}\n")
    _, path = rig.worktree()
    (path / ref).parent.mkdir(exist_ok=True)
    (path / ref).write_text("theirs\n", encoding="utf-8")
    _git(path, "add", "-f", ref)
    _git(path, "commit", "-q", "-m", "theirs")
    before = _head(rig.folder)
    (rig.folder / ref).parent.mkdir(exist_ok=True)
    (rig.folder / ref).write_text("OWNER\n", encoding="utf-8")

    assert await _refused(rig.integrate()) == "merge_refused"
    assert _head(rig.folder) == before
    assert (rig.folder / ref).read_text(encoding="utf-8") == "OWNER\n"


@needs_git
async def test_a_moved_folder_branch_is_stale_base(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    rig.worktree()
    reviewed = _head(rig.folder)
    assert await _refused(rig.integrate(target_branch="side")) == "stale_base"
    _more(rig.folder, "b.txt", "beta\n")
    assert await _refused(rig.integrate(target_oid=reviewed)) == "stale_base"


@needs_git
async def test_a_moved_worktree_head_is_stale_review(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    _, path = rig.worktree()
    earlier = _git(path, "rev-parse", "HEAD~1").strip()
    assert await _refused(rig.integrate(head_oid=earlier)) == "stale_review"


@needs_git
async def test_a_worktree_at_the_folders_head_is_up_to_date(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    rig.worktree(commits=0)
    assert await _refused(rig.integrate()) == "up_to_date"


@needs_git
async def test_a_folder_that_moved_on_is_not_fast_forward(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    rig.worktree()
    before = _more(rig.folder, "b.txt", "beta\n")
    assert await _refused(rig.integrate()) == "not_fast_forward"
    assert _head(rig.folder) == before


@needs_git
@pytest.mark.parametrize("change", ["tracked", "staged"])
async def test_a_tracked_change_in_the_folder_is_dirty_target(tmp_path: Path, change: str) -> None:
    rig = _rig(tmp_path)
    rig.worktree()
    before = _head(rig.folder)
    if change == "tracked":
        (rig.folder / "a.txt").write_text("edited\n", encoding="utf-8")
    else:
        (rig.folder / "staged.txt").write_text("staged\n", encoding="utf-8")
        _git(rig.folder, "add", "staged.txt")
    assert await _refused(rig.integrate()) == "dirty_target"
    assert _head(rig.folder) == before


@needs_git
async def test_a_binary_in_the_range_is_unsupported_change(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    _, path = rig.worktree()
    (path / "blob.bin").write_bytes(b"\0\1\2binary")
    _commit(path, "binary")
    assert await _refused(rig.integrate()) == "unsupported_change"


@needs_git
async def test_a_detached_folder_is_detached_target(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    rig.worktree()
    _git(rig.folder, "switch", "-q", "--detach")
    assert await _refused(rig.integrate()) == "detached_target"


@needs_git
async def test_a_target_branch_viki_cannot_name_is_refused_before_any_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _rig(tmp_path)
    rig.worktree()
    calls = _count_git(rig, monkeypatch)
    assert await _refused(rig.integrate(target_branch="a+b")) == "unsupported_branch"
    assert await _refused(rig.push(branch="a+b")) == "unsupported_branch"
    assert calls == []


@needs_git
async def test_a_busy_session_in_the_folder_is_checkout_busy(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    _, path = rig.worktree()
    rig.store.put_session_root(
        {
            "session_id": SESSION,
            "workspace_id": rig.workspace_id,
            "worktree_id": WORKTREE,
            "display_root": "<repo>",
            "created_at": STAMP,
        }
    )
    other = "33333333-3333-4333-8333-000000000002"
    rig.store.put_session_root(
        {
            "session_id": other,
            "workspace_id": rig.workspace_id,
            "worktree_id": None,
            "display_root": "<repo>",
            "created_at": STAMP,
        }
    )
    rig.busy.add(other)
    assert await _refused(rig.integrate()) == "checkout_busy"

    rig.busy.clear()
    rig.busy.add(SESSION)  # a turn in the worktree itself does not hold the folder
    assert await rig.integrate() == _head(path)


# --- push ---------------------------------------------------------------------------------------


@needs_git
async def test_push_a_new_branch_and_then_a_fast_forward(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    bare = rig.origin()
    first = _head(rig.folder)
    assert await rig.push() == PushResult("pushed", first)
    assert _remote_head(bare) == first

    second = _more(rig.folder, "b.txt", "beta\n")
    assert await rig.push() == PushResult("pushed", second)
    assert _remote_head(bare) == second


@needs_git
async def test_a_non_fast_forward_push_is_rejected(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    bare = rig.origin()
    _git(rig.folder, "switch", "-q", "-c", "side")
    theirs = _more(rig.folder, "b.txt", "theirs\n")
    _git(rig.folder, "push", "-q", "origin", "side:main")
    _git(rig.folder, "switch", "-q", "main")
    _more(rig.folder, "c.txt", "ours\n")

    assert await _refused(rig.push()) == "push_rejected"
    assert _remote_head(bare) == theirs


@needs_git
async def test_a_refusing_pre_push_hook_is_push_rejected(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    bare = rig.origin()
    hooks = rig.short / "hooks"
    hooks.mkdir()
    hook = hooks / "pre-push"
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8", newline="\n")
    hook.chmod(0o755)
    _git(rig.folder, "config", "core.hooksPath", str(hooks))

    assert await _refused(rig.push()) == "push_rejected"
    assert _remote_head(bare) is None


@needs_git
async def test_an_unreachable_remote_is_push_failed(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    rig = _rig(tmp_path)
    missing = rig.short / "missing"
    _git(rig.folder, "remote", "add", "origin", str(missing))
    with caplog.at_level(logging.WARNING):
        assert await _refused(rig.push()) == "push_failed"
    assert "git push failed (RuntimeError)" in caplog.text
    assert str(missing) not in caplog.text and "missing" not in caplog.text


@needs_git
async def test_a_push_timeout_reads_the_remote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _rig(tmp_path)
    bare = rig.origin()
    landed = _head(rig.folder)
    _git(rig.folder, "push", "-q", "origin", "main")
    real = rig.git.git

    def timing_out(root: Path, *args: str, timeout: float = 5.0) -> str:
        if args[0] == "push":
            raise TimeoutError("push")
        return real(root, *args, timeout=timeout)

    monkeypatch.setattr(rig.git, "git", timing_out)
    assert await rig.push() == PushResult("pushed", landed)

    _more(rig.folder, "b.txt", "beta\n")
    assert await rig.push() == PushResult("unknown", landed)
    assert _remote_head(bare) == landed


@needs_git
async def test_a_moved_branch_or_another_remote_is_stale_review(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    bare = rig.origin()
    reviewed = _head(rig.folder)
    _more(rig.folder, "b.txt", "beta\n")
    assert await _refused(rig.push(head_oid=reviewed)) == "stale_review"
    assert await _refused(rig.push(remote="upstream")) == "stale_review"
    assert _remote_head(bare) is None


@needs_git
async def test_no_origin_and_an_unsupported_remote(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    assert await _refused(rig.push()) == "no_remote"
    rig.origin()
    elsewhere = rig.short / "p"
    _git(rig.folder, "config", "remote.origin.pushurl", str(elsewhere))
    assert await _refused(rig.push()) == "remote_unsupported"


# --- check (every platform) ---------------------------------------------------------------------


@needs_git
async def test_check_reads_the_remotes_branch(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    rig.origin()
    landed = _head(rig.folder)
    _git(rig.folder, "push", "-q", "origin", "main")
    check = rig.integration.check
    assert await check(rig.workspace_id, "origin", "main", landed) == CheckResult("pushed", landed)

    later = _more(rig.folder, "b.txt", "beta\n")
    assert await check(rig.workspace_id, "origin", "main", later) == CheckResult(
        "not_pushed", landed
    )
    assert await check(rig.workspace_id, "origin", "side", later) == CheckResult("not_pushed", None)

    _git(rig.folder, "remote", "set-url", "origin", str(rig.short / "missing"))
    assert await _refused(check(rig.workspace_id, "origin", "main", later)) == "push_failed"


# --- argv and platform --------------------------------------------------------------------------


@needs_git
async def test_no_force_or_skipped_hook_reaches_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _rig(tmp_path)
    bare = rig.origin()
    _, path = rig.worktree()
    head = _head(path)
    seen: list[tuple[str, ...]] = []
    real = winproc.git

    def spy(cwd: str | os.PathLike[str], *args: str, timeout: float = 15.0, **kwargs: Any) -> str:
        seen.append(args)
        return real(cwd, *args, timeout=timeout, **kwargs)

    monkeypatch.setattr(winproc, "git", spy)
    assert await rig.integrate() == head
    assert await rig.push(branch="main") == PushResult("pushed", head)
    assert await rig.push(branch=BRANCH) == PushResult("pushed", head)

    assert _remote_head(bare) == head and _remote_head(bare, BRANCH) == head
    merge = ("merge", "--ff-only", "--no-autostash", "--no-overwrite-ignore", head)
    assert (*HARDENING, *merge) in seen
    push = (*HARDENING, "push", "--porcelain", "--no-follow-tags", "--recurse-submodules=no")
    assert (*push, "origin", f"{head}:refs/heads/main") in seen
    assert (*push, "origin", f"{head}:refs/heads/{BRANCH}") in seen
    # `remote get-url --all` reads every URL; the push-only options are banned from pushes.
    pushes = [args for args in seen if "push" in args]
    assert len(pushes) == 2
    for args in pushes:
        assert not {"-u", "--set-upstream", "--mirror", "--all", "--tags"} & set(args), args
    for args in seen:
        assert not {"-f", "--no-verify"} & set(args), args
        assert not any(arg.startswith(("--force", "+")) for arg in args), args


@needs_git
async def test_an_unsupported_drive_refuses_integrate_and_push(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _rig(tmp_path)
    rig.worktree()
    bare = rig.origin()
    folder_head = _head(rig.folder)
    monkeypatch.setattr(vault_mutation, "_supported_volume", lambda _path: False)

    assert await _refused(rig.integrate()) == "unsupported_filesystem"
    assert await _refused(rig.push()) == "unsupported_filesystem"

    assert _head(rig.folder) == folder_head
    assert _remote_head(bare) is None
