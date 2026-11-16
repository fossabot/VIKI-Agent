import { boundedJson } from "../boundedResponse";

// The wire format is the server's checks routes (`/api/v2/code/{id}/checks`,
// `/api/v2/checks/{id}/...`, `/api/v2/check-runs/{id}/...`).
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const OID = /^(?:[0-9a-f]{40}|[0-9a-f]{64})$/;
const MIB = 1024 * 1024;
const LIST_MAX = 4 * MIB;
const OUTPUT_MAX = 2 * MIB;
const SMALL_MAX = 256 * 1024;
const TIMEOUT = 15_000;
const RUN_TIMEOUT = 60_000;
const TIME_LENGTH = 24;
const OUTCOMES = ["running", "passed", "failed", "cancelled", "timed_out", "interrupted"] as const;

export type CheckOutcome = (typeof OUTCOMES)[number];
export interface CheckRunJson {
  run_id: string;
  check_id: string;
  workspace_id: string;
  worktree_id: string | null;
  head_oid: string | null;
  /** Null exactly when `head_oid` is. */
  dirty: boolean | null;
  outcome: CheckOutcome;
  exit_code: number | null;
  duration_ms: number | null;
  output_bytes: number | null;
  started_at: string;
  ended_at: string | null;
}
export interface CodeCheckJson {
  check_id: string;
  /** Null when the check is not readable. */
  name: string | null;
  command: string | null;
  timeout_s: number;
  created_at: string;
  readable: boolean;
  last_run: CheckRunJson | null;
}
export interface CheckOutputJson {
  run: CheckRunJson;
  start: number;
  next: number;
  text: string;
  dropped: boolean;
  done: boolean;
}
export interface NewCheckBody {
  name: string;
  command: string;
  timeout_s: number;
}
/** The server's typed code, or "unavailable" when the answer carries none. */
export class ChecksApiError extends Error {
  constructor(readonly code: string) {
    super(code);
  }
}
export interface ChecksApi {
  list(
    workspaceId: string,
    worktreeId: string | null,
    signal?: AbortSignal,
  ): Promise<CodeCheckJson[]>;
  add(workspaceId: string, body: NewCheckBody, signal?: AbortSignal): Promise<CodeCheckJson>;
  remove(checkId: string, signal?: AbortSignal): Promise<void>;
  run(checkId: string, worktreeId: string | null, signal?: AbortSignal): Promise<CheckRunJson>;
  runs(checkId: string, signal?: AbortSignal): Promise<CheckRunJson[]>;
  cancel(runId: string, signal?: AbortSignal): Promise<CheckRunJson>;
  output(runId: string, after: number, signal?: AbortSignal): Promise<CheckOutputJson>;
}

const record = (value: unknown): Record<string, unknown> | null =>
  value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
const str = (value: unknown): value is string => typeof value === "string";
const uuid = (value: unknown): value is string => str(value) && UUID.test(value);
const oid = (value: unknown): value is string => str(value) && OID.test(value);
const time = (value: unknown): value is string => str(value) && value.length === TIME_LENGTH;
const count = (value: unknown): value is number =>
  Number.isSafeInteger(value) && (value as number) >= 0;
const orNull = <T>(check: (value: unknown) => value is T, value: unknown): value is T | null =>
  value === null || check(value);
const unavailable = () => new ChecksApiError("unavailable");

function parseRun(value: unknown): CheckRunJson {
  const row = record(value);
  if (
    !row ||
    !uuid(row.run_id) ||
    !uuid(row.check_id) ||
    !uuid(row.workspace_id) ||
    !orNull(uuid, row.worktree_id) ||
    !orNull(oid, row.head_oid) ||
    !(OUTCOMES as readonly unknown[]).includes(row.outcome) ||
    !(row.exit_code === null || Number.isSafeInteger(row.exit_code)) ||
    !orNull(count, row.duration_ms) ||
    !orNull(count, row.output_bytes) ||
    !time(row.started_at) ||
    !orNull(time, row.ended_at)
  )
    throw unavailable();
  if (row.head_oid === null ? row.dirty !== null : typeof row.dirty !== "boolean")
    throw unavailable();
  return {
    run_id: row.run_id,
    check_id: row.check_id,
    workspace_id: row.workspace_id,
    worktree_id: row.worktree_id,
    head_oid: row.head_oid,
    dirty: row.dirty as boolean | null,
    outcome: row.outcome as CheckOutcome,
    exit_code: row.exit_code as number | null,
    duration_ms: row.duration_ms,
    output_bytes: row.output_bytes,
    started_at: row.started_at,
    ended_at: row.ended_at,
  };
}

