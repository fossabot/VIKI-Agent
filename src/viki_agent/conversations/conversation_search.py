"""Revisioned, bounded native conversation search and whole-turn anchor windows."""

from __future__ import annotations

import hashlib
import json
import re
import time
import unicodedata
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from viki_agent.catalog import (
    SEARCH_DEADLINE_S,
    Catalog,
    ConversationChain,
    _map_entry,
    _subagent_ids_before,
)
from viki_agent.drivers.claude.mapper import Mapper
from viki_agent.events import TextDone, UserMessage, history_item

MAX_HITS = 100
MAX_SEARCH_BYTES = 256 << 10
MAX_ANCHOR_EVENTS = 1000
MAX_ANCHOR_BYTES = 2 << 20
# Code points between budget checks inside one text's normalization walk.
SEGMENT_CHECK = 1024
# An all-ASCII text's normalization units: a whitespace run (the same \s that _fold collapses)
# or any one other character.
_ASCII_UNITS = re.compile(r"\s+|.", re.DOTALL)
_NON_SPACE = re.compile(r"\S")


@dataclass(frozen=True)
class Budget:
    """The time one search or anchor may spend on a chain it already holds, plus the request's
    cancellation. The wait for the chain is not in it: that keeps the parse permit's own bound.

    `deadline` is on `clock`'s scale. check() runs between bounded steps, so one step (one
    entry's mapping, one chunk's fold) can overshoot the deadline by its own cost."""

    deadline: float
    cancelled: Callable[[], bool]
    clock: Callable[[], float]

    def check(self) -> None:
        # Cancellation first: a superseded request is answered as superseded, not as slow.
        if self.cancelled():
            raise InterruptedError
        if self.clock() >= self.deadline:
            raise TimeoutError


def _fold(value: str) -> str:
    # Match the browser's locale-independent lowercase rules, including sharp-s and sigma.
    normalized = unicodedata.normalize("NFKC", value).lower().replace("ß", "ss").replace("ς", "σ")
    return re.sub(r"\s+", " ", normalized)


def _is_mark(ch: str) -> bool:
    return unicodedata.category(ch).startswith("M") or ch in "ﾞﾟ"


def _fold_joins(tail: str, ch: str) -> bool:
    """Whether folding `ch` after `tail` differs in length from folding the two apart."""
    return len(_fold(tail + ch)) != len(_fold(tail)) + len(_fold(ch))


def _chunks(text: str, budget: Budget) -> Iterator[tuple[int, int]]:
    """Spans of at least SEGMENT_CHECK code points (the last may be shorter), each cut where
    folding separates the two sides, with the budget checked before every span but the first.

    NFKC is super-linear on heavily expanding text and on long runs of marks, so folding a whole
    text would run unchecked for as long as the text makes it. A whitespace run stays whole
    (it folds linearly); a run of marks with no separable cut within SEGMENT_CHECK more code
    points is cut anyway, so one span's fold stays bounded and a match across that cut can be
    missed."""
    start = 0
    while start < len(text):
        if start:
            budget.check()
        # A whitespace run folds linearly, so the chunk takes the rest of it rather than cut it.
        following = _NON_SPACE.search(text, min(start + SEGMENT_CHECK, len(text)))
        cut = following.start() if following else len(text)
        limit = min(cut + SEGMENT_CHECK, len(text))
        while cut < limit and (_is_mark(text[cut]) or _fold_joins(text[cut - 32 : cut], text[cut])):
            cut += 1
        yield start, cut
        start = cut


def _segments(text: str, budget: Budget) -> Iterator[tuple[int, int]]:
    """Normalization units, with the budget checked every SEGMENT_CHECK code points.

    An all-ASCII text is its own NFKC form and has no marks, so _folded_segments' test reduces to
    "a whitespace run joins, every other character stands alone": one regex pass gives the same
    units without folding anything."""
    if text.isascii():
        return _ascii_segments(text, budget)
    return _folded_segments(text, budget)


