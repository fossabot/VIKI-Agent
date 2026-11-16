"""Read-only, revision-free lookup of WebFetch/Grep/Glob tool_use rows from a transcript.

Unlike `ArtifactDetails`, a lookup never claims a specific entry: a tool_use_id either turns up
somewhere in the session's main chain or it does not, and the caller already knows which ids it
is asking about (its own chat history). So there is no `source_revision` to check here, and no
`changed` state."""

from __future__ import annotations

import sys
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any
from urllib.parse import urlsplit

from viki_agent.catalog import Catalog
from viki_agent.conversations.artifacts import _posix_key, _workspace_path
from viki_agent.conversations.conversation_search import SEARCH_DEADLINE_S, Budget
from viki_agent.drivers.claude.mapper import _cut

# The tools a chip can show; everything else is a known id that is simply never answered.
_TOOLS = frozenset({"WebFetch", "Grep", "Glob"})
MAX_URL_CHARS = 2048
MAX_PATTERN_CHARS = 160


def _web_url(raw: object) -> str | None:
    """`raw` as an openable WebFetch URL, or None when it is missing, unbounded, carries
    whitespace or a control character, holds a backslash, or does not parse to an http(s) URL
    with a host. A backslash is refused outright: the WHATWG parser a browser uses treats it as
    a path separator in a special scheme, so `urlsplit` and the browser can read different hosts
    from the same string (e.g. `https://evil.example\\@docs.python.org/3/`)."""
    if (
        not isinstance(raw, str)
        or not 1 <= len(raw) <= MAX_URL_CHARS
        or "\\" in raw
        or any(ch.isspace() or unicodedata.category(ch) == "Cc" for ch in raw)
    ):
        return None
    try:
        parts = urlsplit(raw)
    except ValueError:
        return None
    return raw if parts.scheme in ("http", "https") and parts.hostname else None


def _is_root(raw_path: str, root: str | Path) -> bool:
    """Whether `raw_path` is lexically the session root itself: `.`, or the root's own absolute
    spelling in either slash style, with or without a trailing separator — never by resolving
    the filesystem. Anything else, a path outside the root included, is for `_workspace_path`."""
    if raw_path == ".":
        return True
    if sys.platform == "win32":
        candidate = PureWindowsPath(raw_path.replace("/", "\\"))
        anchor = PureWindowsPath(str(root).replace("/", "\\"))
        return candidate.is_absolute() and tuple(
            part.casefold() for part in candidate.parts
        ) == tuple(part.casefold() for part in anchor.parts)
    candidate = PurePosixPath(raw_path)
    anchor = PurePosixPath(str(root))
    return candidate.is_absolute() and tuple(_posix_key(p) for p in candidate.parts) == tuple(
        _posix_key(p) for p in anchor.parts
    )


def _item(tool_use_id: str, name: str, tool_input: object, root: Path) -> dict[str, Any] | None:
    """The chip row for one matched tool_use block, or None for a WebFetch whose URL is unusable
    (Grep/Glob always answer once matched: a bad or missing pattern/path just becomes "")."""
    data = tool_input if isinstance(tool_input, dict) else {}
    if name == "WebFetch":
        url = _web_url(data.get("url"))
        return None if url is None else {"tool_use_id": tool_use_id, "tool": name, "url": url}
    raw_path = data.get("path")
    if (
        raw_path is None
        or raw_path == ""
        or (isinstance(raw_path, str) and _is_root(raw_path, root))
    ):
        path, path_state = "", "root"
    else:
        path, path_state = _workspace_path(raw_path, root)
    return {
        "tool_use_id": tool_use_id,
        "tool": name,
        "pattern": _cut(str(data.get("pattern", "")), MAX_PATTERN_CHARS),
        "path": path,
        "path_state": path_state,
    }


@dataclass(frozen=True)
class ToolSources:
    catalog: Catalog
    resolve: Callable[[str], tuple[str, str | None]]

    def lookup(
        self,
        session_id: str,
        ids: list[str],
        cancelled: Callable[[], bool] | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> dict[str, Any]:
        empty: dict[str, Any] = {"items": []}
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
        wanted = set(ids)
        found: set[str] = set()
        items: list[dict[str, Any]] = []
        root = self.catalog.root_for(session_id)
        budget = Budget(clock() + SEARCH_DEADLINE_S, is_cancelled, clock)
        try:
            for entry in chain.entries:
                budget.check()
                if entry.get("type") != "assistant":
                    continue
                message = entry.get("message")
                blocks = message.get("content") if isinstance(message, dict) else None
                if not isinstance(blocks, list):
                    continue
                for block in blocks:
                    if (
                        not isinstance(block, dict)
                        or block.get("type") != "tool_use"
                        or block.get("name") not in _TOOLS
                    ):
                        continue
                    tool_use_id = str(block.get("id"))
                    if tool_use_id not in wanted or tool_use_id in found:
                        continue
                    found.add(tool_use_id)
                    item = _item(tool_use_id, str(block.get("name")), block.get("input"), root)
                    if item is not None:
                        items.append(item)
        except TimeoutError:
            return {**empty, "state": "error", "reason": "time_budget"}
        ready = chain.status == "complete" or wanted <= found
        return {
            "items": items,
            "state": "ready" if ready else "partial",
            "reason": "" if ready else "outside_window",
        }
