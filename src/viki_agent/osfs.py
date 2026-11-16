"""File primitives several modules need: a rename that never replaces its destination, a
directory-fd-relative twin of it, a directory fsync, the final path of an open descriptor, the
filesystem type of a path, a type-checked bounded read, and the create-exclusive private file.
It starts no process."""

from __future__ import annotations

import ctypes
import errno
import os
import re
import stat
import sys
from collections.abc import Callable
from pathlib import Path
from typing import BinaryIO

# The native no-replace rename reports these when the kernel or the filesystem lacks the flag.
_UNSUPPORTED = frozenset({errno.EINVAL, errno.ENOSYS, errno.ENOTSUP, errno.EOPNOTSUPP})
_ESCAPE = re.compile(r"\\([0-7]{3})")

if sys.platform != "win32":
    import fcntl

    _libc = ctypes.CDLL(None, use_errno=True)

    if sys.platform == "darwin":
        _RENAME_EXCL = 0x4
        _renamex_np = getattr(_libc, "renamex_np", None)
        if _renamex_np is not None:
            _renamex_np.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint)
            _renamex_np.restype = ctypes.c_int

        _renameatx_np = getattr(_libc, "renameatx_np", None)
        if _renameatx_np is not None:
            _renameatx_np.argtypes = (
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_uint,
            )
            _renameatx_np.restype = ctypes.c_int

        class _Statfs(ctypes.Structure):
            # The arm64 `struct statfs` (64-bit inodes), the only macOS layout supported.
            _fields_ = [
                ("f_bsize", ctypes.c_uint32),
                ("f_iosize", ctypes.c_int32),
                ("f_blocks", ctypes.c_uint64),
                ("f_bfree", ctypes.c_uint64),
                ("f_bavail", ctypes.c_uint64),
                ("f_files", ctypes.c_uint64),
                ("f_ffree", ctypes.c_uint64),
                ("f_fsid", ctypes.c_int32 * 2),
                ("f_owner", ctypes.c_uint32),
                ("f_type", ctypes.c_uint32),
                ("f_flags", ctypes.c_uint32),
                ("f_fssubtype", ctypes.c_uint32),
                ("f_fstypename", ctypes.c_char * 16),
                ("f_mntonname", ctypes.c_char * 1024),
                ("f_mntfromname", ctypes.c_char * 1024),
                ("f_flags_ext", ctypes.c_uint32),
                ("f_reserved", ctypes.c_uint32 * 7),
            ]

        _statfs = _libc.statfs
        _statfs.argtypes = (ctypes.c_char_p, ctypes.POINTER(_Statfs))
        _statfs.restype = ctypes.c_int

        def _rename_exclusive(src: bytes, dst: bytes) -> int:
            """Return 0 once `src` is renamed to an absent `dst`, else the errno."""
            if _renamex_np is None:
                return errno.ENOSYS
            return 0 if _renamex_np(src, dst, _RENAME_EXCL) == 0 else ctypes.get_errno()

        def _rename_exclusive_at(src_dir_fd: int, src: bytes, dst_dir_fd: int, dst: bytes) -> int:
            """Return 0 once `src` under `src_dir_fd` is renamed to an absent `dst` under
            `dst_dir_fd`, else the errno."""
            if _renameatx_np is None:
                return errno.ENOSYS
            result = _renameatx_np(src_dir_fd, src, dst_dir_fd, dst, _RENAME_EXCL)
            return 0 if result == 0 else ctypes.get_errno()

    else:
        _AT_FDCWD = -100
        _RENAME_NOREPLACE = 1
        _renameat2 = getattr(_libc, "renameat2", None)  # glibc 2.28 and later
        if _renameat2 is not None:
            _renameat2.argtypes = (
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_uint,
            )
            _renameat2.restype = ctypes.c_int

        def _rename_exclusive(src: bytes, dst: bytes) -> int:
            """Return 0 once `src` is renamed to an absent `dst`, else the errno."""
            if _renameat2 is None:
                return errno.ENOSYS
            result = _renameat2(_AT_FDCWD, src, _AT_FDCWD, dst, _RENAME_NOREPLACE)
            return 0 if result == 0 else ctypes.get_errno()

        def _rename_exclusive_at(src_dir_fd: int, src: bytes, dst_dir_fd: int, dst: bytes) -> int:
            """Return 0 once `src` under `src_dir_fd` is renamed to an absent `dst` under
            `dst_dir_fd`, else the errno."""
            if _renameat2 is None:
                return errno.ENOSYS
            result = _renameat2(src_dir_fd, src, dst_dir_fd, dst, _RENAME_NOREPLACE)
            return 0 if result == 0 else ctypes.get_errno()

    def final_path(fd: int) -> str:
        """The path the kernel holds for an open descriptor: on macOS, the spelling on disk."""
        if sys.platform == "darwin":
            name = fcntl.fcntl(fd, fcntl.F_GETPATH, bytes(1024))
            return os.fsdecode(name.split(b"\0", 1)[0])
        return os.readlink(f"/proc/self/fd/{fd}")

    def filesystem_type(path: str) -> str:
        """The type name of the filesystem that holds `path`, such as `apfs` or `nfs4`."""
        if sys.platform == "darwin":
            info = _Statfs()
            if _statfs(os.fsencode(path), ctypes.byref(info)) != 0:
                code = ctypes.get_errno()
                raise OSError(code, os.strerror(code))
            return os.fsdecode(info.f_fstypename)
        # surrogateescape keeps a non-UTF-8 mount point comparable with os.fsdecode paths.
        mountinfo = Path("/proc/self/mountinfo").read_text(
            encoding="utf-8", errors="surrogateescape"
        )
        return _mount_type(mountinfo, os.path.realpath(path))

    def rename_at_no_replace(src_dir_fd: int, src: str, dst_dir_fd: int, dst: str) -> None:
        """Rename leaf `src` under `src_dir_fd` to leaf `dst` under `dst_dir_fd`, raising
        FileExistsError rather than replacing an existing `dst`.

        Raises `OSError(ENOTSUP)` when the kernel, libc or filesystem lacks the no-replace
        rename flag, or the native symbol is missing. Unlike `rename_no_replace`, this never
        falls back to a link-and-unlink: a directory-relative hard link has no portable ctypes
        binding, and the caller (a directory-fd-relative publication step) needs a plain refusal
        it can map to `unsupported_filesystem`, not a silent degrade.
        """
        source, target = os.fsencode(src), os.fsencode(dst)
        code = _rename_exclusive_at(src_dir_fd, source, dst_dir_fd, target)
        if code == 0:
            return
        if code in _UNSUPPORTED:
            raise OSError(errno.ENOTSUP, os.strerror(errno.ENOTSUP))
        raise OSError(code, os.strerror(code))  # EEXIST constructs FileExistsError.


