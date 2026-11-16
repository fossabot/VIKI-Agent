"""Read-only, revision-checked pages of native textual tool results."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from viki_agent.catalog import Catalog
from viki_agent.conversations.conversation_search import SEARCH_DEADLINE_S, Budget
from viki_agent.drivers.claude.mapper import _result_text

PAGE_BYTES = 64 << 10
DETAIL_BYTES = 8 << 20


@dataclass(frozen=True)
class ArtifactDetails:
    catalog: Catalog
    resolve: Callable[[str], tuple[str, str | None]]

    def page(
        self,
        session_id: str,
        ref: dict[str, str],
        cursor: int,
        cancelled: Callable[[], bool] | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> dict[str, Any]:
        empty = {"text": "", "next_cursor": None, "byte_size": 0, "truncated": False}
        is_cancelled = cancelled or (lambda: False)
        source, until = self.resolve(session_id)
        chain = self.catalog.conversation_chain(source, until, is_cancelled)
        if chain.status not in {"complete", "partial"}:
            return {
                **empty,
                "state": "missing" if chain.status == "missing" else "error",
                "reason": {
                    "missing": "transcript_missing",
                    "busy": "busy",
                    "unreadable": "read_failed",
                }[chain.status],
            }
        budget = Budget(clock() + SEARCH_DEADLINE_S, is_cancelled, clock)
        try:
            for entry in chain.entries:
                budget.check()
                if entry["uuid"] != ref["entry_uuid"]:
                    continue
                if entry["_viki_source_revision"] != ref["source_revision"]:
                    return {**empty, "state": "changed", "reason": "source_changed"}
                message = entry.get("message")
                blocks = message.get("content") if isinstance(message, dict) else None
                if entry.get("type") != "user" or not isinstance(blocks, list):
                    return {**empty, "state": "missing", "reason": "entry_missing"}
                matches = [
                    block
                    for block in blocks
                    if isinstance(block, dict)
                    and block.get("type") == "tool_result"
                    and block.get("tool_use_id") == ref["tool_use_id"]
                ]
                if len(matches) != 1:
                    return {**empty, "state": "missing", "reason": "entry_missing"}
                content = matches[0].get("content")
                if not (
                    isinstance(content, str)
                    or isinstance(content, list)
                    and all(
                        isinstance(part, dict)
                        and part.get("type") == "text"
                        and isinstance(part.get("text"), str)
                        for part in content
                    )
                ):
                    return {**empty, "state": "error", "reason": "read_failed"}
                payload = _result_text(content).encode("utf-8", errors="replace")
                limit = min(len(payload), DETAIL_BYTES)
                if (
                    cursor > limit
                    or cursor
                    and cursor < len(payload)
                    and payload[cursor] & 0xC0 == 0x80
                ):
                    raise ValueError("invalid_cursor")
                end = min(cursor + PAGE_BYTES, limit)
                while end < len(payload) and end > cursor and payload[end] & 0xC0 == 0x80:
                    end -= 1
                final = end == len(payload) or (
                    len(payload) > DETAIL_BYTES and end >= DETAIL_BYTES - 3
                )
                return {
                    "state": "ready",
                    "reason": "",
                    "text": payload[cursor:end].decode("utf-8"),
                    "next_cursor": None if final else end,
                    "byte_size": len(payload),
                    "truncated": final and end < len(payload),
                }
        except TimeoutError:
            return {**empty, "state": "error", "reason": "time_budget"}
        return {
            **empty,
            "state": "partial" if chain.status == "partial" else "missing",
            "reason": "outside_window" if chain.status == "partial" else "entry_missing",
        }
