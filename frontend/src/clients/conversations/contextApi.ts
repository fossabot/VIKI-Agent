/** Bounded, read-only client for a live chat's context report (server: src/viki_agent/conversations/context_api.py). */

import { boundedJson } from "../boundedResponse";

export type ContextPart = "categories" | "memory_files" | "mcp_servers" | "skills" | "agents";

export type ContextReason = "not_observed" | "absent" | "unrecognized" | "timeout" | "unavailable";

export type CategoryKind = "used" | "deferred" | "buffer" | "free";

export interface ContextCategory {
  name: string;
  tokens: number;
  kind: CategoryKind;
}

interface ContextMemoryFile {
  name: string;
  type: string;
  tokens: number;
}

interface ContextMcpServer {
  server: string;
  tools: number;
  loaded: number;
  loaded_tokens: number;
}

interface ContextSkillItem {
  name: string;
  source: string;
  plugin: string | null;
  tokens: number;
}

interface ContextSkills {
  total: number;
  included: number;
  tokens: number | null;
  items: ContextSkillItem[];
}

interface ContextAgent {
  type: string;
  source: string;
  tokens: number;
}

interface ContextAutoCompact {
  enabled: boolean;
  threshold: number | null;
  source: string | null;
}

interface ContextHidden {
  categories: number;
  memory_files: number;
  mcp_servers: number;
  skills: number;
  agents: number;
}

export interface ContextReport {
  observed_at: string | null;
  total_tokens: number | null;
  max_tokens: number | null;
  raw_max_tokens: number | null;
  percentage: number | null;
  auto_compact: ContextAutoCompact | null;
  categories: ContextCategory[] | null;
  memory_files: ContextMemoryFile[] | null;
  mcp_servers: ContextMcpServer[] | null;
  skills: ContextSkills | null;
  agents: ContextAgent[] | null;
  reasons: Partial<Record<ContextPart, ContextReason>>;
  hidden: ContextHidden;
}

export class ContextApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
  ) {
    super("The context report is unavailable.");
    this.name = "ContextApiError";
  }
}

export interface ContextApi {
  get(sessionId: string, signal?: AbortSignal): Promise<ContextReport>;
}

