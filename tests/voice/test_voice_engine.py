"""VoiceEngine against the real `winproc.spawn` of tests/fake_voice_worker.py.

The test pins name the fake's modes as model ids, so the model id picks the fake's behaviour.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from viki_agent import compiled, winproc
from viki_agent.media import media_worker
from viki_agent.voice import voice_engine
from viki_agent.voice import voice_models as vm
from viki_agent.voice.voice_engine import Fallback, VoiceEngine, VoiceError, VoiceResult
from viki_agent.voice.voice_stream import END_SILENCE_FRAMES

HANG_GUARD_S = 10
FAKE = Path(__file__).parent.parent / "fake_voice_worker.py"
CAPTURE = "6f1c2a1e-3b4d-4e5f-8a9b-0c1d2e3f4a5b"
OTHER = "00000000-0000-4000-8000-000000000000"
MODES = (
    "fake-ok",
    "fake-engine-missing",
    "fake-model-error",
    "fake-hang-ready",
    "fake-wrong-version",
    "fake-garbage",
    "fake-long-line",
    "fake-wrong-capture",
    "fake-crash",
    "fake-no-done",
    "fake-no-read",
    "fake-stderr",
    "fake-gpu-fails",
    "fake-gpu-crash",
)
WEIGHTS = b"fake weights"
VAD = b"fake vad"
LIB = b"fake cuda library"
CHUNK = b"\x01\x00" * 8_000  # 16,000 bytes: one partial from the fake
BIG = b"\x00" * 65_536  # the largest audio frame


def _pin(name: str, data: bytes) -> vm.PinnedFile:
    return vm.PinnedFile(name, len(data), hashlib.sha256(data).hexdigest())


@pytest.fixture(autouse=True)
def pins(monkeypatch: pytest.MonkeyPatch) -> None:
    weights = _pin("weights.bin", WEIGHTS)
    monkeypatch.setattr(vm, "VAD", _pin("vad.bin", VAD))
    monkeypatch.setattr(
        vm, "MODELS", {mode: vm.Model(mode, weights, ("en", "ja"), 0) for mode in MODES}
    )
    monkeypatch.setattr(
        vm,
        "BACKENDS",
        {
            "cpu": vm.Backend("cpu", ("win32", "darwin", "linux"), (), None, (), ""),
            "cuda": vm.Backend(
                "cuda", ("win32",), ("AMD64",), None, (_pin("lib.bin", LIB),), "lib.bin"
            ),
            "metal": vm.BACKENDS["metal"],
        },
    )
    monkeypatch.setattr(voice_engine, "_engine_installed", lambda: True)


def install_model(data: Path, mode: str, weights: bytes = WEIGHTS) -> None:
    folder = vm.model_dir(data, mode)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "weights.bin").write_bytes(weights)
    (folder / "vad.bin").write_bytes(VAD)


def install_cuda(data: Path, lib: bytes = LIB) -> None:
    folder = vm.backend_dir(data, "cuda")
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "lib.bin").write_bytes(lib)


@dataclass
class Call:
    argv: list[str]
    cwd: Any
    env: dict[str, str]
    memory_limit_bytes: int | None


class RecordedChild(winproc.Child):
    """A Child that records how many processes its Job still held when it was released."""

    active_at_release: int | None = None

    def release(self) -> None:
        if self.active_at_release is None:
            self.active_at_release = self.job.active_processes()
        super().release()


class Recorder:
    """winproc.spawn, recording each call; `reported` rewrites what the fake will report."""

    def __init__(self, reported: dict[str, str] | None = None) -> None:
        self.calls: list[Call] = []
        self.children: list[RecordedChild] = []
        self.reaped_before: list[bool] = []  # per spawn: every earlier child was already reaped
        self.spawned = asyncio.Event()
        self._reported = reported or {}

    async def __call__(
        self, argv: list[str], *, cwd: Any, env: dict[str, str], memory_limit_bytes: int | None
    ) -> winproc.Child:
        self.calls.append(Call(list(argv), cwd, dict(env), memory_limit_bytes))
        self.reaped_before.append(self.all_reaped())
        argv = [self._reported.get(part, part) for part in argv]
        spawned = await winproc.spawn(argv, cwd=cwd, env=env, memory_limit_bytes=memory_limit_bytes)
        child = RecordedChild(spawned.process, spawned.job)
        self.adapt(child)
        self.children.append(child)
        self.spawned.set()
        return child

    def adapt(self, child: RecordedChild) -> None:
        """A hook for a test to swap the child's pipes."""

    def backends(self) -> list[str]:
        return [call.argv[-1] for call in self.calls]

    def all_reaped(self) -> bool:
        """Every Job was released, and only once its whole tree had ended."""
        return all(child.active_at_release == 0 for child in self.children)


