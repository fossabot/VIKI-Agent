from __future__ import annotations

import asyncio
import ctypes
import hashlib
import os
import re
import sys
import unicodedata
from collections.abc import Iterator
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from viki_agent.conversations.turn_parts import (
    EXCERPT_KEYS,
    parse_excerpt_part,
    render_excerpt_block,
)
from viki_agent.domain import CanonicalRoot
from viki_agent.vaults import vault as vault_module
from viki_agent.vaults.note_metadata import extract_display
from viki_agent.vaults.vault import VaultError, VaultService
from viki_agent.winproc import child_env, run, spawn
from viki_agent.workspaces import WorkspaceService, root_key


def service(root: Path, platform: str = sys.platform, active_id: str = "w") -> VaultService:
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
        resolve_active=lambda: SimpleNamespace(workspace=SimpleNamespace(workspace_id=active_id)),
    )
    return VaultService(cast(WorkspaceService, owner), platform=platform)


def test_scan_read_and_japanese_search_exclude_private_tree(tmp_path: Path) -> None:
    (tmp_path / "notes").mkdir()
    note_name = "\u65e5\u672c\u8a9e"
    milk = "\u725b\u4e73"
    body = "\n".join(
        ["---", f"title: {note_name}", "---", f"これは{milk}のメモです", "[[missing]]"]
    )
    (tmp_path / "notes" / f"{note_name}.md").write_text(body, encoding="utf-8", newline="\r\n")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "secret.md").write_text("牛乳", encoding="utf-8")
    vault = service(tmp_path)
    result = vault.scan("w")
    assert [item.ref for item in result.entries] == [f"notes/{note_name}.md"]
    note = vault.read("w", f"notes/{note_name}.md")
    assert note.text.startswith("---")
    assert "\r\n" in note.text
    assert note.frontmatter["title"] == note_name
    hits = vault.search("w", milk)
    assert hits[0].line == 4
    assert hits[0].exact == milk
    assert hits[0].links[0].state == "missing"


