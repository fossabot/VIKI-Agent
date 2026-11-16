"""The Decision contract: vocabularies and limits, canonical JSON and keys, the settings parser
(defaults, never-send, quiet hours), the errors, and the shared fakes."""

from __future__ import annotations

import asyncio
import json
import math
from dataclasses import FrozenInstanceError, fields, replace
from datetime import UTC, datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

import pytest

from tests.decision_loop.fakes import (
    QUIET_ANSWERS,
    QUIET_NOUL,
    WALL,
    FakeClock,
    FakeHost,
    FakeModel,
    settings_values,
)
from viki_agent.app.settings import KEYS, QUIET_TIMES
from viki_agent.decision_loop import contracts
from viki_agent.decision_loop.contracts import (
    ChoiceAnswer,
    DecisionLoopError,
    DecisionSettings,
    ModelError,
    ModelRequest,
    NoulAnswer,
    canonical_json,
    item_key,
    parse_settings,
    quiet_until,
    sha256_hex,
)
from viki_agent.events import Error, SessionClosed, TurnDone, Usage

if TYPE_CHECKING:
    from viki_agent.automation.scheduler import Clock
    from viki_agent.decision_loop.contracts import MissionHost, MissionSession, ModelPort
    from viki_agent.session import SessionActor, SessionManager

JST = timezone(timedelta(hours=9))
SESSION = "8f14e45f-ceea-467a-9575-0c6fd2b2c0a1"
HANG_GUARD_S = 0.05  # bounds a wait that settles at once; it never fires in a healthy run
# Written as escapes: hiragana a-i-u, for text outside ASCII.
KANA = "\u3042\u3044\u3046"


def _field(key: str) -> str:
    """The `DecisionSettings` field a `decision.*` key fills."""
    return key.removeprefix("decision.").replace(".", "_")


# ---- vocabularies and limits ----------------------------------------------------------------


def test_vocabularies_hold_every_value_the_design_names() -> None:
    assert contracts.CANDIDATES == (
        "approvals",
        "tasks_due",
        "agent_runs",
        "inbox",
        "uncommitted",
        "routine",
        "mission_report",
        "card_needs_you",
        "card_stale",
        "seat_waiting",
        "seat_working",
        "seat_start",
        "seat_report",
        "vault_recovery",
        "memory_drift",
        "bridge_stopped",
        "chat_waiting",
        "send_uncertain",
        "mcp_context",
        "code_look",
        "long_task_look",
        "correction_look",
        "stalled_project",
        "overdue_tasks",
        "context_handoff",
        "source_note",
        "commit_checks",
        "inbox_triage",
        "skill_offer",
        "skill_suggest",
        "learn_source",
        "retro_due",
        "question_tune",
        "note_link",
        "note_superseded",
        "tasks_ticked",
    )
    assert len(contracts.CANDIDATES) == 36
    assert contracts.PROJECT_PLANS == ("stalled_project", "overdue_tasks")
    assert contracts.MESSAGE_DRAFTS == ("context_handoff", "source_note")
    assert contracts.CONTEXT_NEARLY_FULL == 70
    assert contracts.SAVED_CHECKS == ("commit_checks",)
    assert contracts.CHECK_PHASES == ("waiting", "running", "passed", "failed", "stopped")
    assert timedelta(minutes=5) == contracts.CHECK_LOOK_EVERY
    assert timedelta(minutes=10) == contracts.CHECK_HEAD_SETTLE
    assert contracts.MAX_CHECK_FOLDERS == 3
    assert contracts.INBOX_TRIAGE == ("inbox_triage",)
    assert contracts.TRIAGE_PHASES == ("waiting", "running", "ready", "failed", "nothing")
    assert timedelta(minutes=10) == contracts.INBOX_SETTLE
    assert contracts.CARDS == ("card_needs_you", "card_stale")
    assert contracts.SEATS == ("seat_waiting", "seat_working", "seat_start", "seat_report")
    assert contracts.VAULT_LOOKS == ("vault_recovery", "memory_drift")
    assert contracts.APP_LOOKS == (
        "bridge_stopped",
        "chat_waiting",
        "send_uncertain",
        "mcp_context",
    )
    assert contracts.LOOKS == contracts.VAULT_LOOKS + contracts.APP_LOOKS
    assert contracts.LOOK_MISSIONS == ("code_look",)
    assert contracts.SECOND_LOOKS == ("long_task_look", "correction_look")
    assert contracts.SECOND_LOOK_TYPES == {
        "correction": "correction_look",
        "long_session": "long_task_look",
    }
    assert set(contracts.SECOND_LOOK_TYPES) == set(contracts.SKILL_TRIGGERS) - {"repeated_request"}
    assert contracts.FINDING_KINDS == ("flaw", "approach", "missing")
    assert contracts.UNTITLED_CHAT == "This chat"
    assert contracts.MISSION_TYPES == ("tasks_due",)
    quiet = (contracts.CARD_QUIET, contracts.CARD_STALE_WORKING, contracts.CARD_STALE_TODO)
    assert quiet == (timedelta(minutes=30), timedelta(days=1), timedelta(days=3))
    assert (contracts.MAX_CARDS_NEED_YOU, contracts.MAX_CARDS_STALE) == (6, 3)
    assert (*contracts.CANDIDATES, "unwatched") == contracts.TOPICS
    assert contracts.KINDS == ("inform", "ask", "suggest", "ready", "report")
    assert contracts.WORK == ("none", "prepare", "run", "coordinate")
    assert contracts.LEVELS == ("silent", "list", "badge", "nudge", "alert")
    assert contracts.ALPHA == {"silent": 0, "list": 0, "badge": 0.1, "nudge": 1, "alert": 3}
    assert tuple(contracts.ALPHA) == contracts.LEVELS
    assert contracts.TIMINGS == ("now", "at", "after_focus", "in_context")
    assert contracts.FEEDBACK_SHOWN == ("useful", "wrong", "later", "mute")
    assert contracts.FEEDBACK_HELD == ("missed", "right_to_hold")
    assert contracts.FEEDBACK_REASONS == (
        "out_of_date",
        "already_knew",
        "wrong_topic",
        "bad_timing",
    )
    assert contracts.SOURCES == ("brief", "rail", "review", "launcher", "page")
    assert contracts.TRIGGERS == (
        "app_open",
        "new_chat",
        "review",
        "change",
        "launcher",
        "context",
        "heartbeat",
        "mission",
    )
    assert contracts.GRANTS == ("off", "ask", "allowed")
    assert contracts.ACTIONS == (
        "open_chat",
        "open_note",
        "open_approvals",
        "open_inbox",
        "open_routine",
        "open_runs",
        "open_context",
        "open_vault",
        "open_messaging",
        "open_code",
        "open_triage",
        "start_prepare",
        "start_run",
        "stop_mission",
        "start_seat",
        "stop_seat",
        "run_checks",
        "stop_checks",
        "suggest_inbox",
        "draft_chat",
        "draft_new",
    )
    assert len(contracts.ACTIONS) == 21
    assert contracts.ITEM_STATES == ("open", "acted", "dismissed", "stale", "done")
    assert contracts.MISSION_STATES == (
        "starting",
        "running",
        "done",
        "failed",
        "stopped",
        "unknown",
    )
    assert contracts.CALL_OUTCOMES == ("answered", "failed")
    assert contracts.MODEL_ERROR_CODES == (
        "no_key",
        "key_rejected",
        "refused",
        "too_large",
        "rate_limited",
        "overloaded",
        "unreachable",
        "invalid_reply",
        "failed",
    )
    assert contracts.LOOP_ERROR_CODES == (
        "invalid_request",
        "unknown_item",
        "not_applicable",
        "limit",
        "busy",
        "unavailable",
    )


