"""Received messages between Claude Code sessions, parsed from the listed chats' own transcripts.

A session that sends another one a message leaves no useful record of it in the sender's own
transcript; the record lives in the RECEIVING session's transcript instead, wrapped in a
`<cross-session-message>` element that one of three shapes carries it as: a `queue-operation`
"enqueue" entry's `content`, an `attachment` "queued_command" entry's `prompt`, or an `isMeta`
user turn's injected text. All three are untrusted — another process wrote them — so parsing
fails closed: an entry that is not one of these three shapes, or whose tag does not parse exactly
as described below, contributes nothing rather than a best guess.

`received_messages` extracts every such element from one transcript's raw bytes.
`between_chats` reads the vault's listed chats (bounded) and turns what it finds into the wire
shape: `at`, `from_name`, `from_session_id` (resolved only against the running-sessions registry
and the listing itself, never against a path), `to_session_id`, `text`.
"""

from __future__ import annotations

import heapq
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypedDict

from viki_agent.catalog import _UUID_RE, Catalog, _iter_lines, _stat_for_read, _tail_window
from viki_agent.session import _registry

# Bounds: the newest listed, non-other sessions scanned, the newest whole lines read from each
# (one open-read-close per file), and the newest messages returned across all of them.
SCAN_SESSIONS = 30
SCAN_BYTES = 8 * 1024 * 1024
MAX_MESSAGES = 20
TEXT_CHARS = 200
NAME_CHARS = 64
# The open tag's match window: far more than any real tag (from, from-name, timestamp and
# optionally hop-chain, from-mode) ever needs, so an unterminated attribute run only ever costs a
# bounded scan rather than the whole (up to SCAN_BYTES) carrier.
OPEN_TAG_MAX_CHARS = 4096

_MARKER = b"<cross-session-message"
_ENQUEUE_PREFIX = "Another Claude session sent a message:"
_CLOSE_TAG = "</cross-session-message>"
# The element's opening tag: space-separated `name="value"` attributes (names lowercase and
# hyphens, values anything but a quote), only trailing whitespace before `>` is free. Anything
# else at the start of the carrier — a different tag, stray text, no tag at all — is not a match.
# The repeat is possessive (no backtracking back into already-matched attributes) and ASCII-only;
# the `.match()` call below also bounds the scan to OPEN_TAG_MAX_CHARS.
_OPEN_TAG_RE = re.compile(r'^<cross-session-message((?: [a-z][a-z-]*="[^"]*")*+)\s*>', re.ASCII)
_ATTR_RE = re.compile(r' ([a-z][a-z-]*)="([^"]*)"', re.ASCII)
_TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z", re.ASCII)


@dataclass(frozen=True, kw_only=True)
class Received:
    at: str  # the first carrier's timestamp attribute, verbatim
    from_address: str  # the raw `from` attribute: a "uds:" address, or any other string
    from_name: str  # the `from-name` attribute, clipped
    text: str  # the first non-blank body line, clipped


class _Message(TypedDict):
    at: str
    from_name: str
    from_session_id: str | None
    to_session_id: str
    text: str


def _utf16_len(s: str) -> int:
    """The length the client's JS `.length` sees: UTF-16 code units, not code points."""
    return len(s.encode("utf-16-le")) // 2


def _clip16(text: str, limit: int) -> str:
    """Like catalog's `_clip`, but in UTF-16 units: the wire is JSON with `ensure_ascii=True`
    and the client counts `.length`, not code points. Whitespace runs collapsed to one space,
    exactly as `_clip` does; within `limit` returned as-is, else cut at a code-point boundary —
    Python strings are sequences of code points, so slicing here never splits a surrogate pair —
    so the kept part plus "…" stays within `limit`."""
    collapsed = " ".join(text.split())
    if _utf16_len(collapsed) <= limit:
        return collapsed
    budget = limit - 1  # "…" costs one UTF-16 unit
    used = 0
    kept = 0
    for ch in collapsed:
        cost = 2 if ord(ch) > 0xFFFF else 1
        if used + cost > budget:
            break
        used += cost
        kept += 1
    return collapsed[:kept] + "…"


def _carrier_of(entry: dict[str, Any]) -> str | None:
    """The string that might start with a `<cross-session-message>` element, from whichever of
    the three shapes a received message is carried as, else None for anything else — including
    the tag merely quoted in a tool result, assistant text or plain user turn, none of which
    match one of these three shapes."""
    etype = entry.get("type")
    if etype == "queue-operation" and entry.get("operation") == "enqueue":
        content = entry.get("content")
        return content if isinstance(content, str) else None
    if etype == "attachment":
        attachment = entry.get("attachment")
        if isinstance(attachment, dict) and attachment.get("type") == "queued_command":
            prompt = attachment.get("prompt")
            return prompt if isinstance(prompt, str) else None
        return None
    if etype == "user" and entry.get("isMeta") is True:
        message = entry.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, str) and content.startswith(_ENQUEUE_PREFIX):
            return content[len(_ENQUEUE_PREFIX) :].lstrip()
    return None


