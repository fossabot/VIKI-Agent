/**
 * The frontend twin of `pack_voices.py`: Kokoro's and Qwen3's offered voices, their defaults, and
 * the pure logic the Settings picker and read-aloud routing share. Mirrored by hand from the pin
 * table and `voice_pack.VOICES`; a drifted id fails the `voice.pack_voices` PATCH (`not_allowed`)
 * rather than silently mismatching.
 */
import { PACK_LANGUAGES } from "./voicePackApi";

export interface PackVoice {
  id: string;
  name: string;
  gender: "female" | "male";
  lang: string;
  accent?: "American" | "British";
}

// hexgrad/Kokoro-82M's 41 offered voices, in pin-table order. English rows (`a*` American,
// `b*` British) carry their accent in `accent`, name left plain; the rest need none (one
// language each).
export const KOKORO_VOICES: readonly PackVoice[] = [
  { id: "af_alloy", name: "Alloy", gender: "female", lang: "en", accent: "American" },
  { id: "af_aoede", name: "Aoede", gender: "female", lang: "en", accent: "American" },
  { id: "af_bella", name: "Bella", gender: "female", lang: "en", accent: "American" },
  { id: "af_heart", name: "Heart", gender: "female", lang: "en", accent: "American" },
  { id: "af_jessica", name: "Jessica", gender: "female", lang: "en", accent: "American" },
  { id: "af_kore", name: "Kore", gender: "female", lang: "en", accent: "American" },
  { id: "af_nicole", name: "Nicole", gender: "female", lang: "en", accent: "American" },
  { id: "af_nova", name: "Nova", gender: "female", lang: "en", accent: "American" },
  { id: "af_river", name: "River", gender: "female", lang: "en", accent: "American" },
  { id: "af_sarah", name: "Sarah", gender: "female", lang: "en", accent: "American" },
  { id: "af_sky", name: "Sky", gender: "female", lang: "en", accent: "American" },
  { id: "am_adam", name: "Adam", gender: "male", lang: "en", accent: "American" },
  { id: "am_echo", name: "Echo", gender: "male", lang: "en", accent: "American" },
  { id: "am_eric", name: "Eric", gender: "male", lang: "en", accent: "American" },
  { id: "am_fenrir", name: "Fenrir", gender: "male", lang: "en", accent: "American" },
  { id: "am_liam", name: "Liam", gender: "male", lang: "en", accent: "American" },
  { id: "am_michael", name: "Michael", gender: "male", lang: "en", accent: "American" },
  { id: "am_onyx", name: "Onyx", gender: "male", lang: "en", accent: "American" },
  { id: "am_puck", name: "Puck", gender: "male", lang: "en", accent: "American" },
  { id: "am_santa", name: "Santa", gender: "male", lang: "en", accent: "American" },
  { id: "bf_alice", name: "Alice", gender: "female", lang: "en", accent: "British" },
  { id: "bf_emma", name: "Emma", gender: "female", lang: "en", accent: "British" },
  { id: "bf_isabella", name: "Isabella", gender: "female", lang: "en", accent: "British" },
  { id: "bf_lily", name: "Lily", gender: "female", lang: "en", accent: "British" },
  { id: "bm_daniel", name: "Daniel", gender: "male", lang: "en", accent: "British" },
  { id: "bm_fable", name: "Fable", gender: "male", lang: "en", accent: "British" },
  { id: "bm_george", name: "George", gender: "male", lang: "en", accent: "British" },
  { id: "bm_lewis", name: "Lewis", gender: "male", lang: "en", accent: "British" },
  { id: "ef_dora", name: "Dora", gender: "female", lang: "es" },
  { id: "em_alex", name: "Alex", gender: "male", lang: "es" },
  { id: "em_santa", name: "Santa", gender: "male", lang: "es" },
  { id: "ff_siwis", name: "Siwis", gender: "female", lang: "fr" },
  { id: "if_sara", name: "Sara", gender: "female", lang: "it" },
  { id: "im_nicola", name: "Nicola", gender: "male", lang: "it" },
  { id: "pf_dora", name: "Dora", gender: "female", lang: "pt" },
  { id: "pm_alex", name: "Alex", gender: "male", lang: "pt" },
  { id: "pm_santa", name: "Santa", gender: "male", lang: "pt" },
  { id: "zf_xiaobei", name: "Xiaobei", gender: "female", lang: "zh" },
  { id: "zf_xiaoni", name: "Xiaoni", gender: "female", lang: "zh" },
  { id: "zf_xiaoxiao", name: "Xiaoxiao", gender: "female", lang: "zh" },
  { id: "zf_xiaoyi", name: "Xiaoyi", gender: "female", lang: "zh" },
];

export const KOKORO_LANGUAGES: readonly string[] = ["en", "zh", "fr", "pt", "es", "it"];

export const KOKORO_DEFAULTS: Record<string, string> = {
  en: "af_heart",
  zh: "zf_xiaobei",
  fr: "ff_siwis",
  pt: "pf_dora",
  es: "ef_dora",
  it: "if_sara",
};

