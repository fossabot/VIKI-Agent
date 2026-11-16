"""The voice worker: one isolated process that turns PCM frames into transcript lines.

``VoiceEngine`` starts ``[python, "-I", "-B", voice_worker.py, <model_id>, <backend_id>]`` with
cwd ``<data>/voice``; compiled, that is instead the installed binary's own
``<app> --viki-worker voice <model_id> <backend_id>``. The worker keeps the original stdout for
protocol lines only and points
fd 1 at stderr, so native engine output never reaches the channel. An audit hook then refuses,
on the Python side and before the engine is imported: sockets; process creation; file creation,
writes, truncation, renames, links, deletions, folder creation and attribute changes; SQLite
connections; and registry writes. Native code, and anything that reaches the OS without an audit
event, is not covered. The engine (pywhispercpp and numpy) is imported only inside
``_load_recognizer``; every other import is stdlib or a stdlib-only module of this package.
"""

from __future__ import annotations

import contextlib
import importlib.metadata
import importlib.util
import json
import math
import os
import platform
import sys
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, BinaryIO, Protocol

from viki_agent.voice import voice_gpu, voice_models
from viki_agent.voice.voice_stream import (
    SAMPLE_RATE,
    CaptureCapError,
    FrameError,
    Recognizer,
    Session,
    parse_begin,
    read_frame,
)

# Besides every "socket.*" event and the write-open check in `_audit`, exactly these events are
# refused: network requests, process creation, deletions, renames, links, folder creation,
# truncation, attribute changes, SQLite connections and registry writes.
_REFUSED_EVENTS = frozenset(
    {
        "urllib.Request",
        "subprocess.Popen",
        "os.system",
        "os.exec",
        "os.posix_spawn",
        "os.spawn",
        "os.startfile",
        "os.fork",
        "os.forkpty",
        "pty.spawn",
        "_winapi.CreateProcess",  # multiprocessing on Windows starts children without Popen
        "_winapi.CreateFile",
        "_winapi.CreateJunction",
        "os.remove",
        "os.rename",
        "os.rmdir",
        "os.mkdir",
        "os.truncate",
        "os.link",
        "os.symlink",
        "os.chmod",
        "os.chown",
        "os.chflags",
        "os.setxattr",
        "os.removexattr",
        "os.utime",
        "shutil.rmtree",
        "shutil.move",
        "sqlite3.connect",
        "winreg.CreateKey",
        "winreg.SetValue",
        "winreg.DeleteKey",
        "winreg.DeleteValue",
        "winreg.SaveKey",
        "winreg.LoadKey",
    }
)
_WRITE_MODES = frozenset("wax+")
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC
# whisper.cpp returns nothing for input under 1.0 s, so shorter input is padded to 1.1 s.
_MIN_DECODE_SAMPLES = SAMPLE_RATE
_PADDED_SAMPLES = SAMPLE_RATE * 11 // 10
_MAX_THREADS = 8
_FULL_AUDIO_CTX = 1500


class Gpu(Protocol):
    """The part of `voice_gpu` the cuda start checks call; tests pass a fake."""

    def preload(self, path: Path) -> None: ...
    def devices(self, ggml: Path, ggml_base: Path) -> list[voice_gpu.Device]: ...
    def first_gpu(
        self, devices: Iterable[voice_gpu.Device], prefix: str
    ) -> voice_gpu.Device | None: ...
    def module_file(self, name: str) -> str | None: ...
    def is_loaded(self, path: Path) -> bool: ...


# load(weights, vad, threads, use_gpu, proportional_partials, check_binding) -> recognizer
Loader = Callable[[Path, Path, int, bool, bool, Callable[[], None]], Recognizer]


