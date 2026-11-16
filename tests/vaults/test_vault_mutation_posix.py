"""The note writer off Windows: every primitive, the read side (guard, opens, probes, reads) and
the write side (stage, rename, delete, folders), runs on a supported POSIX volume, and every
typed refusal leaves the vault as it was."""

from __future__ import annotations

import contextlib
import errno
import gc
import hashlib
import os
import stat
import sys
import types
import uuid
from collections.abc import Callable
from pathlib import Path

import pytest

from viki_agent import osfs, winproc
from viki_agent.vaults import vault_mutation
from viki_agent.vaults.vault_mutation import (
    BACKUP_PREFIX,
    MAX_PUBLICATION_BYTES,
    MAX_STAGE_BYTES,
    STAGE_SUFFIX,
    FileFingerprint,
    MutationCoordinator,
    MutationError,
    MutationLease,
    OrdinaryMutationRoot,
    PublishedPublication,
    StagedPublication,
    VaultPublication,
    _safe_ref,
)
from viki_agent.workspaces import root_key

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="the POSIX note writer")


def test_a_windows_device_name_is_refused_on_every_os() -> None:
    with pytest.raises(MutationError) as caught:
        _safe_ref("CON.md")
    assert caught.value.code == "invalid_ref"


def test_a_name_the_posix_volume_cannot_hold_is_refused() -> None:
    """Off Windows a part over NAME_MAX (255 UTF-8 bytes) or with a lone surrogate is refused;
    Windows counts UTF-16 units, so it still admits the 86-character CJK name."""
    cjk = "日" * 86 + ".md"  # 261 UTF-8 bytes, 178 UTF-16 bytes
    if sys.platform == "win32":
        assert _safe_ref(cjk) == (cjk,)
        return
    for name in (cjk, "a\ud800.md"):
        with pytest.raises(MutationError) as caught:
            _safe_ref(name)
        assert caught.value.code == "invalid_ref", name


def _snapshot(root: Path) -> list[tuple[str, int, int, str]]:
    """Every entry under `root` by lstat: a link, a FIFO or a folder is recorded by its mode and
    inode, never followed or opened."""
    entries: list[tuple[str, int, int, str]] = []
    for path in root.rglob("*"):
        info = path.lstat()
        digest = hashlib.sha256(path.read_bytes()).hexdigest() if stat.S_ISREG(info.st_mode) else ""
        entries.append((str(path.relative_to(root)), info.st_mode, info.st_ino, digest))
    return sorted(entries)


def _vault(tmp_path: Path) -> tuple[Path, OrdinaryMutationRoot]:
    """A vault folder and the admitted root a lease takes for it."""
    root = tmp_path / "vault"
    root.mkdir()
    root = root.resolve()
    info = os.stat(root)
    return root, OrdinaryMutationRoot(
        workspace_id="workspace",
        path=root,
        canonical_key=root_key(str(root)),
        physical_id=f"{info.st_dev}:{info.st_ino}",
        policy_revision=1,
        git_common_dir=None,
    )


def _fingerprint(path: Path, content: bytes) -> FileFingerprint:
    """The identity and hash a verified primitive expects of `path`, holding `content`."""
    info = path.stat()
    return FileFingerprint(
        f"{info.st_dev}:{info.st_ino}", hashlib.sha256(content).hexdigest(), len(content)
    )


def _listing(root: Path) -> dict[str, bytes]:
    """Every file directly under `root` and its bytes."""
    return {entry.name: entry.read_bytes() for entry in root.iterdir()}


def _saved_over_after_the_rename(monkeypatch: pytest.MonkeyPatch, root: Path, name: str) -> None:
    """Just after the real no-replace rename onto `name`, another program saves `name` once, as
    editors and sync clients do: a new file of its own, renamed over the name."""
    assert sys.platform != "win32"
    real_rename = osfs.rename_at_no_replace

    def racing(src_dir_fd: int, src: str, dst_dir_fd: int, dst: str) -> None:
        real_rename(src_dir_fd, src, dst_dir_fd, dst)
        if dst == name:
            monkeypatch.setattr(osfs, "rename_at_no_replace", real_rename)
            (root / f"{name}.editor-swap").write_bytes(b"theirs\n")
            os.rename(root / f"{name}.editor-swap", root / name)

    monkeypatch.setattr(osfs, "rename_at_no_replace", racing)


def _descriptors() -> int:
    """How many descriptors this process holds."""
    return len(os.listdir("/proc/self/fd" if sys.platform == "linux" else "/dev/fd"))


def _sibling() -> str:
    """A recorded backup name, the kind of leaf a probe classifies."""
    return f"{BACKUP_PREFIX}{uuid.uuid4()}{STAGE_SUFFIX}"


def _stage(
    publication: VaultPublication,
    lease: MutationLease,
    root: OrdinaryMutationRoot,
    ref: str,
    content: bytes,
) -> StagedPublication:
    return publication.stage(lease, root, ref, publication.reserve(), content)


def _replace(
    publication: VaultPublication,
    lease: MutationLease,
    staged: StagedPublication,
    old: FileFingerprint,
) -> PublishedPublication:
    """Every replace step in the service's order, through the discard of the backup."""
    with publication.prepare_replace(lease, staged, old.sha256, staged.backup_name) as prepared:
        prepared.move_target_to_backup()
        prepared.publish_stage_no_replace()
        published = prepared.verify_published()
    publication.discard_backup_after_publish(
        lease, staged.root, staged.ref, staged.backup_name, published.fingerprint, prepared.old
    )
    return published


@posix_only
def test_the_test_volume_is_a_supported_filesystem(tmp_path: Path) -> None:
    """Every test below needs its temp folder on an allowlisted type: a runner that puts it on
    another fails here first, naming the type."""
    assert sys.platform != "win32"
    assert vault_mutation._supported_volume(tmp_path), osfs.filesystem_type(str(tmp_path))


