"""The Decision page's read-only numbers: the user's 30-day counts, recent decisions, muted
candidates, the stored episode range and today's count; the rows the learning fit and
calibration read (`learning_rows`), the shadow comparison reads (`shadow_rows`) and the trial
comparison reads (`trial_rows`); the rows `decision_loop.owner.fill` turns into the Decision
model's user part (`owner_rows`); and the weekly held-back audit and its launcher nudge
(`audit`, `review`); the Data page's scoped inventory (`data_summary`) and one stored decision
with its current item text (`decision_record`).

A port of the earlier engine's statistics onto `decision_episodes` and `decision_preferences`,
dropping the `study` and `by_mode` breakdowns the earlier engine kept; `page_parts` fills the
shape `wire.page_view` reads, its `held_back` now `audit`'s stable weekly sample. Each method
opens one private read connection (`page_parts` opens a second through `audit`); `data_summary`
starts an explicit read transaction across its tables. Nothing here writes. `counts` are plain
totals, left for the caller to turn into a rate. Refusals
are `DecisionLoopError("unavailable")` for store and protection failures, the pattern `store.py`
and `agenda.py` already use.
"""

from __future__ import annotations

import contextlib
import json
import math
import sqlite3
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from typing import Any, Protocol

from viki_agent.automation.routines import parse_stamp, stamp
from viki_agent.conversations.drafts import require_private_store
from viki_agent.decision_loop.agenda import CLOSED_KEEP_DAYS
from viki_agent.decision_loop.changes import MAX_CHANGES
from viki_agent.decision_loop.contracts import (
    AUDIT_SIZE,
    CHANGES,
    FEEDBACK_HELD,
    FEEDBACK_SHOWN,
    MAX_CALLS,
    MAX_EPISODES,
    MAX_LEARN_ROWS,
    MAX_OPEN_ITEMS,
    MESSAGE_DRAFTS,
    RETENTION_DAYS,
    DecisionLoopError,
    ItemRecord,
    LookRow,
    sha256_hex,
)
from viki_agent.decision_loop.missions import _day
from viki_agent.decision_loop.proposals import MAX_PROPOSALS
from viki_agent.decision_loop.state import MUTED_OR_WRONG, RECENT, RECENT_FEEDBACK
from viki_agent.decision_loop.store import _SHOWN
from viki_agent.protection import ProtectionError
from viki_agent.storage import SessionStore, StateError

COUNT_WINDOW_DAYS = 30  # the page's `counts`
MAX_RECENT = 20
MAX_DRAFT_ROWS = 400  # the item rows `draft_rows` reads
_SHOWN_LEVELS = ("list", "badge", "nudge", "alert")  # the user could see these; silent cannot
_FEEDBACK_VALUES = (*FEEDBACK_SHOWN, *FEEDBACK_HELD)
_COLUMNS = "episode_id,entry,created_at,day,candidate,item_key,policy,level,timing,reason,feedback"
_HELD_BACK_FIELDS = ("episode_id", "created_at", "candidate", "level", "reason", "feedback")
_MUTED_REASON = "rules:muted_by_owner"  # `audit` leaves these out; the user already said so
_SHADOWED = "answers LIKE '%\"shadow\"%'"  # `shadow_rows`: answers naming `cycle.SHADOW`
_TRIALED = "answers LIKE '%\"trial\"%'"  # `trial_rows`: answers naming `cycle.TRIAL`


class ItemLookup(Protocol):
    """What `page_parts` and `audit` need of `DecisionAgenda` to fill a row's `why`."""

    def item(self, scope: str, item_key: str) -> ItemRecord | None: ...


def _why(agenda: ItemLookup | None, scope: str, item_key: str | None) -> str:
    """The row's current "why now" text, from its item's sealed body, or "" once it has none:
    no *agenda* was given, the episode stored no `item_key`, or the item is gone."""
    if agenda is None or item_key is None:
        return ""
    found = agenda.item(scope, item_key)
    return "" if found is None else found.body.why


