import { createContext } from "react";
import { boundedBlob, boundedJson } from "../clients/boundedResponse";

// The wire format is the server's Voicebox routes (`/api/v2/voice/voicebox`,
// `/api/v2/voice/voicebox/speech`); a refusal carries `{error: {code}}`, the server's
// `voicebox_http.VoiceboxError` codes.
const STATUS_TIMEOUT_MS = 15_000;
// Ready wait (180s) + speech (120s) + 30s margin: the pack's own ready/speech budget, so a cold
// pack engine doesn't look like a voicebox_timeout before it has finished warming up.
const SPEECH_TIMEOUT_MS = 330_000;
const JSON_MAX = 256 * 1024;
const AUDIO_MAX = 8 * 1024 * 1024;
const MAX_PROFILES = 200;
const STATES: readonly unknown[] = ["ready", "unreachable", "unexpected", "no_settings"];

/** The languages Voicebox can speak; mirrors `voicebox_http.LANGUAGES` (Voicebox v0.5.0). */
export const VOICEBOX_LANGUAGES: readonly string[] = [
  "zh",
  "en",
  "ja",
  "ko",
  "de",
  "fr",
  "ru",
  "pt",
  "es",
  "it",
  "he",
  "ar",
  "da",
  "el",
  "fi",
  "hi",
  "ms",
  "nl",
  "no",
  "pl",
  "sv",
  "sw",
  "tr",
];

export interface VoiceboxProfile {
  id: string;
  name: string;
  language: string;
}

export interface VoiceboxStatus {
  state: "ready" | "unreachable" | "unexpected" | "no_settings";
  gpu: boolean | null;
  profiles: VoiceboxProfile[];
}

/** The server's typed code, or "unavailable" when the answer carries none or fails to parse. */
export class VoiceboxError extends Error {
  constructor(readonly code: string) {
    super(code);
  }
}

export interface VoiceboxApi {
  status(signal?: AbortSignal): Promise<VoiceboxStatus>;
  speech(text: string, language: string, signal: AbortSignal): Promise<ArrayBuffer>;
}

/** The current Voicebox choice, bundled for callers that need the api beside its settings. */
export interface VoiceboxSettings {
  api: VoiceboxApi;
  url: string;
  speakWhileWriting: boolean;
  /** Which engine is speaking: the installed pack, or a separate Voicebox app. Governs wording only. */
  engine?: "pack" | "voicebox";
  /** The engine's spoken languages, when narrower than `VOICEBOX_LANGUAGES` (the pack speaks 10). */
  languages?: readonly string[];
}

/** Provided by main.tsx; without it, Voicebox playback stays unavailable. */
export const VoiceboxContext = createContext<VoiceboxApi | null>(null);

const record = (value: unknown): Record<string, unknown> | null =>
  value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
const str = (value: unknown): value is string => typeof value === "string";
const unavailable = () => new VoiceboxError("unavailable");
const exactKeys = (row: Record<string, unknown>, keys: string[]) => {
  const have = Object.keys(row);
  return have.length === keys.length && keys.every((key) => key in row);
};

function parseProfile(value: unknown): VoiceboxProfile {
  const row = record(value);
  if (
    !row ||
    !exactKeys(row, ["id", "name", "language"]) ||
    !str(row.id) ||
    !str(row.name) ||
    !str(row.language) ||
    !VOICEBOX_LANGUAGES.includes(row.language)
  )
    throw unavailable();
  return { id: row.id, name: row.name, language: row.language };
}

function parseStatus(answer: Record<string, unknown>): VoiceboxStatus {
  if (
    !exactKeys(answer, ["state", "gpu", "profiles"]) ||
    !STATES.includes(answer.state) ||
    !(answer.gpu === null || typeof answer.gpu === "boolean") ||
    !Array.isArray(answer.profiles) ||
    answer.profiles.length > MAX_PROFILES
  )
    throw unavailable();
  return {
    state: answer.state as VoiceboxStatus["state"],
    gpu: answer.gpu as boolean | null,
    profiles: answer.profiles.map(parseProfile),
  };
}

/** A refusal carries `{error: {code, message}}`; the code alone is acted on. */
function refusalError(answer: Record<string, unknown> | null): VoiceboxError {
  const error = record(answer?.error);
  return error && str(error.code) && error.code ? new VoiceboxError(error.code) : unavailable();
}