def test_refs_are_relative_and_links_report_ambiguity(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("[[same]]", encoding="utf-8")
    (tmp_path / "x").mkdir()
    (tmp_path / "y").mkdir()
    (tmp_path / "x" / "same.md").write_text("x", encoding="utf-8")
    (tmp_path / "y" / "same.md").write_text("y", encoding="utf-8")
    vault = service(tmp_path)
    with pytest.raises(VaultError, match="invalid_ref"):
        vault.read("w", "../a.md")
    assert vault.search("w", "same")[0].links[0].state == "ambiguous"


@pytest.mark.parametrize("platform", ["darwin", "win32", "linux"])
def test_an_nfc_link_reaches_an_nfd_named_note_only_on_macos(tmp_path: Path, platform: str) -> None:
    guide = "ガイド"  # its first kana carries a dakuten, so NFD splits it in two
    (tmp_path / "docs").mkdir()
    stored = unicodedata.normalize("NFD", guide)
    (tmp_path / "docs" / f"{stored}.md").write_text("target", encoding="utf-8")
    # A wiki link by stem and a Markdown link by path, both typed composed.
    (tmp_path / "source.md").write_text(f"[[{guide}]]\n[g](docs/{guide}.md)", encoding="utf-8")
    vault = service(tmp_path, platform)
    links = vault.inspect("w", "source.md").links
    backlinks = vault.backlinks("w", f"docs/{stored}.md")
    if platform == "darwin":
        assert [(link.state, link.candidates) for link in links] == [
            ("resolved", (f"docs/{stored}.md",))
        ] * 2
        assert [item.source for item in backlinks] == ["source.md"]
        composed = vault.backlinks("w", f"docs/{guide}.md")
        assert [item.source for item in composed] == ["source.md"]
    else:
        assert [link.state for link in links] == ["missing", "missing"]
        assert list(backlinks) == []
        with pytest.raises(VaultError, match="missing_note"):
            vault.backlinks("w", f"docs/{guide}.md")


def test_symlink_is_refused(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.md"
    outside.write_text("secret", encoding="utf-8")
    link = tmp_path / "link.md"
    try:
        link.symlink_to(outside)
    except OSError, NotImplementedError:
        pytest.skip("symlinks unavailable")
    vault = service(tmp_path)
    with pytest.raises(VaultError, match="unsafe_path"):
        vault.read("w", "link.md")


def test_a_note_over_2_mib_is_refused_and_one_at_the_bound_reads(tmp_path: Path) -> None:
    (tmp_path / "big.md").write_bytes(b"x" * (2 * 1024 * 1024 + 1))
    (tmp_path / "edge.md").write_bytes(b"x" * (2 * 1024 * 1024))
    vault = service(tmp_path)
    with pytest.raises(VaultError, match="note_too_large"):
        vault.read("w", "big.md")
    assert vault.read("w", "edge.md").byte_size == 2 * 1024 * 1024


@pytest.mark.skipif(sys.platform != "win32", reason="junctions are Windows only")
def test_a_junction_is_omitted_from_the_scan_and_refused_on_read(tmp_path: Path) -> None:
    assert sys.platform == "win32"
    vault_root, outside = tmp_path / "vault", tmp_path / "outside"
    vault_root.mkdir()
    outside.mkdir()
    (outside / "x.md").write_text("outside", encoding="utf-8")
    link = vault_root / "link"
    done = run(["cmd", "/c", "mklink", "/J", str(link), str(outside)], cwd=tmp_path, timeout=10.0)
    assert done.returncode == 0, done.stderr
    vault = service(vault_root)
    scan = vault.scan("w")
    assert scan.entries == ()
    assert "unsafe_entry" in scan.partial_reasons
    with pytest.raises(VaultError, match="unsafe_path"):
        vault.read("w", "link/x.md")


def short_name(path: Path) -> str:
    """The 8.3 name Windows gave the last component of `path`, or the long name if it gave none."""
    assert sys.platform == "win32"
    get_short = ctypes.WinDLL("kernel32", use_last_error=True).GetShortPathNameW
    buffer = ctypes.create_unicode_buffer(1024)
    assert 0 < get_short(str(path), buffer, len(buffer)) < len(buffer), ctypes.get_last_error()
    return Path(buffer.value).name


def _private_agent(root: Path) -> None:
    (root / ".claude" / "agents").mkdir(parents=True)
    (root / ".claude" / "agents" / "x.md").write_text("private", encoding="utf-8")


@pytest.mark.skipif(sys.platform != "win32", reason="8.3 short names are Windows only")
def test_a_read_through_the_short_name_of_a_private_directory_is_refused(tmp_path: Path) -> None:
    assert sys.platform == "win32"
    _private_agent(tmp_path)
    alias = short_name(tmp_path / ".claude")
    if alias == ".claude":
        pytest.skip("this volume makes no 8.3 short names")
    with pytest.raises(VaultError, match="private_path"):
        service(tmp_path).read("w", f"{alias}/agents/x.md")


@pytest.fixture
def trailing_period_alias(tmp_path: Path) -> Iterator[Path]:
    """An empty `.claude.` beside `.claude`; Win32 path parsing opens `.claude` for both."""
    assert sys.platform == "win32"
    _private_agent(tmp_path)
    alias = f"\\\\?\\{tmp_path}\\.claude."
    os.mkdir(alias)
    try:
        yield tmp_path
    finally:
        os.rmdir(alias)


@pytest.mark.skipif(sys.platform != "win32", reason="Win32 path parsing")
def test_a_read_through_a_trailing_period_alias_is_refused(trailing_period_alias: Path) -> None:
    assert sys.platform == "win32"
    with pytest.raises(VaultError, match="private_path"):
        service(trailing_period_alias).read("w", ".claude./agents/x.md")


@pytest.mark.skipif(sys.platform != "win32", reason="Win32 path parsing")
def test_the_scan_omits_a_trailing_period_alias(trailing_period_alias: Path) -> None:
    assert sys.platform == "win32"
    (trailing_period_alias / "public.md").write_text("visible", encoding="utf-8")
    scan = service(trailing_period_alias).scan("w")
    assert [entry.ref for entry in scan.entries] == ["public.md"]
    assert scan.partial_reasons == ("unsafe_entry",)


@pytest.mark.skipif(sys.platform != "win32", reason="the Win32 handle layer")
def test_same_final_path_compares_the_opened_handles_normalized_name(tmp_path: Path) -> None:
    assert sys.platform == "win32"
    (tmp_path / "Notes").mkdir()
    with vault_module._handle(tmp_path / "Notes", directory=True) as handle:
        assert vault_module.same_final_path(handle, str(tmp_path / "notes"))
        assert not vault_module.same_final_path(handle, str(tmp_path / "other"))
    assert not vault_module.same_final_path(0, str(tmp_path))


def test_private_components_are_case_insensitive_for_scan_and_direct_read(tmp_path: Path) -> None:
    private = tmp_path / ".GiT"
    private.mkdir()
    (private / "note.md").write_text("hidden", encoding="utf-8")
    env = tmp_path / ".ENV.Production"
    env.mkdir()
    (env / "note.md").write_text("hidden", encoding="utf-8")
    (tmp_path / "public.md").write_text("visible", encoding="utf-8")
    vault = service(tmp_path)
    assert [entry.ref for entry in vault.scan("w").entries] == ["public.md"]
    for ref in (".GiT/note.md", ".ENV.Production/note.md", "Credentials.JSON"):
        with pytest.raises(VaultError, match="private_path"):
            vault.read("w", ref)


def _mixed(name: str) -> str:
    return "".join(c.upper() if i % 2 else c for i, c in enumerate(name))


def test_write_only_names_and_prefixes_exclude_writes_but_not_reads() -> None:
    pinned = {"agents.md", "agents.override.md", "claude.md", "claude.local.md", "gemini.md"}
    assert pinned == vault_module.WRITE_ONLY_NAMES
    private = (*vault_module.EXCLUDED_DIRS, *vault_module.PRIVATE_NAMES, ".env.synthetic")
    write_only = (
        *vault_module.WRITE_ONLY_NAMES,
        f"{vault_module.STAGE_PREFIX}x.md",
        f"{vault_module.BACKUP_PREFIX}x.md",
    )
    for name in (*private, *write_only):
        for spelled in (name, _mixed(name), name.upper()):
            assert vault_module.excluded_part(spelled, write=True), spelled
            assert vault_module.excluded_part(spelled, write=False) == (name in private), spelled
    for name in ("note.md", "agents", "claude.md.md", ".environment", "env.local"):
        assert not vault_module.excluded_part(name, write=True), name


def test_reader_lists_write_only_names_and_refuses_private_names_at_any_depth(
    tmp_path: Path,
) -> None:
    visible = [_mixed(name) for name in sorted(vault_module.WRITE_ONLY_NAMES)]
    (tmp_path / "sub").mkdir()
    for name in visible:
        (tmp_path / name).write_text("visible", encoding="utf-8")
        (tmp_path / "sub" / name).write_text("visible", encoding="utf-8")
    private = sorted({*vault_module.EXCLUDED_DIRS, *vault_module.PRIVATE_NAMES, ".env.synthetic"})
    for name in private:
        (tmp_path / "sub" / _mixed(name)).mkdir()
        (tmp_path / "sub" / _mixed(name) / "n.md").write_text("hidden", encoding="utf-8")
    vault = service(tmp_path)
    expected = sorted([*visible, *(f"sub/{name}" for name in visible)], key=str.casefold)
    assert sorted((entry.ref for entry in vault.scan("w").entries), key=str.casefold) == expected
    assert vault.read("w", _mixed("agents.md")).text == "visible"
    assert vault.read("w", f"sub/{_mixed('claude.local.md')}").text == "visible"
    for name in private:
        for ref in (f"sub/{_mixed(name)}/n.md", f"a/b/{_mixed(name)}", f"a/{name.upper()}/n.md"):
            with pytest.raises(VaultError, match="private_path"):
                vault.read("w", ref)


def test_malformed_frontmatter_is_reported_without_repair(tmp_path: Path) -> None:
    original = "---\ntitle: Kept\ninvalid line\nBody"
    path = tmp_path / "broken.md"
    path.write_bytes(original.encode())
    note = service(tmp_path).read("w", "broken.md")
    assert note.frontmatter == {"title": "Kept"}
    assert note.frontmatter_error == "invalid_line"
    assert note.text == original
    assert note.source == original
    assert path.read_text(encoding="utf-8") == original


def test_note_source_preserves_a_utf8_bom_for_exact_editing(tmp_path: Path) -> None:
    (tmp_path / "bom.md").write_bytes(b"\xef\xbb\xbf# Exact\n")
    note = service(tmp_path).read("w", "bom.md")
    assert note.text == "# Exact\n"
    assert note.source == "\ufeff# Exact\n"


def test_root_identity_and_hardlinks_fail_closed(tmp_path: Path) -> None:
    original = tmp_path / "original.md"
    linked = tmp_path / "linked.md"
    original.write_text("same bytes", encoding="utf-8")
    os.link(original, linked)
    vault = service(tmp_path)
    with pytest.raises(VaultError, match="unsafe_path"):
        vault.read("w", "linked.md")

    wrong = service(tmp_path)
    wrong._workspaces.get("w").root = CanonicalRoot(  # type: ignore[attr-defined]
        key=root_key(tmp_path.resolve()),
        display=str(tmp_path.resolve()),
        physical_id="1:2",
        aliased=False,
    )
    with pytest.raises(VaultError, match="root_changed"):
        wrong.scan("w")


def test_relative_links_backlinks_and_search_offsets_share_one_scan(tmp_path: Path) -> None:
    (tmp_path / "target.md").write_text("Target", encoding="utf-8")
    docs = tmp_path / "docs"
    docs.mkdir()
    text = "Alpha crème Beta\n[[../target]]\n[child](child.md)"
    (docs / "source.md").write_text(text, encoding="utf-8")
    (docs / "child.md").write_text("Child", encoding="utf-8")
    vault = service(tmp_path)

    inspection = vault.inspect("w", "docs/source.md")
    assert [link.state for link in inspection.links] == ["resolved", "resolved"]
    assert inspection.links[0].candidates == ("target.md",)
    assert inspection.links[1].candidates == ("docs/child.md",)

    hits = vault.search("w", "alpha")
    assert len(hits) == 1
    assert (hits[0].start, hits[0].end, hits[0].exact) == (0, 5, "Alpha")
    backlinks = vault.backlinks("w", "target.md")
    assert [(item.source, item.line) for item in backlinks] == [("docs/source.md", 2)]


def test_scan_and_search_report_typed_limits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "a.md").write_text("hit", encoding="utf-8")
    (tmp_path / "b.md").write_text("hit", encoding="utf-8")
    monkeypatch.setattr(vault_module, "MAX_ENTRIES", 1)
    result = service(tmp_path).scan("w")
    assert result.partial
    assert result.partial_reasons == ("entry_limit",)
    assert len(result.entries) == 1

    monkeypatch.setattr(vault_module, "MAX_ENTRIES", 5_000)
    monkeypatch.setattr(vault_module, "MAX_HITS", 1)
    search = service(tmp_path).search("w", "hit")
    assert search.partial
    assert search.partial_reasons == ("hit_limit",)
    assert len(search) == 1


def test_a_large_synthetic_vault_lists_every_note_with_nothing_omitted(tmp_path: Path) -> None:
    """The scan reads no content, so 22.9 MiB of notes never charges a read budget."""
    content = b"x" * 8_000
    for folder in range(10):
        directory = tmp_path / f"folder{folder}"
        directory.mkdir()
        for index in range(300):
            (directory / f"note{index:04d}.md").write_bytes(content)
    # A loaded machine must not turn this into a `deadline` partial.
    result = service(tmp_path).scan("w", deadline=10)
    assert len(result.entries) == 3_000
    assert not result.partial
    assert result.omitted == 0


@pytest.mark.parametrize("png_at", range(6))
def test_a_scan_that_hits_its_entry_budget_reports_an_exact_omitted_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, png_at: int
) -> None:
    """It patches MAX_ENTRIES, and the drain that follows is bounded and counts only note-named,
    non-excluded entries. ext4 lists a directory in a per-filesystem hash order seeded per
    filesystem, so the count must not depend on it: this pins the scan's own order instead of
    trusting the filesystem's, and parametrizes where the non-note entry falls in it."""
    order = [f"n{index}.md" for index in range(5)]
    order.insert(png_at, "z.png")
    for name in order:
        if name == "z.png":
            (tmp_path / name).write_bytes(b"x")
        else:
            (tmp_path / name).write_text("hit", encoding="utf-8")
    real_scandir = os.scandir
    rank = {name: index for index, name in enumerate(order)}

    class _OrderedScandir:
        """`os.scandir(folder)` (folder a path or an fd), listing the real entries but in the
        fixed order above rather than whatever order the filesystem gives; its own context
        manager and iterator, as the scan needs: opened with `with`, iterated, and the leftover
        `itertools.islice`d, reading only `entry.name`."""

        def __init__(self, folder: Path | int) -> None:
            with real_scandir(folder) as real:
                entries = list(real)
            entries.sort(key=lambda entry: rank.get(entry.name, len(order)))
            self._entries = iter(entries)

        def __enter__(self) -> _OrderedScandir:
            return self

        def __exit__(self, *exc_info: object) -> None:
            return None

        def __iter__(self) -> _OrderedScandir:
            return self

        def __next__(self) -> os.DirEntry[str]:
            return next(self._entries)

    monkeypatch.setattr(vault_module.os, "scandir", _OrderedScandir)
    monkeypatch.setattr(vault_module, "MAX_ENTRIES", 2)
    result = service(tmp_path).scan("w")
    assert result.partial
    assert result.partial_reasons == ("entry_limit",)
    assert len(result.entries) == 2
    assert result.omitted == 3


