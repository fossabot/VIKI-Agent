import { boundedJson } from "../boundedResponse";

// The wire format is the server's review and integration routes
// (`/api/v2/code/{id}/review`, `/commit`, `/push`, `/api/v2/worktrees/{id}/integrate`).
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const OID = /^(?:[0-9a-f]{40}|[0-9a-f]{64})$/;
const SHA256 = /^[0-9a-f]{64}$/;
const REMOTE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$/;
const PAGE_URL = /^https:\/\/(github\.com|gitlab\.com)\/\S+$/;
const MIB = 1024 * 1024;
const REVIEW_MAX = 8 * MIB;
const VIEW_MAX = 16 * MIB;
const SMALL_MAX = 64 * 1024;
const REVIEW_TIMEOUT = 60_000;
const VIEW_TIMEOUT = 15_000;
const COMMIT_TIMEOUT = 180_000;
const INTEGRATE_TIMEOUT = 90_000;
const PUSH_TIMEOUT = 180_000;
const CHECK_TIMEOUT = 45_000;
const KINDS = [
  "text",
  "binary",
  "symlink",
  "submodule",
  "nested_repository",
  "filtered",
  "private",
  "too_large",
  "unmerged",
  "unsupported_name",
  "other",
] as const;
const CHANGES = ["added", "modified", "deleted"] as const;
const REMOTE_STATES = ["ready", "none", "unsupported"] as const;
const VIEW_REASONS = [
  "binary",
  "too_large",
  "no_diff",
  "no_commits",
  "unsupported_encoding",
] as const;
const PUSH_OUTCOMES = ["pushed", "unknown"] as const;
const CHECK_OUTCOMES = ["pushed", "not_pushed"] as const;

export type ReviewKind = (typeof KINDS)[number];
export type ReviewChange = (typeof CHANGES)[number];
export interface ReviewEntryJson {
  path: string;
  change: ReviewChange;
  kind: ReviewKind;
  /** SHA-256 of the reviewed bytes: present exactly for `text` entries. */
  digest?: string;
}
export interface IntegrationJson {
  target_branch: string | null;
  target_oid: string;
  merge_base: string | null;
  fast_forward: boolean;
  ahead: number;
  behind: number;
  commits: { oid: string; subject: string }[];
  commits_truncated: boolean;
  paths: { path: string; change: ReviewChange; kind: ReviewKind }[];
  paths_truncated: boolean;
}
export interface ReviewJson {
  workspace_id: string;
  worktree_id: string | null;
  branch: string | null;
  head_oid: string;
  in_progress: boolean;
  entries: ReviewEntryJson[];
  truncated: boolean;
  remote: string | null;
  remote_state: (typeof REMOTE_STATES)[number];
  remote_oid: string | null;
  pull_request_url: string | null;
  integration: IntegrationJson | null;
}
export interface ReviewViewJson {
  path: string;
  diff: string | null;
  content: string | null;
  reason: (typeof VIEW_REASONS)[number] | null;
}
export interface CommitBody {
  policy_revision: number;
  root_physical_id: string;
  worktree_id: string | null;
  branch: string;
  head_oid: string;
  message: string;
  paths: { path: string; digest: string }[];
}
export interface CommitJson {
  outcome: "committed";
  head_oid: string;
  hook_changed: string[];
  unexpected: string[];
}
export interface IntegrateBody {
  policy_revision: number;
  root_physical_id: string;
  target_branch: string;
  target_oid: string;
  head_oid: string;
}
export interface PushBody {
  policy_revision: number;
  root_physical_id: string;
  remote: string;
  branch: string;
  head_oid: string;
}
export interface PushJson {
  outcome: (typeof PUSH_OUTCOMES)[number];
  remote_oid: string | null;
}
export interface CheckBody {
  remote: string;
  branch: string;
  head_oid: string;
}
export interface CheckJson {
  outcome: (typeof CHECK_OUTCOMES)[number];
  remote_oid: string | null;
}
/** The server's typed code, or "unavailable" when the answer carries none. */
export class ReviewApiError extends Error {
  constructor(readonly code: string) {
    super(code);
  }
}
export interface ReviewApi {
  review(workspaceId: string, worktreeId: string | null, signal?: AbortSignal): Promise<ReviewJson>;
  view(
    workspaceId: string,
    path: string,
    options: { worktreeId: string | null; integration: boolean },
    signal?: AbortSignal,
  ): Promise<ReviewViewJson>;
  commit(workspaceId: string, body: CommitBody, signal?: AbortSignal): Promise<CommitJson>;
  integrate(
    worktreeId: string,
    body: IntegrateBody,
    signal?: AbortSignal,
  ): Promise<{ outcome: "integrated"; head_oid: string }>;
  push(workspaceId: string, body: PushBody, signal?: AbortSignal): Promise<PushJson>;
  check(workspaceId: string, body: CheckBody, signal?: AbortSignal): Promise<CheckJson>;
}

