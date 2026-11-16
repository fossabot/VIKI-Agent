"""`notes_changed` over a synthetic vault in `tmp_path`, read through the real bounded
`VaultService` (the `test_brief` fixtures), with each note's file time set by `os.utime`, so the
check is proved against the same zero-content listing the scheduler reads.
"""

from __future__ import annotations

import dataclasses
import os
import unicodedata
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tests.automation.test_brief import WID, service, spy_reads, write
from viki_agent.automation.folder_changes import notes_changed
from viki_agent.vaults.vault import ScanResult, VaultError

SINCE = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)


def note(root: Path, ref: str, at: datetime) -> None:
    path = root / ref
    write(path, "# A note\n")
    os.utime(path, (at.timestamp(), at.timestamp()))


def changed(root: Path, folder: str = "00-inbox") -> bool | None:
    return notes_changed(service(root), WID, folder, SINCE)


def test_a_note_edited_after_the_moment_is_a_change(tmp_path: Path) -> None:
    note(tmp_path, "00-inbox/old.md", SINCE - timedelta(days=1))
    note(tmp_path, "00-inbox/new.md", SINCE + timedelta(seconds=1))
    assert changed(tmp_path) is True


def test_notes_no_later_than_the_moment_are_no_change(tmp_path: Path) -> None:
    note(tmp_path, "00-inbox/a.md", SINCE - timedelta(days=1))
    note(tmp_path, "00-inbox/b.md", SINCE)
    assert changed(tmp_path) is False


def test_a_folder_without_notes_is_no_change(tmp_path: Path) -> None:
    (tmp_path / "00-inbox").mkdir()
    (tmp_path / "00-inbox" / "picture.png").write_bytes(b"\x89PNG")
    note(tmp_path, "Home.md", SINCE + timedelta(hours=1))
    assert changed(tmp_path) is False


def test_notes_at_any_depth_count_and_only_under_the_folder(tmp_path: Path) -> None:
    note(tmp_path, "10-projects/site/tasks.md", SINCE + timedelta(minutes=1))
    note(tmp_path, "10-projects/site-old/plan.md", SINCE + timedelta(minutes=1))
    note(tmp_path, "10-projects/site2.md", SINCE + timedelta(minutes=1))
    assert changed(tmp_path, "10-projects/site") is True
    assert changed(tmp_path, "10-projects/site/deeper") is False
    assert changed(tmp_path, "10-project") is False
    assert changed(tmp_path, "10-projects") is True


def test_another_letter_case_or_unicode_form_still_finds_the_folder(tmp_path: Path) -> None:
    decomposed = unicodedata.normalize("NFD", "Café")
    note(tmp_path, f"{decomposed}/a.md", SINCE + timedelta(minutes=1))
    note(tmp_path, "00-Inbox/a.md", SINCE + timedelta(minutes=1))
    assert changed(tmp_path, "café") is True
    assert changed(tmp_path, "00-inbox") is True


def test_the_check_reads_no_note(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    note(tmp_path, "00-inbox/a.md", SINCE + timedelta(minutes=1))
    reader = service(tmp_path)
    reads = spy_reads(reader, monkeypatch)
    assert notes_changed(reader, WID, "00-inbox", SINCE) is True
    assert reads == []


def test_a_partial_listing_is_unknown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    note(tmp_path, "00-inbox/a.md", SINCE - timedelta(days=1))
    reader = service(tmp_path)
    original = reader.scan
    monkeypatch.setattr(
        reader, "scan", lambda wid: dataclasses.replace(original(wid), partial=True)
    )
    assert notes_changed(reader, WID, "00-inbox", SINCE) is None


def test_a_listing_error_is_unknown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reader = service(tmp_path)

    def failing(workspace_id: str) -> ScanResult:
        raise VaultError("root_unavailable")

    monkeypatch.setattr(reader, "scan", failing)
    assert notes_changed(reader, WID, "00-inbox", SINCE) is None


def test_a_removed_note_is_no_change(tmp_path: Path) -> None:
    note(tmp_path, "00-inbox/kept.md", SINCE - timedelta(days=1))
    note(tmp_path, "00-inbox/filed.md", SINCE - timedelta(days=1))
    (tmp_path / "00-inbox" / "filed.md").unlink()
    assert changed(tmp_path) is False


def test_a_note_moved_in_keeps_its_older_file_time(tmp_path: Path) -> None:
    note(tmp_path, "elsewhere/moved.md", SINCE - timedelta(days=1))
    (tmp_path / "00-inbox").mkdir()
    (tmp_path / "elsewhere" / "moved.md").rename(tmp_path / "00-inbox" / "moved.md")
    assert changed(tmp_path) is False
