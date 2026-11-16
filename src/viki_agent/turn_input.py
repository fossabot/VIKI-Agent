"""The durable, provider-neutral shape of one user turn.

Binary bytes never enter this value.  Attachments refer only to app-owned asset IDs; the
asset service is responsible for resolving those IDs after admission.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from viki_agent.conversations.turn_parts import (
    EXCERPTS_PER_TURN,
    ExcerptPart,
    TurnInputError,
    parse_excerpt_part,
    part_data,
)

MAX_TEXT_BYTES = 1 << 20
MAX_ATTACHMENTS = 10
MAX_ATTACHMENT_BYTES = 20 << 20
MAX_TURN_ATTACHMENT_BYTES = 50 << 20
MAX_PIXELS = 40_000_000
IMAGE_MEDIA_TYPES = frozenset({"image/png", "image/jpeg", "image/webp"})
_SOURCES = frozenset({"picker", "paste", "drop", "history"})

# Conservative adapter limits, deliberately below the app's local intake caps.  These are
# provider capability facts, not a claim that the CLI accepts every image the app can retain.
CLAUDE_MAX_ATTACHMENT_BYTES = 5 << 20
CLAUDE_MAX_TURN_ATTACHMENT_BYTES = 20 << 20
CLAUDE_MAX_DIMENSION = 8000


class AttachmentUnavailable(Exception):
    """An attachment could not be read, or no longer matches its metadata. Raised before any
    provider write, so nothing of the turn was sent.

    `transient` marks a read that may succeed if retried (e.g. a momentarily busy resolver),
    as opposed to a durable refusal (missing resolver, corrupt or mismatched bytes)."""

    def __init__(self, *, transient: bool = False) -> None:
        self.transient = transient
        super().__init__()


def _uuid(value: Any) -> str:
    if isinstance(value, str) and len(value) == 36:
        try:
            if str(uuid.UUID(value)) == value:
                return value
        except ValueError:
            pass
    raise TurnInputError("invalid attachment")


def _timestamp(value: Any) -> str:
    if not isinstance(value, str) or len(value) != 24:
        raise TurnInputError("invalid attachment")
    try:
        canonical = datetime.fromisoformat(value).isoformat(timespec="milliseconds")
    except ValueError:
        raise TurnInputError("invalid attachment") from None
    if canonical.replace("+00:00", "Z") != value:
        raise TurnInputError("invalid attachment")
    return value


@dataclass(frozen=True)
class Attachment:
    """A validated image asset as the user selected it, never a filesystem path."""

    asset_id: str
    media_type: str
    byte_size: int
    width: int
    height: int
    name: str
    modified_at: str | None = None
    source: str = "picker"

    def __post_init__(self) -> None:
        _uuid(self.asset_id)
        if self.media_type not in IMAGE_MEDIA_TYPES:
            raise TurnInputError("invalid attachment")
        if type(self.byte_size) is not int or not 1 <= self.byte_size <= MAX_ATTACHMENT_BYTES:
            raise TurnInputError("invalid attachment")
        if (
            type(self.width) is not int
            or type(self.height) is not int
            or self.width < 1
            or self.height < 1
            or self.width * self.height > MAX_PIXELS
        ):
            raise TurnInputError("invalid attachment")
        try:
            name_bytes = self.name.encode("utf-8") if isinstance(self.name, str) else b""
        except UnicodeError:
            raise TurnInputError("invalid attachment") from None
        if (
            not isinstance(self.name, str)
            or not self.name
            or len(name_bytes) > 512
            or "\x00" in self.name
            or "/" in self.name
            or "\\" in self.name
        ):
            raise TurnInputError("invalid attachment")
        if self.modified_at is not None:
            _timestamp(self.modified_at)
        if self.source not in _SOURCES:
            raise TurnInputError("invalid attachment")

    def data(self) -> dict[str, object]:
        return {
            "asset_id": self.asset_id,
            "media_type": self.media_type,
            "byte_size": self.byte_size,
            "width": self.width,
            "height": self.height,
            "name": self.name,
            "modified_at": self.modified_at,
            "source": self.source,
        }


@dataclass(frozen=True)
class TurnInput:
    text: str
    attachments: tuple[Attachment, ...] = ()
    parts: tuple[ExcerptPart, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise TurnInputError("invalid text")
        try:
            text_bytes = self.text.encode("utf-8")
        except UnicodeError:
            raise TurnInputError("invalid text") from None
        if len(text_bytes) > MAX_TEXT_BYTES:
            raise TurnInputError("invalid text")
        if not isinstance(self.attachments, tuple) or len(self.attachments) > MAX_ATTACHMENTS:
            raise TurnInputError("invalid attachment")
        if not all(isinstance(value, Attachment) for value in self.attachments):
            raise TurnInputError("invalid attachment")
        if len({value.asset_id for value in self.attachments}) != len(self.attachments):
            raise TurnInputError("invalid attachment")
        if sum(value.byte_size for value in self.attachments) > MAX_TURN_ATTACHMENT_BYTES:
            raise TurnInputError("invalid attachment")
        if not isinstance(self.parts, tuple) or len(self.parts) > EXCERPTS_PER_TURN:
            raise TurnInputError("invalid part")
        if not all(isinstance(value, ExcerptPart) for value in self.parts):
            raise TurnInputError("invalid part")
        spans = {(value.ref, value.start_line, value.end_line) for value in self.parts}
        if len(spans) != len(self.parts):
            raise TurnInputError("invalid part")

    @property
    def text_bytes(self) -> int:
        return len(self.text.encode("utf-8"))

    @property
    def sendable(self) -> bool:
        return bool(self.text.encode("utf-8") or self.attachments)

    @property
    def wire_format(self) -> int:
        """The durable format of this turn's row: 1 text only, 2 with attachments, 3 with parts."""
        return 3 if self.parts else (2 if self.attachments else 1)

    def data(self) -> dict[str, object]:
        data: dict[str, object] = {
            "text": self.text,
            "attachments": [value.data() for value in self.attachments],
        }
        if self.parts:
            # Only a format 3 turn carries the key, so every older row re-encodes byte-identically.
            data["parts"] = [part_data(value) for value in self.parts]
        return data

    def encode(self) -> bytes:
        return json.dumps(self.data(), ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    @classmethod
    def decode(cls, value: bytes) -> TurnInput:
        try:
            raw = json.loads(value.decode("utf-8"))
        except UnicodeError, ValueError, RecursionError:
            raise TurnInputError("invalid turn") from None
        # Format 2's two keys, or format 3's three, and nothing else: a fourth key still fails
        # closed, as a format 2 reader does on a format 3 row.
        if not isinstance(raw, dict) or set(raw) - {"parts"} != {"text", "attachments"}:
            raise TurnInputError("invalid turn")
        attachments = raw.get("attachments")
        if not isinstance(attachments, list):
            raise TurnInputError("invalid attachment")
        if not all(isinstance(item, dict) for item in attachments):
            raise TurnInputError("invalid attachment")
        parts = raw.get("parts", [])
        if not isinstance(parts, list):
            raise TurnInputError("invalid turn")
        try:
            return cls(
                text=raw["text"],
                attachments=tuple(Attachment(**item) for item in attachments),
                parts=tuple(parse_excerpt_part(item) for item in parts),
            )
        except TypeError, TurnInputError:
            raise TurnInputError("invalid turn") from None


def coerce(value: str | TurnInput) -> TurnInput:
    """Keep the plain-string APIs source-compatible while normalising at the boundary."""
    if isinstance(value, str):
        return TurnInput(value)
    if isinstance(value, TurnInput):
        return value
    raise TurnInputError("invalid turn")


def claude_accepts(value: TurnInput) -> bool:
    """The typed Claude adapter's published image envelope; text has no extra local limit."""
    return (
        len(value.attachments) <= MAX_ATTACHMENTS
        and sum(item.byte_size for item in value.attachments) <= CLAUDE_MAX_TURN_ATTACHMENT_BYTES
        and all(
            item.byte_size <= CLAUDE_MAX_ATTACHMENT_BYTES
            and item.width <= CLAUDE_MAX_DIMENSION
            and item.height <= CLAUDE_MAX_DIMENSION
            for item in value.attachments
        )
    )
