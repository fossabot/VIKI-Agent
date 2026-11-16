"""Native library preload, loaded-module checks and the ggml device query."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from viki_agent.voice import voice_gpu
from viki_agent.voice.voice_gpu import DEVICE_GPU, DEVICE_IGPU, Device, first_gpu

BINDING = "whisper-fa12b85c909a26b32bc9c8ea9b0dcc91.dll"
windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows loader")


def _device(index: int, kind: int, name: str) -> Device:
    return Device(index, kind, name, f"{name} description", 1, 2)


def test_first_gpu_skips_cpu_devices() -> None:
    cpu = _device(0, 0, "CUDA-like CPU")
    gpu = _device(1, DEVICE_GPU, "CUDA0")
    assert first_gpu([cpu, gpu], "CUDA") == gpu


def test_first_gpu_takes_an_igpu_and_the_first_match() -> None:
    igpu = _device(0, DEVICE_IGPU, "CUDA0")
    gpu = _device(1, DEVICE_GPU, "CUDA1")
    assert first_gpu([igpu, gpu], "CUDA") == igpu


def test_first_gpu_needs_the_name_prefix() -> None:
    accel = _device(0, 3, "CUDA0")
    other = _device(1, DEVICE_GPU, "Vulkan0")
    assert first_gpu([accel, other], "CUDA") is None
    assert first_gpu([], "CUDA") is None


def test_preload_refuses_a_relative_path() -> None:
    with pytest.raises(ValueError):
        voice_gpu.preload(Path(BINDING))


def test_devices_refuses_a_relative_path() -> None:
    with pytest.raises(ValueError):
        voice_gpu.devices(Path("ggml.dll"), Path("ggml-base.dll"))


@windows_only
def test_loaded_module_queries_on_windows() -> None:
    from viki_agent.winproc import system_dir

    found = voice_gpu.module_file("kernel32.dll")
    assert found is not None and found.lower().endswith("kernel32.dll")
    assert voice_gpu.is_loaded(system_dir() / "kernel32.dll")
    assert voice_gpu.module_file("no-such-module.dll") is None
    assert not voice_gpu.is_loaded(system_dir() / "no-such-module.dll")


@pytest.mark.skipif(sys.platform == "win32", reason="the non-Windows answers")
def test_loader_answers_off_windows(tmp_path: Path) -> None:
    assert voice_gpu.module_file("libc.so.6") is None
    assert not voice_gpu.is_loaded(tmp_path / BINDING)
    with pytest.raises(OSError):
        voice_gpu.preload(tmp_path / BINDING)


@windows_only
@pytest.mark.skipif(
    not os.environ.get("VIKI_VOICE_CUDA_DIR"), reason="VIKI_VOICE_CUDA_DIR names no cuda folder"
)
def test_cuda_backend_reports_a_cuda_device() -> None:
    folder = Path(os.environ["VIKI_VOICE_CUDA_DIR"]).resolve()
    voice_gpu.preload(folder / BINDING)
    assert voice_gpu.is_loaded(folder / BINDING)
    device = first_gpu(voice_gpu.devices(folder / "ggml.dll", folder / "ggml-base.dll"), "CUDA")
    assert device is not None and device.total > 0, "no CUDA device"
