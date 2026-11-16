"""VaultFacts for the JEV brief: a synthetic vault in `tmp_path`, read through a real
`VaultService` over a fake workspace (a `SimpleNamespace` standing in for
`WorkspaceService`), so these tests exercise the same bounded reader the app builds, never a
stand-in for it.
"""

from __future__ import annotations

import dataclasses
import hashlib
import os
import random
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from viki_agent.automation.brief import (
    INBOX_EXCERPT_CHARS,
    MAX_GOAL_READS,
    MAX_GOALS,
    MAX_INBOX_ITEMS,
    MAX_INBOX_NOTE_BYTES,
    MAX_INBOX_READS,
    MAX_TASK_FILES,
    MAX_TASK_ITEMS,
    SOON_DAYS,
    TEXT_MAX,
    InboxFacts,
    TaskFacts,
    TaskItem,
    TickedFile,
    VaultFacts,
    captured_at,
    parse_tasks,
    parse_ticked,
    read_goals,
    read_vault_facts,
)
from viki_agent.decision_loop.contracts import DecisionSettings
from viki_agent.decision_loop.detectors import Observation, detect
from viki_agent.decision_loop.state import BuiltState, History, Now, Owner, build_state
from viki_agent.domain import CanonicalRoot
from viki_agent.vaults.vault import Note, ScanResult, VaultError, VaultService
from viki_agent.workspaces import WorkspaceService, root_key

WID = "ws-1"
TODAY = date(2026, 10, 9)


