"""Durable user choices: one per native request, encrypted, never replayed by a read."""

from __future__ import annotations

import contextlib
import dataclasses
import json
import logging
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from viki_agent import protection
from viki_agent.conversations import approvals
from viki_agent.conversations.approvals import (
    MAX_CIPHERTEXT_BYTES,
    MAX_RECORD_BYTES,
    ApprovalConflict,
    ApprovalError,
    ApprovalService,
    ChoiceInput,
    NativeRequest,
    PermissionChoice,
    QuestionChoice,
    choice_from_json,
    choice_kind,
    choice_to_json,
)
from viki_agent.state import AppState, Prefs
from viki_agent.storage import StateError

SID, GEN, OWNER, CID, OTHER, NEXT_OWNER = (
    f"00000000-0000-4000-8000-{index:012d}" for index in range(1, 7)
)
RUNTIME, REQUEST, TURN, OTHER_REQUEST = (
    f"00000000-0000-4000-9000-{index:012d}" for index in range(1, 5)
)
TOOL = "toolu_01SyntheticApprovalToolUse"
NATIVE = NativeRequest(RUNTIME, REQUEST, TURN)
SECRET = "synthetic-approval-secret-3f9c2a naïve\n"
DISPLAYED = "synthetic-displayed-request-4c81ab"
CHOICE = QuestionChoice({"q-1": "Option A"}, SECRET)
INPUT = ChoiceInput(CID, SID, GEN, TOOL, NATIVE, CHOICE)
ITEM: dict[str, Any] = {
    "type": "question.asked",
    "data": {
        "tool_use_id": TOOL,
        "questions": [{"id": "q-1", "question": DISPLAYED, "options": ["Option A", "Option B"]}],
    },
    "native_request": {"runtime_id": RUNTIME, "request_id": REQUEST, "turn_id": TURN},
}


@pytest.fixture
def service(tmp_path: Path) -> ApprovalService:
    state = AppState(tmp_path / "data" / "state.json", Prefs())
    return ApprovalService(state, tmp_path / "vault", owner=OWNER)


def reopen(tmp_path: Path, owner: str = OWNER, vault: str = "vault") -> ApprovalService:
    state = AppState(tmp_path / "data" / "state.json", Prefs())
    return ApprovalService(state, tmp_path / vault, owner=owner)


def card(padding: int) -> dict[str, Any]:
    return {**ITEM, "data": {**ITEM["data"], "padding": "x" * padding}}


def block_edge_padding() -> int:
    """The filler size at which the unquantised allow document lands on a PAD_TO boundary.

    That is the case a single round-up of the finished document cannot equalise: allow fills
    the last block exactly and deny spills one byte into the next one.
    """
    for size in range(approvals.PAD_TO + 1):
        whole = approvals._canonical(
            {"choice": {"kind": "permission", "allow": True}, "request": card(size)}
        )
        if len(whole) % approvals.PAD_TO == 0:
            return size
    raise AssertionError("no block-aligned card size exists")


def ceiling_padding() -> int:
    """The filler that makes the unpadded document exactly MAX_RECORD_BYTES for either choice.

    The document ends with "}" before its trailing pad, so rstrip recovers the unpadded size,
    which grows one byte per filler character. Measuring it beats restating _document's framing.
    """
    unpadded = len(approvals._document(PermissionChoice(True), card(0)).rstrip(b" "))
    assert (
        len(approvals._document(PermissionChoice(True), card(100)).rstrip(b" ")) == unpadded + 100
    )
    return MAX_RECORD_BYTES - unpadded


def rows(service: ApprovalService, column: str) -> list[Any]:
    with contextlib.closing(sqlite3.connect(service._store.path)) as connection:
        return [row[0] for row in connection.execute(f"SELECT {column} FROM approvals")]


def seeded_id(index: int) -> str:
    return f"00000000-0000-4000-a000-{index:012d}"


def seed(service: ApprovalService, rows_in: list[tuple[int, str, str]]) -> None:
    """Insert (index, owner, state) rows straight through SQLite, without the service.

    Seeded rows stand in for what an earlier launch left behind: only their plain columns are
    ever read, so no request hash or ciphertext is built for them.
    """
    with contextlib.closing(sqlite3.connect(service._store.path)) as connection:
        connection.executemany(
            f"INSERT INTO approvals VALUES ({','.join('?' * 15)})",
            [
                (
                    service._scope,
                    seeded_id(index),
                    SID,
                    GEN,
                    RUNTIME,
                    seeded_id(index),  # one row per native request, as the UNIQUE says
                    TURN,
                    TOOL,
                    "b" * 64,
                    owner,
                    row_state,
                    "interrupted" if row_state in ("not_delivered", "unknown") else "",
                    b"x",
                    f"2026-09-17T00:00:{index:02d}.000Z",
                    f"2026-09-17T00:00:{index:02d}.000Z",
                )
                for index, owner, row_state in rows_in
            ],
        )
        connection.commit()


