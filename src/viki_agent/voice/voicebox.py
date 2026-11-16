"""The Voicebox client: status and speech, routed by `voice.speech_engine` to the user's own
Voicebox app (ADR-047) or to VIKI's own voice pack, never probing either before it is chosen.

Settings are read at every call. One synthesis runs at a time: a newer speech request waits for
it and supersedes an older waiter, which fails with `voicebox_busy`.
"""

from __future__ import annotations

import asyncio
import io
import json
import time
import unicodedata
import uuid
import wave
from collections.abc import Callable, Mapping
from typing import Protocol

from viki_agent.app.settings import _refuse_constant
from viki_agent.voice import pack_voices, voice_pack, voicebox_http
from viki_agent.voice.voicebox_http import (
    LANGUAGES,
    MAX_JSON_BYTES,
    MAX_WAV_BYTES,
    SPEECH_TIMEOUT_S,
    STATUS_TIMEOUT_S,
    VoiceboxError,
    loopback,
)

MAX_PROFILES = 200

# The status state for a /profiles call that failed with each code; any other code is unexpected.
_STATES = {
    "voicebox_off": "no_settings",
    "voicebox_unreachable": "unreachable",
    "voicebox_timeout": "unreachable",
}


def _json(data: bytes) -> object:
    """The parsed body, or None for bad UTF-8, broken JSON, NaN/Infinity or too deep a nest."""
    try:
        return json.loads(data.decode("utf-8"), parse_constant=_refuse_constant)
    except ValueError, RecursionError:
        return None


def _canonical_uuid(value: str) -> bool:
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False


def _profile(item: object) -> dict[str, str] | None:
    """The three fields VIKI keeps of one /profiles item, or None if any is malformed."""
    if not isinstance(item, dict):
        return None
    profile_id, name, language = item.get("id"), item.get("name"), item.get("language")
    if (
        not isinstance(profile_id, str)
        or not _canonical_uuid(profile_id)
        or not isinstance(name, str)
        or not 1 <= len(name) <= 100
        or any(unicodedata.category(ch) == "Cc" for ch in name)
        or not isinstance(language, str)
        or language not in LANGUAGES
    ):
        return None
    return {"id": profile_id, "name": name, "language": language}


class PackTarget(Protocol):
    """VIKI's own natural-voices server, as the "pack" engine needs it."""

    async def address(self) -> tuple[str, int]:
        """The server's loopback (host, port); raises `VoiceboxError("voicebox_pack_missing")`
        or `VoiceboxError("voicebox_pack_failed")` if it isn't up."""
        ...

    async def profile(self, language: str, voice: str) -> str:
        """The profile id to speak `language` with `voice`."""
        ...

    async def kokoro_ready(self) -> bool:
        """Whether Kokoro's pinned files are already on disk."""
        ...

    def note_speed(self, model: str, factor: float) -> None:
        """Keeps the latest speech-time-over-audio-time factor for `model`."""
        ...


def _speech_answer(code: int, data: bytes) -> bytes:
    """WAV bytes from one `/generate/stream` answer, or a VoiceboxError with its code."""
    if code == 200 and data.startswith(b"RIFF") and data[8:12] == b"WAVE":
        return data
    if code == 200:
        raise VoiceboxError("voicebox_invalid")
    if code == 404:
        raise VoiceboxError("voicebox_profile_missing")
    answer = _json(data) if code == 400 else None
    detail = answer.get("detail") if isinstance(answer, dict) else None
    if isinstance(detail, str) and "not downloaded" in detail:
        raise VoiceboxError("voicebox_model_missing")
    raise VoiceboxError("voicebox_refused")


def _wav_seconds(data: bytes) -> float | None:
    """`data`'s duration as a PCM WAV, or None if it cannot be read as one."""
    try:
        with wave.open(io.BytesIO(data)) as wav_file:
            return wav_file.getnframes() / wav_file.getframerate()
    except Exception:
        return None


