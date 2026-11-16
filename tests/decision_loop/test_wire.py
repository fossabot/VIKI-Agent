"""The Decision wire: every view key for key and type for type with the client's examples.json,
versions in place of model ids, each view's own rules, and parsers that take only the exact
client bodies."""

from __future__ import annotations

import copy
import inspect
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import FrozenInstanceError, dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from viki_agent.api_json import json_response
from viki_agent.decision_loop import arbiter, calibration, contracts, playbook, wire
from viki_agent.decision_loop.changes import change_draft
from viki_agent.decision_loop.contracts import (
    MODEL_IDS,
    ActionSpec,
    Calibration,
    CalibrationBin,
    CallBody,
    CallRecord,
    ContextHint,
    DecisionLoopError,
    DecisionSettings,
    EvidenceLabel,
    ItemBody,
    ItemRecord,
    KindYield,
    Learned,
    MissionRecord,
    ModelError,
    ModelReply,
    PageFacts,
    Presence,
    QuestionRate,
    SeatTeam,
    SecondLookHint,
    SecondLookYields,
    ShadowStats,
    SkillHint,
    SourceHint,
    SuggestHint,
    TrialStats,
    TriggerYield,
)
from viki_agent.decision_loop.plan_brake import Hold

CLIENT = Path(__file__).parents[2] / "frontend" / "src" / "clients" / "decisionLoop"
NOW = datetime(2026, 10, 21, 14, 2, 30, tzinfo=UTC)
SCOPE = "315d31aa-25ed-45a7-a4b8-5c582ce841fb"
WINDOW = "0b6c1a43-6d1e-4f47-9a8e-2f1c5b7d9e01"
SESSION = "fe07f4bf-5447-4a8d-9d58-b68385409b99"
ROUTINE = "771411e1-348a-4e26-bd6b-385fd3b89284"
CALL_ID = "d2df3dad-403c-4e31-a5f0-1f07efb73934"
EPISODE = "69a85af7-0708-4498-9e54-6920f9378cc0"
ITEM_KEY = "287012d26416912533449255733ff6202b794dcc9c0d7c5d825fba2494a8e906"
PROPOSAL_ID = "e61a512d-4cce-4c11-b792-e6932dde8c3e"
REVISION = "e73801d660dea203c70a987d6f0487e3dd376f4f5faee2aa985622594e6cef57"
ON = DecisionSettings(enabled=True)
SPLIT = (
    ("candidate", 1104),
    ("owner", 512),
    ("now", 96),
    ("history", 640),
    ("outside", 100),
    ("questions", 1460),
)
# Control characters (Cc): NUL, tab, newline, escape, delete and next line.
CONTROL = tuple(map(chr, (0x00, 0x09, 0x0A, 0x1B, 0x7F, 0x85)))
LONE_SURROGATE = chr(0xD800)
JAPANESE = "".join(map(chr, (0x5EAD, 0x306E, 0x8A08, 0x753B)))  # "garden plan"
SEEDLING = chr(0x1F331)  # an emoji outside the basic plane


def _item(
    level: str = "list",
    *,
    kind: str | None = "inform",
    key: str = ITEM_KEY,
    evidence: tuple[EvidenceLabel, ...] = (),
    actions: tuple[ActionSpec, ...] = (),
    mission_id: str | None = None,
) -> ItemRecord:
    return ItemRecord(
        item_key=key,
        episode_id=EPISODE,
        candidate="tasks_due",
        kind=kind,
        level=level,
        state="open",
        timing="now",
        defer_until=None,
        created_at="2026-10-21T14:00:00.000Z",
        shown_at=None,
        mission_id=mission_id,
        answers_json=None,
        state_hash=None,
        body=ItemBody(
            "Water the seedlings", "Two tasks are due today", "due today", evidence, actions
        ),
    )


def _call(**changes: Any) -> CallRecord:
    call = CallRecord(
        call_id=CALL_ID,
        created_at="2026-10-21T14:02:00.000Z",
        item_key=ITEM_KEY,
        episode_id=EPISODE,
        session_id=SESSION,
        model=MODEL_IDS["pinned"],
        outcome="answered",
        failure=None,
        latency_ms=112,
        input_tokens=3912,
        cost_micro_usd=160,
        body=CallBody(
            title="Water the seedlings",
            candidate="tasks_due",
            state_text='{"candidate":{"title":"Water the seedlings"}}',
            questions_text='{"goal_link":{"type":"noul"}}',
            split=SPLIT,
            answers_json=None,
            held_back=4,
            version_setting="pinned",
            playbook_version="1",
        ),
    )
    return replace(call, **changes)


def _failed_call() -> CallRecord:
    return _call(
        model=None,
        outcome="failed",
        failure="rate_limited",
        latency_ms=None,
        input_tokens=None,
        cost_micro_usd=None,
    )


@dataclass(frozen=True)
class Listed:
    """A call row as the agenda lists it: its body None when the body did not open."""

    call_id: str
    created_at: str
    session_id: str | None
    input_tokens: int | None
    body: CallBody | None


def _mission(**changes: Any) -> MissionRecord:
    mission = MissionRecord(
        mission_id="9273d0ed-e3e0-4ac6-8319-80e6c5e10247",
        scope=SCOPE,
        session_id="fda2250d-d245-468a-9d7c-5fa673b14d3a",
        item_key=ITEM_KEY,
        work="prepare",
        state="running",
        reason="",
        title="Draft a plan for the trip budget",
        brief="VIKI started this chat (Decision).",
        created_at="2026-10-21T13:10:30.000Z",
        deadline_at="2026-10-21T13:40:30.000Z",
        finished_at=None,
    )
    return replace(mission, **changes)


def _launcher(
    settings: DecisionSettings = ON, items: tuple[ItemRecord, ...] = (), **changes: Any
) -> dict[str, Any]:
    arguments: dict[str, Any] = {
        "now": NOW,
        "model_state": "ready",
        "checked_at": None,
        "working": 0,
        "nudge": 0,
        "glow": 0,
        "started": (),
    }
    arguments.update(changes)
    return wire.launcher_view(settings, items, **arguments)


def _no_learning(
    settings: DecisionSettings,
    second_looks: SecondLookYields | None = None,
    project_plans: SecondLookYields | None = None,
    message_drafts: SecondLookYields | None = None,
    saved_checks: SecondLookYields | None = None,
    inbox_triage: SecondLookYields | None = None,
) -> dict[str, Any]:
    """The learning block of a scope with nothing learned and no decisions, as the service
    builds it from no rows."""
    return wire.learning_view(
        settings,
        None,
        labels=0,
        since=None,
        lam=arbiter.START_LAMBDA,
        calibration=calibration.calibration(()),
        questions=calibration.questions(()),
        shadow=calibration.shadow((), settings.shadow),
        second_looks=second_looks,
        project_plans=project_plans,
        message_drafts=message_drafts,
        saved_checks=saved_checks,
        inbox_triage=inbox_triage,
    )


def _page(settings: DecisionSettings = ON, **changes: Any) -> dict[str, Any]:
    arguments: dict[str, Any] = {
        "now": NOW,
        "key_saved": True,
        "model_state": "ready",
        "last_error": None,
        "last_test": None,
        "checked_at": None,
        "open_items": 0,
        "decisions_today": 0,
        "model_calls_today": 0,
        "tokens_today": 0,
        "spent_micro_usd": 0,
        "last_call": None,
        "held_back_today": 0,
        "muted": (),
        "held_back": (),
        "recent": (),
        "counts": {},
        "running": (),
        "prepare_today": 0,
        "run_today": 0,
        "episodes": 0,
        "oldest": None,
        "learning": _no_learning(settings),
        "changes_today": 0,
    }
    arguments.update(changes)
    return wire.page_view(settings, **arguments)


# ---- the shared examples ----------------------------------------------------------------------


def _examples() -> dict[str, Any]:
    return json.loads((CLIENT / "examples.json").read_text(encoding="utf-8"))


def _as_sent(view: object) -> Any:
    """`view` as a route sends it: through `api_json.json_response` and decoded again."""
    return json.loads(bytes(json_response(view).body))


def _built() -> dict[str, Any]:
    return {name: _as_sent(view) for name, view in wire.example_views().items()}


def _json_type(value: object) -> str:
    """The JSON kind of a decoded value. JSON keeps 2 and 2.0 apart and the decoder answers int
    and float for them, so a count must be an int and money a float on both sides; bool is
    tested before int, which it subclasses."""
    kinds = (
        ("boolean", bool),
        ("integer", int),
        ("float", float),
        ("string", str),
        ("array", list),
        ("object", dict),
    )
    for name, kind in kinds:
        if isinstance(value, kind):
            return name
    raise AssertionError(f"not a JSON value: {value!r}")


def _misfits(example: object, built: object, path: str, nullable: frozenset[str]) -> list[str]:
    """Where `built` departs from `example`: a key added or missing, another JSON type, an array
    of another length, or a null on either side where `nullable` does not list the path. Arrays
    are walked element by element, an element's path ending "[]"."""
    if example is None or built is None:
        return [] if path in nullable else [f"{path} is null"]
    if _json_type(example) != _json_type(built):
        return [f"{path} is {_json_type(built)}, not {_json_type(example)}"]
    found: list[str] = []
    if isinstance(example, dict) and isinstance(built, dict):
        found += [f"{path}.{key} is missing" for key in sorted(example.keys() - built.keys())]
        found += [f"{path}.{key} is added" for key in sorted(built.keys() - example.keys())]
        for key in sorted(example.keys() & built.keys()):
            found += _misfits(example[key], built[key], f"{path}.{key}", nullable)
    elif isinstance(example, list) and isinstance(built, list):
        if len(example) != len(built):
            found.append(f"{path} has {len(built)} elements, not {len(example)}")
        for left, right in zip(example, built, strict=False):
            found += _misfits(left, right, f"{path}[]", nullable)
    return found


def _all_misfits(built: dict[str, Any]) -> list[str]:
    examples = _examples()
    nullable = frozenset(examples.pop("_nullable"))
    assert sorted(built) == sorted(examples)
    return [
        misfit
        for name, example in examples.items()
        for misfit in _misfits(example, built[name], name, nullable)
    ]


def test_views_match_the_shared_examples() -> None:
    assert _all_misfits(_built()) == []


def _data_body(kind: object = "decisions", **changes: object) -> dict[str, object]:
    body: dict[str, object] = {
        key: None
        for key in (
            "kind",
            "ids",
            "topic",
            "shown",
            "answer",
            "state",
            "outcome",
            "since",
            "until",
            "before",
        )
    }
    body["kind"] = kind
    body.update(changes)
    return body


@pytest.mark.parametrize(
    ("kind", "changes", "delete"),
    [
        ("decisions", {"topic": "tasks_due", "shown": "held", "answer": "none"}, False),
        ("suggestions", {"topic": "inbox", "state": "open"}, False),
        ("calls", {"outcome": "failed"}, False),
        ("decisions", {"ids": [EPISODE]}, True),
        ("suggestions", {"ids": [ITEM_KEY]}, True),
        ("calls", {"ids": [CALL_ID]}, True),
        ("decisions", {"until": "2026-10-21T14:00:00Z", "shown": "listed"}, True),
        ("suggestions", {"until": "2026-10-21T14:00:00Z", "state": "acted"}, True),
    ],
)
def test_data_query_accepts_each_kind(kind: str, changes: dict[str, object], delete: bool) -> None:
    parsed = wire.parse_data_query(_data_body(kind, **changes), delete=delete)
    assert parsed.kind == kind
    if "ids" in changes:
        ids = changes["ids"]
        assert isinstance(ids, list)
        assert parsed.ids == tuple(ids)


@pytest.mark.parametrize(
    ("changes", "delete"),
    [
        ({"unexpected": 1}, False),
        ({"topic": "not_a_topic"}, False),
        ({"shown": "wrong"}, False),
        ({"answer": "nonsense"}, False),
        ({"state": "open"}, False),
        ({"outcome": "failed"}, False),
        ({"ids": []}, True),
        ({"ids": [EPISODE] * 51}, True),
        ({"ids": [EPISODE, EPISODE]}, True),
        ({"ids": [ITEM_KEY]}, True),
        ({"since": "2026-10-22T00:00:00Z", "until": "2026-10-21T00:00:00Z"}, False),
        ({"since": "2026-10-21T00:00:00Z", "until": "2026-10-21T00:00:00Z"}, False),
        ({"before": {"at": "2026-10-21T00:00:00Z", "id": EPISODE}}, True),
        ({"ids": [EPISODE]}, False),
        ({"ids": [EPISODE], "topic": "tasks_due"}, True),
        ({"topic": "tasks_due"}, True),
        ({"until": "2026-10-21T00:00:00Z"}, True),
    ],
)
def test_data_query_refuses_bad_decision_bodies(changes: dict[str, object], delete: bool) -> None:
    with pytest.raises(DecisionLoopError, match="^invalid_request$"):
        wire.parse_data_query(_data_body(**changes), delete=delete)


def test_data_query_requires_every_key() -> None:
    body = _data_body()
    body.pop("before")
    with pytest.raises(DecisionLoopError, match="^invalid_request$"):
        wire.parse_data_query(body, delete=False)


@pytest.mark.parametrize(
    ("kind", "changes", "delete"),
    [
        ("suggestions", {"shown": "held"}, False),
        ("suggestions", {"answer": "useful"}, False),
        ("suggestions", {"state": "pending"}, False),
        ("suggestions", {"ids": [EPISODE]}, True),
        ("suggestions", {"before": {"at": "2026-10-21T00:00:00Z", "id": EPISODE}}, False),
        ("calls", {"topic": "tasks_due"}, False),
        ("calls", {"since": "2026-10-20T00:00:00Z"}, False),
        ("calls", {"until": "2026-10-21T00:00:00Z"}, False),
        ("calls", {"outcome": "bad"}, False),
        ("calls", {"until": "2026-10-21T00:00:00Z"}, True),
        ("bad", {}, False),
    ],
)
def test_data_query_refuses_other_bad_bodies(
    kind: str, changes: dict[str, object], delete: bool
) -> None:
    with pytest.raises(DecisionLoopError, match="^invalid_request$"):
        wire.parse_data_query(_data_body(kind, **changes), delete=delete)


def test_data_cursor_normalizes_optional_milliseconds_and_record_body() -> None:
    parsed = wire.parse_data_query(
        _data_body(before={"at": "2026-10-21T14:02:00Z", "id": EPISODE}), delete=False
    )
    assert parsed.before == ("2026-10-21T14:02:00.000Z", EPISODE)
    assert wire.parse_data_record({"kind": "decisions", "id": EPISODE}) == ("decisions", EPISODE)
    assert wire.parse_data_record({"kind": "suggestions", "id": ITEM_KEY}) == (
        "suggestions",
        ITEM_KEY,
    )
    for bad in (
        {"kind": "calls", "id": CALL_ID},
        {"kind": "suggestions", "id": EPISODE},
        {"kind": "decisions", "id": ITEM_KEY},
        {"kind": "decisions", "id": EPISODE, "extra": None},
    ):
        with pytest.raises(DecisionLoopError, match="^invalid_request$"):
            wire.parse_data_record(bad)


def test_data_views_cut_text_keep_exact_refs_and_show_unopened_calls() -> None:
    chip = EvidenceLabel("Label" + "x" * 500, SCOPE, "notes/" + "n" * 510)
    detail = {
        **wire.example_views()["data_suggestion"],
        "title": JAPANESE * 150,
        "what": "w" * 500,
        "why": "y" * 500,
        "evidence": (chip, EvidenceLabel("Chat", session_id=SESSION)),
    }
    shown = wire.data_suggestion_view(detail)
    assert len(shown["title"]) <= wire.MAX_TEXT
    assert len(shown["what"]) <= wire.MAX_WHAT
    assert len(shown["why"]) <= wire.MAX_WHY
    assert shown["evidence"][0] == {"label": "Label" + "x" * 114 + "…"}
    assert shown["evidence"][1]["session_id"] == SESSION
    decision = wire.data_decision_view(
        {
            **wire.example_views()["data_decision"],
            "why": "z" * 600,
            "evidence": (EvidenceLabel("A note", SCOPE, "a.md"),) * 21,
        }
    )
    assert len(decision["why"]) <= wire.MAX_WHY
    assert len(decision["evidence"]) == wire.MAX_DATA_EVIDENCE
    unopened = _call(body=None, input_tokens=None, cost_micro_usd=None, outcome="failed")
    call = wire.data_calls_view([unopened])["rows"][0]
    assert (call["title"], call["topic"], call["tokens"], call["cost_usd"]) == ("", None, 0, 0.0)


