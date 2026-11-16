"""The inbox capture listing: order, previews, projects and the two read budgets.

Every behavioural row runs against a real on-disk vault and the real reader, so a write path
added to `captures.py` shows up as a changed byte or modification time in `tree()`.
"""

from __future__ import annotations

import ast
import hashlib
import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from viki_agent.domain import CanonicalRoot
from viki_agent.vaults import captures
from viki_agent.vaults.captures import (
    MAX_CAPTURE_BYTES,
    MAX_CAPTURES,
    MAX_READ_BYTES,
    PREVIEW_CHARS,
    CaptureError,
    CaptureService,
    _captured_value,
    _preview,
)
from viki_agent.vaults.vault import Note, NoteEntry, ScanResult, VaultError, VaultService
from viki_agent.workspaces import WorkspaceError, WorkspaceService, root_key

BASE_FRONTMATTER = "---\ncaptured: 2026-10-09T08:00:00Z\n---\n"


class _Workspaces:
    """Only what `CaptureService` and the real `VaultService` reach."""

    def __init__(self) -> None:
        self.spaces: dict[str, Any] = {}
        self.active_id: str | None = None

    def add(self, root: Path, *, kind: str = "vault") -> str:
        info = os.stat(root)
        workspace_id = str(uuid.uuid4())
        self.spaces[workspace_id] = SimpleNamespace(
            workspace_id=workspace_id,
            kind=kind,
            root=CanonicalRoot(
                key=root_key(root.resolve()),
                display=str(root.resolve()),
                physical_id=f"{info.st_dev}:{info.st_ino}",
                aliased=False,
            ),
        )
        if self.active_id is None:
            self.active_id = workspace_id
        return workspace_id

    def get(self, workspace_id: str, *, kind: str | None = "vault") -> Any:
        if workspace_id not in self.spaces:
            raise WorkspaceError("unknown_workspace")
        return self.spaces[workspace_id]

    def current_root(self, workspace: Any) -> Any:
        return workspace.root

    def resolve_active(self) -> Any:
        assert self.active_id is not None
        return SimpleNamespace(workspace=self.spaces[self.active_id])


Entry = tuple[str, int, str, int]


def tree(root: Path) -> list[Entry]:
    """Every entry under `root` with its bytes and modification time, so even a rewrite shows."""
    entries: list[Entry] = []
    for folder, directories, files in os.walk(root):
        for name in directories:
            path = Path(folder) / name
            entries.append((os.path.relpath(path, root), -1, "", path.stat().st_mtime_ns))
        for name in files:
            path = Path(folder) / name
            data = path.read_bytes()
            entries.append(
                (
                    os.path.relpath(path, root),
                    len(data),
                    hashlib.sha256(data).hexdigest(),
                    path.stat().st_mtime_ns,
                )
            )
    return sorted(entries)


def unchanged(root: Path, call: Callable[..., Any], *args: Any) -> Any:
    """Run one call with the vault tree hashed before and after it."""
    before = tree(root)
    try:
        return call(*args)
    finally:
        assert tree(root) == before


@dataclass
class Harness:
    service: CaptureService
    vault: VaultService
    spaces: _Workspaces
    root: Path
    wid: str

    def note(self, ref: str, text: str) -> Path:
        """Write a note the way an editor outside the app would."""
        path = self.root / ref
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))
        return path


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    root = tmp_path / "vault"
    root.mkdir()
    spaces = _Workspaces()
    vault = VaultService(cast(WorkspaceService, spaces))
    service = CaptureService(cast(WorkspaceService, spaces), vault)
    return Harness(service, vault, spaces, root, spaces.add(root))


# --- order, previews and frontmatter -------------------------------------------------------


def test_captures_are_ordered_by_ref_and_previews_strip_frontmatter(harness: Harness) -> None:
    harness.note("00-inbox/2026-10-09-b.md", BASE_FRONTMATTER + "second capture\n")
    harness.note("00-inbox/2026-10-09-a.md", BASE_FRONTMATTER + "first capture\n")
    harness.note("00-inbox/2026-10-09-c.md", "no frontmatter here\n")

    listing = unchanged(harness.root, harness.service.list, harness.wid)

    assert [c.ref for c in listing.captures] == [
        "00-inbox/2026-10-09-a.md",
        "00-inbox/2026-10-09-b.md",
        "00-inbox/2026-10-09-c.md",
    ]
    a, b, c = listing.captures
    assert a.preview == "first capture\n" and a.cut is False and a.unread is None
    assert a.captured == "2026-10-09T08:00:00Z"
    expected_sha = hashlib.sha256((BASE_FRONTMATTER + "first capture\n").encode()).hexdigest()
    assert a.revision == expected_sha
    assert isinstance(a.modified, str) and a.modified.endswith("Z")
    assert b.preview == "second capture\n" and b.captured == "2026-10-09T08:00:00Z"
    assert c.preview == "no frontmatter here\n" and c.captured is None
    assert listing.workspace_id == harness.wid
    assert listing.partial is False and listing.omitted == 0