export function createVoiceboxApi(token: string, fetcher: typeof fetch = fetch): VoiceboxApi {
  async function status(signal?: AbortSignal): Promise<VoiceboxStatus> {
    if (signal?.aborted) throw new Error("cancelled");
    const controller = new AbortController();
    const abort = () => controller.abort();
    signal?.addEventListener("abort", abort, { once: true });
    const timer = setTimeout(abort, STATUS_TIMEOUT_MS);
    try {
      const response = await fetcher("/api/v2/voice/voicebox", {
        method: "GET",
        signal: controller.signal,
        headers: { authorization: `Bearer ${token}` },
      });
      const answer = record(await boundedJson(response, JSON_MAX).catch(() => null));
      if (response.ok && answer) {
        const view = exactKeys(answer, ["voicebox"]) ? record(answer.voicebox) : null;
        if (!view) throw unavailable();
        return parseStatus(view);
      }
      throw refusalError(answer);
    } finally {
      clearTimeout(timer);
      signal?.removeEventListener("abort", abort);
    }
  }

  async function speech(text: string, language: string, signal: AbortSignal): Promise<ArrayBuffer> {
    const controller = new AbortController();
    const relay = () => controller.abort(signal.reason);
    signal.addEventListener("abort", relay, { once: true });
    if (signal.aborted) relay();
    let timedOut = false;
    const timer = setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, SPEECH_TIMEOUT_MS);
    try {
      const response = await fetcher("/api/v2/voice/voicebox/speech", {
        method: "POST",
        signal: controller.signal,
        headers: { authorization: `Bearer ${token}`, "content-type": "application/json" },
        body: JSON.stringify({ text, language }),
      });
      if (response.ok) {
        if (response.headers.get("content-type")?.split(";")[0] !== "audio/wav")
          throw new VoiceboxError("voicebox_invalid");
        const blob = await boundedBlob(response, AUDIO_MAX).catch(() => {
          throw new VoiceboxError("voicebox_invalid");
        });
        return await blob.arrayBuffer();
      }
      throw refusalError(record(await boundedJson(response, JSON_MAX).catch(() => null)));
    } catch (error) {
      if (error instanceof VoiceboxError) throw error;
      if (timedOut) throw new VoiceboxError("voicebox_timeout");
      throw error;
    } finally {
      clearTimeout(timer);
      signal.removeEventListener("abort", relay);
    }
  }

  return { status, speech };
}

const PROBLEMS: Record<string, (url: string) => string> = {
  voicebox_unreachable: (url) =>
    `Voicebox isn't answering at ${url}. Start the Voicebox app, then try again.`,
  voicebox_timeout: () =>
    "Voicebox took too long to make the speech. Try the 0.6B model size in Settings › Chat.",
  voicebox_busy: () => "Voicebox is busy with another reading. Try again in a moment.",
  voicebox_model_missing: () =>
    "The voice model isn't downloaded in Voicebox. Download it there, or choose another model size in Settings › Chat.",
  voicebox_profile_missing: () =>
    "The chosen Voicebox voice is gone. Choose another in Settings › Chat.",
  voicebox_refused: () => "Voicebox refused to make this speech. Check the voice in Voicebox.",
  voicebox_invalid: () => "Voicebox answered with something that isn't speech audio.",
  voicebox_off: () => "Choose a Voicebox voice in Settings › Chat.",
  voicebox_pack_missing: () =>
    "Natural voices aren't installed yet. Install them in Settings › Chat.",
  voicebox_pack_failed: () => "Natural voices stopped working. Repair them in Settings › Chat.",
};

// Engine "pack" re-words the shared failure codes as "the natural voice" rather than naming the
// separate Voicebox app; voicebox_pack_missing/voicebox_pack_failed and the default fallback read
// the same for every engine, so they stay in PROBLEMS instead of being duplicated here.
const PACK_PROBLEMS: Record<string, () => string> = {
  voicebox_unreachable: () => "The natural voice didn't answer in time. Try again in a moment.",
  voicebox_timeout: () => "The natural voice didn't answer in time. Try again in a moment.",
  voicebox_busy: () => "The natural voice is busy with another reading. Try again in a moment.",
  voicebox_refused: () => "The natural voice couldn't read this reply.",
  voicebox_invalid: () => "The natural voice couldn't read this reply.",
  voicebox_profile_missing: () => "Natural voices don't speak this language.",
  voicebox_model_missing: () =>
    "The natural voice model is missing. Repair natural voices in Settings › Chat.",
};

/** An English note for a `VoiceboxError`'s code, for the reply or Settings to show. */
export function voiceboxProblem(
  code: string,
  url: string,
  engine: "pack" | "voicebox" = "voicebox",
): string {
  if (engine === "pack") {
    const packText = PACK_PROBLEMS[code]?.();
    if (packText) return packText;
  }
  return PROBLEMS[code]?.(url) ?? "Voicebox couldn't make the speech.";
}
