"""The keychain key source over a fake `winproc.run`; only the opt-in `keychain` test is real."""

from __future__ import annotations

import errno
import hashlib
import logging
import os
import stat
import sys
import threading
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import BinaryIO

import pytest

from viki_agent import osfs, protection, winproc
from viki_agent.protection import ProtectionError

_KEY = bytes(range(32))
_HEX = _KEY.hex()
_KEY_ID = hashlib.sha256(_KEY).digest()[:8].hex()
# Only stops the test if the fix regresses and the FIFO open blocks again; a fixed open refuses
# a FIFO record at once, without waiting for a writer.
HANG_GUARD_S = 5


class _Expired(Exception):
    """Stands in for subprocess.TimeoutExpired, which tests may not import."""


class FakeRun:
    """`winproc.run` as the helpers call it: scripted replies, every call recorded."""

    def __init__(self, *replies: tuple[int, str] | BaseException) -> None:
        self.replies = list(replies)
        self.calls: list[dict[str, object]] = []

    def __call__(
        self,
        argv: Sequence[str],
        *,
        cwd: str | os.PathLike[str],
        timeout: float = 15.0,
        input: str | None = None,
    ) -> SimpleNamespace:
        self.calls.append({"argv": list(argv), "cwd": cwd, "timeout": timeout, "input": input})
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return SimpleNamespace(returncode=reply[0], stdout=reply[1], stderr="")


class FakeKeychain:
    """One item store behind either tool's argv, so a source can store and read back."""

    def __init__(self, *, keep: bool = True) -> None:
        self.keep = keep  # False: a store that reports success but keeps another value
        self.locked = False  # True: every lookup exits 1, as when an unlock prompt is dismissed
        self.item: str | None = None
        self.calls: list[list[str]] = []
        self.inputs: list[str | None] = []

    def __call__(
        self,
        argv: Sequence[str],
        *,
        cwd: str | os.PathLike[str],
        timeout: float = 15.0,
        input: str | None = None,
    ) -> SimpleNamespace:
        argv = list(argv)
        self.calls.append(argv)
        self.inputs.append(input)
        if argv[1:2] in (["-i"], ["store"]):
            assert input is not None
            stored = input.split(" -w ")[-1].strip() if argv[1] == "-i" else input
            self.item = stored if self.keep else (b"\x01" * 32).hex()
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if self.item is None or self.locked:
            return SimpleNamespace(returncode=44 if argv[0].endswith("security") else 1, stdout="")
        return SimpleNamespace(returncode=0, stdout=self.item + "\n", stderr="")


def _record(data_dir: Path) -> Path:
    return data_dir / "sealing-key-id"


def _account(data_dir: Path) -> str:
    return hashlib.sha256(os.fsencode(os.path.realpath(data_dir))).hexdigest()[:32]


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(protection, "_keyring", "unchecked")


def test_darwin_read_maps_the_exit_codes(monkeypatch: pytest.MonkeyPatch) -> None:
    run = FakeRun((0, _HEX + "\n"), (44, ""), (1, ""), (0, "abcd\n"))
    monkeypatch.setattr(winproc, "run", run)

    assert protection._darwin_read("acct") == _KEY
    assert protection._darwin_read("acct") is None  # errSecItemNotFound
    with pytest.raises(OSError):
        protection._darwin_read("acct")
    with pytest.raises(ValueError):
        protection._darwin_read("acct")  # not 32 bytes of hex

    argv = ["/usr/bin/security", "find-generic-password", "-s", "viki-agent", "-a", "acct", "-w"]
    for call in run.calls:
        assert call == {"argv": argv, "cwd": Path.home(), "timeout": 60.0, "input": None}


def test_darwin_store_puts_the_key_on_stdin_only(monkeypatch: pytest.MonkeyPatch) -> None:
    run = FakeRun((0, ""), (1, ""))
    monkeypatch.setattr(winproc, "run", run)

    protection._darwin_store("acct", _HEX)
    with pytest.raises(OSError):
        protection._darwin_store("acct", _HEX)

    call = run.calls[0]
    assert call["argv"] == ["/usr/bin/security", "-i"]
    assert call["input"] == (
        f"add-generic-password -s viki-agent -a acct -l viki-agent -w {_HEX}\n"
    )
    assert (call["cwd"], call["timeout"]) == (Path.home(), 60.0)


