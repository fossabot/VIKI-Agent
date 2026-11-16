"""pack_voices: the Kokoro/Qwen3 voice tables and the `voice.pack_voices` trust-boundary parser
(ADR-048)."""

from __future__ import annotations

import pytest

from viki_agent.voice import pack_voices, voice_pack


def test_parse_accepts_a_map_of_kokoro_qwen_and_system_picks() -> None:
    text = '{"en":"af_bella","ja":"Ono_Anna","de":"system"}'
    assert pack_voices.parse(text) == {"en": "af_bella", "ja": "Ono_Anna", "de": "system"}


@pytest.mark.parametrize(
    "text",
    [
        '{"ja":"jf_alpha"}',  # Kokoro doesn't speak Japanese in this bundle: not a real voice id
        '{"ja":"af_bella"}',  # af_bella speaks English, not Japanese
        '{"he":"system"}',  # "he" isn't a voice_pack.LANGUAGES member
        '{"en":"ryan"}',  # presets are matched case-sensitively; the preset is "Ryan"
        '{"en":1}',  # value not a string
        '{"en":[]}',  # value not a string
        '{"en":"x","en":"y"}',  # repeated key
        '{"en":NaN}',  # refused constant
        "[]",  # not a dict
        "",  # not valid JSON
    ],
)
def test_parse_refuses_invalid_maps(text: str) -> None:
    assert pack_voices.parse(text) is None


def test_model_of_identifies_kokoro_qwen_and_neither() -> None:
    assert pack_voices.model_of("af_bella") == "kokoro"
    assert pack_voices.model_of("Ryan") == "qwen"
    assert pack_voices.model_of("system") is None
    assert pack_voices.model_of("nobody") is None


def test_voice_for_with_no_pick_falls_back_to_kokoro_default_or_voice_pack_voices() -> None:
    assert pack_voices.voice_for({}, "en", False) == voice_pack.VOICES["en"]
    assert pack_voices.voice_for({}, "en", True) == pack_voices.KOKORO_DEFAULTS["en"]
    # ja isn't one of Kokoro's languages, so it stays on voice_pack.VOICES even when Kokoro-ready.
    assert pack_voices.voice_for({}, "ja", True) == voice_pack.VOICES["ja"] == "Ono_Anna"


def test_voice_for_an_invalid_map_counts_as_no_picks() -> None:
    settings = {"voice.pack_voices": "not json"}
    assert pack_voices.voice_for(settings, "en", False) == voice_pack.VOICES["en"]


def test_voice_for_system_and_unknown_language_give_none() -> None:
    settings = {"voice.pack_voices": '{"en":"system"}'}
    assert pack_voices.voice_for(settings, "en", True) is None
    assert pack_voices.voice_for({}, "he", True) is None
