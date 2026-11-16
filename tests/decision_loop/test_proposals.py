"""Wording proposals for Decision's questions: sealed at rest under a context that names the
row's scope and id, states derived from the scope's rows, one open proposal per scope, acts
compared and set at the revision the user saw, the wording in use failing closed, pruning, and
the inbox item the clients validate."""

from __future__ import annotations

import re
import sqlite3
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from viki_agent import protection, storage
from viki_agent.automation.routines import stamp
from viki_agent.decision_loop import playbook
from viki_agent.decision_loop import proposals as module
from viki_agent.decision_loop.contracts import DecisionLoopError, canonical_json, sha256_hex
from viki_agent.decision_loop.proposals import MAX_PROPOSALS, ProposalRecord, QuestionProposals
from viki_agent.state import AppState, Prefs
from viki_agent.storage import SessionStore, StateError

NOW = datetime(2026, 10, 22, 9, 0, tzinfo=UTC)
SCOPE = "00000000-0000-4000-8000-0000000000a1"
OTHER = "00000000-0000-4000-8000-0000000000b2"
CONTEXT = b"VIKI decision proposal v1\x00"
MARKER = "Marker 5d1e"


@pytest.fixture
def store(tmp_path: Path) -> SessionStore:
    return AppState(tmp_path / "state.json", Prefs()).store


@pytest.fixture
def proposals(store: SessionStore) -> QuestionProposals:
    return QuestionProposals(store)


def worded(question: str) -> dict[str, object]:
    """A wording `playbook.check_question` takes for any of the tunable questions."""
    return {
        "type": "noul",
        "instructions": {"question": question, "focus": playbook.OUTSIDE_SENTENCE},
        "criteria": {
            "true": "Moves a listed goal",
            "false": {"what": "Touches no listed goal", "not_for": "Tidiness alone"},
        },
    }


X = worded(f"{MARKER}: does `candidate` serve one of `owner.goals`?")
Y = worded("Does `candidate` move one of `owner.goals` forward or hold it back?")
N = worded("Has the owner not seen `candidate` lately?")


def later(minutes: int) -> datetime:
    return NOW + timedelta(minutes=minutes)


def refused(code: str, operation: Callable[..., object], *args: object) -> None:
    with pytest.raises(DecisionLoopError) as caught:
        operation(*args)
    assert caught.value.code == code and str(caught.value) == code


def rows(store: SessionStore) -> list[dict[str, Any]]:
    with store._connection() as connection:
        connection.row_factory = sqlite3.Row
        return [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM decision_proposals ORDER BY created_at,proposal_id"
            )
        ]


def execute(store: SessionStore, sql: str, values: tuple[object, ...]) -> None:
    with store._connection(write=True) as connection:
        connection.execute(sql, values)


def insert(store: SessionStore, *numbers: int, **changes: object) -> list[str]:
    """Rows written past the service in one transaction, one per number (its id, and its
    stamps that many seconds after NOW), each readable unless `changes` say otherwise."""
    ids: list[str] = []
    with store._connection(write=True) as connection:
        for n in numbers:
            row: dict[str, object] = {
                "proposal_id": f"00000000-0000-4000-8000-{n:012d}",
                "scope": SCOPE,
                "name": "goal_link",
                "proposed_sha256": "0" * 64,
                "body": b"{}",
                "created_at": stamp(NOW + timedelta(seconds=n)),
                "updated_at": stamp(NOW + timedelta(seconds=n)),
                "approved_at": None,
                "rejected_at": None,
                "reason": None,
                **changes,
            }
            connection.execute(
                f"INSERT INTO decision_proposals ({','.join(row)}) "
                f"VALUES ({','.join('?' * len(row))})",
                tuple(row.values()),
            )
            ids.append(str(row["proposal_id"]))
    return ids


def approved(
    proposals: QuestionProposals, name: str, question: dict[str, object], at: datetime
) -> ProposalRecord:
    created = proposals.create(SCOPE, name, question, at)
    return proposals.act(SCOPE, created.proposal_id, "approve", created.revision, None, at)


