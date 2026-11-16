"""The OS folder dialog: each system's dialog argv, the output parser, the availability table
and the macOS and Linux pick over a fake `winproc.run`; on Windows, the COM dialog created,
configured, read back and released, its result reading over a real one-folder shell item
array, the apartment rules and the owner window. No test opens a dialog: Show and every dialog
program are refused unless a test scripts the program's reply."""

from __future__ import annotations

import ctypes
import os
import sys
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from ctypes import wintypes
from pathlib import Path
from types import SimpleNamespace

import pytest

from viki_agent import winproc
from viki_agent.app import folder_dialog
from viki_agent.app.folder_dialog import PickerError, PickerStatus

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="the COM dialog")
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="no owner window off Windows")

_TITLE = 'Pick "a" folder; $(id) `x`'
# PICKFOLDERS | FORCEFILESYSTEM | PATHMUSTEXIST | NOCHANGEDIR | DONTADDTORECENT
_FOLDER_OPTIONS = 0x20 | 0x40 | 0x800 | 0x8 | 0x2000000
_ALLOWMULTISELECT = 0x200
_COINIT_MULTITHREADED = 0x0


@pytest.fixture(autouse=True)
def _no_real_dialog(monkeypatch: pytest.MonkeyPatch) -> None:
    """pytest.fail raises a BaseException, so pick's `except Exception` cannot swallow it."""

    def refuse(*args: object, **kwargs: object) -> None:
        pytest.fail("a test tried to open a real folder dialog")

    monkeypatch.setattr(winproc, "run", refuse)
    if sys.platform == "win32":
        monkeypatch.setattr(folder_dialog, "_Show", refuse)


class _Expired(Exception):
    """Stands in for subprocess.TimeoutExpired, which tests may not import."""


_Reply = tuple[int, str] | tuple[int, str, str]


class FakeRun:
    """`winproc.run` as pick calls it: scripted replies (returncode, stdout, and optionally
    stderr, "" when left out), every call recorded."""

    def __init__(self, *replies: _Reply | BaseException) -> None:
        self.replies = list(replies)
        self.calls: list[tuple[list[str], object, float]] = []

    def __call__(
        self, argv: Sequence[str], *, cwd: str | os.PathLike[str], timeout: float = 15.0
    ) -> SimpleNamespace:
        self.calls.append((list(argv), cwd, timeout))
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        returncode, stdout, *rest = reply
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=rest[0] if rest else "")


def _posix(
    monkeypatch: pytest.MonkeyPatch, platform: str, *replies: _Reply | BaseException
) -> FakeRun:
    """Send `pick` down `platform`'s path on any host, over scripted dialog replies."""
    run = FakeRun(*replies)
    monkeypatch.setattr(sys, "platform", platform)  # pick reads it at call time
    monkeypatch.setattr(winproc, "run", run)
    return run


def _osascript(choose: str) -> list[str]:
    script = [
        "on run argv",
        f"set picked to ({choose}) as list",
        "set posixPaths to {}",
        "repeat with f in picked",
        "set end of posixPaths to POSIX path of f",
        "end repeat",
        "set AppleScript's text item delimiters to linefeed",
        "return posixPaths as text",
        "end run",
    ]
    return ["/usr/bin/osascript", *(arg for line in script for arg in ("-e", line)), _TITLE]


# --- argv, parsing and availability ------------------------------------------------------------


def test_macos_dialog_passes_the_title_as_one_argument_to_a_fixed_script() -> None:
    prompt = "choose folder with prompt (item 1 of argv)"
    assert folder_dialog._dialog_argvs("darwin", _TITLE, False) == [_osascript(prompt)]
    assert folder_dialog._dialog_argvs("darwin", _TITLE, True) == [
        _osascript(prompt + " with multiple selections allowed")
    ]


def test_linux_dialog_tries_zenity_then_kdialog_with_the_title_as_one_argument() -> None:
    zenity = ["zenity", "--file-selection", "--directory", "--title=" + _TITLE]
    kdialog = ["kdialog", "--title", _TITLE, "--getexistingdirectory", str(Path.home())]
    assert folder_dialog._dialog_argvs("linux", _TITLE, False) == [zenity, kdialog]
    assert folder_dialog._dialog_argvs("linux", _TITLE, True) == [
        [*zenity, "--multiple", "--separator=\n"],
        kdialog,  # kdialog picks one folder only
    ]
    for multiple in (False, True):
        for platform in ("linux", "darwin"):
            for argv in folder_dialog._dialog_argvs(platform, _TITLE, multiple):
                assert argv[0] not in ("sh", "/bin/sh", "bash")
                assert sum(_TITLE in arg for arg in argv) == 1