def test_saved_check_records_default_their_optional_fields() -> None:
    folder = contracts.CheckFolder("w", "garden-app", "a" * 40, "waiting")
    assert folder.pending == ()
    assert contracts.CheckFolder("w", "n", "h", "waiting", ("c",)).pending == ("c",)
    assert contracts.CheckLook((folder,)).failures == ()
    assert contracts.CheckLook(None, ("checks_unknown",)).folders is None
    with pytest.raises(FrozenInstanceError):
        folder.phase = "passed"  # type: ignore[misc]


def test_inbox_triage_records_hold_their_fields() -> None:
    run = contracts.InboxRun("r1", "failed", "no_suggestions", "2026-10-21T09:00:00.000Z")
    look = contracts.InboxLook(run, {"00-inbox/a.md": "s"}, {"k": ""})
    assert (look.run, look.answered, look.started) == (run, {"00-inbox/a.md": "s"}, {"k": ""})
    assert contracts.InboxLook(None, {}, {}).run is None
    with pytest.raises(FrozenInstanceError):
        run.state = "done"  # type: ignore[misc]


def test_model_ids_price_and_limits_match_the_design() -> None:
    assert contracts.MODEL_IDS == {
        "pinned": "jev-1.14.0",
        "latest": "jev-latest",
        "preview": "jev-preview",
    }
    assert contracts.PRICE_USD_PER_MTOK == 0.042
    assert (contracts.REQUEST_LIMIT, contracts.QUESTION_LIMIT) == (64_000, 32_000)
    assert (contracts.MAX_IN_FLIGHT, contracts.MAX_PER_MINUTE) == (2, 10)
    assert (contracts.OBSERVE_S, contracts.HEARTBEAT_S, contracts.COALESCE_S) == (60, 300, 2)
    assert contracts.RECHECK_S == 3.0
    assert (contracts.NUDGE_CAP, contracts.TYPING_HOLD_S, contracts.AWAY_S) == (2, 3, 300)
    assert contracts.ESCALATE_S == 1800
    assert (contracts.MAX_RUNNING_MISSIONS, contracts.MISSION_MINUTES) == (2, 30)
    assert (contracts.MAX_OPEN_ITEMS, contracts.MAX_CALLS) == (50, 20)
    assert (contracts.MAX_EPISODES, contracts.RETENTION_DAYS) == (10_000, 365)
    assert timedelta(hours=4) == contracts.LATER
    assert contracts.STATE_BYTES == 12_000
    assert contracts.EXCERPT == {"titles": 0, "excerpts": 200, "sections": 2000}


# ---- canonical JSON, hashes and keys ---------------------------------------------------------


