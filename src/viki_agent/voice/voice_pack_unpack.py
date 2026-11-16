"""Unpacks a pinned Voicebox release archive and records which revision is installed.

Each archive is a PyInstaller onedir tree: members are relative to the archive root, mostly
regular files with PAX headers and the occasional symlink. The archives are hash-pinned before
`extract` ever runs, but extraction still refuses any member that would land outside the
destination -- an absolute, drive or UNC path (member name or link target) on every OS, a `../`
traversal, or a symlink/hardlink target outside it -- by checking each member's anchor before
`tar.extract` and otherwise extracting under tarfile's "data" filter (defence in depth).
"""

from __future__ import annotations

import gzip
import os
import tarfile
import zlib
from collections.abc import Callable
from pathlib import Path, PureWindowsPath


def extract(
    archive: Path,
    destination: Path,
    *,
    progress: Callable[[int], None] = lambda done: None,
) -> str | None:
    """Unpack the `archive` tar.gz into `destination`, creating it (and its parents) first.

    Calls `progress` with the compressed bytes read so far, after each member. Returns None on
    success, `"bad_archive"` for a malformed or unsafe archive (including one a member of which
    would escape `destination`), or `"write_failed"` for any other write error.
    """
    destination.mkdir(parents=True, exist_ok=True)
    try:
        with archive.open("rb") as raw, tarfile.open(fileobj=raw, mode="r:gz") as tar:
            for member in tar:
                # An anchored name or link target -- rooted ("/x", "\x"), a drive ("C:\x"),
                # drive-relative ("C:x"), or a UNC share ("\\srv\share\x") -- must be refused
                # outright: the data filter only strips a leading "/" or `os.sep` and re-roots
                # what is left instead of refusing it, and a drive-relative member can land the
                # join and commonpath check on a different drive than destination, which raises
                # a bare ValueError that nothing here catches. PureWindowsPath reads both
                # separators on every OS, so this one check holds everywhere. A link target is
                # checked raw and as the data filter writes it (`os.path.normpath`): "./C:x"
                # becomes drive-relative only once normalized.
                if PureWindowsPath(member.name).anchor:
                    return "bad_archive"
                if (member.islnk() or member.issym()) and any(
                    PureWindowsPath(target).anchor
                    for target in (member.linkname, os.path.normpath(member.linkname))
                ):
                    return "bad_archive"
                tar.extract(member, destination, filter="data")
                progress(raw.tell())
    except gzip.BadGzipFile, EOFError, zlib.error, tarfile.TarError:
        return "bad_archive"
    except OSError:
        return "write_failed"
    return None


def write_ref(repo_dir: Path, revision: str) -> None:
    """Record `revision` as the installed revision in `repo_dir/refs/main` (ASCII, no newline).

    Written through `refs/main.part` and renamed into place with `os.replace`, so a reader never
    observes a partial ref.
    """
    refs_dir = repo_dir / "refs"
    refs_dir.mkdir(parents=True, exist_ok=True)
    part = refs_dir / "main.part"
    part.write_text(revision, encoding="ascii")
    os.replace(part, refs_dir / "main")
