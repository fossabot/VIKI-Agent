"""The whitelisted context report built from one `get_context_usage` reply.

`context_report` and `failed_report` are the only entry points, and both are pure: no I/O, no
logging, and `usage` is only ever read, never mutated. The reply is an untrusted CLI shape;
an absent or off-shape part reads as `None` with its reason
in `reasons` instead of raising — one bad row spoils its whole part, the same rule
`drivers/claude/driver.py`'s `_rows` already applies to the driver's own inventory snapshot.
Nothing outside the named fields below is ever copied: no description, prompt, content, colour,
`gridRows`, `apiUsage`, `model`, or path but a memory file's own basename.

`driver.py` calls this module; it never imports that module.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from pathlib import PureWindowsPath
from typing import Any, cast

REPORT_PARTS = ("categories", "memory_files", "mcp_servers", "skills", "agents")
CATEGORY_KINDS = frozenset({"used", "deferred", "buffer", "free"})
MAX_COUNT = 2**53 - 1


def _count(value: object) -> int | None:
    """A whole number in 0..MAX_COUNT. A `bool` is an `int` subclass and is never one."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 0 <= value <= MAX_COUNT else None


def _percentage(value: object) -> int | float | None:
    """An int or a finite float in 0..100, else None. Never a `bool`."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value if 0 <= value <= 100 else None


def _rows(
    value: object, read_row: Callable[[object], dict[str, object] | None]
) -> list[dict[str, object]] | None:
    """Every element read by `read_row`, or None: one bad element spoils the whole list."""
    if not isinstance(value, list):
        return None
    rows: list[dict[str, object]] = []
    for item in cast(list[object], value):
        row = read_row(item)
        if row is None:
            return None
        rows.append(row)
    return rows


def _category_row(item: object) -> dict[str, object] | None:
    """`color` and `isDeferred` are never copied; `kind` must be one of CATEGORY_KINDS."""
    if not isinstance(item, Mapping):
        return None
    item = cast(Mapping[str, object], item)
    name, tokens, kind = item.get("name"), _count(item.get("tokens")), item.get("kind")
    if (
        not isinstance(name, str)
        or tokens is None
        or not (isinstance(kind, str) and kind in CATEGORY_KINDS)
    ):
        return None
    return {"name": name, "tokens": tokens, "kind": kind}


def _memory_file_row(item: object) -> dict[str, object] | None:
    """The path itself is never copied, only its basename (or the whole value when a Windows
    path has none, e.g. a bare drive root)."""
    if not isinstance(item, Mapping):
        return None
    item = cast(Mapping[str, object], item)
    path, kind, tokens = item.get("path"), item.get("type"), _count(item.get("tokens"))
    if not isinstance(path, str) or not isinstance(kind, str) or tokens is None:
        return None
    return {"name": PureWindowsPath(path).name or path, "type": kind, "tokens": tokens}


def _mcp_tool_row(item: object) -> dict[str, object] | None:
    """Only what the per-server aggregate below needs; the tool's own name is never copied."""
    if not isinstance(item, Mapping):
        return None
    item = cast(Mapping[str, object], item)
    server, loaded = item.get("serverName"), item.get("isLoaded")
    tokens = _count(item.get("tokens"))
    if not isinstance(server, str) or not isinstance(loaded, bool) or tokens is None:
        return None
    return {"server": server, "loaded": loaded, "tokens": tokens}


def _mcp_servers(value: object) -> list[dict[str, object]] | None:
    """`mcpTools` rows folded per `serverName`, in first-seen order. A deferred (unloaded) tool
    counts toward `tools` but never toward `loaded` or `loaded_tokens`."""
    rows = _rows(value, _mcp_tool_row)
    if rows is None:
        return None
    order: list[str] = []
    totals: dict[str, dict[str, int]] = {}
    for row in rows:
        server = cast(str, row["server"])
        bucket = totals.get(server)
        if bucket is None:
            order.append(server)
            bucket = totals[server] = {"tools": 0, "loaded": 0, "loaded_tokens": 0}
        bucket["tools"] += 1
        if cast(bool, row["loaded"]):
            bucket["loaded"] += 1
            bucket["loaded_tokens"] += cast(int, row["tokens"])
    return [{"server": server, **totals[server]} for server in order]


