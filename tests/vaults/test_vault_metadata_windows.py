from __future__ import annotations

import ctypes
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.vaults.test_vault import service
from viki_agent.vaults import vault as module
from viki_agent.vaults.vault import VaultError

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows native prefix reads")


@pytest.fixture
def native_io(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[tuple[int, int]]]:
    assert sys.platform == "win32"
    read, create, close = module._ReadFile, module._CreateFileW, module._CloseHandle
    calls, opened, closed = [], [], []

    def read_file(handle, buffer, amount, transferred, overlapped):
        ok = read(handle, buffer, amount, transferred, overlapped)
        actual = ctypes.cast(transferred, ctypes.POINTER(ctypes.c_uint32)).contents.value
        calls.append((amount, actual))
        return ok

    def create_file(*args):
        handle = create(*args)
        if handle != module._INVALID_HANDLE_VALUE:
            opened.append(handle)
        return handle

    def close_handle(handle):
        ok = close(handle)
        assert ok
        closed.append(handle)
        return ok

    monkeypatch.setattr(module, "_ReadFile", read_file)
    monkeypatch.setattr(module, "_CreateFileW", create_file)
    monkeypatch.setattr(module, "_CloseHandle", close_handle)
    yield calls
    assert sorted(opened) == sorted(closed)


@pytest.mark.parametrize(
    "raw,stop,complete",
    [
        (b"", 0, True),
        (b"\xef\xbb\xbf# A\r\nbody\r\n", 32768, True),
        (b"x" * 32768, 32768, True),
        (b"x" * 32769, 32768, False),
        (b"x" * 32767 + "日本語".encode(), 32768, False),
        (b"x\n" * 3000 + b"tail", 4096, False),
        (b"x\r\n" * 3000 + b"tail", 4096, False),
    ],
    ids=["empty", "bom-eof", "byte-eof", "byte-cut", "utf8-cut", "lf-limit", "crlf-limit"],
)
def test_native_prefix_boundaries(
    tmp_path: Path, native_io: list[tuple[int, int]], raw: bytes, stop: int, complete: bool
) -> None:
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "a.md").write_bytes(raw)
    vault = service(tmp_path)
    budget = module._PrefixBudget(1.0, clock=lambda: 0.0)
    assert vault._read_prefix(vault._root("w"), "nested/a.md", budget) == (raw[:stop], complete)
    assert budget.read_bytes == sum(actual for _, actual in native_io) == len(raw[:stop])
    assert all(0 < requested <= 4096 and actual <= requested for requested, actual in native_io)
    assert len(native_io) == (len(raw[:stop]) + 4095) // 4096


@pytest.mark.parametrize("reason", ["deadline", "byte_limit"])
def test_native_partial_budget_refusal_closes_handles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, native_io: list[tuple[int, int]], reason: str
) -> None:
    (tmp_path / "a.md").write_bytes(b"x" * 6000)
    vault, now = service(tmp_path), [0.0]
    start = 1024 * 1024 - 5000 if reason == "byte_limit" else 0
    budget = module._PrefixBudget(1.0, clock=lambda: now[0], read_bytes=start)
    assert sys.platform == "win32"
    read = module._ReadFile

    def expire_after_read(*args):
        ok = read(*args)
        if reason == "deadline":
            now[0] = 1.0
        return ok

    monkeypatch.setattr(module, "_ReadFile", expire_after_read)
    with pytest.raises(VaultError, match=f"^{reason}$"):
        vault._read_prefix(vault._root("w"), "a.md", budget)
    expected = [(4096, 4096)] if reason == "deadline" else [(4096, 4096), (904, 904)]
    assert native_io == expected
    assert budget.read_bytes == start + sum(actual for _, actual in native_io)


def test_changed_native_note_stamp_discards_bytes_but_keeps_charge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, native_io: list[tuple[int, int]]
) -> None:
    assert sys.platform == "win32"
    (tmp_path / "a.md").write_bytes(b"stamped\n")
    vault, info = service(tmp_path), module._handle_info

    def changed(handle):
        stamp = info(handle)
        if native_io and not stamp.attributes & module._FILE_ATTRIBUTE_DIRECTORY:
            stamp.written.dwLowDateTime ^= 1
        return stamp

    monkeypatch.setattr(module, "_handle_info", changed)
    budget = module._PrefixBudget(1.0, clock=lambda: 0.0)
    with pytest.raises(VaultError, match="^changed$"):
        vault._read_prefix(vault._root("w"), "a.md", budget)
    assert native_io == [(8, 8)]
    assert budget.read_bytes == 8


