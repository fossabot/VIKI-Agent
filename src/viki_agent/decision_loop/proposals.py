"""Wording proposals for Decision's yes/no questions, sealed in the app's own store.

The user may propose a new wording for one of `playbook.TUNABLE`'s questions. `create` takes
only a wording `playbook.check_question` accepts and seals its canonical JSON into
`decision_proposals` under a context that names the row's scope and id, so a body copied into
another row does not open; `proposed_sha256` holds that JSON's hash. No column says what state a
proposal is in: each read derives it from the scope's rows (`_states`):

- open: neither approved nor rejected. A scope holds one at a time, and `wording` names it as the
  trial;
- approved: approved, not rejected, and the newest approval of its question: the wording in use;
- replaced: approved, not rejected, while a newer approval of its question is in use;
- rejected: rejected, an undone approval too, which brings the approval before it back into use.

`wording` answers the wording in use, its `playbook.version` and the trial. When a row it needs
does not open, does not hash to `proposed_sha256` or fails the check, it fails closed: the
playbook's own wording, version "1" and no trial. `act` approves an open proposal, or rejects an
open or approved one, at the revision the user saw: a digest of the text's hash and its
approval and rejection stamps, so every act moves it. The check and the write share one write
transaction, as `create`'s do. A scope keeps at most `MAX_PROPOSALS` rows that read back:
`create` deletes the oldest rejected or replaced ones to make room. A row this build cannot read
back (a question it does not tune, or an id, stamp or reason out of shape) is left out of every
read and left as it is, so it holds nothing open.

Each method checks its arguments, then the store's privacy, then runs its transactions; bodies
are sealed before a write transaction and opened after a read one. Refusals are
`DecisionLoopError` codes: invalid_request, unknown_item, not_applicable, and unavailable for
store and protection failures. Nothing here logs.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import sqlite3
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, NamedTuple

from viki_agent import protection
from viki_agent.api_json import unique_object
from viki_agent.automation.routines import RoutineError, _uuid, parse_stamp, stamp
from viki_agent.conversations.drafts import require_private_store
from viki_agent.decision_loop import playbook
from viki_agent.decision_loop.contracts import DecisionLoopError, canonical_json, sha256_hex
from viki_agent.storage import SessionStore, StateError

MAX_PROPOSALS = 50  # rows per scope
MAX_REASON_CHARS = 200  # UTF-16 code units, the length a browser measures
ACTIONS = ("approve", "reject")

_CONTEXT = b"VIKI decision proposal v1\x00"
_COLUMNS = "proposal_id,name,proposed_sha256,created_at,updated_at,approved_at,rejected_at,reason"
_REVISION = re.compile(r"[0-9a-f]{64}")
# A reason is one line: no control character, no Unicode line or paragraph separator, and no
# lone surrogate, which the store cannot encode.
_NOT_IN_REASON = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029\ud800-\udfff]")
# The hash of each tunable question's own wording, the one in use until an approval replaces it.
_OWN = {name: sha256_hex(canonical_json(playbook.questions()[name])) for name in playbook.TUNABLE}

Wording = dict[str, dict[str, object]]  # question name -> its wording in use
OnTrial = tuple[str, dict[str, object]]  # the open proposal's question name and wording


@dataclass(frozen=True)
class ProposalRecord:
    """One proposal as read back, its `state` derived from its scope's rows at that read.
    `question` is the proposed wording, which only `create` and `get` fill."""

    proposal_id: str
    scope: str
    name: str
    proposed_sha256: str
    state: str
    created_at: str
    updated_at: str
    approved_at: str | None
    rejected_at: str | None
    reason: str | None
    question: Mapping[str, object] | None = None

    @property
    def revision(self) -> str:
        """The revision an act names (see the module docstring)."""
        return _revision(self.proposed_sha256, self.approved_at, self.rejected_at)


class _Row(NamedTuple):
    proposal_id: str
    name: str
    proposed_sha256: str
    created_at: str
    updated_at: str
    approved_at: str | None
    rejected_at: str | None
    reason: str | None
    body: bytes | None  # None unless read with its body


class QuestionProposals:
    """The `decision_proposals` rows of each workspace scope."""

    def __init__(self, store: SessionStore, *, wall: Callable[[], int] = time.time_ns) -> None:
        self.store = store
        self._wall = wall

    @contextlib.contextmanager
    def _transaction(self, *, write: bool) -> Iterator[sqlite3.Connection]:
        try:
            require_private_store(self.store)
            with self.store._connection(write=write) as connection:
                yield connection
        except StateError, protection.ProtectionError:
            raise DecisionLoopError("unavailable") from None

    def create(
        self, scope: str, name: str, question: object, now: datetime | None = None
    ) -> ProposalRecord:
        """Seal a proposed wording of question `name` as the scope's open proposal; answer it.

        invalid_request unless `playbook.check_question` takes it; not_applicable while the
        scope holds an open proposal, or when it is the wording in use for `name` in any key
        order. *now* defaults to the wall clock.
        """
        scope = _id(scope)
        try:
            checked = playbook.check_question(name, question)
        except ValueError:
            raise DecisionLoopError("invalid_request") from None
        text = canonical_json(checked)
        proposed_sha256 = sha256_hex(text)
        proposal_id = str(uuid.uuid4())
        body = _seal(_context(scope, proposal_id), text.encode("ascii"))
        stamped = self._stamp(now)
        with self._transaction(write=True) as connection:
            rows = _read(connection, scope)
            states = _states(rows)
            if "open" in states.values():
                raise DecisionLoopError("not_applicable")
            in_use = _in_use(rows).get(name)
            if (_OWN[name] if in_use is None else in_use.proposed_sha256) == proposed_sha256:
                raise DecisionLoopError("not_applicable")
            _prune(connection, rows, states)
            connection.execute(
                f"INSERT INTO decision_proposals ({_COLUMNS},scope,body) "
                "VALUES (?,?,?,?,?,NULL,NULL,NULL,?,?)",
                (proposal_id, name, proposed_sha256, stamped, stamped, scope, body),
            )
        return ProposalRecord(
            proposal_id=proposal_id,
            scope=scope,
            name=name,
            proposed_sha256=proposed_sha256,
            state="open",
            created_at=stamped,
            updated_at=stamped,
            approved_at=None,
            rejected_at=None,
            reason=None,
            question=checked,
        )

    def wording(self, scope: str) -> tuple[Wording, str, OnTrial | None]:
        """The scope's wording in use, the version it is asked under, and the open proposal's
        question name and wording, if any. A row this needs that does not open fails closed:
        the playbook's own wording, version "1" and no trial."""
        scope = _id(scope)
        with self._transaction(write=False) as connection:
            rows = _read(connection, scope, bodies=True)
        states = _states(rows)
        live: Wording = {}
        trial: OnTrial | None = None
        for row in rows:
            state = states[row.proposal_id]
            if state not in ("open", "approved"):
                continue
            question = _question(scope, row)
            if question is None:
                return {}, playbook.PLAYBOOK_VERSION, None
            if state == "approved":
                live[row.name] = question
            elif trial is None:
                trial = (row.name, question)
        return live, playbook.version(live), trial

    def get(self, scope: str, proposal_id: str) -> ProposalRecord:
        """One proposal of the scope with its wording; unknown_item when the scope holds none
        by that id, or its wording does not open, hash to its row or pass the check."""
        scope = _id(scope)
        proposal_id = _id(proposal_id)
        with self._transaction(write=False) as connection:
            rows = _read(connection, scope, bodies=True)
        row = next((row for row in rows if row.proposal_id == proposal_id), None)
        question = None if row is None else _question(scope, row)
        if row is None or question is None:
            raise DecisionLoopError("unknown_item")
        return _record(scope, row, _states(rows)[proposal_id], question)

    def act(
        self,
        scope: str,
        proposal_id: str,
        action: str,
        revision: str,
        reason: str | None = None,
        now: datetime | None = None,
    ) -> ProposalRecord:
        """Approve an open proposal, or reject an open or approved one, at the `revision` the
        user saw; answer it as the write left it, without its wording.

        The first thing wrong is refused, in this order: a malformed request (invalid_request;
        a reason comes with a rejection alone, one line of 1 to `MAX_REASON_CHARS` UTF-16 code
        units once stripped, and is kept stripped); an id the scope does not hold, or for an
        approval a wording that does not open (unknown_item); then a revision that moved or a
        state the action does not fit (not_applicable). *now* defaults to the wall clock.
        """
        scope = _id(scope)
        proposal_id = _id(proposal_id)
        if (
            action not in ACTIONS
            or not isinstance(revision, str)
            or not _REVISION.fullmatch(revision)
            or not (reason is None or (action == "reject" and _valid_reason(reason)))
        ):
            raise DecisionLoopError("invalid_request")
        if action == "approve":
            self.get(scope, proposal_id)  # only a wording that opens can be put in use
        allowed = ("open",) if action == "approve" else ("open", "approved")
        stamped = self._stamp(now)
        with self._transaction(write=True) as connection:
            rows = _read(connection, scope)
            row = next((row for row in rows if row.proposal_id == proposal_id), None)
            if row is None:
                raise DecisionLoopError("unknown_item")
            moved = _revision(row.proposed_sha256, row.approved_at, row.rejected_at) != revision
            if moved or _states(rows)[proposal_id] not in allowed:
                raise DecisionLoopError("not_applicable")
            if action == "approve":
                written = row._replace(approved_at=stamped, updated_at=stamped)
            else:
                kept = None if reason is None else reason.strip()
                written = row._replace(rejected_at=stamped, reason=kept, updated_at=stamped)
            connection.execute(
                "UPDATE decision_proposals SET approved_at=?,rejected_at=?,reason=?,updated_at=? "
                "WHERE proposal_id=?",
                (
                    written.approved_at,
                    written.rejected_at,
                    written.reason,
                    written.updated_at,
                    proposal_id,
                ),
            )
        rows = [written if found.proposal_id == proposal_id else found for found in rows]
        return _record(scope, written, _states(rows)[proposal_id])

    def inbox_items(self, scope: str) -> list[dict[str, object]]:
        """The scope's proposals as Approvals inbox items, newest first."""
        return [self.item(record) for record in self.list(scope)]

    @staticmethod
    def item(record: ProposalRecord) -> dict[str, object]:
        """`record` as an Approvals inbox item, in exactly the shape the client's
        `isQuestionProposalItem` (`frontend/src/state/decisionsApi.ts`) takes."""
        return {
            "kind": "question_proposal",
            "id": record.proposal_id,
            "origin": {"workspace_id": record.scope, "session_id": None},
            "revision": record.revision,
            "state": record.state,
            "attention": record.state == "open",
            "title": f"Question change: {playbook.LABELS[record.name]}",
            "name": record.name,
            "created_at": record.created_at,
            "updated_at": record.updated_at,
            "approved_at": record.approved_at,
            "rejected_at": record.rejected_at,
            "reason": record.reason,
        }

    def list(self, scope: str) -> list[ProposalRecord]:
        """The scope's proposals, newest first, without their wording."""
        scope = _id(scope)
        with self._transaction(write=False) as connection:
            rows = _read(connection, scope)
        states = _states(rows)
        return [_record(scope, row, states[row.proposal_id]) for row in rows]

    def _stamp(self, now: datetime | None) -> str:
        if now is None:
            now = datetime.fromtimestamp(self._wall() / 1_000_000_000, UTC)
        return stamp(now)


