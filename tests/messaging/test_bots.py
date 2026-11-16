"""Durable Bot links, owned schedules and metadata-only receipts in synthetic app state."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from tests.automation.test_routines import _document_raw
from tests.persistence.test_storage import _workspace
from tests.profiles.test_profile_document import document
from viki_agent.automation.routines import RoutineService, stamp
from viki_agent.automation.tasks import Attempt, TaskStore
from viki_agent.messaging import bots
from viki_agent.profiles.service import ProfileService
from viki_agent.state import AppState, Prefs
from viki_agent.storage import StateError
from viki_agent.workspaces import WorkspaceService, root_key

_NOW = datetime(2026, 10, 14, tzinfo=UTC)
_STAMP = stamp(_NOW)


def _id() -> str:
    return str(uuid.uuid4())


@pytest.fixture
def service(tmp_path: Path) -> bots.BotService:
    store = AppState(tmp_path / "data" / "state.json", Prefs()).store
    profiles = ProfileService(store)
    workspaces = WorkspaceService(
        store,
        env={},
        data_dir=tmp_path / "data",
        home=tmp_path / "home",
        install_dir=tmp_path / "install",
        git_available=lambda: False,
        hide_previews=lambda: False,
    )
    routines = RoutineService(store, profiles, None, workspaces, now=lambda: _NOW)
    return bots.BotService(store, profiles, routines, workspaces, now=lambda: _NOW)


def _profile(service: bots.BotService, name: str = "Review helper") -> str:
    profile_id = _id()
    value = document()
    value["identity"]["name"] = name
    service.profiles.create(profile_id, _id(), value)
    return profile_id


def _vault(service: bots.BotService, *, active: bool = False, kind: str = "vault") -> str:
    workspace_id = _id()
    root = service.store.path.parent.parent / workspace_id
    root.mkdir()
    info = root.stat()
    row = _workspace(
        workspace_id,
        canonical_key=root_key(root.resolve()),
        display_root=str(root),
        physical_id=f"{info.st_dev}:{info.st_ino}",
        bootstrap="complete",
        active=int(active),
        kind=kind,
    )
    service.store.update_workspace(workspace_id, lambda _current, _rows: row)
    return workspace_id


def _bot(service: bots.BotService, **overrides: Any) -> dict[str, Any]:
    values = {
        "bot_id": _id(),
        "profile_id": overrides.get("profile_id") or _profile(service),
        "expected_profile_revision": 1,
        "workspace_id": overrides.get("workspace_id") or _vault(service),
        **overrides,
    }
    return service.create(**values)[0]


def _routine(service: bots.BotService, bot: dict[str, Any], *, enabled: bool = True) -> str:
    row, _created = service.routines.create(
        _id(),
        _document_raw(profile_id=bot["profile_id"], workspace_id=bot["workspace_id"]),
    )
    if enabled:
        with service.store._connection(write=True) as connection:
            connection.execute(
                "UPDATE routines SET state='enabled',state_revision=2,tested_revision=1,"
                "enabled_since=? WHERE routine_id=?",
                (_STAMP, row["routine_id"]),
            )
    return row["routine_id"]


def _attempt(service: bots.BotService, routine_id: str, *, done: bool = True) -> Attempt:
    tasks = TaskStore(service.store, "a" * 64, _id())
    with service.store._connection(write=True) as connection:
        attempt = tasks.insert_claim(
            connection,
            routine_id=routine_id,
            kind="manual",
            revision=1,
            now=_STAMP,
            trigger_id=_id(),
            request_hash="a" * 64,
        )
    if done:
        assert tasks.finish(attempt.attempt_id, "succeeded", "", _STAMP)
    return attempt


def _receipt(
    service: bots.BotService, attempt: Attempt, bot: dict[str, Any], **changes: Any
) -> bool:
    values = {
        "attention": "notify",
        "reason": "runs_to_review",
        "state": "sending",
        "delivery_reason": "none",
        **changes,
    }
    return service.record_delivery(attempt.attempt_id, bot["bot_id"], **values)


def _bind_chat(service: bots.BotService, bot: dict[str, Any]) -> None:
    values = {
        "session_id": bot["chat_session_id"],
        "command_id": bot["chat_session_id"],
        "request_hash": "a" * 64,
        "profile_id": bot["profile_id"],
        "profile_revision": 1,
        "workspace_id": bot["workspace_id"],
        "workspace_revision": 1,
        "policy": "ordinary",
        "policy_revision": 1,
        "settings_revision": 0,
        "runtime": "claude",
        "capability_key": "b" * 64,
        "launch_spec_hash": "c" * 64,
        "native_started": 1,
        "created_at": _STAMP,
    }
    with service.store._connection(write=True) as connection:
        connection.execute(
            "INSERT INTO sessions VALUES (?,'plan','high',NULL,'profile')",
            (bot["chat_session_id"],),
        )
        connection.execute(
            f"INSERT INTO session_profile_bindings ({','.join(values)}) "
            f"VALUES ({','.join('?' * len(values))})",
            tuple(values.values()),
        )


def _owned_chat(service: bots.BotService, bot: dict[str, Any], *, memory: bool = True) -> str:
    """A task thread of `bot`: a bound chat, with its Bot-memory owner row when `memory`."""
    session_id = _id()
    _bind_chat(service, {**bot, "chat_session_id": session_id})
    if memory:
        with service.store._connection(write=True) as connection:
            connection.execute(
                "INSERT INTO session_bot_memory VALUES (?,?)", (session_id, bot["bot_id"])
            )
    return session_id


def _group_member(service: bots.BotService, bot: dict[str, Any], session_id: str) -> None:
    """Retain `session_id` as a source of a stored Bot thread in the Bot's vault."""
    thread_id = _id()
    with service.store._connection(write=True) as connection:
        connection.execute(
            "INSERT INTO bot_threads VALUES (?,?,?,?,?,?,?,?)",
            (thread_id, bot["workspace_id"], 1, b"x", 1, "a" * 64, _STAMP, _STAMP),
        )
        connection.execute("INSERT INTO bot_thread_sessions VALUES (?,?)", (thread_id, session_id))


