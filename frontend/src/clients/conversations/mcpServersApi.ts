/**
 * Client for the MCP servers toggle and reconnect actions (server: `src/viki_agent/conversations/mcp_api.py`).
 * Both routes answer `{"status":"done"}` on success; every rejection is a `McpServersApiError`
 * whose `.code` is one of a fixed vocabulary — the server's own message text is never kept.
 */
import { boundedJson } from "../boundedResponse";

export type McpServersErrorCode =
  | "unauthorized"
  | "forbidden_origin"
  | "invalid_input"
  | "overloaded"
  | "not_found"
  | "not_running"
  | "busy"
  | "unknown_server"
  | "managed"
  | "not_approved"
  | "blocked"
  | "refused"
  | "not_connected"
  | "timeout"
  | "cancelled"
  | "invalid_response"
  | "unavailable";

export class McpServersApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: McpServersErrorCode,
  ) {
    super(code);
    this.name = "McpServersApiError";
  }
}

export interface McpServersApi {
  setEnabled(sessionId: string, name: string, enabled: boolean): Promise<void>;
  reconnect(sessionId: string, name: string): Promise<void>;
}

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const TIMEOUT = 60_000;
const MAX_RESPONSE = 4 << 10; // 4 KiB

const KNOWN_CODES = new Set<McpServersErrorCode>([
  "unauthorized",
  "forbidden_origin",
  "invalid_input",
  "overloaded",
  "not_found",
  "not_running",
  "busy",
  "unknown_server",
  "managed",
  "not_approved",
  "blocked",
  "refused",
  "not_connected",
  "timeout",
  "cancelled",
  "invalid_response",
  "unavailable",
]);

function object(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
function exact(value: unknown, keys: readonly string[]): value is Record<string, unknown> {
  return (
    object(value) && Object.keys(value).length === keys.length && keys.every((key) => key in value)
  );
}

/** A non-ok status keeps the body's `error.code` only when it is one of ours, else unavailable. */
function errorCodeFrom(body: unknown): McpServersErrorCode {
  const candidate = object(body) && object(body.error) ? body.error.code : null;
  return typeof candidate === "string" && KNOWN_CODES.has(candidate as McpServersErrorCode)
    ? (candidate as McpServersErrorCode)
    : "unavailable";
}

function isDoneStatus(value: unknown): value is { status: "done" } {
  return exact(value, ["status"]) && value.status === "done";
}

export function createMcpServersApi(token: string, fetchImpl: typeof fetch = fetch): McpServersApi {
  async function send(sessionId: string, path: string, body: unknown): Promise<void> {
    if (!UUID.test(sessionId)) throw new McpServersApiError(0, "invalid_input");
    const controller = new AbortController();
    let timedOut = false;
    const timer = setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, TIMEOUT);
    let raw: unknown;
    try {
      const response = await fetchImpl(path, {
        method: "POST",
        signal: controller.signal,
        headers: {
          authorization: `Bearer ${token}`,
          "content-type": "application/json",
        },
        body: JSON.stringify(body),
      });
      let parsed: unknown;
      try {
        parsed = await boundedJson(response, MAX_RESPONSE);
      } catch (error) {
        if (controller.signal.aborted) throw error;
        if (!response.ok) throw new McpServersApiError(response.status, "unavailable");
        throw new McpServersApiError(0, "invalid_response");
      }
      if (!response.ok) throw new McpServersApiError(response.status, errorCodeFrom(parsed));
      raw = parsed;
    } catch (error) {
      if (error instanceof McpServersApiError) throw error;
      throw new McpServersApiError(0, timedOut ? "timeout" : "unavailable");
    } finally {
      clearTimeout(timer);
    }
    if (!isDoneStatus(raw)) throw new McpServersApiError(0, "invalid_response");
  }

  return {
    async setEnabled(sessionId, name, enabled) {
      await send(sessionId, `/api/v2/sessions/${sessionId}/mcp/toggle`, { name, enabled });
    },
    async reconnect(sessionId, name) {
      await send(sessionId, `/api/v2/sessions/${sessionId}/mcp/reconnect`, { name });
    },
  };
}