@posix_only
@pytest.mark.parametrize("kind", ["nfs4", "smbfs", "exfat", "fuse.sshfs", "overlay"])
async def test_an_unsupported_filesystem_type_refuses_every_primitive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    """The type is asked again on every step, so a lease taken before the volume changed
    gains nothing, and no step changes a byte."""
    assert sys.platform != "win32"
    root, mutation_root = _vault(tmp_path)
    note = root / "n.md"
    note.write_bytes(b"note\n")
    (root / "attachments").mkdir()
    (root / "attachments" / "a.png").write_bytes(b"image")
    expected = _fingerprint(note, b"note\n")
    publication = VaultPublication()
    reservation = publication.reserve()
    staged = StagedPublication(
        root=mutation_root,
        ref="n.md",
        operation_id=reservation.operation_id,
        stage_name=reservation.stage_name,
        backup_name=reservation.backup_name,
        physical_id=expected.physical_id,
        sha256=expected.sha256,
        byte_size=expected.byte_size,
    )
    before = _snapshot(root)
    refs = ("n.md", "moved.md", "sub/n.md", "attachments/a.png")
    async with MutationCoordinator().lease(mutation_root, refs) as lease:
        monkeypatch.setattr(osfs, "filesystem_type", lambda path: kind)

        def prepare_replace() -> None:
            with publication.prepare_replace(lease, staged, expected.sha256, staged.backup_name):
                pass

        primitives = {
            "stage": lambda: publication.stage(
                lease, mutation_root, "moved.md", publication.reserve(), b"x"
            ),
            "publish_create": lambda: publication.publish_create(lease, staged),
            "prepare_replace": prepare_replace,
            "move_verified_no_replace": lambda: publication.move_verified_no_replace(
                lease, mutation_root, "n.md", staged.stage_name, expected
            ),
            "move_verified": lambda: publication.move_verified(
                lease, mutation_root, "n.md", "moved.md", expected
            ),
            "read_verified": lambda: publication.read_verified(
                lease, mutation_root, "n.md", expected
            ),
            "remove_verified": lambda: publication.remove_verified(
                lease, mutation_root, "n.md", expected
            ),
            "ensure_folders": lambda: publication.ensure_folders(lease, mutation_root, ("sub",)),
            "probe_verified": lambda: publication.probe_verified(
                lease, mutation_root, "n.md", None, expected
            ),
            "attachment_sha256": lambda: publication.attachment_sha256(
                lease, mutation_root, "attachments/a.png"
            ),
            "discard_backup_after_publish": lambda: publication.discard_backup_after_publish(
                lease, mutation_root, "n.md", staged.backup_name, expected, expected
            ),
            "discard": lambda: publication.discard(lease, staged),
        }
        for name, call in primitives.items():
            with pytest.raises(MutationError) as caught:
                call()
            assert caught.value.code == "unsupported_filesystem", name
    with pytest.raises(MutationError) as caught:
        async with MutationCoordinator().lease(mutation_root, ("n.md",)):
            pass
    assert caught.value.code == "unsupported_filesystem"
    assert _snapshot(root) == before


@posix_only
def test_a_folder_check_admits_only_a_real_folder(tmp_path: Path) -> None:
    assert sys.platform != "win32"
    root, _ = _vault(tmp_path)
    (root / "folder").mkdir()
    (root / "file").write_bytes(b"x")
    (root / "link").symlink_to(root / "folder", target_is_directory=True)
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        assert vault_mutation._folder_exists(fd, "folder") is True
        assert vault_mutation._folder_exists(fd, "missing") is False
        for name in ("file", "link"):
            with pytest.raises(MutationError) as caught:
                vault_mutation._folder_exists(fd, name)
            assert caught.value.code == "unsafe_parent", name
    finally:
        os.close(fd)


@posix_only
async def test_a_probe_tells_absent_from_match_and_another_file(tmp_path: Path) -> None:
    assert sys.platform != "win32"
    root, mutation_root = _vault(tmp_path)
    note = root / "n.md"
    note.write_bytes(b"note\n")
    expected = _fingerprint(note, b"note\n")
    sibling = _sibling()
    publication = VaultPublication()
    async with MutationCoordinator().lease(mutation_root, ("n.md", "sub/n.md")) as lease:

        def probe(ref: str, name: str | None) -> str:
            return publication.probe_verified(lease, mutation_root, ref, name, expected)

        assert probe("n.md", None) == "match"
        assert probe("n.md", sibling) == "absent"
        # The same bytes in another file are not the recorded identity.
        (root / sibling).write_bytes(b"note\n")
        assert probe("n.md", sibling) == "unexpected"
        # A missing folder holds no entry either.
        assert probe("sub/n.md", None) == "absent"
        (root / "sub").mkdir()
        assert probe("sub/n.md", None) == "absent"
    assert note.read_bytes() == b"note\n"


@posix_only
@pytest.mark.parametrize("shape", ["symlink", "fifo", "hard_link", "folder"])
async def test_a_probe_reads_a_link_fifo_hard_link_or_folder_as_unexpected(
    tmp_path: Path, shape: str
) -> None:
    """None of these is opened as the note. Following the link, or linking the note, would find
    the recorded identity, so only a refusal answers `unexpected`; the FIFO open must not
    block."""
    assert sys.platform != "win32"
    root, mutation_root = _vault(tmp_path)
    note = root / "n.md"
    note.write_bytes(b"note\n")
    expected = _fingerprint(note, b"note\n")
    name = _sibling()
    if shape == "symlink":
        (root / name).symlink_to(note)
    elif shape == "fifo":
        os.mkfifo(root / name)
    elif shape == "hard_link":
        os.link(note, root / name)
    else:
        (root / name).mkdir()
    before = _snapshot(root)
    async with MutationCoordinator().lease(mutation_root, ("n.md",)) as lease:
        probed = VaultPublication().probe_verified(lease, mutation_root, "n.md", name, expected)
    assert probed == "unexpected"
    assert _snapshot(root) == before


@posix_only
@pytest.mark.parametrize("size", [0, 7, 64 * 1024 + 7])
async def test_a_read_returns_every_byte_of_the_note(tmp_path: Path, size: int) -> None:
    """A read crosses the 64 KiB chunk it reads at a time, and an empty note is no bytes."""
    assert sys.platform != "win32"
    root, mutation_root = _vault(tmp_path)
    note = root / "sub" / "n.md"
    note.parent.mkdir()
    content = bytes(index % 251 for index in range(size))
    note.write_bytes(content)
    expected = _fingerprint(note, content)
    async with MutationCoordinator().lease(mutation_root, ("sub/n.md",)) as lease:
        read = VaultPublication().read_verified(lease, mutation_root, "sub/n.md", expected)
    assert read == content


