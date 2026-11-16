import { createContext } from "react";
import { boundedJson } from "../clients/boundedResponse";

// The wire format is the server's pack routes (`/api/v2/voice/pack`, `/api/v2/voice/pack/<action>`,
// `/api/v2/voice/speech-settings`); a refusal carries `{error: {code}}`, same shape as voiceboxApi.
const TIMEOUT_MS = 15_000;
const JSON_MAX = 256 * 1024;

/** The languages the installed voice pack speaks (Qwen3-TTS 0.6B); a subset of `VOICEBOX_LANGUAGES`. */
export const PACK_LANGUAGES = ["zh", "en", "ja", "ko", "de", "fr", "ru", "pt", "es", "it"] as const;

export const VOICEBOX_RELEASES_URL = "https://github.com/jamiepine/voicebox/releases";

export type SpeechEngine = "system" | "pack" | "voicebox";

/** The stored `voice.speech_engine` when it's one of the three engines; otherwise inferred from
 * whether a Voicebox profile is already chosen, same table the server's settings spec enforces. */
export function speechEngine(settings: Readonly<Record<string, unknown>>): SpeechEngine {
  const engine = settings["voice.speech_engine"];
  if (engine === "system" || engine === "pack" || engine === "voicebox") return engine;
  return typeof settings["voice.voicebox_profile"] === "string" ? "voicebox" : "system";
}

export type PackState =
  | "unchecked"
  | "checking"
  | "unsupported"
  | "not_installed"
  | "installing"
  | "install_failed"
  | "removing"
  | "stopped"
  | "starting"
  | "ready"
  | "failed";

const STATES: readonly PackState[] = [
  "unchecked",
  "checking",
  "unsupported",
  "not_installed",
  "installing",
  "install_failed",
  "removing",
  "stopped",
  "starting",
  "ready",
  "failed",
];

type PackReason = "platform" | "no_nvidia" | "probe_failed" | "driver_old" | "low_vram";
const REASONS: readonly PackReason[] = [
  "platform",
  "no_nvidia",
  "probe_failed",
  "driver_old",
  "low_vram",
];

type PackStep = "server" | "unpack" | "model";
const STEPS: readonly PackStep[] = ["server", "unpack", "model"];

export interface VoicePackProgress {
  step: PackStep;
  done: number;
  total: number;
}

export interface VoicePackGpu {
  name: string;
  memoryMib: number;
  driver: string;
}

/** Speech time over audio time, per model, for this app run; null until that model has
 * synthesized once (its first synthesis in a run doesn't count, see `fallbacks` below). */
export interface VoicePackSpeed {
  kokoro: number | null;
  qwen: number | null;
}

/** One hands-free reply that fell back to the system voice. */
export interface VoicePackFallback {
  code: string;
  at: string;
}

export interface VoicePackStatus {
  state: PackState;
  reason: PackReason | null;
  error: string | null;
  progress: VoicePackProgress | null;
  downloadBytes: number;
  diskBytes: number;
  needBytes: number;
  freeBytes: number | null;
  gpu: VoicePackGpu | null;
  kokoro: boolean;
  speed: VoicePackSpeed;
  /** Newest first, at most three. */
  fallbacks: VoicePackFallback[];
}

export type PackAction = "check" | "install" | "repair" | "cancel" | "remove" | "retry" | "warm";

/** Actions the server can take longer than `TIMEOUT_MS` to answer: `check` runs nvidia-smi for up
 * to 15 s, `repair` stops a running server first, and `remove` answers only once about 7.9 GB is
 * deleted. */
const ACT_TIMEOUT_MS: Partial<Record<PackAction, number>> = {
  check: 30_000,
  repair: 30_000,
  remove: 120_000,
};

/** True once the pack has a server worth talking to (even a failed one), vs. not yet reached that
 * point (`unchecked`...`installing`) or torn back down (`install_failed`, `removing`). */
export function installedState(state: PackState): boolean {
  return state === "stopped" || state === "starting" || state === "ready" || state === "failed";
}

export class VoicePackError extends Error {
  constructor(readonly code: string) {
    super(code);
  }
}

export interface VoicePackApi {
  status(signal?: AbortSignal): Promise<VoicePackStatus>;
  act(action: PackAction): Promise<VoicePackStatus>;
  openSpeechSettings(): Promise<void>;
}

export interface VoicePackServices {
  api: VoicePackApi;
  openUrl(url: string): void;
}

/** Provided by main.tsx; without it, the pack's install/repair UI stays unavailable. */
export const VoicePackContext = createContext<VoicePackServices | null>(null);

const record = (value: unknown): Record<string, unknown> | null =>
  value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
const str = (value: unknown): value is string => typeof value === "string";
const int = (value: unknown): value is number =>
  typeof value === "number" && Number.isInteger(value);
const num = (value: unknown): value is number => typeof value === "number";
const FALLBACKS_MAX = 3;
const unavailable = () => new VoicePackError("unavailable");
const exactKeys = (row: Record<string, unknown>, keys: string[]) => {
  const have = Object.keys(row);
  return have.length === keys.length && keys.every((key) => key in row);
};

