/**
 * The voice client. The server keeps one capture slot for the whole app and frees it before it
 * closes a socket (`voice_socket_route`); a socket that connects while an earlier one still holds
 * the slot is closed 1013 (busy). One api serves one window, whose message boxes can hand the mic
 * to each other, so its sockets connect in turn: a new socket waits until each earlier socket of
 * this api has closed, and one closed while it waits never connects.
 */

import { boundedJson } from "../clients/boundedResponse";
import { type DictationWebSocket, openDictationSocket } from "./dictationSocket";
import type {
  CaptureFactory,
  DictationApi,
  HandsFreeReportEvent,
  Microphone,
  VoiceFallback,
  VoiceState,
  VoiceStatus,
} from "./dictationTypes";

// The wire format is the server's voice routes (`/api/v2/voice`, `/api/v2/voice/fallback/clear`).
const TIMEOUT = 15_000;
const STATUS_MAX = 64 * 1024;
const STATES: readonly unknown[] = [
  "ready",
  "setup_needed",
  "backend_missing",
  "engine_unavailable",
];
const STATUS_KEYS = [
  "state",
  "running",
  "model",
  "backend",
  "device",
  "languages",
  "language",
  "language_unavailable",
  "models",
  "backends",
  "fallback",
  "cpu_model",
  "slow_on_cpu",
];

/** The server's typed code, or "unavailable" when the answer carries none. */
export class DictationApiError extends Error {
  constructor(readonly code: string) {
    super(code);
  }
}
export interface DictationApiDeps {
  workletUrl: string;
  startCapture: CaptureFactory;
  microphones(): Promise<Microphone[]>;
  location?: Pick<Location, "protocol" | "host">;
  createSocket?: (url: string) => DictationWebSocket;
}

const record = (value: unknown): Record<string, unknown> | null =>
  value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
const str = (value: unknown): value is string => typeof value === "string";
const strings = (value: unknown): value is string[] => Array.isArray(value) && value.every(str);
const unavailable = () => new DictationApiError("unavailable");
const exactKeys = (row: Record<string, unknown>, keys: string[]) => {
  const have = Object.keys(row);
  return have.length === keys.length && keys.every((key) => key in row);
};

function parseFallback(value: unknown): VoiceFallback | null {
  if (value === null) return null;
  const row = record(value);
  if (
    !row ||
    !exactKeys(row, ["backend", "code", "detail"]) ||
    !str(row.backend) ||
    !str(row.code) ||
    !(row.detail === null || str(row.detail))
  )
    throw unavailable();
  return { backend: row.backend, code: row.code, detail: row.detail };
}

function parseStatus(answer: Record<string, unknown>): VoiceStatus {
  const row = record(answer.voice);
  if (
    !exactKeys(answer, ["voice"]) ||
    !row ||
    !exactKeys(row, STATUS_KEYS) ||
    !STATES.includes(row.state) ||
    typeof row.running !== "boolean" ||
    !(row.model === null || str(row.model)) ||
    !str(row.backend) ||
    !(row.device === null || str(row.device)) ||
    !strings(row.languages) ||
    !str(row.language) ||
    typeof row.language_unavailable !== "boolean" ||
    !strings(row.models) ||
    !strings(row.backends) ||
    !(row.cpu_model === null || str(row.cpu_model)) ||
    typeof row.slow_on_cpu !== "boolean"
  )
    throw unavailable();
  return {
    state: row.state as VoiceState,
    running: row.running,
    model: row.model,
    backend: row.backend,
    device: row.device,
    languages: row.languages,
    language: row.language,
    language_unavailable: row.language_unavailable,
    models: row.models,
    backends: row.backends,
    fallback: parseFallback(row.fallback),
    cpu_model: row.cpu_model,
    slow_on_cpu: row.slow_on_cpu,
  };
}

export function createDictationApi(token: string, deps: DictationApiDeps): DictationApi {
  // This api's sockets that have not closed yet, and a promise that settles once each has closed.
  let unclosed = 0;
  let allClosed = Promise.resolve();
  async function call(
    method: "GET" | "POST",
    url: string,
    signal?: AbortSignal,
  ): Promise<VoiceStatus> {
    if (signal?.aborted) throw new Error("cancelled");
    const controller = new AbortController();
    const abort = () => controller.abort();
    signal?.addEventListener("abort", abort, { once: true });
    const timer = setTimeout(abort, TIMEOUT);
    const headers: Record<string, string> = { authorization: `Bearer ${token}` };
    if (method === "POST") headers["content-type"] = "application/json";
    try {
      const response = await fetch(url, {
        method,
        signal: controller.signal,
        headers,
        ...(method === "POST" ? { body: "{}" } : {}),
      });
      const answer = record(await boundedJson(response, STATUS_MAX).catch(() => null));
      if (response.ok && answer) return parseStatus(answer);
      // A refusal carries {error: {code, message}}; the code alone is acted on.
      const error = record(answer?.error);
      throw error && str(error.code) && error.code
        ? new DictationApiError(error.code)
        : unavailable();
    } finally {
      clearTimeout(timer);
      signal?.removeEventListener("abort", abort);
    }
  }
  return {
    status: (signal) => call("GET", "/api/v2/voice", signal),
    clearFallback: (signal) => call("POST", "/api/v2/voice/fallback/clear", signal),
    socket(handlers) {
      const where = deps.location ?? window.location;
      const scheme = where.protocol === "https:" ? "wss" : "ws";
      const after = unclosed > 0 ? allClosed : undefined;
      const { promise: closed, resolve: settle } = Promise.withResolvers<void>();
      const socket = openDictationSocket(
        `${scheme}://${where.host}/ws/voice`,
        token,
        {
          ...handlers,
          closed(code) {
            unclosed -= 1;
            settle();
            handlers.closed(code);
          },
        },
        deps.createSocket,
        after,
      );
      unclosed += 1;
      allClosed = after ? after.then(() => closed) : closed;
      return socket;
    },
    startCapture: deps.startCapture,
    microphones: deps.microphones,
    workletUrl: deps.workletUrl,
    report(event: HandsFreeReportEvent, code: string) {
      fetch("/api/v2/voice/handsfree/report", {
        method: "POST",
        headers: {
          authorization: `Bearer ${token}`,
          "content-type": "application/json",
        },
        body: JSON.stringify({ event, code }),
      }).catch(() => undefined);
    },
  };
}