def states(proposals: QuestionProposals, scope: str = SCOPE) -> dict[str, str]:
    return {record.proposal_id: record.state for record in proposals.list(scope)}


# ---- create ----------------------------------------------------------------------------


def test_create_seals_an_open_proposal(proposals: QuestionProposals, store: SessionStore) -> None:
    created = proposals.create(SCOPE, "goal_link", X, NOW)

    assert uuid.UUID(created.proposal_id).version == 4
    assert str(uuid.UUID(created.proposal_id)) == created.proposal_id
    assert (created.scope, created.name, created.state, created.question) == (
        SCOPE,
        "goal_link",
        "open",
        X,
    )
    plain = canonical_json(X).encode()
    assert created.proposed_sha256 == sha256_hex(canonical_json(X))
    (row,) = rows(store)
    assert {name: value for name, value in row.items() if name != "body"} == {
        "proposal_id": created.proposal_id,
        "scope": SCOPE,
        "name": "goal_link",
        "proposed_sha256": created.proposed_sha256,
        "created_at": "2026-10-22T09:00:00.000Z",
        "updated_at": "2026-10-22T09:00:00.000Z",
        "approved_at": None,
        "rejected_at": None,
        "reason": None,
    }
    # The text is sealed at rest, to this row alone.
    for raw in (row["body"], store.path.read_bytes()):
        assert MARKER.encode() not in raw
    context = CONTEXT + SCOPE.encode() + created.proposal_id.encode()
    assert protection.unprotect(row["body"], context=context) == plain
    with pytest.raises(protection.ProtectionError):
        protection.unprotect(
            row["body"], context=CONTEXT + OTHER.encode() + created.proposal_id.encode()
        )
    assert proposals.get(SCOPE, created.proposal_id) == created
    refused("unknown_item", proposals.get, OTHER, created.proposal_id)


def test_japanese_wording_round_trips_sealed(
    proposals: QuestionProposals, store: SessionStore
) -> None:
    question = worded("`candidate` は `owner.goals` のどれかを前に進めますか？")
    created = proposals.create(SCOPE, "goal_link", question, NOW)
    (row,) = rows(store)
    assert "前に進め".encode() not in row["body"]
    assert canonical_json("前").strip('"').encode() not in row["body"]  # nor its escaped form
    assert proposals.get(SCOPE, created.proposal_id).question == question
    assert proposals.wording(SCOPE) == ({}, playbook.PLAYBOOK_VERSION, ("goal_link", question))


OUT_OF_SHAPE = {
    "score_question": ("importance", X),
    "own_question": ("worth_note", X),
    "unknown_question": ("nope", X),
    "not_a_name": (5, X),
    "extra_key": ("goal_link", {**X, "extra": 1}),
    "focus_without_the_outside_sentence": (
        "goal_link",
        {**X, "instructions": {"question": "Does it?", "focus": "Focus on goals."}},
    ),
    "a_string": ("goal_link", "Does `candidate` serve a goal?"),
}


@pytest.mark.parametrize(("name", "question"), OUT_OF_SHAPE.values(), ids=OUT_OF_SHAPE.keys())
def test_create_refuses_a_wording_out_of_shape(
    proposals: QuestionProposals, store: SessionStore, name: object, question: object
) -> None:
    refused("invalid_request", proposals.create, SCOPE, name, question, NOW)
    assert rows(store) == []


@pytest.mark.parametrize("scope", ["not-a-scope", SCOPE.upper(), None])
def test_create_refuses_a_scope_out_of_shape(
    proposals: QuestionProposals, store: SessionStore, scope: object
) -> None:
    refused("invalid_request", proposals.create, scope, "goal_link", X, NOW)
    assert rows(store) == []


