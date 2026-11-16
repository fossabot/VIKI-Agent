/** Private, revision-bound search. No native paths or caller-supplied source sessions. */

import type { ArtifactDetailRef, HistoryItem } from "../../protocol";
import { boundedJson } from "../boundedResponse";

export interface NativeAnchor {
  kind: "native_entry";
  entry_uuid: string;
  mapped_index: number;
  source_revision: string;
}
export interface MatchSnapshot {
  before: string;
  match: string;
  after: string;
}
export interface NativeSearchHit {
  role: "user" | "assistant";
  at: string;
  snapshot: MatchSnapshot;
  anchor: NativeAnchor;
}
export interface ConversationSearchResult {
  hits: NativeSearchHit[];
  state: "ready" | "partial" | "superseded" | "error";
  reason: string;
  coverage: "complete" | "tail";
  request_id: string;
}
export interface HistoryAnchorPage {
  request_id: string;
  state: "ready" | "changed" | "missing" | "partial" | "error";
  reason: string;
  events: HistoryItem[];
  anchor_turn_index: number | null;
  older_before_uuid: string | null;
  newer_after_uuid: string | null;
  coverage: "complete" | "tail";
}
export interface ArtifactDetailPage {
  request_id: string;
  state: "ready" | "changed" | "missing" | "partial" | "error";
  reason: string;
  text: string;
  next_cursor: number | null;
  byte_size: number;
  truncated: boolean;
}
export interface ToolSourceWebFetch {
  tool_use_id: string;
  tool: "WebFetch";
  url: string;
}
export interface ToolSourceGrepGlob {
  tool_use_id: string;
  tool: "Grep" | "Glob";
  pattern: string;
  path: string;
  path_state: "workspace_relative" | "root" | "unknown";
}
export type ToolSource = ToolSourceWebFetch | ToolSourceGrepGlob;
export interface ToolSourcesResult {
  request_id: string;
  state: "ready" | "partial" | "missing" | "error";
  reason: string;
  items: ToolSource[];
}
export interface ConversationSearchApi {
  search(
    sessionId: string,
    query: string,
    requestId: string,
    signal?: AbortSignal,
  ): Promise<ConversationSearchResult>;
  anchor(
    sessionId: string,
    anchor: NativeAnchor,
    requestId: string,
    signal?: AbortSignal,
  ): Promise<HistoryAnchorPage>;
  artifactDetail?(
    sessionId: string,
    ref: ArtifactDetailRef,
    cursor: number,
    requestId: string,
    signal?: AbortSignal,
  ): Promise<ArtifactDetailPage>;
  toolSources?(
    sessionId: string,
    toolUseIds: string[],
    requestId: string,
    signal?: AbortSignal,
  ): Promise<ToolSourcesResult>;
}

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const HASH = /^[0-9a-f]{64}$/;
const encoder = new TextEncoder();
const SEARCH_REASONS: Record<string, readonly string[]> = {
  ready: [""],
  partial: ["tail_budget", "response_budget", "time_budget"],
  error: ["busy", "read_failed", "transcript_missing"],
  superseded: ["cancelled"],
};
const ANCHOR_REASONS: Record<string, readonly string[]> = {
  ready: [""],
  changed: ["source_changed"],
  missing: ["entry_missing", "transcript_missing"],
  partial: ["outside_window"],
  error: ["busy", "read_failed", "too_large", "time_budget"],
};
const invalid = (): never => {
  throw new Error("invalid_response");
};
function object(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) return invalid();
  return value as Record<string, unknown>;
}
function text(value: unknown, maximum: number): string {
  if (typeof value !== "string" || [...value].length > maximum) return invalid();
  return value;
}
function uuid(value: unknown): string {
  const id = text(value, 36);
  return UUID.test(id) ? id : invalid();
}
function cursor(value: unknown): string | null {
  return value === null ? null : uuid(value);
}
function coverage(value: unknown): "complete" | "tail" {
  return value === "complete" || value === "tail" ? value : invalid();
}
function parseAnchor(value: unknown): NativeAnchor {
  const row = object(value);
  if (
    row.kind !== "native_entry" ||
    !Number.isSafeInteger(row.mapped_index) ||
    (row.mapped_index as number) < 0 ||
    (row.mapped_index as number) > 63 ||
    typeof row.source_revision !== "string" ||
    !HASH.test(row.source_revision)
  )
    return invalid();
  return {
    kind: "native_entry",
    entry_uuid: uuid(row.entry_uuid),
    mapped_index: row.mapped_index as number,
    source_revision: row.source_revision,
  };
}
function parseSearch(value: unknown, requestId: string): ConversationSearchResult {
  const row = object(value);
  if (
    row.request_id !== requestId ||
    !["ready", "partial", "superseded", "error"].includes(row.state as string) ||
    !SEARCH_REASONS[row.state as string]?.includes(row.reason as string) ||
    !Array.isArray(row.hits) ||
    row.hits.length > 100
  )
    return invalid();
  if (
    ((row.state === "error" || row.state === "superseded") && row.hits.length) ||
    (row.reason === "tail_budget" && row.coverage !== "tail") ||
    (row.state === "ready" && row.coverage !== "complete")
  )
    return invalid();
  const hits = row.hits.map((raw): NativeSearchHit => {
    const hit = object(raw),
      snapshot = object(hit.snapshot);
    if (hit.role !== "user" && hit.role !== "assistant") return invalid();
    return {
      role: hit.role,
      at: text(hit.at, 64),
      snapshot: {
        before: text(snapshot.before, 120),
        match: text(snapshot.match, 256),
        after: text(snapshot.after, 120),
      },
      anchor: parseAnchor(hit.anchor),
    };
  });
  return {
    hits,
    state: row.state as ConversationSearchResult["state"],
    reason: row.reason as string,
    coverage: coverage(row.coverage),
    request_id: requestId,
  };
}
function parsePage(value: unknown, requestId: string): HistoryAnchorPage {
  const row = object(value);
  if (
    row.request_id !== requestId ||
    !["ready", "changed", "missing", "partial", "error"].includes(row.state as string) ||
    !ANCHOR_REASONS[row.state as string]?.includes(row.reason as string) ||
    !Array.isArray(row.events) ||
    row.events.length > 20_000
  )
    return invalid();
  // Events originate from the same authenticated mapper as history.page. Require envelopes,
  // never treat caller-supplied JSON/HTML as a rendered archive or executable component.
  const events = row.events.map((raw) => {
    const item = object(raw);
    text(item.type, 64);
    object(item.data);
    return item as unknown as HistoryItem;
  });
  const index = row.anchor_turn_index;
  if (row.state === "ready") {
    if (
      !Number.isSafeInteger(index) ||
      (index as number) < 0 ||
      (index as number) >= 20 ||
      !events.length
    )
      return invalid();
  } else if (
    index !== null ||
    events.length ||
    row.older_before_uuid !== null ||
    row.newer_after_uuid !== null
  )
    return invalid();
  if (row.state === "partial" && row.coverage !== "tail") return invalid();
  return {
    request_id: requestId,
    state: row.state as HistoryAnchorPage["state"],
    reason: row.reason as string,
    events,
    anchor_turn_index: index as number | null,
    older_before_uuid: cursor(row.older_before_uuid),
    newer_after_uuid: cursor(row.newer_after_uuid),
    coverage: coverage(row.coverage),
  };
}

