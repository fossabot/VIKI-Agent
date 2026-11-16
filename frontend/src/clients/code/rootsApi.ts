import { boundedJson } from "../boundedResponse";

// The wire format is the server's session-root and worktree routes (`/api/v2/session-roots`,
// `/api/v2/worktrees`, `/api/v2/code/{id}/worktrees`).
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const OID = /^(?:[0-9a-f]{40}|[0-9a-f]{64})$/;
const TIMEOUT = 15_000;
const CREATE_TIMEOUT = 150_000;
const REMOVE_TIMEOUT = 90_000;
const CLEANUP_TIMEOUT = 120_000;
const MAX = 1024 * 1024;
const STATES = ["creating", "ready", "removing", "removed", "kept"] as const;
const OUTCOMES = ["removed", "kept"] as const;
const REASONS = ["dirty", "git_refused", "unknown_state"] as const;
const BOT_KINDS = ["session", "task", "thread"] as const;
const FINGERPRINT = /^[0-9a-f]{64}$/;
// The cleanup policy's own action and reason codes (`worktree_rules.py::REASONS`, same order),
// distinct from `REASONS` above (remove's three outcome reasons).
const ACTIONS = ["remove", "archive", "keep"] as const;
const DECISION_REASONS = [
  "unknown",
  "untrusted",
  "live",
  "bot_chat",
  "locked",
  "missing",
  "recent",
  "submodule",
  "embedded_repo",
  "worktree_refs",
  "hidden_changes",
  "in_progress",
  "branch_changed",
  "merged",
  "filtered",
  "large_untracked",
  "dirty",
  "unpushed",
  "busy",
  "changed",
  "in_use",
  "git_refused",
] as const;

export type WorktreeState = (typeof STATES)[number];
export interface SessionRootJson {
  session_id: string;
  workspace_id: string;
  worktree_id: string | null;
  readonly additional_workspace_ids?: readonly string[];
}
/** A Bot's code copy as the worktree list describes it; a null name or title is unknown. */
export interface BotCopyJson {
  kind: (typeof BOT_KINDS)[number];
  bot_name: string | null;
  title: string | null;
  released: boolean;
}
export interface WorktreeJson {
  worktree_id: string;
  workspace_id: string;
  branch: string;
  base_oid: string;
  path: string;
  state: WorktreeState;
  created_at: string;
  updated_at: string;
  /** Absent when no Bot uses the copy, or when the server describes none. */
  bot_copy?: BotCopyJson;
}
export interface RemovalJson {
  worktree: WorktreeJson;
  outcome: (typeof OUTCOMES)[number];
  reason: (typeof REASONS)[number] | null;
  changes: number | null;
}
export interface ArchiveJson {
  worktree_id: string;
  oid: string;
  archived_at: string;
}
export interface DecisionJson {
  worktree_id: string;
  action: (typeof ACTIONS)[number];
  reason: (typeof DECISION_REASONS)[number];
  idle_since: string | null;
  eligible_at: string | null;
  bytes: number | null;
}
/** The server's typed code, or "unavailable" when the answer carries none. */
export class RootsApiError extends Error {
  constructor(readonly code: string) {
    super(code);
  }
}
export interface RootsApi {
  roots(signal?: AbortSignal): Promise<{ roots: SessionRootJson[]; truncated: boolean }>;
  record(root: SessionRootJson, signal?: AbortSignal): Promise<SessionRootJson>;
  worktrees(signal?: AbortSignal): Promise<{ worktrees: WorktreeJson[]; truncated: boolean }>;
  create(
    workspaceId: string,
    body: { policy_revision: number; root_physical_id: string; branch: string },
    signal?: AbortSignal,
  ): Promise<WorktreeJson>;
  remove(
    worktreeId: string,
    body: { policy_revision: number; root_physical_id: string },
    signal?: AbortSignal,
  ): Promise<RemovalJson>;
  archives(
    workspaceId: string,
    signal?: AbortSignal,
  ): Promise<{ enabled: boolean; archives: ArchiveJson[] }>;
  cleanup(workspaceId: string, signal?: AbortSignal): Promise<DecisionJson[]>;
  restore(
    worktreeId: string,
    body: { policy_revision: number; root_physical_id: string },
    signal?: AbortSignal,
  ): Promise<WorktreeJson>;
  /** The folder's saved Git-settings fingerprint; null when none is saved. Optional so older
   * fakes stay valid: without it the Worktrees tab cannot offer to confirm settings. */
  gitSettings?(workspaceId: string, signal?: AbortSignal): Promise<string | null>;
  /** Replaces `expected_fingerprint` with the folder's current settings, if it is still saved. */
  confirmGitSettings?(
    workspaceId: string,
    body: { policy_revision: number; root_physical_id: string; expected_fingerprint: string },
    signal?: AbortSignal,
  ): Promise<string>;
}