const MAX_RESPONSE = 1 << 20;
const TIMEOUT = 15_000;
const MAX_CATEGORIES = 32;
const MAX_SERVERS = 64;
const MAX_ENTRIES = 256;
const MAX_STRING_CHARS = 256;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
// A short, dotted status code, as every server failure code is (src/viki_agent/conversations/inventory_api.py's error model).
const CODE = /^[a-z][a-z0-9_.:-]{0,127}$/;
const KINDS: readonly CategoryKind[] = ["used", "deferred", "buffer", "free"];
const REASONS: readonly ContextReason[] = [
  "not_observed",
  "absent",
  "unrecognized",
  "timeout",
  "unavailable",
];
const PARTS: readonly ContextPart[] = [
  "categories",
  "memory_files",
  "mcp_servers",
  "skills",
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
function count(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0;
}
function nullableCount(value: unknown): value is number | null {
  return value === null || count(value);
}
function nullablePercentage(value: unknown): value is number | null {
  return (
    value === null ||
    (typeof value === "number" && Number.isFinite(value) && value >= 0 && value <= 100)
  );
}
function kind(value: unknown): value is CategoryKind {
  return typeof value === "string" && (KINDS as readonly string[]).includes(value);
}
function reason(value: unknown): value is ContextReason {
  return typeof value === "string" && (REASONS as readonly string[]).includes(value);
}
function rowList<T>(value: unknown, max: number, row: (item: unknown) => item is T): value is T[] {
  return Array.isArray(value) && value.length <= max && value.every((item) => row(item));
}

function category(value: unknown): value is ContextCategory {
  return (
    exact(value, ["name", "tokens", "kind"]) &&
    text(value.name) &&
    count(value.tokens) &&
    kind(value.kind)
  );
}
function memoryFile(value: unknown): value is ContextMemoryFile {
  return (
    exact(value, ["name", "type", "tokens"]) &&
    text(value.name) &&
    text(value.type) &&
    count(value.tokens)
  );
}
function mcpServer(value: unknown): value is ContextMcpServer {
  return (
    exact(value, ["server", "tools", "loaded", "loaded_tokens"]) &&
    text(value.server) &&
    count(value.tools) &&
    count(value.loaded) &&
    count(value.loaded_tokens)
  );
}
function skillItem(value: unknown): value is ContextSkillItem {
  return (
    exact(value, ["name", "source", "plugin", "tokens"]) &&
    text(value.name) &&
    text(value.source) &&
    nullableText(value.plugin) &&
    count(value.tokens)
  );
}
function skills(value: unknown): value is ContextSkills {
  return (
    exact(value, ["total", "included", "tokens", "items"]) &&
    count(value.total) &&
    count(value.included) &&
    nullableCount(value.tokens) &&
    rowList(value.items, MAX_ENTRIES, skillItem)
  );
}
function agent(value: unknown): value is ContextAgent {
  return (
    exact(value, ["type", "source", "tokens"]) &&
    text(value.type) &&
    text(value.source) &&
    count(value.tokens)
  );
}
function autoCompact(value: unknown): value is ContextAutoCompact {
  return (
    exact(value, ["enabled", "threshold", "source"]) &&
    typeof value.enabled === "boolean" &&
    nullableCount(value.threshold) &&
    nullableText(value.source)
  );
}
function hidden(value: unknown): value is ContextHidden {
  return (
    exact(value, ["categories", "memory_files", "mcp_servers", "skills", "agents"]) &&
    count(value.categories) &&
    count(value.memory_files) &&
    count(value.mcp_servers) &&
    count(value.skills) &&
    count(value.agents)
  );
}

function parseContext(value: unknown): ContextReport | null {
  if (
    !exact(value, [
      "observed_at",
      "total_tokens",
      "max_tokens",
      "raw_max_tokens",
      "percentage",
      "auto_compact",
      "categories",
      "memory_files",
      "mcp_servers",
      "skills",
      "agents",
      "reasons",
      "hidden",
    ]) ||
    !nullableText(value.observed_at) ||
    !nullableCount(value.total_tokens) ||
    !nullableCount(value.max_tokens) ||
    !nullableCount(value.raw_max_tokens) ||
    !nullablePercentage(value.percentage) ||
    !(value.auto_compact === null || autoCompact(value.auto_compact)) ||
    !(value.categories === null || rowList(value.categories, MAX_CATEGORIES, category)) ||
    !(value.memory_files === null || rowList(value.memory_files, MAX_ENTRIES, memoryFile)) ||
    !(value.mcp_servers === null || rowList(value.mcp_servers, MAX_SERVERS, mcpServer)) ||
    !(value.skills === null || skills(value.skills)) ||
    !(value.agents === null || rowList(value.agents, MAX_ENTRIES, agent)) ||
    !object(value.reasons) ||
    !hidden(value.hidden)
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
  return value as unknown as ContextReport;
}

function serverError(status_: number, body: unknown): ContextApiError {
  const candidate = object(body) && object(body.error) ? body.error.code : null;
  return new ContextApiError(
    status_,
    typeof candidate === "string" && CODE.test(candidate) ? candidate : "unavailable",
  );
}

export function createContextApi(token: string, fetchImpl: typeof fetch = fetch): ContextApi {
  async function request(path: string, signal?: AbortSignal): Promise<unknown> {
    if (signal?.aborted) throw new ContextApiError(0, "cancelled");
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
        throw new ContextApiError(0, "invalid_response");
      }
      if (!response.ok) throw serverError(response.status, parsed);
      return parsed;
    } catch (error) {
      if (error instanceof ContextApiError) throw error;
      if (signal?.aborted) throw new ContextApiError(0, "cancelled");
      if (timedOut) throw new ContextApiError(0, "timeout");
      throw new ContextApiError(0, "unavailable");
    } finally {
      clearTimeout(timer);
      signal?.removeEventListener("abort", relay);
    }
  }
  return {
    async get(sessionId, signal) {
      if (!UUID.test(sessionId)) throw new ContextApiError(0, "invalid_input");
      const raw = await request(`/api/v2/sessions/${sessionId}/context`, signal);
      const parsed = parseContext(raw);
      if (!parsed) throw new ContextApiError(0, "invalid_response");
      return parsed;
    },
  };
}