def test_a_search_that_hits_its_byte_budget_reports_an_exact_omitted_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for index in range(5):
        (tmp_path / f"n{index}.md").write_bytes(b"hit " + b"x" * 6)  # 10 bytes each, all match
    monkeypatch.setattr(vault_module, "MAX_SCAN_BYTES", 25)
    result = service(tmp_path).search("w", "hit")
    assert result.partial
    assert "byte_limit" in result.partial_reasons
    assert result.omitted == 3


def test_a_search_that_hits_its_hit_budget_reports_an_exact_omitted_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for index in range(5):
        (tmp_path / f"n{index}.md").write_text("hit", encoding="utf-8")
    monkeypatch.setattr(vault_module, "MAX_HITS", 1)
    result = service(tmp_path).search("w", "hit")
    assert result.partial
    assert result.partial_reasons == ("hit_limit",)
    assert result.omitted == 4


@pytest.mark.parametrize(
    ("note", "query", "exact"),
    [
        ("x ｶﾞｲﾄﾞ y", "ガイド", "ｶﾞｲﾄﾞ"),
        ("x ＡＢＣ y", "abc", "ＡＢＣ"),
        ("x Straße y", "strasse", "Straße"),
        ("x 猫 y", "猫", "猫"),
    ],
)
def test_search_folds_the_query_like_the_conversation_search(
    tmp_path: Path, note: str, query: str, exact: str
) -> None:
    (tmp_path / "n.md").write_text(note, encoding="utf-8")
    hits = service(tmp_path).search("w", query)
    assert [(hit.ref, hit.start, hit.end, hit.exact) for hit in hits] == [
        ("n.md", 2, 2 + len(exact), exact)
    ]