def test_linux_read_treats_any_failure_or_silence_as_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = FakeRun((0, _HEX), (1, ""), (0, ""), (0, "zz"))
    monkeypatch.setattr(winproc, "run", run)

    assert protection._linux_read("acct") == _KEY
    assert protection._linux_read("acct") is None
    assert protection._linux_read("acct") is None
    with pytest.raises(ValueError):
        protection._linux_read("acct")

    argv = ["secret-tool", "lookup", "service", "viki-agent", "account", "acct"]
    for call in run.calls:
        assert call == {"argv": argv, "cwd": Path.home(), "timeout": 60.0, "input": None}


def test_linux_store_puts_the_key_on_stdin_only(monkeypatch: pytest.MonkeyPatch) -> None:
    run = FakeRun((0, ""), (1, ""))
    monkeypatch.setattr(winproc, "run", run)

    protection._linux_store("acct", _HEX)
    with pytest.raises(OSError):
        protection._linux_store("acct", _HEX)

    argv = ["secret-tool", "store", "--label=VIKI Agent", "service", "viki-agent"]
    assert run.calls[0] == {
        "argv": [*argv, "account", "acct"],
        "cwd": Path.home(),
        "timeout": 60.0,
        "input": _HEX,
    }


def _sync_spy(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, object]]:
    """Wraps os.fsync and osfs.fsync_dir, keeping the real behaviour and recording, in order,
    each synced regular file's inode and each synced directory's path."""
    calls: list[tuple[str, object]] = []
    real_fsync, real_fsync_dir = os.fsync, osfs.fsync_dir

    def fsync(fd: int) -> None:
        real_fsync(fd)
        status = os.fstat(fd)
        if stat.S_ISREG(status.st_mode):  # fsync_dir's own descriptor is not a file sync
            calls.append(("fsync", status.st_ino))

    def fsync_dir(path: str | os.PathLike[str]) -> None:
        real_fsync_dir(path)
        calls.append(("fsync_dir", Path(path)))

    monkeypatch.setattr(protection.os, "fsync", fsync)
    monkeypatch.setattr(protection.osfs, "fsync_dir", fsync_dir)
    return calls


