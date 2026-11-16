"""The Kokoro and Qwen3 voice tables and strict parsing for the `voice.pack_voices` setting: a
language to a Kokoro voice of that language, a Qwen3 preset, or "system" (ADR-048).

Stdlib only, plus `viki_agent.voice.voice_pack` for `LANGUAGES` and `VOICES` (the voice a
language keeps until the user picks one here). Never imports `viki_agent.app.settings`: that
module imports this one, at the PATCH trust boundary, not the other way around.
"""

from __future__ import annotations

import json
from collections.abc import Mapping

from viki_agent.voice import voice_pack

SYSTEM = "system"

KOKORO_LANGUAGES: tuple[str, ...] = ("zh", "en", "fr", "pt", "es", "it")

# hexgrad/Kokoro-82M's 41 offered voices (id to language), at `voice_pack.KOKORO_REVISION`.
KOKORO_VOICES: dict[str, str] = {
    "af_alloy": "en",
    "af_aoede": "en",
    "af_bella": "en",
    "af_heart": "en",
    "af_jessica": "en",
    "af_kore": "en",
    "af_nicole": "en",
    "af_nova": "en",
    "af_river": "en",
    "af_sarah": "en",
    "af_sky": "en",
    "am_adam": "en",
    "am_echo": "en",
    "am_eric": "en",
    "am_fenrir": "en",
    "am_liam": "en",
    "am_michael": "en",
    "am_onyx": "en",
    "am_puck": "en",
    "am_santa": "en",
    "bf_alice": "en",
    "bf_emma": "en",
    "bf_isabella": "en",
    "bf_lily": "en",
    "bm_daniel": "en",
    "bm_fable": "en",
    "bm_george": "en",
    "bm_lewis": "en",
    "ef_dora": "es",
    "em_alex": "es",
    "em_santa": "es",
    "ff_siwis": "fr",
    "if_sara": "it",
    "im_nicola": "it",
    "pf_dora": "pt",
    "pm_alex": "pt",
    "pm_santa": "pt",
    "zf_xiaobei": "zh",
    "zf_xiaoni": "zh",
    "zf_xiaoxiao": "zh",
    "zf_xiaoyi": "zh",
}

KOKORO_DEFAULTS: dict[str, str] = {
    "en": "af_heart",
    "zh": "zf_xiaobei",
    "fr": "ff_siwis",
    "pt": "pf_dora",
    "es": "ef_dora",
    "it": "if_sara",
}

# Voicebox's nine qwen_custom_voice presets: any may speak any language, unlike a
# Kokoro voice, which takes its G2P from the request language.
PRESETS: tuple[str, ...] = (
    "Vivian",
    "Serena",
    "Uncle_Fu",
    "Dylan",
    "Eric",
    "Ryan",
    "Aiden",
    "Ono_Anna",
    "Sohee",
)


def _refuse_constant(token: str) -> None:
    raise ValueError(token)


def _no_repeated_key(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("repeated key")
    return result


def parse(text: str) -> dict[str, str] | None:
    """The language-to-voice map `text` encodes, or None for anything else: every key must be a
    `voice_pack.LANGUAGES` member and every value "system", a preset, or a Kokoro voice whose
    language is that key -- one bad entry refuses the whole map (the PATCH trust boundary)."""
    try:
        parsed = json.loads(
            text, object_pairs_hook=_no_repeated_key, parse_constant=_refuse_constant
        )
    except ValueError, RecursionError:
        return None
    if not isinstance(parsed, dict):
        return None
    for language, voice in parsed.items():
        if language not in voice_pack.LANGUAGES or not isinstance(voice, str):
            return None
        if voice != SYSTEM and voice not in PRESETS and KOKORO_VOICES.get(voice) != language:
            return None
    return parsed


def model_of(voice: str) -> str | None:
    """Which model speaks `voice`, or None for "system" or an id neither table lists."""
    if voice in KOKORO_VOICES:
        return "kokoro"
    if voice in PRESETS:
        return "qwen"
    return None


def voice_for(settings: Mapping[str, object], language: str, kokoro_ready: bool) -> str | None:
    """The pack voice to speak `language` with: the user's valid pick for it; else Kokoro's
    default when `kokoro_ready` and Kokoro speaks the language; else the `voice_pack.VOICES`
    pick. None for an explicit "system" pick, or when nothing above names a voice."""
    stored = settings.get("voice.pack_voices")
    picks = parse(stored) if isinstance(stored, str) else None
    pick = picks.get(language) if picks is not None else None
    if pick == SYSTEM:
        return None
    if pick is not None:
        return pick
    if kokoro_ready and language in KOKORO_LANGUAGES:
        return KOKORO_DEFAULTS.get(language)
    return voice_pack.VOICES.get(language)
