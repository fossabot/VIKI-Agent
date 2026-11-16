"""Vault-change detection for Decision's self-improvement: a changed note that could gain a link
to another, an older note a changed one may have replaced, and a project whose ticked tasks are
ready to move to the archive.

`read_changes` never raises. A `VaultError` while reading any one note just drops that note (or
that candidate) and sets `partial`; the function still returns whatever it already found. Every
ref this module puts in its output -- a changed note, a link target, an older note, an archive
target -- has already passed `vault_guard.writable`, so a change proposal built from this output
never names a protected path; `writable` never raises either, so this guard never costs a
read.

At most `MAX_NOTE_READS` real `reader.read` calls happen in one `read_changes` call, shared, in
this order, by the changed notes, then archive targets, then link targets, then superseded
candidates -- archive goes right after the changed notes so a waiting ticked-tasks file is never
starved by however many reads the link and superseded searches would otherwise spend first.
Given what the scope's change rows hold back (`changes.Held`), each kind skips, before
spending a read on it, what no proposal could be filed for: a ticked-tasks file whose ticked
lines' key a row holds, in any state, or that an open row targets, so the most ticked files not
held get the archive reads; a link pair whose key a row holds, the search going on to the next
target; and an older note whose superseded pair's key a row holds, or that an open row already
marks replaced, the next alike older note taken instead. A `held` with no room
(`Held.room` false: no change of the scope can be filed that day) holds back every change, so
the read stops after the changed notes, which still feed the user part: no archive, link or
superseded read, each of those left empty. A
link or superseded candidate's own ref already comes from `scan` (whose own walk
already refused a reparse point or a changed directory), so its read is a second, independent
confirmation, not the only guard -- the same belt-and-suspenders the archive target needs for a
different reason: that ref is built from a slug, not read back from `scan`, so nothing but this
read has looked at it yet. Running out of budget mid-way sets `partial` and stops that and every
later kind, rather than spend past the cap.
"""

from __future__ import annotations

import difflib
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from viki_agent.automation.brief import TickedFile, tags_of
from viki_agent.decision_loop.contracts import item_key
from viki_agent.decision_loop.state import inline_tags
from viki_agent.decision_loop.vault_guard import writable
from viki_agent.vaults.vault import (
    NOTE_SUFFIXES,
    Note,
    NoteEntry,
    ScanResult,
    VaultError,
    VaultService,
)

if TYPE_CHECKING:
    # Only for the type hint: `held` is read by its attributes alone, so this module never
    # needs the store module at run time.
    from viki_agent.decision_loop.changes import Held

# The one read budget this module spends from each cycle, shared by every kind below.
MAX_NOTE_READS = 8
# changed: at most this many notes, newest first.
MAX_CHANGED = 3
# links: at most this many pairs a cycle, and the scan pool a changed note's text is matched
# against.
MAX_LINK_PAIRS = 3
MAX_LINK_POOL = 2_000
# superseded: at most this many pairs a cycle, and the same-folder pool a changed note is
# compared against.
MAX_SUPERSEDED_PAIRS = 2
MAX_SUPERSEDED_POOL = 200
SUPERSEDED_RATIO = 0.9
MIN_SUPERSEDED_LETTERS = 4
# A candidate link target's stem must be at least this many (folded) characters.
MIN_STEM_CHARS = 8
# How much of a changed note's text a link search looks at, and how much of a note's body an
# "opening" keeps.
SCAN_WINDOW_CHARS = 16_000
OPENING_CHARS = 600
MENTION_CHARS = 300
# archive: a tasks.md needs at least this many ticked lines before it is worth archiving, and at
# most this many tasks files get a read a cycle, most-ticked first.
MIN_TICKED = 5
MAX_ARCHIVE_TARGETS = 2

_INBOX_PREFIX = "00-inbox/"
_ARCHIVE_PREFIX = "90-archive/"
_WORD = re.compile(r"\w+")


