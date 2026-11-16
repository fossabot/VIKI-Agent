"""voice_fetch: every byte source is injected or built here; no request leaves the process."""

from __future__ import annotations

import errno
import hashlib
import io
import sys
import zipfile
from email.message import Message
from http.client import IncompleteRead  # noqa: TID251
from pathlib import Path
from typing import Any, BinaryIO
from urllib.request import Request  # noqa: TID251

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))

import voice_fetch  # noqa: E402

DATA = b"pinned backend bytes\n" * 100
SHA = hashlib.sha256(DATA).hexdigest()
URL = "https://example.invalid/backend.dll"


def _serve(data: bytes):
    def opener(url: str) -> BinaryIO:
        assert url == URL
        return io.BytesIO(data)

    return opener


def _refuse(url: str) -> BinaryIO:
    raise AssertionError("the opener must not be called")


def _names(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir())


def _zip(path: Path, member: str, data: bytes) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(member, data)
    return path


def test_fresh_fetch_writes_the_file(tmp_path: Path) -> None:
    error = voice_fetch.fetch(URL, "backend.dll", len(DATA), SHA, tmp_path, opener=_serve(DATA))
    assert error is None
    assert (tmp_path / "backend.dll").read_bytes() == DATA
    assert _names(tmp_path) == ["backend.dll"]


def test_one_byte_past_the_size_leaves_nothing(tmp_path: Path) -> None:
    error = voice_fetch.fetch(
        URL, "backend.dll", len(DATA), SHA, tmp_path, opener=_serve(DATA + b"x")
    )
    assert error == "size_mismatch"
    assert _names(tmp_path) == []


def test_wrong_bytes_leave_no_file(tmp_path: Path) -> None:
    stale = b"stale" * 10
    (tmp_path / "backend.dll").write_bytes(stale)
    wrong = bytes(len(DATA))
    error = voice_fetch.fetch(URL, "backend.dll", len(DATA), SHA, tmp_path, opener=_serve(wrong))
    assert error == "hash_mismatch"
    assert _names(tmp_path) == ["backend.dll"]
    assert (tmp_path / "backend.dll").read_bytes() == stale

    assert (
        voice_fetch.fetch(URL, "backend.dll", len(DATA), SHA, tmp_path, opener=_serve(DATA)) is None
    )
    assert (tmp_path / "backend.dll").read_bytes() == DATA
    assert _names(tmp_path) == ["backend.dll"]


def test_a_verified_target_sends_no_request(tmp_path: Path) -> None:
    (tmp_path / "backend.dll").write_bytes(DATA)
    assert voice_fetch.fetch(URL, "backend.dll", len(DATA), SHA, tmp_path, opener=_refuse) is None


def test_a_stale_regular_part_is_replaced(tmp_path: Path) -> None:
    (tmp_path / "backend.dll.part").write_bytes(b"half")
    assert (
        voice_fetch.fetch(URL, "backend.dll", len(DATA), SHA, tmp_path, opener=_serve(DATA)) is None
    )
    assert _names(tmp_path) == ["backend.dll"]


def test_a_part_folder_is_not_regular_and_untouched(tmp_path: Path) -> None:
    folder = tmp_path / "backend.dll.part"
    folder.mkdir()
    (folder / "keep.txt").write_bytes(b"keep")
    error = voice_fetch.fetch(URL, "backend.dll", len(DATA), SHA, tmp_path, opener=_refuse)
    assert error == "not_regular"
    assert _names(tmp_path) == ["backend.dll.part"]
    assert (folder / "keep.txt").read_bytes() == b"keep"


def test_a_network_error_leaves_no_part(tmp_path: Path) -> None:
    def broken(url: str) -> BinaryIO:
        raise OSError("connection reset")

    error = voice_fetch.fetch(URL, "backend.dll", len(DATA), SHA, tmp_path, opener=broken)
    assert error == "network"
    assert _names(tmp_path) == []


def test_an_endless_body_is_cut_one_chunk_past_the_size(tmp_path: Path) -> None:
    read = 0

    class Endless(io.BytesIO):
        def read(self, n: int | None = -1) -> bytes:
            nonlocal read
            assert n is not None and n > 0
            read += n
            # Ends the stream after two chunks so a missing cap fails the assert, not the disk.
            return b"x" * n if read <= 2 * voice_fetch.CHUNK else b""

    error = voice_fetch.fetch(
        URL, "backend.dll", len(DATA), SHA, tmp_path, opener=lambda url: Endless()
    )
    assert error == "size_mismatch"
    assert read <= len(DATA) + voice_fetch.CHUNK
    assert _names(tmp_path) == []


