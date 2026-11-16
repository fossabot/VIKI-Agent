"""Vault-change proposals Decision files for the user, sealed in the app's own store.

Decision proposes three kinds of vault upkeep (`contracts.CHANGES`): link note `ref` to note
`other` (note_link), mark note `ref` as replaced by the newer note `other` (note_superseded), or
move the ticked tasks of `ref` into the note `archive` (tasks_ticked). Each proposal is a
`decision_proposals` row named for its kind, beside `proposals.QuestionProposals`' rows; each
class reads its own names only. `proposed_sha256` holds the item key, and the body seals the
canonical JSON of the change (`archive`, `episode_id`, `item_key`, `kind`, `other`, `ref`) under
a context that names the row's scope and id, so a body copied into another row does not open.
Each ref a change names passes `vault_guard.writable`, which also refuses the characters that
would break a draft's quoting or its lines.

A row reads back when its id, stamps and reason are in shape and its body opens to a change
`file` takes, of the kind its name says and with the key its column holds. Any other row is left
out of `inbox_items`, `held`'s open targets and `act`; its key is still held, it counts toward
both caps below, and making room deletes it as it would a row that reads back. A row's state
comes from its stamps: open, approved (a draft was made) or rejected.

`file` refuses a key that a change row of the scope holds, in any state. It files at most
`limit` change rows a local day across every scope, counting them in the write transaction that
inserts, and adds a row only when its scope then holds at most `MAX_CHANGES` change rows: to make
room it deletes the scope's oldest approved rows created before that day, then its oldest
rejected ones. `act` approves an open change, answers an approved one's draft again without a
write, or rejects an open one, at the revision the user saw: a digest of the key and the two
stamps, which each approval and rejection moves. Its check and its write share one write
transaction. `change_draft` words the fixed request for a chat to make the change, with refs and
stems filled in.

Each method checks its arguments, then the store's privacy, then runs its transactions; bodies
are sealed before a write transaction and opened after a read one. Refusals are
`DecisionLoopError` codes: invalid_request, unknown_item, not_applicable, limit, and unavailable
for store and protection failures. Nothing here logs.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import time
import uuid
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any, NamedTuple

from viki_agent import protection
from viki_agent.api_json import unique_object
from viki_agent.automation.routines import RoutineError, _uuid, parse_stamp
from viki_agent.conversations.drafts import require_private_store
from viki_agent.decision_loop.agenda import _is_key, _key, _require, _seal, _stamp
from viki_agent.decision_loop.contracts import (
    _LIMITS,
    CHANGES,
    PAIRED,
    DecisionLoopError,
    canonical_json,
)
from viki_agent.decision_loop.missions import _day
from viki_agent.decision_loop.proposals import (
    ACTIONS,
    _id,
    _one_line,
    _open,
    _revision,
    _valid_reason,
)
from viki_agent.decision_loop.vault_guard import writable
from viki_agent.storage import SessionStore, StateError

MAX_CHANGES = 50  # change rows per scope

_CONTEXT = b"VIKI decision change v1\x00"
_COLUMNS = "proposal_id,name,proposed_sha256,created_at,updated_at,approved_at,rejected_at,reason"
_FIELDS = frozenset({"archive", "episode_id", "item_key", "kind", "other", "ref"})
_BODY_BYTES = 8192  # the most a `decision_proposals` body holds
_NAMES = json.dumps(CHANGES)
_NAMED = "name IN (SELECT value FROM json_each(?))"
# The kinds whose open row holds its target: a note marked replaced, a tasks file archived.
_TARGETED = ("note_superseded", "tasks_ticked")
_NAME_CHARS = 60  # of each note or project name in a title
_KEEP = "Change no other line, and keep every word already there as it is."
# (action, state) pairs an act takes: approve an open change, draft an approved one again,
# reject an open one.
_ACTS = frozenset({("approve", "open"), ("approve", "approved"), ("reject", "open")})


@dataclass(frozen=True)
class ChangeRecord:
    """One change proposal as read back, its `state` and `revision` from its stamps. `other` is
    the second note of a paired kind and `archive` the note a tasks_ticked change moves to; each
    is None where the kind names none."""

    proposal_id: str
    scope: str
    kind: str
    ref: str
    other: str | None
    archive: str | None
    item_key: str
    episode_id: str
    state: str
    revision: str
    created_at: str
    updated_at: str
    approved_at: str | None
    rejected_at: str | None
    reason: str | None


@dataclass(frozen=True)
class Held:
    """What a scope's change rows hold back from filing on a day: the key of each, in any state,
    the (kind, ref) of each open note_superseded or tasks_ticked change, and `room`, whether
    `file` could make room for one more of the scope's rows that day."""

    keys: frozenset[str]
    open_targets: frozenset[tuple[str, str]]
    room: bool


