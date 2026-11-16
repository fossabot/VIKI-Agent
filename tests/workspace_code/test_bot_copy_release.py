"""`WorktreeService.release_copies`: a Bot copy released by deleting its owner (the Bot, its Bot
thread, its chat's label, a command reset or the code folder's registration) is removed when
merged, else archived with its branch kept, at recovery and at the start of every sweep, cleanup
on or off. Anything locked, kept, live, changed since its inspection, holding a nested
repository or under changed Git settings stays pending, logged by code only; the release
finishes once the row is `removed`, its binding kept as `former` (AC-5, AC-6). Real services on
one store, with the one CodeGit the server builds and the folder's baseline (`_rig`), real Git in
synthetic folders; E13's server wiring is absent, so each test calls `release_copies` where the
server would."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import uuid
from pathlib import Path
from typing import Any

import pytest

from tests.messaging.test_bot_code_folder import _group
from tests.messaging.test_bots import _NOW, _STAMP, _bind_chat, _bot
from tests.persistence.test_storage_bot_code import _binding, _id, _insert
from tests.persistence.test_storage_bot_code_queries import OWNER, _command
from tests.workspace_code.test_code_git import _commit, _git
from tests.workspace_code.test_code_git_nested import _nested, _plant
from tests.workspace_code.test_worktree_sweep import (
    HANG_GUARD_S,
    PAST,
    Rig,
    _age,
    _archive,
    _branches,
    _verdicts,
    baseline,
    change_settings,
)
from tests.workspace_code.test_worktree_sweep import _rig as _sweep_rig
from tests.workspace_code.test_worktrees import (
    _empty_git_environment,  # noqa: F401 (autouse: no developer Git config)
    needs_git,
)
from viki_agent.app.data_api import ResetService
from viki_agent.automation.routines import RoutineService
from viki_agent.code import worktrees
from viki_agent.code.code_git import CodeGit
from viki_agent.code.worktree_rules import Facts
from viki_agent.code.worktrees import Worktree, WorktreeError
from viki_agent.conversations.session_metadata import SessionMetadataService
from viki_agent.domain import ActiveWorkspace
from viki_agent.messaging.bot_threads import BotThreadService
from viki_agent.messaging.bots import BotService
from viki_agent.profiles.service import ProfileService
from viki_agent.session import now_iso
from viki_agent.storage import StateError

COPY_BRANCH = "viki/bot-20261110-ab12cd"


def _rig(tmp_path: Path, **options: Any) -> Rig:
    """The sweep's rig as the server runs it: one CodeGit that knows the store's Bot copies, and
    the folder's baseline saved before its first copy, as `BotCode` saves it. Without them no
    command here would run as it does in a Bot copy: hooks and filter programs off, outside
    nested repositories."""
    rig = _sweep_rig(tmp_path, **options)
    baseline(rig)
    return rig


def _bots(rig: Rig) -> BotService:
    profiles = ProfileService(rig.store)
    routines = RoutineService(rig.store, profiles, None, rig.workspaces, now=lambda: _NOW)
    return BotService(rig.store, profiles, routines, rig.workspaces, now=lambda: _NOW)


async def _copy(
    rig: Rig,
    bot: dict[str, Any],
    *,
    session_id: str | None = None,
    thread_id: str | None = None,
    branch: str = COPY_BRANCH,
) -> Worktree:
    """A hooks-off worktree bound to `bot`'s chat session or Bot thread, as `BotCode` binds it."""
    workspace = rig.workspaces.get(rig.workspace_id, kind="code")
    worktree = await rig.service.create(
        rig.workspace_id,
        workspace.policy_revision,
        workspace.root.physical_id,
        branch,
        hooks=False,
    )
    with rig.store._connection(write=True) as connection:
        _insert(
            connection,
            "bot_code_copies",
            {
                "worktree_id": worktree.worktree_id,
                "workspace_id": rig.workspace_id,
                "bot_id": bot["bot_id"],
                "session_id": session_id,
                "thread_id": thread_id,
                "state": "bound",
                "created_at": _STAMP,
            },
        )
    return worktree


def _binding_state(rig: Rig, worktree: Worktree) -> str | None:
    found = rig.store.copy_states([worktree.worktree_id]).get(worktree.worktree_id)
    return found["state"] if found is not None else None


