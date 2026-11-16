"""The POSIX rename rule's pre-check, post-check and undo under deterministic interleavings, and
the directory fsyncs every namespace change makes before its step returns.

No sharing mode keeps another program off a held file here, so each no-replace rename checks
the entry's identity before it moves anything, and its identity and bytes after, moving back on
a mismatch. These tests race that window directly: an external program acts (an atomic-save
swap, an in-place write, a fresh file claiming a vacated name) at the one moment the
implementation cannot see without checking, and assert the typed outcome the rule promises.
"""

from __future__ import annotations

import hashlib
import logging
import os
import signal
import socket
import stat
import sys
from pathlib import Path

import pytest

from viki_agent import osfs
from viki_agent.vaults import vault_mutation
from viki_agent.vaults.vault_mutation import (
    BACKUP_PREFIX,
    FileFingerprint,
    MutationCoordinator,
    MutationError,
    MutationLease,
    OrdinaryMutationRoot,
    StagedPublication,
    VaultPublication,
)
from viki_agent.workspaces import root_key

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="the POSIX note writer's races")


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


def _snapshot(root: Path) -> list[tuple[str, int, int, str]]:
    """Every entry under `root` by lstat: a link, a FIFO, a socket or a folder is recorded by its
    mode and inode, never followed or opened."""
    entries: list[tuple[str, int, int, str]] = []
    for path in root.rglob("*"):
        info = path.lstat()
        digest = hashlib.sha256(path.read_bytes()).hexdigest() if stat.S_ISREG(info.st_mode) else ""
        entries.append((str(path.relative_to(root)), info.st_mode, info.st_ino, digest))
    return sorted(entries)


def _stage(
    publication: VaultPublication,
    lease: MutationLease,
    root: OrdinaryMutationRoot,
    ref: str,
    content: bytes,
) -> StagedPublication:
    return publication.stage(lease, root, ref, publication.reserve(), content)


def _atomic_swap(path: Path, content: bytes) -> None:
    """What an external editor's atomic save looks like: a new inode renamed over the name."""
    replacement = path.with_name(path.name + ".editor-swap")
    replacement.write_bytes(content)
    os.rename(replacement, path)


async def test_a_note_replaced_before_the_backup_rename_is_a_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An atomic-save swap changes the name's identity before the backup rename starts, so the
    pre-check refuses `conflict` and never attempts a rename at all: a missing pre-check would
    still be caught here, since the post-check could otherwise undo the result."""
    assert sys.platform != "win32"
    root, mutation_root = _vault(tmp_path)
    note = root / "n.md"
    note.write_bytes(b"old\n")
    old = _fingerprint(note, b"old\n")
    publication = VaultPublication()
    real_rename = osfs.rename_at_no_replace
    attempts: list[str] = []

    def counting(src_dir_fd: int, src: str, dst_dir_fd: int, dst: str) -> None:
        attempts.append(dst)
        real_rename(src_dir_fd, src, dst_dir_fd, dst)

    async with MutationCoordinator().lease(mutation_root, ("n.md",)) as lease:
        staged = _stage(publication, lease, mutation_root, "n.md", b"new\n")
        with publication.prepare_replace(lease, staged, old.sha256, staged.backup_name) as prepared:
            _atomic_swap(note, b"theirs\n")
            monkeypatch.setattr(osfs, "rename_at_no_replace", counting)
            with pytest.raises(MutationError) as caught:
                prepared.move_target_to_backup()
    assert caught.value.code == "conflict"
    assert attempts == []
    assert note.read_bytes() == b"theirs\n"
    assert (root / staged.stage_name).read_bytes() == b"new\n"
    assert not (root / staged.backup_name).exists()


async def test_an_in_place_write_before_the_backup_rename_is_moved_back(tmp_path: Path) -> None:
    """An in-place write keeps the pre-check's identity, so the backup rename proceeds;
    the post-check's re-hash then catches the changed bytes and moves the file back."""
    assert sys.platform != "win32"
    root, mutation_root = _vault(tmp_path)
    note = root / "n.md"
    note.write_bytes(b"old\n")
    old = _fingerprint(note, b"old\n")
    publication = VaultPublication()
    async with MutationCoordinator().lease(mutation_root, ("n.md",)) as lease:
        staged = _stage(publication, lease, mutation_root, "n.md", b"new\n")
        with publication.prepare_replace(lease, staged, old.sha256, staged.backup_name) as prepared:
            note.write_bytes(b"theirs\n")  # the same inode the pre-check saw, new bytes
            with pytest.raises(MutationError) as caught:
                prepared.move_target_to_backup()
    assert caught.value.code == "conflict"
    assert note.read_bytes() == b"theirs\n"
    assert (root / staged.stage_name).read_bytes() == b"new\n"
    assert not (root / staged.backup_name).exists()


