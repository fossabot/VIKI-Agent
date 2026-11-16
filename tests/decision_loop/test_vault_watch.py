"""Tests for `vault_watch.read_changes`: a real `VaultService` over a synthetic vault in
`tmp_path` (the same pattern `tests/automation/test_brief.py` and `tests/decision_loop/
test_detectors.py` use), with `spy_reads` counting every `reader.read` call so the at-most-
`MAX_NOTE_READS` budget and the protected-path guard are both proved
against the real reader, never a stand-in for it.
"""

from __future__ import annotations

import os
import time
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from viki_agent.automation.brief import InboxFacts, TaskFacts, TickedFile, VaultFacts
from viki_agent.decision_loop import improve
from viki_agent.decision_loop.changes import Held
from viki_agent.decision_loop.contracts import (
    CHANGES,
    Candidate,
    DecisionSettings,
    Evidence,
    item_key,
)
from viki_agent.decision_loop.detectors import Observation
from viki_agent.decision_loop.state import BuiltState, History, Now, Owner, build_state
from viki_agent.decision_loop.vault_watch import (
    MAX_ARCHIVE_TARGETS,
    MAX_CHANGED,
    MAX_LINK_PAIRS,
    MAX_NOTE_READS,
    MAX_SUPERSEDED_PAIRS,
    MIN_TICKED,
    LinkPair,
    VaultChanges,
    _link_matches,
    read_changes,
)
from viki_agent.domain import CanonicalRoot
from viki_agent.vaults.vault import Note, NoteEntry, VaultError, VaultService
from viki_agent.workspaces import WorkspaceService, root_key

WID = "ws-1"
TODAY = date(2026, 10, 23)


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def touch(path: Path, when: datetime) -> None:
    """Set `path`'s modification time to `when`, read as this machine's local time."""
    stamp = when.astimezone().timestamp()
    os.utime(path, (stamp, stamp))


def recent(hour: int = 10) -> datetime:
    """A moment on `TODAY`, well after yesterday's local midnight (the "changed" cutoff)."""
    return datetime(TODAY.year, TODAY.month, TODAY.day, hour)


def old() -> datetime:
    """A moment well before the "changed" cutoff."""
    return datetime(2026, 9, 1, 10)


def service(root: Path) -> VaultService:
    info = os.stat(root)
    canonical = CanonicalRoot(
        key=root_key(root.resolve()),
        display=str(root.resolve()),
        physical_id=f"{info.st_dev}:{info.st_ino}",
        aliased=False,
    )
    workspace = SimpleNamespace(root=canonical)
    owner = SimpleNamespace(
        get=lambda _id: workspace,
        current_root=lambda _workspace: workspace.root,
    )
    return VaultService(cast(WorkspaceService, owner))


