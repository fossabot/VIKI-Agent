import { boundedJson } from "../boundedResponse";

// The wire format is the server's previews routes (`/api/v2/code/{id}/previews`,
// `/api/v2/previews/{id}/...`).
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const KIB = 1024;
// The server escapes non-ASCII (an 8,000-character command can take 96,000 bytes) and lists up to
// 6 previews.
const LIST_MAX = KIB * KIB;
const OUTPUT_MAX = 2 * KIB * KIB;
const SMALL_MAX = 128 * KIB;
const TIMEOUT = 15_000;
const LONG_TIMEOUT = 30_000;
const TIME_LENGTH = 24;
const EXIT_MIN = -2_147_483_648;
const EXIT_MAX = 4_294_967_295;
const STATES = ["starting", "listening", "port_taken", "stopped", "exited"] as const;

export type PreviewState = (typeof STATES)[number];
export interface PreviewJson {
  preview_id: string;
  workspace_id: string;
  worktree_id: string | null;
  command: string;
  port: number;
  state: PreviewState;
  /** cmd.exe's pid. */
  pid: number;
  /** Set exactly when `state` is `listening`. */
  listener_pid: number | null;
  /** Set exactly when `state` is `exited`. */
  exit_code: number | null;
  started_at: string;
  /** Null exactly while the preview is live. */
  ended_at: string | null;
}
export interface PreviewOutputJson {
  preview: PreviewJson;
  start: number;
  next: number;
  text: string;
  dropped: boolean;
  done: boolean;
}
export interface NewPreviewBody {
  worktree_id: string | null;
  command: string;
  port: number;
}
/** The server's typed code, or "unavailable" when the answer carries none. */
export class PreviewsApiError extends Error {
  constructor(readonly code: string) {
    super(code);
  }
}
export interface PreviewsApi {
  list(workspaceId: string, signal?: AbortSignal): Promise<PreviewJson[]>;
  start(workspaceId: string, body: NewPreviewBody, signal?: AbortSignal): Promise<PreviewJson>;
  stop(previewId: string, signal?: AbortSignal): Promise<PreviewJson>;
  open(previewId: string, signal?: AbortSignal): Promise<void>;
  remove(previewId: string, signal?: AbortSignal): Promise<void>;
  output(previewId: string, after: number, signal?: AbortSignal): Promise<PreviewOutputJson>;
}

const record = (value: unknown): Record<string, unknown> | null =>
  value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
const str = (value: unknown): value is string => typeof value === "string";
const uuid = (value: unknown): value is string => str(value) && UUID.test(value);
const time = (value: unknown): value is string => str(value) && value.length === TIME_LENGTH;
const count = (value: unknown): value is number =>
  Number.isSafeInteger(value) && (value as number) >= 0;
const positive = (value: unknown): value is number => count(value) && value > 0;
const unavailable = () => new PreviewsApiError("unavailable");

function parsePreview(value: unknown): PreviewJson {
  const row = record(value);
  if (
    !row ||
    !uuid(row.preview_id) ||
    !uuid(row.workspace_id) ||
    !(row.worktree_id === null || uuid(row.worktree_id)) ||
    !str(row.command) ||
    !count(row.port) ||
    row.port < 1024 ||
    row.port > 65535 ||
    !(STATES as readonly unknown[]).includes(row.state) ||
    !positive(row.pid) ||
    !time(row.started_at)
  )
    throw unavailable();
  const state = row.state as PreviewState;
  const live = state === "starting" || state === "listening" || state === "port_taken";
  if (
    (state === "listening" ? !positive(row.listener_pid) : row.listener_pid !== null) ||
    (state === "exited"
      ? !Number.isSafeInteger(row.exit_code) ||
        (row.exit_code as number) < EXIT_MIN ||
        (row.exit_code as number) > EXIT_MAX
      : row.exit_code !== null) ||
    (live ? row.ended_at !== null : !time(row.ended_at))
  )
    throw unavailable();
  return {
    preview_id: row.preview_id,
    workspace_id: row.workspace_id,
    worktree_id: row.worktree_id,
    command: row.command,
    port: row.port,
    state,
    pid: row.pid,
    listener_pid: row.listener_pid as number | null,
    exit_code: row.exit_code as number | null,
    started_at: row.started_at,
    ended_at: row.ended_at as string | null,
  };
}

