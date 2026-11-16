"""The Decision loop's settings keys: registered in
`viki_agent.app.settings.KEYS`, proved through a real `SettingsService` so the write-time
validators and `decision_loop.contracts.parse_settings`'s read-time parsing agree on what a
value means -- the end-to-end path neither `test_contracts.py` (hand-built dicts, no service)
nor `test_settings.py` (the service, but never `parse_settings`) covers alone.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

from viki_agent.app import settings as settings_module
from viki_agent.app.settings import SettingsService
from viki_agent.decision_loop.contracts import DecisionSettings, parse_settings
from viki_agent.state import AppState, Prefs
from viki_agent.storage import SessionStore


def _store(tmp_path: Path) -> SessionStore:
    return AppState(tmp_path / "state.json", Prefs()).store


def _service(store: SessionStore) -> SettingsService:
    return SettingsService(
        store, provider_defaults=lambda: Prefs(), on_chat_defaults=lambda _prefs: None
    )


def test_every_decision_key_round_trips_through_the_service_into_parse_settings(
    tmp_path: Path,
) -> None:
    """Patching every Decision key to a non-default value through the real service, then feeding
    the saved `effective` map into `parse_settings`, reaches the same values `DecisionSettings`
    names -- proving `KEYS`' validators store exactly what `parse_settings` expects to read."""
    service = _service(_store(tmp_path))
    session_id = str(uuid.uuid4())
    saved, _replayed = service.patch(
        str(uuid.uuid4()),
        0,
        {
            "decision.enabled": True,
            "decision.paused_until": "2026-10-21T10:00:00.000Z",
            "decision.focus_until": "2026-10-21T09:30:00.500Z",
            "decision.send_level": "sections",
            "decision.never_send": '["Private/", "Private/", "#health"]',  # a duplicate to drop
            "decision.watch.tasks": False,
            "decision.watch.inbox": False,
            "decision.watch.vault_changes": False,
            "decision.watch.approvals": False,
            "decision.watch.chats": False,
            "decision.watch.routines": False,
            "decision.watch.improve": False,
            "decision.watch.kanban": False,
            "decision.cadence": "15m",
            "decision.daily_cap_usd": 2,
            "decision.nudges_per_hour": 0,
            "decision.alerts": True,
            "decision.in_context": False,
            "decision.model_version": "preview",
            "decision.grant.prepare": "off",
            "decision.grant.run": "allowed",
            "decision.grant.team": "allowed",
            "decision.grant.look": "off",
            "decision.grant.second_look": "ask",
            "decision.grant.project_plan": "off",
            "decision.grant.check": "allowed",
            "decision.grant.inbox_triage": "ask",
            "decision.limit.drafts": 0,
            "decision.limit.chats": 10,
            "decision.limit.changes": 1,
            "decision.plan_brake": "off",
            "decision.limit.team": 9,
            "decision.team.handoffs": True,
            "decision.limit.look": 7,
            "decision.limit.second_look": 4,
            "decision.limit.project_plan": 5,
            "decision.limit.message_draft": 6,
            "decision.limit.check": 7,
            "decision.limit.inbox_triage": 8,
            "decision.learning": False,
            "decision.shadow": "pinned",
            "decision.launcher_session": session_id,
        },
    )
    # The duplicate is dropped, order kept; stored canonical (compact, UTF-8 kept as sent).
    assert saved.effective["decision.never_send"] == '["Private/","#health"]'
    assert parse_settings(saved.effective) == DecisionSettings(
        enabled=True,
        paused_until=datetime(2026, 10, 21, 10, 0, tzinfo=UTC),
        focus_until=datetime(2026, 10, 21, 9, 30, 0, 500_000, tzinfo=UTC),
        send_level="sections",
        never_send=("Private/", "#health"),
        watch_tasks=False,
        watch_inbox=False,
        watch_vault_changes=False,
        watch_approvals=False,
        watch_chats=False,
        watch_routines=False,
        watch_improve=False,
        watch_kanban=False,
        cadence="15m",
        daily_cap_usd=2.0,
        nudges_per_hour=0,
        alerts=True,
        in_context=False,
        model_version="preview",
        grant_prepare="off",
        grant_run="allowed",
        grant_team="allowed",
        grant_look="off",
        grant_second_look="ask",
        grant_project_plan="off",
        grant_check="allowed",
        grant_inbox_triage="ask",
        limit_drafts=0,
        limit_chats=10,
        limit_changes=1,
        plan_brake="off",
        limit_team=9,
        team_handoffs=True,
        limit_look=7,
        limit_second_look=4,
        limit_project_plan=5,
        limit_message_draft=6,
        limit_check=7,
        limit_inbox_triage=8,
        learning=False,
        shadow="pinned",
        launcher_session=session_id,
        quiet=None,
        never_send_invalid=False,
    )