@pytest.mark.parametrize(
    "kind,code",
    [
        ("hardlink", "unsafe_path"),
        ("private", "private_path"),
        ("trailing", "private_path"),
        ("short", "private_path"),
        ("junction", "unsafe_path"),
        ("root", "root_changed"),
        ("large", "note_too_large"),
    ],
)
def test_native_refusals_read_zero_bytes(
    tmp_path: Path, native_io: list[tuple[int, int]], kind: str, code: str
) -> None:
    from tests.vaults.test_vault import short_name
    from viki_agent.winproc import run

    root = tmp_path / "vault"
    root.mkdir()
    (root / "a.md").write_bytes(b"public")
    vault, ref = service(root), "a.md"
    if kind == "hardlink":
        (root / "linked.md").hardlink_to(root / "a.md")
    elif kind == "large":
        (root / "a.md").write_bytes(b"x" * (2 * 1024 * 1024 + 1))
    elif kind in {"private", "trailing", "short"}:
        (root / ".claude").mkdir()
        (root / ".claude" / "a.md").write_bytes(b"synthetic private")
        part = ".claude." if kind == "trailing" else ".claude"
        if kind == "short":
            part = short_name(root / ".claude")
            if part == ".claude":
                pytest.skip("this volume supplies no private-directory 8.3 alias")
        ref = f"{part}/a.md"
    elif kind == "junction":
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "a.md").write_bytes(b"outside")
        done = run(
            ["cmd", "/c", "mklink", "/J", str(root / "link"), str(outside)],
            cwd=tmp_path,
            timeout=10.0,
        )
        assert done.returncode == 0, done.stderr
        ref = "link/a.md"
    elif kind == "root":
        root.rename(tmp_path / "old")
        root.mkdir()
    budget = module._PrefixBudget(1.0, clock=lambda: 0.0)
    with pytest.raises(VaultError, match=f"^{code}$"):
        vault._read_prefix(vault._root("w"), ref, budget)
    assert native_io == []
    assert budget.read_bytes == 0


@pytest.mark.parametrize("directory", [False, True], ids=["note", "ancestor"])
@pytest.mark.parametrize(
    "field",
    [
        "volume",
        "index_high",
        "index_low",
        "attributes",
        "links",
        "created.dwHighDateTime",
        "created.dwLowDateTime",
        "size_high",
        "size_low",
        "written.dwHighDateTime",
        "written.dwLowDateTime",
        "accessed.dwHighDateTime",
        "accessed.dwLowDateTime",
    ],
)
def test_native_stamp_components_and_unrelated_directory_times(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    native_io: list[tuple[int, int]],
    directory: bool,
    field: str,
) -> None:
    assert sys.platform == "win32"
    (tmp_path / "a.md").write_bytes(b"stamped\n")
    vault, info = service(tmp_path), module._handle_info

    def changed(handle):
        stamp = info(handle)
        if native_io and bool(stamp.attributes & module._FILE_ATTRIBUTE_DIRECTORY) == directory:
            holder = stamp
            parts = field.split(".")
            for part in parts[:-1]:
                holder = getattr(holder, part)
            setattr(holder, parts[-1], getattr(holder, parts[-1]) ^ 1)
        return stamp

    monkeypatch.setattr(module, "_handle_info", changed)
    budget = module._PrefixBudget(1.0, clock=lambda: 0.0)
    tracked = not field.startswith("accessed.") and (
        not directory or not field.startswith(("written.", "size_"))
    )
    if tracked:
        with pytest.raises(VaultError, match="^changed$"):
            vault._read_prefix(vault._root("w"), "a.md", budget)
    else:
        assert vault._read_prefix(vault._root("w"), "a.md", budget) == (b"stamped\n", True)
    assert native_io == [(8, 8)]
    assert budget.read_bytes == 8


