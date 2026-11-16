"""A retained memory owner cannot become a replacement Bot's identity."""

from __future__ import annotations

import pytest

from tests.messaging.test_bots import _bind_chat, _bot, _id, _profile
from tests.messaging.test_bots import service as service
from viki_agent.messaging.bots import BotError, BotService


@pytest.mark.parametrize("operation", ["remove", "reset"])
@pytest.mark.parametrize("different_profile", [False, True])
def test_retired_memory_identity_cannot_be_created_again(
    service: BotService, operation: str, different_profile: bool
) -> None:
    owner = _bot(service)
    _bind_chat(service, owner)
    with service.store._connection(write=True) as connection:
        connection.execute(
            "INSERT INTO session_bot_memory VALUES (?,?)",
            (owner["chat_session_id"], owner["bot_id"]),
        )
    replay, replayed = service.create(
        owner["bot_id"], owner["profile_id"], 1, owner["workspace_id"]
    )
    assert replayed and replay["chat_session_id"] == owner["chat_session_id"]
    if operation == "remove":
        service.remove(owner["bot_id"], owner["state_revision"])
    else:
        service.reset(owner["workspace_id"])

    profile = _profile(service) if different_profile else owner["profile_id"]
    with pytest.raises(BotError, match="command_conflict"):
        service.create(owner["bot_id"], profile, 1, owner["workspace_id"])

    replacement, replayed = service.create(_id(), profile, 1, owner["workspace_id"])
    assert not replayed and replacement["bot_id"] != owner["bot_id"]
    assert replacement["chat_session_id"] != owner["chat_session_id"]
    with service.store._connection() as connection:
        assert connection.execute("SELECT * FROM session_bot_memory").fetchall() == [
            (owner["chat_session_id"], owner["bot_id"])
        ]
