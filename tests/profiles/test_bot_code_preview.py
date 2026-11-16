"""The session review names the code copy a granted Bot's main chat or task thread works in."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

from tests.profiles.test_bot_code_provenance import grant, main_bot
from tests.profiles.test_bot_memory import rig as rig
from tests.profiles.test_profile_connection_launch import claim
from tests.profiles.test_profile_connections import _pin
from tests.profiles.test_profile_launch import ident
from tests.profiles.test_profile_launch import setup as setup
from viki_agent.messaging.bot_threads import BotThreadService
from viki_agent.profiles.profile_launch import ProfileLaunchService
from viki_agent.session import now_iso

BRANCH = "viki/bot-20261112-abcdef"


class Bot:
    """A Bot with a trusted code folder granted, in the rig's ordinary vault."""

    def __init__(self, rig: Any, tmp_path: Path) -> None:
        self.service: ProfileLaunchService = rig[0]
        self.state, self.vault, self.pid, self.wid = rig[1:]
        self.tmp_path = tmp_path
        self.bot_id, self.chat = main_bot(self.service, self.pid, self.wid)
        self.folder = grant(self.service, self.bot_id, tmp_path / "repo")
        self.name = self.service.workspaces.get(self.folder, kind="code").name

    def prepare(self, session_id: str | None = None, origin: str = "interactive") -> str:
        session_id = session_id or ident()
        self.service.prepare(
            self.pid, ident(), session_id, 1, self.wid, self.bot_id, launch_origin=cast(Any, origin)
        )
        return session_id

    def review(self, session_id: str) -> dict[str, Any]:
        return self.service.inspect_session(session_id)

    def copy(self, session_id: str, *, state: str = "ready", folder: str | None = None) -> str:
        worktree_id, stamp = ident(), now_iso()
        folder = folder or self.folder
        self.service.store.insert_bot_copy(
            {
                "worktree_id": worktree_id,
                "workspace_id": folder,
                "path": str(self.tmp_path / "worktrees" / worktree_id),
                "branch": BRANCH,
                "base_oid": "a" * 40,
                "state": "creating",
                "created_at": stamp,
                "updated_at": stamp,
            },
            {
                "worktree_id": worktree_id,
                "workspace_id": folder,
                "bot_id": self.bot_id,
                "session_id": session_id,
                "thread_id": None,
                "state": "bound",
                "created_at": stamp,
            },
            limit=32,
        )
        if state != "creating":
            self.service.store.update_worktree_state(worktree_id, "creating", state, stamp)
        return worktree_id

    def sql(self, statement: str, *values: object) -> None:
        with self.service.store._connection(write=True) as connection:
            connection.execute(statement, values)


@pytest.fixture
def granted(rig: Any, tmp_path: Path) -> Bot:
    return Bot(rig, tmp_path)


def pending(bot: Bot) -> dict[str, Any]:
    return {"workspace_id": bot.folder, "name": bot.name, "branch": None}


def test_main_chat_and_task_thread_preview_a_copy_before_their_first_send(granted: Bot) -> None:
    for session_id in (granted.chat, ident()):  # the main chat, then a task thread
        review = granted.review(granted.prepare(session_id))
        assert review["launchable"] and review["code_copy"] == pending(granted)


def test_preparation_returns_the_same_preview(granted: Bot) -> None:
    prepared = granted.service.prepare(
        granted.pid, ident(), granted.chat, 1, granted.wid, granted.bot_id,
        launch_origin="interactive",
    )  # fmt: skip
    assert prepared["code_copy"] == pending(granted)


def test_a_bound_ready_copy_previews_its_exact_branch(granted: Bot) -> None:
    session_id = granted.prepare(granted.chat)
    granted.copy(session_id)
    granted.sql(
        "UPDATE session_profile_bindings SET native_started=1 WHERE session_id=?", session_id
    )
    review = granted.review(session_id)
    assert review["launchable"]
    assert review["code_copy"] == {**pending(granted), "branch": BRANCH}


@pytest.mark.parametrize("state", ["creating", "kept", "removing", "removed"])
def test_a_bound_copy_that_is_not_ready_has_no_preview(granted: Bot, state: str) -> None:
    session_id = granted.prepare()
    granted.copy(session_id, state=state)
    assert "code_copy" not in granted.review(session_id)


def test_a_bot_thread_seat_has_no_preview(granted: Bot) -> None:
    session_id = granted.prepare()
    BotThreadService(granted.service.store, granted.service.workspaces).save(
        granted.wid,
        ident(),
        0,
        {
            "title": "Review",
            "phase": "idle",
            "members": [
                {
                    "key": "chief",
                    "bot_id": granted.bot_id,
                    "profile_id": granted.pid,
                    "session_id": session_id,
                    "enabled": True,
                }
            ],
            "messages": [],
        },
    )
    review = granted.review(session_id)
    assert review["launchable"] and "code_copy" not in review


@pytest.mark.parametrize("stamp", ["unknown", "routine"])
def test_unknown_and_routine_origins_have_no_preview_and_keep_launching(
    granted: Bot, stamp: str
) -> None:
    session_id = granted.prepare(granted.chat, origin=stamp)
    review = granted.review(session_id)
    assert review["launchable"] and review["warnings"] == [] and "code_copy" not in review
    granted.service.validate_launch(session_id, granted.state.prefs(session_id))


