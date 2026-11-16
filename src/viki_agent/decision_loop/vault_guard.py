"""Which vault refs a Decision change proposal may name.

A change proposal becomes a draft the user may send to a chat, so `writable` refuses a ref that
names, or folds to, a protected path. It reads the ref's text only: a link or reparse point
behind a ref is the reader's refusal when the note is read.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import PurePosixPath

from viki_agent.vaults.vault import NOTE_SUFFIXES, VaultError, excluded_part, safe_ref

# Folders at the vault root that no change may name: `system/` (and so `system/memory/`), the
# note templates and the capture inbox.
_ROOT_FOLDERS = frozenset({"system", "_templates", "00-inbox"})
# The create-only originals, at any depth.
_RAW = "_raw"
# A Windows 8.3 short name (`CLAUDE~1.MD`) opens the same entry as its long name.
_SHORT_NAME = re.compile(r"~[0-9]")
# Leading spaces, and trailing dots and spaces: Windows opens `AGENTS.md. ` as `AGENTS.md`, and a
# reader of the draft would not see them.
_EDGES = re.compile(r"\A\s+|[\s.]+\Z")
# A folded part holding one of these would read as two parts, a stream or a drive.
_SEPARATORS = frozenset("/\\:")
# Characters that would end the quoting a draft puts a ref or a stem in: the code span around a
# ref (`), the YAML string of `superseded_by` ("), and a wikilink target ([ ] | # ^).
_QUOTE_BREAKERS = frozenset('`"[]|#^')
# Characters no draft may carry: controls (C0, DEL and C1), which hide in or break its text, the
# line and paragraph separators, which break its line, and lone surrogates, which have no UTF-8
# form.
_UNSAFE = re.compile(r"[\x00-\x1f\x7f-\x9f\N{LINE SEPARATOR}\N{PARAGRAPH SEPARATOR}\ud800-\udfff]")


def writable(ref: object) -> bool:
    """Whether a change proposal may name `ref`. False for anything in doubt; nothing raises.

    True only for an exact str that `safe_ref` takes and that holds no `_UNSAFE` character, when
    every part, as written and folded (`_fold`), passes `excluded_part(write=True)`, starts with
    no "." and holds no 8.3 `~N` and no `_QUOTE_BREAKERS` character; every folded part is
    non-empty, holds no separator and is not `_raw`; the first folded part is not a
    `_ROOT_FOLDERS` name; and the last part's suffix, as written and folded, is a note suffix.
    """
    # An exact str only: a subclass could answer `split` or `casefold` with anything.
    if type(ref) is not str:
        return False
    try:
        parts = safe_ref(ref)
    except VaultError:
        return False
    if _UNSAFE.search(ref):
        return False
    folded = tuple(_fold(part) for part in parts)
    if any(
        excluded_part(part, write=True)
        or part.startswith(".")
        or _SHORT_NAME.search(part)
        or not _QUOTE_BREAKERS.isdisjoint(part)
        for part in parts + folded
    ):
        return False
    if any(not part or part == _RAW or not _SEPARATORS.isdisjoint(part) for part in folded):
        return False
    return (
        folded[0] not in _ROOT_FOLDERS
        and PurePosixPath(parts[-1]).suffix.casefold() in NOTE_SUFFIXES
        and PurePosixPath(folded[-1]).suffix in NOTE_SUFFIXES
    )


def _fold(part: str) -> str:
    """One part as a case- and width-blind reader sees it: NFKC, case-folded, without format
    characters (zero-width spaces and joiners, the soft hyphen, bidi marks) or `_EDGES`."""
    folded = unicodedata.normalize("NFKC", part).casefold()
    return _EDGES.sub("", "".join(c for c in folded if unicodedata.category(c) != "Cf"))
