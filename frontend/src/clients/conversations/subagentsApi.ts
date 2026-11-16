/**
 * Bounded, read-only client for a subagent run's own transcript — the server's subagent
 * transcript route (server: src/viki_agent/conversations/subagents_api.py). `GET /api/v2/sessions/{session_id}/subagents/
 * {block_id}`, bearer-authenticated, read-only. The right rail's Peek and row-stats poller
 * (runStats.ts) are its consumers. Every rejection is a plain `Error` whose message is one of a
 * fixed vocabulary (`invalid_input`, `not_found`, `overloaded`, `unavailable`, `cancelled`,
 * `invalid_response`) — the server's own text is never kept.
 */
import { boundedJson } from "../boundedResponse";

export type PeekLineStatus = "running" | "completed" | "failed" | "stopped";

export type PeekLine =
  | { kind: "text"; id: string; text: string }
  | {
      kind: "tool";
      id: string;
      head: string;
      rest: string;
      tail: string;
      done: boolean;
      error: boolean;
    }
  | { kind: "agent"; id: string; agent: string; description: string; status: PeekLineStatus }
  | { kind: "message"; id: string; text: string };

export interface RunTranscript {
  prompt: string;
  lines: PeekLine[];
  omitted: number;
  partial: boolean;
  startedAt: number | null;
  tokens: number | null;
}

export interface SubagentsApi {
  read(sessionId: string, blockId: string, signal?: AbortSignal): Promise<RunTranscript>;
}

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const BLOCK_ID = /^[A-Za-z0-9_-]{1,128}$/;
const TIMEOUT_MS = 15_000;
const MAX_RESPONSE_BYTES = 1 << 20;
const MAX_LINES = 1_000;
const MAX_FREE_TEXT = 65_536;
const MAX_STR = 4_096;
const MAX_STARTED_AT = 64;
const STATUSES: readonly PeekLineStatus[] = ["running", "completed", "failed", "stopped"];

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
function nonNegInt(value: unknown): value is number {
  return typeof value === "number" && Number.isInteger(value) && value >= 0;
}
function peekStatus(value: unknown): value is PeekLineStatus {
  return typeof value === "string" && (STATUSES as readonly string[]).includes(value);
}

function parseLine(value: unknown): PeekLine | null {
  if (!object(value) || typeof value.kind !== "string") return null;
  const kind = value.kind;
  switch (kind) {
    case "text":
    case "message": {
      if (!exact(value, ["kind", "id", "text"])) return null;
      const { id, text } = value;
      if (!str(id, MAX_STR) || !str(text, MAX_FREE_TEXT)) return null;
      return kind === "text" ? { kind: "text", id, text } : { kind: "message", id, text };
    }
    case "tool": {
      if (!exact(value, ["kind", "id", "head", "rest", "tail", "done", "error"])) return null;
      const { id, head, rest, tail, done, error } = value;
      if (!str(id, MAX_STR) || !str(head, MAX_STR) || !str(rest, MAX_STR) || !str(tail, MAX_STR))
        return null;
      if (typeof done !== "boolean" || typeof error !== "boolean") return null;
      return { kind: "tool", id, head, rest, tail, done, error };
    }
    case "agent": {
      if (!exact(value, ["kind", "id", "agent", "description", "status"])) return null;
      const { id, agent, description, status } = value;
      if (!str(id, MAX_STR) || !str(agent, MAX_STR) || !str(description, MAX_STR)) return null;
      if (!peekStatus(status)) return null;
      return { kind: "agent", id, agent, description, status };
    }
    default:
      return null;
  }
}

function parseTranscript(value: unknown): RunTranscript | null {
  if (!exact(value, ["prompt", "lines", "omitted", "partial", "started_at", "tokens"])) return null;
  const {
    prompt,
    lines: rawLines,
    omitted,
    partial,
    started_at: startedAtRaw,
    tokens: tokensRaw,
  } = value;
  if (!str(prompt, MAX_FREE_TEXT)) return null;
  if (!Array.isArray(rawLines) || rawLines.length > MAX_LINES) return null;
  const lines: PeekLine[] = [];
  for (const raw of rawLines) {
    const line = parseLine(raw);
    if (!line) return null;
    lines.push(line);
  }
  if (!nonNegInt(omitted)) return null;
  if (typeof partial !== "boolean") return null;
  if (!str(startedAtRaw, MAX_STARTED_AT)) return null;
  if (tokensRaw !== null && !nonNegInt(tokensRaw)) return null;
  const startedAtMs = Date.parse(startedAtRaw);
  return {
    prompt,
    lines,
    omitted,
    partial,
    startedAt: startedAtRaw === "" || Number.isNaN(startedAtMs) ? null : startedAtMs,
    tokens: tokensRaw === null ? null : tokensRaw,
  };
}

export function createSubagentsApi(token: string, fetchImpl: typeof fetch = fetch): SubagentsApi {
  return {
    async read(sessionId, blockId, signal) {
      if (!UUID.test(sessionId) || !BLOCK_ID.test(blockId)) throw new Error("invalid_input");
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
          response = await fetchImpl(`/api/v2/sessions/${sessionId}/subagents/${blockId}`, {
            method: "GET",
            signal: controller.signal,
            headers: { authorization: `Bearer ${token}` },
          });
        } catch {
          throw new Error(signal?.aborted ? "cancelled" : "unavailable");
        }
        if (!response.ok) {
          if (response.status === 404) throw new Error("not_found");
          if (response.status === 429) throw new Error("overloaded");
          throw new Error("unavailable");
        }
        let raw: unknown;
        try {
          raw = await boundedJson(response, MAX_RESPONSE_BYTES);
        } catch {
          // A caller cancel or our own timeout can also land here, mid-body-read; sort those
          // into the same buckets the fetch failure above uses rather than invalid_response.
          if (controller.signal.aborted)
            throw new Error(signal?.aborted ? "cancelled" : "unavailable");
          throw new Error("invalid_response");
        }
        const parsed = parseTranscript(raw);
        if (!parsed) throw new Error("invalid_response");
        return parsed;
      } finally {
        clearTimeout(timer);
        signal?.removeEventListener("abort", relay);
      }
    },
  };
}