@dataclass(frozen=True)
class NoteFacts:
    """One changed note, read once, as far as a Decision candidate needs it."""

    ref: str
    title: str | None
    tags: tuple[str, ...] | None
    opening: str
    linked_stems: tuple[str, ...]


@dataclass(frozen=True)
class LinkPair:
    """`target`'s stem names appear as a whole-word run somewhere in `source`'s text, and
    `source` does not already link to it. `mention` is the line of `source` that names it, cut
    to `MENTION_CHARS`. `source` and `target` are the full `NoteFacts` already read for each --
    a ref alone is `.source.ref` / `.target.ref` -- so a candidate never needs a second read just
    to learn a tag or an opening."""

    source: NoteFacts
    target: NoteFacts
    mention: str


@dataclass(frozen=True)
class SupersededPair:
    """`older` sits in the same folder as `newer`, was last modified before it, and its stem is
    close enough that `newer` likely replaces it. Both are the full `NoteFacts` already read for
    each -- a ref alone is `.older.ref` / `.newer.ref`."""

    older: NoteFacts
    newer: NoteFacts


@dataclass(frozen=True)
class VaultChanges:
    changed: tuple[NoteFacts, ...]
    links: tuple[LinkPair, ...]
    superseded: tuple[SupersededPair, ...]
    archive: Mapping[str, str]
    partial: bool


class _Budget:
    """The shared read budget one `read_changes` call spends from. Every attempted
    `reader.read`, whether it succeeds or not, spends one; `exhausted` is set the first time a
    caller wanted to spend with nothing left, so that caller's kind can mark its result
    `partial` instead of pretending it looked everywhere it was allowed to."""

    __slots__ = ("left", "exhausted")

    def __init__(self, left: int) -> None:
        self.left = left
        self.exhausted = False

    def spend(self) -> bool:
        if self.left <= 0:
            self.exhausted = True
            return False
        self.left -= 1
        return True


def _norm(text: str) -> str:
    """`text` as a width- and case-blind reader sees it: NFKC, then case-folded -- the same
    reading `vault_guard.writable` gives a ref's own parts."""
    return unicodedata.normalize("NFKC", text).casefold()


def _is_shelved(ref: str) -> bool:
    folded = ref.casefold()
    return folded.startswith(_INBOX_PREFIX) or folded.startswith(_ARCHIVE_PREFIX)


def _folder(ref: str) -> str:
    return ref.rsplit("/", 1)[0] if "/" in ref else ""


def _newest(entries: Sequence[NoteEntry], limit: int) -> list[NoteEntry]:
    """`entries`, newest-modified first (ties by ref, case-blind, for a deterministic order),
    cut to `limit`."""
    return sorted(entries, key=lambda e: (-e.modified, e.ref.casefold()))[:limit]


def _held(held: Held | None, kind: str, ref: str, second: str) -> bool:
    """Whether the change rows hold back the change of `kind` on `ref` (`second`: its other
    note, or the digest of its ticked lines): a row holds its key, `contracts.item_key(kind,
    ref, second)` as `improve.py` builds it, in any state, or an open row targets `ref` (only a
    note marked replaced or a file of ticked tasks has such a target)."""
    return held is not None and (
        item_key(kind, ref, second) in held.keys or (kind, ref) in held.open_targets
    )


def _read(
    reader: VaultService, scope: str, ref: str, budget: _Budget
) -> tuple[Note | None, str | None]:
    """One budgeted read of `ref`: `(note, None)` on success, `(None, <VaultError code>)` for a
    refusal, `(None, "budget")` when nothing was left to spend (no call made)."""
    if not budget.spend():
        return None, "budget"
    try:
        return reader.read(scope, ref), None
    except VaultError as exc:
        return None, exc.code


def _body(text: str) -> str:
    """`text` from just after its closing frontmatter `---` line onward, unchanged; the whole
    text when there is no frontmatter block, empty when one opens but never closes (the same
    rule `automation.brief` uses for an inbox excerpt). Both `_opening` and the link search
    start here, never at a frontmatter line -- `created:`/`modified:` dates would otherwise
    pair every note with whatever daily note shares their digits."""
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return text
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return "".join(lines[i + 1 :])
    return ""


