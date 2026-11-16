"""The Decision loop's episode store: decisions and the user's reports, per workspace scope.

A `decision_episodes` row holds metadata only: the caller's evidence JSON, the policy and model
that decided, the model's answers as numbers, and what the user saw and did. Beside them,
`decision_preferences` keeps each scope's mutes and laters, the review stamp, the day's measured
spend and usage, the arbiter's `lambda` and learned snapshot (`learned`, restarted from
`learn_since`), the second looks', the project plans', the message drafts', the saved checks' and
the inbox suggestions' restart stamps (`second_looks`, `project_plans`, `message_drafts`,
`saved_checks`, `inbox_triage`) and the last
hour's nudge and alert showings (`loud`).
Each method checks its arguments, then the store's privacy, then runs one transaction, a write
one except that `preferences`, `review_seen_at`, `spent_on`, `usage_on`, `showings_since`,
`exposures_since`, `get_json`, `learn_since` and `decision_records` only read;
`delete_decisions` removes selected episodes; `record` and `report` also prune the
scope to its newest `MAX_EPISODES` rows no older than `RETENTION_DAYS`. A counter
(exposures, a feedback revision, a preference revision) stops at its column's ceiling. Refusals
are `DecisionLoopError` codes: invalid_request, unknown_item, not_applicable, and unavailable for
store and protection failures. Nothing here logs.
"""

from __future__ import annotations

import contextlib
import json
import math
import re
import sqlite3
import uuid
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, fields
from datetime import datetime, timedelta
from typing import Any

from viki_agent.automation.routines import RoutineError, _uuid, parse_stamp, stamp
from viki_agent.conversations.drafts import require_private_store
from viki_agent.decision_loop.contracts import (
    CANDIDATES,
    FEEDBACK_HELD,
    FEEDBACK_REASONS,
    FEEDBACK_SHOWN,
    KINDS,
    LATER,
    LEVELS,
    MAX_EPISODES,
    MAX_OPEN_ITEMS,
    RETENTION_DAYS,
    SOURCES,
    TIMINGS,
    TOPICS,
    TRIGGERS,
    WORK,
    DecisionLoopError,
    canonical_json,
    sha256_hex,
)
from viki_agent.decision_loop.rules import POLICY_VERSION, Prefs, evidence_key
from viki_agent.protection import ProtectionError
from viki_agent.storage import SessionStore, StateError

MAX_EVIDENCE_BYTES = 2048
MAX_JSON_BYTES = 8192
# The only names `get_json` and `set_json` take.
JSON_NAMES = (
    "lambda",
    "learned",
    "second_looks",
    "project_plans",
    "message_drafts",
    "saved_checks",
    "inbox_triage",
)
POLICIES = ("rules", "model")
SHOWN_LEVELS = ("list", "badge", "nudge", "alert")  # the user could see these; silent is held
MAX_SPEND_MICRO_USD = 1_000_000_000  # one `add_spend`, the bound of a stored cost_micro_usd
SHOWINGS = "loud"  # the preference logging the scope's showings; the hourly nudge cap sums all
MAX_SHOWINGS = 16  # the newest showings the log keeps

_CEILING = 1_000_000  # exposures, feedback_revision, a preference's revision and a usage count
_TOKENS_CEILING = 10**12  # the "usage" preference's tokens count stops here
_HOUR = timedelta(hours=1)
_MICROSECOND = timedelta(microseconds=1)
_ANSWERS_CHARS = 2048
_TERMS_CHARS = 512
_POLICY_VERSION_CHARS = 32
_MODEL_CHARS = 32
_REASON_CHARS = 64
_LATENCY_MS = 60_000
_INPUT_TOKENS = 1_000_000
_DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_HEX_KEY = re.compile(r"[0-9a-f]{64}")
_PRINTABLE = re.compile(r"[\x21-\x7e]+")  # printable ASCII only, so SQLite can always bind it
_SPEND = re.compile(r"([0-9]{4}-[0-9]{2}-[0-9]{2}):([0-9]{1,18})")  # "<day>:<micro-USD>"
_SHOWN = "level IN (" + ",".join(f"'{level}'" for level in SHOWN_LEVELS) + ")"
_OPEN_SUGGESTION = (
    "episode_id IN (SELECT episode_id FROM decision_items WHERE scope=? AND state='open')"
)
# event -> what it sets besides observed_until, on the listed shown episodes of the scope
_EVENTS = {
    "shown": f"exposures=min(exposures+1,{_CEILING}),"
    "first_exposed_at=COALESCE(first_exposed_at,:now),last_exposed_at=:now,",
    "opened": "opened_at=COALESCE(opened_at,:now),",
    "acted": "acted_at=COALESCE(acted_at,:now),",
}