def test_a_retained_attempt_outranks_an_interactive_marker(granted: Bot) -> None:
    session_id = granted.prepare()
    _tasks, attempt = claim(granted.service, granted.pid, granted.wid)
    granted.sql(
        "UPDATE task_attempts SET session_id=? WHERE attempt_id=?", session_id, attempt.attempt_id
    )
    assert "code_copy" not in granted.review(session_id)


@pytest.mark.parametrize("change", ["unknown", "routine", "attempt", "other_bot"])
def test_a_bound_copy_with_inconsistent_provenance_refuses(granted: Bot, change: str) -> None:
    session_id = granted.prepare()
    granted.copy(session_id)
    if change == "attempt":
        _tasks, attempt = claim(granted.service, granted.pid, granted.wid)
        granted.sql(
            "UPDATE task_attempts SET session_id=? WHERE attempt_id=?",
            session_id,
            attempt.attempt_id,
        )
    elif change == "other_bot":
        granted.sql("UPDATE bot_code_copies SET bot_id=? WHERE session_id=?", ident(), session_id)
    else:
        granted.sql(
            "UPDATE session_profile_bindings SET launch_origin=? WHERE session_id=?",
            change,
            session_id,
        )
    review = granted.review(session_id)
    assert not review["launchable"] and "code_copy" not in review
    assert "launch_changed" in review["warnings"] and "launch_changed" in review["blockers"]


def test_a_plain_profile_session_with_a_bound_copy_refuses(granted: Bot) -> None:
    session_id = ident()
    granted.service.prepare(
        granted.pid, ident(), session_id, 1, granted.wid, launch_origin="interactive"
    )
    granted.copy(session_id)
    assert "launch_changed" in granted.review(session_id)["warnings"]


def test_no_grant_or_an_untrusted_folder_has_no_preview(granted: Bot) -> None:
    session_id = granted.prepare()
    granted.sql("UPDATE workspaces SET policy='protected' WHERE workspace_id=?", granted.folder)
    assert "code_copy" not in granted.review(session_id)
    granted.sql("DELETE FROM bot_code_folders WHERE bot_id=?", granted.bot_id)
    assert "code_copy" not in granted.review(session_id)


def test_a_bound_copy_of_a_folder_no_longer_granted_has_no_preview(granted: Bot) -> None:
    session_id = granted.prepare()
    granted.copy(session_id)
    grant(granted.service, granted.bot_id, granted.tmp_path / "other")
    review = granted.review(session_id)
    assert "code_copy" not in review and review["warnings"] == []


def test_a_started_chat_without_a_copy_gets_none_later(granted: Bot) -> None:
    session_id = granted.prepare()
    granted.sql(
        "UPDATE session_profile_bindings SET native_started=1 WHERE session_id=?", session_id
    )
    assert "code_copy" not in granted.review(session_id)


def test_a_released_copy_is_not_replaced_by_a_pending_one(granted: Bot) -> None:
    session_id = granted.prepare()
    granted.copy(session_id)
    granted.sql("UPDATE bot_code_copies SET state='released' WHERE session_id=?", session_id)
    assert "code_copy" not in granted.review(session_id)


def test_plain_profiles_and_non_ordinary_vaults_have_no_preview(granted: Bot) -> None:
    plain = ident()
    granted.service.prepare(
        granted.pid, ident(), plain, 1, granted.wid, launch_origin="interactive"
    )
    assert "code_copy" not in granted.review(plain)
    granted.sql("UPDATE workspaces SET policy='protected' WHERE workspace_id=?", granted.wid)
    session_id = granted.prepare()
    review = granted.review(session_id)
    assert "code_copy" not in review
    assert review["bot_memory"] == {
        "bot_id": granted.bot_id,
        "path": str(granted.tmp_path / "data" / "bot-memory" / granted.wid / granted.bot_id),
        "routine": False,
    }


def test_a_pinned_connection_and_a_copy_preview_coexist(granted: Bot) -> None:
    ref = _pin(granted.service, granted.pid)
    session_id = ident()
    review = granted.service.prepare(
        granted.pid, ident(), session_id, 2, granted.wid, granted.bot_id,
        launch_origin="interactive",
    )  # fmt: skip
    assert review["launchable"] and review["prefs"]["model"] == ref
    assert review["code_copy"] == pending(granted)


def test_a_connection_pinned_routine_runs_natively_without_a_copy(granted: Bot) -> None:
    _pin(granted.service, granted.pid)
    granted.service.settings.patch(ident(), 1, {"chat.default_model": "sonnet"})
    _tasks, attempt = claim(granted.service, granted.pid, granted.wid)
    review = granted.service.prepare(
        granted.pid, attempt.attempt_id, attempt.session_id, 2, granted.wid, granted.bot_id,
        launch_origin="routine",
    )  # fmt: skip
    assert review["launchable"] and review["prefs"]["model"] == "sonnet"
    assert "code_copy" not in review