def _opening(text: str) -> str:
    """`_body(text)`, cut to `OPENING_CHARS`."""
    return _body(text)[:OPENING_CHARS]


def _tags(note: Note) -> tuple[str, ...] | None:
    """`note`'s tags: its frontmatter tags (`automation.brief.tags_of`, so a "--- " opener the
    display parser reads as no frontmatter while the plain key reader still finds a `tags` key
    is unknown here too, not just "no tags"), then each inline tag of its whole already-read
    text once, in that order -- so a never-send tag written only in the body, never in
    frontmatter, still shows up here even when the evidence this note feeds only ever carries a
    cut excerpt (a mention line or a 600-character opening) the tag itself may sit past. None
    (unknown) when the frontmatter tags themselves are unknown; no new read either way."""
    front = tags_of(note)
    if front is None:
        return None
    return tuple(dict.fromkeys((*front, *inline_tags(note.text))))


def _link_name(link: str) -> str:
    """`link`'s own name with a trailing note suffix dropped -- but only a real one: plain
    `PurePosixPath(link).stem` cuts at the *last* dot anywhere in the name, so a dotted title
    with no extension at all (`Node.js event loop phases explained`) was cut down to `Node`.
    Any other trailing dot-segment (not a note suffix, so a dotted title, not a real extension)
    stays part of the name; a name that genuinely ends in a note suffix -- including one typed
    or copied with its real `.md` kept, as `Foo.md.md` names its own `Foo.md.md` file -- still
    loses only that one real suffix, matching how `scan`/`read` compute the same note's own
    stem."""
    name = PurePosixPath(link).name
    suffix = PurePosixPath(name).suffix
    return name.removesuffix(suffix) if suffix.casefold() in NOTE_SUFFIXES else name


def _linked_stems(links: tuple[str, ...]) -> tuple[str, ...]:
    """Each already-linked target's own name, folded, once, both as written and without a
    trailing note suffix (`_link_name`) -- what a later mention of the same note must not
    repeat. A note whose own stem ends in a note suffix (the file `X CLAUDE.md.md`), linked the
    usual way as `[[X CLAUDE.md]]`, matches the name as written; a name held too many
    only holds a proposal back."""
    names = (name for link in links for name in (PurePosixPath(link).name, _link_name(link)))
    return tuple(dict.fromkeys(_norm(name) for name in names))


def _note_facts(note: Note) -> NoteFacts:
    display = note.display_metadata
    title = display.display_title if display is not None else None
    return NoteFacts(note.ref, title, _tags(note), _opening(note.text), _linked_stems(note.links))


def _mention(text: str, start: int, end: int) -> str:
    """The line of `text` holding the span `[start, end)`, cut to `MENTION_CHARS`."""
    line_start = text.rfind("\n", 0, start) + 1
    line_end = text.find("\n", end)
    if line_end == -1:
        line_end = len(text)
    return text[line_start:line_end].removesuffix("\r")[:MENTION_CHARS]


def _stem_tokens(stem: str) -> tuple[str, ...]:
    """`stem`'s `\\w+` words, folded, once its folded form is at least `MIN_STEM_CHARS` *and*
    `_stripped_letters(stem)` is not None -- the same floor `_superseded_candidate` holds older
    notes to, so a date-named note (a daily note's stem strips to no letters at all) never
    becomes a link target either: without it, every note's own `created:`/`modified:`
    frontmatter text would otherwise pair it with a same-digited daily note before any real
    mention is even reached. `()` for a short, punctuation-only or date-only stem, which never
    becomes a link target."""
    folded = _norm(stem)
    if len(folded) < MIN_STEM_CHARS or _stripped_letters(stem) is None:
        return ()
    return tuple(_WORD.findall(folded))