function parseProgress(value: unknown): VoicePackProgress | null {
  if (value === null) return null;
  const row = record(value);
  if (
    !row ||
    !exactKeys(row, ["step", "done", "total"]) ||
    !STEPS.includes(row.step as PackStep) ||
    !int(row.done) ||
    row.done < 0 ||
    !int(row.total) ||
    row.total < row.done
  )
    throw unavailable();
  return { step: row.step as PackStep, done: row.done, total: row.total };
}

function parseGpu(value: unknown): VoicePackGpu | null {
  if (value === null) return null;
  const row = record(value);
  if (
    !row ||
    !exactKeys(row, ["name", "memory_mib", "driver"]) ||
    !str(row.name) ||
    !int(row.memory_mib) ||
    !str(row.driver)
  )
    throw unavailable();
  return { name: row.name, memoryMib: row.memory_mib, driver: row.driver };
}

function parseSpeed(value: unknown): VoicePackSpeed {
  const row = record(value);
  if (
    !row ||
    !exactKeys(row, ["kokoro", "qwen"]) ||
    !(row.kokoro === null || num(row.kokoro)) ||
    !(row.qwen === null || num(row.qwen))
  )
    throw unavailable();
  return {
    kokoro: row.kokoro as number | null,
    qwen: row.qwen as number | null,
  };
}

function parseFallback(value: unknown): VoicePackFallback {
  const row = record(value);
  if (!row || !exactKeys(row, ["code", "at"]) || !str(row.code) || !str(row.at))
    throw unavailable();
  return { code: row.code, at: row.at };
}

function parseFallbacks(value: unknown): VoicePackFallback[] {
  if (!Array.isArray(value) || value.length > FALLBACKS_MAX) throw unavailable();
  return value.map(parseFallback);
}

function parseStatus(answer: Record<string, unknown>): VoicePackStatus {
  if (
    !exactKeys(answer, [
      "state",
      "reason",
      "error",
      "progress",
      "download_bytes",
      "disk_bytes",
      "need_bytes",
      "free_bytes",
      "gpu",
      "kokoro",
      "speed",
      "fallbacks",
    ]) ||
    !STATES.includes(answer.state as PackState) ||
    !(answer.reason === null || REASONS.includes(answer.reason as PackReason)) ||
    !(answer.error === null || str(answer.error)) ||
    !int(answer.download_bytes) ||
    !int(answer.disk_bytes) ||
    !int(answer.need_bytes) ||
    !(answer.free_bytes === null || int(answer.free_bytes)) ||
    typeof answer.kokoro !== "boolean"
  )
    throw unavailable();
  return {
    state: answer.state as PackState,
    reason: answer.reason as PackReason | null,
    error: answer.error as string | null,
    progress: parseProgress(answer.progress),
    downloadBytes: answer.download_bytes,
    diskBytes: answer.disk_bytes,
    needBytes: answer.need_bytes,
    freeBytes: answer.free_bytes as number | null,
    gpu: parseGpu(answer.gpu),
    kokoro: answer.kokoro,
    speed: parseSpeed(answer.speed),
    fallbacks: parseFallbacks(answer.fallbacks),
  };
}

/** A refusal carries `{error: {code, message}}`; the code alone is acted on. */
function refusalError(answer: Record<string, unknown> | null): VoicePackError {
  const error = record(answer?.error);
  return error && str(error.code) && error.code ? new VoicePackError(error.code) : unavailable();
}

export function createVoicePackApi(token: string, fetcher: typeof fetch = fetch): VoicePackApi {
  async function call(
    path: string,
    method: "GET" | "POST",
    body: string | undefined,
    signal: AbortSignal | undefined,
    timeoutMs = TIMEOUT_MS,
  ): Promise<Record<string, unknown>> {
    if (signal?.aborted) throw new Error("cancelled");
    const controller = new AbortController();
    const abort = () => controller.abort();
    signal?.addEventListener("abort", abort, { once: true });
    const timer = setTimeout(abort, timeoutMs);
    try {
      const headers: Record<string, string> = {
        authorization: `Bearer ${token}`,
      };
      if (body !== undefined) headers["content-type"] = "application/json";
      const init: RequestInit = { method, signal: controller.signal, headers };
      const response = await fetcher(path, body === undefined ? init : { ...init, body });
      const answer = record(await boundedJson(response, JSON_MAX).catch(() => null));
      if (response.ok && answer) return answer;
      throw refusalError(answer);
    } finally {
      clearTimeout(timer);
      signal?.removeEventListener("abort", abort);
    }
  }

  function pack(answer: Record<string, unknown>): VoicePackStatus {
    const view = exactKeys(answer, ["pack"]) ? record(answer.pack) : null;
    if (!view) throw unavailable();
    return parseStatus(view);
  }

  async function status(signal?: AbortSignal): Promise<VoicePackStatus> {
    return pack(await call("/api/v2/voice/pack", "GET", undefined, signal));
  }

  async function act(action: PackAction): Promise<VoicePackStatus> {
    const timeoutMs = ACT_TIMEOUT_MS[action] ?? TIMEOUT_MS;
    return pack(await call(`/api/v2/voice/pack/${action}`, "POST", "{}", undefined, timeoutMs));
  }

  async function openSpeechSettings(): Promise<void> {
    const answer = await call("/api/v2/voice/speech-settings", "POST", "{}", undefined);
    if (!exactKeys(answer, ["opened"]) || answer.opened !== true) throw unavailable();
  }

  return { status, act, openSpeechSettings };
}