def _id(value: object) -> str:
    """A canonical 36-character UUID string, so it is ASCII inside a sealing context."""
    try:
        return _uuid(value)
    except RoutineError:
        raise DecisionLoopError("invalid_request") from None


def _context(scope: str, proposal_id: str) -> bytes:
    return _CONTEXT + scope.encode("ascii") + proposal_id.encode("ascii")


def _revision(proposed_sha256: str, approved_at: str | None, rejected_at: str | None) -> str:
    return sha256_hex(canonical_json([proposed_sha256, approved_at, rejected_at]))


def _one_line(text: str) -> bool:
    """1 to `MAX_REASON_CHARS` UTF-16 code units, none of them in `_NOT_IN_REASON`."""
    return (
        _NOT_IN_REASON.search(text) is None
        and 1 <= len(text.encode("utf-16-le")) // 2 <= MAX_REASON_CHARS
    )


def _valid_reason(reason: object) -> bool:
    """A reason as the user gives it: one line once stripped. The characters are checked on
    the whole text first, so a line break stripped from either end still refuses it."""
    return (
        isinstance(reason, str)
        and _NOT_IN_REASON.search(reason) is None
        and _one_line(reason.strip())
    )


def _read(connection: sqlite3.Connection, scope: str, *, bodies: bool = False) -> list[_Row]:
    """The scope's rows that read back, newest first, with their bodies when *bodies* is set."""
    found = connection.execute(
        f"SELECT {_COLUMNS},{'body' if bodies else 'NULL'} FROM decision_proposals "
        "WHERE scope=? ORDER BY created_at DESC,proposal_id DESC",
        (scope,),
    ).fetchall()
    return [row for row in map(_row, found) if row is not None]


