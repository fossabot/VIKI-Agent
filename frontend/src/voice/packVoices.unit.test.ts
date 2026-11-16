import { expect, test } from "vitest";
import {
  effectiveVoice,
  KOKORO_DEFAULTS,
  KOKORO_LANGUAGES,
  KOKORO_VOICES,
  modelOf,
  PREVIEW_SAMPLES,
  packLanguages,
  parsePackVoices,
  QWEN_DEFAULTS,
  QWEN_VOICES,
  SLOW_FACTOR,
  SYSTEM_VOICE,
} from "./packVoices";
import { PACK_LANGUAGES } from "./voicePackApi";

test("KOKORO_VOICES lists 41 rows with unique ids, each a KOKORO_LANGUAGES language", () => {
  expect(KOKORO_VOICES).toHaveLength(41);
  expect(new Set(KOKORO_VOICES.map((voice) => voice.id)).size).toBe(41);
  for (const voice of KOKORO_VOICES) expect(KOKORO_LANGUAGES).toContain(voice.lang);
});

test("KOKORO_VOICES names an English voice's accent from its id, and no other voice", () => {
  for (const voice of KOKORO_VOICES) {
    if (voice.lang !== "en") {
      expect(voice.accent).toBeUndefined();
      continue;
    }
    expect(voice.name).not.toContain(",");
    expect(voice.accent).toBe(voice.id.startsWith("a") ? "American" : "British");
  }
});

test("KOKORO_VOICES spot-checks a row per language against the pin table", () => {
  const byId = Object.fromEntries(KOKORO_VOICES.map((voice) => [voice.id, voice]));
  expect(byId.af_heart).toEqual({
    id: "af_heart",
    name: "Heart",
    gender: "female",
    lang: "en",
    accent: "American",
  });
  expect(byId.bf_emma).toEqual({
    id: "bf_emma",
    name: "Emma",
    gender: "female",
    lang: "en",
    accent: "British",
  });
  expect(byId.ef_dora).toEqual({
    id: "ef_dora",
    name: "Dora",
    gender: "female",
    lang: "es",
  });
  expect(byId.ff_siwis).toEqual({
    id: "ff_siwis",
    name: "Siwis",
    gender: "female",
    lang: "fr",
  });
  expect(byId.if_sara).toEqual({
    id: "if_sara",
    name: "Sara",
    gender: "female",
    lang: "it",
  });
  expect(byId.pf_dora).toEqual({
    id: "pf_dora",
    name: "Dora",
    gender: "female",
    lang: "pt",
  });
  expect(byId.zf_xiaobei).toEqual({
    id: "zf_xiaobei",
    name: "Xiaobei",
    gender: "female",
    lang: "zh",
  });
});

test("KOKORO_VOICES has 23 female and 18 male rows", () => {
  const females = KOKORO_VOICES.filter((voice) => voice.gender === "female").length;
  expect(females).toBe(23);
  expect(KOKORO_VOICES.length - females).toBe(18);
});

test("KOKORO_LANGUAGES and KOKORO_DEFAULTS match the plan, each default a real Kokoro voice", () => {
  expect(KOKORO_LANGUAGES).toEqual(["en", "zh", "fr", "pt", "es", "it"]);
  expect(KOKORO_DEFAULTS).toEqual({
    en: "af_heart",
    zh: "zf_xiaobei",
    fr: "ff_siwis",
    pt: "pf_dora",
    es: "ef_dora",
    it: "if_sara",
  });
  for (const [lang, id] of Object.entries(KOKORO_DEFAULTS)) {
    expect(KOKORO_VOICES.some((voice) => voice.id === id && voice.lang === lang)).toBe(true);
  }
});

test("QWEN_VOICES lists the nine presets with humanised names", () => {
  expect(QWEN_VOICES).toEqual([
    { id: "Vivian", name: "Vivian", gender: "female", lang: "zh" },
    { id: "Serena", name: "Serena", gender: "female", lang: "zh" },
    { id: "Uncle_Fu", name: "Uncle Fu", gender: "male", lang: "zh" },
    { id: "Dylan", name: "Dylan", gender: "male", lang: "zh" },
    { id: "Eric", name: "Eric", gender: "male", lang: "zh" },
    { id: "Ryan", name: "Ryan", gender: "male", lang: "en" },
    { id: "Aiden", name: "Aiden", gender: "male", lang: "en" },
    { id: "Ono_Anna", name: "Ono Anna", gender: "female", lang: "ja" },
    { id: "Sohee", name: "Sohee", gender: "female", lang: "ko" },
  ]);
});