def test_canonical_json_sorts_keys_compacts_and_escapes() -> None:
    value = {
        "b": [1, 2.5, None, True],
        "a": "caf\u00e9 " + KANA,
        "at": datetime(2026, 10, 21, 9, 0, tzinfo=UTC),
        "pair": ("x", 1),
    }
    assert canonical_json(value) == (
        '{"a":"caf\\u00e9 \\u3042\\u3044\\u3046","at":"2026-10-21T09:00:00+00:00",'
        '"b":[1,2.5,null,true],"pair":["x",1]}'
    )
    assert canonical_json(value).isascii()


@pytest.mark.parametrize(
    ("value", "error"),
    [({"x": math.nan}, ValueError), ({"x": math.inf}, ValueError), ({"x": {1, 2}}, TypeError)],
)
def test_canonical_json_refuses_what_json_cannot_hold(
    value: object, error: type[Exception]
) -> None:
    with pytest.raises(error):
        canonical_json(value)


def test_sha256_hex_and_item_key() -> None:
    assert sha256_hex("") == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    assert sha256_hex(KANA) == sha256_hex("\u3042\u3044\u3046")  # the UTF-8 bytes are hashed
    assert item_key("inbox", SESSION) == sha256_hex("inbox\x1f" + SESSION)
    assert item_key("ab") != item_key("a", "b") != item_key("b", "a")
    assert len(item_key("inbox")) == 64


# ---- settings ---------------------------------------------------------------------------------


def test_missing_values_read_as_the_registered_defaults() -> None:
    default = DecisionSettings()
    assert parse_settings({}) == default
    assert parse_settings(settings_values()) == default
    assert default == DecisionSettings(
        enabled=False,
        paused_until=None,
        focus_until=None,
        send_level="excerpts",
        never_send=(),
        watch_tasks=True,
        watch_inbox=True,
        watch_vault_changes=True,
        watch_approvals=True,
        watch_chats=True,
        watch_routines=True,
        watch_improve=True,
        watch_kanban=True,
        watch_vault_health=True,
        watch_messaging=True,
        cadence="change",
        daily_cap_usd=0.5,
        nudges_per_hour=2,
        alerts=False,
        in_context=True,
        model_version="pinned",
        grant_prepare="allowed",
        grant_run="ask",
        grant_team="ask",
        grant_look="allowed",
        grant_second_look="allowed",
        grant_project_plan="allowed",
        grant_check="ask",
        grant_inbox_triage="allowed",
        limit_drafts=5,
        limit_chats=3,
        limit_changes=3,
        plan_brake="80",
        limit_team=4,
        team_handoffs=False,
        limit_look=3,
        limit_second_look=3,
        limit_project_plan=3,
        limit_message_draft=3,
        limit_check=3,
        limit_inbox_triage=3,
        learning=True,
        shadow="off",
        launcher_session="",
        quiet=None,
        never_send_invalid=False,
    )


def test_every_key_is_read() -> None:
    values = settings_values(
        **{
            "decision.enabled": True,
            "decision.paused_until": "2026-10-21T10:00:00.000Z",
            "decision.focus_until": "2026-10-21T09:30:00.500Z",
            "decision.send_level": "sections",
            "decision.never_send": '["Private/","#health"]',
            "decision.watch.tasks": False,
            "decision.watch.inbox": False,
            "decision.watch.vault_changes": False,
            "decision.watch.approvals": False,
            "decision.watch.chats": False,
            "decision.watch.routines": False,
            "decision.cadence": "15m",
            "decision.daily_cap_usd": 2,
            "decision.nudges_per_hour": 0,
            "decision.alerts": True,
            "decision.in_context": False,
            "decision.model_version": "preview",
            "decision.grant.prepare": "off",
            "decision.grant.run": "allowed",
            "decision.grant.team": "off",
            "decision.grant.look": "ask",
            "decision.grant.second_look": "off",
            "decision.grant.project_plan": "ask",
            "decision.grant.check": "allowed",
            "decision.grant.inbox_triage": "ask",
            "decision.limit.drafts": 0,
            "decision.limit.chats": 10,
            "decision.limit.changes": 2,
            "decision.plan_brake": "95",
            "decision.limit.team": 7,
            "decision.team.handoffs": True,
            "decision.limit.look": 1,
            "decision.limit.second_look": 2,
            "decision.limit.project_plan": 1,
            "decision.limit.message_draft": 4,
            "decision.limit.check": 5,
            "decision.limit.inbox_triage": 2,
            "decision.learning": False,
            "decision.shadow": "latest",
            "decision.launcher_session": SESSION,
            "notifications.quiet_start": "22:00",
            "notifications.quiet_end": "07:00",
        }
    )
    values["decision.watch.improve"] = False  # outside `settings_values`' own map
    values["decision.watch.kanban"] = False
    values["decision.watch.vault_health"] = False
    values["decision.watch.messaging"] = False
    parsed = parse_settings(values)
    assert parsed == DecisionSettings(
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
        watch_vault_health=False,
        watch_messaging=False,
        cadence="15m",
        daily_cap_usd=2.0,
        nudges_per_hour=0,
        alerts=True,
        in_context=False,
        model_version="preview",
        grant_prepare="off",
        grant_run="allowed",
        grant_team="off",
        grant_look="ask",
        grant_second_look="off",
        grant_project_plan="ask",
        grant_check="allowed",
        grant_inbox_triage="ask",
        limit_drafts=0,
        limit_chats=10,
        limit_changes=2,
        plan_brake="95",
        limit_team=7,
        team_handoffs=True,
        limit_look=1,
        limit_second_look=2,
        limit_project_plan=1,
        limit_message_draft=4,
        limit_check=5,
        limit_inbox_triage=2,
        learning=False,
        shadow="latest",
        launcher_session=SESSION,
        quiet=("22:00", "07:00"),
        never_send_invalid=False,
    )
    unchanged = {
        field.name
        for field in fields(DecisionSettings)
        if getattr(parsed, field.name) == getattr(DecisionSettings(), field.name)
    }
    assert unchanged == {"never_send_invalid"}  # every key above moved its own field