class _Row(NamedTuple):
    proposal_id: str
    name: str
    proposed_sha256: str
    created_at: str
    updated_at: str
    approved_at: str | None
    rejected_at: str | None
    reason: str | None
    body: bytes


class ChangeProposals:
    """The change rows of `decision_proposals`, in every workspace scope."""

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

    def file(
        self,
        scope: str,
        *,
        kind: str,
        ref: str,
        other: str | None,
        archive: str | None,
        item_key: str,
        episode_id: str,
        day: str,
        limit: int,
        now: datetime | None = None,
    ) -> ChangeRecord:
        """Seal a change as an open proposal of the scope, filed on the local day `day`
        ("YYYY-MM-DD"); answer it.

        invalid_request for an argument out of shape: a kind not in `CHANGES`; refs that do not
        fit the kind (a paired kind names `ref` and `other`, tasks_ticked `ref` and `archive`,
        each one `writable` takes and the second one not `ref`; the ref the kind
        does not name is None); an id or key out of shape; a limit the settings do not offer
        (0 to 10); *now*, the wall clock by default, outside `day`; or a sealed body over 8,192
        bytes. Then, in one write transaction: not_applicable when a change row of the scope
        holds `item_key`; limit when the change rows created on `day` in every scope number
        `limit` or more, or when the scope cannot make room (see the module docstring).
        """
        scope = _id(scope)
        _require(_second(kind, ref, other, archive) is not None)
        item_key = _key(item_key)
        episode_id = _id(episode_id)
        start, end = _day(day)
        _require(type(limit) is int and limit in _LIMITS)
        stamped = self._now(now)
        _require(start <= stamped < end)
        proposal_id = str(uuid.uuid4())
        plain = canonical_json(
            {
                "archive": archive,
                "episode_id": episode_id,
                "item_key": item_key,
                "kind": kind,
                "other": other,
                "ref": ref,
            }
        )
        body = _seal(_context(scope, proposal_id), plain.encode("ascii"), _BODY_BYTES)
        with self._transaction(write=True) as connection:
            if _holds(connection, scope, item_key):
                raise DecisionLoopError("not_applicable")
            if _filed_on(connection, (start, end)) >= limit:
                raise DecisionLoopError("limit")
            _make_room(connection, scope, start)
            connection.execute(
                f"INSERT INTO decision_proposals ({_COLUMNS},scope,body) "
                "VALUES (?,?,?,?,?,NULL,NULL,NULL,?,?)",
                (proposal_id, kind, item_key, stamped, stamped, scope, body),
            )
        return ChangeRecord(
            proposal_id=proposal_id,
            scope=scope,
            kind=kind,
            ref=ref,
            other=other,
            archive=archive,
            item_key=item_key,
            episode_id=episode_id,
            state="open",
            revision=_revision(item_key, None, None),
            created_at=stamped,
            updated_at=stamped,
            approved_at=None,
            rejected_at=None,
            reason=None,
        )

    def held(self, scope: str, day: str) -> Held:
        """What the scope's change rows hold back from filing on the local day `day`
        ("YYYY-MM-DD"), in one read transaction (see `Held`): a row that does not read back holds
        its key but no target, and counts toward room as `file` counts it."""
        scope = _id(scope)
        start, _end = _day(day)
        with self._transaction(write=False) as connection:
            keys = connection.execute(
                f"SELECT proposed_sha256 FROM decision_proposals WHERE scope=? AND {_NAMED}",
                (scope, _NAMES),
            ).fetchall()
            rows = _read(connection, scope, _TARGETED, open_only=True)
            room = _spare(connection, scope, start) is not None
        return Held(
            keys=frozenset(key for (key,) in keys),
            open_targets=frozenset((record.kind, record.ref) for record in _records(scope, rows)),
            room=room,
        )

    def today(self, day: str) -> int:
        """How many change rows were created on the local day `day` ("YYYY-MM-DD") in every
        scope: the count `file` holds against its limit."""
        bounds = _day(day)
        with self._transaction(write=False) as connection:
            return _filed_on(connection, bounds)

    def inbox_items(self, scope: str) -> list[dict[str, object]]:
        """The scope's change proposals that read back, as Approvals inbox items, newest
        first."""
        scope = _id(scope)
        with self._transaction(write=False) as connection:
            rows = _read(connection, scope, CHANGES)
        return [self.item(record) for record in _records(scope, rows)]

    @staticmethod
    def item(record: ChangeRecord) -> dict[str, object]:
        """`record` as an Approvals inbox item, in exactly the shape the client's
        `isChangeProposalItem` (`frontend/src/state/decisionsApi.ts`) takes."""
        return {
            "kind": "change_proposal",
            "id": record.proposal_id,
            "origin": {"workspace_id": record.scope, "session_id": None},
            "revision": record.revision,
            "state": record.state,
            "attention": record.state == "open",
            "title": _title(record),
            "change": record.kind,
            "ref": record.ref,
            "created_at": record.created_at,
            "updated_at": record.updated_at,
            "approved_at": record.approved_at,
            "rejected_at": record.rejected_at,
            "reason": record.reason,
        }

    def act(
        self,
        scope: str,
        proposal_id: str,
        action: str,
        revision: str,
        reason: str | None = None,
        now: datetime | None = None,
    ) -> tuple[ChangeRecord, str | None]:
        """Approve an open change, answer an approved one's draft again, or reject an open one,
        at the `revision` the user saw; answer the change as the act left it, with its draft
        for an approval and None for a rejection.

        The first thing wrong is refused, in this order: a malformed request (invalid_request;
        a reason comes with a rejection alone, one line of 1 to 200 UTF-16 code units once
        stripped, and is kept stripped); an id that names no change of the scope that reads
        back (unknown_item); then a revision that moved or a state the action does not fit
        (not_applicable). *now* defaults to the wall clock.
        """
        scope = _id(scope)
        proposal_id = _id(proposal_id)
        _require(
            action in ACTIONS
            and _is_key(revision)
            and (reason is None or (action == "reject" and _valid_reason(reason)))
        )
        stamped = self._now(now)
        with self._transaction(write=False) as connection:
            rows = _read(connection, scope, CHANGES, proposal_id=proposal_id)
        found = next(iter(_records(scope, rows)), None)
        if found is None:
            raise DecisionLoopError("unknown_item")
        with self._transaction(write=True) as connection:
            current = _current(connection, scope, proposal_id)
            if current is None:
                raise DecisionLoopError("unknown_item")
            record = _restamped(found, *current)
            if record.revision != revision or (action, record.state) not in _ACTS:
                raise DecisionLoopError("not_applicable")
            if record.state == "open":
                if action == "approve":
                    record = _restamped(record, stamped, stamped, None, None)
                else:
                    kept = None if reason is None else reason.strip()
                    record = _restamped(record, stamped, None, stamped, kept)
                connection.execute(
                    "UPDATE decision_proposals SET approved_at=?,rejected_at=?,reason=?,"
                    "updated_at=? WHERE proposal_id=?",
                    (
                        record.approved_at,
                        record.rejected_at,
                        record.reason,
                        record.updated_at,
                        proposal_id,
                    ),
                )
        if action == "reject":
            return record, None
        return record, change_draft(record.kind, record.ref, record.other, record.archive)

    def _now(self, now: datetime | None) -> str:
        """*now*, or the wall clock, as a stamp; invalid_request unless an aware instant."""
        if now is None:
            now = datetime.fromtimestamp(self._wall() / 1_000_000_000, UTC)
        return _stamp(now)


