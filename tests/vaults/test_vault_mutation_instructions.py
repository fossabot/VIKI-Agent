"""The instruction-file root grammar of the mutation primitives: replace-only root
`AGENTS.md` and `CLAUDE.md` in an admitted ordinary vault, held to their exact name on disk."""

from __future__ import annotations

import ast
import hashlib
import os
import sys
import uuid
from dataclasses import replace
from pathlib import Path

import pytest

from viki_agent.domain import CanonicalRoot, RootIdentity, Workspace
from viki_agent.vaults import vault_mutation as native
from viki_agent.vaults.vault_mutation import (
    FileFingerprint,
    MutationCoordinator,
    MutationError,
    OrdinaryMutationRoot,
    VaultPublication,
)
from viki_agent.workspaces import root_key


def _vault_root(root: Path) -> OrdinaryMutationRoot:
    resolved = root.resolve()
    info = resolved.stat()
    return OrdinaryMutationRoot.from_admitted_workspace(
        Workspace(
            str(uuid.uuid4()),
            "vault",
            CanonicalRoot(root_key(resolved), str(resolved), f"{info.st_dev}:{info.st_ino}", False),
            "scratch",
            "ordinary",
            4,
            1,
            "opened",
            "unknown",
            None,
            RootIdentity((), None, {}, True, None, "2026-11-13T00:00:00.000Z"),
            None,
            "complete",
            True,
            "2026-11-13T00:00:00.000Z",
            "2026-11-13T00:00:00.000Z",
        )
    )


def _instructions_root(root: Path) -> OrdinaryMutationRoot:
    return replace(_vault_root(root), kind="instructions")


def _sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


@pytest.mark.parametrize("ref", ["AGENTS.md", "CLAUDE.md"])
def test_an_instruction_ref_is_exactly_a_root_instruction_file(ref: str) -> None:
    assert frozenset({"AGENTS.md", "CLAUDE.md"}) == native.INSTRUCTION_REFS
    assert native.instruction_ref(ref) == (ref,)


@pytest.mark.parametrize(
    "ref",
    [
        "agents.md",
        "Agents.md",
        "AGENTS.MD",
        "AGENTS.override.md",
        "CLAUDE.local.md",
        "GEMINI.md",
        "sub/AGENTS.md",
        "./AGENTS.md",
        "AGENTS.md.",
        "AGENTS.md ",
        "ＡGENTS.md",
        "note.md",
        "",
        None,
        b"AGENTS.md",
    ],
)
def test_an_instruction_ref_refuses_every_other_name(ref: object) -> None:
    with pytest.raises(MutationError) as caught:
        native.instruction_ref(ref)  # type: ignore[arg-type]
    assert caught.value.code == "invalid_ref"


async def test_a_vault_root_still_refuses_an_instruction_file(tmp_path: Path) -> None:
    vault = _vault_root(tmp_path)
    for ref in ("CLAUDE.md", "AGENTS.md"):
        with pytest.raises(MutationError) as caught:
            async with MutationCoordinator().lease(vault, (ref,)):
                pass
        assert caught.value.code == "excluded_path"
    async with MutationCoordinator().lease(vault, ()) as lease:
        with pytest.raises(MutationError) as caught:
            lease.require_target(vault, "CLAUDE.md")
        assert caught.value.code == "excluded_path"


async def test_an_instructions_root_takes_no_note_ref(tmp_path: Path) -> None:
    root = _instructions_root(tmp_path)
    with pytest.raises(MutationError) as caught:
        async with MutationCoordinator().lease(root, ("notes/a.md",)):
            pass
    assert caught.value.code == "invalid_ref"
    async with MutationCoordinator().lease(root, ()) as lease:
        for ref in ("notes/a.md", "a.md", "attachments/a.png", "sub/AGENTS.md"):
            with pytest.raises(MutationError) as caught:
                lease.require_target(root, ref)
            assert caught.value.code == "invalid_ref", ref
        assert lease.require_target(root, "AGENTS.md") == ("AGENTS.md",)
    assert list(tmp_path.iterdir()) == []


async def test_an_instructions_root_refuses_create_move_remove_folders_and_attachments(
    tmp_path: Path,
) -> None:
    (tmp_path / "AGENTS.md").write_bytes(b"old")
    root, publication = _instructions_root(tmp_path), VaultPublication()
    expected = FileFingerprint("0:0", _sha(b"old"), 3)
    async with MutationCoordinator().lease(root, ("AGENTS.md", "CLAUDE.md")) as lease:
        staged = publication.stage(lease, root, "CLAUDE.md", publication.reserve(), b"# new\n")
        refusals = {
            "publish_create": lambda: publication.publish_create(lease, staged),
            "move_verified": lambda: publication.move_verified(
                lease, root, "AGENTS.md", "CLAUDE.md", expected
            ),
            "remove_verified": lambda: publication.remove_verified(
                lease, root, "AGENTS.md", expected
            ),
            "ensure_folders": lambda: publication.ensure_folders(lease, root, ("sub",)),
            "attachment_sha256": lambda: publication.attachment_sha256(
                lease, root, "attachments/a.png"
            ),
        }
        for name, call in refusals.items():
            with pytest.raises(MutationError) as caught:
                call()
            assert caught.value.code == "invalid_ref", name
        assert publication.discard(lease, staged)
    assert sorted(path.name for path in tmp_path.iterdir()) == ["AGENTS.md"]
    assert (tmp_path / "AGENTS.md").read_bytes() == b"old"


