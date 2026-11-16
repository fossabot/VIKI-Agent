"""Bounded provider rendering of safe chat and complete literal requests."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterator
from contextlib import suppress

from viki_agent.messaging.mattermost_format import (
    _FENCE_CLOSE,
    _FENCE_OPEN,
    _SHORTENED,
    MAX_POST_BYTES,
    _url_like,
)
from viki_agent.messaging.messaging_channel import Provider

_ESCAPE = re.compile(r"\\(?:u[0-9a-fA-F]{4}|U[0-9a-fA-F]{8}|.)", re.DOTALL)
_LOW_SURROGATE = re.compile(r"\\u[dD][c-fC-F][0-9a-fA-F]{2}")
_SAFE_ESCAPE = re.compile(r"\\(?:u[0-9a-fA-F]{4}|U[0-9a-fA-F]{8})")
_CODE_RUN = re.compile(r"(?:`[^`\n]+`)+")
_CODE_SPAN = re.compile(r"`([^`\n]+)`")
_MAX_PARTS = 32


def _atoms(body: str) -> Iterator[str]:
    index = 0
    while index < len(body):
        escaped = _ESCAPE.match(body, index)
        atom = escaped.group() if escaped else body[index]
        if atom.startswith("\\u") and len(atom) == 6 and 0xD800 <= int(atom[2:], 16) <= 0xDBFF:
            following = _LOW_SURROGATE.match(body, index + len(atom))
            if following:
                atom += following.group()
        index += len(atom)
        yield atom


def _width(value: str, provider: Provider) -> int:
    return len(value) if provider == "slack" else sum(1 + (ord(char) > 0xFFFF) for char in value)


def _slack_prose(value: str) -> str:
    value = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"_\1_", value)
    value = re.sub(r"\*\*([^*\n]+)\*\*|__([^_\n]+)__", lambda m: "*" + (m[1] or m[2]) + "*", value)
    return re.sub(r"(?m)^#{1,6} +([^\n]+)$", r"*\1*", value)


def _safe_content(value: str, *, code: bool = False) -> None:
    if any(char in value for char in "@<>&") or (not code and any(c in value for c in "`~[]")):
        raise ValueError("messaging_render_invalid")
    for char in value:
        if char != "\n" and unicodedata.category(char) in {"Cc", "Cf"}:
            raise ValueError("messaging_render_invalid")
    if not code:
        if any(
            atom.startswith("\\") and not _SAFE_ESCAPE.fullmatch(atom) for atom in _atoms(value)
        ):
            raise ValueError("messaging_render_invalid")
        visible = _SAFE_ESCAPE.sub(" ", value)
        if any(_url_like(match.group()) for match in re.finditer(r"\S+", visible)):
            raise ValueError("messaging_render_invalid")


def _chat_events(formatted: str, provider: Provider) -> list[tuple[str, str]]:
    """Mark only authored code delimiters; unsafe prose stays inert or is refused."""
    events: list[tuple[str, str]] = []
    fence_width = 0
    lines = formatted.split("\n")
    for index, part in enumerate(lines):
        line = part + ("\n" if index < len(lines) - 1 else "")
        if fence_width:
            closer = _FENCE_CLOSE.fullmatch(line)
            if closer and len(closer.group(1)) >= fence_width:
                events.append(
                    ("```" + line[len(closer.group(1)) :] if provider == "slack" else line, "close")
                )
                fence_width = 0
            else:
                _safe_content(line, code=True)
                if "`" in line:
                    raise ValueError("messaging_render_invalid")
                events.extend((atom, "body") for atom in _atoms(line))
            continue
        opener = _FENCE_OPEN.fullmatch(line)
        if opener:
            fence_width = len(opener.group(1))
            events.append(("```\n" if provider == "slack" else line, "open"))
            continue
        cursor = 0
        for match in _CODE_RUN.finditer(line):
            prose = line[cursor : match.start()]
            _safe_content(prose)
            prose = _slack_prose(prose) if provider == "slack" else prose
            events.extend((atom, "body") for atom in _atoms(prose))
            spans = _CODE_SPAN.findall(match.group())
            code = "".join(spans)
            _safe_content(code, code=True)
            events.append(("`", "open"))
            events.extend((atom, "body") for atom in _atoms(code))
            events.append(("`", "close"))
            cursor = match.end()
        prose = line[cursor:]
        _safe_content(prose)
        prose = _slack_prose(prose) if provider == "slack" else prose
        events.extend((atom, "body") for atom in _atoms(prose))
    if fence_width:
        raise ValueError("messaging_render_invalid")
    return events


def _split_chat(events: list[tuple[str, str]], provider: Provider, limit: int) -> tuple[str, ...]:
    chunks: list[str] = []
    current: list[str] = []
    width = 0
    opener = ""
    for atom, action in events:
        following = atom if action == "open" else "" if action == "close" else opener
        suffix = (
            "\n" + "`" * (len(following) - len(following.lstrip("`")))
            if following.startswith("```")
            else "`"
            if following
            else ""
        )
        if width + _width(atom, provider) + _width(suffix, provider) > limit:
            if not current or len(chunks) >= _MAX_PARTS - 1:
                raise ValueError("messaging_render_invalid")
            closing = (
                "\n" + "`" * (len(opener) - len(opener.lstrip("`")))
                if opener.startswith("```")
                else "`"
                if opener
                else ""
            )
            chunks.append("".join(current) + closing)
            current = [opener] if opener else []
            width = _width(opener, provider)
            if width + _width(atom, provider) + _width(suffix, provider) > limit:
                raise ValueError("messaging_render_invalid")
        current.append(atom)
        width += _width(atom, provider)
        opener = following
    chunks.append("".join(current))
    return tuple(chunks)


def render(provider: Provider, formatted: str) -> tuple[str, ...]:
    """Preserve complete literal content, with every expanded post inside its provider cap."""
    if not isinstance(formatted, str) or len(formatted) > MAX_POST_BYTES:
        raise ValueError("messaging_render_invalid")
    size: int | None = None
    with suppress(UnicodeError):
        size = len(formatted.encode("utf-8"))
    if size is None or size > MAX_POST_BYTES:
        raise ValueError("messaging_render_invalid")
    if provider == "mattermost":
        return (formatted,)
    if provider not in ("slack", "discord"):
        raise ValueError("messaging_render_invalid")
    if not formatted.strip():
        raise ValueError("messaging_render_invalid")
    limit = 4000 if provider == "slack" else 2000
    prefix = "```json\n" if formatted.startswith("```json\n") else "```text\n"
    source = formatted.removesuffix(_SHORTENED)
    if not source.startswith(prefix) or not source.endswith("\n```"):
        events = _chat_events(formatted, provider)
        parts = _split_chat(events, provider, limit)
        if len(parts) == 1:
            return parts
        budget = limit - len(f"Part {_MAX_PARTS} of {_MAX_PARTS}\n")
        parts = _split_chat(events, provider, budget)
        return tuple(f"Part {index} of {len(parts)}\n{part}" for index, part in enumerate(parts, 1))
    body = source[len(prefix) : -4]
    if any(char in body for char in "`@<>&"):
        raise ValueError("messaging_render_invalid")
    if source != formatted:
        body += _SHORTENED
    complete = prefix + body + "\n```"
    if _width(complete, provider) <= limit:
        return (complete,)
    budget = limit - len(prefix) - 4 - len(f"Part {_MAX_PARTS} of {_MAX_PARTS}\n")
    chunks: list[str] = []
    current: list[str] = []
    width = 0
    for atom in _atoms(body):
        atom_width = _width(atom, provider)
        if width + atom_width > budget:
            chunks.append("".join(current))
            current, width = [], 0
            if len(chunks) >= _MAX_PARTS:
                raise ValueError("messaging_render_invalid")
        current.append(atom)
        width += atom_width
    chunks.append("".join(current))
    return tuple(
        f"Part {index} of {len(chunks)}\n{prefix}{chunk}\n```"
        for index, chunk in enumerate(chunks, 1)
    )