class Voicebox:
    def __init__(
        self,
        read_settings: Callable[[], Mapping[str, object]],
        *,
        request: Callable[
            [str, int, str, str, bytes | None, float, int], tuple[int, bytes]
        ] = voicebox_http.request,
        pack: PackTarget | None = None,
    ) -> None:
        self._read_settings = read_settings
        self._request = request
        self._pack = pack
        self._lock = asyncio.Lock()
        self._latest = 0  # the newest speech ticket; a waiter holding an older one is superseded

    def _settings(self) -> Mapping[str, object]:
        """The settings; none at all when they cannot be read, like `VoiceService._settings`."""
        try:
            return self._read_settings()
        except Exception:
            return {}

    def engine(self) -> str:
        """The effective speech engine ("system" | "pack" | "voicebox") for the current settings."""
        return voice_pack.speech_engine(self._settings())

    def _voicebox_address(self) -> tuple[str, int]:
        """The user's own Voicebox app's loopback address, or `voicebox_off` with no network."""
        address = loopback(self._settings().get("voice.voicebox_url"))
        if address is None:
            raise VoiceboxError("voicebox_off")
        return address

    async def _call(
        self,
        address: tuple[str, int],
        method: str,
        path: str,
        body: bytes | None,
        timeout: float,
        limit: int,
    ) -> tuple[int, bytes]:
        """(status, body) of one call to `address`."""
        host, port = address
        try:
            # An expired deadline frees the gate; the reader thread stops at request's
            # own deadline of the same length. Voicebox cannot cancel a synthesis anyway.
            async with asyncio.timeout(timeout):
                return await asyncio.to_thread(
                    self._request, host, port, method, path, body, timeout, limit
                )
        except TimeoutError as error:
            raise VoiceboxError("voicebox_timeout") from error

    async def status(self) -> dict[str, object]:
        """{"state", "gpu", "profiles"}; `ready` only after a well-formed /profiles answer."""
        if self.engine() != "voicebox":
            return {"state": "no_settings", "gpu": None, "profiles": []}
        try:
            address = self._voicebox_address()
            code, data = await self._call(
                address, "GET", "/profiles", None, STATUS_TIMEOUT_S, MAX_JSON_BYTES
            )
        except VoiceboxError as error:
            return {"state": _STATES.get(error.code, "unexpected"), "gpu": None, "profiles": []}
        items = _json(data) if code == 200 else None
        if not isinstance(items, list):
            return {"state": "unexpected", "gpu": None, "profiles": []}
        profiles = [profile for item in items if (profile := _profile(item)) is not None]
        return {
            "state": "ready",
            "gpu": await self._gpu(address),
            "profiles": profiles[:MAX_PROFILES],
        }

    async def _gpu(self, address: tuple[str, int]) -> bool | None:
        """/health's `gpu_available`, or None for any failure or other shape."""
        try:
            code, data = await self._call(
                address, "GET", "/health", None, STATUS_TIMEOUT_S, MAX_JSON_BYTES
            )
        except VoiceboxError:
            return None
        health = _json(data) if code == 200 else None
        gpu = health.get("gpu_available") if isinstance(health, dict) else None
        return gpu if isinstance(gpu, bool) else None

    async def speech(self, text: str, language: str) -> bytes:
        """WAV bytes of `text` in the chosen voice, or a VoiceboxError with its code."""
        engine = self.engine()
        if engine == "system":
            raise VoiceboxError("voicebox_off")
        if engine == "voicebox":
            return await self._voicebox_speech(text, language)
        pack = self._pack
        if pack is None:
            raise VoiceboxError("voicebox_off")
        return await self._pack_speech(pack, text, language)

    async def _voicebox_speech(self, text: str, language: str) -> bytes:
        """`speech()`'s "voicebox" path: the user's own Voicebox app."""
        settings = self._settings()
        profile = settings.get("voice.voicebox_profile")
        if not isinstance(profile, str):
            raise VoiceboxError("voicebox_off")
        body = json.dumps(
            {
                "profile_id": profile,
                "text": text,
                "language": language,
                "engine": None,  # Voicebox uses the profile's own engine
                "model_size": settings.get("voice.voicebox_model_size"),
            }
        ).encode()
        self._latest += 1
        ticket = self._latest
        async with self._lock:
            if ticket != self._latest:
                raise VoiceboxError("voicebox_busy")
            code, data = await self._call(
                self._voicebox_address(),
                "POST",
                "/generate/stream",
                body,
                SPEECH_TIMEOUT_S,
                MAX_WAV_BYTES,
            )
        return _speech_answer(code, data)

    async def _pack_speech(self, pack: PackTarget, text: str, language: str) -> bytes:
        """`speech()`'s "pack" path: VIKI's own voice pack, at its own address and profile."""
        self._latest += 1
        ticket = self._latest
        async with self._lock:
            if ticket != self._latest:
                raise VoiceboxError("voicebox_busy")
            voice = pack_voices.voice_for(self._settings(), language, await pack.kokoro_ready())
            if voice is None:
                raise VoiceboxError("voicebox_profile_missing")
            model = pack_voices.model_of(voice) or "qwen"
            # profile() before address(): shrinks the window where a crash and restart between
            # the two sends text to a freed port.
            profile = await pack.profile(language, voice)
            address = await pack.address()
            body = json.dumps(
                {
                    "profile_id": profile,
                    "text": text,
                    "language": language,
                    "engine": None,
                    "model_size": None if model == "kokoro" else voice_pack.MODEL_SIZE,
                }
            ).encode()
            started = time.monotonic()
            code, data = await self._call(
                address, "POST", "/generate/stream", body, SPEECH_TIMEOUT_S, MAX_WAV_BYTES
            )
            elapsed = time.monotonic() - started
        answer = _speech_answer(code, data)
        seconds = _wav_seconds(data)
        if seconds is not None and seconds >= 1:
            pack.note_speed(model, elapsed / seconds)
        return answer

    async def warm(self) -> None:
        """Pre-starts the pack server and its "en" profile, so the first real pack speech request
        does not pay that cold-start cost. A no-op outside the "pack" engine, and skipped
        entirely while a real synthesis already holds the lock: unlike `_pack_speech`, this never
        bumps `_latest`, so it can never be the reason a waiting real request sees
        `voicebox_busy`."""
        pack = self._pack
        if self.engine() != "pack" or pack is None or self._lock.locked():
            return
        async with self._lock:
            voice = pack_voices.voice_for(self._settings(), "en", await pack.kokoro_ready())
            if voice is None:
                raise VoiceboxError("voicebox_profile_missing")
            address = await pack.address()
            profile = await pack.profile("en", voice)
            body = json.dumps(
                {
                    "profile_id": profile,
                    "text": "Ready.",
                    "language": "en",
                    "engine": None,
                    "model_size": voice_pack.MODEL_SIZE,
                }
            ).encode()
            await self._call(
                address, "POST", "/generate/stream", body, SPEECH_TIMEOUT_S, MAX_WAV_BYTES
            )
