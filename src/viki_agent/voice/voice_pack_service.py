"""VoicePack: the wire-facing facade over `PackInstaller` and `PackServer`. `status()` merges
both into the one dict the frontend polls. Every action that changes install state runs under a
single `asyncio.Lock`, so install, repair, removal and cancellation do not overlap. The installer
separately owns a spawn across shutdown; closed facades refuse queued and later actions.
`check()` only probes the GPU and changes no install
state, so it stays outside that lock too, alongside the speech path: `address()`, `profile()` and
`close()`. `_held` closes a narrower window: while `remove()` or `repair()` sits between
`server.stop()` and the pack's files actually settling, a speech call must not start a server
from (or race) a half-settled pack, so `address()`/`profile()` also refuse while it is set.
`kokoro_ready()`, `note_speed()` and `note_fallback()` stay outside the lock too: they read
installed files or update in-memory counters, nothing that needs serializing against an install.
"""

from __future__ import annotations

import asyncio
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from viki_agent.voice import pack_voices, voice_pack
from viki_agent.voice.voice_pack_install import PackInstaller, PackRefused
from viki_agent.voice.voice_pack_server import PackServer
from viki_agent.voice.voicebox_http import VoiceboxError


class _Installer(Protocol):
    async def status(self) -> dict[str, Any]: ...
    async def check(self, *, again: bool = False) -> None: ...
    async def install(self, *, repair: bool = False) -> None: ...
    async def cancel(self) -> None: ...
    async def remove(self) -> None: ...
    async def close(self) -> None: ...


class _Server(Protocol):
    def status(self) -> dict[str, Any]: ...
    async def address(self) -> tuple[str, int]: ...
    async def profile(self, language: str, voice: str) -> str: ...
    def run(self) -> int: ...
    def reset(self) -> None: ...
    async def stop(self) -> None: ...
    async def close(self) -> None: ...


class VoicePack:
    def __init__(self, data_dir: Path, installer: _Installer, server: _Server) -> None:
        self._data_dir = data_dir
        self._installer = installer
        self._server = server
        self._lock = asyncio.Lock()
        self._held = False  # remove/repair: no speech start until the files are settled
        self._closed = False
        self._speed: dict[str, float | None] = {"kokoro": None, "qwen": None}
        self._speed_seen: set[tuple[str, int]] = set()  # (model, run) already past its first call
        self._fallbacks: deque[dict[str, str]] = deque(maxlen=3)  # oldest first; status() reverses

    @classmethod
    def create(cls, data_dir: Path) -> VoicePack:
        return cls(data_dir, PackInstaller(data_dir), PackServer(data_dir))

    async def status(self) -> dict[str, Any]:
        installer = await self._installer.status()
        if installer["state"] == "installed":
            server = self._server.status()
            state, error = server["state"], server["error"]
        else:
            state, error = installer["state"], installer["error"]
        free = await asyncio.to_thread(voice_pack.free_bytes, self._data_dir)
        return {
            "state": state,
            "reason": installer["reason"],
            "error": error,
            "progress": installer["progress"],
            "download_bytes": voice_pack.DOWNLOAD_BYTES,
            "disk_bytes": voice_pack.DISK_BYTES,
            "need_bytes": voice_pack.NEED_BYTES,
            "free_bytes": free,
            "gpu": installer["gpu"],
            "kokoro": await self.kokoro_ready(),
            "speed": dict(self._speed),
            "fallbacks": list(reversed(self._fallbacks)),
        }

    # --- actions, serialized: PackInstaller.install() checks its state across an await ----------

    def _require_open(self) -> None:
        if self._closed:
            raise PackRefused("pack_busy")

    async def check(self) -> dict[str, Any]:
        self._require_open()
        await self._installer.check(again=True)
        return await self.status()

    async def install(self) -> dict[str, Any]:
        async with self._lock:
            self._require_open()
            await self._installer.install()
            self._require_open()
            self._server.reset()
            return await self.status()

    async def repair(self) -> dict[str, Any]:
        async with self._lock:
            self._require_open()
            self._held = True
            try:
                await self._server.stop()
                self._require_open()
                self._server.reset()
                await self._installer.install(repair=True)
            finally:
                self._held = False
            return await self.status()

    async def cancel(self) -> dict[str, Any]:
        async with self._lock:
            self._require_open()
            await self._installer.cancel()
            return await self.status()

    async def remove(self) -> dict[str, Any]:
        async with self._lock:
            self._require_open()
            self._held = True
            try:
                await self._server.stop()
                self._require_open()
                await self._installer.remove()
                self._require_open()
                self._server.reset()
            finally:
                self._held = False
            return await self.status()

    async def retry(self) -> dict[str, Any]:
        async with self._lock:
            self._require_open()
            self._server.reset()
            return await self.status()

    # --- speech, outside the action lock: it must stay responsive while an install runs ---------

    async def kokoro_ready(self) -> bool:
        """Whether Kokoro's pinned files are already on disk, read off the filesystem in a
        thread."""
        return await asyncio.to_thread(voice_pack.kokoro_installed, self._data_dir)

    async def _require_available(self) -> None:
        """Raise unless the pack is installed and its files are not mid remove/repair. A failed
        or cancelled install (e.g. an "Add the fast voice" over a working pack) still counts as
        available when the previously installed files are still on disk; a failed repair or
        fresh install has no such files, so it still refuses."""
        if self._closed:
            raise VoiceboxError("voicebox_pack_failed")
        if self._held:
            raise VoiceboxError("voicebox_pack_missing")
        status = await self._installer.status()
        if self._closed:
            raise VoiceboxError("voicebox_pack_failed")
        state = status["state"]
        available = state == "installed" or (
            state == "install_failed"
            and await asyncio.to_thread(voice_pack.installed, self._data_dir)
        )
        if not available or self._held:
            raise VoiceboxError("voicebox_pack_missing")

    async def address(self) -> tuple[str, int]:
        await self._require_available()
        return await self._server.address()

    async def profile(self, language: str, voice: str) -> str:
        await self._require_available()
        if pack_voices.model_of(voice) == "kokoro":
            ready = await self.kokoro_ready()
            if self._held:
                raise VoiceboxError("voicebox_pack_missing")
            if not ready:
                raise VoiceboxError("voicebox_model_missing")
        return await self._server.profile(language, voice)

    def note_speed(self, model: str, factor: float) -> None:
        """Keeps the latest speech-time-over-audio-time factor per model; each model's first
        synthesis in a server run is dropped, since loading the model inflates it."""
        key = (model, self._server.run())
        if key not in self._speed_seen:
            self._speed_seen.add(key)
            return
        self._speed[model] = round(factor, 3)

    def note_fallback(self, code: str) -> None:
        """Keeps the last three hands-free fallback reports; status() lists them newest first."""
        at = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
        self._fallbacks.append({"code": code, "at": at})

    async def close(self) -> None:
        self._closed = True
        try:
            await self._server.close()
        finally:
            await self._installer.close()
