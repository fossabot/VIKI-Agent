"""`card_waiting` over a synthetic vault in `tmp_path`, read through the real bounded
`VaultService` (the `test_brief` fixtures), so the Kanban contract is checked against the same
metadata reader the app builds.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tests.automation.test_brief import WID, service, spy_reads, write
from viki_agent.automation import kanban
from viki_agent.automation.kanban import MAX_KANBAN_NOTES, card_waiting, read_kanban
from viki_agent.vaults.vault import MetadataBatch, ScanResult, VaultError, VaultService

SEAT = "research"


def card(status: str = "todo", tags: str = "[owner/research]") -> str:
    return f"---\nstatus: {status}\ntags: {tags}\n---\n\n# A card\n"


def waiting(tmp_path: Path) -> bool | None:
    return card_waiting(service(tmp_path), WID, SEAT)


def spy_metadata(reader: VaultService, monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """The refs of every `reader.metadata` call, in call order."""
    calls: list[list[str]] = []
    original = reader.metadata

    def counting(workspace_id: str, refs: list[str]) -> MetadataBatch:
        calls.append(list(refs))
        return original(workspace_id, refs)

    monkeypatch.setattr(reader, "metadata", counting)
    return calls


def test_the_budgets_are_the_values_the_plan_names() -> None:
    assert (MAX_KANBAN_NOTES, kanban._BATCH, kanban.KANBAN_FOLDER) == (128, 32, "kanban")


@pytest.mark.parametrize(
    ("ref", "is_card"),
    [
        ("kanban/x.md", True),
        ("cards/x.md", False),
        ("Kanban/x.md", False),
        ("kanban/sub/x.md", False),
    ],
)
def test_only_a_note_directly_in_kanban_is_a_card(tmp_path: Path, ref: str, is_card: bool) -> None:
    write(tmp_path / ref, card())
    reader = service(tmp_path)
    assert card_waiting(reader, WID, SEAT) is is_card
    cards = read_kanban(reader, WID, reader.scan(WID)).cards
    assert [one.ref for one in cards] == ([ref] if is_card else [])


def test_a_todo_card_for_the_seat_is_waiting(tmp_path: Path) -> None:
    write(tmp_path / "kanban" / "a.md", card())
    assert waiting(tmp_path) is True


def test_block_list_tags_count(tmp_path: Path) -> None:
    text = "---\nstatus: todo\ntags:\n  - other\n  - owner/research\n---\n"
    write(tmp_path / "kanban" / "a.md", text)
    assert waiting(tmp_path) is True


def test_a_match_after_other_notes_is_found(tmp_path: Path) -> None:
    write(tmp_path / "kanban" / "a.md", card("working"))
    write(tmp_path / "kanban" / "b.md", card())
    assert waiting(tmp_path) is True


@pytest.mark.parametrize(
    ("folder", "text"),
    [
        ("kanban", card("Todo")),
        ("kanban", card(tags='["#owner/research"]')),
        ("kanban", card(tags="[Owner/research]")),
        ("Kanban", card()),
        ("kanban", card("working")),
        ("kanban", card(tags="[owner/writer]")),
        ("kanban", card(tags="[owner/research-lead]")),
        ("kanban", card(tags="[owner/researcher]")),
        ("kanban", "---\nstatus: todo\n---\n"),
        ("kanban", "# No frontmatter\n"),
        ("kanban/sub", card()),
        ("other", card()),
    ],
)
def test_a_note_that_is_not_the_seats_todo_card_is_not_waiting(
    tmp_path: Path, folder: str, text: str
) -> None:
    write(tmp_path / folder / "x.md", text)
    assert waiting(tmp_path) is False


def test_no_kanban_folder_is_not_waiting(tmp_path: Path) -> None:
    write(tmp_path / "notes" / "a.md", "# A\n")
    assert waiting(tmp_path) is False


def test_an_unreadable_frontmatter_leaves_it_unknown(tmp_path: Path) -> None:
    write(
        tmp_path / "kanban" / "a.md",
        "---\nstatus: todo\nstatus: todo\ntags: [owner/research]\n---\n",
    )
    assert waiting(tmp_path) is None


def test_a_match_beside_an_unreadable_note_is_waiting(tmp_path: Path) -> None:
    write(tmp_path / "kanban" / "a.md", "---\nstatus: todo\nstatus: todo\n---\n")
    write(tmp_path / "kanban" / "b.md", card())
    assert waiting(tmp_path) is True


def test_a_partial_scan_is_unknown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write(tmp_path / "kanban" / "a.md", card())
    reader = service(tmp_path)
    original = reader.scan
    monkeypatch.setattr(
        reader, "scan", lambda wid: dataclasses.replace(original(wid), partial=True)
    )
    assert card_waiting(reader, WID, SEAT) is None


def test_a_scan_error_is_unknown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reader = service(tmp_path)

    def failing(workspace_id: str) -> ScanResult:
        raise VaultError("root_unavailable")

    monkeypatch.setattr(reader, "scan", failing)
    assert card_waiting(reader, WID, SEAT) is None


def test_a_metadata_error_is_unknown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write(tmp_path / "kanban" / "a.md", card())
    reader = service(tmp_path)

    def failing(workspace_id: str, refs: list[str]) -> MetadataBatch:
        raise VaultError("root_changed")

    monkeypatch.setattr(reader, "metadata", failing)
    assert card_waiting(reader, WID, SEAT) is None


def test_more_than_the_cap_is_unknown_after_four_bounded_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for number in range(MAX_KANBAN_NOTES + 2):
        write(tmp_path / "kanban" / f"n{number:03}.md", card("working"))
    reader = service(tmp_path)
    metadata_calls = spy_metadata(reader, monkeypatch)
    reads = spy_reads(reader, monkeypatch)
    assert card_waiting(reader, WID, SEAT) is None
    assert [len(refs) for refs in metadata_calls] == [32, 32, 32, 32]
    assert reads == []


def test_a_small_kanban_is_one_call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for number in range(31):
        write(tmp_path / "kanban" / f"n{number:03}.md", card("working"))
    reader = service(tmp_path)
    metadata_calls = spy_metadata(reader, monkeypatch)
    reads = spy_reads(reader, monkeypatch)
    assert card_waiting(reader, WID, SEAT) is False
    assert [len(refs) for refs in metadata_calls] == [31]
    assert reads == []


def test_read_kanban_reads_every_batch_in_scan_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for number in range(MAX_KANBAN_NOTES + 2):
        write(tmp_path / "kanban" / f"n{number:03}.md", card("working"))
    reader = service(tmp_path)
    metadata_calls = spy_metadata(reader, monkeypatch)
    reads = spy_reads(reader, monkeypatch)
    result = read_kanban(reader, WID, reader.scan(WID))
    assert [len(refs) for refs in metadata_calls] == [32, 32, 32, 32]
    assert [one.ref for one in result.cards] == [f"kanban/n{n:03}.md" for n in range(128)]
    assert result.unread == ("kanban/n128.md", "kanban/n129.md")
    assert result.complete is True
    assert reads == []


def test_read_kanban_keeps_title_status_tags_and_change_time(tmp_path: Path) -> None:
    write(tmp_path / "kanban" / "a.md", "---\ntitle: Real title\nstatus: todo\n---\n")
    write(tmp_path / "kanban" / "plain-note.md", "---\nstatus: blocked\ntags: [x, owner/a]\n---\n")
    write(tmp_path / "kanban" / "README.md", "---\nstatus: done\n---\n")
    write(tmp_path / "kanban" / "bare.md", "no front matter\n")
    reader = service(tmp_path)
    scan = reader.scan(WID)
    result = read_kanban(reader, WID, scan)
    by_ref = {one.ref: one for one in result.cards}
    assert by_ref["kanban/a.md"].title == "Real title"
    assert by_ref["kanban/plain-note.md"].title == "plain-note"
    assert by_ref["kanban/README.md"].title == "kanban / README.md"
    assert (by_ref["kanban/plain-note.md"].status, by_ref["kanban/plain-note.md"].tags) == (
        "blocked",
        ("x", "owner/a"),
    )
    assert (by_ref["kanban/bare.md"].status, by_ref["kanban/bare.md"].tags) == (None, ())
    entry = next(one for one in scan.entries if one.ref == "kanban/a.md")
    expected = datetime.fromtimestamp(entry.modified, UTC)
    assert by_ref["kanban/a.md"].changed == expected
    assert result.unread == ()
    assert result.complete is True


def test_read_kanban_lists_what_it_could_not_read(tmp_path: Path) -> None:
    write(tmp_path / "kanban" / "a.md", "---\nstatus: todo\nstatus: todo\n---\n")
    write(tmp_path / "kanban" / "b.md", card())
    reader = service(tmp_path)
    result = read_kanban(reader, WID, reader.scan(WID))
    assert [one.ref for one in result.cards] == ["kanban/b.md"]
    assert result.unread == ("kanban/a.md",)
    assert result.complete is True


def test_read_kanban_a_failed_batch_leaves_the_rest_unread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for number in range(40):
        write(tmp_path / "kanban" / f"n{number:03}.md", card("working"))
    reader = service(tmp_path)
    original = reader.metadata
    calls: list[int] = []

    def second_fails(workspace_id: str, refs: list[str]) -> MetadataBatch:
        calls.append(len(refs))
        if len(calls) == 2:
            raise VaultError("root_changed")
        return original(workspace_id, refs)

    monkeypatch.setattr(reader, "metadata", second_fails)
    result = read_kanban(reader, WID, reader.scan(WID))
    assert len(result.cards) == 32
    assert result.unread == tuple(f"kanban/n{n:03}.md" for n in range(32, 40))


def test_read_kanban_a_partial_scan_is_incomplete(tmp_path: Path) -> None:
    write(tmp_path / "kanban" / "a.md", card())
    reader = service(tmp_path)
    scan = dataclasses.replace(reader.scan(WID), partial=True)
    result = read_kanban(reader, WID, scan)
    assert result.complete is False
    assert [one.ref for one in result.cards] == ["kanban/a.md"]


def test_read_kanban_takes_its_read_time_and_keeps_a_file_time_ahead_of_it(tmp_path: Path) -> None:
    """The read time is this computer's clock as the read began; a card whose file time is
    ahead of it is still read, with that time, for the seat planner to compare."""
    write(tmp_path / "kanban" / "a.md", card())
    reader = service(tmp_path)
    scan = reader.scan(WID)
    ahead = (datetime.now(UTC) + timedelta(days=1)).timestamp()
    entries = tuple(dataclasses.replace(one, modified=ahead) for one in scan.entries)
    before = datetime.now(UTC)
    result = read_kanban(reader, WID, dataclasses.replace(scan, entries=entries))
    after = datetime.now(UTC)
    assert result.read_at is not None and before <= result.read_at <= after
    assert result.read_at.utcoffset() == timedelta(0)
    (only,) = result.cards
    assert only.changed > result.read_at and result.unread == ()


def test_read_kanban_an_unholdable_change_time_is_unread(tmp_path: Path) -> None:
    write(tmp_path / "kanban" / "a.md", card())
    reader = service(tmp_path)
    scan = reader.scan(WID)
    entries = tuple(dataclasses.replace(one, modified=1e20) for one in scan.entries)
    result = read_kanban(reader, WID, dataclasses.replace(scan, entries=entries))
    assert result.cards == ()
    assert result.unread == ("kanban/a.md",)


def test_a_first_batch_match_still_makes_one_metadata_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(tmp_path / "kanban" / "n000.md", card())
    for number in range(1, 64):
        write(tmp_path / "kanban" / f"n{number:03}.md", card("working"))
    reader = service(tmp_path)
    metadata_calls = spy_metadata(reader, monkeypatch)
    assert card_waiting(reader, WID, SEAT) is True
    assert [len(refs) for refs in metadata_calls] == [32]
