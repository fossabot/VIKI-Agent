"""Pinned voice models, backends and languages, and the checks the worker and the setup share.

Stdlib only: the worker imports this module under ``python -I -B``.
"""

from __future__ import annotations

import hashlib
import stat
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path

ENGINE = "pywhispercpp"
ENGINE_VERSION = "1.5.1"

_GIB = 1024**3
_WEIGHTS_BASE = (
    "https://huggingface.co/ggerganov/whisper.cpp/resolve/5359861c739e955e79d9a303bcbc70fb988958b1/"
)
_VAD_BASE = (
    "https://huggingface.co/ggml-org/whisper-vad/resolve/9ffd54a1e1ee413ddf265af9913beaf518d1639b/"
)
_CUDA_ZIP = "whisper-cublas-12.4.0-bin-x64.zip"
# The name the wheel's extension imports; the zip's Release/whisper.dll is stored under it.
_BINDING = "whisper-fa12b85c909a26b32bc9c8ea9b0dcc91.dll"


@dataclass(frozen=True)
class PinnedFile:
    name: str
    size: int
    sha256: str
    url: str = ""
    member: str = ""


@dataclass(frozen=True)
class Model:
    model_id: str
    weights: PinnedFile
    languages: tuple[str, ...]
    peak_vram: int
    # Partials may decode with a context sized to the audio; finals always use full context.
    proportional_partials: bool = False


@dataclass(frozen=True)
class Backend:
    backend_id: str
    platforms: tuple[str, ...]
    machines: tuple[str, ...]
    archive: PinnedFile | None
    files: tuple[PinnedFile, ...]
    binding: str


def _weights(name: str, size: int, sha256: str) -> PinnedFile:
    return PinnedFile(name, size, sha256, _WEIGHTS_BASE + name)


def _member(name: str, size: int, sha256: str, stored: str = "") -> PinnedFile:
    return PinnedFile(stored or name, size, sha256, member=f"Release/{name}")


VAD = PinnedFile(
    "ggml-silero-v6.2.0.bin",
    885_098,
    "2aa269b785eeb53a82983a20501ddf7c1d9c48e33ab63a41391ac6c9f7fb6987",
    _VAD_BASE + "ggml-silero-v6.2.0.bin",
)

_TURBO_LANGUAGES = (
    "en",
    "zh",
    "es",
    "fr",
    "pt",
    "id",
    "ru",
    "de",
    "ja",
    "vi",
    "tr",
    "tl",
    "ko",
    "it",
)

# peak_vram: the GPU's memory.used peak over a run on the measuring laptop;
# per-process VRAM is not reported under WDDM
MODELS: dict[str, Model] = {
    "small-q8_0": Model(
        "small-q8_0",
        _weights(
            "ggml-small-q8_0.bin",
            264_464_607,
            "49c8fb02b65e6049d5fa6c04f81f53b867b5ec9540406812c643f177317f779f",
        ),
        ("en", "es", "fr", "pt", "ru", "de", "it"),
        815_792_128,
        proportional_partials=True,
    ),
    "large-v3-turbo-q5_0": Model(
        "large-v3-turbo-q5_0",
        _weights(
            "ggml-large-v3-turbo-q5_0.bin",
            574_041_195,
            "394221709cd5ad1f40c46e6031ca61bce88931e6e088c188294c6d5a55ffa7e2",
        ),
        _TURBO_LANGUAGES,
        1_222_639_616,
    ),
    "large-v3-turbo-q8_0": Model(
        "large-v3-turbo-q8_0",
        _weights(
            "ggml-large-v3-turbo-q8_0.bin",
            874_188_075,
            "317eb69c11673c9de1e1f0d459b253999804ec71ac4c23c17ecf5fbe24e259a1",
        ),
        _TURBO_LANGUAGES,
        1_552_941_056,
    ),
}

BACKENDS: dict[str, Backend] = {
    "cpu": Backend("cpu", ("win32", "darwin", "linux"), (), None, (), ""),
    "metal": Backend("metal", ("darwin",), ("arm64",), None, (), ""),
    "cuda": Backend(
        "cuda",
        ("win32",),
        ("AMD64",),
        PinnedFile(
            _CUDA_ZIP,
            459_827_307,
            "ff50101f85a6026d39053771c25b42f5752ac05d5be9ee2e5d2632541adef231",
            f"https://github.com/ggml-org/whisper.cpp/releases/download/v1.8.5/{_CUDA_ZIP}",
        ),
        (
            _member(
                "whisper.dll",
                1_308_160,
                "354738b34b67873e38d590f4f9c15e114304dc59ff004ea1108951282295d062",
                _BINDING,
            ),
            _member(
                "ggml.dll",
                67_072,
                "893fb8ab38da68e5ea805997cf221f5b25b19b1f018dfb339cee270971bf8dc3",
            ),
            _member(
                "ggml-base.dll",
                638_464,
                "a5d4b0a3a62f2de2c6d44a6a1b61deae66b7f71ad91991c8802b85f789a08df9",
            ),
            _member(
                "ggml-cpu.dll",
                784_896,
                "573630eb6cc2c0fcbc6d4858231401d85ceda8adacbe3fada5ee7c94807afd3c",
            ),
            _member(
                "ggml-cuda.dll",
                103_261_184,
                "3929d860a75f4cd4b46ffaebfb13d29e5f0b2e2ef668953c60b7153e2f95d716",
            ),
            _member(
                "cudart64_12.dll",
                553_984,
                "d28e42265da7462162a54da6b7a99ea4fa2caf8139d862bb500db875d0b32dfc",
            ),
            _member(
                "cublas64_12.dll",
                100_033_536,
                "e40202fe4223c1cd2d2dce7beec59e1ed61c7801bd827309183be9b50e358f4c",
            ),
            _member(
                "cublasLt64_12.dll",
                473_551_360,
                "2a896460bef60ed57ef32b0875812f355a6984e671d638bb632f5e8c1d7a831f",
            ),
        ),
        _BINDING,
    ),
}