def test_a_full_disk_is_write_failed_not_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def full(fd: int) -> None:
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(voice_fetch.os, "fsync", full)
    error = voice_fetch.fetch(URL, "backend.dll", len(DATA), SHA, tmp_path, opener=_serve(DATA))
    assert error == "write_failed"
    assert _names(tmp_path) == []


def test_an_incomplete_read_is_a_network_error(tmp_path: Path) -> None:
    class Cut(io.BytesIO):
        def read(self, n: int | None = -1) -> bytes:
            raise IncompleteRead(b"")

    error = voice_fetch.fetch(
        URL, "backend.dll", len(DATA), SHA, tmp_path, opener=lambda url: Cut(DATA)
    )
    assert error == "network"
    assert _names(tmp_path) == []


def test_redirects_are_followed_only_to_https() -> None:
    handler = voice_fetch._HttpsOnlyRedirects()  # pyright: ignore[reportPrivateUsage]
    request = Request(URL)
    no_headers: Any = Message()  # the success path never reads the response headers
    refused = handler.redirect_request(
        request, io.BytesIO(), 302, "Found", no_headers, "http://example.invalid/backend.dll"
    )
    assert refused is None
    followed = handler.redirect_request(
        request, io.BytesIO(), 302, "Found", no_headers, "https://cdn.example.invalid/b.dll"
    )
    assert followed is not None
    assert followed.full_url == "https://cdn.example.invalid/b.dll"


def test_open_https_refuses_a_plain_http_url() -> None:
    with pytest.raises(ValueError):
        voice_fetch.open_https("http://example.invalid/backend.dll")


def test_extract_writes_the_pinned_member(tmp_path: Path) -> None:
    archive = _zip(tmp_path / "pack.zip", "bin/backend.dll", DATA)
    out = tmp_path / "out"
    out.mkdir()
    assert (
        voice_fetch.extract(archive, "bin/backend.dll", "backend.dll", len(DATA), SHA, out) is None
    )
    assert (out / "backend.dll").read_bytes() == DATA
    assert _names(out) == ["backend.dll"]


def test_extract_wrong_bytes_leave_no_file(tmp_path: Path) -> None:
    archive = _zip(tmp_path / "pack.zip", "backend.dll", bytes(len(DATA)))
    out = tmp_path / "out"
    out.mkdir()
    error = voice_fetch.extract(archive, "backend.dll", "backend.dll", len(DATA), SHA, out)
    assert error == "hash_mismatch"
    assert _names(out) == []


def test_extract_a_declared_size_off_writes_nothing(tmp_path: Path) -> None:
    archive = _zip(tmp_path / "pack.zip", "backend.dll", DATA + b"x")
    out = tmp_path / "out"
    out.mkdir()
    error = voice_fetch.extract(archive, "backend.dll", "backend.dll", len(DATA), SHA, out)
    assert error == "size_mismatch"
    assert _names(out) == []


def test_extract_a_missing_member(tmp_path: Path) -> None:
    archive = _zip(tmp_path / "pack.zip", "other.dll", DATA)
    out = tmp_path / "out"
    out.mkdir()
    error = voice_fetch.extract(archive, "backend.dll", "backend.dll", len(DATA), SHA, out)
    assert error == "missing_member"
    assert _names(out) == []


def test_extract_a_traversal_member_lands_only_under_its_pinned_name(tmp_path: Path) -> None:
    archive = _zip(tmp_path / "pack.zip", "../evil.dll", DATA)
    out = tmp_path / "out"
    out.mkdir()
    assert voice_fetch.extract(archive, "../evil.dll", "backend.dll", len(DATA), SHA, out) is None
    assert _names(out) == ["backend.dll"]
    assert _names(tmp_path) == ["out", "pack.zip"]


def test_extract_a_failed_rename_is_write_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = _zip(tmp_path / "pack.zip", "backend.dll", DATA)
    out = tmp_path / "out"
    out.mkdir()

    def refuse(src: object, dst: object) -> None:
        raise OSError(errno.EACCES, "denied")

    monkeypatch.setattr(voice_fetch.os, "replace", refuse)
    error = voice_fetch.extract(archive, "backend.dll", "backend.dll", len(DATA), SHA, out)
    assert error == "write_failed"
    assert _names(out) == []


def test_extract_a_non_zip_is_a_bad_archive(tmp_path: Path) -> None:
    archive = tmp_path / "pack.zip"
    archive.write_bytes(b"not a zip at all")
    out = tmp_path / "out"
    out.mkdir()
    error = voice_fetch.extract(archive, "backend.dll", "backend.dll", len(DATA), SHA, out)
    assert error == "bad_archive"
    assert _names(out) == []