@posix_only
async def test_a_changed_note_is_not_read(tmp_path: Path) -> None:
    assert sys.platform != "win32"
    root, mutation_root = _vault(tmp_path)
    note = root / "n.md"
    note.write_bytes(b"old")
    expected = _fingerprint(note, b"old")
    note.write_bytes(b"new")  # the same file, other bytes
    async with MutationCoordinator().lease(mutation_root, ("n.md",)) as lease:
        with pytest.raises(MutationError) as caught:
            VaultPublication().read_verified(lease, mutation_root, "n.md", expected)
    assert caught.value.code == "conflict"
    assert note.read_bytes() == b"new"


@posix_only
async def test_an_attachment_hash_is_verified_and_none_when_absent(tmp_path: Path) -> None:
    assert sys.platform != "win32"
    root, mutation_root = _vault(tmp_path)
    ref = "attachments/a.png"
    publication = VaultPublication()
    async with MutationCoordinator().lease(mutation_root, (ref,)) as lease:
        # No folder and no file are both absent, and neither is created.
        assert publication.attachment_sha256(lease, mutation_root, ref) is None
        (root / "attachments").mkdir()
        assert publication.attachment_sha256(lease, mutation_root, ref) is None
        # Over the note cap is still an attachment.
        content = b"z" * (MAX_PUBLICATION_BYTES + 1)
        (root / "attachments" / "a.png").write_bytes(content)
        hashed = publication.attachment_sha256(lease, mutation_root, ref)
    assert hashed == hashlib.sha256(content).hexdigest()
    assert (root / "attachments" / "a.png").read_bytes() == content


@posix_only
@pytest.mark.parametrize("shape", ["symlink", "hard_link", "folder", "over_the_ceiling"])
async def test_an_attachment_hash_refuses_a_link_folder_or_oversized_file(
    tmp_path: Path, shape: str
) -> None:
    assert sys.platform != "win32"
    root, mutation_root = _vault(tmp_path)
    (root / "attachments").mkdir()
    target = root / "attachments" / "a.png"
    (root / "elsewhere.png").write_bytes(b"image")
    if shape == "symlink":
        target.symlink_to(root / "elsewhere.png")
    elif shape == "hard_link":
        os.link(root / "elsewhere.png", target)
    elif shape == "folder":
        target.mkdir()
    else:
        with target.open("wb") as sparse:
            sparse.truncate(MAX_STAGE_BYTES + 1)
    before = _snapshot(root)
    async with MutationCoordinator().lease(mutation_root, ("attachments/a.png",)) as lease:
        with pytest.raises(MutationError) as caught:
            VaultPublication().attachment_sha256(lease, mutation_root, "attachments/a.png")
    assert caught.value.code == "unsafe_target"
    assert not caught.value.stage_retained
    assert _snapshot(root) == before


@posix_only
async def test_a_hard_link_refuses_before_its_final_path_is_asked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """macOS may name another link as a hard link's final path; the link count refuses first,
    so a hard link answers `unsafe_target` on both systems, never `excluded_path`."""
    assert sys.platform != "win32"
    root, mutation_root = _vault(tmp_path)
    (root / "attachments").mkdir()
    first = root / "first.md"
    first.write_bytes(b"note\n")
    os.link(first, root / "n.md")
    os.link(first, root / "attachments" / "a.png")
    inode = first.stat().st_ino
    real_final_path = osfs.final_path

    def final_path(fd: int) -> str:
        return str(first) if os.fstat(fd).st_ino == inode else real_final_path(fd)

    monkeypatch.setattr(osfs, "final_path", final_path)
    expected = _fingerprint(first, b"note\n")
    publication = VaultPublication()
    async with MutationCoordinator().lease(mutation_root, ("n.md", "attachments/a.png")) as lease:
        for call in (
            lambda: publication.read_verified(lease, mutation_root, "n.md", expected),
            lambda: publication.attachment_sha256(lease, mutation_root, "attachments/a.png"),
        ):
            with pytest.raises(MutationError) as caught:
                call()
            assert caught.value.code == "unsafe_target"


@posix_only
async def test_a_symlinked_parent_or_leaf_is_refused(tmp_path: Path) -> None:
    """Every open is relative to a held folder and follows no link, so a link at a folder
    or at the leaf refuses even when its target is the expected file."""
    assert sys.platform != "win32"
    root, mutation_root = _vault(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "n.md").write_bytes(b"outside\n")
    (outside / "a.png").write_bytes(b"image")
    (root / "linked").symlink_to(outside, target_is_directory=True)
    (root / "attachments").symlink_to(outside, target_is_directory=True)
    (root / "leaf.md").symlink_to(outside / "n.md")
    expected = _fingerprint(outside / "n.md", b"outside\n")
    before = _snapshot(root), _snapshot(outside)
    publication = VaultPublication()
    refs = ("linked/n.md", "leaf.md", "attachments/a.png")
    async with MutationCoordinator().lease(mutation_root, refs) as lease:
        for ref, code in (("linked/n.md", "unsafe_parent"), ("leaf.md", "unsafe_target")):
            with pytest.raises(MutationError) as caught:
                publication.read_verified(lease, mutation_root, ref, expected)
            assert caught.value.code == code, ref
        with pytest.raises(MutationError) as caught:
            publication.probe_verified(lease, mutation_root, "linked/n.md", None, expected)
        assert caught.value.code == "unsafe_parent"
        probed = publication.probe_verified(lease, mutation_root, "leaf.md", None, expected)
        assert probed == "unexpected"
        with pytest.raises(MutationError) as caught:
            publication.attachment_sha256(lease, mutation_root, "attachments/a.png")
        assert caught.value.code == "unsafe_parent"
    assert (_snapshot(root), _snapshot(outside)) == before


