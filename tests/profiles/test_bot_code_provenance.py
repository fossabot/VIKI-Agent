"""A profile binding's launch origin: stamped once by its trusted preparer, never inferred."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

from tests.profiles.test_bot_memory import rig as rig
from tests.profiles.test_profile_connection_launch import NOW, claim, restart
from tests.profiles.test_profile_launch import ident, native_started
from tests.profiles.test_profile_launch import setup as setup
from tests.workspace_code.test_workspaces_code import trusted
from viki_agent.automation import tasks as task_module
from viki_agent.profiles import profile_launch
from viki_agent.profiles.profile_launch import ProfileLaunchService
from viki_agent.profiles.service import ProfileError
from viki_agent.session import now_iso


def main_bot(service: ProfileLaunchService, profile_id: str, workspace_id: str) -> tuple[str, str]:
    """A Bot of `profile_id` in `workspace_id` and the session ID of its main chat."""
    bot_id, chat = ident(), ident()
    with service.store._connection(write=True) as connection:
        connection.execute(
            "INSERT INTO bots VALUES (?,?,?,?,?,1,?,?)",
            (bot_id, profile_id, workspace_id, chat, "a" * 64, NOW, NOW),
        )
    return bot_id, chat


def grant(service: ProfileLaunchService, bot_id: str, root: Path) -> str:
    """Register `root` as a trusted code folder and make it `bot_id`'s code folder."""
    root.mkdir()
    folder = trusted(service.workspaces, root)
    with service.store._connection(write=True) as connection:
        connection.execute("DELETE FROM bot_code_folders WHERE bot_id=?", (bot_id,))
        connection.execute(
            "INSERT INTO bot_code_folders VALUES (?,?,?)", (bot_id, folder, now_iso())
        )
    return folder


def binding(service: ProfileLaunchService, session_id: str) -> dict[str, Any]:
    with service.store._connection() as connection:
        row = profile_launch._one(
            connection, "SELECT * FROM session_profile_bindings WHERE session_id=?", (session_id,)
        )
    assert row is not None
    return row


def origin(service: ProfileLaunchService, session_id: str) -> str | None:
    return service.store.profile_launch_origin(session_id)


def test_default_preparation_stamps_unknown(rig: Any) -> None:
    service, _state, _vault, pid, wid = rig
    sid = ident()
    service.prepare(pid, ident(), sid, 1, wid)
    assert origin(service, sid) == "unknown"


@pytest.mark.parametrize("stamp", ["unknown", "interactive", "routine"])
def test_preparation_stamps_the_requested_origin_with_its_binding(rig: Any, stamp: str) -> None:
    service, _state, _vault, pid, wid = rig
    owner, chat = main_bot(service, pid, wid)
    command = ident()
    prepared = service.prepare(pid, command, chat, 1, wid, owner, launch_origin=cast(Any, stamp))
    assert prepared["session_id"] == chat and prepared["launchable"]
    row = binding(service, chat)
    assert row["launch_origin"] == stamp
    # The request hash keeps its meaning: the origin is not part of the request.
    assert row["request_hash"] == profile_launch._hash([pid, command, chat, 1, wid, owner])
    assert native_started(service, chat) == 0


def test_the_launch_spec_hash_does_not_depend_on_the_origin(rig: Any) -> None:
    service, _state, _vault, pid, wid = rig
    hashes = set()
    for stamp in ("unknown", "interactive", "routine"):
        sid = ident()
        service.prepare(pid, ident(), sid, 1, wid, launch_origin=cast(Any, stamp))
        hashes.add(binding(service, sid)["launch_spec_hash"])
    assert len(hashes) == 1


@pytest.mark.parametrize("bad", ["Interactive", "", "routine ", None, 1, "scheduled"])
def test_an_origin_outside_the_enum_is_refused_before_anything_is_written(
    rig: Any, bad: object
) -> None:
    service, state, _vault, pid, wid = rig
    sid = ident()
    with pytest.raises(ProfileError, match="invalid_input"):
        service.prepare(pid, ident(), sid, 1, wid, launch_origin=cast(Any, bad))
    assert state.store.session_origin(sid) is None and origin(service, sid) is None


@pytest.mark.parametrize("replay", ["unknown", "interactive", "routine"])
def test_a_replay_never_promotes_an_unknown_marker(rig: Any, replay: str) -> None:
    service, _state, _vault, pid, wid = rig
    owner, chat = main_bot(service, pid, wid)
    command = ident()
    prepared = service.prepare(pid, command, chat, 1, wid, owner)
    assert service.prepare(pid, command, chat, 1, wid, owner, launch_origin=cast(Any, replay)) == (
        prepared
    )
    assert origin(service, chat) == "unknown"


@pytest.mark.parametrize(
    ("first", "replay", "conflict"),
    [
        ("interactive", "unknown", False),
        ("interactive", "interactive", False),
        ("interactive", "routine", True),
        ("routine", "unknown", False),
        ("routine", "routine", False),
        ("routine", "interactive", True),
    ],
)
def test_a_replay_keeps_a_known_marker_and_refuses_the_other_known_origin(
    rig: Any, first: str, replay: str, conflict: bool
) -> None:
    service, _state, _vault, pid, wid = rig
    owner, chat = main_bot(service, pid, wid)
    command = ident()
    prepared = service.prepare(pid, command, chat, 1, wid, owner, launch_origin=cast(Any, first))
    again = lambda: service.prepare(  # noqa: E731
        pid, command, chat, 1, wid, owner, launch_origin=cast(Any, replay)
    )
    if conflict:
        with pytest.raises(ProfileError, match="command_conflict"):
            again()
    else:
        assert again() == prepared
    assert origin(service, chat) == first