def test_create_refuses_a_second_open_proposal(
    proposals: QuestionProposals, store: SessionStore
) -> None:
    """One proposal is tried at a time in a scope, whatever its question."""
    first = proposals.create(SCOPE, "goal_link", X, NOW)
    refused("not_applicable", proposals.create, SCOPE, "news", N, later(1))
    refused("not_applicable", proposals.create, SCOPE, "goal_link", Y, later(1))
    assert [row["proposal_id"] for row in rows(store)] == [first.proposal_id]

    other = proposals.create(OTHER, "goal_link", X, later(1))  # another scope tries its own
    proposals.act(SCOPE, first.proposal_id, "reject", first.revision, None, later(2))
    second = approved(proposals, "goal_link", Y, later(3))  # after a rejection
    third = proposals.create(SCOPE, "news", N, later(4))  # after an approval
    assert states(proposals) == {
        first.proposal_id: "rejected",
        second.proposal_id: "approved",
        third.proposal_id: "open",
    }
    assert states(proposals, OTHER) == {other.proposal_id: "open"}


def test_create_refuses_the_wording_in_use(
    proposals: QuestionProposals, store: SessionStore
) -> None:
    """The wording in use is the playbook's own until an approved one replaces it; the same
    wording again, in any key order, changes nothing."""
    own = playbook.questions()["goal_link"]
    refused("not_applicable", proposals.create, SCOPE, "goal_link", own, NOW)
    in_use = approved(proposals, "goal_link", X, later(1))
    reordered = dict(reversed(list(X.items())))
    refused("not_applicable", proposals.create, SCOPE, "goal_link", reordered, later(2))
    assert [row["proposal_id"] for row in rows(store)] == [in_use.proposal_id]

    back = proposals.create(SCOPE, "goal_link", own, later(3))  # the playbook's is now a change
    assert proposals.wording(SCOPE)[2] == ("goal_link", own)
    assert back.state == "open"


# ---- the wording in use and its trial --------------------------------------------------


def test_wording_is_the_playbooks_own_without_proposals(proposals: QuestionProposals) -> None:
    assert proposals.wording(SCOPE) == ({}, playbook.PLAYBOOK_VERSION, None)


def test_approval_makes_the_tried_wording_live(proposals: QuestionProposals) -> None:
    created = proposals.create(SCOPE, "goal_link", X, NOW)
    assert proposals.wording(SCOPE) == ({}, playbook.PLAYBOOK_VERSION, ("goal_link", X))

    done = proposals.act(SCOPE, created.proposal_id, "approve", created.revision, None, later(1))
    assert (done.state, done.approved_at, done.updated_at) == (
        "approved",
        stamp(later(1)),
        stamp(later(1)),
    )
    assert (done.created_at, done.rejected_at, done.reason) == (stamp(NOW), None, None)
    assert done.revision != created.revision
    version = playbook.version({"goal_link": X})
    assert version != playbook.PLAYBOOK_VERSION
    assert proposals.wording(SCOPE) == ({"goal_link": X}, version, None)
    assert proposals.wording(OTHER) == ({}, playbook.PLAYBOOK_VERSION, None)


def test_reject_on_an_approved_proposal_restores_the_wording(
    proposals: QuestionProposals,
) -> None:
    """Undo is a rejection of the wording in use: the approval before it is in use again, and
    with none before it the playbook's own."""
    first = approved(proposals, "goal_link", X, NOW)
    second = approved(proposals, "goal_link", Y, later(1))
    assert proposals.wording(SCOPE) == ({"goal_link": Y}, playbook.version({"goal_link": Y}), None)
    assert states(proposals) == {first.proposal_id: "replaced", second.proposal_id: "approved"}

    undone = proposals.act(
        SCOPE, second.proposal_id, "reject", second.revision, "Too vague", later(2)
    )
    assert (undone.state, undone.approved_at, undone.rejected_at, undone.reason) == (
        "rejected",
        stamp(later(1)),
        stamp(later(2)),
        "Too vague",
    )
    assert proposals.wording(SCOPE) == ({"goal_link": X}, playbook.version({"goal_link": X}), None)
    assert states(proposals) == {first.proposal_id: "approved", second.proposal_id: "rejected"}

    proposals.act(SCOPE, first.proposal_id, "reject", first.revision, None, later(3))
    assert proposals.wording(SCOPE) == ({}, playbook.PLAYBOOK_VERSION, None)
    assert set(states(proposals).values()) == {"rejected"}