function parseDetail(value: unknown, requestId: string, cursor: number): ArtifactDetailPage {
  const row = object(value);
  if (
    row.request_id !== requestId ||
    !["ready", "changed", "missing", "partial", "error"].includes(row.state as string) ||
    !ANCHOR_REASONS[row.state as string]?.includes(row.reason as string) ||
    typeof row.text !== "string" ||
    encoder.encode(row.text).byteLength > 65536 ||
    !Number.isSafeInteger(row.byte_size) ||
    (row.byte_size as number) < 0 ||
    typeof row.truncated !== "boolean"
  )
    return invalid();
  const next = row.next_cursor;
  if (
    next !== null &&
    (!Number.isSafeInteger(next) ||
      (next as number) <= cursor ||
      (next as number) > 8 * 1024 * 1024)
  )
    return invalid();
  // Unresolved states mirror the anchor route: they carry no text or continuation.
  if (
    row.state !== "ready" &&
    (row.text !== "" || next !== null || row.byte_size !== 0 || row.truncated)
  )
    return invalid();
  if (row.state === "ready") {
    const end = cursor + encoder.encode(row.text).byteLength;
    if (
      end > (row.byte_size as number) ||
      end > 8 * 1024 * 1024 ||
      (next !== null && (next !== end || row.truncated || end >= (row.byte_size as number))) ||
      (next === null && row.truncated !== end < (row.byte_size as number))
    )
      return invalid();
  }
  return {
    request_id: requestId,
    state: row.state as ArtifactDetailPage["state"],
    reason: row.reason as string,
    text: row.text,
    next_cursor: next as number | null,
    byte_size: row.byte_size as number,
    truncated: row.truncated as boolean,
  };
}

