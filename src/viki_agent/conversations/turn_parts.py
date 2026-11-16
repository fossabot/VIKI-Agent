"""The non-text parts of one durable turn: today only a vault excerpt (wire format 3).

A part names a note and a line span, never the note's bytes, so a stored turn stays small and a
reader can tell what the user quoted.  This module opens no file: `render_excerpt_block` formats
text its caller already read, under the caller's own bounds.

`TurnInputError` lives here, not in `turn_input.py`, so the part parser raises it without importing
back into `turn_input.py`, which imports this module.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass

EXCERPT_MAX_LINES = 200  # one part's line span
EXCERPT_MAX_BYTES = 32 << 10  # one part's note text
EXCERPTS_PER_TURN = 8
EXCERPT_BLOCK_CHARS = 12_000  # all blocks of one turn, together

MAX_LINE = 1_000_000
EXCERPT_KEYS = frozenset({"kind", "vault_id", "ref", "start_line", "end_line", "sha256"})

_HEX64 = re.compile(r"\A[0-9a-f]{64}\Z")
_BACKTICKS = re.compile(r"`+")


class TurnInputError(ValueError):
    """An untrusted turn shape is invalid without exposing its contents."""


def _canonical_uuid(value: object) -> str:
    if isinstance(value, str) and len(value) == 36:
        try:
            if str(uuid.UUID(value)) == value:
                return value
        except ValueError:
            pass
    raise TurnInputError("invalid part")


def _safe_excerpt_ref(value: object) -> str:
    """The ref of a readable note: `vault.safe_ref` already refuses an absolute, relative,
    control-bearing or private ref, testing `excluded_part(part, write=False)` on every
    component at any depth."""
    # Imported here, not at module scope: `turn_input` imports this module, and `vault` reaches
    # `turn_input` through `conversation_search` and `events`, so a module-level import cycles.
    from viki_agent.vaults.vault import VaultError, safe_ref

    if not isinstance(value, str):
        raise TurnInputError("invalid part")
    try:
        safe_ref(value)
    except VaultError:
        raise TurnInputError("invalid part") from None
    return value


@dataclass(frozen=True, slots=True)
class ExcerptPart:
    """A line span of one vault note, as the user attached it to a turn."""

    vault_id: str
    ref: str
    start_line: int
    end_line: int
    sha256: str

    def __post_init__(self) -> None:
        _canonical_uuid(self.vault_id)
        _safe_excerpt_ref(self.ref)
        if type(self.start_line) is not int or type(self.end_line) is not int:
            raise TurnInputError("invalid part")
        if not 1 <= self.start_line <= self.end_line <= MAX_LINE:
            raise TurnInputError("invalid part")
        if self.end_line - self.start_line >= EXCERPT_MAX_LINES:
            raise TurnInputError("invalid part")
        if not isinstance(self.sha256, str) or not _HEX64.match(self.sha256):
            raise TurnInputError("invalid part")


def part_data(part: ExcerptPart) -> dict[str, object]:
    """The wire dict of one part, `kind` first."""
    return {
        "kind": "vault_excerpt",
        "vault_id": part.vault_id,
        "ref": part.ref,
        "start_line": part.start_line,
        "end_line": part.end_line,
        "sha256": part.sha256,
    }


def parse_excerpt_part(value: object) -> ExcerptPart:
    """One part of an untrusted turn body; every other shape is a `TurnInputError`."""
    if not isinstance(value, dict) or set(value) != EXCERPT_KEYS:
        raise TurnInputError("invalid part")
    if value["kind"] != "vault_excerpt":
        raise TurnInputError("invalid part")
    return ExcerptPart(
        vault_id=value["vault_id"],
        ref=value["ref"],
        start_line=value["start_line"],
        end_line=value["end_line"],
        sha256=value["sha256"],
    )


def render_excerpt_block(ref: str, start: int, end: int, text: str) -> str:
    """The one rendering of an excerpt, for the preview and for the dispatched turn alike. The
    fence is one backtick longer than the longest backtick run in the text, never under three, so
    the text cannot close its own block."""
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    longest = max((len(run.group()) for run in _BACKTICKS.finditer(text)), default=0)
    fence = "`" * max(longest + 1, 3)
    return (
        f"[vault excerpt] {ref}, lines {start}-{end}, sha256 {digest[:12]}\n"
        f"{fence}\n{text}\n{fence}"
    )
