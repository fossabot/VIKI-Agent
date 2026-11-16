from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.vaults.test_vault_posix import service
from viki_agent.vaults import vault as module
from viki_agent.vaults.vault import VaultError

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX native prefix reads")


@pytest.fixture
def native_io(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[tuple[int, int]]]:
    opened, closed, calls = [], [], []
    real_open, real_read, real_close = os.open, os.read, os.close

    def open_fd(*args, **kwargs):
        fd = real_open(*args, **kwargs)
        opened.append(fd)
        return fd

    def read_fd(fd, amount):
        data = real_read(fd, amount)
        calls.append((amount, len(data)))
        return data

    def close_fd(fd):
        real_close(fd)
        closed.append(fd)

    monkeypatch.setattr(module.os, "open", open_fd)
    monkeypatch.setattr(module.os, "read", read_fd)
    monkeypatch.setattr(module.os, "close", close_fd)
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


def test_expired_budget_precedes_the_first_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, native_io: list[tuple[int, int]]
) -> None:
    vault = service(tmp_path)
    opened = []
    monkeypatch.setattr(module.os, "open", lambda *args, **kwargs: opened.append(args))
    with pytest.raises(VaultError, match="^deadline$"):
        vault._read_prefix(vault._root("w"), "a.md", module._PrefixBudget(1.0, clock=lambda: 1.0))
    assert opened == native_io == []


@pytest.mark.parametrize("reason", ["deadline", "byte_limit"])
def test_partial_budget_refusal_keeps_charge_and_closes_descriptors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, native_io: list[tuple[int, int]], reason: str
) -> None:
    (tmp_path / "a.md").write_bytes(b"x" * 6000)
    vault, now = service(tmp_path), [0.0]
    start = 1024 * 1024 - 5000 if reason == "byte_limit" else 0
    budget = module._PrefixBudget(1.0, clock=lambda: now[0], read_bytes=start)
    read = module.os.read

    def expire_after_read(fd, amount):
        chunk = read(fd, amount)
        if reason == "deadline":
            now[0] = 1.0
        return chunk

    monkeypatch.setattr(module.os, "read", expire_after_read)
    with pytest.raises(VaultError, match=f"^{reason}$"):
        vault._read_prefix(vault._root("w"), "a.md", budget)
    expected = [(4096, 4096)] if reason == "deadline" else [(4096, 4096), (904, 904)]
    assert native_io == expected
    assert budget.read_bytes == start + sum(actual for _, actual in native_io)


def test_deadline_precedes_every_absolute_and_relative_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, native_io: list[tuple[int, int]]
) -> None:
    (tmp_path / "a" / "b").mkdir(parents=True)
    (tmp_path / "a" / "b" / "n.md").write_bytes(b"note")
    vault, real_open = service(tmp_path), module.os.open
    root = vault._root("w")
    total = len(Path(root.display).parts) + 3
    for stop in range(total):
        attempted = []

        def open_fd(*args, attempted=attempted, **kwargs):
            attempted.append(args)
            return real_open(*args, **kwargs)

        monkeypatch.setattr(module.os, "open", open_fd)
        budget = module._PrefixBudget(
            1.0, clock=lambda attempted=attempted, stop=stop: float(len(attempted) >= stop)
        )
        with pytest.raises(VaultError, match="^deadline$"):
            vault._read_prefix(root, "a/b/n.md", budget)
        assert len(attempted) == stop
        assert budget.read_bytes == 0
    assert native_io == []


