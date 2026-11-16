"""A Bot copy's release finishes with its binding kept as `former`, so a tree restored from its
archive is still a Bot copy to Git: in a folder whose relative `core.hooksPath` (husky) points
into each worktree, no hook the Bot wrote runs from the restored tree or, after integrating it,
in the main folder. An ordinary worktree's restore keeps its own hooks, and a former copy is no
Bot's anywhere else: not pending, not listed as a Bot's, open to a chat. Real services on one
store, real Git in synthetic folders."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.messaging.test_bots import _bot
from tests.workspace_code.test_bot_copy_release import _binding_state, _bots, _copy, _released
from tests.workspace_code.test_code_git import _git
from tests.workspace_code.test_git_guard import isolated_git  # noqa: F401 (autouse)
from tests.workspace_code.test_worktree_sweep import PAST, Rig, _age, _rig, _verdicts, baseline
from tests.workspace_code.test_worktrees import (
    _empty_git_environment,  # noqa: F401 (autouse: no developer Git config)
    needs_git,
)
from viki_agent.code.bot_code import BotCode
from viki_agent.code.code_integration import CodeIntegration
from viki_agent.code.worktrees import Worktree
from viki_agent.messaging.bot_threads import BotThreadService
from viki_agent.profiles.service import ProfileService

HOOKS = ("pre-commit", "post-commit", "reference-transaction", "post-checkout", "post-merge")
SESSION = "00000000-0000-4000-8000-0000000000f1"
THREAD = "00000000-0000-4000-8000-0000000000f2"


def _plant(folder: Path, marker: Path) -> None:
    """Hooks that append their name to `marker` when Git runs them."""
    folder.mkdir(parents=True, exist_ok=True)
    for name in HOOKS:
        hook = folder / name
        hook.write_bytes(f"#!/bin/sh\necho {name} >> '{marker.as_posix()}'\n".encode())
        hook.chmod(0o755)


def _husky(tmp_path: Path) -> Rig:
    """A folder whose hooks are each worktree's own `.husky/_`, as husky sets it."""
    rig = _rig(tmp_path)
    _git(rig.repo, "config", "core.hooksPath", ".husky/_")
    husky = rig.repo / ".husky" / "_"
    husky.mkdir(parents=True)
    (husky / ".gitignore").write_text("*\n", encoding="utf-8")  # husky's own, untracked
    return rig


def _pinned(rig: Rig) -> tuple[int, str]:
    workspace = rig.workspaces.get(rig.workspace_id, kind="code")
    return workspace.policy_revision, workspace.root.physical_id


async def _restored(tmp_path: Path, how: str) -> tuple[Rig, Worktree, Path]:
    """A Bot copy that wrote hooks into its own `.husky/_` (committed, or left untracked), its
    Bot deleted, its release archiving it, and the user restoring that archive."""
    rig = _husky(tmp_path)
    worktree = await _released(rig)
    baseline(rig)
    tree = Path(worktree.path)
    ran = tmp_path / "bot-hook-ran"
    _plant(tree / ".husky" / "_", ran)
    if how == "tracked":
        quiet = ("-c", f"core.hooksPath={tmp_path / 'no-hooks'}")
        _git(tree, *quiet, "add", "-f", ".husky/_")
        _git(tree, *quiet, "commit", "-q", "-m", "bot work")
    reason = "unpushed" if how == "tracked" else "dirty"
    assert _verdicts(await rig.service.release_copies()) == {
        worktree.worktree_id: ("archive", reason)
    }
    assert not tree.exists()

    restored = await rig.service.restore(worktree.worktree_id, *_pinned(rig))

    assert restored.state == "ready"
    assert (tree / ".husky" / "_" / "reference-transaction").is_file()
    assert not ran.exists()
    return rig, worktree, ran


@needs_git
@pytest.mark.parametrize("how", ["tracked", "untracked"])
async def test_a_restored_bot_copy_runs_no_hook_of_its_own(tmp_path: Path, how: str) -> None:
    rig, worktree, ran = await _restored(tmp_path, how)
    tree = Path(worktree.path)

    rig.git.git(tree, "update-ref", "refs/heads/after-restore", "HEAD")
    (tree / "user.txt").write_text("user\n", encoding="utf-8")
    rig.git.output(tree, "add", "user.txt")  # as `CodeCommit` lands a commit
    rig.git.output(tree, "commit", "--message=user commit")

    assert _git(tree, "log", "-1", "--format=%s").strip() == "user commit"
    assert not ran.exists()
    assert _binding_state(rig, worktree) == "former"