def test_data_views_fit_client_json_limit_at_worst_case() -> None:
    examples = wire.example_views()
    row = examples["data_suggestions"]["rows"][0]
    records = {
        "total": 50,
        "deletable": 50,
        "next": None,
        "rows": [{**row, "title": JAPANESE * 100} for _ in range(51)],
    }
    listed = wire.data_records_view("suggestions", records)
    assert len(listed["rows"]) == 50
    assert len(bytes(json_response(listed).body)) < 256 * 1024
    detail = examples["data_suggestion"]
    chips = tuple(EvidenceLabel("note", SCOPE, "r" * 512) for _ in range(20))
    assert (
        len(
            bytes(
                json_response(
                    wire.data_suggestion_view(
                        {
                            **detail,
                            "evidence": chips,
                        }
                    )
                ).body
            )
        )
        < 256 * 1024
    )
    parts = examples["data_summary"]
    many = {
        **parts,
        "days": [{**parts["days"][0], "day": "2026-10-21"} for _ in range(367)],
        "topics": [{**parts["topics"][0]} for _ in range(38)],
    }
    summary = wire.data_summary_view(many, now=NOW, key_saved=True, key_changed_at=None)
    assert len(summary["days"]) == 366
    assert len(summary["topics"]) == 37
    assert len(bytes(json_response(summary).body)) < 256 * 1024


def test_suggestion_delete_body_with_fifty_ids_fits_request_cap() -> None:
    body = _data_body("suggestions", ids=[f"{index:064x}" for index in range(50)])
    assert wire.parse_data_query(body, delete=True).ids is not None
    assert len(json.dumps(body).encode("utf-8")) <= 4096


def test_data_examples_share_stored_call_ids_and_use_synthetic_topics() -> None:
    views = wire.example_views()
    assert {row["id"] for row in views["data_calls"]["rows"]} == {
        row["call_id"] for row in views["calls"]["calls"]
    }
    assert any(row["topic"] == "unwatched" for row in views["data_summary"]["topics"])
    assert all(views[name]["rows"] for name in ("data_decisions", "data_suggestions", "data_calls"))


def test_question_proposal_view_matches_the_example() -> None:
    """The inspector's view walks clean against examples.json's own entry and, unlike the views
    above, equals it value for value: the inspector shows its two wordings' texts as sent, so the
    example holds the view's own pretty JSON of the playbook's live "news" wording."""
    examples = _examples()
    nullable = frozenset(examples["_nullable"])
    built = _built()["question_proposal"]
    assert _misfits(examples["question_proposal"], built, "question_proposal", nullable) == []
    assert built == examples["question_proposal"]


def test_the_launcher_example_shows_each_self_improvement_type_with_its_draft() -> None:
    """One launcher item per `IMPROVE` type that is not a vault-change proposal, each with its
    one draft action, so the client's own walks over every candidate and every action type find
    one. `IMPROVE` also holds `CHANGES` (note_link, note_superseded, tasks_ticked), which never
    reach the launcher (`service.py::_visible`; the "approvals" candidate already counts their
    open rows), so this walk skips them and no example launcher item names one."""
    items = {item["candidate"]: item for item in _built()["launcher"]["items"]}
    assert not set(items) & set(contracts.CHANGES)
    assert {
        name: [one["type"] for one in items[name]["actions"]]
        for name in contracts.IMPROVE
        if name not in contracts.CHANGES
    } == {
        "skill_offer": ["draft_chat"],
        "skill_suggest": ["draft_chat"],
        "learn_source": ["draft_new"],
        "retro_due": ["draft_new"],
        "question_tune": ["draft_new"],
    }


def test_the_launcher_example_shows_each_card_type_with_open_card() -> None:
    """One launcher item per Kanban card type: `card_needs_you` asks and `card_stale` reports, each
    with one primary open_note "Open card" action naming the same note as its evidence chip."""
    items = {item["candidate"]: item for item in _built()["launcher"]["items"]}
    assert {name: items[name]["kind"] for name in contracts.CARDS} == {
        "card_needs_you": "ask",
        "card_stale": "report",
    }
    for name in contracts.CARDS:
        (chip,) = items[name]["evidence"]
        (action,) = items[name]["actions"]
        assert (action["type"], action["label"], action["primary"]) == (
            "open_note",
            "Open card",
            True,
        )
        assert (action["workspace_id"], action["ref"]) == (chip["workspace_id"], chip["ref"])


def test_the_launcher_example_shows_each_seat_type_with_its_actions() -> None:
    """One launcher item per `SEATS` type, with the kind and the actions the seat's card offers."""
    items = {item["candidate"]: item for item in _built()["launcher"]["items"]}
    assert {
        name: (items[name]["kind"], [one["type"] for one in items[name]["actions"]])
        for name in contracts.SEATS
    } == {
        "seat_waiting": ("ask", ["open_chat"]),
        "seat_working": ("inform", ["open_chat", "stop_seat"]),
        "seat_start": ("suggest", ["start_seat"]),
        "seat_report": ("report", ["open_chat"]),
    }


def test_the_launcher_example_shows_each_look_type_with_its_open_action() -> None:
    """One launcher item per `LOOKS` type with no evidence and one primary action: the vault
    ones open the vault view in the scope, the messaging one Messaging, the chat ones a chat."""
    items = {item["candidate"]: item for item in _built()["launcher"]["items"]}
    assert {
        name: (
            items[name]["kind"],
            items[name]["evidence"],
            [(one["type"], one["primary"]) for one in items[name]["actions"]],
        )
        for name in contracts.LOOKS
    } == {
        "vault_recovery": ("report", [], [("open_vault", True)]),
        "memory_drift": ("ask", [], [("open_vault", True)]),
        "bridge_stopped": ("report", [], [("open_messaging", True)]),
        "chat_waiting": ("ask", [], [("open_chat", True)]),
        "send_uncertain": ("report", [], [("open_chat", True)]),
        "mcp_context": ("report", [], [("open_chat", True)]),
    }
    for name in contracts.VAULT_LOOKS:
        (action,) = items[name]["actions"]
        assert action["workspace_id"] == wire._EXAMPLE_SCOPE


def test_missions_block_counts_the_team() -> None:
    settings = DecisionSettings(enabled=True, limit_team=2)
    team = SeatTeam(1, 3, ())
    expected = {"today": 1, "limit": 2, "running": 3}
    assert wire.missions_view([], settings, prepare_today=0, run_today=0, team=team)["team"] == (
        expected
    )
    assert _page(settings, team=team)["missions"]["team"] == expected


def test_the_launcher_example_shows_the_code_look_with_open_in_code_and_take_a_look() -> None:
    (item,) = [i for i in _built()["launcher"]["items"] if i["candidate"] == "code_look"]
    assert (item["kind"], item["level"], item["evidence"]) == ("suggest", "list", [])
    assert [(a["type"], a["label"], a["primary"]) for a in item["actions"]] == [
        ("open_code", "Open in Code", True),
        ("start_prepare", "Take a look", False),
    ]
    assert item["actions"][0]["workspace_id"] == "e84e8a26-0fb1-43b5-8e8b-57f98ba18a62"


def test_missions_block_counts_todays_looks() -> None:
    settings = DecisionSettings(enabled=True, limit_look=3)
    expected = {"today": 2, "limit": 3}
    assert wire.missions_view([], settings, prepare_today=0, run_today=0, look_today=2)["look"] == (
        expected
    )
    assert _page(settings, look_today=2)["missions"]["look"] == expected


def test_the_launcher_example_shows_a_second_look_offer_and_a_finding() -> None:
    items = {item["candidate"]: item for item in _built()["launcher"]["items"]}
    offer, finding = items["long_task_look"], items["correction_look"]
    assert (offer["kind"], offer["level"], offer["what"]) == (
        "suggest",
        "list",
        "A long task just finished in this chat.",
    )
    assert offer["evidence"] == [
        {"label": "garden-plan", "workspace_id": wire._EXAMPLE_SCOPE, "ref": "notes/garden-plan.md"}
    ]
    assert [(a["type"], a["label"], a["primary"]) for a in offer["actions"]] == [
        ("open_chat", "Open chat", True),
        ("start_prepare", "Take a second look", False),
    ]
    assert offer["actions"][0]["session_id"] == SESSION
    assert offer["item_key"] == contracts.item_key(
        "long_task_look", SESSION, "3f0b9c52-6a51-4c27-9a44-2f7e1d0c8b11"
    )
    assert (finding["kind"], finding["level"], finding["evidence"]) == ("suggest", "list", [])
    assert finding["what"] == 'VIKI found a possible flaw in "Garden plan".'
    assert [(a["type"], a["label"], a["primary"]) for a in finding["actions"]] == [
        ("open_chat", "Open plan", True)
    ]
    assert finding["actions"][0]["session_id"] == "9a8b7c6d-5e4f-4a3b-8c2d-1e0f9a8b7c6d"


def test_missions_block_counts_todays_second_looks() -> None:
    settings = DecisionSettings(enabled=True, limit_second_look=2)
    expected = {"today": 1, "limit": 2}
    block = wire.missions_view([], settings, prepare_today=0, run_today=0, second_look_today=1)
    assert block["second_look"] == expected
    assert _page(settings, second_look_today=1)["missions"]["second_look"] == expected


def test_the_launcher_example_shows_project_plan_offers_and_a_finding() -> None:
    items = [i for i in _built()["launcher"]["items"] if i["candidate"] in contracts.PROJECT_PLANS]
    stalled_offer, overdue, stalled_finding = items
    garden = "10-projects/garden/garden.md"
    assert stalled_offer["item_key"] == contracts.item_key(
        "stalled_project", garden, "2026-10-01T09:00:00.000Z"
    )
    assert stalled_offer["evidence"] == [
        {"label": "Garden", "workspace_id": wire._EXAMPLE_SCOPE, "ref": garden}
    ]
    assert [(a["type"], a["label"], a["primary"]) for a in stalled_offer["actions"]] == [
        ("open_note", "Open note", True),
        ("start_prepare", "Plan next steps", False),
    ]
    assert overdue["item_key"] == contracts.item_key(
        "overdue_tasks", "10-projects/garden/tasks.md", "2026-10-15 Order seeds"
    )
    assert overdue["candidate"] == "overdue_tasks"
    assert [(a["type"], a["label"]) for a in overdue["actions"]] == [
        ("open_note", "Open note"),
        ("start_prepare", "Check these tasks"),
    ]
    assert (stalled_finding["candidate"], stalled_finding["evidence"]) == ("stalled_project", [])
    assert stalled_finding["what"] == 'VIKI\'s plan for "Garden" waits for your approval.'
    assert [(a["type"], a["label"], a["primary"]) for a in stalled_finding["actions"]] == [
        ("open_chat", "Open plan", True)
    ]
    assert stalled_finding["actions"][0]["session_id"] == "8e7d6c5b-4a39-4281-9f0e-1d2c3b4a5f6e"
    assert len({i["episode_id"] for i in _built()["launcher"]["items"]}) == 35


def test_missions_block_counts_todays_project_plans() -> None:
    settings = DecisionSettings(enabled=True, limit_project_plan=2)
    expected = {"today": 1, "limit": 2}
    block = wire.missions_view([], settings, prepare_today=0, run_today=0, project_plan_today=1)
    assert block["project_plan"] == expected
    assert _page(settings, project_plan_today=1)["missions"]["project_plan"] == expected


def test_the_launcher_example_shows_both_message_drafts() -> None:
    items = [i for i in _built()["launcher"]["items"] if i["candidate"] in contracts.MESSAGE_DRAFTS]
    handoff, source_note = items
    chat = "2b4d6f8a-1c3e-4a5b-8d7f-9e0a1b2c3d4e"
    assert handoff["item_key"] == contracts.item_key("context_handoff", chat)
    assert (handoff["kind"], handoff["level"], handoff["evidence"]) == ("suggest", "list", [])
    assert handoff["what"] == "This chat's context is nearly full."
    assert [(a["type"], a["label"], a["primary"]) for a in handoff["actions"]] == [
        ("draft_chat", "Draft a handoff", True)
    ]
    assert handoff["actions"][0]["session_id"] == chat
    assert handoff["actions"][0]["text"].startswith("This chat's context is nearly full. Write")
    assert source_note["item_key"] == contracts.item_key(
        "source_note", chat, "7c9e1a3b-5d2f-4e6a-9b8c-0d1e2f3a4b5c"
    )
    assert source_note["what"] == (
        "Your last reply in this chat fetched web pages and read no note in your vault."
    )
    assert [(a["type"], a["label"]) for a in source_note["actions"]] == [
        ("draft_chat", "Draft literature notes")
    ]
    assert source_note["actions"][0]["session_id"] == chat
    assert "first search it for notes on the same subject" in source_note["actions"][0]["text"]


def test_the_launcher_example_shows_a_saved_check_offer_with_run_checks() -> None:
    (item,) = [
        i
        for i in _built()["launcher"]["items"]
        if i["candidate"] == "commit_checks" and i["kind"] == "suggest"
    ]
    folder = "e84e8a26-0fb1-43b5-8e8b-57f98ba18a62"
    assert item["item_key"] == contracts.item_key(
        "commit_checks", folder, "4f1c0d9e8b7a6f5e4d3c2b1a0f9e8d7c6b5a4f3e"
    )
    assert (item["kind"], item["level"], item["evidence"]) == ("suggest", "list", [])
    assert item["what"].startswith("garden-app moved to a new commit")
    assert [(a["type"], a["label"], a["primary"], a["workspace_id"]) for a in item["actions"]] == [
        ("open_code", "Open in Code", True, folder),
        ("run_checks", "Run checks", False, folder),
    ]


def test_the_missions_block_shows_checks_only_with_a_count() -> None:
    settings = DecisionSettings(enabled=True, limit_check=2)
    assert wire.missions_view([], settings, prepare_today=0, run_today=0)["checks"] is None
    assert _page(settings)["missions"]["checks"] is None
    expected = {"today": 1, "limit": 2, "running": 1}
    block = wire.missions_view(
        [], settings, prepare_today=0, run_today=0, check_today=1, check_running=1
    )
    assert block["checks"] == expected
    assert _page(settings, check_today=1, check_running=1)["missions"]["checks"] == expected
    assert _built()["page"]["missions"]["checks"] == {"today": 1, "limit": 3, "running": 0}


def test_the_missions_block_counts_inbox_suggestions() -> None:
    settings = DecisionSettings(enabled=True, limit_inbox_triage=2)
    expected = {"today": 1, "limit": 2}
    block = wire.missions_view([], settings, prepare_today=0, run_today=0, inbox_today=1)
    assert block["inbox"] == expected
    assert list(block).index("inbox") == list(block).index("project_plan") + 1
    assert _page(settings, inbox_today=1)["missions"]["inbox"] == expected
    assert _page(settings)["missions"]["inbox"] == {"today": 0, "limit": 2}
    assert _built()["page"]["missions"]["inbox"] == {"today": 1, "limit": 3}


def test_the_launcher_example_shows_an_inbox_offer_with_suggest_destinations() -> None:
    (item,) = [i for i in _built()["launcher"]["items"] if i["candidate"] == "inbox_triage"]
    scope = "315d31aa-25ed-45a7-a4b8-5c582ce841fb"
    state = contracts.canonical_json(
        [
            ["00-inbox/reading-list-ideas.md", "2026-10-20T07:00:00.000Z"],
            ["00-inbox/seedling-care.md", "2026-10-20T07:30:00.000Z"],
        ]
    )
    assert item["item_key"] == contracts.item_key("inbox_triage", scope, state)
    assert (item["kind"], item["level"], item["evidence"]) == ("suggest", "list", [])
    assert item["what"] == "Your inbox has captures with no suggested destination."
    assert [(a["type"], a["label"], a["primary"]) for a in item["actions"]] == [
        ("open_triage", "Triage inbox", True),
        ("suggest_inbox", "Suggest destinations", False),
    ]


def test_the_page_counts_todays_message_drafts() -> None:
    settings = DecisionSettings(enabled=True, limit_message_draft=2)
    assert _page(settings, message_draft_today=1)["message_drafts"] == {"today": 1, "limit": 2}
    assert _page()["message_drafts"] == {"today": 0, "limit": 3}  # DecisionSettings' own default
    assert _built()["page"]["message_drafts"] == {"today": 1, "limit": 3}


def _set(path: tuple[str | int, ...], value: object) -> Callable[[dict[str, Any]], None]:
    def change(views: dict[str, Any]) -> None:
        target: Any = views
        for step in path[:-1]:
            target = target[step]
        target[path[-1]] = value

    return change


def _drop(path: tuple[str | int, ...]) -> Callable[[dict[str, Any]], None]:
    def change(views: dict[str, Any]) -> None:
        target: Any = views
        for step in path[:-1]:
            target = target[step]
        del target[path[-1]]

    return change