@dataclass(frozen=True)
class EpisodeInput:
    """One decision to record: the `decision_episodes` columns a decision sets.

    `evidence` is stored as its canonical JSON beside `rules.evidence_key` of it.
    `answers_json` is a JSON object whose every value, at any depth, is a number; `terms` maps
    names to numbers and is stored as canonical JSON. `defer_until` is set exactly when `timing`
    is "at". The vocabularies are the contract's; the bounds are the table's.
    """

    candidate: str
    evidence: Mapping[str, object]
    policy: str
    policy_version: str
    level: str
    timing: str
    work: str
    reason: str
    item_key: str | None = None
    state_hash: str | None = None
    model: str | None = None
    answers_json: str | None = None
    terms: Mapping[str, float] | None = None
    kind: str | None = None
    defer_until: datetime | None = None
    rules_level: str | None = None
    latency_ms: int | None = None
    input_tokens: int | None = None
    cost_micro_usd: int | None = None
    mission_id: str | None = None


@dataclass(frozen=True)
class Recorded:
    """A decision episode as stored; `defer_until` is its stamp."""

    episode_id: str
    candidate: str
    level: str
    reason: str
    defer_until: str | None
    feedback: str | None
    feedback_reason: str | None
    feedback_revision: int


_RECORDED = ",".join(field.name for field in fields(Recorded))


@dataclass(frozen=True)
class Usage:
    """One day's measured use, as `add_usage` adds to it and `usage_on` sums across scopes:
    model calls, of them requests made to the shadow version, input tokens, and the requests
    never-send held something back from -- one for each call whose state left out at least one
    of its item's evidence entries (`BuiltState.held_back` above 0), shadow calls included,
    however many entries it left out."""

    calls: int
    shadow_calls: int
    tokens: int
    held_back: int


def _require(ok: object) -> None:
    if not ok:
        raise DecisionLoopError("invalid_request")


def _require_scope(scope: object) -> None:
    _require(isinstance(scope, str) and len(scope) == 36)


def _require_day(day: object) -> None:
    _require(isinstance(day, str) and _DAY.fullmatch(day))


def _bounded_text(value: object, high: int) -> bool:
    """A printable-ASCII `str` of 1 to `high` characters: a machine word, so SQLite can always
    bind it (a lone surrogate, which it cannot, is never printable ASCII)."""
    return (
        isinstance(value, str)
        and 1 <= len(value) <= high
        and _PRINTABLE.fullmatch(value) is not None
    )


def _hex_key(value: object) -> bool:
    return value is None or (isinstance(value, str) and _HEX_KEY.fullmatch(value) is not None)


def _count(value: object, high: int) -> bool:
    """None, or an int from 0 to *high* that is not a flag."""
    return value is None or (
        isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= high
    )


def _bounded_int(value: object, high: int) -> bool:
    """An int from 0 to *high* that is not a flag; unlike `_count`, None fails too."""
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= high


def _number(value: object) -> bool:
    """A finite number that is not a flag."""
    if isinstance(value, bool):
        return False
    return isinstance(value, int) or (isinstance(value, float) and math.isfinite(value))


def _numbers(value: object) -> bool:
    """Whether every leaf of a parsed JSON value is a number: no text, flag or null."""
    if isinstance(value, dict):
        return all(_numbers(item) for item in value.values())
    if isinstance(value, list):
        return all(_numbers(item) for item in value)
    return _number(value)


def _no_constant(name: str) -> object:
    raise ValueError(name)  # NaN and the infinities are no JSON numbers


def _evidence(evidence: Mapping[str, object]) -> str:
    """The canonical JSON that `rules.evidence_key` hashes, at most MAX_EVIDENCE_BYTES long."""
    _require(isinstance(evidence, Mapping))
    try:
        text = canonical_json(evidence)
    except TypeError, ValueError:
        raise DecisionLoopError("invalid_request") from None
    _require(len(text) <= MAX_EVIDENCE_BYTES)  # ASCII, so one byte per character
    return text


def _answers(text: str | None) -> str | None:
    """The answers JSON as given, once it reads as an object of numbers and is ASCII (its keys
    are free text; canonical JSON would already escape anything past ASCII, so this stays a
    machine-generated value rather than one SQLite could refuse to bind)."""
    if text is None:
        return None
    _require(isinstance(text, str) and text.isascii() and 2 <= len(text) <= _ANSWERS_CHARS)
    try:
        value = json.loads(text, parse_constant=_no_constant)
        ok = isinstance(value, dict) and _numbers(value)
    except ValueError, RecursionError:
        ok = False
    _require(ok)
    return text


def _terms(terms: Mapping[str, float] | None) -> str | None:
    """The canonical JSON of a mapping of names to numbers."""
    if terms is None:
        return None
    _require(
        isinstance(terms, Mapping)
        and all(isinstance(name, str) and _number(value) for name, value in terms.items())
    )
    try:
        text = canonical_json(dict(terms))
    except ValueError:  # an integer too long to write out
        raise DecisionLoopError("invalid_request") from None
    _require(len(text) <= _TERMS_CHARS)  # ASCII, so one byte per character
    return text


def _id(value: object) -> str | None:
    if value is None:
        return None
    try:
        return _uuid(value)
    except RoutineError:
        raise DecisionLoopError("invalid_request") from None