def test_search_refuses_one_ascii_character_but_not_two_or_one_cjk_character(
    tmp_path: Path,
) -> None:
    (tmp_path / "n.md").write_text("ab 猫", encoding="utf-8")
    vault = service(tmp_path)
    with pytest.raises(VaultError, match="invalid_query"):
        vault.search("w", "a")
    with pytest.raises(VaultError, match="invalid_query"):
        vault.search("w", " ａ ")
    assert len(vault.search("w", "ab")) == 1
    assert len(vault.search("w", "猫")) == 1


def test_search_stops_with_the_deadline_reason_when_the_match_runs_out_of_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("a.md", "b.md"):
        (tmp_path / name).write_text("hit", encoding="utf-8")

    def out_of_time(*_args: object) -> None:
        raise TimeoutError

    monkeypatch.setattr(vault_module, "match_span", out_of_time)
    result = service(tmp_path).search("w", "hit")
    assert result.partial_reasons == ("deadline",)
    assert result.omitted == 2


def test_safe_ref_splits_on_slash_and_refuses_doubled_or_dot_segments(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "b.md").write_text("kept", encoding="utf-8")
    vault = service(tmp_path)
    for ref in ("a//b.md", "a/./b.md", "/a/b.md"):
        with pytest.raises(VaultError, match="invalid_ref"):
            vault.read("w", ref)
    assert vault.read("w", "a/b.md").text == "kept"


@pytest.mark.skipif(sys.platform != "win32", reason="Win32 share modes")
async def test_held_handles_deny_external_note_write_and_ancestor_rename(tmp_path: Path) -> None:
    assert sys.platform == "win32"
    notes = tmp_path / "notes"
    notes.mkdir()
    target = notes / "note.md"
    target.write_text("stable", encoding="utf-8")
    moved = tmp_path / "moved"
    root = service(tmp_path)._root("w")
    code = r"""
import ctypes, sys
from ctypes import wintypes
k = ctypes.WinDLL('kernel32', use_last_error=True)
f = k.CreateFileW
f.restype = wintypes.HANDLE
f.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
              wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
h = f(sys.argv[1], 0x40000000, 7, None, 3, 0, None)
write_error = ctypes.get_last_error() if h == ctypes.c_void_p(-1).value else 0
m = k.MoveFileExW
m.restype = wintypes.BOOL
m.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD)
renamed = m(sys.argv[2], sys.argv[3], 0)
rename_error = ctypes.get_last_error() if not renamed else 0
print(write_error, rename_error, flush=True)
"""
    with (
        vault_module._open_ancestor_chain(root, ("notes",)) as parent,
        vault_module._open_note(parent / "note.md"),
    ):
        child = await spawn(
            [sys.executable, "-I", "-c", code, str(target), str(notes), str(moved)],
            cwd=Path.cwd(),
            env=child_env("vault-reader-test", os.environ),
        )
        stdout, stderr = await asyncio.wait_for(child.process.communicate(), timeout=5)
        child.release()
    assert stdout.strip() == b"32 32"
    assert stderr == b""
    assert target.read_text(encoding="utf-8") == "stable"
    assert not moved.exists()


