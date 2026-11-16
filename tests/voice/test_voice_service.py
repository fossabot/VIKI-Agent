"""VoiceService over the real `winproc.spawn` of tests/fake_voice_worker.py.

The test pins of test_voice_engine name the fake's modes as model ids; the settings are a dict.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests.voice.test_voice_engine import (
    CAPTURE,
    CHUNK,
    FAKE,
    HANG_GUARD_S,
    OTHER,
    Recorder,
    gone,
    install_cuda,
    install_model,
    pins,  # noqa: F401 (autouse: the fake's modes as pinned models)
)
from viki_agent.app.settings import KEYS
from viki_agent.voice import voice_models as vm
from viki_agent.voice import voice_service
from viki_agent.voice.voice_engine import Fallback, VoiceEngine, VoiceError, VoiceResult
from viki_agent.voice.voice_service import Capturing, VoiceService
from viki_agent.voice.voice_stream import END_SILENCE_FRAMES, parse_begin

THIRD = "11111111-1111-4111-8111-111111111111"


@pytest.fixture(autouse=True)
def windows_amd64(monkeypatch: pytest.MonkeyPatch) -> None:
    """The service offers the pinned cuda backend on any host."""
    monkeypatch.setattr(voice_service, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(voice_service, "platform", SimpleNamespace(machine=lambda: "AMD64"))


class Factory:
    """Builds engines over the recording spawn; `engines` holds every engine built."""

    def __init__(self, **extra: Any) -> None:
        self.recorder = Recorder()
        self.engines: list[VoiceEngine] = []
        self._extra = extra

    def __call__(self, data_dir: Path, **kwargs: Any) -> VoiceEngine:
        engine = VoiceEngine(
            data_dir, python=sys.executable, spawn=self.recorder, **kwargs, **self._extra
        )
        self.engines.append(engine)
        return engine


class Starting:
    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self) -> None:
        self.calls += 1


def make(
    data: Path, settings: Mapping[str, object], idle_close_s: float = 300.0, **extra: Any
) -> tuple[VoiceService, Factory]:
    factory = Factory(**extra)
    service = VoiceService(
        data,
        read_settings=lambda: settings,
        worker=FAKE,
        engine=factory,
        idle_close_s=idle_close_s,
    )
    return service, factory


async def next_result(service: VoiceService) -> VoiceResult:
    return await asyncio.wait_for(service.results().get(), HANG_GUARD_S)


async def cancel_capture(service: VoiceService, capture_id: str) -> None:
    await service.cancel(capture_id)
    assert await next_result(service) == VoiceResult("cancelled", capture_id)
    service.finished()


async def until(predicate: Any) -> None:
    async with asyncio.timeout(HANG_GUARD_S):
        while not predicate():
            await asyncio.sleep(0)


CPU_OK = {"voice.model": "fake-ok", "voice.backend": "cpu"}


async def test_the_first_begin_starts_the_engine_once(tmp_path: Path) -> None:
    install_model(tmp_path, "fake-ok")
    service, factory = make(tmp_path, dict(CPU_OK))
    starting = Starting()
    try:
        capturing = await service.begin(CAPTURE, starting=starting)
        assert capturing == Capturing("fake-ok", "cpu", None, "auto", False, None)
        assert starting.calls == 1 and len(factory.recorder.calls) == 1
        await service.audio(CAPTURE, CHUNK)
        await service.end(CAPTURE)
        assert [(await next_result(service)).kind for _ in range(3)] == [
            "partial",
            "final",
            "done",
        ]
        service.finished()

        await service.begin(OTHER, starting=starting)
        assert starting.calls == 1 and len(factory.recorder.calls) == 1
        assert len(factory.engines) == 1
    finally:
        await service.close()


async def test_pair_change_replaces_the_engine(tmp_path: Path) -> None:
    install_model(tmp_path, "fake-ok")
    install_model(tmp_path, "fake-stderr")
    settings: dict[str, object] = dict(CPU_OK)
    service, factory = make(tmp_path, settings)
    starting = Starting()
    try:
        await service.begin(CAPTURE, starting=starting)
        old = factory.engines[0].pid
        assert old is not None
        await cancel_capture(service, CAPTURE)

        settings["voice.model"] = "fake-stderr"
        capturing = await service.begin(OTHER, starting=starting)
        assert capturing.model == "fake-stderr" and starting.calls == 2
        assert [call.argv[-2:] for call in factory.recorder.calls] == [
            ["fake-ok", "cpu"],
            ["fake-stderr", "cpu"],
        ]
        assert gone(old) and not factory.engines[0].running
    finally:
        await service.close()


async def test_an_unavailable_language_runs_as_auto(tmp_path: Path) -> None:
    install_model(tmp_path, "fake-ok")  # the pins give every mode ("en", "ja")
    settings: dict[str, object] = {**CPU_OK, "voice.dictation_language": "fr"}
    service, _ = make(tmp_path, settings)
    try:
        capturing = await service.begin(CAPTURE, starting=Starting())
        assert (capturing.language, capturing.language_unavailable) == ("auto", True)
        view = await service.status()
        assert (view["language"], view["language_unavailable"]) == ("auto", True)
        await cancel_capture(service, CAPTURE)

        settings["voice.dictation_language"] = "ja"
        capturing = await service.begin(OTHER, starting=Starting())
        assert (capturing.language, capturing.language_unavailable) == ("ja", False)
    finally:
        await service.close()


async def test_stale_results_are_drained(tmp_path: Path) -> None:
    install_model(tmp_path, "fake-ok")
    service, _ = make(tmp_path, dict(CPU_OK))
    try:
        await service.begin(CAPTURE, starting=Starting())
        await cancel_capture(service, CAPTURE)
        service.results().put_nowait(VoiceResult("error", None, code="worker_crashed"))

        await service.begin(OTHER, starting=Starting())
        await service.audio(OTHER, CHUNK)
        assert await next_result(service) == VoiceResult("partial", OTHER, 0, 1, "partial-1")
    finally:
        await service.close()


async def test_finished_closes_the_engine_after_the_idle_delay(tmp_path: Path) -> None:
    install_model(tmp_path, "fake-ok")
    service, factory = make(tmp_path, dict(CPU_OK), idle_close_s=0.02)
    try:
        await service.begin(CAPTURE, starting=Starting())
        pid = factory.engines[0].pid
        assert pid is not None
        await cancel_capture(service, CAPTURE)
        await until(factory.recorder.all_reaped)
        assert gone(pid) and not factory.engines[0].running
    finally:
        await service.close()


async def test_a_begin_cancels_the_idle_close(tmp_path: Path) -> None:
    install_model(tmp_path, "fake-ok")
    service, factory = make(tmp_path, dict(CPU_OK), idle_close_s=0.02)
    try:
        await service.begin(CAPTURE, starting=Starting())
        pid = factory.engines[0].pid
        await cancel_capture(service, CAPTURE)  # arms the 0.02 s close
        await service.begin(OTHER, starting=Starting())
        waiting = asyncio.ensure_future(service.results().get())
        # Must time out: past the idle delay, the engine still holds the open capture.
        await asyncio.wait([waiting], timeout=0.05)
        assert not waiting.done()
        waiting.cancel()
        await asyncio.wait([waiting])
        assert factory.engines[0].running and factory.engines[0].pid == pid
        assert len(factory.recorder.calls) == 1
    finally:
        await service.close()


async def test_a_failed_begin_still_closes_the_started_engine_when_idle(tmp_path: Path) -> None:
    install_model(tmp_path, "fake-ok")
    service, factory = make(tmp_path, dict(CPU_OK), idle_close_s=0.02)
    try:
        # The engine starts, then its own begin refuses the capture id.
        with pytest.raises(VoiceError, match="invalid_capture"):
            await service.begin("not-a-uuid", starting=Starting())
        engine = factory.engines[0]
        pid = engine.pid
        assert pid is not None and engine.running
        await until(factory.recorder.all_reaped)
        assert gone(pid) and not engine.running
    finally:
        await service.close()


def gpu_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """fake-gpu-fails stands for the GPU pair's model, fake-ok for the CPU ranking's."""
    monkeypatch.setattr(
        vm, "RANKING", {"cpu": ("fake-ok",), "cuda": ("fake-gpu-fails",), "metal": ()}
    )
    install_cuda(tmp_path)
    install_model(tmp_path, "fake-gpu-fails")
    install_model(tmp_path, "fake-ok")
    return {"voice.model": "fake-gpu-fails", "voice.backend": "cuda"}


FELL_BACK = Fallback("cuda", "backend_unavailable", "no_device")


async def test_clear_fallback_closes_an_idle_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, factory = make(tmp_path, gpu_fails(tmp_path, monkeypatch))
    try:
        capturing = await service.begin(CAPTURE, starting=Starting())
        assert capturing == Capturing("fake-ok", "cpu", None, "auto", False, FELL_BACK)
        await cancel_capture(service, CAPTURE)
        assert await service.status() == {
            "state": "ready",
            "running": True,
            "model": "fake-ok",
            "backend": "cpu",
            "device": None,
            "languages": ["auto", "en", "ja"],
            "language": "auto",
            "language_unavailable": False,
            "models": ["fake-ok", "fake-gpu-fails"],
            "backends": ["cuda", "cpu"],
            "fallback": {"backend": "cuda", "code": "backend_unavailable", "detail": "no_device"},
            "cpu_model": "fake-ok",
            "slow_on_cpu": False,
        }
        pid = factory.engines[0].pid
        assert pid is not None

        view = await service.clear_fallback()
        assert gone(pid) and factory.recorder.all_reaped()
        assert (view["fallback"], view["running"]) == (None, False)
        assert (view["model"], view["backend"], view["slow_on_cpu"]) == (
            "fake-gpu-fails",
            "cuda",
            False,
        )
    finally:
        await service.close()


async def test_clear_fallback_during_a_capture_closes_at_finished(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, factory = make(tmp_path, gpu_fails(tmp_path, monkeypatch))
    try:
        await service.begin(CAPTURE, starting=Starting())
        engine = factory.engines[0]
        pid = engine.pid
        view = await service.clear_fallback()
        assert view["fallback"] is None and engine.running and engine.pid == pid
        await cancel_capture(service, CAPTURE)  # the idle delay is 300 s: this closes at once
        await until(factory.recorder.all_reaped)
        assert not engine.running

        await service.begin(OTHER, starting=Starting())  # Try GPU again: cuda first
        assert factory.recorder.backends() == ["cuda", "cpu", "cuda", "cpu"]
    finally:
        await service.close()


class HoldsCpuLaunch(Recorder):
    """Holds the second spawn, the CPU launch after the failed GPU one, until `release`."""

    def __init__(self) -> None:
        super().__init__()
        self.held = asyncio.Event()
        self.release = asyncio.Event()

    async def __call__(
        self, argv: list[str], *, cwd: Any, env: dict[str, str], memory_limit_bytes: int | None
    ) -> Any:
        if len(self.calls) == 1:
            self.held.set()
            await self.release.wait()
        return await super().__call__(argv, cwd=cwd, env=env, memory_limit_bytes=memory_limit_bytes)


async def test_clear_fallback_during_a_begin_leaves_the_capture_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, factory = make(tmp_path, gpu_fails(tmp_path, monkeypatch))
    factory.recorder = holds = HoldsCpuLaunch()
    try:
        beginning = asyncio.create_task(service.begin(CAPTURE, starting=Starting()))
        await asyncio.wait_for(holds.held.wait(), HANG_GUARD_S)
        engine = factory.engines[0]
        assert engine.fallback == FELL_BACK

        # The begin holds the service lock. Without the check, this waits for it.
        clearing = asyncio.create_task(service.clear_fallback())
        await asyncio.sleep(0)
        holds.release.set()
        capturing = await asyncio.wait_for(beginning, HANG_GUARD_S)
        view = await asyncio.wait_for(clearing, HANG_GUARD_S)
        assert capturing.fallback is None and view["fallback"] is None
        assert engine.running and (await service.status())["fallback"] is None

        await cancel_capture(service, CAPTURE)  # the restart flag closes the engine at once
        await until(factory.recorder.all_reaped)
        assert not engine.running
    finally:
        await service.close()


async def test_status_on_an_empty_folder(tmp_path: Path) -> None:
    # No model: neither a slow-on-CPU hint nor an unavailable stored language.
    service, factory = make(tmp_path, {"voice.dictation_language": "ja"})
    assert await service.status() == {
        "state": "setup_needed",
        "running": False,
        "model": None,
        "backend": "cpu",
        "device": None,
        "languages": ["auto"],
        "language": "auto",
        "language_unavailable": False,
        "models": [],
        "backends": ["cpu"],
        "fallback": None,
        "cpu_model": None,
        "slow_on_cpu": False,
    }
    assert factory.recorder.calls == []
    with pytest.raises(VoiceError, match="setup_needed"):
        await service.begin(CAPTURE, starting=Starting())
    await service.close()


async def test_status_without_the_cpu_model_is_slow_on_cpu(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = gpu_fails(tmp_path, monkeypatch)
    (vm.model_dir(tmp_path, "fake-ok") / "weights.bin").unlink()
    service, _ = make(tmp_path, settings)
    try:
        capturing = await service.begin(CAPTURE, starting=Starting())
        assert (capturing.model, capturing.backend) == ("fake-gpu-fails", "cpu")
        view = await service.status()
        assert (view["slow_on_cpu"], view["cpu_model"]) == (True, None)
    finally:
        await service.close()


@pytest.mark.parametrize(
    "read",
    [
        pytest.param({"boom": True}, id="raises"),
        pytest.param(
            {"voice.model": 5, "voice.backend": "nope", "voice.dictation_language": None},
            id="wrong-values",
        ),
    ],
)
async def test_unreadable_settings_give_defaults(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    read: dict[str, object],
) -> None:
    monkeypatch.setattr(vm, "RANKING", {"cpu": ("fake-ok",), "cuda": (), "metal": ()})
    install_model(tmp_path, "fake-ok")

    def read_settings() -> dict[str, object]:
        if "boom" in read:
            raise OSError("settings unreadable")
        return read

    pause_frames: list[int] = []
    original_begin = VoiceEngine.begin

    async def spy_begin(self: VoiceEngine, capture_id: str, language: str, **kwargs: Any) -> None:
        pause_frames.append(kwargs["pause_frames"])
        await original_begin(self, capture_id, language, **kwargs)

    monkeypatch.setattr(VoiceEngine, "begin", spy_begin)

    factory = Factory()
    service = VoiceService(tmp_path, read_settings=read_settings, worker=FAKE, engine=factory)
    try:
        capturing = await service.begin(CAPTURE, starting=Starting())
        assert capturing == Capturing("fake-ok", "cpu", None, "auto", False, None)
        assert factory.engines[0]._want_model is None
        assert factory.engines[0]._want_backend == "auto"
        assert pause_frames == [END_SILENCE_FRAMES]
    finally:
        await service.close()
    if "boom" in read:
        assert "voice settings read failed: OSError" in caplog.text


def _pause_values() -> tuple[object, ...]:
    allowed = KEYS["voice.pause_s"].allowed
    assert allowed is not None, "voice.pause_s has no allowed values"
    return allowed


PAUSE_VALUES = _pause_values()


@pytest.mark.parametrize("pause_s", PAUSE_VALUES)
async def test_pause_s_setting_reaches_the_engines_begin_as_pause_frames(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pause_s: float
) -> None:
    expected_frames = {1.2: 60, 2: 100, 3: 150, 4: 200, 5: 250}
    assert set(PAUSE_VALUES) == set(expected_frames)
    expected = expected_frames[pause_s]
    install_model(tmp_path, "fake-ok")
    pause_frames: list[int] = []
    original_begin = VoiceEngine.begin

    async def spy_begin(self: VoiceEngine, capture_id: str, language: str, **kwargs: Any) -> None:
        pause_frames.append(kwargs["pause_frames"])
        await original_begin(self, capture_id, language, **kwargs)

    written: list[tuple[bytes, bytes]] = []
    original_write = VoiceEngine._write

    def spy_write(self: VoiceEngine, kind: bytes, payload: bytes) -> None:
        written.append((kind, payload))
        original_write(self, kind, payload)

    monkeypatch.setattr(VoiceEngine, "begin", spy_begin)
    monkeypatch.setattr(VoiceEngine, "_write", spy_write)
    service, _ = make(tmp_path, {**CPU_OK, "voice.pause_s": pause_s})
    try:
        await service.begin(CAPTURE, starting=Starting())
        assert pause_frames == [expected]
        assert len(written) == 1 and written[0][0] == b"B"
        _, _, frames = parse_begin(written[0][1], ("auto",))
        assert frames == expected
    finally:
        await service.close()


async def test_close_twice_leaves_no_process(tmp_path: Path) -> None:
    install_model(tmp_path, "fake-ok")
    service, factory = make(tmp_path, dict(CPU_OK))
    await service.begin(CAPTURE, starting=Starting())
    pid = factory.engines[0].pid
    assert pid is not None
    await service.close()
    await service.close()
    assert gone(pid) and factory.recorder.all_reaped()
    service.finished()
    with pytest.raises(VoiceError, match="not_running"):  # no worker after shutdown
        await service.begin(THIRD, starting=Starting())
    assert len(factory.recorder.calls) == 1


async def test_close_cancels_a_start(tmp_path: Path) -> None:
    install_model(tmp_path, "fake-hang-ready")
    settings = {"voice.model": "fake-hang-ready", "voice.backend": "cpu"}
    service, factory = make(tmp_path, settings, ready_timeout_s=HANG_GUARD_S)
    beginning = asyncio.create_task(service.begin(CAPTURE, starting=Starting()))
    await asyncio.wait_for(factory.recorder.spawned.wait(), HANG_GUARD_S)
    await service.close()
    with pytest.raises(VoiceError, match="not_running"):
        await asyncio.wait_for(beginning, HANG_GUARD_S)
    assert gone(factory.recorder.children[0].pid) and factory.recorder.all_reaped()


async def test_log_holds_no_capture_text(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    install_model(tmp_path, "fake-ok")
    service, _ = make(tmp_path, dict(CPU_OK))
    try:
        await service.begin(CAPTURE, starting=Starting())
        await service.audio(CAPTURE, CHUNK)
        await service.end(CAPTURE)
        assert [(await next_result(service)).text for _ in range(3)] == [
            "partial-1",
            "final-16000",
            "",
        ]
        service.finished()
    finally:
        await service.close()
    for text in ("partial-", "final-", CAPTURE):
        assert text not in caplog.text