@posix_only
async def test_a_mount_crossing_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A folder or a file on another device than its parent is a mount crossing."""
    assert sys.platform != "win32"
    root, mutation_root = _vault(tmp_path)
    (root / "sub").mkdir()
    (root / "sub" / "n.md").write_bytes(b"note\n")
    note = root / "n.md"
    note.write_bytes(b"note\n")
    (root / "attachments").mkdir()
    image = root / "attachments" / "a.png"
    image.write_bytes(b"image")
    expected = _fingerprint(note, b"note\n")
    real_fstat = os.fstat
    crossing: set[int] = set()  # the inodes reported on another device

    def fstat(fd: int) -> os.stat_result:
        info = real_fstat(fd)
        if info.st_ino not in crossing:
            return info
        fields = list(info)
        fields[stat.ST_DEV] = info.st_dev + 1
        return os.stat_result(fields)

    monkeypatch.setattr(vault_mutation.os, "fstat", fstat)
    before = _snapshot(root)
    publication = VaultPublication()
    refs = ("sub/n.md", "n.md", "attachments/a.png")
    async with MutationCoordinator().lease(mutation_root, refs) as lease:
        crossing.add((root / "sub").stat().st_ino)
        for call in (
            lambda: publication.read_verified(lease, mutation_root, "sub/n.md", expected),
            lambda: publication.probe_verified(lease, mutation_root, "sub/n.md", None, expected),
        ):
            with pytest.raises(MutationError) as caught:
                call()
            assert caught.value.code == "unsafe_parent"
        crossing.update({note.stat().st_ino, image.stat().st_ino})
        with pytest.raises(MutationError) as caught:
            publication.read_verified(lease, mutation_root, "n.md", expected)
        assert caught.value.code == "unsafe_target"
        probed = publication.probe_verified(lease, mutation_root, "n.md", None, expected)
        assert probed == "unexpected"
        with pytest.raises(MutationError) as caught:
            publication.attachment_sha256(lease, mutation_root, "attachments/a.png")
        assert caught.value.code == "unsafe_target"
    assert _snapshot(root) == before


@posix_only
async def test_a_final_path_mismatch_refuses_excluded_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The path the kernel holds for an open folder or file must be the guarded one, and
    a failed query refuses too."""
    assert sys.platform != "win32"
    root, mutation_root = _vault(tmp_path)
    (root / "sub").mkdir()
    note = root / "sub" / "n.md"
    note.write_bytes(b"note\n")
    (root / "top.md").write_bytes(b"top\n")
    (root / "attachments").mkdir()
    image = root / "attachments" / "a.png"
    image.write_bytes(b"image")
    expected = _fingerprint(note, b"note\n")
    real_final_path = osfs.final_path
    aliased: set[str] = set()  # the root keys of the entries reported under another path
    failing = False

    def final_path(fd: int) -> str:
        if failing:
            raise OSError(errno.ENOENT, "no final path")
        path = real_final_path(fd)
        return f"{path}-alias" if root_key(path) in aliased else path

    monkeypatch.setattr(osfs, "final_path", final_path)
    before = _snapshot(root)
    publication = VaultPublication()
    refs = ("sub/n.md", "top.md", "attachments/a.png")
    async with MutationCoordinator().lease(mutation_root, refs) as lease:
        for entry in (root / "sub", note):
            aliased.clear()
            aliased.add(root_key(str(entry)))
            with pytest.raises(MutationError) as caught:
                publication.read_verified(lease, mutation_root, "sub/n.md", expected)
            assert caught.value.code == "excluded_path", entry
        aliased.clear()
        aliased.add(root_key(str(root / "sub")))
        with pytest.raises(MutationError) as caught:
            publication.probe_verified(lease, mutation_root, "sub/n.md", None, expected)
        assert caught.value.code == "excluded_path"
        aliased.clear()
        aliased.add(root_key(str(image)))
        with pytest.raises(MutationError) as caught:
            publication.attachment_sha256(lease, mutation_root, "attachments/a.png")
        assert caught.value.code == "excluded_path"
        aliased.clear()
        failing = True
        with pytest.raises(MutationError) as caught:
            publication.read_verified(lease, mutation_root, "top.md", expected)
        assert caught.value.code == "excluded_path"
    assert _snapshot(root) == before