def later(index: int) -> ChoiceInput:
    """A fresh choice for a fresh native request in the same session."""
    ident = f"00000000-0000-4000-b000-{index:012d}"
    return dataclasses.replace(INPUT, choice_id=ident, native=NativeRequest(RUNTIME, ident, TURN))


def test_no_plaintext_choice_in_files_or_hash(service: ApprovalService, tmp_path: Path) -> None:
    record, created = service.record(INPUT, ITEM)
    assert created and record.state == "recorded" and record.reason == ""
    assert record.choice == CHOICE and record.request == ITEM
    assert record.created_at == record.updated_at and record.created_at.endswith("Z")
    assert (record.runtime_id, record.request_id, record.turn_id) == (RUNTIME, REQUEST, TURN)
    for path in (tmp_path / "data").iterdir():
        if path.is_file():
            data = path.read_bytes()
            for marker in (SECRET, DISPLAYED, "Option A"):
                assert marker.encode("utf-8") not in data
                assert marker.encode("utf-16le") not in data
    identity = approvals._identity(service._scope, INPUT)
    assert rows(service, "request_hash") == [approvals._request_hash(identity)]
    # The digest is over identifiers only: the opposite answer under the same identity would
    # produce the same hash, so no plain column distinguishes Allow once from Deny.
    other = dataclasses.replace(INPUT, choice=PermissionChoice(False))
    assert (
        approvals._request_hash(approvals._identity(service._scope, other))
        == rows(service, "request_hash")[0]
    )
    assert not hasattr(record, "bytes")
    with contextlib.closing(sqlite3.connect(service._store.path)) as connection:
        columns = [row[1] for row in connection.execute("PRAGMA table_info(approvals)")]
    assert "body_bytes" not in columns and not any("bytes" in name for name in columns)
    (cipher,) = rows(service, "length(body)")
    assert (
        len(
            json.dumps(
                {"choice": choice_to_json(CHOICE), "request": ITEM},
                ensure_ascii=True,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("ascii")
        )
        < cipher
    )


def test_record_reads_same_id_request_and_usage_inside_one_immediate_transaction(
    service: ApprovalService, monkeypatch: pytest.MonkeyPatch
) -> None:
    statements: list[tuple[int, str]] = []
    original = sqlite3.connect

    class Recording(sqlite3.Connection):
        def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
            statements.append((id(self), sql))
            return super().execute(sql, parameters)

    monkeypatch.setattr(sqlite3, "connect", lambda *a, **kw: original(*a, factory=Recording, **kw))
    service.record(INPUT, ITEM)
    writer = next(handle for handle, sql in statements if sql == "BEGIN IMMEDIATE")
    issued = [sql for handle, sql in statements if handle == writer]
    inside = issued[issued.index("BEGIN IMMEDIATE") + 1 : issued.index("COMMIT")]
    assert any("FROM approvals WHERE scope=? AND choice_id=?" in sql for sql in inside)
    assert any("session_id=? AND runtime_id=? AND request_id=?" in sql for sql in inside)
    assert any(sql.startswith("SELECT count(*), COALESCE(sum(length(body))") for sql in inside)
    assert any(sql.startswith("INSERT INTO approvals") for sql in inside)


def test_same_id_replays_and_a_second_id_for_one_request_is_already_decided(
    service: ApprovalService,
) -> None:
    record, _ = service.record(INPUT, ITEM)
    assert service.record(INPUT, ITEM) == (record, False)
    assert service.lookup(INPUT) == record
    assert service.get(CID) == record
    assert service.summary(CID) == service.list().approvals[0]
    for conflicting in (
        dataclasses.replace(INPUT, choice=QuestionChoice({"q-1": "Option B"}, SECRET)),
        dataclasses.replace(INPUT, choice=PermissionChoice(True)),
        dataclasses.replace(INPUT, generation=OTHER),
        dataclasses.replace(INPUT, native=NativeRequest(RUNTIME, REQUEST, OTHER)),
        dataclasses.replace(INPUT, tool_use_id="toolu_other"),
    ):
        with pytest.raises(ApprovalConflict):
            service.record(conflicting, ITEM)
        with pytest.raises(ApprovalConflict):
            service.lookup(conflicting)
    with pytest.raises(ApprovalError) as decided:
        service.record(dataclasses.replace(INPUT, choice_id=OTHER), ITEM)
    assert decided.value.code == "already_decided"
    assert service.lookup(dataclasses.replace(INPUT, choice_id=OTHER)) is None
    assert service.find_request(SID, RUNTIME, REQUEST).choice_id == CID
    assert [row.choice_id for row in service.list().approvals] == [CID]


def test_two_clients_race_to_one_record_and_one_intent(service: ApprovalService) -> None:
    barrier = threading.Barrier(2)

    def attempt() -> Any:
        barrier.wait()
        try:
            return service.record(INPUT, ITEM)
        except ApprovalError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = [future.result() for future in (pool.submit(attempt), pool.submit(attempt))]
    assert sum(isinstance(value, tuple) and value[1] for value in outcomes) == 1
    assert all(not isinstance(value, ApprovalError) or value.code == "busy" for value in outcomes)
    barrier.reset()

    def claim() -> Any:
        barrier.wait()
        try:
            return service.mark_delivering(CID)
        except ApprovalError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = [future.result() for future in (pool.submit(claim), pool.submit(claim))]
    assert sum(not isinstance(value, ApprovalError) for value in claims) == 1
    assert all(
        not isinstance(value, ApprovalError) or value.code in {"busy", "conflict"}
        for value in claims
    )


@pytest.mark.parametrize(
    ("state", "reason", "projected"),
    [
        ("recorded", "", ("not_delivered", "owner_changed")),
        ("delivering", "", ("unknown", "owner_changed")),
        ("delivered_locally", "", ("delivered_locally", "")),
        ("not_delivered", "stale_request", ("not_delivered", "stale_request")),
        ("unknown", "internal", ("unknown", "internal")),
    ],
)
def test_previous_owner_nonterminal_rows_are_projected_without_writing(
    service: ApprovalService, tmp_path: Path, state: str, reason: str, projected: tuple[str, str]
) -> None:
    service.record(INPUT, ITEM)
    if state != "recorded":
        service.mark_delivering(CID)
    if state not in {"recorded", "delivering"}:
        service.finish(CID, state, reason)  # type: ignore[arg-type]
    reopened = reopen(tmp_path, owner=NEXT_OWNER)
    before = service._store.path.read_bytes()
    recovered = reopened.get(CID)
    assert (recovered.state, recovered.reason) == projected
    assert recovered.choice == CHOICE and recovered.request == ITEM
    assert reopened.summary(CID).state == recovered.state
    assert reopened.list().approvals[0].state == recovered.state
    assert reopened.find_request(SID, RUNTIME, REQUEST).state == recovered.state
    assert reopened.record(INPUT, ITEM) == (recovered, False)
    assert service._store.path.read_bytes() == before
    with pytest.raises(ApprovalConflict):
        reopened.mark_delivering(CID)
    with pytest.raises(ApprovalConflict):
        reopened.finish(CID, "not_delivered", "runtime_changed")
    with contextlib.closing(sqlite3.connect(service._store.path)) as connection:
        assert connection.execute("SELECT state,reason FROM approvals").fetchone() == (
            state,
            reason,
        )


def test_terminal_states_are_immutable_and_intent_is_once(service: ApprovalService) -> None:
    service.record(INPUT, ITEM)
    assert service.mark_delivering(CID).state == "delivering"
    with pytest.raises(ApprovalConflict):
        service.mark_delivering(CID)
    final = service.finish(CID, "delivered_locally", "")
    assert service.finish(CID, "delivered_locally", "") == final
    for state, reason in (("not_delivered", "stale_request"), ("unknown", "internal")):
        with pytest.raises(ApprovalConflict):
            service.finish(CID, state, reason)  # type: ignore[arg-type]
    with pytest.raises(ApprovalConflict):
        service.mark_delivering(CID)
    assert service.record(INPUT, ITEM) == (final, False)


def test_recorded_can_finish_directly_without_intent(service: ApprovalService) -> None:
    service.record(INPUT, ITEM)
    finished = service.finish(CID, "not_delivered", "storage")
    assert (finished.state, finished.reason) == ("not_delivered", "storage")


@pytest.mark.parametrize(
    ("state", "reason"),
    [
        ("delivered_locally", "internal"),
        ("unknown", "cancelled"),
        ("unknown", "stale_request"),
        ("not_delivered", "internal"),
        ("not_delivered", ""),
        ("recorded", ""),
        ("delivering", ""),
        ("delivered_locally", "owner_changed"),
    ],
)
def test_finish_refuses_a_state_and_reason_the_table_does_not_allow(
    service: ApprovalService, state: str, reason: str
) -> None:
    original, _ = service.record(INPUT, ITEM)
    with pytest.raises(ApprovalError) as failure:
        service.finish(CID, state, reason)  # type: ignore[arg-type]
    assert failure.value.code == "invalid_input"
    assert service.get(CID) == original


def test_ciphertext_bound_to_full_identity_is_never_recovered_elsewhere(
    service: ApprovalService, tmp_path: Path
) -> None:
    service.record(INPUT, ITEM)
    second = dataclasses.replace(
        INPUT, choice_id=OTHER, native=NativeRequest(RUNTIME, OTHER_REQUEST, TURN)
    )
    service.record(second, ITEM)
    elsewhere = reopen(tmp_path, vault="other-vault")
    elsewhere.record(INPUT, ITEM)
    with contextlib.closing(sqlite3.connect(service._store.path)) as connection:
        foreign_row, foreign_scope = (
            connection.execute(
                "SELECT body FROM approvals WHERE choice_id=? AND scope=?", (choice, scope)
            ).fetchone()[0]
            for choice, scope in ((OTHER, service._scope), (CID, elsewhere._scope))
        )
        for cipher in (foreign_row, foreign_scope):
            connection.execute(
                "UPDATE approvals SET body=? WHERE choice_id=? AND scope=?",
                (cipher, CID, service._scope),
            )
            connection.commit()
            with pytest.raises(ApprovalError) as failure:
                service.get(CID)
            assert failure.value.code == "protection"


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("generation", OTHER),
        ("turn_id", OTHER),
        ("tool_use_id", "toolu_swapped"),
        ("owner", "00000000-0000-4000-8000-00000000000Z"),
        ("request_hash", "0" * 64),
        ("created_at", "2026-13-16T00:00:00.000Z"),
        ("body", b"bad"),
        ("state", "recorded"),  # a state whose table forbids the reason this row carries
        ("reason", "internal"),  # a reason not_delivered may never carry
    ],
)
def test_tampered_identity_or_ciphertext_is_never_recovered(
    service: ApprovalService, column: str, value: Any
) -> None:
    service.record(INPUT, ITEM)
    service.finish(CID, "not_delivered", "cancelled")
    with contextlib.closing(sqlite3.connect(service._store.path)) as connection:
        connection.execute(f"UPDATE approvals SET {column}=?", (value,))
        connection.commit()
    with pytest.raises(ApprovalError) as failure:
        service.get(CID)
    assert failure.value.code in {"corrupt", "protection"}