def _link_matches(a_text: str, pool: Sequence[NoteEntry]) -> list[tuple[NoteEntry, int, int]]:
    """Entries of `pool` whose stem occurs in `a_text`'s first `SCAN_WINDOW_CHARS` as a
    whole-word run, with that occurrence's `(start, end)` span, in order of first appearance.

    One pass over `a_text`'s tokens. Stems are bucketed by `(first token, token count)`, each
    bucket holding a set of its stems' own token tuples; at token `i`, every length a stem
    starting there could have is tried once, each as one tuple build plus one set lookup --
    never a per-stem scan: a crafted pool of thousands of same-length stems sharing a
    first word, which the old per-stem comparison took up to 30 s on, costs about what the
    token count alone would suggest.
    """
    by_first_length: dict[tuple[str, int], dict[tuple[str, ...], list[NoteEntry]]] = {}
    for entry in pool:
        tokens = _stem_tokens(entry.title)
        if tokens:
            bucket = by_first_length.setdefault((tokens[0], len(tokens)), {})
            bucket.setdefault(tokens, []).append(entry)
    if not by_first_length:
        return []
    lengths_by_first: dict[str, list[int]] = {}
    for first, length in by_first_length:
        lengths_by_first.setdefault(first, []).append(length)
    window = a_text[:SCAN_WINDOW_CHARS]
    matches = list(_WORD.finditer(window))
    folded = [_norm(match.group()) for match in matches]
    total = len(folded)
    found: dict[str, tuple[NoteEntry, int, int]] = {}
    order: list[str] = []
    for i, token in enumerate(folded):
        for length in lengths_by_first.get(token, ()):
            end = i + length
            if end > total:
                continue
            entries = by_first_length[(token, length)].get(tuple(folded[i:end]))
            if entries is None:
                continue
            for entry in entries:
                if entry.ref not in found:
                    found[entry.ref] = (entry, matches[i].start(), matches[end - 1].end())
                    order.append(entry.ref)
    return [found[ref] for ref in order]


def _stripped_letters(stem: str) -> str | None:
    """`stem`, folded with every digit removed, kept only when at least
    `MIN_SUPERSEDED_LETTERS` letters remain -- a stem that is only a date (or a version number)
    strips down to too little to compare."""
    folded = _norm(stem)
    stripped = "".join(c for c in folded if not c.isdigit())
    if sum(1 for c in stripped if c.isalpha()) < MIN_SUPERSEDED_LETTERS:
        return None
    return stripped


def _read_changed(
    reader: VaultService, scope: str, scan: ScanResult, today: date, budget: _Budget
) -> tuple[list[tuple[NoteEntry, Note]], bool]:
    """Up to `MAX_CHANGED` notes modified at or after yesterday's local midnight, newest first,
    each read once. `00-inbox/`, `90-archive/` and any ref `writable` refuses never qualify."""
    cutoff = datetime.combine(today - timedelta(days=1), time.min).astimezone().timestamp()
    eligible = [
        entry
        for entry in scan.entries
        if entry.modified >= cutoff and not _is_shelved(entry.ref) and writable(entry.ref)
    ]
    read_notes: list[tuple[NoteEntry, Note]] = []
    partial = False
    for entry in _newest(eligible, MAX_CHANGED):
        note, code = _read(reader, scope, entry.ref, budget)
        if note is not None:
            read_notes.append((entry, note))
        else:
            partial = True
            if code == "budget":
                break
    return read_notes, partial


def _link_pool(scan: ScanResult) -> list[NoteEntry]:
    """The newest `MAX_LINK_POOL` writable, non-shelved entries: the candidates every changed
    note's text is checked against."""
    candidates = [
        entry for entry in scan.entries if not _is_shelved(entry.ref) and writable(entry.ref)
    ]
    return _newest(candidates, MAX_LINK_POOL)