@pytest.mark.parametrize(
    ("change", "misfit"),
    [
        (_set(("launcher", "items", 0, "extra"), 1), "launcher.items[].extra is added"),
        (
            _set(("launcher", "items", 4, "evidence", 0, "ref"), "x.md"),
            "launcher.items[].evidence[].ref is added",
        ),
        (_drop(("page", "today", "capped")), "page.today.capped is missing"),
        (_drop(("calls", "calls", 1, "session_id")), "calls.calls[].session_id is missing"),
        (_set(("page", "today", "tokens"), 1666000.0), "page.today.tokens is float, not integer"),
        (_set(("call_detail", "cost_usd"), 0), "call_detail.cost_usd is integer, not float"),
        (_set(("calls", "enabled"), 1), "calls.enabled is integer, not boolean"),
        (_set(("calls", "calls", 0, "title"), None), "calls.calls[].title is null"),
        (_drop(("page", "recent", 3)), "page.recent has 3 elements, not 4"),
        (_set(("mission", "finished_at"), None), None),  # listed in _nullable
    ],
)
def test_the_example_walk_names_each_misfit(
    change: Callable[[dict[str, Any]], None], misfit: str | None
) -> None:
    built = _built()
    change(built)
    assert _all_misfits(built) == ([] if misfit is None else [misfit])


def test_example_views_are_fresh_each_call() -> None:
    first = wire.example_views()
    first["launcher"]["items"].clear()
    assert len(wire.example_views()["launcher"]["items"]) == 35


def _client_vocabularies() -> dict[str, tuple[str, ...]]:
    """Every `export const NAME = [...] as const;` list in types.ts, spreads expanded."""
    source = (CLIENT / "types.ts").read_text(encoding="utf-8")
    found: dict[str, tuple[str, ...]] = {}
    for name, members in re.findall(r"export const (\w+) = \[(.*?)\] as const;", source, re.S):
        spread = tuple(
            value for other in re.findall(r"\.\.\.(\w+)", members) for value in found[other]
        )
        found[name] = (*spread, *re.findall(r'"([^"]*)"', members))
    return found


def test_vocabularies_match_the_client_types() -> None:
    assert _client_vocabularies() == {
        "CANDIDATES": contracts.CANDIDATES,
        "TOPICS": contracts.TOPICS,
        "KINDS": contracts.KINDS,
        "LEVELS": contracts.LEVELS,
        "POLICIES": wire.POLICIES,
        "SOURCES": contracts.SOURCES,
        "FEEDBACK_SHOWN": contracts.FEEDBACK_SHOWN,
        "FEEDBACK_HELD": contracts.FEEDBACK_HELD,
        "FEEDBACK_REASONS": contracts.FEEDBACK_REASONS,
        "OBSERVE_EVENTS": wire.OBSERVE_EVENTS,
        "ACTION_TYPES": contracts.ACTIONS,
        "LAUNCHER_STATES": wire.LAUNCHER_STATES,
        "LAUNCHER_LEVELS": wire.LAUNCHER_LEVELS,
        "MODEL_STATES": wire.MODEL_STATES,
        "MODEL_ERROR_CODES": contracts.MODEL_ERROR_CODES,
        "KEY_STATES": wire.KEY_STATES,
        "VERSION_SETTINGS": tuple(MODEL_IDS),
        "OUTCOMES": contracts.CALL_OUTCOMES,
        "ITEM_STATES": contracts.ITEM_STATES,
        "DATA_KINDS": wire.DATA_KINDS,
        "DATA_SHOWN": wire.DATA_SHOWN,
        "MISSION_WORK": wire.MISSION_WORK,
        "MISSION_STATES": contracts.MISSION_STATES,
        "GIT_STATES": wire.GIT_STATES,
        "FINDING_KINDS": contracts.FINDING_KINDS,
    }


# ---- versions, never model ids ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("model", "shown"),
    [
        (MODEL_IDS["pinned"], "1.14.0"),
        (MODEL_IDS["latest"], ""),
        (MODEL_IDS["preview"], ""),
        (None, ""),
        ("", ""),
        ("model-2.0", "2.0"),
        ("model-7", ""),
        ("1.2.3.4.5", "1.2.3.4"),
        ("v10.0.1-rc2", "10.0.1"),
        ("model-" + chr(0x0661) + "." + chr(0x0662), ""),  # Arabic-Indic digits are not ASCII
    ],
)
def test_display_version_is_the_first_dotted_number(model: str | None, shown: str) -> None:
    assert wire.display_version(model) == shown


@pytest.mark.parametrize(("setting", "model"), sorted(MODEL_IDS.items()))
def test_views_show_versions_never_model_ids(setting: str, model: str) -> None:
    settings = DecisionSettings(enabled=True, model_version=setting)
    call = _call(model=model)
    views: dict[str, Any] = {
        "launcher": _launcher(settings, (_item("badge"),)),
        "page": _page(settings, last_call=call),
        "calls": wire.calls_view(settings, [call], latest="model"),
        "call_detail": wire.call_detail_view(call, questions=(), largest_question=0),
        "mission": wire.mission_view(_mission()),
        "test_result": wire.test_view(ModelReply(model, {}, 10, 112)),
        "examples": wire.example_views(),
    }
    assert "jev" not in json.dumps(views).lower()
    assert views["call_detail"]["version"] == wire.display_version(model)
    assert views["test_result"]["version"] == wire.display_version(model)
    assert views["page"]["model"]["version"] == {
        "setting": setting,
        "display": wire.display_version(MODEL_IDS[setting]),
    }


# ---- the views' own rules ---------------------------------------------------------------------


def test_item_view_leaves_unset_members_out() -> None:
    item = _item(
        "badge",
        kind="ask",
        evidence=(
            EvidenceLabel("Agent runs"),
            EvidenceLabel("Garden tasks", workspace_id=SCOPE, ref="10-projects/g/tasks.md"),
            EvidenceLabel("Started by VIKI", session_id=SESSION),
        ),
        actions=(
            ActionSpec("open_runs", "Open runs", True),
            ActionSpec("open_context", "See what was sent", False, call_id=CALL_ID),
        ),
        mission_id="9273d0ed-e3e0-4ac6-8319-80e6c5e10247",
    )
    assert wire.item_view(item) == {
        "item_key": ITEM_KEY,
        "episode_id": EPISODE,
        "candidate": "tasks_due",
        "kind": "ask",
        "level": "badge",
        "what": "Two tasks are due today",
        "why": "due today",
        "evidence": [
            {"label": "Agent runs"},
            {"label": "Garden tasks", "workspace_id": SCOPE, "ref": "10-projects/g/tasks.md"},
            {"label": "Started by VIKI", "session_id": SESSION},
        ],
        "actions": [
            {"type": "open_runs", "label": "Open runs", "primary": True},
            {
                "type": "open_context",
                "label": "See what was sent",
                "primary": False,
                "call_id": CALL_ID,
            },
        ],
        "created_at": "2026-10-21T14:00:00.000Z",
        "mission_id": "9273d0ed-e3e0-4ac6-8319-80e6c5e10247",
    }


def test_an_item_without_a_kind_shows_as_inform() -> None:
    assert wire.item_view(_item(kind=None))["kind"] == "inform"


def test_evidence_drops_a_lone_workspace_id_or_ref() -> None:
    """A chip needs `workspace_id` and `ref` together (a vault note) or neither: one without
    the other is not one of the client's three shapes (`parse.ts`'s `itemEvidence`), so
    `_evidence` drops both and keeps only the label."""
    assert wire._evidence(EvidenceLabel("A note", workspace_id=SCOPE)) == {"label": "A note"}
    assert wire._evidence(EvidenceLabel("A note", ref="x.md")) == {"label": "A note"}
    assert wire._evidence(EvidenceLabel("A note", workspace_id=SCOPE, ref="x.md")) == {
        "label": "A note",
        "workspace_id": SCOPE,
        "ref": "x.md",
    }
    assert wire._evidence(EvidenceLabel("Started by VIKI", session_id=SESSION)) == {
        "label": "Started by VIKI",
        "session_id": SESSION,
    }
    assert wire._evidence(EvidenceLabel("Agent runs")) == {"label": "Agent runs"}


@pytest.mark.parametrize(
    ("settings", "state", "paused_until"),
    [
        (DecisionSettings(), "off", None),
        (DecisionSettings(paused_until=NOW + timedelta(hours=1)), "off", None),
        (ON, "on", None),
        (DecisionSettings(enabled=True, paused_until=NOW - timedelta(seconds=1)), "on", None),
        (DecisionSettings(enabled=True, paused_until=NOW), "on", None),
        (
            DecisionSettings(enabled=True, paused_until=NOW + timedelta(hours=1)),
            "paused",
            "2026-10-21T15:02:30.000Z",
        ),
    ],
)
def test_launcher_state_follows_the_settings_now(
    settings: DecisionSettings, state: str, paused_until: str | None
) -> None:
    view = _launcher(settings)
    assert (view["state"], view["paused_until"]) == (state, paused_until)


def test_launcher_focus_shows_only_while_it_lasts() -> None:
    def focus_until(until: datetime | None) -> object:
        return _launcher(DecisionSettings(enabled=True, focus_until=until))["focus_until"]

    assert focus_until(None) is None
    assert focus_until(NOW) is None
    assert focus_until(NOW + timedelta(minutes=30)) == "2026-10-21T14:32:30.000Z"


@pytest.mark.parametrize(
    ("levels", "level", "count"),
    [
        ((), "idle", 0),
        (("silent",), "idle", 0),
        (("list", "list"), "list", 0),
        (("badge", "list"), "badge", 1),
        (("list", "nudge", "badge"), "nudge", 2),
        (("badge", "alert", "silent"), "alert", 2),
    ],
)
def test_launcher_counts_follow_its_items(levels: tuple[str, ...], level: str, count: int) -> None:
    items = tuple(_item(name, key=f"{n:064x}") for n, name in enumerate(levels))
    view = _launcher(ON, items)
    assert (view["level"], view["count"], view["open"]) == (level, count, len(levels))
    assert [item["level"] for item in view["items"]] == list(levels)


@pytest.mark.parametrize(
    ("items", "level"),
    [
        ((("badge", "ask"),), "question"),
        ((("badge", "suggest"), ("badge", "ask"), ("list", "inform")), "question"),
        ((("badge", "suggest"), ("badge", None)), "badge"),
        ((("nudge", "inform"), ("badge", "ask")), "nudge"),
        ((("alert", "inform"), ("badge", "ask")), "alert"),
        ((("list", "ask"),), "list"),
        ((("badge", "inform"), ("list", "ask")), "badge"),
    ],
)
def test_a_badge_item_that_asks_shows_a_question(
    items: tuple[tuple[str, str | None], ...], level: str
) -> None:
    records = tuple(_item(name, kind=kind, key=f"{n:064x}") for n, (name, kind) in enumerate(items))
    view = _launcher(ON, records)
    assert view["level"] == level
    assert view["count"] == sum(1 for name, _kind in items if name != "list")


@pytest.mark.parametrize(
    ("settings", "state"),
    [
        (DecisionSettings(), "off"),
        (DecisionSettings(enabled=True, paused_until=NOW + timedelta(hours=1)), "paused"),
    ],
)
def test_a_question_keeps_the_off_and_paused_states(settings: DecisionSettings, state: str) -> None:
    view = _launcher(settings, (_item("badge", kind="ask"),))
    assert (view["state"], view["level"]) == (state, "question")


def test_launcher_lists_at_most_fifty_items_and_started_chats() -> None:
    items = tuple(_item("badge", key=f"{n:064x}") for n in range(60))
    started = tuple(f"00000000-0000-4000-8000-{n:012x}" for n in range(60))
    view = _launcher(ON, items, started=started, working=2, nudge=4, glow=9)
    assert (view["open"], view["count"], len(view["items"])) == (50, 50, 50)
    assert view["started"] == list(started[:50])
    assert (view["working"], view["nudge"], view["glow"]) == (2, 4, 9)


def test_launcher_passes_the_engine_status() -> None:
    view = _launcher(model_state="capped", checked_at=datetime(2026, 10, 21, 5, 2, tzinfo=UTC))
    assert (view["model"], view["checked_at"]) == ("capped", "2026-10-21T05:02:00.000Z")


def test_page_shows_pause_and_focus_as_stamps_or_empty() -> None:
    assert (_page()["paused_until"], _page()["focus_until"]) == ("", "")
    past = DecisionSettings(
        enabled=True, paused_until=NOW - timedelta(hours=1), focus_until=NOW - timedelta(hours=1)
    )
    assert (_page(past)["paused_until"], _page(past)["focus_until"]) == ("", "")
    soon = DecisionSettings(paused_until=NOW + timedelta(hours=2), focus_until=NOW + timedelta(1))
    assert (_page(soon)["enabled"], _page(soon)["paused_until"], _page(soon)["focus_until"]) == (
        False,
        "2026-10-21T16:02:30.000Z",
        "2026-10-22T14:02:30.000Z",
    )


def test_page_spend_is_dollars_and_capped_follows_the_model_state() -> None:
    settings = DecisionSettings(enabled=True, daily_cap_usd=2)
    today = _page(
        settings,
        decisions_today=412,
        model_calls_today=426,
        tokens_today=1_666_000,
        spent_micro_usd=70_000,
    )["today"]
    assert today == {
        "decisions": 412,
        "model_calls": 426,
        "tokens": 1_666_000,
        "cost_usd": 0.07,
        "cap_usd": 2.0,
        "capped": False,
        "shadow_calls": 0,
    }
    # Money stays a float on the wire, even when whole.
    assert isinstance(today["cap_usd"], float)
    assert isinstance(_page(spent_micro_usd=0)["today"]["cost_usd"], float)
    assert _page(model_state="capped")["today"]["capped"] is True
    assert _page(model_state="capped")["model"]["state"] == "capped"


def test_page_model_block() -> None:
    model = _page(
        key_saved=False,
        last_error="rate_limited",
        last_test=(datetime(2026, 10, 21, 9, 15, tzinfo=UTC), 112),
    )["model"]
    assert model == {
        "key": "missing",
        "version": {"setting": "pinned", "display": "1.14.0"},
        "state": "ready",
        "last_error": "rate_limited",
        "last_test": {"at": "2026-10-21T09:15:00.000Z", "latency_ms": 112},
    }
    assert _page()["model"]["key"] == "saved"


def test_page_privacy_names_the_last_call_with_its_tokens() -> None:
    assert _page()["privacy"] == {"last_call": None, "held_back_today": 0}
    assert _page(last_call=_call(), held_back_today=3)["privacy"] == {
        "last_call": {"call_id": CALL_ID, "at": "2026-10-21T14:02:00.000Z", "tokens": 3912},
        "held_back_today": 3,
    }
    # A call without a measured count shows its split's estimate; one without a body, 0.
    assert _page(last_call=_failed_call())["privacy"]["last_call"]["tokens"] == sum(
        tokens for _part, tokens in SPLIT
    )
    unread = Listed(CALL_ID, "2026-10-21T14:02:00.000Z", None, None, None)
    assert _page(last_call=unread)["privacy"]["last_call"]["tokens"] == 0


def test_page_rows_keep_their_members_and_their_limits() -> None:
    held = {
        "episode_id": EPISODE,
        "created_at": "2026-10-21T10:05:00.000Z",
        "candidate": "inbox",
        "level": "silent",
        "reason": "inbox_below_nudge",
        "why": "a few notes in the inbox",
        "feedback": None,
        "defer_until": None,  # a member the page does not show
    }
    recent = {**held, "policy": "rules", "feedback": "useful"}
    page = _page(held_back=[held] * 60, recent=[recent] * 30)
    assert len(page["held_back"]) == wire.MAX_HELD_BACK
    assert len(page["recent"]) == 20
    assert page["held_back"][0] == {key: held[key] for key in held if key != "defer_until"}
    assert list(page["recent"][0]) == [
        "episode_id",
        "created_at",
        "candidate",
        "level",
        "policy",
        "reason",
        "why",
        "feedback",
    ]


def test_page_counts_muted_and_data() -> None:
    page = _page(
        muted=frozenset({"uncommitted", "approvals", "not_a_candidate"}),
        counts={"shown": 128, "useful": 41, "missed": 2},
        episodes=1284,
        oldest="2026-08-02T09:00:00.000Z",
        open_items=7,
        checked_at=datetime(2026, 10, 21, 14, 2, tzinfo=UTC),
    )
    assert page["muted"] == ["approvals", "uncommitted"]
    assert page["counts"] == {
        "shown": 128,
        "useful": 41,
        "wrong": 0,
        "later": 0,
        "mute": 0,
        "missed": 2,
        "right_to_hold": 0,
    }
    assert page["data"] == {
        "episodes": 1284,
        "oldest": "2026-08-02T09:00:00.000Z",
        "max": 10_000,
        "retention_days": 365,
    }
    assert page["status"] == {"open": 7, "checked_at": "2026-10-21T14:02:00.000Z"}