def _ascii_segments(text: str, budget: Budget) -> Iterator[tuple[int, int]]:
    next_check = SEGMENT_CHECK
    for unit in _ASCII_UNITS.finditer(text):
        if unit.start() >= next_check:
            budget.check()
            next_check = unit.start() + SEGMENT_CHECK
        yield unit.span()


def _folded_segments(text: str, budget: Budget) -> Iterator[tuple[int, int]]:
    """Normalization units: kept together while NFKC folding is not separable at the join.

    A character whose Unicode general category starts with "M" (a combining mark), and either
    halfwidth kana sound mark (U+FF9E, U+FF9F), joins the growing segment: this keeps a
    base character and its marks (an accent, a variation selector) together the way the browser's
    grapheme-cluster join usually does, except that a segment already SEGMENT_CHECK code points
    long takes no more marks: NFKC reorders a run of marks in quadratic time, so an uncut run
    would make one segment's fold unbounded. Every other join is decided by the fold test: only the
    last 32 code points of the growing segment are tested against the next character, because
    every context-dependent fold this app forms (halfwidth kana plus a voiced sound mark,
    composable Hangul jamo, a run of whitespace collapsing to one space) resolves within a handful
    of code points, so a segment of 32 or fewer behaves exactly as an unbounded join would. Without
    the cap, a long unbroken run (paste of thousands of spaces, say) folds the whole growing
    segment on every step, which is quadratic in the run's length.
    """
    if not text:
        return
    start = 0
    for index in range(1, len(text)):
        if not index % SEGMENT_CHECK:
            budget.check()
        ch = text[index]
        if _is_mark(ch):
            if index - start < SEGMENT_CHECK:
                continue
        elif _fold_joins(text[max(start, index - 32) : index], ch):
            continue
        yield start, index
        start = index
    yield start, len(text)


def _locate(text: str, needle: str, budget: Budget) -> tuple[int, int, int, int] | None:
    """The needle's offset in the folded text, the folded and raw offsets where the chunk holding
    its start begins, and the raw end of the chunk holding its end; None if it is absent."""
    starts: list[tuple[int, int]] = []
    folded_total = 0
    carry = ""
    for left, right in _chunks(text, budget):
        starts.append((folded_total, left))
        window = carry + _fold(text[left:right])
        found = window.find(needle)
        if found >= 0:
            found += folded_total - len(carry)
            offset, begin = next(start for start in reversed(starts) if start[0] <= found)
            return found, offset, begin, right
        folded_total += len(window) - len(carry)
        # Enough folded text for a match that starts here and ends in the next chunk.
        carry = window[max(0, len(window) - len(needle) + 1) :]
    return None


def match_span(text: str, query: str, budget: Budget) -> tuple[int, int] | None:
    """The raw start and end offsets in `text` of the first match of the folded `query`, or None.
    The span covers whole normalization units, so it can run past the query's own characters."""
    needle = _fold(query)
    located = _locate(text, needle, budget)
    if located is None:
        return None
    found, offset, begin, end = located
    part = text[begin:end]
    start = None
    for left, right in _segments(part, budget):
        offset += len(_fold(part[left:right]))
        if start is None and offset > found:
            start = begin + left
        if start is not None and offset >= found + len(needle):
            return start, right + begin
    return None


def match_snapshot(text: str, query: str, budget: Budget) -> dict[str, str] | None:
    span = match_span(text, query, budget)
    if span is None:
        return None
    start, right = span
    return {
        "before": text[max(0, start - 120) : start],
        "match": text[start : min(right, start + 256)],
        "after": text[right : right + 120],
    }