@posix_only
async def test_no_descriptor_outlives_a_primitive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every primitive closes what it opened, on success and on each refusal."""
    assert sys.platform != "win32"
    root, mutation_root = _vault(tmp_path)
    (root / "sub").mkdir()
    note = root / "sub" / "n.md"
    note.write_bytes(b"note\n")
    os.link(note, root / "sub" / "twin.md")
    twin = _fingerprint(note, b"note\n")
    other = root / "other.md"
    other.write_bytes(b"other\n")
    expected = _fingerprint(other, b"other\n")
    stale = FileFingerprint(expected.physical_id, "0" * 64, expected.byte_size)
    os.mkfifo(root / "sub" / "fifo")
    (root / "sub" / "folder").mkdir()
    (root / "linked").symlink_to(root / "sub", target_is_directory=True)
    (root / "attachments").mkdir()
    (root / "attachments" / "a.png").write_bytes(b"image")
    real_final_path = osfs.final_path

    def final_path(fd: int) -> str:
        path = real_final_path(fd)
        return f"{path}-alias" if root_key(path) == root_key(str(root / "aliased.md")) else path

    monkeypatch.setattr(osfs, "final_path", final_path)
    (root / "aliased.md").write_bytes(b"aliased\n")
    aliased = _fingerprint(root / "aliased.md", b"aliased\n")
    (root / "old.md").write_bytes(b"old\n")
    old = _fingerprint(root / "old.md", b"old\n")
    (root / "taken.md").write_bytes(b"taken\n")
    publication = VaultPublication()
    collide = publication.reserve()
    (root / collide.stage_name).write_bytes(b"theirs\n")
    refs = ("other.md", "sub/twin.md", "linked/n.md", "aliased.md", "missing/n.md")
    refs += ("attachments/a.png", "new.md", "moved.md", "taken.md", "old.md", "a/b/n.md")
    gc.collect()
    held = _descriptors()
    async with MutationCoordinator().lease(mutation_root, refs) as lease:
        calls = {
            "read": lambda: publication.read_verified(lease, mutation_root, "other.md", expected),
            "read conflict": lambda: publication.read_verified(
                lease, mutation_root, "other.md", stale
            ),
            "read hard link": lambda: publication.read_verified(
                lease, mutation_root, "sub/twin.md", twin
            ),
            "read linked parent": lambda: publication.read_verified(
                lease, mutation_root, "linked/n.md", twin
            ),
            "read alias": lambda: publication.read_verified(
                lease, mutation_root, "aliased.md", aliased
            ),
            "probe match": lambda: publication.probe_verified(
                lease, mutation_root, "other.md", None, expected
            ),
            "probe fifo": lambda: publication.probe_verified(
                lease, mutation_root, "sub/twin.md", "fifo", twin
            ),
            "probe folder": lambda: publication.probe_verified(
                lease, mutation_root, "sub/twin.md", "folder", twin
            ),
            "probe missing folder": lambda: publication.probe_verified(
                lease, mutation_root, "missing/n.md", None, twin
            ),
            "attachment": lambda: publication.attachment_sha256(
                lease, mutation_root, "attachments/a.png"
            ),
            "remove": lambda: publication.remove_verified(
                lease, mutation_root, "other.md", expected
            ),
        }
        for name, call in calls.items():
            with contextlib.suppress(MutationError):
                call()
            assert _descriptors() == held, name

        # The write side, step by step, on success and on refusal.
        def closed(name: str) -> None:
            assert _descriptors() == held, name

        def refused(name: str, call: Callable[[], object]) -> None:
            with pytest.raises(MutationError):
                call()
            closed(name)

        def prepare(staged: StagedPublication, expected_sha256: str) -> None:
            with publication.prepare_replace(lease, staged, expected_sha256, staged.backup_name):
                pass

        new = _stage(publication, lease, mutation_root, "new.md", b"new\n")
        closed("stage")
        refused(
            "stage collision",
            lambda: publication.stage(lease, mutation_root, "new.md", collide, b"x"),
        )
        publication.publish_create(lease, new)
        closed("publish")
        taken = _stage(publication, lease, mutation_root, "taken.md", b"ours\n")
        refused("publish onto a taken name", lambda: publication.publish_create(lease, taken))
        publication.discard(lease, taken)
        closed("discard")
        replacing = _stage(publication, lease, mutation_root, "old.md", b"newer\n")
        refused("replace conflict", lambda: prepare(replacing, "0" * 64))
        published = _replace(publication, lease, replacing, old)
        closed("replace")
        restoring = _stage(publication, lease, mutation_root, "old.md", b"newest\n")
        with publication.prepare_replace(
            lease, restoring, published.fingerprint.sha256, restoring.backup_name
        ) as interrupted:
            interrupted.move_target_to_backup()
        closed("backup")
        publication.move_verified_no_replace(
            lease, mutation_root, "old.md", restoring.backup_name, interrupted.old
        )
        closed("move back from the backup")
        publication.discard(lease, restoring)
        publication.move_verified(lease, mutation_root, "new.md", "moved.md", new.fingerprint)
        closed("move")
        refused(
            "move onto a taken name",
            lambda: publication.move_verified(
                lease, mutation_root, "moved.md", "taken.md", new.fingerprint
            ),
        )
        changed = FileFingerprint(new.physical_id, "0" * 64, new.byte_size)
        refused(
            "remove a changed note",
            lambda: publication.remove_verified(lease, mutation_root, "moved.md", changed),
        )
        publication.remove_verified(lease, mutation_root, "moved.md", new.fingerprint)
        closed("remove")
        publication.ensure_folders(lease, mutation_root, ("a", "b"))
        closed("folders")
        publication.ensure_folders(lease, mutation_root, ("a", "b"))
        closed("folders reused")
        refused(
            "folders through a link",
            lambda: publication.ensure_folders(lease, mutation_root, ("linked",)),
        )
    assert _descriptors() == held
    assert (root / "old.md").read_bytes() == b"newer\n"
    assert not (root / "moved.md").exists()
    assert (root / "a" / "b").is_dir()


@posix_only
@pytest.mark.parametrize("shape", ["file", "symlink"])
async def test_a_taken_stage_name_is_a_collision_and_keeps_its_entry(
    tmp_path: Path, shape: str
) -> None:
    """The stage is created exclusively: a file at its name, or a link there to a file outside
    the vault, answers `stage_collision`, and nothing is written through either."""
    assert sys.platform != "win32"
    root, mutation_root = _vault(tmp_path)
    outside = tmp_path / "outside.md"
    outside.write_bytes(b"outside\n")
    publication = VaultPublication()
    reservation = publication.reserve()
    if shape == "file":
        (root / reservation.stage_name).write_bytes(b"theirs\n")
    else:
        (root / reservation.stage_name).symlink_to(outside)
    before = _snapshot(root), outside.read_bytes()
    async with MutationCoordinator().lease(mutation_root, ("n.md",)) as lease:
        with pytest.raises(MutationError) as caught:
            publication.stage(lease, mutation_root, "n.md", reservation, b"new\n")
    assert caught.value.code == "stage_collision"
    assert (_snapshot(root), outside.read_bytes()) == before


@posix_only
async def test_create_publishes_without_replacing(tmp_path: Path) -> None:
    """A create lands its bytes in the stage's own file and leaves no stage; a create or
    a move onto a taken name answers `conflict` and keeps every file as it was."""
    assert sys.platform != "win32"
    root, mutation_root = _vault(tmp_path)
    (root / "taken.md").write_bytes(b"theirs\n")
    (root / "mine.md").write_bytes(b"mine\n")
    mine = _fingerprint(root / "mine.md", b"mine\n")
    publication = VaultPublication()
    async with MutationCoordinator().lease(
        mutation_root, ("new.md", "taken.md", "mine.md")
    ) as lease:
        staged = _stage(publication, lease, mutation_root, "new.md", b"new\n")
        published = publication.publish_create(lease, staged)
        assert published == PublishedPublication("new.md", staged.fingerprint)
        assert _fingerprint(root / "new.md", b"new\n") == staged.fingerprint
        assert sorted(os.listdir(root)) == ["mine.md", "new.md", "taken.md"]
        blocked = _stage(publication, lease, mutation_root, "taken.md", b"ours\n")
        before = _snapshot(root)
        with pytest.raises(MutationError) as caught:
            publication.publish_create(lease, blocked)
        assert caught.value.code == "conflict"
        assert _snapshot(root) == before
        with pytest.raises(MutationError) as caught:
            publication.move_verified(lease, mutation_root, "mine.md", "taken.md", mine)
        assert caught.value.code == "conflict"
    assert _snapshot(root) == before
    assert (root / blocked.stage_name).read_bytes() == b"ours\n"


@posix_only
async def test_a_replace_publishes_the_stage_and_drops_the_backup(tmp_path: Path) -> None:
    """Prepare, back up, publish, verify and discard the backup: the note holds the new bytes
    in the stage's own file, and neither the stage nor the backup is left."""
    assert sys.platform != "win32"
    root, mutation_root = _vault(tmp_path)
    folder = root / "sub"
    folder.mkdir()
    note = folder / "n.md"
    note.write_bytes(b"old\n")
    old = _fingerprint(note, b"old\n")
    publication = VaultPublication()
    async with MutationCoordinator().lease(mutation_root, ("sub/n.md",)) as lease:
        staged = _stage(publication, lease, mutation_root, "sub/n.md", b"new\n")
        assert sorted(os.listdir(folder)) == sorted(["n.md", staged.stage_name])
        with publication.prepare_replace(lease, staged, old.sha256, staged.backup_name) as prepared:
            assert prepared.old == old
            prepared.move_target_to_backup()
            assert sorted(os.listdir(folder)) == sorted([staged.backup_name, staged.stage_name])
            prepared.publish_stage_no_replace()
            published = prepared.verify_published()
        assert (folder / staged.backup_name).read_bytes() == b"old\n"
        publication.discard_backup_after_publish(
            lease, mutation_root, "sub/n.md", staged.backup_name, published.fingerprint, old
        )
    assert published == PublishedPublication("n.md", staged.fingerprint)
    assert _fingerprint(note, b"new\n") == staged.fingerprint
    assert os.listdir(folder) == ["n.md"]