@pytest.mark.usefixtures("desktop_umask")  # so a record made without an explicit mode shows
@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_a_source_that_finds_nothing_and_no_record_stores_reads_back_and_records(
    platform: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    keychain = FakeKeychain()
    monkeypatch.setattr(winproc, "run", keychain)
    caplog.set_level(logging.DEBUG)
    source = protection._keychain_source(tmp_path, platform)
    assert protection._keyring == "unchecked"  # nothing is read before first use
    assert keychain.calls == []

    key = source()

    assert len(key) == 32 and keychain.item == key.hex()
    assert len(keychain.calls) == 3  # read, store, read back
    account = _account(tmp_path)
    assert all(
        account in argv or account in (stdin or "")
        for argv, stdin in zip(keychain.calls, keychain.inputs, strict=True)
    )
    assert not any(key.hex() in part for argv in keychain.calls for part in argv)
    assert protection._keyring == "ready"
    assert source() == key
    assert len(keychain.calls) == 3  # cached for the process lifetime
    assert key.hex() not in caplog.text
    assert _record(tmp_path).read_bytes() == hashlib.sha256(key).digest()[:8].hex().encode()
    if sys.platform != "win32":
        assert stat.S_IMODE(os.lstat(_record(tmp_path)).st_mode) == 0o600


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_a_source_reads_an_existing_key_without_storing_and_records_its_id(
    platform: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    keychain = FakeKeychain()
    keychain.item = _HEX
    monkeypatch.setattr(winproc, "run", keychain)

    assert protection._keychain_source(tmp_path, platform)() == _KEY
    assert len(keychain.calls) == 1
    assert _record(tmp_path).read_text(encoding="ascii") == _KEY_ID
    assert protection._keychain_source(tmp_path, platform)() == _KEY  # the record matches


def test_a_new_record_and_its_directory_are_synced_before_the_key_is_returned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(winproc, "run", FakeKeychain())
    calls = _sync_spy(monkeypatch)

    protection._keychain_source(tmp_path, "linux")()

    assert calls == [("fsync", os.lstat(_record(tmp_path)).st_ino), ("fsync_dir", tmp_path)]


@pytest.mark.skipif(
    sys.platform == "win32", reason="Windows never installs this source; it reads the record plain"
)
def test_an_existing_record_and_its_directory_are_synced_before_the_key_is_returned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert sys.platform != "win32"
    keychain = FakeKeychain()
    keychain.item = _HEX
    _record(tmp_path).write_text(_KEY_ID, encoding="ascii")
    monkeypatch.setattr(winproc, "run", keychain)
    calls = _sync_spy(monkeypatch)

    assert protection._keychain_source(tmp_path, "linux")() == _KEY

    assert calls == [("fsync", os.lstat(_record(tmp_path)).st_ino), ("fsync_dir", tmp_path)]


@pytest.mark.parametrize(
    "kind",
    [
        "directory",
        pytest.param(
            "link",
            marks=pytest.mark.skipif(sys.platform == "win32", reason="links need a privilege"),
        ),
    ],
)
def test_a_record_that_is_a_directory_or_a_link_fails_closed(
    kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    keychain = FakeKeychain()
    keychain.item = _HEX
    if kind == "directory":
        _record(tmp_path).mkdir()
    else:
        (tmp_path / "elsewhere").write_text(_KEY_ID, encoding="ascii")
        _record(tmp_path).symlink_to(tmp_path / "elsewhere")
    monkeypatch.setattr(winproc, "run", keychain)

    with pytest.raises(ProtectionError):
        protection._keychain_source(tmp_path, "linux")()
    assert protection._keyring == "unavailable"
    assert [argv[1] for argv in keychain.calls] == ["lookup"]  # no store


@pytest.mark.skipif(sys.platform == "win32", reason="FIFOs need os.mkfifo")
def test_a_record_that_is_a_fifo_is_refused_without_blocking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert sys.platform != "win32"
    keychain = FakeKeychain()
    keychain.item = _HEX
    record = _record(tmp_path)
    os.mkfifo(record)
    monkeypatch.setattr(winproc, "run", keychain)

    outcome: list[BaseException | None] = []

    def call() -> None:
        try:
            protection._keychain_source(tmp_path, "linux")()
        except BaseException as exc:  # noqa: BLE001 - reported on the main thread below
            outcome.append(exc)
        else:
            outcome.append(None)

    thread = threading.Thread(target=call, daemon=True)
    thread.start()
    thread.join(HANG_GUARD_S)
    if thread.is_alive():
        # Release the blocked reader before failing, so a failing run never leaves a hung thread.
        writer = os.open(record, os.O_WRONLY | os.O_NONBLOCK)
        os.close(writer)
        thread.join(HANG_GUARD_S)
        pytest.fail("the open blocked on the FIFO instead of refusing it at once")

    assert isinstance(outcome[0], ProtectionError)


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_a_recorded_key_that_no_lookup_finds_is_never_replaced(
    platform: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A locked keyring whose unlock prompt was dismissed (Linux: lookup exits 1 as for a missing
    # item), or a moved data directory (macOS: exit 44 under the new account).
    keychain = FakeKeychain()
    keychain.item = _HEX
    keychain.locked = True
    _record(tmp_path).write_text(_KEY_ID, encoding="ascii")
    monkeypatch.setattr(winproc, "run", keychain)

    with pytest.raises(ProtectionError):
        protection._keychain_source(tmp_path, platform)()

    expected = "not_applicable" if sys.platform == "win32" else "unavailable"
    assert (protection.keyring_state(), protection._keyring) == (expected, "unavailable")
    assert not any(argv[1] in ("-i", "store") for argv in keychain.calls)
    assert keychain.item == _HEX


def test_a_record_that_cannot_be_statted_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Only a missing record means nothing was sealed; an I/O error must not read as one, whether
    # the code asks with lstat (the ENOENT check) or stat (as os.path.exists would).
    keychain = FakeKeychain()
    keychain.item = _HEX
    keychain.locked = True
    record = _record(tmp_path)
    record.write_text(_KEY_ID, encoding="ascii")
    real_lstat = os.lstat
    real_stat = os.stat

    def lstat(path: str | os.PathLike[str]) -> os.stat_result:
        if Path(path) == record:
            raise OSError(errno.EIO, "synthetic I/O error")
        return real_lstat(path)

    def stat_(path: str | os.PathLike[str]) -> os.stat_result:
        if Path(path) == record:
            raise OSError(errno.EIO, "synthetic I/O error")
        return real_stat(path)

    monkeypatch.setattr(os, "lstat", lstat)
    monkeypatch.setattr(os, "stat", stat_)
    monkeypatch.setattr(winproc, "run", keychain)

    with pytest.raises(ProtectionError):
        protection._keychain_source(tmp_path, "linux")()
    assert protection._keyring == "unavailable"
    assert [argv[1] for argv in keychain.calls] == ["lookup"]  # no store
    assert keychain.item == _HEX


@pytest.mark.skipif(sys.platform == "win32", reason="links need a privilege")
def test_a_dangling_link_record_with_a_locked_keychain_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # lstat sees a dangling link as a record; os.path.exists (which follows it) would not, and
    # would then read a locked keychain as no record and store a new key.
    keychain = FakeKeychain()
    keychain.locked = True
    _record(tmp_path).symlink_to(tmp_path / "missing")
    monkeypatch.setattr(winproc, "run", keychain)

    with pytest.raises(ProtectionError):
        protection._keychain_source(tmp_path, "linux")()
    assert [argv[1] for argv in keychain.calls] == ["lookup"]  # no store


@pytest.mark.parametrize("record_text", ["0" * 16, _KEY_ID + "\n"], ids=["differs", "longer"])
def test_a_key_whose_id_differs_from_the_record_fails_closed(
    record_text: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    keychain = FakeKeychain()
    keychain.item = _HEX
    _record(tmp_path).write_text(record_text, encoding="ascii")
    monkeypatch.setattr(winproc, "run", keychain)

    with pytest.raises(ProtectionError):
        protection._keychain_source(tmp_path, "linux")()
    assert protection._keyring == "unavailable"


def test_a_record_that_cannot_be_written_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(path: object) -> None:
        raise OSError("no space")

    keychain = FakeKeychain()
    monkeypatch.setattr(winproc, "run", keychain)
    monkeypatch.setattr(osfs, "open_new_private", refuse)

    with pytest.raises(ProtectionError):
        protection._keychain_source(tmp_path, "linux")()
    assert protection._keyring == "unavailable"
    assert not os.path.lexists(_record(tmp_path))


class _Unwritable:
    """A newly created record whose write fails, as on a full disk."""

    def __init__(self, file: BinaryIO) -> None:
        self.file = file

    def __enter__(self) -> _Unwritable:
        return self

    def __exit__(self, *exc: object) -> None:
        self.file.close()

    def write(self, data: bytes) -> int:
        raise OSError("no space")


def test_a_record_whose_write_fails_is_removed_and_a_later_source_records_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_open = osfs.open_new_private
    keychain = FakeKeychain()
    monkeypatch.setattr(winproc, "run", keychain)
    monkeypatch.setattr(osfs, "open_new_private", lambda path: _Unwritable(real_open(path)))

    with pytest.raises(ProtectionError):
        protection._keychain_source(tmp_path, "linux")()
    assert not os.path.lexists(_record(tmp_path))

    monkeypatch.setattr(osfs, "open_new_private", real_open)
    assert keychain.item is not None
    stored = bytes.fromhex(keychain.item)
    assert protection._keychain_source(tmp_path, "linux")() == stored
    assert _record(tmp_path).read_bytes() == hashlib.sha256(stored).digest()[:8].hex().encode()


@pytest.mark.parametrize(
    "run",
    [
        FakeKeychain(keep=False),  # the read-back differs from what was stored
        FakeRun((1, ""), (0, ""), (1, "")),  # the store "succeeds", the read-back finds nothing
        FakeRun(FileNotFoundError()),  # no secret-tool or security
        FakeRun(_Expired()),  # a locked keychain whose prompt was never answered
        FakeRun((0, "not hex\n")),  # a damaged item is refused, never overwritten
    ],
    ids=["mismatch", "lost", "missing_tool", "timeout", "damaged"],
)
def test_a_failing_keychain_fails_closed_once_per_process(
    run: FakeRun | FakeKeychain, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(winproc, "run", run)
    source = protection._keychain_source(tmp_path, "linux")

    with pytest.raises(ProtectionError):
        source()
    assert protection._keyring == "unavailable"
    calls = len(run.calls)
    with pytest.raises(ProtectionError):
        source()  # no second keychain visit: it would prompt again on every save
    assert len(run.calls) == calls


def test_a_missing_secret_tool_is_logged_by_name_and_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The log line is code-only: it names the program, never a path or a key."""
    monkeypatch.setattr(winproc, "run", FakeRun(FileNotFoundError()))
    caplog.set_level(logging.WARNING)

    with pytest.raises(ProtectionError):
        protection._keychain_source(tmp_path, "linux")()

    assert [r.getMessage() for r in caplog.records] == [
        "secret-tool was not found; install libsecret-tools"
    ]


def test_a_failing_keychain_that_is_not_a_missing_tool_logs_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(winproc, "run", FakeRun((0, "not hex\n")))
    caplog.set_level(logging.WARNING)

    with pytest.raises(ProtectionError):
        protection._keychain_source(tmp_path, "linux")()

    assert caplog.records == []


def test_the_linux_hint_names_secret_tool_and_the_other_platforms_do_not() -> None:
    assert "secret-tool" in protection._account_hint("linux")
    assert "libsecret-tools" in protection._account_hint("linux")
    assert "secret-tool" not in protection._account_hint("darwin")
    assert "secret-tool" not in protection._account_hint("win32")
    assert protection._account_hint("darwin") == "your macOS user account and Keychain"


def test_keyring_state_is_not_applicable_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(protection, "_keyring", "ready")
    expected = "not_applicable" if sys.platform == "win32" else "ready"
    assert protection.keyring_state() == expected


def test_the_account_names_the_real_data_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    keychain = FakeKeychain()
    keychain.item = _HEX
    monkeypatch.setattr(winproc, "run", keychain)
    (tmp_path / "data").mkdir()

    protection._keychain_source(tmp_path / "data" / ".." / "data", "linux")()

    assert keychain.calls[0][-1] == _account(tmp_path / "data")
    assert len(keychain.calls[0][-1]) == 32


@pytest.mark.keychain
@pytest.mark.skipif(sys.platform == "win32", reason="Windows seals with DPAPI, not a keychain")
def test_the_real_keychain_returns_one_key_per_data_directory(tmp_path: Path) -> None:
    assert sys.platform != "win32"
    account = _account(tmp_path)
    try:
        # The private builder, so a suite-wide stub of keychain_source cannot stand in for it.
        first = protection._keychain_source(tmp_path, sys.platform)
        key = first()
        assert first() == key
        assert protection._keychain_source(tmp_path, sys.platform)() == key
        assert protection.keyring_state() == "ready"
    finally:
        if sys.platform == "darwin":
            argv = ["/usr/bin/security", "delete-generic-password", "-s", "viki-agent"]
            winproc.run([*argv, "-a", account], cwd=Path.home())
        else:
            argv = ["secret-tool", "clear", "service", "viki-agent", "account", account]
            winproc.run(argv, cwd=Path.home())