def test_numbers_take_their_field_types() -> None:
    parsed = parse_settings(
        settings_values(
            **{
                "decision.daily_cap_usd": 1,
                "decision.nudges_per_hour": 1.0,
                "decision.limit.drafts": 7.0,
            }
        )
    )
    assert (parsed.daily_cap_usd, parsed.nudges_per_hour, parsed.limit_drafts) == (1.0, 1, 7)
    assert type(parsed.daily_cap_usd) is float
    assert type(parsed.nudges_per_hour) is int and type(parsed.limit_drafts) is int


def test_limit_changes_defaults_to_three_and_reads_through() -> None:
    """`decision.limit.changes` (the daily cap on vault-change proposals) defaults to 3
    and reads any listed value, 0 included (0 turns the proposals off)."""
    assert DecisionSettings().limit_changes == 3
    assert parse_settings({}).limit_changes == 3
    assert parse_settings(settings_values()).limit_changes == 3
    assert parse_settings(settings_values(**{"decision.limit.changes": 0})).limit_changes == 0


def test_team_settings_read_through_and_fall_back_on_bad_values() -> None:
    assert (DecisionSettings().grant_team, DecisionSettings().limit_team) == ("ask", 4)
    assert DecisionSettings().team_handoffs is False
    assert parse_settings({}).limit_team == 4
    assert parse_settings(settings_values(**{"decision.limit.team": 0})).limit_team == 0
    assert parse_settings(settings_values(**{"decision.limit.team": 11})).limit_team == 4
    assert parse_settings(settings_values(**{"decision.grant.team": "x"})).grant_team == "ask"
    assert (
        parse_settings(settings_values(**{"decision.team.handoffs": "yes"})).team_handoffs is False
    )


def test_plan_brake_defaults_to_80_and_reads_each_choice() -> None:
    assert DecisionSettings().plan_brake == "80"
    assert parse_settings({}).plan_brake == "80"
    for choice in contracts.PLAN_BRAKES:
        assert (
            parse_settings(settings_values(**{"decision.plan_brake": choice})).plan_brake == choice
        )
    for bad in ("70", 80, True, None):
        assert parse_settings(settings_values(**{"decision.plan_brake": bad})).plan_brake == "80"


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("decision.enabled", "true"),
        ("decision.enabled", 1),
        ("decision.alerts", None),
        ("decision.watch.inbox", 0),
        ("decision.send_level", "everything"),
        ("decision.send_level", None),
        ("decision.cadence", "1m"),
        ("decision.daily_cap_usd", True),
        ("decision.daily_cap_usd", "0.5"),
        ("decision.daily_cap_usd", 100),
        ("decision.daily_cap_usd", math.nan),
        ("decision.nudges_per_hour", 3),
        ("decision.nudges_per_hour", False),
        ("decision.model_version", "jev-1.14.0"),
        ("decision.grant.prepare", "always"),
        ("decision.grant.run", ["allowed"]),
        ("decision.limit.drafts", 11),
        ("decision.limit.chats", -1),
        ("decision.limit.chats", 2.5),
        ("decision.limit.changes", 11),
        ("decision.limit.changes", -1),
        ("decision.learning", "true"),
        ("decision.learning", None),
        ("decision.shadow", "jev-latest"),
        ("decision.shadow", None),
        ("decision.paused_until", "2026-10-21T10:00:00Z"),
        ("decision.paused_until", "2026-10-21 10:00:00.000Z"),
        ("decision.paused_until", "2026-10-21T10:00:00.000+00:00"),
        ("decision.paused_until", "2026-13-21T10:00:00.000Z"),
        ("decision.paused_until", 1_761_040_800),
        ("decision.focus_until", "tomorrow"),
        ("decision.launcher_session", SESSION.upper()),
        ("decision.launcher_session", "{" + SESSION + "}"),
        ("decision.launcher_session", 42),
    ],
)
def test_wrong_typed_or_unlisted_values_read_as_defaults(key: str, value: object) -> None:
    assert parse_settings(settings_values(**{key: value})) == DecisionSettings()


@pytest.mark.parametrize(
    "stored",
    [
        "[]",
        '["Private/"]',
        '["#health", "Clients/Acme/", "#"]',
        pytest.param('["' + "a" * 127 + '/"]', id="128-characters"),
    ],
)
def test_never_send_keeps_every_entry_in_order(stored: str) -> None:
    parsed = parse_settings(settings_values(**{"decision.never_send": stored}))
    assert (parsed.never_send, parsed.never_send_invalid) == (tuple(json.loads(stored)), False)