RANKING: dict[str, tuple[str, ...]] = {
    "cpu": ("small-q8_0",),
    # q8 passes ja and en in 14 languages with the lower mean error. q5 is not ranked:
    # its latency on the shipped path is unmeasured, since partials run without the VAD. A pinned
    # `--model large-v3-turbo-q5_0` still runs it.
    "cuda": ("large-v3-turbo-q8_0",),
    # Metal is unmeasured: cuda's ranking
    "metal": ("large-v3-turbo-q8_0",),
}

# the measuring machine's values
CPU_MIN_CPUS = 32
CPU_MIN_MEMORY = 68_330_168_320
READY_TIMEOUT_S: dict[str, int] = {
    "cpu": 30,
    # the warm-up before ready includes the driver's first kernel compile, 28-48 s cold
    "cuda": 80,
    "metal": 80,  # unmeasured: cuda's value
}
WORKER_MEMORY_BYTES: dict[str, int] = {
    "cpu": 4_026_531_840,
    # 2 x the largest Job peak commit measured on one Windows machine, rounded up to 256 MiB
    "cuda": 7_247_757_312,
    "metal": 7_247_757_312,  # unmeasured: cuda's value; POSIX validates it only
}

# The candidate languages in a fixed order: the allowlists' union is listed in this order.
_LANGUAGE_ORDER = (
    *("en", "zh", "hi", "es", "ar", "fr", "bn", "pt", "id", "ur", "ru", "de", "ja", "mr", "vi"),
    *("te", "sw", "ha", "tr", "tl", "ta", "yue", "fa", "ko", "am", "th", "jw", "it", "gu", "kn"),
    "yo",
)
DICTATION_LANGUAGES = (
    "auto",
    *(
        code
        for code in _LANGUAGE_ORDER
        if any(code in model.languages for model in MODELS.values())
    ),
)

_OFFER_ORDER = ("cuda", "metal", "cpu")
_CHUNK = 1024 * 1024


def model_dir(data_dir: Path, model_id: str) -> Path:
    return data_dir / "voice" / "models" / model_id


def backend_dir(data_dir: Path, backend_id: str) -> Path:
    return data_dir / "voice" / "backends" / backend_id


def model_files(model_id: str) -> tuple[PinnedFile, ...]:
    return (MODELS[model_id].weights, VAD)


def offered(platform: str, machine: str) -> tuple[str, ...]:
    return tuple(
        backend_id
        for backend_id in _OFFER_ORDER
        if platform in BACKENDS[backend_id].platforms
        and (not BACKENDS[backend_id].machines or machine in BACKENDS[backend_id].machines)
    )


def _problem(path: Path, pin: PinnedFile) -> str | None:
    """Return why `path` is not the pinned regular file, from lstat alone."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "not_regular"
    reparse = getattr(info, "st_file_attributes", 0) & getattr(
        stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0
    )
    if not stat.S_ISREG(info.st_mode) or reparse:
        return "not_regular"
    return "size_mismatch" if info.st_size != pin.size else None


def present(directory: Path, files: tuple[PinnedFile, ...]) -> bool:
    return all(_problem(directory / pin.name, pin) is None for pin in files)


def verify(directory: Path, files: tuple[PinnedFile, ...]) -> str | None:
    for pin in files:
        path = directory / pin.name
        problem = _problem(path, pin)
        if problem is None:
            try:
                problem = None if _sha256(path) == pin.sha256 else "hash_mismatch"
            except FileNotFoundError:
                problem = "missing"
            except OSError:
                problem = "not_regular"
        if problem is not None:
            return problem
    return None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def auto_backend(
    platform: str,
    machine: str,
    started: Collection[str],
    models: Collection[str],
    want_model: str | None = None,
) -> str:
    """Pick the first offered backend that was set up and can run a model, else "cpu".

    `started` holds the backends that were set up: those that need no files, or whose folder
    exists. A broken install counts, so its start check reports the fault and the fallback to
    the CPU is visible, not silent. With `want_model` (a pinned model) the first such backend
    wins. Without it, the backend must also rank an installed model of `models`.
    """
    return next(
        (
            b
            for b in offered(platform, machine)
            if b in started and (want_model or auto_model(b, models) is not None)
        ),
        "cpu",
    )


def auto_model(backend_id: str, installed: Collection[str]) -> str | None:
    return next((m for m in RANKING[backend_id] if m in installed), None)