def test_decision_keys_at_their_defaults_parse_to_the_registered_defaults(tmp_path: Path) -> None:
    service = _service(_store(tmp_path))
    assert parse_settings(service.read().effective) == DecisionSettings()


def test_look_switches_default_on_round_trip_and_refuse_a_non_bool(tmp_path: Path) -> None:
    service = _service(_store(tmp_path))
    revision = 0
    for key, field in (
        ("decision.watch.vault_health", "watch_vault_health"),
        ("decision.watch.messaging", "watch_messaging"),
    ):
        assert service.read().effective[key] is True
        saved, _replayed = service.patch(str(uuid.uuid4()), revision, {key: False})
        assert getattr(parse_settings(saved.effective), field) is False
        revision = saved.document.revision
        with pytest.raises(settings_module.SettingsError) as failure:
            service.patch(str(uuid.uuid4()), revision, {key: "yes"})
        assert failure.value.fields == {key: "invalid_type"}


def test_look_keys_default_to_allowed_and_three_round_trip_and_sit_in_order(
    tmp_path: Path,
) -> None:
    service = _service(_store(tmp_path))
    effective = service.read().effective
    assert (effective["decision.grant.look"], effective["decision.limit.look"]) == ("allowed", 3)
    assert (parse_settings(effective).grant_look, parse_settings(effective).limit_look) == (
        "allowed",
        3,
    )
    saved, _replayed = service.patch(
        str(uuid.uuid4()), 0, {"decision.grant.look": "ask", "decision.limit.look": 0}
    )
    parsed = parse_settings(saved.effective)
    assert (parsed.grant_look, parsed.limit_look) == ("ask", 0)
    for key, bad in (("decision.grant.look", "sometimes"), ("decision.limit.look", 11)):
        with pytest.raises(settings_module.SettingsError) as failure:
            service.patch(str(uuid.uuid4()), saved.document.revision, {key: bad})
        assert key in failure.value.fields
    keys = list(settings_module.KEYS)
    assert keys[keys.index("decision.grant.team") + 1] == "decision.grant.look"
    assert keys[keys.index("decision.team.handoffs") + 1] == "decision.limit.look"


def test_second_look_keys_default_to_allowed_and_three_round_trip_and_sit_in_order(
    tmp_path: Path,
) -> None:
    service = _service(_store(tmp_path))
    effective = service.read().effective
    assert (effective["decision.grant.second_look"], effective["decision.limit.second_look"]) == (
        "allowed",
        3,
    )
    defaults = parse_settings(effective)
    assert (defaults.grant_second_look, defaults.limit_second_look) == ("allowed", 3)
    saved, _replayed = service.patch(
        str(uuid.uuid4()),
        0,
        {"decision.grant.second_look": "ask", "decision.limit.second_look": 0},
    )
    parsed = parse_settings(saved.effective)
    assert (parsed.grant_second_look, parsed.limit_second_look) == ("ask", 0)
    for key, bad in (
        ("decision.grant.second_look", "sometimes"),
        ("decision.limit.second_look", 11),
    ):
        with pytest.raises(settings_module.SettingsError) as failure:
            service.patch(str(uuid.uuid4()), saved.document.revision, {key: bad})
        assert key in failure.value.fields
    keys = list(settings_module.KEYS)
    assert keys[keys.index("decision.grant.look") + 1] == "decision.grant.second_look"
    assert keys[keys.index("decision.limit.look") + 1] == "decision.limit.second_look"


def test_project_plan_keys_default_to_allowed_and_three_round_trip_and_sit_in_order(
    tmp_path: Path,
) -> None:
    service = _service(_store(tmp_path))
    effective = service.read().effective
    assert (
        effective["decision.grant.project_plan"],
        effective["decision.limit.project_plan"],
    ) == ("allowed", 3)
    defaults = parse_settings(effective)
    assert (defaults.grant_project_plan, defaults.limit_project_plan) == ("allowed", 3)
    saved, _replayed = service.patch(
        str(uuid.uuid4()),
        0,
        {"decision.grant.project_plan": "ask", "decision.limit.project_plan": 0},
    )
    parsed = parse_settings(saved.effective)
    assert (parsed.grant_project_plan, parsed.limit_project_plan) == ("ask", 0)
    for key, bad in (
        ("decision.grant.project_plan", "sometimes"),
        ("decision.limit.project_plan", 11),
    ):
        with pytest.raises(settings_module.SettingsError) as failure:
            service.patch(str(uuid.uuid4()), saved.document.revision, {key: bad})
        assert key in failure.value.fields
    keys = list(settings_module.KEYS)
    assert keys[keys.index("decision.grant.second_look") + 1] == "decision.grant.project_plan"
    assert keys[keys.index("decision.limit.second_look") + 1] == "decision.limit.project_plan"