def _task_session(rig: Rig, bot: dict[str, Any], number: int) -> str:
    """A task chat of `bot`'s vault with its profile binding, as a task launch records it."""
    with rig.store._connection(write=True) as connection:
        connection.execute(
            "INSERT INTO sessions VALUES (?,'plan','high',NULL,'profile')", (_id(number),)
        )
        _binding(connection, number, workspace_id=bot["workspace_id"])
    return _id(number)


def _scope(rig: Rig, workspace_id: str) -> str:
    row = rig.store.get_workspace(workspace_id)
    assert row is not None
    return hashlib.sha256(row["canonical_key"].encode("utf-8")).hexdigest()


def _commands_reset(rig: Rig, vault_id: str, tmp_path: Path) -> ResetService:
    vault = rig.workspaces.get(vault_id)
    active = ActiveWorkspace(vault, Path(vault.root.display), vault_id, "registration", False)
    return ResetService(
        store=rig.store,
        active_workspace=lambda: active,
        settings_reset=lambda _command_id: None,
        scratch_root=tmp_path / "scratch",
        scratch_in_use=lambda: False,
        owner=OWNER,
    )


async def _released(rig: Rig, **copy: Any) -> Worktree:
    """A copy bound to a new Bot's main chat, released by removing that Bot."""
    bots = _bots(rig)
    bot = _bot(bots)
    worktree = await _copy(rig, bot, session_id=bot["chat_session_id"], **copy)
    bots.remove(bot["bot_id"], 1)
    assert _binding_state(rig, worktree) == "released"
    return worktree


def _assert_pending(rig: Rig, worktree: Worktree, state: str = "ready") -> None:
    """The copy as the release found it: tree, row, registration, branch and binding."""
    assert Path(worktree.path).is_dir()
    assert rig.state(worktree.worktree_id) == state
    assert worktree.branch in _branches(rig)
    assert _binding_state(rig, worktree) == "released"
    assert not rig.roots.blocked(worktree.worktree_id)


def _assert_code_only(caplog: pytest.LogCaptureFixture, worktree: Worktree, tmp_path: Path) -> None:
    for secret in (worktree.path, worktree.worktree_id, worktree.branch, str(tmp_path)):
        assert secret not in caplog.text


# ---- every owner deletion, then release ------------------------------------------------------


TRIGGERS = ["bot_main", "bot_task", "bot_reset", "bot_thread", "chat_label", "commands_reset"]


@needs_git
@pytest.mark.parametrize("trigger", TRIGGERS)
async def test_each_owner_deletion_releases_its_copy_and_release_removes_it_when_merged(
    tmp_path: Path, trigger: str
) -> None:
    """The copy is bound and kept until its owner goes; a just-made (not idle) merged copy is
    then removed by the next release, its branch deleted as the sweep deletes one, its release
    finished."""
    rig = _rig(tmp_path)
    bots = _bots(rig)
    bot = _bot(bots)
    vault = bot["workspace_id"]
    session_id = thread_id = None
    if trigger == "bot_main":
        session_id = bot["chat_session_id"]
    elif trigger == "bot_thread":
        thread_id = _group(bots, vault, [bot["chat_session_id"]])
    elif trigger == "chat_label":
        _bind_chat(bots, bot)
        session_id = bot["chat_session_id"]
    else:
        session_id = _task_session(rig, bot, 5)
    worktree = await _copy(rig, bot, session_id=session_id, thread_id=thread_id)

    assert await rig.service.release_copies() == []
    assert _binding_state(rig, worktree) == "bound"
    assert rig.state(worktree.worktree_id) == "ready"

    if trigger in ("bot_main", "bot_task"):
        bots.remove(bot["bot_id"], 1)
    elif trigger == "bot_reset":
        bots.reset(vault)
    elif trigger == "bot_thread":
        assert thread_id is not None
        BotThreadService(rig.store, rig.workspaces).remove(vault, thread_id, 1)
    elif trigger == "chat_label":
        assert session_id is not None
        SessionMetadataService(rig.store, _scope(rig, vault)).delete(session_id, 0)
    else:
        _command(rig.store, 300, 5, scope=_scope(rig, vault))
        reset = _commands_reset(rig, vault, tmp_path).reset(
            command_id=_id(301), categories=["commands"]
        )
        assert reset["results"] == {"commands": {"status": "done", "count": 1}}
    assert _binding_state(rig, worktree) == "released"

    decisions = await rig.service.release_copies()

    assert _verdicts(decisions) == {worktree.worktree_id: ("remove", "merged")}
    assert rig.state(worktree.worktree_id) == "removed"
    assert not Path(worktree.path).exists()
    assert worktree.branch not in _branches(rig)
    assert _binding_state(rig, worktree) == "former"
    assert list(rig.area.iterdir()) == []