function parseCheck(value: unknown): CodeCheckJson {
  const row = record(value);
  if (
    !row ||
    !uuid(row.check_id) ||
    typeof row.readable !== "boolean" ||
    !Number.isSafeInteger(row.timeout_s) ||
    (row.timeout_s as number) < 60 ||
    (row.timeout_s as number) > 3600 ||
    !time(row.created_at)
  )
    throw unavailable();
  if (
    row.readable ? !str(row.name) || !str(row.command) : row.name !== null || row.command !== null
  )
    throw unavailable();
  return {
    check_id: row.check_id,
    name: row.name as string | null,
    command: row.command as string | null,
    timeout_s: row.timeout_s as number,
    created_at: row.created_at,
    readable: row.readable,
    last_run: row.last_run === null ? null : parseRun(row.last_run),
  };
}

function parseOutput(row: Record<string, unknown>): CheckOutputJson {
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
    run: parseRun(row.run),
    start: row.start,
    next: row.next,
    text: row.text,
    dropped: row.dropped,
    done: row.done,
  };
}

export function createChecksApi(token: string, fetchImpl: typeof fetch = fetch): ChecksApi {
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
      throw error && str(error.code) && error.code ? new ChecksApiError(error.code) : unavailable();
    } finally {
      clearTimeout(timer);
      signal?.removeEventListener("abort", abort);
    }
  }
  const needId = (id: string) => {
    if (!uuid(id)) throw new ChecksApiError("invalid_workspace");
  };
  const list = <T>(answer: Record<string, unknown>, key: string, parse: (v: unknown) => T) => {
    const items = answer[key];
    if (!Array.isArray(items)) throw unavailable();
    return items.map(parse);
  };
  const post = (url: string, body: unknown, signal?: AbortSignal, timeout = TIMEOUT) =>
    call("POST", url, body, timeout, SMALL_MAX, signal);
  const code = (workspaceId: string) => `/api/v2/code/${workspaceId}/checks`;
  return {
    async list(workspaceId, worktreeId, signal) {
      needId(workspaceId);
      const query = new URLSearchParams();
      if (worktreeId !== null) {
        needId(worktreeId);
        query.set("worktree", worktreeId);
      }
      const suffix = query.size ? `?${query}` : "";
      const answer = await call(
        "GET",
        `${code(workspaceId)}${suffix}`,
        null,
        TIMEOUT,
        LIST_MAX,
        signal,
      );
      return list(answer, "checks", parseCheck);
    },
    async add(workspaceId, body, signal) {
      needId(workspaceId);
      return parseCheck((await post(code(workspaceId), body, signal)).check);
    },
    async remove(checkId, signal) {
      needId(checkId);
      const answer = await post(`/api/v2/checks/${checkId}/delete`, {}, signal);
      if (answer.deleted !== true) throw unavailable();
    },
    async run(checkId, worktreeId, signal) {
      needId(checkId);
      if (worktreeId !== null) needId(worktreeId);
      const answer = await post(
        `/api/v2/checks/${checkId}/runs`,
        { worktree_id: worktreeId },
        signal,
        RUN_TIMEOUT,
      );
      return parseRun(answer.run);
    },
    async runs(checkId, signal) {
      needId(checkId);
      const answer = await call(
        "GET",
        `/api/v2/checks/${checkId}/runs`,
        null,
        TIMEOUT,
        SMALL_MAX,
        signal,
      );
      return list(answer, "runs", parseRun);
    },
    async cancel(runId, signal) {
      needId(runId);
      return parseRun((await post(`/api/v2/check-runs/${runId}/cancel`, {}, signal)).run);
    },
    async output(runId, after, signal) {
      needId(runId);
      if (!count(after)) throw new ChecksApiError("invalid_query");
      const answer = await call(
        "GET",
        `/api/v2/check-runs/${runId}/output?after=${after}`,
        null,
        TIMEOUT,
        OUTPUT_MAX,
        signal,
      );
      return parseOutput(answer);
    },
  };
}