class HeldStderr(Recorder):
    """The first child's stderr reaches the engine only up to EOF; the EOF waits for `release`."""

    def __init__(self) -> None:
        super().__init__()
        self.release = asyncio.Event()
        self.real_eof = asyncio.Event()
        self.pump: asyncio.Task[None] | None = None

    def adapt(self, child: RecordedChild) -> None:
        if self.pump is not None:
            return
        real, held = child.process.stderr, asyncio.StreamReader()
        assert real is not None
        child.process.stderr = held
        self.pump = asyncio.create_task(self._pump(real, held))

    async def _pump(self, real: asyncio.StreamReader, held: asyncio.StreamReader) -> None:
        while data := await real.read(65_536):
            held.feed_data(data)
        self.real_eof.set()
        await self.release.wait()
        held.feed_eof()


class BrokenStdout(asyncio.StreamReader):
    """A stdout whose every readline after the ready line raises OSError, like a broken pipe."""

    def __init__(self, real: asyncio.StreamReader) -> None:
        super().__init__()
        self._real = real
        self._lines = 0

    async def readline(self) -> bytes:
        self._lines += 1
        if self._lines > 1:
            raise OSError("broken pipe")
        return await self._real.readline()

    async def read(self, n: int = -1) -> bytes:
        return await self._real.read(n)


class BreaksStdout(Recorder):
    def adapt(self, child: RecordedChild) -> None:
        assert child.process.stdout is not None
        child.process.stdout = BrokenStdout(child.process.stdout)


def make(
    data: Path, mode: str, backend: str = "cpu", recorder: Recorder | None = None, **kwargs: Any
) -> tuple[VoiceEngine, Recorder]:
    recorder = recorder or Recorder()
    engine = VoiceEngine(
        data,
        model_id=mode,
        backend=backend,
        worker=FAKE,
        python=sys.executable,
        spawn=recorder,
        **kwargs,
    )
    return engine, recorder


async def next_result(engine: VoiceEngine) -> VoiceResult:
    return await asyncio.wait_for(engine.results.get(), HANG_GUARD_S)


def gone(pid: int) -> bool:
    return winproc.process_start_time(pid) is None


async def test_happy_path_spawns_the_pinned_argv(tmp_path: Path) -> None:
    install_model(tmp_path, "fake-ok")
    engine, recorder = make(tmp_path, "fake-ok")
    try:
        await engine.start()
        [call] = recorder.calls
        assert call.argv == [sys.executable, "-I", "-B", str(FAKE), "fake-ok", "cpu"]
        assert call.cwd == tmp_path / "voice"
        assert call.env == media_worker._worker_env(sys.platform, os.environ)
        assert call.memory_limit_bytes == vm.WORKER_MEMORY_BYTES["cpu"]
        assert engine.running and engine.pid == recorder.children[0].pid
        assert (engine.model_id, engine.backend, engine.device) == ("fake-ok", "cpu", None)
        assert engine.fallback is None

        await engine.begin(CAPTURE, "ja")
        await engine.audio(CAPTURE, CHUNK)
        await engine.audio(CAPTURE, CHUNK)
        await engine.end(CAPTURE)
        assert [await next_result(engine) for _ in range(4)] == [
            VoiceResult("partial", CAPTURE, 0, 1, "partial-1"),
            VoiceResult("partial", CAPTURE, 0, 2, "partial-2"),
            VoiceResult("final", CAPTURE, 0, None, "final-32000"),
            VoiceResult("done", CAPTURE),
        ]
        await engine.begin(OTHER, "auto")  # done freed the capture
        await engine.cancel(OTHER)
        assert await next_result(engine) == VoiceResult("cancelled", OTHER)
        await engine.begin(CAPTURE, "en")
    finally:
        await engine.close()