@pytest.mark.parametrize(
    "plaintext",
    [
        b"x",
        b'{"choice":{"kind":"permission","allow":true}}',
        b'{"choice":{"kind":"permission","allow":true},"request":[]}',
        b'{"choice":{"kind":"plan"},"request":{}}',
        "{}".encode("utf-16le"),
    ],
)
def test_decoded_document_shape_is_checked(
    service: ApprovalService, monkeypatch: pytest.MonkeyPatch, plaintext: bytes
) -> None:
    service.record(INPUT, ITEM)
    monkeypatch.setattr(protection, "unprotect", lambda *a, **kw: plaintext)
    with pytest.raises(ApprovalError) as failure:
        service.get(CID)
    assert failure.value.code == "corrupt"


@pytest.mark.parametrize("quota", ["MAX_APPROVALS", "MAX_SCOPE_BYTES"])
def test_quota_refuses_without_eviction_and_duplicates_still_recover(
    service: ApprovalService, monkeypatch: pytest.MonkeyPatch, quota: str
) -> None:
    first, _ = service.record(INPUT, ITEM)
    monkeypatch.setattr(
        approvals, quota, 1 if quota == "MAX_APPROVALS" else rows(service, "length(body)")[0]
    )
    later = dataclasses.replace(
        INPUT, choice_id=OTHER, native=NativeRequest(RUNTIME, OTHER_REQUEST, TURN)
    )
    with pytest.raises(ApprovalError) as failure:
        service.record(later, ITEM)
    assert failure.value.code == "capacity"
    assert service.record(INPUT, ITEM) == (first, False)
    assert [row.choice_id for row in service.list().approvals] == [CID]


