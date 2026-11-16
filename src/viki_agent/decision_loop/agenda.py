"""Decision items and model-call records per workspace scope, with their text sealed at rest.

An item is what VIKI may bring up about one candidate, stored under the candidate's `item_key`;
a call record keeps what one model call sent and got, for the Context tab. A row's text lives in
its `body`: the canonical JSON of an item's `ItemBody` together with its `kind` and `mission_id`
(no column holds those), or of a call's `CallBody`, sealed by `protection.protect` under a
context that names the row's scope and key, so a body copied into another row does not open.
The other columns hold codes, ids, stamps, hashes and the caller's answers JSON.

Each public method checks its arguments, then the store's privacy, then runs one transaction,
a write one except for `open_items`, `item`, `item_records`, `item_detail`, `calls` and `call`.
`item_records` and `item_detail` list and inspect items; `delete_items` and `delete_calls` remove
scoped records. Bodies are sealed before a write transaction begins and opened after a read one
ends. A row whose body does not open reads without text in `item_records` and `item_detail`,
`open_items` skips and counts it, `item` answers None, `calls` lists it without a body and `call`
refuses it as `unknown_item`; the next `upsert` of its key replaces it. Refusals
are `DecisionLoopError` codes: `invalid_request` for bad arguments or an oversize body, and
`unavailable` when the store or sealing does not work. Nothing here logs.
"""

from __future__ import annotations

import contextlib
import json
import re
import sqlite3
import uuid
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timedelta
from typing import Any

from viki_agent import protection
from viki_agent.api_json import unique_object
from viki_agent.automation.routines import RoutineError, _uuid, parse_stamp, stamp
from viki_agent.conversations.drafts import require_private_store
from viki_agent.decision_loop.contracts import (
    CALL_OUTCOMES,
    CANDIDATES,
    ITEM_STATES,
    KINDS,
    LEVELS,
    MAX_CALLS,
    MAX_OPEN_ITEMS,
    MODEL_ERROR_CODES,
    TIMINGS,
    ActionSpec,
    CallBody,
    CallRecord,
    DecisionLoopError,
    EvidenceLabel,
    ItemBody,
    ItemRecord,
    canonical_json,
)
from viki_agent.storage import SessionStore, StateError

CLOSED_KEEP_DAYS = 30  # `prune` deletes a closed item last updated longer ago than this
ITEM_BODY_BYTES = 65_536  # the sealed body bounds of `decision_items` and `decision_calls`
CALL_BODY_BYTES = 262_144
MARKS = {"shown": "shown_at", "opened": "opened_at"}  # a `mark` event and the column it stamps

_ITEM_CONTEXT = b"VIKI decision item v1\x00"
_CALL_CONTEXT = b"VIKI decision call v1\x00"
_HEX_KEY = re.compile(r"[0-9a-f]{64}")
_PRINTABLE = re.compile(r"[\x21-\x7e]+")  # printable ASCII only, so SQLite can always bind it
_ANSWERS_CHARS = 2048
_MODEL_CHARS = 32
_LATENCY_MS = 60_000
_INPUT_TOKENS = 1_000_000
_COST_MICRO_USD = 1_000_000_000
# A level's rank, silent 0 up to alert 4. The open items the cap keeps, loudest and then newest
# first, are sorted by `ORDER BY _KEEP` in SQL.
_RANK = {level: rank for rank, level in enumerate(LEVELS)}
_KEEP = (
    "CASE level "
    + " ".join(f"WHEN '{level}' THEN {rank}" for level, rank in _RANK.items())
    + " END DESC,created_at DESC,item_key DESC"
)
_ITEM_COLUMNS = (
    "item_key,episode_id,candidate,level,state,timing,defer_until,created_at,shown_at,"
    "answers,state_hash"
)
_CALL_COLUMNS = (
    "call_id,created_at,item_key,episode_id,session_id,model,outcome,failure,latency_ms,"
    "input_tokens,cost_micro_usd"
)
_ITEM_FIELDS = frozenset({*(field.name for field in fields(ItemBody)), "kind", "mission_id"})
_LABEL_FIELDS = frozenset(field.name for field in fields(EvidenceLabel))
_ACTION_FIELDS = frozenset(field.name for field in fields(ActionSpec))
_OLD_ACTION_FIELDS = _ACTION_FIELDS - {"text"}  # a body stored before `ActionSpec.text` existed
_CALL_FIELDS = frozenset(field.name for field in fields(CallBody))
_CHAT_HOLD = "item_key IN (SELECT item_key FROM decision_missions WHERE scope=?)"
_HELD = f"(state='open' OR {_CHAT_HOLD})"