def _find_links(
    reader: VaultService,
    scope: str,
    scan: ScanResult,
    read_notes: Sequence[tuple[NoteEntry, Note]],
    facts_by_ref: Mapping[str, NoteFacts],
    budget: _Budget,
    held: Held | None,
) -> tuple[tuple[LinkPair, ...], bool]:
    if not read_notes:
        return (), False
    pool = _link_pool(scan)
    pairs: list[LinkPair] = []
    used: set[str] = set()
    partial = False
    for a_entry, a_note in read_notes:
        if len(pairs) >= MAX_LINK_PAIRS:
            break
        # The search (and so the mention line it finds) starts only after A's own frontmatter --
        # never at a `created:`/`modified:` line.
        body = _body(a_note.text)
        already_linked = set(_linked_stems(a_note.links))
        candidates = [
            entry
            for entry in pool
            if entry.ref != a_entry.ref
            and entry.ref not in used
            and _norm(entry.title) not in already_linked
        ]
        for entry, start, end in _link_matches(body, candidates):
            if len(pairs) >= MAX_LINK_PAIRS:
                break
            if _held(held, "note_link", a_entry.ref, entry.ref):
                continue
            note, code = _read(reader, scope, entry.ref, budget)
            if note is not None:
                mention = _mention(body, start, end)
                pairs.append(LinkPair(facts_by_ref[a_entry.ref], _note_facts(note), mention))
                used.add(entry.ref)
            else:
                partial = True
                if code == "budget":
                    return tuple(pairs), partial
    return tuple(pairs), partial


def _superseded_candidate(
    scan: ScanResult, a_entry: NoteEntry, a_stripped: str, taken: set[str], held: Held | None
) -> NoteEntry | None:
    """The best-matching older note in `a_entry`'s own folder whose pair the change rows do not
    hold back: highest stem ratio, ties broken newest-first then by ref, among the folder's
    newest `MAX_SUPERSEDED_POOL` entries."""
    pool = _newest(
        [
            entry
            for entry in scan.entries
            if _folder(entry.ref) == _folder(a_entry.ref)
            and entry.ref != a_entry.ref
            and entry.ref not in taken
        ],
        MAX_SUPERSEDED_POOL,
    )
    best: NoteEntry | None = None
    best_ratio = 0.0
    for entry in pool:
        if entry.modified >= a_entry.modified or not writable(entry.ref):
            continue
        if _held(held, "note_superseded", entry.ref, a_entry.ref):
            continue
        stripped = _stripped_letters(entry.title)
        if stripped is None:
            continue
        ratio = difflib.SequenceMatcher(None, a_stripped, stripped).ratio()
        if ratio < SUPERSEDED_RATIO:
            continue
        if (
            best is None
            or ratio > best_ratio
            or (ratio == best_ratio and entry.modified > best.modified)
        ):
            best, best_ratio = entry, ratio
    return best


def _find_superseded(
    reader: VaultService,
    scope: str,
    scan: ScanResult,
    read_notes: Sequence[tuple[NoteEntry, Note]],
    facts_by_ref: Mapping[str, NoteFacts],
    budget: _Budget,
    held: Held | None,
) -> tuple[tuple[SupersededPair, ...], bool]:
    pairs: list[SupersededPair] = []
    taken: set[str] = set()
    partial = False
    for a_entry, _a_note in read_notes:
        if len(pairs) >= MAX_SUPERSEDED_PAIRS:
            break
        a_stripped = _stripped_letters(a_entry.title)
        if a_stripped is None:
            continue
        older = _superseded_candidate(scan, a_entry, a_stripped, taken, held)
        if older is None:
            continue
        note, code = _read(reader, scope, older.ref, budget)
        if note is not None:
            pairs.append(SupersededPair(_note_facts(note), facts_by_ref[a_entry.ref]))
            taken.add(older.ref)
        else:
            partial = True
            if code == "budget":
                break
    return tuple(pairs), partial