def change_draft(kind: str, ref: str, other: str | None, archive: str | None) -> str:
    """The fixed request that asks a chat to make a change, with its refs and the linked notes'
    stems filled in; invalid_request for refs that do not fit the kind (see `file`). Each opens
    with a fixed sentence longer than a chat title (`catalog.TITLE_LEN`), so a chat titled from
    the request's start names no note or path."""
    second = _second(kind, ref, other, archive)
    if second is None:
        raise DecisionLoopError("invalid_request")
    if kind == "note_link":
        return (
            "Link two notes in this vault by adding one wikilink line to the first note, as "
            "described below:\n"
            f"- In `{ref}`, add one line `- [[{_stem(second)}]]` under a `## Related` heading "
            "at the end of the note (add the heading if the note has none).\n"
            f"{_KEEP}"
        )
    if kind == "note_superseded":
        return (
            "Mark an older note in this vault as replaced by a newer one, with one frontmatter "
            "line, as described below:\n"
            f'- In `{ref}`, add the frontmatter line `superseded_by: "[[{_stem(second)}]]"` '
            "(add frontmatter if the note has none).\n"
            f"Do not delete or move the note. {_KEEP}"
        )
    return (
        "Archive the ticked tasks of one project in this vault, moving them word for word, as "
        "described below:\n"
        f"- Move each ticked line (`- [x]`) word for word from `{ref}` into `{second}`, "
        "creating it if needed.\n"
        "- Keep every open task where it is.\n"
        f"{_KEEP}"
    )