def _row(values: Sequence[Any]) -> _Row | None:
    """A stored row, or None when it does not read back: a question this build does not tune,
    or an id, stamp or reason out of the shape the client's inbox takes (one item it refuses
    drops the whole inbox)."""
    row = _Row(*values)
    try:
        _uuid(row.proposal_id)
        for value in (row.created_at, row.updated_at, row.approved_at, row.rejected_at):
            if value is not None:
                parse_stamp(value)
    except RoutineError, ValueError:
        return None
    if row.name not in playbook.TUNABLE or not (row.reason is None or _one_line(row.reason)):
        return None
    return row


def _approval(row: _Row) -> tuple[str, str, str]:
    """The order of approvals of one question: by approval, then creation, then id."""
    return row.approved_at or "", row.created_at, row.proposal_id


def _in_use(rows: Sequence[_Row]) -> dict[str, _Row]:
    """Each question's row in use: of its approved rows not rejected, the newest approval."""
    in_use: dict[str, _Row] = {}
    for row in rows:
        if row.approved_at is not None and row.rejected_at is None:
            held = in_use.get(row.name)
            if held is None or _approval(row) > _approval(held):
                in_use[row.name] = row
    return in_use


def _states(rows: Sequence[_Row]) -> dict[str, str]:
    """Each row's state by its id (see the module docstring)."""
    in_use = _in_use(rows)

    def state(row: _Row) -> str:
        if row.rejected_at is not None:
            return "rejected"
        if row.approved_at is None:
            return "open"
        return "approved" if in_use[row.name].proposal_id == row.proposal_id else "replaced"

    return {row.proposal_id: state(row) for row in rows}