const record = (value: unknown): Record<string, unknown> | null =>
  value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
const str = (value: unknown): value is string => typeof value === "string";
const uuid = (value: unknown): value is string => str(value) && UUID.test(value);
const oneOf = <T extends string>(set: readonly T[], value: unknown): value is T =>
  set.includes(value as T);
const unavailable = () => new RootsApiError("unavailable");

function parseRoot(value: unknown): SessionRootJson {
  const row = record(value);
  if (!row || !uuid(row.session_id) || !uuid(row.workspace_id)) throw unavailable();
  if (row.worktree_id !== null && !uuid(row.worktree_id)) throw unavailable();
  const additional = row.additional_workspace_ids;
  if (
    additional !== undefined &&
    (!Array.isArray(additional) ||
      additional.length > 7 ||
      [...additional].some((id) => !uuid(id) || id === row.workspace_id) ||
      new Set(additional).size !== additional.length ||
      (additional.length > 0 && row.worktree_id !== null))
  )
    throw unavailable();
  return {
    session_id: row.session_id,
    workspace_id: row.workspace_id,
    worktree_id: row.worktree_id,
    ...(additional?.length ? { additional_workspace_ids: [...additional] } : {}),
  };
}

function parseWorktree(value: unknown): WorktreeJson {
  const row = record(value);
  if (
    !row ||
    !uuid(row.worktree_id) ||
    !uuid(row.workspace_id) ||
    !str(row.branch) ||
    !str(row.base_oid) ||
    !OID.test(row.base_oid) ||
    !str(row.path) ||
    !oneOf(STATES, row.state) ||
    !str(row.created_at) ||
    !str(row.updated_at)
  )
    throw unavailable();
  return {
    worktree_id: row.worktree_id,
    workspace_id: row.workspace_id,
    branch: row.branch,
    base_oid: row.base_oid,
    path: row.path,
    state: row.state,
    created_at: row.created_at,
    updated_at: row.updated_at,
    ...(row.bot_copy === undefined ? {} : { bot_copy: parseBotCopy(row.bot_copy) }),
  };
}

function parseBotCopy(value: unknown): BotCopyJson {
  const row = record(value);
  const label = (text: unknown) => text === null || str(text);
  if (
    !row ||
    !oneOf(BOT_KINDS, row.kind) ||
    !label(row.bot_name) ||
    !label(row.title) ||
    typeof row.released !== "boolean"
  )
    throw unavailable();
  return {
    kind: row.kind,
    bot_name: row.bot_name as string | null,
    title: row.title as string | null,
    released: row.released,
  };
}

function parseArchive(value: unknown): ArchiveJson {
  const row = record(value);
  if (
    !row ||
    !uuid(row.worktree_id) ||
    !str(row.oid) ||
    !OID.test(row.oid) ||
    !str(row.archived_at)
  )
    throw unavailable();
  return { worktree_id: row.worktree_id, oid: row.oid, archived_at: row.archived_at };
}

function parseDecision(value: unknown): DecisionJson {
  const row = record(value);
  if (!row || !uuid(row.worktree_id)) throw unavailable();
  const { action, reason, idle_since, eligible_at, bytes } = row;
  if (
    !oneOf(ACTIONS, action) ||
    !oneOf(DECISION_REASONS, reason) ||
    (idle_since !== null && !str(idle_since)) ||
    (eligible_at !== null && !str(eligible_at)) ||
    (bytes !== null && !(Number.isSafeInteger(bytes) && (bytes as number) >= 0))
  )
    throw unavailable();
  return {
    worktree_id: row.worktree_id,
    action,
    reason,
    idle_since: idle_since as string | null,
    eligible_at: eligible_at as string | null,
    bytes: bytes as number | null,
  };
}

function parseList<T>(
  body: Record<string, unknown>,
  key: string,
  parse: (value: unknown) => T,
): { items: T[]; truncated: boolean } {
  const rows = body[key];
  if (!Array.isArray(rows) || typeof body.truncated !== "boolean") throw unavailable();
  return { items: rows.map(parse), truncated: body.truncated };
}

