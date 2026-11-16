"""One voice worker through `winproc.spawn`: frames in, results out, and the visible fallback.

The worker (`voice_worker.py`) speaks this wire: `>cI` frames on stdin,
one ASCII JSON object per line on stdout, native output on stderr, which is drained unread. A
non-CPU backend that fails before ready, or whose worker crashes, leaves `fallback` set and the
engine on `cpu` until `clear_fallback()`. During a fallback `resolve()` names the CPU ranking's
first installed model, else the pair's own model.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import os
import platform
import struct
import sys
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from viki_agent import compiled, winproc
from viki_agent.api_json import unique_object
from viki_agent.media import media_worker
from viki_agent.voice import voice_models
from viki_agent.voice.voice_stream import END_SILENCE_FRAMES

logger = logging.getLogger(__name__)

WORKER = Path(__file__).with_name("voice_worker.py")
FINAL_TIMEOUT_S = 25
MAX_PENDING_BYTES = 960_000
MAX_CAPTURE_BYTES = 3_840_000
MAX_RESULTS = 1024
MAX_TEXT_CHARS = 4_096

_MAX_AUDIO_BYTES = 65_536
_MAX_LABEL_CHARS = 256  # a device name or a failure detail
_READ_CHUNK_BYTES = 65_536
# Failures the CPU shares: starting `cpu` instead cannot help.
_SHARED_FAILURES = frozenset({"setup_needed", "model_mismatch", "engine_unavailable"})
_EXIT_CODES = {3: "engine_unavailable", 4: "model_unavailable", 5: "backend_unavailable"}
_READY_KEYS = frozenset({"type", "engine", "engine_version", "model", "backend", "device"})
_RESULT_KEYS = {
    "partial": frozenset({"type", "capture", "segment", "revision", "text"}),
    "final": frozenset({"type", "capture", "segment", "text"}),
    "done": frozenset({"type", "capture"}),
    "cancelled": frozenset({"type", "capture"}),
}
_WORKER_CODES = frozenset(
    {
        "invalid_input",
        "engine_unavailable",
        "model_unavailable",
        "backend_unavailable",
        "decode_failed",
        "internal",
    }
)
# What a malformed line raises in json.loads, the checks below or the 64 KiB readline limit.
_LINE_ERRORS = (ValueError, TypeError, RecursionError)

Spawn = Callable[..., Awaitable[winproc.Child]]


class VoiceError(Exception):
    def __init__(self, code: str, detail: str | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class VoiceResult:
    kind: Literal["partial", "final", "done", "cancelled", "error"]
    capture_id: str | None
    segment: int | None = None
    revision: int | None = None
    text: str = ""
    code: str | None = None


@dataclass(frozen=True)
class Fallback:
    backend: str
    code: str
    detail: str | None


def _engine_installed() -> bool:
    return importlib.util.find_spec("pywhispercpp") is not None


async def _drop(stream: asyncio.StreamReader) -> None:
    while await stream.read(_READ_CHUNK_BYTES):
        pass


async def _reap(child: winproc.Child, draining: asyncio.Task[None] | None) -> None:
    """Kill one worker's tree, wait until all of it has ended, then release its Job."""
    try:
        child.kill_tree()
        process = child.process
        assert process.stdin is not None and process.stdout is not None
        process.stdin.close()
        await _drop(process.stdout)  # a paused full reader would otherwise never see EOF
        await process.wait()
        if draining is not None:
            await asyncio.shield(draining)
        await child.wait_tree(poll_s=0.05)  # on Windows the venv launcher's child too
    except Exception as exc:
        logger.warning("voice worker reap failed: %s", type(exc).__name__)
    finally:
        child.release()


def _line_object(line: bytes) -> dict[str, Any]:
    data = json.loads(line.decode("ascii"), object_pairs_hook=unique_object)
    if not isinstance(data, dict):
        raise ValueError("not an object")
    return data


def _is_canonical_uuid(value: str) -> bool:
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False


