"""Saved groups keep sealed references, with existing commands and native histories as sources."""

from __future__ import annotations

import copy
import hashlib
from typing import Any

import pytest
from starlette.testclient import TestClient

from tests.messaging.test_bots import _bind_chat, _bot, _id
from tests.messaging.test_bots import service as service
from tests.messaging.test_bots_api import BEARER, WRITE, Rig
from viki_agent.conversations.session_metadata import SessionMetadataService
from viki_agent.messaging.bot_threads import BotThreadService
from viki_agent.messaging.bots import BotError, BotService

AT = "2026-10-14T00:00:00.000Z"


def group(bot: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": "Synthetic review",
        "phase": "idle",
        "members": [
            {
                "key": "checker",
                "bot_id": bot["bot_id"],
                "profile_id": bot["profile_id"],
                "session_id": None,
                "enabled": True,
            }
        ],
        "messages": [],
    }


@pytest.mark.parametrize("count", [7, 64])
def test_groups_save_larger_teams_without_preparing_chats(service: BotService, count: int) -> None:
    bot = _bot(service)
    state = group(bot)
    state["members"] += [
        {
            "key": _id(),
            "bot_id": None,
            "profile_id": None,
            "session_id": None,
            "enabled": True,
        }
        for _ in range(count - 1)
    ]
    groups = BotThreadService(service.store, service.workspaces)
    saved = groups.save(bot["workspace_id"], _id(), 0, state)
    assert groups.list(bot["workspace_id"])["threads"] == [saved]
    assert len(saved["state"]["members"]) == count
    assert service.store.get(bot["chat_session_id"]) is None
    state["members"] += [{**state["members"][-1], "key": _id()} for _ in range(65 - count)]
    with pytest.raises(BotError, match="invalid_input"):
        groups.save(bot["workspace_id"], _id(), 0, state)


def source(
    service: BotService, bot: dict[str, Any], *, owner: str | None = None
) -> tuple[str, str]:
    _bind_chat(service, bot)
    command_id = _id()
    with service.store._connection(write=True) as connection:
        if owner is not None:
            connection.execute(
                "INSERT INTO session_bot_memory VALUES (?,?)", (bot["chat_session_id"], owner)
            )
        canonical = connection.execute(
            "SELECT canonical_key FROM workspaces WHERE workspace_id=?", (bot["workspace_id"],)
        ).fetchone()[0]
        scope = hashlib.sha256(canonical.encode()).hexdigest()
        # A synthetic source row: group persistence must neither read nor copy its body.
        connection.execute(
            "INSERT INTO commands VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                scope,
                command_id,
                bot["chat_session_id"],
                _id(),
                bot["chat_session_id"],
                1,
                "a" * 64,
                _id(),
                "unknown",
                "",
                b"synthetic sealed command",
                1,
                24,
                AT,
                AT,
            ),
        )
    return command_id, scope


@pytest.mark.parametrize("source_kind", ["member", "message"])
@pytest.mark.parametrize("declared_owner", ["replacement", None])
@pytest.mark.parametrize("enabled", [True, False])
def test_foreign_owners_refuse_group_create_and_update_without_changes(
    service: BotService, source_kind: str, declared_owner: str | None, enabled: bool
) -> None:
    removed = _bot(service)
    foreign_command, _scope = source(service, removed, owner=removed["bot_id"])
    service.remove(removed["bot_id"], removed["state_revision"])
    replacement = _bot(
        service, profile_id=removed["profile_id"], workspace_id=removed["workspace_id"]
    )
    own_command, _scope = source(service, replacement, owner=replacement["bot_id"])
    groups = BotThreadService(service.store, service.workspaces)
    state = group(replacement)
    state["messages"] = [
        {
            "id": _id(),
            "at": AT,
            "member_key": None,
            "session_id": replacement["chat_session_id"],
            "command_id": own_command,
        }
    ]
    thread_id = _id()
    saved = groups.save(replacement["workspace_id"], thread_id, 0, state)
    with service.store._connection() as connection:
        before = (
            connection.execute("SELECT * FROM bot_threads").fetchall(),
            connection.execute("SELECT * FROM bot_thread_sessions").fetchall(),
        )
    changed = copy.deepcopy(state)
    changed["members"][0].update(
        bot_id=replacement["bot_id"] if declared_owner else None, enabled=enabled
    )
    if source_kind == "member":
        changed["members"][0]["session_id"] = removed["chat_session_id"]
    else:
        changed["messages"][0].update(
            member_key="checker",
            session_id=removed["chat_session_id"],
            command_id=foreign_command,
        )
    for identity, revision in [(_id(), 0), (thread_id, 1)]:
        with pytest.raises(BotError, match="invalid_input"):
            groups.save(replacement["workspace_id"], identity, revision, changed)
        assert groups.list(replacement["workspace_id"])["threads"] == [saved]
        with service.store._connection() as connection:
            assert (
                connection.execute("SELECT * FROM bot_threads").fetchall(),
                connection.execute("SELECT * FROM bot_thread_sessions").fetchall(),
            ) == before
    assert groups.save(replacement["workspace_id"], thread_id, 0, state) == saved