def test_every_link_class_stops_at_the_delimiters_that_keep_extraction_linear() -> None:
    text_classes = (vault_module.WIKI_NAME, vault_module.WIKI_HEADING, vault_module.LINK_TEXT)
    for cls in text_classes:
        assert all(re.fullmatch(cls, ch) is None for ch in "[]"), cls
    assert all(re.fullmatch(vault_module.LINK_TARGET, ch) is None for ch in "()[] \t\n")
    # The patterns are built only from those classes, so neither can reintroduce backtracking.
    negated = re.compile(r"\[\^(?:\\.|[^\]\\])*\]")
    for pattern in (vault_module.WIKI, vault_module.MARKDOWN_LINK):
        classes = negated.findall(pattern.pattern)
        assert classes and set(classes) <= {*text_classes, vault_module.LINK_TARGET}, classes


def test_link_patterns_have_no_wildcards() -> None:
    # A wildcard atom (an unescaped `.`) outside the named classes can backtrack quadratically;
    # strip escape pairs (`\.`, `\[`, ...) first so only a real wildcard trips the assertion.
    for pattern in (vault_module.WIKI.pattern, vault_module.MARKDOWN_LINK.pattern):
        assert "." not in re.sub(r"\\.", "", pattern), pattern


@pytest.mark.parametrize(
    "text",
    [
        "[" * 65536,
        "\x1b[32mok\x1b[0m line\n" * 3856,
        "[a](b" * 13107,
        "[" + "a\n" * 32768,
        "[[a|" + "x\n" * 32768,
    ],
    ids=[
        "brackets",
        "ansi_log",
        "unclosed_links",
        "unclosed_bracket_newlines",
        "unclosed_wiki_alias_newlines",
    ],
)
def test_pathological_link_text_yields_no_targets(text: str) -> None:
    assert vault_module._link_targets(text) == ()


def test_ordinary_links_keep_their_targets() -> None:
    text = "[[a#b|c]] [x](<a.md>) [![i](img.png)](u.md) [[t]]\n[y](d/e.md#h) [[p|q]]"
    assert vault_module._link_targets(text) == ("a", "t", "p", "a.md", "img.png", "d/e.md")


def test_markdown_link_text_may_span_a_newline() -> None:
    assert vault_module._link_targets("[long\ntext](note.md)") == ("note.md",)


def test_wiki_alias_may_span_a_newline() -> None:
    assert vault_module._link_targets("[[n|al\nias]]") == ("n",)


def test_predicate_flags_control_characters_and_lone_surrogates() -> None:
    # NTFS refuses a control character in a name, so the predicate is tested directly.
    assert vault_module._unsafe_name("\x00bad.md")
    assert vault_module._unsafe_name("\x1fbad.md")
    assert vault_module._unsafe_name("\t.md")  # tab (9) is a control point below 32 too
    assert vault_module._unsafe_name("\ud800bad.md")
    assert vault_module._unsafe_name("\udfffbad.md")
    assert not vault_module._unsafe_name("good.md")


@pytest.mark.skipif(
    sys.platform != "win32", reason="only a UTF-16 file name holds a lone surrogate"
)
def test_a_lone_surrogate_note_name_is_omitted_as_unsafe_entry(tmp_path: Path) -> None:
    assert sys.platform == "win32"
    name = "\ud800bad.md"
    with open(tmp_path / name, "w", encoding="utf-8", errors="surrogatepass") as handle:
        handle.write("hidden")
    (tmp_path / "visible.md").write_text("visible", encoding="utf-8")
    vault = service(tmp_path)
    scan = vault.scan("w")
    assert [entry.ref for entry in scan.entries] == ["visible.md"]
    assert "unsafe_entry" in scan.partial_reasons


def test_history_refuses_a_bad_or_private_ref_before_asking_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: list[str] = []
    monkeypatch.setattr(vault_module, "note_history", lambda root, ref, **_: asked.append(ref))
    vault = service(tmp_path)
    for ref, code in [
        ("a/../b.md", "invalid_ref"),
        (".git/config", "private_path"),
        ("a//b", "invalid_ref"),
    ]:
        with pytest.raises(VaultError) as raised:
            vault.history("w", ref)
        assert raised.value.code == code
    assert asked == []


def test_history_asks_git_at_the_workspace_root_with_the_ref_and_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[Path, str, int]] = []

    def fake(root: Path, ref: str, *, limit: int) -> str:
        calls.append((root, ref, limit))
        return "history"

    monkeypatch.setattr(vault_module, "note_history", fake)
    assert service(tmp_path).history("w", "notes/a.md", limit=3) == "history"
    assert calls == [(Path(str(tmp_path.resolve())), "notes/a.md", 3)]


def _excerpt_text(root: Path, data: bytes, start: int, end: int) -> str:
    (root / "n.md").write_bytes(data)
    return service(root).excerpt("w", "n.md", start, end).text