@dataclass(frozen=True)
class ItemWrite:
    """What one decision says about an item. `defer_until` is an aware instant exactly when
    `timing` is "at"; `kind` and `mission_id` are kept in the sealed body."""

    item_key: str
    episode_id: str
    candidate: str
    kind: str | None
    level: str
    timing: str
    defer_until: datetime | None
    state_hash: str | None
    answers_json: str | None
    mission_id: str | None
    body: ItemBody


@dataclass(frozen=True)
class CallWrite:
    """One model call to record. `failure` is one of `MODEL_ERROR_CODES` exactly when `outcome`
    is "failed"."""

    item_key: str
    episode_id: str | None
    session_id: str | None
    model: str | None
    outcome: str
    failure: str | None
    latency_ms: int | None
    input_tokens: int | None
    cost_micro_usd: int | None
    body: CallBody


@dataclass(frozen=True)
class CallEntry:
    """A listed call record: its columns as stored and its body, or None when the body does not
    open."""

    call_id: str
    created_at: str
    item_key: str
    episode_id: str | None
    session_id: str | None
    model: str | None
    outcome: str
    failure: str | None
    latency_ms: int | None
    input_tokens: int | None
    cost_micro_usd: int | None
    body: CallBody | None


# ---- argument checks ---------------------------------------------------------------------------


def _require(ok: object) -> None:
    if not ok:
        raise DecisionLoopError("invalid_request")


def _id(value: object) -> str:
    """A canonical 36-character UUID string, so it is ASCII inside a sealing context."""
    try:
        return _uuid(value)
    except RoutineError:
        raise DecisionLoopError("invalid_request") from None


def _optional_id(value: object) -> str | None:
    return None if value is None else _id(value)


def _is_key(value: object) -> bool:
    return isinstance(value, str) and _HEX_KEY.fullmatch(value) is not None


def _key(value: object) -> str:
    if not isinstance(value, str) or not _is_key(value):
        raise DecisionLoopError("invalid_request")
    return value


def _keys(values: Iterable[str]) -> list[str]:
    try:
        return [_key(value) for value in values]
    except TypeError:
        raise DecisionLoopError("invalid_request") from None


def _stamp(value: object) -> str:
    """An aware instant as the store's 24-character UTC stamp."""
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise DecisionLoopError("invalid_request")
    try:
        return stamp(value)
    except OverflowError, ValueError:
        raise DecisionLoopError("invalid_request") from None


def _bounded(value: object, high: int) -> bool:
    """None, or an int (not a bool) from 0 to `high`."""
    return value is None or (type(value) is int and 0 <= value <= high)


def _item_filter(
    scope: str,
    *,
    topic: str | None,
    state: str | None,
    since: datetime | None,
    until: datetime | None,
) -> tuple[str, list[object]]:
    """Validated scope and optional predicates for item rows."""
    scope = _id(scope)
    _require(topic is None or topic in CANDIDATES)
    _require(state is None or state in ITEM_STATES)
    start = None if since is None else _stamp(since)
    end = None if until is None else _stamp(until)
    _require(since is None or until is None or since < until)
    clauses = ["scope=?"]
    values: list[object] = [scope]
    if topic is not None:
        clauses.append("candidate=?")
        values.append(topic)
    if state is not None:
        clauses.append("state=?")
        values.append(state)
    if start is not None:
        clauses.append("updated_at>=?")
        values.append(start)
    if end is not None:
        clauses.append("updated_at<?")
        values.append(end)
    return " AND ".join(clauses), values


# ---- bodies ------------------------------------------------------------------------------------


def _item_context(scope: str, item_key: str) -> bytes:
    return _ITEM_CONTEXT + scope.encode("utf-8") + item_key.encode("utf-8")