def _agent_row(item: object) -> dict[str, object] | None:
    if not isinstance(item, Mapping):
        return None
    item = cast(Mapping[str, object], item)
    kind, source = item.get("agentType"), item.get("source")
    tokens = _count(item.get("tokens"))
    if not isinstance(kind, str) or not isinstance(source, str) or tokens is None:
        return None
    return {"type": kind, "source": source, "tokens": tokens}


def _skill_item_row(item: object) -> dict[str, object] | None:
    """`plugin` is `pluginName`, or None when it is absent; present but off-type spoils the row."""
    if not isinstance(item, Mapping):
        return None
    item = cast(Mapping[str, object], item)
    name, source = item.get("name"), item.get("source")
    tokens = _count(item.get("tokens"))
    if not isinstance(name, str) or not isinstance(source, str) or tokens is None:
        return None
    plugin = item.get("pluginName")
    if plugin is not None and not isinstance(plugin, str):
        return None
    return {"name": name, "source": source, "plugin": plugin, "tokens": tokens}


def _skills(value: object) -> dict[str, object] | None:
    """`total` and `included` must be counts for the object to be recognised; `tokens` alone may
    stay None, and one bad `skillFrontmatter` row spoils the whole part."""
    if not isinstance(value, Mapping):
        return None
    value = cast(Mapping[str, object], value)
    total, included = _count(value.get("totalSkills")), _count(value.get("includedSkills"))
    items = _rows(value.get("skillFrontmatter"), _skill_item_row)
    if total is None or included is None or items is None:
        return None
    return {
        "total": total,
        "included": included,
        "tokens": _count(value.get("tokens")),
        "items": items,
    }


# part -> (its raw get_context_usage key, the reader that whitelists it). REPORT_PARTS is this
# dict's key order; each part reads None with a reason in `reasons` when its key is missing
# (absent) or its value is off shape (unrecognized).
_PARTS: dict[str, tuple[str, Callable[[object], object | None]]] = {
    "categories": ("categories", lambda v: _rows(v, _category_row)),
    "memory_files": ("memoryFiles", lambda v: _rows(v, _memory_file_row)),
    "mcp_servers": ("mcpTools", _mcp_servers),
    "skills": ("skills", _skills),
    "agents": ("agents", lambda v: _rows(v, _agent_row)),
}


def _auto_compact(usage: Mapping[str, Any]) -> dict[str, object] | None:
    """None unless `isAutoCompactEnabled` is a bool; `threshold` and `source` degrade alone."""
    enabled = usage.get("isAutoCompactEnabled")
    if not isinstance(enabled, bool):
        return None
    source = usage.get("autocompactSource")
    return {
        "enabled": enabled,
        "threshold": _count(usage.get("autoCompactThreshold")),
        "source": source if isinstance(source, str) else None,
    }


def context_report(usage: Mapping[str, Any], observed_at: str) -> dict[str, object]:
    """The context report for one `get_context_usage` reply. Pure: only reads `usage`, never mutates
    it. A part in REPORT_PARTS is None with its reason in `reasons`; the scalar fields above them
    degrade silently to None, with no entry in `reasons`."""
    report: dict[str, object] = {
        "observed_at": observed_at,
        "total_tokens": _count(usage.get("totalTokens")),
        "max_tokens": _count(usage.get("maxTokens")),
        "raw_max_tokens": _count(usage.get("rawMaxTokens")),
        "percentage": _percentage(usage.get("percentage")),
        "auto_compact": _auto_compact(usage),
    }
    reasons: dict[str, str] = {}
    for part, (key, read) in _PARTS.items():
        report[part] = read(usage[key]) if key in usage else None
        if report[part] is None:
            reasons[part] = "unrecognized" if key in usage else "absent"
    report["reasons"] = reasons
    return report


def failed_report(reason: str) -> dict[str, object]:
    """The report's keys, every value null, and `reason` against every part in REPORT_PARTS."""
    return {
        "observed_at": None,
        "total_tokens": None,
        "max_tokens": None,
        "raw_max_tokens": None,
        "percentage": None,
        "auto_compact": None,
        **dict.fromkeys(REPORT_PARTS),
        "reasons": dict.fromkeys(REPORT_PARTS, reason),
    }