def _may_yield_text(entry: dict[str, Any]) -> bool:
    """Whether the mapper could make a UserMessage or TextDone of `entry`, decided from its
    shape alone so that search maps no tool result, progress row or tool call. It must never say
    no to an entry that yields one: user text (a string, or text or image blocks, which map to a
    typed prompt or "[image]"), assistant text blocks, and a queued_command attachment (a folded
    steer)."""
    kind = entry.get("type")
    if kind == "attachment":
        attachment = entry.get("attachment")
        return isinstance(attachment, dict) and attachment.get("type") == "queued_command"
    message = entry.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if kind == "user" and isinstance(content, str):
        return True
    wanted = {"user": ("text", "image"), "assistant": ("text",)}.get(str(kind), ())
    return isinstance(content, list) and any(
        isinstance(block, dict) and block.get("type") in wanted for block in content
    )


@dataclass(frozen=True)
class ConversationSearch:
    catalog: Catalog
    resolve: Callable[[str], tuple[str, str | None]]

    def _chain(self, session_id: str, cancelled: Callable[[], bool]) -> ConversationChain:
        source, until = self.resolve(session_id)
        return self.catalog.conversation_chain(source, until, cancelled)

    @staticmethod
    def _revision(entry: dict[str, Any], role: str, text: str) -> str:
        digest = hashlib.sha256(b"viki-conversation-search-v2\0")
        for value in (
            entry["_viki_source_revision"].encode("ascii"),
            role.encode("ascii"),
            text.encode("utf-8", "surrogatepass"),
        ):
            digest.update(len(value).to_bytes(8, "big"))
            digest.update(value)
        return digest.hexdigest()

    def _texts(self, entry: dict[str, Any]) -> list[UserMessage | TextDone]:
        mapper = Mapper(vault=str(self.catalog.vault), mode="default", text_limit=None)
        return [
            event
            for event in _map_entry(mapper, entry)
            if isinstance(event, (UserMessage, TextDone))
        ]

    def search(
        self,
        session_id: str,
        query: str,
        cancelled: Callable[[], bool] = lambda: False,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> dict[str, Any]:
        stopped = {"hits": [], "state": "superseded", "reason": "cancelled", "coverage": "complete"}
        if cancelled():
            return stopped
        chain = self._chain(session_id, cancelled)
        budget = Budget(clock() + SEARCH_DEADLINE_S, cancelled, clock)
        result: dict[str, Any] = {
            "hits": [],
            "state": "partial" if chain.status == "partial" else "ready",
            "reason": "tail_budget" if chain.status == "partial" else "",
            "coverage": "tail" if chain.status == "partial" else "complete",
        }
        if chain.status not in {"complete", "partial"}:
            result.update(
                state="error",
                reason={
                    "missing": "transcript_missing",
                    "busy": "busy",
                    "unreadable": "read_failed",
                }[chain.status],
            )
            return result
        query = unicodedata.normalize("NFKC", query).strip()
        if not query:
            return result
        try:
            for entry in chain.entries:
                budget.check()
                if not _may_yield_text(entry):
                    continue
                for index, event in enumerate(self._texts(entry)[:64]):
                    snapshot = match_snapshot(event.text, query, budget)
                    if snapshot is None:
                        continue
                    if len(result["hits"]) >= MAX_HITS:
                        result.update(state="partial", reason="response_budget")
                        return result
                    role = "user" if isinstance(event, UserMessage) else "assistant"
                    result["hits"].append(
                        {
                            "role": role,
                            "at": str(entry.get("timestamp", ""))[:64],
                            "snapshot": snapshot,
                            "anchor": {
                                "kind": "native_entry",
                                "entry_uuid": entry["uuid"],
                                "mapped_index": index,
                                "source_revision": self._revision(entry, role, event.text),
                            },
                        }
                    )
                    # Reserve the route's UUID and any longer partial reason.
                    if len(json.dumps(result, ensure_ascii=True)) + 128 > MAX_SEARCH_BYTES:
                        result["hits"].pop()
                        result.update(state="partial", reason="response_budget")
                        return result
        except InterruptedError:
            return {**stopped, "coverage": result["coverage"]}
        except TimeoutError:
            result.update(state="partial", reason="time_budget")
        return result

    def anchor(
        self,
        session_id: str,
        anchor: dict[str, Any],
        cancelled: Callable[[], bool] = lambda: False,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> dict[str, Any]:
        chain = self._chain(session_id, cancelled)
        budget = Budget(clock() + SEARCH_DEADLINE_S, cancelled, clock)
        entries = chain.entries
        empty: dict[str, Any] = {
            "events": [],
            "anchor_turn_index": None,
            "older_before_uuid": None,
            "newer_after_uuid": None,
            "coverage": "tail" if chain.status == "partial" else "complete",
        }
        if chain.status == "missing":
            return {**empty, "state": "missing", "reason": "transcript_missing"}
        if chain.status not in {"complete", "partial"}:
            return {
                **empty,
                "state": "error",
                "reason": "busy" if chain.status == "busy" else "read_failed",
            }
        pos = next(
            (i for i, entry in enumerate(entries) if entry["uuid"] == anchor["entry_uuid"]),
            None,
        )
        if pos is None:
            return {
                **empty,
                "state": "partial" if chain.status == "partial" else "missing",
                "reason": "outside_window" if chain.status == "partial" else "entry_missing",
            }
        entry = entries[pos]
        mapped = self._texts(entry)
        index = anchor["mapped_index"]
        if (
            index >= len(mapped)
            or self._revision(
                entry,
                "user" if isinstance(mapped[index], UserMessage) else "assistant",
                mapped[index].text,
            )
            != anchor["source_revision"]
        ):
            return {**empty, "state": "changed", "reason": "source_changed"}
        try:
            classifier = Mapper(vault=str(self.catalog.vault), mode="default")
            root = self.catalog.root_for(session_id)
            starts: list[int] = []
            for point, candidate in enumerate(entries):
                budget.check()
                if any(
                    isinstance(event, UserMessage) for event in _map_entry(classifier, candidate)
                ):
                    starts.append(point)
            bounds = sorted({0, *starts, len(entries)})
            target = max(i for i, point in enumerate(bounds[:-1]) if point <= pos)
            left, right = max(0, target - 9), min(len(bounds) - 1, target + 11)
            while True:
                start, end = bounds[left], bounds[right]
                only_target = left == target and right == target + 1
                events = (
                    self._window(entries, start, end, budget, root)
                    if end - start <= MAX_ANCHOR_EVENTS or only_target
                    else None
                )
                if events is not None:
                    return {
                        **empty,
                        "state": "ready",
                        "reason": "",
                        "events": events,
                        "anchor_turn_index": target - left,
                        "older_before_uuid": entries[start]["uuid"] if start else None,
                        "newer_after_uuid": entries[end - 1]["uuid"]
                        if end < len(entries)
                        else None,
                    }
                if only_target:
                    return {**empty, "state": "error", "reason": "too_large"}
                if target - left >= right - target - 1 and left < target:
                    left += 1
                else:
                    right -= 1
        except TimeoutError:
            return {**empty, "state": "error", "reason": "time_budget"}

    def _window(
        self, entries: list[dict[str, Any]], start: int, end: int, budget: Budget, root: Path
    ) -> list[dict[str, Any]] | None:
        """The history items of entries[start:end], mapped against the session's `root`, or None
        as soon as their running encoded size would pass the anchor ceiling less the response's
        other fields: an oversized window stops mapping at the entry that overflows it instead of
        mapping the rest first."""
        mapper = self.catalog.history_mapper(root)
        mapper.note_subagents(_subagent_ids_before(entries[:start]))
        items: list[dict[str, Any]] = []
        # Each item's default-separator encoding plus one byte is never less than its share of
        # the compact array the route sends, so the running total over-counts, never under.
        size = 0
        for entry in entries[start:end]:
            budget.check()
            for event in _map_entry(mapper, entry, source_revision=entry["_viki_source_revision"]):
                item = history_item(event)
                size += len(json.dumps(item, ensure_ascii=True)) + 1
                if size > MAX_ANCHOR_BYTES - 512:
                    return None
                items.append(item)
        return items