test("QWEN_DEFAULTS mirrors voice_pack.VOICES: Ryan for every language without its own preset", () => {
  expect(QWEN_DEFAULTS).toEqual({
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
  });
});

test('SYSTEM_VOICE is the literal "system"', () => {
  expect(SYSTEM_VOICE).toBe("system");
});

test("parsePackVoices keeps a fully valid map untouched", () => {
  const text = JSON.stringify({ en: "af_heart", ja: "Ono_Anna", de: "system" });
  expect(parsePackVoices(text)).toEqual({
    en: "af_heart",
    ja: "Ono_Anna",
    de: "system",
  });
});

test("parsePackVoices drops an unknown language, a voice of the wrong Kokoro language, and a non-string value, keeping the rest", () => {
  const text = JSON.stringify({
    en: "af_heart", // kept
    he: "system", // unknown language
    ja: "af_bella", // af_bella speaks en, not ja
    zh: 1, // not a string
    fr: "bogus", // not a Kokoro or Qwen voice
  });
  expect(parsePackVoices(text)).toEqual({ en: "af_heart" });
});

test("parsePackVoices accepts a Qwen preset for any language, not only its own", () => {
  expect(parsePackVoices(JSON.stringify({ de: "Vivian" }))).toEqual({
    de: "Vivian",
  });
});

const BAD_INPUTS: [string, unknown][] = [
  ["a number", 1],
  ["null", null],
  ["undefined", undefined],
  ["unparsable text", "not json"],
  ["a JSON array string", "[1,2]"],
  ["an object-valued JSON array", JSON.stringify([{ en: "af_heart" }])],
];
test.each(BAD_INPUTS)("parsePackVoices returns {} for %s", (_label, value) => {
  expect(parsePackVoices(value)).toEqual({});
});

const EFFECTIVE_VOICE_CASES: [string, Record<string, string>, string, boolean, string | null][] = [
  ["an explicit pick, Kokoro installed", { en: "Ryan" }, "en", true, "Ryan"],
  ["an explicit pick, Kokoro not installed", { en: "Ryan" }, "en", false, "Ryan"],
  ["no pick, Kokoro installed and the language is Kokoro's", {}, "en", true, "af_heart"],
  ["no pick, Kokoro not installed", {}, "en", false, "Ryan"],
  ["no pick, Kokoro installed but ja is not Kokoro's", {}, "ja", true, "Ono_Anna"],
  ["an explicit system pick", { en: "system" }, "en", true, null],
  ["an unspoken language with no pick", {}, "he", true, null],
];
test.each(EFFECTIVE_VOICE_CASES)(
  "effectiveVoice: %s -> %s",
  (_label, map, lang, kokoroInstalled, want) => {
    expect(effectiveVoice(map, lang, kokoroInstalled)).toBe(want);
  },
);

const MODEL_OF_CASES: [string, "kokoro" | "qwen" | null][] = [
  ["af_heart", "kokoro"],
  ["zf_xiaobei", "kokoro"],
  ["Ryan", "qwen"],
  ["Uncle_Fu", "qwen"],
  ["system", null],
  ["bogus", null],
];
test.each(MODEL_OF_CASES)("modelOf(%s) -> %s", (id, want) => {
  expect(modelOf(id)).toBe(want);
});

test("packLanguages drops only the languages pinned to the system voice", () => {
  expect(packLanguages({})).toEqual(PACK_LANGUAGES);
  expect(packLanguages({ en: "af_heart", ja: "system" })).toEqual(
    PACK_LANGUAGES.filter((lang) => lang !== "ja"),
  );
  expect(packLanguages({ ja: "system", ko: "system" })).toEqual(
    PACK_LANGUAGES.filter((lang) => lang !== "ja" && lang !== "ko"),
  );
});

test("SLOW_FACTOR is 0.75", () => {
  expect(SLOW_FACTOR).toBe(0.75);
});

test("PREVIEW_SAMPLES has one non-empty sample per PACK_LANGUAGES language", () => {
  expect(Object.keys(PREVIEW_SAMPLES).sort()).toEqual([...PACK_LANGUAGES].sort());
  for (const sample of Object.values(PREVIEW_SAMPLES)) expect(sample.length).toBeGreaterThan(0);
});

test("PREVIEW_SAMPLES matches the plan's exact sentences", () => {
  expect(PREVIEW_SAMPLES).toEqual({
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
  });
});