def test_missions_block_lists_running_chats_with_the_limits() -> None:
    settings = DecisionSettings(enabled=True, limit_drafts=2, limit_chats=0)
    block = wire.missions_view([_mission()], settings, prepare_today=1, run_today=0)
    assert block == {
        "running": [wire.mission_view(_mission())],
        "today": {"prepare": 1, "run": 0},
        "limits": {"prepare": 2, "run": 0},
        "hold": None,
        "team": {"today": 0, "limit": 4, "running": 0},
        "look": {"today": 0, "limit": 3},
        "second_look": {"today": 0, "limit": 3},
        "project_plan": {"today": 0, "limit": 3},
        "inbox": {"today": 0, "limit": 3},
        "checks": None,
    }
    page = _page(settings, running=[_mission()], prepare_today=1, run_today=0)
    assert page["missions"] == block


def test_missions_block_names_the_hold() -> None:
    settings = DecisionSettings(enabled=True)
    ends = Hold(datetime(2026, 10, 21, 14, tzinfo=UTC))

    def held(hold: Hold | None) -> Any:
        return wire.missions_view([], settings, prepare_today=0, run_today=0, hold=hold)["hold"]

    assert held(None) is None
    assert held(ends) == {"until": "2026-10-21T14:00:00.000Z"}
    assert held(Hold(None)) == {"until": None}
    assert _page(settings, hold=ends)["missions"]["hold"] == {"until": "2026-10-21T14:00:00.000Z"}


def test_page_changes() -> None:
    """The page's own daily count of vault-change proposals and the setting's limit."""
    settings = DecisionSettings(enabled=True, limit_changes=7)
    assert _page(settings, changes_today=4)["changes"] == {"today": 4, "limit": 7}
    assert _page()["changes"] == {"today": 0, "limit": 3}  # DecisionSettings' own default


def test_mission_view() -> None:
    stopped = _mission(state="stopped", reason="owner", finished_at="2026-10-21T13:20:00.000Z")
    assert wire.mission_view(stopped) == {
        "mission_id": "9273d0ed-e3e0-4ac6-8319-80e6c5e10247",
        "session_id": "fda2250d-d245-468a-9d7c-5fa673b14d3a",
        "work": "prepare",
        "state": "stopped",
        "title": "Draft a plan for the trip budget",
        "started_at": "2026-10-21T13:10:30.000Z",
        "finished_at": "2026-10-21T13:20:00.000Z",
        "reason": "owner",
    }


def test_calls_view_lists_readable_calls_newest_first_up_to_twenty() -> None:
    unread = Listed(
        "0f0f0f0f-0000-4000-8000-000000000001", "2026-10-21T14:03:00.000Z", None, 5, None
    )
    calls = [unread, _call(), _failed_call()]
    view = wire.calls_view(ON, calls, latest="rules")
    assert view == {
        "enabled": True,
        "latest": "rules",
        "calls": [
            {
                "call_id": CALL_ID,
                "at": "2026-10-21T14:02:00.000Z",
                "title": "Water the seedlings",
                "candidate": "tasks_due",
                "tokens": 3912,
                "session_id": SESSION,
            },
            {
                "call_id": CALL_ID,
                "at": "2026-10-21T14:02:00.000Z",
                "title": "Water the seedlings",
                "candidate": "tasks_due",
                "tokens": 3912,  # the split's estimate: the failed call measured nothing
                "session_id": SESSION,
            },
        ],
    }
    many = [_call(call_id=f"00000000-0000-4000-8000-{n:012x}") for n in range(25)]
    listed = wire.calls_view(DecisionSettings(), many, latest=None)
    assert (listed["enabled"], listed["latest"], len(listed["calls"])) == (False, None, 20)
    assert listed["calls"][0]["call_id"] == many[0].call_id


def test_call_detail_view() -> None:
    questions = (("goal_link", "Linked to a goal?", "Yes, 0.91"),)
    assert wire.call_detail_view(_call(), questions=questions, largest_question=2870) == {
        "call_id": CALL_ID,
        "at": "2026-10-21T14:02:00.000Z",
        "title": "Water the seedlings",
        "candidate": "tasks_due",
        "session_id": SESSION,
        "version": "1.14.0",
        "version_setting": "pinned",
        "latency_ms": 112,
        "cost_usd": 0.00016,
        "long_context": False,
        "request": {"used": 3912, "max": 64_000},
        "largest_question": {"used": 2870, "max": 32_000},
        "split": [{"part": part, "tokens": tokens} for part, tokens in SPLIT],
        "questions": [{"name": "goal_link", "label": "Linked to a goal?", "answer": "Yes, 0.91"}],
        "playbook_version": "1",
        "state_text": '{"candidate":{"title":"Water the seedlings"}}',
        "questions_text": '{"goal_link":{"type":"noul"}}',
        "held_back": 4,
        "outcome": "answered",
        "failure": None,
    }


def test_a_failed_call_shows_zero_time_and_cost_and_the_estimate() -> None:
    view = wire.call_detail_view(_failed_call(), questions=(), largest_question=0)
    assert (view["version"], view["latency_ms"], view["cost_usd"]) == ("", 0, 0.0)
    assert isinstance(view["cost_usd"], float)
    assert view["request"] == {"used": 3912, "max": 64_000}
    assert (view["outcome"], view["failure"]) == ("failed", "rate_limited")


def test_test_view() -> None:
    assert wire.test_view(ModelReply(MODEL_IDS["pinned"], {}, 12, 112)) == {
        "ok": True,
        "latency_ms": 112,
        "version": "1.14.0",
        "error": None,
    }
    assert wire.test_view(ModelError("key_rejected")) == {
        "ok": False,
        "latency_ms": None,
        "version": None,
        "error": "key_rejected",
    }


# ---- learning ---------------------------------------------------------------------------------


def _learned(**changes: Any) -> Learned:
    base = Learned(
        fitted_at=datetime(2026, 10, 20, 3, 0, tzinfo=UTC),
        labels=58,
        weights={
            "bias": -0.4231,
            "goal_link": 0.712,
            "news": 0.551,
            "actionable": 0.383,
            "owner_only": 0.334,
            "fits_now": 0.219,
            "topic.approvals": 0.121,
            "topic.tasks_due": 0.462,
            "topic.agent_runs": -0.083,
            "topic.inbox": 0.054,
            "topic.uncommitted": -0.311,
            "topic.routine": 0.022,
            "topic.mission_report": 0.183,
        },
        reach={"list": 0.284, "badge": 0.641, "nudge": 0.882, "alert": 0.953},
        costs={"badge": 0.061, "nudge": 0.372, "alert": 0.914},
        support={
            "topic.approvals": 7,
            "topic.tasks_due": 19,
            "topic.agent_runs": 5,
            "topic.inbox": 10,
            "topic.uncommitted": 6,
            "topic.routine": 5,
            "topic.mission_report": 6,
            "reach.list": 30,
            "reach.badge": 22,
            "reach.nudge": 10,
            "reach.alert": 4,
            "costs.badge": 22,
            "costs.nudge": 10,
            "costs.alert": 4,
        },
    )
    return replace(base, **changes)


def test_max_held_back_equals_the_weekly_audit_sample_size() -> None:
    assert wire.MAX_HELD_BACK == contracts.AUDIT_SIZE


def test_learning_view_reports_start_and_learned_numbers() -> None:
    """Learning on with a snapshot shows the real numbers, each list in its fixed order, every
    float rounded to 3 places."""
    on = DecisionSettings(enabled=True, learning=True)
    learned = _learned()
    calibration = Calibration(
        n=10,
        brier=0.1234,
        bins=(
            CalibrationBin(0.0, 0.2, 2, 0.111, 0.5),
            CalibrationBin(0.2, 0.4, 0, None, None),
            CalibrationBin(0.4, 0.6, 3, 0.512, 0.3331),
            CalibrationBin(0.6, 0.8, 1, 0.71, 1.0),
            CalibrationBin(0.8, 1.0, 4, 0.91, 0.75),
        ),
    )
    rates = (
        QuestionRate("goal_link", 5, 0.611, 2, None),
        QuestionRate("owner_only", 0, None, 6, 0.3335),
    )
    shadow = ShadowStats("pinned", 9, 7, 8, 4, 0.1119, 0.2221)
    view = wire.learning_view(
        on,
        learned,
        labels=58,
        since=datetime(2026, 9, 1, tzinfo=UTC),
        lam=1.23456,
        calibration=calibration,
        questions=rates,
        shadow=shadow,
    )
    assert view["enabled"] is True
    assert (view["labels"], view["min_labels"]) == (58, contracts.MIN_LABELS)
    assert view["fitted_at"] == "2026-10-20T03:00:00.000Z"
    assert view["since"] == "2026-09-01T00:00:00.000Z"
    assert view["attention_price"] == round(1.23456, 3)
    start_bias = arbiter.START_WEIGHTS["bias"]
    assert view["bias"] == {"start": start_bias, "now": round(learned.weights["bias"], 3)}
    # weights in arbiter.FEATURES order, each with its playbook label.
    assert [w["name"] for w in view["weights"]] == list(arbiter.FEATURES)
    assert view["weights"][0] == {
        "name": "goal_link",
        "label": playbook.LABELS["goal_link"],
        "start": arbiter.START_WEIGHTS["goal_link"],
        "now": round(learned.weights["goal_link"], 3),
    }
    # topics in CANDIDATES order, starting at 0.
    assert [t["candidate"] for t in view["topics"]] == list(contracts.CANDIDATES)
    assert view["topics"][0] == {
        "candidate": "approvals",
        "start": 0.0,
        "now": round(learned.weights["topic.approvals"], 3),
        "support": learned.support["topic.approvals"],
    }
    # reach list to alert, costs badge to alert.
    assert [r["level"] for r in view["reach"]] == ["list", "badge", "nudge", "alert"]
    assert view["reach"][0] == {
        "level": "list",
        "start": arbiter.START_REACH["list"],
        "now": round(learned.reach["list"], 3),
        "support": learned.support["reach.list"],
    }
    assert [c["level"] for c in view["costs"]] == ["badge", "nudge", "alert"]
    assert view["costs"][0] == {
        "level": "badge",
        "start": arbiter.START_COSTS["badge"],
        "now": round(learned.costs["badge"], 3),
        "support": learned.support["costs.badge"],
    }
    assert "ja_floor" not in view
    assert view["calibration"]["n"] == 10
    assert view["calibration"]["brier"] == round(0.1234, 3)
    assert set(view["calibration"]) == {"n", "brier", "bins"}
    assert view["calibration"]["bins"][1] == {
        "low": 0.2,
        "high": 0.4,
        "n": 0,
        "mean_p": None,
        "rate": None,
    }
    # questions in the given order, each with its playbook label.
    assert [q["name"] for q in view["questions"]] == ["goal_link", "owner_only"]
    assert view["questions"][0] == {
        "name": "goal_link",
        "label": playbook.LABELS["goal_link"],
        "yes_n": 5,
        "yes_rate": round(0.611, 3),
        "no_n": 2,
        "no_rate": None,
    }
    assert view["questions"][1]["yes_rate"] is None
    assert view["shadow"] == {
        "setting": "pinned",
        "decisions": 9,
        "agree": 7,
        "kind_agree": 8,
        "labels": 4,
        "brier": round(0.1119, 3),
        "live_brier": round(0.2221, 3),
    }


def test_learning_view_falls_back_to_start_values_when_off_or_unlearned() -> None:
    """Off, or on with no snapshot yet: every `now` is its start, support is 0 and `fitted_at`
    is null -- but calibration, questions and shadow still show what was given, since they
    describe what happened rather than what is deciding now."""
    learned = _learned()
    calibration = Calibration(
        4,
        0.2,
        bins=tuple(CalibrationBin(0.2 * i, 0.2 * (i + 1), 0, None, None) for i in range(5)),
    )
    rates = (QuestionRate("goal_link", 1, 0.5, 1, 0.5),)
    shadow = ShadowStats("latest", 2, 1, 1, 0, None, None)
    cases = (
        (DecisionSettings(enabled=True, learning=False), learned),  # the switch is off
        (DecisionSettings(enabled=True, learning=True), None),  # nothing fitted yet
    )
    for settings, snapshot in cases:
        view = wire.learning_view(
            settings,
            snapshot,
            labels=3,
            since=None,
            lam=0.0,
            calibration=calibration,
            questions=rates,
            shadow=shadow,
        )
        assert view["enabled"] is settings.learning
        assert view["fitted_at"] is None
        start_bias = arbiter.START_WEIGHTS["bias"]
        assert view["bias"] == {"start": start_bias, "now": start_bias}
        assert all(w["start"] == w["now"] for w in view["weights"])
        assert all(t["now"] == 0.0 and t["support"] == 0 for t in view["topics"])
        assert all(r["now"] == r["start"] and r["support"] == 0 for r in view["reach"])
        assert all(c["now"] == c["start"] and c["support"] == 0 for c in view["costs"])
        assert (view["calibration"]["n"], view["questions"][0]["yes_n"]) == (4, 1)
        assert view["shadow"]["setting"] == "latest"


def test_learning_view_reads_zeros_without_second_look_yields() -> None:
    zero_triggers = [
        {
            "candidate": look,
            "made": 0,
            "accepted": 0,
            "rejected": 0,
            "nothing": 0,
            "reasons": dict.fromkeys(contracts.FEEDBACK_REASONS, 0),
            "paused": False,
        }
        for look in contracts.SECOND_LOOKS
    ]
    zero_kinds = [
        {"kind": kind, "rejected_in_a_row": 0, "paused": False} for kind in contracts.FINDING_KINDS
    ]
    block = _no_learning(ON)["second_looks"]
    assert block == {"triggers": zero_triggers, "kinds": zero_kinds}


def test_learning_view_reads_zeros_without_project_plan_yields() -> None:
    zero = [
        {
            "candidate": plan,
            "made": 0,
            "accepted": 0,
            "rejected": 0,
            "nothing": 0,
            "reasons": dict.fromkeys(contracts.FEEDBACK_REASONS, 0),
            "paused": False,
        }
        for plan in contracts.PROJECT_PLANS
    ]
    assert _no_learning(ON)["project_plans"] == zero


def test_learning_view_lists_project_plan_yields_in_vocabulary_order() -> None:
    yields = SecondLookYields(
        triggers=(TriggerYield("overdue_tasks", 3, 1, 1, 1, {"wrong_topic": 1}, True),), kinds=()
    )
    rows = _no_learning(ON, project_plans=yields)["project_plans"]
    assert [row["candidate"] for row in rows] == list(contracts.PROJECT_PLANS)
    assert rows[0]["made"] == 0 and rows[0]["paused"] is False
    assert rows[1] == {
        "candidate": "overdue_tasks",
        "made": 3,
        "accepted": 1,
        "rejected": 1,
        "nothing": 1,
        "reasons": {"out_of_date": 0, "already_knew": 0, "wrong_topic": 1, "bad_timing": 0},
        "paused": True,
    }


def test_learning_view_reads_zeros_without_message_draft_yields() -> None:
    zero = [
        {
            "candidate": draft,
            "made": 0,
            "accepted": 0,
            "rejected": 0,
            "nothing": 0,
            "reasons": dict.fromkeys(contracts.FEEDBACK_REASONS, 0),
            "paused": False,
        }
        for draft in contracts.MESSAGE_DRAFTS
    ]
    assert _no_learning(ON)["message_drafts"] == zero
    assert _no_learning(ON, message_drafts=None)["message_drafts"] == zero


def test_learning_view_reads_zeros_without_saved_check_yields() -> None:
    zero = [
        {
            "candidate": "commit_checks",
            "made": 0,
            "accepted": 0,
            "rejected": 0,
            "nothing": 0,
            "reasons": dict.fromkeys(contracts.FEEDBACK_REASONS, 0),
            "paused": False,
        }
    ]
    assert _no_learning(ON)["saved_checks"] == zero
    yields = SecondLookYields(
        triggers=(TriggerYield("commit_checks", 3, 1, 1, 0, {"already_knew": 1}, False),), kinds=()
    )
    row = _no_learning(ON, saved_checks=yields)["saved_checks"][0]
    assert (row["made"], row["accepted"], row["reasons"]["already_knew"]) == (3, 1, 1)