@needs_git
async def test_unregistering_the_folder_finishes_a_copy_the_user_already_removed(
    tmp_path: Path,
) -> None:
    rig = _rig(tmp_path)
    bot = _bot(_bots(rig))
    worktree = await _copy(rig, bot, session_id=bot["chat_session_id"])
    workspace = rig.workspaces.get(rig.workspace_id, kind="code")
    removal = await rig.service.remove(
        worktree.worktree_id, workspace.policy_revision, workspace.root.physical_id
    )
    assert removal.outcome == "removed"
    assert _binding_state(rig, worktree) == "bound"  # a removal by hand is not a release

    rig.workspaces.remove(rig.workspace_id)
    assert _binding_state(rig, worktree) == "released"

    assert await rig.service.release_copies() == []
    assert _binding_state(rig, worktree) == "former"


# ---- bound copies stay, cleanup on or off -------------------------------------------------------


@needs_git
async def test_archiving_a_chat_keeps_its_idle_merged_copy_bound_through_every_sweep(
    tmp_path: Path,
) -> None:
    rig = _rig(tmp_path)
    bots = _bots(rig)
    bot = _bot(bots)
    _bind_chat(bots, bot)
    chat = bot["chat_session_id"]
    worktree = await _copy(rig, bot, session_id=chat)
    _age(Path(worktree.path), PAST)
    SessionMetadataService(rig.store, _scope(rig, bot["workspace_id"])).patch(
        chat, 0, {"archived": True}
    )

    assert _verdicts(await rig.service.sweep()) == {worktree.worktree_id: ("keep", "bot_chat")}

    assert _binding_state(rig, worktree) == "bound"
    assert rig.state(worktree.worktree_id) == "ready" and Path(worktree.path).is_dir()


@needs_git
async def test_with_cleanup_off_the_sweep_still_releases_and_keeps_bound_copies(
    tmp_path: Path,
) -> None:
    rig = _rig(tmp_path, enabled=lambda: False)
    bots = _bots(rig)
    kept_bot, gone_bot = _bot(bots), _bot(bots)
    kept = await _copy(rig, kept_bot, session_id=kept_bot["chat_session_id"], branch="kept")
    gone = await _copy(rig, gone_bot, session_id=gone_bot["chat_session_id"], branch="gone")
    _age(Path(kept.path), PAST)
    bots.remove(gone_bot["bot_id"], 1)

    assert await rig.service.sweep() == []

    assert rig.state(gone.worktree_id) == "removed" and not Path(gone.path).exists()
    assert _binding_state(rig, gone) == "former"
    assert rig.state(kept.worktree_id) == "ready" and Path(kept.path).is_dir()
    assert _binding_state(rig, kept) == "bound"


# ---- changed or unintegrated work is archived, branch kept; restore and the cap ---------------


