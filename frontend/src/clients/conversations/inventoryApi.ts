/** Bounded, read-only client for a live chat's tool inventory (server: src/viki_agent/conversations/inventory_api.py). */

import { boundedJson } from "../boundedResponse";

type InventoryPart =
  | "mcp_servers"
  | "mcp_tools"
  | "skills"
  | "memory_files"
  | "system_tools"
  | "agents";

export type InventoryReason =
  | "not_observed"
  | "absent"
  | "unrecognized"
  | "timeout"
  | "unavailable";

export type McpServerStatus = "connected" | "failed" | "needs-auth" | "pending" | "disabled";

export interface McpServerRow {
  name: string;
  status: McpServerStatus;
  scope: string | null;
  source: string | null;
  server_name: string | null;
  server_version: string | null;
  egress: string | null;
  tools: string[] | null;
  tools_omitted: boolean;
}

interface McpToolRow {
  name: string;
  server: string;
  loaded: boolean;
}

interface MemoryFileRow {
  path: string;
  type: string;
}

interface AgentRow {
  type: string;
  source: string;
}

export interface ToolInventory {
  observed_at: string | null;
  mcp_servers: McpServerRow[] | null;
  mcp_tools: McpToolRow[] | null;
  skills: string[] | null;
  memory_files: MemoryFileRow[] | null;
  system_tools: string[] | null;
  agents: AgentRow[] | null;
  reasons: Partial<Record<InventoryPart, InventoryReason>>;
}

export class InventoryApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
  ) {
    super("The tool inventory is unavailable.");
    this.name = "InventoryApiError";
  }
}

export interface InventoryApi {
  get(sessionId: string, signal?: AbortSignal): Promise<ToolInventory>;
}

const MAX_RESPONSE = 1 << 20;
const TIMEOUT = 15_000;
const MAX_SERVERS = 64;
const MAX_ENTRIES = 256;
const MAX_STRING_CHARS = 256;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
// A short, dotted status code, as every server failure code is (src/viki_agent/conversations/inventory_api.py's error model).
const CODE = /^[a-z][a-z0-9_.:-]{0,127}$/;
const STATUSES: readonly McpServerStatus[] = [
  "connected",
  "failed",
  "needs-auth",
  "pending",
  "disabled",
];
const REASONS: readonly InventoryReason[] = [
  "not_observed",
  "absent",
  "unrecognized",
  "timeout",
  "unavailable",
];
const PARTS: readonly InventoryPart[] = [
  "mcp_servers",
  "mcp_tools",
  "skills",
  "memory_files",
  "system_tools",
  "agents",
];

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
function status(value: unknown): value is McpServerStatus {
  return typeof value === "string" && (STATUSES as readonly string[]).includes(value);
}
function reason(value: unknown): value is InventoryReason {
  return typeof value === "string" && (REASONS as readonly string[]).includes(value);
}
function stringList(value: unknown, max: number): value is string[] {
  return Array.isArray(value) && value.length <= max && value.every((item) => text(item));
}
function server(value: unknown): value is McpServerRow {
  return (
    exact(value, [
      "name",
      "status",
      "scope",
      "source",
      "server_name",
      "server_version",
      "egress",
      "tools",
      "tools_omitted",
    ]) &&
    text(value.name) &&
    status(value.status) &&
    nullableText(value.scope) &&
    nullableText(value.source) &&
    nullableText(value.server_name) &&
    nullableText(value.server_version) &&
    nullableText(value.egress) &&
    (value.tools === null || stringList(value.tools, MAX_ENTRIES)) &&
    typeof value.tools_omitted === "boolean"
  );
}
function tool(value: unknown): value is McpToolRow {
  return (
    exact(value, ["name", "server", "loaded"]) &&
    text(value.name) &&
    text(value.server) &&
    typeof value.loaded === "boolean"
  );
}
function memoryFile(value: unknown): value is MemoryFileRow {
  return exact(value, ["path", "type"]) && text(value.path) && text(value.type);
}
function agent(value: unknown): value is AgentRow {
  return exact(value, ["type", "source"]) && text(value.type) && text(value.source);
}
function rowList<T>(value: unknown, max: number, row: (item: unknown) => item is T): value is T[] {
  return Array.isArray(value) && value.length <= max && value.every((item) => row(item));
}

function parseInventory(value: unknown): ToolInventory | null {
  if (
    !exact(value, [
      "observed_at",
      "mcp_servers",
      "mcp_tools",
      "skills",
      "memory_files",
      "system_tools",
      "agents",
      "reasons",
    ]) ||
    !nullableText(value.observed_at) ||
    !(value.mcp_servers === null || rowList(value.mcp_servers, MAX_SERVERS, server)) ||
    !(value.mcp_tools === null || rowList(value.mcp_tools, MAX_ENTRIES, tool)) ||
    !(value.skills === null || stringList(value.skills, MAX_ENTRIES)) ||
    !(value.memory_files === null || rowList(value.memory_files, MAX_ENTRIES, memoryFile)) ||
    !(value.system_tools === null || stringList(value.system_tools, MAX_ENTRIES)) ||
    !(value.agents === null || rowList(value.agents, MAX_ENTRIES, agent)) ||
    !object(value.reasons)
  )
    return null;
  const reasons = value.reasons as Record<string, unknown>;
  const reasonKeys = Object.keys(reasons);
  if (
    !reasonKeys.every((key) => (PARTS as readonly string[]).includes(key) && reason(reasons[key]))
  )
    return null;
  const nullParts = PARTS.filter((part) => value[part] === null);
  if (
    nullParts.length !== reasonKeys.length ||
    !nullParts.every((part) => reasonKeys.includes(part))
  )
    return null;
  return value as unknown as ToolInventory;
}

function serverError(status_: number, body: unknown): InventoryApiError {
  const candidate = object(body) && object(body.error) ? body.error.code : null;
  return new InventoryApiError(
    status_,
    typeof candidate === "string" && CODE.test(candidate) ? candidate : "unavailable",
  );
}

export function createInventoryApi(token: string, fetchImpl: typeof fetch = fetch): InventoryApi {
  async function request(path: string, signal?: AbortSignal): Promise<unknown> {
    if (signal?.aborted) throw new InventoryApiError(0, "cancelled");
    const controller = new AbortController();
    const relay = (): void => controller.abort();
    signal?.addEventListener("abort", relay, { once: true });
    let timedOut = false;
    const timer = setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, TIMEOUT);
    try {
      const response = await fetchImpl(path, {
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
        throw new InventoryApiError(0, "invalid_response");
      }
      if (!response.ok) throw serverError(response.status, parsed);
      return parsed;
    } catch (error) {
      if (error instanceof InventoryApiError) throw error;
      if (signal?.aborted) throw new InventoryApiError(0, "cancelled");
      if (timedOut) throw new InventoryApiError(0, "timeout");
      throw new InventoryApiError(0, "unavailable");
    } finally {
      clearTimeout(timer);
      signal?.removeEventListener("abort", relay);
    }
  }
  return {
    async get(sessionId, signal) {
      if (!UUID.test(sessionId)) throw new InventoryApiError(0, "invalid_input");
      const raw = await request(`/api/v2/sessions/${sessionId}/inventory`, signal);
      const parsed = parseInventory(raw);
      if (!parsed) throw new InventoryApiError(0, "invalid_response");
      return parsed;
    },
  };
}
