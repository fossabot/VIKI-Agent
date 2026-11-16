"""The POSIX vault reader: every open is relative to a directory descriptor, never a path."""

from __future__ import annotations

import os
import sys
import time
import unicodedata
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from viki_agent.domain import CanonicalRoot
from viki_agent.vaults import vault as vault_module
from viki_agent.vaults.vault import MAX_NOTE_BYTES, VaultError, VaultService
from viki_agent.workspaces import WorkspaceService, root_key

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="the POSIX descriptor walk")


def canonical(root: Path) -> CanonicalRoot:
    info = os.stat(root)
    return CanonicalRoot(
        key=root_key(root),
        display=str(root),
        physical_id=f"{info.st_dev}:{info.st_ino}",
        aliased=False,
    )


def service(root: Path) -> VaultService:
    workspace = SimpleNamespace(root=canonical(root.resolve()))
    owner = SimpleNamespace(get=lambda _id: workspace, current_root=lambda _ws: workspace.root)
    return VaultService(cast(WorkspaceService, owner))


def refs(vault: VaultService) -> list[str]:
    return [entry.ref for entry in vault.scan("w").entries]


def test_a_small_tree_is_scanned_and_read(tmp_path: Path) -> None:
    (tmp_path / "a" / "b").mkdir(parents=True)
    (tmp_path / "top.md").write_text("top", encoding="utf-8")
    (tmp_path / "a" / "b" / "deep.md").write_text("[[../../top]]", encoding="utf-8")
    (tmp_path / "a" / "image.png").write_bytes(b"png")
    vault = service(tmp_path)
    scan = vault.scan("w")
    assert [entry.ref for entry in scan.entries] == ["a/b/deep.md", "top.md"]
    assert not scan.partial
    assert scan.entries[1].bytes == 3
    note = vault.read("w", "a/b/deep.md")
    assert (note.text, note.links) == ("[[../../top]]", ("../../top",))
    assert vault.inspect("w", "a/b/deep.md").links[0].candidates == ("top.md",)
    with pytest.raises(VaultError, match="missing_note"):
        vault.read("w", "a/gone.md")


def test_a_symlinked_note_and_folder_are_omitted_and_refused(tmp_path: Path) -> None:
    vault_root, outside = tmp_path / "vault", tmp_path / "outside"
    vault_root.mkdir()
    outside.mkdir()
    (outside / "x.md").write_text("outside", encoding="utf-8")
    (vault_root / "note.md").symlink_to(outside / "x.md")
    (vault_root / "folder").symlink_to(outside, target_is_directory=True)
    vault = service(vault_root)
    scan = vault.scan("w")
    assert scan.entries == ()
    assert (scan.partial_reasons, scan.omitted) == (("unsafe_entry",), 2)
    for ref in ("note.md", "folder/x.md"):
        with pytest.raises(VaultError, match="unsafe_path"):
            vault.read("w", ref)


def test_a_symlink_in_the_root_chain_makes_the_root_unavailable(tmp_path: Path) -> None:
    assert sys.platform != "win32"
    (tmp_path / "real").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "real", target_is_directory=True)
    (tmp_path / "real" / "sub").mkdir()
    for root in ("link", "link/sub"):  # the link as the root, then as an ancestor of it
        through_link = canonical(tmp_path.resolve() / root)
        with (
            pytest.raises(VaultError, match="root_unavailable"),
            vault_module._open_root_fd(through_link),
        ):
            pass


def test_a_root_whose_key_is_not_its_display_key_has_changed(tmp_path: Path) -> None:
    (tmp_path / "n.md").write_text("n", encoding="utf-8")
    vault = service(tmp_path)
    stale = replace(vault._root("w"), key="stale")  # the right folder, a stored key of another
    with pytest.raises(VaultError, match="root_changed"):
        vault._scan_root(stale, time.monotonic() + 1)
    with pytest.raises(VaultError, match="root_changed"):
        vault._read_root(stale, "n.md")


