"""Keep VIKI's logo on the app's Edge windows in the taskbar (Windows only).

Edge gives an `--app` window its title-bar and taskbar icons from the page's favicon, chosen for
16 DIP: the taskbar's larger icon is that small frame scaled up, so it blurs, and a favicon Edge
cached keeps showing after the file changes. Windows draws the taskbar button (and Alt+Tab) from
the window's own WM_GETICON icons, so apply() gives every visible top-level window whose process
is in one of the app windows' process trees the icon file's own frames at that window's DPI:
ICON_BIG at SM_CXICON, ICON_SMALL at SM_CXSMICON. A window already showing ours
is left alone; one Edge has reset is set again on the next pass. Edge draws its title bar itself,
from the favicon, so that one follows index.html's versioned favicon link, not this module.

Messages go out with SendMessageTimeoutW (SMTO_ABORTIFHUNG, 200 ms): a hung browser costs a pass
a fraction of a second, never the caller's thread. Loaded icons are cached per (file, size) for
the life of the process; there are only ever a few.
"""

from __future__ import annotations

import ctypes
import sys
from collections.abc import Callable, Sequence
from ctypes import wintypes
from pathlib import Path

Holds = Callable[[int], bool]

_WM_GETICON = 0x007F
_WM_SETICON = 0x0080
# No ICON_SMALL2: it is a WM_GETICON value only, and WM_SETICON takes it as ICON_BIG.
_ICON_SMALL, _ICON_BIG = 0, 1
_SM_CXICON, _SM_CXSMICON = 11, 49
_IMAGE_ICON = 1
_LR_LOADFROMFILE = 0x0010
_SMTO_ABORTIFHUNG = 0x0002
_TIMEOUT_MS = 200
_DEFAULT_DPI = 96

_icons: dict[tuple[str, int], int] = {}

if sys.platform == "win32":
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    _EnumWindows = _user32.EnumWindows
    _EnumWindows.argtypes = (_WNDENUMPROC, wintypes.LPARAM)
    _EnumWindows.restype = wintypes.BOOL
    _IsWindowVisible = _user32.IsWindowVisible
    _IsWindowVisible.argtypes = (wintypes.HWND,)
    _IsWindowVisible.restype = wintypes.BOOL
    _GetWindowThreadProcessId = _user32.GetWindowThreadProcessId
    _GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
    _GetWindowThreadProcessId.restype = wintypes.DWORD
    _GetDpiForWindow = _user32.GetDpiForWindow
    _GetDpiForWindow.argtypes = (wintypes.HWND,)
    _GetDpiForWindow.restype = wintypes.UINT
    _GetSystemMetricsForDpi = _user32.GetSystemMetricsForDpi
    _GetSystemMetricsForDpi.argtypes = (ctypes.c_int, wintypes.UINT)
    _GetSystemMetricsForDpi.restype = ctypes.c_int
    _LoadImageW = _user32.LoadImageW
    _LoadImageW.argtypes = (
        wintypes.HINSTANCE,
        wintypes.LPCWSTR,
        wintypes.UINT,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.UINT,
    )
    _LoadImageW.restype = wintypes.HANDLE
    _SendMessageTimeoutW = _user32.SendMessageTimeoutW
    _SendMessageTimeoutW.argtypes = (
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
        wintypes.UINT,
        wintypes.UINT,
        ctypes.POINTER(ctypes.c_size_t),
    )
    _SendMessageTimeoutW.restype = wintypes.LPARAM

    def _windows() -> list[tuple[int, int]]:
        """(hwnd, pid) for every visible top-level window."""
        found: list[tuple[int, int]] = []

        def visit(hwnd: int, _lparam: int) -> bool:
            if _IsWindowVisible(hwnd):
                pid = wintypes.DWORD()
                _GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                found.append((hwnd, pid.value))
            return True

        _EnumWindows(_WNDENUMPROC(visit), 0)
        return found

    def _load(icon: Path, size: int) -> int:
        key = (str(icon), size)
        if key not in _icons:
            _icons[key] = (
                _LoadImageW(None, str(icon), _IMAGE_ICON, size, size, _LR_LOADFROMFILE) or 0
            )
        return _icons[key]

    def _send(hwnd: int, message: int, wparam: int, lparam: int) -> int | None:
        """The reply, or None when the window did not answer in time (hung or gone)."""
        result = ctypes.c_size_t()
        if not _SendMessageTimeoutW(
            hwnd, message, wparam, lparam, _SMTO_ABORTIFHUNG, _TIMEOUT_MS, ctypes.byref(result)
        ):
            return None
        return result.value

    def apply(icon: Path, holds: Sequence[Holds]) -> int:
        """Give `icon`'s frames to every visible top-level window whose process one of `holds`
        claims and which does not already show them; how many windows were set. 0 with
        nothing to hold, or when the icon file cannot be loaded."""
        if not holds:
            return 0
        owned: dict[int, bool] = {}
        changed = 0
        for hwnd, pid in _windows():
            if pid not in owned:
                owned[pid] = any(hold(pid) for hold in holds)
            if not owned[pid]:
                continue
            dpi = _GetDpiForWindow(hwnd) or _DEFAULT_DPI
            big = _load(icon, _GetSystemMetricsForDpi(_SM_CXICON, dpi))
            small = _load(icon, _GetSystemMetricsForDpi(_SM_CXSMICON, dpi))
            if not big or not small:
                continue
            if _send(hwnd, _WM_GETICON, _ICON_BIG, 0) == big:
                continue
            _send(hwnd, _WM_SETICON, _ICON_BIG, big)
            _send(hwnd, _WM_SETICON, _ICON_SMALL, small)
            changed += 1
        return changed

else:

    def apply(icon: Path, holds: Sequence[Holds]) -> int:
        """Off Windows the browser owns its icons: nothing to set."""
        del icon, holds
        return 0