async def test_a_file_created_at_the_name_after_the_backup_rename_is_kept(tmp_path: Path) -> None:
    """Once the backup rename vacates the note's name, another program may claim it
    first; the stage's own publish rename then refuses `conflict` through the kernel's no-replace
    check, and the intruding file, the backup and the stage all survive untouched."""
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
            note.write_bytes(b"theirs\n")  # another program claims the name while it is vacant
            with pytest.raises(MutationError) as caught:
                prepared.publish_stage_no_replace()
    assert caught.value.code == "conflict"
    assert note.read_bytes() == b"theirs\n"
    assert (root / staged.backup_name).read_bytes() == b"old\n"
    assert (root / staged.stage_name).read_bytes() == b"new\n"


async def test_a_note_swapped_before_delete_is_restored_and_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A delete's own tombstone rename can race an external swap the same way a replace
    does; the post-check undoes it, so the deletion answers `conflict` and the other program's
    bytes land back at the note's name instead of vanishing under the tombstone."""
    assert sys.platform != "win32"
    root, mutation_root = _vault(tmp_path)
    note = root / "n.md"
    note.write_bytes(b"old\n")
    expected = _fingerprint(note, b"old\n")
    real_rename = osfs.rename_at_no_replace
    attempts: list[str] = []

    def racing(src_dir_fd: int, src: str, dst_dir_fd: int, dst: str) -> None:
        attempts.append(dst)
        if len(attempts) == 1:
            # Between the delete's own pre-check and its tombstone rename: an external editor's
            # atomic save. The rename below then moves *its* file to the tombstone name by
            # mistake, which the post-check below must catch.
            _atomic_swap(note, b"theirs\n")
        real_rename(src_dir_fd, src, dst_dir_fd, dst)

    monkeypatch.setattr(osfs, "rename_at_no_replace", racing)
    async with MutationCoordinator().lease(mutation_root, ("n.md",)) as lease:
        with pytest.raises(MutationError) as caught:
            VaultPublication().remove_verified(lease, mutation_root, "n.md", expected)
    assert caught.value.code == "conflict"
    assert len(attempts) == 2
    assert attempts[0].startswith(BACKUP_PREFIX)
    assert attempts[1] == "n.md"
    assert os.listdir(root) == ["n.md"]
    assert note.read_bytes() == b"theirs\n"


async def test_a_failed_undo_loses_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """When the backup copy is itself edited in place and a new file claims the vacated
    name before the post-check runs, the undo's own rename meets that new file and fails. The
    refusal is `unsafe_target`, not the usual `conflict`, the warning names no path, and every
    byte set by any party (ours, the backup's edit, the new file) stays on disk."""
    assert sys.platform != "win32"
    root, mutation_root = _vault(tmp_path)
    note = root / "n.md"
    note.write_bytes(b"old\n")
    old = _fingerprint(note, b"old\n")
    publication = VaultPublication()
    real_rename = osfs.rename_at_no_replace
    attempts: list[str] = []

    def racing(src_dir_fd: int, src: str, dst_dir_fd: int, dst: str) -> None:
        attempts.append(dst)
        real_rename(src_dir_fd, src, dst_dir_fd, dst)
        if len(attempts) == 1:
            # Right after the backup rename lands: an in-place edit of the backup copy, and a
            # fresh file claiming the name the backup rename just vacated.
            (root / dst).write_bytes(b"changed\n")
            note.write_bytes(b"created\n")

    async with MutationCoordinator().lease(mutation_root, ("n.md",)) as lease:
        staged = _stage(publication, lease, mutation_root, "n.md", b"new\n")
        with publication.prepare_replace(lease, staged, old.sha256, staged.backup_name) as prepared:
            monkeypatch.setattr(osfs, "rename_at_no_replace", racing)
            with caplog.at_level(logging.WARNING), pytest.raises(MutationError) as caught:
                prepared.move_target_to_backup()
    assert caught.value.code == "unsafe_target"
    assert note.read_bytes() == b"created\n"
    assert (root / staged.backup_name).read_bytes() == b"changed\n"
    assert (root / staged.stage_name).read_bytes() == b"new\n"
    messages = [record.getMessage() for record in caplog.records]
    assert messages == ["vault rename undo failed"]
    assert all(str(root) not in message and "n.md" not in message for message in messages)