@pytest.mark.parametrize(
    "stored",
    [
        "not json",
        "",
        "{}",
        '"Private/"',
        "[1]",
        '["Private"]',
        '[""]',
        '["Private/", null]',
        pytest.param('["' + "a" * 128 + '/"]', id="129-characters"),
        '["a\\u0007/"]',
        '["#a\\nb"]',
        pytest.param("[" + ",".join(f'"f{index}/"' for index in range(33)) + "]", id="33-entries"),
        pytest.param("[" * 100_000 + "]" * 100_000, id="nested-too-deep"),
        None,
        42,
        ["Private/"],
    ],
)
def test_unusable_never_send_sends_nothing(stored: object) -> None:
    parsed = parse_settings(settings_values(**{"decision.never_send": stored}))
    assert (parsed.never_send, parsed.never_send_invalid) == ((), True)
    assert replace(parsed, never_send_invalid=False) == DecisionSettings()


def test_thirty_two_never_send_entries_fit() -> None:
    stored = "[" + ",".join(f'"f{index}/"' for index in range(32)) + "]"
    parsed = parse_settings(settings_values(**{"decision.never_send": stored}))
    assert (len(parsed.never_send), parsed.never_send_invalid) == (32, False)


@pytest.mark.parametrize(
    ("start", "end", "quiet"),
    [
        ("22:00", "07:00", ("22:00", "07:00")),
        ("09:30", "17:00", ("09:30", "17:00")),
        ("00:00", "23:30", ("00:00", "23:30")),
        (None, "07:00", None),
        ("22:00", None, None),
        (None, None, None),
        ("22:00", "22:00", None),
        ("22:15", "07:00", None),
        ("24:00", "07:00", None),
        ("7:00", "22:00", None),
        (2200, "07:00", None),
    ],
)
def test_quiet_hours_need_two_different_listed_times(
    start: object, end: object, quiet: tuple[str, str] | None
) -> None:
    values = settings_values(**{"notifications.quiet_start": start, "notifications.quiet_end": end})
    assert parse_settings(values).quiet == quiet


def test_every_registered_quiet_time_is_accepted() -> None:
    for time in QUIET_TIMES:
        other = "12:00" if time != "12:00" else "13:00"
        values = settings_values(
            **{"notifications.quiet_start": time, "notifications.quiet_end": other}
        )
        assert parse_settings(values).quiet == (time, other)


def test_defaults_agree_with_the_settings_registry() -> None:
    """The registered Decision keys (when present) each fill a field, their defaults parse to
    `DecisionSettings()`, and every value they allow reads through unchanged."""
    registered = {key: spec for key, spec in KEYS.items() if key.startswith("decision.")}
    names = {field.name for field in fields(DecisionSettings)}
    assert {_field(key) for key in registered} <= names
    assert parse_settings({key: spec.default for key, spec in KEYS.items()}) == DecisionSettings()
    for key, spec in registered.items():
        allowed = (True, False) if spec.kind == "bool" else spec.allowed or ()
        for value in allowed:
            assert getattr(parse_settings({key: value}), _field(key)) == value, (key, value)


def test_parse_settings_reads_learning_and_shadow() -> None:
    """Learning defaults on and the shadow defaults off; each reads through, and a
    wrong-typed or unlisted value falls back to those same defaults."""
    assert (DecisionSettings().learning, DecisionSettings().shadow) == (True, "off")
    for setting in contracts.SHADOWS:
        parsed = parse_settings(
            settings_values(**{"decision.learning": False, "decision.shadow": setting})
        )
        assert (parsed.learning, parsed.shadow) == (False, setting)
    defaulted = parse_settings(
        settings_values(**{"decision.learning": "on", "decision.shadow": "jev-latest"})
    )
    assert (defaulted.learning, defaulted.shadow) == (True, "off")


# ---- self-improvement -------------------------------------------------------------------------


def test_improve_contract() -> None:
    """The self-improvement types, the two draft actions and their text, the skill hint a page
    reports, a tried wording's numbers and the Self-improvement switch."""
    assert contracts.IMPROVE == (
        "skill_offer",
        "skill_suggest",
        "learn_source",
        "retro_due",
        "question_tune",
        "note_link",
        "note_superseded",
        "tasks_ticked",
    )
    assert contracts.CHANGES == ("note_link", "note_superseded", "tasks_ticked")
    assert contracts.PAIRED == ("note_link", "note_superseded")
    assert contracts.SKILL_TRIGGERS == ("correction", "repeated_request", "long_session")
    assert contracts.ACTIONS[-2:] == ("draft_chat", "draft_new")
    assert contracts.ActionSpec("open_chat", "Open chat", True).text is None
    draft = contracts.ActionSpec("draft_new", "Draft it in a new chat", True, text="a\n\tb")
    assert draft.text == "a\n\tb"

    assert [field.name for field in fields(contracts.SkillHint)] == [
        "trigger",
        "sessions",
        "titles",
    ]
    hint = contracts.SkillHint("long_session", (SESSION,), ("Weekly review",))
    page = contracts.PageFacts(0, 0, 0, "unknown", None, None, None, None)
    assert page.skill is None
    assert replace(page, skill=hint).skill == hint

    assert [field.name for field in fields(contracts.SuggestHint)] == [
        "session",
        "title",
        "used",
    ]
    suggest = contracts.SuggestHint(SESSION, "Weekly review", ("sprint-retro",))
    assert page.suggest is None
    assert replace(page, suggest=suggest).suggest == suggest

    assert [field.name for field in fields(contracts.TrialStats)] == [
        "name",
        "decisions",
        "agree",
        "labels",
        "live",
        "trial",
    ]
    live = contracts.QuestionRate("goal_link", 4, 0.75, 6, 0.5)
    tried = contracts.TrialStats("goal_link", 10, 8, 10, live, replace(live, yes_n=5, no_n=5))
    assert (tried.live.yes_n, tried.trial.yes_n) == (4, 5)

    assert DecisionSettings().watch_improve is True
    assert parse_settings({"decision.watch.improve": False}).watch_improve is False
    for wrong in ("false", 0, None):
        assert parse_settings({"decision.watch.improve": wrong}).watch_improve is True

    assert DecisionSettings().watch_kanban is True
    assert parse_settings({"decision.watch.kanban": False}).watch_kanban is False
    for wrong in ("false", 0, None):
        assert parse_settings({"decision.watch.kanban": wrong}).watch_kanban is True