def test_chat_session_ids_include_legacy_tasks_but_not_removed_bots(
    service: bots.BotService,
) -> None:
    # Was `..._include_legacy_tasks_and_removed_bots`: it expected a removed Bot's chat in the
    # set, which hid that chat from the sidebar while no Bot view listed it.
    bot = _bot(service)
    legacy, unrelated = _id(), _id()
    _bind_chat(service, {**bot, "chat_session_id": legacy})
    _bind_chat(service, {**bot, "chat_session_id": unrelated, "workspace_id": _vault(service)})
    removed = _bot(service)
    _bind_chat(service, removed)
    with service.store._connection(write=True) as connection:
        connection.execute(
            "INSERT INTO session_bot_memory VALUES (?,?)",
            (removed["chat_session_id"], removed["bot_id"]),
        )
    service.remove(removed["bot_id"], removed["state_revision"])
    assert service.chat_session_ids() == {bot["chat_session_id"], legacy}
    assert service.store.get(bot["chat_session_id"]) is None


def test_chat_session_ids_leave_a_removed_bots_chats_even_when_the_seat_has_a_new_bot(
    service: bots.BotService,
) -> None:
    removed = _bot(service)
    _bind_chat(service, removed)
    task = _owned_chat(service, removed)
    with service.store._connection(write=True) as connection:
        connection.execute(
            "INSERT INTO session_bot_memory VALUES (?,?)",
            (removed["chat_session_id"], removed["bot_id"]),
        )
    member = _owned_chat(service, removed)
    _group_member(service, removed, member)
    service.remove(removed["bot_id"], removed["state_revision"])
    gone = {removed["chat_session_id"], task, member}
    assert not gone & service.chat_session_ids()
    replacement = _bot(
        service, profile_id=removed["profile_id"], workspace_id=removed["workspace_id"]
    )
    assert service.chat_session_ids() == {replacement["chat_session_id"]}


def test_chat_session_ids_keep_a_live_bots_main_chat_tasks_and_thread_members(
    service: bots.BotService,
) -> None:
    bot = _bot(service)
    _bind_chat(service, bot)
    task = _owned_chat(service, bot)
    member = _owned_chat(service, bot)
    _group_member(service, bot, member)
    other = _bot(service, workspace_id=bot["workspace_id"])
    foreign = _owned_chat(service, other)
    assert service.chat_session_ids() == {
        bot["chat_session_id"],
        task,
        member,
        other["chat_session_id"],
        foreign,
    }