def test_excerpt_of_lines_one_to_three_carries_text_block_size_and_revision(
    tmp_path: Path,
) -> None:
    data = b"alpha\nbeta\ngamma\n"
    note = tmp_path / "n.md"
    note.write_bytes(data)
    result = service(tmp_path).excerpt("w", "n.md", 1, 3)
    digest = hashlib.sha256(b"alpha\nbeta\ngamma").hexdigest()
    assert result.text == "alpha\nbeta\ngamma"
    assert result.block == (
        f"[vault excerpt] n.md, lines 1-3, sha256 {digest[:12]}\n```\nalpha\nbeta\ngamma\n```"
    )
    assert result.block_bytes == len(result.block.encode("utf-8"))
    assert result.revision == hashlib.sha256(data).hexdigest()
    assert result.part == {
        "kind": "vault_excerpt",
        "vault_id": "w",
        "ref": "n.md",
        "start_line": 1,
        "end_line": 3,
        "sha256": digest,
    }
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", result.modified_at)
    stamp = datetime.fromisoformat(result.modified_at).timestamp()
    assert abs(stamp - note.stat().st_mtime) < 0.002


def test_excerpt_of_a_note_with_a_pre_1970_mtime_stamps_the_real_time(tmp_path: Path) -> None:
    """A tar restore or a NAS round trip can leave a pre-1970 mtime, and a filesystem with no
    last-write time reports 1601 on Windows; `datetime.fromtimestamp` refuses both there."""
    note = tmp_path / "n.md"
    note.write_bytes(b"alpha\n")
    os.utime(note, (-315619200, -315619200))
    assert service(tmp_path).excerpt("w", "n.md", 1, 1).modified_at == "1960-01-01T00:00:00.000Z"
    assert vault_module._stamp(-11_644_473_600.0) == "1601-01-01T00:00:00.000Z"


@pytest.mark.parametrize("modified", [253_402_300_800.0, 1.8e12, -62_135_596_801.0])
def test_a_modification_time_outside_the_calendar_is_read_failed(modified: float) -> None:
    """A corrupt FILETIME can name a year `datetime` cannot hold; that is the reader's typed
    `read_failed`, not an untyped OverflowError past the route's and the dispatch's handlers."""
    with pytest.raises(VaultError) as raised:
        vault_module._stamp(modified)
    assert raised.value.code == "read_failed"


def test_excerpt_part_parses_back_and_a_middle_span_keeps_only_its_lines(tmp_path: Path) -> None:
    active = "5f0c9a52-7e3b-4c1d-8a47-0a9b6f1d2e34"
    (tmp_path / "n.md").write_bytes(b"one\ntwo\nthree\nfour\n")
    result = service(tmp_path, active_id=active).excerpt(active, "n.md", 2, 3)
    assert result.text == "two\nthree"
    assert set(result.part) == EXCERPT_KEYS
    part = parse_excerpt_part(result.part)
    assert (part.vault_id, part.ref, part.start_line, part.end_line) == (active, "n.md", 2, 3)
    assert result.block == render_excerpt_block("n.md", 2, 3, "two\nthree")


def test_excerpt_drops_the_carriage_return_of_a_crlf_note(tmp_path: Path) -> None:
    text = _excerpt_text(tmp_path, b"alpha\r\nbeta\r\ngamma\r\n", 1, 3)
    assert text == "alpha\nbeta\ngamma"
    assert "\r" not in text


def test_excerpt_of_a_bom_note_starts_without_the_bom(tmp_path: Path) -> None:
    (tmp_path / "n.md").write_bytes(b"\xef\xbb\xbfalpha\nbeta\n")
    first = service(tmp_path).excerpt("w", "n.md", 1, 1)
    assert first.text == "alpha"
    assert first.part["sha256"] == hashlib.sha256(b"alpha").hexdigest()
    assert "\ufeff" not in first.block


@pytest.mark.parametrize(
    ("data", "start", "end"),
    [
        (b"a\nb\nc", 1, 4),  # past the end
        (b"a\nb\nc\n", 1, 4),  # the final newline is no fourth line
        (b"a\nb\nc", 4, 4),
        (b"", 1, 1),  # an empty note has no line
    ],
)
def test_excerpt_past_the_end_of_the_note_is_invalid_range(
    tmp_path: Path, data: bytes, start: int, end: int
) -> None:
    (tmp_path / "n.md").write_bytes(data)
    with pytest.raises(VaultError) as raised:
        service(tmp_path).excerpt("w", "n.md", start, end)
    assert raised.value.code == "invalid_range"


@pytest.mark.parametrize(
    ("start", "end"),
    [(0, 1), (-1, 2), (3, 2), (1, 1_000_001), (1_000_001, 1_000_001), (1, 201), (5, 205)],
)
def test_excerpt_refuses_an_untrusted_range_before_it_reads_anything(
    tmp_path: Path, start: int, end: int
) -> None:
    # The note does not exist: a range refusal must come before the read.
    with pytest.raises(VaultError) as raised:
        service(tmp_path).excerpt("w", "absent.md", start, end)
    assert raised.value.code == "invalid_range"


@pytest.mark.parametrize("start", [True, 1.0, "1", None])
def test_excerpt_refuses_a_start_that_is_not_an_int(tmp_path: Path, start: object) -> None:
    with pytest.raises(VaultError) as raised:
        service(tmp_path).excerpt("w", "absent.md", cast(int, start), 2)
    assert raised.value.code == "invalid_range"