def test_a_full_scope_makes_room_from_a_dead_launch_and_records(
    service: ApprovalService, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed(service, [(index, NEXT_OWNER, "delivered_locally") for index in (1, 2, 3)])
    monkeypatch.setattr(approvals, "MAX_APPROVALS", 3)
    record, created = service.record(INPUT, ITEM)
    assert created and record.choice_id == CID
    # Exactly what the one insert needed: the oldest foreign terminal row, and no more.
    assert sorted(rows(service, "choice_id")) == [CID, seeded_id(2), seeded_id(3)]


def test_a_scope_this_launch_filled_still_refuses(
    service: ApprovalService, monkeypatch: pytest.MonkeyPatch
) -> None:
    for index in (1, 2):
        service.record(later(index), ITEM)
        service.finish(later(index).choice_id, "delivered_locally", "")
    monkeypatch.setattr(approvals, "MAX_APPROVALS", 2)
    before = sorted(rows(service, "choice_id"))
    with pytest.raises(ApprovalError) as failure:
        service.record(INPUT, ITEM)
    # Terminal, but this launch's: a card on screen may still look one of these up, and its
    # native request may still be live. The wall stays until the user reopens VIKI.
    assert failure.value.code == "capacity"
    assert sorted(rows(service, "choice_id")) == before


def test_a_busy_reclaim_reports_the_capacity_it_was_called_for(
    service: ApprovalService, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    seed(service, [(1, NEXT_OWNER, "delivered_locally")])
    monkeypatch.setattr(approvals, "MAX_APPROVALS", 1)

    def busy(*_args: Any, **_kwargs: Any) -> int:
        raise StateError("busy")

    monkeypatch.setattr(service._store, "reclaim_approvals", busy)
    caplog.set_level(logging.WARNING, logger="viki_agent.conversations.approvals")
    with pytest.raises(ApprovalError) as failure:
        service.record(INPUT, ITEM)
    # `busy` would reach the browser as "possibly recorded" and hand the card its buttons back;
    # the quota is full either way, so the capacity this was called for is the true answer.
    assert failure.value.code == "capacity"
    assert [record.getMessage() for record in caplog.records] == ["approvals reclaim refused: busy"]


def test_a_reclaim_that_frees_nothing_re_raises_the_original_capacity(
    service: ApprovalService, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed(service, [(1, NEXT_OWNER, "recorded")])  # foreign, but not terminal
    monkeypatch.setattr(approvals, "MAX_APPROVALS", 1)
    attempts = _count_calls(monkeypatch, service, "record_approval")
    with pytest.raises(ApprovalError) as failure:
        service.record(INPUT, ITEM)
    assert failure.value.code == "capacity"
    assert attempts == [1]  # the refused insert, and no retry over a scope with no room


def test_the_reclaim_refused_warning_is_also_one_line_per_launch(
    service: ApprovalService, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    seed(service, [(1, NEXT_OWNER, "delivered_locally")])
    monkeypatch.setattr(approvals, "MAX_APPROVALS", 1)

    def busy(*_args: Any, **_kwargs: Any) -> int:
        raise StateError("busy")

    monkeypatch.setattr(service._store, "reclaim_approvals", busy)
    caplog.set_level(logging.WARNING, logger="viki_agent.conversations.approvals")
    for index in (1, 2, 3):
        with pytest.raises(ApprovalError) as failure:
            service.record(later(index), ITEM)
        assert failure.value.code == "capacity"
    # The seeded row is never freed (every reclaim is busy), so every one of the three calls
    # above hits this refusal; only the first may log it, same as the reclaimed-count line below.
    assert [record.getMessage() for record in caplog.records] == ["approvals reclaim refused: busy"]


def test_the_reclaim_warning_is_one_line_per_launch_and_a_count(
    service: ApprovalService, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    seed(service, [(index, NEXT_OWNER, "delivered_locally") for index in (1, 2, 3)])
    monkeypatch.setattr(approvals, "MAX_APPROVALS", 3)
    caplog.set_level(logging.WARNING, logger="viki_agent.conversations.approvals")
    for index in (1, 2, 3):
        service.record(later(index), ITEM)
    assert rows(service, "owner").count(NEXT_OWNER) == 0  # all three reclaimed
    # From the moment a scope is full every answered card reclaims, and a warning per turn is
    # what the rotating log must not grow by. The pattern is the hard rule 8 assertion too: a
    # count and nothing else, so no choice_id, scope, session_id, owner or body can be in it.
    assert [record.getMessage() for record in caplog.records] == ["approvals reclaimed: 1"]


def test_reclamation_runs_only_under_pressure(
    service: ApprovalService, monkeypatch: pytest.MonkeyPatch
) -> None:
    reclaims = _count_calls(monkeypatch, service, "reclaim_approvals")
    assert service.record(INPUT, ITEM)[1] is True
    assert reclaims == []  # nothing is deleted while the scope has room


def _count_calls(monkeypatch: pytest.MonkeyPatch, service: ApprovalService, name: str) -> list[int]:
    """Record one entry per call of a store accessor, without changing what it does."""
    calls: list[int] = []
    original = getattr(service._store, name)

    def counted(*args: Any, **kwargs: Any) -> Any:
        calls.append(len(calls) + 1)
        return original(*args, **kwargs)

    monkeypatch.setattr(service._store, name, counted)
    return calls


def test_largest_allowed_document_fits_the_ciphertext_and_column_bounds(
    service: ApprovalService,
) -> None:
    padding = ceiling_padding()
    filler = card(padding)
    assert MAX_RECORD_BYTES % approvals.PAD_TO == 0
    for allow in (True, False):
        # The ceiling is a property of the card, not of the answer: an allow that records
        # while its deny is refused would publish the choice through the refusal.
        assert len(approvals._document(PermissionChoice(allow), filler)) == MAX_RECORD_BYTES
        with pytest.raises(ApprovalError) as refused:
            approvals._document(PermissionChoice(allow), card(padding + 1))
        assert refused.value.code == "too_large"
    record, created = service.record(
        dataclasses.replace(INPUT, choice=PermissionChoice(True)), filler
    )
    assert created and record.choice == PermissionChoice(True)
    (cipher,) = rows(service, "length(body)")
    assert cipher <= MAX_CIPHERTEXT_BYTES
    assert service.get(CID).request == filler
    with pytest.raises(ApprovalError) as failure:
        # A different request as well as a different ID, so only the size can refuse it.
        service.record(
            dataclasses.replace(
                INPUT, choice_id=OTHER, native=NativeRequest(RUNTIME, OTHER_REQUEST, TURN)
            ),
            card(padding + 1),
        )
    assert failure.value.code == "too_large"
    assert [row.choice_id for row in service.list().approvals] == [CID]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("choice_id", "../../bad"),
        ("choice_id", "00000000-0000-4000-8000-00000000000A"),
        ("session_id", "new"),
        ("generation", GEN + "x"),
        ("tool_use_id", "x" * 257),
        ("tool_use_id", "toolu_あ"),
        ("tool_use_id", "toolu_\x00bad"),
        ("tool_use_id", 7),
        ("native", {"runtime_id": RUNTIME, "request_id": REQUEST, "turn_id": TURN}),
        ("native", NativeRequest(RUNTIME, REQUEST, "not-a-uuid")),
    ],
)
def test_invalid_identity_is_refused_before_any_row(
    service: ApprovalService, field: str, value: Any
) -> None:
    bad = dataclasses.replace(INPUT, **{field: value})
    for call in (service.lookup, lambda item: service.record(item, ITEM)):
        with pytest.raises(ApprovalError) as failure:
            call(bad)
        assert failure.value.code == "invalid_input"
    assert service.list().approvals == []


@pytest.mark.parametrize(
    "choice",
    [
        PermissionChoice(1),  # type: ignore[arg-type]
        QuestionChoice({"q-1": 1}, ""),  # type: ignore[dict-item]
        QuestionChoice({1: "a"}, ""),  # type: ignore[dict-item]
        QuestionChoice({}, None),  # type: ignore[arg-type]
        QuestionChoice({"q-1": "x" * (64 << 10)}, ""),
        "permission",
    ],
)
def test_unusable_choice_is_refused_before_any_row(service: ApprovalService, choice: Any) -> None:
    with pytest.raises(ApprovalError) as failure:
        service.record(dataclasses.replace(INPUT, choice=choice), ITEM)
    assert failure.value.code == "invalid_choice"
    assert service.list().approvals == []


@pytest.mark.parametrize(
    "request_item", [[], "text", None, {"data": float("nan")}, {"data": object()}]
)
def test_unusable_request_projection_is_refused_before_any_row(
    service: ApprovalService, request_item: Any
) -> None:
    with pytest.raises(ApprovalError) as failure:
        service.record(INPUT, request_item)
    assert failure.value.code == "invalid_input"
    assert service.list().approvals == []


def test_choice_json_round_trip_rejects_foreign_shapes() -> None:
    for choice in (PermissionChoice(True), PermissionChoice(False), CHOICE):
        assert choice_from_json(choice_to_json(choice)) == choice
    assert choice_kind(PermissionChoice(True)) == "permission"
    assert choice_kind(CHOICE) == "question"
    for value in (
        None,
        {"kind": "permission"},
        {"kind": "permission", "allow": 1},
        {"kind": "permission", "allow": True, "extra": 1},
        {"kind": "question", "answers": {}},
        {"kind": "question", "answers": [], "response": ""},
        {"kind": "plan", "allow": True},
    ):
        with pytest.raises(ApprovalError) as failure:
            choice_from_json(value)
        assert failure.value.code == "invalid_choice"
    with pytest.raises(ApprovalError):
        choice_kind("permission")  # type: ignore[arg-type]


def test_reads_are_scoped_and_missing_records_are_explicit(
    service: ApprovalService, tmp_path: Path
) -> None:
    service.record(INPUT, ITEM)
    elsewhere = reopen(tmp_path, vault="other-vault")
    assert elsewhere.list().approvals == [] and elsewhere.lookup(INPUT) is None
    for missing in (
        lambda: elsewhere.get(CID),
        lambda: elsewhere.summary(CID),
        lambda: elsewhere.find_request(SID, RUNTIME, REQUEST),
        lambda: service.find_request(SID, RUNTIME, OTHER_REQUEST),
        lambda: service.get(OTHER),
        lambda: service.mark_delivering(OTHER),
    ):
        with pytest.raises(ApprovalError) as failure:
            missing()
        assert failure.value.code == "not_found"
    for invalid in (
        lambda: service.get("new"),
        lambda: service.summary("new"),
        lambda: service.mark_delivering("new"),
        lambda: service.finish("new", "delivered_locally", ""),
    ):
        with pytest.raises(ApprovalError) as failure:
            invalid()
        assert failure.value.code == "invalid_id"
    with pytest.raises(ApprovalError) as failure:
        service.find_request(SID, RUNTIME, "new")
    assert failure.value.code == "invalid_input"


def test_list_and_find_never_decrypt_and_paginate(
    service: ApprovalService, monkeypatch: pytest.MonkeyPatch
) -> None:
    service.record(INPUT, ITEM)
    second = dataclasses.replace(
        INPUT, choice_id=OTHER, native=NativeRequest(RUNTIME, OTHER_REQUEST, TURN)
    )
    service.record(second, ITEM)
    monkeypatch.setattr(
        protection, "unprotect", lambda *a, **kw: pytest.fail("metadata read decrypted a choice")
    )
    first = service.list(limit=1)
    assert [row.choice_id for row in first.approvals] == [CID] and first.next_after == CID
    assert not hasattr(first.approvals[0], "choice")
    page = service.list(after=first.next_after, limit=1)
    assert [row.choice_id for row in page.approvals] == [OTHER] and page.next_after is None
    assert service.find_request(SID, RUNTIME, OTHER_REQUEST).choice_id == OTHER
    assert service.summary(CID).choice_id == CID


@pytest.mark.parametrize("kwargs", [{"limit": True}, {"limit": 0}, {"limit": 101}, {"after": "x"}])
def test_page_bounds(service: ApprovalService, kwargs: dict[str, Any]) -> None:
    with pytest.raises(ApprovalError) as failure:
        service.list(**kwargs)
    assert failure.value.code == "invalid_page"


@pytest.mark.parametrize("helper", ["protect", "require_private_directory", "require_private_file"])
def test_protection_failure_has_no_plaintext_fallback_or_record(
    service: ApprovalService, monkeypatch: pytest.MonkeyPatch, helper: str
) -> None:
    def fail(*a: Any, **kw: Any) -> Any:
        raise protection.ProtectionError()

    with monkeypatch.context() as patch:
        patch.setattr(protection, helper, fail)
        with pytest.raises(ApprovalError) as failure:
            service.record(INPUT, ITEM)
        assert failure.value.code == "protection"
    assert service.lookup(INPUT) is None


def test_receipt_decryption_failure_after_the_commit_leaves_a_recoverable_record(
    service: ApprovalService, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*a: Any, **kw: Any) -> Any:
        raise protection.ProtectionError()

    with monkeypatch.context() as patch:
        patch.setattr(protection, "unprotect", fail)
        # The choice is protected before the insert and decoded after the commit, so this is a
        # lost receipt, not a lost record: no native exception escapes and nothing is rewritten.
        with pytest.raises(ApprovalError) as failure:
            service.record(INPUT, ITEM)
        assert failure.value.code == "protection"
        for read in (lambda: service.get(CID), lambda: service.lookup(INPUT)):
            with pytest.raises(ApprovalError) as lost:
                read()
            assert lost.value.code == "protection"
        assert service.summary(CID).state == "recorded"
    recovered, created = service.record(INPUT, ITEM)
    assert not created and recovered.choice == CHOICE and recovered.state == "recorded"


@pytest.mark.parametrize("boundary", ["INSERT INTO approvals", "COMMIT"])
def test_failed_record_publishes_nothing_and_hides_storage_text(
    service: ApprovalService, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    original = sqlite3.connect

    class Connection(sqlite3.Connection):
        def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
            if sql.startswith(boundary):
                raise sqlite3.OperationalError("synthetic-private-error-body")
            return super().execute(sql, parameters)

    with monkeypatch.context() as patch:
        patch.setattr(sqlite3, "connect", lambda *a, **kw: original(*a, factory=Connection, **kw))
        with pytest.raises(ApprovalError) as failure:
            service.record(INPUT, ITEM)
        assert failure.value.code == "unavailable" and "synthetic" not in str(failure.value)
    assert service.lookup(INPUT) is None


def test_failed_transition_commit_preserves_the_last_durable_state(
    service: ApprovalService, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_record, _ = service.record(INPUT, ITEM)
    original = sqlite3.connect

    class Connection(sqlite3.Connection):
        def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
            if sql == "COMMIT":
                raise sqlite3.OperationalError("synthetic failure")
            return super().execute(sql, parameters)

    with monkeypatch.context() as patch:
        patch.setattr(sqlite3, "connect", lambda *a, **kw: original(*a, factory=Connection, **kw))
        with pytest.raises(ApprovalError):
            service.mark_delivering(CID)
    assert service.get(CID) == original_record


@pytest.mark.parametrize("aligned", [False, True])
def test_an_allow_and_a_deny_are_indistinguishable_in_every_plain_column(
    tmp_path: Path, aligned: bool
) -> None:
    """The quantised choice is why: the two answers differ by one plaintext byte."""
    item = card(block_edge_padding()) if aligned else ITEM
    documents = [approvals._document(PermissionChoice(value), item) for value in (True, False)]
    # Unpadded these differ by one byte, and a one-byte difference reaches the ciphertext
    # whenever it crosses a block edge - which the aligned case puts it exactly on.
    assert len(documents[0]) == len(documents[1]) and documents[0] != documents[1]
    assert len(documents[0]) % approvals.PAD_TO == 0
    columns = (
        "scope,choice_id,session_id,generation,runtime_id,request_id,turn_id,"
        "tool_use_id,request_hash,owner,state,reason,length(body)"
    )
    stored: list[Any] = []
    bodies: list[bytes] = []
    for index, allow in enumerate((True, False)):
        # Two stores, one vault: the same identifiers can carry the opposite choice, so every
        # column except the timestamps is comparable directly.
        state = AppState(tmp_path / f"store-{index}" / "state.json", Prefs())
        service = ApprovalService(state, tmp_path / "vault", owner=OWNER)
        record, created = service.record(
            dataclasses.replace(INPUT, choice=PermissionChoice(allow)), item
        )
        assert created and record.choice == PermissionChoice(allow)
        with contextlib.closing(sqlite3.connect(state.database_path)) as connection:
            stored.append(connection.execute(f"SELECT {columns} FROM approvals").fetchone())
            bodies.append(connection.execute("SELECT body FROM approvals").fetchone()[0])
    assert stored[0] == stored[1]
    assert bodies[0] != bodies[1]  # different ciphertexts of the same length, not one row


def test_two_bindings_in_one_store_keep_the_same_ciphertext_length(
    service: ApprovalService,
) -> None:
    service.record(dataclasses.replace(INPUT, choice=PermissionChoice(True)), ITEM)
    service.record(
        dataclasses.replace(
            INPUT,
            choice_id=OTHER,
            choice=PermissionChoice(False),
            native=NativeRequest(RUNTIME, OTHER_REQUEST, TURN),
        ),
        ITEM,
    )
    assert len(set(rows(service, "length(body)"))) == 1


def test_a_card_without_a_native_tool_use_id_stays_answerable(service: ApprovalService) -> None:
    # driver.py emits plan and permission cards with `ctx.tool_use_id or ""`.
    value = dataclasses.replace(INPUT, tool_use_id="", choice=PermissionChoice(True))
    item = {**ITEM, "data": {**ITEM["data"], "tool_use_id": ""}}
    record, created = service.record(value, item)
    assert created and record.tool_use_id == "" and record.choice == PermissionChoice(True)
    assert service.lookup(value) == record
    assert service.find_request(SID, RUNTIME, REQUEST).tool_use_id == ""
    assert service.mark_delivering(CID).state == "delivering"
    assert service.finish(CID, "delivered_locally", "").state == "delivered_locally"
    assert service.get(CID).choice == PermissionChoice(True)


def test_encryption_runs_before_the_writer_reservation(
    service: ApprovalService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DPAPI on a large card costs more than every other writer's whole busy budget."""
    events: list[str] = []
    original_connect, original_protect = sqlite3.connect, protection.protect

    class Recording(sqlite3.Connection):
        def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
            events.append(sql)
            return super().execute(sql, parameters)

    def protect(data: bytes, *, context: bytes) -> bytes:
        events.append("protect")
        return original_protect(data, context=context)

    monkeypatch.setattr(protection, "protect", protect)
    monkeypatch.setattr(
        sqlite3, "connect", lambda *a, **kw: original_connect(*a, factory=Recording, **kw)
    )
    service.record(INPUT, card(ceiling_padding()))
    assert events.count("protect") == 1
    # Nothing opens the store before the ciphertext exists, so no connection - and above all no
    # BEGIN IMMEDIATE - is held across the encryption.
    assert events[0] == "protect"
    assert "protect" not in events[events.index("BEGIN IMMEDIATE") :]


def test_transitions_decrypt_outside_the_writer_reservation(
    service: ApprovalService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transition validates on plain columns. DPAPI inside BEGIN IMMEDIATE would hold the
    writer past storage.BUSY_TIMEOUT_S for every concurrent draft save and card answer."""
    service.record(INPUT, card(ceiling_padding()))
    events: list[str] = []
    original_connect, original_unprotect = sqlite3.connect, protection.unprotect

    class Recording(sqlite3.Connection):
        def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
            events.append(sql)
            return super().execute(sql, parameters)

    def unprotect(data: bytes, *, context: bytes) -> bytes:
        events.append("unprotect")
        return original_unprotect(data, context=context)

    monkeypatch.setattr(protection, "unprotect", unprotect)
    monkeypatch.setattr(
        sqlite3, "connect", lambda *a, **kw: original_connect(*a, factory=Recording, **kw)
    )
    assert service.mark_delivering(CID).state == "delivering"
    assert service.finish(CID, "delivered_locally", "").state == "delivered_locally"
    held = False
    inside: list[str] = []
    for event in events:
        if event == "BEGIN IMMEDIATE":
            held = True
        elif event == "COMMIT":
            held = False
        elif held:
            inside.append(event)
    assert "unprotect" not in inside
    assert events.count("unprotect") == 2  # once per transition, after its commit
    assert "BEGIN IMMEDIATE" in events  # the slicing above proves nothing without a reservation


def test_reasons_is_the_union_of_the_state_table() -> None:
    """The frozen public name and the table finish() enforces cannot drift apart."""
    tabled = {
        "stale_request",
        "runtime_changed",
        "cancelled",
        "storage",
        "interrupted",
        "owner_changed",
        "internal",
    }
    assert tabled == approvals.REASONS  # SIM300: the constant belongs on the right
    listed = {reason for reasons in approvals._REASONS_FOR.values() for reason in reasons}
    assert listed == tabled | {""}
