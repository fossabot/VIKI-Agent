"""Code mutation admission and busy state are checked after a real root lease wait.

Every repository and bare remote is synthetic and local. Busy is an observation before mutation
dispatch, not a reservation against a later session turn.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from tests.workspace_code.test_code_checkout import BRANCH
from tests.workspace_code.test_code_checkout import _rig as checkout_rig
from tests.workspace_code.test_code_commit import SESSION, STAMP, _approval, _commit, _staged
from tests.workspace_code.test_code_git import _git
from tests.workspace_code.test_code_integration import _head, _remote_head
from tests.workspace_code.test_code_integration import _rig as integration_rig
from tests.workspace_code.test_worktree_in_use import _short_rig as removal_rig
from tests.workspace_code.test_worktrees import _committed, needs_git
from tests.workspace_code.test_worktrees import _rig as worktree_rig
from viki_agent.code.code_commit import CodeCommitter
from viki_agent.code.code_files import CodeError
from viki_agent.code.code_review import CodeReview
from viki_agent.code.worktrees import WorktreeError
from viki_agent.vaults.vault_mutation import MutationCoordinator, OrdinaryMutationRoot
from viki_agent.workspaces import WorkspaceService

HANG_GUARD_S = 10.0  # events normally fire in milliseconds; not a bound on Git work


@pytest.fixture(autouse=True)
def _empty_git_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.upper().startswith("GIT_"):
            monkeypatch.delenv(name)
    isolated_home = tmp_path / "git-home"
    isolated_home.mkdir()
    for name in ("HOME", "USERPROFILE", "XDG_CONFIG_HOME"):
        monkeypatch.setenv(name, str(isolated_home))


def _revoke(workspaces: WorkspaceService, workspace_id: str) -> None:
    workspace = workspaces.get(workspace_id, kind="code")
    revoked = workspaces.set_policy(
        str(uuid.uuid4()), workspace_id, workspace.policy_revision, "existing_readonly", []
    )
    assert revoked.policy == "existing_readonly"
    assert revoked.policy_revision == workspace.policy_revision + 1


async def _change_during_wait(
    coordinator: MutationCoordinator,
    root: OrdinaryMutationRoot,
    operation: Awaitable[object],
    change: Callable[[], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    waiting = asyncio.Event()
    original = coordinator._condition.wait

    async def announce() -> bool:
        waiting.set()
        return await original()

    monkeypatch.setattr(coordinator._condition, "wait", announce)
    async with coordinator.lease(root, (), include_git_common=True):
        task = asyncio.ensure_future(operation)
        try:
            await asyncio.wait_for(waiting.wait(), HANG_GUARD_S)
            change()
        except BaseException:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise
    await task


@needs_git
@pytest.mark.parametrize("in_worktree", [False, True])
async def test_commit_refuses_trust_revoked_during_lease_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, in_worktree: bool
) -> None:
    rig = checkout_rig(tmp_path)
    worktree_id, path = rig.worktree() if in_worktree else (None, rig.folder)
    (path / "a.txt").write_text("approved\n", encoding="utf-8")
    approval = _approval(rig, "a.txt", worktree_id=worktree_id)
    head = _head(path)
    folder_head = _head(rig.folder)
    before = (path / "a.txt").read_bytes()
    with pytest.raises(CodeError, match="^workspace_changed$"):
        await _change_during_wait(
            rig.coordinator,
            rig.admit(),
            _commit(
                rig,
                approval,
                worktree_id=worktree_id,
                branch=BRANCH if in_worktree else "main",
                head_oid=head,
            ),
            lambda: _revoke(rig.workspaces, rig.workspace_id),
            monkeypatch,
        )
    assert _head(path) == head and _head(rig.folder) == folder_head
    assert _staged(path) == []
    assert (path / "a.txt").read_bytes() == before


@needs_git
async def test_integrate_refuses_trust_revoked_during_lease_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = integration_rig(tmp_path)
    _, path = rig.worktree()
    head, worktree_head = _head(rig.folder), _head(path)
    before = (rig.folder / "a.txt").read_bytes()
    root = rig.integration._resolve_root(rig.workspace_id, *rig.admission(), kind="code")
    with pytest.raises(CodeError, match="^workspace_changed$"):
        await _change_during_wait(
            rig.integration._coordinator,
            root,
            rig.integrate(target_oid=head, head_oid=worktree_head),
            lambda: _revoke(rig.workspaces, rig.workspace_id),
            monkeypatch,
        )
    assert _head(rig.folder) == head and _head(path) == worktree_head
    assert _staged(rig.folder) == []
    assert (rig.folder / "a.txt").read_bytes() == before


@needs_git
async def test_push_refuses_trust_revoked_during_lease_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = integration_rig(tmp_path)
    bare = rig.origin()
    head = _head(rig.folder)
    root = rig.integration._resolve_root(rig.workspace_id, *rig.admission(), kind="code")
    with pytest.raises(CodeError, match="^workspace_changed$"):
        await _change_during_wait(
            rig.integration._coordinator,
            root,
            rig.push(head_oid=head),
            lambda: _revoke(rig.workspaces, rig.workspace_id),
            monkeypatch,
        )
    assert _remote_head(bare) is None
    assert _head(rig.folder) == head


@needs_git
async def test_create_discards_its_row_when_trust_is_revoked_during_lease_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = _committed(tmp_path)
    rig = worktree_rig(tmp_path, folder)
    before = _git(folder, "worktree", "list", "--porcelain")
    with pytest.raises(WorktreeError, match="^workspace_changed$"):
        await _change_during_wait(
            rig.coordinator,
            rig.root(),
            rig.create(),
            lambda: _revoke(rig.workspaces, rig.workspace_id),
            monkeypatch,
        )
    assert rig.rows() == [] and rig.folders() == []
    assert _git(folder, "worktree", "list", "--porcelain") == before
    assert _git(folder, "branch", "--list", "viki/20261003-ab12") == ""


@needs_git
@pytest.mark.parametrize("state", ["ready", "kept"])
async def test_remove_puts_its_state_back_when_trust_is_revoked_during_lease_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    rig = removal_rig(tmp_path)
    worktree = await rig.create()
    if state == "kept":
        rig.force(worktree.worktree_id, state)
    before = rig.listed()
    with pytest.raises(WorktreeError, match="^workspace_changed$"):
        await _change_during_wait(
            rig.coordinator,
            rig.root(),
            rig.remove(worktree.worktree_id),
            lambda: _revoke(rig.workspaces, rig.workspace_id),
            monkeypatch,
        )
    assert rig.state(worktree.worktree_id) == state
    assert Path(worktree.path).is_dir() and rig.listed() == before
    assert not rig.roots.blocked(worktree.worktree_id)


@needs_git
@pytest.mark.parametrize("in_worktree", [False, True])
async def test_commit_refuses_a_session_that_becomes_busy_during_lease_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, in_worktree: bool
) -> None:
    rig = checkout_rig(tmp_path)
    worktree_id, path = rig.worktree() if in_worktree else (None, rig.folder)
    (path / "a.txt").write_text("approved\n", encoding="utf-8")
    approval = _approval(rig, "a.txt", worktree_id=worktree_id)
    head = _head(path)
    before = (path / "a.txt").read_bytes()
    rig.store.put_session_root(
        {
            "session_id": SESSION,
            "workspace_id": rig.workspace_id,
            "worktree_id": worktree_id,
            "display_root": str(path),
            "created_at": STAMP,
        }
    )
    busy: set[str] = set()
    committer = CodeCommitter(
        CodeReview(rig.store, rig.git),
        rig.git,
        rig.store,
        rig.coordinator,
        rig.resolver,
        busy=busy.__contains__,
    )
    workspace = rig.workspaces.get(rig.workspace_id, kind="code")
    with pytest.raises(CodeError, match="^checkout_busy$"):
        await _change_during_wait(
            rig.coordinator,
            rig.admit(),
            committer.commit(
                rig.workspace_id,
                worktree_id,
                workspace.policy_revision,
                workspace.root.physical_id,
                BRANCH if in_worktree else "main",
                head,
                "change",
                approval,
            ),
            lambda: busy.add(SESSION),
            monkeypatch,
        )
    assert _head(path) == head and _staged(path) == []
    assert (path / "a.txt").read_bytes() == before
    assert SESSION in busy


@needs_git
async def test_integrate_refuses_a_session_that_becomes_busy_during_lease_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = integration_rig(tmp_path)
    _, path = rig.worktree()
    head, worktree_head = _head(rig.folder), _head(path)
    before = (rig.folder / "a.txt").read_bytes()
    rig.store.put_session_root(
        {
            "session_id": SESSION,
            "workspace_id": rig.workspace_id,
            "worktree_id": None,
            "display_root": str(rig.folder),
            "created_at": STAMP,
        }
    )
    root = rig.integration._resolve_root(rig.workspace_id, *rig.admission(), kind="code")
    with pytest.raises(CodeError, match="^checkout_busy$"):
        await _change_during_wait(
            rig.integration._coordinator,
            root,
            rig.integrate(target_oid=head, head_oid=worktree_head),
            lambda: rig.busy.add(SESSION),
            monkeypatch,
        )
    assert _head(rig.folder) == head and _head(path) == worktree_head
    assert _staged(rig.folder) == []
    assert (rig.folder / "a.txt").read_bytes() == before
    assert SESSION in rig.busy
