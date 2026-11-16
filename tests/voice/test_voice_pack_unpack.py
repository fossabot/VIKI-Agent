"""Archive extraction under tarfile's data filter, and the installed-revision ref, in tmp_path."""

from __future__ import annotations

import io
import os
import tarfile
from collections.abc import Callable
from pathlib import Path

import pytest

from viki_agent.voice.voice_pack_unpack import extract, write_ref


def _archive(path: Path, add: Callable[[tarfile.TarFile], None]) -> Path:
    with tarfile.open(path, "w:gz") as tar:
        add(tar)
    return path


def _add_file(tar: tarfile.TarFile, name: str, content: bytes = b"data") -> None:
    info = tarfile.TarInfo(name=name)
    info.size = len(content)
    tar.addfile(info, io.BytesIO(content))


def _add_symlink(tar: tarfile.TarFile, name: str, linkname: str) -> None:
    # A real filesystem symlink may need privileges on Windows; the archive never needs one,
    # since a tar symlink member is just a header with a linkname and no data.
    info = tarfile.TarInfo(name=name)
    info.type = tarfile.SYMTYPE
    info.linkname = linkname
    tar.addfile(info)


def test_good_tree_extracts_and_progress_reaches_archive_size(tmp_path: Path) -> None:
    archive = _archive(
        tmp_path / "good.tar.gz",
        lambda tar: _add_file(tar, "_internal/sub/file.txt", b"hello voicebox"),
    )
    destination = tmp_path / "dest"
    seen: list[int] = []

    code = extract(archive, destination, progress=seen.append)

    assert code is None
    assert (destination / "_internal" / "sub" / "file.txt").read_bytes() == b"hello voicebox"
    assert seen and seen[-1] == archive.stat().st_size
    assert seen == sorted(seen)


def test_a_member_outside_the_destination_is_refused(tmp_path: Path) -> None:
    archive = _archive(tmp_path / "traversal.tar.gz", lambda tar: _add_file(tar, "../escaped.txt"))
    destination = tmp_path / "dest"

    code = extract(archive, destination)

    assert code == "bad_archive"
    assert not (tmp_path / "escaped.txt").exists()


def test_an_absolute_member_is_refused(tmp_path: Path) -> None:
    destination = tmp_path / "dest"
    # Absolute on Windows (drive letter), but still inside tmp_path: if the filter ever failed
    # to catch it, the stray write would land in the sandbox, not on a real system path.
    outside = tmp_path / "evil_abs.txt"
    archive = _archive(tmp_path / "absolute.tar.gz", lambda tar: _add_file(tar, outside.as_posix()))

    code = extract(archive, destination)

    assert code == "bad_archive"
    assert not outside.exists()


@pytest.mark.parametrize(
    "name",
    ["/abs/evil.txt", "C:/evil.txt", "C:evil.txt", "\\evil.txt", "\\\\srv\\share\\evil.txt"],
)
def test_an_anchored_member_name_is_refused_on_every_os(tmp_path: Path, name: str) -> None:
    # Each name is rooted, drive, drive-relative or UNC under PureWindowsPath -- the data filter
    # alone only rejects some of these (a leading "/" or "\" is stripped first, re-rooting the
    # member inside destination instead of refusing it), so this must hold without relying on it.
    archive = _archive(tmp_path / "evil.tar.gz", lambda tar: _add_file(tar, name))
    destination = tmp_path / "dest"

    code = extract(archive, destination)

    assert code == "bad_archive"
    assert [p for p in tmp_path.rglob("*evil*") if p != archive] == []


# The last two are anchored only once normalized, which is what the data filter writes.
@pytest.mark.parametrize("linkname", ["/abs/target", "C:target", "./C:target", "a/../C:target"])
def test_an_anchored_link_target_is_refused_on_every_os(tmp_path: Path, linkname: str) -> None:
    archive = _archive(
        tmp_path / "linked.tar.gz", lambda tar: _add_symlink(tar, "link_out", linkname)
    )
    destination = tmp_path / "dest"

    code = extract(archive, destination)

    assert code == "bad_archive"
    # lexists, not exists: each target dangles, so exists() would miss a link already written.
    assert not os.path.lexists(destination / "link_out")


def test_a_symlink_pointing_outside_the_destination_is_refused(tmp_path: Path) -> None:
    destination = tmp_path / "dest"
    archive = _archive(
        tmp_path / "symlink.tar.gz",
        lambda tar: _add_symlink(tar, "link_out", "../outside_marker"),
    )

    code = extract(archive, destination)

    assert code == "bad_archive"
    assert not (tmp_path / "outside_marker").exists()
    assert not os.path.lexists(destination / "link_out")


def test_a_truncated_archive_is_refused(tmp_path: Path) -> None:
    good = _archive(tmp_path / "good2.tar.gz", lambda tar: _add_file(tar, "file.txt", b"x" * 2000))
    data = good.read_bytes()
    truncated = tmp_path / "truncated.tar.gz"
    truncated.write_bytes(data[: len(data) // 2])
    destination = tmp_path / "dest"

    code = extract(truncated, destination)

    assert code == "bad_archive"


def test_write_ref_writes_ascii_revision_with_no_trailing_newline(tmp_path: Path) -> None:
    repo_dir = tmp_path / "repo"

    write_ref(repo_dir, "abc123")

    ref = repo_dir / "refs" / "main"
    assert ref.read_bytes() == b"abc123"
    assert not (repo_dir / "refs" / "main.part").exists()

    write_ref(repo_dir, "def456")
    assert ref.read_bytes() == b"def456"
