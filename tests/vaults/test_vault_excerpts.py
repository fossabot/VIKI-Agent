"""What a dispatch does with excerpt parts: the re-read, the order and the block budget.

The vault here is the real `VaultService` over a temporary folder, so a refusal is the reader's own
and a stale part is a note that really changed on disk.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from viki_agent.conversations.turn_parts import (
    EXCERPT_BLOCK_CHARS,
    ExcerptPart,
    render_excerpt_block,
)
from viki_agent.domain import CanonicalRoot
from viki_agent.turn_input import MAX_TEXT_BYTES
from viki_agent.vaults.vault import Note, VaultError, VaultService
from viki_agent.vaults.vault_excerpts import check_block_budget, joined_text, resolve_excerpts
from viki_agent.workspaces import WorkspaceService, root_key

ACTIVE = "5f0c9a52-7e3b-4c1d-8a47-0a9b6f1d2e34"
OTHER = "6a1d0b63-8f4c-4d2e-9b58-1b0c7e2e3f45"


def vault(
    root: Path,
    *,
    active_id: str = ACTIVE,
    resolves: list[str] | None = None,
    root_available: bool = True,
) -> VaultService:
    """The reader over `root`. `resolves` collects one entry per root resolve, so a test can count
    what reading N parts costs; `root_available` False is a vault that no longer resolves."""
    info = os.stat(root)
    canonical = CanonicalRoot(
        key=root_key(root.resolve()),
        display=str(root.resolve()),
        physical_id=f"{info.st_dev}:{info.st_ino}",
        aliased=False,
    )
    workspace = SimpleNamespace(root=canonical)

    def current_root(_workspace: object) -> CanonicalRoot | None:
        if resolves is not None:
            resolves.append(canonical.key)
        return canonical if root_available else None

    owner = SimpleNamespace(
        get=lambda _id: workspace,
        current_root=current_root,
        resolve_active=lambda: SimpleNamespace(workspace=SimpleNamespace(workspace_id=active_id)),
    )
    return VaultService(cast(WorkspaceService, owner))


def part(ref: str, start: int, end: int, text: str, vault_id: str = ACTIVE) -> ExcerptPart:
    """The part a chip builds from the excerpt route's answer: the span, and the span's digest."""
    return ExcerptPart(vault_id, ref, start, end, hashlib.sha256(text.encode("utf-8")).hexdigest())


def test_two_parts_resolve_in_their_own_order_with_the_attached_span(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("one\ntwo\nthree\n", encoding="utf-8", newline="\n")
    (tmp_path / "b.md").write_text("alpha\nbeta\n", encoding="utf-8", newline="\n")
    parts = [part("b.md", 2, 2, "beta"), part("a.md", 1, 2, "one\ntwo")]

    blocks = resolve_excerpts(vault(tmp_path), ACTIVE, parts)

    assert blocks == [
        render_excerpt_block("b.md", 2, 2, "beta"),
        render_excerpt_block("a.md", 1, 2, "one\ntwo"),
    ]


def test_a_note_edited_after_the_chip_is_stale_with_its_index(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("one\n", encoding="utf-8", newline="\n")
    (tmp_path / "b.md").write_text("beta\n", encoding="utf-8", newline="\n")
    parts = [part("a.md", 1, 1, "one"), part("b.md", 1, 1, "beta")]
    (tmp_path / "b.md").write_text("gamma\n", encoding="utf-8", newline="\n")

    with pytest.raises(VaultError) as raised:
        resolve_excerpts(vault(tmp_path), ACTIVE, parts)

    assert raised.value.code == "excerpt_stale"
    assert raised.value.extra == {"index": 1}


def test_a_note_shortened_under_the_span_is_stale_not_a_bad_request(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("one\ntwo\nthree\n", encoding="utf-8", newline="\n")
    attached = part("a.md", 2, 3, "two\nthree")
    (tmp_path / "a.md").write_text("one\n", encoding="utf-8", newline="\n")

    with pytest.raises(VaultError) as raised:
        resolve_excerpts(vault(tmp_path), ACTIVE, [attached])

    assert (raised.value.code, raised.value.extra) == ("excerpt_stale", {"index": 0})


def test_a_part_of_another_vault_is_refused_before_any_note_is_read(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("one\n", encoding="utf-8", newline="\n")
    resolves: list[str] = []
    parts = [part("a.md", 1, 1, "one"), part("a.md", 1, 1, "one", vault_id=OTHER)]

    with pytest.raises(VaultError) as raised:
        resolve_excerpts(vault(tmp_path, resolves=resolves), ACTIVE, parts)

    assert (raised.value.code, raised.value.extra) == ("excerpt_wrong_vault", {"index": 1})
    assert len(resolves) == 1  # the first part's read, never the foreign part's


def test_a_vault_whose_root_no_longer_resolves_is_unavailable(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("one\n", encoding="utf-8", newline="\n")

    with pytest.raises(VaultError) as raised:
        resolve_excerpts(vault(tmp_path, root_available=False), ACTIVE, [part("a.md", 1, 1, "one")])

    assert (raised.value.code, raised.value.extra) == ("excerpt_unavailable", {"index": 0})


def test_a_note_that_is_gone_is_unavailable_with_its_index(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("one\n", encoding="utf-8", newline="\n")
    parts = [part("a.md", 1, 1, "one"), part("gone.md", 1, 1, "one")]

    with pytest.raises(VaultError) as raised:
        resolve_excerpts(vault(tmp_path), ACTIVE, parts)

    assert (raised.value.code, raised.value.extra) == ("excerpt_unavailable", {"index": 1})


def test_eight_parts_cost_one_root_resolve_and_one_read_each(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The root is resolved once for the whole call; each part is one bounded note read."""
    for index in range(8):
        (tmp_path / f"n{index}.md").write_text(f"line {index}\n", encoding="utf-8", newline="\n")
    resolves: list[str] = []
    reads: list[str] = []
    read_stamped = VaultService._read_stamped  # pyright: ignore[reportPrivateUsage]

    def counting_read(self: VaultService, root: CanonicalRoot, ref: str) -> tuple[Note, float]:
        reads.append(ref)
        return read_stamped(self, root, ref)

    monkeypatch.setattr(VaultService, "_read_stamped", counting_read)
    parts = [part(f"n{index}.md", 1, 1, f"line {index}") for index in range(8)]

    blocks = resolve_excerpts(vault(tmp_path, resolves=resolves), ACTIVE, parts)

    assert len(blocks) == 8
    assert len(resolves) == 1
    assert len(reads) == 8