def _prune(connection: sqlite3.Connection, rows: Sequence[_Row], states: Mapping[str, str]) -> None:
    """Delete the oldest rejected or replaced `rows` until one more fits in `MAX_PROPOSALS`.
    With no row open, at most one row of each tunable question is not one of them, so enough
    are."""
    excess = len(rows) + 1 - MAX_PROPOSALS
    if excess <= 0:
        return
    spare = [
        row.proposal_id
        for row in reversed(rows)
        if states[row.proposal_id] in ("rejected", "replaced")
    ]
    connection.executemany(
        "DELETE FROM decision_proposals WHERE proposal_id=?",
        [(proposal_id,) for proposal_id in spare[:excess]],
    )


def _record(
    scope: str, row: _Row, state: str, question: Mapping[str, object] | None = None
) -> ProposalRecord:
    return ProposalRecord(
        proposal_id=row.proposal_id,
        scope=scope,
        name=row.name,
        proposed_sha256=row.proposed_sha256,
        state=state,
        created_at=row.created_at,
        updated_at=row.updated_at,
        approved_at=row.approved_at,
        rejected_at=row.rejected_at,
        reason=row.reason,
        question=question,
    )


def _question(scope: str, row: _Row) -> dict[str, object] | None:
    """The wording a row seals, or None when its body does not open, does not hash to its
    `proposed_sha256` or no longer passes `playbook.check_question`."""
    plain = _open(_context(scope, row.proposal_id), row.body)
    if plain is None or hashlib.sha256(plain).hexdigest() != row.proposed_sha256:
        return None
    try:
        return playbook.check_question(row.name, json.loads(plain, object_pairs_hook=unique_object))
    except ValueError, RecursionError:
        return None


def _seal(context: bytes, plain: bytes) -> bytes:
    try:
        return protection.protect(plain, context=context)
    except protection.ProtectionError:
        raise DecisionLoopError("unavailable") from None


def _open(context: bytes, sealed: bytes | None) -> bytes | None:
    """The plain bytes of a stored body, or None when this row's body does not open.

    A bad body and a protection that cannot work at all refuse alike; as in `agenda`, a probe
    sealed and opened under the same context tells them apart: when the probe fails too,
    sealing is unavailable.
    """
    if sealed is None:
        return None
    try:
        return protection.unprotect(sealed, context=context)
    except protection.ProtectionError:
        pass
    try:
        protection.unprotect(protection.protect(b"{}", context=context), context=context)
    except protection.ProtectionError:
        raise DecisionLoopError("unavailable") from None
    return None