@pytest.mark.parametrize(
    "kind,code",
    [
        ("hardlink", "unsafe_path"),
        ("note-link", "unsafe_path"),
        ("folder-link", "unsafe_path"),
        ("fifo", "unsafe_path"),
        ("directory", "unsafe_path"),
        ("private", "private_path"),
        ("unsafe", "invalid_ref"),
        ("large", "note_too_large"),
        ("missing", "missing_note"),
        ("root", "root_changed"),
        ("root-link", "root_unavailable"),
    ],
)
def test_native_refusals_read_zero_bytes(
    tmp_path: Path, native_io: list[tuple[int, int]], kind: str, code: str
) -> None:
    root, outside = tmp_path / "vault", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    path = root / "n.md"
    path.write_bytes(b"note")
    (outside / "n.md").write_bytes(b"outside")
    vault, ref = service(root), "n.md"
    if kind == "hardlink":
        os.link(path, root / "linked.md")
    elif kind == "folder-link":
        (root / "linked").symlink_to(outside, target_is_directory=True)
        ref = "linked/n.md"
    elif kind in {"note-link", "fifo", "directory", "missing"}:
        path.unlink()
        if kind == "note-link":
            path.symlink_to(outside / "n.md")
        elif kind == "fifo":
            assert sys.platform != "win32"
            os.mkfifo(path)
        elif kind == "directory":
            path.mkdir()
    elif kind == "private":
        (root / ".claude").mkdir()
        (root / ".claude" / "n.md").write_bytes(b"private")
        ref = ".CLAUDE/n.md"
    elif kind == "unsafe":
        ref = "bad\n.md"
    elif kind == "large":
        path.write_bytes(b"x" * (2 * 1024 * 1024 + 1))
    elif kind in {"root", "root-link"}:
        root.rename(tmp_path / "old")
        if kind == "root-link":
            root.symlink_to(tmp_path / "old", target_is_directory=True)
        else:
            root.mkdir()
    budget = module._PrefixBudget(1.0, clock=lambda: 0.0)
    with pytest.raises(VaultError, match=f"^{code}$"):
        vault._read_prefix(vault._root("w"), ref, budget)
    assert native_io == []
    assert budget.read_bytes == 0


@pytest.mark.parametrize(
    "directory,field",
    [
        (False, field)
        for field in (
            "st_dev",
            "st_ino",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
            "st_nlink",
            "st_mode",
        )
    ]
    + [(True, field) for field in ("st_dev", "st_ino", "st_mode", "st_nlink", "st_birthtime_ns")],
)
def test_every_changed_stamp_field_discards_bytes_and_keeps_charge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    native_io: list[tuple[int, int]],
    directory: bool,
    field: str,
) -> None:
    import stat
    from types import SimpleNamespace

    # Forced True so this parametrization proves every field, including a directory's st_nlink,
    # on every POSIX host; _DIRECTORY_NLINK itself is only False on darwin.
    monkeypatch.setattr(module, "_DIRECTORY_NLINK", True)
    (tmp_path / "n.md").write_bytes(b"stamped\n")
    vault, fstat = service(tmp_path), module.os.fstat

    def changed(fd):
        info = fstat(fd)
        if not native_io or stat.S_ISDIR(info.st_mode) != directory:
            return info
        values = {
            name: getattr(info, name)
            for name in (
                "st_dev",
                "st_ino",
                "st_size",
                "st_mtime_ns",
                "st_ctime_ns",
                "st_nlink",
                "st_mode",
            )
        }
        # macOS's stat has st_birthtime (seconds) and no st_birthtime_ns: take the birth time as
        # vault._posix_prefix_stamp does, or the fake differs from the real re-stat.
        values["st_birthtime_ns"] = getattr(
            info, "st_birthtime_ns", int(getattr(info, "st_birthtime", 0) * 1_000_000_000)
        )
        values[field] += 1
        return SimpleNamespace(**values)

    monkeypatch.setattr(module.os, "fstat", changed)
    budget = module._PrefixBudget(1.0, clock=lambda: 0.0)
    with pytest.raises(VaultError, match="^changed$"):
        vault._read_prefix(vault._root("w"), "n.md", budget)
    assert native_io == [(8, 8)]
    assert budget.read_bytes == 8


def test_a_directory_nlink_change_is_no_change_where_nlink_counts_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, native_io: list[tuple[int, int]]
) -> None:
    import stat
    from types import SimpleNamespace

    assert sys.platform != "win32"  # module._DIRECTORY_NLINK exists only in the POSIX branch
    # The default already excludes st_nlink only on darwin (APFS counts every entry, not just
    # subdirectories); forced False here so the "no change" behavior is proven on every host.
    assert module._DIRECTORY_NLINK is (sys.platform != "darwin")
    monkeypatch.setattr(module, "_DIRECTORY_NLINK", False)
    (tmp_path / "n.md").write_bytes(b"stamped\n")
    vault, fstat = service(tmp_path), module.os.fstat

    def changed(fd):
        info = fstat(fd)
        if not native_io or not stat.S_ISDIR(info.st_mode):
            return info
        values = {name: getattr(info, name) for name in ("st_dev", "st_ino", "st_mode", "st_nlink")}
        # macOS's stat has st_birthtime (seconds) and no st_birthtime_ns: take the birth time as
        # vault._posix_prefix_stamp does, or the fake differs from the real re-stat.
        values["st_birthtime_ns"] = getattr(
            info, "st_birthtime_ns", int(getattr(info, "st_birthtime", 0) * 1_000_000_000)
        )
        values["st_nlink"] += 1
        return SimpleNamespace(**values)

    monkeypatch.setattr(module.os, "fstat", changed)
    budget = module._PrefixBudget(1.0, clock=lambda: 0.0)
    assert vault._read_prefix(vault._root("w"), "n.md", budget) == (b"stamped\n", True)
    assert native_io == [(8, 8)]
    assert budget.read_bytes == 8