def _decision_filter(
    scope: str,
    *,
    topic: str | None,
    shown: str | None,
    answer: str | None,
    since: datetime | None,
    until: datetime | None,
) -> tuple[str, list[object]]:
    """Validated scope and optional predicates for decision rows; times are UTC stamps."""
    _require_scope(scope)
    _require(topic is None or topic in TOPICS)
    _require(shown is None or shown in ("held", "listed", "attention", "report"))
    _require(answer is None or answer in (*FEEDBACK_SHOWN, *FEEDBACK_HELD, "none"))
    _require(
        (since is None or (isinstance(since, datetime) and since.utcoffset() is not None))
        and (until is None or (isinstance(until, datetime) and until.utcoffset() is not None))
        and (since is None or until is None or since < until)
    )
    clauses = ["scope=?"]
    values: list[object] = [scope]
    if topic is not None:
        clauses.append("candidate=?")
        values.append(topic)
    if shown == "held":
        clauses.append("entry='decision' AND level='silent'")
    elif shown == "listed":
        clauses.append("level='list'")
    elif shown == "attention":
        clauses.append("level IN ('badge','nudge','alert')")
    elif shown == "report":
        clauses.append("entry='report'")
    if answer == "none":
        clauses.append("feedback IS NULL")
    elif answer is not None:
        clauses.append("feedback=?")
        values.append(answer)
    if since is not None:
        clauses.append("created_at>=?")
        values.append(stamp(since))
    if until is not None:
        clauses.append("created_at<?")
        values.append(stamp(until))
    return " AND ".join(clauses), values


def _shared(scope: str, trigger: str, now: datetime, day: str) -> dict[str, object]:
    """Check the values a decision and a report share; answer their shared columns."""
    _require_scope(scope)
    _require(trigger in TRIGGERS)
    _require_day(day)
    created_at = stamp(now)
    return {
        "scope": scope,
        "created_at": created_at,
        "day": day,
        "trigger": trigger,
        "exposures": 0,
        "observed_until": created_at,
    }


def _decision(shared: Mapping[str, object], entry: EpisodeInput) -> dict[str, object]:
    """Check one entry against the vocabularies and the table's bounds; answer its row. The row
    leaves the table's `language` column NULL: earlier versions stored a detected language
    there, which nothing reads."""
    _require(isinstance(entry, EpisodeInput))
    _require(
        entry.candidate in CANDIDATES
        and _hex_key(entry.item_key)
        and _hex_key(entry.state_hash)
        and entry.policy in POLICIES
        and _bounded_text(entry.policy_version, _POLICY_VERSION_CHARS)
        and (
            entry.model is None
            or (
                isinstance(entry.model, str)
                and len(entry.model) <= _MODEL_CHARS
                and _PRINTABLE.fullmatch(entry.model) is not None
            )
        )
        and (entry.kind is None or entry.kind in KINDS)
        and entry.work in WORK
        and entry.level in LEVELS
        and entry.timing in TIMINGS
        and (entry.timing == "at") == (entry.defer_until is not None)
        and (entry.defer_until is None or isinstance(entry.defer_until, datetime))
        and _bounded_text(entry.reason, _REASON_CHARS)
        and (entry.rules_level is None or entry.rules_level in LEVELS)
        and _count(entry.latency_ms, _LATENCY_MS)
        and _count(entry.input_tokens, _INPUT_TOKENS)
        and _count(entry.cost_micro_usd, MAX_SPEND_MICRO_USD)
    )
    evidence = _evidence(entry.evidence)
    key = evidence_key(entry.evidence)
    defer_until = None if entry.defer_until is None else stamp(entry.defer_until)
    parts = [shared["scope"], shared["day"], entry.item_key, key, entry.state_hash]
    parts += [entry.policy_version, entry.level, entry.timing, entry.work, entry.kind]
    parts += [defer_until]
    return {
        **shared,
        "episode_id": str(uuid.uuid4()),
        "entry": "decision",
        "candidate": entry.candidate,
        "item_key": entry.item_key,
        "state_hash": entry.state_hash,
        "evidence": evidence,
        "evidence_key": key,
        "policy": entry.policy,
        "policy_version": entry.policy_version,
        "model": entry.model,
        "answers": _answers(entry.answers_json),
        "terms": _terms(entry.terms),
        "kind": entry.kind,
        "work": entry.work,
        "level": entry.level,
        "timing": entry.timing,
        "defer_until": defer_until,
        "reason": entry.reason,
        "rules_level": entry.rules_level,
        "latency_ms": entry.latency_ms,
        "input_tokens": entry.input_tokens,
        "cost_micro_usd": entry.cost_micro_usd,
        "mission_id": _id(entry.mission_id),
        "feedback_revision": 0,
        "dedupe_key": sha256_hex("|".join("" if part is None else str(part) for part in parts)),
    }


def _insert(connection: sqlite3.Connection, row: Mapping[str, object]) -> None:
    connection.execute(
        f"INSERT INTO decision_episodes ({','.join(row)}) "
        f"VALUES ({','.join('?' for _ in row)}) ON CONFLICT(scope,dedupe_key) DO NOTHING",
        tuple(row.values()),
    )