@pytest.mark.skipif(
    sys.platform != "linux" or os.geteuid() == 0,
    reason="Linux walks a traverse-only parent; root ignores mode bits",
)
def test_a_vault_under_a_traverse_only_parent_is_readable_on_linux(tmp_path: Path) -> None:
    assert sys.platform == "linux"
    parent = tmp_path / "home"
    (parent / "vault").mkdir(parents=True)
    (parent / "vault" / "n.md").write_text("n", encoding="utf-8")
    vault = service(parent / "vault")
    parent.chmod(0o311)  # search without list, as a shared host's 0711 home
    try:
        assert refs(vault) == ["n.md"]
        assert vault.read("w", "n.md").text == "n"
    finally:
        parent.chmod(0o700)


def test_a_fifo_named_like_a_note_blocks_neither_scan_nor_read(tmp_path: Path) -> None:
    assert sys.platform != "win32"
    os.mkfifo(tmp_path / "x.md")
    (tmp_path / "ok.md").write_text("ok", encoding="utf-8")
    vault = service(tmp_path)
    scan = vault.scan("w")
    assert [entry.ref for entry in scan.entries] == ["ok.md"]
    assert scan.partial_reasons == ("unsafe_entry",)
    with pytest.raises(VaultError, match="unsafe_path"):
        vault.read("w", "x.md")


def test_a_hard_linked_note_is_omitted_and_refused(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("same", encoding="utf-8")
    os.link(tmp_path / "a.md", tmp_path / "b.md")
    vault = service(tmp_path)
    scan = vault.scan("w")
    assert scan.entries == ()
    assert (scan.partial_reasons, scan.omitted) == (("unsafe_entry",), 2)
    with pytest.raises(VaultError, match="unsafe_path"):
        vault.read("w", "b.md")


def test_a_note_over_the_bound_is_too_large(tmp_path: Path) -> None:
    (tmp_path / "big.md").write_bytes(b"x" * (MAX_NOTE_BYTES + 1))
    with pytest.raises(VaultError, match="note_too_large"):
        service(tmp_path).read("w", "big.md")


def test_private_names_are_omitted_and_refused(tmp_path: Path) -> None:
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "x.md").write_text("private", encoding="utf-8")
    (tmp_path / ".env").write_text("private", encoding="utf-8")
    (tmp_path / "public.md").write_text("public", encoding="utf-8")
    vault = service(tmp_path)
    assert refs(vault) == ["public.md"]
    for ref in (".env", ".claude/x.md"):
        with pytest.raises(VaultError, match="private_path"):
            vault.read("w", ref)


def test_an_nfd_named_note_is_listed_and_readable(tmp_path: Path) -> None:
    name = unicodedata.normalize("NFD", "café.md")
    (tmp_path / name).write_text("decomposed", encoding="utf-8")
    vault = service(tmp_path)
    [ref] = refs(vault)
    assert unicodedata.normalize("NFC", ref) == "café.md"
    assert vault.read("w", ref).text == "decomposed"


def test_every_descriptor_is_closed_after_reads_and_refusals(tmp_path: Path) -> None:
    assert sys.platform != "win32"
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "n.md").write_text("n", encoding="utf-8")
    (tmp_path / "a" / "link.md").symlink_to(tmp_path / "a" / "n.md")
    os.mkfifo(tmp_path / "a" / "fifo.md")
    vault = service(tmp_path)
    before = len(os.listdir("/dev/fd"))
    vault.scan("w")
    vault.read("w", "a/n.md")
    for ref in ("a/link.md", "a/fifo.md", "a/gone.md", "n.md/x.md"):
        with pytest.raises(VaultError):
            vault.read("w", ref)
    assert len(os.listdir("/dev/fd")) == before


@pytest.mark.skipif(sys.platform != "darwin", reason="APFS opens a case alias as the same entry")
def test_a_case_alias_of_a_private_folder_is_refused_on_macos(tmp_path: Path) -> None:
    assert sys.platform == "darwin"
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "x.md").write_text("private", encoding="utf-8")
    (tmp_path / ".env").write_text("private", encoding="utf-8")
    vault = service(tmp_path)
    with pytest.raises(VaultError, match="private_path"):
        vault.read("w", ".CLAUDE/x.md")
    # The primitives never judge the requested name: the name on disk behind the descriptor does.
    with vault_module._open_root_fd(vault._root("w")) as root_fd:
        with pytest.raises(VaultError, match="private_path"):
            vault_module._open_dir_at(root_fd, ".CLAUDE")
        with pytest.raises(VaultError, match="private_path"):
            vault_module._read_file_at(root_fd, ".ENV", MAX_NOTE_BYTES)
