"""Routine documents and sealing; creating, revising and reading; lifecycle, readiness, plans."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from viki_agent import protection
from viki_agent.app.settings import SettingsError
from viki_agent.automation import routines
from viki_agent.automation.routines import (
    RevisionPlan,
    RoutineError,
    RoutinePlan,
    RoutineService,
    _uuid,
    decode_revision,
    parse_document,
    parse_schedule,
    schedule_text,
    seal,
    stamp,
)
from viki_agent.profiles.service import ProfileService
from viki_agent.state import AppState, Prefs

PROFILE_ID = "11111111-1111-4111-8111-1111111111ab"
WORKSPACE_ID = "22222222-2222-4222-8222-2222222222ab"
ROUTINE_ID = "33333333-3333-4333-8333-3333333333ab"


def _manual_schedule() -> dict[str, Any]:
    return {
        "kind": "manual",
        "times": [],
        "days": [],
        "timezone": None,
        "start_date": None,
        "end_date": None,
        "catch_up": "skip",
    }


def _daily_schedule(**overrides: Any) -> dict[str, Any]:
    schedule = {
        "kind": "daily",
        "times": ["09:00"],
        "days": [],
        "timezone": "Asia/Tokyo",
        "start_date": None,
        "end_date": None,
        "catch_up": "skip",
    }
    schedule.update(overrides)
    return schedule


def _weekly_schedule(**overrides: Any) -> dict[str, Any]:
    schedule = {
        "kind": "weekly",
        "times": ["09:00"],
        "days": [1],
        "timezone": "Asia/Tokyo",
        "start_date": None,
        "end_date": None,
        "catch_up": "skip",
    }
    schedule.update(overrides)
    return schedule


def _document_raw(**overrides: Any) -> dict[str, Any]:
    raw = {
        "name": "Daily brief",
        "purpose": "",
        "prompt": "Summarize overnight news.",
        "profile_id": PROFILE_ID,
        "workspace_id": WORKSPACE_ID,
        "deadline_minutes": 30,
        "schedule": _daily_schedule(),
    }
    raw.update(overrides)
    return raw


def _code(err: pytest.ExceptionInfo[RoutineError]) -> str:
    return err.value.code


# --- _uuid ---


def test_uuid_accepts_canonical_lowercase() -> None:
    assert _uuid(PROFILE_ID) == PROFILE_ID


@pytest.mark.parametrize(
    "value",
    [PROFILE_ID.upper(), PROFILE_ID.replace("-", ""), "not-a-uuid", 5, None, ""],
)
def test_uuid_refuses_everything_else(value: object) -> None:
    with pytest.raises(RoutineError) as err:
        _uuid(value)
    assert _code(err) == "invalid_id"


# --- stamp / parse_stamp ---


def test_stamp_is_the_store_form() -> None:
    value = datetime(2026, 9, 27, 11, 3, 0, 123000, tzinfo=UTC)
    assert stamp(value) == "2026-09-27T11:03:00.123Z"


def test_parse_stamp_round_trips() -> None:
    value = datetime(2026, 9, 27, 11, 3, 0, 123000, tzinfo=UTC)
    text = stamp(value)
    assert routines.parse_stamp(text) == value
    assert stamp(routines.parse_stamp(text)) == text


@pytest.mark.parametrize(
    "value",
    [
        123,
        "2026-09-27T11:03:00.123",  # 23 chars, no Z
        "2026-09-27T11:03:00.1234Z",  # 25 chars
        "2026-02-30T00:00:00.000Z",  # does not parse: no such day
        "2026-09-27t11:03:00.000Z",  # parses, but does not round-trip
    ],
)
def test_parse_stamp_refuses(value: object) -> None:
    with pytest.raises(ValueError):
        routines.parse_stamp(value)


# --- parse_schedule: shape ---


@pytest.mark.parametrize("raw", [None, [], "manual", {**_manual_schedule(), "extra": 1}])
def test_parse_schedule_refuses_bad_shape(raw: object) -> None:
    with pytest.raises(RoutineError) as err:
        parse_schedule(raw)
    assert _code(err) == "invalid_input"


def test_parse_schedule_refuses_missing_key() -> None:
    raw = _manual_schedule()
    del raw["catch_up"]
    with pytest.raises(RoutineError) as err:
        parse_schedule(raw)
    assert _code(err) == "invalid_input"


@pytest.mark.parametrize("kind", ["monthly", None, 5])
def test_parse_schedule_refuses_bad_kind(kind: object) -> None:
    with pytest.raises(RoutineError) as err:
        parse_schedule({**_manual_schedule(), "kind": kind})
    assert _code(err) == "invalid_schedule"


# --- parse_schedule: manual ---


def test_parse_schedule_accepts_manual() -> None:
    assert parse_schedule(_manual_schedule()) == _manual_schedule()


@pytest.mark.parametrize(
    "overrides",
    [
        {"times": ["09:00"]},
        {"days": [1]},
        {"timezone": "UTC"},
        {"start_date": "2026-01-01"},
        {"end_date": "2026-01-01"},
    ],
)
def test_parse_schedule_manual_refuses_extra_fields(overrides: dict[str, Any]) -> None:
    with pytest.raises(RoutineError) as err:
        parse_schedule({**_manual_schedule(), **overrides})
    assert _code(err) == "invalid_schedule"


# --- parse_schedule: times ---


@pytest.mark.parametrize("times", [["00:00"], ["09:00", "10:00", "11:00", "23:59"]])
def test_parse_schedule_times_edges_are_valid(times: list[str]) -> None:
    assert parse_schedule(_daily_schedule(times=times))["times"] == times


@pytest.mark.parametrize(
    "times",
    [
        [],
        ["09:00", "10:00", "11:00", "12:00", "13:00"],
        ["09:00", "09:00"],
        ["10:00", "09:00"],
        ["9:00"],
        ["24:00"],
        ["23:60"],
        [900],
        ["1٣:00"],  # ARABIC-INDIC DIGIT THREE: not an ASCII digit
        ["０９:００"],  # fullwidth "09:00"
    ],
)
def test_parse_schedule_times_refuses(times: object) -> None:
    with pytest.raises(RoutineError) as err:
        parse_schedule(_daily_schedule(times=times))
    assert _code(err) == "invalid_schedule"


def test_parse_schedule_daily_requires_empty_days() -> None:
    with pytest.raises(RoutineError) as err:
        parse_schedule(_daily_schedule(days=[1]))
    assert _code(err) == "invalid_schedule"


# --- parse_schedule: days ---


@pytest.mark.parametrize("days", [[1], [1, 2, 3, 4, 5, 6, 7]])
def test_parse_schedule_days_edges_are_valid(days: list[int]) -> None:
    assert parse_schedule(_weekly_schedule(days=days))["days"] == days


@pytest.mark.parametrize(
    "days",
    [[], [1, 2, 3, 4, 5, 6, 7, 1], [0], [8], [2, 1], [True], ["1"]],
)
def test_parse_schedule_days_refuses(days: object) -> None:
    with pytest.raises(RoutineError) as err:
        parse_schedule(_weekly_schedule(days=days))
    assert _code(err) == "invalid_schedule"


# --- parse_schedule: timezone ---


def test_parse_schedule_timezone_loads() -> None:
    assert parse_schedule(_daily_schedule(timezone="Asia/Tokyo"))["timezone"] == "Asia/Tokyo"


@pytest.mark.parametrize(
    "timezone",
    [
        "Asia/Tokyo!",  # a key no zone database lists
        "A" * 65,  # a long key no zone database lists
        "A" * 64,  # a long key no zone database lists
        "Nowhere/Imaginary",
        "localtime",  # served only by an OS zone database, which an OS update can change
        "posixrules",
        None,
        5,
        [],
    ],
)
def test_parse_schedule_timezone_refuses(timezone: object) -> None:
    with pytest.raises(RoutineError) as err:
        parse_schedule(_daily_schedule(timezone=timezone))
    assert _code(err) == "invalid_timezone"


# --- parse_schedule: dates and catch_up ---


def test_parse_schedule_accepts_equal_start_and_end() -> None:
    schedule = parse_schedule(_daily_schedule(start_date="2026-01-01", end_date="2026-01-01"))
    assert schedule["start_date"] == schedule["end_date"] == "2026-01-01"


@pytest.mark.parametrize(
    "overrides",
    [
        {"start_date": "2026-02-01", "end_date": "2026-01-01"},  # end before start
        {"start_date": "2026-13-01"},  # not a calendar date
        {"start_date": "20260101"},  # parses, but does not round-trip
        {"start_date": 5},
    ],
)
def test_parse_schedule_dates_refuses(overrides: dict[str, Any]) -> None:
    with pytest.raises(RoutineError) as err:
        parse_schedule(_daily_schedule(**overrides))
    assert _code(err) == "invalid_schedule"


@pytest.mark.parametrize("catch_up", ["latest_within_2h", "banana", 5, None])
def test_parse_schedule_catch_up_refuses_anything_but_skip(catch_up: object) -> None:
    with pytest.raises(RoutineError) as err:
        parse_schedule(_daily_schedule(catch_up=catch_up))
    assert _code(err) == "invalid_schedule"


def test_schedule_text_is_canonical_and_key_order_independent() -> None:
    schedule = _daily_schedule()
    reordered = dict(reversed(list(schedule.items())))
    assert schedule_text(schedule) == schedule_text(reordered)
    assert schedule_text(schedule) == json.dumps(
        schedule, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )


# --- parse_document: shape ---


@pytest.mark.parametrize("raw", [None, [], "doc", {**_document_raw(), "extra": 1}])
def test_parse_document_refuses_bad_shape(raw: object) -> None:
    with pytest.raises(RoutineError) as err:
        parse_document(raw)
    assert _code(err) == "invalid_input"


def test_parse_document_refuses_missing_key() -> None:
    raw = _document_raw()
    del raw["purpose"]
    with pytest.raises(RoutineError) as err:
        parse_document(raw)
    assert _code(err) == "invalid_input"


# --- parse_document: name ---


@pytest.mark.parametrize("name", ["a", "a" * 80])
def test_parse_document_name_edges_are_valid(name: str) -> None:
    document, _ = parse_document(_document_raw(name=name))
    assert document["name"] == name


@pytest.mark.parametrize("name", ["", "a" * 81, "bad\x01name", 5, None])
def test_parse_document_name_refuses(name: object) -> None:
    with pytest.raises(RoutineError) as err:
        parse_document(_document_raw(name=name))
    assert _code(err) == "invalid_input"


def test_parse_document_name_refuses_a_lone_surrogate() -> None:
    with pytest.raises(RoutineError) as err:
        parse_document(_document_raw(name="\ud800"))
    assert _code(err) == "invalid_input"


# --- parse_document: purpose ---


@pytest.mark.parametrize("purpose", ["", pytest.param("a" * 1000, id="1000-chars")])
def test_parse_document_purpose_edges_are_valid(purpose: str) -> None:
    document, _ = parse_document(_document_raw(purpose=purpose))
    assert document["purpose"] == purpose


def test_parse_document_purpose_refuses_past_the_bound() -> None:
    with pytest.raises(RoutineError) as err:
        parse_document(_document_raw(purpose="a" * 1001))
    assert _code(err) == "invalid_input"


def test_parse_document_purpose_refuses_a_lone_surrogate() -> None:
    with pytest.raises(RoutineError) as err:
        parse_document(_document_raw(purpose="\ud800"))
    assert _code(err) == "invalid_input"


# --- parse_document: prompt ---


@pytest.mark.parametrize("prompt", ["a", pytest.param("a" * 32768, id="max-bytes")])
def test_parse_document_prompt_edges_are_valid(prompt: str) -> None:
    document, _ = parse_document(_document_raw(prompt=prompt))
    assert document["prompt"] == prompt


@pytest.mark.parametrize(
    "prompt",
    [
        "",
        pytest.param("a" * 32769, id="one-past-max"),
        "bad\x00prompt",
        "surrogate \udcff",
        5,
        None,
    ],
)
def test_parse_document_prompt_refuses(prompt: object) -> None:
    with pytest.raises(RoutineError) as err:
        parse_document(_document_raw(prompt=prompt))
    assert _code(err) == "invalid_input"


# --- parse_document: ids ---


@pytest.mark.parametrize("field", ["profile_id", "workspace_id"])
def test_parse_document_ids_refuse_bad_uuid(field: str) -> None:
    with pytest.raises(RoutineError) as err:
        parse_document(_document_raw(**{field: "not-a-uuid"}))
    assert _code(err) == "invalid_id"


# --- parse_document: deadline_minutes ---


@pytest.mark.parametrize("value", [5, 240])
def test_parse_document_deadline_minutes_edges_are_valid(value: int) -> None:
    document, _ = parse_document(_document_raw(deadline_minutes=value))
    assert document["deadline_minutes"] == value


@pytest.mark.parametrize("value", [4, 241, True, 30.0, "30", None])
def test_parse_document_deadline_minutes_refuses(value: object) -> None:
    with pytest.raises(RoutineError) as err:
        parse_document(_document_raw(deadline_minutes=value))
    assert _code(err) == "invalid_input"


# --- parse_document: nested schedule and canonical bytes ---


def test_parse_document_propagates_schedule_errors() -> None:
    with pytest.raises(RoutineError) as err:
        parse_document(_document_raw(schedule=_daily_schedule(catch_up="latest_within_2h")))
    assert _code(err) == "invalid_schedule"


def test_parse_document_canonical_bytes_are_key_order_independent() -> None:
    raw = _document_raw()
    reordered = dict(reversed(list(raw.items())))
    reordered["schedule"] = dict(reversed(list(raw["schedule"].items())))
    document_a, canonical_a = parse_document(raw)
    document_b, canonical_b = parse_document(reordered)
    assert document_a == document_b
    assert canonical_a == canonical_b
    assert canonical_a == json.dumps(
        document_a, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    assert len(canonical_a) <= routines.MAX_BODY_BYTES


def test_parse_document_pins_the_body_byte_bound() -> None:
    # The canonical body escapes every control character, so a prompt of "\n" characters (each 2
    # canonical bytes) lets the test walk MAX_BODY_BYTES one byte at a time; one plain character
    # pads out an odd remainder.
    base_length = len(
        json.dumps(
            _document_raw(prompt=""), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
    )
    remaining = routines.MAX_BODY_BYTES - base_length
    prompt = "\n" * (remaining // 2) + "x" * (remaining % 2)

    _, canonical = parse_document(_document_raw(prompt=prompt))
    assert len(canonical) == routines.MAX_BODY_BYTES

    with pytest.raises(RoutineError) as err:
        parse_document(_document_raw(prompt=prompt + "x"))
    assert _code(err) == "invalid_input"


# --- parse_document: run_only_if, on_confirmation ---

_CARD = {"kind": "card", "seat": "research"}


@pytest.mark.parametrize("seat", ["research", "a", "a" * 32, "a-1"])
def test_parse_document_accepts_the_two_optional_settings(seat: str) -> None:
    run_only_if = {"seat": seat, "kind": "card"}
    document, canonical = parse_document(
        _document_raw(run_only_if=run_only_if, on_confirmation="stop")
    )
    assert document == {
        **parse_document(_document_raw())[0],
        "run_only_if": {"kind": "card", "seat": seat},
        "on_confirmation": "stop",
    }
    assert document["run_only_if"] is not run_only_if
    assert (
        f'"on_confirmation":"stop","profile_id":"{PROFILE_ID}",'
        '"prompt":"Summarize overnight news.","purpose":"",'
        f'"run_only_if":{{"kind":"card","seat":"{seat}"}},"schedule":'
    ).encode() in canonical


@pytest.mark.parametrize(
    "overrides",
    [
        {"run_only_if": None},
        {"on_confirmation": "wait"},
        {"run_only_if": None, "on_confirmation": "wait"},
    ],
)
def test_parse_document_omits_default_settings(overrides: dict[str, Any]) -> None:
    assert parse_document(_document_raw(**overrides)) == parse_document(_document_raw())


@pytest.mark.parametrize(
    "overrides",
    [
        {"run_only_if": "card"},
        {"run_only_if": {"kind": "inbox", "seat": "research"}},
        {"run_only_if": {"kind": "card"}},
        {"run_only_if": {**_CARD, "extra": 1}},
        *(
            {"run_only_if": {"kind": "card", "seat": seat}}
            for seat in ["Research", "-a", "", "a" * 33, "a_b", "a/b", 5]
        ),
        {"on_confirmation": None},
        {"on_confirmation": True},
        {"on_confirmation": "Stop"},
    ],
)
def test_parse_document_refuses_bad_settings(overrides: dict[str, Any]) -> None:
    with pytest.raises(RoutineError) as err:
        parse_document(_document_raw(**overrides))
    assert _code(err) == "invalid_input"


def test_parse_document_refuses_a_card_condition_on_a_manual_schedule() -> None:
    with pytest.raises(RoutineError) as err:
        parse_document(_document_raw(schedule=_manual_schedule(), run_only_if=_CARD))
    assert _code(err) == "invalid_input"
    document, _ = parse_document(_document_raw(schedule=_manual_schedule(), run_only_if=None))
    assert "run_only_if" not in document


_CHANGED = {"kind": "changed", "folder": "00-inbox"}


@pytest.mark.parametrize("folder", ["00-inbox", "10-projects/site", "Café notes", "a" * 255])
def test_parse_document_accepts_a_folder_condition(folder: str) -> None:
    document, canonical = parse_document(
        _document_raw(run_only_if={"folder": folder, "kind": "changed"})
    )
    assert document == {
        **parse_document(_document_raw())[0],
        "run_only_if": {"kind": "changed", "folder": folder},
    }
    assert f'"run_only_if":{{"folder":"{folder}","kind":"changed"}}'.encode() in canonical


@pytest.mark.parametrize(
    "run_only_if",
    [
        {"kind": "changed"},
        {"kind": "changed", "folder": "00-inbox", "seat": "research"},
        {"kind": "card", "folder": "00-inbox"},
        {"kind": "Changed", "folder": "00-inbox"},
        *(
            {"kind": "changed", "folder": folder}
            for folder in [
                "",
                "/00-inbox",
                "00-inbox/",
                "a//b",
                ".",
                "a/./b",
                "a/../b",
                ".claude",
                "a/.hidden",
                "a\\b",
                "c:notes",
                "a\x01b",
                "a\x7fb",
                "a" * 256,
                "\ud800",
                5,
                None,
            ]
        ),
    ],
)
def test_parse_document_refuses_a_bad_folder_condition(run_only_if: object) -> None:
    with pytest.raises(RoutineError) as err:
        parse_document(_document_raw(run_only_if=run_only_if))
    assert _code(err) == "invalid_input"


def test_parse_document_refuses_a_folder_condition_on_a_manual_schedule() -> None:
    with pytest.raises(RoutineError) as err:
        parse_document(_document_raw(schedule=_manual_schedule(), run_only_if=_CHANGED))
    assert _code(err) == "invalid_input"


# --- seal / decode_revision ---


def _row_for(
    routine_id: str, revision: int, canonical: bytes, schedule: dict[str, Any]
) -> dict[str, Any]:
    return {
        "format": 1,
        "routine_id": routine_id,
        "revision": revision,
        "body": seal(routine_id, revision, canonical),
        "body_bytes": len(canonical),
        "body_sha256": hashlib.sha256(canonical).hexdigest(),
        "schedule": schedule_text(schedule),
    }


def test_seal_and_decode_revision_round_trip() -> None:
    document, canonical = parse_document(_document_raw())
    row = _row_for(ROUTINE_ID, 1, canonical, document["schedule"])
    assert decode_revision(row) == document


def test_seal_maps_protection_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(*_args: Any, **_kwargs: Any) -> bytes:
        raise protection.ProtectionError()

    monkeypatch.setattr(protection, "protect", _raise)
    with pytest.raises(RoutineError) as err:
        seal(ROUTINE_ID, 1, b"{}")
    assert _code(err) == "protection"


@pytest.mark.parametrize("size", [0, 81921])
def test_seal_maps_capacity(monkeypatch: pytest.MonkeyPatch, size: int) -> None:
    monkeypatch.setattr(protection, "protect", lambda *_a, **_k: b"x" * size)
    with pytest.raises(RoutineError) as err:
        seal(ROUTINE_ID, 1, b"{}")
    assert _code(err) == "capacity"


def test_decode_revision_refuses_newer_format() -> None:
    document, canonical = parse_document(_document_raw())
    row = _row_for(ROUTINE_ID, 1, canonical, document["schedule"])
    row["format"] = 2
    with pytest.raises(RoutineError) as err:
        decode_revision(row)
    assert _code(err) == "newer_version"


def test_decode_revision_refuses_a_changed_schedule_column() -> None:
    document, canonical = parse_document(_document_raw())
    row = _row_for(ROUTINE_ID, 1, canonical, document["schedule"])
    row["schedule"] = schedule_text(_weekly_schedule())
    with pytest.raises(RoutineError) as err:
        decode_revision(row)
    assert _code(err) == "corrupt"


@pytest.mark.parametrize("field", ["body_bytes", "body_sha256"])
def test_decode_revision_refuses_a_tampered_digest(field: str) -> None:
    document, canonical = parse_document(_document_raw())
    row = _row_for(ROUTINE_ID, 1, canonical, document["schedule"])
    row[field] = row[field] + 1 if field == "body_bytes" else "0" * 64
    with pytest.raises(RoutineError) as err:
        decode_revision(row)
    assert _code(err) == "corrupt"


def test_decode_revision_refuses_another_routines_context() -> None:
    document, canonical = parse_document(_document_raw())
    other_routine_id = "44444444-4444-4444-8444-444444444444"
    row = _row_for(other_routine_id, 1, canonical, document["schedule"])
    row["routine_id"] = ROUTINE_ID  # decode reads the context from this routine, not the sealer's
    with pytest.raises(RoutineError) as err:
        decode_revision(row)
    assert _code(err) in ("protection", "corrupt")


def test_decode_revision_refuses_missing_keys() -> None:
    with pytest.raises(RoutineError) as err:
        decode_revision({"format": 1})
    assert _code(err) == "corrupt"


def test_decode_revision_refuses_a_stored_explicit_default() -> None:
    document, canonical = parse_document(_document_raw())
    explicit = json.dumps(
        {**document, "on_confirmation": "wait"},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    row = _row_for(ROUTINE_ID, 1, explicit, document["schedule"])
    with pytest.raises(RoutineError) as err:
        decode_revision(row)
    assert _code(err) == "corrupt"


# --- RoutineService: create, revise, get, list ---

NOW = datetime(2026, 9, 27, 9, 0, tzinfo=UTC)


def _ident() -> str:
    return str(uuid.uuid4())


@pytest.fixture
def service(tmp_path: Path) -> RoutineService:
    store = AppState(tmp_path / "state.json", Prefs()).store
    return RoutineService(store, ProfileService(store), None, None, now=lambda: NOW)


def _set_state(service: RoutineService, routine_id: str, state: str) -> None:
    enabled_since = stamp(NOW) if state == "enabled" else None
    with service.store._connection(write=True) as connection:
        connection.execute(
            "UPDATE routines SET state=?,enabled_since=? WHERE routine_id=?",
            (state, enabled_since, routine_id),
        )


def test_create_replays_its_receipt(service: RoutineService) -> None:
    command_id = _ident()
    first, created = service.create(command_id, _document_raw())
    assert created is True
    assert first == {
        "routine_id": first["routine_id"],
        "revision": 1,
        "state": "draft",
        "state_revision": 1,
    }
    assert service.create(command_id, _document_raw()) == (first, False)
    revised, _ = service.revise(first["routine_id"], (revise_id := _ident()), 1, _document_raw())
    assert service.revise(first["routine_id"], revise_id, 1, _document_raw()) == (revised, False)


def test_a_reused_command_id_with_another_request_is_command_conflict(
    service: RoutineService,
) -> None:
    command_id = _ident()
    result, _ = service.create(command_id, _document_raw())
    for call in (
        lambda: service.create(command_id, _document_raw(name="Other")),
        lambda: service.revise(result["routine_id"], command_id, 1, _document_raw()),
    ):
        with pytest.raises(RoutineError) as err:
            call()
        assert _code(err) == "command_conflict"


def test_revise_refuses_a_stale_revision_a_missing_routine_and_bad_ids(
    service: RoutineService,
) -> None:
    routine_id = service.create(_ident(), _document_raw())[0]["routine_id"]
    service.revise(routine_id, _ident(), 1, _document_raw())
    cases = [
        (routine_id, _ident(), 1, "conflict"),
        (_ident(), _ident(), 1, "not_found"),
        ("not-a-uuid", _ident(), 1, "invalid_id"),
        (routine_id, "not-a-uuid", 1, "invalid_id"),
        (routine_id, _ident(), 0, "invalid_revision"),
        (routine_id, _ident(), True, "invalid_revision"),
    ]
    for target, command_id, expected, code in cases:
        with pytest.raises(RoutineError) as err:
            service.revise(target, command_id, expected, _document_raw())
        assert _code(err) == code


def _rows(service: RoutineService) -> tuple[int, ...]:
    with service.store._connection() as connection:
        return tuple(
            connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("routines", "routine_revisions", "routine_commands")
        )


def test_create_and_revise_without_a_document_are_invalid_input(service: RoutineService) -> None:
    routine_id = service.create(_ident(), _document_raw())[0]["routine_id"]
    before = _rows(service)
    for call in (
        lambda: service.create(_ident(), None),
        lambda: service.revise(routine_id, _ident(), 1, None),
    ):
        with pytest.raises(RoutineError) as err:
            call()
        assert _code(err) == "invalid_input"
    assert _rows(service) == before


def test_revise_resets_tested_keeps_other_states_and_refuses_archived(
    service: RoutineService,
) -> None:
    routine_id = service.create(_ident(), _document_raw())[0]["routine_id"]
    _set_state(service, routine_id, "tested")
    result, _ = service.revise(routine_id, _ident(), 1, _document_raw())
    assert (result["state"], result["state_revision"]) == ("draft", 2)
    _set_state(service, routine_id, "paused")
    result, _ = service.revise(routine_id, _ident(), 2, _document_raw())
    assert (result["state"], result["state_revision"]) == ("paused", 2)
    _set_state(service, routine_id, "archived")
    with pytest.raises(RoutineError) as err:
        service.revise(routine_id, _ident(), 3, _document_raw())
    assert _code(err) == "invalid_transition"


def test_the_65th_routine_is_capacity(service: RoutineService) -> None:
    for _ in range(routines.MAX_ROUTINES):
        service.create(_ident(), _document_raw())
    with pytest.raises(RoutineError) as err:
        service.create(_ident(), _document_raw())
    assert _code(err) == "capacity"
    assert len(service.list()) == routines.MAX_ROUTINES


def test_the_257th_revision_is_capacity(service: RoutineService) -> None:
    routine_id = service.create(_ident(), _document_raw())[0]["routine_id"]
    for revision in range(1, routines.MAX_REVISIONS):
        service.revise(routine_id, _ident(), revision, _document_raw())
    with pytest.raises(RoutineError) as err:
        service.revise(routine_id, _ident(), routines.MAX_REVISIONS, _document_raw())
    assert _code(err) == "capacity"


def test_the_receipt_cap_is_capacity_but_replays_still_answer(
    service: RoutineService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(routines, "MAX_RECEIPTS", 1)
    command_id = _ident()
    first = service.create(command_id, _document_raw())[0]
    with pytest.raises(RoutineError) as err:
        service.create(_ident(), _document_raw())
    assert _code(err) == "capacity"
    assert service.create(command_id, _document_raw()) == (first, False)


def test_activation_strictly_increases_under_a_frozen_clock(service: RoutineService) -> None:
    routine_id = service.create(_ident(), _document_raw())[0]["routine_id"]
    service.revise(routine_id, _ident(), 1, _document_raw())
    service.revise(routine_id, _ident(), 2, _document_raw())
    with service.store._connection() as connection:
        activations = [
            row[0]
            for row in connection.execute(
                "SELECT activates_at FROM routine_revisions WHERE routine_id=? ORDER BY revision",
                (routine_id,),
            )
        ]
    assert activations == [stamp(NOW + timedelta(milliseconds=n)) for n in range(3)]
    later = NOW + timedelta(hours=1)
    service.now = lambda: later
    service.revise(routine_id, _ident(), 3, _document_raw())
    assert service.get(routine_id)["activates_at"] == stamp(later)


def test_get_and_list_shapes(service: RoutineService) -> None:
    raw = _document_raw(schedule=_weekly_schedule())
    routine_id = service.create(_ident(), raw)[0]["routine_id"]
    got = service.get(routine_id)
    assert got == {
        "routine_id": routine_id,
        "current_revision": 1,
        "state": "draft",
        "state_revision": 1,
        "tested_revision": None,
        "enabled_since": None,
        "last_slot": None,
        "created_at": stamp(NOW),
        "updated_at": stamp(NOW),
        "revision": 1,
        "activates_at": stamp(NOW),
        "document": parse_document(raw)[0],
    }
    manual_id = service.create(_ident(), _document_raw(schedule=_manual_schedule()))[0][
        "routine_id"
    ]
    assert sorted(service.list(), key=lambda item: item["routine_id"] != routine_id) == [
        {
            "routine_id": routine_id,
            "name": "Daily brief",
            "state": "draft",
            "current_revision": 1,
            "state_revision": 1,
            "schedule": {"kind": "weekly", "timezone": "Asia/Tokyo"},
            "enabled_since": None,
            "last_slot": None,
        },
        {
            "routine_id": manual_id,
            "name": "Daily brief",
            "state": "draft",
            "current_revision": 1,
            "state_revision": 1,
            "schedule": {"kind": "manual", "timezone": None},
            "enabled_since": None,
            "last_slot": None,
        },
    ]
    assert [item["routine_id"] for item in service.list()] == sorted([routine_id, manual_id])
    for value in (_ident(), "not-a-uuid"):
        with pytest.raises(RoutineError) as err:
            service.get(value)
        assert _code(err) == ("not_found" if value != "not-a-uuid" else "invalid_id")


def test_settings_round_trip_through_seal_get_list_and_frozen(service: RoutineService) -> None:
    raw = _document_raw(run_only_if=_CARD, on_confirmation="stop")
    routine_id = service.create(_ident(), raw)[0]["routine_id"]
    document = service.get(routine_id)["document"]
    assert document["run_only_if"] == _CARD
    assert document["on_confirmation"] == "stop"
    assert service.frozen(routine_id, 1) == document == parse_document(raw)[0]
    assert [item["routine_id"] for item in service.list()] == [routine_id]
    service.revise(routine_id, _ident(), 1, _document_raw(run_only_if=None))
    assert service.frozen(routine_id, 2) == parse_document(_document_raw())[0]
    assert service.frozen(routine_id, 1) == document
    service.revise(routine_id, _ident(), 2, _document_raw(run_only_if=_CHANGED))
    assert service.frozen(routine_id, 3)["run_only_if"] == _CHANGED
    assert service.get(routine_id)["document"]["run_only_if"] == _CHANGED


def test_the_store_holds_no_plaintext_body(service: RoutineService, tmp_path: Path) -> None:
    markers = {
        "name": "naïve-name-marker-5e1c",
        "purpose": "naïve-purpose-marker-8b2d",
        "prompt": "naïve-prompt-marker-a7f0",
    }
    routine_id = service.create(_ident(), _document_raw(**markers))[0]["routine_id"]
    service.revise(routine_id, _ident(), 1, _document_raw(**markers))
    files = [path for path in tmp_path.iterdir() if path.is_file()]
    assert service.store.path in files
    for path in files:
        data = path.read_bytes()
        for marker in markers.values():
            escaped = json.dumps(marker, ensure_ascii=True)[1:-1].encode("ascii")
            assert marker.encode("utf-8") not in data
            assert escaped not in data


# --- RoutineService: lifecycle, readiness, the scheduler's read view ---


@pytest.fixture
def ready(service: RoutineService, monkeypatch: pytest.MonkeyPatch) -> RoutineService:
    """The lifecycle with readiness passing; readiness has its own tests below."""
    monkeypatch.setattr(service, "readiness", lambda document, *, scheduled: [])
    return service


def _state_revision(service: RoutineService, routine_id: str) -> int:
    return service.get(routine_id)["state_revision"]


def _tested(service: RoutineService, routine_id: str, revision: int) -> None:
    with service.store._connection(write=True) as connection:
        routines.record_tested(connection, routine_id, revision, stamp(NOW))


def _reach(service: RoutineService, state: str) -> str:
    """A routine in `state`, reached only through the service and `record_tested`."""
    routine_id = service.create(_ident(), _document_raw())[0]["routine_id"]
    path = {
        "draft": [],
        "tested": ["tested"],
        "enabled": ["tested", "enable"],
        "paused": ["tested", "enable", "pause"],
        "archived": ["archive"],
    }[state]
    for step in path:
        if step == "tested":
            _tested(service, routine_id, 1)
        else:
            getattr(service, step)(routine_id, _ident(), _state_revision(service, routine_id))
    assert service.get(routine_id)["state"] == state
    return routine_id


_CELLS = {
    "draft": {"revise": "draft", "enable": None, "pause": None, "archive": "archived"},
    "tested": {"revise": "draft", "enable": "enabled", "pause": None, "archive": "archived"},
    "enabled": {"revise": "enabled", "enable": None, "pause": "paused", "archive": "archived"},
    "paused": {"revise": "paused", "enable": "enabled", "pause": None, "archive": "archived"},
    "archived": {"revise": None, "enable": None, "pause": None, "archive": None},
}


@pytest.mark.parametrize(
    ("start", "operation"),
    [(start, operation) for start, row in _CELLS.items() for operation in row],
)
def test_every_lifecycle_cell(ready: RoutineService, start: str, operation: str) -> None:
    routine_id = _reach(ready, start)
    before = ready.get(routine_id)
    expected = _CELLS[start][operation]
    if operation == "revise":
        call = lambda: ready.revise(routine_id, _ident(), 1, _document_raw())  # noqa: E731
    else:
        method = getattr(ready, operation)
        call = lambda: method(routine_id, _ident(), before["state_revision"])  # noqa: E731
    if expected is None:
        with pytest.raises(RoutineError) as err:
            call()
        assert _code(err) == "invalid_transition"
        assert ready.get(routine_id) == before
        return
    result, created = call()
    after = ready.get(routine_id)
    assert created is True
    assert result == {
        "routine_id": routine_id,
        "revision": after["current_revision"],
        "state": expected,
        "state_revision": after["state_revision"],
    }
    assert after["state"] == expected
    bumped = operation != "revise" or expected != start
    assert after["state_revision"] == before["state_revision"] + bumped
    if expected == "enabled":
        assert after["enabled_since"] == stamp(NOW)
    elif operation in ("pause", "archive"):
        assert after["enabled_since"] is None
    else:
        assert after["enabled_since"] == before["enabled_since"]


def test_state_operations_refuse_stale_missing_and_bad_input_and_replay(
    ready: RoutineService,
) -> None:
    routine_id = _reach(ready, "tested")
    cases = [
        (routine_id, _ident(), 1, "conflict"),
        (_ident(), _ident(), 2, "not_found"),
        (routine_id, "not-a-uuid", 2, "invalid_id"),
        (routine_id, _ident(), 0, "invalid_revision"),
        (routine_id, _ident(), True, "invalid_revision"),
    ]
    for target, command_id, expected, code in cases:
        with pytest.raises(RoutineError) as err:
            ready.enable(target, command_id, expected)
        assert _code(err) == code
    command_id = _ident()
    first = ready.enable(routine_id, command_id, 2)[0]
    assert ready.enable(routine_id, command_id, 2) == (first, False)
    for call in (
        lambda: ready.enable(routine_id, command_id, 3),
        lambda: ready.pause(routine_id, command_id, 2),
    ):
        with pytest.raises(RoutineError) as err:
            call()
        assert _code(err) == "command_conflict"


def test_a_missing_revision_is_invalid_revision(ready: RoutineService) -> None:
    routine_id = _reach(ready, "enabled")
    before = _rows(ready)
    missing: Any = None
    for call in (
        lambda: ready.revise(routine_id, _ident(), missing, _document_raw()),
        lambda: ready.enable(routine_id, _ident(), missing),
        lambda: ready.pause(routine_id, _ident(), missing),
        lambda: ready.archive(routine_id, _ident(), missing),
    ):
        with pytest.raises(RoutineError) as err:
            call()
        assert _code(err) == "invalid_revision"
    assert _rows(ready) == before


def test_an_enable_replay_answers_its_receipt_whatever_readiness_says_now(
    ready: RoutineService, monkeypatch: pytest.MonkeyPatch
) -> None:
    routine_id = _reach(ready, "tested")
    command_id = _ident()
    first, _ = ready.enable(routine_id, command_id, 2)
    monkeypatch.setattr(ready, "readiness", lambda document, *, scheduled: ["profile_missing"])
    assert ready.enable(routine_id, command_id, 2) == (first, False)


def test_a_create_replay_answers_its_recorded_state(service: RoutineService) -> None:
    command_id = _ident()
    routine_id = service.create(command_id, _document_raw())[0]["routine_id"]
    _tested(service, routine_id, 1)
    assert service.create(command_id, _document_raw()) == (
        {"routine_id": routine_id, "revision": 1, "state": "draft", "state_revision": 1},
        False,
    )


def test_mutation_replays_keep_all_recorded_results_after_restart(
    ready: RoutineService, tmp_path: Path
) -> None:
    raw = _document_raw()
    create_id, revise_id, enable_id, pause_id, archive_id = [_ident() for _ in range(5)]
    created = ready.create(create_id, raw)[0]
    routine_id = created["routine_id"]
    _tested(ready, routine_id, 1)
    revised = ready.revise(routine_id, revise_id, 1, raw)[0]
    _tested(ready, routine_id, 2)
    enabled = ready.enable(routine_id, enable_id, 4)[0]
    paused = ready.pause(routine_id, pause_id, 5)[0]
    archived = ready.archive(routine_id, archive_id, 6)[0]
    store = AppState(tmp_path / "state.json", Prefs()).store
    restarted = RoutineService(store, ProfileService(store), None, None)
    before = _rows(restarted)
    assert restarted.create(create_id, raw) == (created, False)
    assert restarted.revise(routine_id, revise_id, 1, raw) == (revised, False)
    assert restarted.enable(routine_id, enable_id, 4) == (enabled, False)
    assert restarted.pause(routine_id, pause_id, 5) == (paused, False)
    assert restarted.archive(routine_id, archive_id, 6) == (archived, False)
    assert _rows(restarted) == before


@pytest.mark.parametrize("operation", ["create", "revise"])
def test_committed_definition_replay_precedes_sealing_after_restart(
    service: RoutineService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    command_id = _ident()
    routine_id = service.create(_ident(), _document_raw())[0]["routine_id"]

    def call(target: RoutineService, command: str, raw: object) -> tuple[dict[str, Any], bool]:
        if operation == "create":
            return target.create(command, raw)
        return target.revise(routine_id, command, 1, raw)

    first = call(service, command_id, _document_raw())[0]
    store = AppState(tmp_path / "state.json", Prefs()).store
    restarted = RoutineService(store, ProfileService(store), None, None)
    before = _rows(restarted)

    def fail_seal(*args: Any) -> bytes:
        raise RoutineError("protection")

    monkeypatch.setattr(routines, "seal", fail_seal)
    assert call(restarted, command_id, _document_raw()) == (first, False)
    for command, raw, code in (
        (command_id, _document_raw(name="Changed"), "command_conflict"),
        (command_id, None, "invalid_input"),
        (_ident(), _document_raw(), "protection"),
    ):
        with pytest.raises(RoutineError) as err:
            call(restarted, command, raw)
        assert _code(err) == code
    assert _rows(restarted) == before


def test_receipt_operation_must_match_even_with_the_same_hash(service: RoutineService) -> None:
    command_id = _ident()
    service.create(command_id, _document_raw())
    with service.store._connection(write=True) as connection:
        connection.execute(
            "UPDATE routine_commands SET operation='restore' WHERE command_id=?", (command_id,)
        )
    with pytest.raises(RoutineError) as err:
        service.create(command_id, _document_raw())
    assert _code(err) == "command_conflict"


@pytest.mark.parametrize("operation", ["create", "revise"])
def test_definition_rechecks_a_winner_committed_during_sealing_before_caps_or_cas(
    service: RoutineService, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    routine_id = service.create(_ident(), _document_raw())[0]["routine_id"]
    command_id = _ident()
    winner = []

    def call() -> tuple[dict[str, Any], bool]:
        if operation == "create":
            return service.create(command_id, _document_raw())
        return service.revise(routine_id, command_id, 1, _document_raw())

    def commit_winner(target: str, revision: int, canonical: bytes) -> bytes:
        monkeypatch.setattr(routines, "seal", seal)
        winner.append(call()[0])
        monkeypatch.setattr(routines, "MAX_RECEIPTS", _rows(service)[2])
        return seal(target, revision, canonical)

    monkeypatch.setattr(routines, "seal", commit_winner)
    result, created = call()
    assert (result, created) == (winner[0], False)
    assert _rows(service) == (2 if operation == "create" else 1, 2, 2)


def test_enable_rechecks_a_winner_committed_during_readiness_before_cas(
    ready: RoutineService, monkeypatch: pytest.MonkeyPatch
) -> None:
    routine_id = _reach(ready, "tested")
    command_id = _ident()
    winner = []

    def commit_winner(document: dict[str, Any], *, scheduled: bool) -> list[str]:
        monkeypatch.setattr(ready, "readiness", lambda document, *, scheduled: [])
        winner.append(ready.enable(routine_id, command_id, 2)[0])
        return []

    monkeypatch.setattr(ready, "readiness", commit_winner)
    assert ready.enable(routine_id, command_id, 2) == (winner[0], False)


def test_enable_is_conflict_when_a_revise_lands_during_its_readiness_check(
    ready: RoutineService, monkeypatch: pytest.MonkeyPatch
) -> None:
    routine_id = _reach(ready, "paused")
    state_revision = _state_revision(ready, routine_id)

    def revise_first(document: dict[str, Any], *, scheduled: bool) -> list[str]:
        ready.revise(routine_id, _ident(), 1, _document_raw(name="Revised"))
        return []

    monkeypatch.setattr(ready, "readiness", revise_first)
    with pytest.raises(RoutineError) as err:
        ready.enable(routine_id, _ident(), state_revision)
    assert _code(err) == "conflict"
    got = ready.get(routine_id)
    assert (got["state"], got["current_revision"], got["state_revision"]) == (
        "paused",
        2,
        state_revision,
    )


def test_readiness_without_services_reports_what_it_can(service: RoutineService) -> None:
    document = parse_document(_document_raw())[0]
    assert service.readiness(document, scheduled=False) == ["profile_missing", "workspace_missing"]
    assert service.readiness(document, scheduled=True) == [
        "profile_missing",
        "workspace_missing",
        "settings_unavailable",
    ]


def test_readiness_maps_the_profile(
    service: RoutineService, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.profiles.test_profile_document import document as profile_document
    from viki_agent.profiles.service import ProfileError

    profile = {"archived": False, "document": profile_document()}
    profile["document"]["runtime"].update(provider="codex", permission_mode="plan")
    monkeypatch.setattr(service.profiles, "get", lambda profile_id: profile)
    document = parse_document(_document_raw())[0]
    assert service.readiness(document, scheduled=True) == [
        "provider_unsupported",
        "workspace_missing",
    ]

    def unreadable(profile_id: str) -> dict[str, Any]:
        raise ProfileError("corrupt")

    monkeypatch.setattr(service.profiles, "get", unreadable)
    with pytest.raises(RoutineError) as err:
        service.readiness(document, scheduled=False)
    assert _code(err) == "unavailable"


def test_readiness_maps_an_unknown_workspace_and_unusable_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.profiles.test_profile_launch_api import Rig

    rig = Rig(tmp_path)
    service = RoutineService(
        rig.state.store, ProfileService(rig.state.store), rig.settings, rig.workspaces
    )
    document = parse_document(_document_raw(workspace_id=_ident()))[0]
    assert service.readiness(document, scheduled=True) == ["profile_missing", "workspace_missing"]
    unusable = ["profile_missing", "workspace_missing", "settings_unavailable"]
    with rig.state.store._connection(write=True) as connection:
        connection.execute(
            "INSERT INTO settings (scope,version,revision,document,updated_at) "
            "VALUES ('app',2,1,'{}',?)",
            (stamp(NOW),),
        )
    assert rig.settings.read().document.read_only is True
    assert service.readiness(document, scheduled=True) == unusable

    def unreadable() -> Any:
        raise SettingsError("corrupt")

    monkeypatch.setattr(rig.settings, "read", unreadable)
    assert service.readiness(document, scheduled=True) == unusable


def test_enable_is_gated_by_the_unattended_blockers(tmp_path: Path) -> None:
    from tests.profiles.test_profile_document import document as profile_document
    from tests.profiles.test_profile_launch_api import Rig

    rig = Rig(tmp_path)
    service = RoutineService(
        rig.state.store, ProfileService(rig.state.store), rig.settings, rig.workspaces
    )
    service.now = lambda: NOW
    profiles = service.profiles
    raw = _document_raw(profile_id=rig.profile_id, workspace_id=rig.workspace_id)
    routine_id = service.create(_ident(), raw)[0]["routine_id"]
    _tested(service, routine_id, 1)
    document = service.get(routine_id)["document"]

    def enable() -> list[str]:
        with pytest.raises(RoutineError) as err:
            service.enable(routine_id, _ident(), 2)
        assert _code(err) == "not_ready"
        return list(err.value.blockers)

    settings = rig.settings.patch(_ident(), 0, {"chat.default_mode": "auto"})[0]
    assert enable() == ["unattended_policy"]
    settings = rig.settings.patch(
        _ident(), settings.document.revision, {"chat.default_mode": "acceptEdits"}
    )[0]
    assert enable() == ["unattended_policy", "unattended_mode"]
    rig.settings.patch(_ident(), settings.document.revision, {"chat.default_mode": "default"})
    edits = profile_document()
    edits["runtime"]["permission_mode"] = "acceptEdits"
    profiles.revise(rig.profile_id, _ident(), 1, edits)
    profiles.archive(rig.profile_id, _ident(), 1, True)
    assert enable() == ["profile_archived", "unattended_policy", "unattended_mode"]
    assert service.readiness(document, scheduled=False) == ["profile_archived"]
    assert service.get(routine_id)["state"] == "tested"

    profiles.archive(rig.profile_id, _ident(), 2, False)
    profiles.revise(rig.profile_id, _ident(), 2, profile_document())
    workspace = rig.workspaces.get(rig.workspace_id)
    required = rig.workspaces.inspect_policy(rig.workspace_id).acknowledgements_required
    rig.workspaces.set_policy(
        _ident(), rig.workspace_id, workspace.policy_revision, "ordinary", required
    )
    assert service.readiness(document, scheduled=True) == []
    result, created = service.enable(routine_id, _ident(), 2)
    assert (result["state"], created) == ("enabled", True)


def test_record_tested_marks_only_a_current_draft(service: RoutineService) -> None:
    routine_id = service.create(_ident(), _document_raw())[0]["routine_id"]
    service.revise(routine_id, _ident(), 1, _document_raw())
    _tested(service, routine_id, 1)
    got = service.get(routine_id)
    assert (got["state"], got["state_revision"], got["tested_revision"]) == ("draft", 1, 1)
    _tested(service, routine_id, 2)
    got = service.get(routine_id)
    assert (got["state"], got["state_revision"], got["tested_revision"]) == ("tested", 2, 2)
    _tested(service, routine_id, 2)
    assert service.get(routine_id)["state_revision"] == 2
    service.archive(routine_id, _ident(), 2)
    _tested(service, routine_id, 1)
    got = service.get(routine_id)
    assert (got["state"], got["tested_revision"]) == ("archived", 2)


def test_frozen_returns_that_revisions_document(service: RoutineService) -> None:
    routine_id = service.create(_ident(), _document_raw())[0]["routine_id"]
    service.revise(routine_id, _ident(), 1, _document_raw(name="Second"))
    assert service.frozen(routine_id, 1)["name"] == "Daily brief"
    assert service.frozen(routine_id, 2)["name"] == "Second"
    for target, revision in ((routine_id, 3), (_ident(), 1)):
        with pytest.raises(RoutineError) as err:
            service.frozen(target, revision)
        assert _code(err) == "not_found"


def test_plans_read_enabled_routines_in_one_connection(
    ready: RoutineService, monkeypatch: pytest.MonkeyPatch
) -> None:
    enabled_id = _reach(ready, "enabled")
    ready.revise(enabled_id, _ident(), 1, _document_raw(schedule=_weekly_schedule()))
    paused_id = _reach(ready, "paused")
    _reach(ready, "draft")
    _reach(ready, "archived")
    opened = []
    original = ready.store._connection

    def spy(*args: Any, **kwargs: Any) -> Any:
        opened.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(ready.store, "_connection", spy)
    plans = ready.plans()
    assert len(opened) == 1
    assert plans == [
        RoutinePlan(
            enabled_id,
            "enabled",
            stamp(NOW),
            None,
            (
                RevisionPlan(1, stamp(NOW), _daily_schedule()),
                RevisionPlan(2, stamp(NOW + timedelta(milliseconds=1)), _weekly_schedule()),
            ),
        )
    ]
    assert ready.plan(paused_id).state == "paused"
    assert ready.plan(enabled_id) == plans[0]
    with pytest.raises(RoutineError) as err:
        ready.plan(_ident())
    assert _code(err) == "not_found"


@pytest.mark.parametrize("schedule", ["{}}", json.dumps({"kind": "hourly"})])
def test_plans_refuse_a_corrupt_schedule_column(ready: RoutineService, schedule: str) -> None:
    routine_id = _reach(ready, "enabled")
    with ready.store._connection(write=True) as connection:
        connection.execute(
            "UPDATE routine_revisions SET schedule=? WHERE routine_id=?", (schedule, routine_id)
        )
    for call in (ready.plans, lambda: ready.plan(routine_id)):
        with pytest.raises(RoutineError) as err:
            call()
        assert _code(err) == "corrupt"
