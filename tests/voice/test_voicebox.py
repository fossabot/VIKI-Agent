"""Voicebox: status filtering, the speech mapping and the one-at-a-time gate, on a fake request."""

from __future__ import annotations

import asyncio
import io
import json
import logging
import threading
import wave
from collections.abc import Mapping

import pytest

from viki_agent.voice import voicebox
from viki_agent.voice.voicebox import Voicebox
from viki_agent.voice.voicebox_http import (
    MAX_JSON_BYTES,
    MAX_WAV_BYTES,
    SPEECH_TIMEOUT_S,
    STATUS_TIMEOUT_S,
    VoiceboxError,
)

HANG_GUARD_S = 5.0

URL = "http://localhost:24680"  # the fake request never connects
PROFILE = "6f1c2a4e-9b3d-4c5e-8f7a-1b2c3d4e5f60"
SETTINGS: Mapping[str, object] = {
    "voice.voicebox_url": URL,
    "voice.voicebox_profile": PROFILE,
    "voice.voicebox_model_size": "0.6B",
}
PACK_SETTINGS: Mapping[str, object] = {"voice.speech_engine": "pack"}
WAV = b"RIFF\x24\x00\x00\x00WAVEfmt "


def _wav(seconds: float, *, rate: int = 24_000) -> bytes:
    """PCM16 mono WAV bytes of `seconds` length, as Voicebox's real output is shaped."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(rate)
        wav_file.writeframes(b"\x00\x00" * round(rate * seconds))
    return buffer.getvalue()


Answer = tuple[int, bytes] | VoiceboxError
Call = tuple[str, int, str, str, bytes | None, float, int]


class FakeRequest:
    """Stands in for `voicebox_http.request`: records every call and answers by path.

    With `hold`, the first call sets `entered`, then blocks until `hold` is set (HANG_GUARD_S at
    most). `log` holds ("enter" | "exit", speech text or path), so a test sees calls overlap.
    """

    def __init__(self, answers: Mapping[str, Answer], hold: threading.Event | None = None) -> None:
        self.answers = answers
        self.hold = hold
        self.entered = threading.Event()
        self.calls: list[Call] = []
        self.log: list[tuple[str, str]] = []

    def __call__(
        self,
        host: str,
        port: int,
        method: str,
        path: str,
        body: bytes | None,
        timeout: float,
        limit: int,
    ) -> tuple[int, bytes]:
        self.calls.append((host, port, method, path, body, timeout, limit))
        label = json.loads(body)["text"] if body is not None else path
        self.log.append(("enter", label))
        if self.hold is not None and len(self.calls) == 1:
            self.entered.set()
            self.hold.wait(HANG_GUARD_S)
        self.log.append(("exit", label))
        answer = self.answers[path]
        if isinstance(answer, VoiceboxError):
            raise answer
        return answer


class FakePack:
    """Stands in for `PackTarget`: records the (language, voice) pairs it was asked for and the
    speed reports it was given, answers a fixed address/profile/readiness, or raises the
    `VoiceboxError` it was given, unchanged."""

    def __init__(
        self,
        address: tuple[str, int] | VoiceboxError = ("localhost", 9100),
        profile: str | VoiceboxError = "pack-narrator",
        kokoro_ready: bool = False,
    ) -> None:
        self._address = address
        self._profile = profile
        self._kokoro_ready = kokoro_ready
        self.profile_calls: list[tuple[str, str]] = []
        self.address_calls = 0
        self.speed_calls: list[tuple[str, float]] = []

    async def address(self) -> tuple[str, int]:
        self.address_calls += 1
        if isinstance(self._address, VoiceboxError):
            raise self._address
        return self._address

    async def profile(self, language: str, voice: str) -> str:
        self.profile_calls.append((language, voice))
        if isinstance(self._profile, VoiceboxError):
            raise self._profile
        return self._profile

    async def kokoro_ready(self) -> bool:
        return self._kokoro_ready

    def note_speed(self, model: str, factor: float) -> None:
        self.speed_calls.append((model, factor))


def _box(fake: FakeRequest, settings: Mapping[str, object] = SETTINGS) -> Voicebox:
    return Voicebox(lambda: settings, request=fake)


# --- speech ---------------------------------------------------------------------------------


async def test_speech_sends_engine_null() -> None:
    fake = FakeRequest({"/generate/stream": (200, WAV)})

    assert await _box(fake).speech("Hello there.", "en") == WAV

    body = (
        b'{"profile_id": "6f1c2a4e-9b3d-4c5e-8f7a-1b2c3d4e5f60", "text": "Hello there.", '
        b'"language": "en", "engine": null, "model_size": "0.6B"}'
    )
    assert fake.calls == [
        ("localhost", 24680, "POST", "/generate/stream", body, SPEECH_TIMEOUT_S, MAX_WAV_BYTES)
    ]


async def test_system_engine_is_off_and_calls_nothing() -> None:
    """The "system" engine wins even with a profile and URL otherwise ready for "voicebox"."""
    fake = FakeRequest({})
    settings = {**SETTINGS, "voice.speech_engine": "system"}

    with pytest.raises(VoiceboxError) as excinfo:
        await _box(fake, settings).speech("Hi.", "en")

    assert excinfo.value.code == "voicebox_off"
    assert fake.calls == []


NO_PROFILE = [
    pytest.param(
        {k: v for k, v in SETTINGS.items() if k != "voice.voicebox_profile"}, id="missing"
    ),
    pytest.param({**SETTINGS, "voice.voicebox_profile": None}, id="null"),
    pytest.param({**SETTINGS, "voice.voicebox_profile": 7}, id="not-a-str"),
]


@pytest.mark.parametrize("settings", NO_PROFILE)
async def test_speech_without_a_profile_is_off_and_calls_nothing(
    settings: Mapping[str, object],
) -> None:
    fake = FakeRequest({})

    with pytest.raises(VoiceboxError) as excinfo:
        await _box(fake, settings).speech("Hi.", "en")

    assert excinfo.value.code == "voicebox_off"
    assert fake.calls == []


NO_URL = [
    pytest.param({k: v for k, v in SETTINGS.items() if k != "voice.voicebox_url"}, id="missing"),
    pytest.param({**SETTINGS, "voice.voicebox_url": None}, id="null"),
    pytest.param({**SETTINGS, "voice.voicebox_url": "http://192.168.1.5:17493"}, id="lan-host"),
    pytest.param({**SETTINGS, "voice.voicebox_url": "https://127.0.0.1:17493"}, id="https"),
]


@pytest.mark.parametrize("settings", NO_URL)
async def test_without_a_loopback_url_nothing_is_called(settings: Mapping[str, object]) -> None:
    fake = FakeRequest({})
    box = _box(fake, settings)

    with pytest.raises(VoiceboxError) as excinfo:
        await box.speech("Hi.", "en")

    assert excinfo.value.code == "voicebox_off"
    assert await box.status() == {"state": "no_settings", "gpu": None, "profiles": []}
    assert fake.calls == []


SPEECH_FAILURES = [
    pytest.param((200, b"RIFF\x24\x00\x00\x00AVI LIST"), "voicebox_invalid", id="riff-not-wave"),
    pytest.param((200, b"RIFF"), "voicebox_invalid", id="short"),
    pytest.param((200, b""), "voicebox_invalid", id="empty"),
    pytest.param((200, b'{"detail": "ok"}'), "voicebox_invalid", id="json"),
    pytest.param((404, b'{"detail": "Profile not found"}'), "voicebox_profile_missing", id="404"),
    pytest.param(
        (400, b'{"detail": "Model qwen-tts-1.7B is not downloaded yet. Download it first."}'),
        "voicebox_model_missing",
        id="400-not-downloaded",
    ),
    pytest.param((400, b'{"detail": "Engine mismatch"}'), "voicebox_refused", id="400-other"),
    pytest.param((400, b'{"detail": ["not downloaded"]}'), "voicebox_refused", id="400-list"),
    pytest.param((400, b'["not downloaded"]'), "voicebox_refused", id="400-not-an-object"),
    pytest.param((400, b"not downloaded"), "voicebox_refused", id="400-not-json"),
    pytest.param((400, b'{"detail": "not downloaded \xff"}'), "voicebox_refused", id="400-utf8"),
    pytest.param((400, b"[" * 100_000), "voicebox_refused", id="400-too-deep"),
    pytest.param((201, WAV), "voicebox_refused", id="201"),
    pytest.param((302, b""), "voicebox_refused", id="redirect"),
    pytest.param((500, b"Internal Server Error"), "voicebox_refused", id="500"),
    pytest.param(VoiceboxError("voicebox_unreachable"), "voicebox_unreachable", id="unreachable"),
    pytest.param(VoiceboxError("voicebox_timeout"), "voicebox_timeout", id="socket-timeout"),
    pytest.param(VoiceboxError("voicebox_invalid"), "voicebox_invalid", id="over-cap"),
]


@pytest.mark.parametrize("answer, code", SPEECH_FAILURES)
async def test_speech_maps_each_answer_to_its_code(answer: Answer, code: str) -> None:
    fake = FakeRequest({"/generate/stream": answer})

    with pytest.raises(VoiceboxError) as excinfo:
        await _box(fake).speech("Hi.", "en")

    assert excinfo.value.code == code


# --- the gate -------------------------------------------------------------------------------


async def test_a_second_speech_waits_for_the_first() -> None:
    hold = threading.Event()
    fake = FakeRequest({"/generate/stream": (200, WAV)}, hold=hold)
    box = _box(fake)
    first = asyncio.create_task(box.speech("First.", "en"))
    try:
        assert await asyncio.to_thread(fake.entered.wait, HANG_GUARD_S)
        second = asyncio.create_task(box.speech("Second.", "en"))
        # Must time out: the first synthesis still holds the gate.
        await asyncio.wait([second], timeout=0.05)
        assert not second.done()
    finally:
        hold.set()

    assert await asyncio.wait_for(asyncio.gather(first, second), HANG_GUARD_S) == [WAV, WAV]
    assert fake.log == [
        ("enter", "First."),
        ("exit", "First."),
        ("enter", "Second."),
        ("exit", "Second."),
    ]


async def test_of_three_speeches_the_middle_is_superseded_and_the_newest_runs() -> None:
    hold = threading.Event()
    fake = FakeRequest({"/generate/stream": (200, WAV)}, hold=hold)
    box = _box(fake)
    first = asyncio.create_task(box.speech("First.", "en"))
    try:
        assert await asyncio.to_thread(fake.entered.wait, HANG_GUARD_S)
        middle = asyncio.create_task(box.speech("Middle.", "en"))
        newest = asyncio.create_task(box.speech("Newest.", "en"))
        await asyncio.sleep(0)  # both reach the gate while the first still holds it
    finally:
        hold.set()

    results = await asyncio.wait_for(
        asyncio.gather(first, middle, newest, return_exceptions=True), HANG_GUARD_S
    )

    assert results[0] == WAV and results[2] == WAV
    assert isinstance(results[1], VoiceboxError) and results[1].code == "voicebox_busy"
    assert fake.log == [
        ("enter", "First."),
        ("exit", "First."),
        ("enter", "Newest."),
        ("exit", "Newest."),
    ]


async def test_an_expired_speech_frees_the_gate_while_its_thread_still_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(voicebox, "SPEECH_TIMEOUT_S", 0.05)
    hold = threading.Event()
    fake = FakeRequest({"/generate/stream": (200, WAV)}, hold=hold)
    box = _box(fake)
    try:
        with pytest.raises(VoiceboxError) as excinfo:
            await box.speech("First.", "en")
        assert excinfo.value.code == "voicebox_timeout"

        assert await asyncio.wait_for(box.speech("Second.", "en"), HANG_GUARD_S) == WAV
        # The first call's thread is still blocked in its read.
        assert fake.log == [("enter", "First."), ("enter", "Second."), ("exit", "Second.")]
    finally:
        hold.set()


# --- pack -------------------------------------------------------------------------------------


async def test_pack_speech_goes_to_the_pack_address_with_the_language_profile() -> None:
    fake = FakeRequest({"/generate/stream": (200, WAV)})
    pack = FakePack(address=("localhost", 9100), profile="ja-narrator")
    box = Voicebox(lambda: PACK_SETTINGS, request=fake, pack=pack)

    assert await box.speech("Hello there.", "ja") == WAV

    assert pack.profile_calls == [("ja", "Ono_Anna")]  # no pick: voice_pack.VOICES's default
    body = (
        b'{"profile_id": "ja-narrator", "text": "Hello there.", "language": "ja", '
        b'"engine": null, "model_size": "0.6B"}'
    )
    assert fake.calls == [
        ("localhost", 9100, "POST", "/generate/stream", body, SPEECH_TIMEOUT_S, MAX_WAV_BYTES)
    ]


async def test_pack_speech_uses_kokoros_default_voice_with_a_null_model_size() -> None:
    fake = FakeRequest({"/generate/stream": (200, WAV)})
    pack = FakePack(address=("localhost", 9100), profile="en-narrator", kokoro_ready=True)
    box = Voicebox(lambda: PACK_SETTINGS, request=fake, pack=pack)

    assert await box.speech("Hello there.", "en") == WAV

    assert pack.profile_calls == [("en", "af_heart")]  # Kokoro's default, Kokoro ready
    body = (
        b'{"profile_id": "en-narrator", "text": "Hello there.", "language": "en", '
        b'"engine": null, "model_size": null}'
    )
    assert fake.calls == [
        ("localhost", 9100, "POST", "/generate/stream", body, SPEECH_TIMEOUT_S, MAX_WAV_BYTES)
    ]


async def test_pack_speech_uses_the_newest_choice() -> None:
    """A later request reads the current `voice.pack_voices`, not a value cached from an earlier
    one, even for the same language."""
    fake = FakeRequest({"/generate/stream": (200, WAV)})
    pack = FakePack()
    picks = ['{"en": "Aiden"}']
    box = Voicebox(
        lambda: {"voice.speech_engine": "pack", "voice.pack_voices": picks[-1]},
        request=fake,
        pack=pack,
    )

    assert await box.speech("First.", "en") == WAV
    # An explicit pick is honoured as stored, even one this FakePack isn't Kokoro-ready for.
    picks.append('{"en": "af_heart"}')
    assert await box.speech("Second.", "en") == WAV

    assert pack.profile_calls == [("en", "Aiden"), ("en", "af_heart")]


async def test_pack_speech_refuses_before_any_start() -> None:
    """A "system" pick and a language `voice_pack` has no voice for both raise before `profile()`
    or `address()` is ever called."""
    fake = FakeRequest({})
    pack = FakePack()
    system_settings = {"voice.speech_engine": "pack", "voice.pack_voices": '{"en": "system"}'}
    system_box = Voicebox(lambda: system_settings, request=fake, pack=pack)
    unspoken_box = Voicebox(lambda: PACK_SETTINGS, request=fake, pack=pack)

    for box, language in [(system_box, "en"), (unspoken_box, "xx")]:
        with pytest.raises(VoiceboxError) as excinfo:
            await box.speech("Hi.", language)
        assert excinfo.value.code == "voicebox_profile_missing"

    assert pack.profile_calls == []
    assert pack.address_calls == 0
    assert fake.calls == []


SPEED_WAVS = [
    pytest.param(_wav(2.0), True, id="2s"),
    pytest.param(_wav(0.5), False, id="half-a-second"),
    pytest.param(WAV, False, id="non-wav-bytes"),
]


@pytest.mark.parametrize("data, reports", SPEED_WAVS)
async def test_pack_speech_reports_its_speed(data: bytes, reports: bool) -> None:
    fake = FakeRequest({"/generate/stream": (200, data)})
    pack = FakePack()
    box = Voicebox(lambda: PACK_SETTINGS, request=fake, pack=pack)

    assert await box.speech("Hi.", "en") == data

    assert len(pack.speed_calls) == (1 if reports else 0)
    if reports:
        model, factor = pack.speed_calls[0]
        assert model == "qwen"
        assert 0 < factor < 1  # an instant fake call over 2s of audio: well under real-time


async def test_pack_speech_reports_kokoros_speed_under_its_own_model() -> None:
    """The model note_speed() is filed under must track the voice actually spoken, not be fixed
    to "qwen": a Kokoro pick must show up as "kokoro"."""
    data = _wav(2.0)
    fake = FakeRequest({"/generate/stream": (200, data)})
    pack = FakePack(kokoro_ready=True)
    box = Voicebox(lambda: PACK_SETTINGS, request=fake, pack=pack)

    assert await box.speech("Hi.", "en") == data

    assert len(pack.speed_calls) == 1
    model, factor = pack.speed_calls[0]
    assert model == "kokoro"
    assert 0 < factor < 1


async def test_pack_speech_asks_for_the_profile_before_the_address() -> None:
    """profile() first shrinks the window where a crash and restart between the two sends text
    to a freed port."""
    fake = FakeRequest({"/generate/stream": (200, WAV)})
    order: list[str] = []

    class OrderedPack:
        async def address(self) -> tuple[str, int]:
            order.append("address")
            return ("localhost", 9100)

        async def profile(self, language: str, voice: str) -> str:
            order.append("profile")
            return "pack-narrator"

        async def kokoro_ready(self) -> bool:
            return False

        def note_speed(self, model: str, factor: float) -> None:
            pass

    box = Voicebox(lambda: PACK_SETTINGS, request=fake, pack=OrderedPack())

    assert await box.speech("Hi.", "en") == WAV

    assert order == ["profile", "address"]


async def test_pack_engine_without_a_pack_target_is_off_and_calls_nothing() -> None:
    fake = FakeRequest({})
    box = Voicebox(lambda: PACK_SETTINGS, request=fake)  # pack=None, the default

    with pytest.raises(VoiceboxError) as excinfo:
        await box.speech("Hi.", "en")

    assert excinfo.value.code == "voicebox_off"
    assert fake.calls == []


PACK_FAILURES = [
    pytest.param("address", VoiceboxError("voicebox_pack_missing"), id="address-missing"),
    pytest.param("address", VoiceboxError("voicebox_pack_failed"), id="address-failed"),
    pytest.param("profile", VoiceboxError("voicebox_pack_missing"), id="profile-missing"),
]


@pytest.mark.parametrize("stage, error", PACK_FAILURES)
async def test_pack_errors_propagate_unchanged(stage: str, error: VoiceboxError) -> None:
    fake = FakeRequest({})
    pack = FakePack(address=error) if stage == "address" else FakePack(profile=error)
    box = Voicebox(lambda: PACK_SETTINGS, request=fake, pack=pack)

    with pytest.raises(VoiceboxError) as excinfo:
        await box.speech("Hi.", "en")

    assert excinfo.value is error
    assert fake.calls == []


async def test_a_superseded_pack_waiter_gets_busy() -> None:
    hold = threading.Event()
    fake = FakeRequest({"/generate/stream": (200, WAV)}, hold=hold)
    box = Voicebox(lambda: PACK_SETTINGS, request=fake, pack=FakePack())
    first = asyncio.create_task(box.speech("First.", "en"))
    try:
        assert await asyncio.to_thread(fake.entered.wait, HANG_GUARD_S)
        middle = asyncio.create_task(box.speech("Middle.", "en"))
        newest = asyncio.create_task(box.speech("Newest.", "en"))
        await asyncio.sleep(0)  # both reach the gate while the first still holds it
    finally:
        hold.set()

    results = await asyncio.wait_for(
        asyncio.gather(first, middle, newest, return_exceptions=True), HANG_GUARD_S
    )

    assert results[0] == WAV and results[2] == WAV
    assert isinstance(results[1], VoiceboxError) and results[1].code == "voicebox_busy"


# --- warm -------------------------------------------------------------------------------------


async def test_warm_sends_ready_in_english() -> None:
    fake = FakeRequest({"/generate/stream": (200, WAV)})
    pack = FakePack(address=("localhost", 9100), profile="pack-narrator")
    box = Voicebox(lambda: PACK_SETTINGS, request=fake, pack=pack)

    await box.warm()

    assert pack.profile_calls == [("en", "Ryan")]  # no pick: voice_pack.VOICES's default
    body = (
        b'{"profile_id": "pack-narrator", "text": "Ready.", "language": "en", '
        b'"engine": null, "model_size": "0.6B"}'
    )
    assert fake.calls == [
        ("localhost", 9100, "POST", "/generate/stream", body, SPEECH_TIMEOUT_S, MAX_WAV_BYTES)
    ]


async def test_warm_uses_the_chosen_pack_voice() -> None:
    """warm() resolves "en"'s voice the same way as a real pack speech request, not a voice
    fixed in advance."""
    fake = FakeRequest({"/generate/stream": (200, WAV)})
    pack = FakePack(address=("localhost", 9100), profile="kokoro-narrator", kokoro_ready=True)
    box = Voicebox(lambda: PACK_SETTINGS, request=fake, pack=pack)

    await box.warm()

    assert pack.profile_calls == [("en", "af_heart")]  # Kokoro's default, Kokoro ready


async def test_warm_refuses_before_any_start_for_a_system_pick() -> None:
    fake = FakeRequest({})
    pack = FakePack()
    settings = {"voice.speech_engine": "pack", "voice.pack_voices": '{"en": "system"}'}
    box = Voicebox(lambda: settings, request=fake, pack=pack)

    with pytest.raises(VoiceboxError) as excinfo:
        await box.warm()

    assert excinfo.value.code == "voicebox_profile_missing"
    assert pack.profile_calls == []
    assert pack.address_calls == 0
    assert fake.calls == []


@pytest.mark.parametrize(
    "settings",
    [SETTINGS, {}, {"voice.speech_engine": "system"}],
    ids=["voicebox", "none", "system"],
)
async def test_warm_does_nothing_for_other_engines(settings: Mapping[str, object]) -> None:
    fake = FakeRequest({})
    pack = FakePack()
    box = Voicebox(lambda: settings, request=fake, pack=pack)

    await box.warm()

    assert fake.calls == []
    assert pack.profile_calls == []


async def test_warm_does_nothing_without_a_pack_target() -> None:
    fake = FakeRequest({})
    box = Voicebox(lambda: PACK_SETTINGS, request=fake)  # pack=None, the default

    await box.warm()

    assert fake.calls == []


async def test_warm_does_nothing_while_a_real_synthesis_holds_the_lock() -> None:
    hold = threading.Event()
    fake = FakeRequest({"/generate/stream": (200, WAV)}, hold=hold)
    pack = FakePack()
    box = Voicebox(lambda: PACK_SETTINGS, request=fake, pack=pack)
    real = asyncio.create_task(box.speech("Real.", "en"))
    try:
        assert await asyncio.to_thread(fake.entered.wait, HANG_GUARD_S)

        await box.warm()  # the lock is held; warm must return at once, calling nothing

        assert pack.profile_calls == [("en", "Ryan")]  # only the real request's own profile()
        assert len(fake.calls) == 1  # only the real request's own HTTP call
    finally:
        hold.set()

    assert await asyncio.wait_for(real, HANG_GUARD_S) == WAV


async def test_warm_does_not_supersede_a_waiting_real_request() -> None:
    """Unlike a real speech request, warm never bumps `_latest`: a real request that starts
    waiting while warm holds the gate must still get its WAV, not `voicebox_busy`."""
    hold = threading.Event()
    fake = FakeRequest({"/generate/stream": (200, WAV)}, hold=hold)
    pack = FakePack()
    box = Voicebox(lambda: PACK_SETTINGS, request=fake, pack=pack)
    warming = asyncio.create_task(box.warm())
    try:
        assert await asyncio.to_thread(fake.entered.wait, HANG_GUARD_S)
        real = asyncio.create_task(box.speech("Real.", "en"))
        await asyncio.sleep(0)  # the real request reaches the gate while warm still holds it
    finally:
        hold.set()

    assert await asyncio.wait_for(asyncio.gather(warming, real), HANG_GUARD_S) == [None, WAV]
    assert fake.log == [
        ("enter", "Ready."),
        ("exit", "Ready."),
        ("enter", "Real."),
        ("exit", "Real."),
    ]


# --- status ---------------------------------------------------------------------------------


NOT_YET_VOICEBOX = [
    pytest.param({}, id="nothing-set"),
    pytest.param({"voice.speech_engine": "system"}, id="system"),
    pytest.param({"voice.speech_engine": "pack"}, id="pack"),
    pytest.param({"voice.voicebox_url": URL}, id="url-without-profile-or-engine"),
]


@pytest.mark.parametrize("settings", NOT_YET_VOICEBOX)
async def test_status_makes_no_call_before_the_owner_chooses_voicebox(
    settings: Mapping[str, object],
) -> None:
    fake = FakeRequest({})

    status = await _box(fake, settings).status()

    assert status == {"state": "no_settings", "gpu": None, "profiles": []}
    assert fake.calls == []


async def test_status_keeps_only_well_formed_profiles_and_their_three_fields() -> None:
    kept = [
        {"id": PROFILE, "name": "Narrator", "language": "ja"},
        {"id": "00000000-0000-4000-8000-000000000001", "name": "x", "language": "en"},
        {"id": "00000000-0000-4000-8000-000000000002", "name": "n" * 100, "language": "zh"},
    ]
    extras = {"description": "warm", "created_at": "2026-10-01T00:00:00", "avatar_path": None}
    dropped: list[object] = [
        "not an object",
        [PROFILE, "Narrator", "en"],
        None,
        {**kept[0], "id": PROFILE.upper()},
        {**kept[0], "id": "{" + PROFILE + "}"},
        {**kept[0], "id": PROFILE.replace("-", "")},
        {**kept[0], "id": 7},
        {"name": "No id", "language": "en"},
        {**kept[0], "name": ""},
        {**kept[0], "name": "n" * 101},
        {**kept[0], "name": "Line\nbreak"},
        {**kept[0], "name": "Bell\x07"},
        {**kept[0], "name": "C1\x85"},
        {**kept[0], "name": 7},
        {**kept[0], "language": "xx"},
        {**kept[0], "language": "EN"},
        {**kept[0], "language": ["en"]},
        {"id": PROFILE, "name": "No language"},
    ]
    items = [{**kept[0], **extras}, *dropped, *kept[1:]]
    fake = FakeRequest(
        {
            "/profiles": (200, json.dumps(items).encode()),
            "/health": (200, b'{"status": "healthy", "gpu_available": true}'),
        }
    )

    settings = {"voice.voicebox_url": URL, "voice.speech_engine": "voicebox"}
    status = await _box(fake, settings).status()  # opted into voicebox; no profile chosen yet

    assert status == {"state": "ready", "gpu": True, "profiles": kept}
    assert fake.calls == [
        ("localhost", 24680, "GET", "/profiles", None, STATUS_TIMEOUT_S, MAX_JSON_BYTES),
        ("localhost", 24680, "GET", "/health", None, STATUS_TIMEOUT_S, MAX_JSON_BYTES),
    ]


async def test_status_keeps_at_most_200_profiles() -> None:
    items = [
        {"id": f"00000000-0000-4000-8000-{n:012x}", "name": f"Voice {n}", "language": "en"}
        for n in range(201)
    ]
    fake = FakeRequest({"/profiles": (200, json.dumps(items).encode()), "/health": (200, b"{}")})

    status = await _box(fake).status()

    assert status["profiles"] == items[:200]


STATUS_FAILURES = [
    pytest.param(VoiceboxError("voicebox_unreachable"), "unreachable", id="unreachable"),
    pytest.param(VoiceboxError("voicebox_timeout"), "unreachable", id="timeout"),
    pytest.param(VoiceboxError("voicebox_invalid"), "unexpected", id="over-cap"),
    pytest.param(VoiceboxError("voicebox_refused"), "unexpected", id="other-error"),
    pytest.param((500, b"[]"), "unexpected", id="500"),
    pytest.param((404, b'{"detail": "Not Found"}'), "unexpected", id="404"),
    pytest.param((200, b'[{"name": "\xff"}]'), "unexpected", id="bad-utf8"),
    pytest.param((200, b"[NaN]"), "unexpected", id="nan"),
    pytest.param((200, b"[-Infinity]"), "unexpected", id="infinity"),
    pytest.param((200, b'{"profiles": []}'), "unexpected", id="object"),
    pytest.param((200, b"null"), "unexpected", id="null"),
    pytest.param((200, b"not json"), "unexpected", id="not-json"),
    pytest.param((200, b"[" * 100_000), "unexpected", id="too-deep"),
]


@pytest.mark.parametrize("answer, state", STATUS_FAILURES)
async def test_status_names_the_failure_and_skips_health(answer: Answer, state: str) -> None:
    fake = FakeRequest({"/profiles": answer, "/health": (200, b'{"gpu_available": true}')})

    assert await _box(fake).status() == {"state": state, "gpu": None, "profiles": []}
    assert [call[3] for call in fake.calls] == ["/profiles"]


HEALTH = [
    pytest.param((200, b'{"gpu_available": true}'), True, id="gpu"),
    pytest.param((200, b'{"gpu_available": false}'), False, id="no-gpu"),
    pytest.param((200, b'{"gpu_available": 1}'), None, id="int"),
    pytest.param((200, b'{"gpu_available": "true"}'), None, id="str"),
    pytest.param((200, b"{}"), None, id="missing"),
    pytest.param((200, b"[true]"), None, id="not-an-object"),
    pytest.param((200, b'{"gpu_available": true, "load": NaN}'), None, id="nan"),
    pytest.param((200, b"\xff"), None, id="bad-utf8"),
    pytest.param((503, b'{"gpu_available": true}'), None, id="503"),
    pytest.param(VoiceboxError("voicebox_unreachable"), None, id="unreachable"),
    pytest.param(VoiceboxError("voicebox_timeout"), None, id="timeout"),
    pytest.param(VoiceboxError("voicebox_invalid"), None, id="over-cap"),
]


@pytest.mark.parametrize("answer, gpu", HEALTH)
async def test_status_reads_the_gpu_from_health(answer: Answer, gpu: bool | None) -> None:
    fake = FakeRequest({"/profiles": (200, b"[]"), "/health": answer})

    assert await _box(fake).status() == {"state": "ready", "gpu": gpu, "profiles": []}


async def test_the_settings_are_read_at_every_call() -> None:
    urls: list[object] = [None]
    fake = FakeRequest({"/profiles": (200, b"[]"), "/health": (200, b"{}")})
    # A new mapping at every read, so a client holding the first one would miss the URL.
    box = Voicebox(
        lambda: {"voice.voicebox_url": urls[-1], "voice.speech_engine": "voicebox"}, request=fake
    )

    assert (await box.status())["state"] == "no_settings"
    urls.append(URL)
    assert (await box.status())["state"] == "ready"


async def test_unreadable_settings_count_as_none(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)

    def unreadable() -> Mapping[str, object]:
        raise RuntimeError("the settings store is unavailable")

    fake = FakeRequest({})
    box = Voicebox(unreadable, request=fake)

    assert await box.status() == {"state": "no_settings", "gpu": None, "profiles": []}
    with pytest.raises(VoiceboxError) as excinfo:
        await box.speech("Hi.", "en")
    assert excinfo.value.code == "voicebox_off"
    assert fake.calls == []
    assert caplog.records == []