// The nine Qwen3-TTS presets `voice_pack_server` already offers. Unlike Kokoro, a preset is not
// locked to its own language: any of the nine can read any of the ten PACK_LANGUAGES.
export const QWEN_VOICES: readonly PackVoice[] = [
  { id: "Vivian", name: "Vivian", gender: "female", lang: "zh" },
  { id: "Serena", name: "Serena", gender: "female", lang: "zh" },
  { id: "Uncle_Fu", name: "Uncle Fu", gender: "male", lang: "zh" },
  { id: "Dylan", name: "Dylan", gender: "male", lang: "zh" },
  { id: "Eric", name: "Eric", gender: "male", lang: "zh" },
  { id: "Ryan", name: "Ryan", gender: "male", lang: "en" },
  { id: "Aiden", name: "Aiden", gender: "male", lang: "en" },
  { id: "Ono_Anna", name: "Ono Anna", gender: "female", lang: "ja" },
  { id: "Sohee", name: "Sohee", gender: "female", lang: "ko" },
];

// = voice_pack.VOICES: today's one preset per language (en/de/fr/ru/pt/es/it all read by Ryan).
export const QWEN_DEFAULTS: Record<string, string> = {
  zh: "Vivian",
  en: "Ryan",
  ja: "Ono_Anna",
  ko: "Sohee",
  de: "Ryan",
  fr: "Ryan",
  ru: "Ryan",
  pt: "Ryan",
  es: "Ryan",
  it: "Ryan",
};

export const SYSTEM_VOICE = "system";

function isKokoroVoice(id: string): boolean {
  return KOKORO_VOICES.some((voice) => voice.id === id);
}

function isKokoroVoiceForLang(id: string, lang: string): boolean {
  return KOKORO_VOICES.some((voice) => voice.id === id && voice.lang === lang);
}

function isQwenVoice(id: string): boolean {
  return QWEN_VOICES.some((voice) => voice.id === id);
}

/** Keeps only the entries whose language is one of `PACK_LANGUAGES` and whose voice is "system",
 * a Qwen3 preset, or a Kokoro voice of that same language — same rule as `pack_voices.parse`, but
 * per entry: a drifted or unknown entry is dropped, not the whole map (parse is strict only at
 * the PATCH boundary that writes this setting). */
export function parsePackVoices(value: unknown): Record<string, string> {
  if (typeof value !== "string") return {};
  let parsed: unknown;
  try {
    parsed = JSON.parse(value);
  } catch {
    return {};
  }
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) return {};
  const map: Record<string, string> = {};
  for (const [lang, voice] of Object.entries(parsed)) {
    if (!(PACK_LANGUAGES as readonly string[]).includes(lang)) continue;
    if (typeof voice !== "string") continue;
    if (voice === SYSTEM_VOICE || isQwenVoice(voice) || isKokoroVoiceForLang(voice, lang)) {
      map[lang] = voice;
    }
  }
  return map;
}

/** Mirrors `pack_voices.voice_for`: the stored pick for `lang`, else Kokoro's default where
 * Kokoro is installed and speaks `lang`, else the Qwen3 default; "system" (stored or defaulted)
 * and an unspoken `lang` both read as null — no natural voice. */
export function effectiveVoice(
  map: Readonly<Record<string, string>>,
  lang: string,
  kokoroInstalled: boolean,
): string | null {
  const pick = Object.hasOwn(map, lang) ? map[lang] : undefined;
  const resolved =
    pick ??
    (kokoroInstalled && KOKORO_LANGUAGES.includes(lang)
      ? KOKORO_DEFAULTS[lang]
      : QWEN_DEFAULTS[lang]);
  return resolved === undefined || resolved === SYSTEM_VOICE ? null : resolved;
}

export function modelOf(id: string): "kokoro" | "qwen" | null {
  if (isKokoroVoice(id)) return "kokoro";
  if (isQwenVoice(id)) return "qwen";
  return null;
}

/** `PACK_LANGUAGES` minus any language the map points at the system voice — what the "pack"
 * engine still covers. */
export function packLanguages(map: Readonly<Record<string, string>>): readonly string[] {
  return PACK_LANGUAGES.filter((lang) => map[lang] !== SYSTEM_VOICE);
}

// Hands-free sends at most 300 characters (~19 s of speech at ~15.7 characters/second) per
// request and waits 15 s for the first audio, so a voice must synthesize in at most 15/19 ≈ 0.79
// of its playing time to make the deadline. 0.75 sits just inside that: Qwen3 measures about 4.1
// (far over), Kokoro about 0.025 (far under).
export const SLOW_FACTOR = 0.75;

// A fixed sample per language the natural voice speaks for Preview; never shown.
export const PREVIEW_SAMPLES: Record<string, string> = {
  en: "Hi, this is how I sound reading your replies.",
  zh: "你好，这是我朗读回复时的声音。",
  ja: "こんにちは。返事はこの声で読み上げます。",
  ko: "안녕하세요. 이 목소리로 답장을 읽어 드려요.",
  de: "Hallo, so klinge ich, wenn ich Antworten vorlese.",
  fr: "Bonjour, voici ma voix pour lire vos réponses.",
  ru: "Здравствуйте, так звучит мой голос при чтении ответов.",
  pt: "Olá, esta é a minha voz ao ler as respostas.",
  es: "Hola, así suena mi voz al leer las respuestas.",
  it: "Ciao, questa è la mia voce quando leggo le risposte.",
};
