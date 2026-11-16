"""Bounded display projection without changing Markdown source or note identity."""

from __future__ import annotations

import codecs
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from typing import Literal

type StructureState = Literal["ready", "absent", "unsupported", "truncated"]
type TitleSource = Literal["title", "name", "h1"]


@dataclass(frozen=True)
class DisplayMetadata:
    state: StructureState
    frontmatter_state: StructureState
    heading_state: StructureState
    display_title: str | None
    title_source: TitleSource | None
    tags: list[str] | None
    type: str | None
    status: str | None


@dataclass(frozen=True)
class ReviewFields:
    confidence: str | None
    created: str | None
    modified: str | None
    summary: str | None


_BYTE_LIMIT = 32768
_LINE_LIMIT = 256
_SCALAR_LIMITS = {"title": 512, "name": 512, "type": 64, "status": 64}
_PLAIN_LITERALS = {"null", "true", "false", "yes", "no", "on", "off"}
_TAG_LIMIT = 16
_TAG_SIZE = 64


def _scalar(raw: str, limit: int) -> str | None:
    if any(unicodedata.category(char) in {"Cc", "Cs"} for char in raw):
        raise ValueError("Unsupported scalar control")
    value = raw.strip(" ")
    if value.startswith('"'):
        value = json.loads(value)
        if not isinstance(value, str):
            raise ValueError("Unsupported scalar type")
    elif value and (
        value.startswith("-")
        or value.lower() in _PLAIN_LITERALS
        or not any(char.isalpha() for char in value)
        or any(not (char.isalpha() or char.isdigit() or char in " .-_/()") for char in value)
    ):
        raise ValueError("Unsupported plain scalar")
    if len(value) > limit or any(unicodedata.category(char) in {"Cc", "Cs"} for char in value):
        raise ValueError("Unsupported scalar size or control")
    return value if value.strip() else None


def _tags(raw: str) -> list[str]:
    if raw.startswith("["):
        try:
            values = json.loads(raw)
        except ValueError:
            return _flow_list_tags(raw)
    else:
        values = [_scalar(raw, _TAG_SIZE)]
    if len(values) > _TAG_LIMIT:
        raise ValueError("Too many tags")
    tags: list[str] = []
    for value in values:
        if not isinstance(value, str):
            raise ValueError("Unsupported tag type")
        tag = _scalar(json.dumps(value), _TAG_SIZE)
        if tag is None or tag in tags:
            raise ValueError("Empty or duplicate tag")
        tags.append(tag)
    return tags


def _flow_list_tags(raw: str) -> list[str]:
    """A flow list of unquoted plain scalars, e.g. ``[Work, note]``: JSON cannot parse it."""
    if not raw.endswith("]"):
        raise ValueError("Unsupported flow list")
    pieces = raw[1:-1].split(",")
    if len(pieces) > _TAG_LIMIT:
        raise ValueError("Too many tags")
    tags: list[str] = []
    for piece in pieces:
        item = piece.strip(" ")
        if not item or any(char in "[]{}\"'" for char in item):
            raise ValueError("Unsupported flow list item")
        tag = _scalar(item, _TAG_SIZE)
        if tag is None or tag in tags:
            raise ValueError("Empty or duplicate tag")
        tags.append(tag)
    return tags


def _heading(lines: list[str], eof: bool) -> tuple[str | None, StructureState]:
    fence = ""
    for line in lines:
        match = re.fullmatch(r" {0,3}(`{3,}|~{3,})(.*)", line)
        if match:
            run, tail = match.groups()
            if not fence:
                fence = run
            elif run[0] == fence[0] and len(run) >= len(fence) and not tail.strip():
                fence = ""
            continue
        if fence or not line.startswith(("# ", "#\t")):
            continue
        title = line[2:]
        if (
            not title.strip()
            or len(title) > 512
            or any(unicodedata.category(char) in {"Cc", "Cs"} for char in title)
        ):
            return None, "unsupported"
        return title, "ready"
    if not eof:
        return None, "truncated"
    return None, "unsupported" if fence else "absent"


def _decode_lines(prefix: bytes, *, complete: bool) -> tuple[list[str], bool]:
    """Bound, decode and split a prefix into lines; ``eof`` is true iff this is the real end."""
    eof = complete and len(prefix) <= _BYTE_LIMIT
    # Decode the whole bounded view: surplus/partial lines cannot hide invalid encoding.
    text = codecs.getincrementaldecoder("utf-8")().decode(prefix[:_BYTE_LIMIT], final=eof)
    text = text.removeprefix("\ufeff")
    parts = text.split("\n")
    lines = [line.removesuffix("\r") for line in parts[:-1]]
    if eof and parts[-1]:
        lines.append(parts[-1])
    eof = eof and len(lines) <= _LINE_LIMIT
    return lines[:_LINE_LIMIT], eof