def test_look_records_default_to_no_failures() -> None:
    """The Look records are frozen, and a part left out of a read names no failure."""
    spans = (
        contracts.SAVE_STOPPED_AFTER,
        contracts.RECOVERY_EXPIRY_WARNING,
        contracts.CHAT_WAIT,
        contracts.UNCERTAIN_SEND_WINDOW,
    )
    assert spans == (
        timedelta(minutes=2),
        timedelta(days=3),
        timedelta(minutes=5),
        timedelta(days=1),
    )
    assert contracts.RECOVERY_FULL_SHARE == 0.9
    assert (contracts.MAX_CHATS_WAITING, contracts.MAX_UNCERTAIN_SENDS) == (6, 5)
    assert (contracts.MCP_CONTEXT_SHARE, contracts.MAX_MCP_CONTEXT) == (0.1, 3)
    assert contracts.VaultLook(None, None, None, None).failures == ()
    assert contracts.AppLook(None, None, None, None).failures == ()
    assert contracts.CodeLook(None).failures == ()
    assert contracts.CodeLook(()).folders == ()
    assert [field.name for field in fields(contracts.CodeFolder)] == [
        "workspace_id",
        "name",
        "newest",
    ]
    assert timedelta(days=1) == contracts.CODE_LOOK_IDLE
    assert timedelta(minutes=30) == contracts.CODE_LOOK_EVERY
    assert contracts.MAX_CODE_LOOKS == 3
    assert (contracts.MAX_SECOND_LOOK_REFS, contracts.MAX_SECOND_LOOK_REF_BYTES) == (10, 1024)
    assert timedelta(days=30) == contracts.SECOND_LOOK_WINDOW
    assert (contracts.SECOND_LOOK_ANSWERED, contracts.SECOND_LOOK_ACCEPT_ONE_IN) == (10, 4)
    assert contracts.SECOND_LOOK_KIND_REJECTS == 5
    assert timedelta(days=14) == contracts.PROJECT_STALL
    assert timedelta(days=3) == contracts.OVERDUE_CHECK_AFTER
    assert contracts.MAX_PROJECT_PLANS == 3
    assert [field.name for field in fields(contracts.SecondLookHint)] == [
        "session",
        "turn",
        "trigger",
        "title",
        "refs",
    ]
    assert [field.name for field in fields(contracts.PageFacts)][-5:] == [
        "skill",
        "suggest",
        "second_look",
        "context",
        "sources",
    ]
    bare = contracts.PageFacts(0, 0, 0, "clean", None, None, None, None)
    assert (bare.second_look, bare.context, bare.sources) == (None, None, None)
    assert [field.name for field in fields(contracts.ContextHint)] == ["session", "percent"]
    assert [field.name for field in fields(contracts.SourceHint)] == ["session", "turn", "notes"]
    assert contracts.ContextHint(SESSION, 70).percent == 70
    assert contracts.SourceHint(SESSION, SESSION, False).notes is False
    assert [field.name for field in fields(contracts.Finding)] == [
        "mission_id",
        "scope",
        "session_id",
        "look",
        "kind",
        "title",
    ]
    assert [field.name for field in fields(contracts.LookRow)] == [
        "look",
        "state",
        "reason",
        "started",
    ]
    assert [field.name for field in fields(contracts.TriggerYield)] == [
        "look",
        "made",
        "accepted",
        "rejected",
        "nothing",
        "reasons",
        "paused",
    ]
    assert [field.name for field in fields(contracts.KindYield)] == [
        "kind",
        "rejected_in_a_row",
        "paused",
    ]
    assert [field.name for field in fields(contracts.SecondLookYields)] == ["triggers", "kinds"]
    assert [field.name for field in fields(contracts.VaultLook)] == [
        "saves",
        "deleted",
        "usage",
        "drift",
        "failures",
    ]
    assert [field.name for field in fields(contracts.AppLook)] == [
        "waiting",
        "heavy",
        "sends",
        "bridges",
        "failures",
    ]
    with pytest.raises(FrozenInstanceError):
        contracts.StoppedBridge("slack", False, None).restart = True  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        contracts.CodeFolder("w", "api", WALL).name = "x"  # type: ignore[misc]