class _Failure(Exception):
    """Ends the worker with one error line and its exit code."""

    def __init__(self, code: str, exit_code: int, detail: str | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.exit_code = exit_code
        self.detail = detail


def _opens_for_writing(args: tuple[Any, ...]) -> bool:
    # builtin open passes (path, mode, flags); os.open passes (path, None, flags).
    mode = args[1] if len(args) > 1 else None
    flags = args[2] if len(args) > 2 else None
    return (isinstance(mode, str) and not _WRITE_MODES.isdisjoint(mode)) or (
        isinstance(flags, int) and flags & _WRITE_FLAGS != 0
    )


def _audit(event: str, args: tuple[Any, ...]) -> None:
    if (
        event.startswith("socket.")
        or event in _REFUSED_EVENTS
        or (event == "open" and _opens_for_writing(args))
    ):
        raise PermissionError(event)


def _install_guard() -> None:
    sys.addaudithook(_audit)


def _open_channel() -> BinaryIO:
    """Keep the real stdout for protocol lines; fd 1, and with it native output and any stray
    print, goes to stderr."""
    fd = os.dup(1)
    os.dup2(2, 1)
    protocol = os.fdopen(fd, "wb", buffering=0)
    sys.stdout = sys.stderr
    return protocol


def _sender(protocol: BinaryIO) -> Callable[[dict[str, object]], None]:
    def send(obj: dict[str, object]) -> None:
        line = json.dumps(obj, ensure_ascii=True, separators=(",", ":"))
        protocol.write(line.encode("ascii") + b"\n")

    return send


def _load_recognizer(
    weights: Path,
    vad: Path,
    threads: int,
    use_gpu: bool,
    proportional_partials: bool,
    check_binding: Callable[[], None],
) -> Recognizer:
    import numpy  # noqa: TID251
    from pywhispercpp.model import Model  # noqa: TID251

    check_binding()
    # False keeps native output on fd 2; None would open a log file, which the guard refuses.
    model = Model(
        str(weights),
        redirect_whispercpp_logs_to=False,
        context_params={"use_gpu": use_gpu},
        n_threads=threads,
        print_progress=False,
        print_realtime=False,
        print_timestamps=False,
        no_context=True,
        suppress_nst=True,
        no_timestamps=True,
        vad=True,
        vad_model_path=str(vad),
    )

    class Engine:
        def transcribe(self, pcm: bytes, language: str, *, partial: bool) -> str:
            # An array, never a path: pywhispercpp runs ffmpeg for a path.
            samples = numpy.frombuffer(pcm, numpy.int16).astype(numpy.float32)
            samples /= 32768
            if samples.size < _MIN_DECODE_SAMPLES:
                samples = numpy.pad(samples, (0, _PADDED_SAMPLES - samples.size))
            # Seconds × rate as one division of the sample count: 1.1 s × 50 would round up.
            frames_50hz = math.ceil(samples.size * 50 / SAMPLE_RATE)
            tokens = math.ceil(10 + samples.size * 12 / SAMPLE_RATE)
            # Decode parameters persist in pywhispercpp, so every call sets them.
            proportional = partial and proportional_partials
            segments = model.transcribe(
                samples,
                language=language,
                audio_ctx=min(_FULL_AUDIO_CTX, frames_50hz + 64) if proportional else 0,
                max_tokens=tokens,
                # The VAD empties the first short partial windows; finals need it.
                vad=not partial,
            )
            return "".join(segment.text for segment in segments).strip()

    return Engine()


class _Decoder:
    """Tells a recognizer failure (decode_failed) apart from any other error (internal)."""

    def __init__(self, recognizer: Recognizer) -> None:
        self._recognizer = recognizer

    def transcribe(self, pcm: bytes, language: str, *, partial: bool) -> str:
        try:
            return self._recognizer.transcribe(pcm, language, partial=partial)
        except Exception:
            raise _Failure("decode_failed", 1) from None


def _admit(argv: list[str]) -> tuple[voice_models.Model, voice_models.Backend, Path, Path]:
    offered = voice_models.offered(sys.platform, platform.machine())
    if len(argv) != 2 or argv[0] not in voice_models.MODELS or argv[1] not in offered:
        raise _Failure("invalid_input", 2)
    model, backend = voice_models.MODELS[argv[0]], voice_models.BACKENDS[argv[1]]
    voice = Path.cwd()
    model_dir = voice / "models" / model.model_id
    backend_dir = voice / "backends" / backend.backend_id
    if not (
        voice_models.present(model_dir, voice_models.model_files(model.model_id))
        and voice_models.present(backend_dir, backend.files)
    ):
        raise _Failure("invalid_input", 2)
    return model, backend, model_dir, backend_dir


def _first_cuda(gpu: Gpu, backend_dir: Path) -> voice_gpu.Device | None:
    return gpu.first_gpu(
        gpu.devices(backend_dir / "ggml.dll", backend_dir / "ggml-base.dll"), "CUDA"
    )


def _cuda_device(gpu: Gpu, binding: Path) -> voice_gpu.Device:
    try:
        gpu.preload(binding)
        device = _first_cuda(gpu, binding.parent)
    except OSError:
        raise _Failure("backend_unavailable", 5, "load_failed") from None
    if device is None:
        raise _Failure("backend_unavailable", 5, "no_device")
    return device


def _check_binding(gpu: Gpu, binding: Path) -> None:
    """The engine must bind the backend's copy of the binding DLL, never the wheel's."""
    spec = importlib.util.find_spec("_pywhispercpp")
    loaded = gpu.module_file(binding.name)
    if (
        spec is None
        or spec.origin is None
        or loaded is None
        or os.path.normcase(loaded) != os.path.normcase(binding)
        or gpu.is_loaded(Path(spec.origin).parent / binding.name)
    ):
        raise _Failure("backend_unavailable", 5, "binding_mismatch")


def _serve(
    argv: list[str],
    stdin: BinaryIO,
    send: Callable[[dict[str, object]], None],
    load: Loader,
    gpu: Gpu,
) -> None:
    model, backend, model_dir, backend_dir = _admit(argv)
    cuda = backend.backend_id == "cuda"
    binding = backend_dir / backend.binding
    device = _cuda_device(gpu, binding) if cuda else None
    try:
        recognizer = load(
            model_dir / model.weights.name,
            model_dir / voice_models.VAD.name,
            min(_MAX_THREADS, os.cpu_count() or 1),
            backend.backend_id != "cpu",
            model.proportional_partials,
            (lambda: _check_binding(gpu, binding)) if cuda else (lambda: None),
        )
        engine_version = importlib.metadata.version(voice_models.ENGINE)
    except ImportError:
        raise _Failure("engine_unavailable", 3) from None
    except _Failure:
        raise
    except Exception:
        raise _Failure("model_unavailable", 4) from None
    if device is not None:
        loaded = _first_cuda(gpu, backend_dir)
        if loaded is None or device.free - loaded.free < model.weights.size // 2:
            raise _Failure("backend_unavailable", 5, "not_on_device")
    if backend.backend_id != "cpu":
        # The driver compiles its kernels at the first decode: the start's ready timeout must
        # cover that, not the first capture. A partial skips the VAD; the text is discarded.
        try:
            recognizer.transcribe(bytes(_PADDED_SAMPLES * 2), "en", partial=True)
        except Exception:
            raise _Failure("model_unavailable", 4) from None
    send(
        {
            "type": "ready",
            "engine": voice_models.ENGINE,
            "engine_version": engine_version,
            "model": model.model_id,
            "backend": backend.backend_id,
            "device": device.description if device else None,
        }
    )
    languages = ("auto", *model.languages)
    session = Session(_Decoder(recognizer), send, languages, clock=time.monotonic)
    try:
        while (frame := read_frame(stdin)) is not None:
            kind, payload = frame
            if kind == b"B":
                session.begin(*parse_begin(payload, languages))
            elif kind == b"A":
                session.audio(payload)
            elif kind == b"E":
                session.end()
            else:
                session.cancel()
    except FrameError, CaptureCapError:
        raise _Failure("invalid_input", 2) from None


def main(
    argv: list[str] | None = None,
    *,
    stdin: BinaryIO | None = None,
    protocol: BinaryIO | None = None,
    load: Loader = _load_recognizer,
    gpu: Gpu = voice_gpu,
) -> int:
    if protocol is None:
        protocol = _open_channel()
        # After the channel (os.fdopen raises a write-mode open event) and before the engine
        # import (urllib3's import-time IPv6 probe meets the refusal and catches it).
        # A caller passing its own channel is a test, whose process stays unguarded.
        _install_guard()
    send = _sender(protocol)
    try:
        _serve(
            sys.argv[1:] if argv is None else argv,
            sys.stdin.buffer if stdin is None else stdin,
            send,
            load,
            gpu,
        )
        return 0
    except _Failure as failure:
        error: dict[str, object] = {"type": "error", "code": failure.code}
        if failure.detail is not None:
            error["detail"] = failure.detail
        exit_code = failure.exit_code
    except Exception:
        error = {"type": "error", "code": "internal"}
        exit_code = 1
    with contextlib.suppress(OSError):  # the parent may already be gone
        send(error)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