def extract_display(prefix: bytes, *, complete: bool) -> DisplayMetadata:
    """Interpret bounded bytes without reading, rewriting or normalizing the source."""
    lines, eof = _decode_lines(prefix, complete=complete)

    frontmatter: StructureState = "absent" if lines or eof else "truncated"
    fields: dict[str, str | None] = {}
    tags: list[str] = []
    seen: set[str] = set()
    unknown_block = False
    tag_list = False
    body_start = 0
    if lines and lines[0] == "---":
        frontmatter = "unsupported" if eof else "truncated"
        for index, line in enumerate(lines[1:], start=1):
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            try:
                if line.startswith((" ", "\t")):
                    if unknown_block:
                        continue
                    if not tag_list or not line.startswith("  - "):
                        raise ValueError("Unsupported indentation")
                    tag = _scalar(line[4:], _TAG_SIZE)
                    if tag is None or tag in tags or len(tags) >= _TAG_LIMIT:
                        raise ValueError("Unsupported tag list item")
                    tags.append(tag)
                    continue
                if tag_list and not tags:
                    raise ValueError("Empty tag list")
                tag_list = unknown_block = False
                if line == "---":
                    frontmatter = "ready"
                    body_start = index + 1
                    break
                key, separator, raw = line.partition(":")
                if not separator or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", key):
                    raise ValueError("Unsupported header boundary")
                if key not in _SCALAR_LIMITS and key != "tags":
                    unknown_block = True
                    continue
                if key in seen:
                    raise ValueError("Duplicate metadata key")
                seen.add(key)
                if key == "tags":
                    if raw.strip(" "):
                        tags = _tags(raw.strip(" "))
                    else:
                        tag_list = True
                else:
                    fields[key] = _scalar(raw, _SCALAR_LIMITS[key])
            except ValueError, RecursionError:
                frontmatter = "unsupported"
                break

    if frontmatter != "ready":
        fields.clear()
    heading, heading_state = (
        _heading(lines[body_start:], eof)
        if frontmatter in {"ready", "absent"}
        else (None, frontmatter)
    )
    title = fields.get("title") or fields.get("name")
    source: TitleSource | None = None
    if title is not None:
        source = "title" if fields.get("title") is not None else "name"
    elif heading is not None:
        title, source = heading, "h1"
    state: StructureState
    if "unsupported" in (frontmatter, heading_state):
        state = "unsupported"
    elif "truncated" in (frontmatter, heading_state):
        state = "truncated"
    else:
        state = "ready" if title or tags or fields.get("type") or fields.get("status") else "absent"
    return DisplayMetadata(
        state=state,
        frontmatter_state=frontmatter,
        heading_state=heading_state,
        display_title=title,
        title_source=source,
        tags=tags if frontmatter in {"ready", "absent"} else None,
        type=fields.get("type"),
        status=fields.get("status"),
    )


def _confidence(raw: str | None) -> str | None:
    if raw is None:
        return None
    try:
        return _scalar(raw, 64)
    except ValueError:
        return None


def _date(raw: str | None) -> str | None:
    if raw is None:
        return None
    text = raw.strip(" ")
    if text[:1] == '"' and text[-1:] == '"':
        text = text[1:-1]
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return None
    try:
        date.fromisoformat(text)
    except ValueError:
        return None
    return text


# Each negated class excludes the delimiters, so a failed match stops at the next `[` instead of
# scanning to the end of the line (mirrors vault.py's WIKI_NAME/WIKI_HEADING/LINK_TEXT).
SUMMARY_LINK_ALIAS = re.compile(r"\[\[([^\[\]|]*)\|([^\[\]]*)\]\]")
SUMMARY_LINK_PLAIN = re.compile(r"\[\[([^\[\]|]*)\]\]")


def _summary(lines: list[str]) -> str | None:
    fence_re = re.compile(r" {0,3}(`{3,}|~{3,})(.*)")
    fence = ""
    body: list[str] | None = None
    for index, line in enumerate(lines):
        match = fence_re.fullmatch(line)
        if match:
            run, tail = match.groups()
            if not fence:
                fence = run
            elif run[0] == fence[0] and len(run) >= len(fence) and not tail.strip():
                fence = ""
            continue
        if fence:
            continue
        if re.fullmatch(r"##[ \t]+summary[ \t]*", line, re.IGNORECASE):
            body = lines[index + 1 :]
            break
    if body is None:
        return None

    collected: list[str] = []
    for line in body:
        if not line.strip():
            if collected:
                break
            continue
        if line.startswith("#") or fence_re.fullmatch(line):
            break
        for marker in ("- ", "* ", "> "):
            if line.startswith(marker):
                line = line[len(marker) :]
                break
        collected.append(line)
    if not collected:
        return None

    text = " ".join(collected)
    text = SUMMARY_LINK_ALIAS.sub(r"\2", text)
    text = SUMMARY_LINK_PLAIN.sub(r"\1", text)
    for marker in ("**", "__", "`"):
        text = text.replace(marker, "")
    text = re.sub(r"\s+", " ", text).strip()
    if not text or any(unicodedata.category(char) in {"Cc", "Cs"} for char in text):
        return None
    if len(text) > 200:
        window = text[:200]
        cut = window.rfind(" ", 121)
        text = (text[:cut] if cut != -1 else text[:200]) + "…"
    return text


def extract_review(prefix: bytes, *, complete: bool) -> ReviewFields:
    """Best-effort review fields: unresolved structure or an invalid value leaves it None."""
    lines, _ = _decode_lines(prefix, complete=complete)
    if not lines or lines[0] != "---":
        return ReviewFields(None, None, None, _summary(lines))

    raw: dict[str, str | None] = {}
    body_start: int | None = None
    for index, line in enumerate(lines[1:], start=1):
        if line == "---":
            body_start = index + 1
            break
        if line.startswith((" ", "\t")):
            continue
        key, separator, value = line.partition(":")
        if not separator or key not in {"confidence", "created", "modified"}:
            continue
        raw[key] = value if key not in raw else None

    if body_start is None:
        return ReviewFields(None, None, None, None)
    return ReviewFields(
        _confidence(raw.get("confidence")),
        _date(raw.get("created")),
        _date(raw.get("modified")),
        _summary(lines[body_start:]),
    )