def rename_no_replace(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
    """Rename `src` to `dst`, raising FileExistsError rather than replacing an existing `dst`."""
    if sys.platform == "win32":
        os.rename(src, dst)  # Windows rename never replaces an existing destination.
        return
    source, target = os.fsencode(src), os.fsencode(dst)
    code = _rename_exclusive(source, target)
    if code == 0:
        return
    if code not in _UNSUPPORTED:
        raise OSError(code, os.strerror(code))  # EEXIST constructs FileExistsError.
    # A hard link is create-exclusive too. It cannot move a directory, so that is refused.
    if stat.S_ISDIR(os.lstat(source).st_mode):
        raise OSError(errno.ENOTSUP, "no-replace directory rename is not supported here")
    os.link(source, target)
    os.unlink(source)


def fsync_dir(path: str | os.PathLike[str]) -> None:
    """Flush a rename or create in the directory to the drive. macOS drive caches also need
    F_FULLFSYNC, which neither this nor SQLite's default uses."""
    if sys.platform == "win32":
        return  # A directory cannot be opened for this on Windows.
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def read_regular(
    path: str | os.PathLike[str],
    limit: int,
    *,
    follow_symlinks: bool = True,
    pre_read: Callable[[int], None] | None = None,
) -> bytes:
    """Read at most `limit` bytes of a regular file.

    Refuses (OSError) a FIFO, a device or a directory, and a symlink when `follow_symlinks` is
    False, instead of blocking on or following one: a plain open can stall forever on a FIFO or
    a link to a terminal, and would otherwise follow a link past a caller's own check. A missing
    or permission-denied path raises as `open` already would. Windows does a plain read:
    the non-blocking, type-checked open is a POSIX mechanism. Both branches request at most
    `min(limit, size + 1)` bytes, sized from the open descriptor's own fstat, so a large `limit`
    never allocates more than the file can hold. `pre_read` may validate that descriptor before
    any bytes are read; a refusal closes it just as a read failure does.
    """
    if sys.platform == "win32":
        with open(path, "rb") as source:
            size = os.fstat(source.fileno()).st_size
            if pre_read is not None:
                pre_read(source.fileno())
            return source.read(min(limit, size + 1))
    # O_NOCTTY: a link to a terminal must not become this process's controlling terminal just
    # because it was opened; the plain open this replaces had the same exposure.
    flags = os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC | os.O_NOCTTY
    if not follow_symlinks:
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise OSError(errno.EINVAL, "refusing a non-regular file", os.fsdecode(path))
        if pre_read is not None:
            pre_read(fd)
        return os.read(fd, min(limit, info.st_size + 1))
    finally:
        os.close(fd)


def open_new_private(path: str | os.PathLike[str]) -> BinaryIO:
    """Create `path` for writing, refusing an existing path; mode 0600 on POSIX."""
    if sys.platform == "win32":
        return open(path, "xb")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    return os.fdopen(os.open(path, flags, 0o600), "wb")


def _mount_type(mountinfo: str, path: str) -> str:
    """The filesystem type of the mount in a /proc/self/mountinfo text that holds `path`."""
    found, longest = "", -1
    for line in mountinfo.splitlines():
        fields = line.split(" ")
        if "-" not in fields[6:]:
            continue
        separator = fields.index("-", 6)
        if separator + 1 >= len(fields):
            continue
        mount_point = _ESCAPE.sub(lambda match: chr(int(match[1], 8)), fields[4])
        inside = path == mount_point or path.startswith(mount_point.rstrip("/") + "/")
        # A later line on the same mount point is mounted over the earlier one, so it wins.
        if inside and len(mount_point) >= longest:
            found, longest = fields[separator + 1], len(mount_point)
    return found