@pytest.mark.parametrize("kind", ["root", "absolute-ancestor", "vault-ancestor", "final"])
def test_changed_lexical_binding_discards_opened_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, native_io: list[tuple[int, int]], kind: str
) -> None:
    anchor = tmp_path / "anchor"
    root = anchor / "vault"
    (root / "a").mkdir(parents=True)
    path = root / "a" / "n.md"
    path.write_bytes(b"note")
    vault, read, stat_at = service(root), module.os.read, module.os.stat
    changed = False

    def substitute():
        nonlocal changed
        changed = True
        if kind == "root":
            root.rename(anchor / "old-vault")
            root.mkdir()
        elif kind == "absolute-ancestor":
            anchor.rename(tmp_path / "old-anchor")
            anchor.mkdir()
            (tmp_path / "old-anchor" / "vault").rename(root)
            # Keep the old ancestor's link count: its lexical binding must still be judged.
            (tmp_path / "old-anchor" / "keep").mkdir()
        elif kind == "vault-ancestor":
            (root / "a").rename(root / "old-a")
            (root / "a").mkdir()
        else:
            path.rename(root / "a" / "old.md")
            path.write_bytes(b"replacement")

    def read_then_substitute(fd, amount):
        chunk = read(fd, amount)
        if kind != "final" and not changed:
            substitute()
        return chunk

    def binding_stat(name, *args, **kwargs):
        # Substitute after the opened note's final fstat, at its lexical check.
        if kind == "final" and name == "n.md" and native_io and not changed:
            substitute()
        return stat_at(name, *args, **kwargs)

    monkeypatch.setattr(module.os, "read", read_then_substitute)
    monkeypatch.setattr(module.os, "stat", binding_stat)
    budget = module._PrefixBudget(1.0, clock=lambda: 0.0)
    with pytest.raises(VaultError, match="^root_changed$" if kind == "root" else "^changed$"):
        vault._read_prefix(vault._root("w"), "a/n.md", budget)
    assert changed
    assert native_io == [(4, 4)]
    assert budget.read_bytes == 4


def test_exact_one_mib_has_no_thirty_third_open_or_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, native_io: list[tuple[int, int]]
) -> None:
    raw = b"x" * 32769
    for index in range(32):
        (tmp_path / f"{index}.md").write_bytes(raw)
    vault, real_open, opened = service(tmp_path), module.os.open, []

    def open_fd(*args, **kwargs):
        opened.append(args)
        return real_open(*args, **kwargs)

    monkeypatch.setattr(module.os, "open", open_fd)
    budget = module._PrefixBudget(1.0, clock=lambda: 0.0)
    for index in range(32):
        assert vault._read_prefix(vault._root("w"), f"{index}.md", budget) == (raw[:32768], False)
    assert native_io == [(4096, 4096)] * 256
    assert budget.read_bytes == 1024 * 1024
    before = len(opened)
    with pytest.raises(VaultError, match="^byte_limit$"):
        vault._read_prefix(vault._root("w"), "0.md", budget)
    assert len(opened) == before
    assert len(native_io) == 256


@pytest.mark.parametrize("kind", ["short", "zero", "error"])
def test_native_short_zero_and_error_reads_keep_actual_charge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, native_io: list[tuple[int, int]], kind: str
) -> None:
    (tmp_path / "n.md").write_bytes(b"x" * 6000)
    vault, read = service(tmp_path), module.os.read

    def changed(fd, amount):
        if native_io and kind == "error":
            raise OSError("synthetic read failure")
        if native_io and kind == "zero":
            return b""
        return read(fd, min(amount, 1000) if kind == "short" else amount)

    monkeypatch.setattr(module.os, "read", changed)
    budget = module._PrefixBudget(1.0, clock=lambda: 0.0)
    if kind == "short":
        assert vault._read_prefix(vault._root("w"), "n.md", budget) == (b"x" * 6000, True)
        assert native_io == [(1000, 1000)] * 6
    else:
        code = "changed" if kind == "zero" else "read_failed"
        with pytest.raises(VaultError, match=f"^{code}$"):
            vault._read_prefix(vault._root("w"), "n.md", budget)
        assert native_io == [(4096, 4096)]
    assert budget.read_bytes == sum(actual for _, actual in native_io)


