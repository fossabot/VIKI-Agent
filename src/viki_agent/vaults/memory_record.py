"""The memory record grammar and its effective state: pure, no I/O, no YAML library.

A record is a Markdown note whose frontmatter fixes a small set of fields, one per line, and whose
body is the claim. The grammar is ours and fails closed: whatever it does not admit is
`Invalid(reason)`, never repaired and never guessed. That is the opposite of the CLI's own
frontmatter reader (see `native_assets._frontmatter`), which is lenient on purpose.

`parse` takes the file as a strict UTF-8 decode of its bytes with the BOM kept
(`vault.Note.source`): the record's `sha256` is the hash of that text re-encoded, which is the
file's own hash. A caller refuses a file that is not strict UTF-8 before parsing, and checks that
`id` equals the file stem.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePosixPath
from typing import Literal

KINDS = frozenset({"owner", "assistant", "inference", "summary"})
SCOPES = frozenset({"vault", "personal"})
STATUSES = frozenset({"candidate", "approved", "retired"})

MAX_CLAIM_BYTES = 16_384
MAX_RECORD_BYTES = 32_768
MAX_RECORD_LINES = 199
MAX_SOURCES = 16
# The closing fence must sit within the first 20 lines: 11 fields, the marker's fence and its own.
_HEAD_LINES = 20
_MARKER = "viki_memory: 1"
# The reader's own suffix rule (`vault.NOTE_SUFFIXES`), repeated to keep this module import-free.
_NOTE_SUFFIXES = frozenset({".md", ".markdown"})

NOT_A_RECORD = "not_a_record"

_KEYS = frozenset(
    {
        "viki_memory",
        "id",
        "kind",
        "scope",
        "status",
        "created",
        "reviewed",
        "creator",
        "sources",
        "supersedes",
        "retired_by",
    }
)
_REQUIRED = ("viki_memory", "id", "kind", "scope", "status", "created")
_LINE = re.compile(r"([a-z_]+): ([^\r\n]+)")
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_TIME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z")


@dataclass(frozen=True, slots=True)
class NoteSource:
    note: str
    sha256: str


@dataclass(frozen=True, slots=True)
class SessionSource:
    session: str


Source = NoteSource | SessionSource


@dataclass(frozen=True, slots=True)
class Fields:
    id: str
    kind: str
    scope: str
    status: str
    created: str
    reviewed: str | None = None
    creator: str | None = None
    sources: tuple[Source, ...] = ()
    supersedes: str | None = None
    retired_by: str | None = None


@dataclass(frozen=True, slots=True)
class Record:
    fields: Fields
    claim: str
    sha256: str


@dataclass(frozen=True, slots=True)
class Invalid:
    """`reason` is one of NOT_A_RECORD (no marker: an ordinary note, not an invalid record),
    `too_large`, `encoding`, `unclosed`, `bad_line`, `unknown_key`, `duplicate_key`,
    `missing_key`, `bad_id`, `bad_value`, `bad_time`, `bad_sources` or `bad_claim`."""

    reason: str


State = Literal["invalid", "retired", "active", "pending", "candidate"]


class _Refusal(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def parse(text: str) -> Record | Invalid:
    try:
        return _parse(text)
    except _Refusal as refusal:
        return Invalid(refusal.reason)


def _parse(text: str) -> Record:
    # One leading BOM and CRLF line ends are what another tool may have written; anything else
    # that is not LF is refused below.
    lines = text.removeprefix("﻿").replace("\r\n", "\n").split("\n")
    close = _frontmatter_end(lines)
    try:
        raw = text.encode("utf-8")
    except UnicodeEncodeError:
        raise _Refusal("encoding") from None
    if len(raw) > MAX_RECORD_BYTES or len(lines) - (lines[-1] == "") > MAX_RECORD_LINES:
        raise _Refusal("too_large")
    values: dict[str, str] = {}
    for line in lines[1:close]:
        match = _LINE.fullmatch(line)
        if match is None:
            raise _Refusal("bad_line")
        key, value = match.groups()
        if key not in _KEYS:
            raise _Refusal("unknown_key")
        if key in values:
            raise _Refusal("duplicate_key")
        values[key] = value
    if any(key not in values for key in _REQUIRED):
        raise _Refusal("missing_key")
    fields = _fields(values)
    claim = "\n".join(lines[close + 1 :]).rstrip("\n")
    if "\r" in claim or not claim.strip() or len(claim.encode("utf-8")) > MAX_CLAIM_BYTES:
        raise _Refusal("bad_claim")
    return Record(fields, claim, hashlib.sha256(raw).hexdigest())


def _frontmatter_end(lines: list[str]) -> int:
    """The index of the closing fence. A file that is not fenced with the marker inside is an
    ordinary note; one that has the marker and no closing fence in time is `unclosed`."""
    if lines[0] != "---":
        raise _Refusal(NOT_A_RECORD)
    marked = False
    for index in range(1, min(_HEAD_LINES, len(lines))):
        if lines[index] == "---":
            if marked:
                return index
            break
        marked = marked or lines[index] == _MARKER
    if not marked:
        raise _Refusal(NOT_A_RECORD)
    raise _Refusal("unclosed")


def _fields(values: dict[str, str]) -> Fields:
    if _UUID.fullmatch(values["id"]) is None:
        raise _Refusal("bad_id")
    if (
        values["kind"] not in KINDS
        or values["scope"] not in SCOPES
        or values["status"] not in STATUSES
    ):
        raise _Refusal("bad_value")
    created = _time(values["created"])
    reviewed = _time(values["reviewed"]) if "reviewed" in values else None
    creator = values.get("creator")
    if creator is not None and not (
        len(creator) <= 64 and creator.isprintable() and creator == creator.strip()
    ):
        raise _Refusal("bad_value")
    sources = _sources(values["sources"]) if "sources" in values else ()
    supersedes = _uuid(values.get("supersedes"))
    retired_by = _uuid(values.get("retired_by"))
    if retired_by is not None and values["status"] != "retired":
        raise _Refusal("bad_value")
    return Fields(
        values["id"],
        values["kind"],
        values["scope"],
        values["status"],
        created,
        reviewed,
        creator,
        sources,
        supersedes,
        retired_by,
    )


def _uuid(value: str | None) -> str | None:
    if value is not None and _UUID.fullmatch(value) is None:
        raise _Refusal("bad_value")
    return value


def _time(value: str) -> str:
    try:
        if _TIME.fullmatch(value) is None:
            raise ValueError(value)
        datetime.fromisoformat(value)
    except ValueError:
        raise _Refusal("bad_time") from None
    return value


def _sources(value: str) -> tuple[Source, ...]:
    try:
        loaded = json.loads(
            value, object_pairs_hook=_refuse_duplicate_keys, parse_constant=_refuse_constant
        )
    except ValueError, RecursionError:
        raise _Refusal("bad_sources") from None
    if not isinstance(loaded, list) or len(loaded) > MAX_SOURCES:
        raise _Refusal("bad_sources")
    return tuple(_source(item) for item in loaded)


def _refuse_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("duplicate key")
    return result


def _refuse_constant(name: str) -> object:
    raise ValueError(name)


def _source(item: object) -> Source:
    if not isinstance(item, dict):
        raise _Refusal("bad_sources")
    keys = set(item)
    if keys == {"note", "sha256"}:
        note, digest = item["note"], item["sha256"]
        if (
            isinstance(note, str)
            and _is_note_ref(note)
            and isinstance(digest, str)
            and _SHA256.fullmatch(digest)
        ):
            return NoteSource(note, digest)
    elif keys == {"session"}:
        session = item["session"]
        if isinstance(session, str) and _UUID.fullmatch(session):
            return SessionSource(session)
    raise _Refusal("bad_sources")


def _is_note_ref(ref: str) -> bool:
    """A vault-relative note name's shape: a pointer is read, never written, so only its form."""
    parts = ref.split("/")
    return (
        1 <= len(ref) <= 512
        and ref.isprintable()
        and "\\" not in ref
        and ":" not in ref
        and all(part not in {"", ".", ".."} for part in parts)
        and PurePosixPath(parts[-1]).suffix.casefold() in _NOTE_SUFFIXES
    )


