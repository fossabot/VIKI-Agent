from __future__ import annotations

import json
from typing import Any

import pytest

from tests.profiles.test_profile_launch import document, ident
from tests.profiles.test_profile_launch import setup as setup
from viki_agent.app.settings import SettingsError
from viki_agent.automation.routines import RoutineService
from viki_agent.profiles.service import ProfileService


def test_connection_pinned_routine_preview_uses_native_default(
    setup: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    launch, state, _vault, profile_id, workspace_id = setup
    settings = launch.settings
    connection_ref = "viki-llm/11111111-1111-4111-8111-1111111111ab/endpoint-model"
    settings.patch(ident(), 0, {"chat.default_model": connection_ref})
    profile = document()
    profile["runtime"]["model"] = connection_ref
    ProfileService(state.store).revise(profile_id, ident(), 1, profile)

    calls: list[str | None] = []

    def native_chat_model(model: str | None) -> str:
        calls.append(model)
        return "native-default"

    monkeypatch.setattr(settings, "native_chat_model", native_chat_model)
    service = RoutineService(state.store, ProfileService(state.store), settings, launch.workspaces)
    resolved = service.resolve(
        {
            "profile_id": profile_id,
            "workspace_id": workspace_id,
            "prompt": "Synthetic",
            "deadline_minutes": 5,
        }
    )

    assert calls == [connection_ref]
    assert resolved["profile"]["model"] == "native-default"
    assert resolved["profile"]["connection_pinned"] is True
    assert connection_ref not in json.dumps(resolved)


def test_connection_pinned_preview_keeps_model_null_when_settings_unreadable(
    setup: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    launch, state, _vault, profile_id, workspace_id = setup
    connection_ref = "viki-llm/11111111-1111-4111-8111-1111111111ab/endpoint-model"
    profile = document()
    profile["runtime"]["model"] = connection_ref
    ProfileService(state.store).revise(profile_id, ident(), 1, profile)

    def unreadable() -> Any:
        raise SettingsError("corrupt")

    monkeypatch.setattr(launch.settings, "read", unreadable)
    service = RoutineService(
        state.store,
        ProfileService(state.store),
        launch.settings,
        launch.workspaces,
    )
    resolved = service.resolve(
        {
            "profile_id": profile_id,
            "workspace_id": workspace_id,
            "prompt": "Synthetic",
            "deadline_minutes": 5,
        }
    )

    assert resolved["profile"]["model"] is None
    assert resolved["profile"]["connection_pinned"] is True
    assert connection_ref not in json.dumps(resolved)