def test_a_long_preview_is_cut_at_the_character_limit(harness: Harness) -> None:
    long_body = "x" * (PREVIEW_CHARS + 50)
    harness.note("00-inbox/long.md", BASE_FRONTMATTER + long_body)

    listing = harness.service.list(harness.wid)

    assert len(listing.captures) == 1
    capture = listing.captures[0]
    assert capture.preview == long_body[:PREVIEW_CHARS]
    assert capture.cut is True


def test_preview_strips_frontmatter_only_when_the_first_line_opens_it() -> None:
    assert _preview("plain body\nline two\n") == ("plain body\nline two\n", False)
    assert _preview("---\ncaptured: x\n---\nbody text\n") == ("body text\n", False)


def test_preview_with_unterminated_frontmatter_keeps_the_whole_text() -> None:
    text = "---\nkey: value\nstill going\n"
    assert _preview(text) == (text, False)


def test_preview_cuts_at_the_character_limit() -> None:
    body = "y" * (PREVIEW_CHARS + 1)
    preview, cut = _preview(body)
    assert preview == body[:PREVIEW_CHARS] and cut is True


def test_captured_value_accepts_only_one_to_sixty_four_characters() -> None:
    assert _captured_value({"captured": "2026-10-09"}) == "2026-10-09"
    assert _captured_value({"captured": ""}) is None
    assert _captured_value({"captured": "x" * 65}) is None
    assert _captured_value({}) is None


# --- mtimes past the datetime range ---------------------------------------------------------


def test_modified_stamp_clamps_past_year_9999_and_before_year_1() -> None:
    """`_EPOCH + timedelta(seconds=mtime)` overflows past year 9999 or before year 1, where
    `datetime.fromtimestamp` would instead raise an untyped `OSError` on Windows. Clamped to the
    client's STAMP pattern rather than raised, so one such file never fails a listing."""
    assert captures._modified_stamp(270_000_000_000.0) == "9999-12-31T23:59:59.999Z"
    assert captures._modified_stamp(-270_000_000_000.0) == "0001-01-01T00:00:00.000Z"


def test_an_mtime_past_year_9999_lists_with_a_clamped_stamp_beside_a_normal_one(
    harness: Harness,
) -> None:
    harness.note("00-inbox/a-normal.md", BASE_FRONTMATTER + "normal\n")
    far_future = harness.note("00-inbox/z-far-future.md", "from the far future\n")
    os.utime(far_future, (270_000_000_000.0, 270_000_000_000.0))
    if far_future.stat().st_mtime < 253_402_300_800:  # 10000-01-01Z
        pytest.skip(
            "this filesystem cannot store an mtime past year 9999 (ext4 reads back 2446, APFS "
            "2262); test_modified_stamp_clamps_past_year_9999_and_before_year_1 covers the clamp"
        )

    listing = unchanged(harness.root, harness.service.list, harness.wid)

    assert [c.ref for c in listing.captures] == [
        "00-inbox/a-normal.md",
        "00-inbox/z-far-future.md",
    ]
    normal, far = listing.captures
    assert far.modified == "9999-12-31T23:59:59.999Z"
    assert far.preview == "from the far future\n" and far.unread is None
    assert isinstance(normal.modified, str) and normal.modified != far.modified


# --- folder matching -------------------------------------------------------------------------


def test_other_folders_and_near_miss_names_are_ignored(harness: Harness) -> None:
    harness.note("00-inbox/real.md", BASE_FRONTMATTER + "kept\n")
    harness.note("20-areas/health.md", "ignored\n")
    harness.note("root-note.md", "ignored\n")
    harness.note("00-inbox-archive/old.md", "ignored, not an exact segment match\n")

    listing = harness.service.list(harness.wid)

    assert [c.ref for c in listing.captures] == ["00-inbox/real.md"]


def test_the_inbox_folder_match_is_case_insensitive(harness: Harness) -> None:
    harness.note("00-InBoX/weird-case.md", BASE_FRONTMATTER + "kept\n")

    listing = harness.service.list(harness.wid)

    assert [c.ref for c in listing.captures] == ["00-InBoX/weird-case.md"]