const record = (value: unknown): Record<string, unknown> | null =>
  value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
const str = (value: unknown): value is string => typeof value === "string";
const uuid = (value: unknown): value is string => str(value) && UUID.test(value);
const oid = (value: unknown): value is string => str(value) && OID.test(value);
const count = (value: unknown): value is number =>
  Number.isSafeInteger(value) && (value as number) >= 0;
const oneOf = <T extends string>(set: readonly T[], value: unknown): value is T =>
  set.includes(value as T);
const unavailable = () => new ReviewApiError("unavailable");
const orNull = <T>(check: (value: unknown) => value is T, value: unknown): value is T | null =>
  value === null || check(value);
const strings = (value: unknown): value is string[] => Array.isArray(value) && value.every(str);
const remoteName = (value: unknown): value is string => str(value) && REMOTE.test(value);
const pageUrl = (value: unknown): value is string => str(value) && PAGE_URL.test(value);

function parseEntry(value: unknown): ReviewEntryJson {
  const row = record(value);
  if (!row || !str(row.path) || !oneOf(CHANGES, row.change) || !oneOf(KINDS, row.kind))
    throw unavailable();
  if (row.kind === "text") {
    if (!str(row.digest) || !SHA256.test(row.digest)) throw unavailable();
    return { path: row.path, change: row.change, kind: row.kind, digest: row.digest };
  }
  if (row.digest !== undefined && row.digest !== null) throw unavailable();
  return { path: row.path, change: row.change, kind: row.kind };
}

function parseIntegration(value: unknown): IntegrationJson {
  const row = record(value);
  if (
    !row ||
    !orNull(str, row.target_branch) ||
    !oid(row.target_oid) ||
    !orNull(oid, row.merge_base) ||
    typeof row.fast_forward !== "boolean" ||
    !count(row.ahead) ||
    !count(row.behind) ||
    !Array.isArray(row.commits) ||
    typeof row.commits_truncated !== "boolean" ||
    !Array.isArray(row.paths) ||
    typeof row.paths_truncated !== "boolean"
  )
    throw unavailable();
  return {
    target_branch: row.target_branch,
    target_oid: row.target_oid,
    merge_base: row.merge_base,
    fast_forward: row.fast_forward,
    ahead: row.ahead,
    behind: row.behind,
    commits: row.commits.map((item) => {
      const commit = record(item);
      if (!commit || !oid(commit.oid) || !str(commit.subject)) throw unavailable();
      return { oid: commit.oid, subject: commit.subject };
    }),
    commits_truncated: row.commits_truncated,
    paths: row.paths.map((item) => {
      const path = record(item);
      if (!path || !str(path.path) || !oneOf(CHANGES, path.change) || !oneOf(KINDS, path.kind))
        throw unavailable();
      return { path: path.path, change: path.change, kind: path.kind };
    }),
    paths_truncated: row.paths_truncated,
  };
}

function parseReview(row: Record<string, unknown>): ReviewJson {
  if (
    !uuid(row.workspace_id) ||
    !orNull(uuid, row.worktree_id) ||
    !orNull(str, row.branch) ||
    !oid(row.head_oid) ||
    typeof row.in_progress !== "boolean" ||
    !Array.isArray(row.entries) ||
    typeof row.truncated !== "boolean" ||
    !orNull(remoteName, row.remote) ||
    !oneOf(REMOTE_STATES, row.remote_state) ||
    !orNull(oid, row.remote_oid) ||
    !orNull(pageUrl, row.pull_request_url)
  )
    throw unavailable();
  return {
    workspace_id: row.workspace_id,
    worktree_id: row.worktree_id,
    branch: row.branch,
    head_oid: row.head_oid,
    in_progress: row.in_progress,
    entries: row.entries.map(parseEntry),
    truncated: row.truncated,
    remote: row.remote,
    remote_state: row.remote_state,
    remote_oid: row.remote_oid,
    pull_request_url: row.pull_request_url,
    integration: row.integration === null ? null : parseIntegration(row.integration),
  };
}