# ---- quiet window ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("quiet", "now", "ends"),
    [
        (("22:00", "07:00"), datetime(2026, 10, 21, 23, 30), datetime(2026, 10, 22, 7, 0)),
        (("22:00", "07:00"), datetime(2026, 10, 21, 22, 0), datetime(2026, 10, 22, 7, 0)),
        (("22:00", "07:00"), datetime(2026, 10, 22, 0, 0), datetime(2026, 10, 22, 7, 0)),
        (("22:00", "07:00"), datetime(2026, 10, 22, 6, 59, 59), datetime(2026, 10, 22, 7, 0)),
        (("22:00", "07:00"), datetime(2026, 10, 22, 7, 0), None),
        (("22:00", "07:00"), datetime(2026, 10, 21, 21, 59), None),
        (("22:00", "07:00"), datetime(2026, 10, 21, 12, 0), None),
        (("09:00", "17:00"), datetime(2026, 10, 21, 9, 0), datetime(2026, 10, 21, 17, 0)),
        (("09:00", "17:00"), datetime(2026, 10, 21, 16, 59), datetime(2026, 10, 21, 17, 0)),
        (("09:00", "17:00"), datetime(2026, 10, 21, 17, 0), None),
        (("09:00", "17:00"), datetime(2026, 10, 21, 8, 59), None),
        (("09:00", "17:00"), datetime(2026, 10, 21, 23, 0), None),
        (("00:00", "00:30"), datetime(2026, 10, 21, 0, 15), datetime(2026, 10, 21, 0, 30)),
        (("23:30", "00:00"), datetime(2026, 10, 21, 23, 45), datetime(2026, 10, 22, 0, 0)),
        (("10:00", "10:00"), datetime(2026, 10, 21, 10, 0), None),
        (None, datetime(2026, 10, 21, 23, 30), None),
    ],
)
def test_quiet_until_ends_the_window_that_holds_now(
    quiet: tuple[str, str] | None, now: datetime, ends: datetime | None
) -> None:
    local = now.replace(tzinfo=JST)
    assert quiet_until(quiet, local) == (None if ends is None else ends.replace(tzinfo=JST))


def test_quiet_until_reads_the_local_wall_clock() -> None:
    # 14:30 UTC is 23:30 in Tokyo: inside 22:00-07:00 there, outside it in UTC.
    now = datetime(2026, 10, 21, 14, 30, tzinfo=UTC)
    assert quiet_until(("22:00", "07:00"), now) is None
    ends = quiet_until(("22:00", "07:00"), now.astimezone(JST))
    assert ends == datetime(2026, 10, 22, 7, 0, tzinfo=JST)
    assert ends is not None and ends.utcoffset() == timedelta(hours=9)


# ---- errors -------------------------------------------------------------------------------------


def test_errors_carry_their_code_and_nothing_else() -> None:
    error = ModelError("rate_limited", retry_after_s=60)
    assert (error.code, error.retry_after_s, str(error)) == ("rate_limited", 60, "rate_limited")
    assert ModelError("key_rejected").retry_after_s is None
    refusal = DecisionLoopError("limit", work="run")
    assert (refusal.code, refusal.extra, str(refusal)) == ("limit", {"work": "run"}, "limit")
    assert DecisionLoopError("busy").extra == {}


# ---- fakes --------------------------------------------------------------------------------------

QUESTIONS: dict[str, dict[str, object]] = {
    "goal_link": {"type": "noul"},
    "importance": {"type": "score"},
    "kind": {"type": "choice"},
    "work": {"type": "choice"},
}


def request(**questions: dict[str, object]) -> ModelRequest:
    return ModelRequest(
        model=contracts.MODEL_IDS["pinned"],
        state={"candidate": {"title": "Inbox " + KANA}},
        questions=questions or QUESTIONS,
    )


async def test_fake_model_answers_every_asked_name_and_records_the_request() -> None:
    model = FakeModel({"goal_link": NoulAnswer(0.9)})
    asked = request()
    reply = await model.ask(asked, key="k1")
    assert reply.answers == {
        "goal_link": NoulAnswer(0.9),
        "importance": QUIET_ANSWERS["importance"],
        "kind": ChoiceAnswer("inform", 1.0, {"inform": 1.0}),
        "work": ChoiceAnswer("none", 1.0, {"none": 1.0}),
    }
    assert reply.model == "jev-1.14.0"
    sent = canonical_json(asked.state) + canonical_json(asked.questions)
    assert reply.input_tokens == math.ceil(len(sent.encode("utf-8")) / 3) > 0
    assert (model.requests, model.keys) == ([asked], ["k1"])
    unscripted = await model.ask(request(sensitive={"type": "noul"}), key="k1")
    assert unscripted.answers == {"sensitive": QUIET_NOUL}


async def test_fake_model_plays_its_script_in_order() -> None:
    model = FakeModel()
    model.script(
        ModelError("rate_limited", retry_after_s=30),
        {"kind": ChoiceAnswer("ask", 0.8, {"ask": 0.8, "inform": 0.2})},
    )
    with pytest.raises(ModelError) as raised:
        await model.ask(request(), key="k")
    assert (raised.value.code, raised.value.retry_after_s) == ("rate_limited", 30)
    second = await model.ask(request(), key="k")
    assert second.answers["kind"] == ChoiceAnswer("ask", 0.8, {"ask": 0.8, "inform": 0.2})
    third = await model.ask(request(), key="k")
    assert third.answers["kind"] == QUIET_ANSWERS["kind"]
    assert len(model.requests) == 3  # a failed ask is recorded too


