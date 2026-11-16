"""Every local Markdown link in published docs resolves to a published path."""

from __future__ import annotations

from pathlib import Path

import public_docs
import pytest

REPO = Path(__file__).resolve().parents[1]


def test_relative_markdown_links_resolve() -> None:
    failures = public_docs.link_failures(REPO, public_docs.inventory(REPO))
    assert not failures, f"unresolved Markdown links: {failures}"


def test_strip_code_removes_code_spans_and_fences() -> None:
    text = (
        "See [ok](real.md) and `[x](Note(1).md)` inline.\n"
        "```\n[block](missing.md)\n```\n"
        "But [broken](missing.md) still counts.\n"
    )
    targets = public_docs.LINK_TARGET.findall(public_docs.strip_code(text))
    assert targets == ["real.md", "missing.md"]


def test_inventory_excludes_existing_ignored_target_and_includes_new_page(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    docs = tmp_path / "docs"
    internal = docs / "internal"
    internal.mkdir(parents=True)
    (docs / "new.md").write_text("# New\n", encoding="utf-8")
    (internal / "hidden.md").write_text("# Hidden\n", encoding="utf-8")
    (tmp_path / "README.md").write_text(
        "[new](docs/new.md) [hidden](docs/internal/hidden.md)\n", encoding="utf-8"
    )
    calls: list[tuple[Path, tuple[str, ...]]] = []

    def git(repo: Path, *args: str) -> str:
        calls.append((repo, args))
        return "README.md\0docs/new.md\0"

    monkeypatch.setattr(public_docs.winproc, "git", git)
    inventory = public_docs.inventory(tmp_path)
    assert calls == [(tmp_path, ("ls-files", "-z", "--cached", "--others", "--exclude-standard"))]
    assert inventory.docs == (tmp_path / "README.md", docs / "new.md")
    assert public_docs.link_failures(tmp_path, inventory) == [
        ("README.md", "docs/internal/hidden.md")
    ]


def test_inventory_directory_requires_published_descendant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    docs = tmp_path / "docs"
    (docs / "internal").mkdir(parents=True)
    (docs / "new").mkdir()
    (docs / "internal" / "hidden.md").write_text("# Hidden\n", encoding="utf-8")
    (docs / "new" / "page.md").write_text("# New\n", encoding="utf-8")
    (tmp_path / "README.md").write_text(
        "[new](docs/new) [hidden](docs/internal)\n", encoding="utf-8"
    )
    monkeypatch.setattr(
        public_docs.winproc,
        "git",
        lambda repo, *args: "README.md\0docs/new/page.md\0",
    )
    assert public_docs.link_failures(tmp_path, public_docs.inventory(tmp_path)) == [
        ("README.md", "docs/internal")
    ]


def test_inventory_scans_tracked_internal_markdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    internal = tmp_path / "docs" / "internal"
    internal.mkdir(parents=True)
    (internal / "tracked.md").write_text("[lost](missing.md)\n", encoding="utf-8")
    (internal / "untracked.md").write_text("[lost](other.md)\n", encoding="utf-8")
    monkeypatch.setattr(
        public_docs.winproc,
        "git",
        lambda repo, *args: "docs/internal/tracked.md\0",
    )
    published = public_docs.inventory(tmp_path)
    assert published.docs == (internal / "tracked.md",)
    assert public_docs.link_failures(tmp_path, published) == [
        ("docs/internal/tracked.md", "missing.md")
    ]


def test_angled_paths_and_link_titles_check_published_targets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    docs = tmp_path / "docs"
    internal = docs / "internal"
    internal.mkdir(parents=True)
    for path in (
        docs / "new notes.md",
        docs / "new.md",
        internal / "private notes.md",
        internal / "private.md",
    ):
        path.write_text("# Note\n", encoding="utf-8")
    (tmp_path / "README.md").write_text(
        "[hidden](<docs/internal/private notes.md>)\n"
        '[hidden](docs/internal/private.md "title")\n'
        "[public](<docs/new notes.md> 'title')\n"
        '[public](docs/new.md "title")\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        public_docs.winproc,
        "git",
        lambda repo, *args: "README.md\0docs/new notes.md\0docs/new.md\0",
    )
    assert public_docs.link_failures(tmp_path, public_docs.inventory(tmp_path)) == [
        ("README.md", "docs/internal/private notes.md"),
        ("README.md", "docs/internal/private.md"),
    ]