@posix_only
async def test_a_save_over_the_name_just_after_the_publish_rename_stays_there(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another program saves the note just after the stage took its name. Moving that file back
    would hide it under the stage's private name, so it stays at the note's name, the publish
    answers `conflict`, and the old note waits in its backup."""
    assert sys.platform != "win32"
    root, mutation_root = _vault(tmp_path)
    note = root / "n.md"
    note.write_bytes(b"old\n")
    old = _fingerprint(note, b"old\n")
    publication = VaultPublication()
    async with MutationCoordinator().lease(mutation_root, ("n.md",)) as lease:
        staged = _stage(publication, lease, mutation_root, "n.md", b"new\n")
        with publication.prepare_replace(lease, staged, old.sha256, staged.backup_name) as prepared:
            prepared.move_target_to_backup()
            _saved_over_after_the_rename(monkeypatch, root, "n.md")
            with pytest.raises(MutationError) as caught:
                prepared.publish_stage_no_replace()
    assert caught.value.code == "conflict"
    assert _listing(root) == {"n.md": b"theirs\n", staged.backup_name: b"old\n"}


@posix_only
async def test_a_save_over_the_name_just_after_a_create_rename_stays_there(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The create's twin: the other program's file stays at the name, the create answers
    `conflict`, and nothing of ours is left."""
    assert sys.platform != "win32"
    root, mutation_root = _vault(tmp_path)
    publication = VaultPublication()
    async with MutationCoordinator().lease(mutation_root, ("n.md",)) as lease:
        staged = _stage(publication, lease, mutation_root, "n.md", b"new\n")
        _saved_over_after_the_rename(monkeypatch, root, "n.md")
        with pytest.raises(MutationError) as caught:
            publication.publish_create(lease, staged)
    assert caught.value.code == "conflict"
    assert _listing(root) == {"n.md": b"theirs\n"}


@posix_only
async def test_a_name_the_check_cannot_read_after_the_rename_is_moved_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the check after the rename cannot read the new name, what holds it is unknown and
    may be ours, so the move is undone: the stage is back at its name and the create answers
    `conflict`, having published nothing."""
    assert sys.platform != "win32"
    root, mutation_root = _vault(tmp_path)
    publication = VaultPublication()
    real_rename = osfs.rename_at_no_replace
    real_stat = os.stat
    unreadable: list[str] = []

    def renamed(src_dir_fd: int, src: str, dst_dir_fd: int, dst: str) -> None:
        real_rename(src_dir_fd, src, dst_dir_fd, dst)
        if dst == "n.md":
            unreadable.append(dst)

    def failing_stat(
        path: str, *, dir_fd: int | None = None, follow_symlinks: bool = True
    ) -> os.stat_result:
        if dir_fd is not None and path in unreadable:
            unreadable.remove(path)
            raise OSError(errno.EIO, os.strerror(errno.EIO))
        return real_stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

    async with MutationCoordinator().lease(mutation_root, ("n.md",)) as lease:
        staged = _stage(publication, lease, mutation_root, "n.md", b"new\n")
        monkeypatch.setattr(osfs, "rename_at_no_replace", renamed)
        monkeypatch.setattr(vault_mutation.os, "stat", failing_stat)
        with pytest.raises(MutationError) as caught:
            publication.publish_create(lease, staged)
    assert caught.value.code == "conflict"
    assert _listing(root) == {staged.stage_name: b"new\n"}


@posix_only
@pytest.mark.parametrize("mode", [0o755, 0o600], ids=oct)
async def test_replace_keeps_the_mode(tmp_path: Path, desktop_umask: None, mode: int) -> None:
    """Under a desktop umask a stage is still created 0600, not 0644; the note it replaces
    keeps its own mode, wider (0755) or as private (0600)."""
    assert sys.platform != "win32"
    root, mutation_root = _vault(tmp_path)
    note = root / "n.md"
    note.write_bytes(b"old\n")
    note.chmod(mode)
    old = _fingerprint(note, b"old\n")
    publication = VaultPublication()
    async with MutationCoordinator().lease(mutation_root, ("n.md",)) as lease:
        staged = _stage(publication, lease, mutation_root, "n.md", b"new\n")
        assert stat.S_IMODE((root / staged.stage_name).stat().st_mode) == 0o600
        _replace(publication, lease, staged, old)
    assert note.read_bytes() == b"new\n"
    assert stat.S_IMODE(note.stat().st_mode) == mode


@posix_only
async def test_a_create_stage_is_0600_and_the_note_takes_a_plain_creates_mode(
    tmp_path: Path, desktop_umask: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under a desktop umask a create's stage is still 0600 until `publish_create`; the note
    then has the mode a plain create gives, 0666 less the umask read at import, set here as
    umask 022 gives it: 0644."""
    assert sys.platform != "win32"
    monkeypatch.setattr(vault_mutation, "_CREATE_MODE", 0o666 & ~0o022)
    root, mutation_root = _vault(tmp_path)
    publication = VaultPublication()
    async with MutationCoordinator().lease(mutation_root, ("n.md",)) as lease:
        staged = _stage(publication, lease, mutation_root, "n.md", b"new\n")
        assert stat.S_IMODE((root / staged.stage_name).stat().st_mode) == 0o600
        publication.publish_create(lease, staged)
    assert _listing(root) == {"n.md": b"new\n"}
    assert stat.S_IMODE((root / "n.md").stat().st_mode) == 0o644


@posix_only
def test_import_reads_the_umask_for_a_creates_mode_and_puts_it_back(tmp_path: Path) -> None:
    """A fresh interpreter under umask 027: the module derives 0640, and the umask is still
    027 after the import that read it."""
    assert sys.platform != "win32"
    code = (
        "import os\n"
        "os.umask(0o027)\n"
        "from viki_agent.vaults import vault_mutation\n"
        "print(oct(vault_mutation._CREATE_MODE), oct(os.umask(0o027)))\n"
    )
    result = winproc.run([sys.executable, "-c", code], cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["0o640", "0o27"]


@pytest.mark.skipif(sys.platform != "linux", reason="a faked group id, run on Linux")
@pytest.mark.parametrize("given", [True, False], ids=["group_given", "group_refused"])
async def test_a_replace_keeps_the_notes_group_or_no_group_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, given: bool
) -> None:
    """A stage in another group than the note's (macOS gives a new file its folder's) is
    given the note's group; when this account may not give it, the stage keeps no group
    access, so a 0640 note in a private group is not opened to the stage's group."""
    assert sys.platform == "linux"
    root, mutation_root = _vault(tmp_path)
    note = root / "n.md"
    note.write_bytes(b"old\n")
    note.chmod(0o640)
    old = _fingerprint(note, b"old\n")
    note_inode = note.stat().st_ino
    publication = VaultPublication()
    real_fstat = os.fstat
    chowned: list[tuple[int, int, int]] = []
    async with MutationCoordinator().lease(mutation_root, ("n.md",)) as lease:
        staged = _stage(publication, lease, mutation_root, "n.md", b"new\n")
        stage_info = (root / staged.stage_name).stat()
        private_gid = stage_info.st_gid + 1  # the note's group, as its descriptor reports it

        def fstat(fd: int) -> os.stat_result:
            info = real_fstat(fd)
            if info.st_ino != note_inode:
                return info
            fields = list(info)
            fields[stat.ST_GID] = private_gid
            return os.stat_result(fields)

        def fchown(fd: int, uid: int, gid: int) -> None:
            chowned.append((real_fstat(fd).st_ino, uid, gid))
            if not given:
                raise PermissionError(errno.EPERM, os.strerror(errno.EPERM))

        monkeypatch.setattr(vault_mutation.os, "fstat", fstat)
        monkeypatch.setattr(vault_mutation.os, "fchown", fchown)
        _replace(publication, lease, staged, old)
    assert chowned == [(stage_info.st_ino, -1, private_gid)]
    assert note.read_bytes() == b"new\n"
    assert stat.S_IMODE(note.stat().st_mode) == (0o640 if given else 0o600)


@posix_only
@pytest.mark.parametrize(
    "lock",
    [
        "mode",
        "owner",
        pytest.param(
            "flag", marks=pytest.mark.skipif(sys.platform != "darwin", reason="chflags(2)")
        ),
    ],
)
async def test_a_read_only_or_locked_note_refuses_read_only_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lock: str
) -> None:
    """A replace would drop mode 0444, another user's ownership or a macOS `uchg`
    lock, so a replace, a move and a remove refuse `read_only_target` and nothing moves."""
    assert sys.platform != "win32"
    root, mutation_root = _vault(tmp_path)
    note = root / "n.md"
    note.write_bytes(b"old\n")
    old = _fingerprint(note, b"old\n")
    publication = VaultPublication()
    async with MutationCoordinator().lease(mutation_root, ("n.md", "moved.md")) as lease:
        staged = _stage(publication, lease, mutation_root, "n.md", b"new\n")
        if lock == "mode":
            note.chmod(0o444)
        elif lock == "owner":
            other_user = os.geteuid() + 1
            monkeypatch.setattr(os, "geteuid", lambda: other_user)
        elif sys.platform == "darwin":
            os.chflags(note, stat.UF_IMMUTABLE)
        try:
            before = _snapshot(root)

            def prepare() -> None:
                with publication.prepare_replace(lease, staged, old.sha256, staged.backup_name):
                    pass

            calls = {
                "replace": prepare,
                "move": lambda: publication.move_verified(
                    lease, mutation_root, "n.md", "moved.md", old
                ),
                "remove": lambda: publication.remove_verified(lease, mutation_root, "n.md", old),
            }
            for name, call in calls.items():
                with pytest.raises(MutationError) as caught:
                    call()
                assert caught.value.code == "read_only_target", name
            assert _snapshot(root) == before
        finally:
            if sys.platform == "darwin":
                os.chflags(note, 0)