def _counts(rows: Sequence[sqlite3.Row]) -> dict[str, int]:
    """The page's `counts` over *rows* (already the scope's rows from the last
    `COUNT_WINDOW_DAYS`): how many were shown, and each feedback value."""
    counts: dict[str, int] = dict.fromkeys(_FEEDBACK_VALUES, 0)
    for row in rows:
        if row["feedback"] is not None:
            counts[row["feedback"]] += 1
    counts["shown"] = sum(
        1 for row in rows if row["entry"] == "report" or row["level"] in _SHOWN_LEVELS
    )
    return counts


@dataclass(frozen=True)
class LearningRow:
    """One decision episode as the learning fit, the calibration tables and the weekly audit
    read it: a shown or labelled "decision" row (`learning_rows`) or a silent one `audit` named;
    the shadow comparison reads its rows in this shape too (`shadow_rows`).

    `p` is the model's `terms["p"]` at decision time, when that reads back as a finite
    probability in 0..1, else None (also None off the rules policy, which stores no terms).
    `label` is `label_of`'s verdict on the user's response. Every stamp but `created_at` is
    None when unset or unreadable; an episode whose own `created_at` does not read back is
    never built -- `learning_rows` and `shadow_rows` drop its row instead.
    """

    episode_id: str
    created_at: datetime
    candidate: str
    level: str
    timing: str
    policy: str
    answers_json: str | None
    p: float | None
    feedback: str | None
    feedback_reason: str | None
    feedback_at: datetime | None
    first_exposed_at: datetime | None
    opened_at: datetime | None
    acted_at: datetime | None
    label: int | None


@dataclass(frozen=True)
class OwnerRows:
    """The scope's `entry='decision'` rows `decision_loop.owner.fill` turns into the Decision
    model's user part (`owner_rows`), newest first, each capped to its own `state.py` limit
    and leaving out every row with no `item_key` (it carries no title to resolve): `seen` as
    (item_key, level) for rows shown (level not silent) created at or after a given instant;
    `marked` as item_key for rows the user labelled mute or wrong; `feedback` as (item_key,
    level, feedback) for rows carrying any feedback, silent held-back ones (missed,
    right_to_hold) included.
    """

    seen: tuple[tuple[str, str], ...] = ()
    marked: tuple[str, ...] = ()
    feedback: tuple[tuple[str, str, str], ...] = ()


def label_of(
    feedback: str | None, opened_at: datetime | None, acted_at: datetime | None
) -> int | None:
    """The row's learning label: `useful` or `missed` feedback is 1; `wrong`, `mute` or
    `right_to_hold` is 0; otherwise opened or acted is 1; a bare "later" (Not now) or no reply
    at all answers None (unlabelled)."""
    if feedback in ("useful", "missed"):
        return 1
    if feedback in ("wrong", "mute", "right_to_hold"):
        return 0
    if opened_at is not None or acted_at is not None:
        return 1
    return None


def _current_offset() -> timedelta | None:
    """This machine's current local UTC offset, to tell a fixed offset that merely coincides
    with it from one `datetime.astimezone()` built to mean "system local" (`_week_midnight`)."""
    return datetime.now().astimezone().utcoffset()


def _system_local(naive: datetime) -> datetime:
    """*naive* (a local midnight) converted to aware time by this machine's own rule -- its
    real DST transitions included, per plain `datetime.astimezone()` (`_week_midnight`)."""
    return naive.astimezone()


def _week_midnight(monday: date, zone: tzinfo | None) -> datetime:
    """Midnight of the Monday *monday*, in *zone*'s own local wall time.

    A `zoneinfo.ZoneInfo` zone is attached to that naive midnight as given and resolves its own
    DST rule for *monday*'s date; any other fixed offset is attached the same way and used
    exactly as given, with no DST to adjust for. The one exception is a fixed `datetime.timezone`
    that equals this machine's current local offset -- `_local`'s "system local" -- which instead
    goes through the system's own rule (`_system_local`), so it follows this machine's real DST
    transitions for *monday* rather than staying at today's offset.
    """
    if isinstance(zone, timezone) and zone.utcoffset(None) == _current_offset():
        return _system_local(datetime.combine(monday, time()))
    return datetime.combine(monday, time(), tzinfo=zone)