def test_a_00_inbox_segment_nested_under_another_folder_is_not_the_top_level_inbox(
    harness: Harness,
) -> None:
    """Only `parts[0]` is matched. A `00-inbox` segment inside `_raw/` (an immutable original)
    or `90-archive/` (an archived copy) must stay out of the listing."""
    harness.note("00-inbox/real.md", BASE_FRONTMATTER + "kept\n")
    harness.note("_raw/00-inbox/original.md", "immutable original\n")
    harness.note("90-archive/2026/00-inbox/old.md", "archived capture\n")

    listing = harness.service.list(harness.wid)

    assert [c.ref for c in listing.captures] == ["00-inbox/real.md"]


def test_projects_are_unique_slugs_at_least_three_parts_deep(harness: Harness) -> None:
    harness.note("10-projects/alpha/alpha.md", "overview\n")
    harness.note("10-projects/alpha/tasks.md", "tasks\n")
    harness.note("10-projects/alpha/notes/extra.md", "deep\n")
    harness.note("10-projects/beta/beta.md", "overview\n")
    harness.note("10-projects/orphan.md", "ignored: only two parts\n")
    harness.note("10-projects-old/gamma/gamma.md", "ignored: not an exact segment match\n")

    listing = harness.service.list(harness.wid)

    assert listing.projects == ("alpha", "beta")


# --- the hundred-item cap and the two read budgets --------------------------------------------


def test_a_hundred_and_one_captures_lists_a_hundred(harness: Harness) -> None:
    for index in range(MAX_CAPTURES + 1):
        harness.note(f"00-inbox/{index:03d}.md", "capture\n")

    listing = harness.service.list(harness.wid)

    assert len(listing.captures) == MAX_CAPTURES
    assert [c.ref for c in listing.captures] == [
        f"00-inbox/{index:03d}.md" for index in range(MAX_CAPTURES)
    ]
    assert listing.omitted == 1
    assert listing.partial is True