const TOOL_SOURCES_REASONS: Record<string, readonly string[]> = {
  ready: [""],
  partial: ["outside_window"],
  missing: ["transcript_missing"],
  error: ["busy", "read_failed", "time_budget"],
};
const TOOL_SOURCES_KEYS = ["request_id", "state", "reason", "items"] as const;
const WEB_FETCH_KEYS = ["tool_use_id", "tool", "url"] as const;
const GREP_GLOB_KEYS = ["tool_use_id", "tool", "pattern", "path", "path_state"] as const;
const exactKeys = (value: Record<string, unknown>, keys: readonly string[]): boolean =>
  Object.keys(value).length === keys.length && keys.every((key) => Object.hasOwn(value, key));

function parseToolSource(value: unknown, askedIds: Set<string>): ToolSource {
  const item = object(value);
  const id = text(item.tool_use_id, 160);
  if (encoder.encode(id).byteLength > 512 || !askedIds.has(id)) return invalid();

  const tool = item.tool;
  if (tool === "WebFetch") {
    if (!exactKeys(item, WEB_FETCH_KEYS)) return invalid();
    const url = text(item.url, 2048);
    const httpMatch = /^https?:\/\//i;
    if (!httpMatch.test(url) || /[\s\p{Cc}]/u.test(url) || url.includes("\\")) return invalid();
    return {
      tool_use_id: id,
      tool: "WebFetch",
      url,
    };
  } else if (tool === "Grep" || tool === "Glob") {
    if (!exactKeys(item, GREP_GLOB_KEYS)) return invalid();
    const pattern = text(item.pattern, 160);
    const path = text(item.path, 512);
    const pathState = item.path_state;
    if (!["workspace_relative", "root", "unknown"].includes(pathState as string)) return invalid();
    if (pathState === "workspace_relative") {
      if (path === "" || path.startsWith("/") || path.includes("\\") || path.includes(":"))
        return invalid();
      const segments = path.split("/");
      for (const seg of segments) {
        if (seg === "." || seg === "..") return invalid();
      }
    } else if (path !== "") {
      return invalid();
    }
    return {
      tool_use_id: id,
      tool,
      pattern,
      path,
      path_state: pathState as ToolSourceGrepGlob["path_state"],
    };
  }
  return invalid();
}

function parseToolSources(
  value: unknown,
  requestId: string,
  askedIds: Set<string>,
): ToolSourcesResult {
  const row = object(value);
  if (
    !exactKeys(row, TOOL_SOURCES_KEYS) ||
    row.request_id !== requestId ||
    !["ready", "partial", "missing", "error"].includes(row.state as string) ||
    !TOOL_SOURCES_REASONS[row.state as string]?.includes(row.reason as string) ||
    !Array.isArray(row.items)
  )
    return invalid();
  const items: ToolSource[] = [];
  const seenIds = new Set<string>();
  for (const raw of row.items) {
    const item = parseToolSource(raw, askedIds);
    if (seenIds.has(item.tool_use_id)) return invalid();
    items.push(item);
    seenIds.add(item.tool_use_id);
  }
  return {
    request_id: requestId,
    state: row.state as ToolSourcesResult["state"],
    reason: row.reason as string,
    items,
  };
}

