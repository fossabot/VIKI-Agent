"""A Bot code copy that never completed does not strand its chat or Bot thread: real profile
preparation, store, services and Git in synthetic folders. A reservation a crash cut short is
forgotten with its `creating` row at recovery, a removed copy no session started in lets the
next first send make a new one while a started owner still refuses, and a Bot deleted or
regranted before the reservation's transaction binds nothing."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.messaging.test_bots import _NOW
from tests.profiles.test_bot_memory import rig as rig
from tests.profiles.test_profile_launch import setup as setup
from tests.workspace_code.test_bot_code_create import Copies
from tests.workspace_code.test_bot_code_create import copies as copies
from tests.workspace_code.test_code_git import _commit, _repo
from tests.workspace_code.test_workspaces_code import trusted
from tests.workspace_code.test_worktrees import (
    _empty_git_environment,  # noqa: F401 (autouse: no developer Git config)
    needs_git,
)
from viki_agent.automation.routines import RoutineService
from viki_agent.code.bot_code import MSG_FAILED, MSG_UNAVAILABLE, _Make
from viki_agent.messaging.bots import BotService

pytestmark = needs_git


def _owners(copies: Copies, target: str) -> tuple[list[str], _Make]:
    """The sessions that share the copy `target` names, and the copy to make for them."""
    if target != "thread":
        session_id = copies.prepare(copies.chat if target == "main" else None)
        return [session_id], _Make(copies.bot_id, copies.folder, session_id, None)
    main, checker = (copies.bot_id, copies.pid), copies.other_bot(copies.folder)
    members = [copies.prepare(bot=main), copies.prepare(bot=checker)]
    thread_id = copies.thread([(main, members[0]), (checker, members[1])])
    return members, _Make(copies.bot_id, copies.folder, None, thread_id)


@pytest.mark.parametrize("target", ["main", "task", "thread"])
async def test_a_reservation_cut_short_is_forgotten_at_recovery(
    copies: Copies, target: str
) -> None:
    members, make = _owners(copies, target)
    # The app stops after the reservation, before the lease and the add made any folder.
    assert copies.code._reserve(make, copies.worktrees) is not None
    assert [row["state"] for row in copies.rows()] == ["creating"]
    assert [binding["state"] for binding in copies.bindings()] == ["bound"]
    await copies.worktrees.recover()
    assert copies.rows() == [] and copies.bindings() == []
    # The next first send makes a new copy, shared by every member of a Bot thread.
    made = [await copies.resolve(session_id) for session_id in members]
    assert made[0] is not None and all(copy == made[0] for copy in made)
    (row,) = copies.rows()
    assert row["state"] == "ready" and Path(made[0].path).name == row["worktree_id"]


@pytest.mark.parametrize("owner", ["chat", "started_chat", "thread", "started_thread"])
async def test_removing_a_copy_a_failed_add_left_frees_an_owner_that_never_started(
    copies: Copies, monkeypatch: pytest.MonkeyPatch, owner: str
) -> None:
    members, _make = _owners(copies, "thread" if "thread" in owner else "task")
    add = copies.worktrees._git_add

    def failing(root: Path, row: Any, hooks: bool) -> None:
        add(root, row, hooks)  # the tree is made, then the add reports a failure
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(copies.worktrees, "_git_add", failing)
    await copies.refused(members[0], MSG_FAILED)
    monkeypatch.setattr(copies.worktrees, "_git_add", add)
    (left,) = copies.rows()
    assert left["state"] == "kept"
    if owner.startswith("started"):
        copies.sql(
            "UPDATE session_profile_bindings SET native_started=1 WHERE session_id=?", members[-1]
        )
    folder = copies.workspaces.get(copies.folder, kind="code")
    removal = await copies.worktrees.remove(
        left["worktree_id"], folder.policy_revision, folder.root.physical_id
    )
    assert removal.outcome == "removed"
    if owner.startswith("started"):
        # Claude Code may have run with that copy: never silently another one.
        assert [binding["worktree_id"] for binding in copies.bindings()] == [left["worktree_id"]]
        for session_id in members:
            await copies.refused(session_id, MSG_UNAVAILABLE)
        assert [row["state"] for row in copies.rows()] == ["removed"]
        return
    assert copies.bindings() == []
    made = [await copies.resolve(session_id) for session_id in members]
    assert made[0] is not None and all(copy == made[0] for copy in made)
    assert Path(made[0].path).name != left["worktree_id"]
    assert sorted(row["state"] for row in copies.rows()) == ["ready", "removed"]


def _delete_bot(copies: Copies) -> None:
    profiles = copies.launch.profiles
    routines = RoutineService(copies.store, profiles, None, copies.workspaces, now=lambda: _NOW)
    BotService(copies.store, profiles, routines, copies.workspaces, now=lambda: _NOW).remove(
        copies.bot_id, 1
    )


@pytest.mark.parametrize("change", ["bot_deleted", "cleared", "switched"])
async def test_a_grant_changed_before_the_reservation_binds_nothing(
    copies: Copies, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    session_id = copies.prepare()
    insert = copies.store.insert_bot_copy

    def changed_first(*args: Any, **kwargs: Any) -> None:
        # After `_reserve` read the grant, before its transaction.
        if change == "bot_deleted":
            _delete_bot(copies)
        elif change == "cleared":
            copies.grant(copies.bot_id, None)
        else:
            other = _repo(copies.tmp_path / "repo2")
            (other / "c.txt").write_text("gamma\n", encoding="utf-8")
            _commit(other, "first")
            copies.grant(copies.bot_id, trusted(copies.workspaces, other))
        insert(*args, **kwargs)

    monkeypatch.setattr(copies.store, "insert_bot_copy", changed_first)
    # As when the grant changed before the read: no copy is made or bound.
    assert await copies.resolve(session_id) is None
    assert copies.rows() == [] and copies.bindings() == []