def _context(scope: str, proposal_id: str) -> bytes:
    return _CONTEXT + scope.encode("ascii") + proposal_id.encode("ascii")


def _second(kind: object, ref: object, other: object, archive: object) -> str | None:
    """The second ref `kind` names (`other` for a paired kind, `archive` for tasks_ticked) when
    the refs fit the kind: `ref` and the second ref `writable` and different, the ref the kind
    does not name None. None when they do not fit."""
    if type(kind) is not str or kind not in CHANGES:
        return None
    second, unnamed = (other, archive) if kind in PAIRED else (archive, other)
    if unnamed is not None or not isinstance(second, str):
        return None
    return second if writable(ref) and writable(second) and second != ref else None


def _stem(ref: str) -> str:
    return PurePosixPath(ref).stem


def _title(record: ChangeRecord) -> str:
    """The inbox title: the notes' stems, or the project's folder, each cut to `_NAME_CHARS`."""
    if record.kind == "tasks_ticked" or record.other is None:
        project = PurePosixPath(record.ref).parent.name or _stem(record.ref)
        return f"Archive ticked tasks in {project[:_NAME_CHARS]}"
    first, second = _stem(record.ref)[:_NAME_CHARS], _stem(record.other)[:_NAME_CHARS]
    if record.kind == "note_link":
        return f"Link {first} to {second}"
    return f"Mark {first} as replaced by {second}"


def _state(approved_at: str | None, rejected_at: str | None) -> str:
    if rejected_at is not None:
        return "rejected"
    return "open" if approved_at is None else "approved"


def _restamped(
    record: ChangeRecord,
    updated_at: str,
    approved_at: str | None,
    rejected_at: str | None,
    reason: str | None,
) -> ChangeRecord:
    """`record` with these stamps and reason, and the state and revision they give."""
    return replace(
        record,
        state=_state(approved_at, rejected_at),
        revision=_revision(record.item_key, approved_at, rejected_at),
        updated_at=updated_at,
        approved_at=approved_at,
        rejected_at=rejected_at,
        reason=reason,
    )


def _read(
    connection: sqlite3.Connection,
    scope: str,
    names: Sequence[str],
    *,
    open_only: bool = False,
    proposal_id: str | None = None,
) -> list[_Row]:
    """The scope's rows named in `names` with their bodies, newest first: only the open ones
    with `open_only`, only `proposal_id`'s when it is given."""
    sql = f"SELECT {_COLUMNS},body FROM decision_proposals WHERE scope=? AND {_NAMED}"
    values: list[object] = [scope, json.dumps(list(names))]
    if open_only:
        sql += " AND approved_at IS NULL AND rejected_at IS NULL"
    if proposal_id is not None:
        sql += " AND proposal_id=?"
        values.append(proposal_id)
    found = connection.execute(f"{sql} ORDER BY created_at DESC,proposal_id DESC", values)
    return [_Row(*row) for row in found.fetchall()]


def _records(scope: str, rows: Sequence[_Row]) -> list[ChangeRecord]:
    return [record for row in rows if (record := _record(scope, row)) is not None]