def test_states_are_derived_per_question(proposals: QuestionProposals) -> None:
    replaced = approved(proposals, "goal_link", X, NOW)
    news = approved(proposals, "news", N, later(1))
    in_use = approved(proposals, "goal_link", Y, later(2))
    dropped = proposals.create(SCOPE, "news", worded("Is `candidate` new to the owner?"), later(3))
    proposals.act(SCOPE, dropped.proposal_id, "reject", dropped.revision, None, later(4))
    tried = proposals.create(SCOPE, "owner_only", X, later(5))

    assert [(record.proposal_id, record.state) for record in proposals.list(SCOPE)] == [
        (tried.proposal_id, "open"),
        (dropped.proposal_id, "rejected"),
        (in_use.proposal_id, "approved"),
        (news.proposal_id, "approved"),
        (replaced.proposal_id, "replaced"),
    ]
    live = {"goal_link": Y, "news": N}
    assert proposals.wording(SCOPE) == (live, playbook.version(live), ("owner_only", X))
    assert all(record.question is None for record in proposals.list(SCOPE))


# ---- acts at a revision ----------------------------------------------------------------


def test_act_refuses_a_moved_revision(proposals: QuestionProposals, store: SessionStore) -> None:
    """A window still showing the open proposal cannot undo the approval made since."""
    created = proposals.create(SCOPE, "goal_link", X, NOW)
    refused(
        "not_applicable",
        proposals.act,
        SCOPE,
        created.proposal_id,
        "approve",
        "0" * 64,
        None,
        later(1),
    )
    assert states(proposals) == {created.proposal_id: "open"}
    done = proposals.act(SCOPE, created.proposal_id, "approve", created.revision, None, later(1))
    before = rows(store)

    for revision in (created.revision, "0" * 64):
        refused(
            "not_applicable",
            proposals.act,
            SCOPE,
            created.proposal_id,
            "reject",
            revision,
            None,
            later(2),
        )
    assert rows(store) == before
    assert proposals.wording(SCOPE)[0] == {"goal_link": X}

    proposals.act(SCOPE, created.proposal_id, "reject", done.revision, None, later(3))
    assert proposals.wording(SCOPE) == ({}, playbook.PLAYBOOK_VERSION, None)


def test_act_refuses_what_a_state_does_not_allow(
    proposals: QuestionProposals, store: SessionStore
) -> None:
    replaced = approved(proposals, "goal_link", X, NOW)
    in_use = approved(proposals, "goal_link", Y, later(1))
    dropped = proposals.create(SCOPE, "news", N, later(2))
    dropped = proposals.act(SCOPE, dropped.proposal_id, "reject", dropped.revision, None, later(3))
    before = rows(store)

    for record, action in (
        (in_use, "approve"),
        (replaced, "approve"),
        (dropped, "approve"),
        (replaced, "reject"),
        (dropped, "reject"),
    ):
        refused(
            "not_applicable",
            proposals.act,
            SCOPE,
            record.proposal_id,
            action,
            record.revision,
            None,
            later(4),
        )
    assert rows(store) == before


MALFORMED = {
    "edit": {"action": "edit"},
    "withdraw": {"action": "withdraw"},
    "capital": {"action": "Approve"},
    "short_revision": {"revision": "abc"},
    "upper_revision": {"revision": "A" * 64},
    "revision_number": {"revision": 5},
    "reason_with_approve": {"action": "approve", "reason": "Fine"},
    "empty_reason": {"reason": ""},
    "blank_reason": {"reason": "   "},
    "two_lines": {"reason": "Too\nvague"},
    "trailing_newline": {"reason": "Too vague\n"},
    "line_separator": {"reason": "Too\u2028vague"},
    "lone_surrogate": {"reason": "Too \ud800"},
    "long_reason": {"reason": "x" * 201},
    "long_in_utf16": {"reason": "\U0001f600" * 101},
    "reason_number": {"reason": 5},
}


@pytest.mark.parametrize("change", MALFORMED.values(), ids=MALFORMED.keys())
def test_act_refuses_a_malformed_request(
    proposals: QuestionProposals, store: SessionStore, change: dict[str, object]
) -> None:
    created = proposals.create(SCOPE, "goal_link", X, NOW)
    request: dict[str, object] = {
        "action": "reject",
        "revision": created.revision,
        "reason": None,
        **change,
    }
    before = rows(store)
    refused(
        "invalid_request",
        proposals.act,
        SCOPE,
        created.proposal_id,
        request["action"],
        request["revision"],
        request["reason"],
        later(1),
    )
    assert rows(store) == before