@pytest.mark.parametrize("owned", [True, False])
@pytest.mark.parametrize("enabled", [True, False])
def test_groups_keep_historical_and_legacy_sources_after_bot_removal(
    service: BotService, owned: bool, enabled: bool
) -> None:
    bot = _bot(service)
    earlier = bot["chat_session_id"]
    owner = bot["bot_id"] if owned else None
    command_id, _scope = source(service, bot, owner=owner)
    latest = service.new_chat(bot["bot_id"], bot["state_revision"])
    source(service, latest, owner=owner)
    service.remove(latest["bot_id"], latest["state_revision"])
    other = _bot(service, workspace_id=bot["workspace_id"])
    user_command, _scope = source(service, other, owner=other["bot_id"])
    state = group(latest)
    state["members"][0].update(
        bot_id=bot["bot_id"] if owned else None,
        session_id=latest["chat_session_id"],
        enabled=enabled,
    )
    state["messages"] = [
        {
            "id": _id(),
            "at": AT,
            "member_key": "checker",
            "session_id": earlier,
            "command_id": command_id,
        },
        {
            "id": _id(),
            "at": AT,
            "member_key": None,
            "session_id": other["chat_session_id"],
            "command_id": user_command,
        },
    ]
    groups = BotThreadService(service.store, service.workspaces)
    thread_id = _id()
    saved = groups.save(bot["workspace_id"], thread_id, 0, state)
    assert saved["state"] == state
    assert groups.save(bot["workspace_id"], thread_id, 0, state) == saved
    changed = groups.save(bot["workspace_id"], thread_id, 1, {**state, "title": "Reviewed"})
    assert changed["revision"] == 2
    assert groups.list(bot["workspace_id"])["threads"] == [changed]


def test_sealed_cas_replay_reopen_and_delete_keep_individual_sources(service: BotService) -> None:
    bot = _bot(service)
    groups = BotThreadService(service.store, service.workspaces)
    thread_id, state = _id(), group(bot)
    saved = groups.save(bot["workspace_id"], thread_id, 0, state)
    assert saved["revision"] == 1 and saved["state"] == state
    assert groups.save(bot["workspace_id"], thread_id, 0, state) == saved
    reopened = BotThreadService(service.store, service.workspaces)
    assert reopened.list(bot["workspace_id"])["threads"] == [saved]
    with service.store._connection() as connection:
        body = connection.execute("SELECT body FROM bot_threads").fetchone()[0]
    assert b"Synthetic review" not in body and bot["bot_id"].encode() not in body
    with pytest.raises(BotError, match="conflict"):
        groups.save(bot["workspace_id"], thread_id, 0, {**state, "title": "Changed"})
    with pytest.raises(BotError, match="conflict"):
        groups.remove(bot["workspace_id"], thread_id, 2)
    assert groups.remove(bot["workspace_id"], thread_id, 1) == {"removed": True}
    assert groups.list(bot["workspace_id"])["threads"] == []
    assert service.get(bot["bot_id"])["bot_id"] == bot["bot_id"]


def test_source_binding_is_exact_and_forget_drops_group_only(service: BotService) -> None:
    bot = _bot(service)
    command_id, scope = source(service, bot)
    groups = BotThreadService(service.store, service.workspaces)
    state = group(bot)
    state["members"][0]["session_id"] = bot["chat_session_id"]
    state["messages"] = [
        {
            "id": _id(),
            "at": AT,
            "member_key": "checker",
            "session_id": bot["chat_session_id"],
            "command_id": command_id,
        }
    ]
    thread_id = _id()
    groups.save(bot["workspace_id"], thread_id, 0, state)
    other = _bot(service)
    with pytest.raises(BotError, match="invalid_input"):
        groups.save(other["workspace_id"], _id(), 0, state)
    changed = copy.deepcopy(state)
    changed["messages"][0]["command_id"] = _id()
    with pytest.raises(BotError, match="invalid_input"):
        groups.save(bot["workspace_id"], thread_id, 1, changed)
    SessionMetadataService(service.store, "f" * 64).delete(bot["chat_session_id"], 0)
    assert groups.list(bot["workspace_id"])["threads"]
    SessionMetadataService(service.store, scope).delete(bot["chat_session_id"], 0)
    assert groups.list(bot["workspace_id"])["threads"] == []
    with service.store._connection() as connection:
        assert connection.execute("SELECT count(*) FROM commands").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM bot_thread_sessions").fetchone()[0] == 0