@needs_git
async def test_a_released_copy_with_changes_is_archived_and_restores_within_the_cap(
    tmp_path: Path,
) -> None:
    rig = _rig(tmp_path)
    worktree = await _released(rig)
    tree = Path(worktree.path)
    (tree / "a.txt").write_text("edited\n", encoding="utf-8")
    (tree / "new.txt").write_text("new\n", encoding="utf-8")
    fillers = [_id(500 + n) for n in range(worktrees.MAX_WORKTREES - 1)]
    for filler in fillers:
        rig.insert_ready(filler, path=rig.area / filler)
    with pytest.raises(StateError) as full:
        rig.insert_ready(_id(600), path=rig.area / _id(600))
    assert full.value.code == "capacity"

    decisions = await rig.service.release_copies()

    assert _verdicts(decisions) == {worktree.worktree_id: ("archive", "dirty")}
    assert rig.state(worktree.worktree_id) == "removed" and not tree.exists()
    assert worktree.branch in _branches(rig)  # an archived copy keeps its branch
    archive = _archive(rig, worktree.worktree_id)
    assert _git(rig.repo, "show", f"{archive}:a.txt") == "edited\n"
    assert _binding_state(rig, worktree) == "former"
    assert [a.worktree_id for a in rig.service.archives(rig.workspace_id)] == [worktree.worktree_id]
    rig.insert_ready(_id(600), path=rig.area / _id(600))  # the archive freed its slot

    workspace = rig.workspaces.get(rig.workspace_id, kind="code")
    pinned = (workspace.policy_revision, workspace.root.physical_id)
    with pytest.raises(WorktreeError, match="worktree_limit"):
        await rig.service.restore(worktree.worktree_id, *pinned)
    rig.store.update_worktree_state(fillers[0], "ready", "removed", now_iso())
    restored = await rig.service.restore(worktree.worktree_id, *pinned)

    assert restored.state == "ready" and restored.branch == worktree.branch
    assert (tree / "a.txt").read_text(encoding="utf-8") == "edited\n"
    assert (tree / "new.txt").read_text(encoding="utf-8") == "new\n"
    assert _binding_state(rig, worktree) == "former"  # still a Bot copy to Git
    assert await rig.service.release_copies() == []
    assert rig.state(worktree.worktree_id) == "ready"