def test_act_refuses_an_id_the_scope_does_not_hold(proposals: QuestionProposals) -> None:
    created = proposals.create(SCOPE, "goal_link", X, NOW)
    for action in ("approve", "reject"):
        for scope, proposal_id in ((SCOPE, str(uuid.uuid4())), (OTHER, created.proposal_id)):
            refused(
                "unknown_item",
                proposals.act,
                scope,
                proposal_id,
                action,
                created.revision,
                None,
                later(1),
            )
    refused("invalid_request", proposals.act, SCOPE, "nope", "reject", created.revision)
    refused("invalid_request", proposals.get, SCOPE, created.proposal_id.upper())
    assert states(proposals) == {created.proposal_id: "open"}


@pytest.mark.parametrize(
    ("reason", "kept"),
    [
        ("  Too vague  ", "Too vague"),
        ("x" * 200, "x" * 200),
        ("\U0001f600" * 100, "\U0001f600" * 100),  # 200 UTF-16 code units, as a browser counts
        ("言い回しが曖昧", "言い回しが曖昧"),
    ],
)
def test_reject_keeps_a_one_line_reason(
    proposals: QuestionProposals, store: SessionStore, reason: str, kept: str
) -> None:
    created = proposals.create(SCOPE, "goal_link", X, NOW)
    done = proposals.act(SCOPE, created.proposal_id, "reject", created.revision, reason, later(1))
    assert done.reason == kept
    assert [row["reason"] for row in rows(store)] == [kept]