def test_windows_and_unknown_systems_have_no_dialog_program() -> None:
    assert folder_dialog._dialog_argvs("win32", _TITLE, True) == []
    assert folder_dialog._dialog_argvs("freebsd14", _TITLE, True) == []


@pytest.mark.parametrize(
    ("text", "paths"),
    [
        ("/Users/you/Documents/\n", ["/Users/you/Documents"]),
        ("/home/you/a\n/home/you/b c\n", ["/home/you/a", "/home/you/b c"]),
        ("\n/home/you/a\n\n", ["/home/you/a"]),
        ("/\n", ["/"]),
        ("", []),
        ("/home/you/a\u2028b\n", ["/home/you/a\u2028b"]),
        ("/" + "a" * 4095, ["/" + "a" * 4095]),
    ],
    ids=["macos-slash", "two", "blank-lines", "root", "empty", "line-separator-in-name", "4096"],
)
def test_parse_keeps_each_absolute_line(text: str, paths: list[str]) -> None:
    assert folder_dialog._parse_paths(text) == paths


@pytest.mark.parametrize(
    "text",
    [
        "relative/path\n",
        "/home/you/a\n~/b\n",
        " /home/you/a\n",
        "C:\\work\\you\n",
        "/" + "a" * 4096,
    ],
    ids=["relative", "one-relative", "leading-space", "windows", "4097"],
)
def test_parse_fails_on_any_other_line(text: str) -> None:
    with pytest.raises(PickerError) as failed:
        folder_dialog._parse_paths(text)
    assert failed.value.code == "failed"


@pytest.mark.parametrize(
    ("platform", "programs", "env", "expected"),
    [
        ("win32", set(), {}, PickerStatus(True, True, None)),
        ("darwin", {"/usr/bin/osascript"}, {}, PickerStatus(True, True, None)),
        ("darwin", set(), {}, PickerStatus(False, False, "no_program")),
        ("linux", {"zenity", "kdialog"}, {"DISPLAY": ":0"}, PickerStatus(True, True, None)),
        ("linux", {"zenity"}, {"WAYLAND_DISPLAY": "wayland-0"}, PickerStatus(True, True, None)),
        ("linux", {"kdialog"}, {"DISPLAY": ":0"}, PickerStatus(True, False, None)),
        ("linux", set(), {"DISPLAY": ":0"}, PickerStatus(False, False, "no_program")),
        ("linux", {"zenity", "kdialog"}, {}, PickerStatus(False, False, "no_display")),
        (
            "linux",
            {"zenity"},
            {"DISPLAY": "", "WAYLAND_DISPLAY": ""},
            PickerStatus(False, False, "no_display"),
        ),
        ("freebsd14", {"zenity"}, {"DISPLAY": ":0"}, PickerStatus(False, False, "unsupported")),
    ],
    ids=[
        "windows",
        "macos",
        "macos-no-osascript",
        "linux-zenity",
        "linux-wayland",
        "linux-kdialog-only",
        "linux-no-program",
        "linux-no-display",
        "linux-empty-display",
        "other",
    ],
)
def test_status_table(
    platform: str, programs: set[str], env: dict[str, str], expected: PickerStatus
) -> None:
    def which(name: str) -> str | None:
        return name if name in programs else None

    assert folder_dialog._status_for(platform, which, env) == expected


# --- pick on macOS and Linux, over a fake winproc.run ------------------------------------------


def test_pick_returns_the_chosen_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    run = _posix(monkeypatch, "linux", (0, "/home/you/a/\n/home/you/b\n"))
    assert folder_dialog.pick(_TITLE, True) == ["/home/you/a", "/home/you/b"]
    argv = folder_dialog._dialog_argvs("linux", _TITLE, True)[0]
    assert run.calls == [(argv, Path.home(), folder_dialog.PICK_TIMEOUT_S)]
    assert folder_dialog.PICK_TIMEOUT_S == 600


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_cancel_exit_is_none(monkeypatch: pytest.MonkeyPatch, platform: str) -> None:
    """Exit 1 is the user's Cancel (zenity, kdialog, osascript's error -128): no paths, and no
    second dialog after it."""
    run = _posix(monkeypatch, platform, (1, "", "execution error: User canceled. (-128)"))
    assert folder_dialog.pick(_TITLE, False) is None
    assert len(run.calls) == 1