@pytest.mark.parametrize("operation", ["open", "fstat"])
def test_failure_at_each_native_open_and_fstat_closes_successful_descriptors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    native_io: list[tuple[int, int]],
    operation: str,
) -> None:
    (tmp_path / "a" / "b").mkdir(parents=True)
    (tmp_path / "a" / "b" / "n.md").write_bytes(b"note")
    vault, original, calls = service(tmp_path), getattr(module.os, operation), []
    root = vault._root("w")

    def counted(*args, **kwargs):
        calls.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(module.os, operation, counted)
    assert vault._read_prefix(root, "a/b/n.md", module._PrefixBudget(1.0, clock=lambda: 0.0)) == (
        b"note",
        True,
    )
    for stop in range(len(calls)):
        counter = [0]

        def failed(*args, counter=counter, fail_at=stop + 1, **kwargs):
            counter[0] += 1
            if counter[0] == fail_at:
                raise OSError("synthetic descriptor failure")
            return original(*args, **kwargs)

        monkeypatch.setattr(module.os, operation, failed)
        native_io.clear()
        budget = module._PrefixBudget(1.0, clock=lambda: 0.0)
        with pytest.raises(VaultError) as error:
            vault._read_prefix(root, "a/b/n.md", budget)
        assert error.value.code in {"root_unavailable", "read_failed", "changed"}
        assert counter[0] >= stop + 1
        assert budget.read_bytes == sum(actual for _, actual in native_io)


def test_unrelated_directory_file_mutation_does_not_change_identity_stamp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, native_io: list[tuple[int, int]]
) -> None:
    (tmp_path / "n.md").write_bytes(b"note")
    vault, read = service(tmp_path), module.os.read

    def change_directory(fd, amount):
        chunk = read(fd, amount)
        (tmp_path / "unrelated.bin").write_bytes(b"other")
        return chunk

    monkeypatch.setattr(module.os, "read", change_directory)
    budget = module._PrefixBudget(1.0, clock=lambda: 0.0)
    assert vault._read_prefix(vault._root("w"), "n.md", budget) == (b"note", True)
    assert budget.read_bytes == 4
    assert native_io == [(4, 4)]


@pytest.mark.skipif(sys.platform != "darwin", reason="native APFS case/normalization aliases")
@pytest.mark.parametrize("disk_name,alias", [(".claude", ".CLAUDE"), (".viki", ".viKi")])
def test_native_private_alias_is_refused_before_body_io(
    tmp_path: Path, native_io: list[tuple[int, int]], disk_name: str, alias: str
) -> None:
    assert sys.platform == "darwin"
    directory = tmp_path / disk_name
    directory.mkdir()
    (directory / "n.md").write_bytes(b"private")
    alias_path = tmp_path / alias
    if not alias_path.exists() or not alias_path.samefile(directory):
        pytest.skip("this filesystem does not expose this case/normalization alias")
    vault = service(tmp_path)
    budget = module._PrefixBudget(1.0, clock=lambda: 0.0)
    with pytest.raises(VaultError, match="^private_path$"):
        vault._read_prefix(vault._root("w"), f"{alias}/n.md", budget)
    # The descriptor helper also judges the real alias, independently of safe_ref.
    with (
        module._open_root_fd(vault._root("w")) as root_fd,
        pytest.raises(VaultError, match="^private_path$"),
    ):
        module._open_dir_at(root_fd, alias, checkpoint=budget.check)
    assert budget.read_bytes == 0
    assert native_io == []


@pytest.mark.skipif(sys.platform != "darwin", reason="native APFS normalization alias")
def test_native_normalization_alias_keeps_raw_prefix_bytes(
    tmp_path: Path, native_io: list[tuple[int, int]]
) -> None:
    import unicodedata

    assert sys.platform == "darwin"
    disk_path = tmp_path / unicodedata.normalize("NFD", "café.md")
    disk_path.write_bytes(b"\xef\xbb\xbf# A\r\n")
    alias_path = tmp_path / "café.md"
    if not alias_path.exists() or not alias_path.samefile(disk_path):
        pytest.skip("this filesystem does not expose canonical normalization aliases")
    vault = service(tmp_path)
    budget = module._PrefixBudget(1.0, clock=lambda: 0.0)
    assert vault._read_prefix(vault._root("w"), "café.md", budget) == (b"\xef\xbb\xbf# A\r\n", True)
    assert budget.read_bytes == 8
    assert native_io == [(8, 8)]
