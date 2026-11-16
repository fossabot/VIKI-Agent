"""The app's one voice engine: built for the settings' pair, started at a press, closed when idle.

The model, backend and dictation language are read from the settings at every `begin` and every
`status()`. A different model or backend closes the held engine and builds one for the new pair.
The engine starts at the first `begin` and closes `idle_close_s` after the last capture, so an idle
app holds no worker, no VRAM and no model in memory.
"""

from __future__ import annotations

import asyncio
import logging
import platform
import sys
from collections.abc import Awaitable, Callable, Coroutine, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from viki_agent.voice import voice_engine, voice_models
from viki_agent.voice.voice_engine import Fallback, VoiceEngine, VoiceError, VoiceResult
from viki_agent.voice.voice_stream import END_SILENCE_FRAMES

logger = logging.getLogger(__name__)

IDLE_CLOSE_S = 300.0


@dataclass(frozen=True)
class Capturing:
    model: str
    backend: str
    device: str | None
    language: str
    language_unavailable: bool
    fallback: Fallback | None


def _language(setting: str, model: str | None) -> tuple[str, bool]:
    """The language a capture runs in, and whether the stored one was unavailable.

    Without a model nothing is unavailable yet: the state is setup_needed.
    """
    if model is None:
        return "auto", False
    if setting == "auto" or setting in voice_models.MODELS[model].languages:
        return setting, False
    return "auto", True


def _files(engine: VoiceEngine) -> tuple[tuple[list[str], list[str]], str, tuple[str, str | None]]:
    """Everything the status view reads from disk (lstat only), for a worker thread."""
    return engine.installed(), engine.status(), engine.resolve()