def test_learning_view_reads_zeros_without_inbox_triage_yields() -> None:
    zero = [
        {
            "candidate": "inbox_triage",
            "made": 0,
            "accepted": 0,
            "rejected": 0,
            "nothing": 0,
            "reasons": dict.fromkeys(contracts.FEEDBACK_REASONS, 0),
            "paused": False,
        }
    ]
    view = _no_learning(ON)
    assert view["inbox_triage"] == zero
    assert list(view).index("inbox_triage") == list(view).index("saved_checks") + 1
    yields = SecondLookYields(
        triggers=(TriggerYield("inbox_triage", 4, 1, 1, 1, {"already_knew": 1}, False),), kinds=()
    )
    row = _no_learning(ON, inbox_triage=yields)["inbox_triage"][0]
    assert (row["made"], row["accepted"], row["nothing"], row["reasons"]["already_knew"]) == (
        4,
        1,
        1,
        1,
    )


def test_learning_view_lists_message_draft_yields_in_vocabulary_order() -> None:
    yields = SecondLookYields(
        triggers=(TriggerYield("source_note", 3, 1, 1, 1, {"bad_timing": 1}, True),), kinds=()
    )
    rows = _no_learning(ON, message_drafts=yields)["message_drafts"]
    assert [row["candidate"] for row in rows] == list(contracts.MESSAGE_DRAFTS)
    assert rows[0]["made"] == 0 and rows[0]["paused"] is False
    assert rows[1] == {
        "candidate": "source_note",
        "made": 3,
        "accepted": 1,
        "rejected": 1,
        "nothing": 1,
        "reasons": {"out_of_date": 0, "already_knew": 0, "wrong_topic": 0, "bad_timing": 1},
        "paused": True,
    }


def test_learning_view_lists_second_look_yields_in_vocabulary_order() -> None:
    yields = SecondLookYields(
        triggers=(TriggerYield("correction_look", 12, 2, 7, 3, {"wrong_topic": 2}, True),),
        kinds=(KindYield("missing", 5, True), KindYield("flaw", 1, False)),
    )
    block = _no_learning(ON, second_looks=yields)["second_looks"]
    assert [t["candidate"] for t in block["triggers"]] == list(contracts.SECOND_LOOKS)
    assert block["triggers"][0]["made"] == 0 and block["triggers"][0]["paused"] is False
    assert block["triggers"][1] == {
        "candidate": "correction_look",
        "made": 12,
        "accepted": 2,
        "rejected": 7,
        "nothing": 3,
        "reasons": {"out_of_date": 0, "already_knew": 0, "wrong_topic": 2, "bad_timing": 0},
        "paused": True,
    }
    assert block["kinds"] == [
        {"kind": "flaw", "rejected_in_a_row": 1, "paused": False},
        {"kind": "approach", "rejected_in_a_row": 0, "paused": False},
        {"kind": "missing", "rejected_in_a_row": 5, "paused": True},
    ]


def test_learning_view_rounds_every_float_to_three_places() -> None:
    learned = replace(_learned(), costs={**_learned().costs, "nudge": 0.123456789})
    calibration = Calibration(
        1,
        0.123456,
        bins=(
            CalibrationBin(0.0, 0.2, 1, 0.999999, 0.000001),
            *(CalibrationBin(0.2 * i, 0.2 * (i + 1), 0, None, None) for i in range(1, 5)),
        ),
    )
    rates = (QuestionRate("goal_link", 1, 0.333333, 1, 0.666666),)
    shadow = ShadowStats("pinned", 1, 1, 1, 1, 0.123454, 0.987654)
    view = wire.learning_view(
        DecisionSettings(enabled=True, learning=True),
        learned,
        labels=1,
        since=None,
        lam=2.999999,
        calibration=calibration,
        questions=rates,
        shadow=shadow,
    )
    assert view["attention_price"] == round(2.999999, 3)
    assert view["bias"]["now"] == round(learned.weights["bias"], 3)
    assert view["costs"][1] == {"level": "nudge", "start": 0.4, "now": 0.123, "support": 10}
    assert view["calibration"]["brier"] == round(0.123456, 3)
    assert view["calibration"]["bins"][0]["mean_p"] == round(0.999999, 3)
    assert view["calibration"]["bins"][0]["rate"] == round(0.000001, 3)
    assert view["questions"][0]["yes_rate"] == round(0.333333, 3)
    assert view["questions"][0]["no_rate"] == round(0.666666, 3)
    assert view["shadow"]["brier"] == round(0.123454, 3)
    assert view["shadow"]["live_brier"] == round(0.987654, 3)


def test_page_view_requires_a_learning_block() -> None:
    """No stand-in block (one that listed no questions would be refused by parse.ts);
    `service.py`, the one caller, passes the block `learning_view` built."""
    learning = inspect.signature(wire.page_view).parameters["learning"]
    assert (learning.kind, learning.default) == (
        inspect.Parameter.KEYWORD_ONLY,
        inspect.Parameter.empty,
    )


def test_page_view_embeds_a_given_learning_block_unchanged() -> None:
    given = {"enabled": True, "shape": "whatever learning_view built"}
    assert _page(learning=given)["learning"] == given


def test_page_view_reports_shadow_calls_today() -> None:
    assert _page()["today"]["shadow_calls"] == 0
    assert _page(shadow_calls_today=7)["today"]["shadow_calls"] == 7


def test_launcher_view_defaults_review_to_not_due() -> None:
    assert _launcher()["review"] == {"due": False, "count": 0}


def test_launcher_view_reports_the_given_review() -> None:
    assert _launcher(review=(True, 4))["review"] == {"due": True, "count": 4}


# ---- a question proposal (GET proposals/{id}) -------------------------------------------------

NEWS = playbook.questions()["news"]
TRIED = TrialStats(
    "news",
    24,
    19,
    6,
    QuestionRate("news", 4, 0.75, 2, 0.5),
    QuestionRate("news", 3, 2 / 3, 3, 1 / 3),
)


def _proposal_item() -> dict[str, Any]:
    """An open proposal as the store lists it in Approvals."""
    return {
        "kind": "question_proposal",
        "id": PROPOSAL_ID,
        "origin": {"workspace_id": SCOPE, "session_id": None},
        "revision": REVISION,
        "state": "open",
        "attention": True,
        "title": "Question change: New to you?",
        "name": "news",
        "created_at": "2026-10-19T09:30:00.000Z",
        "updated_at": "2026-10-19T09:30:00.000Z",
        "approved_at": None,
        "rejected_at": None,
        "reason": None,
    }


def _proposal_view(**changes: Any) -> dict[str, Any]:
    arguments: dict[str, Any] = {"current": NEWS, "proposed": NEWS, "tried": TRIED}
    arguments.update(changes)
    return wire.question_proposal_view(_proposal_item(), **arguments)


def test_question_proposal_view_embeds_its_item_and_names_its_question() -> None:
    item = _proposal_item()
    view = wire.question_proposal_view(item, current=NEWS, proposed=NEWS, tried=TRIED)
    assert sorted(view) == ["current_text", "label", "proposal", "proposed_text", "tried"]
    assert view["proposal"] == _proposal_item() and view["proposal"] is not item
    assert view["label"] == playbook.LABELS["news"]


def test_question_proposal_view_shows_each_wording_as_sorted_indented_json() -> None:
    wording = {
        "type": "noul",
        "instructions": {"question": JAPANESE, "focus": "f"},
        "criteria": {"true": "t", "false": {"what": "w", "examples": ["e"]}},
    }
    view = _proposal_view(proposed=wording)
    assert view["proposed_text"] == "\n".join(
        (
            "{",
            '  "criteria": {',
            '    "false": {',
            '      "examples": [',
            '        "e"',
            "      ],",
            '      "what": "w"',
            "    },",
            '    "true": "t"',
            "  },",
            '  "instructions": {',
            '    "focus": "f",',
            f'    "question": "{JAPANESE}"',
            "  },",
            '  "type": "noul"',
            "}",
        )
    )
    assert json.loads(view["current_text"]) == NEWS


def test_question_proposal_view_cuts_a_wording_to_its_bound_keeping_its_newlines() -> None:
    focus = "a" + chr(0x7F) + "b" + LONE_SURROGATE + "c"  # sorted first: before the long question
    wording = {**NEWS, "instructions": {"question": "q" * 5_000, "focus": focus}}
    text = _proposal_view(current=wording)["current_text"]
    assert _utf16_length(text) == wire.MAX_PROPOSAL_TEXT and text.endswith("…")
    assert '"focus": "abc"' in text and chr(0x0A) in text


def test_question_proposal_view_rounds_each_rate_and_keeps_a_missing_one() -> None:
    tried = replace(TRIED, live=QuestionRate("news", 0, None, 2, 0.5))
    assert _proposal_view(tried=tried)["tried"] == {
        "decisions": 24,
        "agree": 19,
        "labels": 6,
        "live": {"yes_n": 0, "yes_rate": None, "no_n": 2, "no_rate": 0.5},
        "trial": {"yes_n": 3, "yes_rate": 0.667, "no_n": 3, "no_rate": 0.333},
    }


# ---- a change proposal act (POST changes/{id}) -------------------------------------------------

CHANGE_ID = "bc21adb7-872b-5924-b52a-16a0476ff6a3"
CHANGE_REVISION = "1f44767c51ab81c0e6531908fa99c5d713dbba08c2670dfaa6b84782902e60eb"
CHANGE_DRAFT = (
    "Link two notes in this vault by adding one wikilink line to the first note, as described "
    "below:\n"
    "- In `30-knowledge/seedlings-need-even-moisture.md`, add one line "
    "`- [[overwatering-signs-in-seedlings]]` under a `## Related` heading at the end of the "
    "note (add the heading if the note has none).\n"
    "Change no other line, and keep every word already there as it is."
)


def _change_item(**changes: Any) -> dict[str, Any]:
    """An approved change proposal as the store lists it in Approvals (`changes.py::
    ChangeProposals.item`'s own shape); built here as a plain dict, not by importing
    `changes.py` -- the same import boundary `change_act_view` itself keeps."""
    item: dict[str, Any] = {
        "kind": "change_proposal",
        "id": CHANGE_ID,
        "origin": {"workspace_id": SCOPE, "session_id": None},
        "revision": CHANGE_REVISION,
        "state": "approved",
        "attention": False,
        "title": "Link seedlings-need-even-moisture to overwatering-signs-in-seedlings",
        "change": "note_link",
        "ref": "30-knowledge/seedlings-need-even-moisture.md",
        "created_at": "2026-10-21T07:50:00.000Z",
        "updated_at": "2026-10-21T07:55:00.000Z",
        "approved_at": "2026-10-21T07:55:00.000Z",
        "rejected_at": None,
        "reason": None,
    }
    item.update(changes)
    return item


def test_the_example_draft_is_the_one_changes_words() -> None:
    """The examples' change draft is written out by hand in `wire.example_views` (the wire module
    keeps clear of `changes.py`) and in the client's examples.json: each is the draft
    `changes.change_draft` words for the same notes."""
    worded = change_draft(
        "note_link",
        "30-knowledge/seedlings-need-even-moisture.md",
        "30-knowledge/overwatering-signs-in-seedlings.md",
        None,
    )
    assert wire.example_views()["change_act"]["draft"] == worded
    assert _examples()["change_act"]["draft"] == worded
    assert worded == CHANGE_DRAFT


def test_change_act_view_embeds_its_item_and_draft() -> None:
    item = _change_item()
    view = wire.change_act_view(item, CHANGE_DRAFT)
    assert sorted(view) == ["change", "draft"]
    assert view["change"] == _change_item() and view["change"] is not item
    assert view["draft"] == CHANGE_DRAFT


def test_change_act_view_answers_a_null_draft_for_a_rejection() -> None:
    item = _change_item(
        state="rejected",
        approved_at=None,
        rejected_at="2026-10-21T07:55:00.000Z",
        reason="Not useful",
    )
    assert wire.change_act_view(item, None) == {"change": item, "draft": None}


def test_change_act_views_draft_is_cut_to_its_bound_keeping_newline_and_tab() -> None:
    long_draft = CHANGE_DRAFT + chr(0x0A) + chr(0x09) + ("x" * 4_100)
    view = wire.change_act_view(_change_item(), long_draft)
    assert _utf16_length(view["draft"]) == wire.MAX_DRAFT_TEXT and view["draft"].endswith("…")
    assert chr(0x0A) + chr(0x09) in view["draft"]


# ---- every string fits the client's parser -----------------------------------------------------


def _client_bounds() -> dict[str, int]:
    """Every numeric `const MAX_X = N;` bound in parse.ts, keyed by its own name -- read from
    the file itself, so a change there is caught here too, the same way `_client_vocabularies`
    reads types.ts."""
    source = (CLIENT / "parse.ts").read_text(encoding="utf-8")
    return {
        name: int(value.replace("_", ""))
        for name, value in re.findall(r"const (MAX_\w+) = ([\d_]+);", source)
    }


# Every free-text field a view builds, by its JSON key, with the parse.ts bound (its own
# constant name) it must fit and whether a newline or tab passes through (state_text's own
# exception, since the Context tab renders it in a `<pre>`; parse.ts's `text`/`stateText`).
# "ref" is here too: never cut, so a ref still present in a built view must already, untouched,
# fit MAX_REF -- parse.ts's `text`'s plain rule, same as any other bounded field below.
_FIELD_BOUNDS: dict[str, tuple[str, bool]] = {
    "what": ("MAX_WHAT", False),
    "why": ("MAX_WHY", False),
    "label": ("MAX_LABEL", False),
    "ref": ("MAX_REF", False),
    "text": ("MAX_DRAFT_TEXT", True),
    "title": ("MAX_TEXT", False),
    "reason": ("MAX_TEXT", False),
    "playbook_version": ("MAX_TEXT", False),
    "part": ("MAX_TEXT", False),
    "name": ("MAX_TEXT", False),
    "answer": ("MAX_TEXT", False),
    "state_text": ("MAX_STATE_TEXT", True),
    "questions_text": ("MAX_QUESTIONS_TEXT", False),
    "current_text": ("MAX_PROPOSAL_TEXT", True),
    "proposed_text": ("MAX_PROPOSAL_TEXT", True),
}


def test_response_bounds_match_the_client_parser() -> None:
    """Our copy of parse.ts's bounds (`wire.MAX_WHAT` and the rest) has not drifted from the
    file itself."""
    bounds = _client_bounds()
    assert {name: bounds[bound] for name, (bound, _nl) in _FIELD_BOUNDS.items()} == {
        "what": wire.MAX_WHAT,
        "why": wire.MAX_WHY,
        "label": wire.MAX_LABEL,
        "ref": wire.MAX_REF,
        "text": wire.MAX_DRAFT_TEXT,
        "title": wire.MAX_TEXT,
        "reason": wire.MAX_TEXT,
        "playbook_version": wire.MAX_TEXT,
        "part": wire.MAX_TEXT,
        "name": wire.MAX_TEXT,
        "answer": wire.MAX_TEXT,
        "state_text": wire.MAX_STATE_TEXT,
        "questions_text": wire.MAX_QUESTIONS_TEXT,
        "current_text": wire.MAX_PROPOSAL_TEXT,
        "proposed_text": wire.MAX_PROPOSAL_TEXT,
    }


def _utf16_length(value: str) -> int:
    """`value`'s length the way JS (`parse.ts`'s `.length`) measures it: a character outside
    the basic plane counts twice, as the surrogate pair it round-trips to on the wire."""
    return sum(2 if ord(ch) > 0xFFFF else 1 for ch in value)


def _fits_parse_ts(value: str, limit: int, *, allow_newline_tab: bool) -> bool:
    """A from-scratch reimplementation of `parse.ts`'s `text`/`stateText`, independent of
    `wire._client_safe` and `wire._fits`: at most `limit` UTF-16 units, no C0 control or DEL; a
    newline or tab passes only when `allow_newline_tab`. Proves the view against the client's
    real rule, not against our own helper."""
    if _utf16_length(value) > limit:
        return False
    for ch in value:
        code = ord(ch)
        if code == 0x7F:
            return False
        if code < 0x20 and not (allow_newline_tab and code in (0x09, 0x0A)):
            return False
    return True


def _walk_strings(value: object, path: str) -> list[tuple[str, str]]:
    """Every (dotted path, string value) pair anywhere inside `value`; a list's own path ends
    "[index]"."""
    if isinstance(value, str):
        return [(path, value)]
    found: list[tuple[str, str]] = []
    if isinstance(value, dict):
        for key, sub in value.items():
            found += _walk_strings(sub, f"{path}.{key}")
    elif isinstance(value, list):
        for index, sub in enumerate(value):
            found += _walk_strings(sub, f"{path}[{index}]")
    return found