def test_excerpt_of_two_hundred_lines_is_allowed_and_two_hundred_and_one_is_not(
    tmp_path: Path,
) -> None:
    (tmp_path / "n.md").write_text(
        "\n".join(f"line {n}" for n in range(1, 301)), encoding="utf-8", newline="\n"
    )
    vault = service(tmp_path)
    assert vault.excerpt("w", "n.md", 11, 210).text.count("\n") == 199
    with pytest.raises(VaultError) as raised:
        vault.excerpt("w", "n.md", 11, 211)
    assert raised.value.code == "invalid_range"


def test_excerpt_over_32_kib_is_too_large_and_exactly_32_kib_is_allowed(tmp_path: Path) -> None:
    (tmp_path / "n.md").write_text(
        "\n".join(["x" * 1_100] * 30), encoding="utf-8", newline="\n"
    )  # 33,029 bytes
    vault = service(tmp_path)
    with pytest.raises(VaultError) as raised:
        vault.excerpt("w", "n.md", 1, 30)
    assert raised.value.code == "excerpt_too_large"
    assert len(vault.excerpt("w", "n.md", 1, 29).text.encode("utf-8")) == 29 * 1_100 + 28
    (tmp_path / "edge.md").write_text("x" * 32_768, encoding="utf-8")
    assert len(vault.excerpt("w", "edge.md", 1, 1).text) == 32_768
    (tmp_path / "edge.md").write_text("x" * 32_769, encoding="utf-8")
    with pytest.raises(VaultError) as too_large:
        vault.excerpt("w", "edge.md", 1, 1)
    assert too_large.value.code == "excerpt_too_large"


def test_excerpt_size_counts_utf8_bytes_not_characters(tmp_path: Path) -> None:
    (tmp_path / "n.md").write_text("あ" * 11_000, encoding="utf-8")  # 11,000 chars, 33,000 bytes
    with pytest.raises(VaultError) as raised:
        service(tmp_path).excerpt("w", "n.md", 1, 1)
    assert raised.value.code == "excerpt_too_large"


def test_excerpt_of_a_note_with_a_four_backtick_run_fences_with_five(tmp_path: Path) -> None:
    (tmp_path / "n.md").write_text("a\n````\nb\n", encoding="utf-8", newline="\n")
    block = service(tmp_path).excerpt("w", "n.md", 1, 3).block
    lines = block.split("\n")
    assert lines[1] == lines[-1] == "`````"
    assert lines[2:-1] == ["a", "````", "b"]


def test_excerpt_of_another_vault_is_not_active_and_reads_nothing(tmp_path: Path) -> None:
    (tmp_path / "n.md").write_text("a", encoding="utf-8")
    with pytest.raises(VaultError) as raised:
        service(tmp_path, active_id="other").excerpt("w", "n.md", 1, 1)
    assert raised.value.code == "not_active_vault"


@pytest.mark.parametrize(
    ("ref", "code"),
    [(".git/x.md", "private_path"), ("a/../b.md", "invalid_ref"), ("gone.md", "missing_note")],
)
def test_excerpt_refuses_a_bad_ref_with_the_read_error(tmp_path: Path, ref: str, code: str) -> None:
    with pytest.raises(VaultError) as raised:
        service(tmp_path).excerpt("w", ref, 1, 1)
    assert raised.value.code == code


def test_excerpt_at_reads_through_a_root_active_root_resolved_without_resolving_again(
    tmp_path: Path,
) -> None:
    (tmp_path / "n.md").write_text("alpha\nbeta\n", encoding="utf-8", newline="\n")
    vault = service(tmp_path)
    root = vault.active_root("w")

    assert vault.excerpt_at(root, "w", "n.md", 2, 2) == vault.excerpt("w", "n.md", 2, 2)


def test_active_root_refuses_a_vault_that_is_not_the_active_one(tmp_path: Path) -> None:
    with pytest.raises(VaultError) as raised:
        service(tmp_path, active_id="other").active_root("w")
    assert raised.value.code == "not_active_vault"


def test_excerpt_at_refuses_an_untrusted_range_before_it_reads_anything(tmp_path: Path) -> None:
    vault = service(tmp_path)
    with pytest.raises(VaultError) as raised:
        vault.excerpt_at(vault.active_root("w"), "w", "absent.md", 0, 1)
    assert raised.value.code == "invalid_range"