def _prune(connection: sqlite3.Connection, scope: str, now: datetime) -> None:
    """Delete the scope's rows created before RETENTION_DAYS ago and those past the newest
    MAX_EPISODES."""
    connection.execute(
        "DELETE FROM decision_episodes WHERE scope=? AND (created_at<? OR episode_id IN "
        "(SELECT episode_id FROM decision_episodes WHERE scope=? "
        "ORDER BY created_at DESC,episode_id DESC LIMIT -1 OFFSET ?))",
        (scope, stamp(now - timedelta(days=RETENTION_DAYS)), scope, MAX_EPISODES),
    )


def _upsert_preference(
    connection: sqlite3.Connection, scope: str, name: str, value: str, updated_at: str
) -> None:
    connection.execute(
        "INSERT INTO decision_preferences (scope,name,value,revision,updated_at) "
        "VALUES (?,?,?,1,?) ON CONFLICT(scope,name) DO UPDATE SET value=excluded.value,"
        f"revision=min(revision+1,{_CEILING}),updated_at=excluded.updated_at",
        (scope, name, value, updated_at),
    )


def _prefs(connection: sqlite3.Connection, scope: str, now: datetime) -> Prefs:
    """This scope's preferences: muted topics and laters still pending.

    A malformed `later:` stamp counts as absent, as does one that is not later than *now*.
    """
    muted: set[str] = set()
    later: dict[str, datetime] = {}
    rows = connection.execute(
        "SELECT name, value FROM decision_preferences WHERE scope=?", (scope,)
    ).fetchall()
    for name, value in rows:
        if name.startswith("mute:"):
            if value == "1":
                muted.add(name.removeprefix("mute:"))
        elif name.startswith("later:"):
            try:
                until = parse_stamp(value)
            except ValueError:
                continue
            if until > now:
                later[name.removeprefix("later:")] = until
    return Prefs(muted=frozenset(muted), later=later)


def _spent(value: object, day: str) -> int:
    """The micro-USD a `spend` value holds for *day*; another day or a malformed value is 0."""
    found = _SPEND.fullmatch(value) if isinstance(value, str) else None
    return int(found[2]) if found is not None and found[1] == day else 0


_USAGE_BOUNDS = (
    ("calls", _CEILING),
    ("shadow_calls", _CEILING),
    ("tokens", _TOKENS_CEILING),
    ("held_back", _CEILING),
)


def _usage(value: object, day: str) -> Usage:
    """The Usage a scope's `usage` value holds for *day*; another day, or a value that is not
    exactly those keys in bounds, is all zero."""
    parsed = None
    if isinstance(value, str):
        try:
            parsed = json.loads(value, parse_constant=_no_constant)
        except ValueError, RecursionError:
            parsed = None
    if (
        isinstance(parsed, dict)
        and parsed.get("day") == day
        and set(parsed) == {"day", *(name for name, _ in _USAGE_BOUNDS)}
        and all(_bounded_int(parsed[name], high) for name, high in _USAGE_BOUNDS)
    ):
        return Usage(*(parsed[name] for name, _ in _USAGE_BOUNDS))
    return Usage(0, 0, 0, 0)


def _is_stamp(value: object) -> bool:
    try:
        parse_stamp(value)
    except ValueError:
        return False
    return True


def _showings(connection: sqlite3.Connection, scope: str) -> list[tuple[str, str]]:
    """The scope's logged showings as (item_key, stamp), oldest first (`_logged`)."""
    row = connection.execute(
        "SELECT value FROM decision_preferences WHERE scope=? AND name=?", (scope, SHOWINGS)
    ).fetchone()
    return [] if row is None else _logged(row[0])


def _logged(value: str) -> list[tuple[str, str]]:
    """A `loud` value's showings as (item_key, stamp), oldest first. A value that is not a JSON
    list counts as none, as does each entry that is not an item key and a stamp."""
    try:
        entries = json.loads(value, parse_constant=_no_constant)
    except ValueError, RecursionError:
        return []
    if not isinstance(entries, list):
        return []
    found = [
        (entry[0], entry[1])
        for entry in entries
        if isinstance(entry, list)
        and len(entry) == 2
        and entry[0] is not None
        and _hex_key(entry[0])
        and _is_stamp(entry[1])
    ]
    return sorted(found, key=lambda entry: (entry[1], entry[0]))