def _assert_fits_client(view: object, label: str) -> None:
    """Every bounded string anywhere in `view` fits parse.ts's rule (`_fits_parse_ts`) and holds
    no lone surrogate -- stricter than parse.ts, which does not test for one, but our own rule,
    since every such character a Python `str` can hold is unpaired."""
    bounds = _client_bounds()
    for path, value in _walk_strings(view, label):
        field = path.rsplit(".", 1)[-1].split("[")[0]
        if field not in _FIELD_BOUNDS:
            continue
        bound_name, allow_nl = _FIELD_BOUNDS[field]
        assert _fits_parse_ts(value, bounds[bound_name], allow_newline_tab=allow_nl), path
        assert not any(0xD800 <= ord(ch) <= 0xDFFF for ch in value), path


# Two boundary shapes: a label built from 119 plain characters plus one emoji (120 code points,
# 121 UTF-16 units, as a "garden-planner" inbox capture plus 110 characters produced) and a
# ref carrying a DEL (as a file named "draft<DEL>copy.md" would send).
BOUNDARY_LABEL = "a" * 119 + SEEDLING
DEL_REF = "draft" + chr(0x7F) + "copy.md"
LONG_REF = "r" * (wire.MAX_REF + 1)


def test_an_evidence_label_at_the_surrogate_pair_boundary_is_cut_not_left_over() -> None:
    """A label of 120 code points that is 121 UTF-16 units (one of them astral) is cut to
    120 units, never sent over -- and the cut never splits the emoji's surrogate pair."""
    chip = wire._evidence(EvidenceLabel(BOUNDARY_LABEL, workspace_id=SCOPE, ref="a.md"))
    assert chip["label"] == "a" * 119 + "…"
    assert _utf16_length(chip["label"]) == wire.MAX_LABEL
    assert not any(0xD800 <= ord(ch) <= 0xDFFF for ch in chip["label"])


def test_a_ref_carrying_del_is_dropped_with_its_pair_not_sent() -> None:
    """A ref built from a file named with a DEL byte is dropped, keeping only the label --
    never cut, since a cut ref would point at the wrong note. Same for one merely too long."""
    assert wire._evidence(EvidenceLabel("A note", workspace_id=SCOPE, ref=DEL_REF)) == {
        "label": "A note"
    }
    assert wire._evidence(EvidenceLabel("A note", workspace_id=SCOPE, ref=LONG_REF)) == {
        "label": "A note"
    }


def test_an_action_whose_ref_is_unsafe_is_dropped_whole() -> None:
    """An `open_note` action has no shape without `ref`, so a DEL or over-long ref drops the
    whole action, not just the ref."""
    bad_del = ActionSpec("open_note", "Open note", True, ref=DEL_REF, workspace_id=SCOPE)
    bad_long = ActionSpec("open_note", "Open note", True, ref=LONG_REF, workspace_id=SCOPE)
    assert wire._action(bad_del) is None
    assert wire._action(bad_long) is None
    clean = wire._action(ActionSpec("open_note", "Open note", True, ref="a.md", workspace_id=SCOPE))
    assert clean is not None and clean["ref"] == "a.md"


def test_a_draft_action_without_text_is_dropped_whole() -> None:
    """A `draft_chat` or `draft_new` action with nothing to fill the message box is as useless
    as one whose ref fails -- dropped whole, never sent with a missing "text"."""
    assert wire._action(ActionSpec("draft_chat", "Draft it", True, session_id=SESSION)) is None
    assert wire._action(ActionSpec("draft_new", "Draft it", True)) is None
    # an action of some other type never carries "text" at all, drop or not.
    assert wire._action(ActionSpec("open_runs", "Open runs", False)) == {
        "type": "open_runs",
        "label": "Open runs",
        "primary": False,
    }


def test_a_draft_actions_text_rides_cut_to_max_draft_text_keeping_newline_and_tab() -> None:
    filled = wire._action(
        ActionSpec("draft_chat", "Draft it", True, session_id=SESSION, text="Learn this.")
    )
    assert filled == {
        "type": "draft_chat",
        "label": "Draft it",
        "primary": True,
        "session_id": SESSION,
        "text": "Learn this.",
    }
    # BAD_DRAFT_TEXT keeps its newline and tab close enough to the front to survive the cut.
    cut = wire._action(ActionSpec("draft_new", "Draft it", True, text=BAD_DRAFT_TEXT))
    assert cut is not None
    assert _utf16_length(cut["text"]) == wire.MAX_DRAFT_TEXT and cut["text"].endswith("…")
    assert chr(0x0A) + chr(0x09) in cut["text"] and chr(0x01) not in cut["text"]


BAD_WHAT = "w" * 290 + "".join(CONTROL) + "x" * 20  # over MAX_WHAT, every CONTROL char inside
BAD_WHY = "y" * 390 + LONE_SURROGATE + "z" * 20  # over MAX_WHY, a lone surrogate inside
BAD_TITLE = "T" * 390 + chr(0x01) + "t" * 20  # over MAX_TEXT, a C0 control inside
BAD_REASON = "R" * 395 + chr(0x7F) + "r" * 20  # over MAX_TEXT, DEL inside
BAD_CALL_TITLE = "C" * 395 + LONE_SURROGATE + "c" * 20
BAD_PLAYBOOK_VERSION = "P" * 395 + chr(0x02) + "p" * 20
BAD_SPLIT_PART = "part" + chr(0x1B) + "x" * 400
BAD_QUESTION_NAME = "name" + chr(0x00) + "y" * 400
BAD_QUESTION_ANSWER = "answer" + LONE_SURROGATE + "z" * 400
# keeps the newline and tab it carries (state_text's own exception); strips the SOH (0x01).
BAD_STATE_TEXT = ("s" * 19_990) + chr(0x0A) + chr(0x09) + chr(0x01) + ("s" * 100)
# a draft action's own text: over MAX_DRAFT_TEXT, keeps its newline and tab, strips the SOH.
BAD_DRAFT_TEXT = ("d" * 3_990) + chr(0x0A) + chr(0x09) + chr(0x01) + ("d" * 100)
# the newline is stripped here, unlike state_text -- questions_text keeps the plain rule.
BAD_QUESTIONS_TEXT = ("q" * 19_990) + chr(0x0A) + ("q" * 100)
BAD_ROW_REASON = "reason" + chr(0x03) + "z" * 400
BAD_ROW_WHY = "why" + LONE_SURROGATE + "w" * 400
# a wording past MAX_PROPOSAL_TEXT in emoji, so its cut meets a surrogate pair; DEL and a lone
# surrogate stay raw in its pretty JSON (a C0 control would be escaped).
BAD_WORDING = {
    **NEWS,
    "instructions": {"question": SEEDLING * 2_100, "focus": chr(0x7F) + LONE_SURROGATE},
}


def _adversarial_item() -> ItemRecord:
    item = _item(
        "badge",
        kind="ask",
        evidence=(
            EvidenceLabel(BOUNDARY_LABEL, workspace_id=SCOPE, ref="clean/tasks.md"),
            EvidenceLabel("DEL ref", workspace_id=SCOPE, ref=DEL_REF),
            EvidenceLabel("Too-long ref", workspace_id=SCOPE, ref=LONG_REF),
            EvidenceLabel("Started by VIKI", session_id=SESSION),
        ),
        actions=(
            ActionSpec("open_note", "Open the tasks", True, ref=DEL_REF, workspace_id=SCOPE),
            ActionSpec("open_runs", "Open runs", False),
            ActionSpec("draft_new", "Draft it", False, text=BAD_DRAFT_TEXT),
        ),
    )
    return replace(item, body=replace(item.body, what=BAD_WHAT, why=BAD_WHY))


def test_every_view_builder_output_fits_the_client_parser() -> None:
    """Adversarial records -- emoji at the cut boundary, DEL and other C0 characters, lone
    surrogates, over-long refs and labels -- through every view builder, each checked against
    parse.ts's own rules (`_assert_fits_client`)."""
    launcher = _as_sent(_launcher(ON, (_adversarial_item(),)))
    _assert_fits_client(launcher, "launcher")

    mission = _as_sent(wire.mission_view(_mission(title=BAD_TITLE, reason=BAD_REASON)))
    _assert_fits_client(mission, "mission")

    call = _call(
        body=replace(
            _call().body,
            title=BAD_CALL_TITLE,
            playbook_version=BAD_PLAYBOOK_VERSION,
            state_text=BAD_STATE_TEXT,
            questions_text=BAD_QUESTIONS_TEXT,
            split=(("candidate", 10), (BAD_SPLIT_PART, 20)),
        )
    )
    calls = _as_sent(wire.calls_view(ON, [call], latest="model"))
    _assert_fits_client(calls, "calls")

    detail = _as_sent(
        wire.call_detail_view(
            call,
            questions=((BAD_QUESTION_NAME, BOUNDARY_LABEL, BAD_QUESTION_ANSWER),),
            largest_question=10,
        )
    )
    _assert_fits_client(detail, "call_detail")
    assert chr(0x0A) + chr(0x09) in detail["state_text"]
    assert chr(0x01) not in detail["state_text"]
    assert chr(0x0A) not in detail["questions_text"]

    held_row = {
        "episode_id": EPISODE,
        "created_at": "2026-10-21T10:05:00.000Z",
        "candidate": "inbox",
        "level": "silent",
        "reason": BAD_ROW_REASON,
        "why": BAD_ROW_WHY,
        "feedback": None,
    }
    recent_row = {**held_row, "policy": "rules", "feedback": "useful"}
    page = _as_sent(_page(held_back=[held_row], recent=[recent_row]))
    _assert_fits_client(page, "page")

    proposal = _as_sent(_proposal_view(current=BAD_WORDING, proposed=BAD_WORDING))
    _assert_fits_client(proposal, "question_proposal")
    assert proposal["current_text"].endswith("…") and chr(0x7F) not in proposal["current_text"]

    # the item's own bad chip/action never reached the launcher with their bad content.
    sent_item = launcher["items"][0]
    assert all(chip.get("ref") not in (DEL_REF, LONG_REF) for chip in sent_item["evidence"])
    assert [action["type"] for action in sent_item["actions"]] == ["open_runs", "draft_new"]
    # the draft action's own over-long text is cut to MAX_DRAFT_TEXT, not dropped like the ref.
    draft_text = sent_item["actions"][1]["text"]
    assert _utf16_length(draft_text) == wire.MAX_DRAFT_TEXT and draft_text.endswith("…")
    assert chr(0x0A) + chr(0x09) in draft_text and chr(0x01) not in draft_text


# ---- parsers ----------------------------------------------------------------------------------

_DROP = object()


def changed(base: Mapping[str, Any], path: str, value: object = _DROP) -> dict[str, Any]:
    """A deep copy of `base` with the dotted `path` set to `value`, or removed."""
    body = copy.deepcopy(dict(base))
    *parents, last = path.split(".")
    target = body
    for name in parents:
        target = target[name]
    if value is _DROP:
        del target[last]
    else:
        target[last] = value
    return body


def refused(cases: Mapping[str, object]) -> pytest.MarkDecorator:
    return pytest.mark.parametrize("body", list(cases.values()), ids=list(cases))


def _refuses(parse: Callable[[object], object], body: object) -> None:
    with pytest.raises(DecisionLoopError) as caught:
        parse(body)
    assert caught.value.code == "invalid_request"


PAGE: dict[str, Any] = {
    "native_waiting": 2,
    "runs": {"working": 1, "review": 0},
    "git": {"state": "known", "dirty": 3},
    "routine": {"routine_id": ROUTINE, "next_run": "2026-10-21T15:00:00.000Z", "paused": False},
    "skill": None,
    "suggest": None,
    "second_look": None,
    "context": None,
    "sources": None,
}
PRESENCE: dict[str, Any] = {
    "window": WINDOW,
    "focused": True,
    "typing": False,
    "chat": SESSION,
    "chat_title": "Garden planner",
    "note": {"workspace_id": SCOPE, "ref": "10-projects/garden-planner/tasks.md"},
    "launcher": False,
    "page": PAGE,
}


def test_parse_presence_reads_one_window() -> None:
    assert wire.parse_presence(PRESENCE) == (
        WINDOW,
        Presence(
            focused=True,
            typing=False,
            away=False,
            chat_id=SESSION,
            chat_title="Garden planner",
            note_ref=(SCOPE, "10-projects/garden-planner/tasks.md"),
            launcher_open=False,
            page=PageFacts(
                2, 1, 0, "known", 3, ROUTINE, datetime(2026, 10, 21, 15, tzinfo=UTC), False
            ),
        ),
    )
    quiet = {**PRESENCE, "chat": None, "chat_title": None, "note": None, "page": None}
    assert wire.parse_presence(quiet) == (
        WINDOW,
        Presence(True, False, False, None, None, None, False, None),
    )


@pytest.mark.parametrize(
    "title", ["", "t" * 120, JAPANESE, f"Seedlings {SEEDLING}", "No-break" + chr(0xA0) + "space"]
)
def test_parse_presence_takes_any_short_plain_title(title: str) -> None:
    longest_ref = changed(PRESENCE, "note.ref", "r" * 512)
    _window, presence = wire.parse_presence(changed(longest_ref, "chat_title", title))
    assert (presence.chat_title, presence.note_ref) == (title, (SCOPE, "r" * 512))


BAD_PRESENCE: dict[str, object] = {
    "not an object": [WINDOW],
    "null": None,
    "an extra key": changed(PRESENCE, "away", False),
    **{f"no {key}": changed(PRESENCE, key) for key in PRESENCE},
    "window not a UUID": changed(PRESENCE, "window", "window-1"),
    "window in upper case": changed(PRESENCE, "window", WINDOW.upper()),
    "window in braces": changed(PRESENCE, "window", "{" + WINDOW + "}"),
    "window without hyphens": changed(PRESENCE, "window", WINDOW.replace("-", "")),
    "window null": changed(PRESENCE, "window", None),
    "focused a number": changed(PRESENCE, "focused", 1),
    "typing a string": changed(PRESENCE, "typing", "false"),
    "launcher null": changed(PRESENCE, "launcher", None),
    "chat not a UUID": changed(PRESENCE, "chat", "chat-1"),
    "chat in upper case": changed(PRESENCE, "chat", SESSION.upper()),
    "chat a number": changed(PRESENCE, "chat", 7),
    "chat_title too long": changed(PRESENCE, "chat_title", "t" * 121),
    "chat_title a number": changed(PRESENCE, "chat_title", 12),
    **{
        f"chat_title with U+{ord(ch):04X}": changed(PRESENCE, "chat_title", f"Garden{ch}planner")
        for ch in CONTROL
    },
    "chat_title with a lone surrogate": changed(PRESENCE, "chat_title", "Garden" + LONE_SURROGATE),
    "note a string": changed(PRESENCE, "note", "10-projects/x.md"),
    "note with an extra key": changed(PRESENCE, "note.title", "Tasks"),
    "note without its ref": changed(PRESENCE, "note.ref"),
    "note workspace not a UUID": changed(PRESENCE, "note.workspace_id", "vault"),
    "note ref empty": changed(PRESENCE, "note.ref", ""),
    "note ref too long": changed(PRESENCE, "note.ref", "r" * 513),
    "note ref a list": changed(PRESENCE, "note.ref", ["x.md"]),
    **{
        f"note ref with U+{ord(ch):04X}": changed(PRESENCE, "note.ref", f"a{ch}b.md")
        for ch in CONTROL
    },
    "note ref with a lone surrogate": changed(PRESENCE, "note.ref", "a" + LONE_SURROGATE),
    "page a list": changed(PRESENCE, "page", []),
    "page with an extra key": changed(PRESENCE, "page.extra", 1),
    "page git state not listed": changed(PRESENCE, "page.git.state", "dirty"),
}


@refused(BAD_PRESENCE)
def test_parse_presence_refuses(body: object) -> None:
    _refuses(wire.parse_presence, body)


def _raw_page(**changes: Any) -> dict[str, Any]:
    """A page with nothing waiting, nothing running, git unknown, no routine, no skill hint, no
    skill-suggestion facts and no second-look hint."""
    return {
        "native_waiting": 0,
        "runs": {"working": 0, "review": 0},
        "git": {"state": "unknown", "dirty": None},
        "routine": None,
        "skill": None,
        "suggest": None,
        "second_look": None,
        "context": None,
        "sources": None,
        **changes,
    }


SKILL: dict[str, Any] = {
    "trigger": "correction",
    "sessions": [SESSION, "0f0f0f0f-0000-4000-8000-000000000002"],
    "titles": ["Garden planner", ""],
}


def _routine(**changes: Any) -> dict[str, Any]:
    return {
        "routine_id": ROUTINE,
        "next_run": "2026-10-07T12:00:00.000Z",
        "paused": False,
        **changes,
    }