def compose(fields: Fields, claim: str) -> str:
    """The record's text (UTF-8, LF, no BOM) or `ValueError(reason)`: the text must parse back to
    exactly these arguments, so a value that would add a line or change on re-reading is refused."""
    head = [
        "---",
        _MARKER,
        f"id: {fields.id}",
        f"kind: {fields.kind}",
        f"scope: {fields.scope}",
        f"status: {fields.status}",
        f"created: {fields.created}",
    ]
    if fields.reviewed is not None:
        head.append(f"reviewed: {fields.reviewed}")
    if fields.creator is not None:
        head.append(f"creator: {fields.creator}")
    if fields.sources:
        listed = [_source_json(source) for source in fields.sources]
        head.append(f"sources: {json.dumps(listed, ensure_ascii=False, separators=(', ', ': '))}")
    if fields.supersedes is not None:
        head.append(f"supersedes: {fields.supersedes}")
    if fields.retired_by is not None:
        head.append(f"retired_by: {fields.retired_by}")
    text = "\n".join([*head, "---", claim]) + "\n"
    parsed = parse(text)
    if isinstance(parsed, Invalid):
        raise ValueError(parsed.reason)
    if parsed.fields != fields or parsed.claim != claim:
        raise ValueError("not_canonical")
    return text


def _source_json(source: Source) -> dict[str, str]:
    if isinstance(source, NoteSource):
        return {"note": source.note, "sha256": source.sha256}
    return {"session": source.session}


def effective(record: Record | Invalid, approved_sha256: str | None) -> State:
    """The state table: `approved_sha256` is the hash this device approved, if any."""
    if isinstance(record, Invalid):
        return "invalid"
    status = record.fields.status
    if status == "retired":
        return "retired"
    if status == "candidate":
        return "candidate"
    approved_here = approved_sha256 == record.sha256 and record.fields.kind != "inference"
    return "active" if approved_here else "pending"
