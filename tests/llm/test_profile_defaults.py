from __future__ import annotations

from typing import Any

import pytest

from tests.llm.test_connections import FORM
from tests.profiles.test_profile_launch import ident
from tests.profiles.test_profile_launch import setup as setup
from viki_agent.app.settings import SettingsService
from viki_agent.automation.routines import RoutineService
from viki_agent.llm.connections import Connections, parse_connection
from viki_agent.profiles.service import ProfileError
from viki_agent.state import Prefs


@pytest.mark.parametrize("provider_model", [None, "sonnet"])
def test_profile_and_routine_do_not_inherit_ordinary_chat_connection(
    setup: Any, provider_model: str | None
) -> None:
    service, state, _vault, pid, wid = setup
    settings = service.settings = SettingsService(
        state.store,
        provider_defaults=lambda: Prefs(model=provider_model),
        on_chat_defaults=state.set_defaults,
    )
    connection = parse_connection(FORM)
    Connections(settings).write(0, [connection])
    settings.patch(ident(), 1, {"chat.default_model": connection.ref})
    sid = ident()
    prepared = service.prepare(pid, ident(), sid, 1, wid)
    assert prepared["launchable"]
    assert state.prefs(sid).model == provider_model
    routines = RoutineService(state.store, service.profiles, settings, service.workspaces)
    resolved = routines.resolve(
        {"profile_id": pid, "workspace_id": wid, "prompt": "Synthetic", "deadline_minutes": 5}
    )
    assert resolved["profile"]["model"] == provider_model
    # Existing immutable bindings with a custom route must refuse, rather than retarget.
    state.remember(sid, model=connection.ref)
    with pytest.raises(ProfileError, match="launch_blocked"):
        service.validate_launch(sid, state.prefs(sid))
