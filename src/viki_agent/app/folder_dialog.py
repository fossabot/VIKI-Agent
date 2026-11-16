"""The OS folder dialog: the user picks folders and the app gets their absolute paths, nothing
more.

`pick()` blocks until the dialog closes and returns the chosen folders, or None when it was
cancelled; it raises `PickerError` "unavailable" when no dialog program starts and "failed"
when the dialog fails. Windows shows the shell's IFileOpenDialog in folder mode through COM on
the calling thread, which it puts in a single-threaded apartment for the dialog's life. macOS
runs osascript's `choose folder`; Linux runs zenity, else kdialog, which picks one folder only.
Those run through `winproc.run` with the title as one argument of its own, never part of a
script or a shell line, and print the chosen paths one per line. `status()` says whether this
computer can show the dialog, and for more than one folder; `owner_window()` names the app
window a Windows dialog belongs to. Nothing here logs a path.
"""

from __future__ import annotations

import ctypes
import os
import shutil
import sys
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from viki_agent import winproc

# How long a macOS or Linux dialog may stay open before it is closed as if cancelled.
PICK_TIMEOUT_S = 600.0
_MAX_PATH_CHARS = 4096
# Lowercased stderr substrings meaning a dialog could not open its display, not that the user
# cancelled: zenity's GTK init exits 1 the same way Cancel does when it cannot reach one.
_DISPLAY_FAILURES = (
    "cannot open display",
    "failed to open display",
    "could not connect to display",
)


@dataclass(frozen=True)
class PickerStatus:
    """Whether this computer can show the folder dialog, and for more than one folder at a time;
    `reason` says why not when it cannot."""

    available: bool
    multiple: bool
    reason: Literal["unsupported", "no_program", "no_display"] | None


class PickerError(Exception):
    """No folder from the dialog: `code` is "unavailable" (no dialog program started) or
    "failed"; never a path."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _dialog_argvs(platform: str, title: str, multiple: bool) -> list[list[str]]:
    """The dialog commands to try in order on `platform`: none on Windows (a COM dialog) or on
    a system with no known dialog. The title is always one argument of its own."""
    if platform == "darwin":
        choose = "choose folder with prompt (item 1 of argv)"
        if multiple:
            choose += " with multiple selections allowed"
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
        return [["/usr/bin/osascript", *(arg for line in script for arg in ("-e", line)), title]]
    if platform == "linux":
        zenity = ["zenity", "--file-selection", "--directory", "--title=" + title]
        if multiple:
            zenity += ["--multiple", "--separator=\n"]
        return [zenity, ["kdialog", "--title", title, "--getexistingdirectory", str(Path.home())]]
    return []


def _parse_paths(text: str) -> list[str]:
    """The paths a dialog printed, one per line (split at LF only), without the trailing slash
    macOS gives a folder; PickerError "failed" when a non-empty line is not an absolute POSIX
    path of at most 4096 characters."""
    paths: list[str] = []
    for line in text.split("\n"):
        if not line:
            continue
        path = line.removesuffix("/") or "/"
        if not path.startswith("/") or len(path) > _MAX_PATH_CHARS:
            raise PickerError("failed")
        paths.append(path)
    return paths


def _status_for(
    platform: str, which: Callable[[str], str | None], env: Mapping[str, str]
) -> PickerStatus:
    """What `platform` can show, given `which` (shutil.which) and the environment `env`."""
    if platform == "win32":
        return PickerStatus(True, True, None)
    if platform == "darwin":
        if which("/usr/bin/osascript"):
            return PickerStatus(True, True, None)
        return PickerStatus(False, False, "no_program")
    if platform != "linux":
        return PickerStatus(False, False, "unsupported")
    if not env.get("DISPLAY") and not env.get("WAYLAND_DISPLAY"):
        return PickerStatus(False, False, "no_display")
    if which("zenity"):
        return PickerStatus(True, True, None)
    if which("kdialog"):
        return PickerStatus(True, False, None)  # kdialog picks one folder at a time
    return PickerStatus(False, False, "no_program")


def status() -> PickerStatus:
    """Whether this computer can show the folder dialog."""
    return _status_for(sys.platform, shutil.which, os.environ)


def pick(title: str, multiple: bool, owner: int | None = None) -> list[str] | None:
    """Show the folder dialog titled `title` and block until it closes: the chosen folders'
    absolute paths (more than one only when `multiple`), or None when it was cancelled, or, off
    Windows, left open past PICK_TIMEOUT_S, which closes it. `owner` is the window handle a
    Windows dialog belongs to (see owner_window); other systems ignore it. Raises PickerError
    "unavailable" when no dialog program starts, and "failed" when the dialog fails, `title`
    starts with "-" (a dialog program could read it as an option instead of the title), the
    dialog could not open its display (zenity's GTK exiting 1 for a stale DISPLAY, missing
    XAUTHORITY, or a dead X server), or, on macOS, exit 1 is a real AppleScript error rather
    than the user's Cancel ("(-128)" in its stderr)."""
    if title.startswith("-"):
        raise PickerError("failed")
    if sys.platform == "win32":
        return _pick_windows(title, multiple, owner)
    for argv in _dialog_argvs(sys.platform, title, multiple):
        try:
            done = winproc.run(argv, cwd=Path.home(), timeout=PICK_TIMEOUT_S)
        except OSError:
            continue  # not installed: try the next program
        except Exception:  # left open past the timeout (TimeoutExpired; TID251 bans naming it)
            return None
        if done.returncode == 1:
            # zenity's GTK also exits 1 when it cannot open its display; osascript exits 1 on a
            # real AppleScript error, which is not the user's Cancel.
            if sys.platform == "darwin" and "(-128)" not in done.stderr:
                raise PickerError("failed")
            if any(phrase in done.stderr.lower() for phrase in _DISPLAY_FAILURES):
                raise PickerError("failed")
            return None
        if done.returncode != 0:
            raise PickerError("failed")
        return _parse_paths(done.stdout)
    raise PickerError("unavailable")