def _record(scope: str, row: _Row) -> ChangeRecord | None:
    """The change a stored row holds, or None when it does not read back (see the module
    docstring)."""
    try:
        _uuid(row.proposal_id)
        for value in (row.created_at, row.updated_at, row.approved_at, row.rejected_at):
            if value is not None:
                parse_stamp(value)
    except RoutineError, ValueError:
        return None
    if row.reason is not None and (row.rejected_at is None or not _one_line(row.reason)):
        return None
    plain = _open(_context(scope, row.proposal_id), row.body)
    if plain is None:
        return None
    try:
        fields: Any = json.loads(plain, object_pairs_hook=unique_object)
        if not isinstance(fields, dict) or set(fields) != _FIELDS:
            return None
        _uuid(fields["episode_id"])
    except ValueError, RecursionError, RoutineError:
        return None
    if (
        fields["kind"] != row.name
        or fields["item_key"] != row.proposed_sha256
        or _second(fields["kind"], fields["ref"], fields["other"], fields["archive"]) is None
    ):
        return None
    return ChangeRecord(
        proposal_id=row.proposal_id,
        scope=scope,
        kind=row.name,
        ref=fields["ref"],
        other=fields["other"],
        archive=fields["archive"],
        item_key=row.proposed_sha256,
        episode_id=fields["episode_id"],
        state=_state(row.approved_at, row.rejected_at),
        revision=_revision(row.proposed_sha256, row.approved_at, row.rejected_at),
        created_at=row.created_at,
        updated_at=row.updated_at,
        approved_at=row.approved_at,
        rejected_at=row.rejected_at,
        reason=row.reason,
    )


def _holds(connection: sqlite3.Connection, scope: str, item_key: str) -> bool:
    """Whether a change row of the scope, whether or not it reads back, holds `item_key`."""
    return (
        connection.execute(
            "SELECT 1 FROM decision_proposals "
            f"WHERE scope=? AND proposed_sha256=? AND {_NAMED} LIMIT 1",
            (scope, item_key, _NAMES),
        ).fetchone()
        is not None
    )


def _filed_on(connection: sqlite3.Connection, bounds: tuple[str, str]) -> int:
    """The change rows of every scope created inside `bounds`, whether or not they read back."""
    return connection.execute(
        f"SELECT count(*) FROM decision_proposals WHERE {_NAMED} AND created_at>=? "
        "AND created_at<?",
        (_NAMES, *bounds),
    ).fetchone()[0]


def _make_room(connection: sqlite3.Connection, scope: str, start: str) -> None:
    """Make room for one more of the scope's change rows under `MAX_CHANGES` by deleting the
    rows `_spare` names; limit, deleting none, when it holds too few of them."""
    spare = _spare(connection, scope, start)
    if spare is None:
        raise DecisionLoopError("limit")
    connection.executemany("DELETE FROM decision_proposals WHERE proposal_id=?", spare)


def _spare(connection: sqlite3.Connection, scope: str, start: str) -> list[tuple[str]] | None:
    """The ids of the rows to delete for one more of the scope's change rows under
    `MAX_CHANGES`: its oldest approved rows created before `start`, then its oldest rejected
    ones, as many as the rows over the cap (none below it); None when it holds too few."""
    (held,) = connection.execute(
        f"SELECT count(*) FROM decision_proposals WHERE scope=? AND {_NAMED}", (scope, _NAMES)
    ).fetchone()
    excess = held + 1 - MAX_CHANGES
    if excess <= 0:
        return []
    spare = connection.execute(
        f"SELECT proposal_id FROM decision_proposals WHERE scope=? AND {_NAMED} AND created_at<? "
        "AND (approved_at IS NOT NULL OR rejected_at IS NOT NULL) "
        "ORDER BY rejected_at IS NOT NULL,created_at,proposal_id LIMIT ?",
        (scope, _NAMES, start, excess),
    ).fetchall()
    return None if len(spare) < excess else spare


def _current(
    connection: sqlite3.Connection, scope: str, proposal_id: str
) -> tuple[str, str | None, str | None, str | None] | None:
    """A change row's updated_at, approved_at, rejected_at and reason as a write transaction
    finds them; None once the scope holds no change row by that id."""
    return connection.execute(
        "SELECT updated_at,approved_at,rejected_at,reason FROM decision_proposals "
        f"WHERE scope=? AND proposal_id=? AND {_NAMED}",
        (scope, proposal_id, _NAMES),
    ).fetchone()
