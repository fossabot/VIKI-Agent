"""Bounded replay and a separate, reconstructable session projection.

The projection keeps current session state and pending requests outside its recent-turn
budget. Evicting old conversation content therefore cannot revive a resolved request or
erase the latest terminal state. It is volatile display state, never a transcript store.
"""

from __future__ import annotations

import copy
import json
from collections import deque
from dataclasses import dataclass, field
from typing import Any

MAX_REPLAY_EVENTS = 2_000
MAX_REPLAY_BYTES = 8 << 20
MAX_CONTENT_BYTES = 1 << 20
MAX_CONTENT_RECORDS = 512
MAX_TURNS = 16
MAX_CARD_BYTES = 1 << 20
MAX_CARDS = 32
MAX_STATE_BYTES = 256 << 10
MAX_SNAPSHOT_BYTES = 3 << 20
MAX_TEXT_CHARS = 16_384
MAX_STEERS = 8
STEER_TEXT_CHARS = 2048


def encoded_size(value: Any) -> int:
    """Count the exact ASCII JSON representation used by the WebSocket writer."""
    return len(json.dumps(value, separators=(",", ":")))


def _bounded(value: Any, limit: int = MAX_TEXT_CHARS) -> Any:
    if isinstance(value, str):
        return value[:limit]
    if isinstance(value, list):
        return [_bounded(item, limit) for item in value[:128]]
    if isinstance(value, dict):
        return {key: _bounded(item, limit) for key, item in value.items()}
    return value


@dataclass
class _Record:
    item: dict[str, Any]
    size: int
    characters: int = 0


@dataclass
class _Turn:
    records: list[_Record] = field(default_factory=list)
    started: bool = False
    finished: bool = False