def test_page_validation_accepts_the_shape_at_its_bounds() -> None:
    observed = wire.parse_page(
        {
            "native_waiting": 999,
            "runs": {"working": 0, "review": 999},
            "git": {"state": "known", "dirty": 100_000},
            "routine": {"routine_id": ROUTINE, "next_run": "2026-10-08T03:00:00Z", "paused": False},
            "skill": None,
            "suggest": None,
            "second_look": None,
            "context": None,
            "sources": None,
        }
    )
    assert observed == PageFacts(
        999, 0, 999, "known", 100_000, ROUTINE, datetime(2026, 10, 8, 3, tzinfo=UTC), False
    )
    with pytest.raises(FrozenInstanceError):
        observed.native_waiting = 0  # type: ignore[misc]
    with_millis = wire.parse_page(_raw_page(routine=_routine(next_run="2026-10-08T03:00:00.250Z")))
    assert with_millis.next_run == datetime(2026, 10, 8, 3, 0, 0, 250_000, tzinfo=UTC)
    assert wire.parse_page(_raw_page(git={"state": "not_repository", "dirty": 0})).git_dirty == 0
    assert wire.parse_page(_raw_page()) == PageFacts(0, 0, 0, "unknown", None, None, None, None)


def test_parse_page_reads_a_skill_hint() -> None:
    observed = wire.parse_page(_raw_page(skill=SKILL))
    assert observed.skill == SkillHint(
        "correction", (SESSION, "0f0f0f0f-0000-4000-8000-000000000002"), ("Garden planner", "")
    )
    assert wire.parse_page(_raw_page(skill=None)).skill is None
    for trigger in contracts.SKILL_TRIGGERS:
        page = wire.parse_page(_raw_page(skill={**SKILL, "trigger": trigger}))
        assert page.skill is not None and page.skill.trigger == trigger
    ten_sessions = [f"00000000-0000-4000-8000-{n:012x}" for n in range(10)]
    full = wire.parse_page(
        _raw_page(skill={"trigger": "long_session", "sessions": ten_sessions, "titles": [""] * 10})
    )
    assert full.skill is not None and len(full.skill.sessions) == 10


SECOND_LOOK: dict[str, Any] = {
    "session": SESSION,
    "turn": "3f0b9c52-6a51-4c27-9a44-2f7e1d0c8b11",
    "trigger": "long_session",
    "title": "Garden plan",
    "refs": ["notes/garden-plan.md", "notes/beds.md"],
}


def _look(**changes: object) -> dict[str, Any]:
    return _raw_page(second_look={**SECOND_LOOK, **changes})


def test_the_page_reports_the_chat_in_views_second_look() -> None:
    observed = wire.parse_page(_raw_page(second_look=SECOND_LOOK))
    assert observed.second_look == SecondLookHint(
        SESSION,
        "3f0b9c52-6a51-4c27-9a44-2f7e1d0c8b11",
        "long_session",
        "Garden plan",
        ("notes/garden-plan.md", "notes/beds.md"),
    )
    assert wire.parse_page(_raw_page(second_look=None)).second_look is None
    for trigger in contracts.SECOND_LOOK_TYPES:
        page = wire.parse_page(_look(trigger=trigger))
        assert page.second_look is not None and page.second_look.trigger == trigger
    untitled = wire.parse_page(_look(title="")).second_look
    assert untitled is not None and untitled.title == ""
    ten = [f"n{n}.md" for n in range(contracts.MAX_SECOND_LOOK_REFS)]
    assert wire.parse_page(_look(refs=ten)).second_look is not None
    # 1,024 bytes in all is the most; a 3-byte character counts three.
    exact_bytes = ["a" * 509 + ".md", "b" * 500 + JAPANESE[0] * 3 + ".md"]
    assert sum(len(ref.encode()) for ref in exact_bytes) == contracts.MAX_SECOND_LOOK_REF_BYTES
    assert wire.parse_page(_look(refs=exact_bytes)).second_look is not None


BAD_SECOND_LOOK: dict[str, object] = {
    "second_look a list": _raw_page(second_look=[]),
    "second_look a string": _raw_page(second_look="notes/a.md"),
    "no second_look key": {k: v for k, v in _raw_page().items() if k != "second_look"},
    "an extra key": _look(extra=1),
    **{
        f"no {key}": _raw_page(second_look={k: v for k, v in SECOND_LOOK.items() if k != key})
        for key in SECOND_LOOK
    },
    "session not a UUID": _look(session="chat-1"),
    "session in upper case": _look(session=SESSION.upper()),
    "turn not a UUID": _look(turn="turn-1"),
    "turn null": _look(turn=None),
    "trigger repeated_request": _look(trigger="repeated_request"),
    "trigger null": _look(trigger=None),
    "title too long": _look(title="t" * 121),
    "title null": _look(title=None),
    **{f"title with U+{ord(ch):04X}": _look(title=f"Garden{ch}plan") for ch in CONTROL},
    "refs null": _look(refs=None),
    "refs a string": _look(refs="notes/a.md"),
    "refs empty": _look(refs=[]),
    "refs eleven": _look(refs=[f"n{n}.md" for n in range(contracts.MAX_SECOND_LOOK_REFS + 1)]),
    "refs duplicate": _look(refs=["notes/a.md", "notes/a.md"]),
    "ref empty": _look(refs=[""]),
    "ref too long": _look(refs=["r" * 513]),
    "ref a number": _look(refs=[7]),
    "refs 1,025 bytes": _look(refs=["a" * 509 + ".md", "b" * 501 + JAPANESE[0] * 3 + ".md"]),
    **{f"ref with U+{ord(ch):04X}": _look(refs=[f"a{ch}b.md"]) for ch in CONTROL},
    "ref with a lone surrogate": _look(refs=["a" + LONE_SURROGATE]),
}


@refused(BAD_SECOND_LOOK)
def test_parse_page_refuses_bad_second_look_facts(body: object) -> None:
    _refuses(wire.parse_page, body)


CONTEXT: dict[str, Any] = {"session": SESSION, "percent": 75}
SOURCES: dict[str, Any] = {
    "session": SESSION,
    "turn": "3f0b9c52-6a51-4c27-9a44-2f7e1d0c8b11",
    "notes": False,
}


def test_the_page_reports_the_chat_in_views_context_and_sources() -> None:
    observed = wire.parse_page(_raw_page(context=CONTEXT, sources=SOURCES))
    assert observed.context == ContextHint(SESSION, 75)
    assert observed.sources == SourceHint(SESSION, SOURCES["turn"], False)
    quiet = wire.parse_page(_raw_page())
    assert (quiet.context, quiet.sources) == (None, None)
    for percent in (0, 100):
        page = wire.parse_page(_raw_page(context={**CONTEXT, "percent": percent}))
        assert page.context == ContextHint(SESSION, percent)
    page = wire.parse_page(_raw_page(sources={**SOURCES, "notes": True}))
    assert page.sources is not None and page.sources.notes is True


BAD_CONTEXT_AND_SOURCES: dict[str, object] = {
    "no context key": {k: v for k, v in _raw_page().items() if k != "context"},
    "no sources key": {k: v for k, v in _raw_page().items() if k != "sources"},
    "context a list": _raw_page(context=[]),
    "context an extra key": _raw_page(context={**CONTEXT, "extra": 1}),
    "context without session": _raw_page(context={"percent": 75}),
    "context without percent": _raw_page(context={"session": SESSION}),
    "context session not a UUID": _raw_page(context={**CONTEXT, "session": "chat-1"}),
    "context session in upper case": _raw_page(context={**CONTEXT, "session": SESSION.upper()}),
    "percent -1": _raw_page(context={**CONTEXT, "percent": -1}),
    "percent 101": _raw_page(context={**CONTEXT, "percent": 101}),
    "percent 70.5": _raw_page(context={**CONTEXT, "percent": 70.5}),
    "percent true": _raw_page(context={**CONTEXT, "percent": True}),
    "percent a string": _raw_page(context={**CONTEXT, "percent": "75"}),
    "sources a string": _raw_page(sources="web"),
    "sources an extra key": _raw_page(sources={**SOURCES, "extra": 1}),
    **{
        f"sources without {key}": _raw_page(sources={k: v for k, v in SOURCES.items() if k != key})
        for key in SOURCES
    },
    "sources session not a UUID": _raw_page(sources={**SOURCES, "session": "chat-1"}),
    "sources turn not a UUID": _raw_page(sources={**SOURCES, "turn": "turn-1"}),
    "sources notes a number": _raw_page(sources={**SOURCES, "notes": 1}),
    "sources notes a string": _raw_page(sources={**SOURCES, "notes": "false"}),
    "sources notes null": _raw_page(sources={**SOURCES, "notes": None}),
}


@refused(BAD_CONTEXT_AND_SOURCES)
def test_parse_page_refuses_bad_context_and_sources(body: object) -> None:
    _refuses(wire.parse_page, body)


SUGGEST: dict[str, Any] = {
    "session": SESSION,
    "title": "Sprint retro notes",
    "used": ["sprint-retro", "inbox"],
}


def test_parse_page_reads_skill_suggestion_facts() -> None:
    observed = wire.parse_page(_raw_page(suggest=SUGGEST))
    assert observed.suggest == SuggestHint(SESSION, "Sprint retro notes", ("sprint-retro", "inbox"))
    assert wire.parse_page(_raw_page(suggest=None)).suggest is None
    used = [f"skill-{n}" for n in range(wire.MAX_SUGGEST_USED - 1)] + ["a" * 64]
    full = wire.parse_page(_raw_page(suggest={**SUGGEST, "title": "t" * 120, "used": used}))
    assert full.suggest == SuggestHint(SESSION, "t" * 120, tuple(used))
    for title in ("t", JAPANESE, f"Seedlings {SEEDLING}", "No-break" + chr(0xA0) + "space"):
        page = wire.parse_page(_raw_page(suggest={**SUGGEST, "title": title, "used": []}))
        assert page.suggest == SuggestHint(SESSION, title, ())


def _suggest(**changes: object) -> dict[str, Any]:
    return _raw_page(suggest={**SUGGEST, **changes})


BAD_SUGGEST: dict[str, object] = {
    "no suggest key": {key: value for key, value in _raw_page().items() if key != "suggest"},
    "suggest a list": _raw_page(suggest=[]),
    "suggest a string": _raw_page(suggest="sprint-retro"),
    "an extra key": _suggest(skills=[]),
    **{
        f"no {key}": _raw_page(suggest={k: v for k, v in SUGGEST.items() if k != key})
        for key in SUGGEST
    },
    "session not a UUID": _suggest(session="chat-1"),
    "session in upper case": _suggest(session=SESSION.upper()),
    "session null": _suggest(session=None),
    "title empty": _suggest(title=""),
    "title too long": _suggest(title="t" * 121),
    "title null": _suggest(title=None),
    "title a number": _suggest(title=7),
    **{f"title with U+{ord(ch):04X}": _suggest(title=f"Sprint{ch}retro") for ch in CONTROL},
    "title with a lone surrogate": _suggest(title="Sprint" + LONE_SURROGATE),
    "used null": _suggest(used=None),
    "used a string": _suggest(used="sprint-retro"),
    "used too many": _suggest(used=[f"skill-{n}" for n in range(wire.MAX_SUGGEST_USED + 1)]),
    "used not distinct": _suggest(used=["inbox", "inbox"]),
    "used a number": _suggest(used=[7]),
    "used null name": _suggest(used=[None]),
    **{
        f"used {name!r}": _suggest(used=[name])
        for name in (
            "",
            "Inbox",
            "sprint_retro",
            "sprint retro",
            "-inbox",
            "inbox-",
            "sprint--retro",
            "/inbox",
            "inbox\n",
            "ｉｎｂｏｘ",
            "振り返り",
            "a" * 65,
        )
    },
}


@refused(BAD_SUGGEST)
def test_parse_page_refuses_bad_skill_suggestion_facts(body: object) -> None:
    _refuses(wire.parse_page, body)


@pytest.mark.parametrize(
    "bad",
    [
        None,
        [],
        "page",
        {key: value for key, value in _raw_page().items() if key != "routine"},
        _raw_page(extra=1),
        _raw_page(native_waiting=-1),
        _raw_page(native_waiting=1000),
        _raw_page(native_waiting=True),
        _raw_page(native_waiting=1.0),
        _raw_page(native_waiting="1"),
        _raw_page(native_waiting=None),
        _raw_page(runs=[0, 0]),
        _raw_page(runs={"working": 0}),
        _raw_page(runs={"working": 0, "review": 0, "done": 0}),
        _raw_page(runs={"working": 1000, "review": 0}),
        _raw_page(runs={"working": 0, "review": False}),
        _raw_page(git={"state": "known"}),
        _raw_page(git={"state": "dirty", "dirty": None}),
        _raw_page(git={"state": ["known"], "dirty": None}),
        _raw_page(git={"state": "known", "dirty": -1}),
        _raw_page(git={"state": "known", "dirty": 100_001}),
        _raw_page(git={"state": "known", "dirty": True}),
        _raw_page(git={"state": "known", "dirty": 1.5}),
        _raw_page(routine=[]),
        _raw_page(routine={"routine_id": ROUTINE, "next_run": "2026-10-07T12:00:00.000Z"}),
        _raw_page(routine=_routine(name="Morning")),
        _raw_page(routine=_routine(routine_id="r1")),
        _raw_page(routine=_routine(routine_id=ROUTINE.upper())),
        _raw_page(routine=_routine(routine_id=None)),
        _raw_page(routine=_routine(paused=1)),
        _raw_page(routine=_routine(paused="true")),
        _raw_page(routine=_routine(paused=None)),
        _raw_page(routine=_routine(next_run=None)),
        _raw_page(routine=_routine(next_run=1791460800)),
        _raw_page(routine=_routine(next_run="2026-10-07T12:00:00+00:00")),
        _raw_page(routine=_routine(next_run="2026-10-07T12:00:00.12Z")),
        _raw_page(routine=_routine(next_run="2026-10-07 12:00:00Z")),
        _raw_page(routine=_routine(next_run="2026-10-07T12:00:00Z\n")),
        _raw_page(routine=_routine(next_run="2026-02-30T00:00:00Z")),
        _raw_page(routine=_routine(next_run="2026-10-07T24:00:00Z")),
        _raw_page(
            routine=_routine(
                next_run="".join(map(chr, (0xFF12, 0xFF10, 0xFF12, 0xFF16))) + "-10-07T12:00:00Z"
            )
        ),  # fullwidth year
        _raw_page(skill=[]),
        _raw_page(skill="correction"),
        _raw_page(skill={**SKILL, "extra": 1}),
        _raw_page(skill={"trigger": "correction", "sessions": [SESSION]}),  # no titles
        _raw_page(skill={**SKILL, "trigger": "clicked"}),
        _raw_page(skill={**SKILL, "sessions": []}),
        _raw_page(
            skill={**SKILL, "sessions": [f"00000000-0000-4000-8000-{n:012x}" for n in range(11)]}
        ),
        _raw_page(skill={**SKILL, "sessions": [SESSION, SESSION]}),  # not distinct
        _raw_page(skill={**SKILL, "sessions": "not-a-list"}),
        _raw_page(skill={**SKILL, "sessions": [SESSION, "not-a-uuid"]}),
        _raw_page(skill={**SKILL, "titles": ["Garden planner"]}),  # shorter than sessions
        _raw_page(skill={**SKILL, "titles": "not-a-list"}),
        _raw_page(skill={**SKILL, "titles": ["Garden planner", "b" + chr(0x01)]}),
    ],
)
def test_page_validation_refuses_bad_input(bad: object) -> None:
    _refuses(wire.parse_page, bad)


FEEDBACK_ON_ITEM: dict[str, Any] = {
    "item_key": ITEM_KEY,
    "episode_id": None,
    "value": "wrong",
    "reason": "bad_timing",
    "source": "launcher",
}
FEEDBACK_ON_EPISODE: dict[str, Any] = {
    "item_key": None,
    "episode_id": EPISODE,
    "value": "missed",
    "reason": None,
    "source": "page",
}


def test_parse_feedback_takes_shown_values_on_items_and_held_values_on_episodes() -> None:
    assert wire.parse_feedback(FEEDBACK_ON_ITEM) == wire.FeedbackRequest(
        ITEM_KEY, None, "wrong", "bad_timing", "launcher"
    )
    assert wire.parse_feedback(FEEDBACK_ON_EPISODE) == wire.FeedbackRequest(
        None, EPISODE, "missed", None, "page"
    )
    for value in contracts.FEEDBACK_SHOWN:
        body = {**FEEDBACK_ON_ITEM, "value": value, "reason": None}
        assert wire.parse_feedback(body).value == value
    for value in contracts.FEEDBACK_HELD:
        assert wire.parse_feedback({**FEEDBACK_ON_EPISODE, "value": value}).value == value
    for reason in contracts.FEEDBACK_REASONS:
        assert wire.parse_feedback({**FEEDBACK_ON_ITEM, "reason": reason}).reason == reason
    for source in contracts.SOURCES:
        assert wire.parse_feedback({**FEEDBACK_ON_ITEM, "source": source}).source == source


