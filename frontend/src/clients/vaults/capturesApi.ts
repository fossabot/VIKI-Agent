import { boundedJson } from "../boundedResponse";

const MAX = 2 * 1024 * 1024;
const TIMEOUT = 15_000;
// Every timestamp the server sends is its 24-character UTC form, a string.
const STAMP = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/;
const HEX64 = /^[0-9a-f]{64}$/;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const CAPTURES_MAX = 100;
const REF_MAX = 512;
const PREVIEW_MAX = 600;
const WHY_MAX = 200;
const SHORT_MAX = 64; // captured, reason, unread

export const DESTINATIONS = [
  "knowledge",
  "source",
  "task",
  "area",
  "person",
  "daily",
  "discard",
] as const;

export interface Destination {
  to: (typeof DESTINATIONS)[number];
  /** Non-null exactly when `to` is "task". */
  project: string | null;
}
export interface CaptureSuggestion extends Destination {
  why: string | null;
  at: string;
}
export interface Capture {
  ref: string;
  revision: string | null;
  modified: string;
  captured: string | null;
  preview: string | null;
  cut: boolean;
  unread: string | null;
  /** Present only when bound to this capture's current `revision`. */
  suggestion: CaptureSuggestion | null;
}
const RUN_STATES = ["running", "done", "failed"] as const;
export interface SuggestionRun {
  run_id: string;
  session_id: string;
  state: (typeof RUN_STATES)[number];
  started_at: string;
  finished_at: string | null;
  /** One of a closed set of run reasons (CaptureTriage has a sentence for each), or null. */
  reason: string | null;
  suggested: number;
  rejected: number;
}
export interface CaptureListing {
  captures: Capture[];
  projects: string[];
  partial: boolean;
  omitted: number;
  run: SuggestionRun | null;
}

/** The server's error code and its `details` (ids, limits), or a local code. */
export class CapturesApiError extends Error {
  constructor(
    readonly code: string,
    readonly extra: Record<string, unknown> = {},
  ) {
    super(code);
  }
}

const record = (value: unknown): Record<string, unknown> | null =>
  value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
const isString = (value: unknown): value is string => typeof value === "string";
// The server cuts these fields in Unicode code points, not UTF-16 units: count the same way, or
// one astral character (a surrogate pair, two units but one code point) fails a value the server
// considers exactly at the cap.
const capped = (value: unknown, max: number): boolean =>
  isString(value) && [...value].length <= max;
const nullableCapped = (value: unknown, max: number): boolean =>
  value === null || capped(value, max);
const stamp = (value: unknown): boolean => isString(value) && STAMP.test(value);
const nullableStamp = (value: unknown): boolean => value === null || stamp(value);
const hex64 = (value: unknown): boolean => isString(value) && HEX64.test(value);
const uuid = (value: unknown): boolean => isString(value) && UUID.test(value);
const oneOf = (value: unknown, allowed: readonly string[]): boolean =>
  isString(value) && allowed.includes(value);
const nonNegInt = (value: unknown): boolean =>
  typeof value === "number" && Number.isInteger(value) && value >= 0;

function captureSuggestion(value: unknown): value is CaptureSuggestion {
  const item = record(value);
  if (!item || !oneOf(item.to, DESTINATIONS)) return false;
  const isTask = item.to === "task";
  return (
    (isTask ? isString(item.project) : item.project === null) &&
    nullableCapped(item.why, WHY_MAX) &&
    stamp(item.at)
  );
}
function captureItem(value: unknown): value is Capture {
  const item = record(value);
  return (
    !!item &&
    isString(item.ref) &&
    item.ref.length >= 1 &&
    capped(item.ref, REF_MAX) &&
    (item.revision === null || hex64(item.revision)) &&
    stamp(item.modified) &&
    nullableCapped(item.captured, SHORT_MAX) &&
    nullableCapped(item.preview, PREVIEW_MAX) &&
    typeof item.cut === "boolean" &&
    nullableCapped(item.unread, SHORT_MAX) &&
    (item.suggestion === null || captureSuggestion(item.suggestion))
  );
}
function suggestionRun(value: unknown): value is SuggestionRun {
  const item = record(value);
  return (
    !!item &&
    isString(item.run_id) &&
    uuid(item.session_id) &&
    oneOf(item.state, RUN_STATES) &&
    stamp(item.started_at) &&
    nullableStamp(item.finished_at) &&
    nullableCapped(item.reason, SHORT_MAX) &&
    nonNegInt(item.suggested) &&
    nonNegInt(item.rejected)
  );
}
function captureListing(value: unknown): value is CaptureListing {
  const item = record(value);
  return (
    !!item &&
    Array.isArray(item.captures) &&
    item.captures.length <= CAPTURES_MAX &&
    item.captures.every(captureItem) &&
    Array.isArray(item.projects) &&
    item.projects.every(isString) &&
    typeof item.partial === "boolean" &&
    nonNegInt(item.omitted) &&
    (item.run === null || suggestionRun(item.run))
  );
}

export interface CapturesApi {
  list(id: string, signal?: AbortSignal): Promise<CaptureListing>;
  run(id: string, signal?: AbortSignal): Promise<SuggestionRun | null>;
  suggest(id: string): Promise<SuggestionRun>;
}

export function createCapturesApi(token: string, fetchImpl: typeof fetch = fetch): CapturesApi {
  // No custom Origin: the browser sets it, and the server requires one on a POST.
  async function call(
    route: string,
    body?: unknown,
    signal?: AbortSignal,
  ): Promise<Record<string, unknown>> {
    if (signal?.aborted) throw new CapturesApiError("cancelled");
    const controller = new AbortController();
    const abort = () => controller.abort();
    signal?.addEventListener("abort", abort, { once: true });
    const timer = setTimeout(abort, TIMEOUT);
    try {
      const response = await fetchImpl(route, {
        method: body === undefined ? "GET" : "POST",
        signal: controller.signal,
        headers: {
          authorization: `Bearer ${token}`,
          ...(body === undefined ? {} : { "content-type": "application/json" }),
        },
        ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      });
      let parsed: Record<string, unknown> | null;
      try {
        parsed = record(await boundedJson(response, MAX));
      } catch {
        if (response.ok) throw new CapturesApiError("invalid_response");
        throw new CapturesApiError("unavailable");
      }
      if (!response.ok) {
        const error = record(parsed?.error);
        throw new CapturesApiError(
          error && isString(error.code) ? error.code : "unavailable",
          record(error?.details) ?? {},
        );
      }
      if (!parsed) throw new CapturesApiError("invalid_response");
      return parsed;
    } finally {
      clearTimeout(timer);
      signal?.removeEventListener("abort", abort);
    }
  }
  const invalid = (): never => {
    throw new CapturesApiError("invalid_response");
  };
  const path = (id: string) => encodeURIComponent(id);
  return {
    async list(id, signal) {
      const body = await call(`/api/v2/vaults/${path(id)}/captures`, undefined, signal);
      return captureListing(body) ? body : invalid();
    },
    async run(id, signal) {
      const body = await call(`/api/v2/vaults/${path(id)}/captures/suggest`, undefined, signal);
      const found = body.run;
      if (found === null) return null;
      return suggestionRun(found) ? found : invalid();
    },
    async suggest(id) {
      const body = await call(`/api/v2/vaults/${path(id)}/captures/suggest`, {});
      const found = body.run;
      return suggestionRun(found) ? found : invalid();
    },
  };
}