def received_messages(raw: bytes) -> list[Received]:
    """Every `<cross-session-message>` element `raw` (one transcript's bytes, or a tail window of
    one) carries through a recognised carrier, deduped by element text within these bytes — the
    first carrier to record one wins and gives its `at`.

    Fails closed line by line: a line that does not hold the marker, is not one of the three
    carrier shapes, or whose tag does not parse exactly as the module docstring describes
    (a duplicate attribute, an empty or missing `from`/`from-name`, no closing tag, or a
    timestamp that does not fullmatch the ISO 8601 shape), contributes nothing."""
    out: list[Received] = []
    seen: set[str] = set()
    for line in _iter_lines(raw):
        if _MARKER not in line:
            continue
        try:
            entry = json.loads(line.decode("utf-8", errors="replace"))
        except ValueError, RecursionError:
            continue
        if not isinstance(entry, dict):
            continue
        carrier = _carrier_of(entry)
        if carrier is None:
            continue
        match = _OPEN_TAG_RE.match(carrier, 0, OPEN_TAG_MAX_CHARS)
        if match is None:
            continue
        attrs: dict[str, str] = {}
        duplicate = False
        for name, value in _ATTR_RE.findall(match.group(1)):
            if name in attrs:
                duplicate = True
                break
            attrs[name] = value
        if duplicate:
            continue
        from_address, from_name = attrs.get("from", ""), attrs.get("from-name", "")
        if not from_address or not from_name:
            continue
        clipped_name = _clip16(from_name, NAME_CHARS)
        if not clipped_name:
            continue
        close_at = carrier.find(_CLOSE_TAG, match.end())
        if close_at == -1:
            continue
        if not _TIMESTAMP_RE.fullmatch(attrs.get("timestamp", "")):
            continue
        element = carrier[: close_at + len(_CLOSE_TAG)]
        if element in seen:
            continue
        seen.add(element)
        body = carrier[match.end() : close_at]
        first_line = next((ln for ln in body.split("\n") if ln.strip()), "")
        out.append(
            Received(
                at=attrs["timestamp"],
                from_address=from_address,
                from_name=clipped_name,
                text=_clip16(first_line, TEXT_CHARS),
            )
        )
    return out


def _socket_map(claude_home: Path) -> dict[str, str]:
    """`{messagingSocketPath: sessionId}` for every registry entry with both as strings — the
    registry a `uds:` sender address is checked against."""
    socket_map: dict[str, str] = {}
    for entry in _registry(claude_home):
        socket, session_id = entry.get("messagingSocketPath"), entry.get("sessionId")
        if isinstance(socket, str) and isinstance(session_id, str):
            socket_map[socket] = session_id
    return socket_map


def _resolve_sender(
    from_address: str, socket_map: dict[str, str], listed_ids: set[str]
) -> str | None:
    """The sending session's id, only when `from_address` is a `uds:` socket this run's registry
    maps to a session that is itself listed and not `other`, and is a lowercase UUID — never a
    bare path, and never a session this run cannot otherwise show or the client would refuse."""
    if not from_address.startswith("uds:"):
        return None
    session_id = socket_map.get(from_address[len("uds:") :])
    if session_id is None or session_id not in listed_ids:
        return None
    return session_id if _UUID_RE.fullmatch(session_id) else None


def between_chats(catalog: Catalog) -> dict[str, object]:
    """The newest messages the vault's listed chats received from other Claude Code sessions: at
    most MAX_MESSAGES, newest first, read from the newest SCAN_SESSIONS listed non-other
    sessions' newest SCAN_BYTES of whole lines. Messages are not deduped across sessions, so one
    message sent to several chats is one row per chat it reached.

    `partial` when the listing itself is not ready, or a scanned transcript could not be read; a
    transcript that simply vanished, or holds nothing of the shape sought, is not a failure."""
    listing = catalog.list_sessions()
    # Only a lowercase-UUID session id can ever pass the client's `to_session_id`/
    # `from_session_id` checks, so a listed row shaped otherwise is skipped outright.
    non_other = [
        row for row in listing.sessions if not row.other and _UUID_RE.fullmatch(row.session_id)
    ]
    listed_ids = {row.session_id for row in non_other}
    socket_map = _socket_map(catalog.claude_home)

    partial = listing.state != "ready"
    messages: list[_Message] = []
    for row in non_other[:SCAN_SESSIONS]:
        path = catalog.transcript_path(row.session_id)
        if path is None:
            continue
        stat = _stat_for_read(path)
        if isinstance(stat, str):
            if stat == "unreadable":
                partial = True
            continue
        try:
            raw = _tail_window(path, stat.st_size, window=SCAN_BYTES)
        except FileNotFoundError:
            continue
        except OSError:
            partial = True
            continue
        if _MARKER not in raw:
            continue
        # Keep only each tail's own newest MAX_MESSAGES before adding to the request's list: the
        # global cap below can never keep more than MAX_MESSAGES from any one tail anyway, so
        # this bounds the cost of a tail holding far more distinct elements than that.
        newest = heapq.nlargest(MAX_MESSAGES, received_messages(raw), key=lambda r: r.at)
        for received in newest:
            messages.append(
                _Message(
                    at=received.at,
                    from_name=received.from_name,
                    from_session_id=_resolve_sender(received.from_address, socket_map, listed_ids),
                    to_session_id=row.session_id,
                    text=received.text,
                )
            )

    messages.sort(key=lambda m: m["at"], reverse=True)
    return {"messages": messages[:MAX_MESSAGES], "partial": partial}