async def test_fake_model_gate_holds_asks_in_flight() -> None:
    model = FakeModel()
    model.gate = asyncio.Event()
    asks = [asyncio.create_task(model.ask(request(), key="k")) for _ in range(3)]
    await asyncio.sleep(0)
    assert (model.in_flight, model.most_in_flight, len(model.requests)) == (3, 3, 3)
    model.gate.set()
    replies = await asyncio.wait_for(asyncio.gather(*asks), HANG_GUARD_S)
    assert [reply.model for reply in replies] == ["jev-1.14.0"] * 3
    assert (model.in_flight, model.most_in_flight) == (0, 3)


async def test_fake_model_closes() -> None:
    model = FakeModel()
    assert model.listed == [] and not model.closed
    await model.close()
    assert model.closed


async def test_fake_clock_wait_returns_on_its_event_its_deadline_or_a_tick() -> None:
    clock = FakeClock()
    assert (clock.wall(), clock.monotonic()) == (WALL, 1000.0)
    event = asyncio.Event()

    waiting = asyncio.create_task(clock.wait(event, 60))
    await asyncio.sleep(0)
    assert clock.pending == 1
    clock.advance(59.5)
    await asyncio.sleep(0)
    assert not waiting.done()  # half a second short of its deadline
    clock.advance(0.5)
    await asyncio.wait_for(waiting, HANG_GUARD_S)
    assert (clock.pending, clock.wall()) == (0, WALL + timedelta(seconds=60))

    waiting = asyncio.create_task(clock.wait(event, 300))
    await asyncio.sleep(0)
    event.set()
    await asyncio.wait_for(waiting, HANG_GUARD_S)

    event.clear()
    waiting = asyncio.create_task(clock.wait(event, 300))
    await asyncio.sleep(0)
    clock.tick()
    await asyncio.wait_for(waiting, HANG_GUARD_S)
    assert clock.monotonic() == 1060.0  # a tick moves no time

    await asyncio.wait_for(clock.wait(event, 0), HANG_GUARD_S)
    event.set()
    await asyncio.wait_for(clock.wait(event, 300), HANG_GUARD_S)
    assert clock.waits == [60, 300, 300, 0, 300]
    assert clock.pending == 0


async def test_fake_clock_wall_can_jump_without_monotonic_time() -> None:
    clock = FakeClock()
    waiting = asyncio.create_task(clock.wait(asyncio.Event(), 60))
    await asyncio.sleep(0)
    clock.advance(3600, monotonic=False)
    await asyncio.sleep(0)
    assert not waiting.done() and clock.wall() == WALL + timedelta(hours=1)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    assert clock.pending == 0  # a cancelled wait leaves nothing behind


async def test_fake_host_records_remember_create_and_close_in_order() -> None:
    host = FakeHost()
    host.app_state.remember(SESSION, mode="plan")
    actor = host.create(SESSION)
    assert host.get(SESSION) is actor
    assert host.get("other") is None
    await host.close(SESSION)
    assert host.calls == [
        ("remember", SESSION, {"mode": "plan"}),
        ("create", SESSION),
        ("close", SESSION),
    ]
    assert host.get(SESSION) is None


async def test_fake_actor_sends_turns_and_emits_envelopes() -> None:
    host = FakeHost()
    actor = host.create(SESSION)
    seen: list[dict[str, Any]] = []
    busy_seen: list[bool] = []

    def record(envelope: dict[str, Any]) -> None:
        seen.append(envelope)
        busy_seen.append(actor.busy)

    unsubscribe = actor.subscribe(record)
    assert await actor.send("brief") is True
    assert (actor.sent, actor.busy) == (["brief"], True)
    actor.emit(Error(code="crashed", message="", recoverable=False))
    actor.emit(TurnDone(status="success", usage=Usage()))
    await actor.interrupt()
    unsubscribe()
    unsubscribe()  # a second unsubscribe is harmless, as with the real actor
    actor.emit(SessionClosed(reason="requested"))
    assert [(envelope["type"], envelope["seq"]) for envelope in seen] == [
        ("error", 1),
        ("turn.done", 2),
    ]
    assert seen[1]["session_id"] == SESSION and seen[1]["data"]["status"] == "success"
    assert busy_seen == [True, False]  # the turn end clears busy before subscribers hear it
    assert actor.interrupts == 1


async def test_fake_host_plays_refused_sends_and_failed_creates() -> None:
    host = FakeHost()
    host.send_result = False
    actor = host.create(SESSION)
    assert await actor.send("brief") is False
    assert not actor.busy
    host.create_error = RuntimeError("corrupt")
    with pytest.raises(RuntimeError):
        host.create("other")
    assert host.calls[-1] == ("create", "other")


def test_settings_values_refuses_an_unknown_name() -> None:
    with pytest.raises(KeyError):
        settings_values(**{"decision.enabld": True})


if TYPE_CHECKING:
    # Checked by pyright only: the real session classes and the fakes fit the protocols.

    def _manager_is_a_mission_host(manager: SessionManager) -> MissionHost:
        return manager

    def _actor_is_a_mission_session(actor: SessionActor) -> MissionSession:
        return actor

    def _fakes_fit(
        host: FakeHost, model: FakeModel, clock: FakeClock
    ) -> tuple[MissionHost, ModelPort, Clock]:
        return host, model, clock