def test_macos_exit_1_without_the_cancelled_message_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """osascript exits 1 on a real AppleScript error too, not only on Cancel; without "(-128)"
    in stderr it is "failed", not a cancel, so the "type the path instead" hint still shows."""
    run = _posix(monkeypatch, "darwin", (1, "", "execution error: Some AppleScript error."))
    with pytest.raises(PickerError) as failed:
        folder_dialog.pick(_TITLE, False)
    assert failed.value.code == "failed"
    assert len(run.calls) == 1


def test_a_zenity_that_cannot_open_its_display_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """GTK's gtk_init() exits 1 the same way zenity's own Cancel does when it cannot open the
    display (stale DISPLAY, no XAUTHORITY, dead X server): that must not read as Cancel, and
    kdialog must not be tried as a fallback for what is not a missing program."""
    run = _posix(monkeypatch, "linux", (1, "", "Gtk-WARNING **: cannot open display: :0\n"))
    with pytest.raises(PickerError) as failed:
        folder_dialog.pick(_TITLE, True)
    assert failed.value.code == "failed"
    assert [argv[0] for argv, _, _ in run.calls] == ["zenity"]


def test_a_cancel_whose_stderr_has_a_benign_xlib_warning_is_still_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing-extension warning is not a display-open failure: still the user's Cancel."""
    run = _posix(
        monkeypatch, "linux", (1, "", 'Xlib:  extension "RANDR" missing on display ":99".\n')
    )
    assert folder_dialog.pick(_TITLE, False) is None
    assert len(run.calls) == 1


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_pick_refuses_a_title_starting_with_a_dash(
    monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    """zenity and osascript read options until the first non-option argument, so a title
    starting with "-" could be read as one (for osascript, as another `-e` script line):
    refused before any dialog program runs."""
    run = _posix(monkeypatch, platform)
    with pytest.raises(PickerError) as failed:
        folder_dialog.pick("-e tell app", False)
    assert failed.value.code == "failed"
    assert run.calls == []


def test_a_dialog_left_open_past_the_timeout_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    run = _posix(monkeypatch, "linux", _Expired())
    assert folder_dialog.pick(_TITLE, False) is None
    assert len(run.calls) == 1


def test_a_missing_program_falls_through_to_the_next(monkeypatch: pytest.MonkeyPatch) -> None:
    run = _posix(monkeypatch, "linux", FileNotFoundError("zenity"), (0, "/home/you/a\n"))
    assert folder_dialog.pick(_TITLE, True) == ["/home/you/a"]
    assert [argv[0] for argv, _, _ in run.calls] == ["zenity", "kdialog"]


@pytest.mark.parametrize("platform", ["linux", "darwin", "freebsd14"])
def test_no_program_left_is_unavailable(monkeypatch: pytest.MonkeyPatch, platform: str) -> None:
    argvs = folder_dialog._dialog_argvs(platform, _TITLE, False)
    run = _posix(monkeypatch, platform, *(FileNotFoundError(argv[0]) for argv in argvs))
    with pytest.raises(PickerError) as refused:
        folder_dialog.pick(_TITLE, False)
    assert refused.value.code == "unavailable"
    assert len(run.calls) == len(argvs)


def test_another_exit_fails_without_a_second_dialog(monkeypatch: pytest.MonkeyPatch) -> None:
    run = _posix(monkeypatch, "linux", (2, ""))
    with pytest.raises(PickerError) as failed:
        folder_dialog.pick(_TITLE, False)
    assert failed.value.code == "failed"
    assert len(run.calls) == 1


def test_output_that_is_not_a_path_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    _posix(monkeypatch, "darwin", (0, "not a path\n"))
    with pytest.raises(PickerError) as failed:
        folder_dialog.pick(_TITLE, True)
    assert failed.value.code == "failed"


@posix_only
def test_owner_window_is_none_off_windows() -> None:
    assert folder_dialog.owner_window([lambda pid: True]) is None


# --- Windows: the COM dialog, never shown ------------------------------------------------------


def _on_a_new_thread[T](call: Callable[[], T]) -> T:
    """`call` on a fresh thread with its own COM state, as the route's worker runs pick."""
    with ThreadPoolExecutor(1) as pool:
        return pool.submit(call).result()


@windows_only
@pytest.mark.parametrize("multiple", [False, True], ids=["one", "multiple"])
def test_windows_options_round_trip(multiple: bool) -> None:
    """Create the dialog, configure it, read its options back and release it; never Show. The
    apartment is gone afterwards: the thread can then join the multithreaded one."""
    assert sys.platform == "win32"

    def round_trip() -> tuple[int, int, int, int]:
        with folder_dialog._apartment():
            dialog = folder_dialog._dialog(_TITLE, multiple)
            options = wintypes.DWORD()
            try:
                read = folder_dialog._GetOptions(dialog, ctypes.byref(options))
            finally:
                left = folder_dialog._Release(dialog)
        joined = folder_dialog._CoInitializeEx(None, _COINIT_MULTITHREADED)
        folder_dialog._CoUninitialize()
        return read, options.value, left, joined

    read, options, left, joined = _on_a_new_thread(round_trip)
    assert (read, left, joined) == (0, 0, 0)  # S_OK, no reference left, S_OK
    assert options & _FOLDER_OPTIONS == _FOLDER_OPTIONS
    assert bool(options & _ALLOWMULTISELECT) is multiple


@windows_only
def test_windows_reads_the_path_of_each_result(tmp_path: Path) -> None:
    """The half of a pick after Show, over a real one-folder IShellItemArray: GetCount,
    GetItemAt, GetDisplayName, CoTaskMemFree and Release."""
    assert sys.platform == "win32"
    shell32 = ctypes.WinDLL("shell32")
    item_from_name = shell32.SHCreateItemFromParsingName
    item_from_name.argtypes = (
        wintypes.LPCWSTR,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    )
    item_from_name.restype = ctypes.c_long
    array_from_item = shell32.SHCreateShellItemArrayFromShellItem
    array_from_item.argtypes = (ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))
    array_from_item.restype = ctypes.c_long
    iid_item = folder_dialog._guid("43826D1E-E718-42EE-BC55-A1E261C37BFE")  # IShellItem
    iid_array = folder_dialog._guid("B63EA76D-1F85-456F-A19C-48159EFA858B")  # IShellItemArray

    def read() -> list[str]:
        with folder_dialog._apartment():
            item, array = ctypes.c_void_p(), ctypes.c_void_p()
            hr = item_from_name(str(tmp_path), None, ctypes.byref(iid_item), ctypes.byref(item))
            assert hr == 0
            try:
                assert array_from_item(item, ctypes.byref(iid_array), ctypes.byref(array)) == 0
                try:
                    return folder_dialog._paths(array)
                finally:
                    folder_dialog._release(array)
            finally:
                folder_dialog._release(item)

    assert _on_a_new_thread(read) == [str(tmp_path)]