@posix_only
async def test_a_rename_the_kernel_refuses_answers_read_only_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Linux `chattr +i` shows only as the rename's EPERM. The backup rename, a move and a
    remove answer `read_only_target`, and nothing moves."""
    assert sys.platform != "win32"
    root, mutation_root = _vault(tmp_path)
    note = root / "n.md"
    note.write_bytes(b"old\n")
    old = _fingerprint(note, b"old\n")
    publication = VaultPublication()

    def refuse(*_args: object) -> None:
        raise PermissionError(errno.EPERM, os.strerror(errno.EPERM))

    async with MutationCoordinator().lease(mutation_root, ("n.md", "moved.md")) as lease:
        staged = _stage(publication, lease, mutation_root, "n.md", b"new\n")
        before = _snapshot(root)
        monkeypatch.setattr(osfs, "rename_at_no_replace", refuse)

        def back_up() -> None:
            with publication.prepare_replace(
                lease, staged, old.sha256, staged.backup_name
            ) as prepared:
                prepared.move_target_to_backup()

        calls = {
            "backup": back_up,
            "move": lambda: publication.move_verified(
                lease, mutation_root, "n.md", "moved.md", old
            ),
            "remove": lambda: publication.remove_verified(lease, mutation_root, "n.md", old),
        }
        for name, call in calls.items():
            with pytest.raises(MutationError) as caught:
                call()
            assert caught.value.code == "read_only_target", name
    assert _snapshot(root) == before


@posix_only
async def test_an_unsupported_rename_flag_answers_unsupported_filesystem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without the no-replace flag every rename step refuses `unsupported_filesystem`
    and nothing falls back to a hard link: no file moves or gains a second name."""
    assert sys.platform != "win32"
    root, mutation_root = _vault(tmp_path)
    note = root / "n.md"
    note.write_bytes(b"old\n")
    old = _fingerprint(note, b"old\n")
    publication = VaultPublication()
    async with MutationCoordinator().lease(mutation_root, ("n.md", "new.md", "moved.md")) as lease:
        created = _stage(publication, lease, mutation_root, "new.md", b"created\n")
        staged = _stage(publication, lease, mutation_root, "n.md", b"new\n")
        before = _snapshot(root)
        monkeypatch.setattr(osfs, "_rename_exclusive_at", lambda *_args: errno.ENOTSUP)

        def back_up() -> None:
            with publication.prepare_replace(
                lease, staged, old.sha256, staged.backup_name
            ) as prepared:
                prepared.move_target_to_backup()

        calls = {
            "publish": lambda: publication.publish_create(lease, created),
            "backup": back_up,
            "move": lambda: publication.move_verified(
                lease, mutation_root, "n.md", "moved.md", old
            ),
            "remove": lambda: publication.remove_verified(lease, mutation_root, "n.md", old),
        }
        for name, call in calls.items():
            with pytest.raises(MutationError) as caught:
                call()
            assert caught.value.code == "unsupported_filesystem", name
    assert _snapshot(root) == before
    assert [entry.stat().st_nlink for entry in root.iterdir()] == [1, 1, 1]