def _find_archive(
    reader: VaultService,
    scope: str,
    scan: ScanResult,
    today: date,
    ticked: tuple[TickedFile, ...],
    budget: _Budget,
    held: Held | None,
) -> tuple[dict[str, str], bool]:
    """A ticked-tasks file maps to its would-be archive note once it has at least `MIN_TICKED`
    ticked lines, the change rows do not hold it back (neither its ticked lines' key nor an
    open row's target), and its own ref `writable` accepts -- the scan can list a ref `writable`
    refuses (`10-projects/_raw/tasks.md`, `10-projects/CLAUDE.md/tasks.md`), and the slug taken
    from it is only a prefix of the archive target's own name, so the target alone passing
    `writable` is not enough -- but only when the vault already keeps a `90-archive/` note
    at all, so a vault with no such folder yields nothing rather than inventing the convention
    for it. At most `MAX_ARCHIVE_TARGETS` tasks files get a read, the most ticked first (ties by
    ref, for a deterministic order), so one busy project cannot push every other out of a
    cycle. A kept target is `writable` and reading it answers a note (there is already one to
    append to) or `missing_note` (there is none yet, so one would be created); any other refusal
    -- most importantly a link sitting at that exact path -- drops it."""
    if not any(entry.ref.casefold().startswith(_ARCHIVE_PREFIX) for entry in scan.entries):
        return {}, False
    qualifying = sorted(
        (
            tf
            for tf in ticked
            if tf.count >= MIN_TICKED
            and writable(tf.ref)
            and not _held(held, "tasks_ticked", tf.ref, tf.digest)
        ),
        key=lambda tf: (-tf.count, tf.ref),
    )[:MAX_ARCHIVE_TARGETS]
    archive: dict[str, str] = {}
    partial = False
    for tf in qualifying:
        parts = tf.ref.split("/")
        if len(parts) < 2:
            continue
        target = f"{_ARCHIVE_PREFIX}{parts[1]} tasks done {today.year}.md"
        if not writable(target):
            continue
        note, code = _read(reader, scope, target, budget)
        if note is not None or code == "missing_note":
            archive[tf.ref] = target
        else:
            partial = True
            if code == "budget":
                break
    return archive, partial


def read_changes(
    reader: VaultService,
    scope: str,
    scan: ScanResult,
    today: date,
    ticked: tuple[TickedFile, ...],
    held: Held | None = None,
) -> VaultChanges:
    """Vault-change candidates for one Decision cycle -- see the module docstring for the read
    budget, what `held` (what the scope's change rows hold back; None holds nothing back) skips,
    where a `held` with no room stops the read, and the fail-closed guarantee. `scan` is the
    caller's own, already-taken scan (never re-scanned here); `ticked` is `TaskFacts.ticked`
    from the same read."""
    budget = _Budget(MAX_NOTE_READS)
    read_notes, changed_partial = _read_changed(reader, scope, scan, today, budget)
    # Built once from the reads `_read_changed` already made, then reused as `source/target` and
    # `older/newer` below -- so a link or superseded candidate's tags and opening come from the
    # one read its own ref already costs, never a second one.
    facts_by_ref = {entry.ref: _note_facts(note) for entry, note in read_notes}
    changed = tuple(facts_by_ref[entry.ref] for entry, _ in read_notes)
    if held is not None and not held.room:  # no change can be filed: the changed notes alone
        return VaultChanges(changed, (), (), {}, scan.partial or changed_partial)
    # Archive goes right after the changed notes: it has its own small cap
    # (MAX_ARCHIVE_TARGETS), so reading its targets first never starves it behind however much
    # of the shared budget links and superseded would otherwise have already spent.
    archive, archive_partial = _find_archive(reader, scope, scan, today, ticked, budget, held)
    links, links_partial = _find_links(reader, scope, scan, read_notes, facts_by_ref, budget, held)
    superseded, superseded_partial = _find_superseded(
        reader, scope, scan, read_notes, facts_by_ref, budget, held
    )
    partial = (
        scan.partial or changed_partial or archive_partial or links_partial or superseded_partial
    )
    return VaultChanges(changed, links, superseded, archive, partial)