def spy_reads(reader: VaultService, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every ref `reader.read` is actually called with, in call order."""
    calls: list[str] = []
    original = reader.read

    def counting(workspace_id: str, ref: str) -> Note:
        calls.append(ref)
        return original(workspace_id, ref)

    monkeypatch.setattr(reader, "read", counting)
    return calls


def changes(
    reader: VaultService, today: date = TODAY, ticked: tuple[TickedFile, ...] = ()
) -> VaultChanges:
    return read_changes(reader, WID, reader.scan(WID), today, ticked)


# --- the read budgets ------------------------------------------------------------------------


def test_the_budgets_are_the_values_the_plan_names() -> None:
    assert (MAX_NOTE_READS, MAX_CHANGED, MAX_LINK_PAIRS, MAX_SUPERSEDED_PAIRS, MIN_TICKED) == (
        8,
        3,
        3,
        2,
        5,
    )


# --- an empty vault, and a scan failure -------------------------------------------------------


def test_an_empty_vault_gives_empty_unpartial_changes(tmp_path: Path) -> None:
    reader = service(tmp_path)
    result = changes(reader)
    assert result == VaultChanges((), (), (), {}, False)


# --- changed: the cutoff, the cap, the shelved folders, protected paths -----------------------


def test_skips_protected(tmp_path: Path) -> None:
    """A recently touched `AGENTS.md` never becomes a changed note: `writable` refuses
    it even though the scan lists it (the scan itself only excludes dot-directories and private
    names, not the write-only instruction files). A ticked tasks.md whose own ref
    `writable` refuses (`10-projects/_raw/tasks.md`) never reaches `archive` either, even
    though its archive target alone would pass `writable` (the slug is only a prefix of the
    target's own name)."""
    write(tmp_path / "AGENTS.md", "Agent instructions.\n")
    touch(tmp_path / "AGENTS.md", recent())
    write(tmp_path / "30-knowledge" / "Normal note stays visible.md", "Some content here.\n")
    touch(tmp_path / "30-knowledge" / "Normal note stays visible.md", recent())
    write(tmp_path / "90-archive" / "placeholder.md", "x\n")
    ticked = (
        TickedFile("10-projects/_raw/tasks.md", MIN_TICKED, "digest-raw", None),
        TickedFile("10-projects/alpha/tasks.md", MIN_TICKED, "digest-alpha", None),
    )

    result = changes(service(tmp_path), ticked=ticked)

    refs = {fact.ref for fact in result.changed}
    assert refs == {"30-knowledge/Normal note stays visible.md"}
    assert set(result.archive) == {"10-projects/alpha/tasks.md"}


def test_changed_skips_notes_before_yesterdays_local_midnight(tmp_path: Path) -> None:
    write(tmp_path / "30-knowledge" / "Fresh note today.md", "fresh\n")
    touch(tmp_path / "30-knowledge" / "Fresh note today.md", recent())
    write(tmp_path / "30-knowledge" / "Stale note long ago.md", "stale\n")
    touch(tmp_path / "30-knowledge" / "Stale note long ago.md", old())

    result = changes(service(tmp_path))

    assert {fact.ref for fact in result.changed} == {"30-knowledge/Fresh note today.md"}


def test_changed_skips_inbox_and_archive(tmp_path: Path) -> None:
    write(tmp_path / "00-inbox" / "Captured just now.md", "x\n")
    touch(tmp_path / "00-inbox" / "Captured just now.md", recent())
    write(tmp_path / "90-archive" / "Filed just now.md", "x\n")
    touch(tmp_path / "90-archive" / "Filed just now.md", recent())

    result = changes(service(tmp_path))

    assert result.changed == ()


def test_changed_keeps_only_the_newest_three(tmp_path: Path) -> None:
    for i in range(5):
        write(tmp_path / "30-knowledge" / f"Note number {i}.md", "x\n")
        touch(tmp_path / "30-knowledge" / f"Note number {i}.md", recent(hour=i + 1))

    result = changes(service(tmp_path))

    assert [fact.ref for fact in result.changed] == [
        "30-knowledge/Note number 4.md",
        "30-knowledge/Note number 3.md",
        "30-knowledge/Note number 2.md",
    ]


def test_changed_reads_title_tags_opening_and_linked_stems(tmp_path: Path) -> None:
    text = (
        "---\n"
        "title: Formal title\n"
        "tags: [Alpha, Beta]\n"
        "---\n"
        "This is the opening body, right after frontmatter. "
        "It links to [[Some Other Note]] once.\n"
    )
    write(tmp_path / "30-knowledge" / "Readable facts note.md", text)
    touch(tmp_path / "30-knowledge" / "Readable facts note.md", recent())

    result = changes(service(tmp_path))

    assert len(result.changed) == 1
    fact = result.changed[0]
    assert fact.ref == "30-knowledge/Readable facts note.md"
    assert fact.tags == ("alpha", "beta")
    assert fact.opening.startswith("This is the opening body")
    assert fact.linked_stems == ("some other note",)


# --- links ------------------------------------------------------------------------------


def test_links_finds_an_unlinked_whole_word_mention(tmp_path: Path) -> None:
    write(
        tmp_path / "30-knowledge" / "Deny rules hold even in bypass mode.md",
        "Intro line.\nSee also Bypass mode still logs every call for the mechanism.\n",
    )
    touch(tmp_path / "30-knowledge" / "Deny rules hold even in bypass mode.md", recent())
    write(tmp_path / "30-knowledge" / "Bypass mode still logs every call.md", "target body\n")
    touch(tmp_path / "30-knowledge" / "Bypass mode still logs every call.md", old())

    result = changes(service(tmp_path))

    assert len(result.links) == 1
    pair = result.links[0]
    assert pair.source.ref == "30-knowledge/Deny rules hold even in bypass mode.md"
    assert pair.target.ref == "30-knowledge/Bypass mode still logs every call.md"
    assert pair.mention == "See also Bypass mode still logs every call for the mechanism."


def test_links_skips_an_already_linked_mention(tmp_path: Path) -> None:
    """A dotted link target must not be cut at an internal dot the way plain
    `PurePosixPath(link).stem` would -- `[[Node.js event loop phases explained]]` (no real
    extension at all in the link text) and `[[On top of CLAUDE.md.md]]` (the target's full
    file name, both its real dots kept) must each still read as already-linked to their own
    note, alongside the dot-free control."""
    write(
        tmp_path / "30-knowledge" / "Deny rules hold even in bypass mode.md",
        "See [[Bypass mode still logs every call]] for the mechanism. Also see "
        "[[Node.js event loop phases explained]] and [[On top of CLAUDE.md.md]] for more.\n",
    )
    touch(tmp_path / "30-knowledge" / "Deny rules hold even in bypass mode.md", recent())
    write(tmp_path / "30-knowledge" / "Bypass mode still logs every call.md", "target body\n")
    touch(tmp_path / "30-knowledge" / "Bypass mode still logs every call.md", old())
    write(tmp_path / "30-knowledge" / "Node.js event loop phases explained.md", "target body\n")
    touch(tmp_path / "30-knowledge" / "Node.js event loop phases explained.md", old())
    write(tmp_path / "30-knowledge" / "On top of CLAUDE.md.md", "target body\n")
    touch(tmp_path / "30-knowledge" / "On top of CLAUDE.md.md", old())

    result = changes(service(tmp_path))

    assert result.links == ()


def test_links_skip_a_note_linked_without_its_own_note_suffix(tmp_path: Path) -> None:
    """A note whose own stem ends in a note suffix (the file `Notes on top of CLAUDE.md.md`)
    linked the usual way, `[[Notes on top of CLAUDE.md]]`, counts as linked: the link's name is
    held as written and without its note suffix."""
    write(
        tmp_path / "30-knowledge" / "Changed note today.md", "See [[Notes on top of CLAUDE.md]].\n"
    )
    touch(tmp_path / "30-knowledge" / "Changed note today.md", recent())
    write(tmp_path / "30-knowledge" / "Notes on top of CLAUDE.md.md", "target body\n")
    touch(tmp_path / "30-knowledge" / "Notes on top of CLAUDE.md.md", old())

    result = changes(service(tmp_path))

    assert result.links == ()
    assert result.changed[0].linked_stems == ("notes on top of claude.md", "notes on top of claude")


def test_links_skips_a_short_stem(tmp_path: Path) -> None:
    write(
        tmp_path / "30-knowledge" / "Deny rules hold even in bypass mode.md",
        "An Audit happens here regularly.\n",
    )
    touch(tmp_path / "30-knowledge" / "Deny rules hold even in bypass mode.md", recent())
    write(tmp_path / "30-knowledge" / "Audit.md", "target body\n")
    touch(tmp_path / "30-knowledge" / "Audit.md", old())

    result = changes(service(tmp_path))

    assert result.links == ()


@pytest.mark.parametrize("folder", ["00-inbox", "90-archive"])
def test_links_skips_inbox_and_archive_targets(tmp_path: Path, folder: str) -> None:
    write(
        tmp_path / "30-knowledge" / "Deny rules hold even in bypass mode.md",
        "See also Bypass mode still logs every call for the mechanism.\n",
    )
    touch(tmp_path / "30-knowledge" / "Deny rules hold even in bypass mode.md", recent())
    write(tmp_path / folder / "Bypass mode still logs every call.md", "target body\n")
    touch(tmp_path / folder / "Bypass mode still logs every call.md", old())

    result = changes(service(tmp_path))

    assert result.links == ()


def test_links_skips_a_refused_target(tmp_path: Path) -> None:
    write(
        tmp_path / "30-knowledge" / "Deny rules hold even in bypass mode.md",
        "See also Bypass mode still logs every call for the mechanism.\n",
    )
    touch(tmp_path / "30-knowledge" / "Deny rules hold even in bypass mode.md", recent())
    write(tmp_path / "_templates" / "Bypass mode still logs every call.md", "target body\n")
    touch(tmp_path / "_templates" / "Bypass mode still logs every call.md", old())

    result = changes(service(tmp_path))

    assert result.links == ()


def test_links_skips_frontmatter_dates_and_date_named_targets(tmp_path: Path) -> None:
    """The search must not start at A's first character -- its own
    `created:`/`modified:` frontmatter lines would otherwise pair it with every daily note
    sharing those digits, ahead of any real mention -- and a date-named note must never itself
    qualify as a link target (`_stripped_letters` of a date strips to no letters)."""
    write(
        tmp_path / "10-projects" / "viki-agent" / "viki-agent.md",
        "---\ntype: project\nstatus: active\ncreated: 2026-08-18\nmodified: 2026-10-23\n---\n"
        "## Summary\nAs of 2026-10-21 the build follows Deny rules hold even in bypass mode.\n",
    )
    touch(tmp_path / "10-projects" / "viki-agent" / "viki-agent.md", recent())
    for ref in (
        "60-calendar/2026/2026-08-18.md",
        "60-calendar/2026/2026-10-21.md",
        "60-calendar/2026/2026-10-23.md",
    ):
        write(tmp_path / ref, "body\n")
        touch(tmp_path / ref, old())
    write(tmp_path / "30-knowledge" / "Deny rules hold even in bypass mode.md", "body\n")
    touch(tmp_path / "30-knowledge" / "Deny rules hold even in bypass mode.md", old())

    result = changes(service(tmp_path))

    assert [pair.target.ref for pair in result.links] == [
        "30-knowledge/Deny rules hold even in bypass mode.md"
    ]


def test_link_matches_adversarial_pool_runs_under_one_second() -> None:
    """2,000 stems that all share a first token and the same length, matched against 8,000
    tokens that hit that first-token bucket on every position, measured up to 30 s under the
    old per-stem comparison (121 tokens a stem). The lookup must cost close to the token
    count, not stems times stem length."""
    stems = [" ".join(["x"] * 120 + [f"z{i}"]) for i in range(2000)]
    pool = [NoteEntry(f"30-knowledge/a{i}.md", s, 10, 1.0) for i, s in enumerate(stems)]
    text = "x " * 8000

    start = time.perf_counter()
    result = _link_matches(text, pool)
    elapsed = time.perf_counter() - start

    assert elapsed < 1.0
    assert result == []  # the text never continues into any stem's unique trailing "z<i>" token


def test_links_caps_at_three_pairs_a_cycle(tmp_path: Path) -> None:
    write(
        tmp_path / "30-knowledge" / "Hub note gathers references.md",
        "Mentions Reference target alpha here, Reference target beta here, "
        "Reference target gamma here, and Reference target delta here.\n",
    )
    touch(tmp_path / "30-knowledge" / "Hub note gathers references.md", recent())
    for letter in ("alpha", "beta", "gamma", "delta"):
        write(tmp_path / "30-knowledge" / f"Reference target {letter}.md", "x\n")
        touch(tmp_path / "30-knowledge" / f"Reference target {letter}.md", old())

    result = changes(service(tmp_path))

    assert [pair.target.ref for pair in result.links] == [
        "30-knowledge/Reference target alpha.md",
        "30-knowledge/Reference target beta.md",
        "30-knowledge/Reference target gamma.md",
    ]


def test_links_target_facts_carry_tags_and_opening(tmp_path: Path) -> None:
    """`LinkPair.target` is the target's own `NoteFacts`, built from the one read already made
    for it -- so its tags and opening are there for the never-send check and the candidate's
    evidence, with no second read."""
    write(
        tmp_path / "30-knowledge" / "Deny rules hold even in bypass mode.md",
        "See also Bypass mode still logs every call for the mechanism.\n",
    )
    touch(tmp_path / "30-knowledge" / "Deny rules hold even in bypass mode.md", recent())
    write(
        tmp_path / "30-knowledge" / "Bypass mode still logs every call.md",
        "---\ntags: [never-send]\n---\nThe target note's own opening text.\n",
    )
    touch(tmp_path / "30-knowledge" / "Bypass mode still logs every call.md", old())

    result = changes(service(tmp_path))

    assert len(result.links) == 1
    target = result.links[0].target
    assert target.tags == ("never-send",)
    assert target.opening.startswith("The target note's own opening text.")


# --- superseded --------------------------------------------------------------------------


def test_superseded_finds_an_older_alike_note_in_the_same_folder(tmp_path: Path) -> None:
    write(tmp_path / "30-knowledge" / "Topic one standing v2.md", "newer body\n")
    touch(tmp_path / "30-knowledge" / "Topic one standing v2.md", recent())
    write(tmp_path / "30-knowledge" / "Topic one standing v1.md", "older body\n")
    touch(tmp_path / "30-knowledge" / "Topic one standing v1.md", old())

    result = changes(service(tmp_path))

    assert len(result.superseded) == 1
    pair = result.superseded[0]
    assert pair.older.ref == "30-knowledge/Topic one standing v1.md"
    assert pair.newer.ref == "30-knowledge/Topic one standing v2.md"


def test_superseded_skips_date_only_stems(tmp_path: Path) -> None:
    write(tmp_path / "60-calendar" / "2026" / "2026-10-23.md", "today\n")
    touch(tmp_path / "60-calendar" / "2026" / "2026-10-23.md", recent())
    write(tmp_path / "60-calendar" / "2026" / "2026-10-22.md", "yesterday\n")
    touch(tmp_path / "60-calendar" / "2026" / "2026-10-22.md", old())

    result = changes(service(tmp_path))

    assert result.superseded == ()


def test_superseded_skips_another_folder(tmp_path: Path) -> None:
    write(tmp_path / "30-knowledge" / "Topic one standing v2.md", "newer body\n")
    touch(tmp_path / "30-knowledge" / "Topic one standing v2.md", recent())
    write(tmp_path / "40-sources" / "Topic one standing v1.md", "older body, other folder\n")
    touch(tmp_path / "40-sources" / "Topic one standing v1.md", old())

    result = changes(service(tmp_path))

    assert result.superseded == ()


def test_superseded_direction_follows_modification_time_not_the_version_word(
    tmp_path: Path,
) -> None:
    """ "Older" and "newer" are decided by modification time, not by which file is named `v1`
    or `v2`: touched earlier, `v2` is the one actually replaced."""
    write(tmp_path / "30-knowledge" / "Topic one standing v2.md", "touched first\n")
    touch(tmp_path / "30-knowledge" / "Topic one standing v2.md", recent(hour=5))
    write(tmp_path / "30-knowledge" / "Topic one standing v1.md", "touched later\n")
    touch(tmp_path / "30-knowledge" / "Topic one standing v1.md", recent(hour=9))

    result = changes(service(tmp_path))

    assert len(result.superseded) == 1
    pair = result.superseded[0]
    assert pair.older.ref == "30-knowledge/Topic one standing v2.md"
    assert pair.newer.ref == "30-knowledge/Topic one standing v1.md"


def test_superseded_caps_at_two_pairs_a_cycle(tmp_path: Path) -> None:
    for i, folder in enumerate(["30-knowledge", "40-sources", "50-maps"]):
        write(tmp_path / folder / "Topic one standing v2.md", "newer body\n")
        touch(tmp_path / folder / "Topic one standing v2.md", recent(hour=10 - i))
        write(tmp_path / folder / "Topic one standing v1.md", "older body\n")
        touch(tmp_path / folder / "Topic one standing v1.md", old())

    result = changes(service(tmp_path))

    assert [(pair.older.ref, pair.newer.ref) for pair in result.superseded] == [
        ("30-knowledge/Topic one standing v1.md", "30-knowledge/Topic one standing v2.md"),
        ("40-sources/Topic one standing v1.md", "40-sources/Topic one standing v2.md"),
    ]


def test_superseded_older_facts_carry_tags_and_opening(tmp_path: Path) -> None:
    """`SupersededPair.older` is the older note's own `NoteFacts`, built from the one read
    already made for it -- its tags and opening are there with no second read."""
    write(tmp_path / "30-knowledge" / "Topic one standing v2.md", "newer body\n")
    touch(tmp_path / "30-knowledge" / "Topic one standing v2.md", recent())
    write(
        tmp_path / "30-knowledge" / "Topic one standing v1.md",
        "---\ntags: [legacy]\n---\nThe older note's own opening text.\n",
    )
    touch(tmp_path / "30-knowledge" / "Topic one standing v1.md", old())

    result = changes(service(tmp_path))

    assert len(result.superseded) == 1
    older = result.superseded[0].older
    assert older.tags == ("legacy",)
    assert older.opening.startswith("The older note's own opening text.")


def test_superseded_keeps_one_pair_per_older_note_a_cycle(tmp_path: Path) -> None:
    """Two changed notes both alike to the same older note give exactly one SupersededPair --
    the first takes it (`_superseded_candidate`'s `taken`), so the second finds no other
    candidate in the folder and is skipped, rather than filing a second row for the same
    target."""
    write(tmp_path / "30-knowledge" / "Topic one standing v1.md", "older body\n")
    touch(tmp_path / "30-knowledge" / "Topic one standing v1.md", old())
    write(tmp_path / "30-knowledge" / "Topic one standing v2.md", "newer body\n")
    touch(tmp_path / "30-knowledge" / "Topic one standing v2.md", recent(hour=9))
    write(tmp_path / "30-knowledge" / "Topic one standing v3.md", "newest body\n")
    touch(tmp_path / "30-knowledge" / "Topic one standing v3.md", recent(hour=9))

    result = changes(service(tmp_path))

    assert [(pair.older.ref, pair.newer.ref) for pair in result.superseded] == [
        ("30-knowledge/Topic one standing v1.md", "30-knowledge/Topic one standing v2.md")
    ]


# --- archive ------------------------------------------------------------------------------


def test_archive_maps_a_ticked_file_to_a_new_target_when_absent(tmp_path: Path) -> None:
    write(tmp_path / "90-archive" / "placeholder.md", "x\n")
    ticked = (TickedFile("10-projects/alpha/tasks.md", 5, "digest-1", None),)

    result = changes(service(tmp_path), ticked=ticked)

    assert result.archive == {"10-projects/alpha/tasks.md": "90-archive/alpha tasks done 2026.md"}


def test_archive_keeps_an_existing_target(tmp_path: Path) -> None:
    write(tmp_path / "90-archive" / "alpha tasks done 2026.md", "already has entries\n")
    ticked = (TickedFile("10-projects/alpha/tasks.md", 5, "digest-1", None),)

    result = changes(service(tmp_path), ticked=ticked)

    assert result.archive == {"10-projects/alpha/tasks.md": "90-archive/alpha tasks done 2026.md"}


def test_archive_skips_when_no_archive_folder_exists(tmp_path: Path) -> None:
    ticked = (TickedFile("10-projects/alpha/tasks.md", 5, "digest-1", None),)

    result = changes(service(tmp_path), ticked=ticked)

    assert result.archive == {}


def test_archive_skips_under_the_ticked_minimum(tmp_path: Path) -> None:
    write(tmp_path / "90-archive" / "placeholder.md", "x\n")
    ticked = (TickedFile("10-projects/alpha/tasks.md", MIN_TICKED - 1, "digest-1", None),)

    result = changes(service(tmp_path), ticked=ticked)

    assert result.archive == {}


def test_archive_skips_when_the_target_is_a_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(tmp_path / "90-archive" / "placeholder.md", "x\n")
    target = "90-archive/alpha tasks done 2026.md"
    reader = service(tmp_path)
    original = reader.read

    def refuses_the_target(workspace_id: str, ref: str) -> Note:
        if ref == target:
            raise VaultError("unsafe_path")
        return original(workspace_id, ref)

    monkeypatch.setattr(reader, "read", refuses_the_target)
    ticked = (TickedFile("10-projects/alpha/tasks.md", 5, "digest-1", None),)

    result = changes(reader, ticked=ticked)

    assert result.archive == {}


# --- the shared eight-read budget --------------------------------------------


def test_links_and_superseded_cost_no_extra_read_for_their_facts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One changed note that earns both a link pair and a superseded pair: the target and the
    older note are each read exactly once, that one read also carrying their `NoteFacts`."""
    write(
        tmp_path / "30-knowledge" / "Topic one standing v2.md",
        "See also Reference target node here for the mechanism.\n",
    )
    touch(tmp_path / "30-knowledge" / "Topic one standing v2.md", recent())
    write(tmp_path / "30-knowledge" / "Topic one standing v1.md", "older body\n")
    touch(tmp_path / "30-knowledge" / "Topic one standing v1.md", old())
    write(tmp_path / "30-knowledge" / "Reference target node here.md", "target body\n")
    touch(tmp_path / "30-knowledge" / "Reference target node here.md", old())

    reader = service(tmp_path)
    calls = spy_reads(reader, monkeypatch)
    result = changes(reader)

    assert len(result.links) == 1
    assert len(result.superseded) == 1
    assert sorted(calls) == sorted(
        [
            "30-knowledge/Topic one standing v2.md",
            "30-knowledge/Reference target node here.md",
            "30-knowledge/Topic one standing v1.md",
        ]
    )
    assert len(calls) == len(set(calls))  # every ref read exactly once -- no second read


def test_archive_keeps_at_most_two_tasks_files_most_ticked_first(tmp_path: Path) -> None:
    """Archive's own cap is `MAX_ARCHIVE_TARGETS`, independent of the shared read budget --
    with more qualifying tasks files than that, the most-ticked ones win."""
    write(tmp_path / "90-archive" / "placeholder.md", "x\n")
    ticked = (
        TickedFile("10-projects/low/tasks.md", MIN_TICKED, "d-low", None),
        TickedFile("10-projects/mid/tasks.md", MIN_TICKED + 2, "d-mid", None),
        TickedFile("10-projects/high/tasks.md", MIN_TICKED + 5, "d-high", None),
    )

    result = changes(service(tmp_path), ticked=ticked)

    assert MAX_ARCHIVE_TARGETS == 2
    assert set(result.archive) == {"10-projects/high/tasks.md", "10-projects/mid/tasks.md"}


def test_archive_ties_on_ticked_count_break_by_ref(tmp_path: Path) -> None:
    write(tmp_path / "90-archive" / "placeholder.md", "x\n")
    ticked = (
        TickedFile("10-projects/zeta/tasks.md", MIN_TICKED, "d-zeta", None),
        TickedFile("10-projects/alpha/tasks.md", MIN_TICKED, "d-alpha", None),
        TickedFile("10-projects/mid/tasks.md", MIN_TICKED, "d-mid", None),
    )

    result = changes(service(tmp_path), ticked=ticked)

    assert set(result.archive) == {"10-projects/alpha/tasks.md", "10-projects/mid/tasks.md"}


def test_read_changes_shares_its_eight_read_budget_with_archive_going_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Archive reads its (at most two) targets right after the changed notes, so a
    cycle busy enough to want everything from every kind still spends at most 8 reads total --
    archive gets its share first, and whatever is left splits between links and superseded
    (here, entirely to links, leaving none for superseded)."""
    for i, letter in enumerate(("alpha", "beta", "gamma")):
        write(
            tmp_path / "30-knowledge" / f"Topic {letter} standing v2.md",
            f"Mentions Reference target {letter} one, Reference target {letter} two, "
            f"Reference target {letter} three and Reference target {letter} four.\n",
        )
        touch(tmp_path / "30-knowledge" / f"Topic {letter} standing v2.md", recent(hour=i + 1))
        write(tmp_path / "30-knowledge" / f"Topic {letter} standing v1.md", "older body\n")
        touch(tmp_path / "30-knowledge" / f"Topic {letter} standing v1.md", old())
        for word in ("one", "two", "three", "four"):
            write(tmp_path / "30-knowledge" / f"Reference target {letter} {word}.md", "x\n")
            touch(tmp_path / "30-knowledge" / f"Reference target {letter} {word}.md", old())
    write(tmp_path / "90-archive" / "placeholder.md", "x\n")
    ticked = (
        TickedFile("10-projects/p1/tasks.md", MIN_TICKED, "d1", None),
        TickedFile("10-projects/p2/tasks.md", MIN_TICKED, "d2", None),
    )

    reader = service(tmp_path)
    calls = spy_reads(reader, monkeypatch)
    result = changes(reader, ticked=ticked)

    assert len(calls) == MAX_NOTE_READS  # changed 3 + archive 2 + links 3 + superseded 0
    assert set(result.archive) == {"10-projects/p1/tasks.md", "10-projects/p2/tasks.md"}
    assert len(result.links) == MAX_LINK_PAIRS
    assert result.superseded == ()
    assert result.partial is True


def test_read_changes_never_exceeds_eight_reads_with_everything_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for i, letter in enumerate(("alpha", "beta", "gamma")):
        write(
            tmp_path / "30-knowledge" / f"Topic {letter} standing v2.md",
            f"Mentions Reference target {letter} one, Reference target {letter} two, "
            f"Reference target {letter} three and Reference target {letter} four.\n",
        )
        touch(tmp_path / "30-knowledge" / f"Topic {letter} standing v2.md", recent(hour=i + 1))
        write(tmp_path / "30-knowledge" / f"Topic {letter} standing v1.md", "older body\n")
        touch(tmp_path / "30-knowledge" / f"Topic {letter} standing v1.md", old())
        for word in ("one", "two", "three", "four"):
            write(tmp_path / "30-knowledge" / f"Reference target {letter} {word}.md", "x\n")
            touch(tmp_path / "30-knowledge" / f"Reference target {letter} {word}.md", old())
    write(tmp_path / "90-archive" / "placeholder.md", "x\n")
    ticked = (TickedFile("10-projects/alpha/tasks.md", MIN_TICKED, "digest-1", None),)

    reader = service(tmp_path)
    calls = spy_reads(reader, monkeypatch)
    result = changes(reader, ticked=ticked)

    assert len(calls) <= MAX_NOTE_READS
    assert len(result.changed) <= MAX_CHANGED
    assert len(result.links) <= MAX_LINK_PAIRS
    assert len(result.superseded) <= MAX_SUPERSEDED_PAIRS


# --- what the change rows hold back costs no read -----------------------------------------


def test_archive_skips_held_files_so_a_third_project_is_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tasks file whose ticked lines a change row holds (its key, in any state), or that
    an open row already targets, spends no read, so the next most ticked file takes its place:
    a third project is checked once the top two are held."""
    write(tmp_path / "90-archive" / "placeholder.md", "x\n")
    high = TickedFile("10-projects/high/tasks.md", MIN_TICKED + 5, "d-high", None)
    mid = TickedFile("10-projects/mid/tasks.md", MIN_TICKED + 2, "d-mid", None)
    low = TickedFile("10-projects/low/tasks.md", MIN_TICKED, "d-low", None)
    held = Held(
        frozenset({item_key("tasks_ticked", high.ref, high.digest)}),
        frozenset({("tasks_ticked", mid.ref)}),
        room=True,
    )
    reader = service(tmp_path)
    calls = spy_reads(reader, monkeypatch)

    result = read_changes(reader, WID, reader.scan(WID), TODAY, (high, mid, low), held)

    assert result.archive == {low.ref: "90-archive/low tasks done 2026.md"}
    assert calls == ["90-archive/low tasks done 2026.md"]


def test_links_spend_no_read_on_a_held_pair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A link pair whose key a change row holds spends no read on its target, and the
    search goes on to the next target the note mentions."""
    hub = "30-knowledge/Hub note gathers references.md"
    alpha, beta = (f"30-knowledge/Reference target {name}.md" for name in ("alpha", "beta"))
    write(tmp_path / hub, "Mentions Reference target alpha here and Reference target beta.\n")
    touch(tmp_path / hub, recent())
    for ref in (alpha, beta):
        write(tmp_path / ref, "x\n")
        touch(tmp_path / ref, old())
    held = Held(frozenset({item_key("note_link", hub, alpha)}), frozenset(), room=True)
    reader = service(tmp_path)
    calls = spy_reads(reader, monkeypatch)

    result = read_changes(reader, WID, reader.scan(WID), TODAY, (), held)

    assert [(pair.source.ref, pair.target.ref) for pair in result.links] == [(hub, beta)]
    assert calls == [hub, beta]


def test_superseded_spends_no_read_on_a_held_pair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An older note whose pair a change row holds (its key), or that an open row already
    marks replaced, spends no read; the next alike older note is taken instead."""
    newer, v2, v1 = (f"30-knowledge/Topic one standing v{n}.md" for n in (3, 2, 1))
    write(tmp_path / newer, "newer body\n")
    touch(tmp_path / newer, recent())
    for ref, when in ((v2, old()), (v1, datetime(2026, 8, 1, 10))):
        write(tmp_path / ref, "older body\n")
        touch(tmp_path / ref, when)
    reader = service(tmp_path)
    calls = spy_reads(reader, monkeypatch)
    scan = reader.scan(WID)

    held = Held(frozenset({item_key("note_superseded", v2, newer)}), frozenset(), room=True)
    result = read_changes(reader, WID, scan, TODAY, (), held)
    assert [(pair.older.ref, pair.newer.ref) for pair in result.superseded] == [(v1, newer)]
    assert calls == [newer, v1]

    calls.clear()
    held = replace(held, open_targets=frozenset({("note_superseded", v1)}))
    assert read_changes(reader, WID, scan, TODAY, (), held).superseded == ()
    assert calls == [newer]


def test_no_room_reads_the_changed_notes_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no room for a change (`Held.room` false, a day no change can be filed on), the
    read stops after the notes changed lately, which still feed the `owner` part: no archive,
    link or superseded read, each of those left empty."""
    for i, letter in enumerate(("alpha", "beta", "gamma")):
        write(
            tmp_path / "30-knowledge" / f"Topic {letter} standing v2.md",
            f"Mentions Reference target {letter} here.\n",
        )
        touch(tmp_path / "30-knowledge" / f"Topic {letter} standing v2.md", recent(hour=i + 1))
        for ref in (f"Topic {letter} standing v1.md", f"Reference target {letter}.md"):
            write(tmp_path / "30-knowledge" / ref, "x\n")
            touch(tmp_path / "30-knowledge" / ref, old())
    write(tmp_path / "90-archive" / "placeholder.md", "x\n")
    ticked = (TickedFile("10-projects/p1/tasks.md", MIN_TICKED, "d1", None),)
    reader = service(tmp_path)
    calls = spy_reads(reader, monkeypatch)

    held = Held(frozenset(), frozenset(), room=False)
    result = read_changes(reader, WID, reader.scan(WID), TODAY, ticked, held)

    newest = [
        f"30-knowledge/Topic {letter} standing v2.md" for letter in ("gamma", "beta", "alpha")
    ]
    assert calls == newest and [fact.ref for fact in result.changed] == newest
    assert (result.links, result.superseded, result.archive) == ((), (), {})


def test_each_skip_holds_improves_own_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`read_changes` keys what it skips exactly as `improve.py` keys the candidates it
    builds from the same changes: once the rows hold each built candidate's key, a second read
    skips the link, the superseded pair and the ticked file, and reads the changed note alone."""
    newer, older = "30-knowledge/Topic one standing v2.md", "30-knowledge/Topic one standing v1.md"
    write(tmp_path / newer, "Mentions Reference target alpha here.\n")
    touch(tmp_path / newer, recent())
    for ref in (older, "30-knowledge/Reference target alpha.md", "90-archive/placeholder.md"):
        write(tmp_path / ref, "x\n")
        touch(tmp_path / ref, old())
    ticked = (TickedFile("10-projects/p1/tasks.md", MIN_TICKED, "d1", None),)
    reader = service(tmp_path)
    found = read_changes(reader, WID, reader.scan(WID), TODAY, ticked)
    inbox = InboxFacts(count=0, oldest=None, checked=0, undated=0, partial=False)
    tasks = TaskFacts(0, 0, 0, (), 1, "k", False, ticked=ticked)
    obs = Observation(WID, "vault", VaultFacts(inbox, tasks), (), (), None, (), found)
    built = [*improve.note_link(obs), *improve.note_superseded(obs), *improve.tasks_ticked(obs)]
    assert [candidate.type for candidate in built] == list(CHANGES)

    held = Held(frozenset(candidate.item_key for candidate in built), frozenset(), room=True)
    calls = spy_reads(reader, monkeypatch)
    again = read_changes(reader, WID, reader.scan(WID), TODAY, ticked, held)
    assert (again.links, again.superseded, again.archive) == ((), (), {})
    assert calls == [newer]


# --- never raises --------------------------------------------------------------------------------


def test_read_changes_propagates_a_partial_scan_and_never_raises(tmp_path: Path) -> None:
    write(tmp_path / "30-knowledge" / "Fresh note today.md", "fresh\n")
    touch(tmp_path / "30-knowledge" / "Fresh note today.md", recent())
    reader = service(tmp_path)
    scan = reader.scan(WID)
    partial_scan = scan.__class__(
        scan.entries, True, ("deadline",), scan.scanned_bytes, scan.visited_items, scan.omitted
    )

    result = read_changes(reader, WID, partial_scan, TODAY, ())

    assert result.partial is True


def test_read_changes_drops_a_note_that_fails_to_read_and_marks_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(tmp_path / "30-knowledge" / "Fresh note today.md", "fresh\n")
    touch(tmp_path / "30-knowledge" / "Fresh note today.md", recent())
    reader = service(tmp_path)
    original = reader.read

    def always_fails(workspace_id: str, ref: str) -> Note:
        raise VaultError("read_failed")

    monkeypatch.setattr(reader, "read", always_fails)

    result = changes(reader)

    assert result.changed == ()
    assert result.partial is True
    monkeypatch.setattr(reader, "read", original)


# --- NoteFacts.tags carries inline tags too, so never-send still blocks a pair -----------------
#
# A candidate's evidence only ever carries a cut excerpt (a mention line of at most 300
# characters, or an opening of at most 600), while `tags` used to come from frontmatter alone --
# so a never-send tag written only in a note's body, outside whichever small window became its
# evidence, was never seen and the PAIRED block in `state.build_state` never fired.


def _link_candidate(pair: LinkPair) -> Candidate:
    """A note_link candidate built from one `LinkPair`: evidence (A, mention line, A tags),
    (B, B opening, B tags), `related_ref` A. It is built directly here, so this proves
    `build_state`'s own blocking rule against real `NoteFacts`, with no dependency on
    `improve.py`'s candidate builder."""
    a, b = pair.source, pair.target
    return Candidate(
        item_key="b" * 64,
        type="note_link",
        title="Notes to link",
        facts={"workspace_id": WID},
        words={},
        evidence=(
            Evidence(a.ref, pair.mention, False, a.tags),
            Evidence(b.ref, b.opening, False, b.tags),
        ),
        related_chat=None,
        related_ref=a.ref,
        deadline=None,
        shown_elsewhere=(),
        rules_evidence={"pairs": 1},
        origin_session=None,
        work_allowed=(),
    )


def _built(pair: LinkPair, *never_send: str) -> BuiltState:
    settings = DecisionSettings(never_send=never_send)
    now = recent().astimezone()
    return build_state(
        _link_candidate(pair), Owner(), Now(None, False, False, now, 0), History(), settings
    )


def test_an_inline_never_send_tag_on_as_own_first_line_blocks_the_link_pair(
    tmp_path: Path,
) -> None:
    """A's evidence is the line that mentions B; an inline "#private" sitting on a different
    line of A must still reach `NoteFacts.tags` and so still block the pair."""
    write(
        tmp_path / "30-knowledge" / "Lawyer call.md",
        "#private\n\nTalked through the plan; see also Second Brain MOC for the checklist.\n",
    )
    touch(tmp_path / "30-knowledge" / "Lawyer call.md", recent())
    write(tmp_path / "50-maps" / "Second Brain MOC.md", "# Second Brain MOC\nLinks.\n")
    touch(tmp_path / "50-maps" / "Second Brain MOC.md", old())

    result = changes(service(tmp_path))

    assert len(result.links) == 1
    built = _built(result.links[0], "#private")
    assert built.blocked is True
    # A's own ref is `related_ref`, so this trips `_note_blocked`'s upfront check, which reports
    # the whole pair -- both evidence entries -- as held back, not just the one that matched.
    assert built.held_back == 2


def test_an_inline_never_send_tag_past_bs_600_char_opening_blocks_the_link_pair(
    tmp_path: Path,
) -> None:
    """The same bug on the other side of the pair: B's own inline tag sits past the 600
    characters its `opening` excerpt carries, so only scanning B's whole already-read text
    catches it."""
    write(
        tmp_path / "30-knowledge" / "Deny rules hold even in bypass mode.md",
        "See also Bypass mode still logs every call for the mechanism.\n",
    )
    touch(tmp_path / "30-knowledge" / "Deny rules hold even in bypass mode.md", recent())
    write(
        tmp_path / "30-knowledge" / "Bypass mode still logs every call.md",
        "x" * 700 + "\n#private\n",
    )
    touch(tmp_path / "30-knowledge" / "Bypass mode still logs every call.md", old())

    result = changes(service(tmp_path))

    assert len(result.links) == 1
    pair = result.links[0]
    assert "#private" not in pair.target.opening  # the tag truly sits past the cut excerpt
    built = _built(pair, "#private")
    assert built.blocked is True
    assert built.held_back == 1


def test_tags_join_frontmatter_then_inline_each_once_with_no_extra_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(
        tmp_path / "30-knowledge" / "Readable facts note.md",
        "---\ntags: [Alpha]\n---\nSee #alpha and #private twice: #private again.\n",
    )
    touch(tmp_path / "30-knowledge" / "Readable facts note.md", recent())
    reader = service(tmp_path)
    calls = spy_reads(reader, monkeypatch)

    result = changes(reader)

    assert result.changed[0].tags == ("alpha", "private")  # frontmatter first, each tag once
    assert calls == ["30-knowledge/Readable facts note.md"]  # inline tags cost no extra read


def test_tags_stay_none_with_an_inline_tag_when_the_frontmatter_is_unknown(
    tmp_path: Path,
) -> None:
    write(
        tmp_path / "30-knowledge" / "Readable facts note.md",
        "---\ntags: [c++]\n---\n#private inline tag here.\n",
    )
    touch(tmp_path / "30-knowledge" / "Readable facts note.md", recent())

    result = changes(service(tmp_path))

    assert len(result.changed) == 1
    assert result.changed[0].tags is None


def test_tags_are_unknown_when_the_frontmatter_opener_has_a_trailing_space(
    tmp_path: Path,
) -> None:
    """A "--- " opener (trailing space) reads as no frontmatter to the display parser while the
    plain key reader still finds `tags: [private]` -- the same disagreement
    `automation.brief.tags_of` already treats as unknown; `_tags` must agree, not answer `()`
    and let a never-send tag written this way go unseen."""
    write(
        tmp_path / "30-knowledge" / "Readable facts note.md",
        "--- \ntags: [private]\n---\nSome content here.\n",
    )
    touch(tmp_path / "30-knowledge" / "Readable facts note.md", recent())

    result = changes(service(tmp_path))

    assert len(result.changed) == 1
    assert result.changed[0].tags is None