@posix_only
async def test_a_remove_leaves_no_tombstone(tmp_path: Path) -> None:
    """A note's remove moves it to a private tombstone name and unlinks that, so its folder
    keeps nothing of it, hidden or not."""
    assert sys.platform != "win32"
    root, mutation_root = _vault(tmp_path)
    folder = root / "sub"
    folder.mkdir()
    (folder / "n.md").write_bytes(b"note\n")
    (folder / "keep.md").write_bytes(b"keep\n")
    expected = _fingerprint(folder / "n.md", b"note\n")
    async with MutationCoordinator().lease(mutation_root, ("sub/n.md",)) as lease:
        VaultPublication().remove_verified(lease, mutation_root, "sub/n.md", expected)
    assert os.listdir(folder) == ["keep.md"]


@posix_only
async def test_folders_are_created_reused_and_refused(tmp_path: Path) -> None:
    """`ensure_folders` creates and reports only the missing folders, reuses the rest, refuses
    a link in the chain `unsafe_parent` without creating through it, and a part longer than
    NAME_MAX `invalid_ref`."""
    assert sys.platform != "win32"
    root, mutation_root = _vault(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "linked").symlink_to(outside, target_is_directory=True)
    publication = VaultPublication()
    # A lease on the root holds every folder chain under it.
    async with MutationCoordinator().lease(mutation_root, ()) as lease:
        assert publication.ensure_folders(lease, mutation_root, ("a", "b")) == ("a", "a/b")
        assert publication.ensure_folders(lease, mutation_root, ("a", "b")) == ()
        assert publication.ensure_folders(lease, mutation_root, ("a", "c")) == ("a/c",)
        before = _snapshot(root), _snapshot(outside)
        refusals = {
            ("linked", "c"): "unsafe_parent",
            ("x" * 256,): "invalid_ref",
            ("a", "x" * 256): "invalid_ref",
        }
        for parts, code in refusals.items():
            with pytest.raises(MutationError) as caught:
                publication.ensure_folders(lease, mutation_root, parts)
            assert caught.value.code == code, parts
    assert (_snapshot(root), _snapshot(outside)) == before
    assert sorted(os.listdir(root / "a")) == ["b", "c"]


@posix_only
async def test_a_move_crosses_folders_and_keeps_the_file(tmp_path: Path) -> None:
    assert sys.platform != "win32"
    root, mutation_root = _vault(tmp_path)
    for folder in ("from", "to"):
        (root / folder).mkdir()
    (root / "from" / "n.md").write_bytes(b"note\n")
    expected = _fingerprint(root / "from" / "n.md", b"note\n")
    async with MutationCoordinator().lease(mutation_root, ("from/n.md", "to/m.md")) as lease:
        moved = VaultPublication().move_verified(
            lease, mutation_root, "from/n.md", "to/m.md", expected
        )
    assert moved == PublishedPublication("to/m.md", expected)
    assert os.listdir(root / "from") == []
    assert _fingerprint(root / "to" / "m.md", b"note\n") == expected


@posix_only
async def test_a_case_only_rename_lands(tmp_path: Path) -> None:
    """The note takes its new spelling, on a case-sensitive volume as on APFS, whose
    no-replace rename may report the new spelling as taken by the note itself."""
    assert sys.platform != "win32"
    root, mutation_root = _vault(tmp_path)
    (root / "Note.md").write_bytes(b"note\n")
    expected = _fingerprint(root / "Note.md", b"note\n")
    async with MutationCoordinator().lease(mutation_root, ("Note.md", "note.md")) as lease:
        VaultPublication().move_verified(lease, mutation_root, "Note.md", "note.md", expected)
    assert os.listdir(root) == ["note.md"]
    assert _fingerprint(root / "note.md", b"note\n") == expected


@pytest.mark.skipif(sys.platform != "linux", reason="two spellings are two files on ext4")
async def test_a_case_only_move_onto_another_file_is_a_conflict(tmp_path: Path) -> None:
    """On a case-sensitive volume the other spelling is another note, which a rename
    never replaces."""
    assert sys.platform == "linux"
    root, mutation_root = _vault(tmp_path)
    (root / "Note.md").write_bytes(b"upper\n")
    (root / "note.md").write_bytes(b"lower\n")
    expected = _fingerprint(root / "Note.md", b"upper\n")
    before = _snapshot(root)
    async with MutationCoordinator().lease(mutation_root, ("Note.md", "note.md")) as lease:
        with pytest.raises(MutationError) as caught:
            VaultPublication().move_verified(lease, mutation_root, "Note.md", "note.md", expected)
    assert caught.value.code == "conflict"
    assert _snapshot(root) == before


@pytest.mark.skipif(sys.platform != "linux", reason="two spellings are two files on ext4")
def test_the_apfs_case_only_branch_replaces_only_the_note_itself(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On macOS a taken new spelling may be the note itself, met by a case-only rename, and only
    then does a plain rename run. A case-sensitive APFS volume, simulated here on ext4, holds
    both spellings as two files: the branch sees another file at the name, refuses `conflict`
    and keeps both."""
    assert sys.platform == "linux"
    root, _ = _vault(tmp_path)
    (root / "Note.md").write_bytes(b"upper\n")
    (root / "note.md").write_bytes(b"lower\n")
    monkeypatch.setattr(vault_mutation, "sys", types.SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(vault_mutation, "root_key", lambda path: str(path).casefold())
    dir_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    fd = os.open(root / "Note.md", os.O_RDONLY)
    try:
        with pytest.raises(MutationError) as caught:
            vault_mutation._rename_entry(fd, dir_fd, "Note.md", dir_fd, "note.md")
    finally:
        os.close(fd)
        os.close(dir_fd)
    assert caught.value.code == "conflict"
    assert _listing(root) == {"Note.md": b"upper\n", "note.md": b"lower\n"}