@needs_git
async def test_integrating_a_restored_bot_copy_runs_no_hook_in_the_main_folder(
    tmp_path: Path,
) -> None:
    """The fast-forward writes the Bot's tracked hooks into the main folder's `.husky/_`, where
    the merge would run them next: it runs with hooks off."""
    rig, worktree, ran = await _restored(tmp_path, "tracked")
    head = _git(Path(worktree.path), "rev-parse", "HEAD").strip()
    target = _git(rig.repo, "rev-parse", "HEAD").strip()
    branch = _git(rig.repo, "branch", "--show-current").strip()
    integration = CodeIntegration(
        rig.git, rig.store, rig.coordinator, rig.resolver, busy=lambda _: False
    )

    found = await integration.integrate(worktree.worktree_id, *_pinned(rig), branch, target, head)

    assert found == head == _git(rig.repo, "rev-parse", "HEAD").strip()
    assert (rig.repo / ".husky" / "_" / "post-merge").is_file()
    assert not ran.exists()


@needs_git
async def test_a_restored_ordinary_worktree_keeps_its_own_hooks(tmp_path: Path) -> None:
    rig = _husky(tmp_path)
    worktree = await rig.create()
    baseline(rig)
    tree = Path(worktree.path)
    (tree / "a.txt").write_text("edited\n", encoding="utf-8")
    _age(tree, PAST)
    assert _verdicts(await rig.service.sweep()) == {worktree.worktree_id: ("archive", "dirty")}

    await rig.service.restore(worktree.worktree_id, *_pinned(rig))

    assert rig.store.copy_states([worktree.worktree_id]) == {}
    ran = tmp_path / "hook-ran"
    _plant(tree / ".husky" / "_", ran)
    rig.git.git(tree, "update-ref", "refs/heads/probe", "HEAD")
    assert ran.read_text(encoding="utf-8").split() == ["reference-transaction"] * 2


@needs_git
async def test_a_former_copy_is_no_bots_copy_outside_git(tmp_path: Path) -> None:
    rig, worktree, _ran = await _restored(tmp_path, "tracked")
    worktree_id = worktree.worktree_id
    bots = _bots(rig)
    other = _bot(bots)
    pending = await _copy(rig, other, session_id=other["chat_session_id"], branch="other")
    bots.remove(other["bot_id"], 1)

    # Not pending: a later release settles only the other Bot's copy; the sweep leaves the
    # restored tree to its ordinary rules.
    assert set(_verdicts(await rig.service.release_copies())) == {pending.worktree_id}
    assert _binding_state(rig, pending) == "former"
    assert worktree_id not in _verdicts(await rig.service.release_copies())
    swept = _verdicts(await rig.service.sweep())
    assert swept[worktree_id][0] == "keep" and rig.state(worktree_id) == "ready"

    # A Bot made again with the copy's Bot id gets no claim on it: listed as no Bot's, its
    # deletion releases nothing, and the tree is open to a chat of the user's.
    former_bot = rig.store.copy_states([worktree_id])[worktree_id]["bot_id"]
    again = _bot(bots, bot_id=former_bot)
    code = BotCode(
        rig.store,
        rig.workspaces,
        rig.roots,
        ProfileService(rig.store),
        BotThreadService(rig.store, rig.workspaces),
        actor=lambda _session_id: None,
        area=rig.area,
    )
    assert code.describe([worktree_id]) == {}
    bots.remove(again["bot_id"], 1)  # still a session copy of that Bot id
    assert _binding_state(rig, worktree) == "former"
    assert await rig.service.release_copies() == []
    assert rig.state(worktree_id) == "ready" and Path(worktree.path).is_dir()
    with rig.store._connection(write=True) as connection:  # as a Bot thread's copy finishes
        connection.execute(
            "UPDATE bot_code_copies SET session_id=NULL,thread_id=? WHERE worktree_id=?",
            (THREAD, worktree_id),
        )
    assert code.describe([worktree_id]) == {}
    root, created = await rig.roots.prepare(SESSION, rig.workspace_id, worktree_id)
    assert created and root.worktree_id == worktree_id

    # Removed by hand like any clean tree; the binding stays, inert without a tree.
    removal = await rig.service.remove(worktree_id, *_pinned(rig))
    assert removal.outcome == "removed" and not Path(worktree.path).exists()
    assert _binding_state(rig, worktree) == "former"
    assert rig.git._guard is not None and not rig.git._guard.bot_copy(Path(worktree.path))