@pytest.mark.parametrize("name", ["AGENTS.md", "CLAUDE.md"])
async def test_an_instructions_root_replaces_its_file_in_place(tmp_path: Path, name: str) -> None:
    old, new = b"\xef\xbb\xbf# synthetic\r\n", b"\xef\xbb\xbf# changed\r\n"
    (tmp_path / name).write_bytes(old)
    (tmp_path / "note.md").write_bytes(b"kept")
    root, publication = _instructions_root(tmp_path), VaultPublication()
    async with MutationCoordinator().lease(root, (name,)) as lease:
        staged = publication.stage(lease, root, name, publication.reserve(), new)
        with publication.prepare_replace(lease, staged, _sha(old), staged.backup_name) as prepared:
            prepared.move_target_to_backup()
            prepared.publish_stage_no_replace()
            assert prepared.verify_published().ref == name
        publication.discard_backup_after_publish(
            lease, root, name, staged.backup_name, staged.fingerprint, prepared.old
        )
    assert sorted(path.name for path in tmp_path.iterdir()) == sorted([name, "note.md"])
    assert (tmp_path / name).read_bytes() == new
    assert (tmp_path / "note.md").read_bytes() == b"kept"


async def test_a_differently_spelled_file_on_disk_is_not_replaced(tmp_path: Path) -> None:
    """On a case-insensitive volume `AGENTS.md` opens `Agents.md`; the replace would respell
    it, so the exact name on disk refuses it, whatever its hash."""
    content = b"# other spelling\n"
    (tmp_path / "Agents.md").write_bytes(content)
    if not (tmp_path / "AGENTS.md").exists():
        pytest.skip("needs a case-insensitive volume")
    root, publication = _instructions_root(tmp_path), VaultPublication()
    async with MutationCoordinator().lease(root, ("AGENTS.md",)) as lease:
        staged = publication.stage(lease, root, "AGENTS.md", publication.reserve(), b"new")
        with (
            pytest.raises(MutationError) as caught,
            publication.prepare_replace(lease, staged, _sha(content), staged.backup_name),
        ):
            pass
        assert caught.value.code == "excluded_path"
        assert caught.value.stage_retained
        assert publication.discard(lease, staged)
    assert [path.name for path in tmp_path.iterdir()] == ["Agents.md"]
    assert (tmp_path / "Agents.md").read_bytes() == content


def test_the_final_path_query_has_one_binding() -> None:
    """The exact-name check above and the alias check every open runs (`same_final_path`) read
    a handle's final path through one binding, with one buffer size and one set of flags, so a
    change to how that path is read reaches both checks."""
    name = "GetFinalPathNameByHandleW"
    src = Path(__file__).resolve().parents[2] / "src" / "viki_agent"
    sources = {path: path.read_text(encoding="utf-8") for path in sorted(src.rglob("*.py"))}
    bound = [
        path.relative_to(src).as_posix()
        for path, text in sources.items()
        if name in text  # only a file naming it can bind it
        for node in ast.walk(ast.parse(text))
        if isinstance(node, ast.Constant) and node.value == name
    ]
    assert bound == ["vaults/vault.py"]


async def _refused_replace(tmp_path: Path, content: bytes) -> str:
    root, publication = _instructions_root(tmp_path), VaultPublication()
    async with MutationCoordinator().lease(root, ("AGENTS.md",)) as lease:
        staged = publication.stage(lease, root, "AGENTS.md", publication.reserve(), b"new")
        with (
            pytest.raises(MutationError) as caught,
            publication.prepare_replace(lease, staged, _sha(content), staged.backup_name),
        ):
            pass
        assert publication.discard(lease, staged)
    return caught.value.code


async def test_a_hard_linked_instruction_file_is_not_replaced(tmp_path: Path) -> None:
    content = b"# linked\n"
    (tmp_path / "AGENTS.md").write_bytes(content)
    os.link(tmp_path / "AGENTS.md", tmp_path / "other.md")
    assert await _refused_replace(tmp_path, content) == "unsafe_target"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["AGENTS.md", "other.md"]
    assert (tmp_path / "AGENTS.md").read_bytes() == content
    assert (tmp_path / "other.md").read_bytes() == content


posix_only = pytest.mark.skipif(
    sys.platform == "win32", reason="the folder-listing name check runs off Windows only"
)


