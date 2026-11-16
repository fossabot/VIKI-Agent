/** Bounded, read-only client for the native agent and skill inspector (server: src/viki_agent/profiles/native_assets_api.py). */

import { boundedJson } from "../boundedResponse";

type NativeAssetScope = "workspace" | "user";
type NativeAssetKind = "agent" | "skill";

export interface NativeSource {
  source_id: string;
  provider: "claude";
  scope: NativeAssetScope;
}

export interface NativeAssetView {
  source_id: string;
  source_scope: NativeAssetScope;
  provider: "claude";
  relative_id: string;
  kind: NativeAssetKind;
  name: string;
  description: string | null;
  model: string | null;
  tools: string[];
  digest: string;
  observed_at: string;
  supported_fields: string[];
  unsupported_fields: string[];
  launch_supported: false;
}

export interface NativeCatalog {
  source_id: string;
  provider: "claude";
  source_scope: NativeAssetScope;
  assets: NativeAssetView[];
  partial: boolean;
  partial_reasons: string[];
  scanned_entries: number;
  scanned_bytes: number;
  omitted: number;
}

export class NativeAssetsApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
  ) {
    super("Native assets are unavailable.");
    this.name = "NativeAssetsApiError";
  }
}

export interface NativeAssetsApi {
  sources(signal?: AbortSignal): Promise<NativeSource[]>;
  catalog(sourceId: string, signal?: AbortSignal): Promise<NativeCatalog>;
}

const MAX_RESPONSE = 2 << 20;
const TIMEOUT = 15_000;
const MAX_ENTRIES = 256;
const MAX_TOTAL_BYTES = 1024 * 1024;
// A short, dotted status code, as every server failure code is (src/viki_agent/profiles/native_assets_api.py's `failure`).
const CODE = /^[a-z][a-z0-9_.:-]{0,127}$/;
const HEX = /^[0-9a-f]{64}$/;
const TIMESTAMP = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/;
// src/viki_agent/profiles/native_assets.py's `_FIELD` regex, for frontmatter keys the reader did not recognize.
const FIELD_NAME = /^[A-Za-z][A-Za-z0-9_-]{0,63}$/;
const SUPPORTED_FIELDS: readonly string[] = ["name", "description", "model", "tools"];
const PARTIAL_REASONS: readonly string[] = [
  "deadline",
  "entry_limit",
  "byte_limit",
  "oversized",
  "unsafe_entry",
  "unreadable",
  "changed",
  "invalid_encoding",
  "malformed_metadata",
];

