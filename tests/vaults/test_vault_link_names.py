"""A bare `[[Name]]` wikilink resolves by note name from any folder, not only the vault root."""

from __future__ import annotations

from pathlib import Path

from tests.vaults.test_vault import service


def _write(root: Path, ref: str, text: str) -> None:
    path = root / ref
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_a_bare_link_from_a_subfolder_resolves_in_another_folder(tmp_path: Path) -> None:
    _write(tmp_path, "30-knowledge/a.md", "[[B]]")
    _write(tmp_path, "40-sources/B.md", "Target")
    vault = service(tmp_path)
    links = vault.inspect("w", "30-knowledge/a.md").links
    assert [(link.state, link.candidates) for link in links] == [("resolved", ("40-sources/B.md",))]


def test_a_same_folder_exact_match_wins_over_the_name_lookup(tmp_path: Path) -> None:
    _write(tmp_path, "30-knowledge/a.md", "[[B]]")
    _write(tmp_path, "30-knowledge/B.md", "Same folder")
    _write(tmp_path, "40-sources/B.md", "Other folder")
    vault = service(tmp_path)
    links = vault.inspect("w", "30-knowledge/a.md").links
    assert [(link.state, link.candidates) for link in links] == [
        ("resolved", ("30-knowledge/B.md",))
    ]


def test_two_same_named_notes_elsewhere_are_ambiguous(tmp_path: Path) -> None:
    _write(tmp_path, "30-knowledge/a.md", "[[B]]")
    _write(tmp_path, "40-sources/B.md", "One")
    _write(tmp_path, "50-maps/B.md", "Two")
    vault = service(tmp_path)
    links = vault.inspect("w", "30-knowledge/a.md").links
    assert [(link.state, link.candidates) for link in links] == [
        ("ambiguous", ("40-sources/B.md", "50-maps/B.md"))
    ]


def test_a_slash_target_stays_relative_even_if_a_same_stem_exists_elsewhere(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "30-knowledge/a.md", "[[sub/B]]")
    _write(tmp_path, "40-sources/B.md", "Elsewhere")
    vault = service(tmp_path)
    links = vault.inspect("w", "30-knowledge/a.md").links
    assert [(link.state, link.candidates) for link in links] == [("missing", ())]


def test_dot_and_dotdot_targets_never_use_the_name_lookup(tmp_path: Path) -> None:
    _write(tmp_path, "30-knowledge/sub/a.md", "[[.]] [[..]]")
    vault = service(tmp_path)
    links = vault.inspect("w", "30-knowledge/sub/a.md").links
    assert [(link.state, link.candidates) for link in links] == [
        ("missing", ()),
        ("missing", ()),
    ]


def test_a_note_under_an_excluded_folder_is_never_a_candidate(tmp_path: Path) -> None:
    _write(tmp_path, "30-knowledge/a.md", "[[B]]")
    _write(tmp_path, ".claude/B.md", "Hidden")
    vault = service(tmp_path)
    links = vault.inspect("w", "30-knowledge/a.md").links
    assert [(link.state, link.candidates) for link in links] == [("missing", ())]


def test_backlinks_includes_a_bare_link_from_another_folder(tmp_path: Path) -> None:
    _write(tmp_path, "30-knowledge/a.md", "[[B]]")
    _write(tmp_path, "40-sources/B.md", "Target")
    vault = service(tmp_path)
    backlinks = vault.backlinks("w", "40-sources/B.md")
    assert [item.source for item in backlinks] == ["30-knowledge/a.md"]


def test_overview_counts_a_maps_bare_link_to_another_folder(tmp_path: Path) -> None:
    _write(tmp_path, "50-maps/Home.md", "# Home\n[[B]]\n")
    _write(tmp_path, "40-sources/B.md", "Target")
    vault = service(tmp_path)
    rows = {note.ref: note for note in vault.overview("w").notes}
    assert rows["50-maps/Home.md"].links == 1