async def test_compiled_launches_the_installed_binarys_viki_worker_subcommand(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_model(tmp_path, "fake-ok")
    install_dir = tmp_path / "install"
    monkeypatch.setattr(compiled, "COMPILED", True)
    monkeypatch.setattr(compiled, "INSTALL_DIR", install_dir)
    engine, recorder = make(tmp_path, "fake-ok")
    with pytest.raises(VoiceError) as caught:
        await engine.start()
    assert caught.value.code == "worker_unavailable"
    [call] = recorder.calls
    assert call.argv == [
        str(install_dir / compiled.APP_BINARY),
        "--viki-worker",
        "voice",
        "fake-ok",
        "cpu",
    ]


async def test_begin_adds_the_default_or_chosen_pause_frames_to_the_begin_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_model(tmp_path, "fake-ok")
    engine, _ = make(tmp_path, "fake-ok")
    sent: list[tuple[bytes, bytes]] = []
    original_write = engine._write

    def spy_write(kind: bytes, payload: bytes) -> None:
        sent.append((kind, payload))
        original_write(kind, payload)

    monkeypatch.setattr(engine, "_write", spy_write)
    try:
        await engine.start()
        await engine.begin(CAPTURE, "en")  # no pause_frames given: the wire default applies
        await engine.cancel(CAPTURE)
        assert await next_result(engine) == VoiceResult("cancelled", CAPTURE)

        await engine.begin(OTHER, "en", pause_frames=150)
        await engine.cancel(OTHER)
        assert await next_result(engine) == VoiceResult("cancelled", OTHER)

        begins = [json.loads(payload) for kind, payload in sent if kind == b"B"]
        assert begins == [
            {"capture": CAPTURE, "language": "en", "pause_frames": END_SILENCE_FRAMES},
            {"capture": OTHER, "language": "en", "pause_frames": 150},
        ]
    finally:
        await engine.close()


async def test_status_and_installed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    engine, _ = make(tmp_path, "fake-ok")
    cuda, _ = make(tmp_path, "fake-ok", "cuda")
    assert engine.installed() == ([], ["cpu", "metal"])  # cpu and metal need no files
    assert engine.status() == "setup_needed"

    install_model(tmp_path, "fake-ok")
    assert engine.installed() == (["fake-ok"], ["cpu", "metal"])
    assert engine.status() == "ready"
    assert cuda.status() == "backend_missing"
    cuda.fallback = Fallback("cuda", "worker_crashed", None)
    assert cuda.status() == "ready"  # the fallback resolves to cpu
    cuda.clear_fallback()
    assert cuda.fallback is None

    install_cuda(tmp_path)
    assert engine.installed() == (["fake-ok"], ["cpu", "cuda", "metal"])
    assert cuda.status() == "ready"

    monkeypatch.setattr(
        vm, "RANKING", {"cpu": ("fake-ok",), "cuda": ("fake-ok",), "metal": ("fake-ok",)}
    )
    auto = VoiceEngine(tmp_path)
    assert auto.status() == "ready"
    monkeypatch.setattr(
        vm, "RANKING", {"cpu": ("fake-crash",), "cuda": ("fake-crash",), "metal": ("fake-crash",)}
    )
    assert auto.status() == "setup_needed"

    monkeypatch.setattr(voice_engine, "_engine_installed", lambda: False)
    assert engine.status() == "engine_unavailable"


async def test_auto_with_a_broken_gpu_install_falls_back_visibly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Windows x64's offer, the only host with cuda
    monkeypatch.setattr(vm, "offered", lambda platform, machine: ("cuda", "cpu"))
    # fake-ok stands for the turbo model of the cuda pair, fake-stderr for the cpu pair's model
    monkeypatch.setattr(vm, "RANKING", {"cpu": ("fake-stderr",), "cuda": ("fake-ok",)})
    install_model(tmp_path, "fake-ok")
    install_model(tmp_path, "fake-stderr")
    install_cuda(tmp_path, b"short")  # the wrong size: present() is false, the folder exists
    engine = VoiceEngine(tmp_path, worker=FAKE, python=sys.executable, spawn=Recorder())
    assert engine.installed() == (["fake-ok", "fake-stderr"], ["cpu", "metal"])
    assert engine.resolve() == ("cuda", "fake-ok")  # the GPU is not dropped silently
    assert engine.status() == "backend_missing"
    try:
        await engine.start()
        assert engine.backend == "cpu" and engine.model_id == "fake-stderr"  # the CPU pair's
        assert engine.fallback == Fallback("cuda", "backend_missing", None)
    finally:
        await engine.close()


def test_auto_on_apple_silicon_with_only_the_cpu_pair_is_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(vm, "RANKING", {"cpu": ("fake-stderr",), "metal": ("fake-ok",)})
    monkeypatch.setattr(voice_engine, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(voice_engine, "platform", SimpleNamespace(machine=lambda: "arm64"))
    install_model(tmp_path, "fake-stderr")  # metal needs no files, but its model is absent
    engine = VoiceEngine(tmp_path)
    assert engine.resolve() == ("cpu", "fake-stderr")
    assert engine.status() == "ready"


@pytest.mark.parametrize("code", ["setup_needed", "model_mismatch", "engine_unavailable"])
async def test_start_checks_refuse_without_a_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: str
) -> None:
    if code == "model_mismatch":
        install_model(tmp_path, "fake-ok", b"X" * len(WEIGHTS))
    elif code == "engine_unavailable":
        install_model(tmp_path, "fake-ok")
    monkeypatch.setattr(voice_engine, "_engine_installed", lambda: code != "engine_unavailable")
    install_cuda(tmp_path)
    for backend in ("cpu", "cuda"):  # the CPU shares these: no fallback either
        engine, recorder = make(tmp_path, "fake-ok", backend)
        with pytest.raises(VoiceError) as caught:
            await engine.start()
        assert caught.value.code == code
        assert recorder.calls == [] and not engine.running and engine.fallback is None


@pytest.mark.parametrize(
    ("code", "lib"), [("backend_missing", None), ("backend_mismatch", b"X" * len(LIB))]
)
async def test_backend_files_fall_back_without_a_cuda_spawn(
    tmp_path: Path, code: str, lib: bytes | None
) -> None:
    install_model(tmp_path, "fake-ok")
    if lib is not None:
        install_cuda(tmp_path, lib)
    engine, recorder = make(tmp_path, "fake-ok", "cuda")
    try:
        await engine.start()
        assert recorder.backends() == ["cpu"]
        assert engine.backend == "cpu" and engine.fallback == Fallback("cuda", code, None)
    finally:
        await engine.close()


@pytest.mark.parametrize(
    ("mode", "code"),
    [("fake-engine-missing", "engine_unavailable"), ("fake-model-error", "model_unavailable")],
)
async def test_worker_exit_codes(tmp_path: Path, mode: str, code: str) -> None:
    install_model(tmp_path, mode)
    engine, recorder = make(tmp_path, mode)
    with pytest.raises(VoiceError) as caught:
        await engine.start()
    assert caught.value.code == code
    assert recorder.backends() == ["cpu"] and engine.fallback is None  # a cpu failure raises
    assert not engine.running and gone(recorder.children[0].pid)


async def test_ready_timeout(tmp_path: Path) -> None:
    install_model(tmp_path, "fake-hang-ready")
    engine, recorder = make(tmp_path, "fake-hang-ready", ready_timeout_s=0.05)
    with pytest.raises(VoiceError) as caught:
        await engine.start()
    assert caught.value.code == "worker_timeout"
    assert not engine.running and engine.pid is None and gone(recorder.children[0].pid)


async def test_close_cancels_a_start(tmp_path: Path) -> None:
    install_model(tmp_path, "fake-hang-ready")
    install_cuda(tmp_path)
    engine, recorder = make(tmp_path, "fake-hang-ready", "cuda")
    starting = asyncio.create_task(engine.start())
    await asyncio.wait_for(recorder.spawned.wait(), HANG_GUARD_S)
    await engine.close()
    with pytest.raises(VoiceError) as caught:
        await starting
    assert caught.value.code == "not_running"
    assert recorder.backends() == ["cuda"] and engine.fallback is None
    assert gone(recorder.children[0].pid)


@pytest.mark.parametrize(
    "reported",
    [{}, {"cpu": "cuda"}, {"fake-ok": "fake-crash"}],
    ids=["version", "backend", "model"],
)
async def test_engine_mismatch(tmp_path: Path, reported: dict[str, str]) -> None:
    mode = "fake-wrong-version" if not reported else "fake-ok"
    install_model(tmp_path, mode)
    engine, recorder = make(tmp_path, mode, recorder=Recorder(reported))
    with pytest.raises(VoiceError) as caught:
        await engine.start()
    assert caught.value.code == "engine_mismatch"
    assert not engine.running and gone(recorder.children[0].pid)


async def run_capture(
    tmp_path: Path, mode: str, backend: str = "cpu"
) -> tuple[VoiceEngine, Recorder, VoiceResult]:
    install_model(tmp_path, mode)
    engine, recorder = make(tmp_path, mode, backend)
    await engine.start()
    await engine.begin(CAPTURE, "en")
    await engine.audio(CAPTURE, b"\x00\x00" * 320)
    return engine, recorder, await next_result(engine)


@pytest.mark.parametrize("mode", ["fake-garbage", "fake-long-line"])
async def test_bad_line_is_a_protocol_error(tmp_path: Path, mode: str) -> None:
    engine, recorder, result = await run_capture(tmp_path, mode)
    try:
        assert result == VoiceResult("error", CAPTURE, code="protocol_error")
        assert not engine.running and gone(recorder.children[0].pid)
    finally:
        await engine.close()


async def test_wrong_capture_is_a_protocol_error(tmp_path: Path) -> None:
    engine, recorder, result = await run_capture(tmp_path, "fake-wrong-capture")
    try:
        assert result == VoiceResult("error", CAPTURE, code="protocol_error")
        assert not engine.running and gone(recorder.children[0].pid)
        with pytest.raises(VoiceError, match="unknown_capture"):
            await engine.audio(CAPTURE, b"\x00\x00")
    finally:
        await engine.close()


async def test_too_many_queued_results_is_a_protocol_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(voice_engine, "MAX_RESULTS", 2)
    install_model(tmp_path, "fake-ok")
    engine, _ = make(tmp_path, "fake-ok")
    try:
        await engine.start()
        await engine.begin(CAPTURE, "en")
        for _ in range(3):
            await engine.audio(CAPTURE, CHUNK)
        # Read nothing until the reader stops: an early read would keep the queue under the cap.
        async with asyncio.timeout(HANG_GUARD_S):
            while engine.running:
                await asyncio.sleep(0)
        assert [await next_result(engine) for _ in range(3)] == [
            VoiceResult("partial", CAPTURE, 0, 1, "partial-1"),
            VoiceResult("partial", CAPTURE, 0, 2, "partial-2"),
            VoiceResult("error", CAPTURE, code="protocol_error"),
        ]
    finally:
        await engine.close()


async def test_crash_is_reported(tmp_path: Path) -> None:
    engine, recorder, result = await run_capture(tmp_path, "fake-crash")
    try:
        assert result == VoiceResult("error", CAPTURE, code="worker_crashed")
        assert not engine.running and engine.fallback is None
        assert gone(recorder.children[0].pid)
    finally:
        await engine.close()


async def test_appdata_reaches_the_cuda_worker_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("APPDATA", "appdata-test")
    base = media_worker._worker_env(sys.platform, os.environ)
    install_model(tmp_path, "fake-gpu-fails")
    install_cuda(tmp_path)
    engine, recorder = make(tmp_path, "fake-gpu-fails", "cuda")
    try:
        await engine.start()
        assert recorder.backends() == ["cuda", "cpu"]
        assert recorder.calls[0].env == {**base, "APPDATA": "appdata-test"}
        assert recorder.calls[1].env == base
    finally:
        await engine.close()


async def test_gpu_failure_falls_back_visibly(tmp_path: Path) -> None:
    install_model(tmp_path, "fake-gpu-fails")
    install_cuda(tmp_path)
    engine, recorder = make(tmp_path, "fake-gpu-fails", "cuda")
    try:
        await engine.start()
        assert recorder.backends() == ["cuda", "cpu"]
        assert engine.running and engine.backend == "cpu" and engine.device is None
        assert engine.fallback == Fallback("cuda", "backend_unavailable", "no_device")
        assert recorder.calls[0].memory_limit_bytes == vm.WORKER_MEMORY_BYTES["cuda"]
        assert gone(recorder.children[0].pid)

        await engine.close()
        await engine.start()
        assert recorder.backends() == ["cuda", "cpu", "cpu"]

        await engine.close()
        engine.clear_fallback()
        await engine.start()
        assert recorder.backends() == ["cuda", "cpu", "cpu", "cuda", "cpu"]
    finally:
        await engine.close()


async def test_gpu_fallback_runs_the_cpu_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # fake-gpu-fails stands for the GPU pair's model, fake-ok for the CPU ranking's model
    monkeypatch.setattr(
        vm, "RANKING", {"cpu": ("fake-ok",), "cuda": ("fake-gpu-fails",), "metal": ()}
    )
    install_model(tmp_path, "fake-gpu-fails")
    install_model(tmp_path, "fake-ok")
    install_cuda(tmp_path)
    engine, recorder = make(tmp_path, "fake-gpu-fails", "cuda")
    try:
        assert engine.resolve() == ("cuda", "fake-gpu-fails")
        await engine.start()
        assert [call.argv[-2:] for call in recorder.calls] == [
            ["fake-gpu-fails", "cuda"],
            ["fake-ok", "cpu"],
        ]
        assert engine.model_id == "fake-ok" and engine.backend == "cpu"
        assert engine.fallback == Fallback("cuda", "backend_unavailable", "no_device")
        assert engine.resolve() == ("cpu", "fake-ok")

        await engine.close()
        await engine.start()  # the fallback outlives the worker: the next start is the CPU pair
        assert recorder.calls[-1].argv[-2:] == ["fake-ok", "cpu"]

        await engine.close()
        engine.clear_fallback()
        assert engine.resolve() == ("cuda", "fake-gpu-fails")
    finally:
        await engine.close()


async def test_gpu_fallback_without_the_cpu_model_keeps_the_pairs_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        vm, "RANKING", {"cpu": ("fake-ok",), "cuda": ("fake-gpu-fails",), "metal": ()}
    )
    install_model(tmp_path, "fake-gpu-fails")  # fake-ok is not installed
    install_cuda(tmp_path)
    engine, recorder = make(tmp_path, "fake-gpu-fails", "cuda")
    try:
        await engine.start()
        assert [call.argv[-2:] for call in recorder.calls] == [
            ["fake-gpu-fails", "cuda"],
            ["fake-gpu-fails", "cpu"],
        ]
        assert engine.model_id == "fake-gpu-fails" and engine.backend == "cpu"
    finally:
        await engine.close()