@needs_git
async def test_an_unintegrated_commit_is_archived_and_its_branch_kept(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    worktree = await _released(rig)
    tree = Path(worktree.path)
    (tree / "work.txt").write_text("work\n", encoding="utf-8")
    _commit(tree, "work")
    commit = _git(tree, "rev-parse", "HEAD").strip()

    decisions = await rig.service.release_copies()

    assert _verdicts(decisions) == {worktree.worktree_id: ("archive", "unpushed")}
    assert rig.state(worktree.worktree_id) == "removed" and not tree.exists()
    assert _git(rig.repo, "rev-parse", f"refs/heads/{worktree.branch}").strip() == commit
    assert _git(rig.repo, "rev-parse", f"{_archive(rig, worktree.worktree_id)}^1").strip() == (
        commit
    )
    assert _binding_state(rig, worktree) == "former"


# ---- pending: live, locked, kept, stale, changed settings, cancelled ----------------------------


@needs_git
async def test_a_live_released_copy_stays_pending_and_goes_on_a_later_round(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    live: set[str] = set()
    rig = _rig(tmp_path, in_use=lambda worktree_id: worktree_id in live)
    worktree = await _released(rig)
    live.add(worktree.worktree_id)
    caplog.set_level(logging.WARNING)

    assert _verdicts(await rig.service.release_copies()) == {worktree.worktree_id: ("keep", "live")}
    _assert_pending(rig, worktree)
    assert "worktree: release kept a copy (live)" in caplog.text
    _assert_code_only(caplog, worktree, tmp_path)

    live.clear()
    assert _verdicts(await rig.service.release_copies()) == {
        worktree.worktree_id: ("remove", "merged")
    }
    assert _binding_state(rig, worktree) == "former"


@needs_git
@pytest.mark.parametrize("hold", ["locked", "kept", "branch_changed"])
async def test_a_locked_kept_or_switched_copy_stays_pending_until_it_can_go(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, hold: str
) -> None:
    rig = _rig(tmp_path)
    worktree = await _released(rig)
    state = "kept" if hold == "kept" else "ready"
    if hold == "locked":
        _git(rig.repo, "worktree", "lock", worktree.path)
    elif hold == "kept":
        rig.store.update_worktree_state(worktree.worktree_id, "ready", "kept", now_iso())
    else:
        _git(Path(worktree.path), "checkout", "-q", "-b", "other")
    caplog.set_level(logging.WARNING)

    decisions = await rig.service.release_copies()

    assert _verdicts(decisions) == (
        {} if hold == "kept" else {worktree.worktree_id: ("keep", hold)}
    )
    _assert_pending(rig, worktree, state)
    assert f"({hold})" in caplog.text
    _assert_code_only(caplog, worktree, tmp_path)

    if hold == "locked":
        _git(rig.repo, "worktree", "unlock", worktree.path)
    elif hold == "kept":
        rig.store.update_worktree_state(worktree.worktree_id, "kept", "ready", now_iso())
    else:
        _git(Path(worktree.path), "checkout", "-q", worktree.branch)
    assert _verdicts(await rig.service.release_copies()) == {
        worktree.worktree_id: ("remove", "merged")
    }
    assert _binding_state(rig, worktree) == "former"


@needs_git
async def test_a_change_after_the_inspection_keeps_the_copy_until_the_next_round(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale inspection: the act's fresh look finds a file written after it, so nothing is
    archived or removed; the next round reads the change and archives it."""
    rig = _rig(tmp_path)
    worktree = await _released(rig)
    real = worktrees.inspect

    def inspect_then_write(git: CodeGit, tree: Path) -> Facts:
        facts = real(git, tree)
        (tree / "late.txt").write_text("late\n", encoding="utf-8")
        return facts

    with monkeypatch.context() as patch:
        patch.setattr(worktrees, "inspect", inspect_then_write)
        decisions = await rig.service.release_copies()

    assert _verdicts(decisions) == {worktree.worktree_id: ("keep", "changed")}
    _assert_pending(rig, worktree)
    assert _archive(rig, worktree.worktree_id) == ""

    assert _verdicts(await rig.service.release_copies()) == {
        worktree.worktree_id: ("archive", "dirty")
    }
    assert _git(rig.repo, "show", f"{_archive(rig, worktree.worktree_id)}:late.txt") == "late\n"
    assert _binding_state(rig, worktree) == "former"


@needs_git
async def test_changed_git_settings_keep_the_copy_pending_until_confirmed(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    rig = _rig(tmp_path)
    worktree = await _released(rig)
    change_settings(rig.repo)
    caplog.set_level(logging.WARNING)

    assert _verdicts(await rig.service.release_copies()) == {
        worktree.worktree_id: ("keep", "unknown")
    }
    _assert_pending(rig, worktree)
    assert "worktree: skipped a folder (git_settings_changed)" in caplog.text
    _assert_code_only(caplog, worktree, tmp_path)

    old = rig.store.bot_code_baseline(rig.workspace_id)
    assert old is not None
    assert rig.store.replace_bot_code_baseline(
        rig.workspace_id, old["git_fingerprint"], rig.git.settings_fingerprint(rig.repo), now_iso()
    )
    assert _verdicts(await rig.service.release_copies()) == {
        worktree.worktree_id: ("remove", "merged")
    }
    assert _binding_state(rig, worktree) == "former"


@needs_git
async def test_a_cancelled_release_puts_the_row_back_and_recovery_releases_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _rig(tmp_path)
    worktree = await _released(rig)
    real = rig.coordinator.lease
    waiting = asyncio.Event()

    def lease(*args: Any, **kwargs: Any) -> Any:
        waiting.set()
        return real(*args, **kwargs)

    async with rig.coordinator.lease(rig.root(), ()):
        monkeypatch.setattr(rig.coordinator, "lease", lease)
        task = asyncio.create_task(rig.service.release_copies())
        await asyncio.wait_for(waiting.wait(), HANG_GUARD_S)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    monkeypatch.undo()

    _assert_pending(rig, worktree)

    await rig.service.recover()

    assert rig.state(worktree.worktree_id) == "removed" and not Path(worktree.path).exists()
    assert _binding_state(rig, worktree) == "former"


@needs_git
async def test_recovery_settles_a_release_cut_short_after_the_unregister(tmp_path: Path) -> None:
    """A crash after the tree was unregistered and deleted, before its row was recorded:
    recovery marks the row `removed`, then its release finishes."""
    rig = _rig(tmp_path)
    worktree = await _released(rig)
    _git(rig.repo, "worktree", "remove", "--force", worktree.path)
    rig.store.update_worktree_state(worktree.worktree_id, "ready", "removing", now_iso())

    await rig.service.recover()

    assert rig.state(worktree.worktree_id) == "removed"
    assert _binding_state(rig, worktree) == "former"


# ---- a nested repository: kept, with no Git started inside it -----------------------------------


async def _nesting(rig: Rig, how: str, marker: Path) -> Worktree:
    """A released copy holding a nested repository whose own config touches `marker` when Git
    works inside it: `populated`, at the commit the copy's entry for it names; `untracked`, one
    the copy's index does not know; or `deleted`, its entry staged for deletion."""
    if how != "untracked":
        _nested(rig.repo / "nested", "folder\n")
        _git(rig.repo, "add", "nested")
        _git(rig.repo, "commit", "-q", "-m", "gitlink")
    worktree = await _released(rig)
    tree = Path(worktree.path)
    if how == "deleted":
        _git(tree, "rm", "-q", "nested")
        return worktree
    if how == "populated":
        (tree / "nested").rmdir()
        source = str(rig.repo / "nested")
        _git(tree, "-c", "protocol.file.allow=always", "clone", "-q", source, "nested")
        assert _git(tree / "nested", "rev-parse", "HEAD") == _git(tree, "rev-parse", "HEAD:nested")
    else:
        _nested(tree / "nested", "copy\n")
    _plant(tree / "nested", "filter", marker)
    return worktree


@needs_git
@pytest.mark.parametrize("act", ["release", "plan"])
@pytest.mark.parametrize(
    ("how", "reason"),
    [("populated", "submodule"), ("untracked", "embedded_repo"), ("deleted", "submodule")],
)
async def test_a_released_copy_with_a_nested_repository_is_kept_and_git_stays_out_of_it(
    tmp_path: Path, how: str, reason: str, act: str
) -> None:
    """An archive cannot hold a nested repository, and its own config is the Bot's to write. A
    staged deletion leaves no entry in the index: only the status row shows it."""
    rig = _rig(tmp_path)
    marker = tmp_path / "nested-git-ran"
    worktree = await _nesting(rig, how, marker)

    decisions = await (rig.service.release_copies() if act == "release" else rig.service.plan())

    assert _verdicts(decisions) == {worktree.worktree_id: ("keep", reason)}
    _assert_pending(rig, worktree)
    assert _archive(rig, worktree.worktree_id) == ""
    assert not marker.exists()


@needs_git
async def test_removing_a_copy_with_a_nested_entry_staged_for_deletion_reports_the_change(
    tmp_path: Path,
) -> None:
    rig = _rig(tmp_path)
    worktree = await _nesting(rig, "deleted", tmp_path / "unused")
    workspace = rig.workspaces.get(rig.workspace_id, kind="code")

    removal = await rig.service.remove(
        worktree.worktree_id, workspace.policy_revision, workspace.root.physical_id
    )

    assert (removal.outcome, removal.reason, removal.changes) == ("kept", "dirty", 1)
    _assert_pending(rig, worktree)


# ---- a folder the release renamed aside and could not delete -----------------------------------


@needs_git
async def test_a_folder_a_release_could_not_delete_goes_at_the_next_sweep_with_cleanup_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _rig(tmp_path, enabled=lambda: False)
    worktree = await _released(rig)
    with monkeypatch.context() as patch:
        patch.setattr(worktrees, "_delete_trash", lambda trash: None)  # a delete that failed
        assert _verdicts(await rig.service.release_copies()) == {
            worktree.worktree_id: ("remove", "merged")
        }
    trash = rig.area / f".trash-{worktree.worktree_id}"
    assert trash.is_dir() and rig.state(worktree.worktree_id) == "removed"

    assert await rig.service.sweep() == []

    assert list(rig.area.iterdir()) == []


@needs_git
async def test_a_sweep_leaves_the_folder_of_a_removal_still_under_way(tmp_path: Path) -> None:
    """A release runs beside the sweep's loop, and until its `git worktree remove` answers it
    may still rename the folder back."""
    rig = _rig(tmp_path, enabled=lambda: False)
    worktree_id = str(uuid.uuid4())
    trash = rig.area / f".trash-{worktree_id}"
    trash.mkdir(parents=True)
    rig.service._removing.add(worktree_id)  # as `_sweep_row` holds it for the whole act

    assert await rig.service.sweep() == []
    assert trash.is_dir()

    rig.service._removing.discard(worktree_id)
    assert await rig.service.sweep() == []
    assert not trash.exists()
