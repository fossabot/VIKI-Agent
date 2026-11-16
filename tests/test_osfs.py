"""File primitives: no-replace rename, directory fsync, final path, filesystem type, private
create. Synthetic scratch files and a synthetic mountinfo text only."""

from __future__ import annotations

import errno
import os
import stat
import sys
import threading
from pathlib import Path
from typing import BinaryIO

import pytest

from viki_agent import osfs

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX primitives only")
macos_only = pytest.mark.skipif(sys.platform != "darwin", reason="macOS primitives only")
windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows primitive only")
# Only stops the test if the fix regresses and the open blocks on the FIFO again; a fixed open
# refuses a FIFO record at once, without waiting for a writer.
HANG_GUARD_S = 5


def test_rename_no_replace_moves_a_file(tmp_path: Path) -> None:
    src, dst = tmp_path / "stage", tmp_path / "final"
    src.write_bytes(b"synthetic stage")
    osfs.rename_no_replace(src, dst)
    assert not src.exists()
    assert dst.read_bytes() == b"synthetic stage"


def test_rename_no_replace_refuses_an_existing_destination(tmp_path: Path) -> None:
    src, dst = tmp_path / "stage", tmp_path / "final"
    src.write_bytes(b"synthetic stage")
    dst.write_bytes(b"synthetic final")
    with pytest.raises(FileExistsError):
        osfs.rename_no_replace(src, dst)
    assert src.read_bytes() == b"synthetic stage"
    assert dst.read_bytes() == b"synthetic final"