class VoiceService:
    def __init__(
        self,
        data_dir: Path,
        *,
        read_settings: Callable[[], Mapping[str, object]],
        worker: Path = voice_engine.WORKER,
        engine: Callable[..., VoiceEngine] = VoiceEngine,
        idle_close_s: float = IDLE_CLOSE_S,
    ) -> None:
        self._data_dir = data_dir
        self._read_settings = read_settings
        self._worker = worker
        self._make = engine
        self._idle_close_s = idle_close_s
        self._engine: VoiceEngine | None = None
        self._pair: tuple[str | None, str] | None = None
        self._lock = asyncio.Lock()
        self._capture: str | None = None
        self._restart = False  # a clear_fallback() arrived while the engine could not close
        self._closed = False
        self._idle: asyncio.TimerHandle | None = None
        self._generation = 0  # an idle close armed before the latest begin or finish is stale
        self._tasks: set[asyncio.Task[None]] = set()

    # --- settings -------------------------------------------------------------------------------

    def _settings(self) -> tuple[str | None, str, str, int]:
        """(model id or None, backend, language, pause_frames) from the settings; defaults when
        unreadable."""
        try:
            values = self._read_settings()
            model, backend = values.get("voice.model"), values.get("voice.backend")
            language = values.get("voice.dictation_language")
            pause_s = values.get("voice.pause_s")
        except Exception as exc:
            logger.warning("voice settings read failed: %s", type(exc).__name__)
            return None, "auto", "auto", END_SILENCE_FRAMES
        offered = voice_models.offered(sys.platform, platform.machine())
        pause_frames = (
            round(pause_s * 50)
            if isinstance(pause_s, (int, float)) and not isinstance(pause_s, bool)
            else END_SILENCE_FRAMES
        )
        return (
            model if isinstance(model, str) and model in voice_models.MODELS else None,
            backend if isinstance(backend, str) and backend in offered else "auto",
            language if isinstance(language, str) else "auto",
            pause_frames,
        )

    def _build(self, model_id: str | None, backend: str) -> VoiceEngine:
        return self._make(self._data_dir, model_id=model_id, backend=backend, worker=self._worker)

    def _held(self) -> VoiceEngine:
        if self._engine is None:
            raise VoiceError("not_running")
        return self._engine

    # --- captures -------------------------------------------------------------------------------

    async def begin(self, capture_id: str, *, starting: Callable[[], Awaitable[None]]) -> Capturing:
        self._cancel_idle()
        async with self._lock:
            if self._capture is not None:
                raise VoiceError("busy")
            try:
                return await self._begin(capture_id, starting)
            except BaseException:
                self._arm_idle()  # a started engine whose capture failed still closes when idle
                raise

    async def _begin(self, capture_id: str, starting: Callable[[], Awaitable[None]]) -> Capturing:
        model_id, backend, setting, pause_frames = await asyncio.to_thread(self._settings)
        pair = (model_id, backend)
        if self._engine is not None and (self._pair != pair or self._restart):
            await self._engine.close()
            self._restart = False
        if self._engine is None or self._pair != pair:
            self._engine, self._pair = self._build(model_id, backend), pair
        engine = self._engine
        if not engine.running:
            await starting()
            # No await between this check and start(): a close() after it cancels the start.
            if self._closed:
                raise VoiceError("not_running")
            await engine.start()
        # No capture is open, so anything queued is stale: a crash's error after a done, or a
        # late result of a capture that was cut off.
        while not engine.results.empty():
            engine.results.get_nowait()
        model, running_backend = engine.model_id, engine.backend
        if model is None or running_backend is None:
            raise VoiceError("not_running")  # the worker ended right after its start
        language, unavailable = _language(setting, model)
        await engine.begin(capture_id, language, pause_frames=pause_frames)
        self._capture = capture_id
        return Capturing(
            model, running_backend, engine.device, language, unavailable, engine.fallback
        )

    async def audio(self, capture_id: str, pcm: bytes) -> None:
        await self._held().audio(capture_id, pcm)

    async def end(self, capture_id: str) -> None:
        await self._held().end(capture_id)

    async def cancel(self, capture_id: str) -> None:
        await self._held().cancel(capture_id)

    def results(self) -> asyncio.Queue[VoiceResult]:
        return self._held().results

    def finished(self) -> None:
        """The capture is over: close the engine when idle, or now after a clear_fallback()."""
        self._capture = None
        self._arm_idle()

    async def abort(self) -> None:
        if self._engine is not None:
            await self._engine.close()

    # --- idle close -----------------------------------------------------------------------------

    def _cancel_idle(self) -> None:
        self._generation += 1
        if self._idle is not None:
            self._idle.cancel()
            self._idle = None

    def _arm_idle(self) -> None:
        self._cancel_idle()
        if self._closed:
            return
        delay = 0.0 if self._restart else self._idle_close_s
        generation = self._generation
        self._idle = asyncio.get_running_loop().call_later(
            delay, lambda: self._track(self._close_idle(generation))
        )

    def _track(self, work: Coroutine[Any, Any, None]) -> None:
        task = asyncio.ensure_future(work)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _close_idle(self, generation: int) -> None:
        async with self._lock:
            if generation != self._generation or self._capture is not None:
                return  # a begin or a later finish came after this close was armed
            if self._engine is not None:
                self._restart = False
                await self._engine.close()

    # --- status and fallback --------------------------------------------------------------------

    async def status(self) -> dict[str, object]:
        model_id, backend, setting, _pause_frames = await asyncio.to_thread(self._settings)
        engine = self._engine
        if engine is None or self._pair != (model_id, backend):
            engine = self._build(model_id, backend)  # on the loop: it makes asyncio primitives
        (models, backends), state, resolved = await asyncio.to_thread(_files, engine)
        if engine.running and engine.model_id is not None and engine.backend is not None:
            backend, model = engine.backend, engine.model_id
        else:
            backend, model = resolved
        languages = voice_models.MODELS[model].languages if model is not None else ()
        language, unavailable = _language(setting, model)
        fallback = engine.fallback
        offered = voice_models.offered(sys.platform, platform.machine())
        return {
            "state": state,
            "running": engine.running,
            "model": model,
            "backend": backend,
            "device": engine.device,
            "languages": ["auto", *languages],
            "language": language,
            "language_unavailable": unavailable,
            "models": models,
            "backends": [backend_id for backend_id in offered if backend_id in backends],
            "fallback": None
            if fallback is None
            else {"backend": fallback.backend, "code": fallback.code, "detail": fallback.detail},
            "cpu_model": voice_models.auto_model("cpu", models),
            "slow_on_cpu": backend == "cpu"
            and model is not None
            and model not in voice_models.RANKING["cpu"],
        }

    async def clear_fallback(self) -> dict[str, object]:
        engine = self._engine
        if engine is not None:
            engine.clear_fallback()
            if self._capture is None and not self._lock.locked():
                async with self._lock:  # free: no await since the check
                    self._restart = False
                    await engine.close()
            else:
                self._restart = True  # the capture's finished(), or the next begin, closes it
        return await self.status()

    async def close(self) -> None:
        """Close the engine now, without the lock: VoiceEngine.close() cancels a start."""
        self._closed = True
        self._cancel_idle()
        if self._engine is not None:
            await self._engine.close()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