class StreamState:
    def __init__(self) -> None:
        self.replay: deque[tuple[dict[str, Any], int]] = deque()
        self.replay_bytes = 0
        self.replay_floor = 0
        self.turns: list[_Turn] = []
        self.content_bytes = 0
        self.content_records = 0
        self.cards: dict[str, _Record] = {}
        self.card_bytes = 0
        self.steers: dict[str, _Record] = {}
        self.truncated = False
        self.state: dict[str, Any] = {
            "info": None,
            "mode": "",
            "blocked": None,
            "closed": False,
            "error": None,
            "compacting": False,
            "context_percent": None,
            "rate_limit": None,
        }

    def can_keep_card(self, envelope: dict[str, Any]) -> bool:
        item = self._item(envelope)
        old = self.cards.get(item["data"]["tool_use_id"])
        size = encoded_size(item)
        return (
            len(self.cards) + (old is None) <= MAX_CARDS
            and self.card_bytes - (old.size if old else 0) + size <= MAX_CARD_BYTES
        )

    @staticmethod
    def _item(envelope: dict[str, Any]) -> dict[str, Any]:
        item = {"type": envelope["type"], "data": copy.deepcopy(envelope["data"])}
        if envelope["type"] == "subagent.done" and type(envelope.get("seq")) is int:
            item["source_seq"] = envelope["seq"]
        if "native_request" in envelope:
            item["native_request"] = dict(envelope["native_request"])
        return item

    def append(self, envelope: dict[str, Any]) -> None:
        self._project(envelope)
        kind, data = envelope["type"], envelope["data"]
        if kind in {"text.done", "thinking.done"}:
            retained = deque()
            for previous, size in self.replay:
                if previous["type"] in {"text.delta", "thinking.delta"} and (
                    previous["data"]["block_id"] == data["block_id"]
                ):
                    self.replay_bytes -= size
                else:
                    retained.append((previous, size))
            self.replay = retained
        size = encoded_size(envelope)
        self.replay.append((envelope, size))
        self.replay_bytes += size
        while len(self.replay) > MAX_REPLAY_EVENTS or self.replay_bytes > MAX_REPLAY_BYTES:
            removed, removed_size = self.replay.popleft()
            self.replay_bytes -= removed_size
            self.replay_floor = max(self.replay_floor, removed["seq"])

    def events_after(self, seq: int) -> list[dict[str, Any]]:
        return [event for event, _size in self.replay if event["seq"] > seq]

    def snapshot(self) -> dict[str, Any]:
        items = [record.item for turn in self.turns for record in turn.records]
        items.extend(record.item for record in self.steers.values())
        items.extend(record.item for record in self.cards.values())
        result = {
            "events": copy.deepcopy(items),
            "state": copy.deepcopy(self.state),
            "truncated": self.truncated,
        }
        # The separate budgets leave over 700 KiB for array/object framing. Keep this
        # assertion at the construction boundary so a future field cannot weaken the cap.
        assert encoded_size(result) < MAX_SNAPSHOT_BYTES
        return result

    def pending_request(self, tool_use_id: str) -> dict[str, Any] | None:
        """The projected card item, deep-copied so a caller that keeps it cannot mutate the
        projection a later snapshot will send."""
        record = self.cards.get(tool_use_id)
        return copy.deepcopy(record.item) if record is not None else None

    def drop_card(self, key: str) -> None:
        """Forget the projected card `key`, if any. A turn end leaves the cards in place; the
        session actor drops the ones whose requests it stops binding."""
        self._remove_card(key)

    def _remove_card(self, key: str) -> None:
        old = self.cards.pop(key, None)
        if old is not None:
            self.card_bytes -= old.size

    def clear_cards(self) -> None:
        self.cards.clear()
        self.card_bytes = 0

    def _new_turn(self) -> _Turn:
        turn = _Turn()
        self.turns.append(turn)
        self.state["error"] = self.state["rate_limit"] = None
        return turn

    def _project(self, envelope: dict[str, Any]) -> None:
        kind, data = envelope["type"], envelope["data"]
        item = self._item(envelope)
        if kind == "session.started":
            self.state.update(info=data, mode=data["mode"], blocked=None, closed=False, error=None)
        elif kind == "session.blocked":
            self.state["blocked"] = data
        elif kind == "session.closed":
            self.state["closed"] = True
            self.clear_cards()
            self.steers.clear()
        elif kind == "status":
            if data["mode"]:
                self.state["mode"] = data["mode"]
            self.state["compacting"] = data["state"] == "compacting"
        elif kind == "rate_limit":
            self.state["rate_limit"] = data
        elif kind in {"question.asked", "permission.requested"}:
            self._remove_card(data["tool_use_id"])
            record = _Record(item, encoded_size(item))
            self.cards[data["tool_use_id"]] = record
            self.card_bytes += record.size
        elif kind in {"request.answered", "request.cancelled"}:
            current = self.cards.get(data["tool_use_id"])
            if current is not None and current.item.get("native_request") == item.get(
                "native_request"
            ):
                self._remove_card(data["tool_use_id"])
        elif kind == "steer.pending":
            if len(self.steers) < MAX_STEERS:
                bounded = _bounded(item, STEER_TEXT_CHARS)
                self.steers[data["steer_id"]] = _Record(bounded, encoded_size(bounded))
        elif kind == "steer.ended":
            self.steers.pop(data["steer_id"], None)
            if data["outcome"] == "dropped":
                self._conversation(item)
        elif kind == "vault.sessions":
            return
        else:
            self._conversation(item)
        if encoded_size(self.state) > MAX_STATE_BYTES:
            self.state = _bounded(self.state, 512)
            info = self.state["info"]
            if encoded_size(self.state) > MAX_STATE_BYTES and info is not None:
                info["commands"] = info["agents"] = info["models"] = []
            self.truncated = True

    def _conversation(self, item: dict[str, Any]) -> None:
        kind, data = item["type"], item["data"]
        if kind == "user.message":
            self._new_turn()
        elif kind == "turn.started":
            if not self.turns or self.turns[-1].started or self.turns[-1].finished:
                self._new_turn()
            self.turns[-1].started = True
        elif kind == "tool.done":
            self._remove_card(data["block_id"])
        elif kind == "turn.done":
            if data["context_percent"] is not None:
                self.state["context_percent"] = data["context_percent"]
            if self.turns:
                self.turns[-1].finished = True
        elif kind == "compacted":
            self.state["compacting"] = False
        elif kind == "error" and (not self.turns or self.turns[-1].finished):
            self.state["error"] = data
            return
        if not self.turns:
            return
        original_size = encoded_size(item)
        bounded = _bounded(item)
        if bounded != item:
            if kind in {"text.delta", "text.done", "thinking.delta", "thinking.done"}:
                bounded["original_chars"] = len(data["text"])
            else:
                bounded["original_bytes"] = original_size
            self.truncated = True
        item = bounded
        if kind == "turn.done":
            for record in self.turns[-1].records:
                if record.item["type"] == kind:
                    self.content_bytes -= record.size
                    record.item, record.size = item, encoded_size(item)
                    self.content_bytes += record.size
                    self._trim()
                    return
        if kind in {"text.delta", "text.done", "thinking.delta", "thinking.done"}:
            family = kind.split(".", 1)[0]
            for turn in reversed(self.turns):
                for record in reversed(turn.records):
                    old = record.item
                    if old["type"].split(".", 1)[0] != family or (
                        old["data"].get("block_id") != data["block_id"]
                    ):
                        continue
                    if kind.endswith(".delta") and old["type"].endswith(".done"):
                        return
                    if kind.endswith(".delta"):
                        characters = record.characters + len(data["text"])
                        remaining = MAX_TEXT_CHARS - len(old["data"]["text"])
                        item["data"]["text"] = old["data"]["text"] + data["text"][:remaining]
                        if characters > MAX_TEXT_CHARS:
                            item["original_chars"] = characters
                            self.truncated = True
                    else:
                        characters = len(data["text"])
                    self.content_bytes -= record.size
                    record.item, record.size = item, encoded_size(item)
                    record.characters = characters
                    self.content_bytes += record.size
                    self._trim()
                    return
        record = _Record(item, encoded_size(item), len(data.get("text", "")))
        self.turns[-1].records.append(record)
        self.content_bytes += record.size
        self.content_records += 1
        self._trim()

    def _trim(self) -> None:
        while len(self.turns) > MAX_TURNS or (
            len(self.turns) > 1
            and (
                self.content_bytes > MAX_CONTENT_BYTES or self.content_records > MAX_CONTENT_RECORDS
            )
        ):
            old = self.turns.pop(0)
            self.content_bytes -= sum(record.size for record in old.records)
            self.content_records -= len(old.records)
            self.truncated = True
        while self.content_bytes > MAX_CONTENT_BYTES or self.content_records > MAX_CONTENT_RECORDS:
            # Preserve the current turn's identity, user prompt and real completion. Its
            # oldest display block can be omitted, with the snapshot's explicit notice.
            records = self.turns[-1].records
            index = next(
                i
                for i, record in enumerate(records)
                if record.item["type"] not in {"user.message", "turn.started", "turn.done"}
            )
            old = records.pop(index)
            self.content_bytes -= old.size
            self.content_records -= 1
            self.truncated = True