def test_a_replay_that_loses_the_write_race_still_refuses_the_other_known_origin(
    rig: Any,
) -> None:
    service, _state, vault, pid, wid = rig
    command, sid = ident(), ident()
    racer = restart(service, vault)
    calls: list[str] = []

    def exists(session_id: str) -> bool:
        calls.append(session_id)
        if len(calls) == 2:  # after the read checks, before the write: another preparer wins
            racer.prepare(pid, command, sid, 1, wid, launch_origin="routine")
        return False

    service.transcript_exists = exists
    with pytest.raises(ProfileError, match="command_conflict"):
        service.prepare(pid, command, sid, 1, wid, launch_origin="interactive")
    assert len(calls) == 2 and origin(service, sid) == "routine"


def test_interactive_preparation_refuses_a_session_with_a_retained_attempt(rig: Any) -> None:
    service, state, _vault, pid, wid = rig
    owner, _chat = main_bot(service, pid, wid)
    _tasks, attempt = claim(service, pid, wid)
    with pytest.raises(ProfileError, match="conflict"):
        service.prepare(
            pid, ident(), attempt.session_id, 1, wid, owner, launch_origin="interactive"
        )
    assert state.store.session_origin(attempt.session_id) is None
    service.prepare(
        pid, attempt.attempt_id, attempt.session_id, 1, wid, owner, launch_origin="routine"
    )
    assert origin(service, attempt.session_id) == "routine"


def test_an_attempt_claimed_during_interactive_preparation_refuses_the_write(rig: Any) -> None:
    service, state, _vault, pid, wid = rig
    _tasks, attempt = claim(service, pid, wid)
    sid = ident()
    calls: list[str] = []

    def exists(session_id: str) -> bool:
        calls.append(session_id)
        if len(calls) == 2:  # after the attempt check, before the write transaction
            with state.store._connection(write=True) as connection:
                connection.execute(
                    "UPDATE task_attempts SET session_id=? WHERE attempt_id=?",
                    (session_id, attempt.attempt_id),
                )
        return False

    service.transcript_exists = exists
    with pytest.raises(ProfileError, match="conflict"):
        service.prepare(pid, ident(), sid, 1, wid, launch_origin="interactive")
    assert len(calls) == 2
    assert state.store.session_origin(sid) is None and origin(service, sid) is None


def test_native_initialization_and_reopen_never_write_the_marker(rig: Any) -> None:
    service, state, vault, pid, wid = rig
    sessions = {}
    for stamp in ("unknown", "interactive", "routine"):
        sid = ident()
        service.prepare(pid, ident(), sid, 1, wid, launch_origin=cast(Any, stamp))
        sessions[sid] = stamp
    reopened = restart(service, vault)
    for sid, stamp in sessions.items():
        reopened.validate_launch(sid, state.prefs(sid))
        reopened.native_initialized(sid)
        assert native_started(service, sid) == 1
        reopened.reopenable(sid, True)
        assert origin(service, sid) == stamp


@pytest.mark.parametrize("outcome", ["expired", "blocked", "failed"])
def test_a_routine_ended_before_native_start_keeps_its_marker_through_prune(
    rig: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    service, state, vault, pid, wid = rig
    owner, _chat = main_bot(service, pid, wid)
    grant(service, owner, tmp_path / "code")
    revision = service.settings.read().document.revision
    service.settings.patch(ident(), revision, {"chat.default_model": "sonnet"})
    tasks, attempt = claim(service, pid, wid)
    sid = attempt.session_id
    prepared = service.prepare(pid, attempt.attempt_id, sid, 1, wid, owner, launch_origin="routine")
    assert prepared["launchable"] and state.prefs(sid).model == "sonnet"
    assert "code_copy" not in prepared
    # The run ends before Claude Code ever initialized, then its attempt is pruned.
    assert tasks.finish(attempt.attempt_id, cast(Any, outcome), "deadline", NOW)
    monkeypatch.setattr(task_module, "MAX_OCCURRENCES", 0)
    with state.store._connection(write=True) as connection:
        tasks.prune(connection, attempt.routine_id)
        gone = connection.execute("SELECT 1 FROM task_attempts WHERE session_id=?", (sid,))
        assert gone.fetchone() is None
    assert native_started(service, sid) == 0 and origin(service, sid) == "routine"
    reopened = restart(service, vault)
    review = reopened.inspect_session(sid)
    assert review["launchable"] and "code_copy" not in review
    assert review["prefs"]["model"] == "sonnet"  # its native history and model stay
    reopened.validate_launch(sid, state.prefs(sid))
    with pytest.raises(ProfileError, match="command_conflict"):
        reopened.prepare(pid, attempt.attempt_id, sid, 1, wid, owner, launch_origin="interactive")
    reopened.native_initialized(sid)
    assert native_started(service, sid) == 1 and origin(service, sid) == "routine"
    assert "code_copy" not in reopened.inspect_session(sid)


def test_genuine_main_task_and_shared_preparations_are_interactive(rig: Any) -> None:
    service, _state, _vault, pid, wid = rig
    owner, chat = main_bot(service, pid, wid)
    for sid in (chat, ident(), ident()):  # the main chat, a task thread, a Bot-thread seat
        service.prepare(pid, ident(), sid, 1, wid, owner, launch_origin="interactive")
        assert origin(service, sid) == "interactive"