def test_rename_no_replace_reports_a_missing_source(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        osfs.rename_no_replace(tmp_path / "absent", tmp_path / "final")
    assert not (tmp_path / "final").exists()


def test_open_new_private_refuses_an_existing_path(tmp_path: Path) -> None:
    path = tmp_path / "blob"
    with osfs.open_new_private(path) as handle:
        handle.write(b"synthetic blob")
    with pytest.raises(FileExistsError):
        osfs.open_new_private(path)
    assert path.read_bytes() == b"synthetic blob"


_MOUNTINFO = "\n".join(
    [
        "22 1 8:1 / / rw,relatime shared:1 - ext4 /dev/sda1 rw",
        "30 22 8:2 / /home rw,relatime shared:2 - xfs /dev/sda2 rw",
        "31 30 0:40 / /home/user/My\\040Share rw,relatime shared:3 - cifs //server/share rw",
        "32 30 0:41 / /home/user/remote rw,nosuid master:4 shared:5 - fuse.sshfs host:/ rw",
        "33 22 0:42 / /homework rw - tmpfs tmpfs rw",
        "",
    ]
)


@pytest.mark.parametrize(
    ("path", "kind"),
    [
        ("/", "ext4"),
        ("/etc/hosts", "ext4"),
        ("/home", "xfs"),
        ("/home/user/notes", "xfs"),
        ("/home/user/My Share", "cifs"),
        ("/home/user/My Share/vault", "cifs"),
        ("/home/user/My Shared", "xfs"),
        ("/home/user/remote/vault", "fuse.sshfs"),
        ("/homework/vault", "tmpfs"),
    ],
)
def test_mount_type_picks_the_longest_containing_mount(path: str, kind: str) -> None:
    assert osfs._mount_type(_MOUNTINFO, path) == kind


def test_mount_type_is_empty_when_no_mount_contains_the_path() -> None:
    assert osfs._mount_type(_MOUNTINFO.replace(" / / ", " / /sys "), "/home2/vault") == ""
    assert osfs._mount_type("", "/home/vault") == ""


def test_mount_type_takes_the_later_of_two_mounts_on_one_point() -> None:
    stacked = _MOUNTINFO + "40 30 0:50 / /home rw - nfs4 server:/home rw\n"
    assert osfs._mount_type(stacked, "/home/user/notes") == "nfs4"


@posix_only
def test_open_new_private_creates_mode_0600_under_umask_022(tmp_path: Path) -> None:
    assert sys.platform != "win32"
    previous = os.umask(0o022)
    try:
        with osfs.open_new_private(tmp_path / "blob") as handle:
            handle.write(b"synthetic blob")
    finally:
        os.umask(previous)
    assert stat.S_IMODE(os.lstat(tmp_path / "blob").st_mode) == 0o600


@posix_only
def test_open_new_private_refuses_a_symlink(tmp_path: Path) -> None:
    assert sys.platform != "win32"
    target = tmp_path / "target"
    (tmp_path / "link").symlink_to(target)
    with pytest.raises(FileExistsError):
        osfs.open_new_private(tmp_path / "link")
    assert not target.exists()


@posix_only
def test_rename_no_replace_refuses_an_existing_empty_directory(tmp_path: Path) -> None:
    assert sys.platform != "win32"
    src, dst = tmp_path / "stage", tmp_path / "final"
    src.mkdir()
    (src / "source").write_bytes(b"synthetic avatar")
    dst.mkdir()
    with pytest.raises(FileExistsError):
        osfs.rename_no_replace(src, dst)
    assert (src / "source").read_bytes() == b"synthetic avatar"
    assert list(dst.iterdir()) == []


@posix_only
def test_unsupported_flag_falls_back_to_link_then_unlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert sys.platform != "win32"
    monkeypatch.setattr(osfs, "_rename_exclusive", lambda src, dst: errno.EINVAL)
    src, dst = tmp_path / "stage", tmp_path / "final"
    src.write_bytes(b"synthetic stage")
    osfs.rename_no_replace(src, dst)
    assert not src.exists()
    assert dst.read_bytes() == b"synthetic stage"

    src.write_bytes(b"synthetic second")
    with pytest.raises(FileExistsError):
        osfs.rename_no_replace(src, dst)
    assert src.read_bytes() == b"synthetic second"
    assert dst.read_bytes() == b"synthetic stage"

    directory = tmp_path / "stage-dir"
    directory.mkdir()
    with pytest.raises(OSError) as refused:
        osfs.rename_no_replace(directory, tmp_path / "final-dir")
    assert refused.value.errno == errno.ENOTSUP
    assert directory.is_dir()
    assert not (tmp_path / "final-dir").exists()


@posix_only
def test_other_native_errors_are_raised(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert sys.platform != "win32"
    monkeypatch.setattr(osfs, "_rename_exclusive", lambda src, dst: errno.EACCES)
    src = tmp_path / "stage"
    src.write_bytes(b"synthetic stage")
    with pytest.raises(PermissionError):
        osfs.rename_no_replace(src, tmp_path / "final")
    assert src.exists()


@posix_only
def test_rename_at_no_replace_moves_a_file_between_directory_descriptors(tmp_path: Path) -> None:
    assert sys.platform != "win32"
    src_dir, dst_dir = tmp_path / "src", tmp_path / "dst"
    src_dir.mkdir()
    dst_dir.mkdir()
    (src_dir / "note.md").write_bytes(b"synthetic note")
    src_fd = os.open(src_dir, os.O_RDONLY | os.O_DIRECTORY)
    dst_fd = os.open(dst_dir, os.O_RDONLY | os.O_DIRECTORY)
    try:
        osfs.rename_at_no_replace(src_fd, "note.md", dst_fd, "note.md")
    finally:
        os.close(src_fd)
        os.close(dst_fd)
    assert not (src_dir / "note.md").exists()
    assert (dst_dir / "note.md").read_bytes() == b"synthetic note"


@posix_only
def test_rename_at_no_replace_refuses_an_existing_destination(tmp_path: Path) -> None:
    assert sys.platform != "win32"
    src_dir, dst_dir = tmp_path / "src", tmp_path / "dst"
    src_dir.mkdir()
    dst_dir.mkdir()
    (src_dir / "note.md").write_bytes(b"synthetic stage")
    (dst_dir / "note.md").write_bytes(b"synthetic final")
    src_fd = os.open(src_dir, os.O_RDONLY | os.O_DIRECTORY)
    dst_fd = os.open(dst_dir, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(FileExistsError):
            osfs.rename_at_no_replace(src_fd, "note.md", dst_fd, "note.md")
    finally:
        os.close(src_fd)
        os.close(dst_fd)
    assert (src_dir / "note.md").read_bytes() == b"synthetic stage"
    assert (dst_dir / "note.md").read_bytes() == b"synthetic final"


@posix_only
def test_rename_at_no_replace_reports_an_unsupported_flag_without_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert sys.platform != "win32"
    src_dir, dst_dir = tmp_path / "src", tmp_path / "dst"
    src_dir.mkdir()
    dst_dir.mkdir()
    source = src_dir / "note.md"
    source.write_bytes(b"synthetic stage")
    src_fd = os.open(src_dir, os.O_RDONLY | os.O_DIRECTORY)
    dst_fd = os.open(dst_dir, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for unsupported in (errno.EINVAL, errno.ENOSYS):
            monkeypatch.setattr(osfs, "_rename_exclusive_at", lambda *a, code=unsupported: code)
            with pytest.raises(OSError) as refused:
                osfs.rename_at_no_replace(src_fd, "note.md", dst_fd, "note.md")
            assert refused.value.errno == errno.ENOTSUP
            # No link-and-unlink fallback ran: the source keeps its one name and its single link.
            assert source.read_bytes() == b"synthetic stage"
            assert not (dst_dir / "note.md").exists()
            assert os.stat(source).st_nlink == 1
    finally:
        os.close(src_fd)
        os.close(dst_fd)


@posix_only
def test_fsync_dir_and_filesystem_type_on_scratch(tmp_path: Path) -> None:
    assert sys.platform != "win32"
    osfs.fsync_dir(tmp_path)
    assert osfs.filesystem_type(str(tmp_path)) != ""


@posix_only
def test_final_path_names_the_open_file(tmp_path: Path) -> None:
    assert sys.platform != "win32"
    path = tmp_path / "note.md"
    path.write_bytes(b"synthetic note")
    fd = os.open(path, os.O_RDONLY)
    try:
        assert osfs.final_path(fd) == os.path.realpath(path)
    finally:
        os.close(fd)


@macos_only
def test_final_path_returns_the_spelling_on_disk(tmp_path: Path) -> None:
    assert sys.platform == "darwin"
    (tmp_path / "Notes").mkdir()
    fd = os.open(tmp_path / "notes", os.O_RDONLY)
    try:
        assert osfs.final_path(fd).endswith("/Notes")
    finally:
        os.close(fd)


@macos_only
def test_filesystem_type_reads_apfs(tmp_path: Path) -> None:
    assert sys.platform == "darwin"
    assert osfs.filesystem_type(str(tmp_path)) == "apfs"


def test_read_regular_reads_at_most_the_bound(tmp_path: Path) -> None:
    path = tmp_path / "note.txt"
    path.write_bytes(b"hello world")
    assert osfs.read_regular(path, 5) == b"hello"
    assert osfs.read_regular(path, 100) == b"hello world"


@pytest.mark.parametrize("refuse", [False, True])
def test_read_regular_validates_before_bytes_and_closes_the_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, refuse: bool
) -> None:
    path = tmp_path / "note.txt"
    path.write_bytes(b"synthetic text")
    opened: list[int] = []
    reads: list[int] = []
    if sys.platform == "win32":
        real_open = open

        def spy_open(*args: object, **kwargs: object) -> BinaryIO:
            source = real_open(*args, **kwargs)  # type: ignore[call-overload]
            read = source.read

            def spy_read(size: int = -1) -> bytes:
                reads.append(size)
                return read(size)

            source.read = spy_read  # type: ignore[method-assign]
            return source

        monkeypatch.setattr(osfs, "open", spy_open, raising=False)
    else:
        real_read = os.read

        def spy_os_read(fd: int, size: int) -> bytes:
            reads.append(size)
            return real_read(fd, size)

        monkeypatch.setattr(os, "read", spy_os_read)

    def validate(fd: int) -> None:
        assert reads == []
        assert stat.S_ISREG(os.fstat(fd).st_mode)
        opened.append(fd)
        if refuse:
            raise ValueError("synthetic refusal")

    if refuse:
        with pytest.raises(ValueError, match="synthetic refusal"):
            osfs.read_regular(path, 10, pre_read=validate)
        assert reads == []
    else:
        assert osfs.read_regular(path, 10, pre_read=validate) == b"synthetic "
        assert reads == [10]
    (fd,) = opened
    with pytest.raises(OSError):
        os.fstat(fd)


@windows_only
def test_read_regular_bounds_the_windows_read_to_the_files_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert sys.platform == "win32"
    small = tmp_path / "small.txt"
    small.write_bytes(b"hello")
    large = tmp_path / "large.txt"
    large.write_bytes(b"x" * 200)
    requested: list[int] = []
    real_open = open

    def spy_open(*args: object, **kwargs: object) -> BinaryIO:
        source = real_open(*args, **kwargs)  # type: ignore[call-overload]
        real_read = source.read

        def spy_read(size: int = -1) -> bytes:
            requested.append(size)
            return real_read(size)

        source.read = spy_read  # type: ignore[method-assign]
        return source

    monkeypatch.setattr(osfs, "open", spy_open, raising=False)
    assert osfs.read_regular(small, 1024 * 1024) == b"hello"
    assert osfs.read_regular(large, 100) == b"x" * 100
    assert requested == [6, 100]  # size + 1 for the 5-byte file, the limit for the larger one


@posix_only
def test_read_regular_bounds_the_posix_read_to_the_files_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert sys.platform != "win32"
    small = tmp_path / "small.txt"
    small.write_bytes(b"hello")
    large = tmp_path / "large.txt"
    large.write_bytes(b"x" * 200)
    requested: list[int] = []
    real_read = os.read

    def spy_read(fd: int, size: int) -> bytes:
        requested.append(size)
        return real_read(fd, size)

    monkeypatch.setattr(os, "read", spy_read)
    assert osfs.read_regular(small, 1024 * 1024) == b"hello"
    assert osfs.read_regular(large, 100) == b"x" * 100
    assert requested == [6, 100]  # size + 1 for the 5-byte file, the limit for the larger one


@posix_only
def test_read_regular_opens_with_o_noctty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A mutant that drops O_NOCTTY from the open flags must fail this."""
    assert sys.platform != "win32"
    path = tmp_path / "note.txt"
    path.write_bytes(b"hello")
    seen_flags: list[int] = []
    real_open = os.open

    def spy_open(path_arg: object, flags: int, *args: object) -> int:
        seen_flags.append(flags)
        return real_open(path_arg, flags, *args)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "open", spy_open)
    osfs.read_regular(path, 10)
    assert seen_flags[0] & os.O_NOCTTY != 0


def test_read_regular_reports_a_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        osfs.read_regular(tmp_path / "does-not-exist", 10)


@posix_only
def test_read_regular_refuses_a_directory(tmp_path: Path) -> None:
    assert sys.platform != "win32"
    # errno.EINVAL, not os.read's own EISDIR: this proves the type check runs, not merely that
    # os.read happens to refuse a directory on its own.
    with pytest.raises(OSError) as refused:
        osfs.read_regular(tmp_path, 10)
    assert refused.value.errno == errno.EINVAL


@posix_only
def test_read_regular_refuses_a_device(tmp_path: Path) -> None:
    """A type check narrowed to `S_ISFIFO` (devices allowed) would read /dev/null's or
    /dev/zero's bytes instead of refusing them; this must fail under that mutant."""
    assert sys.platform != "win32"
    link = tmp_path / "linked-device"
    link.symlink_to("/dev/null")
    with pytest.raises(OSError):
        osfs.read_regular(link, 10)


@posix_only
def test_read_regular_refuses_a_symlink_when_told_not_to_follow(tmp_path: Path) -> None:
    assert sys.platform != "win32"
    target = tmp_path / "target.txt"
    target.write_bytes(b"secret")
    link = tmp_path / "link.txt"
    link.symlink_to(target)
    with pytest.raises(OSError):
        osfs.read_regular(link, 10, follow_symlinks=False)
    assert osfs.read_regular(link, 10) == b"secret"  # following is still the default


@posix_only
def test_read_regular_refuses_a_fifo_without_blocking(tmp_path: Path) -> None:
    assert sys.platform != "win32"
    fifo = tmp_path / "settings.json"
    os.mkfifo(fifo)

    outcome: list[BaseException | None] = []

    def call() -> None:
        try:
            osfs.read_regular(fifo, 4096)
        except BaseException as exc:  # noqa: BLE001 - reported on the main thread below
            outcome.append(exc)
        else:
            outcome.append(None)

    thread = threading.Thread(target=call, daemon=True)
    thread.start()
    thread.join(HANG_GUARD_S)
    if thread.is_alive():
        # Release the blocked reader before failing, so a failing run never leaves a hung thread.
        writer = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
        os.close(writer)
        thread.join(HANG_GUARD_S)
        pytest.fail("the open blocked on the FIFO instead of refusing it at once")

    assert isinstance(outcome[0], OSError)