def test_chat_session_ids_leave_a_removed_bots_chat_that_sits_on_a_live_bots_pair(
    service: bots.BotService,
) -> None:
    removed = _bot(service)
    live = _bot(service, profile_id=removed["profile_id"], workspace_id=_vault(service))
    # Bound to the live Bot's profile and vault but owned, by memory, by the removed Bot.
    stray = _owned_chat(service, {**live, "bot_id": removed["bot_id"]})
    service.remove(removed["bot_id"], removed["state_revision"])
    assert service.chat_session_ids() == {live["chat_session_id"]}
    assert stray not in service.chat_session_ids()


def test_chat_session_ids_are_empty_after_reset_bots(service: bots.BotService) -> None:
    first = _bot(service)
    workspace_id = first["workspace_id"]
    second = _bot(service, workspace_id=workspace_id)
    elsewhere = _bot(service)
    chats = [_owned_chat(service, first), _owned_chat(service, second)]
    _group_member(service, first, chats[0])
    assert {first["chat_session_id"], second["chat_session_id"], *chats} <= (
        service.chat_session_ids()
    )
    service.reset(workspace_id)
    assert service.chat_session_ids() == {elsewhere["chat_session_id"]}


def test_create_reserves_chat_and_replays_before_changed_profile_admission(
    service: bots.BotService,
) -> None:
    profile_id, workspace_id, bot_id = _profile(service), _vault(service, active=True), _id()
    first, replayed = service.create(bot_id, profile_id, 1, workspace_id)
    assert not replayed and first["state_revision"] == 1
    assert first["profile"]["identity"]["name"] == "Review helper"
    assert first["workspace"] == {"name": "synthetic", "policy": "ordinary", "active": True}
    assert first["chat"] == {"profile_revision": None, "started": False, "archived": False}
    assert service.store.get(first["chat_session_id"]) is None
    expected_hash = hashlib.sha256(
        json.dumps(
            {
                "bot_id": bot_id,
                "profile_id": profile_id,
                "expected_profile_revision": 1,
                "workspace_id": workspace_id,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("ascii")
    ).hexdigest()
    with service.store._connection() as connection:
        assert connection.execute("SELECT request_hash FROM bots").fetchone() == (expected_hash,)
    service.profiles.revise(profile_id, _id(), 1, document())
    service.profiles.archive(profile_id, _id(), 1, True)
    again, replayed = service.create(bot_id, profile_id, 1, workspace_id)
    assert replayed and again["chat_session_id"] == first["chat_session_id"]
    assert again["profile"]["archived"]
    with pytest.raises(bots.BotError, match="command_conflict"):
        service.create(bot_id, profile_id, 2, workspace_id)


def test_restart_preserves_link_and_projection_omits_private_content(
    service: bots.BotService,
) -> None:
    bot = _bot(service)
    reloaded = bots.BotService(
        service.store,
        service.profiles,
        service.routines,
        service.workspaces,
        now=lambda: _NOW,
    )
    assert reloaded.get(bot["bot_id"]) == bot
    assert reloaded.list() == [bot]
    wire = json.dumps(bot)
    assert "request_hash" not in wire and "instructions" not in wire
    assert "Review the change and cite concrete evidence." not in wire
    assert str(service.store.path.parent.parent) not in wire
    assert "request_hash" not in service.record(bot["bot_id"])
    assert service.find(bot["profile_id"], bot["workspace_id"]) == service.record(bot["bot_id"])
    assert service.find(_id(), bot["workspace_id"]) is None


def test_duplicate_pair_cannot_reserve_another_chat(service: bots.BotService) -> None:
    first = _bot(service)
    with pytest.raises(bots.BotError, match="conflict"):
        service.create(_id(), first["profile_id"], 1, first["workspace_id"])
    assert len(service.list()) == 1


def test_chat_archive_scope_and_pinned_revision_are_separate_from_current_profile(
    service: bots.BotService,
) -> None:
    bot = _bot(service)
    _bind_chat(service, bot)
    workspace = service.store.get_workspace(bot["workspace_id"])
    assert workspace is not None
    scope = hashlib.sha256(workspace["canonical_key"].encode("utf-8")).hexdigest()
    with service.store._connection(write=True) as connection:
        connection.executemany(
            "INSERT INTO session_metadata VALUES (?,?,1,'',0,?,0,NULL)",
            [(scope, bot["chat_session_id"], 1), ("a" * 64, bot["chat_session_id"], 0)],
        )
    service.profiles.revise(bot["profile_id"], _id(), 1, document())
    projected = service.get(bot["bot_id"])
    assert projected["profile"]["revision"] == 2
    assert projected["chat"] == {"profile_revision": 1, "started": True, "archived": True}
    with service.store._connection(write=True) as connection:
        connection.execute("UPDATE session_metadata SET archived=(scope<>?)", (scope,))
    assert not service.get(bot["bot_id"])["chat"]["archived"]


def test_missing_links_project_explicit_nulls_and_corrupt_profile_fails_closed(
    service: bots.BotService,
) -> None:
    bot = _bot(service)
    with service.store._connection(write=True) as connection:
        connection.execute("UPDATE profile_revisions SET body_sha256=?", ("f" * 64,))
    with pytest.raises(bots.BotError, match="corrupt"):
        service.get(bot["bot_id"])
    with service.store._connection(write=True) as connection:
        connection.execute("DELETE FROM profiles")
        connection.execute("DELETE FROM workspaces")
    projected = service.get(bot["bot_id"])
    assert projected["profile"] is None and projected["workspace"] is None


def test_selected_workspace_does_not_claim_the_startup_workspace_changed(
    service: bots.BotService,
) -> None:
    first = _bot(service, workspace_id=_vault(service, active=True))
    second = _bot(service)
    service.store.select_workspace(second["workspace_id"])
    assert service.get(first["bot_id"])["workspace"]["active"]
    assert not service.get(second["bot_id"])["workspace"]["active"]


@pytest.mark.parametrize(
    "failure",
    ["profile_missing", "profile_archived", "profile_changed", "workspace_missing", "code"],
)
def test_creation_admits_current_profile_and_registered_vault_only(
    service: bots.BotService,
    failure: str,
) -> None:
    profile_id, workspace_id, revision = _profile(service), _vault(service), 1
    code = failure
    if failure == "profile_missing":
        profile_id = _id()
    elif failure == "profile_archived":
        service.profiles.archive(profile_id, _id(), 1, True)
    elif failure == "profile_changed":
        revision = 2
    elif failure == "workspace_missing":
        workspace_id = _id()
    else:
        workspace_id, code = _vault(service, kind="code"), "workspace_missing"
    with pytest.raises(bots.BotError, match=code):
        service.create(_id(), profile_id, revision, workspace_id)
    assert service.list() == []


@pytest.mark.parametrize("value", [True, 0, -1, 9007199254740992, 1.5, "1"])
def test_create_revision_is_a_positive_safe_integer(service: bots.BotService, value: Any) -> None:
    with pytest.raises(bots.BotError, match="invalid_input"):
        service.create(_id(), _id(), value, _id())


def test_creation_rechecks_profile_revision_before_insert(
    service: bots.BotService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile_id, workspace_id = _profile(service), _vault(service)
    get = service.profiles.get

    def advance(value: str, revision: int | None = None) -> dict[str, Any]:
        row = get(value, revision)
        service.profiles.revise(profile_id, _id(), 1, document())
        return row

    monkeypatch.setattr(service.profiles, "get", advance)
    with pytest.raises(bots.BotError, match="profile_changed"):
        service.create(_id(), profile_id, 1, workspace_id)
    with service.store._connection() as connection:
        assert connection.execute("SELECT count(*) FROM bots").fetchone() == (0,)


def test_chat_replacement_and_delivery_revision_are_compare_and_set(
    service: bots.BotService,
) -> None:
    bot = _bot(service)
    old_chat = bot["chat_session_id"]
    _bind_chat(service, bot)
    bumped = service.bump_revision(bot["bot_id"], 1)
    assert bumped["state_revision"] == 2 and bumped["chat_session_id"] == old_chat
    newer = service.new_chat(bot["bot_id"], 2)
    assert newer["state_revision"] == 3 and newer["chat_session_id"] != old_chat
    assert service.store.get(old_chat) is not None
    assert service.store.get(newer["chat_session_id"]) is None
    for action in (service.bump_revision, service.new_chat, service.remove):
        with pytest.raises(bots.BotError, match="conflict"):
            action(bot["bot_id"], 2)


def test_competing_cas_mutations_admit_one(service: bots.BotService) -> None:
    bot = _bot(service)

    def bump() -> str:
        try:
            service.bump_revision(bot["bot_id"], 1)
            return "ok"
        except (bots.BotError, StateError) as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _item: bump(), range(2)))
    assert outcomes.count("ok") == 1
    assert set(outcomes) <= {"ok", "busy", "conflict"}
    assert service.record(bot["bot_id"])["state_revision"] == 2


def test_safe_revision_limit_and_corrupt_stored_stamp_fail_closed(service: bots.BotService) -> None:
    bot = _bot(service)
    with service.store._connection(write=True) as connection:
        connection.execute("UPDATE bots SET state_revision=?", (9007199254740991,))
    with pytest.raises(bots.BotError, match="capacity"):
        service.bump_revision(bot["bot_id"], 9007199254740991)
    with service.store._connection(write=True) as connection:
        connection.execute("UPDATE bots SET updated_at=?", ("x" * 24,))
    with pytest.raises(bots.BotError, match="corrupt"):
        service.get(bot["bot_id"])


def test_registry_cap_and_over_cap_corruption_are_bounded(service: bots.BotService) -> None:
    profile_id, workspace_id = _profile(service), _vault(service)
    with service.store._connection(write=True) as connection:
        for _ in range(bots.MAX_BOTS):
            connection.execute(
                "INSERT INTO bots VALUES (?,?,?,?,?,1,?,?)",
                (_id(), _id(), _id(), _id(), "a" * 64, _STAMP, _STAMP),
            )
    with pytest.raises(bots.BotError, match="capacity"):
        service.create(_id(), profile_id, 1, workspace_id)
    assert len(service.list()) == bots.MAX_BOTS
    with service.store._connection(write=True) as connection:
        connection.execute(
            "INSERT INTO bots VALUES (?,?,?,?,?,1,?,?)",
            (_id(), _id(), _id(), _id(), "a" * 64, _STAMP, _STAMP),
        )
    with pytest.raises(bots.BotError, match="corrupt"):
        service.list()


def test_remove_pauses_current_owner_only_and_preserves_chats_and_runs(
    service: bots.BotService,
) -> None:
    first, second = _bot(service), _bot(service)
    owned, moved, other = (
        _routine(service, first),
        _routine(service, first),
        _routine(service, second),
    )
    service.routines.revise(
        moved,
        _id(),
        1,
        _document_raw(profile_id=second["profile_id"], workspace_id=second["workspace_id"]),
    )
    attempt = _attempt(service, owned)
    running = _attempt(service, owned, done=False)
    assert _receipt(service, attempt, first)
    _bind_chat(service, first)
    result = service.remove(first["bot_id"], 1)
    assert result == {"removed": True, "bot_id": first["bot_id"], "paused_routine_ids": [owned]}
    paused = service.routines.get(owned)
    assert paused["state"] == "paused" and paused["state_revision"] == 3
    assert paused["enabled_since"] is None
    assert service.routines.get(moved)["state"] == service.routines.get(other)["state"] == "enabled"
    assert service.delivery(attempt.attempt_id) is None
    assert service.profiles.get(first["profile_id"])
    assert service.store.get(first["chat_session_id"])
    with service.store._connection() as connection:
        assert connection.execute("SELECT count(*) FROM task_attempts").fetchone() == (2,)
        assert connection.execute(
            "SELECT state FROM task_attempts WHERE attempt_id=?", (running.attempt_id,)
        ).fetchone() == ("claimed",)


def test_remove_failure_rolls_back_pauses_and_receipt_delete(
    service: bots.BotService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = _bot(service)
    routine_id = _routine(service, bot)
    attempt = _attempt(service, routine_id)
    assert _receipt(service, attempt, bot)
    before = service.store.path.read_bytes()
    connect = sqlite3.connect

    class Connection(sqlite3.Connection):
        def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
            if sql.startswith("DELETE FROM bots"):
                raise sqlite3.OperationalError("synthetic deletion failure")
            return super().execute(sql, parameters)

    with monkeypatch.context() as patch:
        patch.setattr(
            sqlite3, "connect", lambda *args, **kwargs: connect(*args, factory=Connection, **kwargs)
        )
        with pytest.raises(StateError):
            service.remove(bot["bot_id"], 1)
    assert service.store.path.read_bytes() == before
    assert service.routines.get(routine_id)["state"] == "enabled"
    assert service.delivery(attempt.attempt_id) is not None


def test_unreadable_current_owner_aborts_remove_without_partial_changes(
    service: bots.BotService,
) -> None:
    bot = _bot(service)
    first, broken = _routine(service, bot), _routine(service, bot)
    with service.store._connection(write=True) as connection:
        connection.execute(
            "UPDATE routine_revisions SET body_sha256=? WHERE routine_id=?", ("f" * 64, broken)
        )
    before = service.store.path.read_bytes()
    with pytest.raises(bots.BotError, match="corrupt"):
        service.remove(bot["bot_id"], 1)
    assert service.store.path.read_bytes() == before
    assert service.routines.get(first)["state"] == "enabled"


def test_workspace_reset_pauses_and_forgets_only_selected_bot_links(
    service: bots.BotService,
) -> None:
    workspace_id = _vault(service)
    first = _bot(service, workspace_id=workspace_id)
    second = _bot(service, workspace_id=workspace_id)
    other = _bot(service)
    routines = [_routine(service, first), _routine(service, second)]
    unaffected = _routine(service, other)
    result = service.reset(workspace_id)
    assert result["workspace_id"] == workspace_id
    assert set(result["removed_bot_ids"]) == {first["bot_id"], second["bot_id"]}
    assert set(result["paused_routine_ids"]) == set(routines)
    assert [bot["bot_id"] for bot in service.list()] == [other["bot_id"]]
    assert service.routines.get(unaffected)["state"] == "enabled"
    assert all(service.routines.get(routine_id)["state"] == "paused" for routine_id in routines)
    assert service.reset(workspace_id)["removed_bot_ids"] == []


def test_receipts_require_committed_attempt_and_exact_frozen_owner(
    service: bots.BotService,
) -> None:
    first, second = _bot(service), _bot(service)
    routine_id = _routine(service, first)
    attempt = _attempt(service, routine_id, done=False)
    with pytest.raises(bots.BotError, match="conflict"):
        _receipt(service, attempt, first)
    TaskStore(service.store, "a" * 64, _id()).finish(attempt.attempt_id, "succeeded", "", _STAMP)
    service.routines.revise(
        routine_id,
        _id(),
        1,
        _document_raw(profile_id=second["profile_id"], workspace_id=second["workspace_id"]),
    )
    with pytest.raises(bots.BotError, match="conflict"):
        _receipt(service, attempt, second)
    assert _receipt(service, attempt, first)
    assert not _receipt(service, attempt, first)
    latest = service.get(first["bot_id"])["latest_delivery"]
    assert latest["routine_id"] == routine_id and latest["session_id"] == attempt.session_id
    assert latest["outcome"] == "succeeded" and latest["state"] == "sending"
    assert service.get(first["bot_id"])["routine_ids"] == []
    assert service.get(second["bot_id"])["routine_ids"] == [routine_id]


def test_receipt_state_updates_are_cas_and_restart_marks_unknown(service: bots.BotService) -> None:
    bot = _bot(service)
    attempt = _attempt(service, _routine(service, bot))
    assert _receipt(service, attempt, bot)
    values = {
        "attention": "notify",
        "reason": "runs_to_review",
        "state": "sent",
        "delivery_reason": "none",
    }
    assert not service.update_delivery(attempt.attempt_id, expected_state="off", **values)
    assert service.recover_deliveries() == 1
    receipt = service.delivery(attempt.attempt_id)
    assert receipt is not None
    assert receipt["state"] == "unknown"
    assert receipt["delivery_reason"] == "send_unknown"
    assert service.recover_deliveries() == 0
    assert not service.update_delivery(attempt.attempt_id, expected_state="sending", **values)


@pytest.mark.parametrize("field", ["attention", "reason", "state", "delivery_reason"])
def test_receipt_vocabulary_refuses_unrecognized_values(
    service: bots.BotService, field: str
) -> None:
    bot = _bot(service)
    attempt = _attempt(service, _routine(service, bot))
    with pytest.raises(bots.BotError, match="invalid_input"):
        _receipt(service, attempt, bot, **{field: "unreviewed"})
    assert service.delivery(attempt.attempt_id) is None