def week_start(now: datetime) -> datetime:
    """Monday 00:00 of *now*'s week, in local wall time on *now*'s own clock, including across
    a DST change (see `_week_midnight`)."""
    return _week_midnight(now.date() - timedelta(days=now.weekday()), now.tzinfo)


def _optional_stamp(value: str | None) -> datetime | None:
    """*value* as an aware instant, or None when it is unset or does not read back."""
    if value is None:
        return None
    try:
        return parse_stamp(value)
    except ValueError:
        return None


def _term_p(text: str | None) -> float | None:
    """The stored `terms` JSON's "p", when it reads back as a finite probability in 0..1 (an
    int too large for `float` to hold, such as a stray 10**400, reads back as None too)."""
    if text is None:
        return None
    try:
        parsed = json.loads(text)
    except ValueError, RecursionError:
        return None
    if not isinstance(parsed, dict):
        return None
    value = parsed.get("p")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        value = float(value)
    except OverflowError:
        return None
    return value if math.isfinite(value) and 0.0 <= value <= 1.0 else None


class DecisionLoopStats:
    """Read-only statistics and decision details for one scope."""

    def __init__(self, store: SessionStore) -> None:
        self.store = store

    @contextlib.contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        try:
            require_private_store(self.store)
            with self.store._connection(write=False) as connection:
                yield connection
        except StateError, ProtectionError:
            raise DecisionLoopError("unavailable") from None

    def data_summary(self, scope: str, now: datetime) -> dict[str, Any]:
        """One snapshot of the scope's stored Decision data and its last 366 calendar days."""
        day_floor = (now.date() - timedelta(days=365)).isoformat()
        change_names = json.dumps(CHANGES)
        with self._transaction() as connection:
            connection.execute("BEGIN")
            episodes = connection.execute(
                "SELECT count(*) FILTER (WHERE entry='decision'),"
                "count(*) FILTER (WHERE entry='report'),min(created_at),max(created_at) "
                "FROM decision_episodes WHERE scope=?",
                (scope,),
            ).fetchone()
            items = connection.execute(
                "SELECT count(*) FILTER (WHERE state='open'),"
                "count(*) FILTER (WHERE state<>'open'),min(created_at),max(updated_at) "
                "FROM decision_items WHERE scope=?",
                (scope,),
            ).fetchone()
            calls = connection.execute(
                "SELECT count(*),min(created_at),max(created_at) FROM decision_calls WHERE scope=?",
                (scope,),
            ).fetchone()
            chats = connection.execute(
                "SELECT count(*),count(*) FILTER (WHERE state IN ('starting','running')),"
                "min(created_at),max(created_at) FROM decision_missions WHERE scope=?",
                (scope,),
            ).fetchone()
            proposals = connection.execute(
                "SELECT count(*) FILTER (WHERE name NOT IN (SELECT value FROM json_each(?))),"
                "count(*) FILTER (WHERE name IN (SELECT value FROM json_each(?))),"
                "min(created_at),max(created_at) FROM decision_proposals WHERE scope=?",
                (change_names, change_names, scope),
            ).fetchone()
            preferences = connection.execute(
                "SELECT count(*) FILTER (WHERE name GLOB 'mute:*' AND value='1'),"
                "count(*) FILTER (WHERE name='learned'),"
                "max(updated_at) FILTER (WHERE name IN ('learned','lambda','learn_since') "
                "OR name GLOB 'mute:*') FROM decision_preferences WHERE scope=?",
                (scope,),
            ).fetchone()
            days = connection.execute(
                "SELECT day,"
                "sum(entry='decision' AND level='silent'),"
                "sum(entry='decision' AND level='list'),"
                "sum(entry='decision' AND level IN ('badge','nudge','alert')),"
                "sum(entry='report'),"
                "coalesce(sum(entry='decision' AND level<>'silent' AND feedback='useful'),0),"
                "coalesce(sum(entry='decision' AND level<>'silent' AND feedback='wrong'),0),"
                "coalesce(sum(entry='decision' AND level<>'silent' AND feedback='later'),0),"
                "coalesce(sum(entry='decision' AND level<>'silent' AND feedback='mute'),0),"
                "coalesce(sum(entry='decision' AND level='silent' AND feedback='right_to_hold'),0),"
                "coalesce(sum(entry='decision' AND level='silent' AND feedback='missed'),0) "
                "FROM decision_episodes WHERE scope=? AND day>=? GROUP BY day ORDER BY day",
                (scope, day_floor),
            ).fetchall()
            topics = connection.execute(
                "SELECT candidate,"
                "sum(entry='decision' AND level='silent') AS held,"
                "sum(entry='decision' AND level<>'silent') AS shown,"
                "sum(entry='report') AS reports,"
                "coalesce(sum(entry='decision' AND level<>'silent' AND feedback='useful'),0),"
                "coalesce(sum(entry='decision' AND level<>'silent' AND feedback='wrong'),0) "
                "FROM decision_episodes WHERE scope=? GROUP BY candidate "
                "ORDER BY held+shown+reports DESC,candidate",
                (scope,),
            ).fetchall()
        return {
            "decisions": {
                "decisions": episodes[0],
                "reports": episodes[1],
                "oldest": episodes[2],
                "newest": episodes[3],
                "max": MAX_EPISODES,
                "retention_days": RETENTION_DAYS,
            },
            "suggestions": {
                "open": items[0],
                "closed": items[1],
                "oldest": items[2],
                "newest": items[3],
                "max_open": MAX_OPEN_ITEMS,
                "closed_days": CLOSED_KEEP_DAYS,
            },
            "calls": {"count": calls[0], "oldest": calls[1], "newest": calls[2], "max": MAX_CALLS},
            "chats": {
                "count": chats[0],
                "running": chats[1],
                "oldest": chats[2],
                "newest": chats[3],
            },
            "proposals": {
                "wordings": proposals[0],
                "changes": proposals[1],
                "oldest": proposals[2],
                "newest": proposals[3],
                "max_wordings": MAX_PROPOSALS,
                "max_changes": MAX_CHANGES,
            },
            "preferences": {
                "muted": preferences[0],
                "learned": bool(preferences[1]),
                "updated": preferences[2],
            },
            "days": [
                dict(
                    zip(
                        (
                            "day",
                            "held",
                            "listed",
                            "attention",
                            "reports",
                            "useful",
                            "wrong",
                            "later",
                            "mute",
                            "right_to_hold",
                            "missed",
                        ),
                        row,
                        strict=True,
                    )
                )
                for row in days
            ],
            "topics": [
                dict(
                    zip(
                        ("topic", "held", "shown", "reports", "useful", "wrong"),
                        row,
                        strict=True,
                    )
                )
                for row in topics
            ],
        }

    def decision_record(
        self, scope: str, episode_id: str, *, agenda: ItemLookup | None = None
    ) -> dict[str, Any] | None:
        """The scope's stored episode and the current body of its linked item, if present."""
        with self._transaction() as connection:
            connection.execute("BEGIN")
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT episode_id,created_at,entry,candidate,level,policy,item_key,feedback,"
                "feedback_reason,feedback_at,exposures,opened_at,acted_at,input_tokens,"
                "cost_micro_usd FROM decision_episodes WHERE scope=? AND episode_id=?",
                (scope, episode_id),
            ).fetchone()
            if row is None:
                return None
            kept = connection.execute(
                "SELECT 1 FROM decision_items WHERE scope=? AND episode_id=? AND state='open' "
                "LIMIT 1",
                (scope, episode_id),
            ).fetchone()
        item = agenda.item(scope, row["item_key"]) if agenda and row["item_key"] else None
        return {
            "id": row["episode_id"],
            "at": row["created_at"],
            "entry": row["entry"],
            "topic": row["candidate"],
            "level": row["level"],
            "policy": row["policy"],
            "why": "" if item is None else item.body.why,
            "evidence": () if item is None else item.body.evidence[:20],
            "answer": row["feedback"],
            "answer_reason": row["feedback_reason"],
            "answered_at": row["feedback_at"],
            "shown": row["exposures"],
            "opened_at": row["opened_at"],
            "acted_at": row["acted_at"],
            "tokens": row["input_tokens"],
            "cost_usd": None if row["cost_micro_usd"] is None else row["cost_micro_usd"] / 1e6,
            "kept": "open_suggestion" if kept else None,
        }

    def page_parts(
        self, scope: str, now: datetime, *, agenda: ItemLookup | None = None
    ) -> dict[str, Any]:
        """The Decision page's stats-derived members at *now*: `counts` over the last
        `COUNT_WINDOW_DAYS`; the newest `MAX_RECENT` decisions; this week's `audit` sample as
        `held_back`; the scope's `muted` candidates; its stored `data` range; and `today`'s
        decision count.

        `agenda`, when given, fills each recent or held-back row's `why` from the matching
        item's current body (see `_why`).
        """
        with self._transaction() as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                f"SELECT {_COLUMNS} FROM decision_episodes "
                "WHERE scope=? ORDER BY created_at DESC,episode_id DESC",
                (scope,),
            ).fetchall()
            muted_rows = connection.execute(
                "SELECT name,value FROM decision_preferences WHERE scope=?", (scope,)
            ).fetchall()
        decisions = [row for row in rows if row["entry"] == "decision"]
        since = stamp(now - timedelta(days=COUNT_WINDOW_DAYS))
        today = now.date().isoformat()
        return {
            "counts": _counts([row for row in rows if row["created_at"] >= since]),
            "recent": [
                {
                    "episode_id": row["episode_id"],
                    "created_at": row["created_at"],
                    "candidate": row["candidate"],
                    "level": row["level"],
                    "policy": row["policy"],
                    "reason": row["reason"],
                    "why": _why(agenda, scope, row["item_key"]),
                    "feedback": row["feedback"],
                }
                for row in decisions[:MAX_RECENT]
            ],
            "held_back": self.audit(scope, now, agenda=agenda),
            "muted": frozenset(
                name.removeprefix("mute:")
                for name, value in muted_rows
                if name.startswith("mute:") and value == "1"
            ),
            "data": {
                "episodes": len(rows),
                "oldest": rows[-1]["created_at"] if rows else None,
                "max": MAX_EPISODES,
                "retention_days": RETENTION_DAYS,
            },
            "today": {"decisions": sum(1 for row in decisions if row["day"] == today)},
        }

    def learning_rows(
        self, scope: str, *, since: datetime | None, limit: int = MAX_LEARN_ROWS
    ) -> list[LearningRow]:
        """The scope's decision rows the learning fit and calibration read: created at or
        after *since* (None for all), shown or labelled -- a silent row with no feedback yet is
        held back for the weekly audit instead, not fitted -- newest first, at most *limit*.

        A row whose `created_at` does not read back as a stamp is dropped; its other stamps and
        its `terms["p"]` answer None instead when they do not (see `LearningRow`).
        """
        return self._decision_rows(
            scope, f"({_SHOWN} OR feedback IS NOT NULL)", since=since, limit=limit
        )

    def shadow_rows(
        self, scope: str, *, since: datetime | None, limit: int = MAX_LEARN_ROWS
    ) -> list[LearningRow]:
        """The scope's decision rows the shadow comparison reads (`calibration.shadow`): created
        at or after *since* (None for all) whose stored answers name the shadow ask's member
        (`cycle.SHADOW`), silent and unlabelled ones included -- the shadow is asked about each
        fresh answer, whether the live version showed it or held it back -- newest first, at
        most *limit*. Only that name is matched here; `calibration.shadow` decodes and checks
        each summary. Rows read back as in `learning_rows`.
        """
        return self._decision_rows(scope, _SHADOWED, since=since, limit=limit)

    def wrong_reasons(
        self, scope: str, candidates: Sequence[str], since: datetime
    ) -> dict[str, dict[str, int]]:
        """How often the user gave each Not useful reason (feedback `wrong` with a reason) to
        the scope's decisions of each of *candidates*, created at or after *since*, by candidate;
        a candidate with none is absent."""
        with self._transaction() as connection:
            rows = connection.execute(
                "SELECT candidate,feedback_reason,COUNT(*) FROM decision_episodes "
                "WHERE scope=? AND entry='decision' AND feedback='wrong' "
                "AND feedback_reason IS NOT NULL AND created_at>=? "
                "AND candidate IN (SELECT value FROM json_each(?)) "
                "GROUP BY candidate,feedback_reason",
                (scope, stamp(since), json.dumps(list(candidates))),
            ).fetchall()
        found: dict[str, dict[str, int]] = {}
        for candidate, reason, count in rows:
            found.setdefault(candidate, {})[reason] = count
        return found

    def draft_rows(
        self,
        scope: str,
        since: datetime,
        *,
        types: tuple[str, ...] = MESSAGE_DRAFTS,
        closed: str = "nothing",
        done: str | None = None,
    ) -> list[LookRow]:
        """The scope's items of *types* (the message drafts, `MESSAGE_DRAFTS`, the saved checks,
        `SAVED_CHECKS`, or the inbox suggestions, `INBOX_TRIAGE`) the launcher showed (`shown_at`
        set), created at or after *since*, newest first, at most `MAX_DRAFT_ROWS`, as
        `second_looks.yields` reads them: `started` is the item's creation, `state` its state,
        and `reason` its outcome. Such an item has no mission, so its episodes tell the outcome:

        - "rejected:other": any of its episodes took Not useful or Stop these (`wrong` or
          `mute`), even one whose button was selected too -- turning it down wins;
        - *done*, when given: otherwise the item's state is "done" -- the inbox suggestions pass
          "nothing" (VIKI found no destination), which beats used, as a "done" item was settled
          by what the run found, not by the user;
        - "accepted:other": otherwise any of its episodes had its button selected (`acted_at`) or
          took Useful -- *used*, though nothing says the user sent the message or ran the check;
        - *closed*: otherwise the item is no longer open -- "nothing" (*closed unused*) for a
          message draft; a saved check passes "" so that an item that closed unanswered, such as
          one the commit moved past, is not read as a miss;
        - "": it is still open and unanswered.

        Only episodes created at or after *since* are read, one grouped read for all the items.
        """
        with self._transaction() as connection:
            items = connection.execute(
                "SELECT item_key,candidate,state,created_at FROM decision_items "
                "WHERE scope=? AND shown_at IS NOT NULL AND created_at>=? "
                "AND candidate IN (SELECT value FROM json_each(?)) "
                "ORDER BY created_at DESC,item_key DESC LIMIT ?",
                (scope, stamp(since), json.dumps(types), MAX_DRAFT_ROWS),
            ).fetchall()
            answered = {
                key: (used, turned_down)
                for key, used, turned_down in connection.execute(
                    "SELECT item_key,max(acted_at IS NOT NULL OR feedback='useful'),"
                    "max(feedback IN ('wrong','mute')) FROM decision_episodes "
                    "WHERE scope=? AND entry='decision' AND created_at>=? "
                    "AND candidate IN (SELECT value FROM json_each(?)) AND item_key IS NOT NULL "
                    "GROUP BY item_key",
                    (scope, stamp(since), json.dumps(types)),
                )
            }
        rows: list[LookRow] = []
        for key, candidate, state, created_at in items:
            used, turned_down = answered.get(key, (0, 0))
            if turned_down:
                reason = "rejected:other"
            elif done is not None and state == "done":
                reason = done
            elif used:
                reason = "accepted:other"
            else:
                reason = "" if state == "open" else closed
            rows.append(LookRow(candidate, state, reason, parse_stamp(created_at)))
        return rows

    def drafts_today(
        self, scope: str, day: str, keys: Sequence[str], *, types: tuple[str, ...] = MESSAGE_DRAFTS
    ) -> tuple[int, frozenset[str]]:
        """How many items of *types* (the message drafts, `MESSAGE_DRAFTS`, the saved checks,
        `SAVED_CHECKS`, or the inbox suggestions, `INBOX_TRIAGE`), in any scope, state and
        level, were created within the local day *day* ("YYYY-MM-DD"): a held-back silent one
        still takes a slot, so a stored key that later shows never takes the day past the limit.
        And which of *keys* this scope already stores as an item, whenever created and at any
        level and of any type: the daily limit counts new items only."""
        start, end = _day(day)
        with self._transaction() as connection:
            today = connection.execute(
                "SELECT count(*) FROM decision_items WHERE created_at>=? AND created_at<? "
                "AND candidate IN (SELECT value FROM json_each(?))",
                (start, end, json.dumps(types)),
            ).fetchone()[0]
            stored = connection.execute(
                "SELECT item_key FROM decision_items WHERE scope=? "
                "AND item_key IN (SELECT value FROM json_each(?))",
                (scope, json.dumps(list(keys))),
            ).fetchall()
        return today, frozenset(key for (key,) in stored)

    def trial_rows(
        self, scope: str, *, since: datetime | None, until: datetime | None = None
    ) -> list[LearningRow]:
        """The scope's decision rows the trial comparison reads (`calibration.trial`): created
        at or after *since* (None for all), and before *until* when given, whose stored answers
        name the trial ask's member (`cycle.TRIAL`), silent and unlabelled ones included -- the
        trial is asked about each fresh answer to the question it rewords, whether the live
        version showed it or held it back -- newest first, at most `MAX_LEARN_ROWS`. Only that
        name is matched here; `calibration.trial` decodes and checks each summary. Rows read
        back as in `learning_rows`.
        """
        return self._decision_rows(scope, _TRIALED, since=since, until=until, limit=MAX_LEARN_ROWS)

    def _decision_rows(
        self,
        scope: str,
        condition: str,
        *,
        since: datetime | None,
        until: datetime | None = None,
        limit: int,
    ) -> list[LearningRow]:
        """The scope's decision rows that meet the SQL *condition*, created at or after *since*
        (None for all) and, when *until* is given, before it too, newest first, at most *limit*,
        read back as `learning_rows` says."""
        params: list[object] = [scope]
        since_clause = ""
        if since is not None:
            since_clause = "AND created_at>=?"
            params.append(stamp(since))
        until_clause = ""
        if until is not None:
            until_clause = "AND created_at<?"
            params.append(stamp(until))
        params.append(limit)
        with self._transaction() as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT episode_id,created_at,candidate,level,timing,policy,answers,"
                "terms,feedback,feedback_reason,feedback_at,first_exposed_at,opened_at,acted_at "
                "FROM decision_episodes WHERE scope=? AND entry='decision' "
                f"AND {condition} {since_clause} {until_clause} "
                "ORDER BY created_at DESC,episode_id DESC LIMIT ?",
                params,
            ).fetchall()
        found: list[LearningRow] = []
        for row in rows:
            try:
                created_at = parse_stamp(row["created_at"])
            except ValueError:
                continue
            opened_at = _optional_stamp(row["opened_at"])
            acted_at = _optional_stamp(row["acted_at"])
            found.append(
                LearningRow(
                    episode_id=row["episode_id"],
                    created_at=created_at,
                    candidate=row["candidate"],
                    level=row["level"],
                    timing=row["timing"],
                    policy=row["policy"],
                    answers_json=row["answers"],
                    p=_term_p(row["terms"]),
                    feedback=row["feedback"],
                    feedback_reason=row["feedback_reason"],
                    feedback_at=_optional_stamp(row["feedback_at"]),
                    first_exposed_at=_optional_stamp(row["first_exposed_at"]),
                    opened_at=opened_at,
                    acted_at=acted_at,
                    label=label_of(row["feedback"], opened_at, acted_at),
                )
            )
        return found

    def owner_rows(self, scope: str, since: datetime) -> OwnerRows:
        """The scope's rows for the user part (see `OwnerRows`): three parameterized reads in
        one transaction, each newest first and capped at its own `state.py` limit."""
        since_text = stamp(since)
        with self._transaction() as connection:
            connection.row_factory = sqlite3.Row
            seen = connection.execute(
                "SELECT item_key,level FROM decision_episodes WHERE scope=? AND entry='decision' "
                "AND item_key IS NOT NULL AND level!='silent' AND created_at>=? "
                "ORDER BY created_at DESC,episode_id DESC LIMIT ?",
                (scope, since_text, RECENT),
            ).fetchall()
            marked = connection.execute(
                "SELECT item_key FROM decision_episodes WHERE scope=? AND entry='decision' "
                "AND item_key IS NOT NULL AND feedback IN ('mute','wrong') "
                "ORDER BY created_at DESC,episode_id DESC LIMIT ?",
                (scope, MUTED_OR_WRONG),
            ).fetchall()
            feedback = connection.execute(
                "SELECT item_key,level,feedback FROM decision_episodes WHERE scope=? "
                "AND entry='decision' AND item_key IS NOT NULL AND feedback IS NOT NULL "
                "ORDER BY created_at DESC,episode_id DESC LIMIT ?",
                (scope, RECENT_FEEDBACK),
            ).fetchall()
        return OwnerRows(
            seen=tuple((row["item_key"], row["level"]) for row in seen),
            marked=tuple(row["item_key"] for row in marked),
            feedback=tuple((row["item_key"], row["level"], row["feedback"]) for row in feedback),
        )

    def audit(
        self, scope: str, now: datetime, *, agenda: ItemLookup | None = None
    ) -> list[dict[str, Any]]:
        """Up to `AUDIT_SIZE` silent, unmuted "now" decisions from the week before *now*'s week
        (`week_start`), the same set and order all week: ordered by the `sha256_hex` of the
        scope, that week and each episode_id, so the sample does not drift as feedback comes in.

        `agenda`, when given, fills each row's `why` as `page_parts` does.
        """
        start = week_start(now)
        lower = _week_midnight(start.date() - timedelta(days=7), now.tzinfo)
        with self._transaction() as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                f"SELECT {','.join(_HELD_BACK_FIELDS)},item_key FROM decision_episodes "
                "WHERE scope=? AND level='silent' AND timing='now' AND reason!=? "
                "AND created_at>=? AND created_at<?",
                (scope, _MUTED_REASON, stamp(lower), stamp(start)),
            ).fetchall()
        ordered = sorted(
            rows, key=lambda row: sha256_hex(f"{scope}|{start.date()}|{row['episode_id']}")
        )
        return [
            {
                **{name: row[name] for name in _HELD_BACK_FIELDS},
                "why": _why(agenda, scope, row["item_key"]),
            }
            for row in ordered[:AUDIT_SIZE]
        ]

    def review(self, scope: str, now: datetime) -> tuple[bool, int]:
        """Whether this week's audit is due for a look, and how many of its rows still carry no
        feedback: due once that count is positive and `review_seen` was never set or falls
        before this week's start."""
        count = sum(1 for row in self.audit(scope, now) if row["feedback"] is None)
        with self._transaction() as connection:
            found = connection.execute(
                "SELECT value FROM decision_preferences WHERE scope=? AND name='review_seen'",
                (scope,),
            ).fetchone()
        seen = _optional_stamp(found[0] if found is not None else None)
        due = count > 0 and (seen is None or seen < week_start(now))
        return due, count
