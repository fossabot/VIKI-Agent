"""Native GPU calls for the voice worker: DLL preload, loaded-module checks, ggml devices.

The worker imports this module under ``python -I -B``, so it uses only the standard library.
The cuda backend relies on the Windows loader's loaded-module rule: the worker loads the stored
binding DLL by full path before the engine imports, so the engine binds to that copy.
"""

from __future__ import annotations

import ctypes
import errno
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

DEVICE_GPU = 1
DEVICE_IGPU = 2
MAX_DEVICES = 64
MAX_TEXT = 256
_PATH_CHARS = 32_768

if sys.platform == "win32":
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _GetModuleHandleW = _kernel32.GetModuleHandleW
    _GetModuleHandleW.restype = wintypes.HMODULE
    _GetModuleHandleW.argtypes = (wintypes.LPCWSTR,)
    _GetModuleFileNameW = _kernel32.GetModuleFileNameW
    _GetModuleFileNameW.restype = wintypes.DWORD
    _GetModuleFileNameW.argtypes = (wintypes.HMODULE, wintypes.LPWSTR, wintypes.DWORD)


@dataclass(frozen=True)
class Device:
    index: int
    kind: int
    name: str
    description: str
    free: int
    total: int


def _require_absolute(path: Path) -> None:
    if not path.is_absolute():
        raise ValueError(f"not an absolute path: {path}")


def preload(path: Path) -> None:
    """Load a DLL by full path; its dependencies resolve from its own folder, then System32."""
    _require_absolute(path)
    if sys.platform != "win32":
        raise OSError(errno.ENOSYS, "preload is Windows-only")
    # A full path makes ctypes add LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR; ctypes never unloads a
    # library, so the module stays in the loaded-module list for the process's life.
    ctypes.WinDLL(str(path))


def module_file(name: str) -> str | None:
    """The file a loaded module named `name` came from, or None when none is loaded."""
    if sys.platform != "win32":
        return None
    module = _GetModuleHandleW(name)
    if not module:
        return None
    buffer = ctypes.create_unicode_buffer(_PATH_CHARS)
    if not _GetModuleFileNameW(module, buffer, _PATH_CHARS):
        raise ctypes.WinError(ctypes.get_last_error(), "GetModuleFileNameW")
    return buffer.value


def is_loaded(path: Path) -> bool:
    """Whether the module at `path` is loaded in this process."""
    if sys.platform != "win32":
        return False
    return bool(_GetModuleHandleW(str(path)))


def _text(raw: bytes | None) -> str:
    return (raw or b"").decode("utf-8", "replace")[:MAX_TEXT]


def devices(ggml: Path, ggml_base: Path) -> list[Device]:
    """The ggml backend devices registered in the libraries at these two paths."""
    _require_absolute(ggml)
    _require_absolute(ggml_base)
    lib, base = ctypes.CDLL(str(ggml)), ctypes.CDLL(str(ggml_base))
    lib.ggml_backend_dev_count.restype = ctypes.c_size_t
    lib.ggml_backend_dev_count.argtypes = ()
    lib.ggml_backend_dev_get.restype = ctypes.c_void_p
    lib.ggml_backend_dev_get.argtypes = (ctypes.c_size_t,)
    base.ggml_backend_dev_type.restype = ctypes.c_int
    base.ggml_backend_dev_type.argtypes = (ctypes.c_void_p,)
    for text_fn in (base.ggml_backend_dev_name, base.ggml_backend_dev_description):
        text_fn.restype = ctypes.c_char_p
        text_fn.argtypes = (ctypes.c_void_p,)
    size_p = ctypes.POINTER(ctypes.c_size_t)
    base.ggml_backend_dev_memory.restype = None
    base.ggml_backend_dev_memory.argtypes = (ctypes.c_void_p, size_p, size_p)

    found: list[Device] = []
    for index in range(min(lib.ggml_backend_dev_count(), MAX_DEVICES)):
        dev = lib.ggml_backend_dev_get(index)
        if not dev:  # a NULL device would crash every call below
            continue
        free, total = ctypes.c_size_t(), ctypes.c_size_t()
        base.ggml_backend_dev_memory(dev, ctypes.byref(free), ctypes.byref(total))
        found.append(
            Device(
                index=index,
                kind=base.ggml_backend_dev_type(dev),
                name=_text(base.ggml_backend_dev_name(dev)),
                description=_text(base.ggml_backend_dev_description(dev)),
                free=free.value,
                total=total.value,
            )
        )
    return found


def first_gpu(devices: Iterable[Device], prefix: str) -> Device | None:
    """The first GPU or IGPU device whose name starts with `prefix`, as whisper.cpp picks."""
    return next(
        (d for d in devices if d.kind in (DEVICE_GPU, DEVICE_IGPU) and d.name.startswith(prefix)),
        None,
    )