async def test_a_stage_swapped_before_its_unlink_is_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A discard's unlink checks the stage's identity one last time before removing it; a stage
    swapped out from under it refuses `stale_stage` and the swapped-in file survives."""
    assert sys.platform != "win32"
    root, mutation_root = _vault(tmp_path)
    publication = VaultPublication()
    async with MutationCoordinator().lease(mutation_root, ("n.md",)) as lease:
        staged = _stage(publication, lease, mutation_root, "n.md", b"new\n")
        real_holds = vault_mutation._holds

        def swapping(dir_fd: int, name: str, fd: int) -> bool:
            if name == staged.stage_name:
                _atomic_swap(root / name, b"theirs\n")
            return real_holds(dir_fd, name, fd)

        monkeypatch.setattr(vault_mutation, "_holds", swapping)
        with pytest.raises(MutationError) as caught:
            publication.discard(lease, staged)
    assert caught.value.code == "stale_stage"
    assert (root / staged.stage_name).read_bytes() == b"theirs\n"


async def test_every_namespace_change_syncs_its_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stage create, a rename within one folder, a rename across two, an unlink's own
    tombstone rename and unlink, and a mkdir each fsync every directory they changed, before the
    step that made the change returns."""
    assert sys.platform != "win32"
    root, mutation_root = _vault(tmp_path)
    (root / "a").mkdir()
    (root / "a" / "n.md").write_bytes(b"old\n")
    moved = _fingerprint(root / "a" / "n.md", b"old\n")
    real_fsync = vault_mutation.os.fsync
    synced: list[str] = []

    def spy(fd: int) -> None:
        info = os.fstat(fd)
        if stat.S_ISDIR(info.st_mode):
            synced.append(vault_mutation._physical_id(info))
        real_fsync(fd)

    def folder(path: Path) -> str:
        return vault_mutation._physical_id(os.stat(path))

    publication = VaultPublication()
    refs = ("a/n.md", "b/c/m.md", "a/x.md")
    async with MutationCoordinator().lease(mutation_root, refs) as lease:
        monkeypatch.setattr(vault_mutation.os, "fsync", spy)

        staged = _stage(publication, lease, mutation_root, "a/x.md", b"x\n")
        assert synced == [folder(root / "a")], "a stage create fsyncs its own folder"
        synced.clear()

        publication.publish_create(lease, staged)
        assert synced == [folder(root / "a")], "a rename within one folder fsyncs it once"
        synced.clear()

        created = publication.ensure_folders(lease, mutation_root, ("b", "c"))
        assert created == ("b", "b/c")
        assert synced == [folder(root), folder(root / "b")], "each mkdir fsyncs its own parent"
        synced.clear()

        publication.move_verified(lease, mutation_root, "a/n.md", "b/c/m.md", moved)
        assert synced == [
            folder(root / "b" / "c"),
            folder(root / "a"),
        ], "a cross-folder rename fsyncs both, destination first"
        synced.clear()

        publication.remove_verified(lease, mutation_root, "b/c/m.md", moved)
        assert synced == [folder(root / "b" / "c")] * 2, (
            "the tombstone rename and the unlink each fsync that one folder"
        )


@pytest.mark.parametrize("shape", ["fifo", "socket", "symlink_to_device"])
async def test_special_files_are_refused(tmp_path: Path, shape: str) -> None:
    """Another program's race may leave a FIFO, a UNIX socket, or a symlink to a device at
    the note's name instead of a plain file. Every open is non-blocking and follows no link, so
    each is refused typed, the FIFO open never blocks, and the device behind the symlink is
    never reached."""
    assert sys.platform != "win32"
    root, mutation_root = _vault(tmp_path)
    note = root / "n.md"
    note.write_bytes(b"old\n")
    expected = _fingerprint(note, b"old\n")
    note.unlink()
    if shape == "fifo":
        os.mkfifo(note)
    elif shape == "socket":
        # A socket path is capped at 104 bytes on macOS (108 on Linux), and the runners' temp
        # paths run longer: bind the bare name from inside the folder.
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        cwd = os.getcwd()
        os.chdir(note.parent)
        try:
            listener.bind(note.name)
        finally:
            os.chdir(cwd)
            listener.close()
    else:
        note.symlink_to("/dev/null")
    before = _snapshot(root)
    publication = VaultPublication()

    def refuse_a_blocked_open(signum: int, frame: object) -> None:
        raise TimeoutError("the open blocked past its bound: O_NONBLOCK regressed")

    previous_handler = signal.signal(signal.SIGALRM, refuse_a_blocked_open)
    signal.setitimer(signal.ITIMER_REAL, 5.0)
    try:
        async with MutationCoordinator().lease(mutation_root, ("n.md",)) as lease:
            with pytest.raises(MutationError) as caught:
                publication.read_verified(lease, mutation_root, "n.md", expected)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
    assert caught.value.code == "unsafe_target"
    assert _snapshot(root) == before