function parseView(row: Record<string, unknown>): ReviewViewJson {
  if (
    !str(row.path) ||
    !orNull(str, row.diff) ||
    !orNull(str, row.content) ||
    (row.reason !== null && !oneOf(VIEW_REASONS, row.reason))
  )
    throw unavailable();
  return {
    path: row.path,
    diff: row.diff,
    content: row.content,
    reason: row.reason as ReviewViewJson["reason"],
  };
}

function parseCommit(row: Record<string, unknown>): CommitJson {
  if (
    row.outcome !== "committed" ||
    !oid(row.head_oid) ||
    !strings(row.hook_changed) ||
    !strings(row.unexpected)
  )
    throw unavailable();
  return {
    outcome: "committed",
    head_oid: row.head_oid,
    hook_changed: row.hook_changed,
    unexpected: row.unexpected,
  };
}

export function createReviewApi(token: string, fetchImpl: typeof fetch = fetch): ReviewApi {
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
      throw error && str(error.code) && error.code ? new ReviewApiError(error.code) : unavailable();
    } finally {
      clearTimeout(timer);
      signal?.removeEventListener("abort", abort);
    }
  }
  const needId = (id: string) => {
    if (!uuid(id)) throw new ReviewApiError("invalid_workspace");
  };
  const code = (workspaceId: string) => `/api/v2/code/${workspaceId}`;
  const outcome = <T extends string>(
    set: readonly T[],
    answer: Record<string, unknown>,
  ): { outcome: T; remote_oid: string | null } => {
    if (!oneOf(set, answer.outcome) || !orNull(oid, answer.remote_oid)) throw unavailable();
    return { outcome: answer.outcome, remote_oid: answer.remote_oid };
  };
  return {
    async review(workspaceId, worktreeId, signal) {
      needId(workspaceId);
      const query = new URLSearchParams();
      if (worktreeId !== null) {
        needId(worktreeId);
        query.set("worktree", worktreeId);
      }
      const suffix = query.size ? `?${query}` : "";
      const answer = await call(
        "GET",
        `${code(workspaceId)}/review${suffix}`,
        null,
        REVIEW_TIMEOUT,
        REVIEW_MAX,
        signal,
      );
      return parseReview(answer);
    },
    async view(workspaceId, path, options, signal) {
      needId(workspaceId);
      const query = new URLSearchParams({ path });
      if (options.worktreeId !== null) {
        needId(options.worktreeId);
        query.set("worktree", options.worktreeId);
      }
      if (options.integration) query.set("range", "integration");
      const answer = await call(
        "GET",
        `${code(workspaceId)}/review/file?${query}`,
        null,
        VIEW_TIMEOUT,
        VIEW_MAX,
        signal,
      );
      return parseView(answer);
    },
    async commit(workspaceId, body, signal) {
      needId(workspaceId);
      if (body.worktree_id !== null) needId(body.worktree_id);
      const answer = await call(
        "POST",
        `${code(workspaceId)}/commit`,
        body,
        COMMIT_TIMEOUT,
        SMALL_MAX,
        signal,
      );
      return parseCommit(answer);
    },
    async integrate(worktreeId, body, signal) {
      needId(worktreeId);
      const answer = await call(
        "POST",
        `/api/v2/worktrees/${worktreeId}/integrate`,
        body,
        INTEGRATE_TIMEOUT,
        SMALL_MAX,
        signal,
      );
      if (answer.outcome !== "integrated" || !oid(answer.head_oid)) throw unavailable();
      return { outcome: "integrated", head_oid: answer.head_oid };
    },
    async push(workspaceId, body, signal) {
      needId(workspaceId);
      const answer = await call(
        "POST",
        `${code(workspaceId)}/push`,
        body,
        PUSH_TIMEOUT,
        SMALL_MAX,
        signal,
      );
      return outcome(PUSH_OUTCOMES, answer);
    },
    async check(workspaceId, body, signal) {
      needId(workspaceId);
      const answer = await call(
        "POST",
        `${code(workspaceId)}/push/check`,
        body,
        CHECK_TIMEOUT,
        SMALL_MAX,
        signal,
      );
      return outcome(CHECK_OUTCOMES, answer);
    },
  };
}