BAD_FEEDBACK: dict[str, object] = {
    "not an object": "useful",
    "an extra key": changed(FEEDBACK_ON_ITEM, "note", "too early"),
    **{f"no {key}": changed(FEEDBACK_ON_ITEM, key) for key in FEEDBACK_ON_ITEM},
    "both an item and an episode": changed(FEEDBACK_ON_ITEM, "episode_id", EPISODE),
    "neither an item nor an episode": changed(FEEDBACK_ON_ITEM, "item_key", None),
    "item key in upper case": changed(FEEDBACK_ON_ITEM, "item_key", ITEM_KEY.upper()),
    "item key too short": changed(FEEDBACK_ON_ITEM, "item_key", ITEM_KEY[:63]),
    "item key a number": changed(FEEDBACK_ON_ITEM, "item_key", 7),
    "episode not a UUID": changed(FEEDBACK_ON_EPISODE, "episode_id", "episode-1"),
    "a held value on an item": {**FEEDBACK_ON_ITEM, "value": "missed", "reason": None},
    "a shown value on an episode": changed(FEEDBACK_ON_EPISODE, "value", "useful"),
    "an unknown value": changed(FEEDBACK_ON_ITEM, "value", "great"),
    "a value in a list": changed(FEEDBACK_ON_ITEM, "value", ["wrong"]),
    "a reason with later": changed(FEEDBACK_ON_ITEM, "value", "later"),
    "a reason with a held value": changed(FEEDBACK_ON_EPISODE, "reason", "out_of_date"),
    "an unknown reason": changed(FEEDBACK_ON_ITEM, "reason", "boring"),
    "a reason number": changed(FEEDBACK_ON_ITEM, "reason", 1),
    "an unknown source": changed(FEEDBACK_ON_ITEM, "source", "bot"),
    "no source value": changed(FEEDBACK_ON_ITEM, "source", None),
}


@refused(BAD_FEEDBACK)
def test_parse_feedback_refuses(body: object) -> None:
    _refuses(wire.parse_feedback, body)


def test_parse_observe() -> None:
    keys = [f"{n:064x}" for n in range(50)]  # the launcher's most items, MAX_OPEN_ITEMS
    for event in wire.OBSERVE_EVENTS:
        assert wire.parse_observe({"event": event, "item_keys": keys}) == (event, tuple(keys))
    assert wire.parse_observe({"event": "shown", "item_keys": [ITEM_KEY]}) == (
        "shown",
        (ITEM_KEY,),
    )


OBSERVE: dict[str, Any] = {"event": "shown", "item_keys": [ITEM_KEY]}
BAD_OBSERVE: dict[str, object] = {
    "not an object": ["shown"],
    "an extra key": changed(OBSERVE, "channel", "launcher"),
    **{f"no {key}": changed(OBSERVE, key) for key in OBSERVE},
    "an unknown event": changed(OBSERVE, "event", "clicked"),
    "event null": changed(OBSERVE, "event", None),
    "keys a string": changed(OBSERVE, "item_keys", ITEM_KEY),
    "keys an object": changed(OBSERVE, "item_keys", {ITEM_KEY: 1}),
    "no keys at all": changed(OBSERVE, "item_keys", []),
    "51 keys": changed(OBSERVE, "item_keys", [f"{n:064x}" for n in range(51)]),
    "a key twice": changed(OBSERVE, "item_keys", [ITEM_KEY, ITEM_KEY]),
    "a key in upper case": changed(OBSERVE, "item_keys", [ITEM_KEY.upper()]),
    "a key a number": changed(OBSERVE, "item_keys", [7]),
    "a key null": changed(OBSERVE, "item_keys", [None]),
}


@refused(BAD_OBSERVE)
def test_parse_observe_refuses(body: object) -> None:
    _refuses(wire.parse_observe, body)


def test_parse_report() -> None:
    for topic in contracts.TOPICS:
        assert wire.parse_report({"topic": topic, "source": "page"}) == (topic, "page")
    for source in contracts.SOURCES:
        assert wire.parse_report({"topic": "inbox", "source": source}) == ("inbox", source)


REPORT: dict[str, Any] = {"topic": "unwatched", "source": "page"}
BAD_REPORT: dict[str, object] = {
    "not an object": "unwatched",
    "an extra key": changed(REPORT, "trigger", "review"),
    **{f"no {key}": changed(REPORT, key) for key in REPORT},
    "an unknown topic": changed(REPORT, "topic", "weather"),
    "topic null": changed(REPORT, "topic", None),
    "an unknown source": changed(REPORT, "source", "bot"),
    "source a number": changed(REPORT, "source", 3),
}


@refused(BAD_REPORT)
def test_parse_report_refuses(body: object) -> None:
    _refuses(wire.parse_report, body)


def test_parse_preference() -> None:
    for candidate in contracts.CANDIDATES:
        body = {"name": "unmute", "candidate": candidate}
        assert wire.parse_preference(body) == ("unmute", candidate)
    assert wire.parse_preference({"name": "review_seen"}) == ("review_seen", None)
    assert wire.parse_preference({"name": "reset_learning"}) == ("reset_learning", None)


def test_preferences_take_try_again_and_raise_again() -> None:
    for look in contracts.SECOND_LOOKS:
        body = {"name": "try_again", "candidate": look}
        assert wire.parse_preference(body) == ("try_again", look)
    for kind in contracts.FINDING_KINDS:
        assert wire.parse_preference({"name": "raise_again", "kind": kind}) == (
            "raise_again",
            kind,
        )


def test_preferences_take_try_again_for_a_project_plan() -> None:
    for plan in contracts.PROJECT_PLANS:
        assert wire.parse_preference({"name": "try_again", "candidate": plan}) == (
            "try_again",
            plan,
        )
        _refuses(wire.parse_preference, {"name": "raise_again", "kind": plan})


def test_preferences_take_try_again_for_a_message_draft() -> None:
    for draft in contracts.MESSAGE_DRAFTS:
        assert wire.parse_preference({"name": "try_again", "candidate": draft}) == (
            "try_again",
            draft,
        )
        _refuses(wire.parse_preference, {"name": "raise_again", "kind": draft})


def test_preferences_take_try_again_for_a_saved_check() -> None:
    assert wire.parse_preference({"name": "try_again", "candidate": "commit_checks"}) == (
        "try_again",
        "commit_checks",
    )
    _refuses(wire.parse_preference, {"name": "raise_again", "kind": "commit_checks"})


def test_preferences_take_try_again_for_inbox_triage() -> None:
    assert wire.parse_preference({"name": "try_again", "candidate": "inbox_triage"}) == (
        "try_again",
        "inbox_triage",
    )
    _refuses(wire.parse_preference, {"name": "raise_again", "kind": "inbox_triage"})


BAD_PREFERENCE: dict[str, object] = {
    "try_again a candidate that is no second look": {"name": "try_again", "candidate": "inbox"},
    "try_again without a candidate": {"name": "try_again"},
    "try_again with a kind": {"name": "try_again", "kind": "flaw"},
    "try_again with an extra key": {
        "name": "try_again",
        "candidate": "long_task_look",
        "kind": "flaw",
    },
    "raise_again a kind that is no finding": {"name": "raise_again", "kind": "other"},
    "raise_again without a kind": {"name": "raise_again"},
    "raise_again with a candidate": {"name": "raise_again", "candidate": "long_task_look"},
    "raise_again null": {"name": "raise_again", "kind": None},
    "not an object": "review_seen",
    "no keys": {},
    "no name": {"candidate": "inbox"},
    "unmute without a candidate": {"name": "unmute"},
    "unmute a topic that is no candidate": {"name": "unmute", "candidate": "unwatched"},
    "unmute null": {"name": "unmute", "candidate": None},
    "unmute with an extra key": {"name": "unmute", "candidate": "inbox", "value": "1"},
    "review_seen with a candidate": {"name": "review_seen", "candidate": "inbox"},
    "reset_learning with a candidate": {"name": "reset_learning", "candidate": "inbox"},
    "an unknown name": {"name": "study"},
    "an unknown name with a value": {"name": "study", "value": "off"},
    "name null": {"name": None},
    "a name in a list": {"name": ["unmute"], "candidate": "inbox"},
}


@refused(BAD_PREFERENCE)
def test_parse_preference_refuses(body: object) -> None:
    _refuses(wire.parse_preference, body)


def test_parse_mission_start() -> None:
    for work in ("prepare", "run"):
        assert wire.parse_mission_start({"item_key": ITEM_KEY, "work": work}) == (ITEM_KEY, work)


MISSION_START: dict[str, Any] = {"item_key": ITEM_KEY, "work": "prepare"}
BAD_MISSION_START: dict[str, object] = {
    "not an object": ITEM_KEY,
    "an extra key": changed(MISSION_START, "session_id", SESSION),
    **{f"no {key}": changed(MISSION_START, key) for key in MISSION_START},
    "work none": changed(MISSION_START, "work", "none"),
    "work coordinate": changed(MISSION_START, "work", "coordinate"),
    "work null": changed(MISSION_START, "work", None),
    "item key in upper case": changed(MISSION_START, "item_key", ITEM_KEY.upper()),
    "item key too long": changed(MISSION_START, "item_key", ITEM_KEY + "0"),
    "item key null": changed(MISSION_START, "item_key", None),
}


@refused(BAD_MISSION_START)
def test_parse_mission_start_refuses(body: object) -> None:
    _refuses(wire.parse_mission_start, body)


def test_parse_inbox_suggest_takes_exactly_an_item_key() -> None:
    assert wire.parse_inbox_suggest({"item_key": ITEM_KEY}) == ITEM_KEY
    for body in (
        ITEM_KEY,
        {},
        {"item_key": ITEM_KEY.upper()},
        {"item_key": ITEM_KEY + "0"},
        {"item_key": None},
        {"item_key": ITEM_KEY, "work": "prepare"},
    ):
        _refuses(wire.parse_inbox_suggest, body)


PROPOSAL: dict[str, Any] = {"name": "news", "question": NEWS}


def test_parse_proposal_takes_a_name_and_a_wording_object() -> None:
    assert wire.parse_proposal(PROPOSAL) == ("news", NEWS)
    # which names and wordings count is `playbook.check_question`'s to judge, not the parser's
    assert wire.parse_proposal({"name": "a" * 32, "question": {}}) == ("a" * 32, {})
    assert wire.parse_proposal({"name": "same_as_muted", "question": {"type": "score"}}) == (
        "same_as_muted",
        {"type": "score"},
    )


BAD_PROPOSAL: dict[str, object] = {
    "not an object": ["news"],
    "null": None,
    "an extra key": changed(PROPOSAL, "label", "New to you?"),
    **{f"no {key}": changed(PROPOSAL, key) for key in PROPOSAL},
    "name empty": changed(PROPOSAL, "name", ""),
    "name of 33 characters": changed(PROPOSAL, "name", "a" * 33),
    "name in upper case": changed(PROPOSAL, "name", "NEWS"),
    "name with a digit": changed(PROPOSAL, "name", "news2"),
    "name with a hyphen": changed(PROPOSAL, "name", "same-as-muted"),
    "name with a trailing newline": changed(PROPOSAL, "name", "news" + chr(0x0A)),
    "name a number": changed(PROPOSAL, "name", 7),
    "name null": changed(PROPOSAL, "name", None),
    "question a list": changed(PROPOSAL, "question", [NEWS]),
    "question a string": changed(PROPOSAL, "question", json.dumps(NEWS)),
    "question null": changed(PROPOSAL, "question", None),
}


@refused(BAD_PROPOSAL)
def test_parse_proposal_refuses(body: object) -> None:
    _refuses(wire.parse_proposal, body)


PROPOSAL_ACT: dict[str, Any] = {"action": "reject", "revision": REVISION, "reason": "Too vague"}


def test_parse_proposal_act() -> None:
    for action in wire.PROPOSAL_ACTIONS:
        body = {**PROPOSAL_ACT, "action": action, "reason": None}
        assert wire.parse_proposal_act(body) == (action, REVISION, None)
    # a reason comes with a rejection alone: one line of 1 to 200 UTF-16 units once stripped
    for reason in (
        "Too vague",
        JAPANESE,
        " padded ",
        "r" * 200,
        "r" * 198 + SEEDLING,
        "r" * 200 + " ",
    ):
        assert wire.parse_proposal_act({**PROPOSAL_ACT, "reason": reason}) == (
            "reject",
            REVISION,
            reason,
        )


BAD_PROPOSAL_ACT: dict[str, object] = {
    "not an object": "reject",
    "an extra key": changed(PROPOSAL_ACT, "note", "Too vague"),
    **{f"no {key}": changed(PROPOSAL_ACT, key) for key in PROPOSAL_ACT},
    "an unknown action": changed(PROPOSAL_ACT, "action", "undo"),
    "action null": changed(PROPOSAL_ACT, "action", None),
    "action in a list": changed(PROPOSAL_ACT, "action", ["reject"]),
    "revision in upper case": changed(PROPOSAL_ACT, "revision", REVISION.upper()),
    "revision of 63 characters": changed(PROPOSAL_ACT, "revision", REVISION[:63]),
    "revision of 65 characters": changed(PROPOSAL_ACT, "revision", REVISION + "0"),
    "revision not hex": changed(PROPOSAL_ACT, "revision", "g" * 64),
    "revision a number": changed(PROPOSAL_ACT, "revision", 7),
    "revision null": changed(PROPOSAL_ACT, "revision", None),
    "a reason with approve": changed(PROPOSAL_ACT, "action", "approve"),
    "a reason empty": changed(PROPOSAL_ACT, "reason", ""),
    "a reason of spaces": changed(PROPOSAL_ACT, "reason", "   "),
    "a reason of 201 units": changed(PROPOSAL_ACT, "reason", "r" * 201),
    "a reason of 201 units with an emoji": changed(PROPOSAL_ACT, "reason", "r" * 199 + SEEDLING),
    "a reason a number": changed(PROPOSAL_ACT, "reason", 7),
    "a reason in a list": changed(PROPOSAL_ACT, "reason", ["Too vague"]),
    **{
        f"a reason with U+{ord(ch):04X}": changed(PROPOSAL_ACT, "reason", f"Too{ch}vague")
        for ch in (*CONTROL, chr(0x2028), chr(0x2029))
    },
    "a reason with a lone surrogate": changed(PROPOSAL_ACT, "reason", "Too" + LONE_SURROGATE),
}


@refused(BAD_PROPOSAL_ACT)
def test_parse_proposal_act_refuses(body: object) -> None:
    _refuses(wire.parse_proposal_act, body)


PRINTABLE = "".join(map(chr, range(0x21, 0x7F)))  # every printable ASCII character but space


@pytest.mark.parametrize("key", ["k", "sk-test-0123", "k" * 4096, PRINTABLE])
def test_parse_key_takes_printable_ascii(key: str) -> None:
    assert wire.parse_key({"key": key}) == key


BAD_KEYS: dict[str, object] = {
    "empty": "",
    "4097 characters": "k" * 4097,
    "a space": "sk test",
    "a trailing newline": "sk-test" + chr(10),
    "a tab": chr(9) + "sk-test",
    "a NUL": "sk" + chr(0) + "test",
    "a delete": "sk-test" + chr(0x7F),
    "a letter beyond ASCII": "sk-" + chr(0xE9),
    "a fullwidth letter": "sk-" + chr(0xFF4B),
    "a number": 12345,
    "null": None,
    "a list": ["sk-test"],
}


@refused({f"key {name}": {"key": value} for name, value in BAD_KEYS.items()})
def test_parse_key_refuses(body: object) -> None:
    _refuses(wire.parse_key, body)


@refused({"not an object": "sk-test", "no key": {}, "an extra key": {"key": "sk", "slot": 1}})
def test_parse_key_refuses_other_bodies(body: object) -> None:
    _refuses(wire.parse_key, body)


@pytest.mark.parametrize("key", ["secret-key-" + chr(10), "secret key", "secret-" + "k" * 4096])
def test_a_refused_key_is_never_echoed(key: str) -> None:
    with pytest.raises(DecisionLoopError) as caught:
        wire.parse_key({"key": key})
    error = caught.value
    assert (str(error), error.args, error.extra) == ("invalid_request", ("invalid_request",), {})
    assert error.__cause__ is None and error.__context__ is None
    assert "secret" not in repr(error)
