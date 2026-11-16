/**
 * Client for the runs routes — a chat's open tool-permission requests waiting on its background
 * runs, and stopping one run. Server: src/viki_agent/conversations/runs_api.py. The routes
 * are not built yet; this client is coded directly to that written contract. Every rejection is a
 * `RunsApiError` whose `.code` is one of a fixed vocabulary — the server's own message text is
 * never kept.
 */
import { boundedJson } from "../boundedResponse";

export interface OpenRequest {
  toolUseId: string;
  run: string | null;
  raisedAt: number;
}

export type RunsErrorCode =
  | "invalid_input"
  | "not_found"
  | "not_running"
  | "unknown_run"
  | "refused"
  | "timeout"
  | "overloaded"
  | "unavailable"
  | "cancelled"
  | "invalid_response";

export class RunsApiError extends Error {
  readonly code: RunsErrorCode;
  constructor(code: RunsErrorCode) {
    super(code);
    this.name = "RunsApiError";
    this.code = code;
  }
}

export interface RunsApi {
  requests(sessionId: string, signal?: AbortSignal): Promise<OpenRequest[]>;
  stop(sessionId: string, blockId: string): Promise<void>;
}

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const BLOCK_ID = /^[A-Za-z0-9_-]{1,128}$/;
const TIMEOUT_MS = 15_000;
const MAX_RESPONSE_BYTES = 1 << 16; // 64 KiB
const MAX_ITEMS = 64;
const MAX_STR = 128;
const RAISED_AT_LEN = 24;

const KNOWN_CODES = new Set<RunsErrorCode>([
  "invalid_input",
  "not_found",
  "not_running",
  "unknown_run",
  "refused",
  "timeout",
  "overloaded",
  "unavailable",
  "cancelled",
  "invalid_response",
]);

function object(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
function exact(value: unknown, keys: readonly string[]): value is Record<string, unknown> {
  return (
    object(value) && Object.keys(value).length === keys.length && keys.every((key) => key in value)
  );
}
function str(value: unknown, max: number): value is string {
  return typeof value === "string" && value.length <= max;
}

/** A non-ok status keeps the body's `error.code` only when it is one of ours, else unavailable. */
function errorCodeFrom(body: unknown): RunsErrorCode {
  const candidate = object(body) && object(body.error) ? body.error.code : null;
  return typeof candidate === "string" && KNOWN_CODES.has(candidate as RunsErrorCode)
    ? (candidate as RunsErrorCode)
    : "unavailable";
}

function parseOpenRequest(value: unknown): OpenRequest | null {
  if (!exact(value, ["tool_use_id", "run", "raised_at"])) return null;
  const { tool_use_id: toolUseId, run, raised_at: raisedAtRaw } = value;
  if (!str(toolUseId, MAX_STR)) return null;
  if (run !== null && !str(run, MAX_STR)) return null;
  if (typeof raisedAtRaw !== "string" || raisedAtRaw.length !== RAISED_AT_LEN) return null;
  const raisedAt = Date.parse(raisedAtRaw);
  if (!Number.isFinite(raisedAt)) return null;
  return { toolUseId, run: run === null ? null : run, raisedAt };
}

function parseOpenRequests(value: unknown): OpenRequest[] | null {
  if (!exact(value, ["requests"])) return null;
  const { requests } = value;
  if (!Array.isArray(requests) || requests.length > MAX_ITEMS) return null;
  const parsed: OpenRequest[] = [];
  for (const raw of requests) {
    const item = parseOpenRequest(raw);
    if (!item) return null;
    parsed.push(item);
  }
  return parsed;
}

function isSentStatus(value: unknown): value is { status: "sent" } {
  return exact(value, ["status"]) && value.status === "sent";
}

export function createRunsApi(token: string, fetchImpl: typeof fetch = fetch): RunsApi {
  /** Shared GET/POST scaffold: bearer auth, the 15 s timeout, and the known-code error mapping. */
  async function request(
    path: string,
    method: "GET" | "POST",
    signal: AbortSignal | undefined,
  ): Promise<unknown> {
    if (signal?.aborted) throw new RunsApiError("cancelled");
    const controller = new AbortController();
    const relay = (): void => controller.abort();
    signal?.addEventListener("abort", relay, { once: true });
    const timer = setTimeout(relay, TIMEOUT_MS);
    try {
      const response = await fetchImpl(path, {
        method,
        signal: controller.signal,
        headers: { authorization: `Bearer ${token}` },
      });
      let body: unknown;
      try {
        body = await boundedJson(response, MAX_RESPONSE_BYTES);
      } catch (error) {
        // A caller cancel or our own timeout can land here mid-body-read; let the outer catch
        // sort those into cancelled/unavailable rather than misreading them as a data problem.
        if (controller.signal.aborted) throw error;
        if (!response.ok) throw new RunsApiError("unavailable");
        throw new RunsApiError("invalid_response");
      }
      if (!response.ok) throw new RunsApiError(errorCodeFrom(body));
      return body;
    } catch (error) {
      if (error instanceof RunsApiError) throw error;
      throw new RunsApiError(signal?.aborted ? "cancelled" : "unavailable");
    } finally {
      clearTimeout(timer);
      signal?.removeEventListener("abort", relay);
    }
  }

  return {
    async requests(sessionId, signal) {
      if (!UUID.test(sessionId)) throw new RunsApiError("invalid_input");
      const raw = await request(`/api/v2/sessions/${sessionId}/requests`, "GET", signal);
      const parsed = parseOpenRequests(raw);
      if (!parsed) throw new RunsApiError("invalid_response");
      return parsed;
    },
    async stop(sessionId, blockId) {
      if (!UUID.test(sessionId) || !BLOCK_ID.test(blockId)) throw new RunsApiError("invalid_input");
      const raw = await request(
        `/api/v2/sessions/${sessionId}/runs/${blockId}/stop`,
        "POST",
        undefined,
      );
      if (!isSentStatus(raw)) throw new RunsApiError("invalid_response");
    },
  };
}
