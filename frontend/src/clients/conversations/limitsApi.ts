/** Bounded, read-only client for plan rate limits (server: src/viki_agent/conversations/limits_api.py). */

import { boundedJson } from "../boundedResponse";

export type RateLimitStatus = "allowed" | "allowed_warning" | "rejected";

export type LimitingWindow =
  | "five_hour"
  | "seven_day"
  | "seven_day_opus"
  | "seven_day_sonnet"
  | "overage";

export interface PlanWindow {
  utilization: number | null;
  resets_at: number | null;
}

export interface PlanLimitReading {
  observed_at: string | null;
  status: RateLimitStatus;
  limiting: LimitingWindow | null;
  utilization: number | null;
  resets_at: number | null;
  windows: {
    five_hour: PlanWindow | null;
    seven_day: PlanWindow | null;
  };
}

export class LimitsApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
  ) {
    super("The plan limits are unavailable.");
    this.name = "LimitsApiError";
  }
}

export interface LimitsApi {
  get(signal?: AbortSignal): Promise<PlanLimitReading | null>;
}

const MAX_RESPONSE = 64 << 10;
const TIMEOUT = 15_000;
const CODE = /^[a-z][a-z0-9_.:-]{0,127}$/;
const STATUSES: readonly RateLimitStatus[] = ["allowed", "allowed_warning", "rejected"];
const WINDOWS: readonly LimitingWindow[] = [
  "five_hour",
  "seven_day",
  "seven_day_opus",
  "seven_day_sonnet",
  "overage",
];
const MAX_STRING_CHARS = 256;

function object(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
function exact(value: unknown, keys: readonly string[]): value is Record<string, unknown> {
  return (
    object(value) && Object.keys(value).length === keys.length && keys.every((key) => key in value)
  );
}
function text(value: unknown): value is string {
  return typeof value === "string" && value.length <= MAX_STRING_CHARS;
}
function nullableText(value: unknown): value is string | null {
  return value === null || text(value);
}
function status(value: unknown): value is RateLimitStatus {
  return typeof value === "string" && (STATUSES as readonly string[]).includes(value);
}
function window(value: unknown): value is LimitingWindow {
  return typeof value === "string" && (WINDOWS as readonly string[]).includes(value);
}
function nullableWindow(value: unknown): value is LimitingWindow | null {
  return value === null || window(value);
}
function finite(value: unknown): boolean {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 && value <= 10;
}
function safeInt(value: unknown): boolean {
  return Number.isSafeInteger(value) && typeof value === "number" && value > 0;
}
function nullableUtilization(value: unknown): value is number | null {
  return value === null || finite(value);
}
function nullableResetsAt(value: unknown): value is number | null {
  return value === null || safeInt(value);
}
function planWindow(value: unknown): value is PlanWindow | null {
  if (value === null) return true;
  if (!exact(value, ["utilization", "resets_at"])) return false;
  const u = value.utilization;
  const r = value.resets_at;
  return (u === null || finite(u)) && (r === null || safeInt(r)) && !(u === null && r === null);
}

function parseLimits(value: unknown): PlanLimitReading | null {
  if (!exact(value, ["observation"])) return null;

  const obs = value.observation;

  if (!exact(obs, ["observed_at", "status", "limiting", "utilization", "resets_at", "windows"])) {
    return null;
  }

  if (!nullableText(obs.observed_at)) return null;
  if (!status(obs.status)) return null;

  const windows = obs.windows;
  if (!object(windows) || Object.keys(windows).length !== 2) return null;
  if (!planWindow(windows.five_hour) || !planWindow(windows.seven_day)) return null;

  if (!nullableWindow(obs.limiting)) return null;
  if (!nullableUtilization(obs.utilization)) return null;
  if (!nullableResetsAt(obs.resets_at)) return null;

  return {
    observed_at: obs.observed_at,
    status: obs.status,
    limiting: obs.limiting,
    utilization: obs.utilization,
    resets_at: obs.resets_at,
    windows: {
      five_hour: windows.five_hour,
      seven_day: windows.seven_day,
    },
  };
}

function serverError(status_: number, body: unknown): LimitsApiError {
  const candidate = object(body) && object(body.error) ? body.error.code : null;
  return new LimitsApiError(
    status_,
    typeof candidate === "string" && CODE.test(candidate) ? candidate : "unavailable",
  );
}

export function createLimitsApi(token: string, fetchImpl: typeof fetch = fetch): LimitsApi {
  async function request(signal?: AbortSignal): Promise<unknown> {
    if (signal?.aborted) throw new LimitsApiError(0, "cancelled");
    const controller = new AbortController();
    const relay = (): void => controller.abort();
    signal?.addEventListener("abort", relay, { once: true });
    let timedOut = false;
    const timer = setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, TIMEOUT);
    try {
      const response = await fetchImpl("/api/v2/limits", {
        method: "GET",
        signal: controller.signal,
        headers: { authorization: `Bearer ${token}` },
      });
      let parsed: unknown;
      try {
        parsed = await boundedJson(response, MAX_RESPONSE);
      } catch (error) {
        if (controller.signal.aborted) throw error;
        if (!response.ok) throw serverError(response.status, null);
        throw new LimitsApiError(0, "invalid_response");
      }
      if (!response.ok) throw serverError(response.status, parsed);
      return parsed;
    } catch (error) {
      if (error instanceof LimitsApiError) throw error;
      if (signal?.aborted) throw new LimitsApiError(0, "cancelled");
      if (timedOut) throw new LimitsApiError(0, "timeout");
      throw new LimitsApiError(0, "unavailable");
    } finally {
      clearTimeout(timer);
      signal?.removeEventListener("abort", relay);
    }
  }
  return {
    async get(signal) {
      const raw = await request(signal);
      if (exact(raw, ["observation"]) && raw.observation === null) return null;
      const parsed = parseLimits(raw);
      if (parsed === null) throw new LimitsApiError(0, "invalid_response");
      return parsed;
    },
  };
}
