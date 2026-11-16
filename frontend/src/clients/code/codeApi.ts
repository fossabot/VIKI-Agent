import { boundedJson } from "../boundedResponse";

// The wire format is the server's Code routes (`/api/v2/code/{id}/...`).
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const SHA256 = /^[0-9a-f]{64}$/;
const HEAD = /^[0-9a-f]{12}$/;
const TIMEOUT = 15_000;
const MIB = 1024 * 1024;
const TREE_MAX = MIB;
const FILE_MAX = 8 * MIB;
const SEARCH_MAX = 256 * 1024;
const CHANGES_MAX = 4 * MIB;
const DIFF_MAX = 16 * MIB;
const SAVE_MAX = 64 * 1024;
const SAVED = ["saved", "no_change"] as const;
const ENTRY_TYPES = ["file", "dir", "link", "other"] as const;
const ENCODINGS = ["utf-8", "utf-8-bom", "utf-16-le", "utf-16-be"] as const;
const NEWLINES = ["lf", "crlf", "cr", "mixed", "none"] as const;
const FILE_REASONS = ["binary", "unsupported_encoding", "too_large"] as const;
const STATES = ["ready", "unknown"] as const;
const STATUS_CODES = ["M", "T", "A", "D", "R", "C", "U", ".", "?"] as const;
const CHANGE_KINDS = ["tracked", "untracked", "unmerged", "submodule"] as const;
const DIFF_REASONS = ["binary", "too_large", "no_commits", "no_diff"] as const;

export type TreeEntryType = (typeof ENTRY_TYPES)[number];
export interface TreeEntry {
  name: string;
  type: TreeEntryType;
  size: number | null;
}
export interface CodeTree {
  path: string;
  entries: TreeEntry[];
  truncated: boolean;
}
export interface CodeFile {
  path: string;
  size: number;
  sha256: string | null;
  encoding: (typeof ENCODINGS)[number] | null;
  newline: (typeof NEWLINES)[number] | null;
  text: string | null;
  reason: (typeof FILE_REASONS)[number] | null;
}
export interface CodeSearch {
  hits: string[];
  truncated: boolean;
}
export type ChangeStatus = (typeof STATUS_CODES)[number];
export interface ChangeEntry {
  path: string;
  orig_path: string | null;
  index: ChangeStatus;
  worktree: ChangeStatus;
  kind: (typeof CHANGE_KINDS)[number];
  private: boolean;
}
export interface CodeChanges {
  state: (typeof STATES)[number];
  branch: string | null;
  head: string | null;
  detached: boolean;
  upstream: string | null;
  ahead: number | null;
  behind: number | null;
  entries: ChangeEntry[];
  truncated: boolean;
}
export interface CodeDiff {
  path: string;
  text: string | null;
  reason: (typeof DIFF_REASONS)[number] | null;
}
/** The server's typed code, or "unavailable" when the answer carries none. */
export class CodeApiError extends Error {
  constructor(
    readonly code: string,
    readonly operationId: string | null = null,
  ) {
    super(code);
  }
}
export interface CodeSaveBody {
  policy_revision: number;
  root_physical_id: string;
  content: string;
  expected_sha256: string | null;
  encoding: (typeof ENCODINGS)[number];
  newline: "lf" | "crlf" | "cr" | "none";
}
export interface CodeSaveResult {
  operation_id: string;
  status: "saved" | "no_change" | "conflict";
  sha256: string | null;
  cleanup_pending: boolean;
}
export interface CodeApi {
  tree(workspaceId: string, path: string, signal?: AbortSignal): Promise<CodeTree>;
  file(workspaceId: string, path: string, signal?: AbortSignal): Promise<CodeFile>;
  search(workspaceId: string, q: string, signal?: AbortSignal): Promise<CodeSearch>;
  changes(workspaceId: string, signal?: AbortSignal): Promise<CodeChanges>;
  diff(workspaceId: string, path: string, signal?: AbortSignal): Promise<CodeDiff>;
  save(
    workspaceId: string,
    path: string,
    body: CodeSaveBody,
    signal?: AbortSignal,
  ): Promise<CodeSaveResult>;
}

const record = (value: unknown): Record<string, unknown> | null =>
  value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
const uuid = (value: unknown): value is string => typeof value === "string" && UUID.test(value);
const count = (value: unknown): value is number =>
  Number.isSafeInteger(value) && (value as number) >= 0;
const str = (value: unknown): value is string => typeof value === "string";
const nullable = (check: (value: unknown) => boolean, value: unknown): boolean =>
  value === null || check(value);
const oneOf = <T extends string>(set: readonly T[], value: unknown): value is T =>
  set.includes(value as T);
const unavailable = () => new CodeApiError("unavailable");

