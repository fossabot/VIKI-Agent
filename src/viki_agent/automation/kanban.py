"""Whether a seat has a todo card waiting on Kanban: notes directly in `kanban/` whose
frontmatter reads `status: todo` and a tag `owner/<seat>`, spelled exactly as the Kanban view
reads them. Model-free and read-only through `reader` (a `VaultService`): one zero-content scan
and at most `MAX_KANBAN_NOTES / _BATCH` bounded metadata-prefix calls, never a full note read.
Nothing here opens a file, logs, or raises for a vault problem; a `VaultError`, a partial
listing, a frontmatter the display parser could not read, or more notes than the cap leaves the
answer unknown (`None`), so the caller runs the slot rather than skip a card.

`read_kanban` is the second reader over the same walk (`kanban_notes`, `batches`): every card the
Decision loop can see, with the refs it could not read listed apart and never guessed, and the
moment it read them.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath

from viki_agent.vaults.vault import (
    UNIX_EPOCH,
    MetadataItem,
    NoteEntry,
    ScanResult,
    VaultError,
    VaultService,
)

MAX_KANBAN_NOTES = 128
_BATCH = 32
KANBAN_FOLDER = "kanban"
_GENERIC_STEMS = frozenset({"skill", "readme", "index"})


@dataclass(frozen=True)
class Card:
    ref: str
    title: str
    status: str | None
    tags: tuple[str, ...]
    changed: datetime


@dataclass(frozen=True)
class KanbanRead:
    """`read_at` is this computer's clock as `read_kanban` began, None for a Kanban no read made:
    a card changed after it carries a file time ahead of the clock."""

    cards: tuple[Card, ...]
    unread: tuple[str, ...]
    complete: bool
    read_at: datetime | None = None


def kanban_notes(scan: ScanResult) -> tuple[NoteEntry, ...]:
    """The notes directly in `kanban/`, in scan order (`scan.entries` arrives sorted by
    casefolded ref)."""
    return tuple(
        entry
        for entry in scan.entries
        if entry.ref.count("/") == 1 and entry.ref.split("/")[0] == KANBAN_FOLDER
    )


def batches(
    reader: VaultService, workspace_id: str, refs: Sequence[str]
) -> Iterator[tuple[MetadataItem, ...]]:
    """The metadata items of the first `MAX_KANBAN_NOTES` refs, one bounded call per `_BATCH`
    refs, made only as the caller asks for the next batch. A `VaultError` propagates."""
    capped = list(refs[:MAX_KANBAN_NOTES])
    for start in range(0, len(capped), _BATCH):
        yield reader.metadata(workspace_id, capped[start : start + _BATCH]).items


def card_waiting(reader: VaultService, workspace_id: str, seat: str) -> bool | None:
    """True at the first `kanban/` note that is a todo card for `seat`; False only when the
    listing is complete, every Kanban note's frontmatter was read (`ready` or `absent`), none
    matched, and Kanban holds at most `MAX_KANBAN_NOTES` notes; None otherwise."""
    try:
        scan = reader.scan(workspace_id)
    except VaultError:
        return None
    if scan.partial:
        return None
    kanban = kanban_notes(scan)
    owner_tag = f"owner/{seat}"
    undecided = False
    try:
        for items in batches(reader, workspace_id, [entry.ref for entry in kanban]):
            for item in items:
                display = item.display_metadata
                if display is None or display.frontmatter_state not in ("ready", "absent"):
                    undecided = True
                elif (
                    display.frontmatter_state == "ready"
                    and display.status == "todo"
                    and display.tags is not None
                    and owner_tag in display.tags
                ):
                    return True
    except VaultError:
        return None
    return None if undecided or len(kanban) > MAX_KANBAN_NOTES else False


def read_kanban(reader: VaultService, workspace_id: str, scan: ScanResult) -> KanbanRead:
    """Every card among the first `MAX_KANBAN_NOTES` Kanban notes of `scan`. `unread` is every
    Kanban ref the scan lists that did not become a card, in scan order: the refs whose front
    matter, change time or batch could not be read, and the refs listed past the cap, which were
    never read. `complete` says the listing was whole (the scan was not partial), so a ref
    absent from it is gone; it does not say nothing is unread."""
    # Taken after the scan, so a file time it listed is later only when it is ahead of the clock.
    read_at = datetime.now(UTC)
    notes = kanban_notes(scan)
    modified = {entry.ref: entry.modified for entry in notes[:MAX_KANBAN_NOTES]}
    cards: list[Card] = []
    with contextlib.suppress(VaultError):
        for items in batches(reader, workspace_id, [entry.ref for entry in notes]):
            cards.extend(_cards(items, modified))
    placed = {card.ref for card in cards}
    return KanbanRead(
        cards=tuple(cards),
        unread=tuple(entry.ref for entry in notes if entry.ref not in placed),
        complete=not scan.partial,
        read_at=read_at,
    )


def _cards(items: tuple[MetadataItem, ...], modified: dict[str, float]) -> Iterator[Card]:
    for item in items:
        display = item.display_metadata
        if display is None or display.frontmatter_state not in ("ready", "absent"):
            continue
        try:
            changed = UNIX_EPOCH + timedelta(seconds=modified[item.ref])
        except OverflowError, ValueError:
            continue
        yield Card(
            item.ref,
            display.display_title or _path_title(item.ref),
            display.status,
            tuple(display.tags or ()),
            changed,
        )


def _path_title(ref: str) -> str:
    path = PurePosixPath(ref)
    if path.stem.casefold() in _GENERIC_STEMS:
        return f"{path.parent.name} / {path.name}"
    return path.stem