def test_a_dotdot_target_from_a_subfolder_still_resolves_the_single_root_name(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "30-knowledge/a.md", "[[../B]]")
    _write(tmp_path, "40-sources/B.md", "Target")
    vault = service(tmp_path)
    links = vault.inspect("w", "30-knowledge/a.md").links
    assert [(link.state, link.candidates) for link in links] == [("resolved", ("40-sources/B.md",))]


def test_a_double_dotdot_target_from_a_nested_subfolder_still_resolves_the_single_root_name(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "30-knowledge/sub/c.md", "[[../../B]]")
    _write(tmp_path, "40-sources/B.md", "Target")
    vault = service(tmp_path)
    links = vault.inspect("w", "30-knowledge/sub/c.md").links
    assert [(link.state, link.candidates) for link in links] == [("resolved", ("40-sources/B.md",))]


def test_a_dot_target_from_the_root_still_resolves_by_name(tmp_path: Path) -> None:
    _write(tmp_path, "root.md", "[[./B]]")
    _write(tmp_path, "40-sources/B.md", "Target")
    vault = service(tmp_path)
    links = vault.inspect("w", "root.md").links
    assert [(link.state, link.candidates) for link in links] == [("resolved", ("40-sources/B.md",))]


def test_a_folder_and_dotdot_target_from_the_root_still_resolves_by_name(tmp_path: Path) -> None:
    _write(tmp_path, "root.md", "[[x/../B]]")
    _write(tmp_path, "40-sources/B.md", "Target")
    vault = service(tmp_path)
    links = vault.inspect("w", "root.md").links
    assert [(link.state, link.candidates) for link in links] == [("resolved", ("40-sources/B.md",))]


def test_a_trailing_slash_target_from_the_root_still_resolves_by_name(tmp_path: Path) -> None:
    _write(tmp_path, "root.md", "[[B/]]")
    _write(tmp_path, "40-sources/B.md", "Target")
    vault = service(tmp_path)
    links = vault.inspect("w", "root.md").links
    assert [(link.state, link.candidates) for link in links] == [("resolved", ("40-sources/B.md",))]


def test_a_bare_name_containing_a_dot_still_resolves_by_name(tmp_path: Path) -> None:
    _write(tmp_path, "30-knowledge/a.md", "[[Model 5.1 docs]]")
    _write(tmp_path, "40-sources/Model 5.1 docs.md", "Elsewhere")
    vault = service(tmp_path)
    links = vault.inspect("w", "30-knowledge/a.md").links
    assert [(link.state, link.candidates) for link in links] == [
        ("resolved", ("40-sources/Model 5.1 docs.md",))
    ]


def test_a_bare_name_containing_a_dot_prefers_the_same_folder_exact_match(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "30-knowledge/a.md", "[[Model 5.1 docs]]")
    _write(tmp_path, "30-knowledge/Model 5.1 docs.md", "Same folder")
    _write(tmp_path, "40-sources/Model 5.1 docs.md", "Other folder")
    vault = service(tmp_path)
    links = vault.inspect("w", "30-knowledge/a.md").links
    assert [(link.state, link.candidates) for link in links] == [
        ("resolved", ("30-knowledge/Model 5.1 docs.md",))
    ]


def test_a_non_note_extension_target_stays_missing(tmp_path: Path) -> None:
    _write(tmp_path, "30-knowledge/a.md", "[[diagram.png]]")
    vault = service(tmp_path)
    links = vault.inspect("w", "30-knowledge/a.md").links
    assert [(link.state, link.candidates) for link in links] == [("missing", ())]


def test_a_target_with_the_note_suffix_resolves_exactly_as_before(tmp_path: Path) -> None:
    _write(tmp_path, "30-knowledge/a.md", "[[Note.md]]")
    _write(tmp_path, "30-knowledge/Note.md", "Same folder")
    vault = service(tmp_path)
    links = vault.inspect("w", "30-knowledge/a.md").links
    assert [(link.state, link.candidates) for link in links] == [
        ("resolved", ("30-knowledge/Note.md",))
    ]