class VoiceEngine:
    def __init__(
        self,
        data_dir: Path,
        *,
        model_id: str | None = None,
        backend: str = "auto",
        worker: Path = WORKER,
        python: str = sys.executable,
        spawn: Spawn = winproc.spawn,
        ready_timeout_s: float | None = None,
        max_pending_bytes: int = MAX_PENDING_BYTES,
    ) -> None:
        self._data_dir = data_dir
        self._want_model = model_id
        self._want_backend = backend
        self._worker = worker
        self._python = python
        self._spawn = spawn
        self._ready_timeout_s = ready_timeout_s
        self._max_pending_bytes = max_pending_bytes
        self.results: asyncio.Queue[VoiceResult] = asyncio.Queue()
        self.running = False
        self.pid: int | None = None
        self.model_id: str | None = None
        self.backend: str | None = None
        self.device: str | None = None
        self.fallback: Fallback | None = None
        self._languages: tuple[str, ...] = ()
        self._child: winproc.Child | None = None
        self._stdin: asyncio.StreamWriter | None = None
        self._draining: asyncio.Task[None] | None = None
        self._reaping: asyncio.Task[None] | None = None
        self._reader: asyncio.Task[None] | None = None
        self._starting: asyncio.Task[None] | None = None
        self._start_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._capture: str | None = None
        self._closing = False
        self._capture_bytes = 0

    # --- what is installed ----------------------------------------------------------------------

    def installed(self) -> tuple[list[str], list[str]]:
        models = [
            model_id
            for model_id in voice_models.MODELS
            if voice_models.present(
                voice_models.model_dir(self._data_dir, model_id),
                voice_models.model_files(model_id),
            )
        ]
        backends = [
            backend_id
            for backend_id, backend in voice_models.BACKENDS.items()
            if voice_models.present(
                voice_models.backend_dir(self._data_dir, backend_id), backend.files
            )
        ]
        return models, backends

    def resolve(self) -> tuple[str, str | None]:
        models, _ = self.installed()
        backend = self._want_backend
        if backend == "auto":
            started = [
                backend_id
                for backend_id, spec in voice_models.BACKENDS.items()
                if not spec.files or voice_models.backend_dir(self._data_dir, backend_id).is_dir()
            ]
            backend = voice_models.auto_backend(
                sys.platform, platform.machine(), started, models, self._want_model
            )
        model_id = self._want_model or voice_models.auto_model(backend, models)
        if self.fallback:
            # The GPU pair's model is too slow on the CPU: run the CPU ranking's model when set up.
            return "cpu", voice_models.auto_model("cpu", models) or model_id
        return backend, model_id

    def status(self) -> Literal["ready", "setup_needed", "backend_missing", "engine_unavailable"]:
        backend, model_id = self.resolve()
        if model_id is None or not voice_models.present(
            voice_models.model_dir(self._data_dir, model_id), voice_models.model_files(model_id)
        ):
            return "setup_needed"
        if not voice_models.present(
            voice_models.backend_dir(self._data_dir, backend), voice_models.BACKENDS[backend].files
        ):
            return "backend_missing"
        return "ready" if _engine_installed() else "engine_unavailable"

    def clear_fallback(self) -> None:
        self.fallback = None

    # --- start and stop -------------------------------------------------------------------------

    async def start(self) -> None:
        async with self._start_lock:
            # Refuse while a close() runs. A start queued before it gets the lock when the start
            # close() cancelled exits, from that start's own done callback, before close()
            # resumes, so it sees the close lock held. No await sits between this check and
            # `self._starting = ...`, and close() cancels any `_starting` it finds, so a start
            # finishes before close() takes its child, is cancelled by it, or is refused.
            if self._close_lock.locked():
                raise VoiceError("not_running")
            if self.running:
                return
            # A task of its own, so close() can cancel a start (a CUDA cold load takes minutes).
            self._starting = asyncio.ensure_future(self._start())
            try:
                await self._starting
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    raise
                raise VoiceError("not_running") from None  # close() cancelled it
            finally:
                self._starting = None

    async def _start(self) -> None:
        # A crashed worker's reap may still run: spawn after it, and after the fallback it set.
        await self._reaped()
        backend, model_id = self.resolve()
        try:
            await self._launch(model_id, backend)
        except VoiceError as exc:
            if backend == "cpu" or exc.code in _SHARED_FAILURES:
                raise
            self.fallback = Fallback(backend, exc.code, exc.detail)
            logger.warning("voice backend fell back to cpu: %s", exc.code)
            backend, model_id = self.resolve()
            await self._launch(model_id, backend)

    async def _check(self, model_id: str | None, backend: str) -> str:
        """Refuse a start whose files are missing or off, or whose engine is not installed."""
        if model_id is None:
            raise VoiceError("setup_needed")
        model_dir = voice_models.model_dir(self._data_dir, model_id)
        model_files = voice_models.model_files(model_id)
        if not voice_models.present(model_dir, model_files):
            raise VoiceError("setup_needed")
        if await asyncio.to_thread(voice_models.verify, model_dir, model_files):
            raise VoiceError("model_mismatch")
        backend_dir = voice_models.backend_dir(self._data_dir, backend)
        backend_files = voice_models.BACKENDS[backend].files
        if not voice_models.present(backend_dir, backend_files):
            raise VoiceError("backend_missing")
        if await asyncio.to_thread(voice_models.verify, backend_dir, backend_files):
            raise VoiceError("backend_mismatch")
        if not _engine_installed():
            raise VoiceError("engine_unavailable")
        return model_id

    async def _launch(self, model_id: str | None, backend: str) -> None:
        try:
            model_id = await self._check(model_id, backend)
            argv = compiled.worker_argv(
                "voice", [model_id, backend], python=self._python, script=self._worker
            )
            env = media_worker._worker_env(sys.platform, os.environ)
            # Without APPDATA the NVIDIA driver's compute cache is unused and every start
            # recompiles its kernels.
            if backend == "cuda" and "APPDATA" in os.environ:
                env["APPDATA"] = os.environ["APPDATA"]
            try:
                child = await self._spawn(
                    argv,
                    cwd=self._data_dir / "voice",
                    env=env,
                    memory_limit_bytes=voice_models.WORKER_MEMORY_BYTES[backend],
                )
            except OSError:
                raise VoiceError("worker_unavailable") from None
            process = child.process
            assert process.stdin is not None and process.stdout is not None
            assert process.stderr is not None
            self._child, self._stdin = child, process.stdin
            self._draining = asyncio.create_task(_drop(process.stderr))
            try:
                device = await self._ready(child, model_id, backend)
            except BaseException:
                await self._stop()
                raise
        except VoiceError as exc:
            logger.warning("voice start failed: %s", exc.code)
            raise
        self.running, self.pid, self.device = True, child.pid, device
        self.model_id, self.backend = model_id, backend
        self._languages = voice_models.MODELS[model_id].languages
        self._reader = asyncio.create_task(self._read(process.stdout))

    async def _ready(self, child: winproc.Child, model_id: str, backend: str) -> str | None:
        """The device from the worker's ready line, or the VoiceError its absence means."""
        process = child.process
        assert process.stdin is not None and process.stdout is not None
        timeout = self._ready_timeout_s
        if timeout is None:
            timeout = voice_models.READY_TIMEOUT_S[backend]
        try:
            async with asyncio.timeout(timeout):
                line = await process.stdout.readline()
                data = _line_object(line) if line else None
                if data is None or data.get("type") == "error":
                    # The worker refused to start: its exit code says why.
                    process.stdin.close()
                    await _drop(process.stdout)
                    await process.wait()
                    code = _EXIT_CODES.get(process.returncode or 0, "worker_unavailable")
                    detail = data.get("detail") if data and code == "backend_unavailable" else None
                    ok = isinstance(detail, str) and len(detail) <= _MAX_LABEL_CHARS
                    raise VoiceError(code, detail if ok else None)
        except TimeoutError:
            raise VoiceError("worker_timeout") from None
        except _LINE_ERRORS:
            raise VoiceError("protocol_error") from None
        if data.get("type") != "ready":
            raise VoiceError("protocol_error")
        device = data.get("device")
        if (
            set(data) != _READY_KEYS
            or data["engine"] != voice_models.ENGINE
            or data["engine_version"] != voice_models.ENGINE_VERSION
            or data["model"] != model_id
            or data["backend"] != backend
            or not (device is None or (type(device) is str and len(device) <= _MAX_LABEL_CHARS))
        ):
            raise VoiceError("engine_mismatch")
        return device

    async def _stop(self) -> None:
        """Hand the worker to a reap task of its own, then wait for the reap to finish."""
        self.running = False
        self.pid = self.model_id = self.backend = self.device = None
        self._capture, self._closing = None, False
        child, draining = self._child, self._draining
        # Before any await, so no later stop and no newer worker's start shares these handles.
        self._child = self._stdin = self._draining = None
        if child is not None:
            self._reaping = asyncio.create_task(_reap(child, draining))
        await self._reaped()

    async def _reaped(self) -> None:
        """Wait for the latest reap; cancelling the waiter leaves the reap running."""
        if self._reaping is not None:
            await asyncio.wait([self._reaping])

    async def close(self) -> None:
        async with self._close_lock:
            if self._starting is not None:
                self._starting.cancel()
                await asyncio.wait([self._starting])
            if self._child is not None:
                self._child.kill_tree()  # before the reader runs again, so it reports no crash
            reader, self._reader = self._reader, None
            if reader is not None:
                reader.cancel()
                await asyncio.wait([reader])
            await self._stop()

    # --- captures -------------------------------------------------------------------------------

    def _write(self, kind: bytes, payload: bytes) -> None:
        assert self._stdin is not None
        self._stdin.write(struct.pack(">cI", kind, len(payload)) + payload)

    def _require_open(self, capture_id: str) -> None:
        if self._capture is None or capture_id != self._capture or self._closing:
            raise VoiceError("unknown_capture")

    async def begin(
        self, capture_id: str, language: str, *, pause_frames: int = END_SILENCE_FRAMES
    ) -> None:
        if not self.running:
            raise VoiceError("not_running")
        if self._capture is not None:
            raise VoiceError("busy")
        if not _is_canonical_uuid(capture_id):
            raise VoiceError("invalid_capture")
        if language != "auto" and language not in self._languages:
            raise VoiceError("invalid_language")
        body = {"capture": capture_id, "language": language, "pause_frames": pause_frames}
        self._write(b"B", json.dumps(body, separators=(",", ":")).encode("ascii"))
        self._capture, self._closing, self._capture_bytes = capture_id, False, 0

    async def audio(self, capture_id: str, pcm: bytes) -> None:
        self._require_open(capture_id)
        if not 2 <= len(pcm) <= _MAX_AUDIO_BYTES or len(pcm) % 2:
            raise VoiceError("invalid_audio")
        if self._capture_bytes + len(pcm) > MAX_CAPTURE_BYTES:
            raise VoiceError("capture_cap")
        assert self._stdin is not None
        if self._stdin.transport.get_write_buffer_size() + len(pcm) > self._max_pending_bytes:
            raise VoiceError("overloaded")
        self._write(b"A", pcm)
        self._capture_bytes += len(pcm)

    async def end(self, capture_id: str) -> None:
        self._require_open(capture_id)
        self._write(b"E", b"")
        self._closing = True

    async def cancel(self, capture_id: str) -> None:
        self._require_open(capture_id)
        self._write(b"X", b"")
        self._closing = True

    # --- results --------------------------------------------------------------------------------

    def _result(self, line: bytes) -> VoiceResult:
        """The result one worker line carries; ValueError or TypeError for a violation."""
        data = _line_object(line)
        kind = data.get("type")
        if kind == "error":
            code = data.get("code")
            keys = {"type", "code", "detail"} if code == "backend_unavailable" else {"type", "code"}
            if set(data) != keys or code not in _WORKER_CODES:
                raise ValueError("error line")
            return VoiceResult("error", self._capture, code=code)
        if kind not in _RESULT_KEYS or set(data) != _RESULT_KEYS[kind]:
            raise ValueError("keys")
        if self._capture is None or data["capture"] != self._capture:
            raise ValueError("capture")
        segment, revision, text = data.get("segment"), data.get("revision"), data.get("text", "")
        if "segment" in data and not (type(segment) is int and segment >= 0):
            raise ValueError("segment")
        if "revision" in data and not (type(revision) is int and revision >= 1):
            raise ValueError("revision")
        if type(text) is not str or len(text) > MAX_TEXT_CHARS:
            raise ValueError("text")
        return VoiceResult(kind, self._capture, segment, revision, text)

    async def _read(self, stdout: asyncio.StreamReader) -> None:
        code = "worker_crashed"
        try:
            while line := await stdout.readline():  # ValueError past the 64 KiB limit
                result = self._result(line)
                if self.results.qsize() >= MAX_RESULTS:
                    raise ValueError("results")
                self.results.put_nowait(result)
                if result.kind in ("done", "cancelled"):
                    self._capture, self._closing = None, False
        except _LINE_ERRORS:
            code = "protocol_error"
        except OSError:
            code = "worker_crashed"  # a broken pipe ends the worker like EOF
        capture, backend = self._capture, self.backend
        if code == "worker_crashed" and backend is not None and backend != "cpu":
            # Before the stop clears `running`: a start right after it resolves cpu.
            self.fallback = Fallback(backend, code, None)
        await self._stop()
        logger.warning("voice worker stopped: %s", code)
        self.results.put_nowait(VoiceResult("error", capture, code=code))