function object(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
function exact(value: unknown, keys: readonly string[]): value is Record<string, unknown> {
  return (
    object(value) && Object.keys(value).length === keys.length && keys.every((key) => key in value)
  );
}
function text(value: unknown, max = 512, min = 1): value is string {
  if (typeof value !== "string" || !value.isWellFormed()) return false;
  const bytes = new TextEncoder().encode(value).length;
  return bytes >= min && bytes <= max;
}
function nullableText(value: unknown, max = 512): value is string | null {
  return value === null || text(value, max);
}
function scope(value: unknown): value is NativeAssetScope {
  return value === "workspace" || value === "user";
}
function nonNegativeInt(value: unknown, max: number): value is number {
  return Number.isSafeInteger(value) && (value as number) >= 0 && (value as number) <= max;
}
function uniqueStrings(value: unknown, max: number, test: (item: string) => boolean): boolean {
  return (
    Array.isArray(value) &&
    value.length <= max &&
    value.every((item) => typeof item === "string" && test(item)) &&
    new Set(value).size === value.length
  );
}
function source(value: unknown): value is NativeSource {
  return (
    exact(value, ["source_id", "provider", "scope"]) &&
    text(value.source_id, 80) &&
    value.provider === "claude" &&
    scope(value.scope)
  );
}
function asset(value: unknown): value is NativeAssetView {
  return (
    exact(value, [
      "source_id",
      "source_scope",
      "provider",
      "relative_id",
      "kind",
      "name",
      "description",
      "model",
      "tools",
      "digest",
      "observed_at",
      "supported_fields",
      "unsupported_fields",
      "launch_supported",
    ]) &&
    text(value.source_id, 80) &&
    scope(value.source_scope) &&
    value.provider === "claude" &&
    text(value.relative_id, 4096) &&
    (value.kind === "agent" || value.kind === "skill") &&
    text(value.name, 128) &&
    nullableText(value.description, 2048) &&
    nullableText(value.model, 128) &&
    uniqueStrings(value.tools, 64, (item) => text(item, 128)) &&
    typeof value.digest === "string" &&
    HEX.test(value.digest) &&
    typeof value.observed_at === "string" &&
    TIMESTAMP.test(value.observed_at) &&
    uniqueStrings(value.supported_fields, SUPPORTED_FIELDS.length, (item) =>
      SUPPORTED_FIELDS.includes(item),
    ) &&
    uniqueStrings(value.unsupported_fields, MAX_ENTRIES, (item) => FIELD_NAME.test(item)) &&
    value.launch_supported === false
  );
}
function parseCatalog(value: unknown): NativeCatalog | null {
  if (
    !exact(value, [
      "source_id",
      "provider",
      "source_scope",
      "assets",
      "partial",
      "partial_reasons",
      "scanned_entries",
      "scanned_bytes",
      "omitted",
    ]) ||
    !text(value.source_id, 80) ||
    value.provider !== "claude" ||
    !scope(value.source_scope) ||
    !Array.isArray(value.assets) ||
    value.assets.length > MAX_ENTRIES ||
    !value.assets.every(asset) ||
    typeof value.partial !== "boolean" ||
    !uniqueStrings(value.partial_reasons, PARTIAL_REASONS.length, (item) =>
      PARTIAL_REASONS.includes(item),
    ) ||
    !nonNegativeInt(value.scanned_entries, MAX_ENTRIES) ||
    !nonNegativeInt(value.scanned_bytes, MAX_TOTAL_BYTES) ||
    // The server can add one to `omitted` per partial reason on top of its entry cap.
    !nonNegativeInt(value.omitted, MAX_ENTRIES + PARTIAL_REASONS.length)
  )
    return null;
  return value as unknown as NativeCatalog;
}

function serverError(status: number, body: unknown): NativeAssetsApiError {
  const candidate = object(body) && object(body.error) ? body.error.code : null;
  return new NativeAssetsApiError(
    status,
    typeof candidate === "string" && CODE.test(candidate) ? candidate : "unavailable",
  );
}

export function createNativeAssetsApi(
  token: string,
  fetchImpl: typeof fetch = fetch,
): NativeAssetsApi {
  async function request(path: string, signal?: AbortSignal): Promise<unknown> {
    if (signal?.aborted) throw new NativeAssetsApiError(0, "cancelled");
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
        throw new NativeAssetsApiError(0, "invalid_response");
      }
      if (!response.ok) throw serverError(response.status, parsed);
      return parsed;
    } catch (error) {
      if (error instanceof NativeAssetsApiError) throw error;
      if (signal?.aborted) throw new NativeAssetsApiError(0, "cancelled");
      if (timedOut) throw new NativeAssetsApiError(0, "timeout");
      throw new NativeAssetsApiError(0, "unavailable");
    } finally {
      clearTimeout(timer);
      signal?.removeEventListener("abort", relay);
    }
  }
  return {
    async sources(signal) {
      const raw = await request("/api/v2/native-assets", signal);
      if (
        !exact(raw, ["sources"]) ||
        !Array.isArray(raw.sources) ||
        raw.sources.length > 2 ||
        !raw.sources.every(source)
      )
        throw new NativeAssetsApiError(0, "invalid_response");
      return raw.sources as NativeSource[];
    },
    async catalog(sourceId, signal) {
      if (!text(sourceId, 80)) throw new NativeAssetsApiError(0, "invalid_input");
      const raw = await request(
        `/api/v2/native-assets?source_id=${encodeURIComponent(sourceId)}`,
        signal,
      );
      const parsed = parseCatalog(raw);
      if (!parsed || parsed.source_id !== sourceId)
        throw new NativeAssetsApiError(0, "invalid_response");
      return parsed;
    },
  };
}