def write(path: Path, text: str) -> None:
    """A UTF-8 text file with forced `\\n` line endings, so no platform's newline translation
    can shift a byte count or a line number a test depends on."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


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


# --- the budgets ---------------------------------------------------------------------------


def test_the_budgets_are_the_values_the_plan_names() -> None:
    assert (
        MAX_INBOX_READS,
        MAX_INBOX_NOTE_BYTES,
        MAX_TASK_FILES,
        MAX_TASK_ITEMS,
        SOON_DAYS,
        TEXT_MAX,
    ) == (20, 65536, 32, 3, 7, 160)


def test_the_new_budgets_are_the_values_the_plan_names() -> None:
    assert (MAX_INBOX_ITEMS, INBOX_EXCERPT_CHARS, MAX_GOAL_READS, MAX_GOALS) == (3, 2000, 16, 8)


# --- captured_at ---------------------------------------------------------------------------


def test_captured_at_unquotes_and_strips_then_requires_an_offset() -> None:
    aware = datetime(2026, 10, 9, tzinfo=UTC)
    assert captured_at("2026-10-09T00:00:00+00:00") == aware
    assert captured_at('  "2026-10-09T00:00:00+00:00"  ') == aware
    assert captured_at("'2026-10-09T00:00:00+00:00'") == aware
    assert captured_at("2026-10-09T09:00:00+09:00") == aware  # a non-UTC offset is still trusted
    assert captured_at("2026-10-09T00:00:00") is None  # naive: no offset, fail closed
    assert captured_at("not a date") is None
    assert captured_at("") is None
    assert captured_at(None) is None


def test_captured_at_returns_none_when_the_utc_conversion_would_overflow() -> None:
    # Local year-1 midnight, +01:00: the UTC instant is in year 0 — out of datetime's range.
    assert captured_at("0001-01-01T00:00:00+01:00") is None
    # Local year-9999 end of day, -01:00: the UTC instant is in year 10000 — also out of range.
    assert captured_at("9999-12-31T23:59:59-01:00") is None


# --- read_vault_facts: the one scan, and its failure ----------------------------------------


def test_a_scan_failure_gives_empty_all_partial_facts_and_never_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reader = service(tmp_path)

    def _raise(workspace_id: str) -> ScanResult:
        raise VaultError("root_unavailable")

    monkeypatch.setattr(reader, "scan", _raise)
    facts = read_vault_facts(reader, WID, TODAY)
    assert facts == VaultFacts(
        inbox=InboxFacts(count=0, oldest=None, checked=0, undated=0, partial=True),
        tasks=TaskFacts(
            overdue=0,
            today=0,
            soon=0,
            items=(),
            files=0,
            files_key=hashlib.sha256(b"").hexdigest(),
            partial=True,
        ),
    )


def test_a_partial_scan_marks_both_inbox_and_tasks_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    write(vault / "00-inbox" / "a.md", "---\ncaptured: 2026-10-09T00:00:00+00:00\n---\nbody\n")
    reader = service(vault)
    original_scan = reader.scan

    def forced_partial(workspace_id: str) -> ScanResult:
        return dataclasses.replace(original_scan(workspace_id), partial=True)

    monkeypatch.setattr(reader, "scan", forced_partial)
    facts = read_vault_facts(reader, WID, TODAY)
    assert facts.inbox.partial is True
    assert facts.tasks.partial is True


def test_an_empty_vault_gives_zeroed_unpartial_facts(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    facts = read_vault_facts(service(vault), WID, TODAY)
    assert facts == VaultFacts(
        inbox=InboxFacts(count=0, oldest=None, checked=0, undated=0, partial=False),
        tasks=TaskFacts(
            overdue=0,
            today=0,
            soon=0,
            items=(),
            files=0,
            files_key=hashlib.sha256(b"").hexdigest(),
            partial=False,
        ),
    )


# --- inbox -----------------------------------------------------------------------------------


def test_inbox_notes_at_any_depth_resolve_and_oldest_is_the_minimum(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    write(vault / "00-inbox" / "a.md", "---\ncaptured: 2026-10-05T09:00:00+09:00\n---\nbody\n")
    write(vault / "00-inbox" / "b.md", "---\ncaptured: 2026-10-01T00:00:00Z\n---\nbody\n")
    write(vault / "00-inbox" / "c.md", '---\ncaptured: "2026-10-03T00:00:00+00:00"\n---\nbody\n')
    write(
        vault / "00-inbox" / "sub" / "deep" / "d.md",
        "---\ncaptured: 2026-10-04T00:00:00+00:00\n---\nbody\n",
    )
    facts = read_vault_facts(service(vault), WID, TODAY).inbox
    assert (facts.count, facts.checked, facts.undated, facts.partial) == (4, 4, 0, False)
    # a's 09:00+09:00 is 00:00Z the same day as d: b (2026-10-01Z) is still the earliest.
    assert facts.oldest == "2026-10-01T00:00:00.000Z"


def test_a_naive_or_missing_capture_is_undated_and_not_oldest(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    write(vault / "00-inbox" / "naive.md", "---\ncaptured: 2026-10-02T00:00:00\n---\nbody\n")
    write(vault / "00-inbox" / "missing.md", "no frontmatter at all\n")
    facts = read_vault_facts(service(vault), WID, TODAY).inbox
    assert (facts.count, facts.checked, facts.undated, facts.oldest, facts.partial) == (
        2,
        2,
        2,
        None,
        False,
    )


def test_an_oversized_inbox_note_is_skipped_without_a_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    write(vault / "00-inbox" / "normal.md", "---\ncaptured: 2026-10-01T00:00:00Z\n---\nbody\n")
    big_body = "a" * (MAX_INBOX_NOTE_BYTES + 100)
    write(vault / "00-inbox" / "huge.md", big_body)
    reader = service(vault)
    calls = spy_reads(reader, monkeypatch)
    facts = read_vault_facts(reader, WID, TODAY).inbox
    assert calls == ["00-inbox/normal.md"]  # the oversized note is never opened
    assert (facts.count, facts.checked, facts.undated, facts.partial) == (2, 2, 1, False)
    assert facts.oldest == "2026-10-01T00:00:00.000Z"


def test_a_failed_inbox_read_sets_partial_but_is_not_counted_as_undated(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    write(vault / "00-inbox" / "normal.md", "---\ncaptured: 2026-10-01T00:00:00Z\n---\nbody\n")
    (vault / "00-inbox" / "broken.md").write_bytes(b"\xff\xfe not utf-8 at all")
    facts = read_vault_facts(service(vault), WID, TODAY).inbox
    assert (facts.count, facts.checked, facts.undated, facts.partial) == (2, 2, 0, True)
    assert facts.oldest == "2026-10-01T00:00:00.000Z"


def test_an_overflowing_captured_value_is_undated_and_read_vault_facts_does_not_raise(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    write(
        vault / "00-inbox" / "extreme.md",
        "---\ncaptured: 0001-01-01T00:00:00+01:00\n---\nbody\n",
    )
    facts = read_vault_facts(service(vault), WID, TODAY).inbox
    assert (facts.count, facts.checked, facts.undated, facts.oldest, facts.partial) == (
        1,
        1,
        1,
        None,
        False,
    )


def test_only_the_first_20_inbox_notes_are_read_and_more_marks_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    for i in range(MAX_INBOX_READS + 1):
        write(
            vault / "00-inbox" / f"note-{i:02d}.md",
            f"---\ncaptured: 2026-10-{i + 1:02d}T00:00:00+00:00\n---\nbody\n",
        )
    reader = service(vault)
    calls = spy_reads(reader, monkeypatch)
    facts = read_vault_facts(reader, WID, TODAY).inbox
    assert facts.count == MAX_INBOX_READS + 1
    assert facts.checked == MAX_INBOX_READS
    assert facts.partial is True
    assert len(calls) == MAX_INBOX_READS
    # note-20 (the 21st, alphabetically last) falls outside the first 20 and is never read.
    assert "00-inbox/note-20.md" not in calls


# --- inbox items (oldest dated captures) ------------------------------------------------------


def test_inbox_items_are_the_oldest_three_dated_captures_with_title_excerpt_and_tags(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    write(
        vault / "00-inbox" / "a.md",
        '---\ncaptured: 2026-10-05T00:00:00+00:00\ntags: ["Work", "#idea"]\n---\n'
        "# A Title\nBody of a.\n",
    )
    write(
        vault / "00-inbox" / "b.md",
        "---\ncaptured: 2026-10-01T00:00:00+00:00\n---\n# B Title\nBody of b.\n",
    )
    write(
        vault / "00-inbox" / "c.md",
        "---\ncaptured: 2026-10-03T00:00:00+00:00\n---\n# C Title\nBody of c.\n",
    )
    write(
        vault / "00-inbox" / "d.md",
        "---\ncaptured: 2026-10-09T00:00:00+00:00\n---\n# D Title (too new)\nBody of d.\n",
    )
    facts = read_vault_facts(service(vault), WID, TODAY).inbox
    assert [item.ref for item in facts.items] == [
        "00-inbox/b.md",
        "00-inbox/c.md",
        "00-inbox/a.md",
    ]
    b = facts.items[0]
    assert b.title == "B Title"
    assert b.captured == "2026-10-01T00:00:00.000Z"
    assert b.excerpt == "# B Title\nBody of b.\n"
    assert b.tags == ()  # no tags key at all: no tags, not unknown
    a = facts.items[2]
    assert a.title == "A Title"
    assert a.tags == ("work", "idea")  # lower-cased, leading "#" stripped


def test_inbox_item_tags_are_unknown_only_when_the_frontmatter_cannot_be_read(
    tmp_path: Path,
) -> None:
    # A capture is schema-exempt: no `tags` key reads as no tags, like `tags: []`. Only a
    # frontmatter the display parser refuses (`c++` is no plain tag) leaves them unknown.
    vault = tmp_path / "vault"
    write(vault / "00-inbox" / "none.md", "---\ncaptured: 2026-10-01T00:00:00+00:00\n---\nbody\n")
    write(
        vault / "00-inbox" / "empty.md",
        "---\ncaptured: 2026-10-02T00:00:00+00:00\ntags: []\n---\nbody\n",
    )
    write(
        vault / "00-inbox" / "odd.md",
        "---\ncaptured: 2026-10-03T00:00:00+00:00\ntags: [c++]\n---\nbody\n",
    )
    facts = read_vault_facts(service(vault), WID, TODAY).inbox
    assert {item.ref: item.tags for item in facts.items} == {
        "00-inbox/none.md": (),
        "00-inbox/empty.md": (),
        "00-inbox/odd.md": None,
    }


def test_tags_come_from_the_display_parser_past_a_block_list(tmp_path: Path) -> None:
    # The plain key reader stops at the first `  - ` line, before `tags`; the display parser
    # reads the whole block, so these tags are known, never taken for "no tags key".
    vault = tmp_path / "vault"
    write(
        vault / "00-inbox" / "a.md",
        "---\ncaptured: 2026-10-01T00:00:00+00:00\naliases:\n  - other\ntags: [Private]\n---\n"
        "body\n",
    )
    facts = read_vault_facts(service(vault), WID, TODAY).inbox
    assert [item.tags for item in facts.items] == [("private",)]


def test_tags_are_unknown_where_only_the_plain_key_reader_finds_a_frontmatter(
    tmp_path: Path,
) -> None:
    # "--- " (a trailing space) opens no frontmatter for the display parser, so it reads no
    # tags, while the plain key reader still finds `tags: [private]`: the two disagree.
    vault = tmp_path / "vault"
    write(
        vault / "00-inbox" / "a.md",
        "--- \ncaptured: 2026-10-01T00:00:00+00:00\ntags: [private]\n---\nbody\n",
    )
    facts = read_vault_facts(service(vault), WID, TODAY).inbox
    assert [item.tags for item in facts.items] == [None]


def test_inbox_item_excerpt_is_the_body_cut_to_2000_chars_with_no_ellipsis(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    long_body = "x" * 2500
    write(
        vault / "00-inbox" / "long.md",
        f"---\ncaptured: 2026-10-01T00:00:00+00:00\n---\n{long_body}",
    )
    facts = read_vault_facts(service(vault), WID, TODAY).inbox
    assert len(facts.items[0].excerpt) == INBOX_EXCERPT_CHARS
    assert facts.items[0].excerpt == "x" * INBOX_EXCERPT_CHARS


def test_inbox_items_exclude_undated_oversized_and_unreadable_notes(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    write(vault / "00-inbox" / "dated.md", "---\ncaptured: 2026-10-01T00:00:00+00:00\n---\nbody\n")
    write(vault / "00-inbox" / "undated.md", "no frontmatter\n")
    write(vault / "00-inbox" / "huge.md", "a" * (MAX_INBOX_NOTE_BYTES + 100))
    (vault / "00-inbox" / "broken.md").write_bytes(b"\xff\xfe not utf-8")
    facts = read_vault_facts(service(vault), WID, TODAY).inbox
    assert [item.ref for item in facts.items] == ["00-inbox/dated.md"]


def test_inbox_items_only_come_from_the_first_20_notes_read(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    for i in range(MAX_INBOX_READS):
        write(
            vault / "00-inbox" / f"note-{i:02d}.md",
            f"---\ncaptured: 2026-10-{i + 1:02d}T00:00:00+00:00\n---\nbody\n",
        )
    # Alphabetically last (so outside the first MAX_INBOX_READS), but the earliest capture.
    write(
        vault / "00-inbox" / "zz-oldest.md",
        "---\ncaptured: 2020-01-01T00:00:00+00:00\n---\nbody\n",
    )
    facts = read_vault_facts(service(vault), WID, TODAY).inbox
    assert facts.partial is True
    assert all(item.ref != "00-inbox/zz-oldest.md" for item in facts.items)


def test_inbox_items_add_no_new_reads_beyond_the_existing_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    for i in range(5):
        write(
            vault / "00-inbox" / f"note-{i}.md",
            f"---\ncaptured: 2026-10-{i + 1:02d}T00:00:00+00:00\n---\nbody {i}\n",
        )
    reader = service(vault)
    calls = spy_reads(reader, monkeypatch)
    facts = read_vault_facts(reader, WID, TODAY).inbox
    assert len(facts.items) == MAX_INBOX_ITEMS
    assert len(calls) == 5  # one read per inbox note; items reuse them, no extra reads


# --- parse_tasks -------------------------------------------------------------------------------


def test_open_dated_lines_outside_fences_count() -> None:
    text = (
        "- [ ] outside the fence 📅 2026-10-09\n\n```\n- [ ] inside the fence 📅 2026-10-09\n```\n"
    )
    assert parse_tasks(text, TODAY) == [(1, "outside the fence", TODAY)]


def test_parse_tasks_ignores_checked_boxes_and_lines_without_a_date_token() -> None:
    text = (
        "- [x] already done 📅 2026-10-09\n"
        "- [ ] no date here at all\n"
        "+ [ ] plus marker 📅 2026-10-10\n"
        "* [ ] star marker 📅 2026-10-11\n"
    )
    assert parse_tasks(text, TODAY) == [
        (3, "plus marker", date(2026, 10, 10)),
        (4, "star marker", date(2026, 10, 11)),
    ]


def test_parse_tasks_skips_a_line_with_an_invalid_calendar_date() -> None:
    text = "- [ ] bad date 📅 2026-13-40\n- [ ] good date 📅 2026-10-09\n"
    assert parse_tasks(text, TODAY) == [(2, "good date", TODAY)]


def test_parse_tasks_keeps_only_due_within_the_soon_window() -> None:
    horizon = TODAY + timedelta(days=SOON_DAYS)
    text = (
        f"- [ ] long overdue 📅 {TODAY - timedelta(days=300)}\n"
        f"- [ ] right at the edge 📅 {horizon}\n"
        f"- [ ] one day too far 📅 {horizon + timedelta(days=1)}\n"
    )
    assert parse_tasks(text, TODAY) == [
        (1, "long overdue", TODAY - timedelta(days=300)),
        (2, "right at the edge", horizon),
    ]


def test_display_text_strips_formatting_and_the_date_token() -> None:
    text = "- [ ] Review `config.py` and **[[Project Plan]]** before launch 📅 2026-10-09 please\n"
    assert parse_tasks(text, TODAY) == [
        (1, "Review config.py and Project Plan before launch please", TODAY)
    ]


def test_display_text_is_cut_to_text_max_with_an_ellipsis() -> None:
    long_text = "word " * 50  # far longer than TEXT_MAX once collapsed
    text = f"- [ ] {long_text}📅 2026-10-09\n"
    _, display, _ = parse_tasks(text, TODAY)[0]
    assert len(display) == TEXT_MAX
    assert display == " ".join(long_text.split())[: TEXT_MAX - 1] + "…"


def test_a_template_placeholder_line_gives_no_item() -> None:
    # The vault's task template copies forward as "- [ ] <next action> 📅 2026-01-01"; a project
    # that never filled it in must not show a fake overdue task.
    text = "- [ ] <next action> 📅 2026-01-01\n"
    assert parse_tasks(text, TODAY) == []


def test_a_placeholder_like_token_with_surrounding_text_is_kept() -> None:
    text = "- [ ] Call <Ann> back 📅 2026-10-09\n"
    assert parse_tasks(text, TODAY) == [(1, "Call <Ann> back", TODAY)]


def test_a_task_inside_a_multiline_html_comment_is_ignored() -> None:
    text = "<!--\n- [ ] commented out 📅 2026-10-09\n-->\n- [ ] real task 📅 2026-10-09\n"
    assert parse_tasks(text, TODAY) == [(4, "real task", TODAY)]


def test_a_tilde_fence_task_is_ignored() -> None:
    text = "~~~\n- [ ] inside tilde fence 📅 2026-10-09\n~~~\n"
    assert parse_tasks(text, TODAY) == []


def test_a_comment_opener_inside_a_fence_does_not_swallow_the_fences_close() -> None:
    # CommonMark reads a fence's contents as code: an unclosed "<!--" quoted inside one is not a
    # real comment opener, so it must not swallow the fence's closing line (and hide every task
    # after it, as it would if the comment rule fired inside the fence too).
    text = "- [ ] before 📅 2026-10-01\n```html\n<!--\n```\n- [ ] after the fence 📅 2026-10-01\n"
    assert parse_tasks(text, TODAY) == [
        (1, "before", date(2026, 10, 1)),
        (5, "after the fence", date(2026, 10, 1)),
    ]


# --- parse_ticked -----------------------------------------------------------------------------


def test_parse_ticked_counts_lower_and_upper_x_outside_fences() -> None:
    text = "- [x] done one\n- [X] done two\n- [ ] still open\n```\n- [x] fenced, not counted\n```\n"
    assert parse_ticked(text) == ["- [x] done one", "- [X] done two"]


def test_parse_ticked_matches_plus_and_star_markers() -> None:
    text = "+ [x] plus marker\n* [X] star marker\n- [ ] not ticked\n"
    assert parse_ticked(text) == ["+ [x] plus marker", "* [X] star marker"]


def test_parse_ticked_strips_trailing_cr() -> None:
    assert parse_ticked("- [x] done\r\n- [ ] open\r\n") == ["- [x] done"]


def test_parse_ticked_ignores_a_commented_out_line() -> None:
    text = "<!--\n- [x] commented out\n-->\n- [x] real\n"
    assert parse_ticked(text) == ["- [x] real"]


def test_parse_ticked_ignores_a_tilde_fence() -> None:
    text = "~~~\n- [x] inside tilde fence\n~~~\n- [x] outside\n"
    assert parse_ticked(text) == ["- [x] outside"]


def test_parse_ticked_requires_a_space_after_the_closing_bracket() -> None:
    # Same convention as `_TASK_LINE`: a checkbox with no separating space is not a task line.
    assert parse_ticked("- [x]no space\n") == []


# --- read_vault_facts: tasks -------------------------------------------------------------------


def test_only_direct_project_tasks_files_are_matched(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    write(vault / "10-projects" / "alpha" / "tasks.md", "- [ ] counted 📅 2026-10-09\n")
    write(vault / "10-projects" / "alpha" / "notes.md", "- [ ] wrong file 📅 2026-10-09\n")
    write(vault / "10-projects" / "alpha" / "sub" / "tasks.md", "- [ ] too deep 📅 2026-10-09\n")
    write(vault / "10-projects" / "tasks.md", "- [ ] missing the project segment 📅 2026-10-09\n")
    facts = read_vault_facts(service(vault), WID, TODAY).tasks
    assert facts.files == 1
    assert [item.text for item in facts.items] == ["counted"]


def test_items_are_ordered_overdue_then_today_then_soon_tie_broken_by_ref_then_line(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    write(
        vault / "10-projects" / "alpha" / "tasks.md",
        "- [ ] alpha overdue two 📅 2026-10-01\n"
        "- [ ] alpha overdue one 📅 2026-09-30\n"
        "- [ ] alpha today 📅 2026-10-09\n"
        "- [ ] alpha soon two 📅 2026-10-12\n",
    )
    write(
        vault / "10-projects" / "beta" / "tasks.md",
        "- [ ] beta overdue same day 📅 2026-10-01\n"
        "- [ ] beta today 📅 2026-10-09\n"
        "- [ ] beta soon one 📅 2026-10-10\n",
    )
    facts = read_vault_facts(service(vault), WID, TODAY).tasks
    assert (facts.overdue, facts.today, facts.soon) == (3, 2, 2)  # totals, not just the top 3
    assert len(facts.items) == MAX_TASK_ITEMS
    assert facts.items == (
        TaskItem("10-projects/alpha/tasks.md", 2, "alpha overdue one", date(2026, 9, 30), ()),
        TaskItem("10-projects/alpha/tasks.md", 1, "alpha overdue two", date(2026, 10, 1), ()),
        TaskItem("10-projects/beta/tasks.md", 1, "beta overdue same day", date(2026, 10, 1), ()),
    )


def test_files_key_is_the_sha256_of_ref_and_sha256_pairs_in_ref_order(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    write(vault / "10-projects" / "alpha" / "tasks.md", "- [ ] a 📅 2026-10-09\n")
    write(vault / "10-projects" / "beta" / "tasks.md", "- [ ] b 📅 2026-10-09\n")
    facts = read_vault_facts(service(vault), WID, TODAY).tasks
    refs = ["10-projects/alpha/tasks.md", "10-projects/beta/tasks.md"]
    pairs = [(ref, hashlib.sha256((vault / ref).read_bytes()).hexdigest()) for ref in refs]
    expected = hashlib.sha256(
        "\n".join(f"{ref}\t{sha}" for ref, sha in pairs).encode("utf-8")
    ).hexdigest()
    assert facts.files == 2
    assert facts.files_key == expected


def test_a_task_read_error_sets_partial_and_excludes_that_file_from_items_and_the_key(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    write(vault / "10-projects" / "alpha" / "tasks.md", "- [ ] fine 📅 2026-10-09\n")
    (vault / "10-projects" / "broken").mkdir(parents=True)
    (vault / "10-projects" / "broken" / "tasks.md").write_bytes(b"\xff\xfe not utf-8")
    facts = read_vault_facts(service(vault), WID, TODAY).tasks
    assert facts.partial is True
    assert facts.files == 2  # both candidates are selected...
    assert [item.text for item in facts.items] == ["fine"]  # ...but only one contributes tasks
    good = vault / "10-projects/alpha/tasks.md"
    expected = hashlib.sha256(
        f"10-projects/alpha/tasks.md\t{hashlib.sha256(good.read_bytes()).hexdigest()}".encode()
    ).hexdigest()
    assert facts.files_key == expected


def test_only_the_first_32_task_files_are_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    for i in range(MAX_TASK_FILES + 1):
        write(vault / "10-projects" / f"proj-{i:02d}" / "tasks.md", "- [ ] t 📅 2026-10-09\n")
    reader = service(vault)
    calls = spy_reads(reader, monkeypatch)
    facts = read_vault_facts(reader, WID, TODAY).tasks
    assert facts.files == MAX_TASK_FILES
    # The kept items' own project overviews (read for `tags`) add calls beyond the tasks files
    # themselves; what this test pins is that only the first 32 *tasks.md* files are ever read.
    assert len([c for c in calls if c.endswith("/tasks.md")]) == MAX_TASK_FILES
    assert f"10-projects/proj-{MAX_TASK_FILES:02d}/tasks.md" not in calls


def test_a_large_task_set_matches_a_plain_sort_in_items_and_counts(tmp_path: Path) -> None:
    # The file and byte caps bound how much is read, not how many TaskItems come out of parsing;
    # this pins the bounded (heapq) selection to the same result a full collect-and-sort gives.
    vault = tmp_path / "vault"
    rng = random.Random(0)
    refs = ["10-projects/alpha/tasks.md", "10-projects/beta/tasks.md"]
    bodies: dict[str, str] = {}
    for ref in refs:
        bodies[ref] = "".join(
            f"- [ ] task {i:05d} 📅 {(TODAY + timedelta(days=rng.randint(-30, 30))).isoformat()}\n"
            for i in range(2000)
        )
        write(vault / ref, bodies[ref])
    facts = read_vault_facts(service(vault), WID, TODAY).tasks

    all_items = [
        TaskItem(ref, line, text, due, ())  # no tags key and no overview: no tags
        for ref, body in bodies.items()
        for line, text, due in parse_tasks(body, TODAY)
    ]
    overdue = sorted((t for t in all_items if t.due < TODAY), key=lambda t: (t.due, t.ref, t.line))
    due_today = sorted((t for t in all_items if t.due == TODAY), key=lambda t: (t.ref, t.line))
    soon = sorted((t for t in all_items if t.due > TODAY), key=lambda t: (t.due, t.ref, t.line))
    expected = tuple((overdue + due_today + soon)[:MAX_TASK_ITEMS])

    assert (facts.overdue, facts.today, facts.soon) == (len(overdue), len(due_today), len(soon))
    assert facts.items == expected


# --- read_vault_facts: ticked tasks -------------------------------------------------------------


def test_ticked_counts_and_digest(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    write(
        vault / "10-projects" / "alpha" / "tasks.md",
        "- [x] done one 📅 2026-10-01\n"
        "- [X] done two\n"
        "- [ ] still open 📅 2026-10-09\n"
        "```\n- [x] fenced, not counted\n```\n",
    )
    facts = read_vault_facts(service(vault), WID, TODAY).tasks
    assert len(facts.ticked) == 1
    ticked = facts.ticked[0]
    assert ticked.ref == "10-projects/alpha/tasks.md"
    assert ticked.count == 2
    expected_digest = hashlib.sha256(
        "\n".join(["- [x] done one 📅 2026-10-01", "- [X] done two"]).encode("utf-8")
    ).hexdigest()
    assert ticked.digest == expected_digest
    # Open-task results are unchanged by any of this.
    assert [item.text for item in facts.items] == ["still open"]


def test_a_file_with_no_ticked_lines_still_appears_with_zero_count(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    write(vault / "10-projects" / "alpha" / "tasks.md", "- [ ] open only 📅 2026-10-09\n")
    facts = read_vault_facts(service(vault), WID, TODAY).tasks
    assert facts.ticked == (
        TickedFile("10-projects/alpha/tasks.md", 0, hashlib.sha256(b"").hexdigest(), ()),
    )


def test_ticked_tags_join_the_overview_tags_already_read_for_a_kept_item(
    tmp_path: Path,
) -> None:
    # alpha has one dated open task, so it is kept and its overview gets read for that; the
    # ticked file's tags must join the same read, with no read of its own.
    vault = tmp_path / "vault"
    write(
        vault / "10-projects" / "alpha" / "tasks.md",
        "- [x] done\n- [ ] open 📅 2026-10-09\n",
    )
    write(vault / "10-projects" / "alpha" / "alpha.md", "---\ntags: [work]\n---\nOverview\n")
    facts = read_vault_facts(service(vault), WID, TODAY).tasks
    assert facts.items[0].tags == ("work",)  # confirms alpha's overview was read for this item
    assert facts.ticked[0].tags == ("work",)


def test_ticked_tags_are_none_when_the_overview_exists_but_was_never_read(
    tmp_path: Path,
) -> None:
    # alpha has no open dated task, so nothing is kept and its overview is never read — but the
    # overview does exist, so alpha's tags are unknown, not "no tags".
    vault = tmp_path / "vault"
    write(vault / "10-projects" / "alpha" / "tasks.md", "- [x] done\n")
    write(vault / "10-projects" / "alpha" / "alpha.md", "---\ntags: [work]\n---\nOverview\n")
    facts = read_vault_facts(service(vault), WID, TODAY).tasks
    assert facts.items == ()  # nothing kept: alpha's overview truly was never read
    assert facts.ticked[0].tags is None


def test_ticked_tags_are_its_own_when_the_project_has_no_overview_at_all(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    write(
        vault / "10-projects" / "beta" / "tasks.md",
        '---\ntags: ["Private"]\n---\n- [x] done\n',
    )
    facts = read_vault_facts(service(vault), WID, TODAY).tasks
    assert facts.ticked[0].tags == ("private",)


def test_ticked_tags_are_none_when_the_tasks_files_own_tags_are_unknown(
    tmp_path: Path,
) -> None:
    # "--- " (a trailing space) opens no frontmatter for the display parser, while the plain
    # key reader still finds "tags": own tags are unknown, which must stay None regardless of
    # how the overview branch would otherwise resolve (no overview exists here either).
    vault = tmp_path / "vault"
    write(vault / "10-projects" / "gamma" / "tasks.md", "--- \ntags: [private]\n---\n- [x] done\n")
    facts = read_vault_facts(service(vault), WID, TODAY).tasks
    assert facts.ticked[0].tags is None


# --- TickedFile.tags carries inline tags too, so never-send still holds it back ----------------
#
# `tasks_ticked`'s own evidence excerpt is "", so a never-send tag written inline anywhere in
# the tasks.md -- on an open task, a heading, any line other than the ticked ones themselves --
# was never seen unless it also sat in the file's frontmatter. `TickedFile.tags` must carry it
# too, from the one read `_task_facts` already makes.


def test_ticked_tags_gain_an_inline_tag_on_a_line_other_than_the_ticked_ones(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    write(
        vault / "10-projects" / "alpha" / "tasks.md",
        "#private\n- [x] done one\n- [x] done two\n- [x] done three\n"
        "- [x] done four\n- [x] done five\n",
    )
    reader = service(vault)
    calls = spy_reads(reader, monkeypatch)
    facts = read_vault_facts(reader, WID, TODAY).tasks
    assert facts.ticked[0].tags == ("private",)
    assert calls == ["10-projects/alpha/tasks.md"]  # the inline tags cost no extra read


def test_ticked_tags_join_frontmatter_then_inline_each_once(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    write(
        vault / "10-projects" / "alpha" / "tasks.md",
        "---\ntags: [Alpha]\n---\nSee #alpha and #private twice: #private again.\n- [x] done\n",
    )
    facts = read_vault_facts(service(vault), WID, TODAY).tasks
    assert facts.ticked[0].tags == ("alpha", "private")  # frontmatter first, each tag once


def test_ticked_tags_stay_none_with_an_inline_tag_when_own_tags_are_unknown(
    tmp_path: Path,
) -> None:
    # "--- " (a trailing space) opens no frontmatter for the display parser, while the plain key
    # reader still finds "tags": own tags are unknown, which must stay None even though the body
    # carries an inline tag of its own.
    vault = tmp_path / "vault"
    write(
        vault / "10-projects" / "gamma" / "tasks.md",
        "--- \ntags: [private]\n---\n#work\n- [x] done\n",
    )
    facts = read_vault_facts(service(vault), WID, TODAY).tasks
    assert facts.ticked[0].tags is None


def test_ticked_facts_cover_every_candidate_with_no_extra_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    write(vault / "10-projects" / "alpha" / "tasks.md", "- [x] a\n- [x] b\n")  # no open tasks
    write(vault / "10-projects" / "beta" / "tasks.md", "- [ ] open 📅 2026-10-09\n")
    write(vault / "10-projects" / "beta" / "beta.md", "---\ntags: [beta]\n---\nOverview\n")
    reader = service(vault)
    calls = spy_reads(reader, monkeypatch)
    facts = read_vault_facts(reader, WID, TODAY).tasks
    assert {f.ref: f.count for f in facts.ticked} == {
        "10-projects/alpha/tasks.md": 2,
        "10-projects/beta/tasks.md": 0,
    }
    # alpha has no open task, so it is never "best": its (nonexistent) overview is never read,
    # and ticked facts add no call beyond the two tasks.md reads plus beta's one kept overview.
    assert calls == [
        "10-projects/alpha/tasks.md",
        "10-projects/beta/tasks.md",
        "10-projects/beta/beta.md",
    ]


def test_a_failed_read_excludes_that_file_from_ticked_too(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    write(vault / "10-projects" / "alpha" / "tasks.md", "- [x] fine\n")
    (vault / "10-projects" / "broken").mkdir(parents=True)
    (vault / "10-projects" / "broken" / "tasks.md").write_bytes(b"\xff\xfe not utf-8")
    facts = read_vault_facts(service(vault), WID, TODAY).tasks
    assert [f.ref for f in facts.ticked] == ["10-projects/alpha/tasks.md"]
    assert facts.partial is True


# --- task item tags (tasks.md and its project overview) ------------------------------------


@pytest.mark.parametrize(
    ("own", "expected"),
    [
        ('["Private", "#urgent"]', ("private", "urgent", "alpha")),
        ("[]", ("alpha", "urgent")),
    ],
)
def test_task_item_tags_join_the_tasks_files_own_with_the_overviews(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, own: str, expected: tuple[str, ...]
) -> None:
    vault = tmp_path / "vault"
    write(
        vault / "10-projects" / "alpha" / "tasks.md",
        f"---\ntags: {own}\n---\n- [ ] one 📅 2026-10-01\n- [ ] two 📅 2026-10-02\n",
    )
    write(
        vault / "10-projects" / "alpha" / "alpha.md",
        '---\ntags: ["Alpha", "#urgent"]\n---\nOverview\n',
    )
    reader = service(vault)
    calls = spy_reads(reader, monkeypatch)
    facts = read_vault_facts(reader, WID, TODAY).tasks
    assert len(facts.items) == 2
    assert all(item.tags == expected for item in facts.items)
    # tasks.md's own tags come from the read that parsed it: no read is added for them.
    assert calls == ["10-projects/alpha/tasks.md", "10-projects/alpha/alpha.md"]


def test_a_tasks_file_without_a_tags_key_adds_no_tags_of_its_own(tmp_path: Path) -> None:
    # A tasks.md is schema-exempt: no `tags` key reads as no tags, so the overview's stand.
    vault = tmp_path / "vault"
    write(vault / "10-projects" / "alpha" / "tasks.md", "- [ ] solo 📅 2026-10-01\n")
    write(vault / "10-projects" / "alpha" / "alpha.md", "---\ntags: [alpha]\n---\nOverview\n")
    facts = read_vault_facts(service(vault), WID, TODAY).tasks
    assert facts.items[0].tags == ("alpha",)


def test_a_project_without_an_overview_adds_no_tags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    write(
        vault / "10-projects" / "beta" / "tasks.md",
        "---\ntags: [beta]\n---\n- [ ] solo 📅 2026-10-01\n",
    )
    reader = service(vault)
    calls = spy_reads(reader, monkeypatch)
    facts = read_vault_facts(reader, WID, TODAY).tasks
    assert facts.items[0].tags == ("beta",)
    assert calls == ["10-projects/beta/tasks.md", "10-projects/beta/beta.md"]  # no new read


def test_task_item_tags_are_unknown_when_the_overview_cannot_be_read(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    write(vault / "10-projects" / "alpha" / "tasks.md", "- [ ] solo 📅 2026-10-01\n")
    (vault / "10-projects" / "alpha" / "alpha.md").write_bytes(b"\xff\xfe not utf-8")
    facts = read_vault_facts(service(vault), WID, TODAY).tasks
    assert facts.items[0].tags is None


def test_an_overview_the_scan_lists_in_another_case_is_not_taken_for_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # On a case-sensitive disk the read of `alpha/alpha.md` can miss an `alpha/Alpha.md` the
    # scan lists: the project has an overview, unread, so its tags stay unknown.
    vault = tmp_path / "vault"
    write(vault / "10-projects" / "alpha" / "tasks.md", "- [ ] solo 📅 2026-10-01\n")
    write(vault / "10-projects" / "alpha" / "Alpha.md", "---\ntags: [private]\n---\nOverview\n")
    reader = service(vault)
    original = reader.read

    def case_sensitive(workspace_id: str, ref: str) -> Note:
        if ref == "10-projects/alpha/alpha.md":
            raise VaultError("missing_note")
        return original(workspace_id, ref)

    monkeypatch.setattr(reader, "read", case_sensitive)
    facts = read_vault_facts(reader, WID, TODAY).tasks
    assert facts.items[0].tags is None


def test_overview_tags_gain_an_inline_tag_for_both_task_item_and_ticked_file(
    tmp_path: Path,
) -> None:
    """`_task_overview_tags` reads the overview's whole text but only took its frontmatter
    tags, although both `TaskItem.tags` (via `_joined_tags`) and `TickedFile.tags` (via
    `_ticked_tags`) join whatever it finds -- an inline never-send tag in the overview's body,
    not just its frontmatter, must reach both."""
    vault = tmp_path / "vault"
    write(
        vault / "10-projects" / "alpha" / "tasks.md",
        "- [x] done one\n- [x] done two\n- [x] done three\n- [x] done four\n- [x] done five\n"
        "- [ ] call the lawyer 📅 2026-10-09\n",
    )
    write(
        vault / "10-projects" / "alpha" / "alpha.md",
        "# Alpha\n#private\nThe overview of a private project.\n",
    )
    facts = read_vault_facts(service(vault), WID, TODAY).tasks
    assert facts.items[0].tags == ("private",)
    assert facts.ticked[0].tags == ("private",)


def test_task_overview_is_read_once_per_project_and_only_for_kept_items(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    # alpha contributes two of the three kept (most-overdue) items; gamma's task is far enough
    # in the future to fall outside the top MAX_TASK_ITEMS, so its overview must never be read.
    write(
        vault / "10-projects" / "alpha" / "tasks.md",
        "- [ ] a1 📅 2026-09-01\n- [ ] a2 📅 2026-09-02\n",
    )
    write(vault / "10-projects" / "alpha" / "alpha.md", "---\ntags: [alpha]\n---\nOverview\n")
    write(vault / "10-projects" / "beta" / "tasks.md", "- [ ] b1 📅 2026-09-03\n")
    write(vault / "10-projects" / "beta" / "beta.md", "---\ntags: [beta]\n---\nOverview\n")
    write(vault / "10-projects" / "gamma" / "tasks.md", "- [ ] g1 📅 2026-10-15\n")
    write(vault / "10-projects" / "gamma" / "gamma.md", "---\ntags: [gamma]\n---\nOverview\n")
    reader = service(vault)
    calls = spy_reads(reader, monkeypatch)
    facts = read_vault_facts(reader, WID, TODAY).tasks
    assert [item.ref for item in facts.items] == [
        "10-projects/alpha/tasks.md",
        "10-projects/alpha/tasks.md",
        "10-projects/beta/tasks.md",
    ]
    assert calls.count("10-projects/alpha/alpha.md") == 1
    assert "10-projects/beta/beta.md" in calls
    assert "10-projects/gamma/gamma.md" not in calls


# --- the tags under a never-send tag rule (detect, then build_state) --------------------------


def built_under(facts: VaultFacts, *never_send: str) -> dict[str, BuiltState]:
    """Each candidate `detect` finds in `facts`, by type, as `build_state` builds it under
    these never-send entries."""
    now = datetime(2026, 10, 9, 9, 0, tzinfo=UTC)
    settings = DecisionSettings(never_send=never_send)
    obs = Observation(WID, "vault", facts, (), (), None, ())
    moment = Now(None, False, False, now, 0)
    return {
        candidate.type: build_state(candidate, Owner(), moment, History(), settings)
        for candidate in detect(obs, settings, (), now)
    }


def test_untagged_tasks_and_captures_pass_a_tag_rule(tmp_path: Path) -> None:
    # Neither the tasks.md, its overview nor the capture has a `tags` key: their tags are (),
    # so a "#private" rule leaves both candidates to the Decision model, holding nothing back.
    vault = tmp_path / "vault"
    write(vault / "10-projects" / "alpha" / "tasks.md", "- [ ] Renew the passport 📅 2026-10-09\n")
    write(
        vault / "10-projects" / "alpha" / "alpha.md",
        "---\ntype: project\nstatus: active\n---\n# Alpha\n",
    )
    write(vault / "00-inbox" / "a.md", "---\ncaptured: 2026-10-01T00:00:00+00:00\n---\nCall\n")
    facts = read_vault_facts(service(vault), WID, TODAY)
    assert [item.tags for item in facts.tasks.items] == [()]
    assert [item.tags for item in facts.inbox.items] == [()]
    built = built_under(facts, "#private")
    assert {kind: (state.blocked, state.held_back) for kind, state in built.items()} == {
        "tasks_due": (False, 0),
        "inbox": (False, 0),
    }


def test_an_unreadable_overview_still_blocks_its_task_under_a_tag_rule(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    write(vault / "10-projects" / "alpha" / "tasks.md", "- [ ] Renew the passport 📅 2026-10-09\n")
    (vault / "10-projects" / "alpha" / "alpha.md").write_bytes(b"\xff\xfe not utf-8")
    facts = read_vault_facts(service(vault), WID, TODAY)
    assert [item.tags for item in facts.tasks.items] == [None]
    assert built_under(facts, "#private")["tasks_due"].blocked is True
    assert built_under(facts, "health/")["tasks_due"].blocked is False


# --- read_goals -----------------------------------------------------------------------------


def test_read_goals_keeps_only_active_projects_with_title_and_tags(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    write(
        vault / "10-projects" / "alpha" / "alpha.md",
        '---\ntype: project\nstatus: active\ntags: ["Work"]\n---\n# Alpha Project\nOverview.\n',
    )
    write(
        vault / "10-projects" / "beta" / "beta.md",
        "---\ntype: project\nstatus: done\n---\n# Beta Project\nOverview.\n",
    )
    write(
        vault / "10-projects" / "gamma" / "gamma.md",
        "---\ntype: area\nstatus: active\n---\n# Gamma Area\nOverview.\n",
    )
    reader = service(vault)
    goals = read_goals(reader, WID, reader.scan(WID))
    assert [g.ref for g in goals] == ["10-projects/alpha/alpha.md"]
    assert goals[0].title == "Alpha Project"
    assert goals[0].tags == ("work",)


def test_read_goals_ignores_non_overview_project_files(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    write(
        vault / "10-projects" / "alpha" / "notes.md",
        "---\ntype: project\nstatus: active\n---\n# Not The Overview\n",
    )
    write(vault / "10-projects" / "readme.md", "---\ntype: project\nstatus: active\n---\n# Root\n")
    reader = service(vault)
    goals = read_goals(reader, WID, reader.scan(WID))
    assert goals == ()


def test_read_goals_keeps_at_most_8_most_recently_modified(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    for i in range(10):
        ref = vault / "10-projects" / f"proj-{i:02d}" / f"proj-{i:02d}.md"
        write(ref, "---\ntype: project\nstatus: active\n---\nOverview.\n")
        # Stagger modification times so "newest" is unambiguous and matches creation order.
        os.utime(ref, (1_700_000_000 + i, 1_700_000_000 + i))
    reader = service(vault)
    goals = read_goals(reader, WID, reader.scan(WID))
    assert len(goals) == MAX_GOALS
    assert [g.ref for g in goals] == [
        f"10-projects/proj-{i:02d}/proj-{i:02d}.md" for i in reversed(range(2, 10))
    ]


def test_read_goals_stops_reading_once_8_qualify(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    for i in range(10):
        ref = vault / "10-projects" / f"proj-{i:02d}" / f"proj-{i:02d}.md"
        write(ref, "---\ntype: project\nstatus: active\n---\nOverview.\n")
        os.utime(ref, (1_700_000_000 + i, 1_700_000_000 + i))
    reader = service(vault)
    calls = spy_reads(reader, monkeypatch)
    goals = read_goals(reader, WID, reader.scan(WID))
    assert len(goals) == MAX_GOALS
    assert len(calls) == MAX_GOALS  # the two oldest (proj-00, proj-01) are never read


def test_read_goals_reads_at_most_16_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    for i in range(20):
        ref = vault / "10-projects" / f"proj-{i:02d}" / f"proj-{i:02d}.md"
        # None qualify (wrong status), so every candidate is read until the cap stops it.
        write(ref, "---\ntype: project\nstatus: seed\n---\nOverview.\n")
        os.utime(ref, (1_700_000_000 + i, 1_700_000_000 + i))
    reader = service(vault)
    calls = spy_reads(reader, monkeypatch)
    goals = read_goals(reader, WID, reader.scan(WID))
    assert goals == ()
    assert len(calls) == MAX_GOAL_READS


def test_read_goals_skips_a_failed_read(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    write(
        vault / "10-projects" / "alpha" / "alpha.md",
        "---\ntype: project\nstatus: active\n---\n# Alpha\nOverview.\n",
    )
    (vault / "10-projects" / "broken").mkdir(parents=True)
    (vault / "10-projects" / "broken" / "broken.md").write_bytes(b"\xff\xfe not utf-8")
    reader = service(vault)
    goals = read_goals(reader, WID, reader.scan(WID))
    assert [g.ref for g in goals] == ["10-projects/alpha/alpha.md"]


def test_read_goals_never_scans_itself(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    vault = tmp_path / "vault"
    write(
        vault / "10-projects" / "alpha" / "alpha.md",
        "---\ntype: project\nstatus: active\n---\n# Alpha\nOverview.\n",
    )
    reader = service(vault)
    scan = reader.scan(WID)

    def _boom(workspace_id: str) -> ScanResult:
        raise AssertionError("read_goals must not scan on its own")

    monkeypatch.setattr(reader, "scan", _boom)
    goals = read_goals(reader, WID, scan)
    assert [g.ref for g in goals] == ["10-projects/alpha/alpha.md"]


def _active_overview(vault: Path, slug: str) -> Path:
    ref = vault / "10-projects" / slug / f"{slug}.md"
    write(ref, "---\ntype: project\nstatus: active\n---\nOverview.\n")
    return ref


def test_read_goals_reads_when_each_project_last_changed(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    alpha = _active_overview(vault, "alpha")
    beta = _active_overview(vault, "beta")
    tasks = vault / "10-projects" / "alpha" / "tasks.md"
    write(tasks, "- [ ] x\n")
    nested = vault / "10-projects" / "alpha" / "sub" / "deep.md"
    write(nested, "deep\n")
    other_case = vault / "10-projects" / "Beta" / "Extra.md"
    if other_case.parent.exists():  # case-insensitive filesystem: same folder as beta
        other_case = vault / "10-projects" / "beta" / "Extra.md"
    write(other_case, "extra\n")
    os.utime(alpha, (1_700_000_100, 1_700_000_100))
    os.utime(tasks, (1_700_000_200, 1_700_000_200))
    os.utime(nested, (1_700_000_300, 1_700_000_300))
    os.utime(beta, (1_700_000_050, 1_700_000_050))
    os.utime(other_case, (1_700_000_900, 1_700_000_900))
    reader = service(vault)
    goals = {g.ref: g.touched for g in read_goals(reader, WID, reader.scan(WID))}
    assert goals["10-projects/alpha/alpha.md"] == datetime.fromtimestamp(1_700_000_300, UTC)
    # alpha's newer notes do not count for beta, and beta's folder in another case does.
    assert goals["10-projects/beta/beta.md"] == datetime.fromtimestamp(1_700_000_900, UTC)


def test_read_goals_leaves_touched_unknown_on_a_partial_scan(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _active_overview(vault, "alpha")
    _active_overview(vault, "beta")
    reader = service(vault)
    scan = dataclasses.replace(reader.scan(WID), partial=True)
    goals = read_goals(reader, WID, scan)
    assert len(goals) == 2
    assert [g.touched for g in goals] == [None, None]


def test_read_goals_leaves_touched_unknown_for_a_stamp_out_of_range(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _active_overview(vault, "alpha")
    reader = service(vault)
    scan = reader.scan(WID)
    huge = tuple(dataclasses.replace(e, modified=1e20) for e in scan.entries)
    goals = read_goals(reader, WID, dataclasses.replace(scan, entries=huge))
    assert [g.touched for g in goals] == [None]