def test_no_parts_reads_nothing_and_returns_no_block(tmp_path: Path) -> None:
    resolves: list[str] = []

    assert resolve_excerpts(vault(tmp_path, resolves=resolves), ACTIVE, []) == []
    assert resolves == []


def test_the_owner_text_comes_first_and_empty_pieces_are_dropped() -> None:
    assert joined_text("Recap", ["[block a]", "[block b]"]) == "Recap\n[block a]\n[block b]"
    assert joined_text("", ["[block a]"]) == "[block a]"
    assert joined_text("Recap", []) == "Recap"
    assert joined_text("", []) == ""


def test_blocks_at_the_character_budget_pass_and_one_character_over_is_refused() -> None:
    half = EXCERPT_BLOCK_CHARS // 2
    check_block_budget("Recap", ["a" * half, "b" * half])

    with pytest.raises(VaultError) as raised:
        check_block_budget("Recap", ["a" * half, "b" * (half + 1)])

    assert raised.value.code == "excerpts_too_large"
    assert raised.value.extra == {}


def test_a_joined_text_over_the_turn_byte_limit_is_refused() -> None:
    text = "a" * (MAX_TEXT_BYTES - 10)
    check_block_budget(text, ["b" * 9])  # the newline joined_text adds makes this exactly the cap

    with pytest.raises(VaultError) as raised:
        check_block_budget(text, ["b" * 10])

    assert raised.value.code == "excerpts_too_large"
    assert len(joined_text(text, ["b" * 9]).encode("utf-8")) == MAX_TEXT_BYTES