def _listed(folder: Path, opened: str, name: str) -> bool:
    """`_listed_as` for the file opened at `opened` in `folder`, asked about `name`."""
    if sys.platform == "win32":  # narrows the POSIX-only names below for pyright
        pytest.skip("the folder-listing name check runs off Windows only")
    parent = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fd = os.open(opened, os.O_RDONLY, dir_fd=parent)
        try:
            return native._listed_as(parent, native.FileHandle(fd, parent, opened), name)
        finally:
            os.close(fd)
    finally:
        os.close(parent)


@posix_only
def test_the_listing_check_admits_the_entry_named_exactly(tmp_path: Path) -> None:
    (tmp_path / "note.md").write_bytes(b"other")
    (tmp_path / "AGENTS.md").write_bytes(b"# synthetic\n")
    assert _listed(tmp_path, "AGENTS.md", "AGENTS.md")


@posix_only
def test_the_listing_check_refuses_another_spelling_on_disk(tmp_path: Path) -> None:
    """A case-insensitive folder opens `Agents.md` for `AGENTS.md`, and `/proc/self/fd` may
    report the lookup's spelling; the listing holds `Agents.md`, so the check refuses."""
    (tmp_path / "Agents.md").write_bytes(b"# synthetic\n")
    assert not _listed(tmp_path, "Agents.md", "AGENTS.md")


@posix_only
def test_the_listing_check_admits_a_hard_link_only_by_the_exact_name(tmp_path: Path) -> None:
    (tmp_path / "other.md").write_bytes(b"# synthetic\n")
    os.link(tmp_path / "other.md", tmp_path / "Agents.md")
    assert not _listed(tmp_path, "other.md", "AGENTS.md")
    (tmp_path / "Agents.md").unlink()
    os.link(tmp_path / "other.md", tmp_path / "AGENTS.md")
    assert _listed(tmp_path, "other.md", "AGENTS.md")
    assert not _listed(tmp_path, "other.md", "CLAUDE.md")


@posix_only
def test_the_listing_check_refuses_an_entry_of_that_name_holding_another_file(
    tmp_path: Path,
) -> None:
    (tmp_path / "AGENTS.md").write_bytes(b"# synthetic\n")
    (tmp_path / "other.md").write_bytes(b"# synthetic\n")
    assert not _listed(tmp_path, "other.md", "AGENTS.md")


@posix_only
@pytest.mark.parametrize("error", [PermissionError, NotImplementedError, TypeError])
def test_the_listing_check_refuses_when_the_folder_cannot_be_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: type[Exception]
) -> None:
    """NotImplementedError and TypeError are what `os.scandir(fd)` raises where it is not in
    `os.supports_fd`."""
    (tmp_path / "AGENTS.md").write_bytes(b"# synthetic\n")

    def unreadable(_fd: int) -> object:
        raise error("scandir")

    monkeypatch.setattr(native.os, "scandir", unreadable)
    assert not _listed(tmp_path, "AGENTS.md", "AGENTS.md")


@posix_only
def test_the_listing_check_refuses_past_its_entry_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("AGENTS.md", "b.md", "c.md"):
        (tmp_path / name).write_bytes(b"x")
    monkeypatch.setattr(native, "_MAX_LISTED_ENTRIES", 3)
    assert _listed(tmp_path, "AGENTS.md", "AGENTS.md")
    (tmp_path / "d.md").write_bytes(b"x")
    assert not _listed(tmp_path, "AGENTS.md", "AGENTS.md")


@posix_only
async def test_a_replace_refuses_when_the_listing_names_it_otherwise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The listing check is the second, independent one: it refuses alone, stage retained."""
    content = b"# synthetic\n"
    (tmp_path / "AGENTS.md").write_bytes(content)
    monkeypatch.setattr(native, "_listed_as", lambda *_args: False)
    root, publication = _instructions_root(tmp_path), VaultPublication()
    async with MutationCoordinator().lease(root, ("AGENTS.md",)) as lease:
        staged = publication.stage(lease, root, "AGENTS.md", publication.reserve(), b"new")
        with (
            pytest.raises(MutationError) as caught,
            publication.prepare_replace(lease, staged, _sha(content), staged.backup_name),
        ):
            pass
        assert (caught.value.code, caught.value.stage_retained) == ("excluded_path", True)
        assert publication.discard(lease, staged)
    assert [path.name for path in tmp_path.iterdir()] == ["AGENTS.md"]
    assert (tmp_path / "AGENTS.md").read_bytes() == content


async def test_a_symlinked_instruction_file_is_not_replaced(tmp_path: Path) -> None:
    content = b"# target\n"
    (tmp_path / "other.md").write_bytes(content)
    try:
        os.symlink(tmp_path / "other.md", tmp_path / "AGENTS.md")
    except OSError:
        pytest.skip("this account cannot create a symlink")
    assert await _refused_replace(tmp_path, content) == "unsafe_target"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["AGENTS.md", "other.md"]
    assert os.path.islink(tmp_path / "AGENTS.md")
    assert (tmp_path / "other.md").read_bytes() == content
