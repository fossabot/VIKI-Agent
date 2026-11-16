"""Bot threads project existing interactive profile sessions, without reading their contents."""

from __future__ import annotations

import hashlib
from typing import Any

import pytest
from starlette.testclient import TestClient

from tests.messaging.test_bots import _attempt, _bind_chat, _bot, _id, _routine
from tests.messaging.test_bots import service as service
from tests.messaging.test_bots_api import BASE, BEARER, Rig
from viki_agent.conversations.session_metadata import SessionMetadataService
from viki_agent.messaging import bots


def _thread(service: bots.BotService, bot: dict[str, Any], *, owner: str | None = None) -> str:
    session_id = _id()
    _bind_chat(service, {**bot, "chat_session_id": session_id})
    if owner is not None:
        with service.store._connection(write=True) as connection:
            connection.execute("INSERT INTO session_bot_memory VALUES (?,?)", (session_id, owner))
    return session_id


def test_threads_share_exact_profile_and_vault_and_exclude_main_and_routine_runs(
    service: bots.BotService,
) -> None:
    bot = _bot(service)
    _bind_chat(service, bot)
    thread = _thread(service, bot)
    other = _bot(service, profile_id=bot["profile_id"])
    _thread(service, other)
    _thread(service, _bot(service, workspace_id=bot["workspace_id"]))
    attempt = _attempt(service, _routine(service, bot))
    _bind_chat(service, {**bot, "chat_session_id": attempt.session_id})
    with service.store._connection() as connection:
        canonical_key = connection.execute(
            "SELECT canonical_key FROM workspaces WHERE workspace_id=?", (bot["workspace_id"],)
        ).fetchone()[0]
    scope = hashlib.sha256(canonical_key.encode("utf-8")).hexdigest()
    SessionMetadataService(service.store, scope).patch(
        thread, 0, {"title": "<b>Source review</b>", "archived": True}
    )
    result = service.threads(bot["bot_id"])
    assert result == {
        "bot_id": bot["bot_id"],
        "threads": [
            {
                "session_id": thread,
                "profile_revision": 1,
                "title": "<b>Source review</b>",
                "created_at": bot["created_at"],
                "started": True,
                "archived": True,
            }
        ],
        "has_more": False,
    }


def test_replacing_main_chat_keeps_the_previous_chat_as_a_thread(service: bots.BotService) -> None:
    bot = _bot(service)
    _bind_chat(service, bot)
    with service.store._connection(write=True) as connection:
        connection.execute(
            "INSERT INTO session_bot_memory VALUES (?,?)", (bot["chat_session_id"], bot["bot_id"])
        )
    assert service.threads(bot["bot_id"])["threads"] == []
    changed = service.new_chat(bot["bot_id"], bot["state_revision"])
    assert changed["chat_session_id"] != bot["chat_session_id"]
    assert service.threads(bot["bot_id"])["threads"][0]["session_id"] == bot["chat_session_id"]


def test_replacement_bot_lists_legacy_threads_without_reassigning_removed_owners(
    service: bots.BotService,
) -> None:
    removed = _bot(service)
    _bind_chat(service, removed)
    foreign = _thread(service, removed, owner=removed["bot_id"])
    legacy = _thread(service, removed)
    with service.store._connection(write=True) as connection:
        connection.execute(
            "INSERT INTO session_bot_memory VALUES (?,?)",
            (removed["chat_session_id"], removed["bot_id"]),
        )
    service.remove(removed["bot_id"], removed["state_revision"])
    replacement = _bot(
        service, profile_id=removed["profile_id"], workspace_id=removed["workspace_id"]
    )
    result = service.threads(replacement["bot_id"])
    assert [row["session_id"] for row in result["threads"]] == [legacy]
    assert not result["has_more"]
    with service.store._connection() as connection:
        assert connection.execute(
            "SELECT session_id,bot_id FROM session_bot_memory ORDER BY session_id"
        ).fetchall() == sorted(
            [(foreign, removed["bot_id"]), (removed["chat_session_id"], removed["bot_id"])]
        )
    assert service.store.get(foreign) is not None
    assert service.store.get(removed["chat_session_id"]) is not None


def test_foreign_newest_threads_do_not_consume_the_requested_bots_cap(
    service: bots.BotService, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot = _bot(service)
    included = [
        _thread(service, bot),
        _thread(service, bot, owner=bot["bot_id"]),
        _thread(service, bot, owner=bot["bot_id"]),
    ]
    foreign = [_thread(service, bot, owner=_id()) for _ in range(3)]
    with service.store._connection(write=True) as connection:
        connection.executemany(
            "UPDATE session_profile_bindings SET created_at=? WHERE session_id=?",
            [("2026-10-15T00:00:00.000Z", session_id) for session_id in foreign],
        )
    monkeypatch.setattr(bots, "MAX_BOT_THREADS", 2)
    result = service.threads(bot["bot_id"])
    assert [row["session_id"] for row in result["threads"]] == sorted(included, reverse=True)[:2]
    assert result["has_more"]
    with service.store._connection(write=True) as connection:
        connection.execute(
            "DELETE FROM session_profile_bindings WHERE session_id=?", (included[0],)
        )
    result = service.threads(bot["bot_id"])
    assert [row["session_id"] for row in result["threads"]] == sorted(included[1:], reverse=True)
    assert not result["has_more"]


def test_threads_report_the_cap_and_stable_order(
    service: bots.BotService, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot = _bot(service)
    sessions = [_thread(service, bot) for _ in range(3)]
    monkeypatch.setattr(bots, "MAX_BOT_THREADS", 2)
    result = service.threads(bot["bot_id"])
    assert result["has_more"]
    assert [row["session_id"] for row in result["threads"]] == sorted(sessions, reverse=True)[:2]


def test_thread_route_is_private_and_does_not_prepare_a_chat(service: bots.BotService) -> None:
    rig = Rig(service)
    session = _thread(service, rig.bot)
    path = f"{BASE}/{rig.bot['bot_id']}/threads"
    with TestClient(rig.app()) as client:
        assert client.get(path).status_code == 401
        assert (
            client.get(path, headers={**BEARER, "Origin": "https://example.invalid"}).status_code
            == 403
        )
        assert client.get(path + "?all=true", headers=BEARER).status_code == 400
        response = client.get(path, headers=BEARER)
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert response.json()["threads"][0]["session_id"] == session
    assert rig.prepare_calls == 0 and rig.inspected == []
    assert "instructions" not in response.text and "request_hash" not in response.text