def test_deadline_before_every_native_directory_note_and_chunk_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, native_io: list[tuple[int, int]]
) -> None:
    assert sys.platform == "win32"
    (tmp_path / "one" / "two").mkdir(parents=True)
    (tmp_path / "one" / "two" / "a.md").write_bytes(b"body")
    vault, create = service(tmp_path), module._CreateFileW
    now, opened = [0.0], [0]
    for allowed in range(len(tmp_path.resolve().parts) + 4):
        now[0], opened[0] = float(allowed == 0), 0

        def create_file(*args, allowed=allowed):
            handle = create(*args)
            opened[0] += 1
            if opened[0] >= allowed:
                now[0] = 1.0
            return handle

        monkeypatch.setattr(module, "_CreateFileW", create_file)
        budget = module._PrefixBudget(1.0, clock=lambda: now[0])
        with pytest.raises(VaultError, match="^deadline$"):
            vault._read_prefix(vault._root("w"), "one/two/a.md", budget)
        assert opened[0] == allowed
        assert budget.read_bytes == 0
    assert native_io == []


@pytest.mark.parametrize("outcome", ["partial-error", "zero", "over-count"])
def test_native_reported_failure_counts_and_zero_transfer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, native_io: list[tuple[int, int]], outcome: str
) -> None:
    assert sys.platform == "win32"
    (tmp_path / "a.md").write_bytes(b"x" * 6000)
    vault, read = service(tmp_path), module._ReadFile

    def interrupted(handle, buffer, amount, transferred, overlapped):
        assert amount == 4096
        ok = read(handle, buffer, 0 if outcome == "zero" else 7, transferred, overlapped)
        assert ok
        if outcome == "over-count":
            ctypes.cast(transferred, ctypes.POINTER(ctypes.c_uint32)).contents.value = 4097
        if outcome == "partial-error":
            ctypes.set_last_error(5)
            return 0
        return ok

    monkeypatch.setattr(module, "_ReadFile", interrupted)
    budget = module._PrefixBudget(1.0, clock=lambda: 0.0)
    with pytest.raises(
        VaultError, match="^read_failed$" if outcome == "partial-error" else "^changed$"
    ):
        vault._read_prefix(vault._root("w"), "a.md", budget)
    expected = 0 if outcome == "zero" else 7
    assert native_io == [(expected, expected)]
    assert budget.read_bytes == (4097 if outcome == "over-count" else expected)


def test_exact_batch_limit_is_32_actual_native_prefixes_without_extra_io(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, native_io: list[tuple[int, int]]
) -> None:
    assert sys.platform == "win32"
    raw = b"x" * 32769
    refs = [f"n{i}.md" for i in range(32)]
    for ref in refs:
        (tmp_path / ref).write_bytes(raw)
    vault = service(tmp_path)
    budget = module._PrefixBudget(1.0, clock=lambda: 0.0)
    for ref in refs:
        assert vault._read_prefix(vault._root("w"), ref, budget) == (raw[:32768], False)
    assert native_io == [(4096, 4096)] * 256
    assert budget.read_bytes == 1024 * 1024

    def forbidden_open(*_args):
        pytest.fail("exhausted batch opened another handle")

    monkeypatch.setattr(module, "_CreateFileW", forbidden_open)
    with pytest.raises(VaultError, match="^byte_limit$"):
        vault._read_prefix(vault._root("w"), refs[0], budget)
    assert len(native_io) == 256
    assert budget.read_bytes == 1024 * 1024


def test_prefix_leaves_stat_scan_and_full_read_contracts_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, native_io: list[tuple[int, int]]
) -> None:
    import hashlib

    raw = b"\xef\xbb\xbf# Same\r\n" + b"x" * 32769
    (tmp_path / "a.md").write_bytes(raw)
    vault = service(tmp_path)
    scan = vault.scan("w")
    assert native_io == []
    assert scan.scanned_bytes == 0
    assert scan.entries[0].bytes == len(raw)
    note = vault.read("w", "a.md")
    assert note.text == raw.decode("utf-8-sig")
    assert note.source == raw.decode("utf-8")
    assert note.byte_size == len(raw)
    assert note.sha256 == hashlib.sha256(raw).hexdigest()
    assert native_io == [(len(raw), len(raw))]
    native_io.clear()

    def forbidden(*_args, **_kwargs):
        pytest.fail("prefix reached a full-read/scan/hash/parser path")

    monkeypatch.setattr(vault, "_read_stamped", forbidden)
    monkeypatch.setattr(vault, "_scan_root", forbidden)
    monkeypatch.setattr(module.hashlib, "sha256", forbidden)
    monkeypatch.setattr(module, "extract_display", forbidden)
    budget = module._PrefixBudget(1.0, clock=lambda: 0.0)
    assert vault._read_prefix(vault._root("w"), "a.md", budget) == (raw[:32768], False)
    assert native_io == [(4096, 4096)] * 8
    assert budget.read_bytes == 32768