function parseOutput(row: Record<string, unknown>): PreviewOutputJson {
  if (
    !count(row.start) ||
    !count(row.next) ||
    row.start > row.next ||
    !str(row.text) ||
    typeof row.dropped !== "boolean" ||
    typeof row.done !== "boolean"
  )
    throw unavailable();
  return {
    preview: parsePreview(row.preview),
    start: row.start,
    next: row.next,
    text: row.text,
    dropped: row.dropped,
    done: row.done,
  };
}

export function createPreviewsApi(token: string, fetchImpl: typeof fetch = fetch): PreviewsApi {
  async function call(
    method: "GET" | "POST",
    url: string,
    body: unknown,
    timeout: number,
    max: number,
    signal?: AbortSignal,
  ): Promise<Record<string, unknown>> {
    if (signal?.aborted) throw new Error("cancelled");
    const controller = new AbortController();
    const abort = () => controller.abort();
    signal?.addEventListener("abort", abort, { once: true });
    const timer = setTimeout(abort, timeout);
    const headers: Record<string, string> = { authorization: `Bearer ${token}` };
    if (method === "POST") headers["content-type"] = "application/json";
    try {
      const response = await fetchImpl(url, {
        method,
        signal: controller.signal,
        headers,
        ...(method === "POST" ? { body: JSON.stringify(body) } : {}),
      });
      const answer = record(await boundedJson(response, max).catch(() => null));
      if (response.ok && answer) return answer;
      // A refusal carries {error: {code, message}}; the code alone is acted on.
      const error = record(answer?.error);
      throw error && str(error.code) && error.code
        ? new PreviewsApiError(error.code)
        : unavailable();
    } finally {
      clearTimeout(timer);
      signal?.removeEventListener("abort", abort);
    }
  }
  const needId = (id: string) => {
    if (!uuid(id)) throw new PreviewsApiError("invalid_workspace");
  };
  const post = (url: string, body: unknown, signal?: AbortSignal, timeout = TIMEOUT) =>
    call("POST", url, body, timeout, SMALL_MAX, signal);
  const code = (workspaceId: string) => `/api/v2/code/${workspaceId}/previews`;
  return {
    async list(workspaceId, signal) {
      needId(workspaceId);
      const answer = await call("GET", code(workspaceId), null, TIMEOUT, LIST_MAX, signal);
      if (!Array.isArray(answer.previews)) throw unavailable();
      return answer.previews.map(parsePreview);
    },
    async start(workspaceId, body, signal) {
      needId(workspaceId);
      if (body.worktree_id !== null) needId(body.worktree_id);
      const answer = await post(code(workspaceId), body, signal, LONG_TIMEOUT);
      return parsePreview(answer.preview);
    },
    async stop(previewId, signal) {
      needId(previewId);
      return parsePreview((await post(`/api/v2/previews/${previewId}/stop`, {}, signal)).preview);
    },
    async open(previewId, signal) {
      needId(previewId);
      const answer = await post(`/api/v2/previews/${previewId}/open`, {}, signal, LONG_TIMEOUT);
      if (answer.opened !== true) throw unavailable();
    },
    async remove(previewId, signal) {
      needId(previewId);
      const answer = await post(`/api/v2/previews/${previewId}/remove`, {}, signal);
      if (answer.removed !== true) throw unavailable();
    },
    async output(previewId, after, signal) {
      needId(previewId);
      if (!count(after)) throw new PreviewsApiError("invalid_query");
      const answer = await call(
        "GET",
        `/api/v2/previews/${previewId}/output?after=${after}`,
        null,
        TIMEOUT,
        OUTPUT_MAX,
        signal,
      );
      return parseOutput(answer);
    },
  };
}
