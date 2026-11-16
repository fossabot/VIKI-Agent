"""Probe the machine for the voice setup and recommend a backend and model.

Setup-only: the server never imports this script. The real ctypes calls run on Windows only.
"""

from __future__ import annotations

import ctypes
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from platform import machine as _machine
from typing import Any

from viki_agent.voice import voice_models

# The driver CUDA version the pinned whisper.cpp build needs (12.4), as 1000*major + 10*minor.
CUDA_MIN_DRIVER = 12040
_VC_DLLS = ("msvcp140.dll", "vcruntime140.dll", "vcruntime140_1.dll", "vcomp140.dll")
_HEADROOM = 1.5  # the measured peak is not the whole story: leave room for the display and driver


@dataclass(frozen=True)
class Gpu:
    name: str
    memory: int
    cuda_version: int


@dataclass(frozen=True)
class Machine:
    platform: str
    arch: str
    cpus: int
    memory: int
    nvidia: Gpu | None
    vc_runtime: bool


def _system32() -> Path:
    return Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32"


def _total_memory() -> int:
    if sys.platform == "win32":
        from ctypes import wintypes

        class _MemoryStatusEx(ctypes.Structure):
            _fields_ = [
                ("dwLength", wintypes.DWORD),
                ("dwMemoryLoad", wintypes.DWORD),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(_MemoryStatusEx)]
        status = _MemoryStatusEx()
        status.dwLength = ctypes.sizeof(status)
        if not kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            raise OSError("GlobalMemoryStatusEx failed")
        return status.ullTotalPhys
    return os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")


def _vc_runtime() -> bool:
    return all((_system32() / name).is_file() for name in _VC_DLLS)


def _load_nvcuda() -> Any | None:
    if sys.platform != "win32":
        return None
    path = _system32() / "nvcuda.dll"
    try:
        return ctypes.WinDLL(str(path)) if path.is_file() else None
    except OSError:
        return None


def _first_gpu(nvcuda: Any) -> Gpu | None:
    """Ask the driver about device 0; any nonzero result means no usable GPU."""
    try:
        if nvcuda.cuInit(0):
            return None
        version, count = ctypes.c_int(), ctypes.c_int()
        if nvcuda.cuDriverGetVersion(ctypes.pointer(version)):
            return None
        if nvcuda.cuDeviceGetCount(ctypes.pointer(count)) or count.value < 1:
            return None
        device = ctypes.c_int()
        if nvcuda.cuDeviceGet(ctypes.pointer(device), 0):
            return None
        name = ctypes.create_string_buffer(256)
        if nvcuda.cuDeviceGetName(name, 256, device):
            return None
        total = ctypes.c_size_t()
        if nvcuda.cuDeviceTotalMem_v2(ctypes.pointer(total), device):
            return None
    except OSError:
        return None
    return Gpu(name.value.decode("utf-8", "replace"), total.value, version.value)


def probe(
    *, platform: str = sys.platform, arch: str = _machine(), nvcuda: object | None = None
) -> Machine:
    nvidia, vc_runtime = None, False
    if platform == "win32":
        vc_runtime = _vc_runtime()
        driver = nvcuda if nvcuda is not None else _load_nvcuda()
        nvidia = _first_gpu(driver) if driver is not None else None
    return Machine(platform, arch, os.cpu_count() or 1, _total_memory(), nvidia, vc_runtime)


def _backend(machine: Machine) -> str:
    if machine.platform == "darwin" and machine.arch == "arm64":
        return "metal"
    gpu = machine.nvidia
    if (
        machine.platform == "win32"
        and machine.arch == "AMD64"
        and gpu is not None
        and gpu.cuda_version >= CUDA_MIN_DRIVER
        and machine.vc_runtime
    ):
        return "cuda"
    return "cpu"


def _fits(model_id: str, memory: int) -> bool:
    return voice_models.MODELS[model_id].peak_vram * _HEADROOM <= memory


def _gpu_model(backend: str, machine: Machine) -> str | None:
    """The first ranked model that fits the backend's memory on this machine, or None."""
    if backend == "cuda":
        if machine.nvidia is None:
            return None
        memory = machine.nvidia.memory
    else:
        memory = machine.memory // 2  # Apple silicon: the GPU shares the physical memory
    return next((m for m in voice_models.RANKING[backend] if _fits(m, memory)), None)


def _cpu_model(machine: Machine) -> str:
    cpu_ok = (
        machine.cpus >= voice_models.CPU_MIN_CPUS and machine.memory >= voice_models.CPU_MIN_MEMORY
    )
    return voice_models.RANKING["cpu"][0] if cpu_ok else "small-q8_0"


def recommend(machine: Machine, backend: str | None = None) -> tuple[str, str]:
    """Return (backend, model). With no `backend`, a GPU backend with no fitting model falls to
    the CPU. A chosen backend is kept, and with no fitting model gets the CPU rule's model (the
    worker then falls back to the CPU visibly)."""
    chosen = backend or _backend(machine)
    if chosen != "cpu":
        model = _gpu_model(chosen, machine)
        if model is not None:
            return chosen, model
        if backend is None:
            chosen = "cpu"
    return chosen, _cpu_model(machine)
