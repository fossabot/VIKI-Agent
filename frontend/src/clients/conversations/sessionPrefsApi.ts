/**
 * Bounded client for a chat's model/effort preferences before its process starts.
 *
 * `GET`/`PATCH /api/v2/sessions/{id}/prefs`, bearer-authenticated. `save` writes only the given
 * keys; nothing here starts a process or inspects the running session. Every rejection's own
 * message is one of a fixed vocabulary (`invalid_input`, `cancelled`, `unavailable`,
 * `invalid_response`) — the server's own text is never kept.
 */
import { boundedJson } from "../boundedResponse";

export interface ChatPrefs {
  model: string | null;
  effort: string;
}

export interface SessionPrefsApi {
  get(sessionId: string, signal?: AbortSignal): Promise<ChatPrefs>;
  save(sessionId: string, changes: { model?: string; effort?: string }): Promise<ChatPrefs>;
}

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const TIMEOUT_MS = 15_000;
const MAX_BYTES = 65_536;

function validModel(value: unknown): value is string | null {
  return value === null || (typeof value === "string" && value.length >= 1 && value.length <= 1024);
}

function validEffort(value: unknown): value is string {
  return typeof value === "string" && value.length >= 1 && value.length <= 32;
}

function parsePrefs(value: unknown): ChatPrefs {
  if (typeof value !== "object" || value === null || Array.isArray(value))
    throw new Error("invalid_response");
  const row = value as Record<string, unknown>;
  if (
    Object.keys(row).length !== 2 ||
    !("model" in row) ||
    !("effort" in row) ||
    !validModel(row.model) ||
    !validEffort(row.effort)
  )
    throw new Error("invalid_response");
  return { model: row.model, effort: row.effort };
}

export function createSessionPrefsApi(
  token: string,
  fetchImpl: typeof fetch = fetch,
): SessionPrefsApi {
  async function request(
    sessionId: string,
    method: "GET" | "PATCH",
    body: string | undefined,
    signal: AbortSignal | undefined,
  ): Promise<unknown> {
    if (!UUID.test(sessionId)) throw new Error("invalid_input");
    if (signal?.aborted) throw new Error("cancelled");
    const controller = new AbortController();
    const relay = (): void => controller.abort();
    signal?.addEventListener("abort", relay, { once: true });
    // AbortController + setTimeout rather than AbortSignal.timeout (api.ts's own note): the
    // latter runs on a timer vitest's fake clock does not own, so it could only be proven by
    // waiting 15 real seconds.
    const timer = setTimeout(relay, TIMEOUT_MS);
    try {
      let response: Response;
      try {
        response = await fetchImpl(`/api/v2/sessions/${sessionId}/prefs`, {
          method,
          signal: controller.signal,
          headers: {
            authorization: `Bearer ${token}`,
            ...(body !== undefined ? { "content-type": "application/json" } : {}),
          },
          ...(body !== undefined ? { body } : {}),
        });
      } catch {
        throw new Error(signal?.aborted ? "cancelled" : "unavailable");
      }
      if (!response.ok) throw new Error("unavailable");
      try {
        return await boundedJson(response, MAX_BYTES);
      } catch {
        throw new Error(signal?.aborted ? "cancelled" : "invalid_response");
      }
    } finally {
      clearTimeout(timer);
      signal?.removeEventListener("abort", relay);
    }
  }
  return {
    async get(sessionId, signal) {
      return parsePrefs(await request(sessionId, "GET", undefined, signal));
    },
    async save(sessionId, changes) {
      return parsePrefs(await request(sessionId, "PATCH", JSON.stringify(changes), undefined));
    },
  };
}