export function createRootsApi(token: string, fetchImpl: typeof fetch = fetch): RootsApi {
  async function call(
    method: "GET" | "POST",
    url: string,
    body: unknown,
    timeout: number,
    signal?: AbortSignal,
    maximum = MAX,
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
      const answer = record(await boundedJson(response, maximum).catch(() => null));
      if (response.ok && answer) return answer;
      // A refusal carries {error: {code, message}}; the code alone is acted on.
      const error = record(answer?.error);
      throw error && str(error.code) && error.code ? new RootsApiError(error.code) : unavailable();
    } finally {
      clearTimeout(timer);
      signal?.removeEventListener("abort", abort);
    }
  }
  const needId = (id: string) => {
    if (!uuid(id)) throw new RootsApiError("invalid_workspace");
  };
  return {
    async roots(signal) {
      const { items, truncated } = parseList(
        await call("GET", "/api/v2/session-roots", null, TIMEOUT, signal, 2 * MAX),
        "roots",
        parseRoot,
      );
      return { roots: items, truncated };
    },
    async record(root, signal) {
      let body: SessionRootJson;
      try {
        body = parseRoot(root);
      } catch {
        throw new RootsApiError("invalid_workspace");
      }
      return parseRoot((await call("POST", "/api/v2/session-roots", body, TIMEOUT, signal)).root);
    },
    async worktrees(signal) {
      const { items, truncated } = parseList(
        await call("GET", "/api/v2/worktrees", null, TIMEOUT, signal),
        "worktrees",
        parseWorktree,
      );
      return { worktrees: items, truncated };
    },
    async create(workspaceId, body, signal) {
      needId(workspaceId);
      const answer = await call(
        "POST",
        `/api/v2/code/${workspaceId}/worktrees`,
        body,
        CREATE_TIMEOUT,
        signal,
      );
      return parseWorktree(answer.worktree);
    },
    async remove(worktreeId, body, signal) {
      needId(worktreeId);
      const answer = await call(
        "POST",
        `/api/v2/worktrees/${worktreeId}/remove`,
        body,
        REMOVE_TIMEOUT,
        signal,
      );
      const { outcome, reason, changes } = answer;
      if (
        !oneOf(OUTCOMES, outcome) ||
        (reason !== null && !oneOf(REASONS, reason)) ||
        (changes !== null && !(Number.isSafeInteger(changes) && (changes as number) >= 0))
      )
        throw unavailable();
      return {
        worktree: parseWorktree(answer.worktree),
        outcome,
        reason: reason as RemovalJson["reason"],
        changes: changes as number | null,
      };
    },
    async archives(workspaceId, signal) {
      needId(workspaceId);
      const answer = await call(
        "GET",
        `/api/v2/code/${workspaceId}/worktree-archives`,
        null,
        TIMEOUT,
        signal,
      );
      if (typeof answer.enabled !== "boolean" || !Array.isArray(answer.archives))
        throw unavailable();
      return { enabled: answer.enabled, archives: answer.archives.map(parseArchive) };
    },
    async cleanup(workspaceId, signal) {
      needId(workspaceId);
      const answer = await call(
        "GET",
        `/api/v2/code/${workspaceId}/worktree-cleanup`,
        null,
        CLEANUP_TIMEOUT,
        signal,
      );
      if (!Array.isArray(answer.decisions)) throw unavailable();
      return answer.decisions.map(parseDecision);
    },
    async restore(worktreeId, body, signal) {
      needId(worktreeId);
      const answer = await call(
        "POST",
        `/api/v2/worktrees/${worktreeId}/restore`,
        body,
        CREATE_TIMEOUT,
        signal,
      );
      return parseWorktree(answer.worktree);
    },
    async gitSettings(workspaceId, signal) {
      needId(workspaceId);
      const { fingerprint } = await call(
        "GET",
        `/api/v2/code/${workspaceId}/git-settings`,
        null,
        TIMEOUT,
        signal,
      );
      if (fingerprint === null) return null;
      if (!str(fingerprint) || !FINGERPRINT.test(fingerprint)) throw unavailable();
      return fingerprint;
    },
    async confirmGitSettings(workspaceId, body, signal) {
      needId(workspaceId);
      if (!FINGERPRINT.test(body.expected_fingerprint)) throw new RootsApiError("invalid_body");
      const { fingerprint } = await call(
        "POST",
        `/api/v2/code/${workspaceId}/git-settings`,
        {
          policy_revision: body.policy_revision,
          root_physical_id: body.root_physical_id,
          expected_fingerprint: body.expected_fingerprint,
        },
        CREATE_TIMEOUT,
        signal,
      );
      if (!str(fingerprint) || !FINGERPRINT.test(fingerprint)) throw unavailable();
      return fingerprint;
    },
  };
}
