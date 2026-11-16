"""Memories whose cited notes changed after this device reviewed them.

`memory_drift` reads the memory record files of one vault, never a cited note. It starts from
this device's reviews (`memory.reviews`) and the scan's own change times: a memory is only read
when some note outside `memory/` changed after its review, its `memory/<id>.md` is listed, and
that file is at most `MAX_RECORD_BYTES`. At most `MAX_MEMORY_READS` such records are read through
`VaultService.read` in one call, in id order; the rest set `memory_partial`, as do a partial scan
and a record that fails to read. A record drifts when it parses, carries the id of its file, is
`active` for the approved hash this device holds, and cites a note the scan lists with a change
time after the review. A missing cited note, a session source and a failed read are not drift.

With a `seen` map the call remembers, per memory id, what a record read told (`Seen`: whether it
is active, which notes it cites), keyed by the record file's change time and size and by the
approved hash. A record is read again only when one of those changes, so records read in an earlier
call cost no read and every reviewed memory is checked within a few calls, however many are due.
The map lives in memory only, is pruned to the memories this device still has a review row for, and
drift is worked out in every call from it and the scan's change times. Without it each call starts
cold.

Change times, not hashes: a cited note's hash would need a read of every cited note, and the
scan's modification time is already in hand. A touched but unchanged note therefore drifts,
which only asks the user to look at the memory again; Correct or Retire clears it. A change time
a `datetime` cannot hold is skipped. Store errors propagate; the caller names the failure.
"""

from __future__ import annotations

from collections.abc import MutableMapping
from dataclasses import dataclass
from datetime import datetime, timedelta

from viki_agent.automation.routines import parse_stamp
from viki_agent.storage import SessionStore
from viki_agent.vaults import memory_record
from viki_agent.vaults.memory import FOLDER, reviews
from viki_agent.vaults.vault import UNIX_EPOCH, NoteEntry, ScanResult, VaultError, VaultService

MAX_MEMORY_READS = 32


@dataclass(frozen=True)
class Seen:
    """What reading one record told, valid while its file and the approved hash stay the same."""

    modified: float
    bytes: int
    approved: str
    active: bool
    cites: tuple[str, ...]


def memory_drift(
    store: SessionStore,
    reader: VaultService,
    workspace_id: str,
    scan: ScanResult,
    *,
    seen: MutableMapping[str, Seen] | None = None,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The sorted ids of drifted memories, and the failure words (`memory_partial`)."""
    remembered = {} if seen is None else seen
    changed = {
        entry.ref: at for entry in scan.entries if (at := _changed(entry.modified)) is not None
    }
    listed = {entry.ref: entry for entry in scan.entries}
    newest = max(
        (at for ref, at in changed.items() if not ref.startswith(f"{FOLDER}/")), default=None
    )
    held = reviews(store, workspace_id)
    for gone in remembered.keys() - held.keys():
        del remembered[gone]
    due: list[tuple[str, str, datetime, NoteEntry]] = []
    if newest is not None:
        for memory_id, (approved, reviewed_at) in sorted(held.items()):
            try:
                reviewed = parse_stamp(reviewed_at)
            except ValueError:
                continue
            entry = listed.get(f"{FOLDER}/{memory_id}.md")
            if (
                reviewed < newest
                and entry is not None
                and entry.bytes <= memory_record.MAX_RECORD_BYTES
            ):
                due.append((memory_id, approved, reviewed, entry))
    partial = scan.partial
    reads_left = MAX_MEMORY_READS
    drifted: list[str] = []
    for memory_id, approved, reviewed, entry in due:
        known = remembered.get(memory_id)
        if known is None or (known.modified, known.bytes, known.approved) != (
            entry.modified,
            entry.bytes,
            approved,
        ):
            if reads_left == 0:
                partial = True
                continue
            reads_left -= 1
            try:
                note = reader.read(workspace_id, entry.ref)
            except VaultError:
                partial = True
                continue
            known = remembered[memory_id] = _seen(memory_id, note.source, entry, approved)
        if known.active and any(
            (at := changed.get(cited)) is not None and at > reviewed for cited in known.cites
        ):
            drifted.append(memory_id)
    return tuple(sorted(drifted)), ("memory_partial",) if partial else ()


def _seen(memory_id: str, source: str, entry: NoteEntry, approved: str) -> Seen:
    record = memory_record.parse(source)
    if not (
        isinstance(record, memory_record.Record)
        and record.fields.id == memory_id
        and memory_record.effective(record, approved) == "active"
    ):
        return Seen(entry.modified, entry.bytes, approved, False, ())
    cites = tuple(
        cited.note for cited in record.fields.sources if isinstance(cited, memory_record.NoteSource)
    )
    return Seen(entry.modified, entry.bytes, approved, True, cites)


def _changed(modified: float) -> datetime | None:
    try:
        return UNIX_EPOCH + timedelta(seconds=modified)
    except OverflowError, ValueError:
        return None