def test_a_racing_act_cannot_slip_between_the_check_and_the_write(
    proposals: QuestionProposals, store: SessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An approval attempted while a rejection holds its check waits on the write lock and is
    refused, instead of succeeding and then being overwritten by the rejection's stale row."""
    created = proposals.create(SCOPE, "goal_link", X, NOW)
    monkeypatch.setattr(storage, "BUSY_TIMEOUT_S", 0.2)
    real_read = module._read
    raced: list[str] = []

    def read(connection: object, scope: str, *, bodies: bool = False) -> object:
        found = real_read(connection, scope, bodies=bodies)  # type: ignore[arg-type]
        if not raced:  # the rejection's own read: an approval races it here
            raced.append("pending")
            try:
                proposals.act(
                    SCOPE, created.proposal_id, "approve", created.revision, None, later(1)
                )
                raced[0] = "approved"
            except DecisionLoopError as error:
                raced[0] = error.code
        return found

    monkeypatch.setattr(module, "_read", read)
    done = proposals.act(SCOPE, created.proposal_id, "reject", created.revision, None, later(2))
    monkeypatch.undo()
    assert raced == ["unavailable"]
    assert done.state == "rejected"
    (row,) = rows(store)
    assert (row["approved_at"], row["rejected_at"]) == (None, stamp(later(2)))


# ---- the inbox item --------------------------------------------------------------------

CLIENT_KEYS = {
    "kind",
    "id",
    "origin",
    "revision",
    "state",
    "attention",
    "title",
    "name",
    "created_at",
    "updated_at",
    "approved_at",
    "rejected_at",
    "reason",
}
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
STAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z")


def client_accepts(item: dict[str, object]) -> bool:
    """`frontend/src/state/decisionsApi.ts::isQuestionProposalItem`, and exactly its keys: one
    item it refuses drops the client's whole inbox."""
    reason = item["reason"]
    return (
        set(item) == CLIENT_KEYS
        and item["kind"] == "question_proposal"
        and isinstance(item["id"], str)
        and UUID.fullmatch(item["id"]) is not None
        and item["origin"] == {"workspace_id": SCOPE, "session_id": None}
        and isinstance(item["revision"], str)
        and re.fullmatch(r"[0-9a-f]{64}", item["revision"]) is not None
        and item["state"] in ("open", "approved", "replaced", "rejected")
        and isinstance(item["attention"], bool)
        and isinstance(item["title"], str)
        and isinstance(item["name"], str)
        and re.fullmatch(r"[a-z_]{1,32}", item["name"]) is not None
        and all(
            isinstance(item[name], str) and STAMP.fullmatch(str(item[name]))
            for name in ("created_at", "updated_at")
        )
        and all(
            item[name] is None or STAMP.fullmatch(str(item[name]))
            for name in ("approved_at", "rejected_at")
        )
        and (
            reason is None
            or (isinstance(reason, str) and 1 <= len(reason.encode("utf-16-le")) // 2 <= 200)
        )
    )


def test_inbox_items_take_the_shape_the_clients_validate(proposals: QuestionProposals) -> None:
    replaced = approved(proposals, "goal_link", X, NOW)
    approved(proposals, "goal_link", Y, later(1))
    dropped = proposals.create(SCOPE, "news", N, later(2))
    proposals.act(
        SCOPE, dropped.proposal_id, "reject", dropped.revision, "\U0001f600" * 100, later(3)
    )
    tried = proposals.create(SCOPE, "sensitive", X, later(4))
    proposals.create(OTHER, "goal_link", X, later(4))

    items = proposals.inbox_items(SCOPE)
    assert all(client_accepts(item) for item in items)
    assert [(item["state"], item["attention"]) for item in items] == [
        ("open", True),
        ("rejected", False),
        ("approved", False),
        ("replaced", False),
    ]
    assert items[0] == {
        "kind": "question_proposal",
        "id": tried.proposal_id,
        "origin": {"workspace_id": SCOPE, "session_id": None},
        "revision": tried.revision,
        "state": "open",
        "attention": True,
        "title": "Question change: A private matter?",
        "name": "sensitive",
        "created_at": stamp(later(4)),
        "updated_at": stamp(later(4)),
        "approved_at": None,
        "rejected_at": None,
        "reason": None,
    }
    assert items[-1] == QuestionProposals.item(proposals.get(SCOPE, replaced.proposal_id))
    assert items[-1]["title"] == "Question change: Linked to a goal?"
    assert [item["origin"] for item in proposals.inbox_items(OTHER)] == [
        {"workspace_id": OTHER, "session_id": None}
    ]


# ---- failing closed ----------------------------------------------------------------------------


def _unsealed(store: SessionStore, scope: str, proposal_id: str, plain: bytes) -> None:
    execute(store, "UPDATE decision_proposals SET body=? WHERE proposal_id=?", (plain, proposal_id))


def _other_row(store: SessionStore, scope: str, proposal_id: str, plain: bytes) -> None:
    sealed = protection.protect(plain, context=CONTEXT + OTHER.encode() + proposal_id.encode())
    execute(
        store, "UPDATE decision_proposals SET body=? WHERE proposal_id=?", (sealed, proposal_id)
    )


def _other_text(store: SessionStore, scope: str, proposal_id: str, plain: bytes) -> None:
    sealed = protection.protect(
        canonical_json(Y).encode(), context=CONTEXT + scope.encode() + proposal_id.encode()
    )
    execute(
        store, "UPDATE decision_proposals SET body=? WHERE proposal_id=?", (sealed, proposal_id)
    )


def _refused_wording(store: SessionStore, scope: str, proposal_id: str, plain: bytes) -> None:
    """Sealed to its row and hashed to match, but a wording the check refuses."""
    bad = canonical_json({**X, "type": "score"}).encode()
    sealed = protection.protect(bad, context=CONTEXT + scope.encode() + proposal_id.encode())
    execute(
        store,
        "UPDATE decision_proposals SET body=?,proposed_sha256=? WHERE proposal_id=?",
        (sealed, sha256_hex(bad.decode()), proposal_id),
    )


BROKEN = {
    "unsealed": _unsealed,
    "sealed_to_another_row": _other_row,
    "another_text": _other_text,
    "refused_wording": _refused_wording,
}


@pytest.mark.parametrize("which", ["in_use", "on_trial"])
@pytest.mark.parametrize("breaks", BROKEN.values(), ids=BROKEN.keys())
def test_a_body_that_does_not_open_fails_closed(
    proposals: QuestionProposals,
    store: SessionStore,
    breaks: Callable[[SessionStore, str, str, bytes], None],
    which: str,
) -> None:
    """A wording that does not open, match its hash or pass the check reads as none at all: the
    playbook's own, at the playbook's version, no trial. It still lists, so the user can reject
    it, which brings the rest back; it cannot be opened or approved."""
    in_use = approved(proposals, "goal_link", X, NOW)
    tried = proposals.create(SCOPE, "news", N, later(1))
    broken = in_use if which == "in_use" else tried
    breaks(store, SCOPE, broken.proposal_id, canonical_json(X if which == "in_use" else N).encode())

    assert proposals.wording(SCOPE) == ({}, playbook.PLAYBOOK_VERSION, None)
    refused("unknown_item", proposals.get, SCOPE, broken.proposal_id)
    assert states(proposals) == {tried.proposal_id: "open", in_use.proposal_id: "approved"}
    if which == "on_trial":
        refused(
            "unknown_item",
            proposals.act,
            SCOPE,
            tried.proposal_id,
            "approve",
            tried.revision,
            None,
            later(2),
        )
        assert states(proposals)[tried.proposal_id] == "open"

    current = {record.proposal_id: record for record in proposals.list(SCOPE)}[broken.proposal_id]
    proposals.act(SCOPE, broken.proposal_id, "reject", current.revision, None, later(3))
    if which == "in_use":
        assert proposals.wording(SCOPE) == ({}, playbook.PLAYBOOK_VERSION, ("news", N))
    else:
        assert proposals.wording(SCOPE) == (
            {"goal_link": X},
            playbook.version({"goal_link": X}),
            None,
        )


def test_rows_that_do_not_read_back_are_left_out(
    proposals: QuestionProposals, store: SessionStore
) -> None:
    """A row this build cannot read (a question it does not know, an id, a stamp or a reason
    out of shape) is in no read and holds nothing open, so it can never stall the user; it is
    left as it is."""
    insert(store, 1, name="retired_question")
    insert(store, 2, proposal_id="00000000-0000-4000-8000-00000000000A")
    insert(store, 3, created_at="2026-10-22 09:00:03.000Z")
    insert(store, 4, approved_at="2026-13-22T09:00:04.000Z")
    insert(store, 5, rejected_at=stamp(NOW), reason="\U0001f600" * 101)  # 202 UTF-16 units
    insert(store, 6, updated_at="2026-10-22 09:00:06.000Z")  # space instead of T
    insert(store, 7, rejected_at="2026-10-22 09:00:07.000Z")  # space instead of T
    before = rows(store)
    assert len(before) == 7

    assert proposals.list(SCOPE) == []
    assert proposals.inbox_items(SCOPE) == []
    assert proposals.wording(SCOPE) == ({}, playbook.PLAYBOOK_VERSION, None)
    created = proposals.create(SCOPE, "goal_link", X, later(1))
    assert [record.proposal_id for record in proposals.list(SCOPE)] == [created.proposal_id]
    assert [row for row in rows(store) if row["proposal_id"] != created.proposal_id] == before


# ---- pruning -----------------------------------------------------------------------------------


def test_a_scope_keeps_its_newest_rows_pruning_rejected_or_replaced(
    proposals: QuestionProposals, store: SessionStore
) -> None:
    """The oldest rejected or replaced row makes room; a row this build cannot read is neither
    counted nor pruned, and another scope's rows count apart."""
    (unreadable,) = insert(store, 0, name="retired_question", rejected_at=stamp(NOW))
    replaced = approved(proposals, "goal_link", X, later(1))
    news = approved(proposals, "news", N, later(2))
    in_use = approved(proposals, "goal_link", Y, later(3))
    dropped = insert(  # rejected rows created 1,000 seconds after NOW and on
        store, *range(1000, 1000 + MAX_PROPOSALS - 3), name="owner_only", rejected_at=stamp(NOW)
    )
    other = proposals.create(OTHER, "goal_link", X, later(60))
    assert len(rows(store)) == MAX_PROPOSALS + 2

    newest = proposals.create(SCOPE, "fits_now", X, later(61))
    held = {record.proposal_id for record in proposals.list(SCOPE)}
    assert len(held) == MAX_PROPOSALS
    assert replaced.proposal_id not in held  # the oldest rejected or replaced row
    assert {news.proposal_id, in_use.proposal_id, newest.proposal_id} <= held

    proposals.act(SCOPE, newest.proposal_id, "reject", newest.revision, None, later(62))
    again = proposals.create(SCOPE, "fits_now", Y, later(63))
    held = {record.proposal_id for record in proposals.list(SCOPE)}
    assert len(held) == MAX_PROPOSALS
    assert dropped[0] not in held and dropped[1] in held
    assert {news.proposal_id, in_use.proposal_id, newest.proposal_id, again.proposal_id} <= held
    assert unreadable in {row["proposal_id"] for row in rows(store)}
    assert states(proposals, OTHER) == {other.proposal_id: "open"}
    live = {"goal_link": Y, "news": N}
    assert proposals.wording(SCOPE) == (live, playbook.version(live), ("fits_now", Y))


# ---- faults ------------------------------------------------------------------------------------

OPERATIONS: dict[str, Callable[[QuestionProposals, ProposalRecord], object]] = {
    "create": lambda proposals, _record: proposals.create(OTHER, "news", N, later(5)),
    "wording": lambda proposals, _record: proposals.wording(SCOPE),
    "get": lambda proposals, record: proposals.get(SCOPE, record.proposal_id),
    "approve": lambda proposals, record: proposals.act(
        SCOPE, record.proposal_id, "approve", record.revision, None, later(5)
    ),
    "reject": lambda proposals, record: proposals.act(
        SCOPE, record.proposal_id, "reject", record.revision, None, later(5)
    ),
    "list": lambda proposals, _record: proposals.list(SCOPE),
    "inbox_items": lambda proposals, _record: proposals.inbox_items(SCOPE),
}


@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize("fault", ["private", "store"])
def test_a_store_that_does_not_work_reads_unavailable(
    proposals: QuestionProposals,
    store: SessionStore,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    fault: str,
) -> None:
    record = proposals.create(SCOPE, "goal_link", X, NOW)
    before = rows(store)

    def not_private(store: object) -> None:
        raise protection.ProtectionError()

    def broken(self: SessionStore, **kwargs: object) -> object:
        raise StateError("busy")

    if fault == "private":
        monkeypatch.setattr("viki_agent.decision_loop.proposals.require_private_store", not_private)
    else:
        monkeypatch.setattr(SessionStore, "_connection", broken)
    refused("unavailable", OPERATIONS[operation], proposals, record)
    monkeypatch.undo()
    assert rows(store) == before


def test_sealing_that_does_not_work_reads_unavailable(
    proposals: QuestionProposals, store: SessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sealing that fails outright is a fault, not a wording that fails closed."""
    record = proposals.create(SCOPE, "goal_link", X, NOW)
    before = rows(store)

    def refuse(data: bytes, *, context: bytes) -> bytes:
        raise protection.ProtectionError()

    monkeypatch.setattr(protection, "unprotect", refuse)
    for operation in ("wording", "get", "approve"):
        refused("unavailable", OPERATIONS[operation], proposals, record)
    monkeypatch.setattr(protection, "protect", refuse)
    refused("unavailable", OPERATIONS["create"], proposals, record)
    monkeypatch.undo()
    assert rows(store) == before


def test_without_a_time_the_wall_clock_stamps(store: SessionStore) -> None:
    wall = int(datetime(2026, 10, 22, 12, 34, 56, tzinfo=UTC).timestamp()) * 10**9 + 789_000_000
    proposals = QuestionProposals(store, wall=lambda: wall)
    created = proposals.create(SCOPE, "goal_link", X)
    done = proposals.act(SCOPE, created.proposal_id, "approve", created.revision)
    assert (created.created_at, done.approved_at) == (
        "2026-10-22T12:34:56.789Z",
        "2026-10-22T12:34:56.789Z",
    )