def _share(exposures: int, first: object, last: object, since: datetime) -> int:
    """How many of an episode's *exposures* happened at or after *since*, given the stamps of
    its first and last showing, the last at or after *since*: all of them when the first was,
    else its share as if they were spread evenly from the first to the last, rounded up and at
    least one (the last). An episode whose stamps do not read back counts none."""
    try:
        first_at, last_at = parse_stamp(first), parse_stamp(last)
    except ValueError:
        return 0
    if first_at >= since:
        return exposures
    inside = (last_at - since) // _MICROSECOND
    span = (last_at - first_at) // _MICROSECOND  # > 0: first < since <= last
    return max(1, -(-exposures * inside // span))


class DecisionLoopStore:
    """The `decision_episodes` rows and the `decision_preferences` of each scope."""

    def __init__(self, store: SessionStore) -> None:
        self.store = store

    @contextlib.contextmanager
    def _transaction(self, *, write: bool = True) -> Iterator[sqlite3.Connection]:
        try:
            require_private_store(self.store)
            with self.store._connection(write=write) as connection:
                yield connection
        except StateError, ProtectionError:
            raise DecisionLoopError("unavailable") from None

    def record(
        self,
        scope: str,
        entries: Sequence[EpisodeInput],
        *,
        trigger: str,
        now: datetime,
        day: str,
    ) -> list[Recorded]:
        """Record each entry as a decision episode; answer the episodes in input order.

        An entry whose scope, day, item_key, evidence, state_hash, policy_version, level,
        timing, work, kind and defer_until all equal a stored episode's answers that episode,
        with its id, columns, exposures and feedback unchanged.
        """
        shared = _shared(scope, trigger, now, day)
        rows = [_decision(shared, entry) for entry in entries]
        with self._transaction() as connection:
            for row in rows:
                _insert(connection, row)
            found = [
                connection.execute(
                    f"SELECT {_RECORDED} FROM decision_episodes WHERE scope=? AND dedupe_key=?",
                    (scope, row["dedupe_key"]),
                ).fetchone()
                for row in rows
            ]
            _prune(connection, scope, now)
        return [Recorded(*row) for row in found]

    def observe(self, scope: str, *, event: str, episode_ids: Sequence[str], now: datetime) -> int:
        """Apply a page event to the listed shown episodes of this scope; answer how many.

        *episode_ids* lists 1 to `MAX_OPEN_ITEMS` distinct ids, the most one observe of the
        launcher's items names. `shown` counts an exposure, `opened` and `acted` stamp their
        first time. Each moves observed_until forward to *now*. Held episodes and reports are
        left as they are.
        """
        _require_scope(scope)
        try:
            change = _EVENTS[event]
            ids = [_uuid(value) for value in episode_ids]
        except KeyError, TypeError, RoutineError:
            raise DecisionLoopError("invalid_request") from None
        _require(1 <= len(ids) <= MAX_OPEN_ITEMS and len(set(ids)) == len(ids))
        with self._transaction() as connection:
            return connection.execute(
                f"UPDATE decision_episodes SET {change}observed_until=max(observed_until,:now) "
                f"WHERE +scope=:scope AND {_SHOWN} "
                "AND episode_id IN (SELECT value FROM json_each(:ids))",
                {"now": stamp(now), "scope": scope, "ids": json.dumps(ids)},
            ).rowcount

    def report(
        self,
        scope: str,
        *,
        topic: str,
        source: str,
        trigger: str,
        now: datetime,
        day: str,
    ) -> str:
        """Store the user's "you should have told me" about *topic*; answer the report's id.

        A report is its own missed feedback; it takes no feedback and no page event.
        """
        _require(topic in TOPICS and source in SOURCES)
        row = _shared(scope, trigger, now, day)
        episode_id = str(uuid.uuid4())
        row.update(
            episode_id=episode_id,
            entry="report",
            candidate=topic,
            evidence="{}",
            evidence_key=sha256_hex("{}"),
            policy="rules",
            policy_version=POLICY_VERSION,
            reason="owner_report",
            dedupe_key=sha256_hex(str(uuid.uuid4())),
            feedback="missed",
            feedback_source=source,
            feedback_revision=1,
            feedback_at=row["created_at"],
        )
        with self._transaction() as connection:
            _insert(connection, row)
            _prune(connection, scope, now)
        return episode_id

    def feedback(
        self,
        scope: str,
        *,
        episode_id: str,
        value: str,
        reason: str | None,
        source: str,
        now: datetime,
    ) -> tuple[Recorded, Prefs]:
        """Record the user's feedback on a decision episode of this scope; answer the episode
        and the scope's preferences as this transaction leaves them.

        Shown episodes take `FEEDBACK_SHOWN` and silent ones `FEEDBACK_HELD`, from any source;
        anything else is `not_applicable`. Only `wrong` takes a reason. A new value replaces the
        last one, its reason included, and adds one to the revision. `later` also upserts
        `later:<candidate>` = *now* + `LATER`; `mute` also upserts `mute:<candidate>`.
        """
        _require_scope(scope)
        try:
            episode_id = _uuid(episode_id)
        except RoutineError:
            raise DecisionLoopError("invalid_request") from None
        _require(value in FEEDBACK_SHOWN or value in FEEDBACK_HELD)
        _require(reason is None or (value == "wrong" and reason in FEEDBACK_REASONS))
        _require(source in SOURCES)
        stamped = stamp(now)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT candidate, level FROM decision_episodes "
                "WHERE scope=? AND episode_id=? AND entry='decision'",
                (scope, episode_id),
            ).fetchone()
            if row is None:
                raise DecisionLoopError("unknown_item")
            candidate, level = row
            shown = level in SHOWN_LEVELS and value in FEEDBACK_SHOWN
            held = level == "silent" and value in FEEDBACK_HELD
            if not (shown or held):
                raise DecisionLoopError("not_applicable")
            connection.execute(
                "UPDATE decision_episodes SET feedback=?,feedback_reason=?,feedback_source=?,"
                f"feedback_at=?,feedback_revision=min(feedback_revision+1,{_CEILING}) "
                "WHERE scope=? AND episode_id=?",
                (value, reason, source, stamped, scope, episode_id),
            )
            if value == "later":
                _upsert_preference(
                    connection, scope, f"later:{candidate}", stamp(now + LATER), stamped
                )
            elif value == "mute":
                _upsert_preference(connection, scope, f"mute:{candidate}", "1", stamped)
            found = connection.execute(
                f"SELECT {_RECORDED} FROM decision_episodes WHERE scope=? AND episode_id=?",
                (scope, episode_id),
            ).fetchone()
            return Recorded(*found), _prefs(connection, scope, now)

    def preferences(self, scope: str, now: datetime) -> Prefs:
        """The scope's muted topics and its still-pending laters."""
        _require_scope(scope)
        with self._transaction(write=False) as connection:
            return _prefs(connection, scope, now)

    def unmute(self, scope: str, candidate: str, now: datetime) -> Prefs:
        _require_scope(scope)
        _require(candidate in CANDIDATES)
        stamped = stamp(now)
        with self._transaction() as connection:
            _upsert_preference(connection, scope, f"mute:{candidate}", "0", stamped)
            return _prefs(connection, scope, now)

    def mark_review_seen(self, scope: str, now: datetime) -> Prefs:
        _require_scope(scope)
        stamped = stamp(now)
        with self._transaction() as connection:
            _upsert_preference(connection, scope, "review_seen", stamped, stamped)
            return _prefs(connection, scope, now)

    def review_seen_at(self, scope: str) -> datetime | None:
        _require_scope(scope)
        with self._transaction(write=False) as connection:
            row = connection.execute(
                "SELECT value FROM decision_preferences WHERE scope=? AND name='review_seen'",
                (scope,),
            ).fetchone()
        if row is None:
            return None
        try:
            return parse_stamp(row[0])
        except ValueError:
            return None

    def add_spend(self, scope: str, day: str, micro_usd: int, now: datetime) -> int:
        """Add *micro_usd* to the scope's measured spend on *day*; answer its total for the day.

        The `spend` preference holds one day as "<day>:<micro-USD>": when it holds another day,
        or is malformed, the total starts again at *micro_usd*.
        """
        _require_scope(scope)
        _require_day(day)
        _require(micro_usd is not None and _count(micro_usd, MAX_SPEND_MICRO_USD))
        stamped = stamp(now)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT value FROM decision_preferences WHERE scope=? AND name='spend'",
                (scope,),
            ).fetchone()
            total = (0 if row is None else _spent(row[0], day)) + micro_usd
            _upsert_preference(connection, scope, "spend", f"{day}:{total}", stamped)
        return total

    def spent_on(self, day: str) -> int:
        """The measured spend on *day* in micro-USD, summed over every scope; a scope whose
        `spend` holds another day adds nothing."""
        _require_day(day)
        with self._transaction(write=False) as connection:
            rows = connection.execute(
                "SELECT value FROM decision_preferences WHERE name='spend'"
            ).fetchall()
        return sum(_spent(value, day) for (value,) in rows)

    def add_usage(
        self,
        scope: str,
        day: str,
        *,
        calls: int,
        shadow_calls: int,
        tokens: int,
        held_back: int,
        micro_usd: int,
        now: datetime,
    ) -> int:
        """Add *micro_usd* to the scope's measured spend on *day* as `add_spend` does, and
        *calls*, *shadow_calls*, *tokens* and *held_back* to its "usage" preference for the same
        day, in the one transaction; answer the day's spend. *held_back* counts requests, not
        evidence entries: the cycle adds 1 for each request never-send held something back from
        (`Usage`).

        "usage" holds canonical JSON {"calls","day","held_back","shadow_calls","tokens"}; a
        stored value for another day, or one that does not read back, restarts every count at
        0. Each count saturates at `_CEILING`, tokens at `_TOKENS_CEILING`.
        """
        _require_scope(scope)
        _require_day(day)
        _require(micro_usd is not None and _count(micro_usd, MAX_SPEND_MICRO_USD))
        _require(_bounded_int(calls, _CEILING))
        _require(_bounded_int(shadow_calls, _CEILING))
        _require(_bounded_int(tokens, _TOKENS_CEILING))
        _require(_bounded_int(held_back, _CEILING))
        stamped = stamp(now)
        with self._transaction() as connection:
            spend_row = connection.execute(
                "SELECT value FROM decision_preferences WHERE scope=? AND name='spend'", (scope,)
            ).fetchone()
            total = (0 if spend_row is None else _spent(spend_row[0], day)) + micro_usd
            _upsert_preference(connection, scope, "spend", f"{day}:{total}", stamped)

            usage_row = connection.execute(
                "SELECT value FROM decision_preferences WHERE scope=? AND name='usage'", (scope,)
            ).fetchone()
            before = _usage(usage_row[0] if usage_row is not None else None, day)
            after = {
                "day": day,
                "calls": min(before.calls + calls, _CEILING),
                "shadow_calls": min(before.shadow_calls + shadow_calls, _CEILING),
                "tokens": min(before.tokens + tokens, _TOKENS_CEILING),
                "held_back": min(before.held_back + held_back, _CEILING),
            }
            _upsert_preference(connection, scope, "usage", canonical_json(after), stamped)
        return total

    def usage_on(self, day: str) -> Usage:
        """The scope-summed Usage on *day*, as `spent_on` sums spend: a scope whose "usage"
        holds another day, or a value that does not read back, adds nothing."""
        _require_day(day)
        with self._transaction(write=False) as connection:
            rows = connection.execute(
                "SELECT value FROM decision_preferences WHERE name='usage'"
            ).fetchall()
        totals = [_usage(value, day) for (value,) in rows]
        return Usage(
            calls=sum(usage.calls for usage in totals),
            shadow_calls=sum(usage.shadow_calls for usage in totals),
            tokens=sum(usage.tokens for usage in totals),
            held_back=sum(usage.held_back for usage in totals),
        )

    def add_showing(self, scope: str, item_key: str, at: datetime) -> None:
        """Log that the scope's item *item_key* began to show a nudge or an alert at *at*.

        The `loud` preference holds the log as canonical JSON, a list of [item_key, stamp]
        pairs oldest first: this showing joins it, showings from more than an hour before *at*
        leave it, and only the newest `MAX_SHOWINGS` stay, so it stays far below
        MAX_JSON_BYTES. An item that shows again is logged again. A logged value or entry that
        does not read back counts as no showing, and this write drops it.
        """
        _require_scope(scope)
        _require(item_key is not None and _hex_key(item_key))
        _require(isinstance(at, datetime))
        stamped = stamp(at)
        cutoff = stamp(at - _HOUR)
        with self._transaction() as connection:
            logged = sorted(
                [*_showings(connection, scope), (item_key, stamped)],
                key=lambda entry: (entry[1], entry[0]),
            )
            kept = [entry for entry in logged if entry[1] >= cutoff][-MAX_SHOWINGS:]
            _upsert_preference(connection, scope, SHOWINGS, canonical_json(kept), stamped)

    def showings_since(
        self, scope: str, since: datetime, *, exclude_item: str | None = None
    ) -> int:
        """How many logged showings (`add_showing`) happened at or after *since*, summed over
        every scope's log as `spent_on` sums spend: the user's attention spans the workspaces.
        This scope's showings of *exclude_item* are left out; another scope's item of the same
        key still counts."""
        _require_scope(scope)
        _require(isinstance(since, datetime))
        _require(_hex_key(exclude_item))
        start = stamp(since)
        with self._transaction(write=False) as connection:
            rows = connection.execute(
                "SELECT scope, value FROM decision_preferences WHERE name=?", (SHOWINGS,)
            ).fetchall()
        return sum(
            1
            for logged_scope, value in rows
            for key, at in _logged(value)
            if at >= start and (logged_scope != scope or key != exclude_item)
        )

    def exposures_since(self, scope: str, since: datetime) -> dict[str, int]:
        """Each of the scope's items shown at or after *since*, with how often: the `shown`
        events (`observe`) its decision episodes counted then, as `_share` estimates them per
        episode. An item's sum stops at the counter's ceiling; an item not shown is left out."""
        _require_scope(scope)
        _require(isinstance(since, datetime))
        start = stamp(since)
        with self._transaction(write=False) as connection:
            rows = connection.execute(
                "SELECT item_key, exposures, first_exposed_at, last_exposed_at "
                "FROM decision_episodes WHERE scope=? AND entry='decision' "
                "AND item_key IS NOT NULL AND exposures>0 AND last_exposed_at>=?",
                (scope, start),
            ).fetchall()
        window = parse_stamp(start)
        found: dict[str, int] = {}
        for key, exposures, first, last in rows:
            count = _share(exposures, first, last, window)
            if count:
                found[key] = min(found.get(key, 0) + count, _CEILING)
        return found

    def get_json(self, scope: str, name: str) -> object:
        """The scope's stored `lambda`, `learned`, `second_looks`, `project_plans`,
        `message_drafts`, `saved_checks` or `inbox_triage` value; None when unset or unreadable."""
        _require_scope(scope)
        _require(name in JSON_NAMES)
        with self._transaction(write=False) as connection:
            row = connection.execute(
                "SELECT value FROM decision_preferences WHERE scope=? AND name=?", (scope, name)
            ).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row[0], parse_constant=_no_constant)
        except ValueError, RecursionError:
            return None

    def set_json(self, scope: str, name: str, value: object, now: datetime) -> None:
        """Store *value* as the scope's `lambda`, `learned`, `second_looks`, `project_plans`,
        `message_drafts`, `saved_checks` or `inbox_triage`, as canonical JSON of at most
        MAX_JSON_BYTES."""
        _require_scope(scope)
        _require(name in JSON_NAMES)
        try:
            text = canonical_json(value)
        except TypeError, ValueError, RecursionError:
            raise DecisionLoopError("invalid_request") from None
        _require(len(text) <= MAX_JSON_BYTES)  # ASCII, so one byte per character
        stamped = stamp(now)
        with self._transaction() as connection:
            _upsert_preference(connection, scope, name, text, stamped)

    def forget_learning(self, scope: str, *, restart_at: datetime | None) -> None:
        """Delete the scope's "learned" snapshot in one transaction; with *restart_at* also
        delete its "lambda" and upsert "learn_since" to that stamp. Episodes, their labels and
        every other preference stay.
        """
        _require_scope(scope)
        _require(restart_at is None or isinstance(restart_at, datetime))
        with self._transaction() as connection:
            connection.execute(
                "DELETE FROM decision_preferences WHERE scope=? AND name='learned'", (scope,)
            )
            if restart_at is not None:
                connection.execute(
                    "DELETE FROM decision_preferences WHERE scope=? AND name='lambda'", (scope,)
                )
                stamped = stamp(restart_at)
                _upsert_preference(connection, scope, "learn_since", stamped, stamped)

    def learn_since(self, scope: str) -> datetime | None:
        """The scope's "learn_since" stamp (`forget_learning`'s *restart_at*); None when unset
        or malformed."""
        _require_scope(scope)
        with self._transaction(write=False) as connection:
            row = connection.execute(
                "SELECT value FROM decision_preferences WHERE scope=? AND name='learn_since'",
                (scope,),
            ).fetchone()
        if row is None:
            return None
        try:
            return parse_stamp(row[0])
        except ValueError:
            return None

    def decision_records(
        self,
        scope: str,
        *,
        topic: str | None = None,
        shown: str | None = None,
        answer: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        before: tuple[str, str] | None = None,
        limit: int,
    ) -> dict[str, Any]:
        """List matching episodes newest first, with counts before the keyset cursor."""
        where, values = _decision_filter(
            scope, topic=topic, shown=shown, answer=answer, since=since, until=until
        )
        _require(isinstance(limit, int) and not isinstance(limit, bool) and 1 <= limit <= 50)
        cursor: tuple[str, str] | None = None
        if before is not None:
            _require(isinstance(before, tuple) and len(before) == 2)
            try:
                at = stamp(parse_stamp(before[0]))
            except ValueError:
                raise DecisionLoopError("invalid_request") from None
            episode_id = _id(before[1])
            if episode_id is None:
                raise DecisionLoopError("invalid_request")
            cursor = (at, episode_id)
        with self._transaction(write=False) as connection:
            connection.execute("BEGIN")
            total, deletable = connection.execute(
                f"SELECT count(*),count(*) FILTER (WHERE NOT {_OPEN_SUGGESTION}) "
                f"FROM decision_episodes WHERE {where}",
                (scope, *values),
            ).fetchone()
            page_where = where
            page_values: list[object] = list(values)
            if cursor is not None:
                page_where += " AND (created_at,episode_id)<(?,?)"
                page_values.extend(cursor)
            rows = connection.execute(
                "SELECT episode_id,created_at,entry,candidate,level,policy,feedback,"
                f"{_OPEN_SUGGESTION} FROM decision_episodes WHERE {page_where} "
                "ORDER BY created_at DESC,episode_id DESC LIMIT ?",
                (scope, *page_values, limit + 1),
            ).fetchall()
        more = len(rows) > limit
        rows = rows[:limit]
        return {
            "total": total,
            "deletable": deletable,
            "rows": [
                {
                    "id": episode_id,
                    "at": at,
                    "entry": entry,
                    "topic": candidate,
                    "level": level,
                    "policy": policy,
                    "answer": feedback,
                    "kept": "open_suggestion" if kept else None,
                }
                for episode_id, at, entry, candidate, level, policy, feedback, kept in rows
            ],
            "next": {"at": rows[-1][1], "id": rows[-1][0]} if more else None,
        }

    def delete_decisions(
        self,
        scope: str,
        *,
        ids: Sequence[str] | None = None,
        topic: str | None = None,
        shown: str | None = None,
        answer: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> tuple[int, int]:
        """Delete matching episodes except those linked to an open suggestion.

        Return deleted and matched-but-kept counts from one write transaction.
        """
        where, values = _decision_filter(
            scope, topic=topic, shown=shown, answer=answer, since=since, until=until
        )
        if ids is not None:
            _require(
                isinstance(ids, Sequence)
                and not isinstance(ids, (str, bytes))
                and 1 <= len(ids) <= 50
                and all(value is None for value in (topic, shown, answer, since, until))
            )
            selected = [_id(value) for value in ids]
            _require(None not in selected and len(set(selected)) == len(selected))
            where += " AND episode_id IN (SELECT value FROM json_each(?))"
            values.append(json.dumps(selected))
        else:
            _require(
                until is not None
                and any(value is not None for value in (topic, shown, answer, since))
            )
        with self._transaction() as connection:
            matched = connection.execute(
                f"SELECT count(*) FROM decision_episodes WHERE {where}", values
            ).fetchone()[0]
            deleted = connection.execute(
                f"DELETE FROM decision_episodes WHERE {where} AND NOT {_OPEN_SUGGESTION}",
                (*values, scope),
            ).rowcount
        return deleted, matched - deleted

    def delete_episodes(self, scope: str) -> int:
        """Delete every episode row of the scope; answer how many. Preferences stay."""
        _require_scope(scope)
        with self._transaction() as connection:
            return connection.execute(
                "DELETE FROM decision_episodes WHERE scope=?", (scope,)
            ).rowcount