def test_message_draft_limit_defaults_to_three_round_trips_and_sits_in_order(
    tmp_path: Path,
) -> None:
    service = _service(_store(tmp_path))
    effective = service.read().effective
    assert effective["decision.limit.message_draft"] == 3
    assert parse_settings(effective).limit_message_draft == 3
    saved, _replayed = service.patch(str(uuid.uuid4()), 0, {"decision.limit.message_draft": 0})
    assert parse_settings(saved.effective).limit_message_draft == 0
    for bad in (11, "3"):
        with pytest.raises(settings_module.SettingsError) as failure:
            service.patch(
                str(uuid.uuid4()), saved.document.revision, {"decision.limit.message_draft": bad}
            )
        assert "decision.limit.message_draft" in failure.value.fields
    keys = list(settings_module.KEYS)
    assert keys[keys.index("decision.limit.project_plan") + 1] == "decision.limit.message_draft"


def test_check_keys_default_to_ask_and_three_round_trip_and_sit_in_order(
    tmp_path: Path,
) -> None:
    service = _service(_store(tmp_path))
    effective = service.read().effective
    assert (effective["decision.grant.check"], effective["decision.limit.check"]) == ("ask", 3)
    defaults = parse_settings(effective)
    assert (defaults.grant_check, defaults.limit_check) == ("ask", 3)
    saved, _replayed = service.patch(
        str(uuid.uuid4()), 0, {"decision.grant.check": "allowed", "decision.limit.check": 0}
    )
    parsed = parse_settings(saved.effective)
    assert (parsed.grant_check, parsed.limit_check) == ("allowed", 0)
    for key, bad in (("decision.grant.check", "sometimes"), ("decision.limit.check", 11)):
        with pytest.raises(settings_module.SettingsError) as failure:
            service.patch(str(uuid.uuid4()), saved.document.revision, {key: bad})
        assert key in failure.value.fields
    keys = list(settings_module.KEYS)
    assert keys[keys.index("decision.grant.project_plan") + 1] == "decision.grant.check"
    assert keys[keys.index("decision.limit.message_draft") + 1] == "decision.limit.check"


def test_inbox_triage_keys_default_to_allowed_and_three_round_trip_and_sit_in_order(
    tmp_path: Path,
) -> None:
    service = _service(_store(tmp_path))
    effective = service.read().effective
    assert (
        effective["decision.grant.inbox_triage"],
        effective["decision.limit.inbox_triage"],
    ) == ("allowed", 3)
    defaults = parse_settings(effective)
    assert (defaults.grant_inbox_triage, defaults.limit_inbox_triage) == ("allowed", 3)
    saved, _replayed = service.patch(
        str(uuid.uuid4()),
        0,
        {"decision.grant.inbox_triage": "ask", "decision.limit.inbox_triage": 0},
    )
    parsed = parse_settings(saved.effective)
    assert (parsed.grant_inbox_triage, parsed.limit_inbox_triage) == ("ask", 0)
    for key, bad in (
        ("decision.grant.inbox_triage", "sometimes"),
        ("decision.limit.inbox_triage", 11),
    ):
        with pytest.raises(settings_module.SettingsError) as failure:
            service.patch(str(uuid.uuid4()), saved.document.revision, {key: bad})
        assert key in failure.value.fields
    keys = list(settings_module.KEYS)
    assert keys[keys.index("decision.grant.check") + 1] == "decision.grant.inbox_triage"
    assert keys[keys.index("decision.limit.check") + 1] == "decision.limit.inbox_triage"


def test_look_switches_sit_after_watch_kanban() -> None:
    keys = list(settings_module.KEYS)
    kanban = keys.index("decision.watch.kanban")
    assert keys[kanban + 1 : kanban + 3] == [
        "decision.watch.vault_health",
        "decision.watch.messaging",
    ]


@pytest.mark.parametrize(
    "stored",
    [
        "not json",
        json.dumps([f"f{index}/" for index in range(40)]),  # valid JSON, too many entries
    ],
)
def test_a_stored_unreadable_never_send_fails_closed_not_open(tmp_path: Path, stored: str) -> None:
    """A `decision.never_send` value already in storage that the current schema can no longer
    validate must not silently fall back to the permissive default "[]" (send everything): it
    reads as a fixed invalid-JSON sentinel, so `parse_settings` reports `never_send_invalid` and
    the engine blocks every send, rather than leaking past a corrupted restriction list."""
    store = _store(tmp_path)
    with store._connection(write=True) as connection:
        connection.execute(
            "INSERT INTO settings (scope,version,revision,document,updated_at) "
            "VALUES ('app',?,?,?,?)",
            (
                settings_module.SETTINGS_VERSION,
                1,
                json.dumps({"decision.never_send": stored}),
                "2026-01-01T00:00:00.000Z",
            ),
        )
    service = _service(store)
    read = service.read()
    assert read.effective["decision.never_send"] == settings_module._NEVER_SEND_UNREADABLE
    assert read.sources["decision.never_send"] == "setting"  # never silently "default"
    parsed = parse_settings(read.effective)
    assert (parsed.never_send, parsed.never_send_invalid) == ((), True)