def test_group_remove_keeps_source_rows_byte_for_byte(service: BotService) -> None:
    bot = _bot(service)
    command_id, scope = source(service, bot)
    groups = BotThreadService(service.store, service.workspaces)
    state = group(bot)
    state["members"][0]["session_id"] = bot["chat_session_id"]
    state["messages"] = [
        {
            "id": _id(),
            "at": AT,
            "member_key": "checker",
            "session_id": bot["chat_session_id"],
            "command_id": command_id,
        }
    ]
    thread_id = _id()
    saved = groups.save(bot["workspace_id"], thread_id, 0, state)
    with service.store._connection() as connection:
        before = (
            connection.execute(
                "SELECT * FROM sessions WHERE session_id=?", (bot["chat_session_id"],)
            ).fetchall(),
            connection.execute(
                "SELECT * FROM commands WHERE scope=? AND command_id=?", (scope, command_id)
            ).fetchall(),
            connection.execute(
                "SELECT * FROM session_profile_bindings WHERE session_id=?",
                (bot["chat_session_id"],),
            ).fetchall(),
            connection.execute(
                "SELECT * FROM bot_threads WHERE thread_id=?", (thread_id,)
            ).fetchall(),
            connection.execute(
                "SELECT * FROM bot_thread_sessions WHERE thread_id=?", (thread_id,)
            ).fetchall(),
        )
    assert all(before)

    assert groups.remove(bot["workspace_id"], thread_id, saved["revision"]) == {"removed": True}

    with service.store._connection() as connection:
        after = (
            connection.execute(
                "SELECT * FROM sessions WHERE session_id=?", (bot["chat_session_id"],)
            ).fetchall(),
            connection.execute(
                "SELECT * FROM commands WHERE scope=? AND command_id=?", (scope, command_id)
            ).fetchall(),
            connection.execute(
                "SELECT * FROM session_profile_bindings WHERE session_id=?",
                (bot["chat_session_id"],),
            ).fetchall(),
            connection.execute(
                "SELECT * FROM bot_threads WHERE thread_id=?", (thread_id,)
            ).fetchall(),
            connection.execute(
                "SELECT * FROM bot_thread_sessions WHERE thread_id=?", (thread_id,)
            ).fetchall(),
        )
    assert after[:3] == before[:3]
    assert after[3:] == ([], [])


def test_reset_commands_removes_linked_group_metadata(service: BotService) -> None:
    bot = _bot(service)
    command_id, scope = source(service, bot)
    groups = BotThreadService(service.store, service.workspaces)
    state = group(bot)
    state["members"][0]["session_id"] = bot["chat_session_id"]
    state["messages"] = [
        {
            "id": _id(),
            "at": AT,
            "member_key": "checker",
            "session_id": bot["chat_session_id"],
            "command_id": command_id,
        }
    ]
    groups.save(bot["workspace_id"], _id(), 0, state)
    assert service.store.delete_scope_rows("commands", scope, _id()) == (1, 0)
    assert groups.list(bot["workspace_id"])["threads"] == []


@pytest.mark.parametrize(
    "mutate",
    [
        lambda state: state.update(text="transcript body"),
        lambda state: state["members"][0].update(prompt="private input"),
        lambda state: state.update(phase="complete"),
        lambda state: state.update(title="x" * 129),
        lambda state: state["members"].append(copy.deepcopy(state["members"][0])),
    ],
)
def test_groups_refuse_content_and_invalid_shapes(service: BotService, mutate: Any) -> None:
    bot = _bot(service)
    state = group(bot)
    mutate(state)
    with pytest.raises(BotError, match="invalid_input"):
        BotThreadService(service.store, service.workspaces).save(
            bot["workspace_id"], _id(), 0, state
        )


ENGINEER_SEAT = "3f1c9a6e-5b2d-4e8f-9a07-c41d2b6e8f53"


def test_groups_keep_an_empty_engineer_seat_by_its_id_and_refuse_its_word(
    service: BotService,
) -> None:
    bot = _bot(service)
    groups = BotThreadService(service.store, service.workspaces)
    # The member the app saves for an empty seat: no profile, Bot or session.
    state = group(bot)
    state["members"].append(
        {
            "key": ENGINEER_SEAT,
            "bot_id": None,
            "profile_id": None,
            "session_id": None,
            "enabled": False,
        }
    )
    thread_id = _id()
    saved = groups.save(bot["workspace_id"], thread_id, 0, state)
    assert saved["state"] == state
    assert groups.list(bot["workspace_id"])["threads"] == [saved]
    state["members"][1]["key"] = "engineer"
    with pytest.raises(BotError, match="invalid_input"):
        groups.save(bot["workspace_id"], _id(), 0, state)


def test_group_routes_require_auth_origin_and_revision(service: BotService) -> None:
    rig = Rig(service)
    path = f"/api/v2/bot-threads/{rig.bot['workspace_id']}"
    with TestClient(rig.app()) as client:
        assert client.get(path).status_code == 401
        assert client.get(path + "?all=true", headers=BEARER).status_code == 400
        thread = path + "/" + _id()
        body = {"expected_revision": 0, "state": group(rig.bot)}
        assert client.put(thread, json=body, headers=BEARER).status_code == 403
        saved = client.put(thread, json=body, headers=WRITE)
        assert saved.status_code == 200
        assert client.get(path, headers=BEARER).json()["threads"] == [saved.json()]
        assert (
            client.request(
                "DELETE", thread, json={"expected_revision": 1}, headers=WRITE
            ).status_code
            == 200
        )
