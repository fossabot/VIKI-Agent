"""voice_machine: the probe reads the driver through a fake nvcuda; recommend picks the backend
and model that fit the machine."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))

import voice_machine as vm  # noqa: E402

from viki_agent.voice import voice_models  # noqa: E402

GIB = 1024**3
# the shipped tables, read before the autouse fixture swaps in small test ones
SHIPPED_MODELS = dict(voice_models.MODELS)
SHIPPED_RANKING = dict(voice_models.RANKING)


class FakeNvcuda:
    def __init__(self, *, version: int = 12080, memory: int = 8 * GIB, error: int = 0) -> None:
        self.version, self.memory, self.error = version, memory, error

    def cuInit(self, flags: int) -> int:  # noqa: N802
        return self.error

    def cuDriverGetVersion(self, out: Any) -> int:  # noqa: N802
        out.contents.value = self.version
        return 0

    def cuDeviceGetCount(self, out: Any) -> int:  # noqa: N802
        out.contents.value = 1
        return 0

    def cuDeviceGet(self, out: Any, ordinal: int) -> int:  # noqa: N802
        out.contents.value = ordinal
        return 0

    def cuDeviceGetName(self, buf: Any, size: int, device: Any) -> int:  # noqa: N802
        buf.value = b"Fake RTX"
        return 0

    def cuDeviceTotalMem_v2(self, out: Any, device: Any) -> int:  # noqa: N802
        out.contents.value = self.memory
        return 0


@pytest.fixture(autouse=True)
def _test_models(monkeypatch: pytest.MonkeyPatch) -> None:
    big = voice_models.MODELS["large-v3-turbo-q8_0"]
    small = voice_models.MODELS["small-q8_0"]
    models = {
        "large-v3-turbo-q8_0": voice_models.Model(
            big.model_id, big.weights, big.languages, 3 * GIB
        ),
        "small-q8_0": voice_models.Model(small.model_id, small.weights, small.languages, GIB),
    }
    monkeypatch.setattr(voice_models, "MODELS", models)
    monkeypatch.setattr(
        voice_models,
        "RANKING",
        {
            "cpu": ("small-q8_0",),
            "cuda": ("large-v3-turbo-q8_0", "small-q8_0"),
            "metal": ("large-v3-turbo-q8_0", "small-q8_0"),
        },
    )
    monkeypatch.setattr(voice_models, "CPU_MIN_CPUS", 8)
    monkeypatch.setattr(voice_models, "CPU_MIN_MEMORY", 16 * GIB)


def _windows(*, gpu: vm.Gpu | None, runtime: bool = True, cpus: int = 16) -> vm.Machine:
    return vm.Machine("win32", "AMD64", cpus, 32 * GIB, gpu, runtime)


def _rtx(version: int = 12080, memory: int = 8 * GIB) -> vm.Gpu:
    return vm.Gpu("Fake RTX", memory, version)


def test_rtx_gets_cuda_with_best_fitting_model() -> None:
    assert vm.recommend(_windows(gpu=_rtx())) == ("cuda", "large-v3-turbo-q8_0")


def test_old_driver_gets_cpu() -> None:
    assert vm.recommend(_windows(gpu=_rtx(version=12020))) == ("cpu", "small-q8_0")


def test_driver_at_the_floor_gets_cuda() -> None:
    assert vm.recommend(_windows(gpu=_rtx(version=12040)))[0] == "cuda"


def test_missing_vc_runtime_gets_cpu() -> None:
    assert vm.recommend(_windows(gpu=_rtx(), runtime=False))[0] == "cpu"


def test_small_gpu_gets_the_model_that_fits() -> None:
    assert vm.recommend(_windows(gpu=_rtx(memory=2 * GIB))) == ("cuda", "small-q8_0")


def test_gpu_too_small_for_any_model_gets_cpu() -> None:
    assert vm.recommend(_windows(gpu=_rtx(memory=GIB)))[0] == "cpu"


def test_gpu_that_fits_only_q5_gets_cpu(monkeypatch: pytest.MonkeyPatch) -> None:
    # q5 is no longer ranked (its latency on the shipped path is unmeasured), so a GPU on which
    # only q5 would fit gets the cpu, not q5
    q5 = SHIPPED_MODELS["large-v3-turbo-q5_0"]
    fits = voice_models.Model(q5.model_id, q5.weights, q5.languages, GIB)
    monkeypatch.setitem(voice_models.MODELS, q5.model_id, fits)
    monkeypatch.setattr(voice_models, "RANKING", SHIPPED_RANKING)
    assert vm.recommend(_windows(gpu=_rtx(memory=2 * GIB))) == ("cpu", "small-q8_0")


def test_weak_cpu_machine_gets_small_model() -> None:
    machine = vm.Machine("win32", "AMD64", 4, 8 * GIB, None, True)
    assert vm.recommend(machine) == ("cpu", "small-q8_0")


def test_apple_silicon_gets_metal() -> None:
    machine = vm.Machine("darwin", "arm64", 8, 16 * GIB, None, False)
    assert vm.recommend(machine) == ("metal", "large-v3-turbo-q8_0")


def test_linux_with_a_gpu_gets_cpu() -> None:
    machine = vm.Machine("linux", "x86_64", 16, 32 * GIB, _rtx(), False)
    assert vm.recommend(machine)[0] == "cpu"


def test_chosen_cpu_gets_the_cpu_rule_model() -> None:
    assert vm.recommend(_windows(gpu=_rtx()), "cpu") == ("cpu", "small-q8_0")


def test_chosen_cpu_on_a_strong_machine_gets_the_ranked_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(voice_models, "RANKING", {**voice_models.RANKING, "cpu": ("other",)})
    assert vm.recommend(_windows(gpu=None), "cpu") == ("cpu", "other")
    assert vm.recommend(vm.Machine("win32", "AMD64", 4, 8 * GIB, None, True), "cpu")[1] == (
        "small-q8_0"
    )


def test_chosen_cuda_gets_the_model_that_fits_the_gpu() -> None:
    assert vm.recommend(_windows(gpu=_rtx()), "cuda") == ("cuda", "large-v3-turbo-q8_0")
    assert vm.recommend(_windows(gpu=_rtx(memory=2 * GIB)), "cuda") == ("cuda", "small-q8_0")


def test_chosen_cuda_with_no_gpu_or_no_fit_gets_the_cpu_rule_model() -> None:
    assert vm.recommend(_windows(gpu=None), "cuda") == ("cuda", "small-q8_0")
    assert vm.recommend(_windows(gpu=_rtx(memory=GIB)), "cuda") == ("cuda", "small-q8_0")


def test_chosen_metal_fits_half_the_memory() -> None:
    big = vm.Machine("darwin", "arm64", 8, 16 * GIB, None, False)
    assert vm.recommend(big, "metal") == ("metal", "large-v3-turbo-q8_0")
    small = vm.Machine("darwin", "arm64", 8, 4 * GIB, None, False)
    assert vm.recommend(small, "metal") == ("metal", "small-q8_0")
    tiny = vm.Machine("darwin", "arm64", 8, 2 * GIB, None, False)
    assert vm.recommend(tiny, "metal") == ("metal", "small-q8_0")


def test_probe_reads_the_fake_driver(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vm, "_vc_runtime", lambda: True)
    monkeypatch.setattr(vm, "_total_memory", lambda: 32 * GIB)
    machine = vm.probe(platform="win32", arch="AMD64", nvcuda=FakeNvcuda(version=12080))
    assert machine.nvidia == vm.Gpu("Fake RTX", 8 * GIB, 12080)
    assert machine.vc_runtime
    assert machine.memory == 32 * GIB


def test_driver_error_gives_no_gpu(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vm, "_vc_runtime", lambda: True)
    monkeypatch.setattr(vm, "_total_memory", lambda: 32 * GIB)
    assert vm.probe(platform="win32", nvcuda=FakeNvcuda(error=100)).nvidia is None


def test_probe_off_windows_has_no_gpu_and_no_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vm, "_total_memory", lambda: 16 * GIB)
    machine = vm.probe(platform="linux", arch="x86_64", nvcuda=FakeNvcuda())
    assert machine.nvidia is None
    assert not machine.vc_runtime


@pytest.mark.skipif(sys.platform != "win32", reason="GlobalMemoryStatusEx")
def test_real_memory_is_positive() -> None:
    assert vm._total_memory() > 0