def _call_context(scope: str, call_id: str) -> bytes:
    return _CALL_CONTEXT + scope.encode("utf-8") + call_id.encode("utf-8")


def _object(value: object, names: frozenset[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or value.keys() != names:
        raise TypeError("shape")
    return value


def _text(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("text")
    return value


def _optional_text(value: object) -> str | None:
    return None if value is None else _text(value)


def _count(value: object) -> int:
    if type(value) is not int or value < 0:
        raise TypeError("count")
    return value


def _list(value: object) -> list[Any]:
    if not isinstance(value, list):
        raise TypeError("list")
    return value


def _label(value: object) -> EvidenceLabel:
    entry = _object(value, _LABEL_FIELDS)
    return EvidenceLabel(
        label=_text(entry["label"]),
        workspace_id=_optional_text(entry["workspace_id"]),
        ref=_optional_text(entry["ref"]),
        session_id=_optional_text(entry["session_id"]),
    )


def _action(value: object) -> ActionSpec:
    """An action of `_ACTION_FIELDS`'s current shape, or of `_OLD_ACTION_FIELDS` -- a body
    stored before `ActionSpec.text` existed -- which reads with `text` None."""
    has_text = isinstance(value, dict) and "text" in value
    entry = _object(value, _ACTION_FIELDS if has_text else _OLD_ACTION_FIELDS)
    primary = entry["primary"]
    if not isinstance(primary, bool):
        raise TypeError("primary")
    return ActionSpec(
        type=_text(entry["type"]),
        label=_text(entry["label"]),
        primary=primary,
        session_id=_optional_text(entry["session_id"]),
        workspace_id=_optional_text(entry["workspace_id"]),
        ref=_optional_text(entry["ref"]),
        routine_id=_optional_text(entry["routine_id"]),
        call_id=_optional_text(entry["call_id"]),
        mission_id=_optional_text(entry["mission_id"]),
        text=_optional_text(entry["text"]) if has_text else None,
    )


def _item_body(plain: bytes) -> tuple[ItemBody, str | None, str | None] | None:
    """An item's sealed JSON as (body, kind, mission_id), or None when it is not one."""
    try:
        value = _object(json.loads(plain, object_pairs_hook=unique_object), _ITEM_FIELDS)
        body = ItemBody(
            title=_text(value["title"]),
            what=_text(value["what"]),
            why=_text(value["why"]),
            evidence=tuple(_label(entry) for entry in _list(value["evidence"])),
            actions=tuple(_action(entry) for entry in _list(value["actions"])),
        )
        return body, _optional_text(value["kind"]), _optional_text(value["mission_id"])
    except ValueError, TypeError, RecursionError:
        return None


def _split_part(value: object) -> tuple[str, int]:
    pair = _list(value)
    if len(pair) != 2:
        raise TypeError("split")
    return _text(pair[0]), _count(pair[1])


def _call_body(plain: bytes) -> CallBody | None:
    """A call's sealed JSON as its body, or None when it is not one."""
    try:
        value = _object(json.loads(plain, object_pairs_hook=unique_object), _CALL_FIELDS)
        return CallBody(
            title=_text(value["title"]),
            candidate=_text(value["candidate"]),
            state_text=_text(value["state_text"]),
            questions_text=_text(value["questions_text"]),
            split=tuple(_split_part(part) for part in _list(value["split"])),
            answers_json=_optional_text(value["answers_json"]),
            held_back=_count(value["held_back"]),
            version_setting=_text(value["version_setting"]),
            playbook_version=_text(value["playbook_version"]),
        )
    except ValueError, TypeError, RecursionError:
        return None


def _plain(value: object) -> bytes:
    """`value`'s canonical JSON as ASCII bytes; a value JSON cannot hold is invalid_request."""
    try:
        return canonical_json(value).encode("ascii")
    except ValueError, TypeError, RecursionError:
        raise DecisionLoopError("invalid_request") from None


def _seal(context: bytes, plain: bytes, limit: int) -> bytes:
    """Seal `plain`; a body its column cannot hold is invalid_request."""
    _require(len(plain) <= limit)
    try:
        sealed = protection.protect(plain, context=context)
    except protection.ProtectionError:
        raise DecisionLoopError("unavailable") from None
    _require(2 <= len(sealed) <= limit)
    return sealed


def _open(context: bytes, sealed: bytes) -> bytes | None:
    """The plain bytes of a stored body, or None when this row's body does not open.

    A bad body and a protection that cannot work at all refuse alike. As in `messaging_store`,
    a probe sealed and opened under the same context tells them apart: when the probe fails
    too, sealing is unavailable.
    """
    try:
        return protection.unprotect(sealed, context=context)
    except protection.ProtectionError:
        pass
    try:
        protection.unprotect(protection.protect(b"{}", context=context), context=context)
    except protection.ProtectionError:
        raise DecisionLoopError("unavailable") from None
    return None


def _record(
    row: Sequence[Any], body: ItemBody, kind: str | None, mission_id: str | None
) -> ItemRecord:
    """The record of a row read as `_ITEM_COLUMNS` with its opened body."""
    (
        key,
        episode_id,
        candidate,
        level,
        state,
        timing,
        defer_until,
        created_at,
        shown_at,
        answers,
        state_hash,
    ) = row
    return ItemRecord(
        item_key=key,
        episode_id=episode_id,
        candidate=candidate,
        kind=kind,
        level=level,
        state=state,
        timing=timing,
        defer_until=defer_until,
        created_at=created_at,
        shown_at=shown_at,
        mission_id=mission_id,
        answers_json=answers,
        state_hash=state_hash,
        body=body,
    )


def _item_record(scope: str, row: Sequence[Any]) -> ItemRecord | None:
    """The record of a row read as `_ITEM_COLUMNS` and its body, or None when the body does not
    open."""
    plain = _open(_item_context(scope, row[0]), row[-1])
    found = None if plain is None else _item_body(plain)
    return None if found is None else _record(row[:-1], *found)


def _call_entry(scope: str, row: Sequence[Any]) -> CallEntry:
    """The entry of a row read as `_CALL_COLUMNS` and its body; `body` is None when the body
    does not open."""
    plain = _open(_call_context(scope, row[0]), row[-1])
    return CallEntry(*row[:-1], body=None if plain is None else _call_body(plain))


# ---- the store ---------------------------------------------------------------------------------


class DecisionAgenda:
    """The `decision_items` and `decision_calls` rows of each workspace scope."""

    def __init__(self, store: SessionStore) -> None:
        self.store = store

    @contextlib.contextmanager
    def _transaction(self, *, write: bool = True) -> Iterator[sqlite3.Connection]:
        try:
            require_private_store(self.store)
            with self.store._connection(write=write) as connection:
                yield connection
        except StateError, protection.ProtectionError:
            raise DecisionLoopError("unavailable") from None

    # ---- items ---------------------------------------------------------------------------------

    def upsert(self, scope: str, write: ItemWrite, now: datetime) -> ItemRecord:
        """Store the item `write` describes; answer it as stored.

        A new key starts an open row at revision 1. A stored key takes the write's columns and
        body, its revision goes up by one, and it keeps `created_at`, `shown_at` and
        `opened_at`. A stale item opens again; an acted, dismissed or done one keeps its state.
        """
        scope = _id(scope)
        _require(isinstance(write, ItemWrite) and isinstance(write.body, ItemBody))
        key = _key(write.item_key)
        _require(
            write.candidate in CANDIDATES
            and (write.kind is None or write.kind in KINDS)
            and write.level in LEVELS
            and write.timing in TIMINGS
            and (write.timing == "at") == (write.defer_until is not None)
            and (write.state_hash is None or _is_key(write.state_hash))
            and (
                write.answers_json is None
                or (
                    isinstance(write.answers_json, str)
                    and write.answers_json.isascii()
                    and 2 <= len(write.answers_json) <= _ANSWERS_CHARS
                )
            )
        )
        episode_id = _id(write.episode_id)
        mission_id = _optional_id(write.mission_id)
        defer_until = None if write.defer_until is None else _stamp(write.defer_until)
        updated_at = _stamp(now)
        plain = _plain({**asdict(write.body), "kind": write.kind, "mission_id": mission_id})
        # The record answered here is what a later read answers, so a body that would not read
        # back is refused rather than stored.
        found = _item_body(plain)
        if found is None:
            raise DecisionLoopError("invalid_request")
        sealed = _seal(_item_context(scope, key), plain, ITEM_BODY_BYTES)
        with self._transaction() as connection:
            connection.execute(
                "INSERT INTO decision_items (scope,item_key,candidate,state,level,timing,"
                "defer_until,episode_id,state_hash,answers,body,revision,created_at,updated_at) "
                "VALUES (:scope,:key,:candidate,'open',:level,:timing,:defer_until,:episode_id,"
                ":state_hash,:answers,:body,1,:now,:now) "
                "ON CONFLICT(scope,item_key) DO UPDATE SET candidate=excluded.candidate,"
                "state=CASE state WHEN 'stale' THEN 'open' ELSE state END,"
                "level=excluded.level,timing=excluded.timing,defer_until=excluded.defer_until,"
                "episode_id=excluded.episode_id,state_hash=excluded.state_hash,"
                "answers=excluded.answers,body=excluded.body,revision=revision+1,"
                "updated_at=excluded.updated_at",
                {
                    "scope": scope,
                    "key": key,
                    "candidate": write.candidate,
                    "level": write.level,
                    "timing": write.timing,
                    "defer_until": defer_until,
                    "episode_id": episode_id,
                    "state_hash": write.state_hash,
                    "answers": write.answers_json,
                    "body": sealed,
                    "now": updated_at,
                },
            )
            row = connection.execute(
                f"SELECT {_ITEM_COLUMNS} FROM decision_items WHERE scope=? AND item_key=?",
                (scope, key),
            ).fetchone()
        return _record(row, *found)

    def open_items(self, scope: str) -> tuple[list[ItemRecord], int]:
        """The scope's open items, loudest level first and then oldest first, and how many open
        rows it skipped because their body did not open. It answers at most `MAX_OPEN_ITEMS`,
        chosen loudest and then newest first, as `prune` chooses the items it keeps open."""
        scope = _id(scope)
        with self._transaction(write=False) as connection:
            rows = connection.execute(
                f"SELECT {_ITEM_COLUMNS},body FROM decision_items WHERE scope=? AND state='open' "
                f"ORDER BY {_KEEP}",
                (scope,),
            ).fetchall()
        items: list[ItemRecord] = []
        skipped = 0
        for row in rows:
            if len(items) == MAX_OPEN_ITEMS:
                break
            record = _item_record(scope, row)
            if record is None:
                skipped += 1
            else:
                items.append(record)
        items.sort(key=lambda record: (-_RANK[record.level], record.created_at, record.item_key))
        return items, skipped

    def item(self, scope: str, item_key: str) -> ItemRecord | None:
        """The scope's item under `item_key` in any state; None when there is none or its body
        does not open."""
        scope = _id(scope)
        key = _key(item_key)
        with self._transaction(write=False) as connection:
            row = connection.execute(
                f"SELECT {_ITEM_COLUMNS},body FROM decision_items WHERE scope=? AND item_key=?",
                (scope, key),
            ).fetchone()
        return None if row is None else _item_record(scope, row)

    def item_records(
        self,
        scope: str,
        *,
        topic: str | None = None,
        state: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        before: tuple[str, str] | None = None,
        limit: int,
    ) -> dict[str, Any]:
        """List matching items newest first, with counts before the keyset cursor."""
        where, values = _item_filter(scope, topic=topic, state=state, since=since, until=until)
        _require(type(limit) is int and 1 <= limit <= 50)
        cursor: tuple[str, str] | None = None
        if before is not None:
            _require(isinstance(before, tuple) and len(before) == 2)
            try:
                at = stamp(parse_stamp(before[0]))
            except ValueError:
                raise DecisionLoopError("invalid_request") from None
            cursor = (at, _key(before[1]))
        with self._transaction(write=False) as connection:
            connection.execute("BEGIN")
            total, deletable = connection.execute(
                f"SELECT count(*),count(*) FILTER (WHERE NOT {_HELD}) "
                f"FROM decision_items WHERE {where}",
                (scope, *values),
            ).fetchone()
            page_where = where
            page_values = list(values)
            if cursor is not None:
                page_where += " AND (updated_at,item_key)<(?,?)"
                page_values.extend(cursor)
            rows = connection.execute(
                "SELECT item_key,updated_at,created_at,candidate,state,level,"
                f"CASE WHEN state='open' THEN 'open' WHEN {_CHAT_HOLD} THEN 'chat' END,body "
                f"FROM decision_items WHERE {page_where} "
                "ORDER BY updated_at DESC,item_key DESC LIMIT ?",
                (scope, *page_values, limit + 1),
            ).fetchall()
        more = len(rows) > limit
        rows = rows[:limit]
        listed = []
        for key, at, created_at, topic, state, level, kept, sealed in rows:
            plain = _open(_item_context(scope, key), sealed)
            found = None if plain is None else _item_body(plain)
            listed.append(
                {
                    "id": key,
                    "at": at,
                    "created_at": created_at,
                    "title": found[0].title if found else "",
                    "topic": topic,
                    "state": state,
                    "level": level,
                    "kept": kept,
                }
            )
        return {
            "total": total,
            "deletable": deletable,
            "rows": listed,
            "next": {"at": rows[-1][1], "id": rows[-1][0]} if more else None,
        }

    def item_detail(self, scope: str, item_key: str) -> dict[str, Any] | None:
        """The item and its opened text, or empty text when its body does not open."""
        scope = _id(scope)
        key = _key(item_key)
        with self._transaction(write=False) as connection:
            row = connection.execute(
                "SELECT item_key,candidate,state,level,created_at,updated_at,shown_at,opened_at,"
                f"CASE WHEN state='open' THEN 'open' WHEN {_CHAT_HOLD} THEN 'chat' END,body "
                "FROM decision_items WHERE scope=? AND item_key=?",
                (scope, scope, key),
            ).fetchone()
        if row is None:
            return None
        plain = _open(_item_context(scope, key), row[-1])
        found = None if plain is None else _item_body(plain)
        body = None if found is None else found[0]
        return {
            "id": key,
            "title": body.title if body else "",
            "topic": row[1],
            "state": row[2],
            "level": row[3],
            "created_at": row[4],
            "updated_at": row[5],
            "shown_at": row[6],
            "opened_at": row[7],
            "what": body.what if body else "",
            "why": body.why if body else "",
            "evidence": body.evidence if body else (),
            "kept": row[8],
        }

    def delete_items(
        self,
        scope: str,
        *,
        ids: Sequence[str] | None = None,
        topic: str | None = None,
        state: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> tuple[int, int]:
        """Delete matching items except open or mission-held rows; answer (deleted, kept)."""
        where, values = _item_filter(scope, topic=topic, state=state, since=since, until=until)
        if ids is not None:
            _require(
                isinstance(ids, Sequence)
                and not isinstance(ids, (str, bytes))
                and 1 <= len(ids) <= 50
                and all(value is None for value in (topic, state, since, until))
            )
            selected = [_key(value) for value in ids]
            _require(len(set(selected)) == len(selected))
            where += " AND item_key IN (SELECT value FROM json_each(?))"
            values.append(json.dumps(selected))
        else:
            _require(
                until is not None and any(value is not None for value in (topic, state, since))
            )
        with self._transaction() as connection:
            matched = connection.execute(
                f"SELECT count(*) FROM decision_items WHERE {where}", values
            ).fetchone()[0]
            deleted = connection.execute(
                f"DELETE FROM decision_items WHERE {where} AND NOT {_HELD}",
                (*values, scope),
            ).rowcount
        return deleted, matched - deleted

    def set_state(
        self,
        scope: str,
        item_key: str,
        state: str,
        now: datetime,
        *,
        defer_until: datetime | None = None,
    ) -> None:
        """Set the item's state, one of `ITEM_STATES`. With `defer_until`, which needs "open",
        the item is also timed "at" that instant. An item the scope lacks is `unknown_item`."""
        scope = _id(scope)
        key = _key(item_key)
        _require(state in ITEM_STATES and (defer_until is None or state == "open"))
        deferred = None if defer_until is None else _stamp(defer_until)
        updated_at = _stamp(now)
        with self._transaction() as connection:
            changed = connection.execute(
                "UPDATE decision_items SET state=:state,"
                "timing=CASE WHEN :defer IS NULL THEN timing ELSE 'at' END,"
                "defer_until=COALESCE(:defer,defer_until),revision=revision+1,updated_at=:now "
                "WHERE scope=:scope AND item_key=:key",
                {"state": state, "defer": deferred, "now": updated_at, "scope": scope, "key": key},
            ).rowcount
            if changed != 1:
                raise DecisionLoopError("unknown_item")

    def mark(self, scope: str, keys: Iterable[str], event: str, now: datetime) -> int:
        """Stamp `event` ("shown" or "opened") on each listed item of the scope that does not
        carry it yet; answer how many it stamped. `keys` lists 1 to `MAX_OPEN_ITEMS` distinct
        item keys."""
        scope = _id(scope)
        column = MARKS.get(event) if isinstance(event, str) else None
        if column is None:
            raise DecisionLoopError("invalid_request")
        listed = _keys(keys)
        _require(1 <= len(listed) <= MAX_OPEN_ITEMS and len(set(listed)) == len(listed))
        stamped = _stamp(now)
        with self._transaction() as connection:
            return connection.execute(
                f"UPDATE decision_items SET {column}=:now WHERE scope=:scope "
                f"AND {column} IS NULL AND item_key IN (SELECT value FROM json_each(:keys))",
                {"now": stamped, "scope": scope, "keys": json.dumps(listed)},
            ).rowcount

    def close_missing(self, scope: str, present: Iterable[str], now: datetime) -> int:
        """Make each open item of the scope whose key is not in `present` stale; answer how
        many."""
        scope = _id(scope)
        listed = sorted(set(_keys(present)))
        stamped = _stamp(now)
        with self._transaction() as connection:
            return connection.execute(
                "UPDATE decision_items SET state='stale',revision=revision+1,updated_at=:now "
                "WHERE scope=:scope AND state='open' "
                "AND item_key NOT IN (SELECT value FROM json_each(:present))",
                {"now": stamped, "scope": scope, "present": json.dumps(listed)},
            ).rowcount

    def prune(self, scope: str, now: datetime) -> tuple[int, int]:
        """Keep at most `MAX_OPEN_ITEMS` of the scope's items open, making the rest stale from
        the quietest level and then the oldest, and delete its closed items last updated more
        than `CLOSED_KEEP_DAYS` ago; answer (made stale, deleted)."""
        scope = _id(scope)
        stamped = _stamp(now)
        cutoff = _stamp(now - timedelta(days=CLOSED_KEEP_DAYS))
        with self._transaction() as connection:
            staled = connection.execute(
                "UPDATE decision_items SET state='stale',revision=revision+1,updated_at=:now "
                "WHERE scope=:scope AND state='open' AND item_key IN (SELECT item_key "
                "FROM decision_items WHERE scope=:scope AND state='open' "
                f"ORDER BY {_KEEP} LIMIT -1 OFFSET :keep)",
                {"now": stamped, "scope": scope, "keep": MAX_OPEN_ITEMS},
            ).rowcount
            deleted = connection.execute(
                "DELETE FROM decision_items WHERE scope=? AND state<>'open' AND updated_at<?",
                (scope, cutoff),
            ).rowcount
        return staled, deleted

    # ---- calls ---------------------------------------------------------------------------------

    def record_call(self, scope: str, write: CallWrite, now: datetime) -> str:
        """Record one model call under a new `call_id` and answer it. The scope keeps this call
        and its newest `MAX_CALLS` - 1 others; the same transaction deletes the rest."""
        scope = _id(scope)
        _require(isinstance(write, CallWrite) and isinstance(write.body, CallBody))
        key = _key(write.item_key)
        episode_id = _optional_id(write.episode_id)
        session_id = _optional_id(write.session_id)
        _require(
            (
                write.model is None
                or (
                    isinstance(write.model, str)
                    and len(write.model) <= _MODEL_CHARS
                    and _PRINTABLE.fullmatch(write.model) is not None
                )
            )
            and write.outcome in CALL_OUTCOMES
            and (
                write.failure in MODEL_ERROR_CODES
                if write.outcome == "failed"
                else write.failure is None
            )
            and _bounded(write.latency_ms, _LATENCY_MS)
            and _bounded(write.input_tokens, _INPUT_TOKENS)
            and _bounded(write.cost_micro_usd, _COST_MICRO_USD)
        )
        created_at = _stamp(now)
        plain = _plain(asdict(write.body))
        _require(_call_body(plain) is not None)
        call_id = str(uuid.uuid4())
        sealed = _seal(_call_context(scope, call_id), plain, CALL_BODY_BYTES)
        with self._transaction() as connection:
            connection.execute(
                f"INSERT INTO decision_calls ({_CALL_COLUMNS},scope,body) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    call_id,
                    created_at,
                    key,
                    episode_id,
                    session_id,
                    write.model,
                    write.outcome,
                    write.failure,
                    write.latency_ms,
                    write.input_tokens,
                    write.cost_micro_usd,
                    scope,
                    sealed,
                ),
            )
            connection.execute(
                "DELETE FROM decision_calls WHERE call_id IN (SELECT call_id FROM decision_calls "
                "WHERE scope=:scope AND call_id<>:call_id "
                "ORDER BY created_at DESC,call_id DESC LIMIT -1 OFFSET :keep)",
                {"scope": scope, "call_id": call_id, "keep": MAX_CALLS - 1},
            )
        return call_id

    def calls(self, scope: str) -> list[CallEntry]:
        """The scope's call records, newest first, at most `MAX_CALLS`; an entry whose body
        does not open is listed with `body` None."""
        scope = _id(scope)
        with self._transaction(write=False) as connection:
            rows = connection.execute(
                f"SELECT {_CALL_COLUMNS},body FROM decision_calls WHERE scope=? "
                "ORDER BY created_at DESC,call_id DESC LIMIT ?",
                (scope, MAX_CALLS),
            ).fetchall()
        return [_call_entry(scope, row) for row in rows]

    def call(self, scope: str, call_id: str) -> CallRecord:
        """The scope's call record with its body; `unknown_item` when the scope has none under
        `call_id` or its body does not open."""
        scope = _id(scope)
        call_id = _id(call_id)
        with self._transaction(write=False) as connection:
            row = connection.execute(
                f"SELECT {_CALL_COLUMNS},body FROM decision_calls WHERE scope=? AND call_id=?",
                (scope, call_id),
            ).fetchone()
        entry = None if row is None else _call_entry(scope, row)
        if entry is None or entry.body is None:
            raise DecisionLoopError("unknown_item")
        return CallRecord(
            call_id=entry.call_id,
            created_at=entry.created_at,
            item_key=entry.item_key,
            episode_id=entry.episode_id,
            session_id=entry.session_id,
            model=entry.model,
            outcome=entry.outcome,
            failure=entry.failure,
            latency_ms=entry.latency_ms,
            input_tokens=entry.input_tokens,
            cost_micro_usd=entry.cost_micro_usd,
            body=entry.body,
        )

    def delete_calls(self, scope: str, ids: Sequence[str]) -> int:
        """Delete the scope's selected call records and answer how many were removed."""
        scope = _id(scope)
        _require(isinstance(ids, Sequence) and not isinstance(ids, (str, bytes)))
        _require(1 <= len(ids) <= 50)
        selected = [_id(value) for value in ids]
        _require(len(set(selected)) == len(selected))
        with self._transaction() as connection:
            return connection.execute(
                "DELETE FROM decision_calls WHERE scope=? "
                "AND call_id IN (SELECT value FROM json_each(?))",
                (scope, json.dumps(selected)),
            ).rowcount

    def delete_all(self, scope: str) -> tuple[int, int]:
        """Delete every item and call record of the scope; answer (items, calls)."""
        scope = _id(scope)
        with self._transaction() as connection:
            items = connection.execute(
                "DELETE FROM decision_items WHERE scope=?", (scope,)
            ).rowcount
            calls = connection.execute(
                "DELETE FROM decision_calls WHERE scope=?", (scope,)
            ).rowcount
        return items, calls