async def test_gpu_crash_falls_back_on_the_next_start(tmp_path: Path) -> None:
    install_cuda(tmp_path)
    engine, recorder, result = await run_capture(tmp_path, "fake-gpu-crash", "cuda")
    try:
        assert recorder.backends() == ["cuda"]
        assert result == VoiceResult("error", CAPTURE, code="worker_crashed")
        assert engine.fallback == Fallback("cuda", "worker_crashed", None)
        await engine.start()
        assert recorder.backends() == ["cuda", "cpu"] and engine.backend == "cpu"
    finally:
        await engine.close()


async def test_unread_worker_is_overloaded(tmp_path: Path) -> None:
    install_model(tmp_path, "fake-no-read")
    engine, _ = make(tmp_path, "fake-no-read", max_pending_bytes=100_000)
    try:
        await engine.start()
        await engine.begin(CAPTURE, "en")
        stdin = engine._stdin
        assert stdin is not None
        for _ in range(voice_engine.MAX_CAPTURE_BYTES // len(BIG)):
            pending = stdin.transport.get_write_buffer_size()
            try:
                await engine.audio(CAPTURE, BIG)
            except VoiceError as exc:
                assert exc.code == "overloaded"
                assert stdin.transport.get_write_buffer_size() == pending  # nothing written
                break
        else:
            pytest.fail("the unread pipe never overloaded")
    finally:
        await engine.close()


async def test_capture_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(voice_engine, "MAX_CAPTURE_BYTES", 1000)
    install_model(tmp_path, "fake-ok")
    engine, _ = make(tmp_path, "fake-ok")
    try:
        await engine.start()
        await engine.begin(CAPTURE, "en")
        await engine.audio(CAPTURE, b"\x00\x00" * 320)
        with pytest.raises(VoiceError, match="capture_cap"):
            await engine.audio(CAPTURE, b"\x00\x00" * 320)
        await engine.end(CAPTURE)
        assert await next_result(engine) == VoiceResult("final", CAPTURE, 0, None, "final-640")
    finally:
        await engine.close()


async def test_stderr_flood_completes(tmp_path: Path) -> None:
    install_model(tmp_path, "fake-stderr")
    engine, _ = make(tmp_path, "fake-stderr")
    try:
        await engine.start()
        await engine.begin(CAPTURE, "en")
        await engine.audio(CAPTURE, CHUNK)
        await engine.end(CAPTURE)
        assert [(await next_result(engine)).kind for _ in range(3)] == [
            "partial",
            "final",
            "done",
        ]
    finally:
        await engine.close()


async def test_capture_refusals(tmp_path: Path) -> None:
    install_model(tmp_path, "fake-no-done")
    engine, _ = make(tmp_path, "fake-no-done")
    try:
        with pytest.raises(VoiceError, match="not_running"):
            await engine.begin(CAPTURE, "en")
        await engine.start()
        for capture in ("not-a-uuid", CAPTURE.upper()):
            with pytest.raises(VoiceError, match="invalid_capture"):
                await engine.begin(capture, "en")
        with pytest.raises(VoiceError, match="invalid_language"):
            await engine.begin(CAPTURE, "fr")
        with pytest.raises(VoiceError, match="unknown_capture"):
            await engine.audio(CAPTURE, b"\x00\x00")
        await engine.begin(CAPTURE, "en")
        with pytest.raises(VoiceError, match="busy"):
            await engine.begin(OTHER, "en")
        for pcm in (b"", b"\x00" * 3, b"\x00" * 65_538):
            with pytest.raises(VoiceError, match="invalid_audio"):
                await engine.audio(CAPTURE, pcm)
        with pytest.raises(VoiceError, match="unknown_capture"):
            await engine.audio(OTHER, b"\x00\x00")
        await engine.end(CAPTURE)
        assert await next_result(engine) == VoiceResult("final", CAPTURE, 0, None, "final-0")
        with pytest.raises(VoiceError, match="busy"):  # closing: no done from this fake
            await engine.begin(OTHER, "en")
        for step in (engine.end, engine.cancel):
            with pytest.raises(VoiceError, match="unknown_capture"):
                await step(CAPTURE)
    finally:
        await engine.close()


async def test_close_is_idempotent_and_leaves_no_process(tmp_path: Path) -> None:
    install_model(tmp_path, "fake-ok")
    engine, recorder = make(tmp_path, "fake-ok")
    await engine.start()
    pid = engine.pid
    assert pid is not None and not gone(pid)
    await engine.close()
    await engine.close()
    assert gone(pid) and not engine.running and engine.pid is None
    # On Windows the venv's python.exe is a launcher: its child must be gone too.
    assert recorder.all_reaped() and recorder.children[0].job.active_processes() == 0
    assert engine.results.empty()  # a close is not a crash


async def until_stopped(engine: VoiceEngine) -> None:
    async with asyncio.timeout(HANG_GUARD_S):
        while engine.running:
            await asyncio.sleep(0)


async def crash_gpu_worker(tmp_path: Path, recorder: Recorder) -> VoiceEngine:
    """A cuda engine whose worker has just crashed: `running` turned False this moment."""
    install_cuda(tmp_path)
    install_model(tmp_path, "fake-gpu-crash")
    engine, _ = make(tmp_path, "fake-gpu-crash", "cuda", recorder)
    await engine.start()
    await engine.begin(CAPTURE, "en")
    await engine.audio(CAPTURE, b"\x00\x00" * 320)
    await until_stopped(engine)
    return engine


async def test_a_start_right_after_a_gpu_crash_runs_cpu(tmp_path: Path) -> None:
    recorder = Recorder()
    engine = await crash_gpu_worker(tmp_path, recorder)
    try:
        await engine.start()
        assert recorder.backends() == ["cuda", "cpu"] and engine.backend == "cpu"
        assert engine.fallback == Fallback("cuda", "worker_crashed", None)
        assert recorder.reaped_before == [True, True]  # the cpu spawn came after the reap
    finally:
        await engine.close()


async def test_a_start_waits_for_the_crashed_worker_to_be_reaped(tmp_path: Path) -> None:
    recorder = HeldStderr()
    engine = await crash_gpu_worker(tmp_path, recorder)
    try:
        await asyncio.wait_for(recorder.real_eof.wait(), HANG_GUARD_S)
        starting = asyncio.create_task(engine.start())
        # Must time out: the held reap blocks the start, which otherwise spawns well within it.
        await asyncio.wait([starting], timeout=0.05)
        assert not starting.done() and recorder.backends() == ["cuda"]
        recorder.release.set()
        await asyncio.wait_for(starting, HANG_GUARD_S)
        assert recorder.backends() == ["cuda", "cpu"] and engine.backend == "cpu"
        assert recorder.reaped_before == [True, True]  # the cpu spawn came after the reap
        await engine.begin(CAPTURE, "en")  # the old reap left the new worker's handles alone
    finally:
        recorder.release.set()
        await engine.close()
        assert recorder.pump is not None
        await recorder.pump
    assert recorder.all_reaped()


async def test_two_concurrent_closes_both_return(tmp_path: Path) -> None:
    install_model(tmp_path, "fake-ok")
    engine, recorder = make(tmp_path, "fake-ok")
    await engine.start()
    pid = engine.pid
    assert pid is not None
    await asyncio.wait_for(asyncio.gather(engine.close(), engine.close()), HANG_GUARD_S)
    assert gone(pid) and recorder.all_reaped() and len(recorder.children) == 1


async def test_close_during_a_crash_reap_finishes_the_reap(tmp_path: Path) -> None:
    install_model(tmp_path, "fake-crash")
    recorder = HeldStderr()
    engine, _ = make(tmp_path, "fake-crash", recorder=recorder)
    await engine.start()
    await engine.begin(CAPTURE, "en")
    await engine.audio(CAPTURE, b"\x00\x00" * 320)
    await until_stopped(engine)
    await asyncio.wait_for(recorder.real_eof.wait(), HANG_GUARD_S)
    process = recorder.children[0].process
    async with asyncio.timeout(HANG_GUARD_S):
        while process.returncode is None:
            await asyncio.sleep(0)
    for _ in range(20):  # the crash's stop is now past process.wait(), on stderr's EOF
        await asyncio.sleep(0)
    closing = asyncio.create_task(engine.close())
    for _ in range(20):
        await asyncio.sleep(0)
    assert not closing.done()  # close() waits for the reap, which waits for stderr's EOF
    recorder.release.set()
    await asyncio.wait_for(closing, HANG_GUARD_S)  # no CancelledError
    assert recorder.pump is not None
    await recorder.pump
    assert recorder.all_reaped() and recorder.children[0].job.active_processes() == 0


async def test_close_refuses_a_start_queued_behind_another(tmp_path: Path) -> None:
    install_model(tmp_path, "fake-hang-ready")
    engine, recorder = make(tmp_path, "fake-hang-ready", ready_timeout_s=HANG_GUARD_S)
    first = asyncio.create_task(engine.start())
    await asyncio.wait_for(recorder.spawned.wait(), HANG_GUARD_S)
    queued = asyncio.create_task(engine.start())
    await asyncio.sleep(0)  # the queued start now waits on the lock
    try:
        await engine.close()
        for start in (first, queued):
            with pytest.raises(VoiceError) as caught:
                await asyncio.wait_for(start, HANG_GUARD_S)
            assert caught.value.code == "not_running"
        assert len(recorder.calls) == 1 and recorder.all_reaped()
    finally:
        await engine.close()


async def test_a_start_during_close_is_refused(tmp_path: Path) -> None:
    install_model(tmp_path, "fake-ok")
    recorder = HeldStderr()
    engine, _ = make(tmp_path, "fake-ok", recorder=recorder)
    await engine.start()
    closing = asyncio.create_task(engine.close())
    for _ in range(20):
        await asyncio.sleep(0)
    assert not closing.done()  # close() waits for the reap, which waits for stderr's EOF
    starting = asyncio.create_task(engine.start())
    for _ in range(20):
        await asyncio.sleep(0)
    recorder.release.set()
    await asyncio.wait_for(closing, HANG_GUARD_S)
    try:
        with pytest.raises(VoiceError) as caught:
            await asyncio.wait_for(starting, HANG_GUARD_S)
        assert caught.value.code == "not_running" and len(recorder.calls) == 1
        await engine.start()  # a start after close() returned runs normally
        assert len(recorder.calls) == 2 and engine.running
    finally:
        await engine.close()
        assert recorder.pump is not None
        await recorder.pump
    assert recorder.all_reaped()


async def test_an_oserror_on_read_is_a_crash(tmp_path: Path) -> None:
    install_model(tmp_path, "fake-ok")
    engine, recorder = make(tmp_path, "fake-ok", recorder=BreaksStdout())
    try:
        await engine.start()
        assert await next_result(engine) == VoiceResult("error", None, code="worker_crashed")
        assert not engine.running and engine.fallback is None
        assert recorder.all_reaped()
    finally:
        await engine.close()


async def test_log_holds_no_worker_text(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    install_cuda(tmp_path)
    install_model(tmp_path, "fake-gpu-fails")
    engine, _ = make(tmp_path, "fake-gpu-fails", "cuda")
    try:
        await engine.start()
        await engine.begin(CAPTURE, "en")
        await engine.audio(CAPTURE, CHUNK)
        await engine.end(CAPTURE)
        assert [(await next_result(engine)).text for _ in range(3)] == [
            "partial-1",
            "final-16000",
            "",
        ]
    finally:
        await engine.close()
    engine, _, _ = await run_capture(tmp_path, "fake-gpu-crash", "cuda")
    await engine.close()
    assert caplog.records, "the fallback and the crash log a warning"
    for text in ("partial-", "final-", "Fake GPU", "no_device", "fake-", str(tmp_path)):
        assert text not in caplog.text


def test_fake_reports_the_pinned_engine() -> None:
    source = FAKE.read_text(encoding="utf-8")
    assert f'"engine": "{vm.ENGINE}"' in source
    assert f'"{vm.ENGINE_VERSION}"' in source