export function createCodeApi(token: string, fetchImpl: typeof fetch = fetch): CodeApi {
  // A refusal carries {error: {code, message[, operation_id]}}; the code alone is acted on.
  function refusalOf(body: Record<string, unknown> | null): CodeApiError {
    const error = record(body?.error);
    if (!error || !str(error.code) || !error.code) return unavailable();
    return new CodeApiError(error.code, uuid(error.operation_id) ? error.operation_id : null);
  }
  async function get(
    workspaceId: string,
    route: string,
    query: Record<string, string>,
    maximum: number,
    signal?: AbortSignal,
  ): Promise<Record<string, unknown>> {
    if (!uuid(workspaceId)) throw new CodeApiError("invalid_workspace");
    if (signal?.aborted) throw new Error("cancelled");
    const controller = new AbortController();
    const abort = () => controller.abort();
    signal?.addEventListener("abort", abort, { once: true });
    const timer = setTimeout(abort, TIMEOUT);
    const search = Object.entries(query)
      .map(([key, value]) => `${key}=${encodeURIComponent(value)}`)
      .join("&");
    try {
      const response = await fetchImpl(
        `/api/v2/code/${workspaceId}/${route}${search ? `?${search}` : ""}`,
        {
          method: "GET",
          signal: controller.signal,
          headers: { authorization: `Bearer ${token}` },
        },
      );
      const body = record(await boundedJson(response, maximum).catch(() => null));
      if (response.ok && body) return body;
      throw refusalOf(body);
    } finally {
      clearTimeout(timer);
      signal?.removeEventListener("abort", abort);
    }
  }
  return {
    async save(id, path, payload, signal) {
      if (!uuid(id)) throw new CodeApiError("invalid_workspace");
      if (!payload.content.isWellFormed()) throw new CodeApiError("invalid_content");
      if (signal?.aborted) throw new Error("cancelled");
      const controller = new AbortController();
      const abort = () => controller.abort();
      signal?.addEventListener("abort", abort, { once: true });
      const timer = setTimeout(abort, TIMEOUT);
      try {
        const response = await fetchImpl(
          `/api/v2/code/${id}/file?path=${encodeURIComponent(path)}`,
          {
            method: "PUT",
            signal: controller.signal,
            headers: { authorization: `Bearer ${token}`, "content-type": "application/json" },
            body: JSON.stringify(payload),
          },
        );
        const body = record(await boundedJson(response, SAVE_MAX).catch(() => null));
        const conflict = response.status === 409 && body?.status === "conflict";
        if (body && (response.ok || conflict)) {
          if (
            (conflict ? body.sha256 === null : str(body.sha256) && SHA256.test(body.sha256)) &&
            (conflict || oneOf(SAVED, body.status)) &&
            uuid(body.operation_id) &&
            typeof body.cleanup_pending === "boolean"
          )
            return body as unknown as CodeSaveResult;
          throw unavailable();
        }
        throw refusalOf(body);
      } finally {
        clearTimeout(timer);
        signal?.removeEventListener("abort", abort);
      }
    },
    async tree(id, path, signal) {
      const body = await get(id, "tree", { path }, TREE_MAX, signal);
      if (
        !str(body.path) ||
        typeof body.truncated !== "boolean" ||
        !Array.isArray(body.entries) ||
        !body.entries.every((row) => {
          const entry = record(row);
          return (
            !!entry &&
            str(entry.name) &&
            oneOf(ENTRY_TYPES, entry.type) &&
            nullable(count, entry.size)
          );
        })
      )
        throw unavailable();
      return body as unknown as CodeTree;
    },
    async file(id, path, signal) {
      const body = await get(id, "file", { path }, FILE_MAX, signal);
      if (
        !str(body.path) ||
        !count(body.size) ||
        !nullable((v) => str(v) && SHA256.test(v as string), body.sha256) ||
        !nullable((v) => oneOf(ENCODINGS, v), body.encoding) ||
        !nullable((v) => oneOf(NEWLINES, v), body.newline) ||
        !nullable(str, body.text) ||
        !nullable((v) => oneOf(FILE_REASONS, v), body.reason)
      )
        throw unavailable();
      return body as unknown as CodeFile;
    },
    async search(id, q, signal) {
      const body = await get(id, "search", { q }, SEARCH_MAX, signal);
      if (typeof body.truncated !== "boolean" || !Array.isArray(body.hits) || !body.hits.every(str))
        throw unavailable();
      return body as unknown as CodeSearch;
    },
    async changes(id, signal) {
      const body = await get(id, "changes", {}, CHANGES_MAX, signal);
      if (
        !oneOf(STATES, body.state) ||
        !nullable(str, body.branch) ||
        !nullable((v) => str(v) && HEAD.test(v as string), body.head) ||
        typeof body.detached !== "boolean" ||
        !nullable(str, body.upstream) ||
        !nullable(count, body.ahead) ||
        !nullable(count, body.behind) ||
        typeof body.truncated !== "boolean" ||
        !Array.isArray(body.entries) ||
        !body.entries.every((row) => {
          const entry = record(row);
          return (
            !!entry &&
            str(entry.path) &&
            nullable(str, entry.orig_path) &&
            oneOf(STATUS_CODES, entry.index) &&
            oneOf(STATUS_CODES, entry.worktree) &&
            oneOf(CHANGE_KINDS, entry.kind) &&
            typeof entry.private === "boolean"
          );
        })
      )
        throw unavailable();
      return body as unknown as CodeChanges;
    },
    async diff(id, path, signal) {
      const body = await get(id, "diff", { path }, DIFF_MAX, signal);
      if (
        !str(body.path) ||
        !nullable(str, body.text) ||
        !nullable((v) => oneOf(DIFF_REASONS, v), body.reason)
      )
        throw unavailable();
      return body as unknown as CodeDiff;
    },
  };
}