@windows_only
def test_windows_refuses_a_thread_already_in_the_multithreaded_apartment() -> None:
    """RPC_E_CHANGED_MODE: "failed" before any dialog exists, and the thread's own COM is left
    as it was (still initialized, so a second join answers S_FALSE)."""
    assert sys.platform == "win32"

    def attempt() -> tuple[str, int]:
        assert folder_dialog._CoInitializeEx(None, _COINIT_MULTITHREADED) == 0
        try:
            with pytest.raises(PickerError) as failed:
                folder_dialog.pick(_TITLE, False)
            again = folder_dialog._CoInitializeEx(None, _COINIT_MULTITHREADED)
            folder_dialog._CoUninitialize()
            return failed.value.code, again
        finally:
            folder_dialog._CoUninitialize()

    assert _on_a_new_thread(attempt) == ("failed", 1)


@windows_only
def test_owner_window_is_the_foreground_window_only_when_a_hold_claims_its_process() -> None:
    assert sys.platform == "win32"
    asked: list[int] = []

    def claims(pid: int) -> bool:
        asked.append(pid)
        return True

    def closing(pid: int) -> bool:
        raise OSError("the job closed meanwhile")

    hwnd = folder_dialog.owner_window([claims])
    if asked:  # a foreground window exists: its process was the one asked about
        assert hwnd
        pid = wintypes.DWORD()
        folder_dialog._GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        assert asked == [pid.value]
    else:
        assert hwnd is None
    assert folder_dialog.owner_window([lambda pid: False]) is None
    assert folder_dialog.owner_window([closing]) is None
    assert folder_dialog.owner_window([]) is None


@windows_only
def test_windows_can_always_show_the_dialog() -> None:
    assert folder_dialog.status() == PickerStatus(True, True, None)