export function createConversationSearchApi(
  token: string,
  fetchImpl: typeof fetch = fetch,
): ConversationSearchApi {
  async function request<T>(
    sessionId: string,
    requestId: string,
    suffix: string,
    maximum: number,
    parse: (value: unknown, id: string) => T,
    signal?: AbortSignal,
    body?: object,
  ): Promise<T> {
    if (!UUID.test(sessionId) || !UUID.test(requestId)) throw new Error("invalid_request");
    if (signal?.aborted) throw new Error("cancelled");
    const controller = new AbortController();
    const abort = () => controller.abort();
    signal?.addEventListener("abort", abort, { once: true });
    const timer = setTimeout(abort, 30_000);
    try {
      const response = await fetchImpl(`/api/v2/sessions/${sessionId}/${suffix}`, {
        method: body ? "POST" : "GET",
        headers: {
          authorization: `Bearer ${token}`,
          ...(body ? { "content-type": "application/json" } : {}),
        },
        signal: controller.signal,
        ...(body ? { body: JSON.stringify(body) } : {}),
      });
      if (!response.ok) throw new Error(response.status === 429 ? "overloaded" : "unavailable");
      return parse(await boundedJson(response, maximum), requestId);
    } finally {
      clearTimeout(timer);
      signal?.removeEventListener("abort", abort);
    }
  }
  return {
    search(sessionId, query, requestId, signal) {
      if (!query.isWellFormed() || !query.trim() || [...query].length > 256)
        return Promise.reject(new Error("invalid_query"));
      return request(
        sessionId,
        requestId,
        `conversation-search?q=${encodeURIComponent(query)}&request_id=${requestId}`,
        256 * 1024,
        parseSearch,
        signal,
      );
    },
    anchor(sessionId, anchor, requestId, signal) {
      return request(sessionId, requestId, "history-anchor", 2 * 1024 * 1024, parsePage, signal, {
        request_id: requestId,
        anchor: parseAnchor(anchor),
      });
    },
    artifactDetail(sessionId, ref, cursor, requestId, signal) {
      const tool = ref.tool_use_id;
      if (
        !Number.isSafeInteger(cursor) ||
        cursor < 0 ||
        cursor > 8 * 1024 * 1024 ||
        typeof tool !== "string" ||
        tool.length === 0 ||
        [...tool].length > 160 ||
        encoder.encode(tool).byteLength > 512
      )
        return Promise.reject(new Error("invalid_request"));
      // Re-validate the ref as the anchor it synthesizes; mapped_index is unused by this route.
      parseAnchor({
        kind: "native_entry",
        entry_uuid: ref.entry_uuid,
        mapped_index: 0,
        source_revision: ref.source_revision,
      });
      return request(
        sessionId,
        requestId,
        "artifact-detail",
        512 * 1024,
        (value, id) => parseDetail(value, id, cursor),
        signal,
        { request_id: requestId, ref, cursor },
      );
    },
    toolSources(sessionId, toolUseIds, requestId, signal) {
      const idSet = new Set<string>();
      for (const id of toolUseIds) {
        if (
          typeof id !== "string" ||
          id === "" ||
          [...id].length > 160 ||
          encoder.encode(id).byteLength > 512
        )
          return Promise.reject(new Error("invalid_request"));
        idSet.add(id);
      }
      if (idSet.size < 1 || idSet.size > 16) return Promise.reject(new Error("invalid_request"));
      return request(
        sessionId,
        requestId,
        "tool-sources",
        512 * 1024,
        (value, id) => parseToolSources(value, id, idSet),
        signal,
        { request_id: requestId, tool_use_ids: Array.from(idSet) },
      );
    },
  };
}