def test_a_read_spy_proves_the_size_cap_and_the_four_mib_budget(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    fill_count = MAX_READ_BYTES // MAX_CAPTURE_BYTES
    assert fill_count * MAX_CAPTURE_BYTES == MAX_READ_BYTES
    entries = [NoteEntry("00-inbox/aa-huge.md", "aa-huge", MAX_CAPTURE_BYTES + 1, 0.0)]
    entries += [
        NoteEntry(f"00-inbox/fill-{i:02d}.md", f"fill-{i:02d}", MAX_CAPTURE_BYTES, 0.0)
        for i in range(fill_count)
    ]
    entries.append(NoteEntry("00-inbox/zz-overflow.md", "zz-overflow", 1, 0.0))
    scan_result = ScanResult(tuple(entries), False, (), 0, len(entries), 0)
    monkeypatch.setattr(harness.vault, "scan", lambda workspace_id, **_: scan_result)

    read_calls: list[str] = []

    def spy(workspace_id: str, ref: str) -> Note:
        read_calls.append(ref)
        return Note(ref, "x", "body\n", "body\n", MAX_CAPTURE_BYTES, "0" * 64, {}, ())

    monkeypatch.setattr(harness.vault, "read", spy)

    listing = harness.service.list(harness.wid)

    assert read_calls == [f"00-inbox/fill-{i:02d}.md" for i in range(fill_count)]
    assert listing.captures[0].ref == "00-inbox/aa-huge.md"
    assert listing.captures[0].unread == "too_large"
    assert all(c.unread is None for c in listing.captures[1:-1])
    assert listing.captures[-1].ref == "00-inbox/zz-overflow.md"
    assert listing.captures[-1].unread == "budget"
    assert listing.partial is True
    assert listing.omitted == 0


def test_a_note_grown_since_the_scan_is_charged_and_shown_by_what_was_really_read(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The scan reports 1 byte for twenty captures that have each grown to 2,000,000 bytes by
    read time (e.g. a sync client still writing). The stale scan size must not be trusted for
    the cap or the budget: charging the real `note.byte_size` after each read stops admission
    once the budget is truly spent, instead of reading all twenty in full (20 * 2,000,000 =
    40,000,000 bytes)."""
    grown = "y" * 2_000_000
    refs = [f"00-inbox/{i:02d}.md" for i in range(20)]
    for ref in refs:
        harness.note(ref, grown)
    stale = ScanResult(
        tuple(NoteEntry(ref, ref, 1, 0.0) for ref in refs), False, (), 0, len(refs), 0
    )
    monkeypatch.setattr(harness.vault, "scan", lambda workspace_id, **_: stale)
    real_read = harness.vault.read
    read_sizes: list[int] = []

    def spy(workspace_id: str, ref: str) -> Note:
        note = real_read(workspace_id, ref)
        read_sizes.append(note.byte_size)
        return note

    monkeypatch.setattr(harness.vault, "read", spy)

    listing = harness.service.list(harness.wid)

    assert read_sizes == [2_000_000, 2_000_000, 2_000_000]  # not all twenty
    assert sum(read_sizes) <= MAX_READ_BYTES + 2_000_000  # the budget plus one note, not twenty
    assert [c.unread for c in listing.captures] == ["too_large"] * 3 + ["budget"] * 17
    assert all(c.preview == "" and c.revision is None for c in listing.captures)
    assert listing.partial is True


def test_a_note_missing_since_the_scan_is_skipped_not_marked_unread(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness.note("00-inbox/a.md", BASE_FRONTMATTER + "first\n")
    harness.note("00-inbox/b.md", BASE_FRONTMATTER + "second\n")
    real_read = harness.vault.read

    def vanished(workspace_id: str, ref: str) -> Note:
        if ref == "00-inbox/a.md":
            raise VaultError("missing_note")
        return real_read(workspace_id, ref)

    monkeypatch.setattr(harness.vault, "read", vanished)

    listing = harness.service.list(harness.wid)

    assert [c.ref for c in listing.captures] == ["00-inbox/b.md"]
    assert listing.omitted == 0 and listing.partial is False


def test_a_scan_failure_passes_its_code_through(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(workspace_id: str, **_: Any) -> ScanResult:
        raise VaultError("root_unavailable")

    monkeypatch.setattr(harness.vault, "scan", refuse)

    with pytest.raises(CaptureError) as raised:
        harness.service.list(harness.wid)

    assert raised.value.code == "root_unavailable"


# --- the three refusals ------------------------------------------------------------------------


def test_an_unknown_workspace_is_refused(harness: Harness) -> None:
    with pytest.raises(CaptureError) as raised:
        harness.service.list(str(uuid.uuid4()))

    assert raised.value.code == "unknown_workspace"


def test_a_non_vault_workspace_is_refused(harness: Harness) -> None:
    code_id = harness.spaces.add(harness.root, kind="code")

    with pytest.raises(CaptureError) as raised:
        harness.service.list(code_id)

    assert raised.value.code == "not_a_vault"


def test_an_inactive_vault_is_refused(harness: Harness, tmp_path: Path) -> None:
    other_root = tmp_path / "other"
    other_root.mkdir()
    other_id = harness.spaces.add(other_root)  # harness.wid stays the active one

    with pytest.raises(CaptureError) as raised:
        harness.service.list(other_id)

    assert raised.value.code == "not_active_vault"


# --- no write, anywhere ------------------------------------------------------------------------


def test_the_vault_tree_is_byte_identical_after_every_call(harness: Harness) -> None:
    harness.note("00-inbox/a.md", BASE_FRONTMATTER + "first\n")
    harness.note("10-projects/alpha/alpha.md", "overview\n")
    before = tree(harness.root)

    unchanged(harness.root, harness.service.list, harness.wid)
    with pytest.raises(CaptureError):
        unchanged(harness.root, harness.service.list, str(uuid.uuid4()))
    code_id = harness.spaces.add(harness.root, kind="code")
    with pytest.raises(CaptureError):
        unchanged(harness.root, harness.service.list, code_id)

    assert tree(harness.root) == before


def test_the_module_imports_no_writer_and_calls_nothing_that_writes() -> None:
    """The static half of the same claim, read from the syntax tree rather than the prose."""
    module = ast.parse(Path(cast(str, captures.__file__)).read_text(encoding="utf-8"))
    imported = {
        name.name
        for node in ast.walk(module)
        if isinstance(node, ast.Import | ast.ImportFrom)
        for name in node.names
    } | {node.module or "" for node in ast.walk(module) if isinstance(node, ast.ImportFrom)}
    called = {
        node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
        for node in ast.walk(module)
        if isinstance(node, ast.Call)
    }

    banned = ("vault_publication", "vault_mutation", "vault_lifecycle", "vault_operations")
    assert not {name for name in imported if any(word in name for word in banned)}
    assert not called & {
        "open",
        "write_text",
        "write_bytes",
        "mkdir",
        "unlink",
        "replace",
        "rename",
    }