def test_selected_projection_uses_existing_read_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = (
        b'\xef\xbb\xbf---\r\ntitle: "SKILL"\r\ntags: ["One", "two"]\r\n'
        b"type: reference\r\n---\r\n# Body\r\n[[target]]\r\n"
    )
    path = tmp_path / "SKILL.md"
    path.write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    opened, read_data, projected, hashed = [], [], [], []
    opener_owner = vault_module if sys.platform == "win32" else os
    opener_name = "_CreateFileW" if sys.platform == "win32" else "open"
    original_open = getattr(opener_owner, opener_name)
    reader_name = "_read_note_handle" if sys.platform == "win32" else "_read_note_at"
    original_read = getattr(vault_module, reader_name)
    original_hash = hashlib.sha256

    def open_note(name, *args, **kwargs):
        if os.fspath(name) in {str(path), path.name}:
            opened.append(path.name)
        return original_open(name, *args, **kwargs)

    def read_note(*args, **kwargs):
        data, modified = original_read(*args, **kwargs)
        read_data.append(data)
        return data, modified

    def project(prefix, *, complete):
        projected.append((prefix, complete))
        return extract_display(prefix, complete=complete)

    def hash_note(data):
        hashed.append(data)
        return original_hash(data)

    monkeypatch.setattr(opener_owner, opener_name, open_note)
    monkeypatch.setattr(vault_module, reader_name, read_note)
    monkeypatch.setattr(vault_module, "extract_display", project, raising=False)
    monkeypatch.setattr(hashlib, "sha256", hash_note)
    vault = service(tmp_path)
    listing = vault.scan("w")
    assert [(entry.ref, entry.title, entry.bytes) for entry in listing.entries] == [
        ("SKILL.md", "SKILL", len(raw))
    ]
    assert listing.scanned_bytes == 0 and not listing.partial
    assert opened == read_data == projected == hashed == []
    inspection = vault.inspect("w", "SKILL.md")
    note = inspection.note
    wire = asdict(note)
    projection = wire.pop("display_metadata", None)
    assert wire == {
        "ref": "SKILL.md",
        "title": "SKILL",
        "text": raw.decode("utf-8-sig"),
        "source": raw.decode("utf-8"),
        "byte_size": len(raw),
        "sha256": digest,
        "frontmatter": {"title": '"SKILL"', "tags": '["One", "two"]', "type": "reference"},
        "links": ("target",),
        "frontmatter_error": None,
    }
    assert note.source.encode() == path.read_bytes() == raw
    assert [(link.target, link.state) for link in inspection.links] == [("target", "missing")]
    assert opened == ["SKILL.md"] and read_data == hashed == [raw]
    assert projected == [(read_data[0], True)]
    assert projection == {
        "state": "ready",
        "frontmatter_state": "ready",
        "heading_state": "ready",
        "display_title": "SKILL",
        "title_source": "title",
        "tags": ["One", "two"],
        "type": "reference",
        "status": None,
    }


@pytest.mark.parametrize(
    "raw,frontmatter,tags",
    [
        (b"---\ntags: []\n---\n" + b"x" * 32768 + b"\n# Beyond", "ready", []),
        (b"body\n" + b"x" * (32768 - 6) + "名\n# Beyond".encode(), "absent", []),
        (b"---\ntitle: Kept\n" + b" " * 32768 + b"\n---\n# Beyond", "truncated", None),
        (b"body\n" * 256 + b"# Beyond", "absent", []),
    ],
    ids=["byte_cut", "utf8_cut", "header_cut", "line_cut"],
)
def test_selected_projection_limits_only_metadata_not_full_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raw: bytes, frontmatter: str, tags
) -> None:
    (tmp_path / "long.md").write_bytes(raw)
    projected = []

    def project(prefix, *, complete):
        projected.append((prefix, complete))
        return extract_display(prefix, complete=complete)

    monkeypatch.setattr(vault_module, "extract_display", project, raising=False)
    note = service(tmp_path).read("w", "long.md")
    assert note.source.encode() == note.text.encode() == raw
    assert note.byte_size == len(raw) and note.sha256 == hashlib.sha256(raw).hexdigest()
    assert projected == [(raw[:32768], len(raw) <= 32768)]
    assert asdict(note).get("display_metadata") == {
        "state": "truncated",
        "frontmatter_state": frontmatter,
        "heading_state": "truncated",
        "display_title": None,
        "title_source": None,
        "tags": tags,
        "type": None,
        "status": None,
    }


@pytest.mark.parametrize(
    "raw,fields,error",
    [
        (b"---\ntitle: Kept\nbad line\n---\n# Body\n", {"title": "Kept"}, "invalid_line"),
        (b"---\ntitle: 'Authored'\n---\n# Body\n", {"title": "'Authored'"}, None),
    ],
    ids=["malformed_header", "unsupported_quote"],
)
def test_unsupported_projection_keeps_the_legacy_note_readable(
    tmp_path: Path, raw: bytes, fields: dict[str, str], error: str | None
) -> None:
    path = tmp_path / "odd.md"
    path.write_bytes(raw)
    note = service(tmp_path).read("w", "odd.md")
    assert note.frontmatter == fields and note.frontmatter_error == error
    assert note.source.encode() == note.text.encode() == path.read_bytes() == raw
    assert note.sha256 == hashlib.sha256(raw).hexdigest()
    projection = asdict(note).get("display_metadata")
    assert projection == {
        "state": "unsupported",
        "frontmatter_state": "unsupported",
        "heading_state": "unsupported",
        "display_title": None,
        "title_source": None,
        "tags": None,
        "type": None,
        "status": None,
    }


def test_full_invalid_encoding_after_projection_limit_still_refuses_the_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "bad.md").write_bytes(b"body\n" + b"x" * 32768 + b"\xff")
    monkeypatch.setattr(
        vault_module,
        "extract_display",
        lambda *_args, **_kwargs: pytest.fail("decoded first"),
        raising=False,
    )
    with pytest.raises(VaultError) as raised:
        service(tmp_path).read("w", "bad.md")
    assert raised.value.code == "invalid_encoding"


def test_projection_encoding_failure_uses_the_existing_typed_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "valid.md").write_bytes(b"# Valid\n")

    def invalid_projection(*_args, **_kwargs):
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "synthetic projection failure")

    monkeypatch.setattr(vault_module, "extract_display", invalid_projection, raising=False)
    with pytest.raises(VaultError) as raised:
        service(tmp_path).read("w", "valid.md")
    assert raised.value.code == "invalid_encoding"


@pytest.mark.parametrize("error", [None, "invalid_line"])
def test_note_projection_is_optional_after_the_legacy_positional_fields(error: str | None) -> None:
    note = vault_module.Note("n.md", "n", "text", "text", 4, "0" * 64, {}, (), error)
    assert note.frontmatter_error == error
    assert getattr(note, "display_metadata", "missing") is None