if sys.platform == "win32":
    _ole32 = ctypes.WinDLL("ole32")
    _user32 = ctypes.WinDLL("user32", use_last_error=True)

    _S_OK, _S_FALSE = 0, 1
    _CANCELLED = 0x800704C7  # HRESULT_FROM_WIN32(ERROR_CANCELLED): the user closed the dialog
    _COINIT_APARTMENTTHREADED = 0x2
    _COINIT_DISABLE_OLE1DDE = 0x4
    _CLSCTX_INPROC_SERVER = 0x1
    # PICKFOLDERS | FORCEFILESYSTEM | PATHMUSTEXIST | NOCHANGEDIR | DONTADDTORECENT
    _FOS_FOLDERS = 0x20 | 0x40 | 0x800 | 0x8 | 0x2000000
    _FOS_ALLOWMULTISELECT = 0x200
    _SIGDN_FILESYSPATH = 0x80058000

    def _guid(text: str) -> ctypes.Array[ctypes.c_ubyte]:
        """A GUID laid out as COM reads it."""
        return (ctypes.c_ubyte * 16).from_buffer_copy(uuid.UUID(text).bytes_le)

    _CLSID_FILE_OPEN_DIALOG = _guid("DC1C5A9C-E88A-4DDE-A5A1-60F82A20AEF7")
    _IID_IFILE_OPEN_DIALOG = _guid("D57C7288-D4AD-4768-BE02-9D969532D960")

    _CoInitializeEx = _ole32.CoInitializeEx
    _CoInitializeEx.argtypes = (wintypes.LPVOID, wintypes.DWORD)
    _CoInitializeEx.restype = ctypes.c_long
    _CoUninitialize = _ole32.CoUninitialize
    _CoUninitialize.argtypes = ()
    _CoUninitialize.restype = None
    _CoCreateInstance = _ole32.CoCreateInstance
    _CoCreateInstance.argtypes = (
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.LPVOID,
        ctypes.POINTER(ctypes.c_void_p),
    )
    _CoCreateInstance.restype = ctypes.c_long
    _CoTaskMemFree = _ole32.CoTaskMemFree
    _CoTaskMemFree.argtypes = (wintypes.LPVOID,)
    _CoTaskMemFree.restype = None
    _GetForegroundWindow = _user32.GetForegroundWindow
    _GetForegroundWindow.argtypes = ()
    _GetForegroundWindow.restype = wintypes.HWND
    _GetWindowThreadProcessId = _user32.GetWindowThreadProcessId
    _GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
    _GetWindowThreadProcessId.restype = wintypes.DWORD

    # COM methods by vtable slot; each call passes the interface (a c_void_p) first. HRESULTs
    # come back as c_long rather than ctypes.HRESULT, which would raise instead of answering.
    _HR = ctypes.c_long
    _OUT = ctypes.POINTER(ctypes.c_void_p)
    _Release = ctypes.WINFUNCTYPE(wintypes.ULONG)(2, "Release")  # IUnknown, on every interface
    # IFileOpenDialog
    _Show = ctypes.WINFUNCTYPE(_HR, wintypes.HWND)(3, "Show")
    _SetOptions = ctypes.WINFUNCTYPE(_HR, wintypes.DWORD)(9, "SetOptions")
    _GetOptions = ctypes.WINFUNCTYPE(_HR, ctypes.POINTER(wintypes.DWORD))(10, "GetOptions")
    _SetTitle = ctypes.WINFUNCTYPE(_HR, wintypes.LPCWSTR)(17, "SetTitle")
    _GetResults = ctypes.WINFUNCTYPE(_HR, _OUT)(27, "GetResults")
    # IShellItemArray
    _GetCount = ctypes.WINFUNCTYPE(_HR, ctypes.POINTER(wintypes.DWORD))(7, "GetCount")
    _GetItemAt = ctypes.WINFUNCTYPE(_HR, wintypes.DWORD, _OUT)(8, "GetItemAt")
    # IShellItem
    _GetDisplayName = ctypes.WINFUNCTYPE(_HR, wintypes.DWORD, _OUT)(5, "GetDisplayName")

    def _check(hr: int) -> None:
        if hr != _S_OK:
            raise PickerError("failed")

    def _release(unknown: ctypes.c_void_p) -> None:
        """Release an interface this code holds; a NULL one is left alone."""
        if unknown.value:
            _Release(unknown)

    @contextmanager
    def _apartment() -> Iterator[None]:
        """The calling thread in a single-threaded apartment, as the dialog needs, until the
        block ends. A thread already in the multithreaded one (RPC_E_CHANGED_MODE), or any
        other refusal, raises PickerError "failed" and keeps its COM state as it was."""
        hr = _CoInitializeEx(None, _COINIT_APARTMENTTHREADED | _COINIT_DISABLE_OLE1DDE)
        if hr not in (_S_OK, _S_FALSE):
            raise PickerError("failed")
        try:
            yield
        finally:
            _CoUninitialize()

    def _dialog(title: str, multiple: bool) -> ctypes.c_void_p:
        """A new IFileOpenDialog set to pick folders, which the caller releases."""
        dialog = ctypes.c_void_p()
        _check(
            _CoCreateInstance(
                ctypes.byref(_CLSID_FILE_OPEN_DIALOG),
                None,
                _CLSCTX_INPROC_SERVER,
                ctypes.byref(_IID_IFILE_OPEN_DIALOG),
                ctypes.byref(dialog),
            )
        )
        try:
            options = wintypes.DWORD()
            _check(_GetOptions(dialog, ctypes.byref(options)))
            wanted = _FOS_FOLDERS | (_FOS_ALLOWMULTISELECT if multiple else 0)
            _check(_SetOptions(dialog, options.value | wanted))
            _check(_SetTitle(dialog, title))
        except BaseException:
            _release(dialog)
            raise
        return dialog

    def _paths(results: ctypes.c_void_p) -> list[str]:
        """The file-system path of each item in the IShellItemArray `results`."""
        count = wintypes.DWORD()
        _check(_GetCount(results, ctypes.byref(count)))
        paths: list[str] = []
        for index in range(count.value):
            item = ctypes.c_void_p()
            try:
                _check(_GetItemAt(results, index, ctypes.byref(item)))
                name = ctypes.c_void_p()
                _check(_GetDisplayName(item, _SIGDN_FILESYSPATH, ctypes.byref(name)))
                try:
                    paths.append(ctypes.wstring_at(name))
                finally:
                    _CoTaskMemFree(name)
            finally:
                _release(item)
        return paths

    def _pick_windows(title: str, multiple: bool, owner: int | None) -> list[str] | None:
        with _apartment():
            dialog = _dialog(title, multiple)
            results = ctypes.c_void_p()
            try:
                hr = _Show(dialog, owner)
                if hr & 0xFFFFFFFF == _CANCELLED:
                    return None
                _check(hr)
                _check(_GetResults(dialog, ctypes.byref(results)))
                return _paths(results)
            finally:
                _release(results)
                _release(dialog)

    def owner_window(holds: Iterable[Callable[[int], bool]]) -> int | None:
        """The foreground window when one of `holds` claims its process (an app window the
        user is in), else None, and the dialog then has no owner window. A hold that raises
        OSError (its window's job closing meanwhile) also gives None."""
        hwnd = _GetForegroundWindow()
        if not hwnd:
            return None
        pid = wintypes.DWORD()
        _GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        try:
            claimed = any(hold(pid.value) for hold in holds)
        except OSError:
            return None
        return hwnd if claimed else None

else:

    def owner_window(holds: Iterable[Callable[[int], bool]]) -> int | None:
        """Off Windows no dialog takes an owner window."""
        del holds
        return None
